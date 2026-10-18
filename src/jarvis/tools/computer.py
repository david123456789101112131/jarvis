"""The two computer-control tools.

`use_computer` runs a whole session: the user approves once to start it, then the local model drives the
screen until the goal is done (or the user stops it). It is the only one of the two an agent's model sees.

`computer_act` performs **one** action, and is called only from inside a running session. It is where the
safety happens: JARVIS looks at what is actually under the pointer (or in the focused window) with its own
APIs and judges the action from that (`jarvis.computer.policy`) — never from the model's description — so a
click on "Send", a run of a shell command, or typing into a password field is caught whatever the model
called it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.common.errors import PermissionDenied, ToolError
from jarvis.computer import policy
from jarvis.computer.backend import Target, parse_keys
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult

C = Capability
POINTER = ("click", "double_click", "right_click", "drag")


def _service(ctx: ToolContext) -> Any:
    service = ctx.services.extras.get("computer")
    if service is None:
        raise ToolError("computer control is not available")
    return service


# ============================================================================ use_computer (a session)
class UseComputerParams(BaseModel):
    goal: str = Field(min_length=3, max_length=2000, description="What to do on the computer, in plain words.")
    window: str = Field(default="", max_length=200, description=(
        "When the task is in a window that is already open: part of its title or its program, e.g. 'YouTube', "
        "'Chrome', 'Calculator'. JARVIS brings it to the front (restoring it if minimized) before starting."))
    max_steps: int | None = Field(default=None, ge=1, le=100, description="Most actions to take (optional).")


class UseComputerTool(Tool):
    name = "use_computer"
    description = (
        "Control the computer by looking at the screen and using the mouse and keyboard, for a task that can "
        "only be done through a program's window (no command or dedicated tool fits). Give the goal in words. "
        "The user approves before it starts and can stop it at any time by pressing Esc twice. For a window that "
        "is already open, name it in `window`: JARVIS puts it in front itself, so the goal needs no steps (and "
        "no commands) to find or switch to it. Do not use it for things a direct tool already does (opening "
        "apps, files, settings, the web)."
    )
    capabilities = (C.SCREEN_READ, C.INPUT_CONTROL)
    Params = UseComputerParams
    risk = RiskLevel.HIGH
    always_confirm = True          # one approval starts the session; actions inside are judged one by one
    timeout_s = None               # the session keeps its own time limit

    def max_runtime_s(self, services: Any) -> float | None:
        s = services.settings.computer
        confirm = services.settings.security.confirm_timeout_s
        return s.max_minutes * 60 + confirm + 120.0

    async def plan(self, params: UseComputerParams, ctx: ToolContext) -> ActionPlan:
        service = _service(ctx)
        problem = service.problem()
        if problem:
            raise ToolError(ctx.t(f"שליטה במחשב לא זמינה: {problem}", f"Computer control is unavailable: {problem}"))
        if service.busy():
            raise ToolError(ctx.t("כבר רצה משימת שליטה במחשב אחת", "a computer-control session is already running"))
        window = params.window.strip()
        await service.note_working_window(window)    # before the approval comes up in front of it
        service.warm_up(ctx.services.settings.security.confirm_timeout_s + 60.0)   # load the model meanwhile
        goal = params.goal.strip()
        summary = ctx.t(f"שליטה במחשב: {goal}", f"Use the computer: {goal}")
        plan = ActionPlan(summary=summary[:160], risk=RiskLevel.HIGH, always_confirm=True,
                          capabilities=[C.SCREEN_READ, C.INPUT_CONTROL],
                          risk_reasons=[ctx.t("JARVIS יזיז את העכבר ויקליד; אפשר לעצור ב-Esc פעמיים",
                                              "JARVIS will move the mouse and type; press Esc twice to stop")],
                          details={"window": window} if window else {})
        remote = (ctx.services.settings.computer.server_url or "").strip()
        if remote:
            plan.sends_data = True
            plan.destination = remote
            plan.risk_reasons.append(ctx.t(f"צילומי המסך נשלחים לשרת מודל חיצוני ({remote})",
                                           f"screenshots are sent to an external model server ({remote})"))
        return plan

    async def run(self, params: UseComputerParams, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        steps = params.max_steps or ctx.services.settings.computer.max_steps
        outcome = await service.run(params.goal.strip(), ctx, max_steps=steps, lang=ctx.lang)
        data = {"reason": outcome.reason, "steps": outcome.steps}
        if outcome.reason == "answer":
            return ToolResult(ok=True, summary=outcome.content or ctx.t("סיימתי.", "Done."),
                              data=data, actions=outcome.actions)
        if outcome.stopped_by_user:
            text = ctx.t("עצרת את JARVIS (Esc פעמיים).", "You stopped JARVIS (Esc twice).") \
                if outcome.reason == "user_stop" else ctx.t("עצרתי כי לקחת את העכבר.", "Stopped because you took back the mouse.")
            return ToolResult(ok=False, summary=text, error=text, denied="stopped",
                              data={**data, "code": "user_stopped"}, actions=outcome.actions)
        if outcome.reason == "declined":     # the user said no to one of its actions: the whole task stops
            text = ctx.t("לא אישרת את הפעולה, אז עצרתי.", "You did not approve the action, so I stopped.")
            return ToolResult(ok=False, summary=text, error=outcome.error or text, denied="confirmation",
                              data={**data, "code": "confirmation_denied"}, actions=outcome.actions)
        if outcome.reason in ("max_steps", "time_limit"):
            text = ctx.t("לא הספקתי לסיים את המשימה על המסך.", "I could not finish the on-screen task in time.")
            return ToolResult.fail(text, data={**data, "code": "computer_incomplete"}, actions=outcome.actions)
        error = outcome.error or outcome.reason
        return ToolResult.fail(ctx.t(f"שליטה במחשב נכשלה: {error}", f"Computer control failed: {error}"),
                               data={**data, "code": "computer_error"})


# ============================================================================ computer_act (one action)
class ComputerActParams(BaseModel):
    action: Literal["click", "double_click", "right_click", "type", "press_keys", "scroll", "drag"]
    x: int | None = None
    y: int | None = None
    to_x: int | None = None
    to_y: int | None = None
    text: str = ""
    press_enter: bool = False
    keys: str = ""
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = Field(default=3, ge=1, le=15)
    element: str = ""
    session_id: str = ""


class ComputerActTool(Tool):
    name = "computer_act"
    description = "Internal: one mouse or keyboard action inside a computer-control session."
    capabilities = (C.SCREEN_READ, C.INPUT_CONTROL)
    Params = ComputerActParams
    risk = RiskLevel.LOW           # the base; the policy raises it per what is under the pointer
    timeout_s = 90.0

    async def plan(self, params: ComputerActParams, ctx: ToolContext) -> ActionPlan:
        service = _service(ctx)
        session = service.active
        if session is None or (params.session_id and params.session_id != session.id):
            raise PermissionDenied(ctx.t("אין משימת שליטה פעילה", "no active computer-control session is running"),
                                   details={"tool": self.name})
        backend = service.backend
        target = await self._target(backend, params)
        combos = self._combos(params, ctx)
        pending = session.pending.get(target.hwnd, "") if hasattr(session, "pending") else ""
        verdict = policy.assess(params.action, target, lang=ctx.lang, element=params.element, text=params.text,
                                press_enter=params.press_enter, combos=combos, pending_command=pending)
        if verdict.refused:
            raise PermissionDenied(ctx.t(f"הפעולה נחסמה: {verdict.refused}.", f"Refused: {verdict.refused}."),
                                   details={"tool": self.name})
        ctx.extra["computer_target"] = target
        ctx.extra["computer_combos"] = combos
        return ActionPlan(
            summary=self._summary(params, target, ctx), risk=verdict.risk, risk_reasons=verdict.reasons,
            irreversible=verdict.irreversible, capabilities=[C.SCREEN_READ, C.INPUT_CONTROL],
            remember_scope=f"{target.process or 'screen'}:{params.action}:{verdict.rule}",
            details={"window": policy.window_label(target, ctx.lang), "element": target.element or params.element},
        )

    async def run(self, params: ComputerActParams, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        session = service.active
        if session is None:
            return ToolResult.fail(ctx.t("אין משימת שליטה פעילה", "no active computer-control session"))
        await service.ensure_covered()            # hide JARVIS again after any approval was shown
        backend = service.backend
        planned: Target | None = ctx.extra.get("computer_target")
        now = await self._target(backend, params)
        if planned is not None and not now.same_window(planned):
            return ToolResult.fail(ctx.t("החלון השתנה לפני הפעולה; מדלג", "the window changed before the action; skipped"))
        complete = await self._perform(backend, params, session)
        if params.action in ("type", "press_keys") and policy.runs_commands(now):
            text = params.text if params.action == "type" else ""
            pending = session.pending.get(now.hwnd, "")
            if complete:
                session.pending[now.hwnd] = policy.pending_after(pending, text,
                                                                 params.press_enter or params.action == "press_keys")
            else:
                # Stopped partway: any of it may be on the line and none of it is known to have run, so the
                # next Enter there is judged against all of it.
                session.pending[now.hwnd] = (pending + text).replace("\n", " ")
        if not complete:
            if session.stop.is_set():
                return ToolResult.fail(ctx.t("נעצר ב-Esc", "stopped with Esc"))
            return ToolResult.fail(ctx.t("החלון הפעיל התחלף באמצע, אז JARVIS הפסיק להקליד",
                                         "the focused window changed midway, so JARVIS stopped typing there"))
        return ToolResult(ok=True, summary=self._summary(params, now, ctx))

    # ------------------------------------------------------------------ helpers
    async def _target(self, backend: Any, params: ComputerActParams) -> Target:
        if params.action in POINTER and params.x is not None and params.y is not None:
            return await asyncio.to_thread(backend.target_at, params.x, params.y)
        if params.action == "scroll" and params.x is not None and params.y is not None:
            return await asyncio.to_thread(backend.target_at, params.x, params.y)
        return await asyncio.to_thread(backend.focus_target)

    def _combos(self, params: ComputerActParams, ctx: ToolContext) -> list[list[str]]:
        if params.action != "press_keys":
            return []
        try:
            return parse_keys(params.keys)
        except ValueError as exc:
            raise PermissionDenied(ctx.t(f"צירוף מקשים לא מוכר: {exc}", f"unknown keys: {exc}"),
                                   details={"tool": self.name}) from None

    async def _perform(self, backend: Any, params: ComputerActParams, session: Any) -> bool:
        """Do the action. False when typing stopped early (Esc, or the focus left the window that was checked)."""
        a = params
        if a.action in ("click", "double_click", "right_click"):
            button = "right" if a.action == "right_click" else "left"
            count = 2 if a.action == "double_click" else 1
            await asyncio.to_thread(backend.click, a.x or 0, a.y or 0, button=button, count=count)
        elif a.action == "type":
            # The Enter goes in with the text, so it too reaches only the window that was checked.
            text = a.text + ("\n" if a.press_enter else "")
            return bool(await asyncio.to_thread(backend.type_text, text, session.stop))
        elif a.action == "press_keys":
            return bool(await asyncio.to_thread(backend.press_keys, parse_keys(a.keys)))
        elif a.action == "scroll":
            await asyncio.to_thread(backend.scroll, a.x, a.y, a.direction, a.amount)
        elif a.action == "drag":
            await asyncio.to_thread(backend.drag, a.x or 0, a.y or 0, a.to_x or 0, a.to_y or 0)
        return True

    def _summary(self, params: ComputerActParams, target: Target, ctx: ToolContext) -> str:
        where = policy.window_label(target, ctx.lang)
        what = target.element or params.element
        a = params.action
        if a in ("click", "double_click", "right_click"):
            verb = ctx.t({"click": "לוחץ על", "double_click": "לוחץ פעמיים על", "right_click": "לחיצה ימנית על"}[a],
                         {"click": "Click", "double_click": "Double-click", "right_click": "Right-click"}[a])
            thing = f"'{what}' " if what else ""
            return ctx.t(f"{verb} {thing}ב{where}", f"{verb} {thing}in {where}").replace("  ", " ")
        if a == "type":
            tail = ctx.t(" ולוחץ Enter", " and press Enter") if params.press_enter else ""
            return ctx.t(f"מקליד ב{where}{tail}", f"Type in {where}{tail}")
        if a == "press_keys":
            return ctx.t(f"לוחץ {params.keys} ב{where}", f"Press {params.keys} in {where}")
        if a == "scroll":
            return ctx.t(f"גולל ב{where}", f"Scroll in {where}")
        return ctx.t(f"גורר ב{where}", f"Drag in {where}")


COMPUTER_TOOLS = (UseComputerTool, ComputerActTool)

__all__ = ["COMPUTER_TOOLS", "ComputerActTool", "UseComputerTool"]
