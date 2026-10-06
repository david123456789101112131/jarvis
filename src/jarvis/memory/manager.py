"""MemoryManager: JARVIS's four memory layers on top of a replaceable store.

- Short-term: the current session's recent turns and the things just mentioned
  ("open it again", "stop that task") — kept in RAM.
- Long-term: preferences, facts about the user, decisions and notes — persisted.
- Project: known projects with their folder, description, decisions, files and tasks.
- Task: every task with its plan, progress, partial results and context, so an interrupted
  task can continue where it stopped; plus results, created files and command history.

Everything stored passes through the redactor first, so secrets never reach the database.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from jarvis.common.clock import utcnow
from jarvis.common.text import normalize
from jarvis.memory.models import (
    ChatRecord,
    CommandRecord,
    Fact,
    FileRecord,
    ProjectRecord,
    ResultRecord,
    SearchHit,
    TaskRecord,
)
from jarvis.memory.store import MemoryStore
from jarvis.security.redaction import Redactor, default_redactor

log = logging.getLogger(__name__)
T = TypeVar("T")

INTERRUPTIBLE = ("running", "planning", "queued", "waiting", "verifying", "pending")


def _file_size(p: Path) -> int | None:
    try:
        return p.stat().st_size if p.is_file() else None
    except OSError:
        return None


class ShortTermMemory:
    """Working memory for the current session."""

    def __init__(self, max_turns: int = 30) -> None:
        self.turns: deque[ChatRecord] = deque(maxlen=max_turns)
        self.entities: dict[str, Any] = {}

    def add(self, rec: ChatRecord) -> None:
        self.turns.append(rec)

    def recent(self, n: int | None = None) -> list[ChatRecord]:
        items = list(self.turns)
        return items if n is None else items[-n:]

    def note(self, key: str, value: Any) -> None:
        """Remember what was just referred to: last_app, last_folder, last_task, last_file, last_url, ..."""
        self.entities[key] = value
        self.entities[f"{key}_at"] = utcnow().isoformat()

    def get(self, key: str, default: Any = None) -> Any:
        return self.entities.get(key, default)

    def clear(self) -> None:
        self.turns.clear()
        self.entities.clear()


class Recall(BaseModel):
    """What memory knows that is relevant to a request; formatted into prompts."""

    preferences: list[Fact] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    hits: list[SearchHit] = Field(default_factory=list)
    projects: list[ProjectRecord] = Field(default_factory=list)
    recent: list[ChatRecord] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.preferences or self.facts or self.hits or self.projects)

    def format(self, max_chars: int = 3000) -> str:
        lines: list[str] = []
        if self.preferences:
            lines.append("User preferences:")
            lines += [f"- {f.text}" for f in self.preferences]
        if self.facts:
            lines.append("Known facts and decisions:")
            lines += [f"- [{f.category}] {f.text}" for f in self.facts]
        if self.projects:
            lines.append("Known projects:")
            lines += [f"- {p.name}" + (f" ({p.path})" if p.path else "") + (f": {p.description}" if p.description else "")
                      for p in self.projects]
        related = [h for h in self.hits if h.layer in ("task", "conversation")]
        if related:
            lines.append("Related earlier work:")
            lines += [f"- ({h.layer}) {h.text[:220]}" for h in related]
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


class MemoryManager:
    def __init__(self, store: MemoryStore, *, short_term_turns: int = 30, recall_limit: int = 8,
                 redactor: Redactor | None = None) -> None:
        self.store = store
        self.short = ShortTermMemory(short_term_turns)
        self.recall_limit = recall_limit
        self.redactor = redactor or default_redactor()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-memory")
        self._visible_since: datetime | None = None
        self._started = False
        self._listeners: list[Callable[[str, Any], None]] = []

    # -- lifecycle -----------------------------------------------------------------------------
    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args, **kwargs))

    async def start(self) -> None:
        if self._started:
            return
        await self._run(self.store.open)
        for rec in await self._run(self.store.recent_chat, self.short.turns.maxlen or 30):
            self.short.add(rec)
        self._started = True

    async def close(self) -> None:
        if self._started:
            await self._run(self.store.close)
            self._started = False
        self._executor.shutdown(wait=False)

    def add_listener(self, fn: Callable[[str, Any], None]) -> None:
        """`fn(kind, record)` after facts/projects/files change (the server pushes UI updates)."""
        self._listeners.append(fn)

    def _changed(self, kind: str, record: Any) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, record)
            except Exception:  # pragma: no cover
                log.exception("memory listener failed")

    def _clean(self, text: str) -> str:
        return self.redactor.redact_text(text or "")

    # -- conversation --------------------------------------------------------------------------
    async def add_chat(self, rec: ChatRecord) -> ChatRecord:
        rec.text = self._clean(rec.text)
        self.short.add(rec)
        await self._run(self.store.add_chat, rec)
        return rec

    async def chat_history(self, limit: int = 100) -> list[ChatRecord]:
        records = await self._run(self.store.recent_chat, limit)
        if self._visible_since is not None:
            records = [r for r in records if r.ts >= self._visible_since]
        return records

    def clear_visible_chat(self) -> None:
        """Hide the conversation in the UI; memory keeps it."""
        self._visible_since = utcnow()
        self.short.turns.clear()

    # -- long-term -----------------------------------------------------------------------------
    async def remember(self, text: str, category: str = "fact", *, source: str = "user", importance: int = 1,
                       task_id: str | None = None, project_id: str | None = None) -> Fact:
        text = self._clean(text).strip()
        if not text:
            raise ValueError("Nothing to remember")
        key = normalize(text)
        existing = [f for f in await self._run(self.store.list_facts, category=category, limit=500)
                    if normalize(f.text) == key]
        if existing:
            fact = existing[0]
            fact.importance = max(fact.importance, importance)
            fact.project_id = project_id or fact.project_id
            fact.task_id = task_id or fact.task_id
        else:
            fact = Fact(text=text, category=category, source=source, importance=importance,  # type: ignore[arg-type]
                        task_id=task_id, project_id=project_id)
        fact = await self._run(self.store.upsert_fact, fact)
        self._changed("fact", fact)
        return fact

    async def record_decision(self, text: str, *, task_id: str | None = None, project_id: str | None = None,
                              source: str = "jarvis") -> Fact:
        return await self.remember(text, "decision", source=source, task_id=task_id, project_id=project_id)

    async def forget(self, fact_id: str) -> bool:
        ok = await self._run(self.store.delete_fact, fact_id)
        if ok:
            self._changed("fact_deleted", fact_id)
        return ok

    async def forget_matching(self, text: str) -> int:
        """Delete long-term facts containing `text` ("forget that I like jazz")."""
        needle = normalize(text)
        if len(needle) < 2:
            return 0
        count = 0
        for f in await self._run(self.store.list_facts, limit=1000):
            if needle in normalize(f.text):
                count += int(await self.forget(f.id))
        return count

    async def facts(self, *, category: str | None = None, query: str | None = None, limit: int = 100,
                    project_id: str | None = None) -> list[Fact]:
        return await self._run(self.store.list_facts, category=category, query=query, limit=limit,
                               project_id=project_id)

    async def preferences(self, limit: int = 20) -> list[Fact]:
        return await self.facts(category="preference", limit=limit)

    # -- projects ------------------------------------------------------------------------------
    async def upsert_project(self, name: str, *, path: str | Path | None = None, description: str = "",
                             status: str | None = None, meta: dict[str, Any] | None = None) -> ProjectRecord:
        path_s = str(path) if path else None
        existing = None
        if path_s:
            existing = await self._run(self.store.find_project, path_s)
        if existing is None:
            existing = await self._run(self.store.find_project, name)
            if existing is not None and path_s and existing.path and existing.path.lower() != path_s.lower():
                existing = None  # same name, different folder: a different project
        if existing is None:
            project = ProjectRecord(name=name, path=path_s, description=self._clean(description),
                                    status=status or "active", meta=meta or {})
        else:
            project = existing
            project.name = name or project.name
            project.path = path_s or project.path
            if description:
                project.description = self._clean(description)
            if status:
                project.status = status
            if meta:
                project.meta = {**project.meta, **meta}
        project = await self._run(self.store.upsert_project, project)
        self.short.note("last_project", project.id)
        self._changed("project", project)
        return project

    async def project(self, name_or_id: str) -> ProjectRecord | None:
        found = await self._run(self.store.get_project, name_or_id)
        return found or await self._run(self.store.find_project, name_or_id)

    async def projects(self, limit: int = 100) -> list[ProjectRecord]:
        return await self._run(self.store.list_projects, limit)

    async def project_context(self, project_id: str) -> dict[str, Any]:
        project = await self._run(self.store.get_project, project_id)
        if project is None:
            return {}
        facts = await self.facts(project_id=project_id, limit=30)
        files = await self._run(self.store.list_files, limit=30, project_id=project_id)
        tasks = [t for t in await self._run(self.store.list_tasks, limit=200) if t.project_id == project_id][:10]
        return {
            "project": project.model_dump(mode="json"),
            "decisions": [f.text for f in facts if f.category == "decision"],
            "notes": [f.text for f in facts if f.category != "decision"],
            "files": [f.path for f in files],
            "tasks": [{"id": t.id, "title": t.title, "status": t.status, "summary": t.summary} for t in tasks],
        }

    # -- tasks ---------------------------------------------------------------------------------
    async def save_task(self, task: TaskRecord) -> None:
        task.updated_at = utcnow()
        task.goal = self._clean(task.goal)
        task.data = self.redactor.redact(task.data)
        await self._run(self.store.save_task, task)
        self.short.note("last_task", task.id)

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return await self._run(self.store.get_task, task_id)

    async def tasks(self, *, statuses: list[str] | None = None, limit: int = 50) -> list[TaskRecord]:
        return await self._run(self.store.list_tasks, statuses=statuses, limit=limit)

    async def interrupted_tasks(self) -> list[TaskRecord]:
        """Tasks that were in progress when JARVIS stopped (crash, shutdown, power loss)."""
        return await self.tasks(statuses=[*INTERRUPTIBLE, "interrupted"], limit=100)

    async def set_task_context(self, task_id: str, key: str, value: Any) -> None:
        await self._run(self.store.set_context, task_id, key, self.redactor.redact(value))

    async def task_context(self, task_id: str) -> dict[str, Any]:
        return await self._run(self.store.get_context, task_id)

    async def add_result(self, result: ResultRecord) -> ResultRecord:
        result.summary = self._clean(result.summary)
        result.data = self.redactor.redact(result.data)
        await self._run(self.store.add_result, result)
        return result

    async def results(self, task_id: str) -> list[ResultRecord]:
        return await self._run(self.store.list_results, task_id)

    # -- files ---------------------------------------------------------------------------------
    async def record_file(self, path: str | Path, *, task_id: str | None = None, agent_id: str | None = None,
                          project_id: str | None = None, kind: str = "created", important: bool = False) -> FileRecord:
        p = Path(path)
        size = await self._run(_file_size, p)
        rec = FileRecord(path=str(p), name=p.name, size=size, kind=kind, task_id=task_id, agent_id=agent_id,
                         project_id=project_id, important=important)
        await self._run(self.store.add_file, rec)
        self.short.note("last_file", str(p))
        self._changed("file", rec)
        return rec

    async def recent_files(self, limit: int = 30, *, task_id: str | None = None,
                           project_id: str | None = None) -> list[FileRecord]:
        return await self._run(self.store.list_files, limit=limit, task_id=task_id, project_id=project_id)

    # -- command history -----------------------------------------------------------------------
    async def add_command(self, text: str, *, source: str = "text", intent: str | None = None) -> CommandRecord:
        rec = CommandRecord(text=self._clean(text), source=source, intent=intent)
        await self._run(self.store.add_command, rec)
        return rec

    async def update_command(self, command_id: str, *, ok: bool | None = None, task_id: str | None = None,
                             intent: str | None = None) -> None:
        await self._run(self.store.update_command, command_id, ok=ok, task_id=task_id, intent=intent)

    async def command_history(self, limit: int = 50) -> list[CommandRecord]:
        return await self._run(self.store.list_commands, limit)

    # -- search & recall -----------------------------------------------------------------------
    async def search(self, query: str, *, limit: int = 20, layers: list[str] | None = None) -> list[SearchHit]:
        return await self._run(self.store.search, query, limit=limit, layers=layers)

    async def recall(self, query: str, *, limit: int | None = None, project_id: str | None = None) -> Recall:
        limit = limit or self.recall_limit
        prefs = await self.preferences(limit=10)
        hits = await self.search(query, limit=limit * 2, layers=["long_term", "project", "task", "conversation"])
        fact_ids = {h.id for h in hits if h.layer == "long_term"}
        facts = [f for f in await self.facts(limit=500) if f.id in fact_ids and f.category != "preference"]
        if project_id:
            facts += [f for f in await self.facts(project_id=project_id, limit=20) if f.id not in fact_ids]
        project_ids = {h.id for h in hits if h.layer == "project"}
        if project_id:
            project_ids.add(project_id)
        projects = [p for p in await self.projects(limit=200) if p.id in project_ids]
        return Recall(
            preferences=prefs,
            facts=facts[:limit],
            hits=[h for h in hits if h.layer in ("task", "conversation")][:limit],
            projects=projects[:limit],
            recent=self.short.recent(6),
            entities=dict(self.short.entities),
        )

    async def stats(self) -> dict[str, int]:
        return await self._run(self.store.stats)
