"""Stage 1/11: redaction, secrets, kill switch, path guard, sandboxed processes, logging."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

from jarvis.common.errors import KillSwitchEngaged, PermissionDenied
from jarvis.logs import ActivityLog, AuditLog, setup_logging
from jarvis.logs.audit import AuditEntry
from jarvis.security import (
    KillSwitch,
    PathGuard,
    ProcessRegistry,
    Redactor,
    SecretStore,
    run_process,
    sanitized_env,
)
from jarvis.security.path_guard import normalize_path


class TestRedaction:
    def test_registered_values_and_patterns(self) -> None:
        r = Redactor()
        r.register("OPENAI_API_KEY", "custom-secret-value-42")
        text = (
            "keys: sk-ant-api03-abcdefghijklmnopqrstuv and custom-secret-value-42; "
            "password=hunter2hunter; Authorization: Bearer abcdefghijklmnop1234; "
            "url https://bob:pa55word@example.com/x ghp_" + "a" * 36
        )
        out = r.redact_text(text)
        assert "abcdefghijklmnopqrstuv" not in out
        assert "custom-secret-value-42" not in out and "[REDACTED:OPENAI_API_KEY]" in out
        assert "hunter2hunter" not in out
        assert "pa55word" not in out
        assert "a" * 36 not in out

    def test_redact_nested_and_secret_keys(self) -> None:
        r = Redactor()
        data = {"api_key": "zzzzzzzz", "nested": [{"note": "sk-ant-api03-123456789012345678"}], "n": 3}
        out = r.redact(data)
        assert out["api_key"] == "[REDACTED]"
        assert "123456789012345678" not in out["nested"][0]["note"]
        assert out["n"] == 3

    def test_private_key_block(self) -> None:
        r = Redactor()
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----"
        assert "MIIBOgIBAAJBAK" not in r.redact_text(pem)

    def test_find_secrets(self) -> None:
        r = Redactor()
        r.register("TAVILY_API_KEY", "tvly-1234567890")
        assert r.find_secrets("payload tvly-1234567890") == ["TAVILY_API_KEY"]
        assert "anthropic_key" in r.find_secrets("x sk-ant-api03-zzzzzzzzzzzzzzzzzzzz")
        assert r.find_secrets("hello world") == []


class TestSecrets:
    def test_file_backend_roundtrip_and_status(self, tmp_path: Path) -> None:
        red = Redactor()
        store = SecretStore(tmp_path, redactor=red, use_keyring=False, environ={})
        assert store.status()["ANTHROPIC_API_KEY"]["configured"] is False
        assert store.set("ANTHROPIC_API_KEY", "sk-ant-api03-testkey1234567890") == "file"
        assert store.get("ANTHROPIC_API_KEY") == "sk-ant-api03-testkey1234567890"
        status = store.status()["ANTHROPIC_API_KEY"]
        assert status == {"label": status["label"], "configured": True, "source": "file"}
        assert "testkey" not in json.dumps(store.status())
        assert "testkey" not in red.redact_text("using sk-ant-api03-testkey1234567890")
        assert store.delete("ANTHROPIC_API_KEY") and store.get("ANTHROPIC_API_KEY") is None

    def test_env_source(self, tmp_path: Path) -> None:
        store = SecretStore(tmp_path, use_keyring=False, environ={"OPENAI_API_KEY": "sk-envvalue1234567890abcd"})
        assert store.source("OPENAI_API_KEY") == "env"
        assert store.get("OPENAI_API_KEY") == "sk-envvalue1234567890abcd"

    def test_invalid_name_rejected(self, tmp_path: Path) -> None:
        store = SecretStore(tmp_path, use_keyring=False, environ={})
        with pytest.raises(ValueError):
            store.set("bad name!", "x" * 10)

    def test_sanitized_env_strips_secrets(self) -> None:
        env = sanitized_env(environ={"PATH": "/bin", "ANTHROPIC_API_KEY": "x", "MY_TOKEN": "y", "HOME": "/h", "DB_PASSWORD": "z"})
        assert env["PATH"] == "/bin" and env["HOME"] == "/h"
        assert not {"ANTHROPIC_API_KEY", "MY_TOKEN", "DB_PASSWORD"} & set(env)


class TestKillSwitch:
    async def test_global_and_agent_scopes(self) -> None:
        ks = KillSwitch()
        events = []
        ks.add_listener(lambda e: events.append((e.engaged, e.agent_id)))
        ks.check("research")
        await ks.engage("test", agent_id="research")
        with pytest.raises(KillSwitchEngaged):
            ks.check("research")
        ks.check("system")
        await ks.engage("stop everything")
        with pytest.raises(KillSwitchEngaged):
            ks.check("system")
        await ks.release()
        await ks.release(agent_id="research")
        ks.check("research")
        assert events == [(True, "research"), (True, None), (False, None), (False, "research")]


class TestPathGuard:
    def make(self, tmp_path: Path) -> PathGuard:
        ws, pr, data = tmp_path / "ws", tmp_path / "projects", tmp_path / "data"
        for p in (ws, pr, data):
            p.mkdir()
        return PathGuard(
            workspace=ws,
            projects=pr,
            denied_roots=[tmp_path / "secret"],
            protected_roots=[data],
            read_roots=[tmp_path],
        )

    def test_write_inside_roots_only(self, tmp_path: Path) -> None:
        g = self.make(tmp_path)
        assert g.can_write(tmp_path / "ws" / "a" / "b.txt")
        assert g.can_write(tmp_path / "projects" / "app")
        assert not g.can_write(tmp_path / "elsewhere" / "x.txt")
        assert g.can_write(tmp_path / "elsewhere" / "x.txt", extra_roots=[tmp_path / "elsewhere"])
        with pytest.raises(PermissionDenied):
            g.check_write(tmp_path / "elsewhere" / "x.txt")

    def test_traversal_and_protected(self, tmp_path: Path) -> None:
        g = self.make(tmp_path)
        assert not g.can_write(tmp_path / "ws" / ".." / "data" / "config.toml")
        assert not g.can_read(tmp_path / "data" / "secrets.env")
        assert not g.can_read(tmp_path / "secret" / "id_rsa")
        assert g.zone(tmp_path / "ws" / "x") == "workspace"
        assert g.zone(tmp_path / "data") == "denied"

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
    def test_symlink_escape_blocked(self, tmp_path: Path) -> None:
        g = self.make(tmp_path)
        (tmp_path / "secret").mkdir()
        link = tmp_path / "ws" / "link"
        link.symlink_to(tmp_path / "secret", target_is_directory=True)
        assert not g.can_write(link / "loot.txt")

    def test_default_denied_includes_ssh(self) -> None:
        g = PathGuard(workspace=Path.home() / "JARVIS_TEST_WS", projects=Path.home() / "JARVIS_TEST_PR")
        assert g.is_denied(Path.home() / ".ssh" / "id_rsa")
        assert normalize_path("~") == Path.home().resolve()


class TestProcess:
    async def test_success_and_output(self) -> None:
        res = await run_process([sys.executable, "-c", "print('héllo'); import sys; print('err', file=sys.stderr)"], timeout=30)
        assert res.ok and "héllo" in res.stdout and "err" in res.stderr

    async def test_secrets_not_inherited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak-123456")
        res = await run_process([sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY'))"], timeout=30)
        assert res.stdout.strip() == "None"

    async def test_timeout_kills_tree(self) -> None:
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
        res = await run_process([sys.executable, "-c", code], timeout=1.5)
        assert res.timed_out and not res.ok

    async def test_cancel_event(self) -> None:
        cancel = asyncio.Event()
        reg = ProcessRegistry()
        task = asyncio.create_task(
            run_process([sys.executable, "-c", "import time; time.sleep(60)"], timeout=60, cancel_event=cancel, registry=reg, owner="dev")
        )
        await asyncio.sleep(0.5)
        assert reg.list() and reg.list()[0]["owner"] == "dev"
        cancel.set()
        res = await task
        assert res.cancelled and reg.list() == []

    async def test_registry_kill(self) -> None:
        reg = ProcessRegistry()
        task = asyncio.create_task(run_process([sys.executable, "-c", "import time; time.sleep(60)"], timeout=60, registry=reg, owner="system", task_id="t1"))
        await asyncio.sleep(0.5)
        assert reg.kill(task_id="t1") >= 1
        res = await asyncio.wait_for(task, 10)
        assert not res.ok

    async def test_missing_program(self) -> None:
        res = await run_process(["definitely-not-a-real-program-xyz"], timeout=5)
        assert res.exit_code == 127

    async def test_output_truncation(self) -> None:
        res = await run_process([sys.executable, "-c", "print('x'*100000)"], timeout=30, max_output=4000)
        assert res.truncated and len(res.stdout) < 6000


class TestLogs:
    async def test_activity_and_audit(self, tmp_path: Path) -> None:
        red = Redactor()
        red.register("OPENAI_API_KEY", "sk-test-abcdefghijklmnopqrstuvwxyz")
        act = ActivityLog(tmp_path, redactor=red)
        seen = []
        act.add_sink(seen.append)
        e = await act.log("Research Agent started with sk-test-abcdefghijklmnopqrstuvwxyz", source="Research Agent", task_id="t1")
        assert e.line().startswith("[") and "abcdefghij" not in e.line()
        assert (tmp_path / "activity.log").read_text(encoding="utf-8").count("Research Agent started") == 1
        assert seen and act.recent(task_id="t1")
        audit = AuditLog(tmp_path, redactor=red)
        await audit.record(AuditEntry(agent_id="system", tool="shell", params={"cmd": "echo sk-test-abcdefghijklmnopqrstuvwxyz"}))
        line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "abcdefghij" not in line and '"tool": "shell"' in line

    def test_logging_redacts(self, tmp_path: Path) -> None:
        from jarvis.security.redaction import default_redactor

        default_redactor().register("ANTHROPIC_API_KEY", "sk-ant-api03-loggingsecret999999")
        setup_logging(tmp_path, console=False)
        logging.getLogger("jarvis.test").warning("calling with %s", "sk-ant-api03-loggingsecret999999")
        for h in logging.getLogger().handlers:
            h.flush()
        content = (tmp_path / "jarvis.log").read_text(encoding="utf-8")
        assert "loggingsecret" not in content and "calling with" in content
