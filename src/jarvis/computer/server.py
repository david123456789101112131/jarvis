"""Start and stop the local model server (llama.cpp's `llama-server`) for computer control.

The server listens on 127.0.0.1 only, with a random API key, so nothing on the network — or another
program on the machine — can drive it. It loads the Holo weights onto the GPU once and stays up while
JARVIS is using the computer; it is stopped again after a spell of not being used (`idle_minutes`).

Files are found under the models folder, however `jarvis computer install` or an existing download laid
them out: the `llama-server` executable anywhere under `llama.cpp/`, and the Q8_0 weights and the vision
projector (`mmproj`) under `gguf/` (or the folder itself).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import IO, Any

import httpx

from jarvis.config.settings import ComputerSettings
from jarvis.security.process import ProcessRegistry, kill_tree
from jarvis.security.secrets import sanitized_env

log = logging.getLogger(__name__)

OWNER = "computer-model"
EXE_NAME = "llama-server.exe" if sys.platform == "win32" else "llama-server"
HEALTH_TIMEOUT_S = 240.0       # loading ~10 GB onto the GPU takes a while the first time
IMAGE_MIN_TOKENS = 1024        # the least picture detail the model grounds clicks well with
_CREATE_NO_WINDOW = 0x08000000


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ModelFiles:
    """Where the server executable and the weights are; `problem` says what is missing."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.exe = self._find_exe()
        self.model = self._find_gguf("Holo", "Q8") or self._find_gguf("Holo", "q8")
        self.mmproj = self._find_gguf("mmproj")

    def _find_exe(self) -> Path | None:
        direct = self.root / "llama.cpp"
        for base in (direct, self.root):
            if base.is_dir():
                for found in sorted(base.rglob(EXE_NAME)):
                    return found
        return None

    def _find_gguf(self, *must_contain: str) -> Path | None:
        for base in (self.root / "gguf", self.root):
            if not base.is_dir():
                continue
            for found in sorted(base.glob("*.gguf")):
                name = found.name.lower()
                if all(piece.lower() in name for piece in must_contain):
                    return found
        return None

    def problem(self) -> str | None:
        if self.exe is None:
            return "llama-server is not installed (run: jarvis computer install)."
        if self.model is None:
            return "the computer-control model weights are missing (run: jarvis computer install)."
        if self.mmproj is None:
            return "the model's vision projector (mmproj) is missing (run: jarvis computer install)."
        return None


class ModelServer:
    """Owns one `llama-server` process: starts it, waits for it to be healthy, stops it when idle."""

    def __init__(self, settings: ComputerSettings, root: Path, processes: ProcessRegistry, logs_dir: Path) -> None:
        self.settings = settings
        self.files = ModelFiles(root)
        self.processes = processes
        self.logs_dir = logs_dir
        self._pid: int | None = None
        self._port = 0
        self._api_key = ""
        self._log: IO[bytes] | None = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()

    def problem(self) -> str | None:
        return self.files.problem()

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

    async def ensure_running(self) -> None:
        """Start the server if it is not up yet; several callers may await this at once."""
        async with self._lock:
            self.touch()
            if self.running:
                return
            problem = self.problem()
            if problem is not None:
                raise RuntimeError(problem)
            await self._start()

    def command(self) -> list[str]:
        """The llama-server command line, for the port and key of this start."""
        assert self.files.exe and self.files.model and self.files.mmproj
        return [
            str(self.files.exe), "-m", str(self.files.model), "--mmproj", str(self.files.mmproj),
            "--host", "127.0.0.1", "--port", str(self._port),
            "-ngl", str(self.settings.gpu_layers), "-c", str(self.settings.context),
            "-fa", "on", "--jinja", "--parallel", "1", "--api-key", self._api_key,
            # Qwen-VL based models place clicks badly on pictures of fewer than 1024 tokens (llama-server
            # warns about it at load); smaller screenshots (a small or square screen) are scaled up to that.
            "--image-min-tokens", str(IMAGE_MIN_TOKENS),
        ]

    async def _start(self) -> None:
        assert self.files.exe and self.files.model and self.files.mmproj
        self._port = self.settings.port or _free_port()
        self._api_key = secrets.token_hex(24)
        args = self.command()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._log = (self.logs_dir / "computer-model.log").open("ab")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        log.info("Starting the computer-control model server on 127.0.0.1:%d", self._port)
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(self.files.exe.parent), env=sanitized_env(),
            stdin=asyncio.subprocess.DEVNULL, stdout=self._log, stderr=self._log, **kwargs,
        )
        self._pid = proc.pid
        self.processes.register(proc.pid, owner=OWNER)
        try:
            await self._wait_healthy()
        except Exception:
            await self.stop()
            raise
        self.touch()

    async def _wait_healthy(self) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        async with httpx.AsyncClient(timeout=5.0, trust_env=False,
                                     headers={"Authorization": f"Bearer {self._api_key}"}) as client:
            while time.monotonic() < deadline:
                if self._pid is not None and not _alive(self._pid):
                    raise RuntimeError("the computer-control model server stopped while starting (see its log).")
                with contextlib.suppress(httpx.HTTPError):
                    resp = await client.get(f"{self.base_url}/health")
                    if resp.status_code == 200:
                        return
                await asyncio.sleep(1.0)
        raise RuntimeError("the computer-control model server did not become ready in time.")

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

    async def stop_if_idle(self) -> bool:
        if self.running and self.idle_seconds() >= self.settings.idle_minutes * 60:
            log.info("Stopping the idle computer-control model server")
            await self.stop()
            return True
        return False


def _alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return True


__all__ = ["ModelFiles", "ModelServer"]
