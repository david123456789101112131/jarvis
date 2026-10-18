"""Computer control: key parsing, the safety policy, the model conversation, the session loop and the
two tools through the real executor gate. No real screen, model or GPU is touched — a fake backend and a
scripted model stand in, so these run on any platform."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from jarvis.common.enums import Capability, RiskLevel
from jarvis.computer.backend import Shot, Target, parse_keys
from jarvis.computer.model import TOOL_NAMES, Conversation, Step, to_pixels
from jarvis.computer.policy import assess, is_jarvis_title, is_run_box, pending_after, window_label
from jarvis.computer.session import ComputerSession, Outcome
from jarvis.tools.base import ToolContext, ToolResult

C = Capability


# ============================================================================ parse_keys
class TestParseKeys:
    def test_combos(self) -> None:
        assert parse_keys("ctrl+s") == [["ctrl", "s"]]
        assert parse_keys("down down enter") == [["down"], ["down"], ["enter"]]
        assert parse_keys("ctrl++") == [["ctrl", "+"]]
        assert parse_keys("ctrl+alt+del") == [["ctrl", "alt", "delete"]]
        assert parse_keys("WIN") == [["win"]]

    @pytest.mark.parametrize("bad", ["ctrl+", "foo", "ש", ""])
    def test_rejects_unknown(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_keys(bad)

    def test_too_many(self) -> None:
        with pytest.raises(ValueError):
            parse_keys(" ".join(["a"] * 20))


# ============================================================================ policy
def _target(**kw: Any) -> Target:
    base = dict(hwnd=100, pid=10, process="notepad.exe", title="Untitled - Notepad", class_name="Notepad")
    base.update(kw)
    return Target(**base)


class TestPolicy:
    def test_click_plain_button_is_low(self) -> None:
        v = assess("click", _target(element="Format", control="menu item"), element="Format")
        assert v.risk == RiskLevel.LOW and v.refused is None

    def test_click_send_is_high(self) -> None:
        v = assess("click", _target(element="Send", control="button"), element="Send")
        assert v.risk == RiskLevel.HIGH and v.rule == "send"

    def test_buy_is_irreversible(self) -> None:
        v = assess("click", _target(element="Buy now", control="button"))
        assert v.risk == RiskLevel.HIGH and v.irreversible

    def test_hebrew_delete_vs_research(self) -> None:
        assert assess("click", _target(element="מחק")).risk == RiskLevel.HIGH
        assert assess("click", _target(element="מחקר")).risk == RiskLevel.LOW   # word boundary, not substring

    def test_own_window_refused(self) -> None:
        v = assess("click", _target(title="JARVIS", own=True))
        assert v.refused is not None

    def test_password_field_refused(self) -> None:
        v = assess("type", _target(control="edit", password=True), text="hunter2")
        assert v.refused is not None

    def test_secret_in_text_refused(self) -> None:
        v = assess("type", _target(control="edit"), text="sk-ant-api03-" + "x" * 40)
        assert v.refused is not None

    def test_ctrl_alt_del_refused(self) -> None:
        v = assess("press_keys", _target(), combos=[["ctrl", "alt", "delete"]])
        assert v.refused is not None

    def test_type_enter_in_chat_sends(self) -> None:
        chat = _target(process="whatsapp.exe", title="WhatsApp", control="edit")
        v = assess("type", chat, text="hello", press_enter=True)
        assert v.risk == RiskLevel.HIGH and v.rule == "send"

    def test_command_line_enter_is_judged(self) -> None:
        term = _target(process="powershell.exe", title="Windows PowerShell", class_name="ConsoleWindowClass")
        safe = assess("type", term, text="Get-Process", press_enter=True)
        assert safe.risk == RiskLevel.MEDIUM
        danger = assess("type", term, text="Remove-Item -Recurse C:\\data", press_enter=True)
        assert danger.risk >= RiskLevel.HIGH and danger.irreversible

    def test_pending_command_carries_across_calls(self) -> None:
        assert pending_after("", "Remove-Item", press_enter=False) == "Remove-Item"
        assert pending_after("Remove-Item", " x", press_enter=True) == ""
        term = _target(process="cmd.exe", class_name="ConsoleWindowClass")
        v = assess("press_keys", term, combos=[["enter"]], pending_command="del /s C:\\x")
        assert v.risk >= RiskLevel.HIGH

    def test_window_label(self) -> None:
        assert window_label(_target(class_name="Progman", title=""), "en") == "the desktop"
        assert window_label(_target(title="Notepad"), "en") == "Notepad"


class TestInvisibleMarks:
    """Hebrew Windows wraps window titles in bidi marks (U+202A…U+202C); a page can hide zero-width
    characters or vowel points inside a word. The rules must still see the words."""

    def test_plain_and_strip_marks(self) -> None:
        from jarvis.computer.backend import plain, strip_marks

        assert plain("\u202aClaude\u202c") == "Claude"
        assert plain("\u200f  Windows \u200b Security ") == "Windows Security"
        assert strip_marks("a\u200bb\nc\u2066d\u2069") == "ab\ncd"
        assert plain("שְׁלַח") == "שלח"
        assert plain("ｓｅｎｄ") == "send"

    def test_titles_with_bidi_marks(self) -> None:
        assert is_jarvis_title("\u202aJARVIS\u202c")
        run_box = _target(process="explorer.exe", class_name="#32770", title="\u202bהפעלה\u202c")
        assert is_run_box(run_box)
        v = assess("type", run_box, text="format c: /q", press_enter=True)
        assert v.risk >= RiskLevel.HIGH and v.rule == "command"
        security = _target(process="mmc.exe", title="\u202bאבטחת\u200f Windows\u202c")
        assert assess("click", security).refused
        own = _target(process="msedge.exe", class_name="Chrome_WidgetWin_1", title="\u202aJARVIS\u202c")
        assert own.own is False and window_label(own) == "JARVIS"

    def test_hidden_characters_in_a_button_name(self) -> None:
        assert assess("click", _target(element="Se\u200bnd", control="button")).rule == "send"
        assert assess("click", _target(element="שְׁלַח", control="button")).rule == "send"
        assert assess("click", _target(element="\u202aPay now\u202c", control="button")).irreversible

    def test_hidden_characters_in_a_typed_command(self) -> None:
        term = _target(process="powershell.exe", title="Windows PowerShell", class_name="ConsoleWindowClass")
        v = assess("type", term, text="Remove-Item\u200b -Recurse C:\\data", press_enter=True)
        assert v.risk >= RiskLevel.HIGH and v.irreversible

    def test_win32_title_is_cleaned(self) -> None:
        from types import SimpleNamespace

        from jarvis.computer import windows as win

        class User32:
            def GetWindowTextLengthW(self, hwnd: int) -> int:
                return 8

            def GetWindowTextW(self, hwnd: int, buf: Any, size: int) -> int:
                buf.value = "\u202aClaude\u202c"
                return 8

        assert win._text(SimpleNamespace(user32=User32()), 1) == "Claude"   # type: ignore[arg-type]


# ============================================================================ model conversation
class TestConversation:
    def _shot(self) -> Shot:
        return Shot(png=b"\x89PNG", width=1280, height=720, screen_width=1920, screen_height=1080)

    def test_to_pixels(self) -> None:
        assert to_pixels(0, 1920) == 0
        assert to_pixels(500, 1920) == 960
        assert to_pixels(1000, 1920) == 1919      # clamped to the last pixel
        assert to_pixels(None, 1920) == 0

    def test_evicts_old_images(self) -> None:
        conv = Conversation("do a thing")
        for _ in range(5):
            conv.observe(self._shot())
        images = [m for m in conv.messages if isinstance(m.get("content"), list)]
        assert len(images) == 3                   # only the last three keep their picture
        evicted = [m for m in conv.messages if m.get("content") == "[screenshot evicted]"]
        assert len(evicted) == 2

    def test_record_drops_reasoning(self) -> None:
        conv = Conversation("x")
        step = Step(content="clicking start", tool="click", arguments={"x": 1, "y": 2}, call_id="c1",
                    reasoning="long private chain of thought")
        conv.record(step)
        last = conv.messages[-1]
        assert last["role"] == "assistant"
        assert "chain of thought" not in str(last)
        assert last["tool_calls"][0]["function"]["name"] == "click"

    def test_answer_is_a_tool(self) -> None:
        assert "answer" in TOOL_NAMES


# ============================================================================ a fake backend + client
class FakeWatch:
    def __init__(self, on_double: Any) -> None:
        self.on_double = on_double
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeBackend:
    def __init__(self, states: list[tuple[int, int, int]] | None = None,
                 target: Target | None = None, keys_reach: bool = True) -> None:
        self._states = states or [(1, 0, 0)]
        self._i = 0
        self.target = target or _target()
        self.keys_reach = keys_reach         # False: the focus moved away while typing
        self.front = 0                       # what working_window() reports
        self.brings = True                   # what bring_to_front() answers
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.watch: FakeWatch | None = None

    def problem(self) -> str | None:
        return None

    def screen_size(self) -> tuple[int, int]:
        return (1920, 1080)

    def capture(self, max_width: int) -> Shot:
        self.calls.append(("capture", ()))
        return Shot(png=b"img", width=1280, height=720, screen_width=1920, screen_height=1080)

    def target_at(self, x: int, y: int) -> Target:
        return self.target

    def focus_target(self) -> Target:
        return self.target

    def input_state(self) -> tuple[int, int, int]:
        value = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        return value

    def own_windows_visible(self) -> bool:
        return False

    def working_window(self) -> int:
        return self.front

    def bring_to_front(self, hwnd: int) -> bool:
        self.calls.append(("front", (hwnd,)))
        return self.brings

    def move(self, x: int, y: int) -> None: ...
    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> None:
        self.calls.append(("click", (x, y, button, count)))

    def scroll(self, x: Any, y: Any, direction: str, amount: int) -> None:
        self.calls.append(("scroll", (direction, amount)))

    def drag(self, x: int, y: int, to_x: int, to_y: int) -> None:
        self.calls.append(("drag", (x, y, to_x, to_y)))

    def type_text(self, text: str, stop: Any = None) -> bool:
        self.calls.append(("type", (text,)))
        return self.keys_reach

    def press_keys(self, combos: Any) -> bool:
        self.calls.append(("press_keys", (tuple(map(tuple, combos)),)))
        return self.keys_reach

    def watch_escape(self, on_double: Any) -> FakeWatch:
        self.watch = FakeWatch(on_double)
        return self.watch


class FakeClient:
    """Returns pre-scripted steps; an optional hook runs before each one (to simulate the user)."""

    def __init__(self, steps: list[Step], hook: Any = None) -> None:
        self.steps = steps
        self.hook = hook
        self.i = 0

    async def step(self, conv: Conversation) -> Step:
        if self.hook is not None:
            self.hook(self.i)
        step = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        return step

    async def aclose(self) -> None: ...


class FakeService:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.active: Any = None

    async def ensure_covered(self) -> None: ...


class _StubSession:
    """A minimal stand-in for an active session (what computer_act reads of it)."""

    def __init__(self) -> None:
        import threading

        self.id = "sub_stub"
        self.pending: dict[int, str] = {}
        self.stop = threading.Event()


class FakeCtx:
    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self.subtask_id = "sub_1"
        self.task_id = "task_1"
        self.lang = "en"
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def check_cancelled(self) -> None: ...
    async def progress(self, fraction: float, message: str = "") -> None: ...
    async def call(self, tool: str, /, **params: Any) -> ToolResult:
        self.calls.append((tool, params))
        return self.results.get(tool, ToolResult(ok=True, summary=f"{tool} ok"))


def _answer(text: str = "done") -> Step:
    return Step(content="", tool="answer", arguments={"content": text}, call_id="a")


def _click(x: int = 500, y: int = 500) -> Step:
    return Step(content="clicking", tool="click", arguments={"element": "thing", "x": x, "y": y}, call_id="c")


def _session(backend: FakeBackend, client: FakeClient, ctx: FakeCtx, **kw: Any) -> ComputerSession:
    service = FakeService(backend)
    s = ComputerSession("goal", client=client, backend=backend, ctx=ctx, service=service,  # type: ignore[arg-type]
                        max_steps=kw.get("max_steps", 10), max_minutes=kw.get("max_minutes", 5.0),
                        settle_s=0.0, lang="en", stop_on_input=kw.get("stop_on_input", False))
    service.active = s
    return s


# ============================================================================ the session loop
class TestSession:
    async def test_answer_ends_the_session(self) -> None:
        backend = FakeBackend()
        ctx = FakeCtx()
        out = await _session(backend, FakeClient([_click(), _answer("all done")]), ctx).run()
        assert out.reason == "answer" and out.content == "all done"
        assert ("computer_act", {"action": "click", "x": 960, "y": 540, "session_id": "sub_1",
                                 "element": "thing"}) in [(t, p) for t, p in ctx.calls]

    async def test_touching_the_mouse_does_not_stop_it(self) -> None:
        # The user moves the mouse between JARVIS's actions: by default only Esc twice stops a session.
        backend = FakeBackend(states=[(1, 0, 0), (2, 400, 400), (3, 10, 10), (4, 800, 90)])
        session = _session(backend, FakeClient([_click(), _click(), _answer("finished")]), FakeCtx())
        out = await session.run()
        assert out.reason == "answer" and out.content == "finished"
        assert backend._i == 0                     # the input state is not even read

    async def test_takeover_stops_when_turned_on(self) -> None:
        # baseline, step0 check (same), step0 re-baseline, step1 check (changed -> takeover)
        backend = FakeBackend(states=[(1, 0, 0), (1, 0, 0), (1, 0, 0), (2, 400, 400)])
        out = await _session(backend, FakeClient([_click(), _click(), _answer()]), FakeCtx(),
                             stop_on_input=True).run()
        assert out.reason == "takeover" and out.stopped_by_user

    async def test_ending_the_session_stops_typing_under_way(self) -> None:
        # A cancelled task (the kill switch) ends the loop; the stop flag then halts a typing thread at once.
        session = _session(FakeBackend(), FakeClient([_click()]), FakeCtx())
        task = asyncio.ensure_future(session.run())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.stop.is_set()

    async def test_escape_twice_stops(self) -> None:
        backend = FakeBackend()
        ctx = FakeCtx()

        def hook(i: int) -> None:
            if i == 1 and backend.watch is not None:
                backend.watch.on_double()          # the user pressed Esc twice before step 1
        out = await _session(backend, FakeClient([_click(), _click(), _answer()], hook=hook), ctx).run()
        assert out.reason == "user_stop"
        assert backend.watch is not None and backend.watch.stopped   # the watcher is always stopped

    async def test_max_steps(self) -> None:
        out = await _session(FakeBackend(), FakeClient([_click()]), FakeCtx(), max_steps=3).run()
        assert out.reason == "max_steps" and out.steps == 3

    async def test_denied_action_is_fed_back_then_model_answers(self) -> None:
        ctx = FakeCtx(results={"computer_act": ToolResult(ok=False, denied="confirmation", summary="no")})
        session = _session(FakeBackend(), FakeClient([_click(), _answer("gave up")]), ctx)
        out = await session.run()
        assert out.reason == "answer"
        tool_msgs = [m for m in session.conv.messages if m.get("role") == "tool"]
        assert tool_msgs and "did not approve" in tool_msgs[0]["content"]

    async def test_open_app_and_goto_route_to_their_tools(self) -> None:
        ctx = FakeCtx()
        steps = [Step(content="", tool="open_app", arguments={"name": "Notepad"}, call_id="1"),
                 Step(content="", tool="goto", arguments={"url": "https://example.com"}, call_id="2"),
                 _answer()]
        await _session(FakeBackend(), FakeClient(steps), ctx).run()
        tools = [t for t, _ in ctx.calls]
        assert "open_app" in tools and "open_url" in tools


# ============================================================================ keys reach only the checked window
class _FakeUser32:
    def __init__(self, windows: list[int]) -> None:
        self.windows = windows
        self.i = 0

    def GetForegroundWindow(self) -> int:
        value = self.windows[min(self.i, len(self.windows) - 1)]
        self.i += 1
        return value

    def MapVirtualKeyW(self, code: int, kind: int) -> int:
        return 0


class TestWindowsKeyGuard:
    """The Win32 typing loop with a scripted foreground window (no real input is sent)."""

    def _backend(self, monkeypatch: pytest.MonkeyPatch, windows: list[int]) -> tuple[Any, list[int]]:
        from types import SimpleNamespace

        from jarvis.computer import windows as win

        monkeypatch.setattr(win, "_api", lambda: SimpleNamespace(user32=_FakeUser32(windows)))
        backend = win.WindowsBackend()
        sent: list[int] = []
        monkeypatch.setattr(backend, "_send", lambda inputs, count: sent.append(count))
        return backend, sent

    def test_types_everything_while_the_focus_stays(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend, sent = self._backend(monkeypatch, [7])
        assert backend.type_text("abc") is True
        assert sent == [6]                                   # 3 characters, each down + up

    def test_stops_when_the_focus_moves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend, sent = self._backend(monkeypatch, [7, 7, 9])   # start, first batch, then another window
        assert backend.type_text("a" * 40) is False
        assert sent == [32]                                  # only the first 16 characters went out

    def test_no_enter_after_the_focus_moved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend, sent = self._backend(monkeypatch, [7, 7, 9])
        assert backend.type_text("hi\nnext") is False
        assert sent == [4]                                   # "hi" only: no Enter, nothing after it

    def test_esc_stops_typing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import threading

        backend, sent = self._backend(monkeypatch, [7])
        stop = threading.Event()
        stop.set()
        assert backend.type_text("hello", stop) is False
        assert sent == []

    def test_key_combos_stop_when_the_focus_moves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend, sent = self._backend(monkeypatch, [7, 9])     # the first combination switched windows
        assert backend.press_keys([["alt", "tab"], ["ctrl", "w"]]) is False
        assert sent == [2, 2]                                # alt+tab down/up; ctrl+w never sent


# ============================================================================ the user's window comes back first
class _Win:
    def __init__(self, hwnd: int, title: str, *, pid: int = 500, cls: str = "Notepad", iconic: bool = False,
                 exstyle: int = 0, cloaked: bool = False, thread: int = 77) -> None:
        self.hwnd, self.title, self.pid, self.cls, self.iconic = hwnd, title, pid, cls, iconic
        self.exstyle, self.cloaked, self.thread = exstyle, cloaked, thread


class _ZOrderUser32:
    """user32 over a list of top-level windows, front to back. `locked`: SetForegroundWindow is refused
    unless this thread shares the input state of the window in front (AttachThreadInput)."""

    def __init__(self, windows: list[_Win], foreground: int = 0, locked: bool = False) -> None:
        self.by_id = {x.hwnd: x for x in windows}
        self.foreground = foreground
        self.locked = locked
        self.attached: list[tuple[int, int, bool]] = []

    def EnumWindows(self, callback: Any, _lparam: int) -> bool:
        for hwnd in self.by_id:
            if not callback(hwnd, 0):
                break
        return True

    def IsWindow(self, hwnd: int) -> bool:
        return hwnd in self.by_id

    def IsWindowVisible(self, hwnd: int) -> bool:
        return hwnd in self.by_id

    def IsIconic(self, hwnd: int) -> bool:
        return self.by_id[hwnd].iconic

    def GetWindowTextLengthW(self, hwnd: int) -> int:
        return len(self.by_id[hwnd].title)

    def GetWindowTextW(self, hwnd: int, buf: Any, size: int) -> int:
        buf.value = self.by_id[hwnd].title
        return len(buf.value)

    def GetClassNameW(self, hwnd: int, buf: Any, size: int) -> int:
        buf.value = self.by_id[hwnd].cls
        return len(buf.value)

    def GetWindowLongW(self, hwnd: int, index: int) -> int:
        return self.by_id[hwnd].exstyle

    def GetWindowThreadProcessId(self, hwnd: int, pid: Any) -> int:
        if pid is not None:
            pid._obj.value = self.by_id[hwnd].pid
        return self.by_id[hwnd].thread

    def GetForegroundWindow(self) -> int:
        return self.foreground

    def SetForegroundWindow(self, hwnd: int) -> bool:
        sharing = bool(self.attached) and self.attached[-1][2]
        if self.locked and not sharing:
            return False
        self.foreground = hwnd
        return True

    def BringWindowToTop(self, hwnd: int) -> bool:
        return True

    def AttachThreadInput(self, mine: int, theirs: int, attach: bool) -> bool:
        self.attached.append((mine, theirs, attach))
        return True


def _zorder_api(user32: _ZOrderUser32) -> Any:
    from types import SimpleNamespace

    def cloaked(hwnd: int, attribute: int, value: Any, size: int) -> int:
        value._obj.value = int(user32.by_id[hwnd].cloaked)
        return 0

    return SimpleNamespace(user32=user32, kernel32=SimpleNamespace(GetCurrentThreadId=lambda: 11),
                           dwm_attribute=cloaked, WNDENUMPROC=lambda fn: fn)


class TestWorkingWindow:
    """The window a task was asked from is put back in front for the model's first look. In the live trial
    (2026-10-18) a chat window came up over Calculator while the model loaded, and the first clicks landed in it."""

    def _service(self, backend: FakeBackend, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
                 steps: list[Step]) -> tuple[Any, list[str]]:
        from types import SimpleNamespace

        from jarvis.computer import service as svc
        from jarvis.config.settings import ComputerSettings
        from jarvis.security.process import ProcessRegistry

        shell: list[str] = []

        async def window(action: str) -> bool:
            shell.append(action)
            return True

        monkeypatch.setattr(svc, "HoloClient", lambda *a, **k: FakeClient(steps))
        settings = ComputerSettings(server_url="http://127.0.0.1:9", settle_s=0.0)   # no model server to start
        paths = SimpleNamespace(models=tmp_path, logs=tmp_path)
        return svc.ComputerService(settings, paths, ProcessRegistry(), window, backend=backend), shell  # type: ignore[arg-type]

    async def test_the_window_noted_when_planning_is_in_front_for_the_first_look(
            self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = FakeBackend()
        backend.front = 4242                     # Calculator, in front when the task was asked for
        service, shell = self._service(backend, tmp_path, monkeypatch, [_answer("84")])
        try:
            await service.note_working_window()  # what use_computer's plan does, before the approval
            backend.front = 77                   # then a chat window came up over it
            out = await service.run("compute 12 x 7", FakeCtx(), max_steps=5)
        finally:
            await service.aclose()
        assert out.reason == "answer" and out.content == "84"
        assert shell[0] == "computer_on"
        assert backend.calls[:2] == [("front", (4242,)), ("capture", ())]

    async def test_once_per_task(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = FakeBackend()
        backend.front = 4242
        service, _ = self._service(backend, tmp_path, monkeypatch, [_answer()])
        try:
            await service.note_working_window()
            await service.run("one", FakeCtx(), max_steps=5)
            await service.run("two", FakeCtx(), max_steps=5)     # not planned again: nothing to bring back
        finally:
            await service.aclose()
        assert [c for c in backend.calls if c[0] == "front"] == [("front", (4242,))]

    async def test_a_window_that_cannot_come_back_does_not_stop_the_task(
            self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = FakeBackend()
        backend.front, backend.brings = 4242, False             # closed or minimized meanwhile
        service, _ = self._service(backend, tmp_path, monkeypatch, [_answer("done")])
        try:
            await service.note_working_window()
            out = await service.run("goal", FakeCtx(), max_steps=5)
        finally:
            await service.aclose()
        assert out.reason == "answer" and ("capture", ()) in backend.calls

    def test_no_window_where_computer_control_does_not_run(self) -> None:
        from jarvis.computer.backend import UnsupportedBackend

        backend = UnsupportedBackend()
        assert backend.working_window() == 0 and backend.bring_to_front(5) is False

    # -- the Win32 side, over a scripted window list (no real window is touched)
    def test_picks_the_frontmost_app_window_that_is_not_jarvis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import os

        from jarvis.computer import windows as win

        user32 = _ZOrderUser32([
            _Win(1, "JARVIS", pid=os.getpid(), cls="WinFormsWindow"),            # JARVIS's own window
            _Win(2, "JARVIS Orb", pid=900),                                      # a JARVIS window, by its title
            _Win(3, "", cls="tooltips_class32"),                                 # no title
            _Win(4, "Colors", exstyle=win.WS_EX_TOOLWINDOW),                     # a tool palette
            _Win(5, "Settings", cls="ApplicationFrameWindow", cloaked=True),     # a suspended Store app
            _Win(6, "Untitled - Notepad", iconic=True),                          # minimized
            _Win(7, "Taskbar", cls="Shell_TrayWnd"),                             # the shell
            _Win(8, "‫מחשבון‬", cls="ApplicationFrameWindow"),          # Calculator
            _Win(9, "Claude", cls="Chrome_WidgetWin_1"),
        ])
        monkeypatch.setattr(win, "_api", lambda: _zorder_api(user32))
        assert win.WindowsBackend().working_window() == 8

    def test_no_working_window_when_none_qualifies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.computer import windows as win

        user32 = _ZOrderUser32([_Win(7, "Taskbar", cls="Shell_TrayWnd"), _Win(6, "Notepad", iconic=True)])
        monkeypatch.setattr(win, "_api", lambda: _zorder_api(user32))
        assert win.WindowsBackend().working_window() == 0

    def test_brings_the_window_back_when_windows_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.computer import windows as win

        user32 = _ZOrderUser32([_Win(9, "Claude"), _Win(8, "מחשבון")], foreground=9)
        monkeypatch.setattr(win, "_api", lambda: _zorder_api(user32))
        assert win.WindowsBackend().bring_to_front(8) is True
        assert user32.foreground == 8 and user32.attached == []

    def test_shares_the_input_state_when_the_focus_is_locked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The user clicked in another program after approving, so Windows first refuses JARVIS the focus.
        from jarvis.computer import windows as win

        user32 = _ZOrderUser32([_Win(9, "Claude", thread=77), _Win(8, "מחשבון")], foreground=9, locked=True)
        monkeypatch.setattr(win, "_api", lambda: _zorder_api(user32))
        assert win.WindowsBackend().bring_to_front(8) is True
        assert user32.foreground == 8
        assert user32.attached == [(11, 77, True), (11, 77, False)]          # for a moment, then let go

    def test_leaves_closed_and_minimized_windows_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.computer import windows as win

        user32 = _ZOrderUser32([_Win(9, "Claude"), _Win(5, "Notepad", iconic=True)], foreground=9)
        monkeypatch.setattr(win, "_api", lambda: _zorder_api(user32))
        backend = win.WindowsBackend()
        assert backend.bring_to_front(5) is False        # minimized since
        assert backend.bring_to_front(1234) is False     # closed since
        assert backend.bring_to_front(0) is False
        assert user32.foreground == 9

    @pytest.mark.skipif(sys.platform != "win32", reason="the real Win32 window list")
    def test_real_window_list(self) -> None:
        import os

        from jarvis.computer import windows as win

        backend = win.WindowsBackend()
        hwnd = backend.working_window()
        if hwnd:
            assert win._pid(win._api(), hwnd) != os.getpid()
        assert backend.bring_to_front(0) is False


# ============================================================================ the tools through the gate
from jarvis.tools.computer import ComputerActTool, UseComputerTool  # noqa: E402


class _SvcForGate:
    """A service that drives a scripted session when use_computer.run asks it to."""

    def __init__(self, backend: FakeBackend, steps: list[Step]) -> None:
        self.backend = backend
        self.steps = steps
        self.active: Any = None
        self.noted = 0

    def problem(self) -> str | None:
        return None

    def busy(self) -> bool:
        return self.active is not None

    def start(self) -> None: ...
    async def ensure_covered(self) -> None: ...

    async def note_working_window(self) -> None:
        self.noted += 1

    async def run(self, goal: str, ctx: ToolContext, *, max_steps: int, lang: str = "en") -> Outcome:
        session = ComputerSession(goal, client=FakeClient(self.steps), backend=self.backend, ctx=ctx,
                                  service=self, max_steps=max_steps, max_minutes=5.0, settle_s=0.0, lang=lang)
        self.active = session
        try:
            return await session.run()
        finally:
            self.active = None


class _FakeTool:
    """Stand-ins for open_app / open_url so the session's calls are allowed through the gate."""

    def __init__(self, name: str, cap: Capability) -> None:
        from pydantic import BaseModel

        self.name = name
        self.capabilities = (cap,)
        self.risk = RiskLevel.LOW
        self.always_confirm = False
        self.read_only = False
        self.timeout_s = 10.0

        class P(BaseModel):
            name: str = ""
            url: str = ""

        self.Params = P

    async def plan(self, params: Any, ctx: ToolContext) -> Any:
        from jarvis.tools.base import ActionPlan

        return ActionPlan(summary=self.name)

    def max_runtime_s(self, services: Any) -> float | None:
        return None

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": "", "input_schema": {}}

    async def run(self, params: Any, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, summary=f"{self.name} ok")


