"""The single message envelope used between JARVIS, the agents, and observers (UI, logs).

Every field the spec requires is present: task_id, agent_id, type, priority, instruction,
context, permissions, timestamp, status, result. Extra fields support routing
(sender/recipient/topic), request–reply correlation and event payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jarvis.common.clock import utcnow
from jarvis.common.enums import MessageType, Priority
from jarvis.common.ids import new_id


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    id: str = Field(default_factory=lambda: new_id("msg"))
    task_id: str | None = None
    agent_id: str | None = None
    type: MessageType = MessageType.EVENT
    priority: Priority = Priority.NORMAL
    instruction: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=utcnow)
    status: str | None = None
    result: Any = None

    sender: str = "jarvis"
    recipient: str | None = None
    topic: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    parent_id: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.topic:
            self.topic = self.type.value

    def reply(self, **fields: Any) -> Message:
        """Build a reply addressed to this message's sender, correlated to this message."""
        base: dict[str, Any] = {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "priority": self.priority,
            "recipient": self.sender,
            "correlation_id": self.id,
            "parent_id": self.id,
        }
        base.update(fields)
        return Message(**base)

    def to_public(self) -> dict[str, Any]:
        """JSON-safe dict for UI/persistence."""
        return self.model_dump(mode="json")


def event(topic: str, data: dict[str, Any] | None = None, *, sender: str = "jarvis", **fields: Any) -> Message:
    """Shortcut for an observer-facing event (agent status, metrics, chat, voice state...)."""
    return Message(type=MessageType.EVENT, topic=topic, data=data or {}, sender=sender, **fields)
