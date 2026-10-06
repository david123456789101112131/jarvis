"""Permission data model: grants, action requests and decisions."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.clock import utcnow
from jarvis.common.enums import Capability, RiskLevel
from jarvis.common.ids import new_id

PathAccess = Literal["read", "write", "delete"]
GrantSource = Literal["policy", "user", "jarvis"]


class Grant(BaseModel):
    """Permission for one agent ("*" = every agent) to use one capability, optionally scoped."""

    id: str = Field(default_factory=lambda: new_id("grant"))
    agent_id: str
    capability: Capability
    roots: list[str] = Field(default_factory=list)   # filesystem scope; empty = global path rules only
    task_id: str | None = None                        # only valid while working on this task
    expires_at: datetime | None = None
    max_uses: int | None = None
    uses: int = 0
    source: GrantSource = "policy"
    reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        if self.expires_at is not None and now >= self.expires_at:
            return True
        return self.max_uses is not None and self.uses >= self.max_uses

    def applies_to(self, agent_id: str, capability: Capability, task_id: str | None) -> bool:
        if self.agent_id not in ("*", agent_id) or self.capability != capability:
            return False
        return self.task_id is None or self.task_id == task_id

    @classmethod
    def temporary(cls, agent_id: str, capability: Capability, *, ttl_s: float | None = None, **kw: Any) -> Grant:
        expires = utcnow() + timedelta(seconds=ttl_s) if ttl_s else None
        return cls(agent_id=agent_id, capability=capability, expires_at=expires, **kw)


class PathUse(BaseModel):
    path: str
    access: PathAccess = "read"


class ActionRequest(BaseModel):
    """What a tool is about to do, described before it runs."""

    agent_id: str
    task_id: str | None = None
    tool: str
    capabilities: list[Capability]
    summary: str = ""                              # human readable: "Delete folder C:\\x (12 files)"
    paths: list[PathUse] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW                 # tool's own estimate; the assessor may raise it
    risk_reasons: list[str] = Field(default_factory=list)
    outbound: str | None = None                    # payload that leaves the machine (scanned for secrets)
    destination: str | None = None                 # host / URL the payload goes to
    always_confirm: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    allowed: bool
    needs_confirmation: bool = False
    risk: RiskLevel = RiskLevel.SAFE
    reasons: list[str] = Field(default_factory=list)
    grant_ids: list[str] = Field(default_factory=list)
    blocked: bool = False          # hard block (cannot be confirmed), e.g. secrets leaving the machine

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)
