"""Activity log: the human-readable timeline shown in the UI and written to `activity.log`.

    [17:32:10] JARVIS received task
    [17:32:11] Planner Agent assigned
    [17:32:12] Research Agent started
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.clock import local_hms, utcnow
from jarvis.common.ids import new_id
from jarvis.security.redaction import Redactor, default_redactor

log = logging.getLogger("jarvis.activity")

Level = Literal["debug", "info", "success", "warning", "error"]
Sink = Callable[["ActivityEntry"], Awaitable[None] | None]


class ActivityEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("act"))
    ts: datetime = Field(default_factory=utcnow)
    time: str = ""
    source: str = "JARVIS"
    message: str
    level: Level = "info"
    task_id: str | None = None
    agent_id: str | None = None
    kind: str = "activity"
    data: dict[str, Any] = Field(default_factory=dict)

    def line(self) -> str:
        return f"[{self.time}] {self.message}"


class ActivityLog:
    def __init__(self, log_dir: Path | None = None, *, capacity: int = 1500, redactor: Redactor | None = None) -> None:
        self._entries: deque[ActivityEntry] = deque(maxlen=capacity)
        self._sinks: list[Sink] = []
        self._redactor = redactor or default_redactor()
        self._file = (log_dir / "activity.log") if log_dir else None
        if self._file:
            self._file.parent.mkdir(parents=True, exist_ok=True)

    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    async def log(
        self,
        message: str,
        *,
        source: str = "JARVIS",
        level: Level = "info",
        task_id: str | None = None,
        agent_id: str | None = None,
        kind: str = "activity",
        **data: Any,
    ) -> ActivityEntry:
        entry = ActivityEntry(
            source=source,
            message=self._redactor.redact_text(message),
            level=level,
            task_id=task_id,
            agent_id=agent_id,
            kind=kind,
            data=self._redactor.redact(data) if data else {},
        )
        entry.time = local_hms(entry.ts)
        self._entries.append(entry)
        if self._file:
            try:
                with self._file.open("a", encoding="utf-8") as fh:
                    fh.write(entry.line() + "\n")
            except OSError:  # pragma: no cover
                pass
        log.log(logging.WARNING if level in ("warning", "error") else logging.INFO, entry.line())
        for sink in list(self._sinks):
            try:
                res = sink(entry)
                if inspect.isawaitable(res):
                    await asyncio.wait_for(res, timeout=5)
            except Exception:
                log.exception("Activity sink failed")
        return entry

    def log_nowait(self, message: str, **kwargs: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.log(message, **kwargs))

    def recent(self, limit: int = 200, *, task_id: str | None = None, agent_id: str | None = None) -> list[ActivityEntry]:
        items = [
            e for e in self._entries
            if (task_id is None or e.task_id == task_id) and (agent_id is None or e.agent_id == agent_id)
        ]
        return items[-limit:]

    def clear(self) -> int:
        """Forget every entry, in memory and in `activity.log`; how many were in memory."""
        count = len(self._entries)
        self._entries.clear()
        if self._file:
            with contextlib.suppress(OSError):
                self._file.write_text("", encoding="utf-8")
        return count
