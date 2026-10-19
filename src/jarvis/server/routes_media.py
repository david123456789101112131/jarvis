"""Routes of the "Image creation and Video creation" screen(s)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

if TYPE_CHECKING:
    from jarvis.app import JarvisApp


def media_routes(app: JarvisApp) -> APIRouter:
    r = APIRouter()

    @r.get("/api/media/status")
    async def status() -> dict[str, Any]:
        return app.media.status()

    return r


__all__ = ["media_routes"]
