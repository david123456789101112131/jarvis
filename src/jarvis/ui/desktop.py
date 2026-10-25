"""The desktop shell: the HUD in a native window (Edge WebView2) plus the always-on-top mini orb.

Threads: the window toolkit must own the main thread (a Windows GUI rule), so JARVIS's asyncio
loop runs in a background thread (`CoreThread`). The windows load `/?launch=<token>` once the
local server is up; everything else (commands, events) goes through the same API as any UI.

While only the mini orb is on screen, a third, transparent window in the middle of the screen carries the orb
big while JARVIS listens and speaks ("focus orb"); its page asks for it with `focus_show` / `focus_hide` and the
mini orb steps aside meanwhile. It never takes the keyboard from the app the user is in.

The orb and the voice view are parts of JARVIS, not windows of their own: neither has a taskbar button or an
Alt+Tab entry (Windows "tool windows"), so the taskbar shows one JARVIS. pywebview shows a transparent window each
time its page loads, hidden or not, so after every load both are put back as JARVIS wants them.

Closing the main window keeps JARVIS running as the mini orb when "mini orb" is on in the
settings (it keeps listening for the wake word); otherwise it quits. Double-clicking the orb
brings the main window back. When WebView2 is missing, the UI opens as an Edge app window
(or in the default browser) and JARVIS runs until it is told to quit.

`--background` (how Windows starts JARVIS at sign-in, see `jarvis.host.autostart`) opens only the
mini orb; "Hey Jarvis", the orb or the shortcut bring the main window up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis.common.console import show_error

log = logging.getLogger(__name__)

ORB_SIZE = 260
FOCUS_CAPTION = 170          # room under the big orb for what is being said
BACKGROUND = "#02070d"
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x80
_WS_EX_APPWINDOW = 0x40000


def keep_off_taskbar(window: Any) -> bool:
    """Make a pywebview window a Windows tool window: no taskbar button, no Alt+Tab entry. True when its style
    changed now (the taskbar takes it in the next time the window is shown)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = int(window.native.Handle.ToInt64())
        user32 = ctypes.windll.user32
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        style = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
        wanted = (style | _WS_EX_TOOLWINDOW) & ~_WS_EX_APPWINDOW
        if wanted == style:
            return False
        user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, wanted)
        return True
    except Exception as exc:   # no native window yet, or not the WinForms backend
        log.debug("Could not keep a window off the taskbar: %s", exc)
        return False


