"""The "Apps & tools" screen: the ways JARVIS can work with this computer's apps, the installed apps and open
windows, and the jobs the screen starts (open an app, look at the screen, take a correction, suggest the next
step, do that step).

Everything that touches the computer runs through the tool executor as the System agent, with the standard
plan → permission → approval flow: `open_app`; `read_screen` (one look, approved first; that approval can be
remembered for the rest of one apps task, never beyond it); `do_screen_step` (approved every time, then judged
again by JARVIS's own rules on the live screen). Nothing happens in the background: every look is one
screenshot on the user's click, and a suggested step does nothing until the user picks it and approves.

An apps task groups what the user does toward one goal: its looks, the corrections they gave, the suggested
steps and the steps done. Its id is the executor's task id, so a remembered approval belongs to that task
alone; it is forgotten when the task ends or after half an hour without use. The screenshot itself never
reaches this module: the jobs keep only what Windows and the model said about it, in memory (their history
keeps a one-line summary).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn

from jarvis.common.errors import JarvisError, TaskCancelled, ToolError
from jarvis.common.i18n import t
from jarvis.common.ids import new_id
from jarvis.computer import policy
from jarvis.computer.backend import Target, plain
from jarvis.tools.step_tools import model_problem, screen_problem

if TYPE_CHECKING:
    from jarvis.app import JarvisApp
    from jarvis.jobs import Job, JobContext, JobStep
    from jarvis.tools.base import ToolResult

AGENT = "system"
TASK_IDLE_S = 30 * 60          # an apps task unused this long ends (and its remembered approval with it)
MAX_TASKS = 20
MAX_CORRECTIONS = 5
MAX_DONE = 12
POINTER = ("click", "double_click", "right_click", "drag")


class AppsRequestError(JarvisError):
    """A request the screen cannot do as asked (a suggestion already used, an empty task…)."""

    code = "apps_request"

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class AppsTask:
    """One goal the user works toward on the screen, and what JARVIS keeps for it (in memory only)."""

    id: str = field(default_factory=lambda: new_id("apps"))
    goal: str = ""
    corrections: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    suggestions: dict[str, dict[str, Any]] = field(default_factory=dict)   # suggestion job id -> its step
    used: float = field(default_factory=time.monotonic)

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "goal": self.goal, "corrections": list(self.corrections), "done": list(self.done)}


def _program(name: str) -> str:
    """'whatsapp.exe' -> 'whatsapp': how the job's own lines name a window (titles stay out of the history)."""
    stem = name.rsplit("\\", 1)[-1]
    return stem[:-4] if stem.lower().endswith(".exe") else stem


