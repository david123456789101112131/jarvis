"""The development-mode service: one per JARVIS, built by the composition root."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.app import JarvisApp


class DevService:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def aclose(self) -> None:
        return None


__all__ = ["DevService"]
