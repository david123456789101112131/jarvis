"""Raster images with Pillow: banners, social posts, gradients and PNG icons.

Hebrew text is drawn right-to-left: with libraqm (Pillow's complex layout) the logical text is
passed with ``direction="rtl"``; without it (the usual case on Windows) the text is first put in
visual order by `visual_order` (Hebrew runs reversed, numbers and Latin runs kept in order).
"""

from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, features

from ._util import has_hebrew, make_rng, resolve_lang
from .icons import DETAIL, FILL, Shape, icon_shapes
from .palette import Palette, best_text_color, contrast_ratio, generate_palette, hex_to_rgb, mix

FontType = ImageFont.FreeTypeFont | ImageFont.ImageFont

# --------------------------------------------------------------------------------------------
# Bidi helper
# --------------------------------------------------------------------------------------------

_RTL_RE = re.compile(r"[\u0590-\u05FF\uFB1D-\uFB4F\u0600-\u06FF]")
_MIRROR = str.maketrans("()[]{}<>«»", ")(][}{><»«")


def _char_dir(ch: str) -> str:
    if _RTL_RE.match(ch):
        return "R"
    if ch.isalnum():  # Latin letters and digits keep their left-to-right order
        return "L"
    return "N"


def visual_order(text: str) -> str:
    """Reorder a right-to-left paragraph for a renderer that only lays text out left to right.

    Hebrew runs are reversed character by character (brackets mirrored) while runs of Latin
    letters and numbers keep their internal order, and the runs themselves are laid out from
    right to left. Lines are handled independently. Pure left-to-right text is returned as is.
    """
    if not _RTL_RE.search(text or ""):
        return text
    return "\n".join(_visual_line(line) for line in text.split("\n"))


def _visual_line(line: str) -> str:
    dirs = [_char_dir(ch) for ch in line]
    n = len(dirs)
    resolved = dirs[:]
    for i, d in enumerate(dirs):
        if d != "N":
            continue
        prev = next((dirs[j] for j in range(i - 1, -1, -1) if dirs[j] != "N"), "R")
        nxt = next((dirs[j] for j in range(i + 1, n) if dirs[j] != "N"), "R")
        resolved[i] = "L" if prev == "L" and nxt == "L" else "R"
    runs: list[tuple[str, str]] = []
    for ch, d in zip(line, resolved, strict=True):
        if runs and runs[-1][0] == d:
            runs[-1] = (d, runs[-1][1] + ch)
        else:
            runs.append((d, ch))
    out = []
    for d, chunk in reversed(runs):
        out.append(chunk[::-1].translate(_MIRROR) if d == "R" else chunk)
    return "".join(out)


def has_raqm() -> bool:
    try:
        return bool(features.check("raqm"))
    except Exception:  # pragma: no cover - very old Pillow
        return False


# --------------------------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------------------------

