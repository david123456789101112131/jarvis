"""Generated creative assets: the `Asset` record, safe saving and an HTML preview gallery."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from jarvis.common.text import slugify

from ._util import FONT_STACK, esc, stable_seed, text_dir

if TYPE_CHECKING:
    from .palette import Palette

AssetKind = Literal["svg", "html", "css", "png", "pptx", "json", "md", "txt"]

EXTENSIONS: dict[str, str] = {
    "svg": ".svg", "html": ".html", "css": ".css", "png": ".png", "pptx": ".pptx", "json": ".json", "md": ".md",
    "txt": ".txt",
}
MEDIA_TYPES: dict[str, str] = {
    "svg": "image/svg+xml", "html": "text/html", "css": "text/css", "png": "image/png",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "json": "application/json", "md": "text/markdown", "txt": "text/plain",
}


@dataclass
class Asset:
    """One generated file. Either `content` (text or bytes) or `path` (an existing file) is set."""

    name: str
    kind: AssetKind
    content: str | bytes | None = None
    path: Path | None = None
    description: str = ""

    @property
    def extension(self) -> str:
        return EXTENSIONS.get(self.kind, "." + self.kind)

    @property
    def filename(self) -> str:
        """Safe file name: slug of `name` plus the extension of `kind` (e.g. 'logo-1-monogram.svg')."""
        stem = self.name
        if stem.lower().endswith(self.extension):
            stem = stem[: -len(self.extension)]
        return slugify(stem, fallback=self.kind, max_len=80) + self.extension

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES.get(self.kind, "application/octet-stream")

    def read_bytes(self) -> bytes:
        if self.content is not None:
            return self.content.encode("utf-8") if isinstance(self.content, str) else self.content
        if self.path is not None:
            return Path(self.path).read_bytes()
        raise ValueError(f"asset {self.name!r} has neither content nor path")

    def read_text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return self.read_bytes().decode("utf-8", errors="replace")

    @property
    def size(self) -> int:
        return len(self.read_bytes())


def _unique_target(out_dir: Path, filename: str, taken: set[str], overwrite: bool) -> Path:
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    candidate = filename
    n = 1
    while candidate.lower() in taken or (not overwrite and (out_dir / candidate).exists()):
        n += 1
        candidate = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
    taken.add(candidate.lower())
    return out_dir / candidate


def save_assets(assets: Iterable[Asset], out_dir: Path, *, overwrite: bool = False) -> list[Path]:
    """Write every asset into `out_dir` and return the paths in order.

    File names come from `Asset.filename`. Existing files are never overwritten unless
    `overwrite=True`: a clash gets a numbered name instead ('logo.svg' -> 'logo-2.svg'). Two
    assets with the same name in one call always get distinct files. Each asset's `path` is
    updated to where it was written.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    paths: list[Path] = []
    for asset in assets:
        target = _unique_target(out_dir, asset.filename, taken, overwrite)
        if asset.content is not None:
            data = asset.content.encode("utf-8") if isinstance(asset.content, str) else asset.content
            target.write_bytes(data)
        elif asset.path is not None:
            src = Path(asset.path)
            if src.resolve() != target.resolve():
                shutil.copyfile(src, target)
        else:
            raise ValueError(f"asset {asset.name!r} has neither content nor path")
        asset.path = target
        paths.append(target)
    return paths


# --------------------------------------------------------------------------------------------
# Gallery
# --------------------------------------------------------------------------------------------

_ID_RE = re.compile(r'\bid="([^"]+)"')
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
_ON_ATTR_RE = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*')", re.I)
_PROLOG_RE = re.compile(r"<\?xml[^>]*\?>|<!DOCTYPE[^>]*>", re.I)


def namespace_svg_ids(svg: str, prefix: str) -> str:
    """Prefix every id (and the references to it) so several SVGs can be inlined in one page."""
    ids = set(_ID_RE.findall(svg))
    if not ids:
        return svg
    for old in sorted(ids, key=len, reverse=True):
        new = f"{prefix}-{old}"
        svg = svg.replace(f'id="{old}"', f'id="{new}"')
        svg = svg.replace(f"url(#{old})", f"url(#{new})")
        svg = svg.replace(f'href="#{old}"', f'href="#{new}"')
        svg = re.sub(rf'(begin|end)="{re.escape(old)}\.', rf'\1="{new}.', svg)
        svg = svg.replace(f"#{old} ", f"#{new} ").replace(f"#{old}{{", f"#{new}{{")
    return svg


def inline_svg(svg: str, prefix: str) -> str:
    """Sanitized SVG markup safe to embed in an HTML page (no prolog, scripts or handlers)."""
    svg = _PROLOG_RE.sub("", svg)
    svg = _SCRIPT_RE.sub("", svg)
    svg = _ON_ATTR_RE.sub("", svg)
    return namespace_svg_ids(svg.strip(), prefix)


