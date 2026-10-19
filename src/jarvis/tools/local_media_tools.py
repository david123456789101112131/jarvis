"""Creating images and videos on this computer's own graphics card, with free open models (`jarvis.media.local`).

The "Image creation" and "Video creation" screens run these as the Creative agent, through the executor, like
the xAI tools beside them, but nothing here leaves the computer: no network, no key, no price. The plan says
so, names the model and the size, and lists the files read (a preview, or an image of the user's they picked)
and the folder written, which must be inside `<workspace>/media`.

A Hebrew description is first translated to English on this computer (the models follow English best), when
`media.translate` is on and the translation model is installed; the result says which text the model got.

No agent is offered these tools (they are in no agent's tool list): only the screens start them.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability as C
from jarvis.common.errors import ToolError
from jarvis.common.i18n import t
from jarvis.media import catalog
from jarvis.media.files import probe, safe_name
from jarvis.media.local.engine import LocalEngine, LocalMediaError
from jarvis.permissions.models import PathUse
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult
from jarvis.tools.media_tools import _out_dir

HEBREW = re.compile(r"[֐-׿]")
IMAGE_TIMEOUT_S = 30 * 60.0          # the first image also loads the model (a minute or two from a cold disk)
VIDEO_TIMEOUT_S = 90 * 60.0


def local_engine(ctx: ToolContext) -> LocalEngine:
    engine = ctx.services.extras.get("media_engine")
    if not isinstance(engine, LocalEngine):
        raise ToolError(ctx.t("יצירה במחשב לא זמינה כאן", "Creating on this computer is not available here"))
    return engine


def failure_text(lang: str, exc: LocalMediaError) -> str:
    """The engine's reason, in the user's words."""
    texts = {
        "missing": ("המודל עוד לא מותקן במחשב (אפשר להתקין עם: jarvis media install)",
                    "the model is not installed on this computer yet (install it with: jarvis media install)"),
        "start": ("המנוע המקומי לא הצליח לעלות (פרטים ביומן media-model.log)",
                  "the local engine could not start (details in media-model.log)"),
        "no_gpu": ("לא נמצא כרטיס גרפי של NVIDIA שהמודל יכול לעבוד עליו",
                   "no NVIDIA graphics card the model can work on was found"),
        "gpu_busy": ("הכרטיס הגרפי עסוק כרגע בשליטה במחשב; אפשר לנסות שוב כשהיא מסתיימת",
                     "the graphics card is busy with computer control right now; try again when it is done"),
        "oom": ("אין מספיק זיכרון פנוי בכרטיס הגרפי, גם בטעינה בחלקים. אפשר לסגור תוכנה שמשתמשת בכרטיס או "
                "לבחור גודל קטן יותר",
                "there is not enough free memory on the graphics card, even loading the model in parts. Close a "
                "program that uses the card, or choose a smaller size"),
        "crashed": ("המנוע המקומי נעצר באמצע (פרטים ביומן media-model.log)",
                    "the local engine stopped midway (details in media-model.log)"),
    }
    if exc.kind == "low_memory":
        return _low_memory(lang, exc.details)
    he, en = texts.get(exc.kind, ("היצירה במחשב נכשלה", "creating on this computer failed"))
    detail = "" if exc.kind in ("missing", "gpu_busy", "oom") else f": {str(exc)[:300]}"
    return t(lang, he, en) + detail


def _low_memory(lang: str, d: dict[str, Any]) -> str:
    if d.get("stopped"):
        return t(lang, "עצרתי את היצירה כי זיכרון המחשב (RAM) כמעט נגמר, כדי שהמחשב לא ייתקע. אפשר לסגור תוכנה "
                       "גדולה ולנסות שוב",
                 "I stopped because the computer was almost out of memory (RAM), so it would not stall. Close a big "
                 "program and try again")
    gb = {k: float(d.get(k) or 0) / 2**30 for k in ("need_ram", "free_ram", "need_vram", "free_vram")}
    return t(lang,
             f"אין עכשיו מספיק זיכרון פנוי כדי לטעון את המודל: צריך בערך {gb['need_ram']:.0f}GB של RAM "
             f"ו-{gb['need_vram']:.0f}GB בכרטיס הגרפי, ופנויים {gb['free_ram']:.1f}GB ו-{gb['free_vram']:.1f}GB. "
             "כדי לא לתקוע את המחשב לא התחלתי. אפשר לסגור תוכנה גדולה (למשל עוזר AI אחר) ולנסות שוב",
             f"there is not enough free memory to load the model right now: it needs about {gb['need_ram']:.0f} GB "
             f"of RAM and {gb['need_vram']:.0f} GB on the graphics card, and {gb['free_ram']:.1f} GB and "
             f"{gb['free_vram']:.1f} GB are free. So as not to stall the computer, I did not start. Close a big "
             "program (another AI assistant, for example) and try again")


def _size(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 100_000 else f"{max(1, round(n / 1000))} KB"


async def _english(ctx: ToolContext, engine: LocalEngine, prompt: str) -> str:
    if not (engine.settings.translate and HEBREW.search(prompt) and engine.files.has("translate")):
        return prompt
    await ctx.progress(0.02, ctx.t("מתרגם את התיאור לאנגלית, במחשב…", "Translating the description to English, "
                                                                     "on this computer…"))
    answer = await engine.request("translate", {"text": prompt})
    return str(answer.get("text") or prompt)


def _on_progress(ctx: ToolContext, start: float, end: float, *, kind: str) -> Any:
    async def report(msg: dict[str, Any]) -> None:
        phase, share = str(msg.get("phase")), float(msg.get("fraction") or 0.0)
        if phase == "load":
            note = ctx.t("טוען את המודל לכרטיס הגרפי (בפעם הראשונה זה לוקח דקה או שתיים)…",
                         "Loading the model onto the graphics card (the first time takes a minute or two)…")
            if msg.get("retry"):
                note = ctx.t("לא היה מספיק זיכרון בכרטיס; טוען שוב בחלקים…",
                             "Not enough memory on the card; loading again in parts…")
            await ctx.progress(start + 0.1 * (end - start) * share, note)
        elif phase == "generate":
            step, steps = msg.get("step"), msg.get("steps")
            if kind == "image" and msg.get("images", 1) > 1:
                note = ctx.t(f"מצייר תמונה {msg.get('image')} מתוך {msg.get('images')} (שלב {step} מתוך {steps})…",
                             f"Drawing image {msg.get('image')} of {msg.get('images')} (step {step} of {steps})…")
            elif kind == "image":
                note = ctx.t(f"מצייר (שלב {step} מתוך {steps})…", f"Drawing (step {step} of {steps})…")
            else:
                note = ctx.t(f"מכין את הסרטון (שלב {step} מתוך {steps})…", f"Making the video (step {step} of {steps})…")
            await ctx.progress(start + (end - start) * (0.1 + 0.85 * share), note)
        elif phase == "decode":
            await ctx.progress(start + (end - start) * 0.95, ctx.t("מרכיב את התמונות לסרטון…",
                                                                  "Turning the result into video frames…"))
        elif phase == "save":
            await ctx.progress(start + (end - start) * 0.97, ctx.t("שומר…", "Saving…"))

    return report


def _common_plan(ctx: ToolContext, *, summary: str, inputs: list[Path], out: Path, prompt: str,
                 details: dict[str, Any]) -> ActionPlan:
    engine = ctx.services.extras.get("media_engine")
    translate = (isinstance(engine, LocalEngine) and engine.settings.translate and bool(HEBREW.search(prompt))
                 and engine.files.has("translate"))
    return ActionPlan(
        summary=summary,
        paths=[PathUse(path=str(p), access="read") for p in inputs] + [PathUse(path=str(out), access="write")],
        details={
            "engine": "local",
            "where": ctx.t("במחשב הזה, על הכרטיס הגרפי", "on this computer, on its graphics card"),
            "sends": [],
            "nothing_sent": ctx.t("שום דבר לא יוצא מהמחשב ושום דבר לא עולה כסף",
                                  "nothing leaves the computer and nothing costs money"),
            "prompt": prompt,
            "translate": translate,
            "files": [str(p) for p in inputs],
            "saved_to": str(out),
            **details,
        },
    )


async def _input(ctx: ToolContext, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = ctx.resolve(raw)
    if not await asyncio.to_thread(path.is_file):
        raise ToolError(ctx.t(f"התמונה {path} לא נמצאה", f"The image {path} was not found"))
    head = await asyncio.to_thread(_head, path)
    kind = probe(head)
    if kind is None or kind.format not in catalog.INPUT_FORMATS:
        raise ToolError(ctx.t(f"{path.name} אינו תמונה מסוג שאפשר להשתמש בו (JPEG, PNG או WEBP)",
                              f"{path.name} is not an image that can be used (JPEG, PNG or WEBP)"))
    return path


def _head(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(64 * 1024)


def _files(paths: list[str]) -> list[dict[str, Any]]:
    """What the worker wrote, as the screens list it (the format read from the bytes, not trusted from the name)."""
    files = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            continue
        head = _head(path)
        kind = probe(head)
        if kind is None:
            continue
        files.append({"path": str(path), "format": kind.format, "mime": kind.mime, "bytes": path.stat().st_size,
                      "width": kind.width, "height": kind.height})
    return files


# ============================================================================ images
class LocalImageParams(BaseModel):
    prompt: str = Field(min_length=1, max_length=catalog.MAX_PROMPT)
    aspect_ratio: str = "1:1"
    resolution: str = "1k"
    count: int = Field(default=1, ge=1, le=catalog.MAX_IMAGES)
    source: str | None = Field(default=None, max_length=1000, description="An image to redraw (a change or variation)")
    strength: float = Field(default=0.7, ge=0.1, le=1.0, description="How much of the source is redrawn")
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    out_dir: str = Field(min_length=1, max_length=1000, description="A folder inside <workspace>/media")
    name: str = Field(default="image", max_length=120)


class CreateImageLocalTool(Tool):
    name = "create_image_local"
    description = ("Create images on this computer's graphics card with the free open model Z-Image-Turbo, from a "
                   "description (or redraw an image), into a folder inside the workspace's media folder. Nothing "
                   "leaves the computer.")
    capabilities = (C.AI_GENERATE, C.FS_WRITE)
    Params = LocalImageParams
    timeout_s = IMAGE_TIMEOUT_S

    async def plan(self, params: LocalImageParams, ctx: ToolContext) -> ActionPlan:
        out = _out_dir(ctx, params.out_dir)
        source = await _input(ctx, params.source)
        width, height = catalog.local_image_size(params.aspect_ratio, params.resolution)
        count = ctx.n(params.count, "תמונה אחת", "תמונות", "image", "images")
        summary = (ctx.t(f"לצייר מחדש את {source.name} במחשב שלך (Z-Image-Turbo, חינם; {count})",
                         f"Redraw {source.name} on this computer (Z-Image-Turbo, free; {count})") if source else
                   ctx.t(f"ליצור {count} במחשב שלך (Z-Image-Turbo, חינם)", f"Create {count} on this computer "
                                                                          "(Z-Image-Turbo, free)"))
        return _common_plan(ctx, summary=summary, inputs=[source] if source else [], out=out, prompt=params.prompt,
                            details={"model": "Z-Image-Turbo", "size": f"{width}x{height}", "count": params.count})

    async def run(self, params: LocalImageParams, ctx: ToolContext) -> ToolResult:
        out = _out_dir(ctx, params.out_dir)
        source = await _input(ctx, params.source)
        engine = local_engine(ctx)
        width, height = catalog.local_image_size(params.aspect_ratio, params.resolution)
        stem = safe_name(params.name, "image")
        outs = [str(out / f"{stem}-{i}.png") for i in range(1, params.count + 1)]
        try:
            used = await _english(ctx, engine, params.prompt)
            result = await engine.request(
                "image", {"prompt": used, "width": width, "height": height, "out": outs,
                          "steps": engine.settings.image_steps, "seed": params.seed,
                          "source": str(source) if source else None, "strength": params.strength},
                progress=_on_progress(ctx, 0.05, 0.98, kind="image"))
        except LocalMediaError as exc:
            return ToolResult.fail(failure_text(ctx.lang, exc), data={"kind": exc.kind, "engine": "local"})
        files = await asyncio.to_thread(_files, [str(f.get("path")) for f in result.get("files") or []])
        if not files:
            return ToolResult.fail(ctx.t("המודל סיים בלי ליצור תמונה; לא נשמר דבר.",
                                         "The model finished without making an image; nothing was kept."),
                                   data={"kind": "bad_response", "engine": "local"})
        made = ctx.n(len(files), "נוצרה תמונה אחת", "תמונות נוצרו", "image created", "images created")
        return ToolResult(summary=ctx.t(f"{made} במחשב ({result.get('seconds', 0):.0f} שניות)",
                                        f"{made} on this computer ({result.get('seconds', 0):.0f} s)"),
                          files_created=[f["path"] for f in files],
                          data={"files": files, "usd": 0.0, "engine": "local", "model": "Z-Image-Turbo",
                                "prompt_used": used, "translated": used != params.prompt,
                                **{k: result.get(k) for k in ("seconds", "mode", "vram_peak", "seed", "steps",
                                                              "per_image_s")}})


# ============================================================================ videos
class LocalVideoParams(BaseModel):
    prompt: str = Field(min_length=1, max_length=catalog.MAX_PROMPT)
    seconds: int = Field(default=3, ge=catalog.MIN_SECONDS, le=catalog.MAX_SECONDS)
    aspect_ratio: str = "16:9"
    resolution: str = "480p"
    image: str | None = Field(default=None, max_length=1000, description="The first frame")
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    out_dir: str = Field(min_length=1, max_length=1000, description="A folder inside <workspace>/media")
    name: str = Field(default="video", max_length=120)


class CreateVideoLocalTool(Tool):
    name = "create_video_local"
    description = ("Create a short video on this computer's graphics card with the free open model Wan 2.2 "
                   "TI2V-5B, from a description (optionally from a first frame), into a folder inside the "
                   "workspace's media folder. Nothing leaves the computer.")
    capabilities = (C.AI_GENERATE, C.FS_WRITE)
    Params = LocalVideoParams
    timeout_s = VIDEO_TIMEOUT_S

    async def plan(self, params: LocalVideoParams, ctx: ToolContext) -> ActionPlan:
        out = _out_dir(ctx, params.out_dir)
        image = await _input(ctx, params.image)
        width, height = catalog.local_video_size(params.aspect_ratio, params.resolution)
        seconds = ctx.n(params.seconds, "שנייה אחת", "שניות", "second", "seconds")
        summary = ctx.t(f"ליצור סרטון של {seconds} במחשב שלך (Wan 2.2, חינם)",
                        f"Create a {params.seconds}-second video on this computer (Wan 2.2, free)")
        return _common_plan(ctx, summary=summary, inputs=[image] if image else [], out=out, prompt=params.prompt,
                            details={"model": "Wan 2.2 TI2V-5B", "size": f"{width}x{height}",
                                     "seconds": params.seconds, "frames": catalog.local_frames(params.seconds)})

    async def run(self, params: LocalVideoParams, ctx: ToolContext) -> ToolResult:
        out = _out_dir(ctx, params.out_dir)
        image = await _input(ctx, params.image)
        engine = local_engine(ctx)
        width, height = catalog.local_video_size(params.aspect_ratio, params.resolution)
        dest = out / f"{safe_name(params.name, 'video')}.mp4"
        try:
            used = await _english(ctx, engine, params.prompt)
            result = await engine.request(
                "video", {"prompt": used, "width": width, "height": height, "out": str(dest),
                          "frames": catalog.local_frames(params.seconds), "fps": catalog.LOCAL_FPS,
                          "steps": engine.settings.video_steps, "seed": params.seed,
                          "image": str(image) if image else None},
                progress=_on_progress(ctx, 0.05, 0.98, kind="video"))
        except LocalMediaError as exc:
            return ToolResult.fail(failure_text(ctx.lang, exc), data={"kind": exc.kind, "engine": "local"})
        files = [f for f in await asyncio.to_thread(_files, [str(dest)]) if f["format"] == "MP4"]
        if not files:
            await asyncio.to_thread(dest.unlink, missing_ok=True)
            return ToolResult.fail(ctx.t("המודל סיים בלי ליצור סרטון MP4; לא נשמר דבר.",
                                         "The model finished without making an MP4 video; nothing was kept."),
                                   data={"kind": "bad_response", "engine": "local"})
        made = (result.get("files") or [{}])[0]
        files[0].update(seconds=made.get("seconds") or float(params.seconds), width=made.get("width"),
                        height=made.get("height"))
        took = float(result.get("seconds") or 0)
        return ToolResult(summary=ctx.t(f"הסרטון נוצר במחשב ({_size(files[0]['bytes'])}, {took / 60:.1f} דקות)",
                                        f"The video was made on this computer ({_size(files[0]['bytes'])}, "
                                        f"{took / 60:.1f} min)"),
                          files_created=[str(dest)],
                          data={"files": files, "usd": 0.0, "engine": "local", "model": "Wan 2.2 TI2V-5B",
                                "prompt_used": used, "translated": used != params.prompt,
                                **{k: result.get(k) for k in ("seconds", "mode", "vram_peak", "seed", "steps",
                                                              "generate_s")}})


LOCAL_MEDIA_TOOLS: tuple[type[Tool], ...] = (CreateImageLocalTool, CreateVideoLocalTool)

__all__ = ["LOCAL_MEDIA_TOOLS", "CreateImageLocalTool", "CreateVideoLocalTool", "failure_text", "local_engine"]