def _font_dirs() -> list[Path]:
    dirs: list[Path] = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    elif sys.platform == "win32":  # pragma: no cover
        dirs.append(Path("C:/Windows/Fonts"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    dirs += [Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu"),
             Path("/usr/share/fonts/truetype/noto"), Path("/usr/share/fonts/noto"),
             Path("/usr/share/fonts/truetype/freefont"), Path("/usr/share/fonts/truetype/liberation"),
             Path("/usr/share/fonts/TTF"), Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts"),
             Path.home() / ".fonts", Path.home() / ".local/share/fonts"]
    return dirs


_REGULAR = ("Heebo-Regular.ttf", "segoeui.ttf", "arial.ttf", "david.ttf", "DejaVuSans.ttf", "NotoSansHebrew-Regular.ttf",
            "FreeSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf")
_BOLD = ("Heebo-Bold.ttf", "segoeuib.ttf", "arialbd.ttf", "davidbd.ttf", "DejaVuSans-Bold.ttf",
         "NotoSansHebrew-Bold.ttf", "FreeSansBold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf")


@lru_cache(maxsize=8)
def font_candidates(bold: bool = False) -> tuple[Path, ...]:
    """Existing font files in preference order (Windows fonts first, then common Linux/macOS ones)."""
    names = _BOLD if bold else _REGULAR
    found: list[Path] = []
    for name in names:
        for d in _font_dirs():
            p = d / name
            if p.is_file():
                found.append(p)
                break
    return tuple(found)


@lru_cache(maxsize=64)
def _load(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _supports(font: FontType, text: str) -> bool:
    """True when the font has real glyphs for the text's characters (not .notdef boxes)."""
    probe = next((ch for ch in text if _RTL_RE.match(ch)), None)
    if probe is None:
        return True
    try:
        return bytes(font.getmask(probe)) != bytes(font.getmask("\uffff"))
    except Exception:
        return False


def find_font(size: int, *, bold: bool = False, text: str = "") -> FontType:
    """A TrueType font able to draw `text` (Hebrew-capable when needed), or Pillow's default."""
    for path in font_candidates(bold) + (font_candidates(False) if bold else ()):
        try:
            font = _load(str(path), size)
        except OSError:
            continue
        if _supports(font, text):
            return font
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        return ImageFont.load_default()


# --------------------------------------------------------------------------------------------
# Text drawing
# --------------------------------------------------------------------------------------------

def _measure(draw: ImageDraw.ImageDraw, text: str, font: FontType) -> float:
    if has_hebrew(text) and has_raqm():
        return draw.textlength(text, font=font, direction="rtl")
    return draw.textlength(visual_order(text), font=font)


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: FontType, max_width: float) -> list[str]:
    """Greedy word wrap (in logical order) using real font metrics."""
    lines: list[str] = []
    for paragraph in (text or "").split("\n"):
        words = paragraph.split()
        cur = ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if cur and _measure(draw, trial, font) > max_width:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)
    return lines


def draw_text_line(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font: FontType, fill: str,
                   *, align: str = "left") -> None:
    """Draw one line. `xy` is the left, right or center point (by `align`) of the baseline-top box."""
    width = _measure(draw, text, font)
    x, y = xy
    if align == "right":
        x -= width
    elif align == "center":
        x -= width / 2
    if has_hebrew(text) and has_raqm():
        draw.text((x, y), text, font=font, fill=fill, direction="rtl", language="he")
    else:
        draw.text((x, y), visual_order(text), font=font, fill=fill)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: float, max_lines: int, start: int, minimum: int,
              *, bold: bool = True) -> tuple[FontType, list[str]]:
    size = start
    while True:
        font = find_font(size, bold=bold, text=text)
        lines = wrap_text(draw, text, font, max_width)
        if (len(lines) <= max_lines and all(_measure(draw, ln, font) <= max_width for ln in lines)) or size <= minimum:
            return font, lines[: max_lines + 2]
        size -= 4


# --------------------------------------------------------------------------------------------
# Backgrounds
# --------------------------------------------------------------------------------------------

def _gradient_mask(size: tuple[int, int], direction: str = "diagonal") -> Image.Image:
    w, h = size
    vertical = Image.linear_gradient("L").resize((w, h))
    horizontal = Image.linear_gradient("L").transpose(Image.Transpose.TRANSPOSE).resize((w, h))
    if direction == "vertical":
        return vertical
    if direction == "horizontal":
        return horizontal
    return ImageChops.add(horizontal, vertical, scale=2.0)


def gradient_image(size: tuple[int, int], start: str, end: str, direction: str = "diagonal") -> Image.Image:
    a = Image.new("RGB", size, hex_to_rgb(start))
    b = Image.new("RGB", size, hex_to_rgb(end))
    return Image.composite(b, a, _gradient_mask(size, direction))


def _glow(base: Image.Image, center: tuple[float, float], radius: float, color: str, alpha: int) -> Image.Image:
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = center
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*hex_to_rgb(color), alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(radius / 2.2))
    return Image.alpha_composite(base.convert("RGBA"), layer)


