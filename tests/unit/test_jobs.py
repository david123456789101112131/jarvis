"""Jobs (the work screens' actions, shown live, stoppable, retryable) and the speed measurements behind them."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import jarvis.metrics as metrics_module
from jarvis.bus import MessageBus
from jarvis.common.errors import JarvisError, KillSwitchEngaged
from jarvis.jobs import JobContext, JobManager
from jarvis.metrics import Metrics


def _events(bus: MessageBus) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def keep(msg: Any) -> None:
        seen.append(dict(msg.data))

    bus.subscribe("job", keep)
    return seen


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class TestJobs:
    async def test_a_job_shows_its_steps_and_result_and_is_measured(self, tmp_path: Path) -> None:
        bus = MessageBus()
        seen = _events(bus)
        metrics = Metrics()
        jobs = JobManager(bus, tmp_path / "jobs.jsonl", metrics=metrics)

        async def run(ctx: JobContext) -> dict[str, Any]:
            async with ctx.step("look", "בודק"):
                await asyncio.sleep(0.01)
            await ctx.update(plan=["one", "two"], sends=["nothing"])
            async with ctx.step("write", "כותב"):
                pass
            return {"summary": "done", "files": 2}

        job = await jobs.start(kind="dev.inspect", area="code", title="בדיקת הפרויקט", run=run,
                               steps=[("look", "בודק"), ("write", "כותב")], params={"root": "x"})
        assert job.status == "running" and seen and seen[0]["status"] == "running"   # visible at once
        finished = await jobs.wait(job.id, 5)
        assert finished is not None and finished.status == "done" and finished.progress == 1.0
        assert finished.result == {"summary": "done", "files": 2}
        assert [s.status for s in finished.steps] == ["done", "done"]
        assert finished.steps[0].ms is not None and finished.steps[0].ms >= 5
        assert finished.plan == ["one", "two"] and finished.sends == ["nothing"]
        await _settle()
        assert seen[-1]["status"] == "done" and seen[-1]["result"]["files"] == 2
        summary = metrics.summary()
        assert summary["job.dev.inspect"]["count"] == 1 and summary["job.dev.inspect"]["ok"] == 1
        assert summary["dev.inspect.look"]["count"] == 1
        assert not finished.can_retry                                                # nothing to retry

    async def test_stopping_a_job_abandons_its_wait_and_it_can_be_retried(self, tmp_path: Path) -> None:
        jobs = JobManager(MessageBus(), tmp_path / "jobs.jsonl")
        started = asyncio.Event()
        runs = 0

        async def run(ctx: JobContext) -> dict[str, Any]:
            nonlocal runs
            runs += 1
            async with ctx.step("wait", "ממתין"):
                started.set()
                await ctx.guard(asyncio.sleep(30))          # something that knows nothing about the token
            return {"never": True}

        job = await jobs.start(kind="image.create", area="images", title="תמונה", run=run,
                               steps=[("wait", "ממתין"), ("save", "שומר")])
        await asyncio.wait_for(started.wait(), 5)
        assert jobs.cancel(job.id) is job
        done = await jobs.wait(job.id, 5)
        assert done is not None and done.status == "cancelled" and done.result is None
        assert [s.status for s in done.steps] == ["failed", "skipped"]
        assert done.can_retry and "נעצר" in done.message
        again = await jobs.retry(job.id)
        assert again is not None and again.id != job.id and again.retry_of == job.id
        jobs.cancel(again.id)
        await jobs.wait(again.id, 5)
        assert runs == 2

    async def test_errors_are_reported_clearly(self, tmp_path: Path) -> None:
        jobs = JobManager(None)

        async def refused(ctx: JobContext) -> None:
            raise JarvisError("אין חיבור לשירות התמונות")

        async def broken(ctx: JobContext) -> None:
            raise ValueError("bad value")

        async def killed(ctx: JobContext) -> None:
            raise KillSwitchEngaged("kill switch engaged")

        a = await jobs.start(kind="a", area="code", title="a", run=refused)
        b = await jobs.start(kind="b", area="code", title="b", run=broken)
        c = await jobs.start(kind="c", area="code", title="c", run=killed)
        for job in (a, b, c):
            await jobs.wait(job.id, 5)
        assert a.status == "failed" and a.error == "אין חיבור לשירות התמונות" and a.can_retry
        assert b.status == "failed" and b.error is not None and "ValueError" in b.error and "תקלה פנימית" in b.error
        assert c.status == "cancelled" and "חירום" in c.message

    async def test_the_same_request_twice_runs_once(self) -> None:
        jobs = JobManager(None)
        gate = asyncio.Event()

        async def run(ctx: JobContext) -> dict[str, Any]:
            await ctx.guard(gate.wait())
            return {"ok": True}

        first = await jobs.start(kind="dev.inspect", area="code", title="x", run=run, params={"root": "a"})
        second = await jobs.start(kind="dev.inspect", area="code", title="x", run=run, params={"root": "a"})
        other = await jobs.start(kind="dev.inspect", area="code", title="x", run=run, params={"root": "b"})
        assert second is first and other is not first
        gate.set()
        await jobs.wait(first.id, 5)
        await jobs.wait(other.id, 5)
        third = await jobs.start(kind="dev.inspect", area="code", title="x", run=run, params={"root": "a"})
        assert third is not first                                  # finished jobs do not absorb new ones
        await jobs.wait(third.id, 5)

    async def test_history_survives_a_restart_and_can_be_cleared(self, tmp_path: Path) -> None:
        file = tmp_path / "jobs.jsonl"
        jobs = JobManager(None, file)

        async def secret_code(ctx: JobContext) -> dict[str, Any]:
            return {"summary": "שונו 2 קבצים", "diff": "--- a\n+++ b\n+password = 1"}

        job = await jobs.start(kind="dev.propose", area="code", title="שינוי", run=secret_code, keep_result=False)
        await jobs.wait(job.id, 5)
        assert job.result is not None and "diff" in job.result                  # the screen still gets it
        saved = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
        assert saved[-1]["result"] == {"summary": "שונו 2 קבצים"}               # the history does not

        again = JobManager(None, file)
        loaded = again.get(job.id)
        assert loaded is not None and loaded.status == "done" and not loaded.can_retry
        assert again.views()[0]["id"] == job.id
        assert again.clear_history() == 1 and not file.exists() and again.get(job.id) is None

    async def test_a_job_running_when_jarvis_closed_is_shown_as_stopped(self, tmp_path: Path) -> None:
        file = tmp_path / "jobs.jsonl"
        jobs = JobManager(None, file)
        gate = asyncio.Event()

        async def run(ctx: JobContext) -> None:
            await ctx.guard(gate.wait())

        job = await jobs.start(kind="video.create", area="video", title="סרטון", run=run)
        await _settle()
        await jobs.aclose()
        assert job.status == "cancelled"
        loaded = JobManager(None, file).get(job.id)
        assert loaded is not None and loaded.status == "cancelled"

    async def test_progress_events_are_throttled_but_steps_are_not(self) -> None:
        bus = MessageBus()
        seen = _events(bus)
        jobs = JobManager(bus)

        async def run(ctx: JobContext) -> None:
            for i in range(50):
                await ctx.progress(i / 50, f"{i}")
            async with ctx.step("s", "step"):
                pass

        job = await jobs.start(kind="k", area="code", title="t", run=run)
        await jobs.wait(job.id, 5)
        await _settle()
        assert len(seen) < 15                              # 50 progress updates became a few events
        assert seen[-1]["status"] == "done"
        assert any(e["steps"] and e["steps"][0]["status"] == "running" for e in seen)


class TestMetrics:
    def test_summary_has_the_median_and_the_slowest_tenth(self) -> None:
        m = Metrics()
        for ms in range(1, 101):
            m.record("dev.inspect", float(ms))
        m.record("dev.inspect", 5000.0, ok=False)
        s = m.summary()["dev.inspect"]
        assert s["count"] == 101 and s["ok"] == 100
        assert s["median_ms"] == 51 and s["p90_ms"] == 91 and s["max_ms"] == 5000 and s["last_ms"] == 5000
        assert m.summary("image.") == {}

    def test_timer_records_a_failure_as_not_ok(self) -> None:
        m = Metrics()
        with m.timer("x"):
            pass
        with pytest.raises(RuntimeError), m.timer("x"):
            raise RuntimeError("boom")
        assert m.summary()["x"]["count"] == 2 and m.summary()["x"]["ok"] == 1

    def test_only_the_last_samples_are_kept(self) -> None:
        m = Metrics(keep=10)
        for ms in range(25):
            m.record("a", float(ms))
        assert m.summary()["a"]["count"] == 10 and m.summary()["a"]["min_ms"] == 15

    def test_snapshots_and_the_file_survive_a_restart_and_clear(self, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(metrics_module, "SAVE_EVERY_S", 0.0)
        file = tmp_path / "metrics.json"
        m = Metrics(file)
        m.record("command.reply", 900.0)
        snap = m.snapshot("before")
        assert snap["summary"]["command.reply"]["median_ms"] == 900
        m.record("command.reply", 300.0)
        m.flush()
        again = Metrics(file)
        assert again.summary()["command.reply"]["count"] == 2
        assert again.snapshots()["before"]["summary"]["command.reply"]["count"] == 1
        data = json.loads(file.read_text(encoding="utf-8"))
        assert set(data) == {"samples", "snapshots"}          # names and durations only
        again.clear()
        assert Metrics(file).view() == {"summary": {}, "snapshots": {}}

    def test_a_damaged_file_starts_empty(self, tmp_path: Path) -> None:
        file = tmp_path / "metrics.json"
        file.write_text("{not json", encoding="utf-8")
        assert Metrics(file).view() == {"summary": {}, "snapshots": {}}
