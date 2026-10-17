"""Which agent the current model call works for.

The agent manager runs each assignment inside `agent_scope(agent_id)`, so every model call made
while an agent works (its tool loop, a skill's structured draft, a nested helper) is routed with
that agent's provider preference (`ai.agent_providers`). Calls made by JARVIS itself (chat,
intent, planning, verification, the final answer) run outside any scope and use the role order.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

_CURRENT_AGENT: ContextVar[str | None] = ContextVar("jarvis_current_agent", default=None)


def current_agent() -> str | None:
    return _CURRENT_AGENT.get()


@contextlib.contextmanager
def agent_scope(agent_id: str | None) -> Iterator[None]:
    """Model calls in this block, and in tasks created inside it, belong to `agent_id`."""
    token = _CURRENT_AGENT.set(agent_id)
    try:
        yield
    finally:
        _CURRENT_AGENT.reset(token)
