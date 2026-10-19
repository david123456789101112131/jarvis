"""The media tools: creating images and videos with a connected service (the "Image creation" and "Video creation"
screens run them as the Creative agent, through the executor and its approvals).

Each call's plan says exactly what leaves the computer (the text, and any image sent along), where it goes
(api.x.ai) and the estimated price; the executor puts that in the approval whenever one is asked. A description
alone is an ordinary request. An image of the user's own sent along makes the call HIGH risk and always asked,
with the file and xAI named; the screens' own previews (which came from xAI in the first place) go back without
that. What comes back is written only into the folder the call names, which must be inside `<workspace>/media`.

No agent is offered these tools (they are in no agent's tool list): only the screens start them, when the user
presses Create.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from jarvis.common.enums import Capability as C
from jarvis.common.enums import RiskLevel
from jarvis.common.errors import PermissionDenied, ToolError
from jarvis.common.i18n import t
from jarvis.media import catalog
from jarvis.media.client import (
    BASE_URL,
    DOWNLOAD_TIMEOUT_S,
    IMAGE_TIMEOUT_S,
    MediaError,
    Picture,
    VideoState,
    XaiMedia,
)
from jarvis.media.files import probe, safe_name
from jarvis.permissions.models import PathUse
from jarvis.security.path_guard import is_within, normalize_path
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult
from jarvis.tools.services import ToolServices
from jarvis.tools.web import check_network


def media_folder(services: ToolServices) -> Path:
    """Where the screens' previews live: `<workspace>/media`."""
    return normalize_path(services.paths.workspace / "media")


def xai_client(services: ToolServices) -> XaiMedia:
    """The xAI client on the tools' guarded web client; the key is read from the secret store per request."""
    secrets = services.secrets

    def key() -> str | None:
        return secrets.get(catalog.KEY_NAME) if secrets is not None else None

    return XaiMedia(key, services.http, redact=services.redactor.redact_text)


