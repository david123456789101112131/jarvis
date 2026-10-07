"""Stage 12, end to end: JARVIS as the user runs it.

`python -m jarvis --no-window` runs in its own process with its real host, agents, tools, memory and
server, and these tests drive it over HTTP and the WebSocket exactly as the window does. Nothing is
faked: the commands create and delete real folders, build and test a real project, wait for real
approvals and survive a real restart and a crash. Everything stays inside a temporary JARVIS_HOME.

The one step that would reach outside it, opening the finished site in the browser, belongs to the
System Agent; the build test stops that agent first, which is also how it checks that a stopped agent's
optional step does not sink the task.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from e2e_support import AGENTS, BUSY, exists, read_json, until

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(300)]


# =========================================================================== commands and live updates
async def test_commands_act_on_the_computer_and_the_window_follows_live(launch) -> None:
    j = await launch()
    state = j.of("state")[0]
    assert {a["id"] for a in state["agents"]} == AGENTS and state["mode"] == "normal"
    assert not state["kill_switch"]["global"]

    res = await j.say("צור תיקייה בשם דוחות")
    assert res["intent"]["name"] == "create_folder" and "דוחות" in res["reply"]["text"]
    assert await asyncio.to_thread(exists, j.workspace / "דוחות")
    # What the window shows: the action card, the System Agent working and then done, the answer in the chat
    await j.wait_for("action", lambda d: d["status"] == "done" and "דוחות" in d["title"])
    system = [d["status"] for d in j.of("agent.status") if d["id"] == "system"]
    assert "working" in system and "completed" in system[system.index("working"):]
    await j.said(lambda text: "דוחות" in text)

    res = await j.say("בדוק את המחשב")
    assert res["intent"]["name"] == "system_status"
    assert "%" in res["reply"]["text"] and "GB" in res["reply"]["text"]       # real numbers from this computer
    stats = await j.wait_for("system.stats")
    assert stats["cpu"]["cores"] >= 1 and stats["memory"]["total_gb"] > 0

    res = await j.say("תראה לי את הסוכנים")
    assert all(name in res["reply"]["text"] for name in ("סוכן מחקר", "סוכן מערכת", "סוכן פיתוח", "סוכן יצירתי",
                                                         "סוכן תכנון"))


# =========================================================================== a real project, five agents
async def test_the_agents_build_and_verify_a_project_together(launch) -> None:
    j = await launch()
    res = await j.say("עצור את סוכן המערכת")
    assert res["intent"]["name"] == "agent_stop"
    await j.wait_for("agent.status", lambda d: d["id"] == "system" and d["status"] == "offline")

    mark = len(j.events)
    res = await j.say("תבנה לי אתר לחנות פרחים")
    assert res["intent"]["name"] == "build_app" and res["task_id"]
    task = await j.finished(res["task_id"])
    assert task["status"] == "completed", json.dumps(task, ensure_ascii=False)
    steps = task["plan"]["subtasks"]
    assert [s["agent"] for s in steps] == ["planner", "research", "creative", "development", "development", "system"]
    plan, research, design, build, verify, launch_step = steps
    assert plan["status"] == design["status"] == build["status"] == verify["status"] == "completed"
    assert research["status"] in ("completed", "failed")         # optional: the web may be out of reach
    assert launch_step["status"] == "skipped" and launch_step["optional"]   # the System Agent is stopped

    projects = await j.get("/api/memory/projects")
    folder = Path(next(p["path"] for p in projects if p["path"]))
    assert await asyncio.to_thread(exists, folder / "index.html"), folder
    assert "נבדקו" in (verify["summary"] or "")

    # Planning and research run side by side: the window saw at least two agents busy at once.
    busy: set[str] = set()
    most = 0
    for e in j.events[mark:]:
        if e["type"] == "agent.status":
            (busy.add if e["data"]["status"] in BUSY else busy.discard)(e["data"]["id"])
            most = max(most, len(busy))
    assert most >= 2
    # The task card filled up live, from the first step to the end.
    updates = [d for d in j.of("task.updated") if d["id"] == task["id"]]
    assert "running" in [u["status"] for u in updates] and updates[-1]["status"] == "completed"
    assert updates[-1]["progress"] == 1.0
    assert {d["subtask"]["agent"] for d in j.of("task.subtask") if d["task_id"] == task["id"]} >= {"planner",
                                                                                              "development"}

    res = await j.say("הפעל את סוכן המערכת")
    assert res["intent"]["name"] == "agent_activate"
    await j.wait_for("agent.status", lambda d: d["id"] == "system" and d["status"] == "idle", after=mark)


async def test_a_failing_project_is_reported_and_checked_again_after_the_fix(launch) -> None:
    j = await launch()
    project = j.home / "projects" / "מחשבון"
    await asyncio.to_thread(_write_calculator, project, broken=True)

    mark = len(j.events)
    res = await j.say("הרץ את הבדיקות של הפרויקט מחשבון")
    assert res["intent"]["name"] == "run_tests"
    # The project's own code (JARVIS did not write it): running its tests asks first.
    req = await j.approval("run_tests", after=mark)
    assert req["risk"] == "high" and "מחשבון" in req["summary"]
    await j.answer(req["id"], True)
    failed = await j.said(lambda text: "נכשלו" in text, after=mark)
    assert "מחשבון" in failed

    await asyncio.to_thread(_write_calculator, project, broken=False)
    mark = len(j.events)
    await j.say("תריץ שוב את הבדיקות של מחשבון")
    await j.answer((await j.approval("run_tests", after=mark))["id"], True)
    assert "עברה" in await j.said(lambda text: "מחשבון" in text and "עבר" in text, after=mark)


def _write_calculator(project: Path, *, broken: bool) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "requirements.txt").write_text("", encoding="utf-8")
    # The fix changes the file's size too: Python trusts cached bytecode when size and mtime (in seconds) match.
    body = "return a - b" if broken else "return a + b  # fixed"
    (project / "calc.py").write_text(f"def add(a, b):\n    {body}\n", encoding="utf-8")
    (project / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                                          encoding="utf-8")


# =========================================================================== approvals, cancelling, kill switch
async def test_nothing_irreversible_happens_without_an_approval(launch) -> None:
    j = await launch()
    old = j.workspace / "ישן"
    await asyncio.to_thread(old.mkdir, parents=True)

    mark = len(j.events)
    res = await j.say("תמחק לצמיתות את התיקייה ישן")
    assert res["intent"]["name"] == "delete_path"
    req = await j.approval("delete_path", after=mark)
    assert req["irreversible"] and req["risk"] in ("high", "critical")
    await j.answer(req["id"], False)
    await j.said(lambda text: "ישן" in text and text != res["reply"]["text"], after=mark)
    assert await asyncio.to_thread(exists, old)

    # Approved from the window itself: the approval dialog answers over the WebSocket.
    mark = len(j.events)
    await j.say("תמחק לצמיתות את התיקייה ישן")
    req = await j.approval("delete_path", after=mark)
    await j.ws.send(json.dumps({"type": "confirm", "id": req["id"], "approved": True}))
    await until(lambda: not old.exists())
    assert [a for a in await j.get("/api/audit") if a.get("tool") == "delete_path"]


async def test_stopping_a_task_and_the_kill_switch(launch) -> None:
    j = await launch()
    keep = j.workspace / "לשמור"
    await asyncio.to_thread(keep.mkdir, parents=True)
    await j.post("/api/mode", {"mode": "task"})          # in task mode even a one-step command is a task

    # Stop a task while it waits for an approval: the request goes away and nothing is deleted.
    mark = len(j.events)
    res = await j.say("תמחק לצמיתות את התיקייה לשמור")
    task_id = res["task_id"]
    await j.approval("delete_path", after=mark)
    res = await j.say("עצור את המשימה")
    assert res["intent"]["name"] == "stop_task"
    assert (await j.finished(task_id))["status"] == "cancelled"
    assert await j.get("/api/confirmations") == [] and await asyncio.to_thread(exists, keep)

    # The kill switch interrupts a waiting task, and while it is engaged nothing runs.
    mark = len(j.events)
    res = await j.say("תמחק לצמיתות את התיקייה לשמור")
    task_id = res["task_id"]
    await j.approval("delete_path", after=mark)
    snapshot = await j.post("/api/killswitch", {"engage": True, "reason": "e2e"})
    assert snapshot["global"]
    assert (await j.finished(task_id))["status"] == "interrupted"
    assert await j.get("/api/confirmations") == []
    await j.wait_for("killswitch", lambda d: d["global"], after=mark)
    refused = await j.say("צור תיקייה בשם חדשה")
    assert "מתג החירום פעיל" in refused["reply"]["text"]
    assert not await asyncio.to_thread(exists, j.workspace / "חדשה")

    # Released, the interrupted task continues from where it stopped and asks again.
    await j.post("/api/killswitch", {"engage": False})
    mark = len(j.events)
    res = await j.say("תמשיך את המשימה")
    assert res["intent"]["name"] == "resume_task"
    await j.answer((await j.approval("delete_path", after=mark))["id"], True)
    assert (await j.finished(task_id))["status"] == "completed"
    assert not await asyncio.to_thread(exists, keep)


# =========================================================================== restarts
async def test_memory_and_unfinished_work_survive_a_crash_and_a_restart(launch) -> None:
    j = await launch()
    res = await j.say("תזכור שהצבע האהוב עליי הוא כחול")
    assert res["intent"]["name"] == "remember"
    archive = j.workspace / "ארכיון"
    await asyncio.to_thread(archive.mkdir, parents=True)
    await j.post("/api/mode", {"mode": "task"})
    mark = len(j.events)
    res = await j.say("תמחק לצמיתות את התיקייה ארכיון")
    task_id = res["task_id"]
    await j.approval("delete_path", after=mark)
    await j.crash()                                       # gone mid-task, waiting for the user

    j = await launch()                                    # a stale runtime file does not stop a new start
    task = await j.get(f"/api/tasks/{task_id}")
    assert task["status"] == "interrupted" and await asyncio.to_thread(exists, archive)
    chat = await j.get("/api/chat")
    assert any("נקטעו" in m["text"] for m in chat if m["role"] == "jarvis")
    res = await j.say("מה אתה זוכר על צבעים")
    assert "כחול" in res["reply"]["text"]

    mark = len(j.events)
    res = await j.say("תמשיך את המשימה")
    await j.answer((await j.approval("delete_path", after=mark))["id"], True)
    assert (await j.finished(task_id))["status"] == "completed"
    assert not await asyncio.to_thread(exists, archive)

    assert await j.stop() == 0                            # a clean quit removes the runtime file
    assert read_json(j.home / "data" / "runtime.json") is None
    j = await launch()
    assert "כחול" in (await j.say("מה אתה זוכר על הצבע"))["reply"]["text"]
