"""Application, website and process tools (launch, close, list, kill)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import psutil
from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.host import catalog
from jarvis.permissions.risk import analyze_command
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult

C = Capability

# Closing or killing these breaks Windows; never allowed.
CRITICAL_PROCESSES = {
    "system", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
    "dwm.exe", "explorer.exe", "fontdrvhost.exe", "lsaiso.exe", "registry", "memory compression", "audiodg.exe",
    "spoolsv.exe", "msmpeng.exe", "securityhealthservice.exe", "init", "systemd", "launchd", "kernel_task",
}


def is_critical(name: str) -> bool:
    n = name.strip().lower()
    return n in CRITICAL_PROCESSES or f"{n}.exe" in CRITICAL_PROCESSES


def self_pids() -> set[int]:
    """JARVIS's own process and its parents/children — never killed by its own tools."""
    pids: set[int] = set()
    with contextlib.suppress(psutil.Error):
        me = psutil.Process()
        pids.add(me.pid)
        pids.update(p.pid for p in me.parents())
        pids.update(p.pid for p in me.children(recursive=True))
    return pids


class OpenAppParams(BaseModel):
    name: str = Field(..., description="App name in Hebrew or English (כרום, Word, VS Code), a known site or a URL")
    args: list[str] = Field(default_factory=list, description="Optional command-line arguments")


class OpenAppTool(Tool):
    name = "open_app"
    description = ("Open an installed application by its name in Hebrew or English, or a website by name/URL "
                   "(e.g. 'Chrome', 'כרום', 'YouTube', 'calculator').")
    capabilities = (C.PROCESS_LAUNCH,)
    Params = OpenAppParams
    timeout_s = 45.0

    async def plan(self, params: OpenAppParams, ctx: ToolContext) -> ActionPlan:
        name = params.name.strip()
        if catalog.find_site(name) is None and catalog.looks_like_url(name) and catalog.find_app(name) is None:
            return _browser_plan(ctx, catalog.to_url(name))
        if not params.args:
            return ActionPlan(summary=ctx.t(f"פתיחת {params.name}", f"Open {params.name}"))
        # With arguments, a program (PowerShell, cmd, Python…) can be told to run anything: judged like a command.
        shown = " ".join(params.args)
        a = analyze_command(f"{params.name} {shown}", ctx.lang)
        return ActionPlan(
            summary=ctx.t(f"פתיחת {params.name} עם: {shown}", f"Open {params.name} with: {shown}"),
            capabilities=[C.PROCESS_LAUNCH, C.SHELL_EXECUTE], risk=max(RiskLevel.HIGH, a.risk),
            risk_reasons=[ctx.t("תוכנה שמופעלת עם פרמטרים יכולה להריץ כל פקודה",
                                "a program started with arguments can run any command"), *a.reasons[1:]],
            irreversible=a.irreversible, details={"args": params.args}, remember_scope=params.name.strip().lower(),
        )

    async def run(self, params: OpenAppParams, ctx: ToolContext) -> ToolResult:
        host = ctx.services.host
        if host is None:
            return ToolResult.fail(ctx.t("פתיחת תוכנות לא זמינה במערכת הזו.", "Launching apps is not available on this system."))
        name = params.name.strip()
        site = catalog.find_site(name)
        if site is not None or (catalog.looks_like_url(name) and catalog.find_app(name) is None):
            url = site.url if site else catalog.to_url(name)
            await host.open_url(url)
            _note(ctx, "last_url", url)
            shown = site.display if site else url
            return ToolResult(summary=ctx.t(f"נפתח {shown}", f"Opened {shown}"), actions=[f"open {url}"],
                              data={"url": url, "kind": "site"})
        app = await host.find_app(name)
        if app is None:
            return ToolResult.fail(ctx.t(f"לא מצאתי תוכנה מותקנת בשם '{name}'.", f"I couldn't find an installed app called '{name}'."),
                                   data={"name": name})
        launched = await host.launch_app(app, params.args)
        if not launched.ok:
            detail = launched.detail or ctx.t("ההפעלה נכשלה", "launch failed")
            return ToolResult.fail(ctx.t(f"לא הצלחתי לפתוח את {app.name}: {detail}", f"Could not open {app.name}: {detail}"),
                                   data={"app": app.to_dict()})
        _note(ctx, "last_app", app.name)
        return ToolResult(summary=ctx.t(f"נפתח {app.name}", f"Opened {app.name}"), actions=[f"launch {app.name} ({launched.method})"],
                          data={"app": app.to_dict(), "pid": launched.pid, "method": launched.method})


class OpenUrlParams(BaseModel):
    url: str


