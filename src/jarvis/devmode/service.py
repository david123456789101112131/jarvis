"""The development-mode service: the code screen's work on a project the user picked, as jobs.

understand the request → inspect the project → a short plan → focused changes to review → apply (with undo)
→ run the project's tests → a summary of what changed and what passed.

A *session* is one project the user opened with the folder picker. Its id is the task id of every tool call
made for it, so the approvals the user asks to remember last for that project only, and the permissions the
user gives on the screen are scoped to it:

* opening a project gives the Development agent read access to that one folder for the session;
* the "allow changes" switch adds write access to that folder only, for the session (and at most 12 hours),
  shown in Security and taken back when switched off, when the project is closed and when JARVIS stops.

Nothing is written, sent or run without a click: the inspection reads names and small manifests, the AI is
asked only on "Plan with Grok" (approved in the standard dialog, which shows the files, where they go and
the price), a change is written only on "Apply" (approved like any write), and the tests run on "Run tests"
(approved, since they run the project's own code).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from jarvis.common.clock import utcnow
from jarvis.common.enums import Capability
from jarvis.common.errors import JarvisError
from jarvis.common.i18n import t
from jarvis.common.ids import new_id
from jarvis.devmode.edits import MASK, Change, Note, Proposal, build_file, refresh
from jarvis.devmode.prompt import PRICES, PRICES_CHECKED, Engine, money
from jarvis.security.path_guard import normalize_path

if TYPE_CHECKING:
    from jarvis.app import JarvisApp
    from jarvis.jobs import Job, JobContext
    from jarvis.tools.base import ToolResult

log = logging.getLogger(__name__)

AGENT = "development"
GRANT_TTL_S = 12 * 3600
KEEP_CHANGES = 50
OUTPUT_TAIL = 12_000


class DevSession(BaseModel):
    id: str = Field(default_factory=lambda: new_id("dev"))
    root: str
    name: str
    opened_at: datetime = Field(default_factory=utcnow)
    writable: bool = False
    grants: list[str] = Field(default_factory=list)       # read grant first, then the write grants
    report: dict[str, Any] | None = None
    last_change: str | None = None
    last_tests: str | None = None

    def view(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"grants"})


class DevService:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app
        self.dir = app.paths.data / "dev" / "changes"
        self._sessions: dict[str, DevSession] = {}
        self._changes: OrderedDict[str, Change] = OrderedDict()
        app.services.extras["dev"] = self

    # ------------------------------------------------------------------ status
    def engines(self) -> list[dict[str, Any]]:
        """The AI engines that could plan a change here: the cloud ones only on the user's click."""
        from jarvis.ai.router import CLOUD, CLOUD_NAMES

        router = self.app.router
        preferred = router.agent_preference(AGENT)
        out = []
        for name in ("xai", "anthropic", "openai", "local"):
            if not router.has(name):
                continue
            provider = router.providers[name]
            model = getattr(provider, "model", "") or ""
            out.append({"provider": name, "name": CLOUD_NAMES.get(name, "Local model"), "model": model,
                        "local": name not in CLOUD, "price": PRICES.get(model), "preferred": name == preferred})
        out.sort(key=lambda e: (not e["preferred"], e["local"]))
        return out

    def status(self) -> dict[str, Any]:
        engines = self.engines()
        needs = [] if engines else self._needs()
        return {
            "available": True,
            "engines": engines,
            "needs": needs,
            "prices_checked": PRICES_CHECKED,
            "mode": self.app._mode().value if hasattr(self.app, "_mode") else "normal",
            "sessions": [s.view() for s in self._sessions.values()],
            "tools": {"python": bool(shutil.which("python") or shutil.which("py") or shutil.which("python3")),
                      "node": bool(shutil.which("node")), "git": bool(shutil.which("git"))},
            "limits": {"files": 24, "chars": 160_000},
        }

    def _needs(self) -> list[dict[str, str]]:
        free = {"he": "את המודל החינמי שרץ במחשב (jarvis chat install)",
                "en": "the free model that runs on this computer (jarvis chat install)"}
        if not self.app.settings.ai.allow_paid:
            return [free]
        return [{"he": free["he"] + ", או מפתח API של xAI (Grok) בהגדרות > מפתחות API",
                 "en": free["en"] + ", or an xAI (Grok) API key in Settings > API keys"}]

    # ------------------------------------------------------------------ sessions
    def session(self, session_id: str) -> DevSession:
        s = self._sessions.get(session_id)
        if s is None:
            raise JarvisError(t(self.app.lang, "הפרויקט לא פתוח (אולי JARVIS הופעל מחדש); צריך לפתוח אותו שוב",
                                "The project is not open (JARVIS may have restarted); open it again"),
                              details={"code": "no_session"})
        return s

    async def open(self, path: str, *, lang: str) -> tuple[DevSession, Job]:
        """Open the project the user picked: read access to that folder for the session, then an inspection."""
        root = normalize_path(path)
        guard = self.app.guard
        problem = guard.path_problem(root)
        if problem is not None:
            raise JarvisError(t(lang, f"אי אפשר לפתוח את הנתיב הזה: {problem[1]}", f"That path cannot be opened: {problem[0]}"))
        if not await asyncio.to_thread(root.is_dir):
            raise JarvisError(t(lang, f"התיקייה לא נמצאה: {root}", f"Folder not found: {root}"))
        if guard.is_denied(root) or root == Path(root.anchor):
            raise JarvisError(t(lang, "זו תיקייה מוגנת (של המערכת או של JARVIS) ואי אפשר לעבוד עליה כפרויקט",
                                "That is a protected folder (Windows' or JARVIS's own) and cannot be opened as a project"))
        for existing in self._sessions.values():
            if normalize_path(existing.root) == root:
                job = await self.inspect(existing.id, refresh=True, lang=lang)
                return existing, job
        session = DevSession(root=str(root), name=root.name or str(root))
        grant = self.app.permissions.grant(
            AGENT, Capability.FS_READ, roots=[root], task_id=session.id, ttl_s=GRANT_TTL_S, source="user",
            reason=t(lang, f"פתחת את הפרויקט {session.name} במסך הקוד", f"you opened the project {session.name} on the code screen"))
        session.grants.append(grant.id)
        self._sessions[session.id] = session
        job = await self.inspect(session.id, lang=lang)
        return session, job

    def set_writable(self, session_id: str, allow: bool, *, lang: str) -> DevSession:
        """The screen's "allow changes" switch: write access to this project's folder for this session only."""
        s = self.session(session_id)
        perms = self.app.permissions
        if allow and not s.writable:
            reason = t(lang, f"הפעלת שינויים בפרויקט {s.name} במסך הקוד", f"you allowed changes in {s.name} on the code screen")
            for cap in (Capability.FS_WRITE, Capability.FS_DELETE):
                g = perms.grant(AGENT, cap, roots=[s.root], task_id=s.id, ttl_s=GRANT_TTL_S, source="user", reason=reason)
                s.grants.append(g.id)
            s.writable = True
        elif not allow and s.writable:
            for gid in s.grants[1:]:
                perms.revoke(gid)
            s.grants = s.grants[:1]
            s.writable = False
        return s

    def close(self, session_id: str) -> bool:
        s = self._sessions.pop(session_id, None)
        if s is None:
            return False
        self.app.permissions.revoke_task_grants(s.id)
        self.app.broker.forget_task(s.id)
        return True

    # ------------------------------------------------------------------ running tools
    async def _exec(self, tool: str, params: dict[str, Any], session: DevSession, ctx: JobContext) -> ToolResult:
        async def progress(fraction: float, message: str = "") -> None:
            await ctx.progress(fraction, message)

        return await self.app.executor.execute(tool, params, agent_id=AGENT, task_id=session.id, cancel=ctx.cancel,
                                               progress=progress, lang=ctx.lang)

    @staticmethod
    def _failed(result: ToolResult) -> JarvisError:
        return JarvisError(result.error or result.summary, details={"denied": result.denied})

    # ------------------------------------------------------------------ inspect / find files
    async def inspect(self, session_id: str, *, request: str = "", error_text: str = "", refresh: bool = False,
                      lang: str) -> Job:
        s = self.session(session_id)
        finding = bool(request.strip() or error_text.strip())

        async def run(ctx: JobContext) -> dict[str, Any]:
            params = {"path": s.root, "request": request, "error_text": error_text, "refresh": refresh}
            async with ctx.step("scan", ctx.t("קורא את מבנה הפרויקט והתלויות", "Reading the project's layout and dependencies")):
                look = asyncio.ensure_future(self._exec("dev_inspect", params, s, ctx))
                git = asyncio.ensure_future(self._git_status(s, ctx)) if not finding else None
                result = await look
            if not result.ok:
                if git is not None:
                    git.cancel()
                raise self._failed(result)
            data = result.data or {}
            s.report = data.get("report")
            git_state: dict[str, Any] | None = None
            if git is not None:
                async with ctx.step("git", ctx.t("בודק את מצב Git", "Checking Git")):
                    git_state = await git
            else:
                await ctx.skip("git")
            report = data.get("report") or {}
            summary = _report_summary(report, ctx.lang)
            if finding:
                ranked = data.get("ranked") or []
                summary = ctx.t(f"נמצאו {len(ranked)} קבצים רלוונטיים", f"Found {len(ranked)} relevant files") if ranked \
                    else ctx.t("לא נמצאו קבצים שמתאימים לבקשה; אפשר לבחור ידנית", "No files matched the request; you can pick them yourself")
            return {"summary": summary, "session": s.id, "report": report, "ranked": data.get("ranked") or [],
                    "listing": data.get("listing") or [], "listing_total": data.get("listing_total", 0), "git": git_state}

        title = t(lang, f"איתור הקבצים הרלוונטיים ב-{s.name}", f"Find the relevant files in {s.name}") if finding \
            else t(lang, f"בדיקת הפרויקט {s.name}", f"Look at the project {s.name}")
        plan = [t(lang, "לקרוא את שמות הקבצים ואת קובצי ההגדרות (בלי להריץ כלום)", "Read the file names and the manifests (running nothing)")]
        plan.append(t(lang, "לדרג את הקבצים לפי הבקשה, בלי AI", "Rank the files for the request, without AI") if finding
                    else t(lang, "לזהות שפות, מסגרות, פקודות בדיקה והרצה, וכלים חסרים", "Find the languages, frameworks, test and run commands, and missing tools"))
        return await self.app.jobs.start(
            kind="dev.files" if finding else "dev.inspect", area="code", title=title, run=run, plan=plan,
            steps=[("scan", t(lang, "קריאת הפרויקט", "Read the project")), ("git", t(lang, "מצב Git", "Git state"))],
            params={"session": s.id, "request": request[:500], "error": error_text[:200], "refresh": refresh},
            real=True, lang=lang, keep_result=False)

    async def _git_status(self, s: DevSession, ctx: JobContext) -> dict[str, Any] | None:
        if not shutil.which("git") or not (Path(s.root) / ".git").exists():
            return None
        result = await self._exec("git", {"path": s.root, "args": ["status", "--porcelain=v1", "--branch"]}, s, ctx)
        if not result.ok:
            return {"ok": False, "error": (result.error or "")[:300]}
        out = (result.data or {}).get("stdout", "") if isinstance(result.data, dict) else ""
        lines = [line for line in out.splitlines() if line.strip()]
        branch = lines[0][3:].split("...")[0] if lines and lines[0].startswith("## ") else ""
        changed = [line[3:] for line in lines if not line.startswith("## ")]
        return {"ok": True, "branch": branch, "changed": len(changed), "files": changed[:20]}

    # ------------------------------------------------------------------ propose
    def engine_for(self, provider: str | None, lang: str) -> dict[str, Any]:
        engines = self.engines()
        if not engines:
            need = self._needs()[0]
            raise JarvisError(t(lang, f"אין מודל AI לתכנון שינויים: צריך {need['he']}.",
                                f"No AI model can plan changes: it needs {need['en']}."),
                              details={"code": "no_engine"})
        if provider:
            match = next((e for e in engines if e["provider"] == provider), None)
            if match is None:
                raise JarvisError(t(lang, f"המנוע {provider} לא זמין", f"The engine {provider} is not available"))
            return match
        return engines[0]

    async def propose(self, session_id: str, request: str, *, files: list[str] | None = None, mode: str = "auto",
                      error_text: str = "", parent_id: str = "", engine: str | None = None, lang: str) -> Job:
        from jarvis.ai.context import allow_provider

        s = self.session(session_id)
        chosen_engine = self.engine_for(engine, lang)
        name, local = chosen_engine["name"], chosen_engine["local"]
        picked = [f for f in (files or []) if f][:24]
        host = {"xai": "api.x.ai", "anthropic": "api.anthropic.com", "openai": "api.openai.com"}.get(chosen_engine["provider"], "")
        sends = [] if local else [t(lang, f"הבקשה, תיאור הפרויקט ו{'-' + str(len(picked)) + ' קבצים' if picked else 'הקבצים הרלוונטיים'} → {name} ({host})",
                                    f"the request, the project facts and {str(len(picked)) + ' files' if picked else 'the relevant files'} → {name} ({host})")]

        async def run(ctx: JobContext) -> dict[str, Any]:
            async with ctx.step("understand", ctx.t("הבנת הבקשה", "Understand the request")):
                if s.report is None:
                    look = await self._exec("dev_inspect", {"path": s.root}, s, ctx)
                    if not look.ok:
                        raise self._failed(look)
                    s.report = (look.data or {}).get("report")
            chosen = picked
            if not chosen:
                async with ctx.step("files", ctx.t("איתור הקבצים הרלוונטיים (בלי AI)", "Find the relevant files (no AI)")) as step:
                    found = await self._exec("dev_inspect", {"path": s.root, "request": request, "error_text": error_text}, s, ctx)
                    if not found.ok:
                        raise self._failed(found)
                    chosen = [r["path"] for r in (found.data or {}).get("ranked", [])][:12]
                    step.detail = ", ".join(chosen[:4]) + ("…" if len(chosen) > 4 else "")
            else:
                await ctx.skip("files", ctx.t("בחרת את הקבצים", "you chose the files"))
            async with ctx.step("plan", ctx.t(f"{name} מתכנן את השינוי", f"{name} plans the change")):
                params = {"path": s.root, "session_id": s.id, "request": request, "files": chosen, "mode": mode,
                          "error_text": error_text, "parent_id": parent_id}
                with allow_provider(None if local else chosen_engine["provider"]):
                    result = await self._exec("dev_propose", params, s, ctx)
            if not result.ok:
                raise self._failed(result)
            change = self.change(str((result.data or {}).get("change_id")))
            if change is None:
                raise JarvisError(ctx.t("ההצעה לא נשמרה", "the proposal was not kept"))
            s.last_change = change.id
            async with ctx.step("check", ctx.t("בדיקת השינוי המוצע: תחביר, ייבוא והיקף", "Check the proposal: syntax, imports and size")):
                blocked = [f.path for f in change.files if f.blocked]
                warned = [f.path for f in change.files if f.warnings]
            if change.cost_usd is not None:
                await ctx.update(cost=t(ctx.lang, f"{money(change.cost_usd)} לפי המחירון", f"{money(change.cost_usd)} by the list price"))
            summary = change.summary or ctx.t("השינוי מוכן לסקירה", "The change is ready to review")
            return {"summary": summary, "change_id": change.id, "session": s.id, "understood": change.understood,
                    "files": [{"path": f.path, "added": f.added, "removed": f.removed, "blocked": f.blocked} for f in change.files],
                    "blocked": blocked, "warned": warned, "questions": change.questions,
                    "assumptions": change.assumptions, "usage": change.usage, "cost_usd": change.cost_usd}

        engine_label = f"{name} ({chosen_engine['model']})" if chosen_engine.get("model") else name
        plan = [t(lang, "לאתר את הקבצים הרלוונטיים בפרויקט (בלי AI)", "Find the relevant files in the project (no AI)") if not picked
                else t(lang, f"לקחת את {len(picked)} הקבצים שבחרת", f"Take the {len(picked)} files you chose"),
                t(lang, f"לשלוח ל-{engine_label} ולקבל תוכנית ושינויים ממוקדים", f"Send to {engine_label} and get a plan with focused edits")
                if not local else t(lang, "לבקש מהמודל המקומי תוכנית ושינויים ממוקדים", "Ask the local model for a plan with focused edits"),
                t(lang, "לבדוק את ההצעה (תחביר, ייבוא, היקף) ולהציג אותה לסקירה; שום קובץ לא משתנה לפני האישור",
                  "Check the proposal (syntax, imports, size) and show it for review; no file changes until you apply it")]
        return await self.app.jobs.start(
            kind="dev.fix" if error_text.strip() else "dev.propose", area="code",
            title=t(lang, f"תכנון שינוי: {request.strip()[:90]}", f"Plan a change: {request.strip()[:90]}"), run=run,
            plan=plan, params={"session": s.id, "request": request[:2000], "files": picked, "mode": mode,
                                "error": error_text[:300], "parent": parent_id, "engine": chosen_engine["provider"]},
            steps=[("understand", t(lang, "הבנת הבקשה", "Understand")), ("files", t(lang, "קבצים רלוונטיים", "Relevant files")),
                   ("plan", t(lang, "תוכנית ושינויים", "Plan and edits")), ("check", t(lang, "בדיקת ההצעה", "Check"))],
            real=True, sends=sends, cost=None if local else t(lang, "יוצג באישור (לפי גודל הקבצים)", "shown in the approval (by the files' size)"),
            lang=lang)

    def record_proposal(self, *, session_id: str, root: Path, request: str, proposal: Proposal, facts: dict[str, Any],
                        engine: Engine, usage: dict[str, int], cost: float | None, sent: list[str], masked: int,
                        parent_id: str | None, error_text: str, lang: str) -> Change:
        """Turn the AI's answer into a change to review (called by `dev_propose`; nothing is written here)."""
        change = Change(session_id=session_id, root=str(root), request=request, lang=lang, understood=proposal.understood,
                        plan=proposal.plan, assumptions=proposal.assumptions, questions=proposal.questions,
                        summary=proposal.summary, risks=proposal.risks, tests=proposal.tests, provider=engine.provider,
                        model=engine.model, effort=engine.effort, usage=usage, cost_usd=cost, sent=sent, masked=masked,
                        parent_id=parent_id, error_text=error_text[-4000:])
        seen: set[str] = set()
        for edit in proposal.edits[:40]:
            fc = build_file(root, edit, lang, facts)
            if fc.path in seen:
                fc.problems.append(Note(code="duplicate", text=t(lang, "הקובץ הוצע פעמיים; רק ההצעה הראשונה נלקחה",
                                                                 "the file was proposed twice; only the first is used")))
                fc.include = False
            seen.add(fc.path)
            if fc.action == "modify" and fc.old is not None and fc.path not in sent:
                fc.problems.append(Note(code="unseen", text=t(
                    lang, "המודל לא קיבל את הקובץ הזה, ולכן אי אפשר לסמוך על השינוי בו",
                    "the model was not given this file, so its edit to it cannot be trusted")))
            change.files.append(fc)
        self._keep(change)
        return change

    # ------------------------------------------------------------------ the changes
    def change(self, change_id: str) -> Change | None:
        if change_id in self._changes:
            return self._changes[change_id]
        if not re.fullmatch(r"chg_[A-Za-z0-9]+", change_id or ""):
            return None
        path = self.dir / f"{change_id}.json"
        try:
            change = Change.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        self._changes[change.id] = change
        return change

    def changes(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """The project's recent changes, newest first (also those kept from before JARVIS restarted)."""
        s = self.session(session_id)
        root = normalize_path(s.root)
        if self.dir.is_dir():
            for path in sorted(self.dir.glob("chg_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:KEEP_CHANGES]:
                self.change(path.stem)
        found = [c for c in self._changes.values() if normalize_path(c.root) == root]
        found.sort(key=lambda c: c.created_at, reverse=True)
        return [{"id": c.id, "request": c.request[:300], "summary": c.summary[:300], "status": c.status,
                 "created_at": c.created_at.isoformat(), "files": [f.path for f in c.files if f.include],
                 "can_undo": bool(c.to_undo()), "model": c.model, "cost_usd": c.cost_usd} for c in found[:limit]]

    def change_view(self, change_id: str) -> dict[str, Any] | None:
        """The change as the screen shows it: secret-looking values hidden, the old text left out."""
        change = self.change(change_id)
        if change is None:
            return None
        mask = self.app.redactor.mask_code
        files = []
        for f in change.files:
            new, hidden_new = mask(f.new)
            diff, hidden_diff = mask(f.diff)
            files.append({
                "path": f.path, "action": f.action, "why": f.why, "include": f.include, "edited": f.edited,
                "status": f.status, "added": f.added, "removed": f.removed, "diff": diff,
                "content": new, "editable": not (hidden_new or hidden_diff) and f.status != "applied",
                "masked": bool(hidden_new or hidden_diff), "blocked": f.blocked,
                "problems": [n.model_dump() for n in f.problems], "warnings": [n.model_dump() for n in f.warnings],
                "eol": "CRLF" if f.eol == "\r\n" else "LF", "new_file": f.old is None,
            })
        data = change.model_dump(mode="json", exclude={"files", "error_text"})
        data["files"] = files
        data["digest"] = change.digest() if change.to_apply() else None
        data["undo_digest"] = change.digest(undo=True) if change.to_undo() else None
        data["can_apply"] = bool(change.to_apply()) and not any(f.blocked for f in change.to_apply())
        session = self._sessions.get(change.session_id)
        data["writable"] = bool(session and session.writable)
        return data

    def edit_file(self, change_id: str, path: str, *, content: str | None = None, include: bool | None = None,
                  lang: str) -> Change:
        """The user's own edit of a proposed file (its new text), or leaving it in or out of the change."""
        change = self._require(change_id, lang)
        f = change.file(path)
        if f is None:
            raise JarvisError(t(lang, "הקובץ לא נמצא בשינוי", "that file is not part of the change"))
        if f.status == "applied":
            raise JarvisError(t(lang, "הקובץ כבר עודכן; אפשר לבטל את השינוי ולבקש חדש", "that file was already updated; undo the change to edit it"))
        if include is not None:
            f.include = include
        if content is not None:
            if MASK in content:
                raise JarvisError(t(lang, "בטקסט יש ערך סודי מוסתר; עריכה כאן הייתה דורסת אותו. ערוך את הקובץ בעורך שלך.",
                                    "The text has a hidden secret; editing it here would overwrite it. Edit the file in your editor."))
            f.new = content.replace("\r\n", "\n")
            f.edited = True
            f.problems = [p for p in f.problems if p.code not in ("not_found", "ambiguous", "empty", "masked", "unseen")]
            refresh(f, lang, self._facts(change))
        self._save(change)
        return change

    def discard(self, change_id: str, *, lang: str) -> Change:
        change = self._require(change_id, lang)
        if change.to_undo():
            raise JarvisError(t(lang, "חלק מהשינוי כבר הוחל; קודם לבטל אותו", "part of the change is applied; undo it first"))
        change.status = "discarded"
        self._save(change)
        return change

    def mark_written(self, change_id: str, written: list[dict[str, Any]], *, undo: bool) -> None:
        change = self.change(change_id)
        if change is None:
            return
        by_path = {w["path"]: w for w in written}
        for f in change.files:
            w = by_path.get(f.path)
            if w is None:
                continue
            if undo:
                f.status, f.applied_sha = "undone", None
            else:
                f.status, f.applied_sha = "applied", w.get("sha")
        statuses = {f.status for f in change.files if f.include}
        if undo:
            change.status = "undone" if "applied" not in statuses else "partial"
            change.undone_at = utcnow()
        else:
            change.status = "applied" if statuses == {"applied"} else "partial"
            change.applied_at = utcnow()
        self._save(change)

    def mark_conflicts(self, change_id: str, paths: list[str], *, undo: bool) -> None:
        change = self.change(change_id)
        if change is None:
            return
        for f in change.files:
            if f.path in paths and not undo:
                f.status = "conflict"
                if not any(n.code == "conflict" for n in f.problems):
                    f.problems.append(Note(code="conflict", text=t(change.lang, "הקובץ השתנה מאז ההצעה; צריך הצעה חדשה",
                                                                   "the file changed since the proposal; ask for a new one")))
        self._save(change)

    def previous(self, change_id: str) -> dict[str, str] | None:
        """What a follow-up request builds on: the change already applied (a fix after it), or a proposal not
        applied yet (a revision the user asked for), with its diff and its request."""
        change = self.change(change_id)
        if change is None:
            return None
        applied = [f.diff for f in change.files if f.status == "applied"]
        kind = "applied" if applied else "proposed"
        diff = "\n".join(applied or [f.diff for f in change.files if f.include and not f.blocked])
        return {"kind": kind, "diff": diff[:20_000], "request": change.request}

    async def apply(self, change_id: str, *, lang: str, undo: bool = False) -> Job:
        change = self._require(change_id, lang)
        session = self.session(change.session_id)
        files = change.to_undo() if undo else change.to_apply()
        if not files:
            raise JarvisError(t(lang, "אין מה לבטל" if undo else "אין קבצים להחיל", "nothing to undo" if undo else "no files to apply"))
        if not session.writable:
            raise JarvisError(t(lang, "קודם צריך להפעיל 'לאפשר שינויים בפרויקט' במסך הקוד",
                                "First turn on 'Allow changes in this project' on the code screen"), details={"code": "not_writable"})
        digest = change.digest(undo=undo)

        async def run(ctx: JobContext) -> dict[str, Any]:
            async with ctx.step("write", ctx.t("מחזיר את הגרסה הקודמת" if undo else "כותב את השינוי", "Putting the previous text back" if undo else "Writing the change")):
                result = await self._exec("dev_apply", {"change_id": change.id, "digest": digest, "undo": undo}, session, ctx)
            if not result.ok:
                raise self._failed(result)
            done = [f for f in change.files if f.path in set((result.data or {}).get("written", []))]
            lines = [f"{f.path} (+{f.added} −{f.removed}{ctx.t(', חדש', ', new') if f.old is None else ''})" for f in done]
            summary = (ctx.t("בוטל השינוי: ", "Undone: ") if undo else ctx.t("שיניתי: ", "Changed: ")) + "; ".join(lines)
            return {"summary": summary, "change_id": change.id, "session": session.id, "files": lines,
                    "about": change.summary, "undo": undo}

        what = "; ".join(f.path for f in files[:4]) + ("…" if len(files) > 4 else "")
        return await self.app.jobs.start(
            kind="dev.undo" if undo else "dev.apply", area="code",
            title=t(lang, f"ביטול השינוי ב-{what}", f"Undo the change in {what}") if undo else t(lang, f"החלת השינוי ב-{what}", f"Apply the change to {what}"),
            run=run, plan=[t(lang, "לוודא שהקבצים לא השתנו מאז ההצעה", "Check the files did not change since the proposal"),
                           t(lang, "להחזיר את הטקסט הקודם של כל קובץ" if undo else "לכתוב את הקבצים, ולשמור את הגרסה הקודמת לביטול",
                             "Put back each file's previous text" if undo else "Write the files, keeping the previous text for undo")],
            steps=[("write", t(lang, "כתיבה", "Write"))], params={"change": change.id, "undo": undo, "digest": digest},
            real=True, lang=lang)

    # ------------------------------------------------------------------ tests
    async def run_tests(self, session_id: str, *, command: str = "", lang: str) -> Job:
        s = self.session(session_id)

        async def run(ctx: JobContext) -> dict[str, Any]:
            async with ctx.step("detect", ctx.t("איתור פקודת הבדיקות של הפרויקט", "Find the project's test command")) as step:
                if s.report is None:
                    look = await self._exec("dev_inspect", {"path": s.root}, s, ctx)
                    if not look.ok:
                        raise self._failed(look)
                    s.report = (look.data or {}).get("report")
                found = (s.report or {}).get("commands", {}).get("test", "")
                label = command.strip() or found
                if not label:
                    raise JarvisError(ctx.t("לא מצאתי בדיקות בפרויקט הזה (אין בדיקות pytest או סקריפט test ב-package.json). "
                                            "אפשר לבקש ממני להוסיף בדיקות.",
                                            "I found no tests in this project (no pytest tests and no test script in package.json). "
                                            "You can ask me to add some."), details={"code": "no_tests"})
                step.detail = label
            async with ctx.step("run", ctx.t(f"מריץ: {label}", f"Running: {label}")):
                result = await self._exec("run_tests", {"path": s.root, "command": command.strip(), "timeout_s": 900}, s, ctx)
            if result.denied:
                raise self._failed(result)
            data = result.data if isinstance(result.data, dict) else {}
            out = ((data.get("stdout") or "") + "\n" + (data.get("stderr") or "")).strip()
            counts = data.get("counts") or {}
            failures = parse_failures(out)
            hint = _test_hint(out, ctx.lang)
            if not data and not result.ok:
                raise self._failed(result)
            s.last_tests = ctx.job.id
            passed = result.ok and not counts.get("failed") and not counts.get("error") and not counts.get("errors")
            summary = result.summary or (ctx.t("הבדיקות עברו", "The tests passed") if passed else ctx.t("הבדיקות נכשלו", "The tests failed"))
            return {"summary": summary, "session": s.id, "ok": passed, "runner": data.get("runner") or label,
                    "command": " ".join(Path(a).name if i == 0 else a for i, a in enumerate(data.get("args") or [])),
                    "counts": counts, "failures": failures[:40], "exit_code": data.get("exit_code"),
                    "duration_s": data.get("duration_s"), "timed_out": data.get("timed_out", False),
                    "output": out[-OUTPUT_TAIL:], "output_cut": len(out) > OUTPUT_TAIL, "hint": hint}

        return await self.app.jobs.start(
            kind="dev.tests", area="code", title=t(lang, f"הרצת הבדיקות של {s.name}", f"Run the tests of {s.name}"), run=run,
            plan=[t(lang, "למצוא את פקודת הבדיקות של הפרויקט", "Find the project's own test command"),
                  t(lang, "להריץ אותה (הבדיקות הן קוד של הפרויקט, לכן זה באישור)", "Run it (tests are the project's own code, so it asks first)"),
                  t(lang, "להציג מה הורץ, מה עבר ומה נכשל", "Show what ran, what passed and what failed")],
            steps=[("detect", t(lang, "פקודת הבדיקות", "Test command")), ("run", t(lang, "הרצה", "Run"))],
            params={"session": s.id, "command": command}, real=True, lang=lang, keep_result=False)

    async def fix(self, session_id: str, *, tests_job_id: str = "", error_text: str = "", request: str = "",
                  mode: str = "auto", engine: str | None = None, lang: str) -> Job:
        """Ask for a fix of failing tests (from a test run) or of an error the user pasted."""
        s = self.session(session_id)
        text = error_text.strip()
        if tests_job_id:
            job = self.app.jobs.get(tests_job_id)
            result = (job.result or {}) if job else {}
            if not result:
                raise JarvisError(t(lang, "תוצאת הבדיקות לא נמצאה; צריך להריץ אותן שוב", "the test result was not found; run the tests again"))
            names = "\n".join(f"- {f['name']}: {f.get('message', '')}" for f in result.get("failures", [])[:20])
            text = (f"Test command: {result.get('command') or result.get('runner')}\nFailing tests:\n{names}\n\n"
                    f"Output (end):\n{result.get('output', '')[-6000:]}")
        if not text:
            raise JarvisError(t(lang, "אין שגיאה לתקן: להדביק הודעת שגיאה או להריץ בדיקות", "nothing to fix: paste an error or run the tests"))
        ask = request.strip() or t(lang, "לתקן את השגיאה / את הבדיקות שנכשלו, בתיקון ממוקד", "Fix the error / the failing tests with a focused fix")
        parent = s.last_change if s.last_change and (c := self.change(s.last_change)) is not None and c.to_undo() else ""
        return await self.propose(s.id, ask, mode=mode, error_text=text, parent_id=parent or "", engine=engine, lang=lang)

    # ------------------------------------------------------------------ privacy
    def inventory(self) -> dict[str, Any]:
        files = list(self.dir.glob("chg_*.json")) if self.dir.is_dir() else []
        size = sum(f.stat().st_size for f in files if f.is_file())
        from jarvis.devmode.inspect import _CACHE

        return {"changes": len(files), "bytes": size, "cached_projects": len(_CACHE), "sessions": len(self._sessions)}

    def clear(self) -> dict[str, int]:
        """Forget the proposed changes (with the backups undo needs) and the cached project scans."""
        from jarvis.devmode.inspect import clear_cache

        removed = 0
        if self.dir.is_dir():
            for f in self.dir.glob("chg_*.json"):
                with contextlib.suppress(OSError):
                    f.unlink()
                    removed += 1
        self._changes.clear()
        for s in self._sessions.values():
            s.last_change = None
        return {"changes": removed, "cache": clear_cache()}

    async def aclose(self) -> None:
        for sid in list(self._sessions):
            with contextlib.suppress(Exception):
                self.close(sid)

    # ------------------------------------------------------------------ helpers
    def _require(self, change_id: str, lang: str) -> Change:
        change = self.change(change_id)
        if change is None:
            raise JarvisError(t(lang, "השינוי לא נמצא (אולי נמחק בניקוי הנתונים)", "the change was not found (it may have been cleared)"))
        return change

    def _facts(self, change: Change) -> dict[str, Any]:
        session = self._sessions.get(change.session_id)
        return (session.report if session and session.report else {}) or {}

    def _keep(self, change: Change) -> None:
        self._changes[change.id] = change
        self._changes.move_to_end(change.id)
        while len(self._changes) > KEEP_CHANGES:
            self._changes.popitem(last=False)
        self._save(change)
        self._trim_disk()

    def _save(self, change: Change) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"{change.id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(change.model_dump_json(), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            log.warning("Could not save the proposed change %s", change.id, exc_info=True)

    def _trim_disk(self) -> None:
        with contextlib.suppress(OSError):
            files = sorted(self.dir.glob("chg_*.json"), key=lambda p: p.stat().st_mtime)
            for old in files[:-KEEP_CHANGES]:
                old.unlink(missing_ok=True)


# ============================================================================ reading test output
_PYTEST = re.compile(r"^(FAILED|ERROR)\s+(\S+?)(?:\s+-\s+(.*))?$", re.M)
_VITEST = re.compile(r"^\s*(?:FAIL|×|✗|✕)\s+(.+?)(?:\s+\d+ms)?\s*$", re.M)
_JEST = re.compile(r"^\s*●\s+(.+?)\s*$", re.M)
_TAP = re.compile(r"^\s*not ok \d+ - (.+?)\s*$", re.M)


def parse_failures(output: str) -> list[dict[str, str]]:
    """The failing tests named in pytest, vitest, jest or node:test output, with pytest's short reason."""
    seen: dict[str, dict[str, str]] = {}
    for kind, name, message in _PYTEST.findall(output):
        seen.setdefault(name, {"name": name, "message": (message or kind.lower())[:300]})
    if not seen:
        for regex in (_VITEST, _JEST, _TAP):
            for name in regex.findall(output):
                name = name.strip()
                if name and len(name) < 300 and not name.lower().startswith(("tests ", "test files", "test suites")):
                    seen.setdefault(name, {"name": name, "message": ""})
    return list(seen.values())


def _test_hint(output: str, lang: str) -> str:
    if "No module named pytest" in output:
        return t(lang, "pytest לא מותקן בסביבה של הפרויקט. אפשר להתקין אותו (pip install pytest) ולהריץ שוב.",
                 "pytest is not installed in the project's environment. Install it (pip install pytest) and run again.")
    if re.search(r"Cannot find module|ERR_MODULE_NOT_FOUND|'vitest' is not recognized|vitest: not found", output):
        return t(lang, "חסרות תלויות של Node. כנראה צריך להריץ npm install בפרויקט.",
                 "Node dependencies are missing. The project probably needs npm install.")
    if "no tests ran" in output or "collected 0 items" in output:
        return t(lang, "לא נמצאו בדיקות להריץ.", "No tests were found to run.")
    return ""


def _report_summary(report: dict[str, Any], lang: str) -> str:
    langs = ", ".join(x["name"] for x in report.get("languages", [])[:3]) or t(lang, "בלי קוד מוכר", "no known code")
    frameworks = ", ".join(report.get("frameworks", [])[:4])
    test = (report.get("commands") or {}).get("test") or t(lang, "אין", "none")
    parts = [t(lang, f"{report.get('files', 0)} קבצים", f"{report.get('files', 0)} files"), langs]
    if frameworks:
        parts.append(frameworks)
    parts.append(t(lang, f"בדיקות: {test}", f"tests: {test}"))
    return " · ".join(parts)


__all__ = ["DevService", "DevSession", "parse_failures"]
