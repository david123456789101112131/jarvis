"""The computer-control service: one session at a time, the model server, and JARVIS's own windows.

It is built once by the composition root. `use_computer` runs a session through it; `computer_act` (one
action) reads the live backend and the active session from it. It also hides JARVIS's windows while a
session runs — and shows them just long enough to display an approval — through the desktop shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from jarvis.computer.backend import ComputerBackend, get_backend
from jarvis.computer.model import HoloClient
from jarvis.computer.server import ModelServer
from jarvis.computer.session import ComputerSession, Outcome
from jarvis.config.paths import AppPaths
from jarvis.config.settings import ComputerSettings
from jarvis.security.process import ProcessRegistry
from jarvis.tools.base import ToolContext

log = logging.getLogger(__name__)

WindowFn = Callable[[str], Awaitable[bool]]
IDLE_POLL_S = 30.0
COVER_POLL_S = 0.1
COVER_WAIT_S = 2.0


class ComputerService:
    def __init__(self, settings: ComputerSettings, paths: AppPaths, processes: ProcessRegistry,
                 window: WindowFn, *, backend: ComputerBackend | None = None) -> None:
        self.settings = settings
        self.paths = paths
        self.window = window
        self.backend = backend or get_backend()
        self.server = ModelServer(settings, self.root(), processes, paths.logs)
        self._active: ComputerSession | None = None
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None

    def root(self) -> Path:
        return Path(self.settings.models_dir) if self.settings.models_dir else self.paths.models / "computer"

    @property
    def active(self) -> ComputerSession | None:
        return self._active

    def problem(self) -> str | None:
        """None when a computer session could run here, else why not (model not installed, not Windows…)."""
        if not self.settings.enabled:
            return "computer control is turned off in the settings."
        return self.backend.problem() or self.server.problem()

    def busy(self) -> bool:
        return self._active is not None

    def start(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.ensure_future(self._idle_loop())

    # ------------------------------------------------------------------ running a session
    async def run(self, goal: str, ctx: ToolContext, *, max_steps: int, lang: str = "en") -> Outcome:
        if self.settings.server_url:
            base_url, api_key = self.settings.server_url.rstrip("/"), ""
        else:
            await self.server.ensure_running()
            base_url, api_key = self.server.base_url, self.server.api_key
        self.start()
        client = HoloClient(base_url, api_key=api_key, timeout_s=max(60.0, self.settings.max_minutes * 60))
        async with self._lock:
            session = ComputerSession(goal, client=client, backend=self.backend, ctx=ctx, service=self,
                                      max_steps=max_steps, max_minutes=self.settings.max_minutes,
                                      settle_s=self.settings.settle_s, lang=lang,
                                      stop_on_input=self.settings.stop_on_user_input)
            self._active = session
            try:
                await self.window("computer_on")
                return await session.run()
            finally:
                self._active = None
                with contextlib.suppress(Exception):
                    await self.window("computer_off")
                with contextlib.suppress(Exception):
                    await client.aclose()
                self.server.touch()

    # ------------------------------------------------------------------ JARVIS's windows
    async def ensure_covered(self) -> None:
        """Hide JARVIS's windows (and wait until they are gone) so they are not in the screenshot."""
        if not await asyncio.to_thread(self.backend.own_windows_visible):
            return
        await self.window("computer_hide")
        deadline = asyncio.get_running_loop().time() + COVER_WAIT_S
        while asyncio.get_running_loop().time() < deadline:
            if not await asyncio.to_thread(self.backend.own_windows_visible):
                return
            await asyncio.sleep(COVER_POLL_S)

    async def reveal_for_approval(self, req_task_id: str | None) -> None:
        """An action is waiting for approval: bring JARVIS up so the dialog is visible (active session only)."""
        session = self._active
        if session is None or (session.task_id is not None and req_task_id != session.task_id):
            return
        with contextlib.suppress(Exception):
            await self.window("computer_show")

    def on_confirm(self, req: object) -> None:
        """Broker listener (sync): schedule the reveal for an approval that belongs to the active session."""
        if self._active is None:
            return
        task_id = getattr(req, "task_id", None)
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self.reveal_for_approval(task_id))

    # ------------------------------------------------------------------ lifecycle
    async def _idle_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(IDLE_POLL_S)
                if not self.busy():
                    with contextlib.suppress(Exception):
                        await self.server.stop_if_idle()
        except asyncio.CancelledError:
            pass

    async def aclose(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(BaseException):
                await self._idle_task
        with contextlib.suppress(Exception):
            await self.server.stop()


__all__ = ["ComputerService"]
