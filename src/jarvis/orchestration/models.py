"""Task plan data model: a DAG of subtasks, each owned by one of the five agents.

The plan is part of the task record saved after every state change, which is what lets an
interrupted task resume without redoing finished subtasks.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jarvis.common.clock import iso
from jarvis.common.enums import AgentId, Priority, TaskStatus
from jarvis.common.errors import PlanError
from jarvis.common.ids import new_id

DONE_OK = (TaskStatus.COMPLETED,)
FINISHED = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED)


class SubTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("sub"))
    key: str = ""
    agent: AgentId
    title: str
    instruction: str
    depends_on: list[str] = Field(default_factory=list)       # subtask ids
    skill: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    priority: Priority = Priority.NORMAL
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    verify: list[dict[str, Any]] = Field(default_factory=list)
    timeout_s: float | None = None
    max_retries: int | None = None
    optional: bool = False
    fallback_agent: AgentId | None = None
    followup_of: str | None = None

    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    progress: float = 0.0
    result: dict[str, Any] | None = None
    summary: str | None = None
    error: str | None = None
    error_history: list[str] = Field(default_factory=list)
    files_created: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def finished(self) -> bool:
        return self.status in FINISHED

    def view(self) -> dict[str, Any]:
        """SubTaskView (docs/API.md)."""
        return {
            "id": self.id, "agent": self.agent.value, "title": self.title, "instruction": self.instruction,
            "status": self.status.value, "progress": round(self.progress, 3), "attempts": self.attempts,
            "depends_on": list(self.depends_on), "optional": self.optional,
            "started_at": iso(self.started_at) if self.started_at else None,
            "finished_at": iso(self.finished_at) if self.finished_at else None,
            "summary": self.summary, "error": self.error, "files_created": list(self.files_created),
        }


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    strategy: str = ""
    subtasks: list[SubTask] = Field(default_factory=list)
    source: Literal["rules", "llm", "direct", "fallback"] = "rules"
    project_dir: str | None = None
    notes: list[str] = Field(default_factory=list)

    def get(self, subtask_id: str) -> SubTask:
        for s in self.subtasks:
            if s.id == subtask_id:
                return s
        raise KeyError(subtask_id)

    def by_key(self, key: str) -> SubTask | None:
        return next((s for s in self.subtasks if s.key == key), None)

    @property
    def agents(self) -> list[AgentId]:
        seen: list[AgentId] = []
        for s in self.subtasks:
            if s.agent not in seen:
                seen.append(s.agent)
        return seen

    def ready(self) -> list[SubTask]:
        """Pending subtasks whose dependencies are all finished successfully (or were optional)."""
        out = []
        for s in self.subtasks:
            if s.status != TaskStatus.PENDING:
                continue
            deps = [self.get(d) for d in s.depends_on]
            if all(d.status == TaskStatus.COMPLETED or (d.optional and d.finished) for d in deps):
                out.append(s)
        return sorted(out, key=lambda s: int(s.priority))

    def blocked(self) -> list[SubTask]:
        """Pending subtasks that can never run because a required dependency failed."""
        out = []
        for s in self.subtasks:
            if s.status != TaskStatus.PENDING:
                continue
            for d in (self.get(x) for x in s.depends_on):
                if d.status in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED) and not d.optional:
                    out.append(s)
                    break
        return out

    def progress(self) -> float:
        if not self.subtasks:
            return 0.0
        weights = [0.5 if s.optional else 1.0 for s in self.subtasks]
        done = sum(w * (1.0 if s.finished else s.progress) for s, w in zip(self.subtasks, weights, strict=True))
        return round(done / sum(weights), 3)

    def validate_dag(self) -> None:
        ids = {s.id for s in self.subtasks}
        if len(ids) != len(self.subtasks):
            raise PlanError("Duplicate subtask ids in the plan")
        for s in self.subtasks:
            unknown = [d for d in s.depends_on if d not in ids]
            if unknown:
                raise PlanError(f"Subtask '{s.title}' depends on unknown steps: {unknown}")
            if s.id in s.depends_on:
                raise PlanError(f"Subtask '{s.title}' depends on itself")
        # Kahn's algorithm: every subtask must be reachable without a cycle.
        indegree = {s.id: len(set(s.depends_on)) for s in self.subtasks}
        children: dict[str, list[str]] = {s.id: [] for s in self.subtasks}
        for s in self.subtasks:
            for d in set(s.depends_on):
                children[d].append(s.id)
        queue = [i for i, n in indegree.items() if n == 0]
        seen = 0
        while queue:
            node = queue.pop()
            seen += 1
            for child in children[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if seen != len(self.subtasks):
            raise PlanError("The plan has a dependency cycle")


# ------------------------------------------------------------------------------------------ LLM schema
AgentName = Literal["research", "system", "development", "creative", "planner"]
PriorityName = Literal["critical", "high", "normal", "low"]


class PlannedStep(BaseModel):
    key: str = Field(description="Short unique step id, e.g. 'research_market'")
    agent: AgentName = Field(description="The one agent that owns this step")
    title: str = Field(description="A few words, shown in the task view")
    instruction: str = Field(description="Complete, self-contained instructions for the agent")
    depends_on: list[str] = Field(description="Keys of steps that must finish first (their results are passed on)")
    acceptance_criteria: list[str] = Field(description="How to tell the step is done")
    expected_files: list[str] = Field(description="Files the step should create (full or workspace-relative paths)")
    optional: bool = Field(description="True when the task can still succeed without this step")
    priority: PriorityName


class PlanDraft(BaseModel):
    title: str = Field(description="Short task title in the user's language")
    strategy: str = Field(description="One or two sentences: how the work is split and why")
    steps: list[PlannedStep]


def plan_from_draft(draft: PlanDraft, *, max_steps: int = 12) -> TaskPlan:
    """Turn the model's draft into a validated TaskPlan (keys → ids, unknown deps dropped)."""
    steps = draft.steps[:max_steps]
    if not steps:
        raise PlanError("The plan has no steps")
    ids: dict[str, str] = {}
    subtasks: list[SubTask] = []
    for step in steps:
        key = step.key.strip() or f"step{len(ids) + 1}"
        while key in ids:
            key += "_2"
        sub = SubTask(key=key, agent=AgentId(step.agent), title=step.title.strip()[:120] or step.instruction[:60],
                      instruction=step.instruction.strip(), acceptance_criteria=step.acceptance_criteria[:8],
                      expected_files=step.expected_files[:20], optional=step.optional,
                      priority=Priority.parse(step.priority))
        ids[key] = sub.id
        subtasks.append(sub)
    for step, sub in zip(steps, subtasks, strict=True):
        sub.depends_on = [ids[d] for d in step.depends_on if d in ids and ids[d] != sub.id]
    plan = TaskPlan(title=draft.title.strip(), strategy=draft.strategy.strip(), subtasks=subtasks, source="llm")
    try:
        plan.validate_dag()
    except PlanError:
        # A cycle from the model: keep only dependencies on earlier steps.
        order = {s.id: i for i, s in enumerate(subtasks)}
        for s in subtasks:
            s.depends_on = [d for d in s.depends_on if order[d] < order[s.id]]
        plan.validate_dag()
    return plan
