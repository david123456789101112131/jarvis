"""Memory tools: let agents store and look up facts, decisions and project context."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.tools.base import Tool, ToolContext, ToolResult

C = Capability


class RememberParams(BaseModel):
    text: str = Field(..., description="One self-contained fact, preference or decision")
    category: Literal["preference", "fact", "decision", "person", "note", "project"] = "fact"
    project: str = Field("", description="Project name this belongs to, if any")


class RememberTool(Tool):
    name = "remember"
    description = "Save a fact, user preference or decision to JARVIS's long-term memory."
    capabilities = (C.MEMORY_WRITE,)
    Params = RememberParams
    risk = RiskLevel.SAFE

    async def run(self, params: RememberParams, ctx: ToolContext) -> ToolResult:
        memory = ctx.services.memory
        if memory is None:
            return ToolResult.fail("Memory is not available.")
        project_id = None
        if params.project:
            project = await memory.project(params.project)
            project_id = project.id if project else None
        fact = await memory.remember(params.text, params.category, source=ctx.agent_id, task_id=ctx.task_id,
                                     project_id=project_id)
        return ToolResult(summary=f"Remembered: {fact.text}", data={"id": fact.id})


class RecallParams(BaseModel):
    query: str
    limit: int = Field(8, ge=1, le=30)


class RecallTool(Tool):
    name = "recall"
    description = "Search JARVIS's memory (preferences, facts, decisions, projects, earlier tasks and conversations)."
    capabilities = (C.MEMORY_READ,)
    Params = RecallParams
    read_only = True
    risk = RiskLevel.SAFE

    async def run(self, params: RecallParams, ctx: ToolContext) -> ToolResult:
        memory = ctx.services.memory
        if memory is None:
            return ToolResult.fail("Memory is not available.")
        recall = await memory.recall(params.query, limit=params.limit)
        text = recall.format()
        return ToolResult(summary=text or "Nothing relevant in memory.", data={
            "preferences": [f.text for f in recall.preferences],
            "facts": [{"text": f.text, "category": f.category} for f in recall.facts],
            "projects": [{"name": p.name, "path": p.path, "description": p.description} for p in recall.projects],
            "related": [{"layer": h.layer, "text": h.text[:400]} for h in recall.hits],
        })


class ProjectInfoParams(BaseModel):
    name: str = Field(..., description="Project name or folder")


class ProjectInfoTool(Tool):
    name = "project_info"
    description = "What JARVIS knows about a project: folder, description, decisions, files and earlier tasks."
    capabilities = (C.MEMORY_READ,)
    Params = ProjectInfoParams
    read_only = True
    risk = RiskLevel.SAFE

    async def run(self, params: ProjectInfoParams, ctx: ToolContext) -> ToolResult:
        memory = ctx.services.memory
        if memory is None:
            return ToolResult.fail("Memory is not available.")
        project = await memory.project(params.name)
        if project is None:
            return ToolResult.fail(f"No project called '{params.name}' in memory.")
        data = await memory.project_context(project.id)
        return ToolResult(summary=f"Project {project.name}" + (f" at {project.path}" if project.path else ""), data=data)


class TaskHistoryParams(BaseModel):
    status: list[Literal["pending", "planning", "queued", "running", "waiting", "verifying", "completed", "failed",
                         "cancelled", "interrupted"]] = Field(default_factory=list,
                                                              description="Only tasks in these states")
    query: str = Field("", description="Only tasks whose title or goal contains this text")
    limit: int = Field(20, ge=1, le=100)


class TaskHistoryTool(Tool):
    name = "task_history"
    description = ("JARVIS's recent tasks with their state, progress, summary, errors and the steps that are still "
                   "open or blocked (for progress reviews).")
    capabilities = (C.MEMORY_READ,)
    Params = TaskHistoryParams
    read_only = True
    risk = RiskLevel.SAFE

    async def run(self, params: TaskHistoryParams, ctx: ToolContext) -> ToolResult:
        memory = ctx.services.memory
        if memory is None:
            return ToolResult.fail("Memory is not available.")
        records = await memory.tasks(statuses=list(params.status) or None, limit=max(params.limit * 3, 30))
        needle = params.query.strip().lower()
        items = []
        for rec in records:
            if needle and needle not in f"{rec.title} {rec.goal}".lower():
                continue
            items.append(task_overview(rec))
            if len(items) >= params.limit:
                break
        counts: dict[str, int] = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        summary = f"{len(items)} task(s): " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) if items \
            else "No matching tasks."
        return ToolResult(summary=summary, data={"tasks": items, "counts": counts})


def task_overview(rec: Any) -> dict[str, Any]:
    """A compact view of a stored task: state, timing and its plan steps."""
    plan = (rec.data or {}).get("plan") or {}
    steps = []
    for sub in plan.get("subtasks") or []:
        steps.append({"title": sub.get("title"), "agent": sub.get("agent"), "status": sub.get("status"),
                      "error": (sub.get("error") or None) and str(sub.get("error"))[:300],
                      "optional": bool(sub.get("optional"))})
    return {
        "id": rec.id, "title": rec.title or rec.goal[:80], "goal": rec.goal[:300], "status": rec.status,
        "created_at": rec.created_at.isoformat(timespec="seconds") if rec.created_at else None,
        "finished_at": rec.finished_at.isoformat(timespec="seconds") if rec.finished_at else None,
        "summary": (rec.summary or "")[:500] or None, "error": (rec.error or "")[:300] or None,
        "progress": (rec.data or {}).get("progress"), "steps": steps,
    }


MEMORY_TOOLS: tuple[type[Tool], ...] = (RememberTool, RecallTool, ProjectInfoTool, TaskHistoryTool)
