"""Shell commands (PowerShell / cmd / bash) run in a sandboxed child process.

The command text is risk-assessed before it runs (format/registry/installs/downloads-and-run
are HIGH or CRITICAL and need approval or are refused), secrets are stripped from the child
environment, output is capped, and the process tree is killed on timeout, task cancel or
kill switch.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.common.errors import PermissionDenied
from jarvis.common.i18n import t
from jarvis.permissions.models import PathUse
from jarvis.security.process import ProcessResult, run_process
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult

C = Capability
ShellName = Literal["auto", "powershell", "cmd", "bash", "sh"]


def shell_argv(command: str, shell: str, host=None) -> list[str]:
    if host is not None:
        try:
            return host.shell_argv(command, None if shell == "auto" else shell)
        except Exception:  # pragma: no cover - fall back to the generic forms
            pass
    if shell == "auto":
        shell = "powershell" if sys.platform == "win32" else "bash"
    if shell == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
    if shell == "cmd":
        return ["cmd.exe", "/d", "/s", "/c", command]
    if shell == "sh":
        return ["sh", "-c", command]
    return ["bash", "-c", command]


async def run_in_context(argv: list[str], ctx: ToolContext, *, cwd: Path | None, timeout: float,
                         stdin: bytes | None = None, extra_env: dict[str, str] | None = None) -> ProcessResult:
    settings = ctx.services.settings.security
    return await run_process(
        argv, cwd=cwd, timeout=timeout, max_output=settings.max_output_chars, cancel_event=ctx.cancel.event,
        registry=ctx.services.processes, owner=ctx.agent_id, task_id=ctx.task_id, stdin_data=stdin,
        extra_env=extra_env,
    )


def process_result(res: ProcessResult, what: str, lang: str = "en", what_he: str | None = None) -> ToolResult:
    """The tool result of a finished child process; `what` names it in messages (`what_he` in Hebrew)."""
    data = res.to_dict()
    he = what_he or what
    if res.cancelled:
        return ToolResult.fail(t(lang, f"הריצה של {he} נעצרה.", f"{what} was stopped."), data=data)
    if res.timed_out:
        return ToolResult.fail(t(lang, f"הריצה של {he} לא הסתיימה בזמן ({res.duration_s:.0f} שניות).",
                                 f"{what} timed out after {res.duration_s:.0f}s."), data=data)
    if res.exit_code != 0:
        return ToolResult.fail(t(lang, f"הריצה של {he} נכשלה (קוד יציאה {res.exit_code}): {res.summary(400)}",
                                 f"{what} failed (exit {res.exit_code}): {res.summary(400)}"), data=data)
    return ToolResult(summary=t(lang, f"הריצה של {he} הסתיימה: {res.summary(300)}", f"{what} finished: {res.summary(300)}"),
                      data=data)


class RunCommandParams(BaseModel):
    command: str = Field(..., description="The command line to run")
    shell: ShellName = Field("auto", description="auto = PowerShell on Windows, bash elsewhere")
    cwd: str | None = Field(None, description="Working folder (default: the task folder / workspace)")
    timeout_s: float = Field(120, ge=1, le=3600)


class RunCommandTool(Tool):
    name = "run_command"
    description = ("Run a PowerShell/cmd/bash command and return its output. Dangerous commands (deleting, "
                   "installing, registry, power, downloads) are checked and may need the user's approval.")
    capabilities = (C.SHELL_EXECUTE,)
    Params = RunCommandParams
    risk = RiskLevel.MEDIUM
    timeout_s = 3700.0

    async def plan(self, params: RunCommandParams, ctx: ToolContext) -> ActionPlan:
        if not ctx.services.settings.security.shell_enabled:
            raise PermissionDenied(ctx.t("פקודות מערכת מושבתות בהגדרות האבטחה.", "Shell commands are disabled in the security settings."))
        cwd = ctx.resolve(params.cwd) if params.cwd else (ctx.cwd or ctx.services.paths.workspace)
        short = params.command if len(params.command) <= 160 else params.command[:157] + "…"
        return ActionPlan(summary=ctx.t(f"הרצת פקודה: {short}", f"Run: {short}"), paths=[PathUse(path=str(cwd), access="read")],
                          details={"command": params.command, "shell": params.shell, "cwd": str(cwd)})

    async def run(self, params: RunCommandParams, ctx: ToolContext) -> ToolResult:
        cwd = ctx.resolve(params.cwd) if params.cwd else (ctx.cwd or ctx.services.paths.workspace)
        if not cwd.is_dir():
            return ToolResult.fail(ctx.t(f"תיקיית העבודה לא נמצאה: {cwd}", f"Working folder not found: {cwd}"))
        argv = shell_argv(params.command, params.shell, ctx.services.host)
        res = await run_in_context(argv, ctx, cwd=cwd, timeout=params.timeout_s)
        result = process_result(res, "Command", ctx.lang, "הפקודה")
        result.actions.append(f"ran: {params.command[:200]}")
        return result


SHELL_TOOLS: tuple[type[Tool], ...] = (RunCommandTool,)
