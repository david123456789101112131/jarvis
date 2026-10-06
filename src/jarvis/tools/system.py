"""System tools: status and health, volume, brightness, media keys, lock, power, screenshots,
notifications and the clipboard."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.host.base import HostUnsupported
from jarvis.host.monitor import SystemMonitor, health_findings
from jarvis.permissions.models import PathUse
from jarvis.tools.apps import top_processes
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult

C = Capability
_monitor: SystemMonitor | None = None


def monitor() -> SystemMonitor:
    global _monitor
    if _monitor is None:
        _monitor = SystemMonitor()
    return _monitor


def _host(ctx: ToolContext):
    host = ctx.services.host
    if host is None:
        raise HostUnsupported("This action is not available on this system.")
    return host


class Empty(BaseModel):
    pass


class SystemInfoParams(BaseModel):
    focus: Literal["all", "cpu", "memory", "disk", "network", "battery"] = "all"


class SystemInfoTool(Tool):
    name = "system_info"
    description = "Current CPU, memory, disk, network and battery status of this computer."
    capabilities = (C.SYSTEM_INFO,)
    Params = SystemInfoParams
    read_only = True
    risk = RiskLevel.SAFE

    async def run(self, params: SystemInfoParams, ctx: ToolContext) -> ToolResult:
        stats = await asyncio.to_thread(monitor().sample)
        mem, cpu = stats["memory"], stats["cpu"]
        disks = ", ".join(f"{d['mount']} {d['percent']:.0f}% ({d.get('free_gb', 0)} GB free)" for d in stats["disks"])
        parts = {
            "cpu": f"CPU {cpu['percent']:.0f}% ({cpu['cores']} cores)",
            "memory": f"RAM {mem['percent']:.0f}% ({mem['used_gb']}/{mem['total_gb']} GB)",
            "disk": f"Disk {disks}",
            "network": f"Network ↑{stats['network']['up_kbps']:.0f} ↓{stats['network']['down_kbps']:.0f} kbps",
            "battery": (f"Battery {stats['battery']['percent']}%{' (charging)' if stats['battery']['plugged'] else ''}"
                        if stats["battery"] else "No battery"),
        }
        summary = parts[params.focus] if params.focus != "all" else "; ".join(parts.values())
        return ToolResult(summary=summary, data=stats)


class HealthCheckTool(Tool):
    name = "health_check"
    description = "Check the computer's health (CPU, memory, disk space, battery, heavy processes, uptime)."
    capabilities = (C.SYSTEM_INFO, C.PROCESS_LIST)
    Params = Empty
    read_only = True
    risk = RiskLevel.SAFE

    async def run(self, params: Empty, ctx: ToolContext) -> ToolResult:
        stats = await asyncio.to_thread(monitor().sample)
        top = await asyncio.to_thread(top_processes, 8, "cpu")
        findings = health_findings(stats, top)
        warnings = [f for f in findings if f["level"] == "warning"]
        summary = ("Everything looks healthy." if not findings else
                   f"{len(warnings)} warning(s), {len(findings) - len(warnings)} note(s): "
                   + " ".join(f["message"] for f in findings[:4]))
        return ToolResult(summary=summary, data={"findings": findings, "stats": stats, "top_processes": top})


class VolumeParams(BaseModel):
    action: Literal["set", "up", "down", "mute", "unmute", "get"] = "set"
    level: int | None = Field(None, ge=0, le=100, description="Target level for set")
    step: int = Field(10, ge=1, le=100)


class VolumeTool(Tool):
    name = "volume"
    description = "Get or change the speaker volume (set to a level, up/down by a step, mute/unmute)."
    capabilities = (C.SYSTEM_SETTINGS,)
    Params = VolumeParams
    risk = RiskLevel.LOW

    async def plan(self, params: VolumeParams, ctx: ToolContext) -> ActionPlan:
        return ActionPlan(summary=f"Volume {params.action}" + (f" {params.level}%" if params.level is not None else ""),
                          risk=RiskLevel.SAFE if params.action == "get" else RiskLevel.LOW)

    async def run(self, params: VolumeParams, ctx: ToolContext) -> ToolResult:
        host = _host(ctx)
        if params.action == "get":
            level = await host.get_volume()
        elif params.action == "set":
            if params.level is None:
                return ToolResult.fail("Give a level between 0 and 100.")
            level = await host.set_volume(params.level)
        elif params.action in ("up", "down"):
            level = await host.change_volume(params.step if params.action == "up" else -params.step)
        else:
            await host.set_mute(params.action == "mute")
            return ToolResult(summary="Muted" if params.action == "mute" else "Unmuted",
                              actions=[f"volume {params.action}"])
        return ToolResult(summary=f"Volume is {level}%" if level is not None else "Volume changed",
                          actions=[f"volume {params.action}"], data={"level": level})


class BrightnessParams(BaseModel):
    action: Literal["set", "up", "down", "get"] = "set"
    level: int | None = Field(None, ge=0, le=100)
    step: int = Field(10, ge=1, le=100)


class BrightnessTool(Tool):
    name = "brightness"
    description = "Get or change the screen brightness (laptops and supported monitors)."
    capabilities = (C.SYSTEM_SETTINGS,)
    Params = BrightnessParams
    risk = RiskLevel.LOW

    async def run(self, params: BrightnessParams, ctx: ToolContext) -> ToolResult:
        host = _host(ctx)
        current = await host.get_brightness()
        if params.action == "get":
            return ToolResult(summary=f"Brightness is {current}%" if current is not None else "Brightness unknown",
                              data={"level": current})
        if params.action == "set":
            if params.level is None:
                return ToolResult.fail("Give a level between 0 and 100.")
            target = params.level
        else:
            if current is None:
                return ToolResult.fail("This screen does not report its brightness.")
            target = current + (params.step if params.action == "up" else -params.step)
        level = await host.set_brightness(max(0, min(100, target)))
        return ToolResult(summary=f"Brightness set to {level if level is not None else target}%",
                          actions=[f"brightness {params.action}"], data={"level": level})


class MediaParams(BaseModel):
    action: Literal["play_pause", "next", "previous", "stop"]


class MediaTool(Tool):
    name = "media"
    description = "Media keys: play/pause, next, previous, stop."
    capabilities = (C.SYSTEM_SETTINGS,)
    Params = MediaParams
    risk = RiskLevel.LOW

    async def run(self, params: MediaParams, ctx: ToolContext) -> ToolResult:
        await _host(ctx).media(params.action)
        return ToolResult(summary=f"Media: {params.action.replace('_', '/')}", actions=[f"media {params.action}"])


class LockTool(Tool):
    name = "lock_screen"
    description = "Lock the computer (the user signs in again to continue)."
    capabilities = (C.SYSTEM_SETTINGS,)
    Params = Empty
    risk = RiskLevel.LOW

    async def run(self, params: Empty, ctx: ToolContext) -> ToolResult:
        await _host(ctx).lock_screen()
        return ToolResult(summary="Screen locked", actions=["lock screen"])


class PowerParams(BaseModel):
    action: Literal["shutdown", "restart", "sleep", "logoff"]
    delay_s: int = Field(30, ge=0, le=3600, description="Seconds to wait before shutdown/restart (can be cancelled)")


class PowerTool(Tool):
    name = "power"
    description = "Shut down, restart, sleep or sign out of the computer (always asks the user first)."
    capabilities = (C.SYSTEM_POWER,)
    Params = PowerParams
    risk = RiskLevel.HIGH
    always_confirm = True

    async def plan(self, params: PowerParams, ctx: ToolContext) -> ActionPlan:
        when = f" in {params.delay_s}s" if params.delay_s and params.action in ("shutdown", "restart") else ""
        return ActionPlan(summary=f"{params.action.capitalize()} the computer{when}",
                          risk_reasons=["unsaved work in open apps may be lost"])

    async def run(self, params: PowerParams, ctx: ToolContext) -> ToolResult:
        await _host(ctx).power(params.action, params.delay_s)
        return ToolResult(summary=f"{params.action.capitalize()} scheduled" + (
            f" in {params.delay_s} seconds" if params.delay_s else ""), actions=[f"power {params.action}"])


class CancelShutdownTool(Tool):
    name = "cancel_shutdown"
    description = "Cancel a scheduled shutdown or restart."
    capabilities = (C.SYSTEM_SETTINGS,)
    Params = Empty
    risk = RiskLevel.LOW

    async def run(self, params: Empty, ctx: ToolContext) -> ToolResult:
        await _host(ctx).cancel_power()
        return ToolResult(summary="Scheduled shutdown cancelled", actions=["cancel shutdown"])


class ScreenshotParams(BaseModel):
    path: str = Field("", description="Where to save the PNG (default: Pictures/Screenshots or workspace)")


class ScreenshotTool(Tool):
    name = "screenshot"
    description = "Take a screenshot of the screen and save it as a PNG file."
    capabilities = (C.FS_WRITE,)
    Params = ScreenshotParams
    risk = RiskLevel.LOW

    def _target(self, params: ScreenshotParams, ctx: ToolContext):
        if params.path:
            p = ctx.resolve(params.path)
            return p if p.suffix else p / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
        return ctx.services.paths.workspace / "screenshots" / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"

    async def plan(self, params: ScreenshotParams, ctx: ToolContext) -> ActionPlan:
        target = self._target(params, ctx)
        return ActionPlan(summary=f"Save a screenshot to {target}", paths=[PathUse(path=str(target), access="write")])

    async def run(self, params: ScreenshotParams, ctx: ToolContext) -> ToolResult:
        target = self._target(params, ctx)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        saved = await _host(ctx).screenshot(target)
        return ToolResult(summary=f"Screenshot saved: {saved.name}", files_created=[str(saved)], data={"path": str(saved)})


class NotifyParams(BaseModel):
    title: str = "JARVIS"
    message: str


class NotifyTool(Tool):
    name = "notify"
    description = "Show a Windows notification."
    capabilities = (C.NOTIFY,)
    Params = NotifyParams
    risk = RiskLevel.SAFE

    async def run(self, params: NotifyParams, ctx: ToolContext) -> ToolResult:
        await _host(ctx).notify(params.title, params.message)
        return ToolResult(summary=f"Notification shown: {params.title}")


class ClipboardParams(BaseModel):
    action: Literal["get", "set"] = "get"
    text: str = ""


class ClipboardTool(Tool):
    name = "clipboard"
    description = "Read the clipboard text, or put text on the clipboard."
    capabilities = (C.CLIPBOARD,)
    Params = ClipboardParams
    risk = RiskLevel.LOW

    async def run(self, params: ClipboardParams, ctx: ToolContext) -> ToolResult:
        host = _host(ctx)
        if params.action == "set":
            await host.set_clipboard(params.text)
            return ToolResult(summary=f"Copied {len(params.text)} characters to the clipboard")
        text = await host.get_clipboard()
        return ToolResult(summary=f"Clipboard has {len(text)} characters", data={"text": text[:20000]})


SYSTEM_TOOLS: tuple[type[Tool], ...] = (
    SystemInfoTool, HealthCheckTool, VolumeTool, BrightnessTool, MediaTool, LockTool, PowerTool, CancelShutdownTool,
    ScreenshotTool,
    NotifyTool, ClipboardTool,
)