class OpenUrlTool(Tool):
    name = "open_url"
    description = "Open a web address in the default browser."
    capabilities = (C.PROCESS_LAUNCH,)
    Params = OpenUrlParams

    async def plan(self, params: OpenUrlParams, ctx: ToolContext) -> ActionPlan:
        return _browser_plan(ctx, catalog.to_url(params.url))

    async def run(self, params: OpenUrlParams, ctx: ToolContext) -> ToolResult:
        host = ctx.services.host
        if host is None:
            return ToolResult.fail(ctx.t("פתיחת דפי אינטרנט לא זמינה במערכת הזו.", "Opening web pages is not available on this system."))
        url = catalog.to_url(params.url)
        if not url.lower().startswith(("http://", "https://")):
            return ToolResult.fail(ctx.t("אפשר לפתוח רק כתובות http(s).", "Only http(s) addresses can be opened."))
        await host.open_url(url)
        _note(ctx, "last_url", url)
        return ToolResult(summary=ctx.t(f"נפתח {url}", f"Opened {url}"), actions=[f"open {url}"], data={"url": url})


def _browser_plan(ctx: ToolContext, url: str) -> ActionPlan:
    """Opening an address sends what is in it to that site: checked like the web tools' requests."""
    from jarvis.tools.web import url_payload, url_risk

    carried, sends = url_payload(url)
    risk, reasons = url_risk(ctx, url)
    return ActionPlan(summary=ctx.t(f"פתיחת {url}", f"Open {url}"), destination=url, outbound=carried,
                      sends_data=sends, risk=risk, risk_reasons=reasons)


class CloseAppParams(BaseModel):
    name: str = Field(..., description="App or process name, e.g. Chrome, כרום, notepad.exe")
    force: bool = Field(False, description="Kill immediately instead of asking the app to close")


class CloseAppTool(Tool):
    name = "close_app"
    description = "Close an application by name (asks it to close; force kills it)."
    capabilities = (C.PROCESS_KILL,)
    Params = CloseAppParams
    risk = RiskLevel.MEDIUM

    async def plan(self, params: CloseAppParams, ctx: ToolContext) -> ActionPlan:
        entry = catalog.find_app(params.name)
        target = entry.display if entry else params.name
        procs = entry.process_names if entry else (params.name,)
        critical = any(is_critical(p) for p in procs)
        return ActionPlan(
            summary=ctx.t(f"{'סגירה בכוח של' if params.force else 'סגירת'} {target}", f"{'Force-close' if params.force else 'Close'} {target}"),
            risk=RiskLevel.CRITICAL if critical else (RiskLevel.HIGH if params.force else None),
            risk_reasons=([ctx.t("סוגר תהליך חיוני של Windows", "closing a core Windows process")] if critical else [])
            + ([ctx.t("עבודה שלא נשמרה בתוכנה עלולה ללכת לאיבוד", "unsaved work in the app may be lost")] if params.force else []),
        )

    async def run(self, params: CloseAppParams, ctx: ToolContext) -> ToolResult:
        host = ctx.services.host
        if host is None:
            return ToolResult.fail(ctx.t("סגירת תוכנות לא זמינה במערכת הזו.", "Closing apps is not available on this system."))
        entry = catalog.find_app(params.name)
        if any(is_critical(p) for p in (entry.process_names if entry else (params.name,))):
            return ToolResult.fail(ctx.t(f"{params.name} הוא תהליך חיוני של המערכת ולא ייסגר.",
                                         f"{params.name} is a core system process and will not be closed."), denied="blocked")
        closed = await host.close_app(params.name, force=params.force)
        shown = entry.display if entry else params.name
        if not closed:
            return ToolResult.fail(ctx.t(f"{shown} לא פועל כרגע.", f"{shown} is not running."))
        return ToolResult(summary=ctx.t(f"נסגר {shown}", f"Closed {shown}"),
                          actions=[f"close {', '.join(sorted(set(closed)))}"], data={"closed": closed})


class RunningAppsParams(BaseModel):
    pass


class RunningAppsTool(Tool):
    name = "running_apps"
    description = "List the applications that are open right now."
    capabilities = (C.PROCESS_LIST,)
    Params = RunningAppsParams
    read_only = True
    risk = RiskLevel.SAFE

    async def plan(self, params: RunningAppsParams, ctx: ToolContext) -> ActionPlan:
        return ActionPlan(summary=ctx.t("רשימת התוכנות הפתוחות", "List the open apps"))

    async def run(self, params: RunningAppsParams, ctx: ToolContext) -> ToolResult:
        host = ctx.services.host
        apps = await host.running_apps() if host is not None else []
        return ToolResult(summary=ctx.t(f"{len(apps)} תוכנות פתוחות", f"{len(apps)} apps open"), data={"apps": apps})


def top_processes(limit: int = 15, sort: str = "cpu", sample_s: float = 0.4) -> list[dict[str, Any]]:
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        with contextlib.suppress(psutil.Error):
            p.cpu_percent(None)
            procs.append(p)
    time.sleep(sample_s)
    cores = psutil.cpu_count() or 1
    rows = []
    for p in procs:
        with contextlib.suppress(psutil.Error):
            rows.append({
                "pid": p.pid,
                "name": p.info.get("name") or "",
                "cpu": round(p.cpu_percent(None) / cores, 1),
                "memory_mb": round(p.memory_info().rss / 1_048_576, 1),
            })
    key = "memory_mb" if sort == "memory" else "cpu"
    rows.sort(key=lambda r: r[key], reverse=True)
    return rows[:limit]


