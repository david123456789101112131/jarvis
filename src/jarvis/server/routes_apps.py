"""Routes of the "Apps & tools" screen(s)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

if TYPE_CHECKING:
    from jarvis.app import JarvisApp


def apps_routes(app: JarvisApp) -> APIRouter:
    r = APIRouter()

    @r.get("/api/apps/status")
    async def status() -> dict[str, Any]:
        return app.apps.status()

    return r


__all__ = ["apps_routes"]
