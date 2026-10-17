"""Agent Manager: lifecycle, mailboxes, priorities, concurrency, cancellation and live status of the
five agents.

* Every assignment travels on the bus: `submit()` publishes a `task.assign` Message to the agent's
  priority mailbox; the agent's workers take the most urgent one; the answer is published as a
  `task.result` Message correlated to the assignment and also resolves the caller's future.
* Each agent runs `concurrency` assignments at once plus one extra slot reserved for CRITICAL work.
* Timeouts and cancellation are cooperative first (the cancel token reaches tools, model calls
  and child processes), then forced after a short grace period, so nothing keeps running.
* Kill switches (global or per agent) cancel running work, drop queued work, kill the agent's
  child processes and refuse new work until released.
* Status (`offline, idle, thinking, working, waiting, completed, error`), current task, progress,
  last action, recent errors, permissions, queue and stats are published as `agent.status`
  events in the `AgentView` shape of docs/API.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jarvis.agents.base import (
    AGENT_INFO,
    AgentResult,
    AgentRuntime,
    AgentTask,
    BaseAgent,
    RunContext,
    agent_address,
    t,
)
from jarvis.ai.context import agent_scope
from jarvis.bus.message import Message, event
from jarvis.common.cancel import CancelToken
from jarvis.common.clock import iso, utcnow
from jarvis.common.enums import AgentId, AgentStatus, MessageType, Priority, ResultStatus
from jarvis.common.errors import (
    JarvisError,
    KillSwitchEngaged,
    ProviderError,
    ProviderRefusal,
    ProviderUnavailable,
    TaskCancelled,
)
from jarvis.memory.models import ResultRecord
from jarvis.permissions.confirm import ConfirmationBroker
from jarvis.security.killswitch import KillEvent, KillSwitch

log = logging.getLogger(__name__)

GRACE_S = 10.0              # after cancel/timeout, how long an agent may take to stop by itself
COMPLETED_HOLD_S = 4.0      # how long "completed" shows before the agent is idle again
ERROR_HOLD_S = 10.0
EMIT_INTERVAL_S = 0.25      # progress/last-action updates are throttled to 4 per second per agent
REMEMBER_TIMEOUT_S = 5.0    # recording an assignment's files and result in memory


@dataclass
class _Assignment:
    msg: Message
    task: AgentTask
    future: asyncio.Future[AgentResult]
    token: CancelToken
    state: str = "queued"                   # queued | running | done
    phase: AgentStatus = AgentStatus.THINKING
    progress: float = 0.0
    message: str = ""
    started: float | None = None
    started_at: datetime | None = None
    timed_out: bool = False
    confirmations: set[str] = field(default_factory=set)
    last_progress_emit: float = 0.0
    timeout_s: float = 0.0                  # the working time it has (a long tool call can extend it)
    deadline: float = 0.0                   # loop time when it runs out
    timer: asyncio.TimerHandle | None = None

    @property
    def id(self) -> str:
        return self.msg.id


@dataclass
class _AgentState:
    agent: BaseAgent
    enabled: bool = True
    rest: AgentStatus = AgentStatus.IDLE     # status while nothing is running
    workers: list[asyncio.Task[None]] = field(default_factory=list)
    running: dict[str, _Assignment] = field(default_factory=dict)
    last_action: str | None = None
    last_action_at: datetime | None = None
    errors: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=10))
    completed: int = 0
    failed: int = 0
    durations: deque[float] = field(default_factory=lambda: deque(maxlen=50))
    rest_handle: asyncio.TimerHandle | None = None
    last_emit: float = 0.0
    emit_pending: bool = False
    current_id: str | None = None

    @property
    def id(self) -> AgentId:
        return self.agent.id


class AgentManager:
    def __init__(
        self,
        runtime: AgentRuntime,
        kill_switch: KillSwitch,
        *,
        broker: ConfirmationBroker | None = None,
        agents: Iterable[BaseAgent] | None = None,
    ) -> None:
        self.runtime = runtime
        self.bus = runtime.bus
        self.kill_switch = kill_switch
        self.broker = broker
        if agents is None:
            from jarvis.agents import create_agents

            agents = create_agents(runtime)
        self._states: dict[AgentId, _AgentState] = {a.id: _AgentState(agent=a) for a in agents}
        missing = set(AgentId) - set(self._states)
        if missing:
            raise ValueError(f"The agent roster must have all five agents; missing {sorted(missing)}")
        self._assignments: dict[str, _Assignment] = {}
        self._subs: list[Any] = []
        self._started = False
        self._tasks: set[asyncio.Task[Any]] = set()

    # ================================================================== lifecycle
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for st in self._states.values():
            self.bus.register_mailbox(agent_address(st.id))
            st.enabled = self.runtime.settings.agents.autostart
            if st.enabled:
                self._start_workers(st)
        self._subs.append(self.bus.subscribe("confirm.request", self._on_confirm_request, name="agents.confirm"))
        self._subs.append(self.bus.subscribe("confirm.response", self._on_confirm_response, name="agents.confirm"))
        self.kill_switch.add_listener(self._on_kill)
        for st in self._states.values():
            await self._emit(st, force=True)

    async def shutdown(self) -> None:
        for a in list(self._assignments.values()):
            if a.state != "done":
                a.token.cancel("JARVIS is shutting down")
        for st in self._states.values():
            await self._drain_queue(st, "JARVIS is shutting down")
        deadline = time.monotonic() + GRACE_S
        while any(st.running for st in self._states.values()) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        for st in self._states.values():
            await self._stop_workers(st)
            if st.rest_handle is not None:
                st.rest_handle.cancel()
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()
        for task in list(self._tasks):
            task.cancel()
        self._started = False

    def _start_workers(self, st: _AgentState) -> None:
        st.workers = [w for w in st.workers if not w.done()]
        if st.workers:
            return
        slots = max(1, int(self.runtime.settings.agents.concurrency.get(st.id.value, 1)))
        for i in range(slots):
            st.workers.append(asyncio.create_task(self._worker(st, reserved=False), name=f"{st.id.value}-worker-{i}"))
        st.workers.append(asyncio.create_task(self._worker(st, reserved=True), name=f"{st.id.value}-critical"))

    async def _stop_workers(self, st: _AgentState) -> None:
        workers, st.workers = st.workers, []
        for w in workers:
            w.cancel()
        for w in workers:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(w, timeout=GRACE_S)

    # ================================================================== submitting work
    def agent(self, agent: AgentId | str) -> BaseAgent:
        return self._states[AgentId.parse(agent)].agent

    def agents(self) -> list[BaseAgent]:
        return [st.agent for st in self._states.values()]

    def is_available(self, agent: AgentId | str) -> bool:
        st = self._states[AgentId.parse(agent)]
        return st.enabled and not self.kill_switch.is_engaged(st.id.value)

    def skill_for_intent(self, intent: str) -> tuple[AgentId, str] | None:
        for st in self._states.values():
            name = st.agent.skill_for_intent(intent)
            if name is not None:
                return st.id, name
        return None

    async def submit(self, task: AgentTask, *, cancel: CancelToken | None = None) -> asyncio.Future[AgentResult]:
        """Queue an assignment; the returned future resolves with the agent's result (never raises)."""
        st = self._states[task.agent]
        future: asyncio.Future[AgentResult] = asyncio.get_running_loop().create_future()
        lang = task.language
        if self.kill_switch.is_engaged(st.id.value):
            future.set_result(self._stamp(AgentResult.cancelled(
                t(lang, f"מתג החירום פעיל: {st.agent.name_he} לא מקבל משימות.",
                  f"The kill switch is engaged: {st.agent.name} is not taking work."),
                data={"code": "kill_switch"}), task))
            return future
        if not st.enabled:
            future.set_result(self._stamp(AgentResult.failed(
                t(lang, f"{st.agent.name_he} כבוי. אפשר להפעיל אותו מחדש מלוח הסוכנים או בפקודה.",
                  f"{st.agent.name} is stopped. Start it again from the Agents panel or by command."),
                data={"code": "agent_stopped"}), task))
            return future
        token = cancel.child() if cancel is not None else CancelToken()
        msg = task.to_message(permissions=self.runtime.permissions.capabilities(st.id.value, task.task_id))
        a = _Assignment(msg=msg, task=task, future=future, token=token)
        self._assignments[msg.id] = a
        token.on_cancel(lambda reason: self._on_token_cancelled(a, reason))
        if token.cancelled:  # the caller's task was already cancelled; answered above
            return future
        await self.bus.publish(msg)
        await self._log(t(lang, f"JARVIS העביר ל{st.agent.name_he}: {task.display_title}",
                          f"JARVIS assigned to {st.agent.name}: {task.display_title}"),
                        st, task.task_id, kind="assign")
        await self._emit(st, force=True)
        return future

    async def run(self, task: AgentTask, *, cancel: CancelToken | None = None) -> AgentResult:
        """Submit and wait for the result."""
        future = await self.submit(task, cancel=cancel)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await self.cancel_subtask(task.id, "the caller stopped waiting")
            raise

    def _on_token_cancelled(self, a: _Assignment, reason: str) -> None:
        if a.state == "queued" and not a.future.done():
            # Still waiting in the mailbox: answer now and take it out of the queue.
            a.state = "done"
            a.future.set_result(self._stamp(AgentResult.cancelled(reason), a.task))
            self._assignments.pop(a.id, None)
            box = self.bus.mailbox(agent_address(a.task.agent))
            if box is not None:
                self._spawn(box.remove(lambda m: m.id == a.id))
            self._spawn(self._emit(self._states[a.task.agent], force=True))

    async def cancel_task(self, task_id: str, reason: str = "cancelled by user") -> int:
        """Cancel every assignment (queued or running) of a JARVIS task."""
        hits = [a for a in self._assignments.values() if a.task.task_id == task_id and a.state != "done"]
        for a in hits:
            a.token.cancel(reason)
        if self.broker is not None:
            self.broker.cancel_task(task_id, reason)
        await asyncio.to_thread(self.runtime.executor.services.processes.kill, task_id=task_id)
        return len(hits)

    async def cancel_subtask(self, subtask_id: str, reason: str = "cancelled") -> bool:
        for a in list(self._assignments.values()):
            if a.task.id == subtask_id and a.state != "done":
                a.token.cancel(reason)
                return True
        return False

    def active_assignments(self) -> list[dict[str, Any]]:
        return [{"agent": a.task.agent.value, "task_id": a.task.task_id, "subtask_id": a.task.id,
                 "title": a.task.display_title, "state": a.state, "progress": round(a.progress, 3)}
                for a in self._assignments.values() if a.state != "done"]

    # ================================================================== workers
    async def _worker(self, st: _AgentState, *, reserved: bool) -> None:
        box = self.bus.register_mailbox(agent_address(st.id))

        def wanted(m: Message) -> bool:
            return m.type == MessageType.TASK_ASSIGN and (not reserved or m.priority == Priority.CRITICAL)

        while True:
            msg = await box.get(wanted)
            a = self._assignments.get(msg.id)
            if a is None or a.future.done():
                continue
            try:
                await self._execute(st, a, msg)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - _execute handles errors; never lose a worker
                log.exception("Agent worker crashed while running %s", a.task.id)

    async def _execute(self, st: _AgentState, a: _Assignment, msg: Message) -> None:
        agent = st.agent
        task = AgentTask.from_message(msg)       # the agent works from the (sanitized) bus message
        lang = task.language
        a.state, a.started, a.started_at = "running", time.perf_counter(), utcnow()   # monotonic() ticks every 16 ms on Windows
        a.phase = AgentStatus.THINKING if not task.skill and agent.ai_available else AgentStatus.WORKING
        st.running[a.id] = a
        st.current_id = a.id
        self._cancel_rest_timer(st)
        await self._emit(st, force=True)
        await self._log(t(lang, f"{agent.name_he} התחיל: {task.display_title}", f"{agent.name} started: {task.display_title}"),
                        st, task.task_id, kind="agent")
        await self._publish_progress(a, 0.0, "started", force=True)

        a.timeout_s = task.timeout_s or float(self.runtime.settings.agents.timeouts_s.get(st.id.value, 300.0))
        self._arm_timer(a, a.timeout_s)
        run = RunContext(agent, task, a.token, reporter=self, assignment_id=a.id)
        with agent_scope(st.id.value):           # every model call of this assignment uses the agent's provider
            handle = asyncio.ensure_future(agent.handle(task, run))
        try:
            result = await self._await_handle(handle, a)
        except asyncio.CancelledError:
            a.token.cancel("JARVIS is shutting down")
            handle.cancel()
            self._finish(st, a, self._stamp(run.result(status=ResultStatus.CANCELLED, progress=a.progress,
                                                       summary="cancelled"), task))
            raise
        except (TaskCancelled, KillSwitchEngaged) as exc:
            result = run.result(status=ResultStatus.CANCELLED, progress=a.progress,
                                summary=t(lang, f"בוטל: {exc.message}", f"Cancelled: {exc.message}"))
        except ProviderRefusal as exc:
            result = run.result(status=ResultStatus.FAILED, progress=a.progress,
                                summary=t(lang, "מודל ה-AI סירב לבקשה הזאת.", "The AI model declined this request."),
                                errors=[exc.message], data={"code": "refusal", **exc.details})
        except ProviderUnavailable as exc:
            result = run.result(status=ResultStatus.FAILED, progress=a.progress, summary=exc.message,
                                errors=[exc.message], data={"code": "ai_unavailable"})
        except ProviderError as exc:
            result = run.result(status=ResultStatus.FAILED, progress=a.progress,
                                summary=t(lang, f"שגיאה מול ספק ה-AI: {exc.message}", f"AI provider error: {exc.message}"),
                                errors=[exc.message], data={"code": "ai_error", "retryable": exc.retryable})
        except JarvisError as exc:
            result = run.result(status=ResultStatus.FAILED, progress=a.progress, summary=exc.message,
                                errors=[exc.message], data={"code": exc.code})
        except Exception as exc:
            log.exception("%s crashed on %s", agent.name, task.id)
            result = run.result(status=ResultStatus.FAILED, progress=a.progress,
                                summary=t(lang, f"תקלה פנימית ב{agent.name_he}: {exc}", f"{agent.name} hit an internal error: {exc}"),
                                errors=[f"{type(exc).__name__}: {exc}"], data={"code": "internal_error"})
        finally:
            if a.timer is not None:
                a.timer.cancel()

        if a.timed_out:
            timeout = a.timeout_s
            note = t(lang, f"{agent.name_he} חרג מזמן העבודה ({timeout:.0f} שניות).",
                     f"{agent.name} ran out of time ({timeout:.0f}s).")
            result.status = ResultStatus.FAILED
            result.errors = [*result.errors, note]
            result.summary = note if not result.summary or result.summary.startswith(("Cancelled", "בוטל")) else \
                f"{note} {result.summary}"
            result.data.setdefault("code", "timeout")
        elif a.token.cancelled and result.status != ResultStatus.CANCELLED:
            result.status = ResultStatus.CANCELLED
            result.errors = [*result.errors, a.token.reason or "cancelled"]
        if result.status in (ResultStatus.CANCELLED, ResultStatus.FAILED) and a.token.cancelled:
            await asyncio.to_thread(self.runtime.executor.services.processes.kill, owner=st.id.value,
                                    task_id=task.task_id)
        self._stamp(result, task)
        await self._remember(task, result)
        self._finish(st, a, result)
        await self.bus.publish(a.msg.reply(type=MessageType.TASK_RESULT, topic=MessageType.TASK_RESULT.value,
                                           sender=agent_address(st.id), status=result.status.value,
                                           result=result.model_dump(mode="json")))
        await self._log(self._result_line(agent, task, result), st, task.task_id,
                        level="success" if result.status == ResultStatus.SUCCESS else "info" if result.asks_user else
                        "warning" if result.status in (ResultStatus.PARTIAL, ResultStatus.CANCELLED) else "error",
                        kind="result")
        await self._after_run(st, result)

    def _arm_timer(self, a: _Assignment, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        if a.timer is not None:
            a.timer.cancel()
        a.deadline = loop.time() + seconds

        def on_timeout() -> None:
            a.timed_out = True
            a.token.cancel(f"timeout after {a.timeout_s:.0f}s")

        a.timer = loop.call_later(seconds, on_timeout)

    async def _await_handle(self, handle: asyncio.Future[AgentResult], a: _Assignment) -> AgentResult:
        waiter: asyncio.Future[Any] = asyncio.ensure_future(a.token.wait())
        try:
            done, _ = await asyncio.wait({handle, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if handle in done:
                return handle.result()
            try:  # cancelled or timed out: let the agent stop cooperatively first
                return await asyncio.wait_for(asyncio.shield(handle), timeout=GRACE_S)
            except TimeoutError:
                handle.cancel()
                with contextlib.suppress(BaseException):
                    await handle
                raise TaskCancelled(a.token.reason or "cancelled") from None
        finally:
            waiter.cancel()

    def _finish(self, st: _AgentState, a: _Assignment, result: AgentResult) -> None:
        a.state = "done"
        st.running.pop(a.id, None)
        self._assignments.pop(a.id, None)
        if st.current_id == a.id:
            st.current_id = next(iter(st.running), None)
        if a.started is not None:
            result.duration_s = round(time.perf_counter() - a.started, 6)
            st.durations.append(result.duration_s)
        if result.status in (ResultStatus.SUCCESS, ResultStatus.PARTIAL):
            st.completed += 1
        elif result.status == ResultStatus.FAILED:
            st.failed += 1
            st.errors.appendleft({"at": iso(), "message": (result.errors[-1] if result.errors else result.summary)[:300]})
        if not a.future.done():
            a.future.set_result(result)

    async def _remember(self, task: AgentTask, result: AgentResult) -> None:
        """The files an assignment created or changed (recent files) and its result (task history) go to
        memory. Memory trouble never holds up or changes the result."""
        memory = self.runtime.memory
        if memory is None or (result.status == ResultStatus.CANCELLED and not result.files_created):
            return
        try:
            async with asyncio.timeout(REMEMBER_TIMEOUT_S):
                for kind, paths in (("created", result.files_created), ("modified", result.files_modified)):
                    for path in list(dict.fromkeys(paths))[:50]:
                        await memory.record_file(path, task_id=task.task_id, agent_id=task.agent.value, kind=kind)
                if task.task_id:
                    data: dict[str, Any] = {"title": task.display_title}
                    for key in ("files_created", "errors", "denied"):
                        items = getattr(result, key)
                        if items:
                            data[key] = [str(x)[:300] for x in items[:20]]
                    if result.next_step:
                        data["next_step"] = result.next_step[:500]
                    if result.data.get("code"):
                        data["code"] = result.data["code"]
                    await memory.add_result(ResultRecord(task_id=task.task_id, subtask_id=task.id,
                                                         agent_id=task.agent.value, status=result.status.value,
                                                         summary=(result.summary or "")[:2000], data=data))
        except Exception as exc:
            log.warning("Could not record %s's result in memory: %s", task.agent.value, exc)

    def _stamp(self, result: AgentResult, task: AgentTask) -> AgentResult:
        result.agent_id = task.agent.value
        result.task_id = task.task_id
        result.subtask_id = task.id
        return result

    @staticmethod
    def _result_line(agent: BaseAgent, task: AgentTask, result: AgentResult) -> str:
        lang = task.language
        summary = (result.summary or "").replace("\n", " ")[:220]
        if result.status == ResultStatus.SUCCESS:
            return t(lang, f"{agent.name_he} סיים: {summary}", f"{agent.name} completed: {summary}")
        if result.status == ResultStatus.PARTIAL:
            return t(lang, f"{agent.name_he} סיים חלקית: {summary}", f"{agent.name} partly completed: {summary}")
        if result.status == ResultStatus.CANCELLED:
            return t(lang, f"{agent.name_he} עצר: {summary}", f"{agent.name} stopped: {summary}")
        if result.asks_user:
            return t(lang, f"{agent.name_he} שואל: {summary}", f"{agent.name} asks: {summary}")
        return t(lang, f"{agent.name_he} נכשל: {summary}", f"{agent.name} failed: {summary}")

    async def _after_run(self, st: _AgentState, result: AgentResult) -> None:
        if not st.running:
            if result.status == ResultStatus.CANCELLED or result.asks_user:   # nothing went wrong
                st.rest = AgentStatus.IDLE
            else:
                st.rest = AgentStatus.ERROR if result.status == ResultStatus.FAILED else AgentStatus.COMPLETED
                hold = ERROR_HOLD_S if st.rest == AgentStatus.ERROR else COMPLETED_HOLD_S
                self._cancel_rest_timer(st)
                st.rest_handle = asyncio.get_running_loop().call_later(hold, self._back_to_idle, st)
        await self._emit(st, force=True)

    def _back_to_idle(self, st: _AgentState) -> None:
        st.rest_handle = None
        if not st.running and st.rest in (AgentStatus.COMPLETED, AgentStatus.ERROR):
            st.rest = AgentStatus.IDLE
            self._spawn(self._emit(st, force=True))

    def _cancel_rest_timer(self, st: _AgentState) -> None:
        if st.rest_handle is not None:
            st.rest_handle.cancel()
            st.rest_handle = None

    # ================================================================== Reporter (called by RunContext)
    async def phase(self, assignment_id: str, status: AgentStatus) -> None:
        a = self._assignments.get(assignment_id)
        if a is None or a.phase == status:
            return
        a.phase = status
        await self._emit(self._states[a.task.agent])

    async def progress(self, assignment_id: str, fraction: float, message: str = "") -> None:
        a = self._assignments.get(assignment_id)
        if a is None:
            return
        a.progress = max(a.progress, min(1.0, float(fraction)))
        if message:
            a.message = message
        await self._publish_progress(a, a.progress, message)
        await self._emit(self._states[a.task.agent])

    async def action(self, assignment_id: str, text: str) -> None:
        a = self._assignments.get(assignment_id)
        if a is None:
            return
        st = self._states[a.task.agent]
        st.last_action, st.last_action_at = text[:200], utcnow()
        await self._emit(st)

    async def extend_deadline(self, assignment_id: str, seconds: float) -> None:
        """A tool call that may run long (computer control) gets its time: the deadline never moves in."""
        a = self._assignments.get(assignment_id)
        if a is None or a.state != "running" or a.timed_out or a.token.cancelled:
            return
        remaining = a.deadline - asyncio.get_running_loop().time()
        if seconds <= remaining:
            return
        a.timeout_s += seconds - remaining
        self._arm_timer(a, seconds)

    async def _publish_progress(self, a: _Assignment, fraction: float, message: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - a.last_progress_emit < EMIT_INTERVAL_S:
            return
        a.last_progress_emit = now
        await self.bus.publish(Message(
            type=MessageType.TASK_PROGRESS, task_id=a.task.task_id, agent_id=a.task.agent.value,
            priority=a.task.priority, status=a.state, sender=agent_address(a.task.agent), correlation_id=None,
            parent_id=a.id, data={"subtask_id": a.task.id, "progress": round(fraction, 3), "message": message[:200]},
        ))

    # ================================================================== confirmations → WAITING
    async def _on_confirm_request(self, msg: Message) -> None:
        agent_id, conf_id = msg.data.get("agent_id"), msg.data.get("id")
        if not agent_id or not conf_id:
            return
        with contextlib.suppress(ValueError):
            st = self._states[AgentId.parse(agent_id)]
            for a in st.running.values():
                if a.task.task_id == msg.data.get("task_id") or msg.data.get("task_id") is None:
                    a.confirmations.add(conf_id)
            await self._emit(st, force=True)

    async def _on_confirm_response(self, msg: Message) -> None:
        conf_id = msg.data.get("id")
        for st in self._states.values():
            changed = False
            for a in st.running.values():
                if conf_id in a.confirmations:
                    a.confirmations.discard(conf_id)
                    changed = True
            if changed:
                await self._emit(st, force=True)

    # ================================================================== control
    async def start_agent(self, agent: AgentId | str) -> dict[str, Any]:
        st = self._states[AgentId.parse(agent)]
        await self._command(st, "start")
        st.enabled = True
        st.rest = AgentStatus.IDLE
        box = self.bus.register_mailbox(agent_address(st.id))
        await box.reopen()
        self._start_workers(st)
        await self._log(f"{st.agent.name} started", st, None, kind="agent")
        await self._emit(st, force=True)
        return self.view(st.id)

    async def stop_agent(self, agent: AgentId | str, reason: str = "stopped by the user") -> dict[str, Any]:
        st = self._states[AgentId.parse(agent)]
        await self._command(st, "stop")
        st.enabled = False
        await self._halt(st, reason)
        await self._stop_workers(st)
        st.rest = AgentStatus.OFFLINE
        await self._log(f"{st.agent.name} stopped", st, None, kind="agent", level="warning")
        await self._emit(st, force=True)
        return self.view(st.id)

    async def restart_agent(self, agent: AgentId | str) -> dict[str, Any]:
        st = self._states[AgentId.parse(agent)]
        await self._command(st, "restart")
        st.enabled = False
        await self._halt(st, "restarting")
        await self._stop_workers(st)
        st.agent = type(st.agent)(self.runtime)      # a fresh instance: no state carried over
        st.errors.clear()
        st.last_action, st.last_action_at = None, None
        if self.kill_switch.is_engaged(st.id.value):
            st.rest = AgentStatus.OFFLINE
            await self._emit(st, force=True)
            return self.view(st.id)
        st.enabled = True
        st.rest = AgentStatus.IDLE
        box = self.bus.register_mailbox(agent_address(st.id))
        await box.reopen()
        self._start_workers(st)
        await self._log(f"{st.agent.name} restarted", st, None, kind="agent")
        await self._emit(st, force=True)
        return self.view(st.id)

    async def kill_agent(self, agent: AgentId | str, reason: str = "kill switch") -> dict[str, Any]:
        st = self._states[AgentId.parse(agent)]
        await self.kill_switch.engage(reason, agent_id=st.id.value)   # → _on_kill
        return self.view(st.id)

    async def release_agent(self, agent: AgentId | str) -> dict[str, Any]:
        st = self._states[AgentId.parse(agent)]
        await self.kill_switch.release(agent_id=st.id.value)
        return self.view(st.id)

    async def stop_all(self, reason: str = "stopped by the user") -> list[dict[str, Any]]:
        return [await self.stop_agent(a, reason) for a in list(self._states)]

    async def start_all(self) -> list[dict[str, Any]]:
        return [await self.start_agent(a) for a in list(self._states)]

    async def _halt(self, st: _AgentState, reason: str) -> int:
        """Cancel running work, drop queued work, wait for the agent to go quiet."""
        n = await self._drain_queue(st, reason)
        for a in list(st.running.values()):
            a.token.cancel(reason)
            n += 1
        deadline = time.monotonic() + GRACE_S + 1
        while st.running and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return n

    async def _drain_queue(self, st: _AgentState, reason: str) -> int:
        box = self.bus.mailbox(agent_address(st.id))
        if box is None:
            return 0
        removed = await box.remove(lambda m: m.type == MessageType.TASK_ASSIGN)
        for msg in removed:
            a = self._assignments.pop(msg.id, None)
            if a is not None and not a.future.done():
                a.state = "done"
                a.future.set_result(self._stamp(AgentResult.cancelled(reason), a.task))
        return len(removed)

    async def _command(self, st: _AgentState, action: str) -> None:
        await self.bus.publish(Message(type=MessageType.AGENT_COMMAND, agent_id=st.id.value, instruction=action,
                                       sender="jarvis", data={"action": action}))

    async def _on_kill(self, evt: KillEvent) -> None:
        targets = list(self._states.values()) if evt.agent_id is None else [self._states[AgentId.parse(evt.agent_id)]]
        if evt.engaged:
            reason = f"kill switch: {evt.reason}"
            stopped = 0
            for st in targets:
                stopped += await self._drain_queue(st, reason)
                for a in list(st.running.values()):
                    a.token.cancel(reason)
                    stopped += 1
            if self.broker is not None:             # their open approvals go now, before the tasks report back
                self.broker.deny_all(reason, agents={st.id.value for st in targets})
            processes = self.runtime.executor.services.processes
            if evt.agent_id is None:
                await asyncio.to_thread(processes.kill)
            else:
                await asyncio.to_thread(processes.kill, owner=targets[0].id.value)
            for st in targets:
                self._cancel_rest_timer(st)
                st.rest = AgentStatus.OFFLINE
                await self._emit(st, force=True)
            who = "all agents" if evt.agent_id is None else targets[0].agent.name
            await self._log(f"Kill switch engaged for {who}: {stopped} assignment(s) stopped", None, None,
                            level="error", kind="killswitch")
        else:
            for st in targets:
                if st.enabled and not self.kill_switch.is_engaged(st.id.value):
                    st.rest = AgentStatus.IDLE
                    if not st.workers:
                        self._start_workers(st)
                await self._emit(st, force=True)
            who = "all agents" if evt.agent_id is None else targets[0].agent.name
            await self._log(f"Kill switch released for {who}", None, None, level="warning", kind="killswitch")

    # ================================================================== views
    def _status(self, st: _AgentState) -> AgentStatus:
        if self.kill_switch.is_engaged(st.id.value) or not st.enabled:
            return AgentStatus.OFFLINE if not st.running else AgentStatus.WORKING
        if st.running:
            items = list(st.running.values())
            if any(a.confirmations for a in items):
                return AgentStatus.WAITING
            if any(a.phase == AgentStatus.WORKING for a in items):
                return AgentStatus.WORKING
            return AgentStatus.THINKING
        return st.rest

    def view(self, agent: AgentId | str) -> dict[str, Any]:
        st = self._states[AgentId.parse(agent)]
        info = AGENT_INFO[st.id]
        current = st.running.get(st.current_id) if st.current_id else None
        if current is None and st.running:
            current = next(iter(st.running.values()))
        box = self.bus.mailbox(agent_address(st.id))
        queued = sum(1 for m in box.peek_all() if m.type == MessageType.TASK_ASSIGN) if box is not None else 0
        avg = round(sum(st.durations) / len(st.durations), 2) if st.durations else None
        return {
            "id": st.id.value,
            "number": st.agent.number,
            "name": info["name"],
            "name_he": info["name_he"],
            "role": info["role"],
            "role_he": info["role_he"],
            "status": self._status(st).value,
            "enabled": st.enabled,
            "killed": self.kill_switch.is_engaged(st.id.value),
            "current": {"task_id": current.task.task_id, "subtask_id": current.task.id,
                        "title": current.task.display_title} if current else None,
            "progress": round(current.progress, 3) if current else (1.0 if st.rest == AgentStatus.COMPLETED else 0.0),
            "last_action": st.last_action,
            "last_action_at": iso(st.last_action_at) if st.last_action_at else None,
            "errors": list(st.errors),
            "permissions": self.runtime.permissions.capabilities(st.id.value),
            "queue": queued,
            "running": len(st.running),
            "stats": {"completed": st.completed, "failed": st.failed, "avg_duration_s": avg},
            "skills": sorted(st.agent.skills),
            "ai": self.runtime.router.agent_info(st.id.value) if self.runtime.router is not None else None,
        }

    def views(self) -> list[dict[str, Any]]:
        return [self.view(a) for a in sorted(self._states, key=lambda a: self._states[a].agent.number)]

    async def publish_views(self) -> None:
        """Send every agent's card again (e.g. after the AI provider they work with changed)."""
        for st in self._states.values():
            await self._emit(st, force=True)

    def status(self, agent: AgentId | str) -> AgentStatus:
        return self._status(self._states[AgentId.parse(agent)])

    async def _emit(self, st: _AgentState, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - st.last_emit < EMIT_INTERVAL_S:
            if not st.emit_pending:
                st.emit_pending = True
                asyncio.get_running_loop().call_later(EMIT_INTERVAL_S, lambda: self._spawn(self._emit(st, force=True)))
            return
        st.emit_pending = False
        st.last_emit = now
        await self.bus.publish(event("agent.status", self.view(st.id), sender="agents", agent_id=st.id.value))

    # ================================================================== helpers
    async def _log(self, message: str, st: _AgentState | None, task_id: str | None, *, level: str = "info",
                   kind: str = "agent") -> None:
        activity = self.runtime.activity
        if activity is None:
            return
        try:
            await activity.log(message, source=st.agent.name if st else "JARVIS", level=level,  # type: ignore[arg-type]
                               task_id=task_id, agent_id=st.id.value if st else None, kind=kind)
        except Exception:  # pragma: no cover
            log.exception("activity log failed")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
