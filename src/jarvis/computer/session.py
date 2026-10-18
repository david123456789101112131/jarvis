"""One computer-control session: look at the screen, ask the model for the next action, run it, repeat.

Every action is a `computer_act` (or `open_app` / `open_url`) call through the tool executor, so the
permission gate, approval and audit apply to each one — the model never reaches the mouse directly. Besides
the model finishing or the limits, a session stops at once when the user presses **Esc twice** (watched by
the backend) or engages the kill switch. Touching the mouse or keyboard does not stop it, so the user can
keep working alongside; with `computer.stop_on_user_input` on, it does, and JARVIS hands control back.

While a session runs, JARVIS's own windows are hidden so they are not in the screenshots; they are shown
again only to display an approval, then hidden before the approved action happens.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jarvis.computer.backend import ComputerBackend, Shot
from jarvis.computer.model import ACTION_NAMES, Conversation, HoloClient, ModelError, Step, to_pixels
from jarvis.tools.base import ToolContext, ToolResult

if TYPE_CHECKING:
    from jarvis.computer.service import ComputerService

log = logging.getLogger(__name__)

MAX_IMAGE_WIDTH = 1280         # the screenshot is scaled to this before it goes to the model


@dataclass
class Outcome:
    reason: str                # "answer" | "user_stop" | "takeover" | "max_steps" | "time_limit" | "error"
    content: str = ""
    steps: int = 0
    error: str = ""
    actions: list[str] = field(default_factory=list)

    @property
    def stopped_by_user(self) -> bool:
        return self.reason in ("user_stop", "takeover")


class ComputerSession:
    def __init__(self, goal: str, *, client: HoloClient, backend: ComputerBackend, ctx: ToolContext,
                 service: ComputerService, max_steps: int, max_minutes: float, settle_s: float,
                 lang: str = "en", stop_on_input: bool = False) -> None:
        self.goal = goal
        self.client = client
        self.backend = backend
        self.ctx = ctx
        self.service = service
        self.max_steps = max_steps
        self.max_minutes = max_minutes
        self.settle_s = settle_s
        self.lang = lang
        self.stop_on_input = stop_on_input
        self.id = ctx.subtask_id or ctx.task_id or "computer"
        self.task_id = ctx.task_id
        self.stop = threading.Event()        # set by the Esc-twice watcher, read here and while typing
        self.conv = Conversation(goal)
        self.actions: list[str] = []
        self.pending: dict[int, str] = {}    # per command-line window: text typed but not yet run (Enter)

    # ------------------------------------------------------------------ the loop
    async def run(self) -> Outcome:
        watch = self.backend.watch_escape(self.stop.set)
        try:
            await self.service.ensure_covered()
            baseline = await self._input_baseline()
            deadline = time.monotonic() + self.max_minutes * 60
            for n in range(self.max_steps):
                self.ctx.check_cancelled()
                if self.stop.is_set():
                    return Outcome("user_stop", steps=n, actions=self.actions)
                if time.monotonic() > deadline:
                    return Outcome("time_limit", steps=n, actions=self.actions)

                await self.service.ensure_covered()
                shot = await asyncio.to_thread(self.backend.capture, MAX_IMAGE_WIDTH)
                if baseline is not None and await self._taken_over(baseline):
                    return Outcome("takeover", steps=n, actions=self.actions)

                self.conv.observe(shot)
                try:
                    step = await self.client.step(self.conv)
                except ModelError as exc:
                    log.warning("Computer-control model step failed: %s", exc)
                    return Outcome("error", steps=n, error=str(exc), actions=self.actions)
                self.conv.record(step)
                await self._report(step, n)

                if step.is_answer:
                    return Outcome("answer", content=step.arguments.get("content", ""), steps=n + 1,
                                   actions=self.actions)

                result = await self._act(step, shot)
                self.conv.result(step, _feedback(result))
                if result.ok and step.content:
                    self.actions.append(step.content.splitlines()[0][:200])
                await asyncio.sleep(self.settle_s)
                baseline = await self._input_baseline()
            return Outcome("max_steps", steps=self.max_steps, actions=self.actions)
        finally:
            self.stop.set()      # however it ended (the kill switch cancels us): typing still under way stops
            watch.stop()

    async def _input_baseline(self) -> tuple[int, int, int] | None:
        """With `stop_on_input` only: the last-input time and cursor right after JARVIS's own action."""
        if not self.stop_on_input:
            return None
        return await asyncio.to_thread(self.backend.input_state)

    async def _taken_over(self, baseline: tuple[int, int, int]) -> bool:
        """Between our own actions, any change to the last-input time or cursor means a person stepped in."""
        current = await asyncio.to_thread(self.backend.input_state)
        return current != baseline

    # ------------------------------------------------------------------ running one action
    async def _act(self, step: Step, shot: Shot) -> ToolResult:
        name, a = step.tool, step.arguments
        if name not in ACTION_NAMES:
            return ToolResult.fail(f"unknown action: {name}")
        if name == "wait":
            await asyncio.sleep(max(1.0, min(10.0, float(a.get("seconds", 1.0) or 1.0))))
            return ToolResult(ok=True, summary="waited")
        if name == "open_app":
            return await self.ctx.call("open_app", name=str(a.get("name", "")))
        if name == "goto":
            return await self.ctx.call("open_url", url=str(a.get("url", "")))

        w, h = shot.screen_width, shot.screen_height
        common = {"session_id": self.id, "element": str(a.get("element", ""))}
        if name in ("click", "double_click", "right_click"):
            return await self.ctx.call("computer_act", action=name, x=to_pixels(a.get("x"), w),
                                       y=to_pixels(a.get("y"), h), **common)
        if name == "type":
            return await self.ctx.call("computer_act", action="type", text=str(a.get("text", "")),
                                       press_enter=bool(a.get("press_enter", False)), session_id=self.id)
        if name == "press_keys":
            return await self.ctx.call("computer_act", action="press_keys", keys=str(a.get("keys", "")),
                                       session_id=self.id)
        if name == "scroll":
            xy = {"x": to_pixels(a["x"], w), "y": to_pixels(a["y"], h)} if a.get("x") is not None \
                and a.get("y") is not None else {}
            return await self.ctx.call("computer_act", action="scroll", direction=str(a.get("direction", "down")),
                                       amount=int(a.get("amount", 3) or 3), session_id=self.id, **xy)
        if name == "drag":
            return await self.ctx.call("computer_act", action="drag", x=to_pixels(a.get("x"), w),
                                       y=to_pixels(a.get("y"), h), to_x=to_pixels(a.get("to_x"), w),
                                       to_y=to_pixels(a.get("to_y"), h), **common)
        return ToolResult.fail(f"unsupported action: {name}")

    async def _report(self, step: Step, n: int) -> None:
        fraction = min(0.95, (n + 1) / (self.max_steps + 1))
        note = (step.content.splitlines()[0] if step.content else step.tool)[:120]
        await self.ctx.progress(fraction, note)


def _feedback(result: ToolResult) -> str:
    """What the model is told happened, so it can plan the next step (or give up)."""
    if result.ok:
        return f"done: {result.summary}" if result.summary else "done"
    if result.denied == "confirmation":
        return "The user did not approve this action. Stop and report with answer."
    if result.denied:
        return f"JARVIS refused this action: {result.error or result.summary}. Do not retry it."
    return f"failed: {result.error or result.summary}"


__all__ = ["ComputerSession", "Outcome"]