def _size(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 100_000 else f"{max(1, round(n / 1000))} KB"


def _out_dir(ctx: ToolContext, raw: str) -> Path:
    out = ctx.resolve(raw)
    root = media_folder(ctx.services)
    if not is_within(out, root) or is_within(root, out):
        raise PermissionDenied(ctx.t(f"הכלי כותב רק לתיקייה בתוך {root}", f"This tool writes only into a folder inside {root}"),
                                 details={"path": str(out)})
    return out


async def _inputs(ctx: ToolContext, raws: list[str]) -> list[tuple[Path, int]]:
    """The images sent along, with their sizes: each must exist, be small enough, and be one xAI reads."""
    found: list[tuple[Path, int]] = []
    for raw in raws:
        path = ctx.resolve(raw)
        try:
            size = (await asyncio.to_thread(path.stat)).st_size
        except OSError:
            raise ToolError(ctx.t(f"התמונה {path} לא נמצאה", f"The image {path} was not found")) from None
        if not await asyncio.to_thread(path.is_file):
            raise ToolError(ctx.t(f"{path} אינו קובץ תמונה", f"{path} is not an image file"))
        if size > catalog.MAX_INPUT_BYTES:
            raise ToolError(ctx.t(f"התמונה {path.name} גדולה מדי ({_size(size)}); אפשר לשלוח עד {_size(catalog.MAX_INPUT_BYTES)}",
                                  f"The image {path.name} is too large ({_size(size)}); up to "
                                  f"{_size(catalog.MAX_INPUT_BYTES)} can be sent"))
        found.append((path, size))
    return found


async def _read_picture(ctx: ToolContext, path: Path) -> Picture:
    data = await asyncio.to_thread(path.read_bytes)
    if len(data) > catalog.MAX_INPUT_BYTES:
        raise ToolError(ctx.t(f"התמונה {path.name} גדולה מדי", f"The image {path.name} is too large"))
    kind = probe(data)
    if kind is None or kind.format not in catalog.INPUT_FORMATS:
        raise ToolError(ctx.t(f"{path.name} אינו תמונה מסוג ש-xAI מקבל (JPEG, PNG או WEBP)",
                              f"{path.name} is not an image xAI takes (JPEG, PNG or WEBP)"))
    return Picture(data, kind.mime)


def _plan(ctx: ToolContext, *, summary: str, endpoint: str, prompt: str, inputs: list[tuple[Path, int]], out: Path,
          cost: float, details: dict[str, Any]) -> ActionPlan:
    """What a creation call sends, where to, for how much; the user's own files raise it to always asking."""
    root = media_folder(ctx.services)
    uploads = [p for p, _ in inputs if not is_within(p, root)]
    sends = [ctx.t(f"הטקסט ({len(prompt)} תווים) ← {catalog.SERVICE_NAME}, {catalog.SERVICE_HOST}",
                   f"the text ({len(prompt)} characters) → {catalog.SERVICE_NAME}, {catalog.SERVICE_HOST}")]
    for path, size in inputs:
        own = not any(path == u for u in uploads)
        sends.append(ctx.t(f"{'התצוגה המקדימה' if own else 'הקובץ שלך'} {path.name} ({_size(size)}) ← xAI",
                           f"{'the preview' if own else 'your file'} {path.name} ({_size(size)}) → xAI"))
    reasons: list[str] = []
    risk: RiskLevel | None = None
    if uploads:
        names = ", ".join(p.name for p in uploads)
        reasons.append(ctx.t(f"מעלה ל-xAI ‏({catalog.SERVICE_HOST}) קובץ שלך: {names}",
                             f"uploads your file to xAI ({catalog.SERVICE_HOST}): {names}"))
        risk = RiskLevel.HIGH
    price = catalog.usd(cost)
    paths = [PathUse(path=str(p), access="read") for p, _ in inputs] + [PathUse(path=str(out), access="write")]
    return ActionPlan(
        summary=f"{summary} — ≈ {price}",
        paths=paths,
        risk=risk,
        risk_reasons=reasons,
        outbound=prompt,
        destination=catalog.SERVICE_HOST,
        sends_data=True,
        always_confirm=bool(uploads),
        details={
            "service": catalog.SERVICE_NAME,
            "to": f"{BASE_URL}{endpoint}",
            "sends": sends,
            "prompt": prompt,
            "files": [str(p) for p, _ in inputs],
            "price": ctx.t(f"≈ {price} (לפי המחירון של xAI מ-{catalog.PRICES_CHECKED})",
                           f"≈ {price} (xAI's prices as of {catalog.PRICES_CHECKED})"),
            "saved_to": str(out),
            **details,
        },
    )


def _failure(exc: MediaError, client: XaiMedia, **data: Any) -> ToolResult:
    return ToolResult.fail(exc.message, data={"kind": exc.details.get("kind", "failed"), "requests": client.requests, **data})


# ============================================================================ images
class CreateImageParams(BaseModel):
    prompt: str = Field(min_length=1, max_length=catalog.MAX_PROMPT)
    model: str = catalog.DEFAULT_IMAGE_MODEL
    count: int = Field(default=1, ge=1, le=catalog.MAX_IMAGES)
    aspect_ratio: str = "1:1"
    resolution: str = "1k"
    source: str | None = Field(default=None, max_length=1000, description="An image to change, sent along")
    out_dir: str = Field(min_length=1, max_length=1000, description="A folder inside <workspace>/media")
    name: str = Field(default="image", max_length=120)

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: str) -> str:
        if value not in {m.id for m in catalog.IMAGE_MODELS}:
            raise ValueError(f"unknown image model {value!r}")
        return value

    @field_validator("aspect_ratio")
    @classmethod
    def _known_aspect(cls, value: str) -> str:
        if value not in catalog.IMAGE_ASPECTS:
            raise ValueError(f"unknown aspect ratio {value!r}")
        return value

    @field_validator("resolution")
    @classmethod
    def _known_resolution(cls, value: str) -> str:
        if value not in catalog.IMAGE_RESOLUTIONS:
            raise ValueError(f"unknown resolution {value!r}")
        return value