class ListProcessesParams(BaseModel):
    sort: str = Field("cpu", description="cpu or memory")
    limit: int = Field(15, ge=1, le=100)


class ListProcessesTool(Tool):
    name = "list_processes"
    description = "Show the processes using the most CPU or memory."
    capabilities = (C.PROCESS_LIST,)
    Params = ListProcessesParams
    read_only = True
    risk = RiskLevel.SAFE

    async def plan(self, params: ListProcessesParams, ctx: ToolContext) -> ActionPlan:
        return ActionPlan(summary=ctx.t("רשימת התהליכים הכבדים", "List the heaviest processes"))

    async def run(self, params: ListProcessesParams, ctx: ToolContext) -> ToolResult:
        rows = await asyncio.to_thread(top_processes, params.limit, params.sort)
        head = ", ".join(f"{r['name']} {r['cpu']}%/{r['memory_mb']:.0f}MB" for r in rows[:5])
        by = ctx.t("זיכרון" if params.sort == "memory" else "מעבד", params.sort)
        return ToolResult(summary=ctx.t(f"התהליכים הכבדים ביותר ({by}): {head}", f"Top processes by {by}: {head}"),
                          data={"processes": rows})


class KillProcessParams(BaseModel):
    pid: int | None = None
    name: str | None = None


class KillProcessTool(Tool):
    name = "kill_process"
    description = "End a process by PID or name (core Windows processes and JARVIS itself are protected)."
    capabilities = (C.PROCESS_KILL,)
    Params = KillProcessParams
    risk = RiskLevel.MEDIUM

    def _targets(self, params: KillProcessParams) -> list[psutil.Process]:
        out = []
        if params.pid is not None:
            with contextlib.suppress(psutil.Error):
                out.append(psutil.Process(params.pid))
        elif params.name:
            want = params.name.lower()
            for p in psutil.process_iter(["name"]):
                n = (p.info.get("name") or "").lower()
                if n == want or n == f"{want}.exe" or n.removesuffix(".exe") == want:
                    out.append(p)
        return out

    async def plan(self, params: KillProcessParams, ctx: ToolContext) -> ActionPlan:
        targets = await asyncio.to_thread(self._targets, params)
        names = sorted({_safe_name(t) for t in targets})
        critical = any(is_critical(n) for n in names) or (params.name is not None and is_critical(params.name))
        own = bool({t.pid for t in targets} & self_pids())
        return ActionPlan(
            summary=ctx.t(f"סגירת {', '.join(names) or params.name or params.pid}",
                          f"End {', '.join(names) or params.name or params.pid}")
            + f" ({ctx.n(len(targets), 'תהליך אחד', 'תהליכים', 'process', 'processes')})",
            risk=RiskLevel.CRITICAL if critical or own else (RiskLevel.HIGH if len(targets) > 5 else None),
            risk_reasons=([ctx.t("תהליך חיוני של המערכת", "core system process")] if critical else [])
            + ([ctx.t("התהליך של JARVIS עצמו", "JARVIS's own process")] if own else []),
            affected_count=len(targets),
        )

    async def run(self, params: KillProcessParams, ctx: ToolContext) -> ToolResult:
        if params.pid is None and not params.name:
            return ToolResult.fail(ctx.t("צריך מספר תהליך (pid) או שם תהליך.", "Give a pid or a process name."))
        targets = await asyncio.to_thread(self._targets, params)
        if not targets:
            return ToolResult.fail(ctx.t("אין תהליך מתאים שפועל כרגע.", "No matching process is running."))
        protected = self_pids()
        killed: list[str] = []

        def kill() -> None:
            for t in targets:
                name = _safe_name(t)
                if t.pid in protected or is_critical(name):
                    continue
                with contextlib.suppress(psutil.Error):
                    t.terminate()
                    try:
                        t.wait(3)
                    except psutil.TimeoutExpired:
                        t.kill()
                    killed.append(f"{name} ({t.pid})")

        await asyncio.to_thread(kill)
        if not killed:
            return ToolResult.fail(ctx.t("התהליכים האלה מוגנים ולא נסגרו.", "Those processes are protected and were not ended."),
                                   denied="blocked")
        return ToolResult(summary=ctx.t(f"נסגרו: {', '.join(killed)}", f"Ended {', '.join(killed)}"),
                          actions=[f"kill {k}" for k in killed])


def _safe_name(p: psutil.Process) -> str:
    try:
        return p.name()
    except psutil.Error:
        return str(p.pid)


def _note(ctx: ToolContext, key: str, value: Any) -> None:
    memory = ctx.services.memory
    if memory is not None:
        memory.short.note(key, value)


APP_TOOLS: tuple[type[Tool], ...] = (
    OpenAppTool, OpenUrlTool, CloseAppTool, RunningAppsTool, ListProcessesTool, KillProcessTool,
)