class CoreThread(threading.Thread):
    """Runs `JarvisApp` in its own event loop until `stop()` (or a "quit" from the UI)."""

    def __init__(self, make_app: Callable[[], Any]) -> None:
        super().__init__(name="jarvis-core", daemon=True)
        self.make_app = make_app
        self.loop = asyncio.new_event_loop()
        self.app: Any = None
        self.error: BaseException | None = None
        self.ready = threading.Event()
        self.finished = threading.Event()
        self._stop: asyncio.Event | None = None

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        finally:
            with contextlib.suppress(Exception):
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()
            self.finished.set()

    async def _main(self) -> None:
        self._stop = asyncio.Event()
        try:
            self.app = self.make_app()
            await self.app.start()
        except BaseException as exc:      # reported to the main thread, which shows it and exits
            self.error = exc
            if self.app is not None:
                with contextlib.suppress(BaseException):
                    await self.app.shutdown()
            self.ready.set()
            return
        self.ready.set()
        try:
            await self._stop.wait()
        finally:
            await self.app.shutdown()

    def request_stop(self) -> None:
        if self._stop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._stop.set)

    def stop(self, timeout: float = 30.0) -> None:
        self.request_stop()
        self.finished.wait(timeout)

    def submit(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_version() -> str | None:
    """The version of the Edge WebView2 runtime (it ships with Windows 11 and current Edge), or None."""
    if sys.platform != "win32":
        return None
    import winreg

    places = [(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + WEBVIEW2_CLIENT),
              (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + WEBVIEW2_CLIENT),
              (winreg.HKEY_CURRENT_USER, "SOFTWARE\\" + WEBVIEW2_CLIENT)]
    for root, key in places:
        try:
            with winreg.OpenKey(root, key) as k:
                version, _ = winreg.QueryValueEx(k, "pv")
                if version and version != "0.0.0.0":
                    return str(version)
        except OSError:
            continue
    return None


def webview2_available() -> bool:
    """Can the native window open? Only Windows needs the WebView2 runtime for it."""
    return sys.platform != "win32" or webview2_version() is not None


def find_edge() -> str | None:
    if sys.platform != "win32":
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            exe = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if exe.is_file():
                return str(exe)
    return shutil.which("msedge")


def app_icon() -> str | None:
    """The JARVIS icon (drawn by scripts/make_icon.py), shipped in the package and next to JARVIS.exe."""
    package = Path(__file__).resolve().parents[1]
    for candidate in (package / "assets" / "jarvis.ico", Path(sys.executable).parent / "jarvis.ico"):
        if candidate.is_file():
            return str(candidate)
    return None


class DesktopShell:
    """The two pywebview windows and what the API's /api/window/{action} does with them."""

    def __init__(self, webview: Any, core: CoreThread, *, background: bool = False) -> None:
        self.webview = webview
        self.core = core
        self.app = core.app
        self.background = background
        self.main: Any = None
        self.orb: Any = None
        self._quitting = False
        self._lock = threading.Lock()
        self.focus: Any = None
        self._computer_mode = False       # JARVIS's windows are hidden while it controls the computer
        self._resume_full = False          # whether the main window was up before the session
        self._focus_wanted = False         # the voice view's page wants to be on screen (the latest word wins)
        self._focus_up = False

    # ------------------------------------------------------------------ windows
    def create(self) -> None:
        ui = self.app.settings.ui
        name = self.app.settings.general.assistant_name or "JARVIS"
        self.main = self.webview.create_window(
            name, url=self.app.launch_url, width=ui.window_width, height=ui.window_height, min_size=(1100, 700),
            background_color=BACKGROUND, text_select=True, hidden=self.background)
        x, y = self._orb_position()
        self.orb = self.webview.create_window(
            f"{name} Orb", url=self.app.launch_url + "&view=orb", width=ORB_SIZE, height=ORB_SIZE, x=x, y=y,
            resizable=False, frameless=True, easy_drag=True, on_top=True,
            hidden=not (self.background and ui.mini_orb), transparent=True,
            shadow=False, focus=False, background_color=BACKGROUND)
        fx, fy, fw, fh = self._focus_box()
        self.focus = self.webview.create_window(
            f"{name} Voice", url=self.app.launch_url + "&view=focus", width=fw, height=fh, x=fx, y=fy,
            resizable=False, frameless=True, easy_drag=False, on_top=True, hidden=True, transparent=True,
            shadow=False, focus=False, background_color=BACKGROUND)
        self.focus.events.closing += lambda: self._quitting
        for overlay in (self.orb, self.focus):
            overlay.events.loaded += lambda: self._later(self._settle_overlays)
        self.main.events.closing += self._on_main_closing
        self.main.events.shown += lambda: self._visible(True)
        self.main.events.restored += lambda: self._visible(True)
        self.main.events.minimized += self._on_minimized
        self.orb.events.closing += self._on_orb_closing
        self.app.window_handler = self.handle
        self.app.dialog_handler = self.dialog
        self.app.ui_visible = not self.background

    def _orb_position(self) -> tuple[int | None, int | None]:
        try:
            screen = self.webview.screens[0]
            return int(screen.width) - ORB_SIZE - 40, int(screen.height) - ORB_SIZE - 80
        except Exception:
            return None, None

    def _focus_box(self) -> tuple[int | None, int | None, int, int]:
        """The voice view's window: a square for the orb plus room for the caption, in the middle of the screen."""
        try:
            screen = self.webview.screens[0]
            sw, sh = int(screen.width), int(screen.height)
        except Exception:
            return None, None, 640, 640 + FOCUS_CAPTION
        side = max(420, min(760, int(min(sw, sh) * 0.6)))
        side = min(side, max(300, sh - FOCUS_CAPTION - 40))
        width, height = side, side + FOCUS_CAPTION
        return (sw - width) // 2, max(0, (sh - height) // 2 - 20), width, height

    def run(self) -> None:
        self.create()
        storage = self.app.paths.cache / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        gui = "edgechromium" if sys.platform == "win32" else None
        self.webview.start(gui=gui, debug=bool(self.app.settings.ui.debug_devtools), private_mode=False,
                           storage_path=str(storage), icon=app_icon())

    # ------------------------------------------------------------------ events (GUI thread: never block)
    def _visible(self, value: bool) -> None:
        self.app.ui_visible = value
        if value:              # the main window is back (e.g. from the taskbar): it has the orb, the others step aside
            self._later(self._put_overlays_away)

    def _put_overlays_away(self) -> None:
        with self._lock:
            self._drop_focus()
            if self.app.ui_visible:
                with contextlib.suppress(Exception):
                    self.orb.hide()

    def _on_minimized(self) -> None:
        self._visible(False)
        if self.app.settings.ui.mini_orb:
            self._later(self._show_orb)

    def _on_main_closing(self) -> bool:
        if self._quitting:
            return True
        ui = self.app.settings.ui
        if ui.mini_orb or ui.always_active:
            # Keep running in the background (still listening for the wake word).
            self._later(self.to_mini)
            return False
        self._later(self.quit)
        return False

    def _on_orb_closing(self) -> bool:
        return self._quitting

    @staticmethod
    def _later(fn: Callable[[], Any]) -> None:
        # pywebview window calls must not run on the GUI thread from inside its own event handlers.
        threading.Thread(target=fn, name="jarvis-window", daemon=True).start()

    # ------------------------------------------------------------------ actions (/api/window/{action})
    def handle(self, action: str) -> bool:
        actions = {"show": self.to_full, "full": self.to_full, "hide": self.to_mini, "mini": self.to_mini,
                   "toggle": self.toggle, "reveal": self.reveal, "quit": self.quit,
                   "computer_on": self.computer_on, "computer_off": self.computer_off,
                   "computer_hide": self._hide_all, "computer_show": self.to_full,
                   "focus_show": self._apply_focus, "focus_hide": self._apply_focus}
        fn = actions.get(action)
        if fn is None:
            return False
        if action.startswith("focus_"):
            self._focus_wanted = action == "focus_show"   # set now: the threads may run out of order
        self._later(fn)
        return True

    def dialog(self, request: dict[str, Any]) -> list[str] | None:
        """The native Open / Folder / Save dialog over the main window (called from a worker thread; it blocks
        until the user picks or cancels). The paths picked, or None."""
        if not self.app.ui_visible:
            self.to_full()                     # the dialog belongs to a window the user can see
        kinds = getattr(self.webview, "FileDialog", None)
        dialog_type = {"open": getattr(kinds, "OPEN", 10), "folder": getattr(kinds, "FOLDER", 20),
                       "save": getattr(kinds, "SAVE", 30)}[request["kind"]]
        result = self.main.create_file_dialog(
            dialog_type, directory=request.get("directory") or "", allow_multiple=bool(request.get("multiple")),
            save_filename=request.get("save_name") or "", file_types=tuple(request.get("file_types") or ()))
        if not result:
            return None
        if isinstance(result, str):
            return [result]
        return [str(p) for p in result]

    def _apply_focus(self) -> None:
        """Show or hide the voice view as its page last asked; only while the mini orb alone is on screen."""
        with self._lock:
            want = (self._focus_wanted and not self.app.ui_visible and not self._computer_mode
                    and bool(getattr(self.app.settings.ui, "focus_orb", True)))
            if want == self._focus_up:
                return
            self._focus_up = want
            with contextlib.suppress(Exception):
                if want:
                    keep_off_taskbar(self.focus)
                    self.focus.show()
                else:
                    self.focus.hide()
            if self.app.settings.ui.mini_orb:          # the small orb steps aside while the big one is out
                with contextlib.suppress(Exception):
                    if want:
                        self.orb.hide()
                    else:
                        self.orb.show()

    def _settle_overlays(self) -> None:
        """A page of the orb or the voice view loaded, and pywebview showed its window: off the taskbar, and each
        shown only if JARVIS wants it now."""
        with self._lock:
            orb_wanted = (not self.app.ui_visible and bool(self.app.settings.ui.mini_orb) and not self._computer_mode
                          and not self._focus_up)
            for window, wanted in ((self.orb, orb_wanted), (self.focus, self._focus_up)):
                restyled = keep_off_taskbar(window)
                with contextlib.suppress(Exception):
                    if restyled or not wanted:
                        window.hide()
                    if wanted:
                        window.show()

    def _drop_focus(self) -> None:
        """The main window or computer control takes over: put the voice view away (lock held)."""
        if self._focus_up:
            self._focus_up = False
            with contextlib.suppress(Exception):
                self.focus.hide()

    def to_full(self) -> None:
        with self._lock:
            self._drop_focus()
            with contextlib.suppress(Exception):
                self.main.show()
                self.main.restore()
            with contextlib.suppress(Exception):
                self.orb.hide()
            self._visible(True)

    def to_mini(self) -> None:
        with self._lock:
            with contextlib.suppress(Exception):
                self.main.hide()
            self._visible(False)
            if self.app.settings.ui.mini_orb:
                self._show_orb()

    def _show_orb(self) -> None:
        with contextlib.suppress(Exception):
            keep_off_taskbar(self.orb)
            self.orb.show()

    def _hide_all(self) -> None:
        """Hide both windows (the orb too), so JARVIS is not on screen while it controls the computer."""
        with self._lock:
            self._drop_focus()
            for window in (self.main, self.orb):
                with contextlib.suppress(Exception):
                    window.hide()
            self._visible(False)

    def computer_on(self) -> None:
        """Enter computer-control: remember whether the main window was up, then hide everything."""
        if not self._computer_mode:
            self._computer_mode = True
            self._resume_full = self.app.ui_visible
        self._hide_all()

    def computer_off(self) -> None:
        """Leave computer-control: put JARVIS back the way it was."""
        if not self._computer_mode:
            return
        self._computer_mode = False
        if self._resume_full:
            self.to_full()
        else:
            self.to_mini()

    def reveal(self) -> None:
        """A voice command opened a screen: bring the main window up if only the orb is on screen."""
        if not self.app.ui_visible:
            self.to_full()

    def toggle(self) -> None:
        if self.app.ui_visible:
            self.to_mini()
        else:
            self.to_full()

    def quit(self) -> None:
        with self._lock:
            if self._quitting:
                return
            self._quitting = True
        self.app.window_handler = None
        self.app.dialog_handler = None
        self.core.stop()                       # JARVIS shuts down cleanly (tasks saved as interrupted)
        for window in (self.focus, self.orb, self.main):
            with contextlib.suppress(Exception):
                window.destroy()


APP_USER_MODEL_ID = "JARVIS"    # the same id as the Start-menu shortcut (packaging/jarvis.iss) and the notifications


def set_app_user_model_id() -> bool:
    """Windows groups a program's windows with its Start-menu shortcut and pinned taskbar button only when
    the process carries the shortcut's AppUserModelID; without it JARVIS would get a second taskbar button."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID) == 0)
    except Exception:
        return False


def run_desktop(make_app: Callable[[], Any], *, background: bool = False) -> int:
    set_app_user_model_id()
    core = CoreThread(make_app)
    core.start()
    while not core.ready.wait(0.5):
        if not core.is_alive():
            break
    if core.error is not None or core.app is None:
        log.error("JARVIS could not start", exc_info=core.error)
        show_error(f"JARVIS לא הצליח לעלות:\n{core.error}\n\nJARVIS could not start. The log file has the details.")
        return 1
    core.app.manage_autostart = True
    with contextlib.suppress(Exception):
        core.submit(core.app.apply_autostart())     # the sign-in entry follows the setting (and this copy)
    try:
        import webview
    except ImportError:
        webview = None  # type: ignore[assignment]
    if webview is not None and webview2_available():
        shell = DesktopShell(webview, core, background=background)
        try:
            shell.run()
        except Exception:
            log.exception("The desktop window failed; JARVIS keeps running in the browser")
            return _run_without_window(core, background=background)
        finally:
            if not shell._quitting:
                core.stop()
        return 0
    log.warning("pywebview or the WebView2 runtime is missing: opening JARVIS as an Edge app window")
    return _run_without_window(core, background=background)


def _run_without_window(core: CoreThread, *, background: bool = False) -> int:
    """No native window: an Edge app window (or the browser); JARVIS runs until told to quit (or Ctrl+C)."""
    app = core.app
    edge = find_edge()

    def open_ui() -> bool:
        app.ui_visible = True              # an Edge or browser window cannot report that it was closed
        if edge:
            profile = app.paths.cache / "edge-app"
            subprocess.Popen([edge, f"--app={app.launch_url}", f"--user-data-dir={profile}", "--no-first-run",
                              f"--window-size={app.settings.ui.window_width},{app.settings.ui.window_height}"],
                             close_fds=True)
        else:
            webbrowser.open(app.launch_url)
        return True

    def handle(action: str) -> bool:
        if action == "quit":
            core.request_stop()
            return True
        if action in ("show", "full", "toggle"):
            return open_ui()
        if action == "reveal":             # a voice command opened a screen while nothing was on screen
            return app.ui_visible or open_ui()
        return False

    app.window_handler = handle
    app.ui_visible = not background
    if not background:
        open_ui()
    print(f"JARVIS is running: {app.launch_url}\nPress Ctrl+C here to quit.", flush=True)
    try:
        while not core.finished.wait(0.5):
            pass
    except KeyboardInterrupt:
        core.stop()
    return 0
