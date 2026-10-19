"""The computer-control service: one session at a time, the model server, and JARVIS's own windows.

It is built once by the composition root. `use_computer` runs a session through it; `computer_act` (one
action) reads the live backend and the active session from it. It also hides JARVIS's windows while a
session runs — and shows them just long enough to display an approval — through the desktop shell, and
puts the window the user asked from back in front, so the model's first look is at what they meant.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from jarvis.common.errors import ToolError
from jarvis.common.i18n import t
from jarvis.computer.backend import ComputerBackend, get_backend
from jarvis.computer.model import HoloClient
from jarvis.computer.server import ModelServer
from jarvis.computer.session import ComputerSession, OneStep, Outcome
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
        self._active: ComputerSession | OneStep | None = None
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._warm_task: asyncio.Task[None] | None = None
        self._warm_until = 0.0
        self._warm_runs = 0
        self._runs = 0                     # tasks started so far: a model loaded for none of them is stopped
        self._working = 0                  # the window the last task was asked for in (0: none)
        self._named = False                # …found by the name the agent gave, so it may be restored

    def root(self) -> Path:
        return Path(self.settings.models_dir) if self.settings.models_dir else self.paths.models / "computer"

    @property
    def active(self) -> ComputerSession | OneStep | None:
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
    def warm_up(self, unused_after_s: float) -> None:
        """While the approval is on screen: start loading the local model (about 20 s), so an approved task can
        begin at once. If no task has started `unused_after_s` later (declined, or the approval expired), the
        model is stopped again and the graphics card freed. Nothing is looked at or touched before approval."""
        if self.settings.server_url or self.problem() is not None:
            return
        self._warm_until = time.monotonic() + unused_after_s      # a newer request moves the deadline
        self._warm_runs = self._runs
        if self._warm_task is None or self._warm_task.done():
            self.start()
            self._warm_task = asyncio.ensure_future(self._warm())

    async def _warm(self) -> None:
        try:
            await self.server.ensure_running()
        except Exception as exc:              # the task tries again when it starts, and reports it there
            log.warning("Loading the computer-control model ahead of the task failed: %s", exc)
            return
        while (left := self._warm_until - time.monotonic()) > 0:
            await asyncio.sleep(left)
        if self._runs == self._warm_runs and not self.busy():
            log.info("Stopping the computer-control model: the task it was loaded for did not start")
            await self.server.stop()

    async def run(self, goal: str, ctx: ToolContext, *, max_steps: int, lang: str = "en") -> Outcome:
        self._runs += 1
        if self.settings.server_url:
            base_url, api_key = self.settings.server_url.rstrip("/"), ""
        else:
            await self.server.ensure_running()
            base_url, api_key = self.server.base_url, self.server.api_key
        self.start()
        client = HoloClient(base_url, api_key=api_key, thinking=self.settings.thinking,
                            timeout_s=max(60.0, self.settings.max_minutes * 60))
        async with self._lock:
            session = ComputerSession(goal, client=client, backend=self.backend, ctx=ctx, service=self,
                                      max_steps=max_steps, max_minutes=self.settings.max_minutes,
                                      settle_s=self.settings.settle_s, lang=lang,
                                      stop_on_input=self.settings.stop_on_user_input)
            self._active = session
            try:
                await self.window("computer_on")
                await self._bring_back_working_window()
                outcome = await session.run()
                log.info("Computer session ended (%s) after %d steps%s", outcome.reason, outcome.steps,
                         f": {outcome.error}" if outcome.error else "")
                return outcome
            finally:
                self._active = None
                with contextlib.suppress(Exception):
                    await self.window("computer_off")
                with contextlib.suppress(Exception):
                    await client.aclose()
                self.server.touch()

    # ------------------------------------------------------------------ one look or one step, outside a session
    @contextlib.asynccontextmanager
    async def model_client(self) -> AsyncIterator[HoloClient]:
        """The model for one look at the screen or one proposed step (the "Apps & tools" screen). It is started
        first if it is not up yet, while JARVIS is still on screen showing that it loads."""
        self._runs += 1
        if self.settings.server_url:
            base_url, api_key = self.settings.server_url.rstrip("/"), ""
        else:
            await self.server.ensure_running()
            base_url, api_key = self.server.base_url, self.server.api_key
        self.start()
        client = HoloClient(base_url, api_key=api_key, thinking=self.settings.thinking, timeout_s=120.0)
        try:
            yield client
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()
            self.server.touch()

    @contextlib.asynccontextmanager
    async def screen_turn(self, ctx: ToolContext, *, window: int = 0, act: bool = False) -> AsyncIterator[OneStep | None]:
        """The screen for one look, or for one approved step (`act`), outside a session: JARVIS's windows are out
        of the way and the window it is about (`window`, else the one noted for the task) is in front. Esc twice
        stops the job meanwhile. Afterwards JARVIS comes back the way it was.

        With `act`, a `OneStep` is the active session while it lasts, so that step's `computer_act` passes the
        gate and the approvals it asks for are shown."""
        if self.busy() or self._lock.locked():
            raise ToolError(t(ctx.lang, "שליטה במחשב כבר רצה כרגע (Esc פעמיים עוצר אותה)",
                              "computer control is already running (Esc twice stops it)"))
        loop = asyncio.get_running_loop()
        async with self._lock:
            step = OneStep(ctx) if act else None
            stop = step.stop if step is not None else threading.Event()

            def escape() -> None:                  # called from the watcher's thread
                stop.set()
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(ctx.cancel.cancel, "Esc")

            watch = self.backend.watch_escape(escape)
            self._active = step
            try:
                await self.window("computer_on")
                if window:
                    await self.ensure_covered()
                    if not await asyncio.to_thread(self.backend.bring_to_front, window, restore=True):
                        raise ToolError(t(ctx.lang, "לא הצלחתי להביא את החלון הזה לחזית (אולי הוא נסגר)",
                                          "could not bring that window to the front (it may have closed)"))
                    await asyncio.sleep(self.settings.settle_s)        # let it draw
                else:
                    await self._bring_back_working_window()
                await self.ensure_covered()
                yield step
            finally:
                self._active = None
                stop.set()                         # typing still under way stops
                with contextlib.suppress(Exception):
                    watch.stop()
                with contextlib.suppress(Exception):
                    await self.window("computer_off")

    # ------------------------------------------------------------------ the task's window
    async def note_working_window(self, window: str = "") -> bool:
        """Before the approval: remember the window the task is for. That is the open window named `window`
        (part of its title or program) when there is one, else the one the user is working in. True when
        the named one was found.

        It has to be now: by the time the session starts, the approval, and whatever the user opened while
        the model loaded (a chat, say), may be in front of it."""
        self._working, self._named = 0, False
        with contextlib.suppress(Exception):
            if window.strip():
                self._working = int(await asyncio.to_thread(self.backend.find_window, window) or 0)
                self._named = bool(self._working)
            if not self._working:
                self._working = int(await asyncio.to_thread(self.backend.working_window) or 0)
        return self._named

    async def _bring_back_working_window(self) -> None:
        """JARVIS is out of the way: put that window in front for the model's first look (once per task).

        A window the task named is restored if it is minimized; the user's own window is left alone then."""
        hwnd, restore = self._working, self._named
        self._working, self._named = 0, False
        if not hwnd:
            return
        await self.ensure_covered()
        brought = False
        with contextlib.suppress(Exception):
            brought = bool(await asyncio.to_thread(self.backend.bring_to_front, hwnd, restore=restore))
        if brought:
            await asyncio.sleep(self.settings.settle_s)        # let it draw before the screenshot
        else:
            log.info("The window the task was asked from could not be put back in front (closed or minimized)")

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
        for task in (self._idle_task, self._warm_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        with contextlib.suppress(Exception):
            await self.server.stop()


__all__ = ["ComputerService"]
