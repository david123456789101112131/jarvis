"""Audit log: one record for every tool action and permission decision (append-only JSONL)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.clock import utcnow
from jarvis.common.ids import new_id
from jarvis.security.redaction import Redactor, default_redactor

log = logging.getLogger("jarvis.audit")

Decision = Literal["allowed", "denied", "confirmed", "rejected", "blocked", "error"]
Sink = Callable[["AuditEntry"], Awaitable[None] | None]
MAX_PARAM_CHARS = 300      # a file's content or a long text is summarised, not copied into the log


def shorten(value: Any, limit: int = MAX_PARAM_CHARS) -> Any:
    """Long strings in tool parameters become their start plus a length note (recursively)."""
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]}… [{len(value)} chars]"
    if isinstance(value, dict):
        return {k: shorten(v, limit) for k, v in value.items()}
    if isinstance(value, list | tuple):
        items = [shorten(v, limit) for v in list(value)[:50]]
        return items + ([f"… [{len(value)} items]"] if len(value) > 50 else [])
    return value


class AuditEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("aud"))
    ts: datetime = Field(default_factory=utcnow)
    agent_id: str | None = None
    task_id: str | None = None
    tool: str = ""
    capability: str = ""
    action: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    risk: str = "safe"
    decision: Decision = "allowed"
    outcome: Literal["pending", "ok", "error", "skipped"] = "pending"
    reason: str = ""
    error: str | None = None
    duration_ms: int | None = None


class AuditLog:
    def __init__(self, log_dir: Path | None = None, *, capacity: int = 3000, redactor: Redactor | None = None) -> None:
        self._entries: deque[AuditEntry] = deque(maxlen=capacity)
        self._sinks: list[Sink] = []
        self._redactor = redactor or default_redactor()
        self._file = (log_dir / "audit.jsonl") if log_dir else None
        if self._file:
            self._file.parent.mkdir(parents=True, exist_ok=True)

    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    async def record(self, entry: AuditEntry) -> AuditEntry:
        entry.params = shorten(self._redactor.redact(entry.params))
        entry.action = self._redactor.redact_text(entry.action)
        if entry.error:
            entry.error = self._redactor.redact_text(entry.error)
        self._entries.append(entry)
        if self._file:
            try:
                with self._file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
            except OSError:  # pragma: no cover
                pass
        for sink in list(self._sinks):
            try:
                res = sink(entry)
                if inspect.isawaitable(res):
                    await asyncio.wait_for(res, timeout=5)
            except Exception:
                log.exception("Audit sink failed")
        return entry

    def recent(self, limit: int = 300, *, agent_id: str | None = None, task_id: str | None = None) -> list[AuditEntry]:
        items = [
            e for e in self._entries
            if (agent_id is None or e.agent_id == agent_id) and (task_id is None or e.task_id == task_id)
        ]
        return items[-limit:]

    def clear(self) -> int:
        """Forget every entry, in memory and in `audit.jsonl`; how many were in memory."""
        count = len(self._entries)
        self._entries.clear()
        if self._file:
            with contextlib.suppress(OSError):
                self._file.write_text("", encoding="utf-8")
        return count
