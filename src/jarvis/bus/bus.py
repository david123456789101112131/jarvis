"""Communication Bus.

Two delivery styles share one envelope (`Message`):

* **Point-to-point**: a message with a `recipient` goes into that owner's priority
  `Mailbox` (CRITICAL first, FIFO within a priority). Agents consume their mailbox.
* **Pub/sub**: every message is also published on its `topic` to matching subscribers
  (UI bridge, recorder, debug views). Patterns: exact (`agent.status`), prefix wildcard
  (`task.*`) or everything (`*`).

Request/reply: `request()` waits for a message whose `correlation_id` equals the request id.
A `sanitizer` (the secret redactor) runs on every message before anyone sees it.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import inspect
import itertools
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from jarvis.bus.message import Message

log = logging.getLogger(__name__)

Handler = Callable[[Message], Awaitable[None] | None]


def topic_matches(pattern: str, topic: str) -> bool:
    if pattern in ("*", "#"):
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return topic == prefix or topic.startswith(prefix + ".")
    return pattern == topic


@dataclass
class Subscription:
    pattern: str
    handler: Handler
    name: str = ""
    _bus: MessageBus | None = field(default=None, repr=False)

    def cancel(self) -> None:
        if self._bus is not None:
            self._bus.unsubscribe(self)
            self._bus = None


class Mailbox:
    """Async priority queue of messages with support for discarding queued items."""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self._heap: list[tuple[int, int, Message]] = []
        self._seq = itertools.count()
        self._cond = asyncio.Condition()
        self._closed = False

    def __len__(self) -> int:
        return len(self._heap)

    @property
    def closed(self) -> bool:
        return self._closed

    async def put(self, msg: Message) -> None:
        async with self._cond:
            heapq.heappush(self._heap, (int(msg.priority), next(self._seq), msg))
            # Every waiter re-checks: a getter with a predicate may not want this message.
            self._cond.notify_all()

    async def get(self, predicate: Callable[[Message], bool] | None = None) -> Message:
        """Next message by priority (then FIFO); with `predicate`, the next one that matches."""
        async with self._cond:
            while True:
                if predicate is None:
                    if self._heap:
                        return heapq.heappop(self._heap)[2]
                else:
                    for item in sorted(self._heap):
                        if predicate(item[2]):
                            self._heap.remove(item)
                            heapq.heapify(self._heap)
                            return item[2]
                if self._closed:
                    raise asyncio.CancelledError(f"mailbox {self.owner} closed")
                await self._cond.wait()

    def peek_all(self) -> list[Message]:
        return [m for _, _, m in sorted(self._heap)]

    async def remove(self, predicate: Callable[[Message], bool]) -> list[Message]:
        async with self._cond:
            kept, removed = [], []
            for item in self._heap:
                (removed if predicate(item[2]) else kept).append(item)
            heapq.heapify(kept)
            self._heap = kept
            return [m for _, _, m in removed]

    async def close(self) -> None:
        async with self._cond:
            self._closed = True
            self._cond.notify_all()

    async def reopen(self) -> None:
        async with self._cond:
            self._closed = False


class MessageBus:
    def __init__(self, *, history_size: int = 2000, sanitizer: Callable[[Message], Message] | None = None) -> None:
        self._subs: list[Subscription] = []
        self._mailboxes: dict[str, Mailbox] = {}
        self._waiters: dict[str, asyncio.Future[Message]] = {}
        self._history: deque[Message] = deque(maxlen=history_size)
        self._sanitizer = sanitizer
        self._loop: asyncio.AbstractEventLoop | None = None
        self.handler_timeout_s = 5.0

    # -- setup -------------------------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_running_loop()

    def set_sanitizer(self, sanitizer: Callable[[Message], Message] | None) -> None:
        self._sanitizer = sanitizer

    def subscribe(self, pattern: str, handler: Handler, *, name: str = "") -> Subscription:
        sub = Subscription(pattern=pattern, handler=handler, name=name or getattr(handler, "__qualname__", ""), _bus=self)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subs.remove(sub)

    def register_mailbox(self, owner: str) -> Mailbox:
        box = self._mailboxes.get(owner)
        if box is None:
            box = Mailbox(owner)
            self._mailboxes[owner] = box
        return box

    def mailbox(self, owner: str) -> Mailbox | None:
        return self._mailboxes.get(owner)

    def unregister_mailbox(self, owner: str) -> None:
        self._mailboxes.pop(owner, None)

    # -- publishing ----------------------------------------------------------------------
    async def publish(self, msg: Message) -> Message:
        if self._sanitizer is not None:
            try:
                msg = self._sanitizer(msg)
            except Exception:  # pragma: no cover - sanitizer must never break delivery
                log.exception("Message sanitizer failed")
        self._history.append(msg)

        if msg.recipient:
            box = self._mailboxes.get(msg.recipient)
            if box is not None and not box.closed:
                await box.put(msg)

        if msg.correlation_id:
            waiter = self._waiters.pop(msg.correlation_id, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(msg)

        for sub in list(self._subs):
            if topic_matches(sub.pattern, msg.topic):
                await self._deliver(sub, msg)
        return msg

    async def _deliver(self, sub: Subscription, msg: Message) -> None:
        try:
            result = sub.handler(msg)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=self.handler_timeout_s)
        except TimeoutError:
            log.warning("Bus handler %s timed out on %s", sub.name, msg.topic)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Bus handler %s failed on %s", sub.name, msg.topic)

    def publish_nowait(self, msg: Message) -> None:
        """Fire-and-forget publish from sync code running inside the loop thread."""
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            running.create_task(self.publish(msg))
        elif loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.publish(msg), loop)
        else:
            log.debug("Dropped message %s: no running loop", msg.topic)

    def publish_threadsafe(self, msg: Message) -> None:
        """Publish from another thread (audio callbacks, desktop shell)."""
        if self._loop is None or self._loop.is_closed():
            log.debug("Dropped threadsafe message %s: bus loop not bound", msg.topic)
            return
        asyncio.run_coroutine_threadsafe(self.publish(msg), self._loop)

    async def request(self, msg: Message, *, timeout: float = 30.0) -> Message:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Message] = loop.create_future()
        self._waiters[msg.id] = fut
        try:
            await self.publish(msg)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._waiters.pop(msg.id, None)

    # -- introspection ---------------------------------------------------------------------
    def history(self, *, topic: str | None = None, task_id: str | None = None, limit: int = 200) -> list[Message]:
        items = [
            m
            for m in self._history
            if (topic is None or topic_matches(topic, m.topic)) and (task_id is None or m.task_id == task_id)
        ]
        return items[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def stats(self) -> dict[str, Any]:
        return {
            "subscribers": len(self._subs),
            "mailboxes": {k: len(v) for k, v in self._mailboxes.items()},
            "history": len(self._history),
        }