class CreateImageTool(Tool):
    name = "create_image"
    description = ("Create images with xAI's Grok Imagine from a description (or change an image sent along), "
                   "and write them into a folder inside the workspace's media folder. Costs money per image.")
    capabilities = (C.AI_GENERATE, C.NET_FETCH, C.FS_WRITE)
    Params = CreateImageParams
    timeout_s = IMAGE_TIMEOUT_S + 120.0

    async def plan(self, params: CreateImageParams, ctx: ToolContext) -> ActionPlan:
        check_network(ctx, BASE_URL)
        out = _out_dir(ctx, params.out_dir)
        inputs = await _inputs(ctx, [params.source] if params.source else [])
        model = catalog.image_model(params.model)
        count = ctx.n(params.count, "תמונה אחת", "תמונות", "image", "images")
        if inputs:
            summary = ctx.t(f"שינוי התמונה {inputs[0][0].name} ב-{catalog.SERVICE_NAME} ({count})",
                            f"Change the image {inputs[0][0].name} with {catalog.SERVICE_NAME} ({count})")
        else:
            summary = ctx.t(f"יצירת {count} ב-{catalog.SERVICE_NAME}", f"Create {count} with {catalog.SERVICE_NAME}")
        cost = catalog.image_cost(model.id, params.resolution, params.count, inputs=len(inputs))
        return _plan(ctx, summary=summary, endpoint="/images/edits" if inputs else "/images/generations",
                     prompt=params.prompt, inputs=inputs, out=out, cost=cost,
                     details={"model": model.id, "count": params.count,
                              **({} if inputs else {"aspect_ratio": params.aspect_ratio, "resolution": params.resolution})})

    async def run(self, params: CreateImageParams, ctx: ToolContext) -> ToolResult:
        out = _out_dir(ctx, params.out_dir)
        source = await _read_picture(ctx, ctx.resolve(params.source)) if params.source else None
        client = xai_client(ctx.services)
        await ctx.progress(0.05, ctx.t("שולח ל-xAI ומחכה לתמונות…", "Sending to xAI and waiting for the images…"))
        try:
            batch = await client.images(prompt=params.prompt, model=params.model, count=params.count,
                                        aspect_ratio=params.aspect_ratio, resolution=params.resolution, source=source,
                                        lang=ctx.lang)
        except MediaError as exc:
            return _failure(exc, client)
        await ctx.progress(0.9, ctx.t("שומר את התצוגה המקדימה…", "Saving the preview…"))
        files = await asyncio.to_thread(_write_pictures, out, safe_name(params.name, "image"), batch.pictures)
        if not files:
            return ToolResult.fail(ctx.t("xAI החזיר משהו שאינו תמונה; לא נשמר דבר.",
                                         "xAI returned something that is not an image; nothing was kept."),
                                   data={"kind": "bad_response", "requests": client.requests})
        made = ctx.n(len(files), "נוצרה תמונה אחת", "תמונות נוצרו", "image created", "images created")
        return ToolResult(summary=made, files_created=[f["path"] for f in files],
                          data={"files": files, "usd": batch.usd, "model": batch.model, "requests": client.requests})


def _write_pictures(folder: Path, stem: str, pictures: list[Picture]) -> list[dict[str, Any]]:
    folder.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for index, picture in enumerate(pictures, 1):
        kind = probe(picture.data)
        if kind is None or kind.format == "MP4":
            continue
        path = folder / f"{stem}-{index}{kind.ext}"
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(picture.data)
        tmp.replace(path)
        files.append({"path": str(path), "format": kind.format, "mime": kind.mime, "bytes": len(picture.data),
                      "width": kind.width, "height": kind.height})
    return files


