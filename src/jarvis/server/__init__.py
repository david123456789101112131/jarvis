"""Local server: the REST API and the WebSocket event stream the UI talks to (docs/API.md)."""

from jarvis.server.auth import ServerAuth
from jarvis.server.hub import FORWARDED_TOPICS, EventHub
from jarvis.server.runner import LocalServer, ServerStartError

__all__ = ["FORWARDED_TOPICS", "EventHub", "LocalServer", "ServerAuth", "ServerStartError"]