class AppsService:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app
        self._tasks: OrderedDict[str, AppsTask] = OrderedDict()

    def _lang(self, lang: str | None) -> str:
        return lang if lang in ("he", "en") else self.app.lang

    # ================================================================== what is possible here
    def status(self, lang: str | None = None) -> dict[str, Any]:
        """The ways of working with apps, each available or not and why, how to stop, and the limits."""
        lang = self._lang(lang)
        computer = self.app.computer
        windows_host = getattr(self.app.host, "platform", "") == "windows"
        screen = screen_problem(computer, lang)
        model = model_problem(computer, lang)
        remote = (computer.settings.server_url or "").strip()
        here = {"win32": "Windows", "linux": "Linux", "darwin": "macOS"}.get(sys.platform, sys.platform)
        not_windows = t(lang, f"זמין רק ב-Windows (JARVIS רץ כאן על {here})",
                        f"available on Windows only (JARVIS runs on {here} here)")
        run_on_windows = [t(lang, "להריץ את JARVIS על מחשב Windows", "Run JARVIS on a Windows computer")]

        def needs(problem: str | None) -> list[str]:
            if problem is None:
                return []
            if not computer.settings.enabled:
                return [t(lang, "להפעיל שליטה במחשב (בכפתור כאן או בהגדרות: computer.enabled)",
                          "Turn computer control on (the button here, or the setting computer.enabled)")]
            if computer.backend.problem() is not None:
                return run_on_windows
            return [t(lang, "להתקין את המודל המקומי פעם אחת: בחלון פקודה, jarvis computer install",
                      "Install the local model once: in a terminal, jarvis computer install"),
                    t(lang, "ואז להפעיל מחדש את JARVIS", "then restart JARVIS")]

        vision_detail = t(lang, "צילום מסך אחד בלחיצה שלך, מנותח במודל Holo במחשב הזה; שום דבר לא נשלח",
                          "one screenshot on your click, analysed by the Holo model on this computer; nothing is "
                          "sent")
        if remote:
            vision_detail = t(lang, f"צילום מסך אחד בלחיצה שלך, שנשלח לשרת המודל {remote}",
                              f"one screenshot on your click, sent to the model server {remote}")
        ways = [
            self._way("launch", t(lang, "פתיחת תוכנות מותקנות (תפריט התחל)", "Opening installed apps (Start menu)"),
                      t(lang, "JARVIS פותח את התוכנה שבחרת, רק בלחיצה שלך", "JARVIS opens the app you pick, only on "
                                                                       "your click"),
                      None if windows_host else not_windows, [] if windows_host else run_on_windows),
            self._way("windows", t(lang, "רשימת החלונות הפתוחים", "The open windows"),
                      t(lang, "כותרת ותוכנה של כל חלון, לקריאה בלבד; חלונות JARVIS לא מוצגים",
                        "each window's title and program, read only; JARVIS's own windows are left out"),
                      None if windows_host else not_windows, [] if windows_host else run_on_windows),
            self._way("uia", t(lang, "זיהוי רכיבים (UI Automation)", "Reading elements (UI Automation)"),
                      t(lang, "מה Windows עצמו מדווח: החלון שבחזית, הרכיב שבפוקוס או מתחת לנקודה, סוגו, והאם "
                              "זה שדה סיסמה", "what Windows itself reports: the window in front, the element in "
                                              "focus or under a point, its kind, and whether it is a password field"),
                      screen, needs(screen)),
            self._way("vision", t(lang, "מבט במודל המקומי", "A look with the local model"), vision_detail,
                      model, needs(model)),
            self._way("connectors", t(lang, "חיבורים רשמיים לאפליקציות (API)", "Official app connectors (APIs)"),
                      t(lang, "חיבור ישיר לאפליקציה דרך הממשק הרשמי שלה", "a direct link to an app through its "
                                                                         "official interface"),
                      t(lang, "אין כרגע אף חיבור רשמי מחובר; JARVIS עובד עם אפליקציות דרך המסך בלבד, באישור שלך",
                        "no official connector is connected; JARVIS works with apps through the screen only, with "
                        "your approval"), []),
        ]
        stop = [
            {"key": "stop_button", "label": t(lang, "כפתור עצור", "The Stop button"),
             "detail": t(lang, "בכל פעולה שרצה כאן", "on every action running here")},
            {"key": "esc_twice", "label": t(lang, "Esc פעמיים", "Esc twice"),
             "detail": t(lang, "עוצר מבט או צעד באמצע, גם כש-JARVIS מוסתר", "stops a look or a step midway, even "
                                                                          "while JARVIS is hidden")},
            {"key": "kill_switch", "label": t(lang, "מתג החירום", "The kill switch"),
             "detail": t(lang, "עוצר הכול מיד", "stops everything at once")},
        ]
        limits = [
            t(lang, "חלונות של תוכנות שרצות כמנהל (Administrator) לא מקבלים קלט מ-JARVIS (UIPI של Windows), "
                    "וחלון האישור של UAC נמצא בשולחן עבודה מאובטח ש-JARVIS לא מגיע אליו",
              "windows of programs running as administrator take no input from JARVIS (Windows' UIPI), and the "
              "UAC prompt is on a secure desktop JARVIS cannot reach"),
            t(lang, "מסכי כניסה, שדות סיסמה, מנהלי סיסמאות ואבטחת Windows: JARVIS לא מקליד בהם ולא לוחץ בהם",
              "sign-in screens, password fields, password managers and Windows Security: JARVIS never types or "
              "clicks there"),
            t(lang, "שליחה, מחיקה, פרסום ותשלום מוצגים מראש ומאושרים שוב רגע לפני הפעולה",
              "sending, deleting, publishing and paying are previewed, and asked again right before they happen"),
        ]
        newest = next(reversed(self._tasks.values()), None) if self._tasks else None
        return {
            "ways": ways, "stop": stop, "limits": limits,
            "note": t(lang, "שום דבר לא קורה ברקע: כל מבט הוא צילום מסך אחד, בלחיצה שלך",
                      "nothing happens in the background: every look is one screenshot, on your click"),
            "computer_enabled": computer.settings.enabled, "busy": computer.busy(),
            "task": newest.view() if newest is not None and not self._stale(newest) else None,
        }

    @staticmethod
    def _way(key: str, label: str, detail: str, problem: str | None, needs: list[str]) -> dict[str, Any]:
        return {"key": key, "label": label, "detail": detail, "available": problem is None, "reason": problem,
                "needs": needs}

    # ================================================================== the lists (read only)
    async def listing(self, query: str = "", refresh: bool = False, lang: str | None = None) -> dict[str, Any]:
        """The installed apps (matching `query`) and the open windows, each with what JARVIS can do with it."""
        lang = self._lang(lang)
        status = self.status(lang)
        ways = {w["key"]: w for w in status["ways"]}
        execute = self.app.executor.execute
        installed, running = await asyncio.gather(
            execute("list_installed_apps", {"query": query, "refresh": refresh}, agent_id=AGENT, lang=lang),
            execute("running_apps", {}, agent_id=AGENT, lang=lang))
        errors = [r.error for r in (installed, running) if not r.ok and r.error]
        launch = ways["launch"]
        apps = [{**row, "can": ["open"] if launch["available"] else [],
                 "why_not": None if launch["available"] else launch["reason"]}
                for row in ((installed.data or {}).get("apps") or [])]
        windows = [self._window_row(row, ways, lang) for row in ((running.data or {}).get("apps") or [])
                   if not self._own(row)]
        return {"installed": apps, "total": (installed.data or {}).get("total", len(apps)),
                "matched": (installed.data or {}).get("matched", len(apps)), "windows": windows, "errors": errors}

    @staticmethod
    def _own(row: dict[str, Any]) -> bool:
        return row.get("pid") == os.getpid() or policy.is_jarvis_title(str(row.get("title") or ""))

    @staticmethod
    def _window_row(row: dict[str, Any], ways: dict[str, dict[str, Any]], lang: str) -> dict[str, Any]:
        program = str(row.get("name") or "")
        title = plain(str(row.get("title") or ""))
        found = policy.sensitive(Target(process=program.lower(), title=title))
        can: list[str] = []
        note = None
        if found is not None:
            note = t(lang, f"זה {found[1]}: JARVIS לא מסתכל לתוכו ולא נוגע בו",
                     f"this is {found[0]}: JARVIS does not look into it or touch it")
        else:
            if ways["uia"]["available"]:
                can.append("look")
            if ways["vision"]["available"]:
                can.append("suggest")
            if not can:
                note = ways["uia"]["reason"]
        return {"program": program, "title": title, "pid": row.get("pid"), "windows": row.get("windows", 1),
                "window": title or _program(program), "can": can, "note": note}

    # ================================================================== apps tasks
    def _stale(self, task: AppsTask) -> bool:
        return time.monotonic() - task.used > TASK_IDLE_S

    def _task(self, task_id: str | None) -> AppsTask:
        """The apps task with that id, or a new one (an unknown or expired id starts afresh)."""
        for old in [k for k, v in self._tasks.items() if self._stale(v)]:
            self.end_task(old)
        task = self._tasks.get(task_id) if task_id else None
        if task is None:
            task = AppsTask()
            self._tasks[task.id] = task
            while len(self._tasks) > MAX_TASKS:
                self.end_task(next(iter(self._tasks)))
        task.used = time.monotonic()
        self._tasks.move_to_end(task.id)
        return task

    def task(self, task_id: str) -> AppsTask | None:
        return self._tasks.get(task_id)

    def end_task(self, task_id: str) -> bool:
        """End an apps task: its corrections and suggestions go, and so does any approval remembered for it."""
        found = self._tasks.pop(task_id, None)
        self.app.broker.forget_task(task_id)
        return found is not None

    def correct(self, text: str, task_id: str | None = None, lang: str | None = None) -> dict[str, Any]:
        """Keep what the user says JARVIS got wrong; the next look and suggestion of that task are told."""
        lang = self._lang(lang)
        text = " ".join((text or "").split())[:300]
        if not text:
            raise AppsRequestError(t(lang, "התיקון ריק", "The correction is empty"), status=400)
        task = self._task(task_id)
        task.corrections = [*task.corrections, text][-MAX_CORRECTIONS:]
        return {"task": task.view()}

    # ================================================================== jobs
    async def open_app(self, name: str, lang: str | None = None) -> Job:
        lang = self._lang(lang)
        name = " ".join((name or "").split())[:200]
        if not name:
            raise AppsRequestError(t(lang, "לא נבחרה תוכנה", "No app was chosen"), status=400)

        async def run(ctx: JobContext) -> dict[str, Any]:
            async with ctx.step("open", ctx.t(f"פותח את {name}", f"Opening {name}")) as step:
                result = await self.app.executor.execute("open_app", {"name": name}, agent_id=AGENT,
                                                         subtask_id=ctx.job.id, cancel=ctx.cancel, lang=ctx.lang)
                if not result.ok:
                    self._mark(step, result, ctx)
            if not result.ok:
                self._fail(ctx, result)
            data = result.data or {}
            return {"summary": result.summary, "app": data.get("app"), "pid": data.get("pid"),
                    "method": data.get("method")}

        return await self.app.jobs.start(
            kind="apps.open", area="apps", title=t(lang, f"פתיחת {name}", f"Open {name}"), run=run,
            plan=[t(lang, f"למצוא את {name} בין התוכנות המותקנות", f"Find {name} among the installed apps"),
                  t(lang, "לפתוח אותה דרך כללי ההרשאות של JARVIS", "Open it through JARVIS's permission rules")],
            steps=[("open", t(lang, f"פותח את {name}", f"Opening {name}"))], params={"name": name}, lang=lang)

    async def look(self, task_id: str | None = None, window: str = "", lang: str | None = None) -> Job:
        lang = self._lang(lang)
        window = " ".join((window or "").split())[:200]
        task = self._task(task_id)
        model = model_problem(self.app.computer, lang) is None
        params = {"purpose": "look", "corrections": task.corrections[-MAX_CORRECTIONS:], "window": window}

        async def run(ctx: JobContext) -> dict[str, Any]:
            result = await self._gated(ctx, task, "read_screen", params,
                                       ("look", ctx.t("המבט במסך", "The look at the screen")))
            return {"summary": result.summary, "task_id": task.id, "look": dict(result.data or {})}

        target = _program(window) if window else t(lang, "החלון שעבדת בו", "the window you were working in")
        plan = [t(lang, "לבקש את אישורך לגישה למסך", "Ask for your approval to see the screen"),
                t(lang, f"להסתיר את JARVIS ולהביא לחזית את {target}", f"Hide JARVIS and bring {target} to the front")]
        if model:
            plan += [t(lang, "לצלם את המסך פעם אחת, בזיכרון בלבד (לא נשמר לדיסק ולא בהיסטוריה)",
                       "Take one screenshot, in memory only (not saved to disk or to the history)"),
                     t(lang, "לקרוא מ-Windows את החלון שבחזית ואת הרכיב שבפוקוס",
                       "Read the window in front and the focused element from Windows"),
                     t(lang, "לתאר את המסך במודל המקומי, ולמחוק את התמונה", "Describe the screen with the local "
                                                                           "model, then drop the picture")]
        else:
            plan += [t(lang, "לקרוא מ-Windows את החלון שבחזית ואת הרכיב שבפוקוס, בלי צילום (אין מודל מקומי)",
                       "Read the window in front and the focused element from Windows, with no screenshot (no "
                       "local model)")]
        plan.append(t(lang, "להחזיר את JARVIS כמו שהיה", "Bring JARVIS back the way it was"))
        return await self.app.jobs.start(
            kind="apps.look", area="apps", title=t(lang, "מה אני רואה במסך", "What JARVIS sees on the screen"),
            run=run, plan=plan, params={"task_id": task.id, "window": window}, lang=lang, keep_result=False,
            sends=self._sends(lang) if model else [],
            steps=[("approve", t(lang, "האישור שלך", "Your approval")),
                   ("look", t(lang, "המבט במסך", "The look at the screen"))])

    async def suggest(self, goal: str, task_id: str | None = None, window: str = "",
                      lang: str | None = None) -> Job:
        lang = self._lang(lang)
        goal = " ".join((goal or "").split())[:1000]
        window = " ".join((window or "").split())[:200]
        if len(goal) < 2:
            raise AppsRequestError(t(lang, "צריך לכתוב מה לעשות", "Write what should be done"), status=400)
        task = self._task(task_id)
        task.goal = goal
        params = {"purpose": "suggest", "goal": goal, "corrections": task.corrections[-MAX_CORRECTIONS:],
                  "done": task.done[-MAX_DONE:], "window": window}

        async def run(ctx: JobContext) -> dict[str, Any]:
            result = await self._gated(ctx, task, "read_screen", params,
                                       ("look", ctx.t("מבט והצעת צעד", "Look and suggest a step")))
            data = dict(result.data or {})
            proposal = dict(data.pop("proposal", None) or {})
            proposal["can_do"] = proposal.get("kind") == "action" and not proposal.get("refused")
            if proposal["can_do"]:
                task.suggestions[ctx.job.id] = proposal      # only this job's own step can be done from it
            return {"summary": result.summary, "task_id": task.id, "look": data, "proposal": proposal}

        target = _program(window) if window else t(lang, "החלון שעבדת בו", "the window you were working in")
        return await self.app.jobs.start(
            kind="apps.suggest", area="apps", title=t(lang, f"הצעד הבא: {goal}", f"Next step: {goal}"), run=run,
            plan=[t(lang, "לבקש את אישורך לגישה למסך", "Ask for your approval to see the screen"),
                  t(lang, f"להסתיר את JARVIS ולהביא לחזית את {target}", f"Hide JARVIS and bring {target} to the front"),
                  t(lang, "צילום מסך אחד בזיכרון בלבד; המודל המקומי מציע צעד אחד בלבד לקראת המשימה",
                    "One screenshot, in memory only; the local model proposes one single step toward the task"),
                  t(lang, "לבדוק מה Windows מדווח שנמצא במקום הזה ולשפוט את הצעד לפי כללי הבטיחות",
                    "Check what Windows reports is at that spot, and judge the step by JARVIS's safety rules"),
                  t(lang, "לא לבצע כלום: הצעד מחכה ללחיצה ולאישור שלך", "Do nothing: the step waits for your click "
                                                                       "and approval")],
            params={"task_id": task.id, "goal": goal, "window": window}, lang=lang, keep_result=False,
            sends=self._sends(lang),
            steps=[("approve", t(lang, "האישור שלך", "Your approval")),
                   ("look", t(lang, "מבט והצעת צעד", "Look and suggest a step"))])

    async def step(self, suggestion: str, lang: str | None = None) -> Job:
        """Do the step a finished suggestion proposed (once): asked first, then checked again on the screen."""
        lang = self._lang(lang)
        for job in self.app.jobs.running():                  # the same click twice: the step already running
            if job.kind == "apps.step" and job.params.get("suggestion") == suggestion:
                return job
        found = self.app.jobs.get(suggestion)
        if found is None or found.kind != "apps.suggest":
            raise AppsRequestError(t(lang, "ההצעה הזו לא נמצאה", "That suggestion was not found"), status=404)
        if found.status != "done":
            raise AppsRequestError(t(lang, "ההצעה עוד לא מוכנה", "That suggestion is not ready"))
        task = self._tasks.get(str(found.params.get("task_id") or ""))
        proposal = task.suggestions.pop(suggestion, None) if task is not None and not self._stale(task) else None
        if task is None or proposal is None:
            raise AppsRequestError(t(lang, "ההצעה הזו כבר נוצלה או שהמשימה הסתיימה; אפשר לבקש הצעה חדשה",
                                     "That suggestion was already used, or its task ended; ask for a new one"))
        task.used = time.monotonic()
        params = self._step_params(proposal)
        words = str(proposal.get("words") or "")
        program = _program(str(proposal.get("program") or ""))
        brief = t(lang, f"להקליד ב-{program}", f"Type in {program}") if params["action"] == "type" and program \
            else words

        async def run(ctx: JobContext) -> dict[str, Any]:
            result = await self._gated(ctx, task, "do_screen_step", params,
                                       ("act", ctx.t("הצעד עצמו", "The step itself")))
            task.done = [*task.done, words][-MAX_DONE:]
            return {"summary": result.summary, "task_id": task.id, "words": words}

        plan = [t(lang, "לבקש את אישורך (תמיד): הפעולה, החלון, הרכיב ולמה זה עלול להיות מסוכן",
                  "Ask for your approval (always): the action, the window, the element and why it may be risky")]
        if params["action"] not in ("open_app", "goto"):
            plan += [t(lang, f"להסתיר את JARVIS ולהביא לחזית את {program}", f"Hide JARVIS and bring {program} to the "
                                                                          "front"),
                     t(lang, "לבדוק שזה אותו חלון ואותו רכיב; אם משהו השתנה, לעצור בלי לעשות כלום",
                       "Check it is the same window and element; if anything changed, stop without doing anything"),
                     t(lang, "פעולה אחת דרך כללי הבטיחות של JARVIS (הם עשויים לבקש אישור נוסף או לסרב)",
                       "One action through JARVIS's safety rules (they may ask once more, or refuse)"),
                     t(lang, "להחזיר את JARVIS כמו שהיה", "Bring JARVIS back the way it was")]
        sends = [t(lang, f"מה שמוכן לשליחה ב-{program}, דרך התוכנה עצמה", f"what is ready to send in {program}, by "
                                                                       "that app itself")] \
            if proposal.get("rule") in ("send", "pay") else []
        return await self.app.jobs.start(
            kind="apps.step", area="apps", title=t(lang, f"ביצוע הצעד: {brief}", f"Do the step: {brief}"), run=run,
            plan=plan, params={"suggestion": suggestion, "task_id": task.id}, lang=lang, keep_result=False,
            sends=sends,
            steps=[("approve", t(lang, "האישור שלך", "Your approval")), ("act", t(lang, "הצעד עצמו", "The step itself"))])

    @staticmethod
    def _step_params(proposal: dict[str, Any]) -> dict[str, Any]:
        """What `do_screen_step` gets: the action, and what JARVIS found there when it proposed it."""
        action = dict(proposal.get("action") or {})
        params: dict[str, Any] = {k: v for k, v in action.items() if v is not None}
        target = proposal.get("target") or {}
        if target:
            params.update(window_id=int(target.get("hwnd") or 0), window_pid=int(target.get("pid") or 0),
                          program=str(target.get("process") or ""), title=str(target.get("title") or ""),
                          class_name=str(target.get("class_name") or ""),
                          expect_element=str(target.get("element") or ""),
                          expect_control=str(target.get("control") or ""),
                          expect_password=bool(target.get("password")))
        return params

    def _sends(self, lang: str) -> list[str]:
        remote = (self.app.computer.settings.server_url or "").strip()
        return [t(lang, f"צילום מסך אחד לשרת המודל {remote}", f"One screenshot to the model server {remote}")] \
            if remote else []

    # ================================================================== running a tool for a job
    async def _gated(self, ctx: JobContext, task: AppsTask, tool: str, params: dict[str, Any],
                     work: tuple[str, str]) -> ToolResult:
        """Run one approved tool for a job: the "approve" step lasts until the user decided (the tool reports
        its first progress once it runs), then the work step until it is done. A "no" ends the job as
        stopped, any other failure as failed, with the reason in words."""
        started = asyncio.Event()

        async def progress(fraction: float, message: str) -> None:
            started.set()
            await ctx.progress(fraction, message)

        call = asyncio.ensure_future(self.app.executor.execute(
            tool, params, agent_id=AGENT, task_id=task.id, subtask_id=ctx.job.id, cancel=ctx.cancel,
            progress=progress, lang=ctx.lang))
        try:
            outcome: ToolResult | None = None
            async with ctx.step("approve") as step:
                await ctx.progress(None, ctx.t("ממתין לאישור שלך בחלון האישור", "Waiting for your approval"))
                gate = asyncio.ensure_future(started.wait())
                try:
                    await asyncio.wait({call, gate}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    gate.cancel()
                if call.done():
                    outcome = call.result()
                    if not outcome.ok:
                        self._mark(step, outcome, ctx)
            if outcome is None:
                async with ctx.step(*work) as step:
                    outcome = await call
                    if not outcome.ok:
                        self._mark(step, outcome, ctx)
            if not outcome.ok:
                self._fail(ctx, outcome)
            task.used = time.monotonic()
            return outcome
        finally:
            if not call.done():
                call.cancel()

    @staticmethod
    def _mark(step: JobStep, result: ToolResult, ctx: JobContext) -> None:
        if result.denied == "confirmation":
            step.status = "skipped"
            step.detail = ctx.t("לא אישרת, אז לא נעשה דבר", "You did not approve, so nothing was done")
        else:
            step.status = "failed"

    @staticmethod
    def _fail(ctx: JobContext, result: ToolResult) -> NoReturn:
        if result.denied == "confirmation":
            raise TaskCancelled("declined")
        raise ToolError(result.error or result.summary or ctx.t("הפעולה נכשלה", "The action failed"))

    async def aclose(self) -> None:
        for task_id in list(self._tasks):
            self.end_task(task_id)


__all__ = ["AppsRequestError", "AppsService", "AppsTask"]
