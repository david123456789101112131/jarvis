"""Who may talk to the local server: this machine, this app, this run.

* The server listens on the loopback interface only.
* Every API call needs the per-run token (the `X-JARVIS-Token` header, or `?token=` on the
  WebSocket) or a browser session cookie. The token is created at start-up and written only to
  `runtime.json` in the user's private data folder, which the agents' tools cannot read.
* The page itself is served only to a browser that opened `/?launch=<token>` (the JARVIS window
  and `jarvis open` do that). The launch link is exchanged for a random session id in an
  HttpOnly cookie, so the page's scripts never see the token or the session id; its API calls
  and its WebSocket are authorised by the cookie.
* `Host` must be a loopback name (blocks DNS rebinding) and a browser `Origin` must be this
  server (blocks other web pages and cross-site WebSocket hijacking).
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
import threading
from collections import OrderedDict
from collections.abc import Iterable
from urllib.parse import urlsplit

COOKIE = "jarvis_session"
HEADER = "x-jarvis-token"
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    name = host.strip().lower().strip("[]")
    if name in LOOPBACK_NAMES or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def split_host_header(value: str) -> tuple[str, int | None]:
    """'127.0.0.1:8765' → ('127.0.0.1', 8765); '[::1]:8765' → ('::1', 8765)."""
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = value.partition(":")
    return host, int(port) if port.isdigit() else None


class ServerAuth:
    MAX_SESSIONS = 32       # windows opened during this run (the main window, the mini orb, `jarvis open`)

    def __init__(self, token: str | None = None, *, extra_origins: Iterable[str] = ()) -> None:
        self.token = token or secrets.token_urlsafe(32)
        self.port: int | None = None
        # e.g. the Vite dev server while developing the UI ("http://localhost:5173")
        self.extra_origins = {o.rstrip("/") for o in extra_origins}
        self._sessions: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def check_token(self, value: str | None) -> bool:
        if not value:
            return False
        return hmac.compare_digest(value.encode(), self.token.encode())

    def new_session(self) -> str:
        """A browser session id for the cookie (the oldest is forgotten beyond MAX_SESSIONS)."""
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[sid] = None
            while len(self._sessions) > self.MAX_SESSIONS:
                self._sessions.popitem(last=False)
        return sid

    def check_session(self, value: str | None) -> bool:
        if not value:
            return False
        with self._lock:
            known = list(self._sessions)
        found = False
        for sid in known:  # compare with every id, so the time taken does not depend on where it matched
            found |= hmac.compare_digest(value.encode(), sid.encode())
        return found

    def host_ok(self, host_header: str | None) -> bool:
        if not host_header:
            return False
        host, _ = split_host_header(host_header)
        return is_loopback_host(host)

    def origin_ok(self, origin: str | None) -> bool:
        """No Origin (CLI, tests, the desktop shell) is fine; a browser origin must be this server."""
        if origin is None:
            return True
        origin = origin.rstrip("/")
        if origin in self.extra_origins:
            return True
        try:
            parts = urlsplit(origin)
            port = parts.port
        except ValueError:
            return False
        return parts.scheme == "http" and is_loopback_host(parts.hostname) and port is not None and port == self.port

    def authorized(self, *, header: str | None, cookie: str | None, query: str | None = None) -> bool:
        return self.check_token(header) or self.check_session(cookie) or self.check_token(query)
