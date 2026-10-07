"""Task Orchestrator: how JARVIS turns one request into finished work.

1. **Plan** — the Planner Agent splits the goal into a DAG of steps for the five agents (rule
   templates for known request types, the AI model for anything else). If the Planner cannot
   answer, JARVIS falls back to a basic plan itself.
2. **Run** — every step whose dependencies are done is assigned (over the bus, via the Agent
   Manager) right away, so independent steps run in parallel, most urgent first. Each step gets the
   results of the steps it depends on as context.
3. **Recover** — a failed step is retried with backoff and feedback, helped by a research step,
   or handed to another agent; a stopped agent pauses the task instead of failing it. Retry
   limits, a same-error guard and a per-task dispatch budget make endless loops impossible.
4. **Verify** — results are checked on disk (and against the acceptance criteria by an AI
   reviewer when one is available); a step that does not pass is sent back with the problems.
5. **Follow up** — in autonomous mode JARVIS checks the goal itself and adds follow-up steps.
6. **Answer** — one reply built from all results (see `synthesis`).

The task (plan, every step's state and result, the timeline) is saved after every change, so a
task can be opened later exactly as it ran, and an interrupted task continues where it stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from jarvis.agents import manager as manager_module
from jarvis.agents.base import AGENT_INFO, AgentResult, AgentTask, t
from jarvis.agents.planner.planning import fallback_plan, rule_plan
from jarvis.bus.message import Message, event
from jarvis.common.cancel import CancelToken
from jarvis.common.clock import utcnow
from jarvis.common.enums import AgentId, Mode, Priority, ResultStatus, TaskStatus
from jarvis.common.errors import PlanError, TaskRefused
from jarvis.common.text import detect_language
from jarvis.orchestration.models import PlanDraft, SubTask, TaskPlan, plan_from_draft
from jarvis.orchestration.recovery import (
    FALLBACK_AGENTS,
    HALT_CODES,
    MODEL_CODES,
    NO_RETRY_CODES,
    decide,
    dispatch_budget,
    failure_code,
)
from jarvis.orchestration.synthesis import synthesize, template_outcome
from jarvis.orchestration.task import JarvisTask, dependency_record, result_record, short_title
from jarvis.orchestration.verify import Verifier

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.agents.manager import AgentManager
    from jarvis.config.settings import OrchestratorSettings
    from jarvis.memory.manager import MemoryManager
    from jarvis.security.killswitch import KillEvent

log = logging.getLogger(__name__)

TaskListener = Callable[[str, JarvisTask], Awaitable[None] | None]

SAVE_INTERVAL_S = 2.0       # progress-only changes are saved at most this often (status changes at once)
EMIT_INTERVAL_S = 0.25      # task.updated events for progress are throttled to 4 per second
RAM_TASKS = 200             # finished tasks kept in memory for fast views (all of them are in task memory)
SYNTHESIS_TIMEOUT_S = 120.0
RESTART_WAIT_S = 3.0        # a restarting agent gets this long to come back before the task pauses
ACTIVE_STEP = (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.VERIFYING)


@dataclass
class _Run:
    """Runtime state of one task while it runs (not persisted: the task itself is)."""

    task: JarvisTask
    token: CancelToken
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    runner: asyncio.Task[None] | None = None
    running: dict[str, asyncio.Future[AgentResult]] = field(default_factory=dict)
    not_before: dict[str, float] = field(default_factory=dict)
    offline: set[str] = field(default_factory=set)
    feedback: dict[str, str] = field(default_factory=dict)
    helped: set[str] = field(default_factory=set)
    fallen_back: set[str] = field(default_factory=set)
    confirmations: dict[str, str] = field(default_factory=dict)   # confirmation id -> subtask id
    stop: str | None = None          # "cancel" | "shutdown" | "timeout"
    stop_reason: str = ""
    halt: str | None = None          # why the task pauses (an agent is stopped / a kill switch is engaged)
    budget_hit: bool = False
    last_save: float = 0.0
    save_pending: bool = False
    last_emit: float = 0.0
    emit_pending: bool = False


class TaskOrchestrator:
    def __init__(self, manager: AgentManager, *, mode_provider: Callable[[], Mode] | None = None) -> None:
        self.manager = manager
        self.runtime = manager.runtime
        self.bus = self.runtime.bus
        self.settings = self.runtime.settings
        self.mode_provider = mode_provider or (lambda: Mode(self.settings.general.default_mode))
        self.verifier = Verifier(roots=self._roots, router=lambda: self.runtime.router)
        self._runs: dict[str, _Run] = {}
        self._tasks: dict[str, JarvisTask] = {}
        self._listeners: list[TaskListener] = []
        self._subs: list[Any] = []
        self._bg: set[asyncio.Task[Any]] = set()
        self._started = False

    # ================================================================== properties
    @property
    def cfg(self) -> OrchestratorSettings:
        return self.settings.orchestrator

    @property
    def memory(self) -> MemoryManager | None:
        return self.runtime.memory

    @property
    def ai(self) -> bool:
        return self.runtime.ai_available

    def _roots(self) -> list[Path]:
        paths = self.runtime.paths
        return [paths.workspace, paths.projects]

    def add_listener(self, listener: TaskListener) -> None:
        """`listener(kind, task)` with kind in created / planned / waiting / finished."""
        self._listeners.append(listener)

    # ================================================================== lifecycle
    async def start(self) -> list[JarvisTask]:
        """Subscribe to agent progress and confirmations; returns tasks a previous run left unfinished."""
        if self._started:
            return []
        self._started = True
        self._subs += [
            self.bus.subscribe("task.progress", self._on_progress, name="orchestrator"),
            self.bus.subscribe("confirm.request", self._on_confirm_request, name="orchestrator"),
            self.bus.subscribe("confirm.response", self._on_confirm_response, name="orchestrator"),
        ]
        self.manager.kill_switch.add_listener(self._on_kill)
        return await self.recover()

    async def shutdown(self, timeout: float = 20.0) -> None:
        """Stop every running task; they are saved as interrupted and can be resumed next time."""
        runs = list(self._runs.values())
        for run in runs:
            self._stop(run, "shutdown", t(run.task.language, "JARVIS נסגר", "JARVIS is shutting down"))
        runners = [r.runner for r in runs if r.runner is not None]
        if runners:
            _, pending = await asyncio.wait(runners, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()
        for task in list(self._bg):
            task.cancel()
        self._started = False

    # ================================================================== public API
    async def submit(
        self,
        goal: str,
        *,
        title: str | None = None,
        intent: str | None = None,
        slots: dict[str, Any] | None = None,
        language: str | None = None,
        mode: Mode | None = None,
        priority: Priority | str | None = None,
        source: str = "text",
        plan: TaskPlan | None = None,
        context: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> JarvisTask:
        """Start a task in the background and return it at once (status pending → planning → running)."""
        goal = " ".join((goal or "").split())
        lang = language if language in ("he", "en") else detect_language(goal, "he")
        if not goal:
            raise TaskRefused(t(lang, "לא התקבלה משימה.", "No task was given."))
        mode = mode or self.mode_provider()
        if mode == Mode.CHAT:
            raise TaskRefused(t(lang, "במצב שיחה אני לא מבצע משימות. אפשר לעבור למצב רגיל כדי שאבצע את זה.",
                                "In chat mode I don't run tasks. Switch to normal mode and I'll do it."))
        if plan is not None:
            plan.validate_dag()
        task = JarvisTask(goal=goal, title=(title or short_title(goal))[:120], auto_title=title is None,
                          intent=intent, slots=dict(slots or {}), language=lang, mode=mode,
                          priority=Priority.parse(priority), source=source, plan=plan, context=dict(context or {}),
                          project_id=project_id)
        self._keep(task)
        await self._note(task, "received", t(lang, f"JARVIS קיבל משימה: {task.title}", f"JARVIS received a task: "
                                             f"{task.title}"), activity=True)
        await self._save(task, force=True)
        await self._emit(task, force=True)
        await self._notify("created", task)
        self._start_run(task)
        return task

    async def run(self, goal: str, *, timeout: float | None = None, **kwargs: Any) -> JarvisTask:
        """Submit and wait until the task finishes (or pauses)."""
        task = await self.submit(goal, **kwargs)
        return await self.wait(task.id, timeout=timeout)

    async def wait(self, task_id: str, *, timeout: float | None = None) -> JarvisTask:
        run = self._runs.get(task_id)
        if run is not None:
            await asyncio.wait_for(asyncio.shield(run.done.wait()), timeout)
        task = await self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    async def cancel(self, task_id: str, reason: str | None = None) -> JarvisTask | None:
        """Stop a task now: running steps are cancelled (and their processes killed), queued ones dropped."""
        run = self._runs.get(task_id)
        if run is None:
            task = await self.get(task_id)
            if task is None:
                return None
            if task.status in (TaskStatus.INTERRUPTED, TaskStatus.PENDING):
                await self._cancel_idle(task, reason)
            return task
        lang = run.task.language
        reason = reason or t(lang, "בוטל על ידי המשתמש", "cancelled by the user")
        if run.stop is None:
            await self._note(run.task, "cancel", t(lang, f"המשימה בוטלה: {reason}", f"Task cancelled: {reason}"),
                             level="warning", activity=True)
        self._stop(run, "cancel", reason)
        await self.manager.cancel_task(task_id, reason)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(run.done.wait()), manager_module.GRACE_S + 10)
        return run.task

    async def cancel_all(self, reason: str | None = None) -> int:
        ids = list(self._runs)
        for task_id in ids:
            await self.cancel(task_id, reason)
        return len(ids)

    async def resume(self, task_id: str) -> JarvisTask:
        """Continue a paused, cancelled or failed task: finished steps are kept, the rest run again."""
        run = self._runs.get(task_id)
        if run is not None:
            return run.task
        task = await self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        lang = task.language
        if task.status == TaskStatus.COMPLETED:
            raise TaskRefused(t(lang, "המשימה כבר הושלמה.", "The task is already complete."))
        if task.status == TaskStatus.FAILED:
            return await self.retry(task_id)
        done = 0
        if task.plan is not None:
            for s in task.plan.subtasks:
                if s.status in (*ACTIVE_STEP, TaskStatus.CANCELLED, TaskStatus.SKIPPED):
                    s.status, s.progress, s.finished_at = TaskStatus.PENDING, 0.0, None
                done += s.status == TaskStatus.COMPLETED
        total = len(task.plan.subtasks) if task.plan else 0
        await self._restart(task, t(lang, f"ממשיך מהמקום שבו המשימה נעצרה: {done} מתוך {total} שלבים כבר הושלמו.",
                                    f"Continuing where the task stopped: {done} of {total} steps are already done."))
        return task

    async def retry(self, task_id: str) -> JarvisTask:
        """Run the failed (and skipped) steps of a finished task again, with fresh retry limits."""
        run = self._runs.get(task_id)
        if run is not None:
            return run.task
        task = await self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        lang = task.language
        again = 0
        if task.plan is not None:
            for s in task.plan.subtasks:
                if s.status in (TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED, *ACTIVE_STEP):
                    s.status, s.progress, s.finished_at = TaskStatus.PENDING, 0.0, None
                    s.attempts, s.error, s.error_history = 0, None, []
                    again += 1
        if task.plan is not None and again == 0 and task.status == TaskStatus.COMPLETED:
            raise TaskRefused(t(lang, "אין שלבים שנכשלו במשימה הזאת.", "This task has no failed steps."))
        await self._restart(task, t(lang, f"מנסה שוב {again} שלבים שלא הושלמו.", f"Retrying {again} unfinished steps.")
                            if task.plan is not None else t(lang, "מתכנן את המשימה מחדש.", "Planning the task again."))
        return task

    async def recover(self) -> list[JarvisTask]:
        """Tasks that were running when JARVIS last stopped: marked interrupted (resumable)."""
        memory = self.memory
        if memory is None:
            return []
        try:
            records = await memory.interrupted_tasks()
        except Exception:
            log.exception("Reading interrupted tasks failed")
            return []
        found: list[JarvisTask] = []
        was_running: list[str] = []
        for rec in records:
            if rec.id in self._runs:
                continue
            task = JarvisTask.from_record(rec)
            if task.status != TaskStatus.INTERRUPTED:
                was_running.append(task.id)
                lang = task.language
                task.status = TaskStatus.INTERRUPTED
                task.error = t(lang, "JARVIS נסגר באמצע המשימה", "JARVIS closed while the task was running")
                if task.plan is not None:
                    for s in task.plan.subtasks:
                        if s.status in ACTIVE_STEP:
                            s.status, s.progress = TaskStatus.PENDING, 0.0
                task.note("interrupted", t(lang, "המשימה נקטעה כש-JARVIS נסגר; אפשר להמשיך אותה מאותה נקודה.",
                                           "The task was cut off when JARVIS closed; it can continue from there."),
                          level="warning")
                task.outcome = template_outcome(task)
                await self._save(task, force=True)
            self._keep(task)
            found.append(task)
        if self.settings.general.resume_interrupted_on_start:
            for task_id in was_running:
                with contextlib.suppress(Exception):
                    await self.resume(task_id)
        return found

    # -- views ----------------------------------------------------------------------------------
    async def get(self, task_id: str) -> JarvisTask | None:
        task = self._tasks.get(task_id)
        if task is not None or self.memory is None:
            return task
        try:
            rec = await self.memory.get_task(task_id)
        except Exception:
            log.exception("Reading task %s failed", task_id)
            return None
        return JarvisTask.from_record(rec) if rec is not None else None

    def active_tasks(self) -> list[JarvisTask]:
        return [r.task for r in self._runs.values()]

    def is_running(self, task_id: str) -> bool:
        return task_id in self._runs

    async def detail(self, task_id: str) -> dict[str, Any] | None:
        task = await self.get(task_id)
        return task.detail_view() if task else None

    async def list_tasks(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """TaskSummary rows, newest first (live tasks from RAM, the rest from task memory)."""
        views: dict[str, dict[str, Any]] = {}
        if self.memory is not None:
            try:
                records = await self.memory.tasks(statuses=[status] if status else None, limit=limit)
            except Exception:
                log.exception("Listing tasks failed")
                records = []
            for rec in records:
                views[rec.id] = JarvisTask.from_record(rec).summary_view()
        for task in self._tasks.values():
            if status is None or task.status.value == status:
                views[task.id] = task.summary_view()
        rows = sorted(views.values(), key=lambda v: v["created_at"], reverse=True)
        return rows[: max(1, limit)]

    @staticmethod
    def direct_plan(agent: AgentId | str, instruction: str, *, title: str = "", skill: str | None = None,
                    params: dict[str, Any] | None = None, lang: str = "he") -> TaskPlan:
        """A one-step plan (for "give the Development Agent this task", or a known command in task mode)."""
        agent_id = AgentId.parse(agent)
        name = AGENT_INFO[agent_id]["name_he" if lang == "he" else "name"]
        sub = SubTask(agent=agent_id, key="main", title=(title or short_title(instruction, 80)), instruction=instruction,
                      skill=skill, params=dict(params or {}), priority=Priority.HIGH)
        return TaskPlan(title=sub.title, strategy=t(lang, f"שלב אחד ל{name}.", f"One step for the {name}."),
                        subtasks=[sub], source="direct")

    # ================================================================== the run
    def _start_run(self, task: JarvisTask) -> _Run:
        run = _Run(task=task, token=CancelToken())
        task.runs += 1
        task.dispatches = 0          # the loop guard counts per run; every new run is the user's decision
        self._runs[task.id] = run
        run.runner = asyncio.create_task(self._drive(run), name=f"jarvis-task-{task.id}")
        return run

    async def _restart(self, task: JarvisTask, message: str) -> None:
        task.status, task.error, task.outcome, task.finished_at = TaskStatus.PENDING, None, None, None
        self._keep(task)
        await self._note(task, "resume", message, activity=True)
        await self._save(task, force=True)
        await self._emit(task, force=True)
        self._start_run(task)

    async def _drive(self, run: _Run) -> None:
        task = run.task
        limit = self.cfg.autonomous_task_timeout_s if task.mode == Mode.AUTONOMOUS else self.cfg.task_timeout_s
        try:
            deadline = asyncio.timeout(limit)
            try:
                async with deadline:
                    if task.plan is None:
                        await self._plan(run)
                    if task.plan is not None and run.stop is None and run.halt is None:
                        await self._execute(run)
            except TimeoutError:
                if not deadline.expired():
                    raise
                self._stop(run, "timeout", t(task.language, f"המשימה חרגה מהזמן המקסימלי ({limit / 60:.0f} דקות)",
                                             f"The task ran out of time ({limit / 60:.0f} minutes)"))
                await self._note(task, "timeout", run.stop_reason, level="error", activity=True)
                await self.manager.cancel_task(task.id, run.stop_reason)
            await self._drain(run)
            await self._conclude(run)
        except asyncio.CancelledError:
            self._settle(run, TaskStatus.INTERRUPTED)
            with contextlib.suppress(BaseException):
                await asyncio.shield(self._save(task, force=True))
            raise
        except Exception as exc:
            log.exception("Task %s crashed", task.id)
            task.error = t(task.language, f"תקלה פנימית ב-JARVIS: {exc}", f"JARVIS hit an internal error: {exc}")
            self._settle(run, TaskStatus.FAILED)
            with contextlib.suppress(Exception):
                await self._note(task, "error", task.error, level="error", activity=True)
                await self._save(task, force=True)
                await self._emit(task, force=True)
                await self._notify("finished", task)
        finally:
            run.done.set()
            self._runs.pop(task.id, None)
            self._trim()

    # -- planning --------------------------------------------------------------------------------
    async def _plan(self, run: _Run) -> None:
        task = run.task
        lang = task.language
        task.status = TaskStatus.PLANNING
        task.started_at = task.started_at or utcnow()
        memory_text = await self._memory_context(task)
        if memory_text:
            task.context["memory"] = memory_text
        await self._note(task, "planning", t(lang, "סוכן התכנון מפרק את המשימה לשלבים",
                                             "The Planner Agent is splitting the task into steps"),
                         agent_id=AgentId.PLANNER.value, activity=True)
        await self._emit(task, force=True)
        await self._save(task, force=True)
        request = AgentTask(
            task_id=task.id, agent=AgentId.PLANNER, skill="decompose", instruction=task.goal,
            title=t(lang, "פירוק המשימה לשלבים", "Split the task into steps"),
            params={"goal": task.goal, "intent": task.intent, "slots": task.slots, "context": memory_text,
                    "max_steps": 12 if task.mode == Mode.AUTONOMOUS else 8},
            context={"goal": task.goal}, priority=self._priority(task, Priority.HIGH), mode=task.mode, language=lang,
        )
        result = await self.manager.run(request, cancel=run.token)
        if run.stop is not None:
            return
        plan: TaskPlan | None = None
        problem = result.summary
        raw = result.data.get("plan") if result.ok else None
        if isinstance(raw, dict):
            try:
                plan = TaskPlan.model_validate(raw)
                plan.validate_dag()
                if not plan.subtasks:
                    raise PlanError("empty plan")
            except (ValidationError, PlanError) as exc:
                plan, problem = None, str(exc)
        if plan is None:
            plan = self._local_plan(task)
            await self._note(task, "plan_fallback", t(lang, f"סוכן התכנון לא החזיר תוכנית ({problem}); JARVIS בנה תוכנית "
                                                            "בסיסית בעצמו.",
                                                      f"The Planner Agent returned no plan ({problem}); JARVIS made a "
                                                      "basic plan itself."), level="warning", activity=True)
        task.plan = plan
        if task.auto_title and plan.title:
            task.title = plan.title[:120]
        names = ", ".join(AGENT_INFO[a]["name_he" if lang == "he" else "name"] for a in plan.agents)
        await self._note(task, "plan", t(lang, f"התוכנית: {len(plan.subtasks)} שלבים ({names}). {plan.strategy}",
                                         f"The plan: {len(plan.subtasks)} steps ({names}). {plan.strategy}").strip(),
                         agent_id=AgentId.PLANNER.value, activity=True)
        if task.mode == Mode.DEBUG:
            for s in plan.subtasks:
                deps = ", ".join(plan.get(d).key or plan.get(d).title for d in s.depends_on) or "-"
                await self._note(task, "debug", f"[{s.key}] {s.agent.value} skill={s.skill or 'llm'} deps={deps} "
                                                f"priority={s.priority.name.lower()}"
                                                + (" optional" if s.optional else ""), level="debug", subtask_id=s.id)
        await self._save(task, force=True)
        await self._emit(task, force=True)
        for s in plan.subtasks:
            await self._emit_subtask(task, s)
        await self._notify("planned", task)

    def _local_plan(self, task: JarvisTask) -> TaskPlan:
        plan = None
        with contextlib.suppress(PlanError):
            plan = rule_plan(task.intent, task.slots, task.goal, task.language, ai=self.ai)
        return plan or fallback_plan(task.goal, task.language)

    async def _memory_context(self, task: JarvisTask) -> str:
        given = task.context.get("memory")
        if given:
            return str(given)[:3000]
        if self.memory is None:
            return ""
        try:
            recall = await self.memory.recall(task.goal, project_id=task.project_id)
        except Exception:
            log.exception("Memory recall failed")
            return ""
        return "" if recall.is_empty else recall.format(2500)

    # -- scheduling ------------------------------------------------------------------------------
    async def _execute(self, run: _Run) -> None:
        task = run.task
        plan = task.plan
        assert plan is not None
        task.started_at = task.started_at or utcnow()
        if task.status != TaskStatus.WAITING:
            task.status = TaskStatus.RUNNING
        await self._emit(task, force=True)
        while True:
            await self._launch_ready(run)
            if run.running:
                await self._wait_any(run)
                continue
            if run.stop is not None or run.halt is not None or run.budget_hit:
                break
            pending = [s for s in plan.subtasks if s.status == TaskStatus.PENDING]
            if not pending:
                if await self._autonomous_followups(run):
                    continue
                break
            delay = self._next_due(run)
            if delay is None:      # nothing can ever start (should not happen with a valid DAG)
                for s in pending:
                    await self._finish_step(task, s, TaskStatus.SKIPPED,
                                            t(task.language, "לא ניתן היה להתחיל את השלב", "The step could not start"))
                break
            await self._sleep(run, delay)

    async def _launch_ready(self, run: _Run) -> None:
        if run.stop is not None or run.halt is not None or run.budget_hit:
            return
        task = run.task
        plan = task.plan
        assert plan is not None
        await self._skip_blocked(run)
        now = time.monotonic()
        limit = max(1, int(self.cfg.max_parallel_subtasks))
        budget = dispatch_budget(len(plan.subtasks), max_retries=self.cfg.max_retries,
                                 max_followups=self._max_followups(task), autonomous=task.mode == Mode.AUTONOMOUS)
        for s in plan.ready():
            if len(run.running) >= limit:
                break
            if s.id in run.running or run.not_before.get(s.id, 0.0) > now:
                continue
            if task.dispatches >= budget:
                run.budget_hit = True
                await self._note(task, "loop_guard", t(task.language, f"עצרתי: המשימה הגיעה למגבלת {budget} הפעלות של "
                                                                      "סוכנים (הגנה מלולאה אינסופית).",
                                                       f"Stopped: the task reached its limit of {budget} agent "
                                                       "assignments (protection against endless loops)."),
                                 level="error", activity=True)
                break
            await self._dispatch(run, s)
            if run.halt is not None:
                break

    async def _dispatch(self, run: _Run, s: SubTask) -> None:
        task = run.task
        plan = task.plan
        assert plan is not None
        lang = task.language
        s.attempts += 1
        task.dispatches += 1
        s.status = TaskStatus.QUEUED
        s.started_at = s.started_at or utcnow()
        s.finished_at, s.error, s.progress = None, None, 0.0
        request = AgentTask(
            id=s.id, task_id=task.id, agent=s.agent, instruction=s.instruction, title=s.title, skill=s.skill,
            params=dict(s.params), context=self._context(run, s), priority=self._priority(task, s.priority),
            mode=task.mode, timeout_s=s.timeout_s, cwd=plan.project_dir, language=lang,
            acceptance_criteria=list(s.acceptance_criteria), expected_files=list(s.expected_files),
            attempt=s.attempts, allow_llm=s.id not in run.offline,
        )
        future = await self.manager.submit(request, cancel=run.token)
        run.running[s.id] = future
        who = self._who(s, lang)
        line = t(lang, f"{who} קיבל: {s.title}", f"{who} assigned: {s.title}")
        if s.attempts > 1:
            line += t(lang, f" (ניסיון {s.attempts})", f" (attempt {s.attempts})")
        if s.id in run.offline:
            line += t(lang, " — בלי מודל AI", " — without the AI model")
        await self._note(task, "assign", line, agent_id=s.agent.value, subtask_id=s.id)
        await self._emit_subtask(task, s)
        await self._emit(task)
        await self._save(task)

    async def _wait_any(self, run: _Run) -> None:
        waker = asyncio.ensure_future(run.wake.wait())
        try:
            await asyncio.wait([*run.running.values(), waker], timeout=self._next_due(run),
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            waker.cancel()
        run.wake.clear()
        plan = run.task.plan
        assert plan is not None
        for sid, future in list(run.running.items()):
            if future.done():
                run.running.pop(sid, None)
                await self._on_result(run, plan.get(sid), future.result())

    async def _sleep(self, run: _Run, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(run.wake.wait(), max(0.0, seconds))
        run.wake.clear()

    def _next_due(self, run: _Run) -> float | None:
        plan = run.task.plan
        if plan is None:
            return None
        now = time.monotonic()
        due = [run.not_before[s.id] - now for s in plan.subtasks
               if s.status == TaskStatus.PENDING and run.not_before.get(s.id, 0.0) > now]
        return max(0.0, min(due)) if due else None

    async def _drain(self, run: _Run) -> None:
        """Wait for steps still running (after a stop they end within the agents' grace period)."""
        if not run.running:
            return
        plan = run.task.plan
        assert plan is not None
        futures = dict(run.running)
        await asyncio.wait(list(futures.values()), timeout=manager_module.GRACE_S + 5)
        for sid, future in futures.items():
            run.running.pop(sid, None)
            if future.done():
                await self._on_result(run, plan.get(sid), future.result())

    # -- results ---------------------------------------------------------------------------------
    async def _on_result(self, run: _Run, s: SubTask, result: AgentResult) -> None:
        task = run.task
        lang = task.language
        s.progress = max(s.progress, min(1.0, float(result.progress or 0.0)))
        for conf_id in [c for c, sid in run.confirmations.items() if sid == s.id]:
            run.confirmations.pop(conf_id, None)
        if result.status in (ResultStatus.SUCCESS, ResultStatus.PARTIAL):
            problems: list[str] = []
            if self.cfg.verify_results and run.stop is None:
                s.status = TaskStatus.VERIFYING
                await self._emit_subtask(task, s)
                plan = task.plan
                assert plan is not None
                problems = await self.verifier.check_result(s, result, lang=lang, project_dir=plan.project_dir)
                if not problems and self.verifier.wants_review(s, result):
                    await self._note(task, "verify", t(lang, f"JARVIS בודק את העבודה של {self._who(s, lang)}",
                                                       f"JARVIS is checking the {self._who(s, lang)}'s work"),
                                     agent_id=s.agent.value, subtask_id=s.id)
                    problems = await self.verifier.review(task, s, result)
            if not problems:
                await self._complete(run, s, result)
                return
            error = t(lang, "האימות נכשל: ", "Verification failed: ") + "; ".join(problems[:5])
            await self._note(task, "verify_failed", f"{self._who(s, lang)}: {error}", agent_id=s.agent.value,
                             subtask_id=s.id, level="warning", activity=True)
            await self._failed(run, s, result, code="verification_failed", error=error,
                               feedback="\n".join(f"- {p}" for p in problems))
            return
        if result.status == ResultStatus.CANCELLED:
            if run.stop is not None:
                if run.stop == "shutdown":
                    s.status, s.progress = TaskStatus.PENDING, 0.0
                    s.attempts = max(0, s.attempts - 1)       # the cut-off attempt does not count
                    await self._emit_subtask(task, s)
                else:
                    await self._finish_step(task, s, TaskStatus.CANCELLED, run.stop_reason or result.summary)
                return
            # Stopped from outside this task: the agent was stopped or restarted, or a kill switch engaged.
            if not self.manager.kill_switch.is_engaged(s.agent.value):
                deadline = time.monotonic() + RESTART_WAIT_S
                while not self.manager.is_available(s.agent) and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
            if self.manager.is_available(s.agent):
                s.attempts = max(0, s.attempts - 1)
                s.status, s.progress = TaskStatus.PENDING, 0.0
                await self._note(task, "retry", t(lang, f"{self._who(s, lang)} הופעל מחדש; השלב יתחיל שוב.",
                                                  f"The {self._who(s, lang)} restarted; the step starts again."),
                                 agent_id=s.agent.value, subtask_id=s.id, level="warning")
                await self._emit_subtask(task, s)
                return
            code = "kill_switch" if self.manager.kill_switch.is_engaged(s.agent.value) else "agent_stopped"
            await self._failed(run, s, result, code=code, error=result.summary or code)
            return
        code = failure_code(result.status, result.data.get("code"), result.denied)
        error = (result.errors[-1] if result.errors else result.summary) or t(lang, "שגיאה לא ידועה", "Unknown error")
        await self._failed(run, s, result, code=code, error=error, feedback=error)

    async def _complete(self, run: _Run, s: SubTask, result: AgentResult) -> None:
        task = run.task
        plan = task.plan
        assert plan is not None
        s.status, s.progress, s.finished_at, s.error = TaskStatus.COMPLETED, 1.0, utcnow(), None
        s.summary = (result.summary or "")[:1500]
        s.files_created = list(dict.fromkeys(result.files_created))[:200]
        s.result = result_record(result)
        run.feedback.pop(s.id, None)
        run.offline.discard(s.id)
        data = result.result if isinstance(result.result, dict) else {}
        if plan.project_dir is None and s.agent == AgentId.DEVELOPMENT and isinstance(data.get("path"), str):
            plan.project_dir = data["path"]
            if task.project_id is None and self.memory is not None and data.get("name"):
                with contextlib.suppress(Exception):
                    project = await self.memory.project(str(data["name"]))
                    task.project_id = project.id if project is not None else None
        lang = task.language
        line = t(lang, f"{self._who(s, lang)} סיים: {s.title}", f"{self._who(s, lang)} completed: {s.title}")
        if result.status == ResultStatus.PARTIAL:
            line += t(lang, " (חלקית)", " (partly)")
        await self._note(task, "done", line, agent_id=s.agent.value, subtask_id=s.id,
                         level="success" if result.status == ResultStatus.SUCCESS else "warning")
        await self._emit_subtask(task, s)
        await self._emit(task, force=True)
        await self._save(task, force=True)

    async def _failed(self, run: _Run, s: SubTask, result: AgentResult, *, code: str | None, error: str,
                      feedback: str = "") -> None:
        """Record the failure, then retry / get help / hand over / pause / give up (see `recovery`)."""
        task = run.task
        plan = task.plan
        assert plan is not None
        lang = task.language
        who = self._who(s, lang)
        error = " ".join(str(error).split())[:1000]
        s.error = error
        s.error_history = [*s.error_history, error[:500]][-20:]
        if result.summary:
            s.summary = result.summary[:1500]
        s.result = result_record(result)
        decision = decide(
            code=code, error=error, attempts=s.attempts, history=s.error_history,
            max_retries=self.cfg.max_retries if s.max_retries is None else s.max_retries,
            max_same_error=self.cfg.max_same_error, backoff_s=self.cfg.retry_backoff_s,
            backoff_max_s=self.cfg.retry_backoff_max_s, retryable=result.data.get("retryable"),
            can_run_offline=self._offline_ok(s), can_help=self._can_help(run, s, code),
            can_fallback=self._fallback_target(run, s, code) is not None,
        )
        short = _clip(error, 160)
        if decision.action == "retry":
            s.status, s.progress = TaskStatus.PENDING, 0.0
            run.not_before[s.id] = time.monotonic() + decision.delay_s
            if feedback:
                run.feedback[s.id] = feedback[:2000]
            if decision.offline:
                run.offline.add(s.id)
            total = 1 + (self.cfg.max_retries if s.max_retries is None else s.max_retries)
            await self._note(task, "retry", t(lang, f"{who} נכשל ({short}). מנסה שוב בעוד {decision.delay_s:.0f} שניות "
                                                    f"(ניסיון {s.attempts + 1} מתוך {total})"
                                                    + (" בלי מודל ה-AI" if decision.offline else "") + ".",
                                              f"The {who} failed ({short}). Retrying in {decision.delay_s:.0f}s "
                                              f"(attempt {s.attempts + 1} of {total})"
                                              + (" without the AI model" if decision.offline else "") + "."),
                             agent_id=s.agent.value, subtask_id=s.id, level="warning", activity=True)
        elif decision.action == "help":
            helper = self._helper(s, error, lang)
            plan.subtasks.insert(plan.subtasks.index(s), helper)
            s.depends_on = [*s.depends_on, helper.id]
            s.status, s.progress = TaskStatus.PENDING, 0.0
            run.helped.add(s.id)
            if feedback:
                run.feedback[s.id] = feedback[:2000]
            task.followups += 1
            await self._note(task, "help", t(lang, f"{who} נכשל ({short}). סוכן המחקר מחפש פתרון, ואז {who} ינסה שוב.",
                                             f"The {who} failed ({short}). The Research Agent looks for a fix, then the "
                                             f"{who} tries again."),
                             agent_id=s.agent.value, subtask_id=s.id, level="warning", activity=True)
            await self._emit_subtask(task, helper)
        elif decision.action == "fallback":
            target = self._fallback_target(run, s, code)
            assert target is not None
            old = who
            s.agent = target
            if s.skill and s.skill not in self.manager.agent(target).skills:
                s.skill = None
            s.status, s.progress, s.attempts = TaskStatus.PENDING, 0.0, 0
            run.fallen_back.add(s.id)
            run.offline.discard(s.id)
            run.not_before.pop(s.id, None)
            if feedback:
                run.feedback[s.id] = feedback[:2000]
            await self._note(task, "fallback", t(lang, f"{old} לא הצליח ({short}). מעביר את השלב ל{self._who(s, lang)}.",
                                                 f"The {old} could not do it ({short}). Handing the step to the "
                                                 f"{self._who(s, lang)}."),
                             agent_id=target.value, subtask_id=s.id, level="warning", activity=True)
        elif decision.action == "halt":
            s.attempts = max(0, s.attempts - 1)
            if s.optional:
                await self._finish_step(task, s, TaskStatus.SKIPPED, t(lang, f"דילגתי (שלב רשות): {who} לא זמין.",
                                                                       f"Skipped (optional): the {who} is unavailable."))
                return
            s.status, s.progress = TaskStatus.PENDING, 0.0
            reason = t(lang, f"{who} לא זמין: {short}", f"the {who} is unavailable: {short}")
            run.halt = run.halt or reason
            await self._note(task, "paused", t(lang, f"המשימה ממתינה: {reason}. אפשר להפעיל את הסוכן ולהמשיך את המשימה.",
                                               f"The task is paused: {reason}. Start the agent and resume the task."),
                             agent_id=s.agent.value, subtask_id=s.id, level="warning", activity=True)
        else:
            s.status, s.finished_at = TaskStatus.FAILED, utcnow()
            note = t(lang, f"{who} נכשל: {short}", f"The {who} failed: {short}")
            if decision.reason in ("retries used up", "same error again") and s.attempts > 1:
                note += t(lang, f" (אחרי {s.attempts} ניסיונות)", f" (after {s.attempts} attempts)")
            if s.optional:
                note += t(lang, " — שלב רשות, ממשיך בלעדיו.", " — optional step, continuing without it.")
            await self._note(task, "failed", note, agent_id=s.agent.value, subtask_id=s.id,
                             level="warning" if s.optional else "error", activity=True)
        await self._emit_subtask(task, s)
        await self._emit(task, force=True)
        await self._save(task, force=True)

    async def _finish_step(self, task: JarvisTask, s: SubTask, status: TaskStatus, reason: str) -> None:
        s.status, s.finished_at = status, utcnow()
        s.error = reason
        level = "warning" if status in (TaskStatus.SKIPPED, TaskStatus.CANCELLED) else "error"
        await self._note(task, status.value, f"{self._who(s, task.language)} — {s.title}: {reason}",
                         agent_id=s.agent.value, subtask_id=s.id, level=level)
        await self._emit_subtask(task, s)

    async def _skip_blocked(self, run: _Run) -> None:
        task = run.task
        plan = task.plan
        assert plan is not None
        lang = task.language
        changed = True
        while changed:
            changed = False
            for s in plan.blocked():
                dep = next(plan.get(d) for d in s.depends_on
                           if plan.get(d).status in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED)
                           and not plan.get(d).optional)
                await self._finish_step(task, s, TaskStatus.SKIPPED, t(lang, f"דילגתי: השלב '{dep.title}' לא הושלם",
                                                                       f"Skipped: '{dep.title}' did not complete"))
                changed = True

    # -- recovery helpers --------------------------------------------------------------------------
    def _offline_ok(self, s: SubTask) -> bool:
        agent = self.manager.agent(s.agent)
        if s.skill:
            info = agent.skills.get(s.skill)
            return info is not None and not info.needs_llm
        return agent.can_work_offline(AgentTask(agent=s.agent, instruction=s.instruction, params=dict(s.params)))

    def _can_help(self, run: _Run, s: SubTask, code: str | None) -> bool:
        return (s.agent == AgentId.DEVELOPMENT and s.id not in run.helped and s.followup_of is None
                and code not in NO_RETRY_CODES and code not in HALT_CODES and code not in MODEL_CODES
                and self.ai and self.settings.security.network_enabled
                and self.manager.is_available(AgentId.RESEARCH)
                and run.task.followups < self._max_followups(run.task))

    def _fallback_target(self, run: _Run, s: SubTask, code: str | None) -> AgentId | None:
        if s.id in run.fallen_back or code in NO_RETRY_CODES or code in HALT_CODES:
            return None
        target = s.fallback_agent or (FALLBACK_AGENTS.get(s.agent) if s.skill is None else None)
        if target is None or target == s.agent or not self.manager.is_available(target):
            return None
        keeps_skill = bool(s.skill) and s.skill in self.manager.agent(target).skills
        if not keeps_skill and not self.ai:
            return None      # without its skill the step needs the model loop
        return target

    def _helper(self, s: SubTask, error: str, lang: str) -> SubTask:
        query = f"{s.title}: {_clip(error, 300)}"
        return SubTask(
            agent=AgentId.RESEARCH, key=f"{s.key or 'step'}_fix",
            title=t(lang, f"מחקר: פתרון לבעיה ב'{s.title}'", f"Research a fix for '{s.title}'"),
            instruction=t(lang, f"מצא איך לפתור את הבעיה הזאת ב'{s.title}': {_clip(error, 800)}. סכם פתרונות מעשיים, "
                                "צעד אחר צעד, עם מקורות.",
                          f"Find how to fix this problem in '{s.title}': {_clip(error, 800)}. Summarize practical, "
                          "step-by-step fixes with sources."),
            skill="research", params={"query": query, "save": False}, optional=True, followup_of=s.id,
            priority=Priority.HIGH,
        )

    def _max_followups(self, task: JarvisTask) -> int:
        n = max(0, int(self.cfg.max_followups))
        return n * 2 if task.mode == Mode.AUTONOMOUS else n

    async def _autonomous_followups(self, run: _Run) -> bool:
        """Autonomous mode: check the goal itself and add follow-up steps when something is missing."""
        task = run.task
        plan = task.plan
        assert plan is not None
        if task.mode != Mode.AUTONOMOUS or not self.ai or run.stop or run.halt or run.budget_hit:
            return False
        if task.followups >= self._max_followups(task):
            return False
        if any(not s.optional and s.status != TaskStatus.COMPLETED for s in plan.subtasks):
            return False
        lang = task.language
        await self._note(task, "verify", t(lang, "JARVIS בודק אם המטרה הושגה", "JARVIS is checking whether the goal is "
                                                                              "reached"), activity=True)
        task.status = TaskStatus.VERIFYING
        await self._emit(task, force=True)
        check = await self.verifier.goal_check(task)
        task.status = TaskStatus.RUNNING
        if check is None or check.achieved or not check.steps:
            if check is not None:
                await self._note(task, "verify", t(lang, "המטרה הושגה.", "The goal is reached."), level="success")
            return False
        try:
            extra = plan_from_draft(PlanDraft(title=task.title, strategy="", steps=check.steps[:3]), max_steps=3)
        except PlanError as exc:
            log.warning("Follow-up steps were not usable: %s", exc.message)
            return False
        task.followups += 1
        round_no = task.followups
        for sub in extra.subtasks:
            sub.key = f"followup{round_no}_{sub.key}"
            sub.followup_of = "goal"
        plan.subtasks.extend(extra.subtasks)
        plan.validate_dag()
        missing = "; ".join(check.missing[:3]) or t(lang, "השלמות", "finishing touches")
        await self._note(task, "followup", t(lang, f"חסר עדיין: {missing}. מוסיף {len(extra.subtasks)} שלבי המשך.",
                                             f"Still missing: {missing}. Adding {len(extra.subtasks)} follow-up steps."),
                         activity=True)
        for sub in extra.subtasks:
            await self._emit_subtask(task, sub)
        await self._save(task, force=True)
        return True

    # -- context and priority --------------------------------------------------------------------------
    def _context(self, run: _Run, s: SubTask) -> dict[str, Any]:
        task = run.task
        plan = task.plan
        assert plan is not None
        dep_ids = list(s.depends_on)
        if s.followup_of == "goal":     # follow-ups see everything that was done
            dep_ids = [x.id for x in plan.subtasks if x.status == TaskStatus.COMPLETED and x.id != s.id][-8:] + dep_ids
        deps = []
        for dep_id in dict.fromkeys(dep_ids):
            dep = plan.get(dep_id)
            if dep.status == TaskStatus.COMPLETED:
                deps.append(dependency_record(dep))
        context: dict[str, Any] = {"goal": task.goal, "dependencies": deps}
        if task.context.get("memory"):
            context["memory"] = task.context["memory"]
        if run.feedback.get(s.id):
            context["feedback"] = run.feedback[s.id]
        if plan.project_dir:
            context["project"] = {"path": plan.project_dir}
        return context

    @staticmethod
    def _priority(task: JarvisTask, wanted: Priority) -> Priority:
        """A step's priority within its task: a CRITICAL task makes every step critical, a LOW one keeps
        every step low; otherwise steps keep their plan priority (never critical)."""
        if task.priority == Priority.CRITICAL:
            return Priority.CRITICAL
        if task.priority == Priority.LOW:
            return Priority.LOW
        if task.priority == Priority.HIGH:
            return Priority(min(int(wanted), int(Priority.HIGH)))
        return Priority(max(int(wanted), int(Priority.HIGH)))

    @staticmethod
    def _who(s: SubTask, lang: str) -> str:
        return AGENT_INFO[s.agent]["name_he" if lang == "he" else "name"]

    # -- the end ------------------------------------------------------------------------------------
    def _settle(self, run: _Run, status: TaskStatus) -> None:
        task = run.task
        task.status = status
        if task.plan is not None:
            for s in task.plan.subtasks:
                if s.status in ACTIVE_STEP:
                    s.status = TaskStatus.PENDING if status == TaskStatus.INTERRUPTED else TaskStatus.CANCELLED
        if status != TaskStatus.INTERRUPTED:
            task.finished_at = utcnow()
        with contextlib.suppress(Exception):
            task.outcome = template_outcome(task)

    async def _conclude(self, run: _Run) -> None:
        task = run.task
        plan = task.plan
        lang = task.language
        stop = run.stop
        paused = stop == "shutdown" or (stop is None and run.halt is not None)
        if plan is not None:
            for s in plan.subtasks:
                if s.status in ACTIVE_STEP:
                    s.status = TaskStatus.PENDING if paused else TaskStatus.CANCELLED
                    if not paused:
                        s.finished_at = utcnow()
        if paused:
            status = TaskStatus.INTERRUPTED
            task.error = run.halt or run.stop_reason or None
        elif stop == "cancel":
            status = TaskStatus.CANCELLED
            task.error = None
            for s in plan.subtasks if plan else []:
                if s.status == TaskStatus.PENDING:
                    s.status, s.finished_at = TaskStatus.CANCELLED, utcnow()
        elif stop == "timeout":
            status = TaskStatus.FAILED
            task.error = run.stop_reason
            for s in plan.subtasks if plan else []:
                if s.status == TaskStatus.PENDING:
                    s.status, s.finished_at, s.error = TaskStatus.CANCELLED, utcnow(), run.stop_reason
        elif plan is None:
            status = TaskStatus.FAILED
            task.error = task.error or t(lang, "לא נוצרה תוכנית למשימה", "No plan was made for the task")
        else:
            for s in plan.subtasks:
                if s.status == TaskStatus.PENDING:
                    reason = (t(lang, "לא הופעל: המשימה הגיעה למגבלת ההפעלות", "Not run: the task reached its "
                                "assignment limit") if run.budget_hit else t(lang, "לא הופעל", "Not run"))
                    s.status, s.finished_at, s.error = (TaskStatus.FAILED if run.budget_hit else TaskStatus.SKIPPED,
                                                        utcnow(), reason)
            bad = [s for s in plan.subtasks if not s.optional and s.status != TaskStatus.COMPLETED]
            status = TaskStatus.FAILED if bad else TaskStatus.COMPLETED
            task.error = None if not bad else (bad[0].error or t(lang, "שלב חובה לא הושלם", "A required step did not "
                                                                                          "complete"))
        missing: list[str] = []
        if status == TaskStatus.COMPLETED and plan is not None:
            task.status = TaskStatus.VERIFYING
            await self._note(task, "verify", t(lang, "JARVIS מאמת את התוצאות", "JARVIS verification started"),
                             activity=True)
            await self._emit(task, force=True)
            files = [f for s in plan.subtasks if s.status == TaskStatus.COMPLETED for f in s.files_created]
            missing = await self.verifier.missing_files(files[:500])
        task.status = status
        router = self.runtime.router if status in (TaskStatus.COMPLETED, TaskStatus.FAILED) else None
        try:
            task.outcome = await asyncio.wait_for(synthesize(task, router, missing_files=missing), SYNTHESIS_TIMEOUT_S)
        except TimeoutError:
            task.outcome = template_outcome(task, missing_files=missing)
        if status != TaskStatus.INTERRUPTED:
            task.finished_at = utcnow()
        summary = _clip(task.outcome.summary, 300)
        if status == TaskStatus.COMPLETED:
            await self._note(task, "completed", t(lang, f"המשימה הושלמה: {summary}", f"Task completed: {summary}"),
                             level="success", activity=True)
        elif status == TaskStatus.FAILED:
            await self._note(task, "failed", t(lang, f"המשימה נכשלה: {summary}", f"Task failed: {summary}"),
                             level="error", activity=True)
        elif status == TaskStatus.CANCELLED:
            await self._note(task, "cancelled", t(lang, "המשימה בוטלה.", "The task was cancelled."), level="warning",
                             activity=True)
        else:
            await self._note(task, "interrupted", t(lang, f"המשימה נעצרה באמצע ({task.error}); אפשר להמשיך אותה.",
                                                    f"The task paused ({task.error}); it can be resumed."),
                             level="warning", activity=True)
        await self._save(task, force=True)
        await self._emit(task, force=True)
        await self._notify("finished", task)

    async def _cancel_idle(self, task: JarvisTask, reason: str | None) -> None:
        lang = task.language
        task.status, task.finished_at, task.error = TaskStatus.CANCELLED, utcnow(), None
        if task.plan is not None:
            for s in task.plan.subtasks:
                if s.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED):
                    s.status, s.finished_at = TaskStatus.CANCELLED, utcnow()
        task.outcome = template_outcome(task)
        await self._note(task, "cancelled", t(lang, f"המשימה בוטלה: {reason}" if reason else "המשימה בוטלה.",
                                              f"Task cancelled: {reason}" if reason else "The task was cancelled."),
                         level="warning", activity=True)
        await self._save(task, force=True)
        await self._emit(task, force=True)
        await self._notify("finished", task)

    def _stop(self, run: _Run, kind: str, reason: str) -> None:
        if run.stop is None:
            run.stop, run.stop_reason = kind, reason
        run.token.cancel(reason)
        run.wake.set()

    # ================================================================== bus events
    async def _on_progress(self, msg: Message) -> None:
        run = self._runs.get(msg.task_id or "")
        if run is None or run.task.plan is None:
            return
        try:
            s = run.task.plan.get(str(msg.data.get("subtask_id")))
        except KeyError:
            return
        if s.id not in run.running:
            return
        started = s.status == TaskStatus.QUEUED
        if started:
            s.status = TaskStatus.RUNNING
        value = msg.data.get("progress")
        if isinstance(value, (int, float)):
            s.progress = max(s.progress, min(0.99, float(value)))
        await self._emit_subtask(run.task, s)
        await self._emit(run.task, force=started)
        await self._save(run.task)

    async def _on_confirm_request(self, msg: Message) -> None:
        run = self._runs.get(msg.task_id or str(msg.data.get("task_id") or ""))
        if run is None or run.task.plan is None:
            return
        agent, conf_id = msg.data.get("agent_id"), msg.data.get("id")
        s = next((x for x in run.task.plan.subtasks if x.id in run.running and x.agent.value == agent), None)
        if s is None or not conf_id:
            return
        task = run.task
        lang = task.language
        run.confirmations[str(conf_id)] = s.id
        s.status = TaskStatus.WAITING
        task.status = TaskStatus.WAITING
        await self._note(task, "confirm", t(lang, f"{self._who(s, lang)} ממתין לאישור שלך: {msg.data.get('summary', '')}",
                                            f"The {self._who(s, lang)} is waiting for your approval: "
                                            f"{msg.data.get('summary', '')}"),
                         agent_id=s.agent.value, subtask_id=s.id, level="warning")
        await self._emit_subtask(task, s)
        await self._emit(task, force=True)
        await self._save(task, force=True)
        await self._notify("waiting", task)

    async def _on_confirm_response(self, msg: Message) -> None:
        conf_id = str(msg.data.get("id") or "")
        for run in list(self._runs.values()):
            sid = run.confirmations.pop(conf_id, None)
            if sid is None or run.task.plan is None:
                continue
            task = run.task
            lang = task.language
            s = task.plan.get(sid)
            if s.status == TaskStatus.WAITING and sid not in run.confirmations.values():
                s.status = TaskStatus.RUNNING
            if task.status == TaskStatus.WAITING and not run.confirmations:
                task.status = TaskStatus.RUNNING
            status = str(msg.data.get("status") or "")
            text = {"approved": t(lang, "אושר", "approved"), "denied": t(lang, "נדחה", "denied"),
                    "expired": t(lang, "לא התקבלה תשובה בזמן, ולכן נדחה", "no answer in time, so denied"),
                    "cancelled": t(lang, "בוטל", "cancelled")}.get(status, status)
            await self._note(task, "confirm", f"{msg.data.get('summary', '')}: {text}", agent_id=s.agent.value,
                             subtask_id=s.id, level="success" if status == "approved" else "warning")
            await self._emit_subtask(task, s)
            await self._emit(task, force=True)

    async def _on_kill(self, evt: KillEvent) -> None:
        if not evt.engaged or evt.agent_id is not None:
            return       # one agent's switch pauses only the tasks that need it (seen in its results)
        for run in list(self._runs.values()):
            lang = run.task.language
            run.halt = run.halt or t(lang, f"מתג החירום הופעל ({evt.reason})", f"the kill switch was engaged "
                                     f"({evt.reason})")
            run.wake.set()

    # ================================================================== events, saving, listeners
    async def _note(self, task: JarvisTask, kind: str, message: str, *, agent_id: str | None = None,
                    subtask_id: str | None = None, level: str = "info", activity: bool = False) -> None:
        if level == "debug" and task.mode != Mode.DEBUG:
            return
        entry = task.note(kind, message, agent_id=agent_id, subtask_id=subtask_id, level=level)
        with contextlib.suppress(Exception):
            await self.bus.publish(event("task.timeline", {"task_id": task.id, "entry": entry.view()},
                                         sender="orchestrator", task_id=task.id, agent_id=agent_id))
        if activity and self.runtime.activity is not None:
            try:
                await self.runtime.activity.log(message, source="JARVIS", level=level,  # type: ignore[arg-type]
                                                task_id=task.id, agent_id=agent_id, kind="task")
            except Exception:  # pragma: no cover
                log.exception("activity log failed")

    async def _emit(self, task: JarvisTask, *, force: bool = False) -> None:
        run = self._runs.get(task.id)
        now = time.monotonic()
        if run is not None and not force and now - run.last_emit < EMIT_INTERVAL_S:
            if not run.emit_pending:
                run.emit_pending = True
                asyncio.get_running_loop().call_later(EMIT_INTERVAL_S,
                                                      lambda: self._spawn(self._emit(task, force=True)))
            return
        if run is not None:
            run.last_emit, run.emit_pending = now, False
        task.touch()
        with contextlib.suppress(Exception):
            await self.bus.publish(event("task.updated", task.summary_view(), sender="orchestrator", task_id=task.id))

    async def _emit_subtask(self, task: JarvisTask, s: SubTask) -> None:
        with contextlib.suppress(Exception):
            await self.bus.publish(event("task.subtask", {"task_id": task.id, "subtask": s.view()},
                                         sender="orchestrator", task_id=task.id, agent_id=s.agent.value))

    async def _save(self, task: JarvisTask, *, force: bool = False) -> None:
        memory = self.memory
        if memory is None:
            return
        run = self._runs.get(task.id)
        now = time.monotonic()
        if run is not None and not force and now - run.last_save < SAVE_INTERVAL_S:
            if not run.save_pending:
                run.save_pending = True
                asyncio.get_running_loop().call_later(SAVE_INTERVAL_S,
                                                      lambda: self._spawn(self._save(task, force=True)))
            return
        if run is not None:
            run.last_save, run.save_pending = now, False
        task.touch()
        try:
            await memory.save_task(task.to_record())
        except Exception:
            log.exception("Saving task %s failed", task.id)

    async def _notify(self, kind: str, task: JarvisTask) -> None:
        for listener in list(self._listeners):
            try:
                res = listener(kind, task)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("Task listener failed (%s)", kind)

    def _keep(self, task: JarvisTask) -> None:
        self._tasks.pop(task.id, None)
        self._tasks[task.id] = task
        self._trim()

    def _trim(self) -> None:
        if len(self._tasks) <= RAM_TASKS:
            return
        for task_id in list(self._tasks):
            if len(self._tasks) <= RAM_TASKS:
                break
            if task_id not in self._runs:
                del self._tasks[task_id]

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)


def _clip(text: str, limit: int) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


__all__ = ["TaskListener", "TaskOrchestrator"]
