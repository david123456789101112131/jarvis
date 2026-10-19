"""The code screen's three tools: look at a project, ask an AI for a change, write (or undo) the reviewed change.

They run as the Development agent through the tool executor like every other action, so the agent's folder
permissions, the approval dialog and the audit apply. They are not offered to the agents' own models: only
the code screen starts them, on the user's click (`jarvis.devmode.service`).

* `dev_inspect` reads the project (file names and small manifests) and ranks the files a request is about.
* `dev_propose` sends the request and the files the user ticked to the AI the user chose. Sending the user's
  code out of the computer is HIGH risk, so it is approved in the standard dialog, which lists the files, the
  destination and the estimated price. Secret-looking values are hidden before anything is sent.
* `dev_apply` writes exactly the change the user reviewed (bound by a digest of its text, so a change edited
  after the approval is not written), or puts the previous text back for undo. Files that changed on disk
  since the proposal are not touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.common.errors import PermissionDenied, ToolError
from jarvis.permissions.models import PathUse
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult

C = Capability


def _dev(ctx: ToolContext) -> Any:
    service = ctx.services.extras.get("dev")
    if service is None:
        raise ToolError(ctx.t("מצב הפיתוח לא זמין", "development mode is not available"))
    return service


def _root(ctx: ToolContext, path: str) -> Path:
    root = ctx.resolve(path)
    if not root.is_dir():
        raise ToolError(ctx.t(f"תיקיית הפרויקט לא נמצאה: {root}", f"Project folder not found: {root}"))
    return root


# ============================================================================ dev_inspect
class DevInspectParams(BaseModel):
    path: str = Field(..., description="The project folder")
    request: str = Field("", max_length=4000, description="What the user wants (ranks the files it is about)")
    error_text: str = Field("", max_length=20_000, description="An error or test output to find the files of")
    refresh: bool = False
    limit: int = Field(8, ge=1, le=24)


class DevInspectTool(Tool):
    name = "dev_inspect"
    description = "Internal (code screen): what a project is and which of its files a request is about."
    capabilities = (C.FS_READ,)
    Params = DevInspectParams
    read_only = True
    risk = RiskLevel.SAFE
    timeout_s = 120.0

    async def plan(self, params: DevInspectParams, ctx: ToolContext) -> ActionPlan:
        root = ctx.resolve(params.path)
        return ActionPlan(summary=ctx.t(f"בדיקת הפרויקט {root.name}", f"Look at the project {root.name}"),
                          paths=[PathUse(path=str(root), access="read")])

    async def run(self, params: DevInspectParams, ctx: ToolContext) -> ToolResult:
        from jarvis.devmode.inspect import inspect_project, scan_cached
        from jarvis.devmode.relevance import rank_files

        root = _root(ctx, params.path)
        scanned = await asyncio.to_thread(scan_cached, root, refresh=params.refresh)
        ctx.check_cancelled()
        report = await asyncio.to_thread(inspect_project, root, scanned)
        ranked: list[dict[str, Any]] = []
        if params.request.strip() or params.error_text.strip():
            found = await asyncio.to_thread(rank_files, root, scanned.files, params.request,
                                            error_text=params.error_text, limit=params.limit, lang=ctx.lang)
            ranked = [{"path": r.path, "score": round(r.score, 1), "size": r.size, "reasons": r.reasons,
                       "lines": r.lines} for r in found]
        listing = sorted(f.path for f in scanned.files if not f.secret)
        summary = ctx.t(f"נבדק {root.name}: {report['files']} קבצים", f"Looked at {root.name}: {report['files']} files")
        if ranked:
            summary += ctx.t(f", {len(ranked)} רלוונטיים", f", {len(ranked)} relevant")
        return ToolResult(summary=summary, data={"report": report, "ranked": ranked, "listing": listing[:3000],
                                                 "listing_total": len(listing)})


# ============================================================================ dev_propose
class DevProposeParams(BaseModel):
    path: str = Field(..., description="The project folder")
    session_id: str = Field(..., min_length=3, max_length=64)
    request: str = Field(..., min_length=2, max_length=4000)
    files: list[str] = Field(default_factory=list, max_length=24, description="Project-relative files to send")
    mode: Literal["auto", "fast", "strong"] = "auto"
    error_text: str = Field("", max_length=20_000)
    parent_id: str = Field("", max_length=64)


class DevProposeTool(Tool):
    name = "dev_propose"
    description = "Internal (code screen): ask the chosen AI for a focused change to the project, to review."
    capabilities = (C.AI_GENERATE, C.FS_READ)
    Params = DevProposeParams
    timeout_s = 900.0

    async def plan(self, params: DevProposeParams, ctx: ToolContext) -> ActionPlan:
        from jarvis.devmode import prompt
        from jarvis.devmode.edits import safe_relative
        from jarvis.devmode.inspect import MAX_TEXT_BYTES, is_secret_name

        root = _root(ctx, params.path)
        engine = _engine(ctx, params, files=len(params.files), chars=0)
        chosen: list[tuple[str, Path, int]] = []
        skipped: list[str] = []
        total = 0
        for given in params.files:
            rel = safe_relative(given)
            target = (root / rel) if rel else None
            if rel is None or target is None:
                skipped.append(given)
                continue
            if is_secret_name(target.name):
                skipped.append(rel)
                continue
            try:
                size = target.stat().st_size if target.is_file() else -1
            except OSError:
                size = -1
            if size < 0 or size > MAX_TEXT_BYTES:
                skipped.append(rel)
                continue
            chars = min(size, prompt.MAX_FILE_CHARS)
            if total + chars > prompt.MAX_SEND_CHARS:
                skipped.append(rel)
                continue
            total += chars
            chosen.append((rel, target, chars))
        effort, why = prompt.choose_effort(params.mode, params.request, len(chosen), total, bool(params.error_text.strip()))
        engine = _engine(ctx, params, files=len(chosen), chars=total, effort=effort)
        prompt_chars = total + len(params.request) + min(len(params.error_text), 8_000) + 6_000
        estimate = prompt.estimate_cost(engine, prompt_chars)
        ctx.extra.update(engine=engine, chosen=chosen, effort_why=why, skipped=skipped)
        names = [f"{rel} ({chars // 1000 or 1}K)" for rel, _p, chars in chosen]
        what = ctx.t(f"הבקשה ו-{len(chosen)} קבצים מהפרויקט", f"the request and {len(chosen)} project files") \
            if chosen else ctx.t("הבקשה ותיאור הפרויקט (בלי קבצים)", "the request and the project facts (no files)")
        paths = [PathUse(path=str(root), access="read"), *(PathUse(path=str(p), access="read") for _r, p, _c in chosen)]
        details: dict[str, Any] = {
            "engine": engine.label(ctx.lang),
            "files": names,
            "chars": total,
            "price": prompt.cost_text(estimate, ctx.lang, local=engine.local),
            "secrets": ctx.t("ערכים שנראים סודיים (מפתחות, סיסמאות) מוסתרים לפני השליחה",
                             "values that look secret (keys, passwords) are hidden before sending"),
        }
        if skipped:
            details["not_sent"] = skipped[:20]
        if engine.local:
            return ActionPlan(summary=ctx.t(f"תכנון שינוי במודל המקומי ({root.name})", f"Plan a change with the local model ({root.name})"),
                              capabilities=[C.AI_GENERATE, C.FS_READ], paths=paths, details=details,
                              remember_scope=str(root))
        return ActionPlan(
            summary=ctx.t(f"שליחת {what} אל {engine.name} לתכנון שינוי", f"Send {what} to {engine.name} to plan a change"),
            capabilities=[C.AI_GENERATE, C.FS_READ], paths=paths, risk=RiskLevel.HIGH, sends_data=True,
            destination=f"{engine.name} ({_host(engine.provider)})", details=details, remember_scope=str(root),
            risk_reasons=[ctx.t(f"הקוד של הפרויקט יוצא מהמחשב אל {engine.name} ({_host(engine.provider)})",
                                f"the project's code leaves the computer for {engine.name} ({_host(engine.provider)})"),
                          ctx.t(f"מחיר משוער: {prompt.cost_text(estimate, 'he', local=False)}",
                                f"estimated price: {prompt.cost_text(estimate, 'en', local=False)}")],
        )

    async def run(self, params: DevProposeParams, ctx: ToolContext) -> ToolResult:
        from jarvis.ai.context import agent_scope
        from jarvis.ai.types import AIMessage
        from jarvis.common.errors import ProviderError
        from jarvis.devmode.edits import Proposal
        from jarvis.devmode.inspect import inspect_project, read_text, scan_cached
        from jarvis.devmode.prompt import MAX_FILE_CHARS, actual_cost, build_messages

        root = _root(ctx, params.path)
        engine = ctx.extra["engine"]
        router = ctx.services.ai
        if router is None:
            return ToolResult.fail(ctx.t("אין מודל AI מוגדר", "no AI model is configured"))
        dev = _dev(ctx)
        scanned = await asyncio.to_thread(scan_cached, root)
        facts = await asyncio.to_thread(inspect_project, root, scanned)
        sent: list[tuple[str, str, bool]] = []
        masked = 0
        for rel, target, _chars in ctx.extra["chosen"]:
            text = await asyncio.to_thread(read_text, target)
            if text is None:
                continue
            cut = len(text) > MAX_FILE_CHARS
            text = text[:MAX_FILE_CHARS]
            text, n = ctx.services.redactor.mask_code(text)
            masked += n
            sent.append((rel, text.replace("\r\n", "\n"), cut))
        error_text, n = ctx.services.redactor.mask_code(params.error_text)
        masked += n
        request, n = ctx.services.redactor.mask_code(params.request)
        masked += n
        previous = dev.previous_diff(params.parent_id) if params.parent_id else ""
        listing = sorted(f.path for f in scanned.files if not f.secret)
        system, user = build_messages(request, facts, listing, sent, error_text=error_text, previous=previous,
                                      lang=ctx.lang)
        await ctx.progress(0.2, ctx.t(f"ממתין לתשובה מ-{engine.name}…", f"Waiting for {engine.name}…"))
        req = router.request("agent", [AIMessage.user(user)], system=system, json_schema=Proposal.model_json_schema(),
                             schema_name="Proposal", effort=engine.effort, max_tokens=engine.max_tokens)
        try:
            with agent_scope("development"):
                response = await router.complete(req, provider=engine.provider)
        except ProviderError as exc:
            return ToolResult.fail(ctx.t(f"{engine.name} לא ענה: {exc.message}", f"{engine.name} did not answer: {exc.message}"),
                                   data={"code": exc.details.get("code") or "ai_error"})
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens,
                 "reasoning_tokens": response.usage.reasoning_tokens}
        cost = actual_cost(engine, usage)
        if response.refusal is not None or response.stop_reason == "refusal":
            why = response.refusal.explanation if response.refusal else ""
            return ToolResult.fail(ctx.t(f"{engine.name} סירב לבקשה. {why}".strip(), f"{engine.name} declined the request. {why}".strip()),
                                   data={"code": "refused", "usage": usage, "cost_usd": cost})
        try:
            proposal = Proposal.model_validate(response.parse_json())
        except Exception:
            cutoff = response.stop_reason == "max_tokens"
            return ToolResult.fail(
                ctx.t("התשובה של המודל נקטעה באמצע (ארוכה מדי); שום דבר לא השתנה. אפשר לנסות עם פחות קבצים."
                      if cutoff else "התשובה של המודל לא הגיעה בצורה הצפויה; שום דבר לא השתנה. אפשר לנסות שוב.",
                      "The model's answer was cut off (too long); nothing changed. Try again with fewer files."
                      if cutoff else "The model's answer did not come in the expected form; nothing changed. You can try again."),
                data={"code": "bad_answer", "usage": usage, "cost_usd": cost})
        change = await asyncio.to_thread(
            dev.record_proposal, session_id=params.session_id, root=root, request=params.request, proposal=proposal,
            facts=facts, engine=engine, usage=usage, cost=cost, sent=[s[0] for s in sent], masked=masked,
            parent_id=params.parent_id or None, error_text=params.error_text, lang=ctx.lang)
        blocked = sum(1 for f in change.files if f.blocked)
        summary = ctx.t(f"{engine.name} הציע שינוי ב-{len(change.files)} קבצים", f"{engine.name} proposed a change to {len(change.files)} file(s)")
        if blocked:
            summary += ctx.t(f" ({blocked} עם בעיה שצריך לבדוק)", f" ({blocked} with a problem to look at)")
        return ToolResult(summary=summary, data={"change_id": change.id, "files": len(change.files), "blocked": blocked,
                                                 "usage": usage, "cost_usd": cost, "model": response.model or engine.model,
                                                 "masked": masked})


def _host(provider: str) -> str:
    return {"xai": "api.x.ai", "anthropic": "api.anthropic.com", "openai": "api.openai.com"}.get(provider, provider)


def _engine(ctx: ToolContext, params: DevProposeParams, *, files: int, chars: int, effort: str = "low") -> Any:
    from jarvis.ai.router import CLOUD_NAMES, NO_PROVIDER
    from jarvis.devmode.prompt import Engine

    router = ctx.services.ai
    if router is None:
        raise ToolError(ctx.t("אין מודל AI מוגדר", "no AI model is configured"))
    chain = router.chain_for("agent", "development")
    if not chain:
        told = router.asks_first("agent", "development")
        if told:
            name = CLOUD_NAMES.get(told, told)
            raise ToolError(ctx.t(f"{name} משמש רק כשמבקשים אותו: לחיצה על 'לתכנן עם {name}' במסך הקוד",
                                  f"{name} is used only when you ask for it: press 'Plan with {name}' on the code screen"))
        raise ToolError(ctx.t("אין מודל AI מוגדר: צריך מפתח של xAI (Grok) או מפתח אחר בהגדרות > מפתחות API.",
                              NO_PROVIDER))
    provider = chain[0]
    name = CLOUD_NAMES.get(provider.name, ctx.t("מודל מקומי", "local model"))
    max_tokens = 12_000 if effort == "low" else 24_000
    return Engine(provider=provider.name, name=name, model=getattr(provider, "model", "") or "", effort=effort,
                  max_tokens=max_tokens, local=provider.name == "local")


# ============================================================================ dev_apply
class DevApplyParams(BaseModel):
    change_id: str = Field(..., min_length=3, max_length=64)
    digest: str = Field(..., min_length=8, max_length=64, description="The digest of the reviewed change")
    undo: bool = False


class DevApplyTool(Tool):
    name = "dev_apply"
    description = "Internal (code screen): write the change the user reviewed, or put the previous text back."
    capabilities = (C.FS_WRITE,)
    Params = DevApplyParams
    timeout_s = 120.0

    async def plan(self, params: DevApplyParams, ctx: ToolContext) -> ActionPlan:
        from jarvis.tools.fs import program_landing

        change = _dev(ctx).change(params.change_id)
        if change is None:
            raise ToolError(ctx.t("השינוי לא נמצא", "the change was not found"))
        if change.digest(undo=params.undo) != params.digest:
            raise PermissionDenied(ctx.t("השינוי נערך אחרי שנבדק; צריך לאשר אותו מחדש", "the change was edited after it was reviewed; approve it again"))
        root = Path(change.root)
        files = change.to_undo() if params.undo else change.to_apply()
        if not files:
            raise ToolError(ctx.t("אין קבצים לעדכן", "there are no files to update"))
        if not params.undo and any(f.blocked for f in files):
            raise ToolError(ctx.t("בחלק מהקבצים יש בעיה שצריך לתקן או להוציא מהשינוי לפני ההחלה",
                                  "some files have a problem to fix, or to leave out of the change, before applying"))
        paths: list[PathUse] = []
        reasons: list[str] = []
        risk: RiskLevel | None = None
        caps = [C.FS_WRITE]
        shown = []
        for f in files:
            target = root / f.path
            removing = params.undo and f.old_raw is None
            paths.append(PathUse(path=str(target), access="delete" if removing else "write"))
            if removing and C.FS_DELETE not in caps:
                caps.append(C.FS_DELETE)
            landing = await asyncio.to_thread(program_landing, ctx, None, target)
            if landing and not removing:
                risk = RiskLevel.HIGH
                reasons.extend(r for r in landing if r not in reasons)
            if params.undo:
                shown.append(f"{f.path} ({ctx.t('יימחק' if removing else 'יחזור לגרסה הקודמת', 'removed' if removing else 'restored')})")
            else:
                shown.append(f"{f.path} (+{f.added} −{f.removed}{ctx.t(', חדש', ', new') if f.old is None else ''})")
        count = ctx.n(len(files), "קובץ אחד", "קבצים", "file", "files")
        summary = ctx.t(f"ביטול השינוי ב-{change_name(root)}: {count}", f"Undo the change in {change_name(root)}: {count}") \
            if params.undo else ctx.t(f"החלת השינוי ב-{change_name(root)}: {count}", f"Apply the change in {change_name(root)}: {count}")
        reasons.append(ctx.t("הגרסה הקודמת של כל קובץ נשמרת, ואפשר לבטל בלחיצה", "the previous text of every file is kept, so it can be undone with one click")
                       if not params.undo else ctx.t("רק קבצים שלא השתנו מאז ההחלה מוחזרים", "only files unchanged since the change was applied are put back"))
        return ActionPlan(summary=summary, capabilities=caps, paths=paths, risk=risk, risk_reasons=reasons,
                          details={"files": shown}, remember_scope=str(root), affected_count=len(files))

    async def run(self, params: DevApplyParams, ctx: ToolContext) -> ToolResult:
        dev = _dev(ctx)
        change = dev.change(params.change_id)
        if change is None:
            return ToolResult.fail(ctx.t("השינוי לא נמצא", "the change was not found"))
        if change.digest(undo=params.undo) != params.digest:
            return ToolResult.fail(ctx.t("השינוי נערך אחרי האישור, ולכן לא נכתב. צריך לאשר אותו שוב.",
                                         "The change was edited after it was approved, so it was not written. Approve it again."),
                                   denied="blocked")
        root = Path(change.root)
        files = change.to_undo() if params.undo else change.to_apply()
        outcome = await asyncio.to_thread(_write_all, root, files, params.undo)
        if outcome["conflicts"]:
            names = ", ".join(outcome["conflicts"][:5])
            await asyncio.to_thread(dev.mark_conflicts, change.id, outcome["conflicts"], undo=params.undo)
            return ToolResult.fail(ctx.t(f"לא כתבתי כלום: הקבצים האלה השתנו מאז ההצעה: {names}. צריך לבקש הצעה חדשה.",
                                         f"Nothing was written: these files changed since the proposal: {names}. Ask for a new proposal."),
                                   data={"conflicts": outcome["conflicts"]})
        if outcome["error"]:
            return ToolResult.fail(ctx.t(f"הכתיבה נכשלה והקבצים הוחזרו למצבם: {outcome['error']}",
                                         f"Writing failed and the files were put back: {outcome['error']}"))
        await asyncio.to_thread(dev.mark_written, change.id, outcome["written"], undo=params.undo)
        result = ToolResult(summary=ctx.t(f"{'בוטל' if params.undo else 'הוחל'} השינוי ב-{ctx.n(len(files), 'קובץ אחד', 'קבצים', 'file', 'files')}",
                                          f"{'Undid' if params.undo else 'Applied'} the change to {ctx.n(len(files), 'קובץ אחד', 'קבצים', 'file', 'files')}"),
                            data={"written": [w["path"] for w in outcome["written"]]})
        for w in outcome["written"]:
            target = str(root / w["path"])
            if w["kind"] == "created":
                result.files_created.append(target)
            elif w["kind"] == "deleted":
                result.files_deleted.append(target)
            else:
                result.files_modified.append(target)
        return result


def change_name(root: Path) -> str:
    return root.name or str(root)


def _write_all(root: Path, files: list[Any], undo: bool) -> dict[str, Any]:
    """Write every file or none: check each one is still as expected, then write; on a failure put back what
    was already written."""
    from jarvis.devmode.edits import sha256

    conflicts: list[str] = []
    plans: list[tuple[Any, Path, bytes | None, bytes | None]] = []   # (file, target, current bytes, new bytes)
    for f in files:
        target = root / f.path
        current = target.read_bytes() if target.is_file() else None
        expected = f.applied_sha if undo else f.old_sha
        now = sha256(current) if current is not None else None
        if now != expected:
            conflicts.append(f.path)
            continue
        new = f.original() if undo else f.encoded()
        plans.append((f, target, current, new))
    if conflicts:
        return {"conflicts": conflicts, "written": [], "error": ""}
    done: list[tuple[Path, bytes | None]] = []
    written: list[dict[str, Any]] = []
    try:
        for f, target, current, new in plans:
            if new is None:                      # undo of a file the change created
                _remove(target)
                written.append({"path": f.path, "kind": "deleted", "sha": None})
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".jarvis-tmp")
                tmp.write_bytes(new)
                os.replace(tmp, target)
                kind = "created" if current is None else "modified"
                written.append({"path": f.path, "kind": kind, "sha": sha256(new)})
            done.append((target, current))
    except OSError as exc:
        for target, previous in reversed(done):
            with contextlib.suppress(OSError):
                if previous is None:
                    target.unlink()
                else:
                    target.write_bytes(previous)
        return {"conflicts": [], "written": [], "error": str(exc)}
    return {"conflicts": [], "written": written, "error": ""}


def _remove(target: Path) -> None:
    """A file the change created goes to the Recycle Bin when undone (deleted outright where there is none)."""
    try:
        from send2trash import send2trash

        send2trash(str(target))
    except Exception:
        target.unlink(missing_ok=True)


DEVMODE_TOOLS: tuple[type[Tool], ...] = (DevInspectTool, DevProposeTool, DevApplyTool)

__all__ = ["DEVMODE_TOOLS", "DevApplyTool", "DevInspectTool", "DevProposeTool"]