# ============================================================================ videos
class CreateVideoParams(BaseModel):
    prompt: str = Field(min_length=1, max_length=catalog.MAX_PROMPT)
    model: str = catalog.DEFAULT_VIDEO_MODEL
    seconds: int = Field(default=catalog.DEFAULT_SECONDS, ge=catalog.MIN_SECONDS, le=catalog.MAX_SECONDS)
    aspect_ratio: str = "16:9"
    resolution: str = "480p"
    image: str | None = Field(default=None, max_length=1000, description="The first frame, sent along")
    references: list[str] = Field(default_factory=list, max_length=catalog.MAX_REFERENCES)
    out_dir: str = Field(min_length=1, max_length=1000, description="A folder inside <workspace>/media")
    name: str = Field(default="video", max_length=120)

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: str) -> str:
        if value not in {m.id for m in catalog.VIDEO_MODELS}:
            raise ValueError(f"unknown video model {value!r}")
        return value

    @field_validator("aspect_ratio")
    @classmethod
    def _known_aspect(cls, value: str) -> str:
        if value not in catalog.VIDEO_ASPECTS:
            raise ValueError(f"unknown aspect ratio {value!r}")
        return value

    @field_validator("resolution")
    @classmethod
    def _known_resolution(cls, value: str) -> str:
        if value not in catalog.VIDEO_RESOLUTIONS:
            raise ValueError(f"unknown resolution {value!r}")
        return value

    @model_validator(mode="after")
    def _references_fit(self) -> CreateVideoParams:
        if self.references:
            if not catalog.video_model(self.model).references:
                raise ValueError("reference images need the full video model")
            if self.resolution not in catalog.REFERENCE_RESOLUTIONS:
                raise ValueError("a video from reference images goes up to 720p")
            if self.image:
                raise ValueError("send either a first frame or reference images, not both")
        return self


class CreateVideoTool(Tool):
    name = "create_video"
    description = ("Create a short video with xAI's Grok Imagine from a description (optionally from a first frame or "
                   "reference images), wait for it, and download it into a folder inside the workspace's media "
                   "folder. Costs money per second of video.")
    capabilities = (C.AI_GENERATE, C.NET_FETCH, C.FS_WRITE)
    Params = CreateVideoParams
    timeout_s = catalog.VIDEO_WAIT_S + DOWNLOAD_TIMEOUT_S + 120.0

    async def plan(self, params: CreateVideoParams, ctx: ToolContext) -> ActionPlan:
        check_network(ctx, BASE_URL)
        out = _out_dir(ctx, params.out_dir)
        inputs = await _inputs(ctx, [*([params.image] if params.image else []), *params.references])
        model = catalog.video_model(params.model)
        seconds = ctx.n(params.seconds, "שנייה אחת", "שניות", "second", "seconds")
        summary = ctx.t(f"יצירת סרטון של {seconds} ב-{catalog.SERVICE_NAME}",
                        f"Create a {params.seconds}-second video with {catalog.SERVICE_NAME}")
        cost = catalog.video_cost(model.id, params.resolution, params.seconds, inputs=len(inputs))
        return _plan(ctx, summary=summary, endpoint="/videos/generations", prompt=params.prompt, inputs=inputs, out=out,
                     cost=cost, details={"model": model.id, "seconds": params.seconds, "resolution": params.resolution,
                                         **({} if params.image else {"aspect_ratio": params.aspect_ratio}),
                                         "if_stopped": ctx.t("אם עוצרים באמצע, xAI עשוי להשלים את הסרטון אצלו (ולחייב "
                                                             "עליו), אבל JARVIS לא ישמור ממנו דבר.",
                                                             "If you stop midway, xAI may still finish the video on "
                                                             "its side (and charge for it), but JARVIS keeps nothing.")})

    async def run(self, params: CreateVideoParams, ctx: ToolContext) -> ToolResult:
        out = _out_dir(ctx, params.out_dir)
        image = await _read_picture(ctx, ctx.resolve(params.image)) if params.image else None
        references = [await _read_picture(ctx, ctx.resolve(r)) for r in params.references]
        client = xai_client(ctx.services)
        request_id = ""
        dest = out / f"{safe_name(params.name, 'video')}.mp4"
        try:
            await ctx.progress(0.02, ctx.t("שולח ל-xAI…", "Sending to xAI…"))
            request_id = await client.start_video(prompt=params.prompt, model=params.model, seconds=params.seconds,
                                                  aspect_ratio=params.aspect_ratio, resolution=params.resolution,
                                                  image=image, references=references, lang=ctx.lang)
            await ctx.progress(0.06, ctx.t("xAI התחיל להכין את הסרטון", "xAI started making the video"))
            state = await _wait_video(client, request_id, ctx)
            check_network(ctx, state.url)                 # the download follows the same network rules
            await ctx.progress(0.92, ctx.t("מוריד את הסרטון…", "Downloading the video…"))
            size = await client.download(state.url, dest, lang=ctx.lang)
        except MediaError as exc:
            return _failure(exc, client, request_id=request_id or None)
        head = await asyncio.to_thread(_head, dest)
        kind = probe(head)
        if kind is None or kind.format != "MP4":
            await asyncio.to_thread(dest.unlink, missing_ok=True)
            return ToolResult.fail(ctx.t("xAI החזיר קובץ שאינו סרטון MP4; לא נשמר דבר.",
                                         "xAI returned a file that is not an MP4 video; nothing was kept."),
                                   data={"kind": "bad_response", "requests": client.requests, "request_id": request_id})
        return ToolResult(summary=ctx.t(f"הסרטון נוצר ({_size(size)})", f"The video was created ({_size(size)})"),
                          files_created=[str(dest)],
                          data={"files": [{"path": str(dest), "format": "MP4", "mime": kind.mime, "bytes": size,
                                           "seconds": state.seconds or float(params.seconds)}],
                                "usd": state.usd, "model": params.model, "request_id": request_id,
                                "requests": client.requests})


