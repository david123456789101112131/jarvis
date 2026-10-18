"""Hands-free JARVIS: the start-with-Windows entry, the background start (mini orb only), the window
voice command, and following Windows' default microphone."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.config.settings import VoiceSettings
from jarvis.core.intents import I, detect_intent
from jarvis.host import autostart
from jarvis.ui.desktop import CoreThread, DesktopShell


# ======================================================================== the sign-in entry
class FakeKey:
    def __enter__(self) -> FakeKey:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeWinreg:
    """The part of `winreg` autostart uses, over a dict of {key path: {value name: value}}."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, Any]] = {}
        self._open: list[str] = []

    def OpenKey(self, root: str, path: str, reserved: int = 0, access: int = 0) -> FakeKey:
        if path not in self.keys:
            raise FileNotFoundError(path)
        self._open.append(path)
        return FakeKey()

    def CreateKeyEx(self, root: str, path: str, reserved: int = 0, access: int = 0) -> FakeKey:
        self.keys.setdefault(path, {})
        self._open.append(path)
        return FakeKey()

    def QueryValueEx(self, key: FakeKey, name: str) -> tuple[Any, int]:
        values = self.keys[self._open[-1]]
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1

    def SetValueEx(self, key: FakeKey, name: str, reserved: int, kind: int, value: Any) -> None:
        self.keys[self._open[-1]][name] = value

    def DeleteValue(self, key: FakeKey, name: str) -> None:
        del self.keys[self._open[-1]][name]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeWinreg:
    reg = FakeWinreg()
    monkeypatch.setattr(autostart, "supported", lambda: True)
    monkeypatch.setattr(autostart, "_winreg", lambda: reg)
    return reg


