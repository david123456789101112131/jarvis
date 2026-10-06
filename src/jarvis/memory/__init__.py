"""Local memory: short-term, long-term, project and task layers over a replaceable store."""

from pathlib import Path

from jarvis.memory.manager import MemoryManager, Recall, ShortTermMemory
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
from jarvis.memory.sqlite_store import SQLiteMemoryStore
from jarvis.memory.store import MemoryStore


def create_memory(db_path: Path | str, *, backend: str = "sqlite", short_term_turns: int = 30,
                  recall_limit: int = 8, redactor=None) -> MemoryManager:
    """Build the memory manager for the configured backend."""
    if backend != "sqlite":
        raise ValueError(f"Unknown memory backend: {backend}")
    return MemoryManager(SQLiteMemoryStore(db_path), short_term_turns=short_term_turns, recall_limit=recall_limit,
                         redactor=redactor)


__all__ = [
    "ChatRecord",
    "CommandRecord",
    "Fact",
    "FileRecord",
    "MemoryManager",
    "MemoryStore",
    "ProjectRecord",
    "Recall",
    "ResultRecord",
    "SQLiteMemoryStore",
    "SearchHit",
    "ShortTermMemory",
    "TaskRecord",
    "create_memory",
]
