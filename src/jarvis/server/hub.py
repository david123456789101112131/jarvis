"""WebSocket hub: every connected UI gets the live events as `{type, data, ts}` messages.

Events come from the bus (already redacted by the bus sanitizer) plus a few direct pushes
(the microphone level, ~15 per second, which would only crowd the bus history). Each client
has its own bounded queue and sender task, so one slow window never delays the core or the
other clients; a client that falls too far behind is disconnected and, on reconnect, starts
again from a fresh state snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from jarvis.bus import MessageBus
from jarvis.bus.message import Message
from jarvis.common.clock import iso

log = logging.getLogger(__name__)

# Bus topics the UI receives (docs/API.md, "WebSocket /ws").
FORWARDED_TOPICS = (
    "chat.message", "agent.status", "task.updated", "task.subtask", "task.timeline", "activity", "audit",
    "system.stats", "voice.state", "voice.transcript", "voice.pipeline", "action", "speech",
    "confirm.request", "confirm.response", "mode", "killswitch", "notification", "settings", "providers",
    "ui.navigate",
)
QUEUE_SIZE = 2000
SLOW_CLIENT_CODE = 4408

ClientHandler = Callable[[dict[str, Any], "Client"], Awaitable[None]]


def encode(type_: str, data: Any) -> str:
    return json.dumps({"type": type_, "data": data, "ts": iso()}, ensure_ascii=False, default=str)


class Client:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.overflowed = False

    def push(self, text: str) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.overflowed = True
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.get_nowait()
                self.queue.put_nowait(None)      # wake the sender so it closes the socket

    async def send_now(self, type_: str, data: Any) -> None:
        await self.ws.send_text(encode(type_, data))

    async def pump(self) -> None:
        while True:
            text = await self.queue.get()
            if text is None or self.overflowed:
                with contextlib.suppress(Exception):
                    await self.ws.close(code=SLOW_CLIENT_CODE)
                return
            await self.ws.send_text(text)


class EventHub:
    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus
        self.clients: set[Client] = set()
        self._subs = [bus.subscribe(topic, self._on_bus, name="ws-hub") for topic in FORWARDED_TOPICS]
        self._closed = False

    @property
    def client_count(self) -> int:
        return len(self.clients)

    def _on_bus(self, msg: Message) -> None:
        self.broadcast(msg.topic, msg.data)

    def broadcast(self, type_: str, data: Any) -> None:
        if not self.clients or self._closed:
            return
        text = encode(type_, data)
        for client in list(self.clients):
            client.push(text)

    async def serve(self, ws: WebSocket, *, snapshot: Callable[[], Awaitable[dict[str, Any]]],
                    version: str, on_message: ClientHandler) -> None:
        """Run one connection: hello + state, then live events; messages from the client go to `on_message`."""
        client = Client(ws)
        # Register before taking the snapshot: an event that lands in between is queued, never lost.
        self.clients.add(client)
        sender: asyncio.Task[None] | None = None
        try:
            await client.send_now("hello", {"version": version})
            await client.send_now("state", await snapshot())
            sender = asyncio.create_task(client.pump(), name="jarvis-ws-sender")
            while True:
                receive = asyncio.ensure_future(ws.receive_text())
                done, _ = await asyncio.wait({receive, sender}, return_when=asyncio.FIRST_COMPLETED)
                if sender in done:
                    receive.cancel()
                    with contextlib.suppress(BaseException):
                        await receive
                    break
                try:
                    raw = receive.result()
                except (WebSocketDisconnect, RuntimeError):
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await on_message(msg, client)
        except (WebSocketDisconnect, RuntimeError):     # closed by the client or mid-send
            pass
        except Exception:
            log.exception("WebSocket connection failed")
        finally:
            self.clients.discard(client)
            if sender is not None:
                sender.cancel()
                with contextlib.suppress(BaseException):
                    await sender

    async def close(self) -> None:
        self._closed = True
        for sub in self._subs:
            sub.cancel()
        for client in list(self.clients):
            with contextlib.suppress(Exception):
                await client.ws.close(code=1001)
        self.clients.clear()
