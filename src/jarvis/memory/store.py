"""The storage interface behind JARVIS's memory.

`MemoryManager` only talks to this interface, so the database can be replaced (another SQL
engine, a vector store) by implementing `MemoryStore`. Methods are synchronous; the manager
runs them on a dedicated worker thread.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

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


class MemoryStore(ABC):
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    # conversation
    @abstractmethod
    def add_chat(self, rec: ChatRecord) -> None: ...

    @abstractmethod
    def recent_chat(self, limit: int, *, before: str | None = None) -> list[ChatRecord]: ...

    # long-term facts / preferences / decisions
    @abstractmethod
    def upsert_fact(self, fact: Fact) -> Fact: ...

    @abstractmethod
    def delete_fact(self, fact_id: str) -> bool: ...

    @abstractmethod
    def list_facts(self, *, category: str | None = None, query: str | None = None, limit: int = 100,
                   project_id: str | None = None) -> list[Fact]: ...

    # projects
    @abstractmethod
    def upsert_project(self, project: ProjectRecord) -> ProjectRecord: ...

    @abstractmethod
    def get_project(self, project_id: str) -> ProjectRecord | None: ...

    @abstractmethod
    def find_project(self, name_or_path: str) -> ProjectRecord | None: ...

    @abstractmethod
    def list_projects(self, limit: int = 100) -> list[ProjectRecord]: ...

    # tasks
    @abstractmethod
    def save_task(self, task: TaskRecord) -> None: ...

    @abstractmethod
    def get_task(self, task_id: str) -> TaskRecord | None: ...

    @abstractmethod
    def list_tasks(self, *, statuses: list[str] | None = None, limit: int = 50) -> list[TaskRecord]: ...

    @abstractmethod
    def set_context(self, task_id: str, key: str, value: Any) -> None: ...

    @abstractmethod
    def get_context(self, task_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def add_result(self, result: ResultRecord) -> None: ...

    @abstractmethod
    def list_results(self, task_id: str) -> list[ResultRecord]: ...

    # files
    @abstractmethod
    def add_file(self, rec: FileRecord) -> None: ...

    @abstractmethod
    def list_files(self, *, limit: int = 30, task_id: str | None = None, project_id: str | None = None,
                   important: bool | None = None) -> list[FileRecord]: ...

    # command history
    @abstractmethod
    def add_command(self, rec: CommandRecord) -> None: ...

    @abstractmethod
    def update_command(self, command_id: str, *, ok: bool | None = None, task_id: str | None = None,
                       intent: str | None = None) -> None: ...

    @abstractmethod
    def list_commands(self, limit: int = 50) -> list[CommandRecord]: ...

    # search
    @abstractmethod
    def search(self, query: str, *, limit: int = 20, layers: list[str] | None = None) -> list[SearchHit]: ...

    @abstractmethod
    def stats(self) -> dict[str, int]: ...