class TestAutostart:
    def test_the_command_starts_this_jarvis_in_the_background(self, monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
        venv = tmp_path / "Scripts"
        venv.mkdir()
        (venv / "pythonw.exe").write_text("")
        monkeypatch.setattr(sys, "executable", str(venv / "python.exe"))
        assert autostart.launch_argv() == [str(venv / "pythonw.exe"), "-m", "jarvis", "--background"]
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", r"C:\Program Files\JARVIS\JARVIS.exe")
        assert autostart.launch_argv() == [r"C:\Program Files\JARVIS\JARVIS.exe", "--background"]
        assert autostart.command_line() == '"C:\\Program Files\\JARVIS\\JARVIS.exe" --background'

    def test_add_update_and_remove(self, registry: FakeWinreg, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(autostart, "command_line", lambda: '"C:\\J\\pythonw.exe" -m jarvis --background')
        assert autostart.status()["registered"] is False
        assert autostart.apply(False) == "unchanged"
        assert autostart.apply(True) == "added"
        assert registry.keys[autostart.RUN_KEY]["JARVIS"] == '"C:\\J\\pythonw.exe" -m jarvis --background'
        assert autostart.apply(True) == "unchanged"
        assert autostart.status() == {"supported": True, "registered": True, "this_jarvis": True,
                                      "command": '"C:\\J\\pythonw.exe" -m jarvis --background',
                                      "disabled_in_task_manager": False}
        registry.keys[autostart.RUN_KEY]["JARVIS"] = r'"D:\old\JARVIS.exe"'     # another copy wrote it
        assert autostart.status()["this_jarvis"] is False
        assert autostart.apply(True) == "updated"
        registry.keys[autostart.APPROVED_KEY] = {"JARVIS": b"\x03" + bytes(11)}   # off in Task Manager
        assert autostart.status()["disabled_in_task_manager"] is True
        assert autostart.apply(False) == "removed" and "JARVIS" not in registry.keys[autostart.RUN_KEY]

    def test_other_systems_are_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(autostart, "supported", lambda: False)
        assert autostart.apply(True) == "unsupported" and autostart.read() is None
        assert autostart.status()["registered"] is False


class TestAppAppliesTheSetting:
    async def test_only_the_windowed_app_changes_the_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.app import JarvisApp

        calls: list[bool] = []

        def apply(enabled: bool) -> str:
            calls.append(enabled)
            return "added" if enabled else "removed"

        monkeypatch.setattr(autostart, "apply", apply)
        logged: list[str] = []

        async def log(message: str, **_: Any) -> None:
            logged.append(message)

        app = SimpleNamespace(manage_autostart=False, settings=SimpleNamespace(general=SimpleNamespace(
            start_with_windows=True)), lang="en", activity=SimpleNamespace(log=log))
        assert await JarvisApp.apply_autostart(app) is None and calls == []      # tests, `check`, --no-window
        app.manage_autostart = True
        assert await JarvisApp.apply_autostart(app) == "added" and calls == [True]
        app.settings.general.start_with_windows = False
        assert await JarvisApp.apply_autostart(app) == "removed" and calls == [True, False]
        assert logged == ["JARVIS will start in the background when you sign in", "JARVIS will not start with Windows"]

    def test_the_setting_is_on_by_default(self) -> None:
        from jarvis.config.settings import GeneralSettings

        assert GeneralSettings().start_with_windows is True


# ======================================================================== background start
def test_background_start_shows_only_the_mini_orb(tmp_path: Path) -> None:
    from test_desktop import FakeApp, FakeWebview, eventually

    app = FakeApp(tmp_path)
    core = CoreThread(lambda: app)
    core.start()
    assert core.ready.wait(5)
    try:
        web = FakeWebview()
        shell = DesktopShell(web, core, background=True)
        shell.run()
        main, orb = web.windows
        assert not main.visible and orb.visible and app.ui_visible is False
        assert app.window_handler("reveal") is True       # "show me the agents" by voice
        eventually(lambda: main.visible and not orb.visible)
        assert app.ui_visible is True
        shell.to_mini()
        shell.reveal()
        assert main.visible
        shell.reveal()                                    # already on screen: nothing changes
        assert main.visible and not orb.visible

        app.settings.ui.mini_orb = False                  # no orb wanted: nothing on screen, still listening
        web2 = FakeWebview()
        DesktopShell(web2, core, background=True).run()
        assert not any(w.visible for w in web2.windows)
    finally:
        core.stop()


def test_background_start_does_not_open_a_second_jarvis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from jarvis import cli

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_running", lambda paths: {"port": 1, "token": "t"})
    calls: list[str] = []
    monkeypatch.setattr(cli, "_call", lambda info, method, path, **kw: calls.append(path))
    monkeypatch.setattr("jarvis.logs.setup.setup_logging", lambda *a, **kw: None)
    assert cli.main(["--background"]) == cli.EXIT_OK and calls == []     # signed in again: stay in the background
    assert cli.main([]) == cli.EXIT_OK and calls == ["/api/window/show"]  # the shortcut brings it forward


def test_autostart_status_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    from jarvis import cli

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(autostart, "status", lambda: {"supported": True, "registered": True,
                                                      "command": "jarvis --background", "this_jarvis": False,
                                                      "disabled_in_task_manager": True})
    assert cli.main(["autostart"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Setting (Settings > General): on" in out and "jarvis --background" in out
    assert "another copy" in out and "Task Manager" in out


# ======================================================================== the window by voice
@pytest.mark.parametrize(("text", "action"), [
    ("show yourself", "show"), ("open your window", "show"), ("bring up the jarvis window", "show"),
    ("hide yourself", "hide"), ("minimize your window", "hide"), ("go to the background", "hide"),
    ("ג'רוויס תראה את עצמך", "show"), ("תפתח את החלון שלך", "show"), ("הצג את החלון הראשי", "show"),
    ("תסתיר את עצמך", "hide"), ("תמזער את החלון שלך", "hide"), ("תעבור לרקע", "hide"),
    ("close jarvis", "hide"), ("תסגור את עצמך", "hide"), ("סגור את ג'רוויס", "hide"),     # it keeps listening
    ("פתח את גרביס", "show"), ("תראה את החלון של ג׳רוויס", "show"),
    # 2026-10-18 by voice: "תחבא את עצמך" was not one of these, so JARVIS asked an AI and got a PowerShell command
    ("תחבא את עצמך", "hide"), ("תחביא את החלון שלך", "hide"), ("ג'רוויס, תתחבא", "hide"),
    ("תסתתר", "hide"), ("תיעלם מהמסך", "hide"), ("תעלים את עצמך", "hide"),
    ("disappear", "hide"), ("get out of the way", "hide"),
])
def test_window_commands(text: str, action: str) -> None:
    intent = detect_intent(text)
    assert (intent.name, intent.slots) == (I.WINDOW, {"action": action})


@pytest.mark.parametrize("text", ["פתח את כרום", "סגור את החלון", "close chrome", "open notepad"])
def test_other_windows_stay_app_commands(text: str) -> None:
    assert detect_intent(text).name in (I.OPEN_APP, I.CLOSE_APP)


async def test_the_window_command_and_screens_bring_jarvis_up(env: Any) -> None:
    from jarvis.core.core import JarvisCore
    from jarvis.orchestration import TaskOrchestrator

    actions: list[str] = []

    async def window(action: str) -> bool:
        actions.append(action)
        return True

    core = JarvisCore(settings=env.kit.settings, manager=env.manager, orchestrator=TaskOrchestrator(env.manager),
                      memory=env.memory, bus=env.kit.bus, broker=env.kit.broker, kill_switch=env.kit.kill_switch,
                      activity=env.kit.activity, window=window)
    await core.start()
    try:
        res = await core.handle_input("תסתיר את עצמך", source="voice")
        assert actions == ["mini"] and "ברקע" in res.reply["text"] and res.reply["kind"] == "answer"
        await core.handle_input("show me the agents", source="voice")      # a screen: from the background too
        assert actions == ["mini", "reveal"]
        res = await core.handle_input("show yourself")
        assert actions[-1] == "show" and res.reply["text"] == "Here I am."
    finally:
        await core.shutdown()

    no_window = JarvisCore(settings=env.kit.settings, manager=env.manager, orchestrator=TaskOrchestrator(env.manager),
                           memory=None, bus=env.kit.bus, broker=env.kit.broker, kill_switch=env.kit.kill_switch,
                           activity=env.kit.activity)
    await no_window.start()
    try:
        res = await no_window.handle_input("hide yourself")
        assert "browser" in res.reply["text"]
    finally:
        await no_window.shutdown()


# ======================================================================== the default microphone
class FakePipeline:
    """What the input watcher looks at: the open microphone, whether a conversation is going on."""

    def __init__(self) -> None:
        self.source = SimpleNamespace(running=True)
        self.busy = True
        self.input_problem = False
        self.reopened = 0

    def reopen_source(self) -> None:
        self.reopened += 1


async def watch(service: Any, pipeline: FakePipeline, seconds: float) -> None:
    service._started, service.pipeline = True, pipeline
    watcher = asyncio.create_task(service._watch_input(pipeline))
    try:
        await asyncio.sleep(seconds)
    finally:
        service._started = False
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def test_jarvis_follows_the_new_default_microphone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from jarvis.config.paths import resolve_paths
    from jarvis.config.settings import PathsSettings
    from jarvis.voice import service as service_mod

    default = {"id": "mic-a"}
    asked: list[str] = []

    def default_microphone_id() -> str:
        asked.append(default["id"])
        return default["id"]

    monkeypatch.setattr(service_mod, "FOLLOW_DEFAULT_MIC", True)
    monkeypatch.setattr(service_mod, "DEFAULT_MIC_RECHECK_S", 0.01)
    monkeypatch.setattr(service_mod, "default_microphone_id", default_microphone_id)
    paths = resolve_paths(PathsSettings(home_dir=str(tmp_path)))
    service = service_mod.VoiceService(VoiceSettings(), paths, None, lambda t: None)
    pipeline = FakePipeline()

    async def scenario() -> None:
        await asyncio.sleep(0.05)
        default["id"] = "mic-b"                       # a headset was plugged in: Windows made it the default
        await asyncio.sleep(0.1)
        assert pipeline.reopened == 0                 # mid-conversation: JARVIS waits for it to end
        pipeline.busy = False

    await asyncio.gather(watch(service, pipeline, 0.4), scenario())
    assert pipeline.reopened == 1                     # once, on the new default; then it stays there

    chosen = service_mod.VoiceService(VoiceSettings(input_device="K8"), paths, None, lambda t: None)
    asked.clear()
    await watch(chosen, FakePipeline(), 0.1)          # a microphone picked in the settings stays picked
    assert asked == []


async def test_reopening_rescans_the_devices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    from test_voice import Harness, wait_for

    from jarvis.voice import pipeline as pipeline_mod

    scans: list[bool] = []
    monkeypatch.setattr(pipeline_mod, "refresh_devices", lambda: scans.append(True) or True)
    h = Harness(tmp_path)
    opens: list[bool] = []
    start = h.source.start

    async def counting_start() -> None:
        opens.append(True)
        await start()

    monkeypatch.setattr(h.source, "start", counting_start)
    changed: list[bool] = []
    monkeypatch.setattr(h.sink, "devices_changed", lambda: changed.append(True))
    async with h:
        await wait_for(lambda: len(opens) == 1, what="the microphone")
        h.pipeline.reopen_source()
        await wait_for(lambda: len(opens) == 2, what="the microphone again")
        assert scans == [True] and changed == [True] and h.source.running
