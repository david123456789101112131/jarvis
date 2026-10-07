"""Run the local API inside JARVIS's own event loop (uvicorn, loopback only)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import sys
from collections.abc import Iterator
from typing import Any

import uvicorn

from jarvis.common.errors import JarvisError
from jarvis.server.auth import is_loopback_host

log = logging.getLogger(__name__)


class ServerStartError(JarvisError):
    pass


class _Server(uvicorn.Server):
    """uvicorn without its signal handling: JARVIS decides how Ctrl+C and closing the window end the run."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    def install_signal_handlers(self) -> None:  # uvicorn < 0.29
        return None


def bind_loopback(host: str, port: int, span: int) -> socket.socket:
    """Listen on the first free port in [port, port + span] (0 = any free port)."""
    if not is_loopback_host(host):
        log.warning("Refusing to listen on %s: the JARVIS server only listens on 127.0.0.1", host)
        host = "127.0.0.1"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    ports = [0] if port == 0 else range(port, port + max(0, span) + 1)
    last: OSError | None = None
    for candidate in ports:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                # Exclusive: no other program can bind the same port and intercept the UI's requests.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, candidate))
            sock.listen(128)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise ServerStartError(f"No free port for the JARVIS server between {port} and {port + span}: {last}")


class LocalServer:
    def __init__(self, app: Any, *, host: str = "127.0.0.1", port: int = 8765, span: int = 20) -> None:
        self.app = app
        self.host = host
        self.port_wanted = port
        self.span = span
        self.port: int | None = None
        self._server: _Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._sock: socket.socket | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> int:
        self._sock = bind_loopback(self.host, self.port_wanted, self.span)
        self.port = int(self._sock.getsockname()[1])
        config = uvicorn.Config(self.app, lifespan="off", log_level="warning", access_log=False,
                                timeout_graceful_shutdown=3, ws_ping_interval=20, ws_ping_timeout=20)
        self._server = _Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[self._sock]), name="jarvis-server")
        while not self._server.started:
            if self._task.done():
                exc = self._task.exception() if not self._task.cancelled() else None
                raise ServerStartError(f"The JARVIS server did not start: {exc}")
            await asyncio.sleep(0.01)
        log.info("JARVIS server listening on http://127.0.0.1:%s", self.port)
        return self.port

    async def stop(self, timeout: float = 8.0) -> None:
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)
        except TimeoutError:
            self._server.force_exit = True
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        except Exception:
            log.debug("Server task ended with an error", exc_info=True)
        finally:
            if self._sock is not None:
                with contextlib.suppress(OSError):
                    self._sock.close()
            self._server = self._task = self._sock = None
