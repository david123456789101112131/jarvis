"""Tool base class, the per-call context, and results.

A tool is the only way an agent can touch the machine or the network. A tool declares its
capabilities and risk, describes what a call will do in `plan()` (paths, data leaving the
machine, how many items are affected), and performs it in `run()`. The executor decides
between those two steps whether the call is allowed, needs confirmation, or is refused.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from jarvis.common.cancel import CancelToken
from jarvis.common.enums import Capability, Mode, RiskLevel
from jarvis.common.errors import ToolError
from jarvis.permissions.models import PathUse

if TYPE_CHECKING:
    from jarvis.tools.services import ToolServices

ProgressFn = Callable[[float, str], Awaitable[None] | None]


class ToolResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool = True
    summary: str = ""
    data: Any = None
    files_created: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    files_deleted: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    error: str | None = None
    denied: str | None = None          # "permission" | "confirmation" | "mode" | "blocked" when refused
    tool: str = ""
    duration_ms: int = 0

    @classmethod
    def fail(cls, error: str, **kw: Any) -> ToolResult:
        return cls(ok=False, error=error, summary=kw.pop("summary", error), **kw)

    def raise_for_error(self) -> ToolResult:
        if not self.ok:
            raise ToolError(self.error or self.summary or f"{self.tool} failed", details={"tool": self.tool})
        return self

    def for_model(self, limit: int = 12000) -> str:
        """Compact text form for an LLM tool_result block."""
        import json

        payload: dict[str, Any] = {"ok": self.ok, "summary": self.summary}
        if self.error:
            payload["error"] = self.error
        if self.denied:
            payload["denied"] = self.denied
        if self.data is not None:
            payload["data"] = self.data
        for key in ("files_created", "files_modified", "files_deleted"):
            if getattr(self, key):
                payload[key] = getattr(self, key)
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return text if len(text) <= limit else text[: limit - 40] + '…", "truncated": true}'


class ActionPlan(BaseModel):
    """What one call is about to do; produced before running so it can be judged."""

    summary: str
    capabilities: list[Capability] | None = None
    paths: list[PathUse] = Field(default_factory=list)
    risk: RiskLevel | None = None
    risk_reasons: list[str] = Field(default_factory=list)
    outbound: str | None = None
    destination: str | None = None
    affected_count: int | None = None
    always_confirm: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ToolContext:
    agent_id: str
    task_id: str | None
    subtask_id: str | None
    mode: Mode
    cancel: CancelToken
    services: ToolServices
    cwd: Path | None = None
    progress_fn: ProgressFn | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def check_cancelled(self) -> None:
        self.cancel.raise_if_cancelled()

    async def progress(self, fraction: float, message: str = "") -> None:
        if self.progress_fn is None:
            return
        res = self.progress_fn(max(0.0, min(1.0, fraction)), message)
        if inspect.isawaitable(res):
            await res

    def resolve(self, path: str | Path) -> Path:
        """Resolve a user/agent supplied path: special folder keys, ~, env vars, relative to cwd/workspace."""
        from jarvis.security.path_guard import normalize_path

        text = str(path).strip().strip('"')
        special = self.services.special_folder(text)
        if special is not None:
            return special
        base = self.cwd or self.services.paths.workspace
        return normalize_path(text, base=base)


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    capabilities: ClassVar[tuple[Capability, ...]]
    Params: ClassVar[type[BaseModel]]
    risk: ClassVar[RiskLevel] = RiskLevel.LOW
    always_confirm: ClassVar[bool] = False
    read_only: ClassVar[bool] = False
    timeout_s: ClassVar[float | None] = 120.0

    async def plan(self, params: Any, ctx: ToolContext) -> ActionPlan:
        return ActionPlan(summary=f"{self.name}")

    @abstractmethod
    async def run(self, params: Any, ctx: ToolContext) -> ToolResult: ...

    @classmethod
    def schema(cls) -> dict[str, Any]:
        """Provider-neutral tool definition (name, description, JSON schema of the input)."""
        schema = cls.Params.model_json_schema()
        schema.pop("title", None)
        _strip_titles(schema)
        return {"name": cls.name, "description": cls.description, "input_schema": schema}


def _strip_titles(node: Any) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("title"), str):  # schema annotation, not a property called "title"
            node.pop("title")
        for v in node.values():
            _strip_titles(v)
    elif isinstance(node, list):
        for v in node:
            _strip_titles(v)
