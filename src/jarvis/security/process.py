"""Sandboxed child-process execution.

Every program an agent runs goes through `run_process`:
* secret variables are stripped from the environment,
* the process gets its own process group (POSIX session / Windows new process group,
  no console window),
* output is captured with a size limit (head + tail kept),
* timeout and cooperative cancellation kill the whole process tree,
* live processes are tracked in a `ProcessRegistry` so kill switches can terminate them.
"""

from __future__ import annotations

import asyncio
import contextlib
import locale
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import psutil

from jarvis.security.secrets import sanitized_env

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"


@dataclass
class ProcessResult:
    args: list[str]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    duration_s: float = 0.0
    pid: int | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    def summary(self, limit: int = 600) -> str:
        state = "timed out" if self.timed_out else "cancelled" if self.cancelled else f"exit {self.exit_code}"
        out = (self.stdout or "").strip()
        err = (self.stderr or "").strip()
        body = out[-limit:] if out else ""
        if err:
            body = (body + "\n" if body else "") + "stderr: " + err[-limit:]
        return f"[{state}] {body}".strip()

    def to_dict(self) -> dict:
        return {
            "args": self.args,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass
class _Tracked:
    pid: int
    owner: str | None
    task_id: str | None
    started: float = field(default_factory=time.monotonic)


class ProcessRegistry:
    """Live child processes started by tools, so they can be killed on cancel/kill switch."""

    def __init__(self) -> None:
        self._procs: dict[int, _Tracked] = {}

    def register(self, pid: int, owner: str | None = None, task_id: str | None = None) -> None:
        self._procs[pid] = _Tracked(pid, owner, task_id)

    def unregister(self, pid: int) -> None:
        self._procs.pop(pid, None)

    def list(self) -> list[dict]:
        return [{"pid": t.pid, "owner": t.owner, "task_id": t.task_id} for t in self._procs.values()]

    def kill(self, *, owner: str | None = None, task_id: str | None = None) -> int:
        """Kill tracked process trees matching the filters (no filters = all)."""
        killed = 0
        for t in list(self._procs.values()):
            if owner is not None and t.owner != owner:
                continue
            if task_id is not None and t.task_id != task_id:
                continue
            killed += kill_tree(t.pid)
            self.unregister(t.pid)
        return killed


def kill_tree(pid: int, timeout: float = 3.0) -> int:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return 0
    procs = [parent]
    with contextlib.suppress(psutil.Error):
        procs = [*parent.children(recursive=True), parent]
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.terminate()
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        with contextlib.suppress(psutil.Error):
            p.kill()
    return len(procs)


def decode_output(data: bytes) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in (["oem", "mbcs"] if IS_WINDOWS else []) + [locale.getpreferredencoding(False)]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class _Collector:
    def __init__(self, limit: int) -> None:
        self.limit = max(1024, limit)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        half = self.limit // 2
        if len(self.head) < half:
            take = half - len(self.head)
            self.head += chunk[:take]
            chunk = chunk[take:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > half:
                del self.tail[: len(self.tail) - half]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + len(self.tail)

    def text(self) -> str:
        if self.truncated:
            return decode_output(bytes(self.head)) + "\n…[output truncated]…\n" + decode_output(bytes(self.tail))
        return decode_output(bytes(self.head) + bytes(self.tail))


async def run_process(
    args: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float = 300.0,
    env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    max_output: int = 20000,
    cancel_event: asyncio.Event | None = None,
    registry: ProcessRegistry | None = None,
    owner: str | None = None,
    task_id: str | None = None,
    stdin_data: bytes | None = None,
    on_output: Callable[[str], None] | None = None,
) -> ProcessResult:
    argv = [str(a) for a in args]
    started = time.monotonic()
    proc_env = env if env is not None else sanitized_env(extra_env)
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=proc_env,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except FileNotFoundError:
        return ProcessResult(argv, exit_code=127, stderr=f"Program not found: {argv[0]}", duration_s=0.0)
    except PermissionError as exc:
        return ProcessResult(argv, exit_code=126, stderr=f"Permission error starting {argv[0]}: {exc}")
    except OSError as exc:
        return ProcessResult(argv, exit_code=1, stderr=f"Cannot start {argv[0]}: {exc}")

    if registry is not None:
        registry.register(proc.pid, owner, task_id)
    out_c, err_c = _Collector(max_output), _Collector(max_output // 2)

    async def pump(stream: asyncio.StreamReader | None, coll: _Collector) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            coll.feed(chunk)
            if on_output is not None:
                with contextlib.suppress(Exception):
                    on_output(decode_output(chunk))

    async def feed_stdin() -> None:
        if stdin_data is not None and proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
                proc.stdin.close()

    pumps = asyncio.gather(pump(proc.stdout, out_c), pump(proc.stderr, err_c), feed_stdin())
    waiter = asyncio.ensure_future(proc.wait())
    cancel_waiter = asyncio.ensure_future(cancel_event.wait()) if cancel_event is not None else None
    timed_out = cancelled = False
    try:
        wait_set = {waiter} | ({cancel_waiter} if cancel_waiter else set())
        done, _ = await asyncio.wait(wait_set, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if waiter not in done:
            if cancel_waiter is not None and cancel_waiter in done:
                cancelled = True
            else:
                timed_out = True
            await asyncio.to_thread(kill_tree, proc.pid)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(waiter, timeout=5)
    except asyncio.CancelledError:
        cancelled = True
        await asyncio.to_thread(kill_tree, proc.pid)
        raise
    finally:
        if cancel_waiter is not None:
            cancel_waiter.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(pumps, timeout=5)
        if registry is not None:
            registry.unregister(proc.pid)

    return ProcessResult(
        argv,
        exit_code=proc.returncode,
        stdout=out_c.text(),
        stderr=err_c.text(),
        timed_out=timed_out,
        cancelled=cancelled,
        truncated=out_c.truncated or err_c.truncated,
        duration_s=time.monotonic() - started,
        pid=proc.pid,
    )
