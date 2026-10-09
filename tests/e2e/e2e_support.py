"""A real JARVIS process for the end-to-end tests, driven over HTTP and the WebSocket like the window drives it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[2]
AGENTS = {"research", "system", "development", "creative", "planner"}
FINAL = {"completed", "failed", "cancelled", "interrupted"}
BUSY = {"thinking", "working", "waiting"}

def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def exists(path: Path) -> bool:
    return path.exists()


class Jarvis:
    """One real JARVIS process on a JARVIS_HOME, with an HTTP client and a WebSocket that records every event."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.workspace = home / "workspace"
        self.proc: asyncio.subprocess.Process | None = None
        self.output: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.http: httpx.AsyncClient | None = None
        self.ws: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self.info: dict[str, Any] = {}

    @property
    def launch_url(self) -> str:
        """What the JARVIS window opens: the page swaps the token for a session cookie."""
        return f"http://127.0.0.1:{self.info['port']}/?launch={self.info['token']}"

    # ------------------------------------------------------------------ the process
    async def start(self, timeout: float = 90.0) -> Jarvis:
        env = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS__")}
        env.update(JARVIS_HOME=str(self.home), PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1",
                   JARVIS__VOICE__ENABLED="false", JARVIS__UI__DESKTOP_NOTIFICATIONS="false")
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY"):
            env.pop(key, None)
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "jarvis", "--no-window", "--quiet", "--port", "0", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        ready = asyncio.Event()
        self._tasks.append(asyncio.create_task(self._read_output(ready)))
        try:
            await asyncio.wait_for(ready.wait(), timeout)
        except TimeoutError:
            raise AssertionError("JARVIS did not start:\n" + self.log()) from None
        info = read_json(self.home / "data" / "runtime.json")
        assert info and self.proc.pid in (info["pid"], info.get("ppid")), self.log()   # ppid: a venv launcher
        self.info = info
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{info['port']}", timeout=60,
                                      headers={"X-JARVIS-Token": info["token"]})
        self.ws = await websockets.connect(f"ws://127.0.0.1:{info['port']}/ws?token={info['token']}", max_size=None)
        self._tasks.append(asyncio.create_task(self._read_events()))
        await self.wait_for("state")
        return self

    async def _read_output(self, ready: asyncio.Event) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            self.output.append(line)
            if "JARVIS is running" in line:
                ready.set()

    async def _read_events(self) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            async for raw in self.ws:
                self.events.append(json.loads(raw))

    def log(self) -> str:
        return "\n".join(self.output[-60:])

    async def stop(self) -> int:
        """Quit the way the window's Quit does; returns the exit code."""
        assert self.proc is not None and self.http is not None
        r = await self.http.post("/api/window/quit")
        assert r.status_code == 200, r.text
        code = await asyncio.wait_for(self.proc.wait(), 60)
        await self.close()
        return code

    async def crash(self) -> None:
        """Kill the process outright: nothing gets a chance to save."""
        assert self.proc is not None
        self.proc.kill()
        await self.proc.wait()
        await self.close()

    async def close(self) -> None:
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
        if self.http is not None:
            await self.http.aclose()
        if self.proc is not None and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()

    # ------------------------------------------------------------------ talking to it
    async def say(self, text: str) -> dict[str, Any]:
        assert self.http is not None
        r = await self.http.post("/api/command", json={"text": text}, timeout=200)
        assert r.status_code == 200, r.text
        return r.json()

    async def get(self, path: str) -> Any:
        assert self.http is not None
        r = await self.http.get(path)
        assert r.status_code == 200, r.text
        return r.json()

    async def post(self, path: str, body: Any = None) -> Any:
        assert self.http is not None
        r = await self.http.post(path, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def of(self, type_: str) -> list[Any]:
        return [e["data"] for e in self.events if e["type"] == type_]

    async def wait_for(self, type_: str, check: Callable[[Any], bool] = lambda d: True, timeout: float = 60.0,
                       after: int = 0) -> Any:
        """The first `type_` event (from the `after`-th event on) that passes `check`."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for e in self.events[after:]:
                if e["type"] == type_ and check(e["data"]):
                    return e["data"]
            if asyncio.get_running_loop().time() > deadline:
                seen = [e["type"] for e in self.events[after:] if e["type"] != "system.stats"][-40:]
                chat = [e["data"]["text"] for e in self.events[after:] if e["type"] == "chat.message"]
                raise AssertionError(f"no {type_} event matched; saw {seen}\nchat: {chat}\n{self.log()}")
            await asyncio.sleep(0.05)

    async def said(self, check: Callable[[str], bool], after: int = 0, timeout: float = 60.0) -> str:
        """The text of the first JARVIS chat message that passes `check`."""
        msg = await self.wait_for("chat.message", lambda d: d["role"] == "jarvis" and check(d["text"]),
                                  timeout=timeout, after=after)
        return str(msg["text"])

    async def finished(self, task_id: str, timeout: float = 240.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            task = await self.get(f"/api/tasks/{task_id}")
            if task["status"] in FINAL:
                return task
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"task {task_id} still {task['status']}:\n{json.dumps(task, ensure_ascii=False)}")
            await asyncio.sleep(0.25)

    async def approval(self, tool: str, after: int = 0) -> dict[str, Any]:
        return await self.wait_for("confirm.request", lambda d: d["tool"] == tool, after=after)

    async def answer(self, request_id: str, approved: bool) -> None:
        await self.post(f"/api/confirmations/{request_id}", {"approved": approved})


async def until(check: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)