def _decorate(img: Image.Image, palette: Palette, rng_seed: str, *, side: str, ink: str) -> Image.Image:
    """Geometric decorations (rings, dots, a blob) on one side of the canvas."""
    rng = make_rng("decor", rng_seed)
    w, h = img.size
    img = _glow(img, (w * (0.18 if side == "left" else 0.82), h * 0.25), min(w, h) * 0.45, palette.accent, 110)
    img = _glow(img, (w * (0.05 if side == "left" else 0.95), h * 0.95), min(w, h) * 0.35, palette.secondary, 90)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    ink_rgb = hex_to_rgb(ink)
    accent = hex_to_rgb(palette.accent)
    base_x = w * (0.2 if side == "left" else 0.8)
    for i in range(3):
        r = min(w, h) * (0.16 + 0.11 * i) * rng.uniform(0.9, 1.1)
        cy = h * 0.55
        d.ellipse((base_x - r, cy - r, base_x + r, cy + r), outline=(*ink_rgb, 46 - i * 10), width=max(2, w // 400))
    r = min(w, h) * 0.075
    cx, cy = base_x + min(w, h) * 0.2 * (1 if side == "left" else -1), h * 0.3
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*accent, 230))
    step = max(18, w // 48)
    x0 = int(w * (0.04 if side == "left" else 0.72))
    for gx in range(6):
        for gy in range(4):
            x, y = x0 + gx * step, int(h * 0.72) + gy * step
            d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(*ink_rgb, 90))
    sq = min(w, h) * 0.09
    sx, sy = base_x - min(w, h) * 0.22 * (1 if side == "left" else -1), h * 0.8
    d.rounded_rectangle((sx - sq / 2, sy - sq / 2, sx + sq / 2, sy + sq / 2), radius=sq / 4,
                        outline=(*accent, 220), width=max(3, w // 300))
    return Image.alpha_composite(img.convert("RGBA"), layer)


def _gradient_colors(palette: Palette) -> tuple[str, str, str]:
    """Start/end colors for a background and a text color with at least 4.5:1 on both ends."""
    start = palette.primary
    end = mix(palette.primary, palette.secondary, 0.6)
    ink = best_text_color(start, ("#ffffff", "#141414"))
    t = 0.6
    while contrast_ratio(ink, end) < 4.5 and t > 0:
        t = round(t - 0.15, 2)
        end = mix(palette.primary, palette.secondary, max(t, 0))
    if contrast_ratio(ink, start) < 4.5 or contrast_ratio(ink, end) < 4.5:
        dark = "#141414" if ink == "#ffffff" else "#ffffff"
        start = mix(start, dark, 0.35)
        end = mix(end, dark, 0.35)
    return start, end, ink


def render_gradient(size: tuple[int, int], palette: Palette | None, path: Path, *, direction: str = "diagonal",
                    glow: bool = True) -> Path:
    """A smooth primary -> secondary gradient with soft accent glows (PNG)."""
    palette = palette or generate_palette()
    img = gradient_image(size, palette.primary, palette.secondary, direction).convert("RGBA")
    if glow:
        w, h = size
        img = _glow(img, (w * 0.8, h * 0.2), min(w, h) * 0.5, palette.accent, 120)
        img = _glow(img, (w * 0.15, h * 0.9), min(w, h) * 0.4, palette.secondary, 100)
    return _save(img, path)


def _save(img: Image.Image, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, format="PNG", optimize=True)
    return path


# --------------------------------------------------------------------------------------------
# Banner and social post
# --------------------------------------------------------------------------------------------

def render_banner(title: str, subtitle: str = "", *, size: tuple[int, int] = (1200, 630), palette: Palette | None = None,
                  lang: str = "he", path: Path, tag: str = "") -> Path:
    """A 1200x630 (Open Graph sized) banner: gradient, geometric decorations, title and subtitle."""
    lang = resolve_lang(lang, title)
    palette = palette or generate_palette(f"{title} {subtitle}")
    w, h = size
    start, end, ink = _gradient_colors(palette)
    img = gradient_image(size, start, end).convert("RGBA")
    rtl = lang == "he"
    img = _decorate(img, palette, title, side="left" if rtl else "right", ink=ink)
    draw = ImageDraw.Draw(img)
    margin = int(w * 0.07)
    text_w = w * 0.58
    x = w - margin if rtl else margin
    align = "right" if rtl else "left"

    font, lines = _fit_font(draw, title, text_w, 3, int(h * 0.13), int(h * 0.06))
    sub_font = find_font(max(18, int(h * 0.045)), text=subtitle)
    sub_lines = wrap_text(draw, subtitle, sub_font, text_w)[:3] if subtitle else []
    line_h = font.size * 1.18 if isinstance(font, ImageFont.FreeTypeFont) else 40
    sub_h = sub_font.size * 1.4 if isinstance(sub_font, ImageFont.FreeTypeFont) else 24
    tag_h = 0
    if tag:
        tag_h = int(h * 0.075)
    block = tag_h + (tag_h * 0.5 if tag else 0) + len(lines) * line_h + (h * 0.04 if sub_lines else 0) + len(sub_lines) * sub_h
    y = (h - block) / 2

    if tag:
        tag_font = find_font(int(tag_h * 0.45), bold=True, text=tag)
        tw = _measure(draw, tag, tag_font) + tag_h * 0.8
        x0 = x - tw if rtl else x
        draw.rounded_rectangle((x0, y, x0 + tw, y + tag_h), radius=tag_h / 2, fill=(*hex_to_rgb(ink), 38),
                               outline=(*hex_to_rgb(ink), 120), width=2)
        draw_text_line(draw, (x0 + tw / 2, y + tag_h * 0.22), tag, tag_font, ink, align="center")
        y += tag_h * 1.5
    for ln in lines:
        draw_text_line(draw, (x, y), ln, font, ink, align=align)
        y += line_h
    if sub_lines:
        y += h * 0.04
        bar_w = w * 0.08
        bx = x - bar_w if rtl else x
        draw.rounded_rectangle((bx, y - h * 0.025, bx + bar_w, y - h * 0.025 + 6), radius=3, fill=palette.accent)
        for ln in sub_lines:
            draw_text_line(draw, (x, y), ln, sub_font, ink, align=align)
            y += sub_h
    return _save(img, path)


def render_social_post(title: str, subtitle: str = "", *, size: tuple[int, int] = (1080, 1080),
                       palette: Palette | None = None, lang: str = "he", path: Path, cta: str = "",
                       handle: str = "") -> Path:
    """A square social media post: framed gradient card, centered headline, optional CTA pill and handle."""
    lang = resolve_lang(lang, title)
    palette = palette or generate_palette(f"{title} {subtitle}")
    w, h = size
    start, end, ink = _gradient_colors(palette)
    img = gradient_image(size, start, end, "diagonal").convert("RGBA")
    img = _glow(img, (w * 0.85, h * 0.12), w * 0.35, palette.accent, 140)
    img = _glow(img, (w * 0.1, h * 0.92), w * 0.3, palette.secondary, 110)
    draw = ImageDraw.Draw(img)
    ink_rgb = hex_to_rgb(ink)
    m = int(w * 0.06)
    draw.rounded_rectangle((m, m, w - m, h - m), radius=int(w * 0.05), outline=(*ink_rgb, 110), width=max(2, w // 360))
    rng = make_rng("post", title)
    for _ in range(5):
        r = rng.uniform(0.01, 0.025) * w
        cx, cy = rng.uniform(m * 2, w - m * 2), rng.uniform(m * 2, h * 0.25)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*ink_rgb, 70))

    font, lines = _fit_font(draw, title, w * 0.74, 4, int(h * 0.11), int(h * 0.05))
    line_h = font.size * 1.2 if isinstance(font, ImageFont.FreeTypeFont) else 40
    sub_font = find_font(max(18, int(h * 0.038)), text=subtitle)
    sub_lines = wrap_text(draw, subtitle, sub_font, w * 0.7)[:4] if subtitle else []
    sub_h = sub_font.size * 1.45 if isinstance(sub_font, ImageFont.FreeTypeFont) else 24
    block = len(lines) * line_h + (h * 0.05 + len(sub_lines) * sub_h if sub_lines else 0)
    y = (h - block) / 2 - (h * 0.04 if cta else 0)
    for ln in lines:
        draw_text_line(draw, (w / 2, y), ln, font, ink, align="center")
        y += line_h
    if sub_lines:
        y += h * 0.02
        draw.rounded_rectangle((w / 2 - w * 0.05, y, w / 2 + w * 0.05, y + 6), radius=3, fill=palette.accent)
        y += h * 0.03
        for ln in sub_lines:
            draw_text_line(draw, (w / 2, y), ln, sub_font, ink, align="center")
            y += sub_h
    if cta:
        cta_font = find_font(int(h * 0.036), bold=True, text=cta)
        cw = _measure(draw, cta, cta_font) + w * 0.09
        ch = h * 0.085
        cy = h - m - h * 0.16
        pill_fill = palette.accent
        pill_ink = best_text_color(pill_fill, ("#ffffff", "#141414"))
        draw.rounded_rectangle((w / 2 - cw / 2, cy, w / 2 + cw / 2, cy + ch), radius=ch / 2, fill=pill_fill)
        draw_text_line(draw, (w / 2, cy + ch * 0.24), cta, cta_font, pill_ink, align="center")
    if handle:
        hf = find_font(int(h * 0.026), text=handle)
        draw_text_line(draw, (w / 2, h - m - h * 0.055), handle, hf, ink, align="center")
    return _save(img, path)


# --------------------------------------------------------------------------------------------
# Minimal SVG geometry rasterizer for the built-in icons
# --------------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
Point = tuple[float, float]


def _arc_points(p0: Point, rx: float, ry: float, phi_deg: float, large: bool, sweep: bool, p1: Point) -> list[Point]:
    """SVG elliptical arc (endpoint form) flattened to points, per SVG spec F.6.5."""
    (x1, y1), (x2, y2) = p0, p1
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [p1]
    phi = math.radians(phi_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p = cos_p * dx + sin_p * dy
    y1p = -sin_p * dx + cos_p * dy
    rx, ry = abs(rx), abs(ry)
    lam = (x1p**2) / (rx**2) + (y1p**2) / (ry**2)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx**2 * ry**2 - rx**2 * y1p**2 - ry**2 * x1p**2
    den = rx**2 * y1p**2 + ry**2 * x1p**2
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    steps = max(4, int(abs(dt) / (math.pi / 24)))
    pts = []
    for i in range(1, steps + 1):
        t = t1 + dt * i / steps
        x = cos_p * rx * math.cos(t) - sin_p * ry * math.sin(t) + cx
        y = sin_p * rx * math.cos(t) + cos_p * ry * math.sin(t) + cy
        pts.append((x, y))
    return pts


def _cubic(p0: Point, c1: Point, c2: Point, p1: Point, steps: int = 16) -> list[Point]:
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        out.append((
            mt**3 * p0[0] + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * p1[0],
            mt**3 * p0[1] + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * p1[1],
        ))
    return out


def _quad(p0: Point, c: Point, p1: Point, steps: int = 12) -> list[Point]:
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        out.append((mt**2 * p0[0] + 2 * mt * t * c[0] + t**2 * p1[0], mt**2 * p0[1] + 2 * mt * t * c[1] + t**2 * p1[1]))
    return out


def path_to_polylines(d: str) -> list[tuple[list[Point], bool]]:
    """Flatten SVG path data into (points, closed) subpaths. Supports every path command."""
    tokens = _TOKEN_RE.findall(d)
    i = 0
    subpaths: list[tuple[list[Point], bool]] = []
    cur: list[Point] = []
    pos: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    last_ctrl: Point | None = None
    last_cmd = ""
    cmd = ""

    def num() -> float:
        nonlocal i
        v = float(tokens[i])
        i += 1
        return v

    def flag() -> bool:
        nonlocal i
        tok = tokens[i]
        # flags may be glued to the next number ("01" or "1.5"): consume one digit
        if len(tok) > 1 and tok[0] in "01" and not tok.startswith(("0.", "1.")):
            tokens[i] = tok[1:]
            return tok[0] == "1"
        if len(tok) > 1 and tok[0] in "01" and tok[1] == ".":
            tokens[i] = tok[1:]
            return tok[0] == "1"
        i += 1
        return float(tok) != 0

    while i < len(tokens):
        tok = tokens[i]
        if tok.isalpha():
            cmd = tok
            i += 1
            if cmd in "Zz":
                if cur:
                    subpaths.append((cur, True))
                cur = []
                pos = start
                last_cmd = cmd
                continue
        elif not cmd:
            break
        rel = cmd.islower()
        c = cmd.upper()
        ox, oy = (pos if rel else (0.0, 0.0))
        if c != "M" and not cur:
            cur = [pos]
        if c == "M":
            if cur:
                subpaths.append((cur, False))
            pos = (ox + num(), oy + num())
            start = pos
            cur = [pos]
            cmd = "l" if rel else "L"
            last_ctrl = None
        elif c == "L":
            pos = (ox + num(), oy + num())
            cur.append(pos)
            last_ctrl = None
        elif c == "H":
            pos = ((pos[0] if rel else 0.0) + num(), pos[1])
            cur.append(pos)
            last_ctrl = None
        elif c == "V":
            pos = (pos[0], (pos[1] if rel else 0.0) + num())
            cur.append(pos)
            last_ctrl = None
        elif c == "C":
            c1 = (ox + num(), oy + num())
            c2 = (ox + num(), oy + num())
            p1 = (ox + num(), oy + num())
            cur.extend(_cubic(pos, c1, c2, p1))
            pos, last_ctrl = p1, c2
        elif c == "S":
            c1 = (2 * pos[0] - last_ctrl[0], 2 * pos[1] - last_ctrl[1]) if last_ctrl and last_cmd in "CcSs" else pos
            c2 = (ox + num(), oy + num())
            p1 = (ox + num(), oy + num())
            cur.extend(_cubic(pos, c1, c2, p1))
            pos, last_ctrl = p1, c2
        elif c == "Q":
            c1 = (ox + num(), oy + num())
            p1 = (ox + num(), oy + num())
            cur.extend(_quad(pos, c1, p1))
            pos, last_ctrl = p1, c1
        elif c == "T":
            c1 = (2 * pos[0] - last_ctrl[0], 2 * pos[1] - last_ctrl[1]) if last_ctrl and last_cmd in "QqTt" else pos
            p1 = (ox + num(), oy + num())
            cur.extend(_quad(pos, c1, p1))
            pos, last_ctrl = p1, c1
        elif c == "A":
            rx, ry, rot = num(), num(), num()
            large, sweep = flag(), flag()
            p1 = (ox + num(), oy + num())
            cur.extend(_arc_points(pos, rx, ry, rot, large, sweep, p1))
            pos = p1
            last_ctrl = None
        else:  # pragma: no cover - unknown command
            i += 1
        last_cmd = cmd
    if cur and len(cur) > 1:
        subpaths.append((cur, False))
    elif cur and len(cur) == 1 and subpaths == []:
        subpaths.append((cur, False))
    return subpaths


def _ellipse_points(cx: float, cy: float, rx: float, ry: float, n: int = 64) -> list[Point]:
    return [(cx + rx * math.cos(2 * math.pi * k / n), cy + ry * math.sin(2 * math.pi * k / n)) for k in range(n)]


def _rounded_rect(x: float, y: float, w: float, h: float, r: float) -> list[Point]:
    r = max(0.0, min(r, w / 2, h / 2))
    if r == 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    pts: list[Point] = []
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
        for k in range(9):
            a = math.radians(a0 + 90 * k / 8)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _points_attr(value: str) -> list[Point]:
    nums = [float(v) for v in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)", value)]
    return list(zip(nums[0::2], nums[1::2], strict=False))


def _apply_transform(polys: list[tuple[list[Point], bool]], transform: str | None) -> list[tuple[list[Point], bool]]:
    if not transform:
        return polys
    m = re.match(r"rotate\(\s*([-\d.]+)(?:[\s,]+([-\d.]+)[\s,]+([-\d.]+))?\s*\)", transform)
    if not m:
        return polys
    a = math.radians(float(m.group(1)))
    cx, cy = float(m.group(2) or 0), float(m.group(3) or 0)
    ca, sa = math.cos(a), math.sin(a)
    return [([(cx + (x - cx) * ca - (y - cy) * sa, cy + (x - cx) * sa + (y - cy) * ca) for x, y in pts], closed)
            for pts, closed in polys]


def shape_polylines(shape: Shape) -> list[tuple[list[Point], bool]]:
    a = shape.attr_dict()
    f = lambda k, default=0.0: float(a.get(k) or default)  # noqa: E731
    if shape.tag == "path":
        polys = path_to_polylines(a["d"])
    elif shape.tag == "circle":
        polys = [(_ellipse_points(f("cx"), f("cy"), f("r"), f("r")), True)]
    elif shape.tag == "ellipse":
        polys = [(_ellipse_points(f("cx"), f("cy"), f("rx"), f("ry")), True)]
    elif shape.tag == "rect":
        polys = [(_rounded_rect(f("x"), f("y"), f("width"), f("height"), f("rx")), True)]
    elif shape.tag == "polyline":
        polys = [(_points_attr(a["points"]), False)]
    elif shape.tag == "polygon":
        polys = [(_points_attr(a["points"]), True)]
    else:  # pragma: no cover
        polys = []
    return _apply_transform(polys, a.get("transform"))


def rasterize_icon(name: str, size: int, *, color: str = "#111827", style: str = "outline", accent: str | None = None,
                   stroke_width: float = 2.0, background: str | None = None, knockout: str = "#ffffff",
                   padding: float = 0.0) -> Image.Image:
    """Draw a built-in icon into an RGBA image (4x supersampled, then downscaled for anti-aliasing)."""
    ss = 4
    canvas = size * ss
    scale = canvas / (24 + 2 * padding)
    off = padding * scale
    bg = (*hex_to_rgb(background), 255) if background else (0, 0, 0, 0)
    img = Image.new("RGBA", (canvas, canvas), bg)
    rgb = hex_to_rgb(color)
    shapes = icon_shapes(name)
    sw = stroke_width * scale

    def tr(pts: Iterable[Point]) -> list[Point]:
        return [(off + x * scale, off + y * scale) for x, y in pts]

    if style == "duotone":
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for s in shapes:
            if s.role == FILL:
                for pts, _closed in shape_polylines(s):
                    if len(pts) > 2:
                        ld.polygon(tr(pts), fill=(*hex_to_rgb(accent or color), 72))
        img = Image.alpha_composite(img, layer)
    draw = ImageDraw.Draw(img)

    def stroke(pts: list[Point], closed: bool, ink: tuple[int, int, int]) -> None:
        p = tr([*pts, pts[0]] if closed and len(pts) > 1 else pts)
        if len(p) > 1:
            draw.line(p, fill=(*ink, 255), width=max(1, round(sw)), joint="curve")
        r = sw / 2
        for x, y in (p[0], p[-1]) if p else ():
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(*ink, 255))

    ordered = sorted(shapes, key=lambda s: 1 if s.role == DETAIL else 0) if style == "filled" else shapes
    for s in ordered:
        polys = shape_polylines(s)
        ink = rgb
        if style == "filled" and s.role == FILL:
            for pts, _closed in polys:
                if len(pts) > 2:
                    draw.polygon(tr(pts), fill=(*rgb, 255))
        if style == "filled" and s.role == DETAIL:
            ink = hex_to_rgb(knockout)
        for pts, closed in polys:
            stroke(pts, closed, ink)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def render_icon_png(name: str, size: int = 256, palette: Palette | None = None, path: Path | None = None, *,
                    style: str = "outline", background: bool = False, stroke_width: float = 2.0) -> Path:
    """Rasterize a built-in icon to PNG in the palette's primary color (transparent background by default)."""
    palette = palette or generate_palette()
    if path is None:
        raise ValueError("path is required")
    img = rasterize_icon(name, size, color=palette.primary, style=style, accent=palette.accent,
                         stroke_width=stroke_width, background=palette.surface if background else None,
                         knockout=palette.surface, padding=1.5 if background else 0.0)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    return path


def render_icon_sheet(names: list[str], path: Path, *, palette: Palette | None = None, style: str = "outline",
                      columns: int = 10, cell: int = 96) -> Path:
    """A PNG contact sheet of icons with their names (useful for previews)."""
    palette = palette or generate_palette()
    rows = math.ceil(len(names) / columns) or 1
    img = Image.new("RGBA", (columns * cell, rows * (cell + 18)), (*hex_to_rgb(palette.surface), 255))
    label_font = find_font(11)
    d = ImageDraw.Draw(img)
    for idx, name in enumerate(names):
        x, y = (idx % columns) * cell, (idx // columns) * (cell + 18)
        icon = rasterize_icon(name, cell // 2, color=palette.text, style=style, accent=palette.accent,
                              knockout=palette.surface)
        img.alpha_composite(icon, (x + cell // 4, y + cell // 6))
        d.text((x + cell / 2, y + cell * 0.8), name, font=label_font, fill=palette.muted, anchor="mm")
    return _save(img, path)
