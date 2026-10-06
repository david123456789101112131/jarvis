"""Memory tools: let agents store and look up facts, decisions and project context."""

from __future__ import annotations

from typing import Literal

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


MEMORY_TOOLS: tuple[type[Tool], ...] = (RememberTool, RecallTool, ProjectInfoTool)
