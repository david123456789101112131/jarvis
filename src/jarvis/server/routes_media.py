"""Routes of the "Image creation" and "Video creation" screens.

Every action answers at once with the job it started; its progress then arrives as `job` events on the
WebSocket, and its result carries the previews. The work itself is the media service's (`jarvis.media.service`):
creating goes to xAI through the tool executor, saving copies through it to the place the user picked.

`GET /api/media/file/{id}` serves one preview and nothing else: the id is looked up in the service's index, and
the file must still be inside `<workspace>/media`, so no path from the request ever reaches the disk.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from jarvis.common.i18n import t
from jarvis.media.service import (
    ImageRequest,
    MediaRequestError,
    RefineImageRequest,
    RefineVideoRequest,
    SaveRequest,
    VideoRequest,
)
from jarvis.security.path_guard import normalize_path

if TYPE_CHECKING:
    from jarvis.app import JarvisApp

_ITEM_ID = re.compile(r"media_[a-z0-9]{4,40}")
# A preview opened on its own runs nothing and loads nothing (an SVG placeholder included).
_FILE_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600",
                 "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                                            "media-src 'self'; sandbox"}


def _refused(exc: MediaRequestError) -> JSONResponse:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details:
        error["details"] = exc.details
    return JSONResponse({"error": error}, status_code=exc.status)


def media_routes(app: JarvisApp) -> APIRouter:
    r = APIRouter()

    @r.get("/api/media/status")
    async def status(lang: str | None = None) -> dict[str, Any]:
        return app.media.status(lang)

    # ---------------------------------------------------------------------------- the previews
    @r.get("/api/media/items")
    async def items(kind: Literal["image", "video"] | None = None) -> list[dict[str, Any]]:
        return app.media.items(kind)

    @r.get("/api/media/file/{item_id}")
    async def file(item_id: str) -> Response:
        item = app.media.file(item_id) if _ITEM_ID.fullmatch(item_id) else None
        if item is None:
            raise HTTPException(404, t(app.lang, "אין תצוגה מקדימה כזו.", "There is no such preview."))
        return FileResponse(normalize_path(item.path), media_type=item.mime, headers=_FILE_HEADERS)

    @r.delete("/api/media/items/{item_id}")
    async def delete_item(item_id: str) -> dict[str, Any]:
        if not await app.media.delete(item_id):
            raise HTTPException(404, t(app.lang, "אין תצוגה מקדימה כזו.", "There is no such preview."))
        return {"deleted": True}

    @r.delete("/api/media/items")
    async def clear_items(kind: Literal["image", "video"] | None = None) -> dict[str, Any]:
        return {"cleared": await app.media.clear(kind)}

    # ---------------------------------------------------------------------------- the actions (each one a job)
    @r.post("/api/media/images", response_model=None)
    async def create_image(body: ImageRequest) -> dict[str, Any] | JSONResponse:
        try:
            return (await app.media.create_image(body)).view()
        except MediaRequestError as exc:
            return _refused(exc)

    @r.post("/api/media/images/refine", response_model=None)
    async def refine_image(body: RefineImageRequest) -> dict[str, Any] | JSONResponse:
        try:
            return (await app.media.refine_image(body)).view()
        except MediaRequestError as exc:
            return _refused(exc)

    @r.post("/api/media/videos", response_model=None)
    async def create_video(body: VideoRequest) -> dict[str, Any] | JSONResponse:
        try:
            return (await app.media.create_video(body)).view()
        except MediaRequestError as exc:
            return _refused(exc)

    @r.post("/api/media/videos/refine", response_model=None)
    async def refine_video(body: RefineVideoRequest) -> dict[str, Any] | JSONResponse:
        try:
            return (await app.media.refine_video(body)).view()
        except MediaRequestError as exc:
            return _refused(exc)

    @r.post("/api/media/save", response_model=None)
    async def save(body: SaveRequest) -> dict[str, Any] | JSONResponse:
        try:
            return (await app.media.save(body)).view()
        except MediaRequestError as exc:
            return _refused(exc)

    return r


__all__ = ["media_routes"]
