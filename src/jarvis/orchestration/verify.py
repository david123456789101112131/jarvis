"""Verification layer: JARVIS does not take "done" from an agent on trust.

* **Facts** (always, no AI needed): every file an agent reports as created exists; expected files
  are there and not empty; plan checks (`files_exist`, `path_exists`, `success`) hold.
* **Requirements** (when an AI provider is configured): for work the model did, a strict reviewer
  checks the report against the step's acceptance criteria.
* **Goal** (autonomous mode): after the plan finishes, a reviewer judges whether the user's goal is
  reached and, if not, proposes the follow-up steps.

The agents' own checks complement this (the Development Agent runs the project's tests and link
checks, the System Agent verifies moves), and their results are what the reviewer reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from jarvis.agents.base import AGENT_INFO, AgentResult, t
from jarvis.common.enums import ResultStatus, TaskStatus
from jarvis.common.errors import ProviderError
from jarvis.orchestration.models import PlannedStep, SubTask

if TYPE_CHECKING:
    from jarvis.ai.router import ProviderRouter
    from jarvis.orchestration.task import JarvisTask

log = logging.getLogger(__name__)

_TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".json", ".md", ".txt",
                  ".java", ".csv", ".xml", ".yml", ".yaml", ".svg", ".toml", ".ini", ".sql"}


class StepVerdict(BaseModel):
    passed: bool = Field(description="True when the report shows every acceptance criterion is met")
    problems: list[str] = Field(description="Concrete unmet criteria or errors, one short sentence each; empty if passed")


class GoalCheck(BaseModel):
    achieved: bool = Field(description="True when the results fully deliver what the user asked for")
    missing: list[str] = Field(description="What is still missing, one short item each; empty if achieved")
    steps: list[PlannedStep] = Field(description="Up to 3 follow-up steps that would complete the goal; empty if achieved")


STEP_SYSTEM = """You review the work of one agent of JARVIS, a personal AI assistant on the user's Windows computer.
Decide if the step is really done, judging only from the agent's report, its file list and the acceptance criteria.
- Fail it when a criterion is clearly not met, the report admits an error that matters, or required files are missing.
- Pass it when the report plausibly covers every criterion; do not fail for style, wording or extras.
- Problems must be concrete and actionable, so the agent can fix them on the next attempt."""

GOAL_SYSTEM = """You are JARVIS's final reviewer. The user's agents finished a plan. Decide whether the user's goal is
fully achieved from the results. If something important is missing, propose at most 3 follow-up steps for these agents:
research, system, development, creative, planner. Each instruction must be complete on its own. Do not propose steps
for nice-to-have polish, and never propose deleting data, installing software or changing security settings unless the
user asked for it. If the goal is achieved, return achieved=true with no steps."""


class Verifier:
    def __init__(self, *, roots: Callable[[], list[Path]], router: Callable[[], ProviderRouter | None]) -> None:
        self._roots = roots
        self._router = router

    @property
    def ai(self) -> ProviderRouter | None:
        router = self._router()
        return router if router is not None and router.available else None

    # ------------------------------------------------------------------ facts
    async def check_result(self, sub: SubTask, result: AgentResult, *, lang: str,
                           project_dir: str | None = None) -> list[str]:
        """Problems found on disk and in the result itself (empty list = verified)."""
        return await asyncio.to_thread(self._check_sync, sub, result, lang, project_dir)

    def _search_roots(self, result: AgentResult, project_dir: str | None) -> list[Path]:
        roots: list[Path] = []
        data = result.result if isinstance(result.result, dict) else {}
        for value in (data.get("path"), data.get("folder"), project_dir):
            if isinstance(value, str) and value:
                roots.append(Path(value))
        roots += self._roots()
        return roots

    def _check_sync(self, sub: SubTask, result: AgentResult, lang: str, project_dir: str | None) -> list[str]:
        problems: list[str] = []
        roots = self._search_roots(result, project_dir)

        def locate(name: str) -> Path | None:
            p = Path(name)
            if p.is_absolute():
                return p if p.exists() else None
            for root in roots:
                candidate = root / p
                if candidate.exists():
                    return candidate
            return None

        missing = [f for f in result.files_created[:300] if locate(f) is None]
        if missing:
            shown = ", ".join(missing[:5]) + (f" (+{len(missing) - 5})" if len(missing) > 5 else "")
            problems.append(t(lang, f"קובץ שדווח כנוצר לא קיים: {shown}" if len(missing) == 1
                              else f"{len(missing)} קבצים שדווחו כנוצרו לא קיימים: {shown}",
                              f"A file reported as created does not exist: {shown}" if len(missing) == 1
                              else f"{len(missing)} files reported as created do not exist: {shown}"))
        produced = {Path(f).name.lower() for f in [*result.files_created, *result.files_modified]}
        for expected in sub.expected_files[:30]:
            name = Path(expected).name.lower()
            found = locate(expected)
            if found is None and name in produced:
                continue  # created under another folder than the plan guessed
            if found is None:
                problems.append(t(lang, f"הקובץ הצפוי לא נמצא: {expected}", f"Expected file not found: {expected}"))
            elif found.is_file() and found.suffix.lower() in _TEXT_SUFFIXES and found.stat().st_size == 0:
                problems.append(t(lang, f"הקובץ {expected} ריק", f"The file {expected} is empty"))
        for check in sub.verify:
            kind = str(check.get("kind") or "")
            if kind == "files_exist":
                data = result.result if isinstance(result.result, dict) else {}
                value = data.get("path") or data.get("folder")
                folder = Path(value) if isinstance(value, str) and value else None
                has_folder_files = folder is not None and folder.is_dir() and any(folder.iterdir())
                if not result.files_created and not has_folder_files:
                    problems.append(t(lang, "לא נוצרו קבצים", "No files were created"))
            elif kind == "path_exists":
                target = str(check.get("path") or "")
                if target and locate(target) is None:
                    problems.append(t(lang, f"הנתיב {target} לא קיים", f"The path {target} does not exist"))
            elif kind == "success" and result.status != ResultStatus.SUCCESS:
                problems.append(t(lang, "השלב הושלם רק חלקית", "The step was only partly completed"))
        return problems

    async def missing_files(self, paths: list[str]) -> list[str]:
        """Files from earlier steps that no longer exist (checked again before the final answer)."""
        return await asyncio.to_thread(lambda: [p for p in paths if Path(p).is_absolute() and not Path(p).exists()])

    # ------------------------------------------------------------------ requirements (AI reviewer)
    def wants_review(self, sub: SubTask, result: AgentResult) -> bool:
        return bool(result.used_llm and sub.acceptance_criteria and self.ai is not None)

    async def review(self, task: JarvisTask, sub: SubTask, result: AgentResult) -> list[str]:
        """Problems the reviewer found; [] when it passed (or when no reviewer is reachable)."""
        router = self.ai
        if router is None:
            return []
        lines = [
            f"The user's overall request: {task.goal}",
            f"Step given to the {AGENT_INFO[sub.agent]['name']}: {sub.title}",
            f"Instructions:\n{sub.instruction}",
            "Acceptance criteria:\n" + "\n".join(f"- {c}" for c in sub.acceptance_criteria),
            f"Agent status: {result.status.value}",
            f"Agent report:\n{(result.summary or '')[:1500]}\n{(result.details or '')[:4000]}",
        ]
        if result.files_created:
            lines.append("Files created: " + ", ".join(result.files_created[:40]))
        if result.errors:
            lines.append("Errors reported: " + "; ".join(result.errors[:10]))
        lines.append(f"Write problems in {'Hebrew' if task.language == 'he' else 'English'}.")
        try:
            verdict = await router.structured("verifier", "\n\n".join(lines), StepVerdict, system=STEP_SYSTEM)
        except ProviderError as exc:
            log.warning("Step review unavailable: %s", exc.message)
            return []
        if verdict.passed:
            return []
        return [p.strip() for p in verdict.problems if p.strip()][:8] or [
            t(task.language, "הבודק לא אישר את התוצאה", "The reviewer did not accept the result")]

    # ------------------------------------------------------------------ goal (autonomous mode)
    async def goal_check(self, task: JarvisTask) -> GoalCheck | None:
        router = self.ai
        if router is None or task.plan is None:
            return None
        results = []
        for sub in task.plan.subtasks:
            rec: dict[str, Any] = {"agent": sub.agent.value, "title": sub.title, "status": sub.status.value}
            if sub.status == TaskStatus.COMPLETED and sub.result:
                rec.update({k: sub.result[k] for k in ("summary", "details", "files_created", "next_step")
                            if k in sub.result})
            elif sub.error:
                rec["error"] = sub.error
            results.append(json.dumps(rec, ensure_ascii=False, default=str)[:3000])
        prompt = (f"The user's goal: {task.goal}\n\nResults of the plan:\n" + "\n".join(results)
                  + f"\n\nWrite in {'Hebrew' if task.language == 'he' else 'English'}.")
        try:
            return await router.structured("verifier", prompt, GoalCheck, system=GOAL_SYSTEM)
        except ProviderError as exc:
            log.warning("Goal check unavailable: %s", exc.message)
            return None


__all__ = ["GoalCheck", "StepVerdict", "Verifier"]
