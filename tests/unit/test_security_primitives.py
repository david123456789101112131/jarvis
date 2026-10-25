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
from jarvis.security.redaction import looks_like_secret_name


class TestRedaction:
    def test_registered_values_and_patterns(self) -> None:
        r = Redactor()
        r.register("OPENAI_API_KEY", "custom-secret-value-42")
        text = (
            "keys: sk-ant-api03-abcdefghijklmnopqrstuv and custom-secret-value-42; "
            "password=hunter2hunter; Authorization: Bearer abcdefghijklmnop1234; "
            "url https://bob:pa55word@example.com/x ghp_" + "a" * 36
            + " grok key xai-" + "Q7" * 40
        )
        out = r.redact_text(text)
        assert "abcdefghijklmnopqrstuv" not in out
        assert "Q7Q7Q7" not in out and "xai-org" in r.redact_text("github.com/xai-org")
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

    def test_secret_names_are_whole_words(self) -> None:
        for name in ("ANTHROPIC_API_KEY", "x-api-key", "apiKey", "Authorization", "accessToken", "GITHUB_PAT",
                     "STRIPE_KEY", "AWS_SECRET_ACCESS_KEY", "db_pwd", "session_cookie"):
            assert looks_like_secret_name(name), name
        for name in ("key", "author", "GIT_AUTHOR_NAME", "KEYBOARD_LAYOUT", "PATHEXT", "max_tokens", "token_count",
                     "sort_key", "hotkey", "keyword", "public_key"):
            assert not looks_like_secret_name(name), name
        r = Redactor()
        plan = {"key": "build_site", "author": "David", "api_key": "abcdefgh", "max_tokens": "4096"}
        assert r.redact(plan) == {"key": "build_site", "author": "David", "api_key": "[REDACTED]", "max_tokens": "4096"}

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

    def test_jarvis_keyring_0_keeps_the_store_off_the_keyring(self, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
        class Vault:                                    # stands in for Windows Credential Manager
            saved: dict[str, str] = {}

            def get_password(self, service: str, name: str) -> str | None:
                return self.saved.get(name)

            def set_password(self, service: str, name: str, value: str) -> None:
                self.saved[name] = value

        monkeypatch.setattr(SecretStore, "_init_keyring", staticmethod(Vault))
        monkeypatch.delenv("JARVIS_KEYRING")
        assert SecretStore(tmp_path, environ={}).set("OPENAI_API_KEY", "sk-vault1234567890abcdef") == "keyring"
        monkeypatch.setenv("JARVIS_KEYRING", "0")       # what the tests and `jarvis check --full` run with
        store = SecretStore(tmp_path, environ={})
        assert store.backend_name == "file" and store.get("OPENAI_API_KEY") is None
        assert store.set("OPENAI_API_KEY", "sk-test1234567890abcdef") == "file"
        assert Vault.saved == {"OPENAI_API_KEY": "sk-vault1234567890abcdef"}

    def test_a_key_typed_in_hebrew_is_refused_and_invisible_marks_dropped(self, tmp_path: Path) -> None:
        from jarvis.security.secrets import SecretFormatError

        store = SecretStore(tmp_path, use_keyring=False, environ={})
        with pytest.raises(SecretFormatError):
            store.set("XAI_API_KEY", "שלום" + "xai-" + "k" * 30)      # the keyboard was in Hebrew first
        with pytest.raises(SecretFormatError):
            store.set("XAI_API_KEY", "xai-abc def")
        assert store.get("XAI_API_KEY") is None
        assert store.set("XAI_API_KEY", "\u200fxai-" + "k" * 30 + "\u200e \n") == "file"
        assert store.get("XAI_API_KEY") == "xai-" + "k" * 30

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


class TestHelpersEndWithJarvis:
    def test_the_job_limits_have_the_windows_layout(self) -> None:
        import ctypes

        from jarvis.security import process

        if ctypes.sizeof(ctypes.c_void_p) == 8:
            assert ctypes.sizeof(process._BasicLimits) == 64 and ctypes.sizeof(process._ExtendedLimits) == 144

    @pytest.mark.skipif(sys.platform == "win32", reason="the Windows behaviour is tested below")
    def test_elsewhere_nothing_is_done(self) -> None:
        from jarvis.security.process import end_with_jarvis

        assert end_with_jarvis(1) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows only")
    async def test_a_helper_ends_when_jarvis_crashes(self) -> None:
        import psutil

        # a stand-in for JARVIS: starts a helper, ties it, then ends at once with no clean-up at all
        code = ("import os, subprocess, sys\n"
                "from jarvis.security.process import end_with_jarvis\n"
                "helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                "print(helper.pid, end_with_jarvis(helper.pid), flush=True)\n"
                "os._exit(3)\n")
        res = await run_process([sys.executable, "-c", code], timeout=60)
        pid, tied = res.stdout.split()
        assert res.exit_code == 3 and tied == "True"
        for _ in range(100):
            if not psutil.pid_exists(int(pid)):
                break
            await asyncio.sleep(0.1)
        assert not psutil.pid_exists(int(pid)), "the helper outlived its parent"


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

    def test_the_log_keeps_going_when_it_cannot_start_afresh(self, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.logs.setup import JsonFormatter, RotatingLog

        handler = RotatingLog(tmp_path / "jarvis.log", maxBytes=300, backupCount=2, encoding="utf-8")
        handler.setFormatter(JsonFormatter())

        def in_use(source: str, dest: str) -> None:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")

        monkeypatch.setattr(handler, "rotate", in_use)          # Windows: another program has the file open
        logger = logging.getLogger("jarvis.test.rotation")
        logger.addHandler(handler)
        logger.propagate = False
        try:
            for i in range(20):
                logger.warning("line %d %s", i, "x" * 40)
        finally:
            logger.removeHandler(handler)
            handler.close()
        text = (tmp_path / "jarvis.log").read_text(encoding="utf-8")
        assert "line 0 " in text and "line 19 " in text and text.count("not started afresh") == 1

    def test_a_crash_leaves_every_threads_stack(self, tmp_path: Path) -> None:
        import subprocess

        code = ("import ctypes, sys\n"
                "from pathlib import Path\n"
                "from jarvis.logs.setup import keep_crash_traces\n"
                "keep_crash_traces(Path(sys.argv[1]))\n"
                "ctypes.string_at(0)\n")                     # an access violation inside native code
        done = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, timeout=60)
        assert done.returncode != 0
        text = (tmp_path / "crash.log").read_text(encoding="utf-8")
        assert text.startswith("--- JARVIS started ") and "Fatal Python error" in text and "<string>" in text

    def test_logging_redacts(self, tmp_path: Path) -> None:
        from jarvis.security.redaction import default_redactor

        default_redactor().register("ANTHROPIC_API_KEY", "sk-ant-api03-loggingsecret999999")
        setup_logging(tmp_path, console=False)
        logging.getLogger("jarvis.test").warning("calling with %s", "sk-ant-api03-loggingsecret999999")
        for h in logging.getLogger().handlers:
            h.flush()
        content = (tmp_path / "jarvis.log").read_text(encoding="utf-8")
        assert "loggingsecret" not in content and "calling with" in content
