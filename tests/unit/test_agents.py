"""Stage 10: the five agents and the Agent Manager, run through the real bus, permission gate and tools.

Model calls use `ScriptedProvider`; the desktop is `FakeHost`; web services are `httpx.MockTransport`.
Everything else (files, projects, tests of generated projects, Git, memory) is real.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_env import Env, make_runtime, until
from fakes import reply, tool
from jarvis.agents import (
    AgentManager,
    AgentTask,
    PlannerAgent,
    RunContext,
    agent_classes,
)
from jarvis.agents.base import parse_report
from jarvis.agents.creative import DESIGN_ROUTES, parse_email, route
from jarvis.agents.planner import documents
from jarvis.agents.planner.planning import rule_plan
from jarvis.common.cancel import CancelToken
from jarvis.common.enums import AgentId, AgentStatus, Priority, ResultStatus
from jarvis.common.errors import ProviderError, ProviderRefusal, ProviderUnavailable, TaskCancelled
from jarvis.memory.models import TaskRecord
from jarvis.orchestration import TaskPlan
from jarvis.tools.builtin import create_registry

S = ResultStatus


# ================================================================================== roster
class TestRoster:
    def test_five_agents_in_number_order(self) -> None:
        assert [cls.id for cls in agent_classes()] == [AgentId.RESEARCH, AgentId.SYSTEM, AgentId.DEVELOPMENT,
                                                        AgentId.CREATIVE, AgentId.PLANNER]

    async def test_default_roster_views_and_skills(self, kit) -> None:
        kit.registry = create_registry()
        kit.executor.registry = kit.registry
        manager = AgentManager(make_runtime(kit), kit.kill_switch)
        await manager.start()
        try:
            views = manager.views()
            assert [v["number"] for v in views] == [1, 2, 3, 4, 5]
            assert {v["status"] for v in views} == {"idle"}
            for v in views:
                assert v["name"] and v["name_he"] and v["role_he"] and v["permissions"] and v["enabled"]
                assert v["current"] is None and v["queue"] == 0 and not v["killed"]
            skills = {v["id"]: set(v["skills"]) for v in views}
            assert {"research", "summarize", "analyze_data", "weather"} <= skills["research"]
            assert {"open_app", "open_project", "organize_folder", "power", "system_status"} <= skills["system"]
            assert {"create_project", "verify_project", "run_project", "run_tests", "write_code", "git"} \
                <= skills["development"]
            assert {"design", "brand_kit", "presentation", "ideas", "write_text"} <= skills["creative"]
            assert {"decompose", "make_plan", "project_brief", "review_progress", "folder_review"} <= skills["planner"]
            assert manager.skill_for_intent("build_app") == (AgentId.DEVELOPMENT, "create_project")
            assert manager.skill_for_intent("plan") == (AgentId.PLANNER, "make_plan")
            assert manager.skill_for_intent("design") == (AgentId.CREATIVE, "design")
            assert manager.skill_for_intent("weather") == (AgentId.RESEARCH, "weather")
            assert manager.skill_for_intent("open_app") == (AgentId.SYSTEM, "open_app")
        finally:
            await manager.shutdown()

    def test_every_model_tool_exists_and_is_permitted(self, kit) -> None:
        registry = create_registry()
        runtime = make_runtime(kit)
        for cls in agent_classes():
            allowed = {t.name for t in registry.for_agent(cls.id.value, kit.permissions)}
            assert [n for n in cls.llm_tools if not registry.has(n)] == [], cls.id
            assert [n for n in cls.llm_tools if n not in allowed] == [], cls.id
            assert cls(runtime).skills, cls.id

    def test_the_roster_must_be_complete(self, kit) -> None:
        runtime = make_runtime(kit)
        with pytest.raises(ValueError, match="five agents"):
            AgentManager(runtime, kit.kill_switch, agents=[PlannerAgent(runtime)])


# ================================================================================== manager
class TestManager:
    async def test_assignment_and_result_travel_on_the_bus(self, env: Env) -> None:
        task = env.task("system", "volume 30", skill="volume", params={"action": "set", "level": 30})
        result = await asyncio.wait_for(env.manager.run(task), 30)
        assert result.ok and env.host.volume == 30
        assert (result.agent_id, result.task_id, result.subtask_id, result.skill) == \
            ("system", task.task_id, task.id, "volume")
        assert result.duration_s > 0
        assign = [m for m in env.kit.bus.history(topic="task.assign") if m.task_id == task.task_id]
        assert len(assign) == 1 and assign[0].recipient == "agent.system" and assign[0].permissions
        answers = [m for m in env.kit.bus.history(topic="task.result") if m.correlation_id == assign[0].id]
        assert len(answers) == 1 and answers[0].status == "success"
        assert answers[0].result["summary"] == result.summary
        progress = env.kit.bus.history(topic="task.progress", task_id=task.task_id)
        assert progress and progress[0].data["subtask_id"] == task.id
        lines = [e.message for e in env.kit.activity.recent(task_id=task.task_id)]
        assert any(line.startswith("סוכן מערכת סיים") for line in lines), lines

    async def test_an_agent_answers_with_the_whole_result_record(self, env: Env) -> None:
        result = await env.run("system", "צור תיקייה בשם דוחות", skill="create_folder", params={"name": "דוחות"})
        folder = env.kit.paths.workspace / "דוחות"
        assert folder.is_dir()
        assert (result.status, result.progress, result.errors) == (S.SUCCESS, 1.0, [])
        assert result.result == {"path": str(folder)} and result.files_created == [str(folder)]
        assert result.actions_taken == [f"נוצרה התיקייה: {folder}"] and result.next_step is None
        failed = await env.run("system", "crash", skill="crash")
        assert failed.status == S.FAILED and failed.errors == ["RuntimeError: boom"] and not failed.files_created

    async def test_statuses_show_thinking_working_completed_and_error(self, env: Env) -> None:
        seen: list[tuple[str, str]] = []
        env.kit.bus.subscribe("agent.status", lambda m: seen.append((m.data["id"], m.data["status"])))
        env.use_ai(default=reply("Three names: Bean There, Daily Grind, Brew Lab.\n\nSTATUS: success"))
        assert (await env.run("creative", "תן לי שלושה שמות לבית קפה")).used_llm
        assert not (await env.run("system", "crash", skill="crash")).ok
        await until(lambda: env.manager.status("system") == AgentStatus.IDLE)
        creative = [s for a, s in seen if a == "creative"]
        system = [s for a, s in seen if a == "system"]
        assert creative.index("thinking") < creative.index("completed")
        assert system.index("working") < system.index("error") < len(system) - 1 and system[-1] == "idle"

    async def test_status_events_follow_the_work(self, env: Env) -> None:
        seen: list[tuple[str, str]] = []
        env.kit.bus.subscribe("agent.status", lambda m: seen.append((m.data["id"], m.data["status"])))
        assert (await env.run("system", skill="note", title="a")).ok
        statuses = [s for a, s in seen if a == "system"]
        assert "working" in statuses and statuses[-1] == "completed"
        assert env.manager.view("system")["stats"]["completed"] == 1
        await until(lambda: env.manager.status("system") == AgentStatus.IDLE)
        assert seen[-1] == ("system", "idle")

    async def test_stop_start_and_restart_an_agent(self, env: Env) -> None:
        view = await env.manager.stop_agent("creative")
        assert view["status"] == "offline" and not view["enabled"]
        refused = await env.run("creative", skill="ideas", params={"topic": "בית קפה", "kind": "names"})
        assert refused.status == S.FAILED and refused.data["code"] == "agent_stopped"
        assert (await env.manager.start_agent("creative"))["status"] == "idle"
        assert (await env.run("creative", skill="ideas", params={"topic": "בית קפה", "kind": "names"})).ok
        before = env.manager.agent("creative")
        assert (await env.manager.restart_agent("creative"))["status"] == "idle"
        assert env.manager.agent("creative") is not before
        assert (await env.run("creative", skill="ideas", params={"topic": "בית קפה", "kind": "slogans"})).ok
        commands = [m.data["action"] for m in env.kit.bus.history(topic="agent.command")]
        assert commands[-3:] == ["stop", "start", "restart"]

    async def test_stopping_an_agent_ends_its_running_and_queued_work(self, env: Env) -> None:
        running = await env.manager.submit(env.task("system", skill="hold", title="long"))
        await until(lambda: env.manager.status("system") == AgentStatus.WORKING)
        queued = await env.manager.submit(env.task("system", skill="note", title="queued"))
        assert env.manager.view("system")["queue"] == 1
        await env.manager.stop_agent("system")
        first, second = await asyncio.wait_for(asyncio.gather(running, queued), 10)
        assert first.status == second.status == S.CANCELLED
        assert env.manager.view("system")["queue"] == 0 and env.manager.status("system") == AgentStatus.OFFLINE
        assert "order" not in env.runtime.extras

    @pytest.mark.parametrize("stubborn", [False, True], ids=["cooperative", "stubborn"])
    async def test_timeout(self, env: Env, stubborn: bool) -> None:
        result = await env.run("system", skill="hold", params={"stubborn": stubborn}, timeout_s=0.3)
        assert result.status == S.FAILED and result.data["code"] == "timeout"
        assert "חרג מזמן העבודה" in result.summary and result.duration_s < 5
        await until(lambda: env.manager.view("system")["running"] == 0)
        assert (await env.run("system", skill="note", title="next")).ok   # the worker is free again

    async def test_cancel_a_task_running_and_queued(self, env: Env) -> None:
        running = env.task("system", skill="hold", title="running")
        waiting = env.task("system", skill="note", title="waiting", task_id=running.task_id)
        f1 = await env.manager.submit(running)
        await until(lambda: env.manager.status("system") == AgentStatus.WORKING)
        f2 = await env.manager.submit(waiting)
        assert await env.manager.cancel_task(running.task_id, "the user said stop") == 2
        r1, r2 = await asyncio.wait_for(asyncio.gather(f1, f2), 10)
        assert r1.status == r2.status == S.CANCELLED and "the user said stop" in r2.summary
        assert "order" not in env.runtime.extras
        await until(lambda: env.manager.status("system") == AgentStatus.IDLE)

    async def test_the_callers_cancel_token_reaches_the_agent(self, env: Env) -> None:
        token = CancelToken()
        job = asyncio.ensure_future(env.manager.run(env.task("system", skill="hold"), cancel=token))
        await until(lambda: env.manager.status("system") == AgentStatus.WORKING)
        token.cancel("task stopped")
        assert (await asyncio.wait_for(job, 10)).status == S.CANCELLED

    async def test_priorities_and_the_critical_slot(self, env: Env) -> None:
        blocker = await env.manager.submit(env.task("system", skill="hold", title="blocker"))
        await until(lambda: env.manager.status("system") == AgentStatus.WORKING)
        low = await env.manager.submit(env.task("system", skill="note", title="low", priority=Priority.LOW))
        high = await env.manager.submit(env.task("system", skill="note", title="high", priority=Priority.HIGH))
        normal = await env.manager.submit(env.task("system", skill="note", title="normal"))
        urgent = await env.run("system", skill="note", title="critical", priority=Priority.CRITICAL)
        assert urgent.ok and env.runtime.extras["order"] == ["critical"]   # ran while the regular slot was busy
        assert env.manager.view("system")["queue"] == 3
        env.runtime.extras["gate"].set()
        await asyncio.wait_for(asyncio.gather(blocker, low, high, normal), 10)
        assert env.runtime.extras["order"] == ["critical", "high", "normal", "low"]

    async def test_agent_kill_switch(self, env: Env) -> None:
        running = await env.manager.submit(env.task("system", skill="hold"))
        await until(lambda: env.manager.status("system") == AgentStatus.WORKING)
        await env.manager.kill_agent("system", "testing")
        result = await asyncio.wait_for(running, 10)
        assert result.status == S.CANCELLED and "kill switch" in result.summary
        view = env.manager.view("system")
        assert view["status"] == "offline" and view["killed"]
        refused = await env.run("system", skill="note", title="x")
        assert refused.status == S.CANCELLED and refused.data["code"] == "kill_switch"
        assert (await env.run("planner", skill="decompose", params={"goal": "מחקר על קפה"})).ok   # others work on
        await env.manager.release_agent("system")
        assert env.manager.status("system") == AgentStatus.IDLE
        assert (await env.run("system", skill="note", title="after")).ok

    async def test_global_kill_switch(self, env: Env) -> None:
        await env.kit.kill_switch.engage("emergency")
        assert {v["status"] for v in env.manager.views()} == {"offline"}
        refused = await env.run("planner", skill="decompose", params={"goal": "x"})
        assert refused.data["code"] == "kill_switch"
        await env.kit.kill_switch.release()
        assert {v["status"] for v in env.manager.views()} == {"idle"}
        assert (await env.run("planner", skill="decompose", params={"goal": "x"})).ok

    async def test_a_crash_is_reported_and_the_agent_recovers(self, env: Env) -> None:
        result = await env.run("system", skill="crash")
        assert result.status == S.FAILED and result.data["code"] == "internal_error"
        assert "RuntimeError: boom" in result.errors
        view = env.manager.view("system")
        assert view["status"] == "error" and view["stats"]["failed"] == 1 and "boom" in view["errors"][0]["message"]
        await until(lambda: env.manager.status("system") == AgentStatus.IDLE)
        assert (await env.run("system", skill="note", title="fine")).ok

    async def test_unknown_skill(self, env: Env) -> None:
        result = await env.run("planner", skill="fly")
        assert result.status == S.FAILED and "no skill called 'fly'" in result.summary

    async def test_waiting_for_approval(self, env: Env) -> None:
        pending = await env.manager.submit(env.task("system", skill="power", params={"action": "shutdown"}))
        req = await env.pending()
        assert req.agent_id == "system" and req.tool == "power"
        assert env.manager.status("system") == AgentStatus.WAITING
        env.kit.broker.respond(req.id, False)
        result = await asyncio.wait_for(pending, 10)
        assert result.status == S.FAILED and "לא אושר" in result.summary and result.denied
        assert not [c for c in env.host.calls if c[0] == "power"]
        pending = await env.manager.submit(env.task("system", skill="power", params={"action": "restart",
                                                                                     "delay_s": 60}))
        env.kit.broker.respond((await env.pending()).id, True)
        result = await asyncio.wait_for(pending, 10)
        assert result.ok and ("power", "restart", 60) in env.host.calls


# ================================================================================== base agent
class TestBaseAgent:
    def test_parse_report(self) -> None:
        r = parse_report("## Done\nCreated app.py.\n\nMore detail.\n\n**STATUS:** partial\nNEXT: install Node.js")
        assert r.status == S.PARTIAL and r.next_step == "install Node.js"
        assert r.summary == "Done\nCreated app.py." and "STATUS" not in r.body and "More detail." in r.body
        r = parse_report("הכול מוכן.\nSTATUS: success\nNEXT: none")
        assert r.status == S.SUCCESS and r.next_step is None and r.summary == "הכול מוכן."
        assert parse_report("no markers").status is None

    async def test_guard_stops_a_slow_call_at_once(self) -> None:
        task = AgentTask(agent=AgentId.PLANNER, instruction="x")
        assert await RunContext(None, task).guard(asyncio.sleep(0, result=7)) == 7  # type: ignore[arg-type]
        token = CancelToken()
        run = RunContext(None, task, token)  # type: ignore[arg-type]
        started = asyncio.Event()

        async def slow() -> None:
            started.set()
            await asyncio.sleep(30)

        job = asyncio.ensure_future(run.guard(slow()))
        await started.wait()
        token.cancel("stop now")
        with pytest.raises(TaskCancelled, match="stop now"):
            await asyncio.wait_for(job, 2)

    async def test_skills_that_need_a_model_say_so_offline(self, env: Env) -> None:
        result = await env.run("development", "תכתוב לי סקריפט פייתון שממיין קבצים")
        assert result.status == S.FAILED and result.data["code"] == "ai_required" and result.next_step

    async def test_a_model_failure_falls_back_to_offline_work(self, env: Env) -> None:
        env.mock_http(web_handler)
        provider = env.use_ai(ProviderUnavailable("network down"))
        result = await env.run("research", "solar panel efficiency")
        assert result.ok and not result.used_llm and result.sources and len(provider.requests) == 1
        provider.push(ProviderError("overloaded"))
        result = await env.run("research", skill="research", params={"query": "solar panel efficiency"})
        assert result.ok and not result.used_llm and not result.errors and len(provider.requests) == 2

    async def test_a_refusal_is_reported_as_it_is(self, env: Env) -> None:
        env.use_ai(ProviderRefusal("declined"))
        result = await env.run("research", "something")
        assert result.status == S.FAILED and result.data["code"] == "refusal"

    async def test_tools_outside_an_agents_permissions_are_refused(self, env: Env) -> None:
        provider = env.use_ai(tool("run_command", {"command": "echo hi"}), reply("I could not run the command."))
        result = await env.run("planner", "check the disk with a command")
        assert result.status == S.PARTIAL and result.denied and result.used_llm
        assert "run_command" not in {t.name for t in provider.requests[0].tools}
        assert provider.requests[1].messages[-1].tool_results[0].is_error


# ================================================================================== planner
class TestPlanner:
    async def test_decompose_build_app(self, env: Env) -> None:
        goal = "תבנה לי אתר לחנות קפה"
        result = await env.run("planner", goal, skill="decompose",
                               params={"goal": goal, "intent": "build_app", "slots": {"kind": "store"}})
        assert result.ok and result.data["source"] == "rules"
        plan = TaskPlan.model_validate(result.result)
        plan.validate_dag()
        assert [s.key for s in plan.subtasks] == ["plan", "research", "design", "build", "test", "launch"]
        assert set(plan.agents) == set(AgentId)
        for sub in plan.subtasks:
            assert sub.skill in env.manager.agent(sub.agent).skills, sub.key
        build = plan.by_key("build")
        assert build is not None and build.params["kind"] == "store"
        assert set(build.depends_on) == {plan.by_key("plan").id, plan.by_key("design").id}  # type: ignore[union-attr]
        assert [s.key for s in plan.ready()] == ["plan", "research"]

    async def test_decompose_falls_back_to_one_agent(self, env: Env) -> None:
        result = await env.run("planner", skill="decompose", params={"goal": "מה ההבדל בין SSD ל-HDD"})
        plan = TaskPlan.model_validate(result.result)
        assert result.data["source"] == "fallback" and len(plan.subtasks) == 1
        assert plan.subtasks[0].agent == AgentId.RESEARCH

    async def test_decompose_with_the_model(self, env: Env) -> None:
        step = {"acceptance_criteria": [], "expected_files": [], "optional": False, "priority": "normal"}
        draft = {"title": "השוואת מחשבים", "strategy": "מחקר ואז מצגת", "steps": [
            {**step, "key": "compare", "agent": "research", "title": "השוואה", "instruction": "השווה 3 ניידים",
             "depends_on": [], "priority": "high"},
            {**step, "key": "deck", "agent": "creative", "title": "מצגת", "instruction": "מצגת מההשוואה",
             "depends_on": ["compare", "nope"], "optional": True}]}
        provider = env.use_ai(reply(json.dumps(draft, ensure_ascii=False)))
        result = await env.run("planner", skill="decompose", params={"goal": "תשווה ניידים ותכין מצגת"})
        assert result.ok and result.used_llm and result.data["source"] == "llm"
        compare, deck = TaskPlan.model_validate(result.result).subtasks
        assert deck.depends_on == [compare.id] and deck.optional and compare.priority == Priority.HIGH
        assert provider.requests[0].role == "planner" and provider.requests[0].json_schema

    async def test_decompose_survives_a_model_failure(self, env: Env) -> None:
        env.use_ai(ProviderError("overloaded"))
        result = await env.run("planner", skill="decompose", params={"goal": "תכתוב לי שיר על הים"})
        assert result.ok and result.data["source"] == "fallback" and "overloaded" in result.data["notes"][0]

    async def test_make_plan_offline(self, env: Env) -> None:
        result = await env.run("planner", "תכין לי תוכנית לחתונה")
        assert result.ok and result.skill == "make_plan" and not result.used_llm
        path = Path(result.result["path"])
        assert path.parent == env.kit.paths.workspace / "plans" and str(path) in result.files_created
        text = path.read_text(encoding="utf-8")
        plan = result.result["plan"]
        assert text.startswith("# תוכנית לחתונה") and len(plan["phases"]) >= 3
        assert all(p["title"] in text for p in plan["phases"]) and plan["finish"] in result.summary

    async def test_a_plan_fits_the_time_the_request_gives(self, env: Env) -> None:
        result = await env.run("planner", "ג'ארוויס, תכין לי תוכנית לימודים לשבוע ותשמור אותה כקובץ",
                               skill="make_plan", params={"start": "2026-11-01"})
        plan = result.result["plan"]
        assert plan["title"] == "תוכנית לימודים לשבוע" and plan["finish"] == "2026-11-07"
        assert sum(p["days"] for p in plan["phases"]) == 7 and "תאריך יעד" not in plan["missing"]
        assert Path(result.result["path"]).name.endswith("-תוכנית-לימודים-לשבוע.md")

    @pytest.mark.parametrize(("goal", "days"), [
        ("תוכנית לימודים לשבועיים", 14), ("תכנן לי טיול ל-3 ימים בצפון", 3), ("תוכנית ליום אחד בירושלים", 1),
        ("תכין לי תוכנית למשך 10 ימים", 10), ("תוך חודש לסיים את הפרויקט", 30), ("תוכנית לשנה הבאה", 365),
        ("a study plan for a week", 7), ("plan a 10-day trip to Italy", 10), ("build a website within 2 months", 60),
        ("תוכנית אימונים 3 פעמים לשבוע", None), ("תוכנית ליום הולדת של הבת שלי", None),
        ("תכנן לי את החודשים הקרובים", None), ("a workout plan for weeks", None), ("train 3 times a week", None),
    ])
    def test_the_time_a_request_gives(self, goal: str, days: int | None) -> None:
        assert documents.horizon_days(goal) == days
        plan = documents.build_plan(goal, "en")
        if days is not None:
            assert sum(p["days"] for p in plan["phases"]) == days and all(p["days"] >= 1 for p in plan["phases"])

    def test_titles_say_what_was_asked_for(self) -> None:
        assert documents.request_topic("ג'ארוויס, תעצב לי לוגו לבית קפה ותשמור אותו בתיקייה") == "לוגו לבית קפה"
        assert documents.request_topic("Jarvis, please write me an email to my landlord and save it to a file") == \
            "email to my landlord"
        assert rule_plan("plan", {}, "תכין לי תוכנית לחתונה ותשמור אותה כקובץ", "he").title == "תוכנית לחתונה"
        assert rule_plan("design", {}, "תעצב לי לוגו לבית קפה", "he").title == "עיצוב: לוגו לבית קפה"
        assert rule_plan("write_code", {}, "write me code that sorts photos", "en").title == "Code that sorts photos"

    async def test_files_and_results_are_recorded_in_memory(self, env: Env) -> None:
        result = await env.run("planner", "תכין לי תוכנית לטיול", task_id="task_trip")
        path = result.result["path"]
        files = await env.memory.recent_files(task_id="task_trip")
        assert [(f.path, f.agent_id, f.kind) for f in files] == [(path, "planner", "created")] and files[0].size
        [rec] = await env.memory.results("task_trip")
        assert rec.subtask_id == result.subtask_id and rec.agent_id == "planner" and rec.status == "success"
        assert rec.summary == result.summary and rec.data["files_created"] == [path]

    async def test_make_plan_with_the_model(self, env: Env) -> None:
        draft = {"title": "תוכנית לחתונה", "summary": "מתחילים מהאולם.", "phases": [
            {"title": "אולם", "days": 14, "tasks": [{"title": "לבחור אולם", "priority": "high", "owner": "user"}]},
            {"title": "הזמנות", "days": 7, "tasks": [{"title": "לעצב הזמנה", "priority": "normal",
                                                     "owner": "creative"}]}],
                 "first_actions": ["לקבוע תקציב"], "risks": ["חריגה מהתקציב"], "missing_information": ["מספר אורחים"]}
        env.use_ai(reply(json.dumps(draft, ensure_ascii=False)))
        result = await env.run("planner", skill="make_plan", params={"goal": "תוכנית לחתונה", "start": "2026-11-01"})
        plan = result.result["plan"]
        assert result.ok and result.used_llm and plan["source"] == "ai"
        assert [p["start"] for p in plan["phases"]] == ["2026-11-01", "2026-11-15"] and plan["finish"] == "2026-11-21"
        assert plan["phases"][1]["tasks"][0]["agent"] == "creative" and "מספר אורחים" in result.next_step
        assert "לבחור אולם" in Path(result.result["path"]).read_text(encoding="utf-8")

    async def test_project_brief(self, env: Env) -> None:
        result = await env.run("planner", skill="project_brief",
                               params={"goal": "חנות אונליין לפולי קפה", "kind": "store", "name": "Bean Shop"})
        brief = result.result
        assert result.ok and brief["kind"] == "store" and len(brief["pages"]) == 4 and brief["acceptance"]
        assert [s["agent"] for s in brief["stages"]] == ["planner", "creative", "development", "development", "system"]
        assert Path(brief["path"]).name == "bean-shop-brief.md" and "Bean Shop" in Path(brief["path"]).read_text(
            encoding="utf-8")

    async def test_review_progress(self, env: Env) -> None:
        await env.memory.save_task(TaskRecord(id="task_a", title="אתר לחנות קפה", status="completed",
                                              summary="האתר מוכן"))
        await env.memory.save_task(TaskRecord(id="task_b", title="מצגת", status="failed", error="האחסון מלא",
                                              data={"plan": {"subtasks": [{"title": "יצירת קבצים", "agent": "creative",
                                                                           "status": "failed", "error": "disk full"}]}}))
        await env.memory.save_task(TaskRecord(id="task_c", title="כיבוי המחשב", status="waiting"))
        result = await env.run("planner", skill="review_progress")
        assert result.ok and result.result["counts"] == {"completed": 1, "failed": 1, "waiting": 1}
        assert any("disk full" in b for b in result.result["blockers"])
        assert "כיבוי המחשב" in result.next_step and "3 משימות" in result.summary

    async def test_folder_review_moves_nothing(self, env: Env) -> None:
        root = env.kit.paths.workspace / "inbox"
        (root / "app").mkdir(parents=True)
        (root / "app" / "package.json").write_text("{}", encoding="utf-8")
        for name in ("photo.jpg", "invoice.pdf", "notes.txt", "song.mp3"):
            (root / name).write_bytes(b"x" * 10)
        before = sorted(p.name for p in root.iterdir())
        result = await env.run("planner", skill="folder_review", params={"path": str(root)})
        analysis = result.result
        assert result.ok and analysis["loose"] == 4 and analysis["projects"] == ["app"] and not analysis["is_project"]
        assert len(analysis["suggestions"]) >= 2 and sorted(p.name for p in root.iterdir()) == before


# ================================================================================== development
class TestDevelopment:
    async def test_create_verify_and_open_a_website(self, env: Env) -> None:
        result = await env.run("development", skill="create_project",
                               params={"kind": "website", "name": "Coffee House", "description": "אתר לבית קפה"})
        assert result.ok, result.summary
        folder = Path(result.result["path"])
        assert folder.parent == env.kit.paths.projects and (folder / "index.html").is_file()
        assert len(result.files_created) == result.result["files"]
        record = await env.memory.project("Coffee House")
        assert record is not None and Path(record.path) == folder and record.meta["kind"] == "website"
        again = await env.run("development", skill="create_project", params={"kind": "website", "name": "Coffee House"})
        assert again.ok and Path(again.result["path"]).name == "coffee-house-2" and not again.errors
        verified = await env.run("development", skill="verify_project", params={"path": str(folder)})
        assert verified.result["static"]["problems"] == []
        if shutil.which("npm"):
            assert verified.status == S.SUCCESS, verified.details
            assert verified.result["tests"]["ok"] and "בדיקות עברו" in verified.summary
        else:
            assert verified.status == S.PARTIAL and "Node.js" in verified.next_step
        opened = await env.run("development", skill="run_project", params={"path": str(folder)})
        assert opened.ok and ("open_path", str(folder / "index.html")) in env.host.calls

    async def test_verify_finds_a_broken_link(self, env: Env) -> None:
        created = await env.run("development", skill="create_project", params={"kind": "website", "name": "Broken"})
        index = Path(created.result["path"]) / "index.html"
        html = index.read_text(encoding="utf-8")
        index.write_text(html.replace("</head>", '<link rel="stylesheet" href="css/missing.css">\n</head>', 1),
                         encoding="utf-8")
        # The project is no longer exactly as JARVIS generated it, so running its tests asks first.
        verifying = asyncio.create_task(env.run("development", skill="verify_project",
                                                params={"path": str(index.parent)}))
        req = await env.pending()
        assert req.tool == "run_tests" and req.risk == "high" and not req.irreversible
        assert req.scope == str(index.parent)
        env.kit.broker.respond(req.id, True)
        verified = await verifying
        assert verified.status == S.FAILED and "missing.css" in (verified.details or "")
        assert any(p["kind"] == "missing" for p in verified.result["static"]["problems"])

    async def test_the_brand_palette_reaches_the_project(self, env: Env) -> None:
        from jarvis.creative.palette import generate_palette

        palette = generate_palette("bakery", base_color="#1E7A46")
        design = {"key": "design", "agent": "creative", "status": "success", "result": {"palette": palette.to_dict()}}
        result = await env.run("development", skill="create_project", context={"dependencies": [design]},
                               params={"kind": "website", "name": "Green Bakery"})
        css = (Path(result.result["path"]) / "css" / "site.css").read_text(encoding="utf-8")
        assert palette.hex("primary").lower() in css.lower()

    async def test_installing_missing_dependencies_waits_for_approval(self, env: Env) -> None:
        project = env.kit.paths.projects / "calc"
        (project / "tests").mkdir(parents=True)
        (project / "requirements.txt").write_text("jarvis-missing-package-xyz\n", encoding="utf-8")
        (project / "tests" / "test_calc.py").write_text("import jarvis_missing_mod_xyz\n\n\ndef test_x():\n"
                                                        "    assert jarvis_missing_mod_xyz\n", encoding="utf-8")
        job = asyncio.ensure_future(env.run("development", skill="run_tests", params={"path": str(project)}))
        tests = await env.pending()    # the project's own code: JARVIS did not generate it, so it asks first
        assert tests.tool == "run_tests" and tests.risk == "high"
        env.kit.broker.respond(tests.id, True)
        await until(lambda: bool(env.kit.broker.pending()) and env.kit.broker.pending()[0].id != tests.id)
        req = await env.pending()
        assert req.tool == "install_packages" and req.agent_id == "development"
        env.kit.broker.respond(req.id, False)
        result = await job
        assert result.status == S.PARTIAL and "התלויות לא הותקנו" in result.summary and result.next_step
        assert not (project / ".venv").exists()

    @pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
    async def test_git_snapshot(self, env: Env) -> None:
        project = env.kit.paths.projects / "notes"
        project.mkdir(parents=True)
        (project / "a.txt").write_text("hello\n", encoding="utf-8")
        result = await env.run("development", skill="git", params={"action": "commit", "path": str(project),
                                                                   "message": "first snapshot"})
        assert result.ok and not result.errors, result.summary
        assert (project / ".git").is_dir()
        log = await env.run("development", skill="git", params={"action": "log", "path": str(project)})
        assert "first snapshot" in (log.details or "")
        again = await env.run("development", skill="git", params={"action": "commit", "path": str(project)})
        assert again.ok and not again.errors and again.summary == "אין שינויים לשמור."

    async def test_write_code_with_the_model(self, env: Env) -> None:
        target = env.kit.paths.workspace / "code" / "hello" / "hello.py"
        provider = env.use_ai(
            tool("write_file", {"path": str(target), "content": "print('shalom')\n"}),
            tool("run_python", {"file": str(target)}),
            reply("כתבתי את hello.py והרצתי אותו: הוא מדפיס shalom.\n\nSTATUS: success\nNEXT: none"),
        )
        result = await env.run("development", "תכתוב סקריפט שמדפיס שלום", skill="write_code")
        assert result.status == S.SUCCESS and result.used_llm and result.provider == "anthropic"
        assert str(target) in result.files_created and target.read_text(encoding="utf-8") == "print('shalom')\n"
        assert result.summary.startswith("כתבתי") and result.next_step is None
        offered = {t.name for t in provider.requests[0].tools}
        assert {"write_file", "run_python", "run_tests", "git"} <= offered and "power" not in offered
        assert "workspace/code/" in provider.requests[0].system
        ran = provider.requests[2].messages[-1].tool_results[0]
        assert not ran.is_error and "shalom" in ran.content

    async def test_create_project_keeps_the_template_when_the_model_fails(self, env: Env) -> None:
        env.use_ai(ProviderError("overloaded"))
        result = await env.run("development", skill="create_project", params={"kind": "game", "name": "Snake"})
        assert result.status == S.PARTIAL and (Path(result.result["path"]) / "index.html").is_file()
        assert "overloaded" in result.errors


# ================================================================================== creative
class TestCreative:
    def test_routing_and_email_parsing(self) -> None:
        assert route("תעצב לי לוגו לחברה", DESIGN_ROUTES, "brand_kit") == "logo"
        assert route("תכין מצגת על החלל", DESIGN_ROUTES, "brand_kit") == "presentation"
        assert route("something else", DESIGN_ROUTES, "brand_kit") == "brand_kit"
        assert parse_email("תכתוב מייל לדנה שאני חולה מחר") == ("דנה", "אני חולה מחר")
        assert parse_email("write an email to Dana about the meeting") == ("Dana", "the meeting")

    async def test_a_logo_from_a_spoken_request(self, env: Env) -> None:
        result = await env.run("creative", "תעצב לי לוגו לחברה בשם נובה")
        assert result.ok and result.skill == "design" and "נובה" in result.summary
        logos = [f for f in result.files_created if Path(f).name.startswith("logo-")]
        assert len(logos) >= 3 and all(Path(f).is_file() for f in logos)
        assert ("open_path", result.result["gallery"]) in env.host.calls

    async def test_brand_kit_hands_its_palette_on(self, env: Env) -> None:
        result = await env.run("creative", skill="brand_kit", params={"brief": "מאפייה שכונתית", "name": "Lev Bakery"})
        roles = {c["role"] for c in result.result["palette"]["colors"]}
        names = {Path(f).name for f in result.files_created}
        assert result.ok and {"primary", "secondary", "accent"} <= roles and {"palette.json", "brand.md"} <= names
        assert not [c for c in env.host.calls if c[0] == "open_path"]   # a kit made inside a build stays closed

    async def test_presentation_offline(self, env: Env) -> None:
        result = await env.run("creative", skill="presentation",
                               params={"topic": "בינה מלאכותית לעסקים קטנים", "n_slides": 5})
        assert result.ok and not result.used_llm
        assert Path(result.result["pptx"]).is_file() and Path(result.result["html"]).is_file()
        assert ("open_path", result.result["html"]) in env.host.calls

    async def test_presentation_with_the_model(self, env: Env) -> None:
        def slide(title: str, layout: str, bullets: list[str]) -> dict[str, Any]:
            return {"title": title, "layout": layout, "bullets": bullets, "notes": "", "columns": []}

        deck = {"title": "AI לעסקים", "slides": [slide("AI לעסקים קטנים", "title", ["מדריך מעשי"]),
                                                  slide("איפה מתחילים", "bullets", ["שירות", "שיווק", "חשבונות"]),
                                                  slide("תודה", "closing", ["שאלות?"])]}
        provider = env.use_ai(reply(json.dumps(deck, ensure_ascii=False)))
        result = await env.run("creative", skill="presentation", params={"topic": "AI לעסקים", "open_result": False})
        assert result.ok and result.used_llm and provider.requests[0].json_schema
        assert "איפה מתחילים" in Path(result.result["html"]).read_text(encoding="utf-8")

    async def test_an_email_draft_is_saved_not_sent(self, env: Env) -> None:
        result = await env.run("creative", "תכתוב מייל לדנה שאני חולה מחר")
        text = result.result["text"]
        assert result.ok and result.skill == "write_text" and result.result["kind"] == "email"
        assert "שלום דנה" in text and "מחר" in text and "היום" not in text
        assert Path(result.result["path"]).read_text(encoding="utf-8").strip() == text.strip()

    async def test_name_ideas(self, env: Env) -> None:
        result = await env.run("creative", skill="ideas", params={"topic": "בית קפה", "kind": "names", "n": 6})
        assert result.ok and len(result.result["items"]) == 6 and result.details.count("\n") >= 6


# ================================================================================== research
WEB_DDG = """
<div class="result"><a class="result__a" href="https://example.com/solar">Solar basics</a>
<a class="result__snippet">Solar panel efficiency explained.</a></div>
<div class="result"><a class="result__a" href="https://example.org/panels">Panel guide</a>
<a class="result__snippet">How efficient are solar panels?</a></div>
"""
WEB_PAGE = """<html><head><title>{title}</title></head><body><main><h1>{title}</h1>
<p>Modern solar panel efficiency is between 18 and 23 percent for most home panels.</p>
<p>Panel efficiency drops slightly as the temperature rises, so cooler climates help.</p>
<p>Unrelated sentence about gardening tools and flowers.</p></main></body></html>"""


def web_handler(request: httpx.Request) -> httpx.Response:
    if "duckduckgo" in request.url.host:
        return httpx.Response(200, text=WEB_DDG, headers={"content-type": "text/html"})
    return httpx.Response(200, text=WEB_PAGE.format(title=request.url.host),
                          headers={"content-type": "text/html; charset=utf-8"})


def weather_handler(request: httpx.Request) -> httpx.Response:
    if "geocoding" in request.url.host:
        return httpx.Response(200, json={"results": [{"name": "חיפה", "country": "ישראל", "latitude": 32.79,
                                                      "longitude": 34.99}]})
    return httpx.Response(200, json={
        "current": {"temperature_2m": 24.4, "apparent_temperature": 25.1, "relative_humidity_2m": 60,
                    "wind_speed_10m": 12, "weather_code": 1},
        "daily": {"weather_code": [1, 61], "temperature_2m_max": [27.0, 22.0], "temperature_2m_min": [19.0, 17.0],
                  "precipitation_probability_max": [5, 70]},
    })


class TestResearch:
    async def test_weather_tomorrow(self, env: Env) -> None:
        seen = env.mock_http(weather_handler)
        result = await env.run("research", "מה מזג האוויר בחיפה מחר")
        assert result.ok and result.skill == "weather" and result.result["max_c"] == 22.0
        assert result.summary.startswith("מחר בחיפה") and "גשם קל" in result.summary and "70%" in result.summary
        assert seen[0].url.params["name"] == "חיפה" and env.memory.short.get("weather_location") == "חיפה"

    async def test_weather_uses_the_remembered_city(self, env: Env) -> None:
        env.mock_http(weather_handler)
        await env.memory.remember("אני גר בחיפה")
        result = await env.run("research", skill="weather")
        assert result.ok and result.summary.startswith("עכשיו בחיפה")

    async def test_weather_asks_for_a_city(self, env: Env) -> None:
        result = await env.run("research", skill="weather")
        assert result.status == S.FAILED and result.data["code"] == "need_location"

    async def test_offline_research_reads_and_cites_sources(self, env: Env) -> None:
        env.mock_http(web_handler)
        result = await env.run("research", skill="research", params={"query": "solar panel efficiency", "save": True})
        assert result.status == S.SUCCESS and {s["url"] for s in result.sources} == {
            "https://example.com/solar", "https://example.org/panels"}
        assert "efficiency" in result.summary and "gardening" not in result.summary
        saved = Path(result.files_created[0])
        assert saved.parent == env.kit.paths.workspace / "research" and "## מקורות" in saved.read_text(encoding="utf-8")

    async def test_analyze_a_table(self, env: Env) -> None:
        data = env.kit.paths.workspace / "sales.csv"
        data.write_text("month,amount,region\nJan,100,North\nFeb,250,South\nMar,150,North\n", encoding="utf-8")
        result = await env.run("research", skill="analyze_data", params={"path": str(data)})
        amount = next(c for c in result.result["columns"] if c["name"] == "amount")
        region = next(c for c in result.result["columns"] if c["name"] == "region")
        assert result.result["rows"] == 3 and amount["sum"] == 500 and amount["median"] == 150
        assert tuple(region["top"][0]) == ("North", 2)
        big = env.kit.paths.workspace / "big.csv"     # several read_file pages
        big.write_text("id,value\n" + "".join(f"{i},{i % 7}\n" for i in range(12000)), encoding="utf-8")
        result = await env.run("research", skill="analyze_data", params={"path": str(big)})
        value = next(c for c in result.result["columns"] if c["name"] == "value")
        assert result.result["rows"] == 12000 and value["sum"] == sum(i % 7 for i in range(12000))

    async def test_summarize_text(self, env: Env) -> None:
        text = ("JARVIS runs on the user's computer. It has five agents. The Research Agent finds information. "
                "The System Agent controls the computer. Every risky action needs approval.")
        result = await env.run("research", skill="summarize", params={"text": text, "max_sentences": 2})
        assert result.ok and len(result.result["points"]) == 2


# ================================================================================== system
class TestSystem:
    async def test_open_an_app_from_speech(self, env: Env) -> None:
        result = await env.run("system", "פתח את כרום")
        assert result.ok and result.skill == "open_app" and "Google Chrome" in result.summary
        assert ("launch", "Google Chrome", ()) in env.host.calls

    async def test_organize_a_folder_by_type(self, env: Env) -> None:
        inbox = env.kit.paths.workspace / "inbox"
        inbox.mkdir()
        for name in ("a.jpg", "b.pdf", "c.mp3"):
            (inbox / name).write_bytes(b"1")
        result = await env.run("system", skill="organize_folder", params={"path": str(inbox)})
        assert result.ok and all(p.is_dir() for p in inbox.iterdir())
        assert sorted(p.name for p in inbox.rglob("*") if p.is_file()) == ["a.jpg", "b.pdf", "c.mp3"]

    async def test_open_a_project_after_a_build(self, env: Env) -> None:
        built = await env.run("development", skill="create_project", params={"kind": "game", "name": "Snake"})
        dependency = {"key": "build", "agent": "development", "status": "success", "result": built.result}
        result = await env.run("system", skill="open_project", params={"name": "Snake"},
                               context={"dependencies": [dependency]})
        assert result.ok and ("open_path", str(Path(built.result["path"]) / "index.html")) in env.host.calls

    async def test_an_unknown_project(self, env: Env) -> None:
        result = await env.run("system", skill="open_project", params={"name": "does not exist"})
        assert result.status == S.FAILED and result.data["code"] == "project_not_found"

    async def test_delete_goes_to_the_recycle_bin(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        import send2trash

        trashed: list[str] = []
        monkeypatch.setattr(send2trash, "send2trash", lambda p: (trashed.append(p), shutil.rmtree(p)))
        folder = env.kit.paths.workspace / "ישן"
        folder.mkdir()
        (folder / "a.txt").write_text("x")
        result = await env.run("system", skill="delete_path", params={"name": "ישן", "kind": "folder"})
        assert result.ok and trashed == [str(folder)] and not folder.exists()
        assert result.summary == "העברתי את התיקייה ישן לסל המחזור." and result.data["permanent"] is False

    async def test_a_permanent_delete_waits_for_approval(self, env: Env) -> None:
        folder = env.kit.paths.workspace / "פרויקט-דמו"
        folder.mkdir()
        params = {"name": "פרויקט-דמו", "kind": "folder", "permanent": True}
        pending = await env.manager.submit(env.task("system", skill="delete_path", params=params))
        req = await env.pending()
        assert req.tool == "delete_path" and req.summary.startswith("מחיקה לצמיתות: פרויקט-דמו")
        env.kit.broker.respond(req.id, False)
        result = await asyncio.wait_for(pending, 10)
        assert result.status == S.FAILED and result.denied and folder.exists()
        pending = await env.manager.submit(env.task("system", skill="delete_path", params=params))
        env.kit.broker.respond((await env.pending()).id, True)
        result = await asyncio.wait_for(pending, 10)
        assert result.ok and not folder.exists() and result.summary == "מחקתי לצמיתות את התיקייה פרויקט-דמו."

    async def test_delete_never_guesses_the_target(self, env: Env) -> None:
        missing = await env.run("system", skill="delete_path", params={"name": "nothing-here"})
        assert missing.status == S.FAILED and missing.data["code"] == "need_location"
        assert missing.summary == "לא מצאתי את nothing-here בסביבת העבודה. איפה זה נמצא?"
        notes = env.kit.paths.workspace / "notes"
        notes.write_text("x")
        wrong = await env.run("system", skill="delete_path", params={"name": "notes", "kind": "folder"})
        assert wrong.status == S.FAILED and "קובץ ולא תיקייה" in wrong.summary and notes.exists()
        assert not env.kit.broker.pending()
