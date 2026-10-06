"""Kill switches: one global, one per agent.

Engaging a switch notifies listeners (Agent Manager cancels running work and kills child
processes; Orchestrator cancels tasks) and makes the tool layer refuse every call for the
affected scope until the switch is released.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from jarvis.common.clock import utcnow
from jarvis.common.errors import KillSwitchEngaged

log = logging.getLogger(__name__)

Listener = Callable[["KillEvent"], Awaitable[None] | None]


@dataclass
class KillEvent:
    engaged: bool
    agent_id: str | None  # None = global
    reason: str
    at: datetime = field(default_factory=utcnow)


class KillSwitch:
    def __init__(self) -> None:
        self._global = False
        self._global_reason = ""
        self._agents: dict[str, str] = {}
        self._listeners: list[Listener] = []
        self.history: list[KillEvent] = []

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    @property
    def global_engaged(self) -> bool:
        return self._global

    def engaged_agents(self) -> dict[str, str]:
        return dict(self._agents)

    def is_engaged(self, agent_id: str | None = None) -> bool:
        return self._global or (agent_id is not None and agent_id in self._agents)

    def check(self, agent_id: str | None = None) -> None:
        if self._global:
            raise KillSwitchEngaged(f"Global kill switch engaged: {self._global_reason}")
        if agent_id is not None and agent_id in self._agents:
            raise KillSwitchEngaged(f"Kill switch engaged for agent {agent_id}: {self._agents[agent_id]}")

    async def engage(self, reason: str = "user request", agent_id: str | None = None) -> KillEvent:
        if agent_id is None:
            self._global = True
            self._global_reason = reason
        else:
            self._agents[agent_id] = reason
        evt = KillEvent(engaged=True, agent_id=agent_id, reason=reason)
        self.history.append(evt)
        log.warning("Kill switch ENGAGED (%s): %s", agent_id or "global", reason)
        await self._notify(evt)
        return evt

    async def release(self, agent_id: str | None = None, reason: str = "user request") -> KillEvent:
        if agent_id is None:
            self._global = False
            self._global_reason = ""
        else:
            self._agents.pop(agent_id, None)
        evt = KillEvent(engaged=False, agent_id=agent_id, reason=reason)
        self.history.append(evt)
        log.warning("Kill switch released (%s)", agent_id or "global")
        await self._notify(evt)
        return evt

    async def _notify(self, evt: KillEvent) -> None:
        for listener in list(self._listeners):
            try:
                res = listener(evt)
                if inspect.isawaitable(res):
                    await asyncio.wait_for(res, timeout=15)
            except Exception:
                log.exception("Kill switch listener failed")

    def snapshot(self) -> dict:
        return {"global": self._global, "reason": self._global_reason, "agents": dict(self._agents)}
