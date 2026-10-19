"""The apps service behind the "Apps & tools" screen, built once by the composition root."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.app import JarvisApp


class AppsService:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app

    def status(self) -> dict[str, Any]:
        return {"computer": self.app.computer.problem() is None}

    async def aclose(self) -> None:
        return None


__all__ = ["AppsService"]
