"""Jobs: the actions the work screens start (code, images, video, apps), shown live while they run.

A job is one thing the user asked for on one of those screens: inspect a project, ask Grok for a change,
apply it, run the tests, create an image, look at the screen. Each job carries what JARVIS understood it
was asked (`title`), what it is about to do (`plan`), whether it is a real action or a demonstration
(`real`), what leaves the computer and where to (`sends`), its estimated price (`cost`), its steps with
their timings, and finally its result or a clear error. It can be stopped while it runs and tried again
after it failed or was stopped.

Every change is published on the bus as a `job` event (the bus redacts secrets on the way), so every
open window shows the same state. Finished jobs are kept in `<data>/jobs.jsonl`, the task history the
privacy area can clear, and each job's duration and its steps' durations go to the speed measurements.

The same request twice in a row runs once: while a job runs, asking for the same kind of job with the same
parameters returns the running one instead of starting a second copy.

Stopping is cooperative: `cancel()` sets the job's CancelToken. Tool calls made through the executor with
`cancel=ctx.cancel` stop on it (and end their processes), and anything else a job awaits goes through
`ctx.guard(...)`, which abandons that wait the moment the token is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

from jarvis.bus.message import event
from jarvis.common.cancel import CancelToken
from jarvis.common.clock import utcnow
from jarvis.common.errors import JarvisError, KillSwitchEngaged, TaskCancelled
from jarvis.common.i18n import t
from jarvis.common.ids import new_id

if TYPE_CHECKING:
    from jarvis.bus import MessageBus
    from jarvis.metrics import Metrics

log = logging.getLogger(__name__)

T = TypeVar("T")
JobStatus = Literal["running", "done", "failed", "cancelled"]
StepStatus = Literal["pending", "running", "done", "failed", "skipped"]
Runner = Callable[["JobContext"], Awaitable["dict[str, Any] | None"]]

KEEP = 200                 # jobs kept in memory and in the history file
PROGRESS_EVERY_S = 0.15    # progress events at most this often per job (step changes go out at once)
MAX_RESULT_CHARS = 200_000 # a result bigger than this is not written to the history file


class JobStep(BaseModel):
    key: str
    label: str
    status: StepStatus = "pending"
    detail: str = ""
    ms: int | None = None


class Job(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    kind: str                       # "dev.inspect", "dev.propose", "image.create", "apps.look", …
    area: str                       # the screen it belongs to: "code", "images", "video", "apps"
    title: str                      # what JARVIS understood it was asked to do
    plan: list[str] = Field(default_factory=list)       # what it is about to do
    real: bool = True               # a real action (False: a demonstration, and the UI says so)
    status: JobStatus = "running"
    steps: list[JobStep] = Field(default_factory=list)
    progress: float | None = None   # 0..1, or None while it cannot be measured
    message: str = ""               # the latest progress line
    result: dict[str, Any] | None = None
    error: str | None = None
    sends: list[str] = Field(default_factory=list)      # what leaves the computer, and where to
    cost: str | None = None         # the estimated price, when it costs money
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    ms: int | None = None
    can_retry: bool = False
    retry_of: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)  # what it was started with (for retry and dedupe)
    lang: str = "he"
    keep_result: bool = True        # False: the history keeps only the result's "summary" (code, diffs stay out)

    def view(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class JobContext:
    """What a running job uses to report its steps and progress and to stop when asked."""

    def __init__(self, manager: JobManager, job: Job, cancel: CancelToken) -> None:
        self.job = job
        self.cancel = cancel
        self._manager = manager

    @property
    def lang(self) -> str:
        return self.job.lang

    def t(self, he: str, en: str) -> str:
        return t(self.job.lang, he, en)

    def check(self) -> None:
        """Raise TaskCancelled if the user stopped the job."""
        self.cancel.raise_if_cancelled()

    @contextlib.asynccontextmanager
    async def step(self, key: str, label: str | None = None, detail: str = "") -> AsyncIterator[JobStep]:
        """Run one step: it shows as running, then done (or failed), with how long it took."""
        self.check()
        step = next((s for s in self.job.steps if s.key == key), None)
        if step is None:
            step = JobStep(key=key, label=label or key)
            self.job.steps.append(step)
        elif label:
            step.label = label
        step.status, step.detail, step.ms = "running", detail or step.detail, None
        self.job.message = step.label
        await self._manager.publish(self.job, force=True)
        started = time.perf_counter()
        try:
            yield step
        except BaseException:
            step.status = "failed"
            step.ms = int((time.perf_counter() - started) * 1000)
            raise
        step.ms = int((time.perf_counter() - started) * 1000)
        if step.status == "running":
            step.status = "done"
        self._manager.record(f"{self.job.kind}.{key}", step.ms)
        await self._manager.publish(self.job, force=True)

    async def skip(self, key: str, detail: str = "") -> None:
        for step in self.job.steps:
            if step.key == key:
                step.status, step.detail = "skipped", detail or step.detail
        await self._manager.publish(self.job, force=True)

    async def progress(self, fraction: float | None, message: str = "") -> None:
        self.job.progress = None if fraction is None else max(0.0, min(1.0, float(fraction)))
        if message:
            self.job.message = message
        await self._manager.publish(self.job)

    async def update(self, **fields: Any) -> None:
        """Change what the job shows (its plan, what it sends, its price, real or demo) and publish it."""
        for key, value in fields.items():
            if key in ("title", "plan", "real", "sends", "cost", "message"):
                setattr(self.job, key, value)
        await self._manager.publish(self.job, force=True)

    async def guard(self, awaitable: Awaitable[T]) -> T:
        """Await something that knows nothing about the job's token, abandoning it if the job is stopped."""
        self.check()
        work = asyncio.ensure_future(awaitable)
        stop = asyncio.ensure_future(self.cancel.wait())
        try:
            done, _ = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return work.result()
            work.cancel()
            with contextlib.suppress(BaseException):
                await work
            raise TaskCancelled(self.cancel.reason or "stopped")
        finally:
            stop.cancel()
            if not work.done():
                work.cancel()


