"""Registry of every tool; agents see only the tools their permissions allow."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from jarvis.common.errors import ToolNotFound
from jarvis.tools.base import Tool

if TYPE_CHECKING:
    from jarvis.permissions.manager import PermissionManager


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.register(t)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFound(f"Unknown tool: {name}", details={"tool": name}) from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in self.names()]

    def for_agent(self, agent_id: str, permissions: PermissionManager, task_id: str | None = None) -> list[Tool]:
        caps = set(permissions.capabilities(agent_id, task_id))
        return [t for t in self.all() if all(c.value in caps for c in t.capabilities)]

    def schemas(self, tools: Iterable[Tool]) -> list[dict[str, Any]]:
        return [t.schema() for t in tools]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "capabilities": [c.value for c in t.capabilities],
                "risk": t.risk.name.lower(),
                "always_confirm": t.always_confirm,
            }
            for t in self.all()
        ]
