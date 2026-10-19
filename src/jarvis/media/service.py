"""The media service: image and video creation, built once by the composition root."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.app import JarvisApp


class MediaService:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app

    def status(self) -> dict[str, Any]:
        return {"xai": self.app.secrets.has("XAI_API_KEY")}

    async def aclose(self) -> None:
        return None


__all__ = ["MediaService"]