_GALLERY_TEXT = {
    "he": {
        "title": "גלריית נכסים", "items": "פריטים", "open": "פתיחה", "download": "הורדה", "svg": "לוגואים וגרפיקה",
        "png": "תמונות", "html": "דפים ומוקאפים", "pptx": "מצגות", "css": "סגנונות (CSS)", "json": "נתונים",
        "md": "טקסטים", "txt": "טקסטים", "palette": "פלטת צבעים", "footer": "נוצר אופליין על ידי JARVIS",
        "pptx_note": "מצגת PowerPoint – פותחים ב-PowerPoint, Keynote או Google Slides",
    },
    "en": {
        "title": "Asset gallery", "items": "items", "open": "Open", "download": "Download", "svg": "Logos & graphics",
        "png": "Images", "html": "Pages & mockups", "pptx": "Presentations", "css": "Styles (CSS)", "json": "Data",
        "md": "Text", "txt": "Text", "palette": "Color palette", "footer": "Generated offline by JARVIS",
        "pptx_note": "PowerPoint deck: open it in PowerPoint, Keynote or Google Slides",
    },
}
_ORDER = ("svg", "png", "html", "pptx", "css", "json", "md", "txt")


def _palette_from_asset(asset: Asset) -> dict | None:
    if asset.kind != "json":
        return None
    try:
        data = json.loads(asset.read_text())
    except (ValueError, OSError):
        return None
    if isinstance(data, dict) and isinstance(data.get("colors"), list) and all(
        isinstance(c, dict) and "hex" in c for c in data["colors"]
    ):
        return data
    return None


def _swatches(colors: list[dict]) -> str:
    items = []
    for c in colors:
        hexv = esc(c.get("hex", ""))
        role = esc(str(c.get("role") or c.get("name") or "").replace("_", " "))
        items.append(f'<li class="sw"><span class="chip" style="background:{hexv}"></span>'
                     f"<b>{role}</b><code>{hexv}</code></li>")
    return f'<ul class="swatches">{"".join(items)}</ul>'


def _rel(path: Path, base: Path) -> str:
    try:
        rel = path.resolve().relative_to(base.resolve())
    except ValueError:
        return path.resolve().as_uri()
    return quote(rel.as_posix())


def build_gallery(assets: Iterable[Asset], out_dir: Path, title: str = "", lang: str = "he", *,
                  palette: Palette | None = None) -> Path:
    """Save the assets (when not yet saved inside `out_dir`) and write an index.html previewing them.

    SVGs are inlined, images shown, HTML pages previewed in frames and linked, decks linked for
    download, palettes rendered as swatches. Returns the gallery path (index.html, or
    gallery.html when an asset already uses that name).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lang = "he" if lang == "he" else "en"
    t = _GALLERY_TEXT[lang]
    items = list(assets)
    base = out_dir.resolve()
    pending = [a for a in items if a.path is None or not Path(a.path).exists() or base not in Path(a.path).resolve().parents]
    if pending:
        save_assets(pending, out_dir)

    taken = {Path(a.path).name.lower() for a in items if a.path}
    gallery_name = "index.html"
    n = 1
    while gallery_name in taken or ((out_dir / gallery_name).exists() and "JARVIS gallery" not in
                                    (out_dir / gallery_name).read_text(encoding="utf-8", errors="ignore")[:600]):
        gallery_name = "gallery.html" if n == 1 else f"gallery-{n}.html"
        n += 1
    gallery = out_dir / gallery_name

    if palette is None:
        from .palette import generate_palette

        palette = generate_palette(title or "gallery")
    page_title = title or t["title"]

    sections: list[str] = []
    palette_blocks: list[str] = []
    for kind in _ORDER:
        group = [a for a in items if a.kind == kind]
        if not group:
            continue
        cards: list[str] = []
        for a in group:
            href = _rel(Path(a.path), out_dir) if a.path else ""
            name = esc(a.name)
            desc = f"<p>{esc(a.description)}</p>" if a.description else ""
            links = f'<a class="btn" href="{href}" download>{t["download"]}</a>'
            if kind == "svg":
                prefix = f"a{stable_seed(a.name, href) % 10**8:x}"
                preview = f'<div class="preview svg">{inline_svg(a.read_text(), prefix)}</div>'
                links = f'<a class="btn" href="{href}" target="_blank" rel="noopener">{t["open"]}</a>' + links
            elif kind == "png":
                preview = f'<div class="preview"><img src="{href}" alt="{name}" loading="lazy"></div>'
            elif kind == "html":
                preview = (f'<div class="preview frame"><iframe src="{href}" title="{name}" loading="lazy" '
                           f'tabindex="-1"></iframe></div>')
                links = f'<a class="btn" href="{href}" target="_blank" rel="noopener">{t["open"]}</a>' + links
            elif kind == "pptx":
                preview = f'<div class="preview deck"><span>PPTX</span><small>{t["pptx_note"]}</small></div>'
            else:
                pal = _palette_from_asset(a)
                if pal is not None:
                    palette_blocks.append(_swatches(pal["colors"]))
                    preview = f'<div class="preview">{_swatches(pal["colors"])}</div>'
                else:
                    text = a.read_text()
                    snippet = text[:1500] + ("\n…" if len(text) > 1500 else "")
                    preview = f'<pre class="preview code" dir="auto">{esc(snippet)}</pre>'
            cards.append(f'<article class="card {kind}">{preview}<div class="meta"><h3>{name}</h3>{desc}'
                         f'<div class="links">{links}</div></div></article>')
        sections.append(f'<section><h2>{t[kind]} <small>{len(group)}</small></h2>'
                        f'<div class="grid {kind}">{"".join(cards)}</div></section>')

    pal_section = ""
    if not palette_blocks:
        pal_section = (f'<section class="pal"><h2>{t["palette"]} <small>{esc(palette.name)}</small></h2>'
                       f'{_swatches([c.to_dict() for c in palette.colors])}</section>')

    css = f"""{palette.to_css()}
