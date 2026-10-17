"""The real entry point: `python -m jarvis --no-window` in its own process, driven by the other CLI commands."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def cli_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS__")}
    env.update(JARVIS_HOME=str(home), PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1", JARVIS__VOICE__ENABLED="false",
               JARVIS__UI__DESKTOP_NOTIFICATIONS="false")
    for key in ("ANTHROPIC_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY"):
        env.pop(key, None)
    return env


def jarvis(home: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "jarvis", *args], env=cli_env(home), capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handling; Windows is covered by the setup check")
def test_run_headless_then_talk_to_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.Popen([sys.executable, "-m", "jarvis", "--no-window", "--quiet", "--port", "0"],
                            env=cli_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8")
    try:
        line = ""
        deadline = time.monotonic() + 60
        while "JARVIS is running" not in line:
            assert time.monotonic() < deadline and proc.poll() is None, proc.stderr.read() if proc.stderr else ""
            line = proc.stdout.readline() if proc.stdout else ""
        assert "/?launch=" in line
        runtime = json.loads((home / "data" / "runtime.json").read_text(encoding="utf-8"))
        assert runtime["pid"] == proc.pid

        status = jarvis(home, "status")
        assert status.returncode == 0 and "Research Agent" in status.stdout and "Mode: normal" in status.stdout
        said = jarvis(home, "say", "מה", "השעה")
        assert said.returncode == 0 and said.stdout.startswith("השעה"), said.stderr
        again = jarvis(home)                                   # a second start finds the running one
        assert again.returncode == 0 and "already running" in again.stdout
        keys = jarvis(home, "keys")
        assert keys.returncode == 0 and "ANTHROPIC_API_KEY" in keys.stdout
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
    assert proc.returncode == 0
    assert not (home / "data" / "runtime.json").exists()
    assert jarvis(home, "status").returncode == 3                # not running any more


@pytest.mark.skipif(sys.platform == "win32", reason="closes the standard streams the POSIX way")
def test_runs_without_a_console_and_jarvis_stop_ends_it(tmp_path: Path) -> None:
    """pythonw.exe and the windowed JARVIS.exe start JARVIS with no stdout or stderr at all."""
    home = tmp_path / "home"
    home.mkdir()
    # `>&- 2>&-` starts Python with its standard streams closed, so sys.stdout and sys.stderr are None.
    proc = subprocess.Popen(["sh", "-c", 'exec "$0" -m jarvis --no-window --port 0 <&- >&- 2>&-', sys.executable],
                            env=cli_env(home))
    try:
        deadline = time.monotonic() + 60
        while jarvis(home, "status").returncode != 0:
            assert proc.poll() is None, f"JARVIS exited with code {proc.returncode} without a console"
            assert time.monotonic() < deadline, "JARVIS did not start without a console"
            time.sleep(0.5)
        stopped = jarvis(home, "stop")
        assert stopped.returncode == 0 and "JARVIS stopped." in stopped.stdout, stopped.stderr
        assert proc.wait(30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not (home / "data" / "runtime.json").exists()
    assert jarvis(home, "stop").returncode == 3                  # nothing left to stop


def test_keys_delete_all_removes_the_stored_keys(tmp_path: Path) -> None:
    from jarvis.security.secrets import SecretStore

    store = SecretStore(tmp_path / "data", environ={})      # JARVIS_KEYRING=0 here: the file store
    store.set("ANTHROPIC_API_KEY", "sk-ant-api03-cli1234567890abcdef")
    store.set("TAVILY_API_KEY", "tvly-cli1234567890abcdef")
    out = jarvis(tmp_path, "keys", "delete", "--all")
    assert out.returncode == 0 and out.stdout.strip() == "Removed: ANTHROPIC_API_KEY, TAVILY_API_KEY.", out.stderr
    assert not SecretStore(tmp_path / "data", environ={}).status()["ANTHROPIC_API_KEY"]["configured"]
    assert jarvis(tmp_path, "keys", "delete", "--all").stdout.strip() == "No stored keys."


def test_paths_and_version(tmp_path: Path) -> None:
    out = jarvis(tmp_path, "paths")
    assert out.returncode == 0 and str(tmp_path / "workspace") in out.stdout
    assert jarvis(tmp_path, "--version").stdout.startswith("JARVIS ")
    assert jarvis(tmp_path, "say", "hello").returncode == 3
