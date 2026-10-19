"""Stage 6: local memory layers (short-term, long-term, project, task), search and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.memory import ChatRecord, ResultRecord, TaskRecord, create_memory
from jarvis.security.redaction import Redactor


def _db_bytes(folder: Path) -> bytes:
    return b"".join(p.read_bytes() for p in folder.glob("jarvis.db*"))


@pytest.fixture
async def memory(tmp_path: Path):
    redactor = Redactor()
    mem = create_memory(tmp_path / "jarvis.db", redactor=redactor)
    await mem.start()
    yield mem
    await mem.close()


class TestLongTerm:
    async def test_remember_dedupe_forget(self, memory) -> None:
        a = await memory.remember("המשתמש מעדיף מצב כהה", "preference")
        b = await memory.remember("המשתמש  מעדיף מצב כהה", "preference", importance=3)
        assert a.id == b.id and b.importance == 3
        await memory.remember("Dana is my designer", "person")
        assert [f.text for f in await memory.preferences()] == ["המשתמש מעדיף מצב כהה"]
        assert await memory.forget_matching("dana") == 1
        assert len(await memory.facts()) == 1
        assert await memory.forget(a.id)
        assert await memory.facts() == []

    async def test_secrets_never_reach_the_database(self, memory, tmp_path: Path) -> None:
        key = "sk-ant-api03-" + "Q" * 40
        memory.redactor.register("ANTHROPIC_API_KEY", key)
        await memory.remember(f"my key is {key}")
        await memory.add_chat(ChatRecord(role="user", text=f"use {key}"))
        await memory.save_task(TaskRecord(id="task_1", goal=f"call api with {key}", data={"k": key}))
        await memory.close()
        assert key.encode() not in _db_bytes(tmp_path)


class TestSearchAndRecall:
    async def test_hebrew_substring_search(self, memory) -> None:
        await memory.remember("הפרויקט של החנות בנוי ב-React")
        await memory.remember("I like jazz in the evening", "preference")
        hits = await memory.search("פרויקט")
        assert hits and "החנות" in hits[0].text
        assert (await memory.search("jazz"))[0].layer == "long_term"
        assert await memory.search("   ") == []

    async def test_hebrew_plurals_and_the_article_find_the_base_word(self, memory) -> None:
        await memory.remember("הצבע האהוב עליי הוא כחול")
        await memory.remember("תוכנית האימונים מתחילה ביום ראשון")
        assert "כחול" in (await memory.search("צבעים"))[0].text
        assert "ראשון" in (await memory.search("התוכניות"))[0].text
        assert not await memory.search("חנויות")

    async def test_recall_formats_relevant_context(self, memory) -> None:
        await memory.remember("Always answer in Hebrew", "preference")
        await memory.record_decision("The store uses Stripe for payments")
        await memory.upsert_project("Online Store", path="C:/Projects/store", description="React shop")
        await memory.save_task(TaskRecord(id="task_9", title="Build the store", goal="Build an online store",
                                          status="completed", summary="Store built with React and Stripe"))
        recall = await memory.recall("store payments")
        text = recall.format()
        assert "Always answer in Hebrew" in text
        assert "Stripe" in text and "Online Store" in text


class TestProjectsAndTasks:
    async def test_projects(self, memory) -> None:
        p1 = await memory.upsert_project("Shop", path="C:/P/shop", description="first")
        p2 = await memory.upsert_project("Shop", path="C:/P/shop", status="paused")
        assert p1.id == p2.id and p2.status == "paused" and p2.description == "first"
        other = await memory.upsert_project("Shop", path="D:/other/shop")
        assert other.id != p1.id
        await memory.record_decision("Use Vite", project_id=p1.id)
        await memory.record_file(Path("C:/P/shop/index.html"), project_id=p1.id, agent_id="development")
        ctx = await memory.project_context(p1.id)
        assert ctx["decisions"] == ["Use Vite"] and ctx["files"][0].endswith("index.html")
        assert (await memory.project("Shop")) is not None

    async def test_task_checkpoint_and_resume_state(self, memory, tmp_path: Path) -> None:
        task = TaskRecord(id="task_42", title="Website", goal="Build a website", status="running",
                          data={"plan": {"subtasks": [{"id": "s1", "status": "completed"}, {"id": "s2",
                                                                                            "status": "running"}]}})
        await memory.save_task(task)
        await memory.set_task_context("task_42", "research", {"summary": "3 competitors"})
        await memory.add_result(ResultRecord(task_id="task_42", subtask_id="s1", agent_id="research",
                                             summary="Found 3 competitors"))
        await memory.close()

        again = create_memory(tmp_path / "jarvis.db")
        await again.start()
        try:
            interrupted = await again.interrupted_tasks()
            assert [t.id for t in interrupted] == ["task_42"]
            loaded = await again.get_task("task_42")
            assert loaded.data["plan"]["subtasks"][1]["status"] == "running"
            assert (await again.task_context("task_42"))["research"]["summary"] == "3 competitors"
            assert (await again.results("task_42"))[0].agent_id == "research"
        finally:
            await again.close()


class TestConversationAndHistory:
    async def test_chat_short_term_and_visibility(self, memory, tmp_path: Path) -> None:
        for i in range(3):
            await memory.add_chat(ChatRecord(role="user", text=f"שלום {i}"))
        assert [r.text for r in memory.short.recent(2)] == ["שלום 1", "שלום 2"]
        memory.short.note("last_app", "chrome")
        assert memory.short.get("last_app") == "chrome"
        memory.clear_visible_chat()
        assert await memory.chat_history() == []
        assert len(await memory.search("שלום")) == 3  # still remembered
        await memory.close()
        again = create_memory(tmp_path / "jarvis.db")
        await again.start()
        assert len(again.short.recent()) == 3  # restored on start
        await again.close()

    async def test_command_history(self, memory) -> None:
        cmd = await memory.add_command("פתח את כרום", source="voice", intent="open_app")
        await memory.update_command(cmd.id, ok=True, task_id="task_1")
        history = await memory.command_history()
        assert history[0].ok is True and history[0].task_id == "task_1" and history[0].source == "voice"
        stats = await memory.stats()
        assert stats["commands"] == 1


class TestClear:
    async def _fill(self, memory) -> None:
        await memory.add_chat(ChatRecord(role="user", text="סוד-השיחה zebra-chat"))
        await memory.add_command("פתח את zebra-command", source="voice")
        await memory.remember("הצבע האהוב עליי zebra-fact", "preference")
        await memory.upsert_project("zebra-project", path="/tmp/zebra", description="פרויקט")
        await memory.record_file("/tmp/zebra-file.txt", task_id="task_done")
        await memory.save_task(TaskRecord(id="task_done", title="zebra-finished", status="completed"))
        await memory.add_result(ResultRecord(task_id="task_done", summary="zebra-result"))
        await memory.set_task_context("task_done", "notes", {"text": "zebra-context"})
        for tid, status in (("task_live", "running"), ("task_paused", "interrupted")):
            await memory.save_task(TaskRecord(id=tid, title=f"zebra-{status}", status=status))
        memory.short.note("last_app", "zebra-app")

    async def test_each_kind_goes_for_good_and_work_in_progress_stays(self, memory, tmp_path: Path) -> None:
        await self._fill(memory)
        stats = await memory.stats()
        assert stats["finished_tasks"] == 1 and stats["tasks"] == 3
        out = await memory.clear(["chat", "commands", "facts", "projects", "files", "tasks"])
        assert out == {"chat": 1, "commands": 1, "facts": 1, "projects": 1, "files": 1, "tasks": 1}
        stats = await memory.stats()
        assert {k: stats[k] for k in ("chat", "commands", "facts", "projects", "files", "results")} == dict.fromkeys(
            ("chat", "commands", "facts", "projects", "files", "results"), 0)
        assert sorted(t.id for t in await memory.tasks()) == ["task_live", "task_paused"]
        assert await memory.task_context("task_done") == {}
        assert memory.short.recent() == [] and memory.short.get("last_app") is None
        assert [h.id for h in await memory.search("zebra")] in (["task_live", "task_paused"], ["task_paused", "task_live"])
        # nothing deleted lingers in the database file or its write-ahead log
        raw = _db_bytes(tmp_path)
        for gone in ("zebra-chat", "zebra-command", "zebra-fact", "zebra-project", "zebra-file", "zebra-finished",
                     "zebra-result", "zebra-context"):
            assert gone.encode() not in raw, gone
        assert b"zebra-running" in raw

    async def test_one_kind_at_a_time_and_unknown_kinds_are_refused(self, memory) -> None:
        await self._fill(memory)
        assert await memory.clear(["facts"]) == {"facts": 1}
        stats = await memory.stats()
        assert stats["facts"] == 0 and stats["chat"] == 1 and stats["tasks"] == 3
        assert len(memory.short.recent()) == 1                 # the conversation stays
        with pytest.raises(ValueError, match="cannot clear"):
            await memory.clear(["chat", "everything"])
        assert (await memory.stats())["chat"] == 1             # refused before anything went
        assert await memory.clear([]) == {}