async def _approve_all(kit: Any) -> None:
    for _ in range(200):
        for req in kit.broker.pending():
            kit.broker.respond(req.id, True)
        await asyncio.sleep(0.01)


class TestToolsThroughGate:
    def _wire(self, kit: Any, svc: Any) -> None:
        kit.services.extras["computer"] = svc
        kit.registry.register(UseComputerTool())
        kit.registry.register(ComputerActTool())
        kit.registry.register(_FakeTool("open_app", C.PROCESS_LAUNCH))   # type: ignore[arg-type]
        kit.registry.register(_FakeTool("open_url", C.PROCESS_LAUNCH))    # type: ignore[arg-type]
        for cap in (C.SCREEN_READ, C.INPUT_CONTROL, C.PROCESS_LAUNCH):
            kit.permissions.grant("system", cap)

    async def test_full_session_with_an_approved_action(self, kit: Any) -> None:
        backend = FakeBackend(target=_target(process="whatsapp.exe", title="WhatsApp",
                                             element="Send", control="button"))
        svc = _SvcForGate(backend, [_click(), _answer("message sent")])
        self._wire(kit, svc)
        approver = asyncio.ensure_future(_approve_all(kit))
        try:
            res = await kit.executor.execute("use_computer", {"goal": "send a message"},
                                             agent_id="system", task_id="task_1")
        finally:
            approver.cancel()
        assert res.ok, res.error
        assert res.summary == "message sent"
        assert ("click", (960, 540, "left", 1)) in backend.calls     # the approved click ran
        assert svc.noted == 1                  # the user's window was noted when the task was planned

    async def test_computer_act_refused_without_a_session(self, kit: Any) -> None:
        svc = _SvcForGate(FakeBackend(), [])
        self._wire(kit, svc)      # no active session
        res = await kit.executor.execute("computer_act", {"action": "click", "x": 10, "y": 10},
                                         agent_id="system", task_id="task_1")
        assert not res.ok and res.denied in ("permission", "blocked")

    async def test_password_field_blocks_typing(self, kit: Any) -> None:
        backend = FakeBackend(target=_target(control="edit", password=True))
        svc = _SvcForGate(backend, [])
        self._wire(kit, svc)
        svc.active = _StubSession()      # a running session, so computer_act is allowed to plan
        res = await kit.executor.execute("computer_act",
                                         {"action": "type", "text": "secret", "session_id": svc.active.id},
                                         agent_id="system", task_id="task_1")
        assert not res.ok and res.denied == "permission"
        assert ("type", ("secret",)) not in backend.calls

    async def test_enter_is_typed_with_the_text(self, kit: Any) -> None:
        # One keystroke stream, so the Enter too reaches only the window that was checked.
        backend = FakeBackend(target=_target(process="notepad.exe", title="Untitled - Notepad", control="edit"))
        svc = _SvcForGate(backend, [])
        self._wire(kit, svc)
        svc.active = _StubSession()
        res = await kit.executor.execute("computer_act", {"action": "type", "text": "hello", "press_enter": True,
                                                          "session_id": svc.active.id},
                                         agent_id="system", task_id="task_1")
        assert res.ok, res.error
        assert backend.calls == [("type", ("hello\n",))]

    async def test_focus_moving_away_stops_typing(self, kit: Any) -> None:
        backend = FakeBackend(target=_target(process="notepad.exe", title="Untitled - Notepad", control="edit"),
                              keys_reach=False)
        svc = _SvcForGate(backend, [])
        self._wire(kit, svc)
        svc.active = _StubSession()
        res = await kit.executor.execute("computer_act", {"action": "type", "text": "hello",
                                                          "session_id": svc.active.id},
                                         agent_id="system", task_id="task_1")
        assert not res.ok and "window changed" in (res.error or res.summary)

    async def test_cut_short_on_a_command_line_keeps_all_of_it_pending(self, kit: Any) -> None:
        # The Enter never went, so a later Enter there is judged against everything that may be on the line.
        term = _target(process="powershell.exe", title="Windows PowerShell", class_name="ConsoleWindowClass")
        backend = FakeBackend(target=term, keys_reach=False)
        svc = _SvcForGate(backend, [])
        self._wire(kit, svc)
        svc.active = _StubSession()
        svc.active.pending[term.hwnd] = "Remove-Item"
        approver = asyncio.ensure_future(_approve_all(kit))
        try:
            res = await kit.executor.execute("computer_act", {"action": "type", "text": " -Recurse C:\\data",
                                                              "press_enter": True, "session_id": svc.active.id},
                                             agent_id="system", task_id="task_1")
        finally:
            approver.cancel()
        assert not res.ok
        assert svc.active.pending[term.hwnd] == "Remove-Item -Recurse C:\\data"


