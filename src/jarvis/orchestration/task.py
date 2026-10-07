"""A JARVIS task: the user's goal, its plan, the live state of every subtask, a timeline of what
happened and the final outcome.

The whole object is saved to task memory after every state change (`to_record`), and rebuilt from
there (`from_record`), which is what lets JARVIS show a past task exactly as it ran and continue an
interrupted one without redoing finished steps.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.common.clock import iso, local_hms, parse_iso, utcnow
from jarvis.common.enums import AgentId, Mode, Priority, ResultStatus, TaskStatus
from jarvis.common.ids import new_id
from jarvis.common.text import WAKE_WORD_FORMS
from jarvis.memory.models import TaskRecord
from jarvis.orchestration.models import SubTask, TaskPlan

TIMELINE_LIMIT = 400
ACTIVE = (TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING,
          TaskStatus.VERIFYING)


class TimelineEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ts: datetime = Field(default_factory=utcnow)
    kind: str
    message: str
    agent_id: str | None = None
    subtask_id: str | None = None
    level: str = "info"

    def view(self) -> dict[str, Any]:
        return {"ts": iso(self.ts), "time": local_hms(self.ts), "kind": self.kind, "message": self.message,
                "agent_id": self.agent_id, "subtask_id": self.subtask_id, "level": self.level}


class TaskOutcome(BaseModel):
    """The one answer JARVIS gives for a task (`TaskDetail.result` in docs/API.md)."""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    details: str | None = None
    files_created: list[str] = Field(default_factory=list)
    actions_taken: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    next_step: str | None = None
    used_llm: bool = False

    def view(self) -> dict[str, Any]:
        return {"summary": self.summary, "details": self.details, "files_created": list(self.files_created),
                "actions_taken": list(self.actions_taken), "issues": list(self.issues), "next_step": self.next_step}


class JarvisTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("task"))
    title: str = ""
    goal: str
    status: TaskStatus = TaskStatus.PENDING
    priority: Priority = Priority.NORMAL
    mode: Mode = Mode.NORMAL
    source: str = "text"
    language: str = "he"
    intent: str | None = None
    slots: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    plan: TaskPlan | None = None
    timeline: list[TimelineEntry] = Field(default_factory=list)
    outcome: TaskOutcome | None = None
    error: str | None = None
    project_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    dispatches: int = 0          # agent assignments made for this task (the loop guard counts them)
    followups: int = 0           # follow-up rounds added after the plan (helpers, autonomous steps)
    runs: int = 0                # 1 for the first run, +1 for every resume or retry
    auto_title: bool = True      # the title came from the goal and may be replaced by the plan's title

    # ------------------------------------------------------------------ state
    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def agents(self) -> list[AgentId]:
        return self.plan.agents if self.plan else []

    def progress(self) -> float:
        if self.status == TaskStatus.COMPLETED:
            return 1.0
        if self.plan is None or not self.plan.subtasks:
            return 0.0
        return self.plan.progress()

    def note(self, kind: str, message: str, *, agent_id: str | None = None, subtask_id: str | None = None,
             level: str = "info") -> TimelineEntry:
        entry = TimelineEntry(kind=kind, message=message, agent_id=agent_id, subtask_id=subtask_id, level=level)
        self.timeline.append(entry)
        if len(self.timeline) > TIMELINE_LIMIT:
            # keep the first entries (how the task started) and the most recent ones
            del self.timeline[20: len(self.timeline) - TIMELINE_LIMIT + 20]
        return entry

    def touch(self) -> None:
        self.updated_at = utcnow()

    # ------------------------------------------------------------------ views (docs/API.md)
    def summary_view(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title or self.goal[:80], "goal": self.goal, "status": self.status.value,
            "progress": round(self.progress(), 3), "priority": self.priority.name.lower(), "mode": self.mode.value,
            "agents": [a.value for a in self.agents], "source": self.source,
            "created_at": iso(self.created_at), "updated_at": iso(self.updated_at),
            "started_at": iso(self.started_at) if self.started_at else None,
            "finished_at": iso(self.finished_at) if self.finished_at else None,
            "summary": self.outcome.summary if self.outcome else None, "error": self.error,
        }

    def detail_view(self) -> dict[str, Any]:
        out = self.summary_view()
        out["plan"] = ({"strategy": self.plan.strategy, "title": self.plan.title, "source": self.plan.source,
                        "subtasks": [s.view() for s in self.plan.subtasks]} if self.plan else None)
        out["timeline"] = [e.view() for e in self.timeline]
        out["result"] = self.outcome.view() if self.outcome else None
        out["language"] = self.language
        out["intent"] = self.intent
        out["runs"] = self.runs
        return out

    # ------------------------------------------------------------------ persistence
    def to_record(self) -> TaskRecord:
        data = {
            "language": self.language, "intent": self.intent, "slots": self.slots,
            "context": _bounded(self.context, 6000),
            "plan": self.plan.model_dump(mode="json") if self.plan else None,
            "timeline": [e.model_dump(mode="json") for e in self.timeline],
            "outcome": self.outcome.model_dump(mode="json") if self.outcome else None,
            "dispatches": self.dispatches, "followups": self.followups, "runs": self.runs,
            "auto_title": self.auto_title,
        }
        return TaskRecord(
            id=self.id, title=self.title, goal=self.goal, status=self.status.value,
            priority=self.priority.name.lower(), mode=self.mode.value, source=self.source,
            created_at=self.created_at, updated_at=self.updated_at, started_at=self.started_at,
            finished_at=self.finished_at, summary=self.outcome.summary if self.outcome else None, error=self.error,
            project_id=self.project_id, data=data,
        )

    @classmethod
    def from_record(cls, rec: TaskRecord) -> JarvisTask:
        data = dict(rec.data or {})
        plan = None
        if isinstance(data.get("plan"), dict):
            try:
                plan = TaskPlan.model_validate(data["plan"])
            except ValidationError:
                plan = None
        timeline: list[TimelineEntry] = []
        for item in data.get("timeline") or []:
            try:
                timeline.append(TimelineEntry.model_validate(item))
            except ValidationError:
                continue
        outcome = None
        if isinstance(data.get("outcome"), dict):
            try:
                outcome = TaskOutcome.model_validate(data["outcome"])
            except ValidationError:
                outcome = None
        try:
            status = TaskStatus(rec.status)
        except ValueError:
            status = TaskStatus.INTERRUPTED
        try:
            mode = Mode(rec.mode)
        except ValueError:
            mode = Mode.NORMAL
        return cls(
            id=rec.id, title=rec.title, goal=rec.goal, status=status, priority=Priority.parse(rec.priority),
            mode=mode, source=rec.source or "text", language=str(data.get("language") or "he"),
            intent=data.get("intent"), slots=dict(data.get("slots") or {}), context=dict(data.get("context") or {}),
            plan=plan, timeline=timeline, outcome=outcome, error=rec.error, project_id=rec.project_id,
            created_at=parse_iso(rec.created_at) or utcnow(), updated_at=parse_iso(rec.updated_at) or utcnow(),
            started_at=parse_iso(rec.started_at), finished_at=parse_iso(rec.finished_at),
            dispatches=int(data.get("dispatches") or 0), followups=int(data.get("followups") or 0),
            runs=int(data.get("runs") or 0), auto_title=bool(data.get("auto_title", True)),
        )


# ------------------------------------------------------------------------------------------ helpers
_WAKE_PREFIX = re.compile(
    r"^\s*(?:" + "|".join(re.escape(w) for w in sorted(WAKE_WORD_FORMS, key=len, reverse=True)) + r")[\s,.:!]*",
    re.IGNORECASE,
)


def short_title(goal: str, limit: int = 70) -> str:
    """A task title from the request: without the wake word, one line, cut at a word boundary."""
    text = _WAKE_PREFIX.sub("", " ".join((goal or "").split())).strip(" .,!?")
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]) + "…"


def compact(value: Any, budget: int = 6000) -> Any:
    """`value` reduced to roughly `budget` characters of JSON: long lists are cut and large dict entries
    are dropped first, so small useful fields (a project path, a palette, a URL) always survive."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)[:budget]
    if len(encoded) <= budget:
        return value
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            try:
                size = len(json.dumps(item, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                continue
            items.append((size, key, item))
        out: dict[str, Any] = {}
        used = 2
        for size, key, item in sorted(items, key=lambda x: x[0]):
            if used + size + len(str(key)) + 4 <= budget:
                out[key] = item
                used += size + len(str(key)) + 4
            elif isinstance(item, (list, dict)) and used + 200 < budget:
                reduced = compact(item, min(budget - used - 20, max(200, budget // 4)))
                out[key] = reduced
                used += len(json.dumps(reduced, ensure_ascii=False, default=str)) + len(str(key)) + 4
        return {k: out[k] for k in value if k in out}   # original key order
    if isinstance(value, list):
        out_list: list[Any] = []
        used = 2
        for item in value:
            reduced = compact(item, max(100, budget // 4))
            size = len(json.dumps(reduced, ensure_ascii=False, default=str)) + 2
            if used + size > budget:
                break
            out_list.append(reduced)
            used += size
        return out_list
    if isinstance(value, str):
        return value[: max(0, budget - 1)] + "…"
    return value


def _bounded(value: dict[str, Any], budget: int) -> dict[str, Any]:
    reduced = compact(value, budget)
    return reduced if isinstance(reduced, dict) else {}


def result_record(result: Any) -> dict[str, Any]:
    """What a finished subtask keeps of its `AgentResult`: enough for the steps after it, the final
    answer and the task view, small enough to save after every change."""
    rec: dict[str, Any] = {"agent": result.agent_id, "status": result.status.value, "summary": result.summary}
    if result.details:
        rec["details"] = result.details[:3000]
    for key in ("files_created", "files_modified", "errors", "denied", "actions_taken"):
        items = getattr(result, key)
        if items:
            rec[key] = [str(x)[:500] for x in items[:40]]
    if result.next_step:
        rec["next_step"] = result.next_step
    if result.sources:
        rec["sources"] = result.sources[:10]
    if result.result is not None:
        rec["result"] = compact(result.result, 6000)
    code = result.data.get("code") if isinstance(result.data, dict) else None
    if code:
        rec["code"] = code
    if result.used_llm:
        rec["used_llm"] = True
    if result.status == ResultStatus.PARTIAL:
        rec["partial"] = True
    return rec


def dependency_record(sub: SubTask) -> dict[str, Any]:
    """A finished step as the next agents see it in `task.context["dependencies"]`."""
    rec = {"key": sub.key, "agent": sub.agent.value, "title": sub.title}
    rec.update(sub.result or {})
    rec.setdefault("summary", sub.summary or "")
    rec.setdefault("status", "success")
    return rec


__all__ = ["ACTIVE", "JarvisTask", "TaskOutcome", "TimelineEntry", "compact", "dependency_record",
           "result_record", "short_title"]
