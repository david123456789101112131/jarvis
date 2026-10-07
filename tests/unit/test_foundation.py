"""Stage 1: configuration, paths, enums, text helpers and the communication bus."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.bus import Message, MessageBus, event, topic_matches
from jarvis.common.enums import AgentId, MessageType, Mode, Priority, RiskLevel, TaskStatus
from jarvis.common.i18n import in_seconds, plural, steps_already_done, steps_done
from jarvis.common.ids import new_id
from jarvis.common.text import detect_language, normalize, slugify, strip_wake_word
from jarvis.config import load_settings, resolve_paths, save_settings, update_settings
from jarvis.config.loader import env_overrides


class TestConfig:
    def test_defaults_without_file(self, jarvis_home: Path) -> None:
        s = load_settings()
        assert s.general.default_mode is Mode.NORMAL
        assert s.server.host == "127.0.0.1"
        assert s.ai.anthropic.model == "claude-opus-5-5"
        assert s.ai.role("planner").effort == "high"

    def test_file_and_env_precedence(self, jarvis_home: Path, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[server]\nport = 9000\n[voice]\nstt_model = "base"\n', encoding="utf-8")
        s = load_settings(cfg, environ={"JARVIS__SERVER__PORT": "9100", "JARVIS__VOICE__ENABLED": "false"})
        assert s.server.port == 9100  # env beats file
        assert s.voice.stt_model == "base"  # file beats default
        assert s.voice.enabled is False

    def test_env_nested_keys(self) -> None:
        out = env_overrides({"JARVIS__AI__ROLES__CHAT__EFFORT": "high", "OTHER": "x"})
        assert out == {"ai": {"roles": {"chat": {"effort": "high"}}}}

    def test_save_roundtrip(self, jarvis_home: Path, tmp_path: Path) -> None:
        cfg = tmp_path / "c.toml"
        s = update_settings(load_settings(cfg), {"ui": {"theme_color": "purple"}, "general": {"user_name": "דוד"}})
        save_settings(s, cfg)
        again = load_settings(cfg)
        assert again.ui.theme_color == "purple"
        assert again.general.user_name == "דוד"

    def test_invalid_value_raises(self, tmp_path: Path) -> None:
        from jarvis.common.errors import ConfigError

        with pytest.raises(ConfigError):
            load_settings(tmp_path / "missing.toml", overrides={"ui": {"theme_color": "pink"}})

    def test_paths_portable(self, jarvis_home: Path) -> None:
        paths = resolve_paths(load_settings().paths).ensure()
        assert paths.home == jarvis_home.resolve()
        assert paths.workspace.is_dir() and paths.projects.is_dir() and paths.logs.is_dir()
        assert paths.db.parent == paths.data


class TestEnumsAndText:
    def test_priority_order_and_parse(self) -> None:
        assert sorted([Priority.LOW, Priority.CRITICAL, Priority.NORMAL]) == [Priority.CRITICAL, Priority.NORMAL, Priority.LOW]
        assert Priority.parse("urgent") is Priority.CRITICAL
        assert Priority.parse("medium") is Priority.NORMAL
        assert Priority.parse(None) is Priority.NORMAL

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Research Agent", AgentId.RESEARCH),
            ("agent 3", AgentId.DEVELOPMENT),
            ("3", AgentId.DEVELOPMENT),
            ("סוכן מערכת", AgentId.SYSTEM),
            ("planner", AgentId.PLANNER),
            ("Creative-Agent", AgentId.CREATIVE),
        ],
    )
    def test_agent_parse(self, text: str, expected: AgentId) -> None:
        assert AgentId.parse(text) is expected

    def test_risk_bump_caps(self) -> None:
        assert RiskLevel.HIGH.bump(5) is RiskLevel.CRITICAL

    def test_terminal_statuses(self) -> None:
        assert TaskStatus.COMPLETED.is_terminal and not TaskStatus.RUNNING.is_terminal

    def test_language_detection(self) -> None:
        assert detect_language("פתח לי את כרום") == "he"
        assert detect_language("open chrome") == "en"
        assert detect_language("JARVIS, תפתח את Chrome בבקשה") == "he"

    @pytest.mark.parametrize(
        ("text", "rest"),
        [
            ("JARVIS, open Chrome", "open Chrome"),
            ("Hey Jarvis open chrome", "open chrome"),
            ("ג'ארוויס, פתח את כרום", "פתח את כרום"),
            ("ג׳רוויס תבנה לי אפליקציה", "תבנה לי אפליקציה"),
            ("open chrome", "open chrome"),
        ],
    )
    def test_strip_wake_word(self, text: str, rest: str) -> None:
        assert strip_wake_word(text)[0] == rest

    def test_normalize_unifies_geresh(self) -> None:
        assert normalize("ג׳ארוויס  ") == "ג'ארוויס"

    def test_slugify(self) -> None:
        assert slugify("My Store: Website!") == "my-store-website"
        assert slugify("חנות פרחים") == "חנות-פרחים"
        assert slugify("CON") == "item"

    def test_ids_unique_sortable(self) -> None:
        ids = [new_id("t") for _ in range(200)]
        assert len(set(ids)) == 200 and all(i.startswith("t_") for i in ids)

    def test_counts_read_naturally_for_one(self) -> None:
        assert plural("he", 1, "קובץ אחד", "קבצים", "file", "files") == "קובץ אחד"
        assert plural("he", 3, "קובץ אחד", "קבצים", "file", "files") == "3 קבצים"
        assert plural("en", 1, "קובץ אחד", "קבצים", "file", "files") == "1 file"
        assert [steps_done("he", d, n) for d, n in ((1, 1), (0, 1), (0, 4), (1, 4), (2, 4))] == [
            "השלב הושלם", "השלב לא הושלם", "אף אחד מ-4 השלבים לא הושלם", "הושלם שלב אחד מתוך 4", "הושלמו 2 מתוך 4 שלבים"]
        assert steps_done("en", 2, 4) == "2 of 4 steps were completed"
        assert steps_already_done("he", 1, 3) == ": שלב אחד מתוך 3 כבר הושלם." and steps_already_done("en", 0, 3) == "."
        assert [in_seconds("he", s) for s in (0.2, 1, 5)] == ["מיד", "בעוד שנייה", "בעוד 5 שניות"]


class TestBus:
    def test_topic_matching(self) -> None:
        assert topic_matches("*", "x.y")
        assert topic_matches("task.*", "task.result")
        assert topic_matches("task.*", "task")
        assert not topic_matches("task.*", "tasks.result")
        assert topic_matches("agent.status", "agent.status")

    def test_message_has_required_fields(self) -> None:
        m = Message(task_id="t1", agent_id="research", type=MessageType.TASK_ASSIGN, instruction="find x")
        d = m.to_public()
        for key in ("task_id", "agent_id", "type", "priority", "instruction", "context", "permissions", "timestamp", "status", "result"):
            assert key in d
        assert m.topic == "task.assign"

    async def test_mailbox_priority_order(self) -> None:
        bus = MessageBus()
        box = bus.register_mailbox("dev")
        for prio, name in [(Priority.LOW, "low"), (Priority.CRITICAL, "crit"), (Priority.NORMAL, "n1"), (Priority.NORMAL, "n2")]:
            await bus.publish(Message(type=MessageType.TASK_ASSIGN, recipient="dev", priority=prio, instruction=name))
        got = [(await box.get()).instruction for _ in range(4)]
        assert got == ["crit", "n1", "n2", "low"]

    async def test_pubsub_and_handler_isolation(self) -> None:
        bus = MessageBus()
        seen: list[str] = []

        def bad(_: Message) -> None:
            raise RuntimeError("boom")

        async def good(m: Message) -> None:
            seen.append(m.topic)

        bus.subscribe("*", bad)
        bus.subscribe("agent.*", good)
        await bus.publish(event("agent.status", {"x": 1}))
        await bus.publish(event("system.metrics", {}))
        assert seen == ["agent.status"]

    async def test_request_reply(self) -> None:
        bus = MessageBus()

        async def responder(m: Message) -> None:
            await bus.publish(m.reply(type=MessageType.TASK_RESULT, result={"ok": True}, sender="research"))

        bus.subscribe("task.assign", responder)
        resp = await bus.request(Message(type=MessageType.TASK_ASSIGN, recipient="research"), timeout=2)
        assert resp.result == {"ok": True}

    async def test_mailbox_remove_and_close(self) -> None:
        bus = MessageBus()
        box = bus.register_mailbox("sys")
        await bus.publish(Message(type=MessageType.TASK_ASSIGN, recipient="sys", task_id="a"))
        await bus.publish(Message(type=MessageType.TASK_ASSIGN, recipient="sys", task_id="b"))
        removed = await box.remove(lambda m: m.task_id == "a")
        assert [m.task_id for m in removed] == ["a"]
        assert (await box.get()).task_id == "b"
        getter = asyncio.create_task(box.get())
        await asyncio.sleep(0.01)
        await box.close()
        with pytest.raises(asyncio.CancelledError):
            await getter

    async def test_sanitizer_applied(self) -> None:
        from jarvis.security.redaction import Redactor

        red = Redactor()
        red.register("ANTHROPIC_API_KEY", "sk-ant-secretvalue123456789")
        bus = MessageBus(sanitizer=lambda m: m.model_copy(update={"data": red.redact(m.data)}))
        got: list[Message] = []
        bus.subscribe("*", got.append)
        await bus.publish(event("log", {"text": "key is sk-ant-secretvalue123456789"}))
        assert "secretvalue" not in got[0].data["text"]

    async def test_history_filtering(self) -> None:
        bus = MessageBus(history_size=5)
        for i in range(8):
            await bus.publish(event("t.x", {"i": i}, task_id="k" if i % 2 else None))
        assert len(bus.history()) == 5
        assert all(m.task_id == "k" for m in bus.history(task_id="k"))


class TestConsole:
    def test_without_a_console_output_goes_to_a_sink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        from jarvis.common import console

        assert console.ensure_std_streams() is False and console.has_console()   # a console: nothing changes
        monkeypatch.setattr(console, "_no_console", False)
        monkeypatch.setattr(sys, "stdout", None)                                  # how pythonw.exe starts
        monkeypatch.setattr(sys, "stderr", None)
        try:
            assert console.ensure_std_streams() is True and not console.has_console()
            print("goes nowhere")
            assert not sys.stderr.isatty()                    # uvicorn's log formatter asks exactly this
        finally:
            sys.stdout.close()
            sys.stderr.close()

    def test_an_error_without_a_console_is_a_message_box(self, monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
        import ctypes

        from jarvis.common import console

        boxes: list[tuple[str, str]] = []
        user32 = type("user32", (), {"MessageBoxW": staticmethod(lambda _w, text, title, _f: boxes.append((title, text)))})
        monkeypatch.setattr(ctypes, "windll", type("windll", (), {"user32": user32}), raising=False)
        monkeypatch.setattr(console.sys, "platform", "win32")
        monkeypatch.setattr(console, "_no_console", True)
        console.show_error("JARVIS could not start.")
        assert boxes == [("JARVIS", "JARVIS could not start.")] and capsys.readouterr().err == ""
        monkeypatch.setattr(console, "_no_console", False)                      # with a console: stderr
        console.show_error("The configuration has an error.")
        assert boxes == [("JARVIS", "JARVIS could not start.")]
        assert "JARVIS: The configuration has an error." in capsys.readouterr().err
