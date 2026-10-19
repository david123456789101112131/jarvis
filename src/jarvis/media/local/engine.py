"""The local image and video engine as JARVIS sees it: the worker process, started and stopped from here.

The worker (`worker.py`) runs with the media runtime's own Python, `<media>/venv`, so PyTorch and the models stay
out of JARVIS's process. It starts when the first image or video is asked for, takes requests one at a time, and
is stopped after `media.idle_minutes` without work, or as soon as computer control needs the graphics card, which
frees the card completely. Stopping a job midway stops the worker too (the next job starts it again).

Before the worker starts, the other big local model (computer control) lets go of the graphics card (`GpuTurns`);
while it is in the middle of a task, nothing is started here and the user is told the card is busy.

The worker never uses the network (it runs with HF_HUB_OFFLINE), and its environment has every secret removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import IO, Any

from jarvis.common.gpu import GpuTurns
from jarvis.config.settings import MediaSettings
from jarvis.media.local import worker as worker_module
from jarvis.security.process import ProcessRegistry, kill_tree
from jarvis.security.secrets import sanitized_env

log = logging.getLogger(__name__)

OWNER = "media-model"
GPU_NAME = "media"
READY_TIMEOUT_S = 300.0          # the first start imports PyTorch from a cold disk
LINE_LIMIT = 1 << 20
IDLE_POLL_S = 30.0
_CREATE_NO_WINDOW = 0x08000000

IMAGE_DIR = worker_module.IMAGE_DIR
VIDEO_DIR = worker_module.VIDEO_DIR
TRANSLATE_DIR = worker_module.TRANSLATE_DIR

Progress = Callable[[dict[str, Any]], Awaitable[None]]


class LocalMediaError(RuntimeError):
    """The engine could not do it. `kind`: missing, no_gpu, gpu_busy, oom, low_memory, crashed, start, bad_request,
    failed; `details` holds the worker's numbers for it (for low_memory: need_ram, free_ram, need_vram, free_vram)."""

    def __init__(self, message: str, kind: str = "failed", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


def _details(msg: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in msg.items() if k not in ("id", "event", "kind", "message")}


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


class LocalFiles:
    """Where the runtime and the models are, and what is missing for each kind of work."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.venv = root / "venv"
        # The source file itself: in the packaged app the module is compiled, and its .py is shipped beside it.
        self.worker = Path(worker_module.__file__).with_suffix(".py")

    @property
    def python(self) -> Path:
        return venv_python(self.venv)

    def has(self, part: str) -> bool:
        if part == "runtime":
            return self.python.is_file() and self.worker.is_file()
        if part == "image":
            return (self.root / IMAGE_DIR / "model_index.json").is_file()
        if part == "video":
            return (self.root / VIDEO_DIR / "model_index.json").is_file()
        if part == "translate":
            return (self.root / TRANSLATE_DIR / "config.json").is_file()
        raise ValueError(part)

    def missing(self, kind: str) -> list[str]:
        """The parts `kind` ("image" or "video") still needs: "runtime", "image", "video"."""
        return [part for part in ("runtime", kind) if not self.has(part)]


class LocalEngine:
    def __init__(self, settings: MediaSettings, root: Path, processes: ProcessRegistry, logs_dir: Path, *,
                 gpu: GpuTurns | None = None, command: Callable[[], list[str]] | None = None) -> None:
        self.settings = settings
        self.files = LocalFiles(root)
        self.processes = processes
        self.logs_dir = logs_dir
        self.gpu = gpu
        self._command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._log: IO[bytes] | None = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._seq = 0
        self._working = False
        self._idle_task: asyncio.Task[None] | None = None
        self.info: dict[str, Any] = {}

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def busy(self) -> bool:
        """Making something right now (an idle worker with a model loaded is not busy: it can let go)."""
        return self._working

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_used if self.running else 0.0

    def command(self) -> list[str]:
        if self._command is not None:
            return self._command()
        return [str(self.files.python), "-I", "-u", str(self.files.worker), "--root", str(self.files.root),
                "--offload", self.settings.offload]

    # ------------------------------------------------------------------ the process
    async def _start(self) -> None:
        if self.gpu is not None:
            other = await self.gpu.take(GPU_NAME)
            if other is not None:
                raise LocalMediaError(f"the graphics card is busy ({other})", "gpu_busy")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._log = (self.logs_dir / "media-model.log").open("ab")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        env = sanitized_env({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"})
        args = self.command()
        log.info("Starting the local media engine")
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(self.files.root if self.files.root.is_dir() else Path.home()), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self._log,
                limit=LINE_LIMIT, **kwargs)
        except OSError as exc:
            self._close_log()
            raise LocalMediaError(f"the local engine could not start: {exc}", "start") from exc
        self._proc = proc
        self.processes.register(proc.pid, owner=OWNER)
        try:
            first = await asyncio.wait_for(self._read(), READY_TIMEOUT_S)
        except TimeoutError:
            await self.stop()
            raise LocalMediaError("the local engine did not get ready in time", "start") from None
        except BaseException:
            await self.stop()        # stopped while it was starting (or it ended): nothing is left running
            raise
        if first.get("event") != "ready":
            await self.stop()
            raise LocalMediaError(str(first.get("message") or "the local engine could not start"),
                                  str(first.get("kind") or "start"), _details(first))
        self.info = {k: v for k, v in first.items() if k not in ("event",)}
        log.info("Local media engine ready: torch %s, %s", self.info.get("torch"), self.info.get("device", "no GPU"))

    async def _read(self) -> dict[str, Any]:
        """The next message from the worker; it ending is an error that says what its log says."""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                code = await self._proc.wait()
                if code == worker_module.LOW_MEMORY_EXIT:
                    raise LocalMediaError(f"the local engine stopped itself: the computer was running out of "
                                          f"memory{self._log_tail()}", "low_memory", {"stopped": True})
                raise LocalMediaError(f"the local engine stopped (exit code {code}){self._log_tail()}", "crashed")
            with contextlib.suppress(ValueError):
                msg = json.loads(line.decode("utf-8", "replace"))
                if isinstance(msg, dict):
                    return msg

    def _log_tail(self, limit: int = 600) -> str:
        path = self.logs_dir / "media-model.log"
        with contextlib.suppress(OSError):
            with path.open("rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - limit))
                tail = fh.read().decode("utf-8", "replace").strip().splitlines()[-3:]
                if tail:
                    return ": " + " | ".join(tail)
        return ""

    def _close_log(self) -> None:
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log.close()
            self._log = None

    async def stop(self) -> None:
        """End the worker: whatever it was doing stops, and the graphics card is free at once."""
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.returncode is None:
                await asyncio.to_thread(kill_tree, proc.pid)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 10)
            self.processes.unregister(proc.pid)
        self._close_log()
        self.info = {}

    async def stop_if_idle(self) -> bool:
        if self.running and not self._lock.locked() and self.idle_seconds() >= self.settings.idle_minutes * 60:
            log.info("Stopping the idle local media engine")
            await self.stop()
            return True
        return False

    def start_idle_watch(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.ensure_future(self._idle_loop())

    async def _idle_loop(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(IDLE_POLL_S)
                with contextlib.suppress(Exception):
                    await self.stop_if_idle()

    async def aclose(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(BaseException):
                await self._idle_task
        await self.stop()

    # ------------------------------------------------------------------ requests
    async def request(self, op: str, payload: dict[str, Any] | None = None, *,
                      progress: Progress | None = None) -> dict[str, Any]:
        """Send one request and wait for its answer. Cancelling the caller ends the worker (that is the stop)."""
        async with self._lock:
            heavy = op in ("image", "video")
            self._working = heavy
            try:
                if not self.running:
                    await self._start()
                elif heavy and self.gpu is not None:
                    other = await self.gpu.take(GPU_NAME)
                    if other is not None:
                        raise LocalMediaError(f"the graphics card is busy ({other})", "gpu_busy")
                self.start_idle_watch()
                return await self._exchange(op, payload or {}, progress)
            finally:
                self._working = False
                self._last_used = time.monotonic()

    async def _exchange(self, op: str, payload: dict[str, Any], progress: Progress | None) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdin is not None
        self._seq += 1
        rid = f"r{self._seq}"
        try:
            self._proc.stdin.write((json.dumps({"id": rid, "op": op, **payload}, ensure_ascii=False) + "\n").encode())
            await self._proc.stdin.drain()
            while True:
                msg = await self._read()
                if msg.get("id") != rid:
                    continue
                event = msg.get("event")
                if event == "progress":
                    if progress is not None:
                        await progress(msg)
                elif event == "done":
                    return {k: v for k, v in msg.items() if k not in ("id", "event")}
                elif event == "error":
                    raise LocalMediaError(str(msg.get("message") or "failed"), str(msg.get("kind") or "failed"),
                                          _details(msg))
        except LocalMediaError as exc:
            if exc.kind == "crashed" or exc.details.get("stopped"):
                await self.stop()
            raise
        except BaseException:
            # Stopped midway (or the pipe broke): end the worker so the card stops working on it right away.
            await self.stop()
            raise


__all__ = ["IMAGE_DIR", "TRANSLATE_DIR", "VIDEO_DIR", "LocalEngine", "LocalFiles", "LocalMediaError", "venv_python"]