# ============================================================================ intent + card
class TestIntentAndCard:
    def test_detects_computer_task(self) -> None:
        from jarvis.core.intents import I, IntentDetector

        det = IntentDetector()
        assert det.detect("take control of the computer and book a table").name == I.COMPUTER_TASK
        assert det.detect("תשלוט במחשב ותסדר לי את החלון").name == I.COMPUTER_TASK

    def test_card(self) -> None:
        from jarvis.core.intents import I
        from jarvis.core.replies import action_title

        title, icon = action_title(I.COMPUTER_TASK, {"goal": "open the browser and search"}, "he")
        assert icon == "monitor" and "שולט במחשב" in title


# ============================================================================ installer (pure parts)
class TestInstall:
    def test_sha256_file(self, tmp_path: Any) -> None:
        import hashlib

        from jarvis.computer import install as inst

        f = tmp_path / "x.bin"
        f.write_bytes(b"hello jarvis")
        assert inst.sha256_file(f) == hashlib.sha256(b"hello jarvis").hexdigest()

    def test_pick_asset(self) -> None:
        from jarvis.computer import install as inst

        assets = [{"name": "llama-b1-bin-win-cuda-13.4-x64.zip"}, {"name": "cudart-llama-bin-win-cuda-13.4-x64.zip"},
                  {"name": "llama-b1-bin-win-vulkan-x64.zip"}]
        assert inst.pick_asset(assets, "bin-win-cuda", "x64")["name"].startswith("llama-")
        assert inst.pick_asset(assets, "cudart", "win", "x64")["name"].startswith("cudart")
        assert inst.pick_asset(assets, "bin-win-rocm") is None

    def test_adopt_complete_folder(self, tmp_path: Any) -> None:
        from jarvis.computer import install as inst

        root = _fake_model_folder(tmp_path / "models")
        result = inst.install(tmp_path / "unused", adopt=root)
        assert result.adopted and result.ok and result.root == root

    def test_adopt_incomplete_folder_errors(self, tmp_path: Any) -> None:
        from jarvis.computer import install as inst

        (tmp_path / "half").mkdir()
        with pytest.raises(OSError):
            inst.install(tmp_path / "unused", adopt=tmp_path / "half")

    def test_already_installed_is_a_noop(self, tmp_path: Any) -> None:
        from jarvis.computer import install as inst

        root = _fake_model_folder(tmp_path / "models")
        result = inst.install(root)      # no adopt, but already complete -> nothing downloaded
        assert result.ok and not result.downloaded

    def test_extract_rejects_traversal(self, tmp_path: Any) -> None:
        import zipfile

        from jarvis.computer import install as inst

        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../escape.txt", "x")
        with pytest.raises(OSError):
            inst._extract(bad, tmp_path / "out", lambda m: None)

    def test_model_files_finds_flat_and_nested(self, tmp_path: Any) -> None:
        from jarvis.computer.server import ModelFiles

        root = _fake_model_folder(tmp_path / "m")
        files = ModelFiles(root)
        assert files.problem() is None
        assert files.model.name == "Holo-3.1-9B-Q8_0.gguf" and files.mmproj.name.startswith("mmproj")


def _fake_model_folder(root: Any) -> Any:
    import sys

    exe = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    (root / "llama.cpp" / "b11716").mkdir(parents=True)
    (root / "llama.cpp" / "b11716" / exe).write_bytes(b"\x00")
    (root / "gguf").mkdir(parents=True)
    (root / "gguf" / "Holo-3.1-9B-Q8_0.gguf").write_bytes(b"\x00")
    (root / "gguf" / "mmproj-Holo-3.1-9B-F16.gguf").write_bytes(b"\x00")
    return root