*{{box-sizing:border-box}}
body{{margin:0;font-family:{FONT_STACK};background:var(--color-background);color:var(--color-text);line-height:1.5}}
header{{padding:48px 24px 32px;background:linear-gradient(135deg,var(--color-primary),var(--color-secondary));
color:var(--color-on-primary)}}
header .wrap,main{{max-width:1200px;margin:0 auto}}
header h1{{margin:0 0 6px;font-size:clamp(28px,4vw,44px);letter-spacing:-.02em}}
header p{{margin:0;opacity:.9}}
main{{padding:24px}}
section{{margin:0 0 40px}}
h2{{font-size:20px;margin:0 0 16px;display:flex;align-items:center;gap:10px}}
h2 small{{font-size:13px;font-weight:600;color:var(--color-muted);background:var(--color-surface);
border:1px solid var(--color-border);border-radius:999px;padding:1px 10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:18px}}
.grid.html,.grid.png{{grid-template-columns:repeat(auto-fill,minmax(340px,1fr))}}
.card{{background:var(--color-surface);border:1px solid var(--color-border);border-radius:18px;overflow:hidden;
display:flex;flex-direction:column;box-shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.05)}}
.preview{{min-height:180px;display:grid;place-items:center;padding:18px;
background:repeating-conic-gradient(rgba(var(--color-text-rgb),.035) 0 25%,transparent 0 50%) 0 0/20px 20px}}
.preview.svg svg{{max-width:100%;max-height:240px;height:auto;width:auto}}
.grid.svg .preview.svg svg[data-icon]{{width:72px;height:72px}}
.preview img{{max-width:100%;max-height:260px;border-radius:10px;display:block}}
.preview.frame{{padding:0;height:240px;overflow:hidden;position:relative;place-items:start}}
.preview.frame iframe{{border:0;width:1280px;height:800px;transform:scale(.27);transform-origin:0 0;
pointer-events:none;position:absolute;inset-inline-start:0;top:0}}
[dir=rtl] .preview.frame iframe{{transform-origin:100% 0}}
.preview.deck{{gap:6px;text-align:center;color:var(--color-muted)}}
.preview.deck span{{font-weight:800;font-size:28px;color:var(--color-primary);border:3px solid currentColor;
border-radius:12px;padding:10px 18px}}
.preview.code{{margin:0;place-items:stretch;font:12px/1.5 Consolas,monospace;white-space:pre-wrap;overflow:auto;
max-height:260px;text-align:start}}
.meta{{padding:14px 16px 16px;border-top:1px solid var(--color-border)}}
.meta h3{{margin:0 0 4px;font-size:15px;word-break:break-word}}
.meta p{{margin:0 0 10px;color:var(--color-muted);font-size:13px}}
.links{{display:flex;gap:8px;flex-wrap:wrap}}
.btn{{display:inline-block;padding:6px 12px;border-radius:999px;font-size:13px;font-weight:600;text-decoration:none;
background:var(--color-primary);color:var(--color-on-primary)}}
.btn+.btn{{background:transparent;color:var(--color-primary);border:1px solid var(--color-border)}}
.btn:focus-visible{{outline:3px solid var(--color-accent);outline-offset:2px}}
.swatches{{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:10px;width:100%}}
.sw{{display:flex;flex-direction:column;gap:2px;font-size:12px;min-width:92px}}
.sw .chip{{display:block;height:56px;border-radius:12px;border:1px solid var(--color-border)}}
.sw code{{font-family:Consolas,monospace;color:var(--color-muted)}}
.pal{{background:var(--color-surface);border:1px solid var(--color-border);border-radius:18px;padding:20px}}
footer{{text-align:center;color:var(--color-muted);font-size:13px;padding:24px}}
"""
    total = len(items)
    html = f"""<!DOCTYPE html>
<html lang="{lang}" dir="{text_dir(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="JARVIS gallery">
<title>{esc(page_title)}</title>
<style>{css}</style>
</head>
<body>
<header><div class="wrap"><h1>{esc(page_title)}</h1><p>{total} {t["items"]}</p></div></header>
<main>
{pal_section}
{"".join(sections)}
</main>
<footer>{t["footer"]}</footer>
</body>
</html>
"""
    gallery.write_text(html, encoding="utf-8")
    return gallery