def _head(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(64)


async def _wait_video(client: XaiMedia, request_id: str, ctx: ToolContext) -> VideoState:
    """Ask xAI about the video every few seconds until it is ready, failed, or the waiting time is over.

    Stopping the job cancels this wait: nothing more is asked or downloaded."""
    deadline = time.monotonic() + catalog.VIDEO_WAIT_S
    while True:
        await asyncio.sleep(catalog.VIDEO_POLL_S)
        ctx.check_cancelled()
        state = await client.video_state(request_id, ctx.lang)
        if state.status == "done":
            if not state.url:
                raise MediaError(ctx.t("xAI אמר שהסרטון מוכן אבל לא נתן כתובת להורדה.",
                                       "xAI said the video was ready but gave no address to download it from."),
                                 details={"kind": "bad_response"})
            return state
        if state.status in ("failed", "expired"):
            raise MediaError(state.error, details={"kind": state.status})
        share = state.progress
        shown = f" ({round(share * 100)}%)" if share is not None else ""
        await ctx.progress(0.06 + 0.84 * (share or 0.0), ctx.t(f"xAI מכין את הסרטון{shown}…", f"xAI is making the video{shown}…"))
        if time.monotonic() > deadline:
            minutes = catalog.VIDEO_WAIT_S // 60
            raise MediaError(t(ctx.lang, f"xAI לא סיים את הסרטון תוך {minutes} דקות. ייתכן שהוא עוד יסתיים אצלם, אבל "
                                         "JARVIS הפסיק לחכות ולא שמר דבר.",
                               f"xAI did not finish the video within {minutes} minutes. It may still finish on their "
                               "side, but JARVIS stopped waiting and kept nothing."), details={"kind": "timeout"})


MEDIA_TOOLS: tuple[type[Tool], ...] = (CreateImageTool, CreateVideoTool)

__all__ = ["MEDIA_TOOLS", "CreateImageTool", "CreateVideoTool", "media_folder", "xai_client"]
