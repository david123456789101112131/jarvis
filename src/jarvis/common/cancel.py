"""Cooperative cancellation tokens shared by the orchestrator, agents and tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from jarvis.common.errors import TaskCancelled


class CancelToken:
    """Set once; children are cancelled with their parent. Safe to create outside a loop."""

    def __init__(self, parent: CancelToken | None = None) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None
        self._children: list[CancelToken] = []
        self._callbacks: list[Callable[[str], None]] = []
        if parent is not None:
            parent._children.append(self)
            if parent.cancelled:
                self.cancel(parent.reason or "parent cancelled")

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def event(self) -> asyncio.Event:
        return self._event

    def cancel(self, reason: str = "cancelled") -> None:
        if self._event.is_set():
            return
        self.reason = reason
        self._event.set()
        for cb in list(self._callbacks):
            try:
                cb(reason)
            except Exception:  # pragma: no cover - callbacks must not break cancellation
                pass
        for child in list(self._children):
            child.cancel(reason)

    def on_cancel(self, callback: Callable[[str], None]) -> None:
        if self.cancelled:
            callback(self.reason or "cancelled")
        else:
            self._callbacks.append(callback)

    def child(self) -> CancelToken:
        return CancelToken(self)

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise TaskCancelled(self.reason or "cancelled")

    async def wait(self) -> None:
        await self._event.wait()
