"""Computer control: key parsing, the safety policy, the model conversation, the session loop and the
two tools through the real executor gate. No real screen, model or GPU is touched — a fake backend and a
scripted model stand in, so these run on any platform."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jarvis.common.enums import Capability, RiskLevel
from jarvis.computer.backend import Shot, Target, parse_keys
from jarvis.computer.model import TOOL_NAMES, Conversation, Step, to_pixels
from jarvis.computer.policy import assess, pending_after, window_label
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
                 target: Target | None = None) -> None:
        self._states = states or [(1, 0, 0)]
        self._i = 0
        self.target = target or _target()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.watch: FakeWatch | None = None

    def problem(self) -> str | None:
        return None

    def screen_size(self) -> tuple[int, int]:
        return (1920, 1080)

    def capture(self, max_width: int) -> Shot:
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

    def move(self, x: int, y: int) -> None: ...
    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> None:
        self.calls.append(("click", (x, y, button, count)))

    def scroll(self, x: Any, y: Any, direction: str, amount: int) -> None:
        self.calls.append(("scroll", (direction, amount)))

    def drag(self, x: int, y: int, to_x: int, to_y: int) -> None:
        self.calls.append(("drag", (x, y, to_x, to_y)))

    def type_text(self, text: str, stop: Any = None) -> int:
        self.calls.append(("type", (text,)))
        return len(text)

    def press_keys(self, combos: Any) -> None:
        self.calls.append(("press_keys", (tuple(map(tuple, combos)),)))

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
                        settle_s=0.0, lang="en")
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

    async def test_takeover_stops(self) -> None:
        # baseline, step0 check (same), step0 re-baseline, step1 check (changed -> takeover)
        backend = FakeBackend(states=[(1, 0, 0), (1, 0, 0), (1, 0, 0), (2, 400, 400)])
        out = await _session(backend, FakeClient([_click(), _click(), _answer()]), FakeCtx()).run()
        assert out.reason == "takeover"

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


# ============================================================================ the tools through the gate
from jarvis.tools.computer import ComputerActTool, UseComputerTool  # noqa: E402


class _SvcForGate:
    """A service that drives a scripted session when use_computer.run asks it to."""

    def __init__(self, backend: FakeBackend, steps: list[Step]) -> None:
        self.backend = backend
        self.steps = steps
        self.active: Any = None

    def problem(self) -> str | None:
        return None

    def busy(self) -> bool:
        return self.active is not None

    def start(self) -> None: ...
    async def ensure_covered(self) -> None: ...

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
