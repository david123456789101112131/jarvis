"""Creative tools: brand kits, logos, palettes, icons, UI mockups, animations, banners and
presentations made by JARVIS's own design library (`jarvis.creative`) and saved as files.

The generators are deterministic code, not an AI model, so they work offline. Every call writes
into one folder that is declared in the plan, so the permission manager checks it like any other
write; existing files are never overwritten (a clash gets a numbered name).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.permissions.models import PathUse
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult

C = Capability
DesignKind = Literal["brand_kit", "logo", "palette", "icons", "mockup", "animation", "banner", "social_post"]
KIND_FOLDER = {"brand_kit": "brand", "logo": "logo", "palette": "palette", "icons": "icons", "mockup": "mockup",
               "animation": "animation", "banner": "banner", "social_post": "post"}
KIND_HE = {"brand_kit": "ערכת מיתוג", "logo": "לוגואים", "palette": "פלטת צבעים", "icons": "אייקונים",
           "mockup": "סקיצות ממשק", "animation": "אנימציות", "banner": "באנר", "social_post": "פוסט לרשתות"}


def folder_slug(text: str, default: str = "design") -> str:
    slug = re.sub(r"[^\w\-]+", "-", (text or "").strip().lower(), flags=re.UNICODE).strip("-")
    return slug[:50].rstrip("-") or default


def fresh_folder(base: Path) -> Path:
    """`base` when it is missing or empty, otherwise the first free `base-2`, `base-3`, …"""
    candidate, n = base, 1
    while candidate.exists() and (not candidate.is_dir() or any(candidate.iterdir())):
        n += 1
        candidate = base.with_name(f"{base.name}-{n}")
    return candidate


def _output_folder(folder: str, default_name: str, ctx: ToolContext) -> Path:
    """The folder a call writes to, chosen once in plan() and reused by run()."""
    chosen = ctx.extra.get("output_folder")
    if chosen is None:
        if folder:
            chosen = ctx.resolve(folder)
        else:
            chosen = fresh_folder(ctx.services.paths.workspace / "design" / default_name)
        ctx.extra["output_folder"] = chosen
    return Path(chosen)


# ------------------------------------------------------------------------------------------ design assets
class CreateDesignParams(BaseModel):
    kind: DesignKind = Field(..., description="brand_kit (palette + logos + icons + texts + banner), logo, palette, "
                                              "icons, mockup (UI screens), animation, banner or social_post")
    brief: str = Field(..., description="What it is for, in the user's words (drives colors, texts and icons)")
    name: str = Field("", description="Brand or project name (default: invented from the brief)")
    folder: str = Field("", description="Output folder (default: workspace/design/<name>)")
    language: Literal["he", "en"] = "he"
    count: int = Field(4, ge=1, le=12, description="Number of logo concepts")
    style: str = Field("auto", description="Logo style: auto, monogram, wordmark, emblem, geometric, icon, stacked; "
                                           "icon style: outline, filled, duotone")
    icons: list[str] = Field(default_factory=list, description="Icon names (default: chosen from the brief)")
    screens: list[str] = Field(default_factory=list,
                               description="Mockup screens: landing, dashboard, login, product, settings, mobile")
    base_color: str = Field("", description="Pin the main color, e.g. #1d4ed8")
    dark: bool = Field(False, description="Dark theme palette")
    title: str = Field("", description="Headline for a banner or social post")
    subtitle: str = Field("", description="Second line for a banner or social post")
    palette: dict[str, Any] = Field(default_factory=dict, description="Reuse a palette returned by an earlier call")
    gallery: bool = Field(True, description="Also write index.html previewing everything")


def build_design(params: CreateDesignParams, out_dir: Path) -> dict[str, Any]:
    """Generate the assets of one design request into `out_dir` (blocking; run in a thread)."""
    from jarvis.creative import (
        Palette,
        build_gallery,
        favicon_svg,
        generate_animations,
        generate_icon_set,
        generate_logos,
        generate_mockup,
        generate_palette,
        images_available,
        landing_copy,
        save_assets,
        slogans,
        taglines,
    )
    from jarvis.creative.assets import Asset

    lang = params.language
    brief = params.brief.strip() or params.name
    palette = Palette.from_dict(params.palette) if params.palette else generate_palette(
        brief, base_color=params.base_color or None, dark=params.dark)
    copy = landing_copy(brief, lang=lang, name=params.name or None)
    name = params.name.strip() or str(copy.get("name") or brief[:30])
    tagline = (taglines(brief, lang=lang, n=1) or [""])[0]
    assets: list[Asset] = []
    images: list[Path] = []
    notes: list[str] = []

    def palette_assets() -> list[Asset]:
        return [Asset(name="palette", kind="svg", content=palette.to_svg(), description="Color palette"),
                Asset(name="palette", kind="json", content=palette.to_json(), description="Palette (JSON)"),
                Asset(name="palette", kind="css", content=palette.to_css(), description="CSS variables")]

    kind = params.kind
    if kind in ("palette", "brand_kit"):
        assets += palette_assets()
    logos: list[Asset] = []
    if kind in ("logo", "brand_kit"):
        logos = generate_logos(name, tagline=tagline, palette=palette, style=params.style if kind == "logo" else "auto",
                               count=params.count, lang=lang)
        assets += logos
        assets.append(Asset(name="favicon", kind="svg", content=favicon_svg(name, palette), description="Favicon"))
    if kind in ("icons", "brand_kit"):
        style = params.style if params.style in ("outline", "filled", "duotone") else "outline"
        assets += generate_icon_set(params.icons or None, brief=brief, palette=palette, style=style)
    if kind == "mockup":
        assets += generate_mockup(brief, screens=params.screens or None, palette=palette, lang=lang)
    if kind == "animation":
        logo_svg = generate_logos(name, palette=palette, count=1, lang=lang)[0].content
        assets += generate_animations(brief, palette=palette, lang=lang, logo_svg=str(logo_svg), title=name)
    if kind == "brand_kit":
        lines = [f"# {name}", "", f"**{tagline}**" if tagline else "", "",
                 "## " + ("סלוגנים" if lang == "he" else "Slogans"), ""]
        lines += [f"- {s}" for s in slogans(brief, lang=lang, n=6, name=name)]
        lines += ["", "## " + ("צבעים" if lang == "he" else "Colors"), ""]
        lines += [f"- {role}: `{hex_}`" for role, hex_ in palette.roles.items()]
        lines += ["", "## " + ("קהל יעד" if lang == "he" else "Audience"), ""]
        lines += [f"- {a}" for a in copy.get("audience", [])]
        lines += ["", "## " + ("מסר מרכזי" if lang == "he" else "Key message"), "", str(copy.get("headline", "")),
                  "", str(copy.get("subheadline", ""))]
        assets.append(Asset(name="brand", kind="md", content="\n".join(lines).replace("\n\n\n", "\n\n") + "\n",
                            description="Brand guide"))

    saved = save_assets(assets, out_dir) if assets else []
    if kind in ("banner", "social_post", "brand_kit"):
        if images_available():
            from jarvis.creative import render_banner, render_social_post

            title = params.title or name
            subtitle = params.subtitle or tagline or str(copy.get("subheadline", ""))
            if kind in ("banner", "brand_kit"):
                images.append(render_banner(title, subtitle, palette=palette, lang=lang,
                                            path=fresh_file(out_dir / "banner.png")))
            if kind == "social_post":
                images.append(render_social_post(title, subtitle, palette=palette, lang=lang,
                                                 path=fresh_file(out_dir / "post.png"), cta=str(copy.get("cta", ""))))
        elif kind != "brand_kit":
            raise RuntimeError("Images need the Pillow package (pip install pillow).")
        else:
            notes.append("banner skipped: Pillow is not installed")
    gallery = None
    if params.gallery and (saved or images):
        gallery_assets = [*assets, *(Asset(name=p.stem, kind="png", path=p) for p in images)]
        gallery = build_gallery(gallery_assets, out_dir, name, lang, palette=palette)
    files = [str(p) for p in [*saved, *images]] + ([str(gallery)] if gallery else [])
    return {
        "kind": kind, "name": name, "tagline": tagline, "folder": str(out_dir), "files": files,
        "gallery": str(gallery) if gallery else None, "palette": palette.to_dict(),
        "logo": str(logos[0].path) if logos and logos[0].path else None, "notes": notes,
    }


def fresh_file(path: Path) -> Path:
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
    return candidate


class CreateDesignTool(Tool):
    name = "create_design"
    description = ("Create design files offline: a brand kit (palette, logos, favicon, icons, slogans, banner), logo "
                   "concepts, a color palette, an icon set, UI mockup screens, CSS/SVG animations, a banner or a "
                   "social post image. Returns the files and the palette (pass it to later calls or to "
                   "create_project so everything shares the same colors).")
    capabilities = (C.FS_WRITE,)
    Params = CreateDesignParams
    risk = RiskLevel.LOW
    timeout_s = 300.0

    def _default_name(self, params: CreateDesignParams) -> str:
        return f"{folder_slug(params.name or params.brief[:40])}-{KIND_FOLDER[params.kind]}"

    async def plan(self, params: CreateDesignParams, ctx: ToolContext) -> ActionPlan:
        out_dir = await asyncio.to_thread(_output_folder, params.folder, self._default_name(params), ctx)
        return ActionPlan(summary=ctx.t(f"יצירת קבצי {KIND_HE[params.kind]} ב-{out_dir}",
                                        f"Create {params.kind.replace('_', ' ')} files in {out_dir}"),
                          paths=[PathUse(path=str(out_dir), access="write")])

    async def run(self, params: CreateDesignParams, ctx: ToolContext) -> ToolResult:
        out_dir = await asyncio.to_thread(_output_folder, params.folder, self._default_name(params), ctx)
        try:
            data = await asyncio.to_thread(build_design, params, out_dir)
        except (RuntimeError, KeyError, ValueError) as exc:
            return ToolResult.fail(ctx.t(f"לא הצלחתי ליצור {KIND_HE[params.kind]}: {exc}",
                                         f"Could not create the {params.kind.replace('_', ' ')}: {exc}"))
        files = data["files"]
        return ToolResult(summary=ctx.t(f"נוצרו {len(files)} קבצי {KIND_HE[params.kind]} ב-{out_dir}",
                                        f"Created {len(files)} {params.kind.replace('_', ' ')} file(s) in {out_dir}"),
                          files_created=files, actions=[f"create {params.kind} in {out_dir}"], data=data)


# ------------------------------------------------------------------------------------------ presentations
class SlideInput(BaseModel):
    title: str
    bullets: list[str] = Field(default_factory=list, description="Points (or subtitle lines on title/closing slides)")
    notes: str = Field("", description="Speaker notes")
    layout: Literal["title", "section", "bullets", "two_column", "quote", "closing"] = "bullets"
    columns: list[str] = Field(default_factory=list, description="Two column headings for a two_column slide")


class CreatePresentationParams(BaseModel):
    topic: str = Field(..., description="What the presentation is about")
    slides: list[SlideInput] = Field(default_factory=list,
                                     description="The slides; leave empty to have a complete outline written")
    title: str = Field("", description="Deck title (default: from the topic)")
    n_slides: int = Field(8, ge=3, le=14, description="Length of a generated outline")
    audience: str = ""
    language: Literal["he", "en"] = "he"
    folder: str = Field("", description="Output folder (default: workspace/presentations/<topic>)")
    base_color: str = ""
    palette: dict[str, Any] = Field(default_factory=dict)


def build_presentation(params: CreatePresentationParams, out_dir: Path) -> dict[str, Any]:
    from jarvis.creative import Palette, Slide, generate_palette, generate_presentation, save_assets

    palette = Palette.from_dict(params.palette) if params.palette else generate_palette(
        params.topic, base_color=params.base_color or None)
    slides = None
    if params.slides:
        slides = [Slide(title=s.title, bullets=s.bullets, notes=s.notes, layout=s.layout,
                        columns=(s.columns[0], s.columns[1]) if len(s.columns) == 2 else None) for s in params.slides]
    assets = generate_presentation(params.topic, lang=params.language, n_slides=params.n_slides,
                                   audience=params.audience, palette=palette, slides=slides,
                                   title=params.title or None)
    paths = save_assets(assets, out_dir)
    by_kind = {a.kind: str(p) for a, p in zip(assets, paths, strict=True)}
    count = len(slides) if slides else len(json.loads(assets[2].read_text())["slides"])
    return {"folder": str(out_dir), "files": [str(p) for p in paths], "html": by_kind.get("html"),
            "pptx": by_kind.get("pptx"), "outline": by_kind.get("json"), "slides": count,
            "palette": palette.to_dict()}


class CreatePresentationTool(Tool):
    name = "create_presentation"
    description = ("Create a presentation as an editable PowerPoint file (.pptx, right-to-left for Hebrew, with "
                   "speaker notes) plus a single-file HTML deck. Give the slides you wrote, or only a topic to get a "
                   "complete outline.")
    capabilities = (C.FS_WRITE,)
    Params = CreatePresentationParams
    risk = RiskLevel.LOW
    timeout_s = 300.0

    async def _folder(self, params: CreatePresentationParams, ctx: ToolContext) -> Path:
        chosen = ctx.extra.get("output_folder")
        if chosen is None:
            if params.folder:
                chosen = ctx.resolve(params.folder)
            else:
                base = ctx.services.paths.workspace / "presentations" / folder_slug(params.title or params.topic[:40],
                                                                                   "presentation")
                chosen = await asyncio.to_thread(fresh_folder, base)
            ctx.extra["output_folder"] = chosen
        return Path(chosen)

    async def plan(self, params: CreatePresentationParams, ctx: ToolContext) -> ActionPlan:
        out_dir = await self._folder(params, ctx)
        return ActionPlan(summary=ctx.t(f"יצירת מצגת ב-{out_dir}", f"Create a presentation in {out_dir}"),
                          paths=[PathUse(path=str(out_dir), access="write")])

    async def run(self, params: CreatePresentationParams, ctx: ToolContext) -> ToolResult:
        out_dir = await self._folder(params, ctx)
        try:
            data = await asyncio.to_thread(build_presentation, params, out_dir)
        except (KeyError, ValueError) as exc:
            return ToolResult.fail(ctx.t(f"לא הצלחתי ליצור את המצגת: {exc}", f"Could not create the presentation: {exc}"))
        return ToolResult(summary=ctx.t(f"נוצרה מצגת של {data['slides']} שקפים ב-{out_dir}",
                                        f"Created a {data['slides']}-slide presentation in {out_dir}"),
                          files_created=data["files"], actions=[f"create presentation in {out_dir}"], data=data)


CREATIVE_TOOLS: tuple[type[Tool], ...] = (CreateDesignTool, CreatePresentationTool)
