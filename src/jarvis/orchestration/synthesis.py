"""The final answer: JARVIS turns the agents' results into one clear reply.

With an AI provider the reply is written by the model (it also reconciles results that disagree);
without one, a structured template lists what each agent did, the files created, problems and the
next step. Either way the files, actions and issues come from the recorded results, not from the
model, so the facts in the answer are the facts on disk.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from jarvis.agents.base import AGENT_INFO, t
from jarvis.common.enums import TaskStatus
from jarvis.common.errors import ProviderError
from jarvis.common.i18n import steps_done
from jarvis.orchestration.task import TaskOutcome, short_title

if TYPE_CHECKING:
    from jarvis.ai.router import ProviderRouter
    from jarvis.orchestration.models import SubTask
    from jarvis.orchestration.task import JarvisTask

log = logging.getLogger(__name__)


class SynthesisDraft(BaseModel):
    summary: str = Field(description="One or two sentences that answer the request; this part is read aloud")
    details: str = Field(description="Key results as short lines: findings, decisions, files (full paths), how to use "
                                     "them; empty when the summary says it all")
    next_step: str = Field(description="The most useful next step for the user, or an empty string")


SYNTHESIS_SYSTEM = """You are JARVIS, the user's personal AI assistant on their Windows computer. Your five agents just
worked on the user's request. Write your final answer to the user from their results.
- Speak as JARVIS ("I"), in the user's language.
- The summary answers the request or says what was done in one or two sentences; it is read aloud.
- Details: short lines with the key results (findings, decisions, files with full paths, how to open or use them).
- If agents' results disagree, resolve it: say which result you rely on and why.
- Be honest about anything that failed or is incomplete and what the user can do about it.
- Use only facts from the results. No markdown headings or tables; at most 10 short lines in details."""

_MARK = {TaskStatus.COMPLETED: "✓", TaskStatus.FAILED: "✗", TaskStatus.SKIPPED: "–", TaskStatus.CANCELLED: "■",
         TaskStatus.PENDING: "…"}


def _agent(sub: SubTask, lang: str) -> str:
    return AGENT_INFO[sub.agent]["name_he" if lang == "he" else "name"]


def _dedupe(items: Any, limit: int) -> list[str]:
    out: list[str] = []
    for item in items:
        text = str(item)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def collect(task: JarvisTask, *, missing_files: list[str] | None = None) -> TaskOutcome:
    """Files, actions, issues and next step gathered from the subtasks (no wording yet)."""
    lang = task.language
    subs = task.plan.subtasks if task.plan else []
    done = [s for s in subs if s.status == TaskStatus.COMPLETED]
    files = _dedupe((f for s in done for f in s.files_created), 100)
    actions = _dedupe((a for s in done for a in (s.result or {}).get("actions_taken", [])), 60)
    issues: list[str] = []
    for s in subs:
        who = _agent(s, lang)
        rec = s.result or {}
        if s.status == TaskStatus.COMPLETED and rec.get("partial"):
            for err in rec.get("errors", [])[:3]:
                issues.append(f"{who}: {err}")
        elif s.status == TaskStatus.FAILED:
            issues.append(f"{who} — {s.title}: {s.error or t(lang, 'נכשל', 'failed')}")
        elif s.status == TaskStatus.SKIPPED:
            issues.append(f"{who} — {s.title}: {s.error or t(lang, 'דילגתי על השלב', 'skipped')}")
        for denied in rec.get("denied", [])[:3]:
            issues.append(t(lang, f"{who}: לא אושר — {denied}", f"{who}: not approved — {denied}"))
    for path in (missing_files or [])[:10]:
        issues.append(t(lang, f"הקובץ {path} כבר לא קיים", f"The file {path} no longer exists"))
    next_step = None
    for s in reversed(done):
        step = (s.result or {}).get("next_step")
        if step:
            next_step = str(step)
            break
    return TaskOutcome(files_created=files, actions_taken=actions, issues=_dedupe(issues, 30), next_step=next_step)


def template_outcome(task: JarvisTask, *, missing_files: list[str] | None = None) -> TaskOutcome:
    """The answer without an AI model."""
    lang = task.language
    outcome = collect(task, missing_files=missing_files)
    subs = task.plan.subtasks if task.plan else []
    title = task.title or short_title(task.goal)
    done = [s for s in subs if s.status == TaskStatus.COMPLETED]
    n, d = len(subs), len(done)
    agents = ", ".join(_dedupe((_agent(s, lang) for s in done), 5))
    status = task.status
    if status == TaskStatus.COMPLETED:
        if n == 1 and done:
            summary = done[0].summary or t(lang, f"סיימתי: {title}.", f"Done: {title}.")
        else:
            summary = t(lang, f"סיימתי את '{title}'. {'שלב אחד הושלם' if d == 1 else f'{d} שלבים הושלמו'} ({agents}).",
                        f"I finished '{title}'. {d} step{'' if d == 1 else 's'} completed ({agents}).")
        k = len(outcome.issues)
        if k:
            summary += t(lang, " יש הערה אחת בפירוט." if k == 1 else f" יש {k} הערות בפירוט.",
                         " There is 1 note below." if k == 1 else f" There are {k} notes below.")
    elif status == TaskStatus.CANCELLED:
        summary = t(lang, f"עצרתי את '{title}'. {steps_done(lang, d, n)}.",
                    f"I stopped '{title}'. {steps_done(lang, d, n).capitalize()}.")
    elif status == TaskStatus.INTERRUPTED:
        reason = f" ({task.error})" if task.error else ""
        summary = t(lang, f"המשימה '{title}' נעצרה באמצע{reason}. {steps_done(lang, d, n)}, ואפשר להמשיך מאותה נקודה.",
                    f"'{title}' was paused{reason}. {steps_done(lang, d, n).capitalize()}, and it can continue from there.")
    elif task.plan is None:
        summary = t(lang, f"לא הצלחתי לתכנן את '{title}'.", f"I could not plan '{title}'.")
        if task.error:
            summary += f" {task.error}"
    else:
        summary = t(lang, f"לא הצלחתי להשלים את '{title}': {steps_done(lang, d, n)}.",
                    f"I could not complete '{title}': {steps_done(lang, d, n)}.")
        first = next((s for s in subs if s.status in (TaskStatus.FAILED, TaskStatus.CANCELLED) and not s.optional),
                     None)
        if first is not None and first.error:
            summary += t(lang, f" {_agent(first, lang)} נכשל: {first.error}", f" The {_agent(first, lang)} failed: "
                         f"{first.error}")
        elif task.error:
            summary += f" {task.error}"
    lines: list[str] = []
    if n > 1 or status != TaskStatus.COMPLETED:
        for s in subs:
            text = s.summary if s.status == TaskStatus.COMPLETED else (s.error or "")
            mark = _MARK.get(s.status, "…")
            lines.append(f"{mark} {_agent(s, lang)} — {s.title}" + (f": {_one_line(text, 240)}" if text else ""))
    elif done and done[0].result and done[0].result.get("details"):
        lines.append(str(done[0].result["details"])[:3000])
    if outcome.files_created:
        lines.append(t(lang, "קבצים:", "Files:"))
        lines += [f"  {f}" for f in outcome.files_created[:15]]
        if len(outcome.files_created) > 15:
            lines.append(t(lang, f"  ועוד {len(outcome.files_created) - 15}", f"  and {len(outcome.files_created) - 15} more"))
    if outcome.issues and (n > 1 or status != TaskStatus.COMPLETED):
        lines.append(t(lang, "הערות:", "Notes:"))
        lines += [f"  {i}" for i in outcome.issues[:10]]
    outcome.summary = summary.strip()
    outcome.details = "\n".join(lines) or None
    if status == TaskStatus.INTERRUPTED:
        outcome.next_step = t(lang, "להמשיך את המשימה כשהסוכנים זמינים.", "Resume the task when the agents are available.")
    elif status == TaskStatus.FAILED and task.plan is not None:
        outcome.next_step = outcome.next_step or t(lang, "לנסות שוב את השלבים שנכשלו.", "Retry the failed steps.")
    return outcome


async def synthesize(task: JarvisTask, router: ProviderRouter | None, *,
                     missing_files: list[str] | None = None) -> TaskOutcome:
    """The final answer: written by the model for multi-step work when one is available, else the template."""
    base = template_outcome(task, missing_files=missing_files)
    subs = task.plan.subtasks if task.plan else []
    worth_it = (len(subs) >= 2 and task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                and any(s.status == TaskStatus.COMPLETED for s in subs))
    if router is None or not router.available or not worth_it:
        return base
    records = []
    budget = 24000
    for s in subs:
        rec: dict[str, Any] = {"agent": s.agent.value, "title": s.title, "status": s.status.value,
                               "optional": s.optional}
        if s.status == TaskStatus.COMPLETED and s.result:
            rec.update({k: s.result[k] for k in ("summary", "details", "files_created", "next_step", "errors",
                                                 "sources") if k in s.result})
        elif s.error:
            rec["error"] = s.error
        encoded = json.dumps(rec, ensure_ascii=False, default=str)[:4000]
        if budget - len(encoded) < 0:
            break
        budget -= len(encoded)
        records.append(encoded)
    prompt = (f"The user's request: {task.goal}\nTask status: {task.status.value}\n\nResults:\n" + "\n".join(records)
              + ("\n\nKnown problems:\n" + "\n".join(f"- {i}" for i in base.issues) if base.issues else "")
              + f"\n\nAnswer in {'Hebrew' if task.language == 'he' else 'English'}.")
    try:
        draft = await router.structured("synthesis", prompt, SynthesisDraft, system=SYNTHESIS_SYSTEM)
    except ProviderError as exc:
        log.warning("Synthesis with the AI model failed (%s); using the template", exc.message)
        return base
    if not draft.summary.strip():
        return base
    base.summary = draft.summary.strip()
    details = draft.details.strip()
    if base.files_created and not any(f in details for f in base.files_created[:3]):
        details += ("\n" if details else "") + t(task.language, "קבצים:", "Files:") + "\n" + "\n".join(
            f"  {f}" for f in base.files_created[:15])
    base.details = details or base.details
    base.next_step = draft.next_step.strip() or base.next_step
    base.used_llm = True
    return base


def _one_line(text: str, limit: int) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


__all__ = ["SynthesisDraft", "collect", "synthesize", "template_outcome"]
