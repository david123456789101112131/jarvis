"""The "Apps & tools" screen's tools: what is installed, one look at the screen, and one previewed step.

* `list_installed_apps` only reads the list of installed apps (safe, never asked); the open windows come
  from `running_apps`;
* `read_screen` looks once, when the user clicks, after the standard approval: one screenshot kept in memory
  only (never written to disk, never in a job's history), the window in front and its focused element (UI
  Automation), and, with the local model, what the screen shows or the one next step it proposes. It never
  clicks or types. Its approval can be remembered for the rest of that apps task, never beyond it. Without
  the model there is no screenshot at all, only the window and element facts;
* `do_screen_step` does one proposed step and always asks first, showing the action, the window, the element
  and why it may be risky. Right before acting it checks that the same window and element are still there,
  then acts through `computer_act`, so JARVIS's own rules judge the action again on the live screen (and may
  ask once more, or refuse). Esc twice, the job's Stop button and the kill switch stop it.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from jarvis.common.enums import Capability, RiskLevel
from jarvis.common.errors import PermissionDenied, ToolError
from jarvis.common.i18n import t
from jarvis.computer import policy
from jarvis.computer.backend import Shot, Target, parse_keys, plain
from jarvis.computer.model import ACTION_NAMES, Conversation, HoloClient, ModelError, Step, to_pixels
from jarvis.computer.session import MAX_IMAGE_WIDTH
from jarvis.host import catalog
from jarvis.security.redaction import default_redactor
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult

C = Capability
POINTER = ("click", "double_click", "right_click", "drag")
ACTS = ("click", "double_click", "right_click", "type", "press_keys", "scroll", "drag")
# Steps whose consequence the preview spells out before the user approves (the policy rule that raised them).
PREVIEW_RULES = frozenset({"send", "delete", "wipe", "pay", "install", "discard", "account", "command", "personal",
                           "program", "paste", "run_box"})
LANGUAGES = {"he": "Hebrew", "en": "English"}


def _service(ctx: ToolContext) -> Any:
    service = ctx.services.extras.get("computer")
    if service is None:
        raise ToolError(ctx.t("שליטה במחשב לא זמינה כאן", "computer control is not available here"))
    return service


def screen_problem(service: Any, lang: str) -> str | None:
    """Why JARVIS cannot look at the screen here (None: it can): computer control off, or not Windows."""
    if not service.settings.enabled:
        return t(lang, "השליטה במחשב כבויה בהגדרות", "computer control is turned off in the settings")
    problem = service.backend.problem()
    if problem is None:
        return None
    if sys.platform != "win32":
        return t(lang, "זה עובד רק ב-Windows", "this works on Windows only")
    return t(lang, f"Windows לא נותן גישה למסך: {problem}", f"Windows gives no access to the screen: {problem}")


def model_problem(service: Any, lang: str) -> str | None:
    """Why the local model cannot look at the screen (None: it can)."""
    problem = screen_problem(service, lang)
    if problem is not None:
        return problem
    if service.server.problem() is not None:
        return t(lang, "המודל המקומי לא מותקן (אפשר להתקין: jarvis computer install)",
                 "the local model is not installed (install it with: jarvis computer install)")
    return None


def _remote(ctx: ToolContext) -> str:
    return (ctx.services.settings.computer.server_url or "").strip()


def facts(target: Target, lang: str) -> dict[str, Any]:
    """What Windows itself reports about a window and its element (never the model's reading of a picture)."""
    found = policy.sensitive(target)
    return {"window": policy.window_label(target, lang), "title": plain(target.title), "program": target.process,
            "element": plain(target.element), "control": target.control, "password": target.password,
            "own": target.own, "sensitive": t(lang, found[1], found[0]) if found else None}


def _off_limits(target: Target, lang: str) -> str | None:
    """Why JARVIS does not look into the window in front (its own, sign-in, security, passwords), or None."""
    if target.own:
        return t(lang, "החלון שבחזית הוא של JARVIS עצמו, אז לא היה על מה להסתכל",
                 "JARVIS's own window was in front, so there was nothing to look at")
    found = policy.sensitive(target)
    if found is not None:
        return t(lang, f"בחזית יש {found[1]}, ו-JARVIS לא מסתכל לתוכו", f"{found[0]} is in front, and JARVIS does not "
                                                                    "look into it")
    return None


# ============================================================================ the installed apps
class ListInstalledAppsParams(BaseModel):
    query: str = Field(default="", max_length=100, description="Part of an app's name, in Hebrew or English.")
    refresh: bool = Field(default=False, description="Build the list again first (new installs).")
    limit: int = Field(default=500, ge=1, le=2000)


def matches(app: Any, query: str) -> bool:
    q = " ".join(query.casefold().split())
    if not q:
        return True
    names = [app.name, *(getattr(app, "aliases", ()) or ())]
    if any(q in str(n).casefold() for n in names):
        return True
    entry = catalog.find_app(query)              # "כרום" finds Google Chrome
    return entry is not None and getattr(app, "id", None) == entry.id


class ListInstalledAppsTool(Tool):
    name = "list_installed_apps"
    description = "List the apps installed on this computer (the Start menu), or only those whose name matches."
    capabilities = (C.PROCESS_LIST,)
    Params = ListInstalledAppsParams
    read_only = True
    risk = RiskLevel.SAFE
    timeout_s = 120.0                            # rebuilding the list reads the whole Start menu

    async def plan(self, params: ListInstalledAppsParams, ctx: ToolContext) -> ActionPlan:
        return ActionPlan(summary=ctx.t("רשימת התוכנות המותקנות", "List the installed apps"), risk=RiskLevel.SAFE)

    async def run(self, params: ListInstalledAppsParams, ctx: ToolContext) -> ToolResult:
        host = ctx.services.host
        if host is None:
            return ToolResult.fail(ctx.t("אין כאן גישה לתוכנות המותקנות", "The installed apps cannot be read here"))
        apps = await host.installed_apps(refresh=params.refresh)
        found = [a for a in apps if matches(a, params.query)]
        rows = [{"name": a.name, "kind": a.kind, "source": a.source, "id": a.id} for a in found[:params.limit]]
        return ToolResult(summary=ctx.n(len(found), "תוכנה אחת", "תוכנות", "app", "apps"),
                          data={"apps": rows, "total": len(apps), "matched": len(found)})


# ============================================================================ one look at the screen
class ReadScreenParams(BaseModel):
    purpose: Literal["look", "suggest"] = "look"
    goal: str = Field(default="", max_length=1000, description="For a suggestion: what the user wants done.")
    corrections: list[str] = Field(default_factory=list, max_length=8,
                                   description="What the user said JARVIS got wrong about the screen.")
    done: list[str] = Field(default_factory=list, max_length=12, description="Steps already done in this task.")
    window: str = Field(default="", max_length=200, description=(
        "The open window to look at (its title or program); empty: the one the user was working in."))

    @model_validator(mode="after")
    def _goal_for_a_suggestion(self) -> ReadScreenParams:
        if self.purpose == "suggest" and len(self.goal.strip()) < 2:
            raise ValueError("a suggestion needs the task: what should be done")
        self.corrections = [c.strip()[:300] for c in self.corrections if c.strip()]
        self.done = [d.strip()[:200] for d in self.done if d.strip()]
        return self


class ReadScreenTool(Tool):
    name = "read_screen"
    description = ("Internal (the Apps & tools screen): look at the screen once, on the user's click. Reports the "
                   "window in front and its focused element and, with the local model, what the screen shows or "
                   "the one next step it proposes. It never clicks or types.")
    capabilities = (C.SCREEN_READ,)
    Params = ReadScreenParams
    risk = RiskLevel.HIGH                        # the standard approval; rememberable for this apps task only
    read_only = True                             # it changes nothing
    timeout_s = 300.0                            # loading the model (about 20 s), the look and its answer

    async def plan(self, params: ReadScreenParams, ctx: ToolContext) -> ActionPlan:
        service = _service(ctx)
        problem = screen_problem(service, ctx.lang)
        if problem:
            raise ToolError(ctx.t(f"אין גישה למסך: {problem}", f"The screen cannot be read: {problem}"))
        if service.busy():
            raise ToolError(ctx.t("שליטה במחשב כבר רצה כרגע (Esc פעמיים עוצר אותה)",
                                  "computer control is already running (Esc twice stops it)"))
        without = model_problem(service, ctx.lang)
        if params.purpose == "suggest" and without:
            raise ToolError(ctx.t(f"כדי להציע צעד צריך את המודל המקומי: {without}",
                                  f"Suggesting a step needs the local model: {without}"))
        window = params.window.strip()
        if not await service.note_working_window(window) and window:      # before the approval covers it
            raise ToolError(ctx.t(f"החלון \"{window}\" כבר לא פתוח", f"The window \"{window}\" is no longer open"))
        model = without is None
        if model:
            service.warm_up(ctx.services.settings.security.confirm_timeout_s + 60.0)   # load it meanwhile
        remote = _remote(ctx)
        if params.purpose == "suggest":
            summary = ctx.t(f"להסתכל על המסך ולהציע את הצעד הבא: {params.goal.strip()}",
                            f"Look at the screen and suggest the next step: {params.goal.strip()}")
        else:
            summary = ctx.t("להסתכל על המסך פעם אחת", "Look at the screen once")
        if model:
            reasons = [ctx.t("צילום מסך אחד עכשיו: JARVIS יראה את כל מה שמוצג במסך",
                             "one screenshot now: JARVIS will see everything that is on the screen"),
                       ctx.t("התמונה נשארת בזיכרון ונמחקת מיד: לא נשמרת לדיסק ולא בהיסטוריה",
                             "the picture stays in memory and is dropped at once: not saved to disk or history")]
            reasons.append(ctx.t(f"התמונה נשלחת לשרת המודל {remote}", f"the picture is sent to the model server {remote}")
                           if remote else ctx.t("הניתוח במחשב הזה בלבד; שום דבר לא נשלח",
                                                "it is analysed on this computer only; nothing is sent"))
        else:
            reasons = [ctx.t("JARVIS יקרא את שם החלון שבחזית ואת הרכיב שבפוקוס, בלי צילום מסך",
                             "JARVIS reads the name of the window in front and its focused element, with no "
                             "screenshot")]
        reasons.append(ctx.t("רק מבט: שום לחיצה או הקלדה", "only a look: no click and no typing"))
        details: dict[str, Any] = {
            ctx.t("מה נקרא", "What is read"): ctx.t("צילום מסך אחד, החלון שבחזית והרכיב שבפוקוס",
                                                    "one screenshot, the window in front, the focused element")
            if model else ctx.t("החלון שבחזית והרכיב שבפוקוס", "the window in front and the focused element"),
        }
        if window:
            details[ctx.t("חלון", "Window")] = window
        return ActionPlan(summary=summary[:200], risk=RiskLevel.HIGH, risk_reasons=reasons,
                          capabilities=[C.SCREEN_READ], details=details, sends_data=bool(remote and model),
                          destination=remote if remote and model else None,
                          remember_scope=ctx.t("מבט במסך", "screen looks"))

    async def run(self, params: ReadScreenParams, ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        async with contextlib.AsyncExitStack() as stack:
            client: HoloClient | None = None
            failed = ""
            if model_problem(service, ctx.lang) is None:
                await ctx.progress(0.05, ctx.t("מכין את המודל המקומי…", "Getting the local model ready…"))
                try:      # loaded first, while JARVIS is still on screen showing it
                    client = await stack.enter_async_context(service.model_client())
                except (RuntimeError, OSError, ModelError) as exc:
                    if params.purpose == "suggest":
                        raise ToolError(ctx.t(f"המודל המקומי לא עלה: {exc}", f"The local model did not start: {exc}")) \
                            from None
                    failed = str(exc)                         # a look still reports the window and element
            return await self._look(params, ctx, service, client, failed)

    async def _look(self, params: ReadScreenParams, ctx: ToolContext, service: Any, client: HoloClient | None,
                    failed: str = "") -> ToolResult:
        backend = service.backend
        await ctx.progress(0.3, ctx.t("מסתכל על המסך…", "Looking at the screen…"))
        shot: Shot | None = None
        proposal: dict[str, Any] | None = None
        error = ""
        async with service.screen_turn(ctx):                  # JARVIS hidden, the task's window in front
            front = await asyncio.to_thread(backend.focus_target)
            blocked = _off_limits(front, ctx.lang)
            if client is not None and blocked is None:
                shot = await asyncio.to_thread(backend.capture, MAX_IMAGE_WIDTH)
            if params.purpose == "suggest" and shot is not None and client is not None:
                await ctx.progress(0.55, ctx.t("המודל המקומי חושב על הצעד הבא…", "The local model is thinking…"))
                try:
                    step = await client.step(_conversation(params, shot, ctx.lang))
                except ModelError as exc:
                    error = str(exc)
                else:   # what is at that point is read now, while the screen is as the model saw it
                    proposal = await propose(step, shot, front, backend, ctx.lang)
        size = {"width": shot.screen_width, "height": shot.screen_height} if shot is not None else None
        data: dict[str, Any] = {"purpose": params.purpose, "facts": facts(front, ctx.lang), "blocked": blocked,
                                "analysed": "model" if shot is not None else "facts", "screen": size,
                                "description": None, "model_error": failed or None, "proposal": None}
        if params.purpose == "suggest":
            shot = None                                       # the picture goes now
            if blocked is not None:
                return ToolResult.fail(ctx.t(f"לא הצעתי צעד: {blocked}", f"No step suggested: {blocked}"), data=data)
            if proposal is None:
                return ToolResult.fail(ctx.t(f"המודל המקומי לא הציע צעד: {error}",
                                             f"The local model suggested no step: {error}"), data=data)
            data["proposal"] = proposal
            return ToolResult(summary=ctx.t("הצעתי צעד אחד (שום דבר לא בוצע)", "Suggested one step (nothing was done)"),
                              data=data)
        if shot is not None and client is not None:
            await ctx.progress(0.6, ctx.t("המודל המקומי מתאר את המסך…", "The local model is describing the screen…"))
            try:
                data["description"] = await client.describe(shot, lang=ctx.lang, notes=params.corrections)
            except ModelError as exc:
                data["model_error"] = str(exc)
            finally:
                shot = None                                   # never kept: not on disk, not in the result
        program = front.process or policy.window_label(front, ctx.lang)
        return ToolResult(summary=ctx.t(f"הסתכלתי על המסך ({program})", f"Looked at the screen ({program})"), data=data)


def _conversation(params: ReadScreenParams, shot: Shot, lang: str) -> Conversation:
    lines = [params.goal.strip()]
    if params.corrections:
        lines.append("The user corrected what was understood about the screen: " + " | ".join(params.corrections))
    if params.done:
        lines.append("Steps already done for this task: " + " | ".join(params.done))
    lines.append("Propose only the single next action: JARVIS shows it to the user, who decides whether to do it. "
                 f"Write your reply text in {LANGUAGES.get(lang, 'English')}.")
    conv = Conversation("\n".join(lines))
    conv.observe(shot)
    return conv


async def propose(step: Step, shot: Shot, front: Target, backend: Any, lang: str) -> dict[str, Any]:
    """The model's next action as JARVIS shows it: in words, what it would reach (window and element, as Windows
    reports them), how risky JARVIS's rules judge it, and whether it would be refused. Nothing is done here."""
    name, a = step.tool, step.arguments
    base: dict[str, Any] = {"understood": step.content.strip()[:600], "model_element": str(a.get("element", ""))[:200]}
    if step.is_answer:
        return {**base, "kind": "answer", "answer": str(a.get("content", "") or step.content)[:1000]}
    if name not in ACTION_NAMES:
        return {**base, "kind": "unknown", "answer": t(lang, f"המודל הציע פעולה ש-JARVIS לא מכיר ({name})",
                                                       f"the model proposed an action JARVIS does not know ({name})")}
    if name == "wait":
        return {**base, "kind": "wait", "answer": t(lang, "המסך עדיין נטען: כדאי לחכות רגע ולבקש הצעה חדשה",
                                                    "the screen is still loading: wait a moment and ask again")}
    if name in ("open_app", "goto"):
        what = str(a.get("name" if name == "open_app" else "url", "")).strip()[:300]
        launch = {"action": name, "name": what} if name == "open_app" else {"action": name, "url": what}
        words = t(lang, f"לפתוח את {what}", f"Open {what}")
        reasons = [] if name == "open_app" else [t(lang, "פותח כתובת אינטרנט בדפדפן", "opens a web address in the browser")]
        return {**base, "kind": "action", "action": launch, "words": words, "window": "", "program": "", "element": "",
                "control": "", "target": None, "risk": "low", "reasons": reasons, "refused": None,
                "irreversible": False, "asks_again": False, "preview": None}

    w, h = shot.screen_width, shot.screen_height
    action: dict[str, Any] = {"action": name, "element": base["model_element"]}
    if name in POINTER or (name == "scroll" and a.get("x") is not None and a.get("y") is not None):
        action["x"], action["y"] = to_pixels(a.get("x"), w), to_pixels(a.get("y"), h)
        target = await asyncio.to_thread(backend.target_at, action["x"], action["y"])
    else:
        target = front
    if name == "drag":
        action["to_x"], action["to_y"] = to_pixels(a.get("to_x"), w), to_pixels(a.get("to_y"), h)
    if name == "type":
        action["text"], action["press_enter"] = str(a.get("text", ""))[:2000], bool(a.get("press_enter", False))
    if name == "press_keys":
        action["keys"] = str(a.get("keys", ""))[:100]
    if name == "scroll":
        action["direction"] = str(a.get("direction", "down")) if a.get("direction") in ("up", "down", "left", "right") \
            else "down"
        action["amount"] = max(1, min(15, int(a.get("amount", 3) or 3)))
    combos: list[list[str]] = []
    refused: str | None = None
    if name == "press_keys":
        try:
            combos = parse_keys(action["keys"])
        except ValueError as exc:
            refused = t(lang, f"צירוף מקשים לא מוכר: {exc}", f"unknown keys: {exc}")
    verdict = policy.assess(name, target, lang=lang, element=action["element"], text=action.get("text", ""),
                            press_enter=action.get("press_enter", False), combos=combos)
    refused = refused or verdict.refused
    if not target.hwnd and refused is None:
        refused = t(lang, "JARVIS לא זיהה לאיזה חלון הצעד הזה מגיע", "JARVIS could not tell which window this step reaches")
    words = describe_action(action, target, lang)
    return {**base, "kind": "action", "action": action, "words": words, "window": policy.window_label(target, lang),
            "program": target.process, "element": plain(target.element), "control": target.control,
            "target": {"hwnd": target.hwnd, "pid": target.pid, "process": target.process, "title": target.title,
                       "class_name": target.class_name, "element": target.element, "control": target.control,
                       "password": target.password},
            "risk": verdict.risk.name.lower(), "reasons": verdict.reasons, "refused": refused,
            "irreversible": verdict.irreversible, "rule": verdict.rule,
            "asks_again": refused is None and (verdict.risk >= RiskLevel.HIGH or verdict.irreversible),
            "preview": None if refused else preview(verdict, words, lang)}


def preview(verdict: policy.Verdict, words: str, lang: str) -> str | None:
    """For a step that sends, deletes, publishes, pays or cannot be undone: what will happen, in one line."""
    if verdict.rule not in PREVIEW_RULES and not verdict.irreversible:
        return None
    why = "; ".join(verdict.reasons) or t(lang, "פעולה רגישה", "a sensitive action")
    line = t(lang, f"מה יקרה: {words}, כלומר {why}.", f"What will happen: {words}, which {why}.")
    if verdict.irreversible:
        line += t(lang, " אי אפשר לבטל את זה אחרי שזה קורה.", " It cannot be undone once it happens.")
    return line


def _in_he(where: str) -> str:
    """Hebrew "in <window>": בשולחן העבודה, במסך, ב-Untitled - Notepad (a hyphen before a non-Hebrew name)."""
    if where == "המסך":
        return "במסך"
    return f"ב{where}" if "\u0590" <= where[:1] <= "\u05ff" else f"ב-{where}"


def describe_action(action: dict[str, Any], target: Target, lang: str) -> str:
    """One proposed action in words, from what Windows reports is there (the model's words only as a fallback)."""
    a = action.get("action", "")
    where = policy.window_label(target, lang)
    within = _in_he(where)
    what = plain(target.element) or str(action.get("element", "")).strip()
    thing = f" '{what}'" if what else ""
    if a in ("click", "double_click", "right_click"):
        he = {"click": "ללחוץ על", "double_click": "ללחוץ פעמיים על", "right_click": "ללחוץ לחיצה ימנית על"}[a]
        en = {"click": "Click", "double_click": "Double-click", "right_click": "Right-click"}[a]
        return t(lang, f"{he}{thing or ' המקום הזה'} {within}", f"{en}{thing or ' that spot'} in {where}")
    if a == "type":
        text = default_redactor().redact_text(str(action.get("text", "")))
        shown = text if len(text) <= 80 else text[:79] + "…"
        tail = t(lang, " וללחוץ Enter", " and press Enter") if action.get("press_enter") else ""
        return t(lang, f"להקליד \"{shown}\" {within}{tail}", f"Type \"{shown}\" in {where}{tail}")
    if a == "press_keys":
        return t(lang, f"ללחוץ {action.get('keys', '')} {within}", f"Press {action.get('keys', '')} in {where}")
    if a == "scroll":
        d = str(action.get("direction", "down"))
        he_dir = {"up": "למעלה", "down": "למטה", "left": "שמאלה", "right": "ימינה"}.get(d, d)
        return t(lang, f"לגלול {he_dir} {within}", f"Scroll {d} in {where}")
    if a == "drag":
        return t(lang, f"לגרור{thing} {within}", f"Drag{thing} in {where}")
    if a == "open_app":
        return t(lang, f"לפתוח את {action.get('name', '')}", f"Open {action.get('name', '')}")
    if a == "goto":
        return t(lang, f"לפתוח את {action.get('url', '')}", f"Open {action.get('url', '')}")
    return a


# ============================================================================ one previewed step
class ScreenStepParams(BaseModel):
    action: Literal["click", "double_click", "right_click", "type", "press_keys", "scroll", "drag", "open_app", "goto"]
    x: int | None = None
    y: int | None = None
    to_x: int | None = None
    to_y: int | None = None
    text: str = Field(default="", max_length=2000)
    press_enter: bool = False
    keys: str = Field(default="", max_length=100)
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = Field(default=3, ge=1, le=15)
    element: str = Field(default="", max_length=200, description="The model's own words for the element.")
    name: str = Field(default="", max_length=300, description="open_app: the app.")
    url: str = Field(default="", max_length=2000, description="goto: the address.")
    # What JARVIS found there when it proposed the step; it must still be there when the step runs.
    window_id: int = Field(default=0, ge=0)
    window_pid: int = Field(default=0, ge=0)
    program: str = Field(default="", max_length=260)
    title: str = Field(default="", max_length=500)
    class_name: str = Field(default="", max_length=260)
    expect_element: str = Field(default="", max_length=300)
    expect_control: str = Field(default="", max_length=60)
    expect_password: bool = False

    def target(self) -> Target:
        return Target(hwnd=self.window_id, pid=self.window_pid, process=self.program, title=self.title,
                      class_name=self.class_name, element=self.expect_element, control=self.expect_control,
                      password=self.expect_password)

    def act_params(self) -> dict[str, Any]:
        """The same action, as `computer_act` takes it."""
        p: dict[str, Any] = {"action": self.action}
        if self.action in POINTER or (self.action == "scroll" and self.x is not None and self.y is not None):
            p.update(x=self.x, y=self.y)
        if self.action in POINTER:
            p["element"] = self.element
        if self.action == "drag":
            p.update(to_x=self.to_x, to_y=self.to_y)
        if self.action == "type":
            p.update(text=self.text, press_enter=self.press_enter)
        if self.action == "press_keys":
            p["keys"] = self.keys
        if self.action == "scroll":
            p.update(direction=self.direction, amount=self.amount)
        return p


class DoScreenStepTool(Tool):
    name = "do_screen_step"
    description = ("Internal (the Apps & tools screen): do the one step JARVIS proposed and the user chose, after "
                   "their approval, if the same window and element are still there.")
    capabilities = (C.SCREEN_READ, C.INPUT_CONTROL)
    Params = ScreenStepParams
    risk = RiskLevel.HIGH
    always_confirm = True                        # asked every time; never covered by a remembered approval
    timeout_s = None                             # a second approval may wait for the user; Stop ends it

    async def plan(self, params: ScreenStepParams, ctx: ToolContext) -> ActionPlan:
        words = describe_action(params.model_dump(), params.target(), ctx.lang)
        if params.action in ("open_app", "goto"):
            return ActionPlan(summary=words[:200], risk=RiskLevel.HIGH, always_confirm=True,
                              capabilities=[C.PROCESS_LAUNCH],
                              risk_reasons=[ctx.t("הצעד שהמודל הציע ושבחרת לבצע", "the step the model proposed and "
                                                                                  "you chose to do")])
        service = _service(ctx)
        problem = screen_problem(service, ctx.lang)
        if problem:
            raise ToolError(ctx.t(f"אי אפשר לבצע צעד במסך: {problem}", f"The step cannot be done: {problem}"))
        if not params.window_id:
            raise PermissionDenied(ctx.t("לא ידוע לאיזה חלון הצעד מגיע", "it is not known which window the step reaches"),
                                   details={"tool": self.name})
        target = params.target()
        combos: list[list[str]] = []
        if params.action == "press_keys":
            try:
                combos = parse_keys(params.keys)
            except ValueError as exc:
                raise PermissionDenied(ctx.t(f"צירוף מקשים לא מוכר: {exc}", f"unknown keys: {exc}"),
                                       details={"tool": self.name}) from None
        verdict = policy.assess(params.action, target, lang=ctx.lang, element=params.element, text=params.text,
                                press_enter=params.press_enter, combos=combos)
        if verdict.refused:
            raise PermissionDenied(ctx.t(f"הצעד נחסם: {verdict.refused}.", f"Refused: {verdict.refused}."),
                                   details={"tool": self.name})
        reasons = [*verdict.reasons,
                   ctx.t("רגע לפני הפעולה JARVIS בודק שזה אותו חלון ואותו רכיב; אם משהו השתנה, לא ייעשה כלום",
                         "right before acting JARVIS checks it is the same window and element; if anything changed, "
                         "nothing is done"),
                   ctx.t("Esc פעמיים עוצר", "Esc twice stops it")]
        if verdict.risk >= RiskLevel.HIGH or verdict.irreversible:
            reasons.append(ctx.t("כללי הבטיחות יבקשו את אישורך פעם נוספת רגע לפני הפעולה עצמה",
                                 "JARVIS's safety rules will ask you once more right before the action itself"))
        what = plain(params.expect_element) or params.element
        details = {ctx.t("פעולה", "Action"): words, ctx.t("חלון", "Window"): policy.window_label(target, ctx.lang)}
        if what:
            details[ctx.t("רכיב", "Element")] = what
        return ActionPlan(summary=words[:200], risk=RiskLevel.HIGH, always_confirm=True,
                          irreversible=verdict.irreversible, capabilities=[C.SCREEN_READ, C.INPUT_CONTROL],
                          risk_reasons=reasons, details=details)

    async def run(self, params: ScreenStepParams, ctx: ToolContext) -> ToolResult:
        await ctx.progress(0.1, ctx.t("מבצע את הצעד…", "Doing the step…"))
        if params.action == "open_app":
            return await ctx.call("open_app", name=params.name)
        if params.action == "goto":
            return await ctx.call("open_url", url=params.url)
        service = _service(ctx)
        backend = service.backend
        async with service.screen_turn(ctx, window=params.window_id, act=True) as turn:
            if params.action in POINTER or (params.action == "scroll" and params.x is not None and params.y is not None):
                now = await asyncio.to_thread(backend.target_at, params.x or 0, params.y or 0)
            else:
                now = await asyncio.to_thread(backend.focus_target)
            why = changed(params, now)
            if why is not None:
                return ToolResult.fail(ctx.t("המסך השתנה; צריך להציע צעד חדש", "The screen changed; ask for a new "
                                                                               "suggestion"),
                                       data={"code": "screen_changed", "changed": why})
            if turn is None or turn.stop.is_set():
                return ToolResult.fail(ctx.t("נעצר ב-Esc", "stopped with Esc"), data={"code": "stopped"})
            return await ctx.call("computer_act", **params.act_params(), session_id=turn.id)


def changed(params: ScreenStepParams, now: Target) -> str | None:
    """What is different from when the step was proposed ("window", "element"), or None."""
    if now.hwnd != params.window_id or now.pid != params.window_pid:
        return "window"
    if params.expect_element and plain(now.element) != plain(params.expect_element):
        return "element"
    if params.expect_control and now.control != params.expect_control:
        return "element"
    if now.password and not params.expect_password:
        return "element"
    return None


STEP_TOOLS: tuple[type[Tool], ...] = (ListInstalledAppsTool, ReadScreenTool, DoScreenStepTool)

__all__ = ["STEP_TOOLS", "DoScreenStepTool", "ListInstalledAppsTool", "ReadScreenTool", "changed", "describe_action",
           "facts", "matches", "model_problem", "preview", "propose", "screen_problem"]
