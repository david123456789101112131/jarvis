"""Stage 7/11: the tool executor gate (permissions, confirmation, modes, timeouts, cancel, kill switch)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from jarvis.common.cancel import CancelToken
from jarvis.common.enums import Capability, Mode, RiskLevel
from jarvis.common.errors import KillSwitchEngaged, TaskCancelled
from jarvis.permissions import PathUse, analyze_command
from jarvis.tools import ActionPlan, Tool, ToolContext, ToolResult

C = Capability


class EchoParams(BaseModel):
    text: str
    title: str = ""


class EchoTool(Tool):
    name = "echo"
    description = "Return the text."
    capabilities = (C.SYSTEM_INFO,)
    Params = EchoParams
    read_only = True
    risk = RiskLevel.SAFE

    async def run(self, params: EchoParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(summary=f"echo {params.text}", data={"text": params.text})


class WriteParams(BaseModel):
    path: str
    content: str = ""


class WriteTool(Tool):
    name = "write"
    description = "Write a file."
    capabilities = (C.FS_WRITE,)
    Params = WriteParams

    async def plan(self, params: WriteParams, ctx: ToolContext) -> ActionPlan:
        p = ctx.resolve(params.path)
        return ActionPlan(summary=f"Write {p.name}", paths=[PathUse(path=str(p), access="write")])

    async def run(self, params: WriteParams, ctx: ToolContext) -> ToolResult:
        p = ctx.resolve(params.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(params.content, encoding="utf-8")
        return ToolResult(summary=f"Wrote {p.name}", files_created=[str(p)])


class ShellParams(BaseModel):
    command: str


class FakeShellTool(Tool):
    """Judged like the real shell tool, but only records the command instead of running it."""

    name = "shell"
    description = "Run a command."
    capabilities = (C.SHELL_EXECUTE,)
    Params = ShellParams
    risk = RiskLevel.MEDIUM

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def plan(self, params: ShellParams, ctx: ToolContext) -> ActionPlan:
        a = analyze_command(params.command)
        return ActionPlan(summary=f"Run: {params.command}", risk=a.risk, risk_reasons=a.reasons)

    async def run(self, params: ShellParams, ctx: ToolContext) -> ToolResult:
        self.ran.append(params.command)
        return ToolResult(summary=f"ran {params.command}")


class SlowParams(BaseModel):
    seconds: float = 30


class SlowTool(Tool):
    name = "slow"
    description = "Sleep."
    capabilities = (C.SYSTEM_INFO,)
    Params = SlowParams
    read_only = True
    timeout_s = 0.4

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self, params: SlowParams, ctx: ToolContext) -> ToolResult:
        self.started.set()
        try:
            await asyncio.sleep(params.seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ToolResult(summary="slept")


class LeakTool(Tool):
    name = "leak"
    description = "Returns a secret by mistake."
    capabilities = (C.SYSTEM_INFO,)
    Params = EchoParams
    read_only = True

    async def run(self, params: EchoParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(summary=f"value is {params.text}", data={"env": {"KEY": params.text}})


def _register(kit: Any, *tools: Tool) -> None:
    for t in tools:
        kit.registry.register(t)


async def _approve_when_asked(kit: Any, approved: bool) -> None:
    while not kit.broker.pending():
        await asyncio.sleep(0.01)
    kit.broker.respond(kit.broker.pending()[0].id, approved)


class TestGate:
    async def test_allowed_call_runs_and_is_logged(self, kit) -> None:
        _register(kit, WriteTool())
        target = kit.paths.workspace / "notes" / "a.txt"
        res = await kit.executor.execute("write", {"path": str(target), "content": "hi"}, agent_id="development",
                                         task_id="task_1")
        assert res.ok, res.error
        assert target.read_text(encoding="utf-8") == "hi"
        assert res.files_created == [str(target)]
        entry = kit.audit.recent()[-1]
        assert (entry.tool, entry.decision, entry.outcome, entry.agent_id) == ("write", "allowed", "ok", "development")
        assert any("Development Agent" in a.message for a in kit.activity.recent())

    async def test_unknown_tool_and_bad_input_fail_softly(self, kit) -> None:
        _register(kit, EchoTool())
        res = await kit.executor.execute("nope", {}, agent_id="system")
        assert not res.ok and "Unknown tool" in res.error
        res = await kit.executor.execute("echo", {"txt": 1}, agent_id="system")
        assert not res.ok and "Invalid input" in res.error

    async def test_missing_capability_is_denied(self, kit) -> None:
        shell = FakeShellTool()
        _register(kit, shell)
        res = await kit.executor.execute("shell", {"command": "dir"}, agent_id="planner", task_id="t")
        assert not res.ok and res.denied == "permission"
        assert shell.ran == []
        assert kit.audit.recent()[-1].decision == "denied"
        assert kit.activity.recent()[-1].level == "warning"

    async def test_folder_scope_is_enforced(self, kit) -> None:
        _register(kit, WriteTool())
        outside = kit.user_docs / "x.txt"
        res = await kit.executor.execute("write", {"path": str(outside)}, agent_id="development")
        assert not res.ok and not outside.exists()
        res = await kit.executor.execute("write", {"path": str(outside)}, agent_id="system")
        assert res.ok and outside.exists()  # the System agent may write in the user's folders

    async def test_protected_data_folder_is_blocked(self, kit) -> None:
        _register(kit, WriteTool())
        res = await kit.executor.execute("write", {"path": str(kit.paths.data / "permissions.json")}, agent_id="system")
        assert not res.ok and res.denied == "blocked"

    async def test_chat_mode_allows_read_only_only(self, kit) -> None:
        _register(kit, EchoTool(), WriteTool())
        kit.mode = Mode.CHAT
        assert (await kit.executor.execute("echo", {"text": "x"}, agent_id="system")).ok
        res = await kit.executor.execute("write", {"path": "a.txt"}, agent_id="system")
        assert not res.ok and res.denied == "mode"


class TestConfirmation:
    async def test_high_risk_waits_for_approval(self, kit) -> None:
        shell = FakeShellTool()
        _register(kit, shell)
        res, _ = await asyncio.gather(
            kit.executor.execute("shell", {"command": "winget install vlc"}, agent_id="system", task_id="t1"),
            _approve_when_asked(kit, True),
        )
        assert res.ok and shell.ran == ["winget install vlc"]
        assert kit.audit.recent()[-1].decision == "confirmed"

    async def test_rejected_action_never_runs(self, kit) -> None:
        shell = FakeShellTool()
        _register(kit, shell)
        res, _ = await asyncio.gather(
            kit.executor.execute("shell", {"command": "shutdown /s /t 0"}, agent_id="system", task_id="t2"),
            _approve_when_asked(kit, False),
        )
        assert not res.ok and res.denied == "confirmation"
        assert shell.ran == []
        assert kit.audit.recent()[-1].decision == "rejected"

    async def test_low_risk_runs_without_asking(self, kit) -> None:
        shell = FakeShellTool()
        _register(kit, shell)
        res = await kit.executor.execute("shell", {"command": "dir"}, agent_id="system")
        assert res.ok and not kit.broker.pending()

    async def test_critical_is_refused_when_above_agent_ceiling(self, kit) -> None:
        shell = FakeShellTool()
        _register(kit, shell)
        kit.permissions.grant("research", C.SHELL_EXECUTE)
        res = await kit.executor.execute("shell", {"command": "format C:"}, agent_id="research")
        assert not res.ok and res.denied == "blocked" and shell.ran == []

    async def test_cancelling_the_task_while_waiting_for_approval(self, kit) -> None:
        shell = FakeShellTool()
        _register(kit, shell)
        token = CancelToken()
        call = asyncio.create_task(kit.executor.execute("shell", {"command": "winget install x"}, agent_id="system",
                                                        task_id="t3", cancel=token))
        while not kit.broker.pending():
            await asyncio.sleep(0.01)
        token.cancel("user stopped the task")
        with pytest.raises(TaskCancelled):
            await call
        assert shell.ran == []


class TestRuntime:
    async def test_timeout(self, kit) -> None:
        slow = SlowTool()
        _register(kit, slow)
        res = await kit.executor.execute("slow", {}, agent_id="system")
        assert not res.ok and "timed out" in res.error
        assert slow.cancelled

    async def test_cancel_mid_run(self, kit) -> None:
        slow = SlowTool()
        slow.timeout_s = None
        _register(kit, slow)
        token = CancelToken()
        call = asyncio.create_task(kit.executor.execute("slow", {}, agent_id="research", cancel=token))
        await slow.started.wait()
        token.cancel("stop")
        with pytest.raises(TaskCancelled):
            await call
        assert slow.cancelled

    async def test_kill_switch_before_and_during(self, kit) -> None:
        slow = SlowTool()
        slow.timeout_s = None
        _register(kit, slow, EchoTool())
        await kit.kill_switch.engage("test", agent_id="system")
        with pytest.raises(KillSwitchEngaged):
            await kit.executor.execute("echo", {"text": "x"}, agent_id="system")
        assert (await kit.executor.execute("echo", {"text": "x"}, agent_id="research")).ok  # other agents unaffected
        await kit.kill_switch.release("system")

        call = asyncio.create_task(kit.executor.execute("slow", {}, agent_id="system"))
        await slow.started.wait()
        await kit.kill_switch.engage("stop everything")
        with pytest.raises(KillSwitchEngaged):
            await call
        assert slow.cancelled

    async def test_secrets_are_redacted_from_results_and_logs(self, kit) -> None:
        secret = "sk-ant-api03-" + "Z" * 40
        kit.redactor.register("ANTHROPIC_API_KEY", secret)
        _register(kit, LeakTool())
        res = await kit.executor.execute("leak", {"text": secret}, agent_id="system")
        assert secret not in res.summary and secret not in str(res.data)
        audit_file = (kit.paths.logs / "audit.jsonl").read_text(encoding="utf-8")
        assert secret not in audit_file


class TestRegistry:
    def test_agents_only_see_tools_they_may_use(self, kit) -> None:
        _register(kit, EchoTool(), WriteTool(), FakeShellTool())
        planner = {t.name for t in kit.registry.for_agent("planner", kit.permissions)}
        system = {t.name for t in kit.registry.for_agent("system", kit.permissions)}
        assert planner == {"echo", "write"}
        assert system == {"echo", "write", "shell"}

    def test_schema_keeps_title_property(self) -> None:
        schema = EchoTool.schema()
        assert schema["name"] == "echo"
        assert set(schema["input_schema"]["properties"]) == {"text", "title"}
        assert "title" not in schema["input_schema"]
        assert "title" not in schema["input_schema"]["properties"]["text"]


@pytest.fixture(autouse=True)
def _fast_paths(tmp_path: Path) -> None:
    (tmp_path / "denied").mkdir(exist_ok=True)
