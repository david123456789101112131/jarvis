"""Stage 2 (JARVIS Core): routing text and voice commands to instant answers, the fast path, the
orchestrator and conversation; modes; control commands; approvals by voice; context; memory.

The core runs on the real five agents, Agent Manager, Task Orchestrator, bus, permission gate and
memory. The desktop is `FakeHost`, web services are `httpx.MockTransport`, the model is
`ScriptedProvider`.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

import jarvis.orchestration.orchestrator as orchestrator_module
from agent_env import Env, until
from fakes import reply
from jarvis.ai.types import AIResponse, CompletionRequest
from jarvis.common.enums import AgentId, AgentStatus, Mode, Priority, TaskStatus
from jarvis.common.errors import ProviderError
from jarvis.core.core import JarvisCore
from jarvis.core.intents import I
from jarvis.memory.models import TaskRecord
from jarvis.orchestration import SubTask, TaskOrchestrator, TaskPlan

S = TaskStatus


class C:
    """The core plus what it published: chat messages, action cards, pipeline steps and speech."""

    def __init__(self, env: Env, core: JarvisCore) -> None:
        self.env, self.core, self.orch = env, core, core.orchestrator
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.spoken: list[tuple[str, str]] = []
        for topic in ("chat.message", "action", "voice.pipeline", "mode", "ui.navigate"):
            env.kit.bus.subscribe(topic, self._record, name=f"test-{topic}")
        core.set_speaker(self._speak)

    async def _record(self, msg: Any) -> None:
        self.events.append((msg.topic, dict(msg.data)))

    async def _speak(self, text: str, lang: str) -> None:
        self.spoken.append((text, lang))

    @property
    def host(self) -> Any:
        return self.env.host

    async def say(self, text: str, source: str = "text") -> Any:
        return await asyncio.wait_for(self.core.handle_input(text, source=source), 30)

    def of(self, topic: str) -> list[dict[str, Any]]:
        return [d for t, d in self.events if t == topic]

    def jarvis(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [m for m in self.of("chat.message") if m["role"] == "jarvis" and (kind is None or m["kind"] == kind)]

    async def finished(self, task_id: str, timeout: float = 60) -> Any:
        return await self.orch.wait(task_id, timeout=timeout)

    def plan(self, agent: str, skill: str, **params: Any) -> TaskPlan:
        sub = SubTask(key="main", agent=AgentId.parse(agent), title=f"{skill} step", instruction=skill, skill=skill,
                      params=params)
        return TaskPlan(title="probe", strategy="test", subtasks=[sub], source="direct")


@pytest.fixture
async def c(env: Env, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(orchestrator_module, "RESTART_WAIT_S", 0.3)
    env.kit.settings.orchestrator.retry_backoff_s = 0.01
    env.kit.settings.orchestrator.retry_backoff_max_s = 0.05
    env.kit.settings.security.confirm_timeout_s = 20
    env.kit.broker.timeout_s = 20
    orch = TaskOrchestrator(env.manager)
    core = JarvisCore(settings=env.kit.settings, manager=env.manager, orchestrator=orch, memory=env.memory,
                      bus=env.kit.bus, broker=env.kit.broker, kill_switch=env.kit.kill_switch,
                      activity=env.kit.activity)
    orch.mode_provider = lambda: core.mode
    env.kit.mode = Mode.NORMAL
    await core.start()
    try:
        yield C(env, core)
    finally:
        await core.shutdown()


def weather_handler(request: httpx.Request) -> httpx.Response:
    if "geocoding" in request.url.host:
        if request.url.params["name"] != "חיפה":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"results": [{"name": "חיפה", "country": "ישראל", "latitude": 32.79,
                                                      "longitude": 34.99}]})
    return httpx.Response(200, json={
        "current": {"temperature_2m": 24.4, "apparent_temperature": 25.1, "relative_humidity_2m": 60,
                    "wind_speed_10m": 12, "weather_code": 1},
        "daily": {"weather_code": [1, 61], "temperature_2m_max": [27.0, 22.0], "temperature_2m_min": [19.0, 17.0],
                  "precipitation_probability_max": [5, 70]},
    })


def chat_turn(text: str, run_task: bool = False, task: str = "") -> Any:
    def respond(request: CompletionRequest) -> AIResponse:
        assert request.role == "chat"
        if request.json_schema:
            return reply(json.dumps({"reply": text, "run_task": run_task, "task": task}, ensure_ascii=False))
        return reply(text)

    return respond


# ================================================================================== the fast path
class TestFastPath:
    async def test_open_an_app_by_voice_in_hebrew(self, c: C) -> None:
        started = time.monotonic()
        res = await c.say("ג'ארוויס, פתח לי את כרום", source="voice")
        assert time.monotonic() - started < 2.0
        assert res.accepted and res.intent["name"] == I.OPEN_APP and res.intent["complexity"] == "simple"
        assert ("launch", "Google Chrome", ()) in c.host.calls
        assert res.reply["text"] == "פתחתי את Google Chrome." and res.reply["kind"] == "result"
        assert res.speech["text"] == "פתחתי את Google Chrome." and res.speech["language"] == "he"
        assert res.message["role"] == "user" and res.message["source"] == "voice"
        cards = c.of("action")
        assert [x["status"] for x in cards] == ["running", "done"] and cards[0]["title"] == "פותח את Google Chrome…"
        steps = [(p["step"], p["status"]) for p in c.of("voice.pipeline")]
        assert steps[:4] == [("understanding", "done"), ("routing", "done"), ("selecting_tool", "done"),
                             ("executing", "active")] and ("executing", "done") in steps
        assert ("done", "done") not in steps          # the voice pipeline reports the end of a voice turn
        [cmd] = await c.env.memory.command_history(5)
        assert cmd.text == "ג'ארוויס, פתח לי את כרום" and cmd.intent == I.OPEN_APP and cmd.ok is True

    async def test_english_commands_get_english_answers(self, c: C) -> None:
        res = await c.say("open notepad")
        assert res.reply["text"] == "Opened Notepad." and res.reply["language"] == "en"
        assert ("done", "done") in [(p["step"], p["status"]) for p in c.of("voice.pipeline")]

    async def test_volume_and_screenshot(self, c: C) -> None:
        await c.say("volume 30")
        assert c.host.volume == 30
        res = await c.say("צלם את המסך")
        assert res.reply["kind"] == "result" and any(call[0] == "screenshot" for call in c.host.calls)

    async def test_a_stopped_agent_says_so(self, c: C) -> None:
        await c.say("עצור את סוכן המערכת")
        res = await c.say("פתח את כרום")
        assert res.reply["kind"] == "error" and "כבוי" in res.reply["text"]
        assert not any(call[0] == "launch" for call in c.host.calls)
        assert (await c.env.memory.command_history(1))[0].ok is False
        await c.say("הפעל את סוכן המערכת")
        res = await c.say("פתח את כרום")
        assert res.reply["kind"] == "result"

    async def test_a_question_and_its_answer(self, c: C) -> None:
        c.env.mock_http(weather_handler)
        res = await c.say("מה מזג האוויר")
        assert res.reply["kind"] == "info" and "באיזו עיר" in res.reply["text"] and res.speech["follow_up"]
        assert c.of("action")[-1]["status"] == "info"          # a question, not an error
        res = await c.say("בחיפה")                    # the answer completes the question (prefix and all)
        assert res.intent["name"] == I.WEATHER and res.reply["kind"] == "result"
        assert res.reply["text"].startswith("עכשיו בחיפה")

    async def test_it_refers_to_the_last_thing(self, c: C) -> None:
        await c.say("open notepad")
        res = await c.say("close it")
        assert res.intent["name"] == I.CLOSE_APP and ("close", "notepad", False) in c.host.calls

    async def test_an_approval_by_voice(self, c: C) -> None:
        res = await c.say("כבה את המחשב", source="voice")
        assert res.speech["ack_key"] == "confirm" and res.speech["follow_up"] and "לאשר" in res.reply["text"]
        assert c.env.kit.broker.pending() and not any(call[0] == "power" for call in c.host.calls)
        res = await c.say("כן", source="voice")
        assert res.intent["name"] == "confirm" and res.reply["text"].startswith("אישרתי")
        await until(lambda: any(call[0] == "power" for call in c.host.calls))
        await until(lambda: c.jarvis("result"))
        assert c.spoken and c.jarvis("result")[-1]["text"] == c.spoken[-1][0]

    async def test_a_denial(self, c: C) -> None:
        await c.say("shut down the computer")
        res = await c.say("no")
        assert res.reply["text"].startswith("Not approved")
        await until(lambda: c.jarvis("error"))
        assert not any(call[0] == "power" for call in c.host.calls)
        assert (await c.env.memory.command_history(5))[1].ok is False

    async def test_a_delete_asks_where_and_then_for_approval(self, c: C) -> None:
        desktop = c.host.folders["desktop"] = c.env.kit.user_docs / "Desktop"   # one of the user's folders
        (desktop / "ישן").mkdir(parents=True)
        res = await c.say("תמחק לצמיתות את התיקייה ישן")
        assert res.intent["name"] == I.DELETE_PATH and res.reply["text"].endswith("איפה זה נמצא?")
        assert res.reply["kind"] == "info" and c.env.manager.status("system") == AgentStatus.IDLE
        assert res.speech["follow_up"] and (desktop / "ישן").exists()
        res = await c.say("משולחן העבודה")              # the answer completes the command
        assert res.intent["name"] == I.DELETE_PATH and "לאשר" in res.reply["text"]
        assert "מחיקה לצמיתות: ישן" in res.reply["text"] and (desktop / "ישן").exists()
        await c.say("כן")
        await until(lambda: not (desktop / "ישן").exists())
        await until(lambda: c.jarvis("result"))
        assert c.jarvis("result")[-1]["text"] == "מחקתי לצמיתות את התיקייה ישן."

    async def test_a_spoken_yes_is_not_enough_for_what_cannot_be_undone(self, c: C) -> None:
        desktop = c.host.folders["desktop"] = c.env.kit.user_docs / "Desktop"
        (desktop / "ישן").mkdir(parents=True)
        await c.say("תמחק לצמיתות את התיקייה ישן", source="voice")
        res = await c.say("משולחן העבודה", source="voice")
        assert "לאשר" in res.reply["text"] and c.env.kit.broker.pending()
        res = await c.say("כן", source="voice")              # a video or the TV could say it
        assert "אי אפשר לבטל" in res.reply["text"] and "בלחיצה" in res.reply["text"]
        assert c.env.kit.broker.pending() and (desktop / "ישן").exists()
        await c.say("כן")                                    # typed: approved
        await until(lambda: not (desktop / "ישן").exists())

    async def test_a_new_command_is_not_taken_as_the_answer(self, c: C) -> None:
        await c.say("תמחק את הקובץ ישן.txt")                       # "where is it?"
        res = await c.say("permanently delete the folder demo")     # said in full: a command of its own
        assert res.intent["name"] == I.DELETE_PATH
        assert res.reply["text"] == "I could not find demo in the workspace. Where is it?"

    async def test_yes_without_a_pending_approval_is_conversation(self, c: C) -> None:
        res = await c.say("כן")
        assert res.intent["name"] == I.CHAT


# ================================================================================== tasks
class TestTasks:
    async def test_build_a_store_website(self, c: C) -> None:
        res = await c.say("ג'ארוויס, תבנה לי אתר לחנות קפה", source="voice")
        assert res.reply["text"] == "קיבלתי. אני מחלק את המשימה בין הסוכנים." and res.reply["kind"] == "ack"
        assert res.speech == {"text": None, "ack_key": "complex_ack", "language": "he", "follow_up": False}
        task = await c.finished(res.task_id)
        assert task.status == S.COMPLETED and task.source == "voice" and len(task.plan.subtasks) == 6
        [plan] = c.jarvis("plan")
        assert plan["text"].startswith("התוכנית (6 שלבים)") and "סוכן פיתוח" in plan["text"]
        await until(lambda: c.jarvis("result"))
        [result] = c.jarvis("result")
        assert result["task_id"] == task.id and result["text"].startswith(task.outcome.summary)
        assert c.spoken == [(task.outcome.summary, "he")]
        cmd = (await c.env.memory.command_history(1))[0]
        assert cmd.task_id == task.id and cmd.ok is True

    async def test_text_tasks_are_not_spoken(self, c: C) -> None:
        res = await c.say("תכין לי תוכנית לחתונה")
        assert res.reply["text"] == "קיבלתי. סוכן תכנון מתחיל לעבוד על זה."
        task = await c.finished(res.task_id)
        await until(lambda: c.jarvis("result"))
        assert task.status == S.COMPLETED and not c.spoken and not c.jarvis("plan")   # one step: no plan message

    async def test_give_an_agent_a_task(self, c: C) -> None:
        res = await c.say("תן לסוכן המערכת את המשימה: בדוק את המחשב")
        assert res.intent["name"] == I.AGENT_ASSIGN
        task = await c.finished(res.task_id)
        assert task.plan.source == "direct" and task.plan.subtasks[0].agent == AgentId.SYSTEM
        assert task.status == S.COMPLETED      # offline, the System Agent routes the instruction to a skill
        assert task.title == "בדוק את המחשב"    # the task is the instruction, not the words that handed it over

    async def test_stop_the_task(self, c: C) -> None:
        task = await c.orch.submit("hold on", plan=c.plan("system", "hold"), language="en")
        await until(lambda: task.status == S.RUNNING)
        res = await c.say("stop the task")
        assert res.reply["text"] == "I stopped the task 'hold on'." and res.speech["ack_key"] == "cancelled"
        assert task.status == S.CANCELLED
        await asyncio.sleep(0.1)
        assert not [m for m in c.jarvis() if m.get("task_id") == task.id and m["kind"] != "answer"]

    async def test_nothing_to_stop(self, c: C) -> None:
        assert (await c.say("עצור")).reply["text"] == "אין משימה פעילה לעצור."

    async def test_cancel_all_tasks(self, c: C) -> None:
        a = await c.orch.submit("first", plan=c.plan("system", "hold"), language="en")
        b = await c.orch.submit("second", plan=c.plan("research", "summarize", text="x " * 50), language="en",
                                priority=Priority.LOW)
        await until(lambda: a.status == S.RUNNING)
        res = await c.say("cancel all tasks")
        assert res.accepted and a.status == S.CANCELLED and b.status in (S.CANCELLED, S.COMPLETED)

    async def test_kill_switch_pauses_and_resume_continues(self, c: C) -> None:
        task = await c.orch.submit("long job", plan=c.plan("system", "hold"), language="en")
        await until(lambda: task.status == S.RUNNING)
        res = await c.say("עצור הכל")
        assert c.env.kit.kill_switch.global_engaged and res.speech["ack_key"] == "stopped"
        assert "מתג החירום פעיל" in res.reply["text"] and "1 משימות נעצרו" in res.reply["text"]
        await c.finished(task.id)
        assert task.status == S.INTERRUPTED
        refused = await c.say("פתח את כרום")
        assert "מתג החירום פעיל" in refused.reply["text"] and not any(x[0] == "launch" for x in c.host.calls)
        blocked = await c.say("תמשיך את המשימה")
        assert "עדיין פעיל" in blocked.reply["text"]
        released = await c.say("שחרר את מתג החירום")
        assert not c.env.kit.kill_switch.global_engaged and "long job" in released.reply["text"]
        c.env.runtime.extras.setdefault("gate", asyncio.Event()).set()
        resumed = await c.say("תמשיך")
        assert resumed.task_id == task.id and resumed.reply["text"].startswith("ממשיך את 'long job'")
        done = await c.finished(task.id)
        assert done.status == S.COMPLETED and done.runs == 2

    async def test_nothing_to_resume(self, c: C) -> None:
        assert (await c.say("continue the task")).reply["text"] == "There's no paused task to continue."

    async def test_agent_control(self, c: C) -> None:
        res = await c.say("stop the research agent")
        assert not c.env.manager.view("research")["enabled"] and "stopped the Research Agent" in res.reply["text"]
        res = await c.say("start the research agent")
        assert c.env.manager.view("research")["enabled"] and res.reply["text"] == "The Research Agent is active."
        res = await c.say("הפעל מחדש את סוכן המערכת")
        assert res.reply["text"] == "הפעלתי מחדש את סוכן מערכת." and c.env.manager.is_available("system")
        res = await c.say("stop all agents")
        assert not any(v["enabled"] for v in c.env.manager.views())

    async def test_recovered_tasks_are_announced(self, env: Env) -> None:
        rec = TaskRecord(id="task_cutoff", title="אתר לחנות", goal="תבנה לי אתר לחנות", status="running",
                         data={"language": "he", "plan": None})
        await env.memory.save_task(rec)
        orch = TaskOrchestrator(env.manager)
        core = JarvisCore(settings=env.kit.settings, manager=env.manager, orchestrator=orch, memory=env.memory,
                          bus=env.kit.bus, broker=env.kit.broker, kill_switch=env.kit.kill_switch)
        found = await core.start()
        try:
            assert [x.id for x in found] == ["task_cutoff"]
            [msg] = [r for r in await env.memory.chat_history(10) if r.role == "jarvis"]
            assert "'אתר לחנות'" in msg.text and "תמשיך את המשימה" in msg.text and msg.task_id == "task_cutoff"
        finally:
            await core.shutdown()


# ================================================================================== modes
class TestModes:
    async def test_chat_mode_refuses_actions(self, c: C) -> None:
        res = await c.say("עבור למצב שיחה")
        assert c.core.mode == Mode.CHAT and res.reply["text"].startswith("עברתי למצב שיחה")
        assert c.of("mode") == [{"mode": "chat"}]
        res = await c.say("פתח את כרום")
        assert "מצב שיחה" in res.reply["text"] and not any(x[0] == "launch" for x in c.host.calls)
        res = await c.say("תבנה לי אתר")
        assert res.task_id is None and not c.orch.active_tasks()
        assert (await c.say("מה השעה")).reply["text"].startswith("השעה")
        await c.say("switch to normal mode")
        assert c.core.mode == Mode.NORMAL

    async def test_task_mode_plans_every_request(self, c: C) -> None:
        await c.say("switch to task mode")
        res = await c.say("open notepad")
        assert res.task_id and res.reply["kind"] == "ack"
        task = await c.finished(res.task_id)
        sub = task.plan.subtasks[0]
        assert task.status == S.COMPLETED and task.mode == Mode.TASK and sub.skill == "open_app"
        assert ("launch", "Notepad", ()) in c.host.calls
        # task mode shows the plan even for one step, inside the ack (it was planned up front)
        assert "The plan (1 step): One step for the System Agent." in res.reply["text"]
        assert "1. System Agent:" in res.reply["text"] and not c.jarvis("plan")

    async def test_debug_mode_shows_the_routing(self, c: C) -> None:
        await c.say("מצב דיבאג")
        assert c.core.mode == Mode.DEBUG
        await c.say("open notepad")
        debug = [m for m in c.jarvis("info") if m["text"].startswith("[debug]")]
        assert debug and "intent=open_app" in debug[-1]["text"]

    async def test_same_mode(self, c: C) -> None:
        assert (await c.say("normal mode")).reply["text"] == "I'm already in normal mode."


# ================================================================================== instant answers
class TestInstant:
    async def test_time_date_help_greeting_thanks(self, c: C) -> None:
        assert (await c.say("מה השעה")).reply["text"].startswith("השעה ")
        assert (await c.say("what's the date")).reply["text"].startswith("Today is ")
        res = await c.say("מה אתה יכול לעשות", source="voice")
        assert "חמישה סוכנים" in res.reply["text"] and res.speech["text"].startswith("אני יכול לפתוח")
        assert "במה אפשר לעזור" in (await c.say("שלום")).reply["text"]
        assert (await c.say("thanks")).reply["text"] == "Anytime."

    async def test_wake_word_alone_is_a_greeting(self, c: C) -> None:
        res = await c.say("ג'ארוויס")
        assert res.intent["name"] == I.GREETING and "במה אפשר לעזור" in res.reply["text"]

    async def test_show_agents_and_tasks(self, c: C) -> None:
        res = await c.say("תראה לי את הסוכנים")
        assert res.reply["text"].startswith("כל חמשת הסוכנים פעילים") and res.reply["text"].count("\n") == 5
        assert {"screen": "agents"} in c.of("ui.navigate")
        assert (await c.say("show tasks")).reply["text"] == "There are no active tasks right now."

    async def test_history_and_the_last_task(self, c: C) -> None:
        res = await c.say("תכין לי תוכנית לחתונה")
        await c.finished(res.task_id)
        story = await c.say("תראה לי את המשימה האחרונה")
        assert story.reply["text"].startswith("המשימה האחרונה:") and "הושלמה" in story.reply["text"]
        assert story.task_id == res.task_id
        history = await c.say("show history")
        assert "תכין לי תוכנית לחתונה" in history.reply["text"]

    async def test_remember_recall_forget(self, c: C) -> None:
        assert (await c.say("תזכור שאני אוהב קפה שחור")).reply["text"] == "אזכור את זה."
        [fact] = await c.env.memory.facts()
        assert fact.text == "אני אוהב קפה שחור" and fact.category == "preference"
        res = await c.say("מה אתה זוכר על קפה")
        assert "אני אוהב קפה שחור" in res.reply["text"]
        assert (await c.say("תשכח את זה")).reply["text"] == "שכחתי את זה."
        assert await c.env.memory.facts() == []
        assert (await c.say("forget that I like jazz")).reply["text"].startswith("I didn't find")


# ================================================================================== conversation
class TestConversation:
    async def test_offline_conversation_explains_itself(self, c: C) -> None:
        res = await c.say("ספר לי בדיחה")
        assert res.intent["name"] == I.CHAT and "מפתח API" in res.reply["text"]

    async def test_conversation_with_the_model_keeps_the_context(self, c: C) -> None:
        provider = c.env.use_ai(default=chat_turn("כמובן. למה המחשב הלך לרופא? כי היה לו וירוס."))
        res = await c.say("ספר לי בדיחה")
        assert res.reply["text"].startswith("כמובן") and res.task_id is None
        provider.default = chat_turn("כי וירוס זה מחלה של מחשבים.")
        await c.say("למה זה מצחיק?")
        last = provider.requests[-1]
        assert [m.role for m in last.messages] == ["user", "assistant", "user"]
        assert last.messages[0].text == "ספר לי בדיחה" and last.messages[-1].text == "למה זה מצחיק?"
        assert "Hebrew" in last.system and "run_task" in last.system

    async def test_the_model_hands_work_to_the_agents(self, c: C) -> None:
        c.env.use_ai(chat_turn("אני על זה.", run_task=True, task="השווה מחירים של מחשבים ניידים"),
                     default=lambda r: (_ for _ in ()).throw(ProviderError("offline in this test")))
        res = await c.say("אני צריך מחשב נייד חדש, תעזור לי להחליט")
        assert res.reply["text"] == "אני על זה." and res.task_id
        task = await c.orch.get(res.task_id)
        assert task.goal == "השווה מחירים של מחשבים ניידים"
        await c.orch.cancel(res.task_id)

    async def test_chat_mode_talks_without_acting(self, c: C) -> None:
        provider = c.env.use_ai(default=chat_turn("במצב רגיל הייתי מעביר את זה לסוכנים."))
        await c.say("chat mode")
        res = await c.say("תסדר לי את המחשב קצת")
        assert res.task_id is None and res.reply["text"].startswith("במצב רגיל")
        assert provider.requests[-1].json_schema is None and "chat mode" in provider.requests[-1].system

    async def test_a_model_failure_is_reported(self, c: C) -> None:
        c.env.use_ai(ProviderError("overloaded", retryable=True))
        res = await c.say("ספר לי בדיחה")
        assert res.reply["kind"] == "error" and "overloaded" in res.reply["text"]


# ================================================================================== chat log
class TestChatLog:
    async def test_messages_are_saved_and_can_be_cleared(self, c: C) -> None:
        await c.say("מה השעה")
        history = await c.core.chat_history()
        assert [m["role"] for m in history] == ["user", "jarvis"] and history[0]["text"] == "מה השעה"
        await c.core.clear_chat()
        assert await c.core.chat_history() == []
        assert len(await c.env.memory.command_history(5)) == 1       # the command history keeps everything

    async def test_empty_input(self, c: C) -> None:
        res = await c.say("   ")
        assert not res.accepted and res.reply is None and not c.of("chat.message")
