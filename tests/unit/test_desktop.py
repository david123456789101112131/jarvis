"""The desktop shell's logic with a stand-in window toolkit: start-up, mini orb, quit, API window actions."""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.config.settings import Settings
from jarvis.ui.desktop import APP_USER_MODEL_ID, CoreThread, DesktopShell, app_icon, set_app_user_model_id

ROOT = Path(__file__).resolve().parents[2]


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, fn: Any) -> FakeEvent:
        self.handlers.append(fn)
        return self

    def fire(self) -> bool:
        """Like pywebview: True when some handler returned False (= cancel)."""
        return any(fn() is False for fn in self.handlers)


class FakeWindow:
    def __init__(self, title: str, **kw: Any) -> None:
        self.title, self.kw = title, kw
        self.visible = not kw.get("hidden", False)
        self.destroyed = False
        self.events = SimpleNamespace(closing=FakeEvent(), shown=FakeEvent(), restored=FakeEvent(),
                                      minimized=FakeEvent())

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def restore(self) -> None:
        self.visible = True

    def destroy(self) -> None:
        self.destroyed = True
        self.visible = False


class FakeWebview:
    def __init__(self) -> None:
        self.windows: list[FakeWindow] = []
        self.screens = [SimpleNamespace(width=1920, height=1080)]
        self.started = threading.Event()

    def create_window(self, title: str, **kw: Any) -> FakeWindow:
        w = FakeWindow(title, **kw)
        self.windows.append(w)
        return w

    def start(self, **kw: Any) -> None:
        self.start_kw = kw
        self.started.set()


class FakeApp:
    def __init__(self, tmp: Path) -> None:
        self.settings = Settings()
        self.paths = SimpleNamespace(cache=tmp)
        self.launch_url = "http://127.0.0.1:9999/?launch=t"
        self.window_handler = None
        self.ui_visible = False
        self.started = self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        await asyncio.sleep(0.01)
        self.stopped = True


def eventually(check: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def make_shell(tmp_path: Path) -> tuple[DesktopShell, FakeWebview, CoreThread, FakeApp]:
    app = FakeApp(tmp_path)
    core = CoreThread(lambda: app)
    core.start()
    assert core.ready.wait(5) and core.error is None
    web = FakeWebview()
    shell = DesktopShell(web, core)
    shell.run()
    return shell, web, core, app


def test_core_thread_runs_the_app_until_stopped(tmp_path: Path) -> None:
    app = FakeApp(tmp_path)
    core = CoreThread(lambda: app)
    core.start()
    assert core.ready.wait(5) and app.started and not app.stopped
    core.stop()
    assert app.stopped and core.finished.is_set()


def test_core_thread_reports_a_failed_start(tmp_path: Path) -> None:
    class Broken(FakeApp):
        async def start(self) -> None:
            raise RuntimeError("port in use")

    app = Broken(tmp_path)
    core = CoreThread(lambda: app)
    core.start()
    assert core.ready.wait(5)
    assert isinstance(core.error, RuntimeError) and app.stopped


def test_windows_and_orb_position(tmp_path: Path) -> None:
    _shell, web, core, app = make_shell(tmp_path)
    main, orb = web.windows
    assert main.kw["url"] == app.launch_url and main.visible
    assert orb.kw["url"].endswith("&view=orb") and not orb.visible and orb.kw["on_top"] and orb.kw["frameless"]
    assert (orb.kw["x"], orb.kw["y"]) == (1920 - 260 - 40, 1080 - 260 - 80)
    assert web.start_kw["private_mode"] is False and Path(web.start_kw["storage_path"]).is_dir()
    assert app.window_handler is not None and app.ui_visible
    core.stop()


def test_closing_the_window_keeps_jarvis_as_the_mini_orb(tmp_path: Path) -> None:
    _shell, web, core, app = make_shell(tmp_path)
    main, orb = web.windows
    assert main.events.closing.fire() is True                 # the close is cancelled...
    eventually(lambda: orb.visible and not main.visible)      # ...and JARVIS shrinks to the orb
    assert app.ui_visible is False and not app.stopped
    assert app.window_handler("show") is True                 # double-click on the orb → /api/window/show
    eventually(lambda: main.visible and not orb.visible)
    assert app.ui_visible is True
    assert app.window_handler("toggle")
    eventually(lambda: orb.visible)
    assert app.window_handler("explode") is False
    core.stop()


def test_computer_mode_hides_jarvis_and_restores_it(tmp_path: Path) -> None:
    _shell, web, core, app = make_shell(tmp_path)
    main, orb = web.windows
    eventually(lambda: main.visible)
    assert app.window_handler("computer_on") is True          # a session starts: JARVIS gets out of the way
    eventually(lambda: not main.visible and not orb.visible)
    assert app.ui_visible is False
    assert app.window_handler("computer_show") is True        # an approval needs to be shown
    eventually(lambda: main.visible)
    assert app.window_handler("computer_hide") is True        # then JARVIS hides again to act
    eventually(lambda: not main.visible)
    assert app.window_handler("computer_off") is True         # the session ends: JARVIS comes back as it was
    eventually(lambda: main.visible)
    core.stop()


def test_quit_shuts_jarvis_down_and_closes_the_windows(tmp_path: Path) -> None:
    _shell, web, core, app = make_shell(tmp_path)
    main, orb = web.windows
    assert app.window_handler("quit") is True
    eventually(lambda: main.destroyed and orb.destroyed)
    assert app.stopped and core.finished.is_set()
    assert main.events.closing.fire() is False                # closing now really closes


def test_without_mini_orb_closing_quits(tmp_path: Path) -> None:
    _shell, web, _core, app = make_shell(tmp_path)
    app.settings.ui.mini_orb = False
    app.settings.ui.always_active = False
    main, orb = web.windows
    main.events.closing.fire()
    eventually(lambda: app.stopped and main.destroyed)
    assert not orb.visible


def test_the_app_icon_has_every_size_windows_asks_for() -> None:
    image = pytest.importorskip("PIL.Image")
    icon = app_icon()
    assert icon is not None and icon.endswith("jarvis.ico")
    with image.open(icon) as ico:
        assert {(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (256, 256)} <= ico.info["sizes"]
    from jarvis.host.windows import _find_app_icon

    assert (_find_app_icon() or "").endswith("jarvis.png")                    # the notifications' icon


def test_windows_sees_one_app_the_shortcuts_the_window_and_the_notifications() -> None:
    from jarvis.host.windows import WindowsHost

    installer = (ROOT / "packaging" / "jarvis.iss").read_text(encoding="utf-8-sig")
    shortcut_ids = re.findall(r'AppUserModelID: "([^"]+)"', installer)
    assert shortcut_ids and set(shortcut_ids) == {APP_USER_MODEL_ID} == {WindowsHost.toast_app_id}
    assert set_app_user_model_id() is (sys.platform == "win32")


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_the_process_carries_the_app_id() -> None:
    import ctypes
    from ctypes import wintypes

    assert set_app_user_model_id()
    value = wintypes.LPWSTR()
    assert ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(value)) == 0
    try:
        assert value.value == APP_USER_MODEL_ID
    finally:
        ctypes.windll.ole32.CoTaskMemFree(value)
