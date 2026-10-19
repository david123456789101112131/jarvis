"""One llama.cpp `llama-server` process that JARVIS owns: started when first needed, healthy before it is used,
stopped again after a spell of not being used.

The server listens on 127.0.0.1 only, with a random API key, so nothing on the network, or another program on the
machine, can drive it. Computer control (the Holo vision model, `jarvis.computer.server`) and chat (the model JARVIS
talks and plans with, `jarvis.ai.local_server`) each own one; they take turns on the graphics card (`GpuTurns`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any

import httpx

from jarvis.common.gpu import GpuTurns, busy_text
from jarvis.security.process import ProcessRegistry, kill_tree
from jarvis.security.secrets import sanitized_env

log = logging.getLogger(__name__)

EXE_NAME = "llama-server.exe" if sys.platform == "win32" else "llama-server"
_CREATE_NO_WINDOW = 0x08000000


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def find_llama_server(*roots: Path) -> Path | None:
    """The `llama-server` executable anywhere under `<root>/llama.cpp/` (or the root itself), first root first."""
    for root in roots:
        for base in (root / "llama.cpp", root):
            if base.is_dir():
                for found in sorted(base.rglob(EXE_NAME)):
                    return found
    return None


def find_gguf(root: Path, *must_contain: str, without: tuple[str, ...] = ()) -> Path | None:
    """The first .gguf under `<root>/gguf/` (or the root) whose name has every piece of `must_contain` and none of
    `without` (case does not matter)."""
    for base in (root / "gguf", root):
        if not base.is_dir():
            continue
        for found in sorted(base.glob("*.gguf")):
            name = found.name.lower()
            if all(p.lower() in name for p in must_contain) and not any(p.lower() in name for p in without):
                return found
    return None


def help_flags(exe: Path) -> set[str]:
    """The options this llama-server build knows (from its --help), so a newer option is passed only where it
    exists. Empty when the help cannot be read."""
    kwargs: dict[str, Any] = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        done = subprocess.run([str(exe), "--help"], capture_output=True, timeout=30, env=sanitized_env(),
                              stdin=subprocess.DEVNULL, check=False, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return set()
    text = (done.stdout or b"").decode("utf-8", "replace") + (done.stderr or b"").decode("utf-8", "replace")
    return set(re.findall(r"--[a-z][a-z0-9-]*", text))


def alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return True


class LlamaServer:
    """Owns one `llama-server` process. Subclasses say what is missing (`problem`) and how to start it (`command`)."""

    owner = "llama-server"            # its name in the process registry (the kill switch stops it)
    gpu_name = "llama"                # its name in GpuTurns
    label = "the local model server"  # in messages
    log_name = "llama-server.log"
    health_timeout_s = 240.0          # loading ~10 GB onto the graphics card takes a while the first time

    def __init__(self, processes: ProcessRegistry, logs_dir: Path) -> None:
        self.processes = processes
        self.logs_dir = logs_dir
        self._pid: int | None = None
        self._port = 0
        self._api_key = ""
        self._log: IO[bytes] | None = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()
        self.gpu: GpuTurns | None = None          # set by the app: the other local models let go of the card first
        self.flags: set[str] = set()              # what this build's --help lists (read before the first start)
        self._flags_of: Path | None = None

    # -- what subclasses fill in
    def problem(self) -> str | None:
        return None

    def command(self) -> list[str]:
        raise NotImplementedError

    def wanted_port(self) -> int:
        return 0

    @property
    def exe(self) -> Path | None:
        return None

    # -- state
    @property
    def running(self) -> bool:
        return self._pid is not None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def api_key(self) -> str:
        return self._api_key

    def touch(self) -> None:
        self._last_used = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_used if self.running else 0.0

    # -- starting and stopping
    async def ensure_running(self) -> None:
        """Start the server if it is not up yet; several callers may await this at once."""
        async with self._lock:
            self.touch()
            if self.running and self._pid is not None and alive(self._pid):
                return
            if self.running:                       # it stopped by itself (out of memory, closed from outside)
                await self.stop()
            problem = self.problem()
            if problem is not None:
                raise RuntimeError(problem)
            if self.gpu is not None:
                busy = await self.gpu.take(self.gpu_name)
                if busy is not None:
                    raise RuntimeError(busy_text(busy))
            await self._start()

    async def _start(self) -> None:
        exe = self.exe
        if exe is not None and exe != self._flags_of:
            self.flags, self._flags_of = await asyncio.to_thread(help_flags, exe), exe
        self._port = self.wanted_port() or free_port()
        self._api_key = secrets.token_hex(24)
        args = self.command()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._log = (self.logs_dir / self.log_name).open("ab")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        log.info("Starting %s on 127.0.0.1:%d", self.label, self._port)
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(exe.parent) if exe is not None else None, env=sanitized_env(),
            stdin=asyncio.subprocess.DEVNULL, stdout=self._log, stderr=self._log, **kwargs,
        )
        self._pid = proc.pid
        self.processes.register(proc.pid, owner=self.owner)
        try:
            await self._wait_healthy()
        except BaseException:
            await self.stop()
            raise
        self.touch()

    async def _wait_healthy(self) -> None:
        deadline = time.monotonic() + self.health_timeout_s
        async with httpx.AsyncClient(timeout=5.0, trust_env=False,
                                     headers={"Authorization": f"Bearer {self._api_key}"}) as client:
            while time.monotonic() < deadline:
                if self._pid is not None and not alive(self._pid):
                    raise RuntimeError(f"{self.label} stopped while starting (see its log, {self.log_name}).")
                with contextlib.suppress(httpx.HTTPError):
                    resp = await client.get(f"{self.base_url}/health")
                    if resp.status_code == 200:
                        return
                await asyncio.sleep(1.0)
        raise RuntimeError(f"{self.label} did not become ready in time.")

    async def stop(self) -> None:
        pid, self._pid = self._pid, None
        if pid is not None:
            await asyncio.to_thread(kill_tree, pid)
            self.processes.unregister(pid)
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log.close()
            self._log = None
        self._port, self._api_key = 0, ""

    async def stop_after(self, idle_s: float) -> bool:
        """Stop the server once it has gone `idle_s` seconds unused. True = it was stopped."""
        if self.running and self.idle_seconds() >= idle_s:
            log.info("Stopping %s: unused for %.0f minutes", self.label, idle_s / 60)
            await self.stop()
            return True
        return False


__all__ = ["EXE_NAME", "LlamaServer", "alive", "find_gguf", "find_llama_server", "free_port", "help_flags"]
