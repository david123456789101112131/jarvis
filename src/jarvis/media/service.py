"""The media service: what the "Image creation" and "Video creation" screens do, each action a job.

Creating runs the `create_image` / `create_video` tools through the executor as the Creative agent, so the
permission check and the approval (whenever one is due) are those of any agent action, and nothing reaches xAI
or the disk another way. Previews land in `<workspace>/media/<job id>/`. Saving copies a preview to the place the
user picked, with `copy_path` through the executor, under a one-use permission for exactly that file that is
withdrawn as soon as the copy is done, refused or stopped.

When xAI is not connected (no key, or the network is off), the screens offer a demonstration instead: the same
steps, marked as not real, and a placeholder that says so in the picture itself. A demonstration sends nothing
anywhere and cannot be saved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from jarvis.common.enums import Capability
from jarvis.common.errors import JarvisError, ToolError
from jarvis.common.i18n import plural, t
from jarvis.jobs import Job, JobContext
from jarvis.media import catalog, demo
from jarvis.media.files import safe_name, with_format
from jarvis.media.store import MediaItem, MediaStore
from jarvis.security.path_guard import normalize_path
from jarvis.tools.base import ToolResult
from jarvis.tools.media_tools import media_folder

if TYPE_CHECKING:
    from jarvis.app import JarvisApp

Lang = Literal["he", "en"]


class MediaRequestError(JarvisError):
    """A request the screens should not have sent, or one that needs the user first (`status` is the HTTP one)."""

    code = "media_request"

    def __init__(self, status: int, message: str, *, code: str | None = None, **details: Any) -> None:
        super().__init__(message, details=details)
        self.status = status
        if code:
            self.code = code


# ============================================================================ what the screens send
def _check(value: str, allowed: tuple[str, ...] | set[str], what: str) -> str:
    if value not in allowed:
        raise ValueError(f"unknown {what} {value!r}")
    return value


class ImageRequest(BaseModel):
    description: str = Field(min_length=1, max_length=catalog.MAX_PROMPT)
    style: str = "none"
    model: str = catalog.DEFAULT_IMAGE_MODEL
    count: int = Field(default=1, ge=1, le=catalog.MAX_IMAGES)
    aspect_ratio: str = "1:1"
    resolution: str = "1k"
    source_item: str | None = Field(default=None, max_length=80)     # a preview to change
    source_path: str | None = Field(default=None, max_length=1000)   # an image of the user's, picked on the screen
    demo: bool = False
    lang: Lang | None = None

    @field_validator("style")
    @classmethod
    def _style(cls, v: str) -> str:
        return _check(v, {s.id for s in catalog.STYLES}, "style")

    @field_validator("model")
    @classmethod
    def _model(cls, v: str) -> str:
        return _check(v, {m.id for m in catalog.IMAGE_MODELS}, "image model")

    @field_validator("aspect_ratio")
    @classmethod
    def _aspect(cls, v: str) -> str:
        return _check(v, catalog.IMAGE_ASPECTS, "aspect ratio")

    @field_validator("resolution")
    @classmethod
    def _resolution(cls, v: str) -> str:
        return _check(v, catalog.IMAGE_RESOLUTIONS, "resolution")

    @model_validator(mode="after")
    def _one_source(self) -> ImageRequest:
        if self.source_item and self.source_path:
            raise ValueError("send one image to change: a preview or a file, not both")
        return self


class RefineImageRequest(BaseModel):
    item_id: str = Field(min_length=1, max_length=80)
    change: str = Field(default="", max_length=catalog.MAX_PROMPT)
    variation: bool = False                  # a variation of the image instead of a change to it
    count: int = Field(default=1, ge=1, le=catalog.MAX_IMAGES)
    lang: Lang | None = None

    @model_validator(mode="after")
    def _says_what(self) -> RefineImageRequest:
        if not self.variation and not self.change.strip():
            raise ValueError("say what to change, or ask for a variation")
        return self


class VideoRequest(BaseModel):
    scene: str = Field(min_length=1, max_length=catalog.MAX_PROMPT)
    characters: str = Field(default="", max_length=1000)
    motion: str = Field(default="", max_length=1000)
    lighting: str = Field(default="", max_length=1000)
    style: str = Field(default="", max_length=1000)
    model: str = catalog.DEFAULT_VIDEO_MODEL
    seconds: int = Field(default=catalog.DEFAULT_SECONDS, ge=catalog.MIN_SECONDS, le=catalog.MAX_SECONDS)
    aspect_ratio: str = "16:9"
    resolution: str = "480p"
    image_item: str | None = Field(default=None, max_length=80)       # a preview image as the first frame
    image_path: str | None = Field(default=None, max_length=1000)     # an image of the user's as the first frame
    reference_items: list[str] = Field(default_factory=list, max_length=catalog.MAX_REFERENCES)
    reference_paths: list[str] = Field(default_factory=list, max_length=catalog.MAX_REFERENCES)
    demo: bool = False
    lang: Lang | None = None

    @field_validator("model")
    @classmethod
    def _model(cls, v: str) -> str:
        return _check(v, {m.id for m in catalog.VIDEO_MODELS}, "video model")

    @field_validator("aspect_ratio")
    @classmethod
    def _aspect(cls, v: str) -> str:
        return _check(v, catalog.VIDEO_ASPECTS, "aspect ratio")

    @field_validator("resolution")
    @classmethod
    def _resolution(cls, v: str) -> str:
        return _check(v, catalog.VIDEO_RESOLUTIONS, "resolution")

    @model_validator(mode="after")
    def _fits(self) -> VideoRequest:
        refs = len(self.reference_items) + len(self.reference_paths)
        if self.image_item and self.image_path:
            raise ValueError("choose one first frame: a preview or a file, not both")
        if refs > catalog.MAX_REFERENCES:
            raise ValueError(f"at most {catalog.MAX_REFERENCES} reference images")
        if refs:
            if not catalog.video_model(self.model).references:
                raise ValueError("reference images need the full video model")
            if self.resolution not in catalog.REFERENCE_RESOLUTIONS:
                raise ValueError("a video from reference images goes up to 720p")
            if self.image_item or self.image_path:
                raise ValueError("send either a first frame or reference images, not both")
        return self

    def parts(self) -> dict[str, str]:
        return {"scene": self.scene, "characters": self.characters, "motion": self.motion,
                "lighting": self.lighting, "style": self.style}


class RefineVideoRequest(BaseModel):
    item_id: str = Field(min_length=1, max_length=80)
    change: str = Field(min_length=1, max_length=catalog.MAX_PROMPT)
    lang: Lang | None = None


class SaveRequest(BaseModel):
    item_id: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=1000)     # the full path the user picked
    overwrite: bool = False
    lang: Lang | None = None


# ============================================================================ the service
class MediaService:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app
        self.store = MediaStore(app.paths.data / "media_items.json", media_folder(app.services))
        app.services.extras["media"] = self

    # ------------------------------------------------------------------ status
    def _lang(self, lang: str | None) -> str:
        return lang if lang in ("he", "en") else self.app.lang

    def _connection(self) -> tuple[bool, bool, bool]:
        """(a key is saved, the network is on, api.x.ai is allowed by the allowlist)."""
        security = self.app.settings.security
        allow = security.network_allowlist
        host = catalog.SERVICE_HOST
        return (self.app.secrets.has(catalog.KEY_NAME), security.network_enabled,
                not allow or any(host == d or host.endswith("." + d) for d in allow))

    def available(self) -> bool:
        return all(self._connection())

    def status(self, lang: str | None = None) -> dict[str, Any]:
        lang = self._lang(lang)
        key, network, allowed = self._connection()
        missing: list[str] = []
        if not key:
            missing.append(t(lang, f"מפתח API של xAI ({catalog.KEY_NAME}): הגדרות ← מפתחות API",
                             f"An xAI API key ({catalog.KEY_NAME}): Settings → API keys"))
        if not network:
            missing.append(t(lang, "גישה לרשת: היא כבויה בהגדרות האבטחה", "Network access: it is off in the security settings"))
        elif not allowed:
            missing.append(t(lang, f"להוסיף את {catalog.SERVICE_HOST} לרשימת האתרים המותרים",
                             f"Add {catalog.SERVICE_HOST} to the network allowlist"))
        available = key and network and allowed
        return {
            "available": available,
            "xai": key,
            "network": network and allowed,
            "demo": not available,
            "service": catalog.SERVICE_NAME,
            "host": catalog.SERVICE_HOST,
            "key_name": catalog.KEY_NAME,
            "missing": missing,
            "reason": None if available else t(
                lang, f"יצירה אמיתית של תמונות וסרטונים צריכה את {catalog.SERVICE_NAME}, והוא לא מחובר כרגע.",
                f"Creating real images and videos needs {catalog.SERVICE_NAME}, and it is not connected right now."),
            "offer": catalog.offer(lang),
            "media_dir": str(self.store.folder),
            "counts": {kind: len(self.store.items(kind, limit=10_000)) for kind in ("image", "video")},
        }

    def _require(self, lang: str) -> None:
        if not self.available():
            missing = self.status(lang)["missing"]
            raise MediaRequestError(409, t(lang, "xAI לא מחובר, ולכן אין יצירה אמיתית. חסר: ", "xAI is not connected, so "
                                                 "nothing real can be created. Missing: ") + "; ".join(missing),
                                    code="unavailable")

    # ------------------------------------------------------------------ the previews
    def items(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [i.view() for i in self.store.items(kind)]

    def file(self, item_id: str) -> MediaItem | None:
        return self.store.file(item_id)

    async def delete(self, item_id: str) -> bool:
        return await asyncio.to_thread(self.store.remove, item_id)

    async def clear(self, kind: str | None = None) -> int:
        return await asyncio.to_thread(self.store.clear, kind)

    def _item(self, item_id: str | None, lang: str, kind: str = "image") -> MediaItem:
        item = self.store.file(item_id or "")
        if item is None:
            raise MediaRequestError(404, t(lang, "התצוגה המקדימה הזו כבר לא קיימת.", "That preview no longer exists."),
                                    code="not_found")
        if item.kind != kind:
            raise MediaRequestError(400, t(lang, "זה לא הסוג הנכון של תצוגה מקדימה לפעולה הזו.",
                                           "That preview is the wrong kind for this action."))
        return item

    def _user_file(self, raw: str | None, lang: str) -> Path | None:
        if not raw:
            return None
        if not Path(raw.strip().strip('"')).is_absolute():
            raise MediaRequestError(400, t(lang, "צריך לבחור קובץ עם נתיב מלא.", "Choose a file with its full path."))
        path = normalize_path(raw)
        if not path.is_file():
            raise MediaRequestError(400, t(lang, f"הקובץ {path} לא נמצא.", f"The file {path} was not found."))
        return path

    def _input(self, item_id: str | None, raw: str | None, lang: str) -> tuple[Path | None, str | None]:
        """An image to send along: (its path, its item id when it is one of the previews)."""
        if item_id:
            item = self._item(item_id, lang)
            if not item.real:
                raise MediaRequestError(400, t(lang, "אי אפשר לשלוח תמונת הדגמה; צריך תמונה אמיתית.",
                                               "A demonstration placeholder cannot be sent; choose a real image."))
            return Path(item.path), item.id
        return self._user_file(raw, lang), None

    # ------------------------------------------------------------------ images
    async def create_image(self, req: ImageRequest) -> Job:
        lang = self._lang(req.lang)
        real = not req.demo
        if real:
            self._require(lang)
        source, source_item = self._input(req.source_item, req.source_path, lang) if real else (None, None)
        prompt = catalog.image_prompt(req.description, req.style)
        return await self._image_job(
            kind="image.create", lang=lang, real=real, prompt=prompt, description=req.description.strip(),
            model=req.model, count=req.count, aspect=req.aspect_ratio, resolution=req.resolution, style=req.style,
            source=source, parent=source_item, changes=[],
            params=req.model_dump(exclude={"lang"}),
        )

    async def refine_image(self, req: RefineImageRequest) -> Job:
        lang = self._lang(req.lang)
        item = self._item(req.item_id, lang)
        real = item.real
        if real:
            self._require(lang)
        base = item.description or item.prompt
        prompt = catalog.variation_prompt(base) if req.variation else catalog.change_prompt(base, req.change)
        p = item.params
        return await self._image_job(
            kind="image.refine", lang=lang, real=real, prompt=prompt, description=base, model=str(p.get("model") or
            catalog.DEFAULT_IMAGE_MODEL), count=req.count, aspect=str(p.get("aspect_ratio") or "1:1"),
            resolution=str(p.get("resolution") or "1k"), style=str(p.get("style") or "none"),
            source=Path(item.path) if real else None, parent=item.id,
            changes=[*p.get("changes", []), req.change.strip() if not req.variation else "(variation)"],
            params=req.model_dump(exclude={"lang"}), variation=req.variation, change=req.change.strip(),
        )

    async def _image_job(self, *, kind: str, lang: str, real: bool, prompt: str, description: str, model: str,
                         count: int, aspect: str, resolution: str, style: str, source: Path | None, parent: str | None,
                         changes: list[str], params: dict[str, Any], variation: bool = False,
                         change: str = "") -> Job:
        if len(prompt) > catalog.MAX_PROMPT:
            raise MediaRequestError(400, t(lang, "התיאור ארוך מדי.", "The description is too long."))
        info = catalog.image_model(model)
        label = info.label_he if lang == "he" else info.label_en
        many = plural(lang, count, "תמונה אחת", "תמונות", "image", "images")
        estimate = catalog.image_cost(model, resolution, count, inputs=1 if source else 0)
        style_label = catalog.style(style)
        style_name = style_label.label_he if lang == "he" else style_label.label_en
        clip = description if len(description) <= 80 else description[:79] + "…"
        if kind == "image.refine":
            title = t(lang, f"וריאציה של התמונה ({many})", f"A variation of the image ({many})") if variation else \
                t(lang, f"לשנות את התמונה: «{change[:80]}»", f"Change the image: “{change[:80]}”")
        else:
            title = t(lang, f"ליצור {many}: «{clip}»", f"Create {many}: “{clip}”")
        if real:
            what = t(lang, " ואת התמונה " + source.name if source else "", " and the image " + source.name if source else "")
            shape = "" if source else f", {aspect}, {resolution}"
            plan = [
                t(lang, f"לשלוח ל-{catalog.SERVICE_NAME} את התיאור{what} וליצור {many} (מודל {label}{shape})",
                  f"Send the description{what} to {catalog.SERVICE_NAME} and create {many} ({label} model{shape})"),
                t(lang, "לשמור תצוגה מקדימה בתיקיית העבודה של JARVIS; אצלך לא נשמר דבר עד שתלחץ שמירה ותבחר מקום",
                  "Keep a preview in JARVIS's workspace; nothing is saved on your computer until you press Save "
                  "and choose where"),
            ]
            if source is not None and not self.store.inside(source):
                plan.append(t(lang, f"לפני השליחה לבקש את אישורך, כי נשלח קובץ שלך ({source.name})",
                              f"Ask for your approval first, because a file of yours is sent ({source.name})"))
            sends = [t(lang, f"התיאור ({len(prompt)} תווים) ← {catalog.SERVICE_NAME}, {catalog.SERVICE_HOST}",
                       f"the description ({len(prompt)} characters) → {catalog.SERVICE_NAME}, {catalog.SERVICE_HOST}")]
            if source is not None:
                sends.append(t(lang, f"התמונה {source.name} ← xAI", f"the image {source.name} → xAI"))
            cost: str | None = f"≈ {catalog.usd(estimate)}"
        else:
            stand_ins = plural(lang, count, "תמונת דוגמה אחת", "תמונות דוגמה", "placeholder", "placeholders")
            plan = [t(lang, "הדגמה: לא נשלח דבר ל-xAI ולא נוצרת תמונה אמיתית",
                      "Demonstration: nothing is sent to xAI and no real image is created"),
                    t(lang, f"להכין במקום זה {stand_ins} עם הכיתוב «{demo.DEMO_IMAGE_HE}»",
                      f"Make {stand_ins} marked “{demo.DEMO_IMAGE_EN}” instead")]
            sends, cost = [], None
        steps = [("prepare", t(lang, "הכנת הבקשה", "Preparing the request")),
                 ("create", t(lang, "שליחה ל-xAI ויצירה", "Sending to xAI and creating")),
                 ("preview", t(lang, "שמירת תצוגה מקדימה", "Keeping a preview"))]
        area_params = {**params, "real": real}

        async def run(ctx: JobContext) -> dict[str, Any]:
            folder = self.store.job_folder(ctx.job.id)
            async with ctx.step("prepare"):
                if source is not None and not await asyncio.to_thread(source.is_file):
                    raise ToolError(ctx.t(f"התמונה {source} כבר לא קיימת.", f"The image {source} no longer exists."))
            usd: float | None = None
            if real:
                async with ctx.step("create"):
                    result = await self.app.executor.execute(
                        "create_image", {"prompt": prompt, "model": model, "count": count, "aspect_ratio": aspect,
                                         "resolution": resolution, "source": str(source) if source else None,
                                         "out_dir": str(folder), "name": safe_name(description[:40], "image")},
                        agent_id="creative", task_id=ctx.job.id, cancel=ctx.cancel, lang=lang,
                        progress=_scaled(ctx, 0.05, 0.9))
                    _raise_for(result)
                data = result.data or {}
                files = list(data.get("files") or [])
                usd = data.get("usd")
            else:
                files = []
                await ctx.skip("create", ctx.t("הדגמה: לא נשלח דבר", "Demonstration: nothing was sent"))
            async with ctx.step("preview"):
                if not real:
                    for index in range(1, count + 1):
                        svg = demo.image_svg(description, aspect=aspect, style_label=style_name, index=index, count=count)
                        path = await self._write_demo(ctx, folder / f"demo-{index}.svg", svg, lang)
                        files.append({"path": str(path), "format": "SVG", "mime": "image/svg+xml",
                                      "bytes": len(svg.encode("utf-8"))})
                share = usd / len(files) if usd is not None and files else None
                items = self.store.add(MediaItem(
                    kind="image", job_id=ctx.job.id, path=f["path"], format=f["format"], mime=f["mime"],
                    bytes=int(f.get("bytes") or 0), width=f.get("width"), height=f.get("height"), real=real,
                    description=description, prompt=prompt, usd=share, cost=cost, parent=parent,
                    params={"model": model, "aspect_ratio": aspect, "resolution": resolution, "style": style,
                            "changes": changes, "source": str(source) if source else None},
                ) for f in files)
            done = plural(lang, len(items), "תמונה אחת מוכנה", "תמונות מוכנות", "image ready", "images ready")
            summary = done if real else t(lang, f"הדגמה מוכנה ({done}) — לא נוצרה תמונה אמיתית",
                                          f"Demonstration ready ({done}) — no real image was created")
            return {"summary": summary, "items": [i.view() for i in items], "usd": usd, "estimated": cost, "real": real}

        return await self.app.jobs.start(kind=kind, area="images", title=title, run=run, plan=plan, steps=steps,
                                         params=area_params, real=real, sends=sends, cost=cost, lang=lang)

    # ------------------------------------------------------------------ videos
    async def create_video(self, req: VideoRequest) -> Job:
        lang = self._lang(req.lang)
        real = not req.demo
        if real:
            self._require(lang)
        image, image_item = self._input(req.image_item, req.image_path, lang) if real else (None, None)
        references: list[Path] = []
        if real:
            for ref in req.reference_items:
                path, _ = self._input(ref, None, lang)
                if path is not None:
                    references.append(path)
            references += [p for p in (self._user_file(r, lang) for r in req.reference_paths) if p is not None]
        prompt = catalog.video_prompt(**req.parts())
        return await self._video_job(kind="video.create", lang=lang, real=real, prompt=prompt, parts=req.parts(),
                                     model=req.model, seconds=req.seconds, aspect=req.aspect_ratio,
                                     resolution=req.resolution, image=image, references=references,
                                     parent=image_item, params=req.model_dump(exclude={"lang"}))

    async def refine_video(self, req: RefineVideoRequest) -> Job:
        lang = self._lang(req.lang)
        item = self._item(req.item_id, lang, kind="video")
        if item.real:
            self._require(lang)
        p = item.params
        # The same first frame or reference images as the video being changed, while they are still there.
        image = next(iter(_existing([p["image"]] if item.real and p.get("image") else [])), None)
        references = _existing(p.get("references") or []) if item.real else []
        parts = dict(p.get("parts") or {"scene": item.description})
        prompt = catalog.video_change_prompt(item.prompt, req.change)
        return await self._video_job(kind="video.refine", lang=lang, real=item.real, prompt=prompt, parts=parts,
                                     model=str(p.get("model") or catalog.DEFAULT_VIDEO_MODEL),
                                     seconds=int(p.get("seconds") or catalog.DEFAULT_SECONDS),
                                     aspect=str(p.get("aspect_ratio") or "16:9"), resolution=str(p.get("resolution") or "480p"),
                                     image=image, references=references, parent=item.id,
                                     params=req.model_dump(exclude={"lang"}), change=req.change.strip())

    async def _video_job(self, *, kind: str, lang: str, real: bool, prompt: str, parts: dict[str, str], model: str,
                         seconds: int, aspect: str, resolution: str, image: Path | None, references: list[Path],
                         parent: str | None, params: dict[str, Any], change: str = "") -> Job:
        if len(prompt) > catalog.MAX_PROMPT:
            raise MediaRequestError(400, t(lang, "התיאור ארוך מדי.", "The description is too long."))
        info = catalog.video_model(model)
        label = info.label_he if lang == "he" else info.label_en
        inputs = [*([image] if image else []), *references]
        estimate = catalog.video_cost(model, resolution, seconds, inputs=len(inputs))
        length = plural(lang, seconds, "שנייה אחת", "שניות", "second", "seconds")
        scene = parts.get("scene", "").strip()
        clip = scene if len(scene) <= 80 else scene[:79] + "…"
        title = t(lang, f"לשנות את הסרטון: «{change[:80]}»", f"Change the video: “{change[:80]}”") if kind == "video.refine" \
            else t(lang, f"ליצור סרטון של {length}: «{clip}»", f"Create a {seconds}-second video: “{clip}”")
        stop_note = t(lang, "אם תעצור באמצע, xAI עשוי להשלים את הסרטון אצלו (ולחייב עליו), אבל אצלך לא יישמר ממנו דבר",
                      "If you stop midway, xAI may still finish the video on its side (and charge for it), but nothing "
                      "of it is kept on your computer")
        if real:
            sent = "".join(t(lang, f" ואת {p.name}", f" and {p.name}") for p in inputs)
            plan = [
                t(lang, f"לשלוח ל-{catalog.SERVICE_NAME} את התיאור{sent} וליצור סרטון של {length} "
                        f"(מודל {label}, {resolution}{'' if image else ', ' + aspect})",
                  f"Send the description{sent} to {catalog.SERVICE_NAME} and create a {seconds}-second video "
                  f"({label} model, {resolution}{'' if image else ', ' + aspect})"),
                t(lang, "לחכות עד שהסרטון מוכן (בדרך כלל כמה דקות) ולהוריד אותו לתיקיית העבודה של JARVIS",
                  "Wait until the video is ready (usually a few minutes) and download it into JARVIS's workspace"),
                stop_note,
            ]
            uploads = [p for p in inputs if not self.store.inside(p)]
            if uploads:
                plan.append(t(lang, f"לפני השליחה לבקש את אישורך, כי נשלח קובץ שלך ({', '.join(p.name for p in uploads)})",
                              f"Ask for your approval first, because a file of yours is sent "
                              f"({', '.join(p.name for p in uploads)})"))
            sends = [t(lang, f"התיאור ({len(prompt)} תווים) ← {catalog.SERVICE_NAME}, {catalog.SERVICE_HOST}",
                       f"the description ({len(prompt)} characters) → {catalog.SERVICE_NAME}, {catalog.SERVICE_HOST}"),
                     *(t(lang, f"התמונה {p.name} ← xAI", f"the image {p.name} → xAI") for p in inputs)]
            cost: str | None = f"≈ {catalog.usd(estimate)}"
        else:
            plan = [t(lang, "הדגמה: לא נשלח דבר ל-xAI ולא נוצר סרטון אמיתי",
                      "Demonstration: nothing is sent to xAI and no real video is created"),
                    t(lang, "להכין במקום זה לוח תכנון (סטוריבורד) של התיאור, שכתוב עליו שהוא הדגמה",
                      "Make a storyboard of the description instead, marked as a demonstration")]
            sends, cost = [], None
        steps = [("prepare", t(lang, "הכנת הבקשה", "Preparing the request")),
                 ("create", t(lang, "יצירת הסרטון ב-xAI: שליחה, המתנה והורדה", "Making the video at xAI: send, wait, download")),
                 ("preview", t(lang, "שמירת תצוגה מקדימה", "Keeping a preview"))]
        labels = {key: t(lang, he, en) for key, he, en in (
            ("scene", "סצנה", "Scene"), ("characters", "דמויות", "Characters"), ("motion", "תנועה", "Motion"),
            ("lighting", "תאורה", "Lighting"), ("style", "סגנון", "Style"))}

        async def run(ctx: JobContext) -> dict[str, Any]:
            folder = self.store.job_folder(ctx.job.id)
            async with ctx.step("prepare"):
                for path in inputs:
                    if not await asyncio.to_thread(path.is_file):
                        raise ToolError(ctx.t(f"התמונה {path} כבר לא קיימת.", f"The image {path} no longer exists."))
            usd: float | None = None
            if real:
                async with ctx.step("create"):
                    result = await self.app.executor.execute(
                        "create_video", {"prompt": prompt, "model": model, "seconds": seconds, "aspect_ratio": aspect,
                                         "resolution": resolution, "image": str(image) if image else None,
                                         "references": [str(p) for p in references], "out_dir": str(folder),
                                         "name": safe_name(scene[:40], "video")},
                        agent_id="creative", task_id=ctx.job.id, cancel=ctx.cancel, lang=lang,
                        progress=_scaled(ctx, 0.05, 0.95))
                    _raise_for(result)
                data = result.data or {}
                files = list(data.get("files") or [])
                usd = data.get("usd")
            else:
                files = []
                await ctx.skip("create", ctx.t("הדגמה: לא נשלח דבר", "Demonstration: nothing was sent"))
            async with ctx.step("preview"):
                if not real:
                    svg = demo.storyboard_svg(parts, labels=labels, aspect=aspect, seconds=seconds)
                    path = await self._write_demo(ctx, folder / "storyboard.svg", svg, lang)
                    files = [{"path": str(path), "format": "SVG", "mime": "image/svg+xml",
                              "bytes": len(svg.encode("utf-8")), "seconds": None}]
                items = self.store.add(MediaItem(
                    kind="video", job_id=ctx.job.id, path=f["path"], format=f["format"], mime=f["mime"],
                    bytes=int(f.get("bytes") or 0), seconds=f.get("seconds"), real=real, description=scene,
                    prompt=prompt, usd=usd, cost=cost, parent=parent,
                    params={"model": model, "seconds": seconds, "aspect_ratio": aspect, "resolution": resolution,
                            "parts": parts, "image": str(image) if image else None,
                            "references": [str(p) for p in references]},
                ) for f in files)
            summary = t(lang, "הסרטון מוכן", "The video is ready") if real else t(
                lang, "הדגמה מוכנה — לא נוצר סרטון אמיתי", "Demonstration ready — no real video was created")
            return {"summary": summary, "items": [i.view() for i in items], "usd": usd, "estimated": cost, "real": real}

        return await self.app.jobs.start(kind=kind, area="video", title=title, run=run, plan=plan, steps=steps,
                                         params={**params, "real": real}, real=real, sends=sends, cost=cost, lang=lang)

    # ------------------------------------------------------------------ saving
    def save_target(self, req: SaveRequest) -> tuple[MediaItem, Path, bool]:
        """The preview and the file it would be saved as (the extension follows the real format), checked."""
        lang = self._lang(req.lang)
        item = self.store.file(req.item_id)
        if item is None:
            raise MediaRequestError(404, t(lang, "התצוגה המקדימה הזו כבר לא קיימת.", "That preview no longer exists."),
                                    code="not_found")
        if not item.real:
            raise MediaRequestError(409, t(lang, "זו הדגמה: אין כאן תמונה או סרטון אמיתיים לשמור.",
                                           "This is a demonstration: there is no real image or video to save."),
                                    code="demo")
        raw = req.target.strip().strip('"')
        if not Path(raw).is_absolute():
            raise MediaRequestError(400, t(lang, "צריך לבחור לאן לשמור, עם נתיב מלא.", "Choose where to save, with a full path."))
        final, renamed = with_format(normalize_path(raw), item.format)
        if final.is_dir():
            raise MediaRequestError(400, t(lang, f"{final} היא תיקייה; צריך לבחור שם קובץ.",
                                           f"{final} is a folder; choose a file name."))
        if final.exists() and not req.overwrite:
            raise MediaRequestError(409, t(lang, f"הקובץ {final} כבר קיים. להחליף אותו?", f"{final} already exists. Replace it?"),
                                    code="exists", path=str(final), renamed=renamed)
        return item, final, renamed

    async def save(self, req: SaveRequest) -> Job:
        lang = self._lang(req.lang)
        item, final, renamed = self.save_target(req)
        what = t(lang, "התמונה" if item.kind == "image" else "הסרטון", "the image" if item.kind == "image" else "the video")
        plan = [t(lang, f"להעתיק את {what} ({Path(item.path).name}) אל {final}", f"Copy {what} ({Path(item.path).name}) to {final}")]
        if renamed:
            plan.append(t(lang, f"הסיומת נקבעה ל-{final.suffix} כי זה הפורמט האמיתי של הקובץ ({item.format})",
                          f"The extension is {final.suffix} because that is the file's real format ({item.format})"))
        if req.overwrite:
            plan.append(t(lang, f"להחליף את הקובץ הקיים {final.name}, כפי שאישרת", f"Replace the existing {final.name}, as you confirmed"))
        plan.append(t(lang, "JARVIS מקבל הרשאה חד־פעמית לכתוב רק את הקובץ הזה, והיא מבוטלת מיד אחרי",
                      "JARVIS gets a one-time permission to write only this file, withdrawn right after"))
        title = t(lang, f"לשמור את {what} ב-{final}", f"Save {what} to {final}")
        label = t(lang, "העתקה למקום שבחרת", "Copying to the place you chose")

        async def run(ctx: JobContext) -> dict[str, Any]:
            async with ctx.step("save"):
                grant = self.app.permissions.grant(
                    "creative", Capability.FS_WRITE, roots=[str(final)], task_id=ctx.job.id, max_uses=1, source="user",
                    reason=t(lang, f"שמירה שביקשת במסך: {final}", f"A save you asked for on the screen: {final}"))
                try:
                    result = await self.app.executor.execute(
                        "copy_path", {"source": item.path, "destination": str(final), "overwrite": req.overwrite},
                        agent_id="creative", task_id=ctx.job.id, cancel=ctx.cancel, lang=lang)
                finally:
                    self.app.permissions.revoke(grant.id)
                _raise_for(result)
            self.store.mark_saved(item.id, str(final))
            return {"summary": t(lang, f"נשמר ב-{final}", f"Saved to {final}"), "path": str(final), "renamed": renamed,
                    "item_id": item.id}

        return await self.app.jobs.start(kind=f"{item.kind}.save", area="images" if item.kind == "image" else "video",
                                         title=title, run=run, plan=plan, steps=[("save", label)],
                                         params=req.model_dump(exclude={"lang"}), real=True, lang=lang)

    # ------------------------------------------------------------------ helpers
    async def _write_demo(self, ctx: JobContext, path: Path, svg: str, lang: str) -> Path:
        """A demonstration placeholder, written into the workspace like any file an agent writes."""
        result = await self.app.executor.execute("write_file", {"path": str(path), "content": svg, "mode": "overwrite"},
                                                 agent_id="creative", task_id=ctx.job.id, cancel=ctx.cancel, lang=lang)
        _raise_for(result)
        return path

    async def aclose(self) -> None:
        return None


def _existing(paths: list[str]) -> list[Path]:
    return [Path(x) for x in paths if Path(x).is_file()]


def _scaled(ctx: JobContext, start: float, end: float) -> Callable[[float, str], Awaitable[None]]:
    """A tool's progress (0..1) as the job's, between `start` and `end`."""
    def report(fraction: float, message: str) -> Awaitable[None]:
        return ctx.progress(start + (end - start) * fraction, message)

    return report


def _raise_for(result: ToolResult) -> None:
    """A tool call that did not work ends the job with its reason (already in the user's language)."""
    if not result.ok:
        raise ToolError(result.error or result.summary or "failed", details={"tool": result.tool,
                                                                           "denied": result.denied,
                                                                           **(result.data if isinstance(result.data, dict) else {})})


__all__ = ["ImageRequest", "MediaRequestError", "MediaService", "RefineImageRequest", "RefineVideoRequest",
           "SaveRequest", "VideoRequest"]
