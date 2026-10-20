"""Stage 5 (Task Orchestrator): planning, parallel DAG execution, priorities, context passing, retries,
research help, fallback agents, verification, cancellation, timeouts, kill switch, resume after an
interruption, confirmations, autonomous follow-ups, the final answer and the saved history.

The five real agents run through the real Agent Manager, bus, permission gate and tools. Test skills
(`Probe`) are mixed into them to fail, block or report on demand; model calls use `ScriptedProvider`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import jarvis.orchestration.orchestrator as orchestrator_module
from agent_env import ProbeSystem, agent_env, until
from fakes import reply
from jarvis.agents import (
    AgentResult,
    AgentRuntime,
    AgentTask,
    BaseAgent,
    CreativeAgent,
    DevelopmentAgent,
    PlannerAgent,
    ResearchAgent,
    RunContext,
    skill,
)
from jarvis.ai.types import AIResponse, CompletionRequest
from jarvis.common.enums import AgentId, AgentStatus, Mode, Priority, ResultStatus, TaskStatus
from jarvis.common.errors import ProviderError, TaskRefused
from jarvis.core.intents import detect_intent
from jarvis.memory.models import TaskRecord
from jarvis.orchestration import JarvisTask, SubTask, TaskOrchestrator, TaskPlan
from jarvis.orchestration.recovery import backoff, decide, dispatch_budget, fingerprint
from jarvis.orchestration.task import compact, short_title
from jarvis.tools.base import ToolResult

S = TaskStatus


# ================================================================================== probe agents
class Probe:
    """Test skills mixed into each of the five real agents."""

    runtime: AgentRuntime
    id: AgentId
    name: str

    @skill(description="test: answer after an optional delay and report the context received")
    async def echo(self, task: AgentTask, run: RunContext, text: str = "", delay: float = 0.0) -> AgentResult:
        log = self.runtime.extras.setdefault("log", [])
        log.append(("start", task.title, time.monotonic()))
        if delay:
            await run.guard(asyncio.sleep(delay))
        log.append(("end", task.title, time.monotonic()))
        deps = task.context.get("dependencies") or []
        return run.result(summary=text or f"{task.title} done", result={
            "deps": [d.get("key") for d in deps], "dep_agents": [d.get("agent") for d in deps],
            "feedback": task.context.get("feedback"), "attempt": task.attempt, "allow_llm": task.allow_llm,
            "priority": task.priority.name.lower(), "goal": task.context.get("goal")})

    @skill(description="test: fail the first `failures` attempts of this step")
    async def flaky(self, task: AgentTask, run: RunContext, failures: int = 1, error: str = "temporary glitch",
                    code: str = "", vary: bool = False) -> AgentResult:
        counts = self.runtime.extras.setdefault("flaky", {})
        n = counts[task.id] = counts.get(task.id, 0) + 1
        self.runtime.extras.setdefault("contexts", {}).setdefault(task.id, []).append(
            {"context": dict(task.context), "agent": self.id.value, "allow_llm": task.allow_llm})
        if n <= failures:
            message = f"{error} {'abcdefghijklmnop'[n]}" if vary else error
            return AgentResult.failed(message, data={"code": code} if code else {})
        return run.result(summary=f"worked on attempt {n}")

    @skill(description="test: one action is declined by the user; report the rest as partly done")
    async def declined(self, task: AgentTask, run: RunContext) -> AgentResult:
        counts = self.runtime.extras.setdefault("declined", {})
        counts[task.id] = counts.get(task.id, 0) + 1
        run.record(ToolResult.fail("The user did not approve: delete old.txt (denied)", denied="confirmation",
                                   tool="delete_path"))
        return run.result(status=ResultStatus.PARTIAL, summary="cleaned up, but old.txt is still there")

    @skill(description="test: write a file, then the user says no to running it")
    async def wrote_then_declined(self, task: AgentTask, run: RunContext) -> AgentResult:
        counts = self.runtime.extras.setdefault("declined", {})
        counts[task.id] = counts.get(task.id, 0) + 1
        res = await run.tool("write_file", path="workspace/fib.py", content="print(1)")
        assert res.ok, res.error
        run.record(ToolResult.fail("The user did not approve: Run fib.py with Python (timed out)",
                                   denied="confirmation", tool="run_python"))
        return run.result(status=ResultStatus.PARTIAL, summary="I wrote fib.py.",
                          details="I wrote fib.py.\n\nIt is ready to run.")

    @skill(description="test: crash (raise) on the first `crashes` attempts of this step")
    async def crashy(self, task: AgentTask, run: RunContext, crashes: int = 1) -> AgentResult:
        counts = self.runtime.extras.setdefault("crashy", {})
        n = counts[task.id] = counts.get(task.id, 0) + 1
        if n <= crashes:
            raise RuntimeError(f"boom {n}")
        return run.result(summary=f"recovered on attempt {n}")

    @skill(description="test: only works on the given agent")
    async def only_on(self, task: AgentTask, run: RunContext, agent: str = "development") -> AgentResult:
        if self.id.value != agent:
            return AgentResult.failed(f"{self.name} cannot do this")
        return run.result(summary=f"done by {self.name}")

    @skill(description="test: write a real file through the tool layer")
    async def make_file(self, task: AgentTask, run: RunContext, name: str = "out.txt",
                        content: str = "hello") -> AgentResult:
        res = await run.tool("write_file", path=f"workspace/{name}", content=content)
        if not res.ok:
            return run.result(status=ResultStatus.FAILED, summary=res.error or "write failed")
        return run.result(summary=f"wrote {name}")

    @skill(description="test: claim a file that does not exist")
    async def fake_file(self, task: AgentTask, run: RunContext, name: str = "ghost.txt") -> AgentResult:
        return run.result(summary="wrote it", files_created=[str(self.runtime.paths.workspace / name)])

    @skill(description="test: block until the gate opens")
    async def block(self, task: AgentTask, run: RunContext) -> AgentResult:
        self.runtime.extras.setdefault("blocked", []).append(task.title)
        gate: asyncio.Event = self.runtime.extras.setdefault("gate", asyncio.Event())
        await run.guard(asyncio.wait_for(gate.wait(), 30))
        return run.result(summary="released")

    @skill(description="test: touch a file, then call the model (which may fail); offline it just succeeds")
    async def smart(self, task: AgentTask, run: RunContext) -> AgentResult:
        if task.allow_llm and self.runtime.ai_available and self.runtime.router is not None:
            await run.tool("write_file", path="workspace/smart-draft.txt", content="draft")
            text = await run.guard(self.runtime.router.ask("agent", "improve the draft"))
            return run.result(summary=f"model: {text}")
        return run.result(summary="did it offline")


class ProbeResearch(Probe, ResearchAgent):
    pass


class ProbeSystemAgent(Probe, ProbeSystem):
    pass


class ProbeDevelopment(Probe, DevelopmentAgent):
    pass


class ProbeCreative(Probe, CreativeAgent):
    pass


class ProbePlanner(Probe, PlannerAgent):
    pass


def probe_agents(runtime: AgentRuntime) -> list[BaseAgent]:
    return [ProbeResearch(runtime), ProbeSystemAgent(runtime), ProbeDevelopment(runtime), ProbeCreative(runtime),
            ProbePlanner(runtime)]


# ================================================================================== helpers
Step = tuple[SubTask, tuple[str, ...]]


def step(key: str, agent: str, skill_name: str | None = "echo", *, after: tuple[str, ...] = (), **kw: Any) -> Step:
    sub = SubTask(key=key, agent=AgentId.parse(agent), title=kw.pop("title", key),
                  instruction=kw.pop("instruction", f"do {key}"), skill=skill_name, params=kw.pop("params", {}), **kw)
    return sub, after


def plan_of(*steps: Step, title: str = "Test plan") -> TaskPlan:
    by_key = {sub.key: sub for sub, _ in steps}
    for sub, after in steps:
        sub.depends_on = [by_key[k].id for k in after]
    return TaskPlan(title=title, strategy="test", subtasks=[sub for sub, _ in steps], source="direct")


def responder(answers: dict[str, list[Any]]) -> Callable[[CompletionRequest], AIResponse]:
    """Answer model calls by output schema (structured calls) or by role; each list is consumed in order
    and its last item repeats."""

    def respond(request: CompletionRequest) -> AIResponse:
        key = request.schema_name if request.json_schema else request.role
        queue = answers.get(key) if key in answers else answers.get(request.role)
        if not queue:
            raise AssertionError(f"unexpected model call: {key} / {request.role}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(item):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, AIResponse):
            return item
        if isinstance(item, (dict, list)):
            return reply(json.dumps(item, ensure_ascii=False))
        return reply(str(item))

    return respond


class Orch:
    def __init__(self, env: Any, orch: TaskOrchestrator) -> None:
        self.env, self.orch = env, orch
        self.events: list[Any] = []
        env.kit.bus.subscribe("task.*", self.events.append, name="test-recorder")

    @property
    def extras(self) -> dict[str, Any]:
        return self.env.runtime.extras

    @property
    def cfg(self) -> Any:
        return self.env.kit.settings.orchestrator

    async def run(self, goal: str = "test goal", *, plan: TaskPlan | None = None, timeout: float = 60,
                  **kw: Any) -> JarvisTask:
        kw.setdefault("language", "en")
        return await asyncio.wait_for(self.orch.run(goal, plan=plan, **kw), timeout)

    async def start(self, goal: str = "test goal", *, plan: TaskPlan | None = None, **kw: Any) -> JarvisTask:
        kw.setdefault("language", "en")
        return await self.orch.submit(goal, plan=plan, **kw)

    def gate(self) -> asyncio.Event:
        return self.extras.setdefault("gate", asyncio.Event())

    def kinds(self, task: JarvisTask) -> list[str]:
        return [e.kind for e in task.timeline]

    def sub(self, task: JarvisTask, key: str) -> SubTask:
        assert task.plan is not None
        found = task.plan.by_key(key)
        assert found is not None, key
        return found


@pytest.fixture
async def o(kit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    kit.settings.orchestrator.retry_backoff_s = 0.01
    kit.settings.orchestrator.retry_backoff_max_s = 0.05
    monkeypatch.setattr(orchestrator_module, "RESTART_WAIT_S", 0.3)
    async with agent_env(kit, tmp_path, monkeypatch, agents=probe_agents) as env:
        orch = TaskOrchestrator(env.manager)
        await orch.start()
        try:
            yield Orch(env, orch)
        finally:
            env.runtime.extras.setdefault("gate", asyncio.Event()).set()
            await orch.shutdown(timeout=5)


# ================================================================================== pure helpers
class TestHelpers:
    def test_short_title(self) -> None:
        assert short_title("ג'ארוויס, תבנה לי אתר לחנות קפה") == "תבנה לי אתר לחנות קפה"
        assert short_title("Hey JARVIS build me an app") == "build me an app"
        long = short_title("word " * 40, limit=30)
        assert len(long) <= 31 and long.endswith("…")

    def test_compact_keeps_the_small_useful_fields(self) -> None:
        value = {"path": "C:/Projects/shop", "files": [f"file{i}.html" for i in range(5000)], "name": "shop"}
        reduced = compact(value, 2000)
        assert reduced["path"] == "C:/Projects/shop" and reduced["name"] == "shop"
        assert len(json.dumps(reduced)) <= 2100 and len(reduced.get("files", [])) < 5000

    def test_fingerprint_ignores_paths_and_numbers(self) -> None:
        a = fingerprint("Timeout after 30s reading C:\\Users\\a\\file1.txt (code 0x1F)")
        b = fingerprint("timeout after 45s reading C:\\Users\\b\\file2.txt (code 0x2A)")
        assert a == b and fingerprint("permission denied") != a

    def test_backoff_grows_and_is_capped(self) -> None:
        assert [backoff(n, 1.0, 5.0) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 5.0]
        assert backoff(3, 0.0, 5.0) == 0.0

    def test_recovery_decisions(self) -> None:
        base = {"max_retries": 2, "max_same_error": 3, "backoff_s": 1.0, "backoff_max_s": 20.0}
        assert decide(code="kill_switch", error="x", attempts=1, history=["x"], **base).action == "halt"
        assert decide(code="confirmation_denied", error="no", attempts=1, history=["no"], **base).action == "fail"
        first = decide(code=None, error="boom", attempts=1, history=["boom"], **base)
        assert (first.action, first.delay_s) == ("retry", 1.0)
        assert decide(code=None, error="boom", attempts=1, history=["boom"], can_help=True, **base).action == "help"
        assert decide(code=None, error="boom", attempts=3, history=["a", "b", "boom"], **base).action == "fail"
        assert decide(code=None, error="boom", attempts=3, history=["a", "b", "boom"], can_fallback=True,
                      **base).action == "fallback"
        same = decide(code=None, error="boom 1", attempts=2, history=["boom 7", "boom 9"],
                      **{**base, "max_same_error": 2})
        assert same.action == "fail" and same.reason == "same error again"
        offline = decide(code="ai_error", error="overloaded", attempts=1, history=["overloaded"],
                         can_run_offline=True, **base)
        assert offline.action == "retry" and offline.offline
        assert decide(code="ai_unavailable", error="no key", attempts=1, history=["no key"], **base).action == "fail"

    def test_dispatch_budget_is_finite(self) -> None:
        assert 10 < dispatch_budget(3, max_retries=2, max_followups=3) < 60
        assert dispatch_budget(1000, max_retries=50, max_followups=50, autonomous=True) == 200

    def test_task_record_round_trip(self) -> None:
        plan = plan_of(step("a", "research"), step("b", "creative", after=("a",)))
        task = JarvisTask(goal="make a logo", title="Logo", plan=plan, language="en", mode=Mode.TASK,
                          priority=Priority.HIGH, source="voice")
        task.note("received", "JARVIS received a task")
        rec = task.to_record()
        assert (rec.status, rec.priority, rec.mode, rec.source) == ("pending", "high", "task", "voice")
        back = JarvisTask.from_record(rec)
        assert back.plan is not None and [s.key for s in back.plan.subtasks] == ["a", "b"]
        assert back.plan.subtasks[1].depends_on == [plan.subtasks[0].id]
        assert back.timeline[0].message == "JARVIS received a task" and back.priority == Priority.HIGH
        view = back.detail_view()
        assert view["plan"]["subtasks"][0]["agent"] == "research" and view["timeline"][0]["time"]
        assert set(view) >= {"id", "title", "goal", "status", "progress", "priority", "mode", "agents", "source",
                             "created_at", "updated_at", "started_at", "finished_at", "summary", "error", "plan",
                             "timeline", "result"}


# ================================================================================== the flagship flow
def web_handler(request: Any) -> Any:
    import httpx

    if "duckduckgo" in request.url.host:
        return httpx.Response(200, text='<div class="result"><a class="result__a" href="https://example.com/cafe">'
                                        'Cafe sites</a><a class="result__snippet">Great coffee shop websites.</a></div>',
                              headers={"content-type": "text/html"})
    return httpx.Response(200, text="<html><head><title>Cafe</title></head><body><main><p>A coffee shop website "
                                    "shows the menu, opening hours and location.</p></main></body></html>",
                          headers={"content-type": "text/html; charset=utf-8"})


class TestFlagship:
    async def test_build_a_coffee_shop_website_with_all_five_agents(self, o: Orch) -> None:
        o.env.mock_http(web_handler)
        goal = "ג'ארוויס, תבנה לי אתר לחנות קפה"
        intent = detect_intent(goal)
        task = await o.run(goal, intent=intent.name, slots=intent.slots, language="he", timeout=120)
        assert task.status == S.COMPLETED, [(s.key, s.status, s.error) for s in task.plan.subtasks]
        assert task.plan is not None and task.plan.source == "rules"
        assert set(task.agents) == set(AgentId)
        assert {s.key: s.status for s in task.plan.subtasks} == {k: S.COMPLETED for k in
                                                                 ("plan", "research", "design", "build", "test", "launch")}
        project = Path(task.plan.project_dir or "")
        assert project.is_dir() and (project / "index.html").exists()
        assert task.outcome is not None and task.outcome.summary.startswith("סיימתי את")
        assert any(f.endswith("index.html") for f in task.outcome.files_created)
        assert ("open_path", str(project / "index.html")) in o.env.host.calls
        kinds = o.kinds(task)
        assert kinds[0] == "received" and "plan" in kinds and "verify" in kinds and kinds[-1] == "completed"
        # the build step received the plan and the design as context
        build_deps = o.sub(task, "build").depends_on
        assert {task.plan.get(d).key for d in build_deps} == {"plan", "design"}
        saved = await o.env.memory.get_task(task.id)
        assert saved is not None and saved.status == "completed" and saved.data["plan"]["subtasks"]
        lines = [e.message for e in o.env.kit.activity.recent(300, task_id=task.id)]
        assert lines[0].startswith("JARVIS קיבל משימה") and any("JARVIS מאמת" in m for m in lines)
        assert any(m.startswith("JARVIS העביר לסוכן פיתוח") for m in lines)


# ================================================================================== scheduling
class TestScheduling:
    async def test_independent_steps_run_in_parallel(self, o: Orch) -> None:
        plan = plan_of(step("r", "research", params={"delay": 0.4}), step("c", "creative", params={"delay": 0.4}),
                       step("p", "planner", params={"delay": 0.4}))
        t0 = time.monotonic()
        task = await o.run(plan=plan)
        assert task.status == S.COMPLETED and time.monotonic() - t0 < 1.1
        log = o.extras["log"]
        starts = [ts for kind, _, ts in log if kind == "start"]
        ends = [ts for kind, _, ts in log if kind == "end"]
        assert len(starts) == 3 and max(starts) < min(ends)      # all three were running at the same time

    async def test_all_five_agents_work_at_the_same_time(self, o: Orch) -> None:
        agents = ("research", "system", "development", "creative", "planner")
        plan = plan_of(*(step(a[:3], a, params={"delay": 0.5}) for a in agents))
        busy: list[set[str]] = []

        async def watch() -> None:
            while True:
                busy.append({a for a in agents if o.env.manager.status(a) == AgentStatus.WORKING})
                await asyncio.sleep(0.02)

        watcher = asyncio.create_task(watch())
        t0 = time.monotonic()
        try:
            task = await o.run(plan=plan)
        finally:
            watcher.cancel()
        assert task.status == S.COMPLETED and time.monotonic() - t0 < 1.4      # five half-second steps at once
        assert set(agents) in busy                                             # all five were working together
        starts = [ts for kind, _, ts in o.extras["log"] if kind == "start"]
        ends = [ts for kind, _, ts in o.extras["log"] if kind == "end"]
        assert len(starts) == 5 and max(starts) < min(ends)

    async def test_dependencies_order_and_context(self, o: Orch) -> None:
        plan = plan_of(step("a", "research", params={"delay": 0.1}), step("b", "creative", after=("a",)),
                       step("c", "planner", after=("a", "b")))
        task = await o.run("write the brief", plan=plan)
        assert task.status == S.COMPLETED
        b, c = o.sub(task, "b"), o.sub(task, "c")
        assert b.result["result"]["deps"] == ["a"] and c.result["result"]["deps"] == ["a", "b"]
        assert c.result["result"]["goal"] == "write the brief"
        log = {(kind, title): ts for kind, title, ts in o.extras["log"]}
        assert log[("start", "b")] >= log[("end", "a")] and log[("start", "c")] >= log[("end", "b")]

    async def test_priorities_order_the_queue(self, o: Orch) -> None:
        o.cfg.max_parallel_subtasks = 1
        plan = plan_of(step("low", "system", "note", priority=Priority.LOW, title="low"),
                       step("normal", "system", "note", priority=Priority.NORMAL, title="normal"),
                       step("high", "system", "note", priority=Priority.HIGH, title="high"))
        task = await o.run(plan=plan)
        assert task.status == S.COMPLETED and o.extras["order"] == ["high", "normal", "low"]

    async def test_a_critical_task_makes_its_steps_critical(self, o: Orch) -> None:
        task = await o.run(plan=plan_of(step("a", "research")), priority="critical")
        assert o.sub(task, "a").result["result"]["priority"] == "critical"
        normal = await o.run(plan=plan_of(step("a", "research", priority=Priority.CRITICAL)))
        assert o.sub(normal, "a").result["result"]["priority"] == "high"   # only a critical task gets critical steps

    async def test_events_and_views(self, o: Orch) -> None:
        task = await o.run(plan=plan_of(step("a", "research"), step("b", "creative", after=("a",))))
        await asyncio.sleep(0.05)
        updates = [m.data["status"] for m in o.events if m.topic == "task.updated" and m.task_id == task.id]
        assert updates[0] == "pending" and "running" in updates and updates[-1] == "completed"
        subs = [m.data["subtask"]["status"] for m in o.events if m.topic == "task.subtask"]
        assert {"queued", "running", "completed"} <= set(subs)
        assert any(m.topic == "task.timeline" for m in o.events)
        rows = await o.orch.list_tasks()
        assert rows[0]["id"] == task.id and rows[0]["status"] == "completed" and rows[0]["agents"] == [
            "research", "creative"]
        fresh = TaskOrchestrator(o.env.manager)          # history comes back from task memory
        detail = await fresh.detail(task.id)
        assert detail is not None and [s["status"] for s in detail["plan"]["subtasks"]] == ["completed", "completed"]
        assert detail["result"]["summary"] and detail["timeline"][-1]["kind"] == "completed"

    async def test_chat_mode_refuses_tasks(self, o: Orch) -> None:
        with pytest.raises(TaskRefused):
            await o.start("build a site", mode=Mode.CHAT)


# ================================================================================== recovery
class TestRecovery:
    async def test_retry_with_feedback_then_success(self, o: Orch) -> None:
        plan = plan_of(step("a", "research", "flaky", params={"failures": 2, "vary": True}))
        task = await o.run(plan=plan)
        a = o.sub(task, "a")
        assert task.status == S.COMPLETED and a.attempts == 3 and len(a.error_history) == 2
        contexts = o.extras["contexts"][a.id]
        assert contexts[0]["context"].get("feedback") is None
        assert "temporary glitch" in contexts[1]["context"]["feedback"]
        assert o.kinds(task).count("retry") == 2
        lines = [e.message for e in o.env.kit.activity.recent(200, task_id=task.id)]
        assert any("failed (temporary glitch b). Retrying now (attempt 2 of 3)" in m for m in lines)

    async def test_retry_limit_and_skipped_dependents(self, o: Orch) -> None:
        plan = plan_of(step("a", "research", "flaky", params={"failures": 99, "vary": True}),
                       step("b", "creative", after=("a",)))
        task = await o.run(plan=plan)
        a, b = o.sub(task, "a"), o.sub(task, "b")
        assert task.status == S.FAILED and a.status == S.FAILED and a.attempts == 3   # 1 + max_retries (2)
        assert b.status == S.SKIPPED and "a" in (b.error or "")
        assert task.outcome is not None and "could not complete" in task.outcome.summary

    async def test_the_same_error_stops_retrying(self, o: Orch) -> None:
        o.cfg.max_retries = 6
        o.cfg.max_same_error = 2
        task = await o.run(plan=plan_of(step("a", "research", "flaky", params={"failures": 99})))
        assert task.status == S.FAILED and o.sub(task, "a").attempts == 2

    async def test_no_retry_after_the_user_said_no(self, o: Orch) -> None:
        plan = plan_of(step("a", "system", "flaky", params={"failures": 5, "code": "confirmation_denied"}))
        task = await o.run(plan=plan)
        assert task.status == S.FAILED and o.sub(task, "a").attempts == 1

    async def test_a_step_the_user_said_no_to_ends_there(self, o: Orch) -> None:
        o.cfg.max_retries = 3
        plan = plan_of(step("a", "system", "declined"), step("b", "creative", after=("a",)))
        task = await o.run(plan=plan)
        a, b = o.sub(task, "a"), o.sub(task, "b")
        assert a.status == S.FAILED and a.attempts == 1 and o.extras["declined"] == {a.id: 1}   # not again
        assert "did not approve: delete old.txt" in (a.error or "")
        assert b.status == S.SKIPPED

    async def test_what_a_step_made_before_the_user_said_no_is_kept(self, o: Orch) -> None:
        o.cfg.max_retries = 3
        task = await o.run(plan=plan_of(step("a", "development", "wrote_then_declined")))
        a = o.sub(task, "a")
        assert task.status == S.COMPLETED and a.status == S.COMPLETED and a.attempts == 1
        assert o.extras["declined"] == {a.id: 1}                   # not again, and not checked into a retry
        assert [Path(f).name for f in a.files_created] == ["fib.py"]
        assert task.outcome is not None and [Path(f).name for f in task.outcome.files_created] == ["fib.py"]
        assert [i for i in task.outcome.issues if "did not approve" in i] == [
            "Development Agent: The user did not approve: Run fib.py with Python (timed out)"]   # said once
        assert any(m.endswith("(partly)") for m in (e.message for e in task.timeline if e.kind == "done"))
        assert task.outcome.summary.startswith("I wrote fib.py.") and task.outcome.details is not None
        assert task.outcome.details.startswith("It is ready to run.")          # the summary is not said twice

    async def test_steps_built_on_a_declined_action_still_do_not_run(self, o: Orch) -> None:
        plan = plan_of(step("a", "development", "wrote_then_declined"), step("b", "system", after=("a",)),
                       step("c", "creative"))
        task = await o.run(plan=plan)
        a, b, c = o.sub(task, "a"), o.sub(task, "b"), o.sub(task, "c")
        assert a.status == S.COMPLETED and a.files_created
        assert b.status == S.SKIPPED and "was not approved" in (b.error or "")
        assert c.status == S.COMPLETED and task.status == S.FAILED

    async def test_an_optional_step_may_fail(self, o: Orch) -> None:
        plan = plan_of(step("a", "research", "flaky", params={"failures": 99}, optional=True),
                       step("b", "creative", after=("a",)))
        task = await o.run(plan=plan)
        assert task.status == S.COMPLETED and o.sub(task, "a").status == S.FAILED
        assert o.sub(task, "b").status == S.COMPLETED
        assert task.outcome is not None and any("temporary glitch" in i for i in task.outcome.issues)

    async def test_a_crash_is_recovered_by_retrying(self, o: Orch) -> None:
        task = await o.run(plan=plan_of(step("a", "system", "crashy")))
        a = o.sub(task, "a")
        assert task.status == S.COMPLETED and a.attempts == 2 and a.summary == "recovered on attempt 2"

    async def test_a_persistent_crash_stops_at_the_retry_limit(self, o: Orch) -> None:
        task = await o.run(plan=plan_of(step("a", "system", "crash")))
        a = o.sub(task, "a")
        assert task.status == S.FAILED and a.attempts == 3   # the first try + max_retries (2), never a loop
        assert "boom" in (a.error or "")

    async def test_a_model_failure_is_retried_without_the_model(self, o: Orch) -> None:
        o.env.use_ai(default=responder({"agent": [ProviderError("overloaded", retryable=True)]}))
        task = await o.run(plan=plan_of(step("a", "creative", "smart")))
        a = o.sub(task, "a")
        assert task.status == S.COMPLETED and a.summary == "did it offline" and a.attempts == 2
        assert any("without the AI model" in e.message for e in task.timeline)

    async def test_development_failure_gets_research_help(self, o: Orch) -> None:
        o.env.use_ai(default=responder({
            "agent": ["Use a newer Node version and clear the cache.\n\nSTATUS: success"],
            "SynthesisDraft": [{"summary": "Built after a fix.", "details": "", "next_step": ""}],
        }))
        plan = plan_of(step("build", "development", "flaky", params={"failures": 1, "error": "npm ERR! code ELIFECYCLE"}))
        task = await o.run(plan=plan)
        assert task.status == S.COMPLETED, [(s.key, s.status, s.error) for s in task.plan.subtasks]
        helper = o.sub(task, "build_fix")
        build = o.sub(task, "build")
        assert helper.agent == AgentId.RESEARCH and helper.followup_of == build.id and helper.status == S.COMPLETED
        assert helper.id in build.depends_on and build.attempts == 2
        second = o.extras["contexts"][build.id][1]["context"]
        assert [d["agent"] for d in second["dependencies"]] == ["research"] and "ELIFECYCLE" in second["feedback"]
        assert "help" in o.kinds(task) and task.followups == 1

    async def test_another_agent_takes_over(self, o: Orch) -> None:
        plan = plan_of(step("a", "system", "only_on", params={"agent": "development"},
                            fallback_agent=AgentId.DEVELOPMENT))
        task = await o.run(plan=plan)
        a = o.sub(task, "a")
        assert task.status == S.COMPLETED and a.agent == AgentId.DEVELOPMENT and a.summary == "done by Development Agent"
        assert "fallback" in o.kinds(task)

    async def test_the_loop_guard_stops_a_runaway_task(self, o: Orch, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(orchestrator_module, "dispatch_budget", lambda *a, **k: 4)
        o.cfg.max_retries = 50
        o.cfg.max_same_error = 50
        task = await o.run(plan=plan_of(step("a", "research", "flaky", params={"failures": 999, "vary": True})))
        assert task.status == S.FAILED and task.dispatches == 4
        assert "loop_guard" in o.kinds(task)

    async def test_the_planner_unavailable_means_a_local_plan(self, o: Orch) -> None:
        await o.env.manager.stop_agent("planner")
        o.env.mock_http(web_handler)
        task = await o.run("research coffee shop websites", intent="research",
                           slots={"query": "coffee shop websites"})
        assert task.status == S.COMPLETED and task.plan is not None and task.plan.agents == [AgentId.RESEARCH]
        assert "plan_fallback" in o.kinds(task)


# ================================================================================== verification
class TestVerification:
    async def test_real_files_pass(self, o: Orch) -> None:
        plan = plan_of(step("a", "system", "make_file", params={"name": "report.txt"}, expected_files=["report.txt"]))
        task = await o.run(plan=plan)
        a = o.sub(task, "a")
        assert task.status == S.COMPLETED and a.files_created and Path(a.files_created[0]).read_text() == "hello"
        assert task.outcome is not None and a.files_created[0] in task.outcome.files_created

    async def test_a_claimed_file_that_does_not_exist_fails(self, o: Orch) -> None:
        task = await o.run(plan=plan_of(step("a", "creative", "fake_file")))
        a = o.sub(task, "a")
        assert task.status == S.FAILED and a.attempts == 3 and "ghost.txt" in (a.error or "")
        assert "verify_failed" in o.kinds(task)

    async def test_a_missing_expected_file_fails(self, o: Orch) -> None:
        o.cfg.max_retries = 0
        plan = plan_of(step("a", "system", "make_file", params={"name": "notes.txt"},
                            expected_files=["summary.pdf"]))
        task = await o.run(plan=plan)
        assert task.status == S.FAILED and "summary.pdf" in (o.sub(task, "a").error or "")

    async def test_verification_can_be_switched_off(self, o: Orch) -> None:
        o.cfg.verify_results = False
        task = await o.run(plan=plan_of(step("a", "creative", "fake_file")))
        assert task.status == S.COMPLETED

    async def test_the_reviewer_sends_work_back_with_its_problems(self, o: Orch) -> None:
        o.env.use_ai(default=responder({
            "agent": ["Wrote the slogan.\n\nSTATUS: success"],
            "StepVerdict": [{"passed": False, "problems": ["The slogan is longer than five words"]},
                            {"passed": True, "problems": []}],
        }))
        plan = plan_of(step("slogan", "creative", None, instruction="Write a five-word slogan for a bakery",
                            acceptance_criteria=["At most five words"]))
        task = await o.run(plan=plan)
        slogan = o.sub(task, "slogan")
        assert task.status == S.COMPLETED and slogan.attempts == 2
        provider = o.env.runtime.router.providers["anthropic"]
        second_agent_prompt = [r for r in provider.requests if r.role == "agent"][1].messages[0].text
        assert "longer than five words" in second_agent_prompt


# ================================================================================== control
class TestControl:
    async def test_cancel_a_running_task(self, o: Orch) -> None:
        plan = plan_of(step("a", "creative", "block"), step("b", "planner", after=("a",)))
        task = await o.start(plan=plan)
        await until(lambda: o.extras.get("blocked") == ["a"])
        await asyncio.wait_for(o.orch.cancel(task.id), 10)
        assert task.status == S.CANCELLED
        assert o.sub(task, "a").status == S.CANCELLED and o.sub(task, "b").status == S.CANCELLED
        assert o.env.manager.active_assignments() == [] and not o.orch.is_running(task.id)
        assert task.outcome is not None and "stopped" in task.outcome.summary

    async def test_the_task_time_limit(self, o: Orch) -> None:
        o.cfg.task_timeout_s = 0.5
        task = await o.run(plan=plan_of(step("a", "creative", "block")))
        assert task.status == S.FAILED and "ran out of time" in (task.error or "")
        assert o.sub(task, "a").status == S.CANCELLED

    async def test_kill_switch_pauses_and_resume_continues(self, o: Orch) -> None:
        plan = plan_of(step("a", "research"), step("b", "creative", "block", after=("a",)))
        task = await o.start(plan=plan)
        await until(lambda: o.extras.get("blocked") == ["b"])
        await o.env.kit.kill_switch.engage("test")
        await o.orch.wait(task.id, timeout=10)
        assert task.status == S.INTERRUPTED and "kill switch" in (task.error or "")
        assert o.sub(task, "a").status == S.COMPLETED and o.sub(task, "b").status == S.PENDING
        await o.env.kit.kill_switch.release()
        o.gate().set()
        await o.orch.resume(task.id)
        await o.orch.wait(task.id, timeout=20)
        assert task.status == S.COMPLETED and o.sub(task, "a").attempts == 1   # not redone
        assert [title for kind, title, _ in o.extras["log"] if kind == "start"].count("a") == 1
        assert "resume" in o.kinds(task)

    async def test_a_stopped_agent_pauses_the_task(self, o: Orch) -> None:
        await o.env.manager.stop_agent("creative")
        task = await o.run(plan=plan_of(step("a", "research"), step("b", "creative", after=("a",))))
        assert task.status == S.INTERRUPTED and o.sub(task, "b").status == S.PENDING
        await o.env.manager.start_agent("creative")
        await o.orch.resume(task.id)
        await o.orch.wait(task.id, timeout=20)
        assert task.status == S.COMPLETED

    async def test_an_optional_step_of_a_stopped_agent_is_skipped(self, o: Orch) -> None:
        await o.env.manager.stop_agent("creative")
        task = await o.run(plan=plan_of(step("a", "research"), step("b", "creative", optional=True)))
        assert task.status == S.COMPLETED and o.sub(task, "b").status == S.SKIPPED

    async def test_restarting_an_agent_restarts_its_step(self, o: Orch) -> None:
        task = await o.start(plan=plan_of(step("a", "creative", "block")))
        await until(lambda: o.extras.get("blocked") == ["a"])
        await o.env.manager.restart_agent("creative")
        await until(lambda: o.extras.get("blocked") == ["a", "a"])
        o.gate().set()
        await o.orch.wait(task.id, timeout=20)
        assert task.status == S.COMPLETED and o.sub(task, "a").attempts == 1

    async def test_waiting_for_approval(self, o: Orch) -> None:
        plan = plan_of(step("off", "system", "power", params={"action": "shutdown"}))
        task = await o.start(plan=plan)
        request = await o.env.pending()
        await until(lambda: task.status == S.WAITING)
        assert o.sub(task, "off").status == S.WAITING
        o.env.kit.broker.respond(request.id, True)
        await o.orch.wait(task.id, timeout=20)
        assert task.status == S.COMPLETED and any(c[0] == "power" for c in o.env.host.calls)
        assert "confirm" in o.kinds(task)

    async def test_a_denied_action_fails_without_retrying(self, o: Orch) -> None:
        task = await o.start(plan=plan_of(step("off", "system", "power", params={"action": "shutdown"})))
        request = await o.env.pending()
        o.env.kit.broker.respond(request.id, False)
        await o.orch.wait(task.id, timeout=20)
        off = o.sub(task, "off")
        assert task.status == S.FAILED and off.attempts == 1 and not o.env.kit.broker.pending()
        assert not any(c[0] == "power" for c in o.env.host.calls)

    async def test_retry_runs_the_failed_steps_again(self, o: Orch) -> None:
        o.cfg.max_retries = 0
        plan = plan_of(step("a", "research"), step("b", "creative", "flaky", params={"failures": 1}, after=("a",)))
        task = await o.run(plan=plan)
        assert task.status == S.FAILED
        await o.orch.retry(task.id)
        await o.orch.wait(task.id, timeout=20)
        assert task.status == S.COMPLETED and o.sub(task, "b").attempts == 1 and task.runs == 2
        assert [title for kind, title, _ in o.extras["log"] if kind == "start"].count("a") == 1
        with pytest.raises(TaskRefused):
            await o.orch.resume(task.id)


# ================================================================================== interruption and resume
class TestResume:
    async def test_shutdown_saves_an_interrupted_task_that_resumes_later(self, o: Orch) -> None:
        plan = plan_of(step("a", "research"), step("b", "creative", "block", after=("a",)),
                       step("c", "planner", after=("b",)))
        task = await o.start(plan=plan)
        await until(lambda: o.extras.get("blocked") == ["b"])
        await o.orch.shutdown(timeout=10)
        saved = await o.env.memory.get_task(task.id)
        assert saved is not None and saved.status == "interrupted"
        statuses = {s["key"]: s["status"] for s in saved.data["plan"]["subtasks"]}
        assert statuses == {"a": "completed", "b": "pending", "c": "pending"}
        fresh = TaskOrchestrator(o.env.manager)          # "next start"
        found = await fresh.start()
        assert [t.id for t in found] == [task.id] and found[0].status == S.INTERRUPTED
        o.gate().set()
        await fresh.resume(task.id)
        done = await fresh.wait(task.id, timeout=20)
        assert done.status == S.COMPLETED and [s.attempts for s in done.plan.subtasks] == [1, 1, 1]
        await fresh.shutdown()

    async def test_a_task_cut_off_by_a_crash_is_recovered(self, o: Orch) -> None:
        plan = plan_of(step("a", "research"), step("b", "creative", after=("a",)))
        plan.subtasks[0].status, plan.subtasks[0].attempts = S.COMPLETED, 1
        plan.subtasks[0].result = {"agent": "research", "status": "success", "summary": "found it"}
        plan.subtasks[1].status = S.RUNNING
        crashed = JarvisTask(goal="crashed task", language="en", plan=plan, status=S.RUNNING)
        await o.env.memory.save_task(crashed.to_record())
        o.env.kit.settings.general.resume_interrupted_on_start = True
        fresh = TaskOrchestrator(o.env.manager)
        found = await fresh.start()
        assert [t.id for t in found] == [crashed.id]
        done = await fresh.wait(crashed.id, timeout=20)
        assert done.status == S.COMPLETED and done.plan.subtasks[1].result["result"]["deps"] == ["a"]
        assert "interrupted" in [e.kind for e in done.timeline]
        await fresh.shutdown()

    async def test_cancel_an_interrupted_task(self, o: Orch) -> None:
        record = TaskRecord(id="task_old", goal="old", status="interrupted",
                            data={"language": "en", "plan": plan_of(step("a", "research")).model_dump(mode="json")})
        await o.env.memory.save_task(record)
        task = await o.orch.cancel("task_old")
        assert task is not None and task.status == S.CANCELLED
        assert (await o.env.memory.get_task("task_old")).status == "cancelled"


# ================================================================================== planning, AI and autonomy
class TestPlanningAndAI:
    async def test_the_planner_plans_with_the_model(self, o: Orch) -> None:
        draft = {"title": "Launch plan", "strategy": "Research first, then write.", "steps": [
            {"key": "facts", "agent": "research", "title": "Collect facts", "instruction": "Collect facts",
             "depends_on": [], "acceptance_criteria": [], "expected_files": [], "optional": False, "priority": "high"},
            {"key": "post", "agent": "creative", "title": "Write the post", "instruction": "Write the post",
             "depends_on": ["facts"], "acceptance_criteria": [], "expected_files": [], "optional": False,
             "priority": "normal"}]}
        o.env.use_ai(default=responder({
            "PlanDraft": [draft],
            "agent": ["Done.\n\nSTATUS: success"],
            "SynthesisDraft": [{"summary": "The post is ready.", "details": "Facts collected first.",
                                "next_step": "Publish it."}],
        }))
        task = await o.run("write a launch post about our cafe")
        assert task.status == S.COMPLETED and task.plan.source == "llm" and task.title == "Launch plan"
        assert [s.key for s in task.plan.subtasks] == ["facts", "post"]
        assert task.outcome.summary == "The post is ready." and task.outcome.used_llm
        assert task.outcome.next_step == "Publish it."

    async def test_the_plan_and_every_step_see_what_jarvis_remembers(self, o: Orch) -> None:
        await o.env.memory.remember("The user prefers Python for new projects", "preference")
        seen: dict[str, list[str]] = {"plan": [], "agent": []}

        def capture(kind: str, answer: Any) -> Callable[[CompletionRequest], Any]:
            def respond(request: CompletionRequest) -> Any:
                seen[kind].append(json.dumps([m.model_dump(mode="json") for m in request.messages], ensure_ascii=False)
                                  + (request.system or ""))
                return answer
            return respond

        draft = {"title": "Tool plan", "strategy": "One step.", "steps": [
            {"key": "code", "agent": "development", "title": "Write the tool", "instruction": "Write the tool",
             "depends_on": [], "acceptance_criteria": [], "expected_files": [], "optional": False, "priority": "normal"}]}
        o.env.use_ai(default=responder({
            "PlanDraft": [capture("plan", draft)],
            "agent": [capture("agent", "Done.\n\nSTATUS: success")],
            "SynthesisDraft": [{"summary": "Written.", "details": "", "next_step": ""}],
        }))
        task = await o.run("write a small tool that renames photos")
        assert task.status == S.COMPLETED
        assert "prefers Python" in task.context.get("memory", "")
        assert seen["plan"] and all("prefers Python" in text for text in seen["plan"])
        assert seen["agent"] and all("prefers Python" in text for text in seen["agent"])

    async def test_autonomous_mode_adds_follow_up_steps(self, o: Orch) -> None:
        missing_step = {"key": "faq", "agent": "creative", "title": "Write the FAQ", "instruction": "Write an FAQ",
                        "depends_on": [], "acceptance_criteria": [], "expected_files": [], "optional": False,
                        "priority": "normal"}
        o.env.use_ai(default=responder({
            "agent": ["Wrote the FAQ.\n\nSTATUS: success"],
            "GoalCheck": [{"achieved": False, "missing": ["an FAQ"], "steps": [missing_step]},
                          {"achieved": True, "missing": [], "steps": []}],
            "SynthesisDraft": [{"summary": "All done.", "details": "", "next_step": ""}],
        }))
        task = await o.run("make the help page", plan=plan_of(step("page", "creative")), mode=Mode.AUTONOMOUS)
        assert task.status == S.COMPLETED and task.followups == 1
        follow = [s for s in task.plan.subtasks if s.followup_of == "goal"]
        assert len(follow) == 1 and follow[0].status == S.COMPLETED and follow[0].key.startswith("followup1_")
        assert "followup" in o.kinds(task)

    async def test_normal_mode_does_not_add_follow_ups(self, o: Orch) -> None:
        o.env.use_ai(default=responder({"SynthesisDraft": [{"summary": "ok", "details": "", "next_step": ""}]}))
        task = await o.run("make the help page", plan=plan_of(step("page", "creative")))
        assert task.status == S.COMPLETED and task.followups == 0 and len(task.plan.subtasks) == 1

    async def test_the_template_answer_without_a_model(self, o: Orch) -> None:
        plan = plan_of(step("a", "research", params={"text": "Found three cafes."}),
                       step("b", "system", "make_file", params={"name": "cafes.txt"}))
        task = await o.run(plan=plan)
        assert task.outcome is not None and not task.outcome.used_llm
        assert task.outcome.summary.startswith("I finished") and "Found three cafes." in task.outcome.details
        assert task.outcome.files_created[0].endswith("cafes.txt")