def _dedupe_key(kind: str, params: dict[str, Any]) -> str:
    return kind + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)


class JobManager:
    def __init__(self, bus: MessageBus | None, history_file: Path | None = None, *,
                 metrics: Metrics | None = None) -> None:
        self.bus = bus
        self.history_file = history_file
        self.metrics = metrics
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._tokens: dict[str, CancelToken] = {}
        self._starters: dict[str, Callable[[], Awaitable[Job]]] = {}
        self._published: dict[str, float] = {}
        self._load()

    # ------------------------------------------------------------------ starting
    async def start(
        self,
        *,
        kind: str,
        area: str,
        title: str,
        run: Runner,
        plan: Iterable[str] = (),
        steps: Iterable[tuple[str, str]] = (),
        params: dict[str, Any] | None = None,
        real: bool = True,
        sends: Iterable[str] = (),
        cost: str | None = None,
        lang: str = "he",
        dedupe: bool = True,
        keep_result: bool = True,
        retry_of: str | None = None,
    ) -> Job:
        """Start a job in the background and return it at once (its first event is already out)."""
        params = dict(params or {})
        if dedupe:
            key = _dedupe_key(kind, params)
            for running in self._jobs.values():
                if running.status == "running" and _dedupe_key(running.kind, running.params) == key:
                    return running
        job = Job(kind=kind, area=area, title=title, plan=list(plan), real=real, sends=list(sends), cost=cost,
                  steps=[JobStep(key=k, label=label) for k, label in steps], params=params, lang=lang,
                  keep_result=keep_result, retry_of=retry_of)
        token = CancelToken()
        self._jobs[job.id] = job
        self._tokens[job.id] = token
        plan_list, steps_list, sends_list = list(job.plan), [(s.key, s.label) for s in job.steps], list(job.sends)

        async def again() -> Job:
            return await self.start(kind=kind, area=area, title=title, run=run, plan=plan_list, steps=steps_list,
                                    params=params, real=real, sends=sends_list, cost=cost, lang=lang, dedupe=dedupe,
                                    keep_result=keep_result, retry_of=job.id)

        self._starters[job.id] = again
        self._trim()
        await self.publish(job, force=True)
        self._tasks[job.id] = asyncio.create_task(self._run(job, run, token), name=f"jarvis-job-{kind}")
        return job

    async def _run(self, job: Job, run: Runner, token: CancelToken) -> None:
        ctx = JobContext(self, job, token)
        started = time.perf_counter()
        try:
            result = await run(ctx)
            if token.cancelled:
                raise TaskCancelled(token.reason or "stopped")
            job.result = result if result is None or isinstance(result, dict) else {"value": result}
            job.status = "done"
            job.progress = 1.0
        except (TaskCancelled, asyncio.CancelledError) as exc:
            job.status = "cancelled"
            job.message = t(job.lang, "נעצר לפי בקשתך", "Stopped as you asked")
            if isinstance(exc, asyncio.CancelledError) and not token.cancelled:
                job.message = t(job.lang, "נעצר כי JARVIS נסגר", "Stopped because JARVIS closed")
        except KillSwitchEngaged as exc:
            job.status = "cancelled"
            job.message = t(job.lang, "נעצר בעצירת החירום", "Stopped by the kill switch")
            job.error = exc.message
        except JarvisError as exc:
            job.status, job.error = "failed", exc.message
        except Exception as exc:
            log.exception("Job %s (%s) failed", job.id, job.kind)
            job.status = "failed"
            job.error = t(job.lang, f"תקלה פנימית: {type(exc).__name__}: {exc}",
                          f"Internal error: {type(exc).__name__}: {exc}")
        finally:
            for step in job.steps:
                if step.status == "running":
                    step.status = "failed" if job.status == "failed" else "skipped"
                elif step.status == "pending" and job.status != "done":
                    step.status = "skipped"
            job.ms = int((time.perf_counter() - started) * 1000)
            job.finished_at = utcnow()
            job.can_retry = job.status in ("failed", "cancelled") and job.id in self._starters
            self._tasks.pop(job.id, None)
            self._tokens.pop(job.id, None)
            self.record(f"job.{job.kind}", job.ms, ok=job.status == "done")
            with contextlib.suppress(Exception):
                await self.publish(job, force=True)
            self._append_history(job)

    # ------------------------------------------------------------------ control
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, *, area: str | None = None, kind: str | None = None, limit: int = 50) -> list[Job]:
        items = [j for j in reversed(self._jobs.values())
                 if (area is None or j.area == area) and (kind is None or j.kind == kind)]
        return items[:limit]

    def views(self, limit: int = 50) -> list[dict[str, Any]]:
        return [j.view() for j in self.recent(limit=limit)]

    def cancel(self, job_id: str, reason: str = "stopped") -> Job | None:
        job = self._jobs.get(job_id)
        token = self._tokens.get(job_id)
        if job is None:
            return None
        if token is not None:
            token.cancel(reason)
        return job

    async def retry(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        starter = self._starters.get(job_id)
        if job is None or starter is None or job.status == "running":
            return None
        return await starter()

    async def wait(self, job_id: str, timeout: float | None = None) -> Job | None:
        """Wait for a job to finish (tests and callers that need the result)."""
        task = self._tasks.get(job_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout)
        return self._jobs.get(job_id)

    def running(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status == "running"]

    def clear_history(self) -> int:
        """Forget every finished job (memory and file). Running jobs are kept."""
        done = [jid for jid, j in self._jobs.items() if j.status != "running"]
        for jid in done:
            self._jobs.pop(jid, None)
            self._starters.pop(jid, None)
            self._published.pop(jid, None)
        if self.history_file is not None:
            with contextlib.suppress(OSError):
                self.history_file.unlink()
        return len(done)

    async def aclose(self) -> None:
        for token in list(self._tokens.values()):
            token.cancel("JARVIS is shutting down")
        tasks = list(self._tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(BaseException):
                    await task

    # ------------------------------------------------------------------ events & measurements
    async def publish(self, job: Job, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._published.get(job.id, 0.0) < PROGRESS_EVERY_S:
            return
        self._published[job.id] = now
        if self.bus is not None:
            await self.bus.publish(event("job", job.view(), sender="jobs"))

    def record(self, name: str, ms: float, *, ok: bool = True) -> None:
        if self.metrics is not None:
            self.metrics.record(name, ms, ok=ok)

    # ------------------------------------------------------------------ history
    def _trim(self) -> None:
        while len(self._jobs) > KEEP:
            oldest = next((jid for jid, j in self._jobs.items() if j.status != "running"), None)
            if oldest is None:
                break
            self._jobs.pop(oldest, None)
            self._starters.pop(oldest, None)
            self._published.pop(oldest, None)

    def _load(self) -> None:
        if self.history_file is None or not self.history_file.is_file():
            return
        try:
            lines = self.history_file.read_text(encoding="utf-8").splitlines()[-KEEP:]
        except OSError:
            return
        for line in lines:
            try:
                job = Job.model_validate_json(line)
            except (ValidationError, ValueError):
                continue
            if job.status == "running":              # it was running when JARVIS stopped
                job.status, job.message = "cancelled", t(job.lang, "נעצר כי JARVIS נסגר", "Stopped because JARVIS closed")
            job.can_retry = False                     # the code that ran it is gone
            self._jobs[job.id] = job

    def _append_history(self, job: Job) -> None:
        if self.history_file is None:
            return
        record = job.model_copy()
        if not job.keep_result and job.result is not None:
            record.result = {k: job.result[k] for k in ("summary",) if k in job.result} or None
        text = record.model_dump_json()
        if len(text) > MAX_RESULT_CHARS:
            record.result = {"note": "the result was too large to keep in the history"}
            text = record.model_dump_json()
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with self.history_file.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
            if self.history_file.stat().st_size > 8_000_000:
                self._compact()
        except OSError:
            log.warning("Could not write the job history", exc_info=True)

    def _compact(self) -> None:
        assert self.history_file is not None
        lines = self.history_file.read_text(encoding="utf-8").splitlines()[-KEEP:]
        tmp = self.history_file.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, self.history_file)


__all__ = ["Job", "JobContext", "JobManager", "JobStep"]
