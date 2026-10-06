"""Confirmation broker: risky actions wait here for an explicit human decision.

A request is published on the bus (`confirm.request`) so the UI shows a dialog and the voice
pipeline can ask out loud. The tool call stays suspended until someone answers. No answer
before the timeout means DENY. Approvals can optionally be remembered for the rest of the
same task (never for CRITICAL actions).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.bus import MessageBus
from jarvis.bus.message import event
from jarvis.common.clock import utcnow
from jarvis.common.enums import RiskLevel
from jarvis.common.ids import new_id

log = logging.getLogger(__name__)

ConfirmStatus = Literal["pending", "approved", "denied", "expired", "cancelled"]
Listener = Callable[["ConfirmationRequest"], Awaitable[None] | None]


class ConfirmationRequest(BaseModel):
    id: str = Field(default_factory=lambda: new_id("conf"))
    agent_id: str
    task_id: str | None = None
    tool: str
    summary: str
    reasons: list[str] = Field(default_factory=list)
    risk: str = "high"
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    status: ConfirmStatus = "pending"
    decided_by: str | None = None
    remember: bool = False

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ConfirmationBroker:
    def __init__(self, bus: MessageBus | None = None, *, timeout_s: float = 120.0) -> None:
        self.bus = bus
        self.timeout_s = timeout_s
        self._pending: dict[str, tuple[ConfirmationRequest, asyncio.Future[bool]]] = {}
        self._remembered: set[tuple[str, str | None, str]] = set()
        self._listeners: list[Listener] = []
        self.history: list[ConfirmationRequest] = []

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def pending(self) -> list[ConfirmationRequest]:
        return [req for req, _ in self._pending.values()]

    def latest_pending(self) -> ConfirmationRequest | None:
        items = self.pending()
        return max(items, key=lambda r: r.created_at) if items else None

    def is_remembered(self, agent_id: str, task_id: str | None, tool: str) -> bool:
        return task_id is not None and (agent_id, task_id, tool) in self._remembered

    async def request(self, req: ConfirmationRequest, *, timeout_s: float | None = None) -> bool:
        if req.risk != RiskLevel.CRITICAL.name.lower() and self.is_remembered(req.agent_id, req.task_id, req.tool):
            req.status, req.decided_by = "approved", "remembered"
            self.history.append(req)
            return True
        timeout = self.timeout_s if timeout_s is None else timeout_s
        req.expires_at = utcnow() + timedelta(seconds=timeout)
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[req.id] = (req, fut)
        await self._announce("confirm.request", req)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except TimeoutError:
            req.status, req.decided_by = "expired", "timeout"
            log.info("Confirmation %s expired -> denied", req.id)
            return False
        except asyncio.CancelledError:
            req.status, req.decided_by = "cancelled", "cancelled"
            raise
        finally:
            self._pending.pop(req.id, None)
            if not fut.done():
                fut.cancel()
            self.history.append(req)
            self.history = self.history[-500:]
            await self._announce("confirm.response", req)

    def respond(self, request_id: str, approved: bool, *, by: str = "user", remember: bool = False) -> bool:
        item = self._pending.get(request_id)
        if item is None:
            return False
        req, fut = item
        req.status = "approved" if approved else "denied"
        req.decided_by = by
        req.remember = bool(remember and approved and req.risk != RiskLevel.CRITICAL.name.lower())
        if req.remember and req.task_id:
            self._remembered.add((req.agent_id, req.task_id, req.tool))
        if not fut.done():
            fut.set_result(approved)
        return True

    def cancel_task(self, task_id: str, reason: str = "task cancelled") -> int:
        n = 0
        for req, fut in list(self._pending.values()):
            if req.task_id == task_id and not fut.done():
                req.status, req.decided_by = "cancelled", reason
                fut.set_result(False)
                n += 1
        self._remembered = {k for k in self._remembered if k[1] != task_id}
        return n

    def deny_all(self, reason: str = "kill switch") -> int:
        n = 0
        for req, fut in list(self._pending.values()):
            if not fut.done():
                req.status, req.decided_by = "denied", reason
                fut.set_result(False)
                n += 1
        return n

    def forget_task(self, task_id: str) -> None:
        self._remembered = {k for k in self._remembered if k[1] != task_id}

    async def _announce(self, topic: str, req: ConfirmationRequest) -> None:
        if self.bus is not None:
            with contextlib.suppress(Exception):
                await self.bus.publish(event(topic, req.public(), sender="permissions", task_id=req.task_id,
                                             agent_id=req.agent_id))
        if topic == "confirm.request":
            for listener in list(self._listeners):
                try:
                    res = listener(req)
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    log.exception("Confirmation listener failed")
