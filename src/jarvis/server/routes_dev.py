"""Routes of the "Code & development" screen(s)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

if TYPE_CHECKING:
    from jarvis.app import JarvisApp


def dev_routes(app: JarvisApp) -> APIRouter:
    r = APIRouter()

    @r.get("/api/dev/status")
    async def status() -> dict[str, Any]:
        return app.dev.status()

    return r


__all__ = ["dev_routes"]
