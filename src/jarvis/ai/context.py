"""Which agent the current model call works for, and which cloud AI the user allowed for it.

The agent manager runs each assignment inside `agent_scope(agent_id)`, so every model call made
while an agent works (its tool loop, a skill's structured draft, a nested helper) is routed with
that agent's provider preference (`ai.agent_providers`). Calls made by JARVIS itself (chat,
intent, planning, verification, the final answer) run outside any scope and use the role order.

Cloud AI (Grok, Claude, OpenAI) costs money per request, so with `ai.cloud_only_when_told` the
router uses one only inside `allow_provider(name)`: the core opens it when the user names that AI
in a request ("תשתמש בגרוק ...") or says yes when JARVIS asks, and a task carries it to its steps.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

_CURRENT_AGENT: ContextVar[str | None] = ContextVar("jarvis_current_agent", default=None)
_ALLOWED: ContextVar[str | None] = ContextVar("jarvis_allowed_ai", default=None)


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


def allowed_provider() -> str | None:
    """The cloud AI the user asked for (or said yes to) for the work in progress; None = free models only."""
    return _ALLOWED.get()


@contextlib.contextmanager
def allow_provider(name: str | None) -> Iterator[None]:
    """Model calls in this block, and in tasks created inside it, may use the cloud AI `name` (None: none)."""
    token = _ALLOWED.set(name or None)
    try:
        yield
    finally:
        _ALLOWED.reset(token)
