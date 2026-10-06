"""Records kept in JARVIS's local memory."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.clock import local_hms, utcnow
from jarvis.common.ids import new_id

FactCategory = Literal["preference", "fact", "decision", "person", "note", "project"]
Layer = Literal["short_term", "long_term", "project", "task", "conversation", "file", "command"]


class ChatRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    role: Literal["user", "jarvis", "system"]
    text: str
    ts: datetime = Field(default_factory=utcnow)
    source: str = "text"
    language: str = "he"
    kind: str = "answer"
    task_id: str | None = None
    intent: str | None = None

    def view(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["time"] = local_hms(self.ts)
        return data


class Fact(BaseModel):
    """Long-term memory: preferences, facts about the user, decisions, notes."""

    id: str = Field(default_factory=lambda: new_id("fact"))
    text: str
    category: FactCategory = "fact"
    source: str = "user"
    importance: int = 1
    task_id: str | None = None
    project_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProjectRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("proj"))
    name: str
    path: str | None = None
    description: str = ""
    status: str = "active"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    meta: dict[str, Any] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    """A task as stored for history and for resuming after an interruption.

    `data` holds the orchestrator's full serialized task (plan, subtask states, partial
    results, agent conversations) so an interrupted task continues where it stopped.
    """

    id: str
    title: str = ""
    goal: str = ""
    status: str = "pending"
    priority: str = "normal"
    mode: str = "normal"
    source: str = "text"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: str | None = None
    error: str | None = None
    project_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ResultRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("res"))
    task_id: str
    subtask_id: str | None = None
    agent_id: str | None = None
    status: str = "success"
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class FileRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("file"))
    path: str
    name: str = ""
    size: int | None = None
    kind: str = "created"
    task_id: str | None = None
    agent_id: str | None = None
    project_id: str | None = None
    important: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class CommandRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("cmd"))
    text: str
    source: str = "text"
    intent: str | None = None
    task_id: str | None = None
    ok: bool | None = None
    ts: datetime = Field(default_factory=utcnow)


class SearchHit(BaseModel):
    layer: str
    id: str
    text: str
    score: float
    ts: datetime | None = None
    ref: str | None = None
