"""The five JARVIS agents and the manager that runs them.

The roster is fixed: Research, System, Development, Creative and Planner. Each agent works only
through tools (and so through the permission gate) and talks to the rest of JARVIS only through
bus messages handled by the AgentManager.
"""

from __future__ import annotations

import importlib
from typing import Any

from jarvis.agents.base import (
    AGENT_INFO,
    AGENT_NUMBER,
    AgentResult,
    AgentRuntime,
    AgentTask,
    BaseAgent,
    RunContext,
    agent_address,
    skill,
)

_LAZY = {
    "ResearchAgent": "jarvis.agents.research",
    "SystemAgent": "jarvis.agents.system",
    "DevelopmentAgent": "jarvis.agents.development",
    "CreativeAgent": "jarvis.agents.creative",
    "PlannerAgent": "jarvis.agents.planner",
    "AgentManager": "jarvis.agents.manager",
}


def agent_classes() -> list[type[BaseAgent]]:
    """The fixed roster, in agent-number order (1 Research … 5 Planner)."""
    classes = [getattr(importlib.import_module(_LAZY[name]), name)
               for name in ("ResearchAgent", "SystemAgent", "DevelopmentAgent", "CreativeAgent", "PlannerAgent")]
    return sorted(classes, key=lambda cls: AGENT_NUMBER[cls.id])


def create_agents(runtime: AgentRuntime) -> list[BaseAgent]:
    """One instance of each of the five agents, sharing the runtime services."""
    return [cls(runtime) for cls in agent_classes()]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module 'jarvis.agents' has no attribute {name!r}")


__all__ = [
    "AGENT_INFO", "AgentManager", "AgentResult", "AgentRuntime", "AgentTask", "BaseAgent", "CreativeAgent",
    "DevelopmentAgent", "PlannerAgent", "ResearchAgent", "RunContext", "SystemAgent", "agent_address",
    "agent_classes", "create_agents", "skill",
]
