"""Routes of the "Privacy & data" screen.

`GET /api/privacy` says what JARVIS keeps on this computer (how much, and where), what it keeps on purpose, and
what can reach the microphone, the screen and the internet; `POST /api/privacy/clear` deletes the kinds asked for,
for good, and returns a result per kind with the new inventory. Only counts, sizes, places and names are
returned, never the data itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis.privacy import CLEARABLE

if TYPE_CHECKING:
    from jarvis.app import JarvisApp


class ClearBody(BaseModel):
    items: list[str] = Field(min_length=1, max_length=len(CLEARABLE))


def privacy_routes(app: JarvisApp) -> APIRouter:
    r = APIRouter()

    @r.get("/api/privacy")
    async def inventory() -> dict[str, Any]:
        return await app.privacy.inventory()

    @r.post("/api/privacy/clear")
    async def clear(body: ClearBody) -> dict[str, Any]:
        unknown = [k for k in body.items if k not in CLEARABLE]
        if unknown:
            raise HTTPException(422, f"Unknown kind of data: {', '.join(unknown)} (one of {', '.join(CLEARABLE)})")
        return await app.privacy.clear(body.items)

    return r


__all__ = ["privacy_routes"]
