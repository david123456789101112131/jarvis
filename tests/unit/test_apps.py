"""The "Apps & tools" screen: what is possible per environment, the lists, opening an app, one look at the
screen, a suggested step and doing it, all through the real executor gate (permissions, approvals, audit).

No real screen, model or GPU is touched: a fake backend reports windows and records every click, a scripted
model proposes steps, and the user's answers go through the confirmation broker."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from fakes import FakeHost
from jarvis.common.errors import ToolError
from jarvis.computer.backend import Shot, Target, UnsupportedBackend
from jarvis.computer.model import Conversation, HoloClient, ModelError, Step
from jarvis.integrations.apps import AppsRequestError, AppsService
from jarvis.jobs import Job, JobManager
from jarvis.tools.apps import OpenAppTool, OpenUrlTool, RunningAppsTool
from jarvis.tools.computer import ComputerActTool
from jarvis.tools.step_tools import STEP_TOOLS, ScreenStepParams, changed, matches

PNG = b"\x89PNG-fake-pixels-of-the-screen"
INPUT = ("click", "type", "press_keys", "scroll", "drag")


def _target(**kw: Any) -> Target:
    base = {"hwnd": 100, "pid": 10, "process": "notepad.exe", "title": "Untitled - Notepad", "class_name": "Notepad"}
    return Target(**{**base, **kw})


class FakeWatch:
    def __init__(self, on_double: Any) -> None:
        self.on_double = on_double
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeBackend:
    """Windows as JARVIS sees it: the focused window and element, what is under a point, and every input."""

    def __init__(self, *, focus: Target | None = None, at: Target | None = None) -> None:
        self.focus = focus or _target(element="Text editor", control="document")
        self.at = at or _target(element="Save", control="button")
        self.calls: list[tuple[Any, ...]] = []
        self.watch: FakeWatch | None = None
        self.brings = True

    def problem(self) -> str | None:
        return None

    def screen_size(self) -> tuple[int, int]:
        return (1920, 1080)

    def capture(self, max_width: int) -> Shot:
        self.calls.append(("capture",))
        return Shot(PNG, 1280, 720, 1920, 1080)

    def target_at(self, x: int, y: int) -> Target:
        self.calls.append(("target_at", x, y))
        return self.at

    def focus_target(self) -> Target:
        self.calls.append(("focus",))
        return self.focus

    def input_state(self) -> tuple[int, int, int]:
        return (1, 0, 0)

    def own_windows_visible(self) -> bool:
        return False

    def working_window(self) -> int:
        return 100

    def find_window(self, title: str) -> int:
        return 100 if title.lower() in ("notepad", "untitled - notepad") else 0

    def bring_to_front(self, hwnd: int, *, restore: bool = False) -> bool:
        self.calls.append(("front", hwnd, restore))
        return self.brings

    def move(self, x: int, y: int) -> None: ...

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> None:
        self.calls.append(("click", x, y, button, count))

    def scroll(self, x: Any, y: Any, direction: str, amount: int) -> None:
        self.calls.append(("scroll", direction, amount))

    def drag(self, x: int, y: int, to_x: int, to_y: int) -> None:
        self.calls.append(("drag", x, y, to_x, to_y))

    def type_text(self, text: str, stop: Any = None) -> bool:
        self.calls.append(("type", text))
        return True

    def press_keys(self, combos: Any) -> bool:
        self.calls.append(("press_keys", tuple(map(tuple, combos))))
        return True

    def watch_escape(self, on_double: Any) -> FakeWatch:
        self.watch = FakeWatch(on_double)
        return self.watch


def acted(backend: FakeBackend) -> list[tuple[Any, ...]]:
    return [c for c in backend.calls if c[0] in INPUT]


class FakeHolo:
    """The local model: scripted next steps and a fixed description."""

    def __init__(self, steps: list[Step] | None = None, description: str = "Notepad, an empty document",
                 fail: str = "") -> None:
        self.steps = list(steps or [])
        self.description = description
        self.fail = fail
        self.convs: list[Conversation] = []
        self.described: list[tuple[str, tuple[str, ...]]] = []

    async def step(self, conv: Conversation) -> Step:
        self.convs.append(conv)
        if self.fail:
            raise ModelError(self.fail)
        return self.steps.pop(0)

    async def describe(self, shot: Shot, *, lang: str = "en", notes: Any = ()) -> str:
        self.described.append((lang, tuple(notes)))
        if self.fail:
            raise ModelError(self.fail)
        return self.description

    async def aclose(self) -> None: ...


class FakeModelServer:
    def __init__(self) -> None:
        self.running = False
        self.base_url, self.api_key = "http://127.0.0.1:1", "key"

    def problem(self) -> str | None:
        return None

    async def ensure_running(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def stop_if_idle(self) -> bool:
        return False

    def touch(self) -> None: ...


def _click(element: str = "Save", x: int = 500, y: int = 500, text: str = "I will save the file") -> Step:
    return Step(content=text, tool="click", arguments={"element": element, "x": x, "y": y}, call_id="c1")


def _type(text: str, enter: bool = False) -> Step:
    return Step(content="typing", tool="type", arguments={"text": text, "press_enter": enter}, call_id="t1")


class Env:
    """The apps service on the test kit: real executor, permissions, broker and jobs; fake screen and model."""

    def __init__(self, kit: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, backend: Any = None,
                 model: bool = True, holo: FakeHolo | None = None, enabled: bool = True, server_url: str = "",
                 platform: str = "windows") -> None:
        from jarvis.computer import service as svc
        from jarvis.security.process import ProcessRegistry

        self.kit = kit
        self.shell: list[str] = []
        self.answered: set[str] = set()
        self.holo = holo or FakeHolo()
        monkeypatch.setattr(svc, "HoloClient", lambda *a, **k: self.holo)
        settings = kit.settings.computer
        settings.enabled, settings.server_url, settings.settle_s = enabled, server_url, 0.0
        self.backend = backend if backend is not None else FakeBackend()
        self.computer = svc.ComputerService(settings, SimpleNamespace(models=tmp_path, logs=tmp_path),  # type: ignore[arg-type]
                                            ProcessRegistry(), self._window, backend=self.backend)
        if model:
            self.computer.server = FakeModelServer()      # type: ignore[assignment]
        kit.services.extras["computer"] = self.computer
        self.host = FakeHost()
        self.host.platform = platform                   # type: ignore[misc]
        kit.services.host = self.host
        for tool in (*STEP_TOOLS, ComputerActTool, OpenAppTool, OpenUrlTool, RunningAppsTool):
            kit.registry.register(tool())
        self.history = tmp_path / "jobs.jsonl"
        self.jobs = JobManager(kit.bus, self.history)
        self.app = SimpleNamespace(computer=self.computer, host=self.host, executor=kit.executor, broker=kit.broker,
                                   jobs=self.jobs, lang="en")
        self.apps = AppsService(self.app)  # type: ignore[arg-type]

    async def _window(self, action: str) -> bool:
        self.shell.append(action)
        return True

    async def request(self, timeout: float = 5.0) -> Any:
        """The next approval JARVIS asks for (as the dialog would show it), not one already answered."""
        for _ in range(int(timeout / 0.01)):
            pending = [r for r in self.kit.broker.pending() if r.id not in self.answered]
            if pending:
                return pending[0]
            await asyncio.sleep(0.01)
        raise AssertionError("no approval was asked for")

    async def answer(self, approved: bool = True, *, remember: bool = False) -> Any:
        req = await self.request()
        assert self.kit.broker.respond(req.id, approved, remember=remember)
        self.answered.add(req.id)
        return req

    async def done(self, job: Job) -> Job:
        finished = await self.jobs.wait(job.id, 10)
        assert finished is not None and finished.status != "running"
        return finished

    async def close(self) -> None:
        await self.jobs.aclose()
        await self.computer.aclose()


@pytest.fixture
async def make(kit: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    made: list[Env] = []

    def build(**kw: Any) -> Env:
        env = Env(kit, tmp_path, monkeypatch, **kw)
        made.append(env)
        return env

    yield build
    for env in made:
        await env.close()


# ============================================================================ what is possible here
class TestStatus:
    def _ways(self, env: Env) -> dict[str, dict[str, Any]]:
        return {w["key"]: w for w in env.apps.status("en")["ways"]}

    def test_on_windows_with_the_model_everything_but_connectors_is_real(self, make: Any) -> None:
        env = make()
        status = env.apps.status("en")
        ways = self._ways(env)
        assert all(ways[k]["available"] for k in ("launch", "windows", "uia", "vision"))
        assert not ways["connectors"]["available"] and "no official connector" in ways["connectors"]["reason"]
        assert "nothing is sent" in ways["vision"]["detail"]
        assert [s["key"] for s in status["stop"]] == ["stop_button", "esc_twice", "kill_switch"]
        assert "background" in status["note"] and "one screenshot" in status["note"]
        assert any("UIPI" in line for line in status["limits"])
        assert any("password fields" in line for line in status["limits"])

    def test_elsewhere_nothing_on_the_screen_is_offered(self, make: Any) -> None:
        env = make(backend=UnsupportedBackend(), platform="posix")
        ways = self._ways(env)
        for key in ("launch", "windows", "uia", "vision"):
            assert not ways[key]["available"] and "Windows" in ways[key]["reason"], key
            assert ways[key]["needs"] == ["Run JARVIS on a Windows computer"]

    def test_without_the_model_only_the_facts_are_real(self, make: Any) -> None:
        env = make(model=False)
        ways = self._ways(env)
        assert ways["uia"]["available"] and not ways["vision"]["available"]
        assert "not installed" in ways["vision"]["reason"]
        assert any("jarvis computer install" in line for line in ways["vision"]["needs"])

    def test_computer_control_turned_off(self, make: Any) -> None:
        env = make(enabled=False)
        ways = self._ways(env)
        assert not ways["uia"]["available"] and not ways["vision"]["available"]
        assert "turned off" in ways["uia"]["reason"] and "computer.enabled" in ways["uia"]["needs"][0]
        assert ways["launch"]["available"]                 # opening an app is not computer control
        assert env.apps.status("en")["computer_enabled"] is False

    def test_a_model_server_elsewhere_is_named(self, make: Any) -> None:
        env = make(server_url="http://192.168.1.20:8080")
        assert "sent to the model server http://192.168.1.20:8080" in self._ways(env)["vision"]["detail"]

    def test_hebrew(self, make: Any) -> None:
        env = make(enabled=False)
        status = env.apps.status("he")
        assert "כבויה" in {w["key"]: w for w in status["ways"]}["uia"]["reason"]
        assert status["note"].startswith("שום דבר לא קורה ברקע")


# ============================================================================ the lists
class TestListing:
    async def test_installed_apps_with_search_in_both_languages(self, make: Any) -> None:
        env = make()
        everything = await env.apps.listing(lang="en")
        assert {a["name"] for a in everything["installed"]} == {"Google Chrome", "Notepad", "Calculator"}
        assert all(a["can"] == ["open"] for a in everything["installed"])
        assert [a["name"] for a in (await env.apps.listing("note", lang="en"))["installed"]] == ["Notepad"]
        assert [a["name"] for a in (await env.apps.listing("כרום", lang="en"))["installed"]] == ["Google Chrome"]

    async def test_refresh_rebuilds_the_list(self, make: Any) -> None:
        env = make()
        asked: list[bool] = []
        original = env.host.installed_apps

        async def installed(refresh: bool = False) -> Any:
            asked.append(refresh)
            return await original(refresh)

        env.host.installed_apps = installed               # type: ignore[method-assign]
        await env.apps.listing(refresh=True, lang="en")
        assert asked == [True]

    async def test_open_windows_leave_jarvis_out_and_say_what_can_be_done(self, make: Any) -> None:
        import os

        env = make()

        async def running() -> list[dict[str, Any]]:
            return [{"name": "notepad.exe", "pid": 10, "title": "Untitled - Notepad", "windows": 1},
                    {"name": "python.exe", "pid": os.getpid(), "title": "JARVIS", "windows": 2},
                    {"name": "jarvis.exe", "pid": 77, "title": "JARVIS Orb", "windows": 1},
                    {"name": "1password.exe", "pid": 30, "title": "1Password", "windows": 1}]

        env.host.running_apps = running                   # type: ignore[method-assign]
        windows = (await env.apps.listing(lang="en"))["windows"]
        assert [w["program"] for w in windows] == ["notepad.exe", "1password.exe"]
        assert windows[0]["can"] == ["look", "suggest"] and windows[0]["window"] == "Untitled - Notepad"
        assert windows[1]["can"] == [] and "password manager" in windows[1]["note"]

    async def test_without_the_model_a_window_can_only_be_looked_at(self, make: Any) -> None:
        env = make(model=False)

        async def running() -> list[dict[str, Any]]:
            return [{"name": "notepad.exe", "pid": 10, "title": "Untitled - Notepad", "windows": 1}]

        env.host.running_apps = running                   # type: ignore[method-assign]
        assert (await env.apps.listing(lang="en"))["windows"][0]["can"] == ["look"]

    def test_search_matches_names_aliases_and_catalog(self) -> None:
        app = SimpleNamespace(name="Visual Studio Code", aliases=("vscode",), id="vscode")
        assert matches(app, "studio") and matches(app, "VSCODE") and matches(app, "") and not matches(app, "word")


# ============================================================================ opening an app
class TestOpen:
    async def test_opens_through_the_executor(self, make: Any) -> None:
        env = make()
        job = await env.apps.open_app("Notepad", lang="en")
        assert job.kind == "apps.open" and job.area == "apps"
        done = await env.done(job)
        assert done.status == "done", done.error
        assert ("launch", "Notepad", ()) in env.host.calls
        assert done.result and done.result["summary"] == "Opened Notepad" and done.result["pid"] == 4242

    async def test_an_unknown_app_fails_in_words(self, make: Any) -> None:
        env = make()
        done = await env.done(await env.apps.open_app("Photoshop", lang="en"))
        assert done.status == "failed" and "couldn't find an installed app called 'Photoshop'" in (done.error or "")
        assert not [c for c in env.host.calls if c[0] == "launch"]

    async def test_the_same_click_twice_runs_once(self, make: Any) -> None:
        env = make()
        first, second = await env.apps.open_app("Notepad", lang="en"), await env.apps.open_app("Notepad", lang="en")
        assert first.id == second.id
        await env.done(first)


# ============================================================================ one look
class TestLook:
    async def test_asks_first_and_sees_nothing_before_the_approval(self, make: Any) -> None:
        env = make()
        job = await env.apps.look(lang="en")
        req = await env.request()
        assert req.tool == "read_screen" and req.agent_id == "system" and req.risk == "high"
        assert not req.always_ask and not req.irreversible and req.task_id == job.params["task_id"]
        assert any("one screenshot" in r for r in req.reasons)
        assert any("not saved to disk" in r for r in req.reasons)
        assert any("nothing is sent" in r for r in req.reasons)
        assert ("capture",) not in env.backend.calls and env.shell == []        # nothing looked at yet
        env.kit.broker.respond(req.id, True)
        done = await env.done(job)
        assert done.status == "done", done.error
        look = done.result["look"]  # type: ignore[index]
        assert look["analysed"] == "model" and look["description"] == "Notepad, an empty document"
        assert look["facts"]["program"] == "notepad.exe" and look["facts"]["element"] == "Text editor"
        assert look["screen"] == {"width": 1920, "height": 1080}
        assert env.backend.calls.count(("capture",)) == 1 and acted(env.backend) == []
        assert env.shell[0] == "computer_on" and env.shell[-1] == "computer_off"    # hidden, then back
        assert ("front", 100, False) in env.backend.calls    # the window the user was working in, in front
        assert [s.status for s in done.steps] == ["done", "done"]

    async def test_a_chosen_window_comes_to_the_front_restored(self, make: Any) -> None:
        env = make()
        job = await env.apps.look(window="Notepad", lang="en")
        req = await env.answer()
        assert "Notepad" in str(req.details)
        assert (await env.done(job)).status == "done"
        assert ("front", 100, True) in env.backend.calls

    async def test_a_window_that_closed_is_said_so(self, make: Any) -> None:
        env = make()
        done = await env.done(await env.apps.look(window="Paint", lang="en"))
        assert done.status == "failed" and "no longer open" in (done.error or "")
        assert env.kit.broker.pending() == [] and ("capture",) not in env.backend.calls

    async def test_without_the_model_only_facts_and_no_screenshot(self, make: Any) -> None:
        env = make(model=False)
        job = await env.apps.look(lang="en")
        req = await env.answer()
        assert any("no screenshot" in r for r in req.reasons)
        done = await env.done(job)
        look = done.result["look"]  # type: ignore[index]
        assert look["analysed"] == "facts" and look["description"] is None and look["screen"] is None
        assert ("capture",) not in env.backend.calls
        assert done.sends == []

    async def test_the_approval_is_remembered_within_the_task_only(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[_click()]))
        first = await env.apps.look(lang="en")
        req = await env.answer(remember=True)
        assert req.rememberable
        task_id = (await env.done(first)).params["task_id"]
        again = await env.done(await env.apps.look(task_id=task_id, lang="en"))      # no second question
        assert again.status == "done" and env.kit.broker.history[-1].decided_by == "remembered"
        suggested = await env.done(await env.apps.suggest("save the file", task_id=task_id, lang="en"))
        assert suggested.status == "done"                                           # the same look, remembered
        other = await env.apps.look(lang="en")                                      # another task asks again
        assert other.params["task_id"] != task_id
        await env.answer(False)
        assert (await env.done(other)).status == "cancelled"
        env.apps.end_task(task_id)                                                  # and an ended task forgets it
        ended = await env.apps.look(task_id=task_id, lang="en")
        await env.answer(False)
        assert (await env.done(ended)).status == "cancelled"

    async def test_nothing_is_written_to_disk_or_kept(self, make: Any, tmp_path: Path, jarvis_home: Path) -> None:
        env = make()
        job = await env.apps.look(lang="en")
        await env.answer()
        done = await env.done(job)
        assert done.status == "done"
        encoded = base64.b64encode(PNG)
        for root in (tmp_path, jarvis_home):
            for path in root.rglob("*"):
                if path.is_file():
                    content = path.read_bytes()
                    assert PNG not in content and encoded not in content, path
        history = env.history.read_text(encoding="utf-8")
        assert "Untitled - Notepad" not in history and "an empty document" not in history   # only the summary
        assert "Looked at the screen" in history
        view = str(done.view())
        assert encoded.decode() not in view and "PNG" not in view

    async def test_a_no_stops_it_and_nothing_is_seen(self, make: Any) -> None:
        env = make()
        job = await env.apps.look(lang="en")
        await env.answer(False)
        done = await env.done(job)
        assert done.status == "cancelled"
        assert done.steps[0].status == "skipped" and "did not approve" in done.steps[0].detail
        assert env.backend.calls == [] and env.shell == []

    async def test_a_password_manager_in_front_is_not_looked_into(self, make: Any) -> None:
        env = make(backend=FakeBackend(focus=_target(process="1password.exe", title="1Password")))
        job = await env.apps.look(lang="en")
        await env.answer()
        look = (await env.done(job)).result["look"]  # type: ignore[index]
        assert "password manager" in look["blocked"] and look["description"] is None
        assert ("capture",) not in env.backend.calls

    async def test_a_model_that_fails_still_gives_the_facts(self, make: Any) -> None:
        env = make(holo=FakeHolo(fail="could not reach the computer-control model"))
        job = await env.apps.look(lang="en")
        await env.answer()
        done = await env.done(job)
        look = done.result["look"]  # type: ignore[index]
        assert done.status == "done" and look["description"] is None and "could not reach" in look["model_error"]
        assert look["facts"]["program"] == "notepad.exe"

    async def test_a_correction_reaches_the_next_look_and_suggestion(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[_click()]))
        task = env.apps.correct("this is the Save As dialog, not the main window", lang="en")["task"]
        job = await env.apps.look(task_id=task["id"], lang="en")
        await env.answer()
        await env.done(job)
        assert env.holo.described == [("en", ("this is the Save As dialog, not the main window",))]
        suggested = await env.apps.suggest("save it", task_id=task["id"], lang="en")
        await env.answer()
        await env.done(suggested)
        assert "Save As dialog" in env.holo.convs[0].messages[1]["content"]
        with pytest.raises(AppsRequestError):
            env.apps.correct("   ", lang="en")

    async def test_stopping_while_the_approval_waits(self, make: Any) -> None:
        env = make()
        job = await env.apps.look(lang="en")
        await env.request()
        env.jobs.cancel(job.id)
        done = await env.done(job)
        assert done.status == "cancelled" and env.kit.broker.pending() == []
        assert env.backend.calls == []


# ============================================================================ a suggested step
class TestSuggest:
    async def _suggest(self, env: Env, goal: str = "save the file") -> Job:
        job = await env.apps.suggest(goal, lang="en")
        await env.answer()
        return await env.done(job)

    async def test_it_does_nothing_and_says_what_it_would_do(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[_click()]))
        done = await self._suggest(env)
        assert done.status == "done", done.error
        p = done.result["proposal"]  # type: ignore[index]
        assert p["kind"] == "action" and p["words"] == "Click 'Save' in Untitled - Notepad"
        assert p["action"] == {"action": "click", "element": "Save", "x": 960, "y": 540}
        assert p["risk"] == "low" and p["refused"] is None and p["preview"] is None and not p["asks_again"]
        assert p["understood"] == "I will save the file" and p["can_do"] is True
        assert ("target_at", 960, 540) in env.backend.calls
        assert acted(env.backend) == []                                    # nothing was done
        goal = env.holo.convs[0].messages[1]["content"]
        assert goal.startswith("Your task: save the file") and "single next action" in goal

    async def test_a_password_field_is_refused(self, make: Any) -> None:
        env = make(backend=FakeBackend(focus=_target(element="Password", control="edit", password=True)),
                   holo=FakeHolo(steps=[_type("hunter2", enter=True)]))
        done = await self._suggest(env, "sign in")
        p = done.result["proposal"]  # type: ignore[index]
        assert "password field" in p["refused"] and p["can_do"] is False
        assert env.apps.task(done.params["task_id"]).suggestions == {}        # nothing to do from it
        with pytest.raises(AppsRequestError):
            await env.apps.step(done.id, lang="en")

    async def test_send_is_previewed_and_asked_again(self, make: Any) -> None:
        env = make(backend=FakeBackend(at=_target(process="whatsapp.exe", title="WhatsApp", element="Send",
                                                  control="button")),
                   holo=FakeHolo(steps=[_click("Send")]))
        p = (await self._suggest(env, "send the message")).result["proposal"]  # type: ignore[index]
        assert p["risk"] == "high" and p["rule"] == "send" and p["asks_again"] is True
        assert p["preview"].startswith("What will happen: Click 'Send' in WhatsApp") and "sends" in p["preview"]

    async def test_an_irreversible_purchase_says_it_cannot_be_undone(self, make: Any) -> None:
        env = make(backend=FakeBackend(at=_target(process="chrome.exe", title="Shop - Chrome", element="Buy now",
                                                  control="button")),
                   holo=FakeHolo(steps=[_click("Buy now")]))
        p = (await self._suggest(env, "buy it")).result["proposal"]  # type: ignore[index]
        assert p["irreversible"] is True and "cannot be undone" in p["preview"]

    async def test_a_sign_in_screen_is_refused(self, make: Any) -> None:
        env = make(backend=FakeBackend(at=_target(process="credentialuibroker.exe", title="Windows Security",
                                                  element="OK", control="button")),
                   holo=FakeHolo(steps=[_click("OK")]))
        p = (await self._suggest(env, "approve")).result["proposal"]  # type: ignore[index]
        assert "sign-in" in p["refused"] and p["can_do"] is False

    async def test_a_model_answer_is_shown_as_such(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[Step(content="", tool="answer", arguments={"content": "It is already saved."},
                                             call_id="a")]))
        p = (await self._suggest(env)).result["proposal"]  # type: ignore[index]
        assert p == {**p, "kind": "answer", "answer": "It is already saved.", "can_do": False}

    async def test_needs_the_model(self, make: Any) -> None:
        env = make(model=False)
        done = await env.done(await env.apps.suggest("save the file", lang="en"))
        assert done.status == "failed" and "needs the local model" in (done.error or "")
        assert env.kit.broker.pending() == [] and env.backend.calls == []

    async def test_needs_a_task(self, make: Any) -> None:
        env = make()
        with pytest.raises(AppsRequestError):
            await env.apps.suggest(" ", lang="en")


# ============================================================================ doing the step
class TestStep:
    async def _suggested(self, env: Env, goal: str = "save the file") -> Job:
        job = await env.apps.suggest(goal, lang="en")
        await env.answer()
        done = await env.done(job)
        assert done.status == "done", done.error
        return done

    async def test_asked_every_time_then_checked_and_done_through_computer_act(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[_click()]))
        suggestion = await self._suggested(env)
        env.shell.clear()
        job = await env.apps.step(suggestion.id, lang="en")
        req = await env.request()
        assert req.tool == "do_screen_step" and req.always_ask and not req.rememberable
        assert req.summary == "Click 'Save' in Untitled - Notepad"
        assert req.details["Window"] == "Untitled - Notepad" and req.details["Element"] == "Save"
        assert any("same window and element" in r for r in req.reasons)
        assert acted(env.backend) == [] and env.shell == []                  # nothing before the approval
        env.kit.broker.respond(req.id, True)
        done = await env.done(job)
        assert done.status == "done", done.error
        assert acted(env.backend) == [("click", 960, 540, "left", 1)]
        assert env.shell[0] == "computer_on" and env.shell[-1] == "computer_off"
        assert ("front", 100, True) in env.backend.calls
        assert env.computer.active is None                                   # the one-step session is over
        assert env.apps.task(done.params["task_id"]).done == ["Click 'Save' in Untitled - Notepad"]
        assert any(e.get("tool") == "computer_act" for e in _audit(env))     # through the gate, audited

    async def test_a_changed_screen_does_nothing(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[_click()]))
        suggestion = await self._suggested(env)
        env.backend.at = _target(hwnd=555, pid=11, process="chrome.exe", title="News", element="Ad")
        job = await env.apps.step(suggestion.id, lang="en")
        await env.answer()
        done = await env.done(job)
        assert done.status == "failed" and done.error == "The screen changed; ask for a new suggestion"
        assert acted(env.backend) == []

    async def test_another_element_in_the_same_window_is_a_change_too(self) -> None:
        params = ScreenStepParams(action="click", x=1, y=1, window_id=100, window_pid=10, expect_element="Save",
                                  expect_control="button")
        assert changed(params, _target(element="Save", control="button")) is None
        assert changed(params, _target(element="Delete", control="button")) == "element"
        assert changed(params, _target(hwnd=101, element="Save", control="button")) == "window"
        assert changed(params, _target(element="Save", control="button", password=True)) == "element"

    async def test_a_no_does_nothing(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[_click()]))
        suggestion = await self._suggested(env)
        env.shell.clear()
        job = await env.apps.step(suggestion.id, lang="en")
        await env.answer(False)
        done = await env.done(job)
        assert done.status == "cancelled" and done.steps[0].status == "skipped"
        assert acted(env.backend) == [] and env.shell == []

    async def test_the_rules_ask_again_for_a_send_and_a_no_stops_it(self, make: Any) -> None:
        env = make(backend=FakeBackend(at=_target(process="whatsapp.exe", title="WhatsApp", element="Send",
                                                  control="button")),
                   holo=FakeHolo(steps=[_click("Send")]))
        suggestion = await self._suggested(env, "send it")
        job = await env.apps.step(suggestion.id, lang="en")
        first = await env.answer(True)
        assert first.tool == "do_screen_step"
        assert any("ask you once more" in r for r in first.reasons)
        second = await env.answer(False)                       # computer_act's own question, on the live screen
        assert second.tool == "computer_act" and second.risk == "high"
        assert any("sends, publishes or shares" in r for r in second.reasons)
        done = await env.done(job)
        assert done.status == "cancelled" and acted(env.backend) == []
        assert done.steps[1].status == "skipped" and "did not approve" in done.steps[1].detail

    async def test_a_suggestion_is_used_once(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[_click()]))
        suggestion = await self._suggested(env)
        job = await env.apps.step(suggestion.id, lang="en")
        assert (await env.apps.step(suggestion.id, lang="en")).id == job.id   # the same click twice: one job
        await env.answer()
        await env.done(job)
        with pytest.raises(AppsRequestError):
            await env.apps.step(suggestion.id, lang="en")
        with pytest.raises(AppsRequestError):
            await env.apps.step("job_nope", lang="en")

    async def test_stop_while_it_waits(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[_click()]))
        suggestion = await self._suggested(env)
        job = await env.apps.step(suggestion.id, lang="en")
        await env.request()
        env.jobs.cancel(job.id)
        assert (await env.done(job)).status == "cancelled"
        assert acted(env.backend) == [] and env.kit.broker.pending() == []

    async def test_esc_twice_stops_it_while_jarvis_is_hidden(self, make: Any) -> None:
        env = make(backend=FakeBackend(at=_target(process="whatsapp.exe", title="WhatsApp", element="Send",
                                                  control="button")),
                   holo=FakeHolo(steps=[_click("Send")]))
        suggestion = await self._suggested(env, "send it")
        job = await env.apps.step(suggestion.id, lang="en")
        await env.answer(True)
        await env.request()                                    # the second question is up: the step is under way
        assert env.backend.watch is not None
        env.backend.watch.on_double()                          # the user pressed Esc twice
        done = await env.done(job)
        assert done.status == "cancelled" and acted(env.backend) == []
        assert env.backend.watch.stopped and env.computer.active is None

    async def test_the_kill_switch_stops_it(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[_click()]))
        suggestion = await self._suggested(env)
        job = await env.apps.step(suggestion.id, lang="en")
        await env.request()
        await env.kit.kill_switch.engage("test")               # the gate notices it while the approval waits
        done = await env.done(job)
        assert done.status == "cancelled" and done.message == "Stopped by the kill switch"
        assert acted(env.backend) == []

    async def test_typing_reaches_only_the_field_it_was_proposed_for(self, make: Any) -> None:
        env = make(holo=FakeHolo(steps=[_type("hello")]))
        suggestion = await self._suggested(env, "write hello")
        job = await env.apps.step(suggestion.id, lang="en")
        await env.answer()
        done = await env.done(job)
        assert done.status == "done", done.error
        assert acted(env.backend) == [("type", "hello")]
        assert done.title == "Do the step: Type in notepad"                 # the typed text stays out of the history

    async def test_a_password_field_is_never_typed_into_even_when_asked_directly(self, kit: Any, make: Any) -> None:
        env = make()
        res = await kit.executor.execute("do_screen_step", {"action": "type", "text": "hunter2", "window_id": 100,
                                                            "window_pid": 10, "expect_password": True},
                                         agent_id="system", task_id="t1")
        assert not res.ok and res.denied == "permission" and "password" in (res.error or "")
        assert kit.broker.pending() == [] and acted(env.backend) == []

    async def test_computer_act_outside_the_step_is_refused(self, kit: Any, make: Any) -> None:
        env = make()
        res = await kit.executor.execute("computer_act", {"action": "click", "x": 1, "y": 1}, agent_id="system",
                                         task_id="t1")
        assert not res.ok and res.denied == "permission" and acted(env.backend) == []


def _audit(env: Env) -> list[dict[str, Any]]:
    import json

    rows: list[dict[str, Any]] = []
    for path in env.kit.paths.logs.glob("audit*"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


# ============================================================================ the pieces underneath
class TestScreenTurn:
    async def test_busy_while_a_session_runs(self, make: Any) -> None:
        env = make()
        env.computer._active = object()                         # type: ignore[assignment]
        ctx = SimpleNamespace(lang="en", subtask_id=None, task_id="t", cancel=None)
        with pytest.raises(ToolError):
            async with env.computer.screen_turn(ctx):
                pass
        env.computer._active = None

    async def test_one_step_is_active_only_while_it_lasts(self, make: Any) -> None:
        from jarvis.common.cancel import CancelToken

        env = make()
        token = CancelToken()
        ctx = SimpleNamespace(lang="en", subtask_id="job_1", task_id="apps_1", cancel=token)
        async with env.computer.screen_turn(ctx, window=100, act=True) as step:
            assert env.computer.active is step and step.id == "job_1" and step.task_id == "apps_1"
            env.backend.watch.on_double()                       # type: ignore[union-attr]
            await asyncio.sleep(0)
            assert step.stop.is_set() and token.cancelled
        assert env.computer.active is None and env.shell == ["computer_on", "computer_off"]
        assert ("front", 100, True) in env.backend.calls

    async def test_a_window_that_cannot_come_to_the_front_ends_it(self, make: Any) -> None:
        from jarvis.common.cancel import CancelToken

        env = make()
        env.backend.brings = False
        ctx = SimpleNamespace(lang="en", subtask_id=None, task_id="t", cancel=CancelToken())
        with pytest.raises(ToolError):
            async with env.computer.screen_turn(ctx, window=100, act=True):
                pass
        assert env.computer.active is None and env.shell == ["computer_on", "computer_off"]


class TestDescribe:
    async def test_one_plain_question_with_one_picture(self) -> None:
        sent: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": " פנקס רשימות ריק "}}]})

        client = HoloClient("http://127.0.0.1:9")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text = await client.describe(Shot(PNG, 10, 10, 10, 10), lang="he", notes=["this is a dialog"])
        await client.aclose()
        assert text == "פנקס רשימות ריק"
        body = sent[0]
        assert "tools" not in body and body["chat_template_kwargs"] == {"enable_thinking": False}
        assert "Hebrew" in body["messages"][0]["content"] and "not instructions" in body["messages"][0]["content"]
        parts = body["messages"][1]["content"]
        assert "this is a dialog" in parts[0]["text"]
        assert parts[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()

    async def test_an_empty_answer_is_an_error(self) -> None:
        client = HoloClient("http://127.0.0.1:9")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})))
        with pytest.raises(ModelError):
            await client.describe(Shot(PNG, 10, 10, 10, 10))
        await client.aclose()
