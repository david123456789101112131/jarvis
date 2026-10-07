"""Logo concepts as scalable SVG: monogram badge, wordmark, circular emblem, geometric symbol.

Hebrew names are laid out right to left (direction="rtl"); initials are derived for Hebrew and
English names alike (stop words and the Hebrew definite article are skipped).
"""

from __future__ import annotations

import math
import re

from ._util import (
    FONT_STACK_ATTR,
    SERIF_STACK_ATTR,
    esc,
    estimate_text_width,
    has_hebrew,
    make_rng,
    resolve_lang,
    stable_seed,
)
from .assets import Asset
from .domains import detect_domain
from .icons import icon_inner
from .palette import Palette, best_text_color, generate_palette, mix

CONCEPTS: tuple[str, ...] = ("monogram", "wordmark", "emblem", "geometric", "icon", "stacked")

_STOP_EN = {"the", "of", "and", "&", "a", "an", "for", "co", "co.", "inc", "inc.", "ltd", "ltd.", "llc", "by", "at"}
_STOP_HE = {"של", "את", "עם", "בע\"מ", "בעמ", "ושות'", "ושות"}


def initials(name: str, max_letters: int = 2) -> str:
    """Monogram letters: 'Blue Bottle Coffee' -> 'BB', 'BeanCraft' -> 'BC', 'קפה נמרוד' -> 'קנ',
    'בית הקפה' -> 'בק' (the definite article is skipped)."""
    words = re.findall(r"[\w֐-׿][\w֐-׿'’\"-]*", name or "")
    sig = [w for w in words if w.lower() not in _STOP_EN and w not in _STOP_HE] or words
    if not sig:
        return "?"
    if len(sig) == 1:
        w = sig[0]
        caps = re.findall(r"[A-Z]", w[1:])
        if not has_hebrew(w) and caps and max_letters > 1:
            return (w[0] + caps[0]).upper()
        return w[0].upper()
    letters = []
    for w in sig[:max_letters]:
        if has_hebrew(w) and len(w) > 3 and w[0] == "ה":
            w = w[1:]
        letters.append(w[0].upper())
    return "".join(letters)


class _Ctx:
    def __init__(self, name: str, tagline: str, palette: Palette, lang: str, seed: int) -> None:
        self.name = name.strip() or "Brand"
        self.tagline = tagline.strip()
        self.p = palette
        self.lang = lang
        self.rtl = has_hebrew(self.name)
        self.rng = make_rng(seed=seed)
        self.uid = f"l{seed % 10**8:x}"
        serif = palette.mood in ("luxury", "elegant")
        self.family = SERIF_STACK_ATTR if serif else FONT_STACK_ATTR
        self.serif = serif
        self.initials = initials(self.name)
        self.domain = detect_domain(f"{name} {tagline}")

    def text(self, x: float, y: float, content: str, *, size: float, fill: str, weight: int = 700,
             anchor: str = "middle", family: str | None = None, spacing: float = 0, extra: str = "") -> str:
        rtl = has_hebrew(content)
        bidi = ' direction="rtl" unicode-bidi="embed"' if rtl else ""
        ls = f' letter-spacing="{spacing:g}"' if spacing else ""
        return (f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-family="{family or self.family}" '
                f'font-size="{size:g}" font-weight="{weight}" fill="{fill}"{ls}{bidi}{extra}>{esc(content)}</text>')

    def gradient(self, gid: str, a: str, b: str, angle: float = 135) -> str:
        rad = math.radians(angle)
        x1, y1 = 50 - 50 * math.cos(rad), 50 - 50 * math.sin(rad)
        x2, y2 = 50 + 50 * math.cos(rad), 50 + 50 * math.sin(rad)
        return (f'<linearGradient id="{gid}" x1="{x1:.0f}%" y1="{y1:.0f}%" x2="{x2:.0f}%" y2="{y2:.0f}%">'
                f'<stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/></linearGradient>')

    def width(self, text: str, size: float, weight: int = 700, spacing: float = 0) -> float:
        w = estimate_text_width(text, size, weight=weight, letter_spacing=spacing)
        return w * (1.06 if self.serif else 1.0)


def _svg(width: float, height: float, body: str, label: str, defs: str = "") -> str:
    w, h = math.ceil(width), math.ceil(height)
    d = f"<defs>{defs}</defs>" if defs else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="{esc(label)}"><title>{esc(label)}</title>{d}{body}</svg>')


def _name_block(ctx: _Ctx, cx: float, top: float, *, name_size: float, align: str = "middle") -> tuple[str, float]:
    """Name + tagline lines; returns markup and the block height."""
    p = ctx.p
    spacing = -1.2 if not ctx.rtl and not ctx.serif else 0
    parts = [ctx.text(cx, top + name_size * 0.82, ctx.name, size=name_size, fill=p.text, weight=800,
                      anchor=align, spacing=spacing)]
    h = name_size * 1.05
    if ctx.tagline:
        tag = ctx.tagline if has_hebrew(ctx.tagline) else ctx.tagline.upper()
        ts = max(13, name_size * 0.3)
        tspace = 0.5 if has_hebrew(ctx.tagline) else ts * 0.16
        parts.append(ctx.text(cx, top + h + ts * 1.3, tag, size=ts, fill=p.muted, weight=500, anchor=align,
                              family=FONT_STACK_ATTR, spacing=tspace))
        h += ts * 1.7
    return "".join(parts), h


def _block_width(ctx: _Ctx, name_size: float) -> float:
    spacing = -1.2 if not ctx.rtl and not ctx.serif else 0
    w = ctx.width(ctx.name, name_size, 800, spacing)
    if ctx.tagline:
        ts = max(13, name_size * 0.3)
        tag = ctx.tagline if has_hebrew(ctx.tagline) else ctx.tagline.upper()
        tspace = 0.5 if has_hebrew(ctx.tagline) else ts * 0.16
        w = max(w, estimate_text_width(tag, ts, weight=500, letter_spacing=tspace))
    return w


def _badge_shape(kind: str, x: float, y: float, s: float, fill: str) -> str:
    if kind == "circle":
        return f'<circle cx="{x + s / 2:.1f}" cy="{y + s / 2:.1f}" r="{s / 2:.1f}" fill="{fill}"/>'
    if kind == "hexagon":
        cx, cy, r = x + s / 2, y + s / 2, s / 2
        pts = " ".join(f"{cx + r * math.cos(math.radians(60 * k - 90)):.1f},{cy + r * math.sin(math.radians(60 * k - 90)):.1f}"
                       for k in range(6))
        return f'<polygon points="{pts}" fill="{fill}" stroke="{fill}" stroke-width="{s * 0.08:.1f}" stroke-linejoin="round"/>'
    if kind == "shield":
        return (f'<path d="M{x + s / 2:.1f} {y:.1f}L{x + s:.1f} {y + s * 0.14:.1f}V{y + s * 0.5:.1f}C{x + s:.1f} '
                f'{y + s * 0.8:.1f} {x + s * 0.72:.1f} {y + s * 0.94:.1f} {x + s / 2:.1f} {y + s:.1f}C{x + s * 0.28:.1f} '
                f'{y + s * 0.94:.1f} {x:.1f} {y + s * 0.8:.1f} {x:.1f} {y + s * 0.5:.1f}V{y + s * 0.14:.1f}Z" fill="{fill}"/>')
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{s:.1f}" height="{s:.1f}" rx="{s * 0.24:.1f}" fill="{fill}"/>'


def _monogram(ctx: _Ctx, variant: int) -> tuple[str, str]:
    p = ctx.p
    shape = ("rounded", "circle", "hexagon", "shield")[variant % 4]
    s, pad, gap = 132.0, 36.0, 30.0
    name_size = 54.0
    bw = _block_width(ctx, name_size)
    width = pad * 2 + s + gap + bw
    height = 204.0
    gid = f"{ctx.uid}-g"
    bx = width - pad - s if ctx.rtl else pad
    by = (height - s) / 2
    ink = best_text_color(mix(p.primary, p.secondary, 0.5), ("#ffffff", p.text))
    mono = ctx.initials
    msize = s * (0.5 if len(mono) == 1 else 0.38)
    body = [
        _badge_shape(shape, bx, by, s, f"url(#{gid})"),
        f'<circle cx="{bx + s * 0.28:.1f}" cy="{by + s * 0.24:.1f}" r="{s * 0.42:.1f}" fill="#ffffff" opacity="0.10"/>'
        if shape in ("rounded", "circle") else "",
        ctx.text(bx + s / 2, by + s / 2 + msize * 0.36, mono, size=msize, fill=ink, weight=800),
    ]
    text_cx = (pad + bw / 2) if ctx.rtl else (pad + s + gap + bw / 2)
    block, bh = _name_block(ctx, text_cx, 0, name_size=name_size)
    body.append(f'<g transform="translate(0 {(height - bh) / 2:.1f})">{block}</g>')
    defs = ctx.gradient(gid, p.primary, p.secondary)
    return _svg(width, height, "".join(body), f"{ctx.name} logo: monogram", defs), f"monogram-{shape}"


def _wordmark(ctx: _Ctx, variant: int) -> tuple[str, str]:
    p = ctx.p
    mark = ("dot", "swoosh", "bar", "highlight")[variant % 4]
    name_size = 84.0
    spacing = -2.0 if not ctx.rtl and not ctx.serif else 0
    nw = ctx.width(ctx.name, name_size, 800, spacing)
    pad = 44.0
    extra_w = name_size * 0.32 if mark == "dot" else (name_size * 0.3 if mark == "bar" else 0)
    tag_w = _block_width(ctx, name_size) if ctx.tagline else 0
    width = pad * 2 + max(nw + extra_w, tag_w)
    height = 180.0 + (34 if ctx.tagline else 0)
    cx = width / 2
    base = 118.0
    body: list[str] = []
    defs = ""
    bidi = ' direction="rtl" unicode-bidi="embed"' if ctx.rtl else ""
    ls = f' letter-spacing="{spacing:g}"' if spacing else ""
    if mark == "dot":
        body.append(f'<text x="{cx:.1f}" y="{base}" text-anchor="middle" font-family="{ctx.family}" '
                    f'font-size="{name_size:g}" font-weight="800" fill="{p.text}"{ls}{bidi}>{esc(ctx.name)}'
                    f'<tspan fill="{p.accent}">.</tspan></text>')
    elif mark == "highlight":
        hw = nw * 0.92
        body.append(f'<rect x="{cx - hw / 2:.1f}" y="{base - name_size * 0.32:.1f}" width="{hw:.1f}" '
                    f'height="{name_size * 0.34:.1f}" rx="6" fill="{p.accent}" opacity="0.55"/>')
        body.append(ctx.text(cx, base, ctx.name, size=name_size, fill=p.text, weight=800, spacing=spacing))
    elif mark == "bar":
        bar_w, gap = name_size * 0.15, name_size * 0.18
        bar_h = name_size * 0.74
        start = cx - (bar_w + gap + nw) / 2
        if ctx.rtl:
            name_cx, bar_x = start + nw / 2, start + nw + gap
        else:
            bar_x, name_cx = start, start + bar_w + gap + nw / 2
        slant = bar_h * 0.21
        top, bottom = base - bar_h, base
        body.append(f'<path d="M{bar_x + slant:.1f} {top:.1f}h{bar_w:.1f}L{bar_x + bar_w:.1f} {bottom:.1f}'
                    f'h-{bar_w:.1f}z" fill="{p.primary}" stroke="{p.primary}" stroke-width="4" '
                    f'stroke-linejoin="round"/>')
        body.append(ctx.text(name_cx, base, ctx.name, size=name_size, fill=p.text, weight=800, spacing=spacing))
    else:
        body.append(ctx.text(cx, base, ctx.name, size=name_size, fill=p.text, weight=800, spacing=spacing))
    if mark == "swoosh":
        sw = nw * 0.86
        x0, x1 = cx - sw / 2, cx + sw / 2
        y0 = base + 16
        gid = f"{ctx.uid}-s"
        defs = ctx.gradient(gid, p.primary, p.accent, 0 if not ctx.rtl else 180)
        body.append(f'<path d="M{x0:.1f} {y0 + 6:.1f}Q{cx:.1f} {y0 - 10:.1f} {x1:.1f} {y0 + 2:.1f}" fill="none" '
                    f'stroke="url(#{gid})" stroke-width="9" stroke-linecap="round"/>')
    if ctx.tagline:
        tag = ctx.tagline if has_hebrew(ctx.tagline) else ctx.tagline.upper()
        ts = 20.0
        body.append(ctx.text(cx, base + 58, tag, size=ts, fill=p.muted, weight=500, family=FONT_STACK_ATTR,
                             spacing=0.5 if has_hebrew(tag) else 3.2))
    return _svg(width, height, "".join(body), f"{ctx.name} logo: wordmark", defs), f"wordmark-{mark}"


def _arc_text_fits(ctx: _Ctx, text: str, radius: float, size: float, spacing: float) -> bool:
    return estimate_text_width(text, size, weight=700, letter_spacing=spacing) <= math.pi * radius * 0.82


def _emblem(ctx: _Ctx, variant: int) -> tuple[str, str]:
    p = ctx.p
    size = 360.0
    c = size / 2
    filled = variant % 2 == 0
    bg = p.primary if filled else p.surface
    ink = p.on_primary if filled else p.primary
    gid = f"{ctx.uid}-e"
    defs = ctx.gradient(gid, p.primary, mix(p.primary, p.secondary, 0.45)) if filled else ""
    body = [
        f'<circle cx="{c}" cy="{c}" r="172" fill="{f"url(#{gid})" if filled else bg}" '
        f'stroke="{p.primary}" stroke-width="{0 if filled else 6}"/>',
        f'<circle cx="{c}" cy="{c}" r="156" fill="none" stroke="{ink}" stroke-width="2.5" opacity="0.85"/>',
        f'<circle cx="{c}" cy="{c}" r="104" fill="none" stroke="{ink}" stroke-width="1.6" opacity="0.7"/>',
    ]
    top_text = ctx.name.upper() if not ctx.rtl else ctx.name
    arc_r = 128.0
    arc_size, arc_spacing = 30.0, 4.0
    while not _arc_text_fits(ctx, top_text, arc_r, arc_size, arc_spacing) and arc_size > 18:
        arc_size -= 2
        arc_spacing = max(1.5, arc_spacing - 0.5)
    use_arc = not ctx.rtl and _arc_text_fits(ctx, top_text, arc_r, arc_size, arc_spacing)
    icon_name = ctx.domain.icons[0]
    if use_arc:
        top_id, bottom_id = f"{ctx.uid}-t", f"{ctx.uid}-b"
        defs += (f'<path id="{top_id}" d="M{c - arc_r} {c} A{arc_r} {arc_r} 0 0 1 {c + arc_r} {c}"/>'
                 f'<path id="{bottom_id}" d="M{c - arc_r - 22} {c} A{arc_r + 22} {arc_r + 22} 0 0 0 {c + arc_r + 22} {c}"/>')
        body.append(f'<text font-family="{ctx.family}" font-size="{arc_size:g}" font-weight="800" fill="{ink}" '
                    f'letter-spacing="{arc_spacing:g}"><textPath href="#{top_id}" startOffset="50%" '
                    f'text-anchor="middle">{esc(top_text)}</textPath></text>')
        bottom = (ctx.tagline or ctx.domain.t("label", "en")).upper()
        bsize = 16.0
        while estimate_text_width(bottom, bsize, weight=600, letter_spacing=3) > math.pi * (arc_r + 22) * 0.62 and bsize > 11:
            bsize -= 1
        body.append(f'<text font-family="{FONT_STACK_ATTR}" font-size="{bsize:g}" font-weight="600" fill="{ink}" '
                    f'letter-spacing="3" opacity="0.92"><textPath href="#{bottom_id}" startOffset="50%" '
                    f'text-anchor="middle">{esc(bottom)}</textPath></text>')
        for sx in (c - 146, c + 146):
            body.append(f'<circle cx="{sx}" cy="{c + 4}" r="4.5" fill="{p.accent}"/>')
        mono = ctx.initials
        msize = 88.0 if len(mono) == 1 else 70.0
        body.append(ctx.text(c, c + msize * 0.36, mono, size=msize, fill=ink, weight=800))
    else:
        for k in range(36):
            a = math.radians(k * 10)
            body.append(f'<circle cx="{c + 140 * math.cos(a):.1f}" cy="{c + 140 * math.sin(a):.1f}" r="2.6" '
                        f'fill="{ink}" opacity="0.75"/>')
        body.append(f'<g transform="translate({c - 22} {c - 86}) scale(1.85)" fill="none" stroke="{ink}" '
                    f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
                    f"{icon_inner(icon_name, color=ink)}</g>")
        nsize = 40.0
        while estimate_text_width(ctx.name, nsize, weight=800) > 176 and nsize > 18:
            nsize -= 2
        ribbon_w = max(estimate_text_width(ctx.name, nsize, weight=800) + 56, 200)
        ry = c + 10
        rib = p.accent
        rib_ink = best_text_color(rib, ("#ffffff", p.text))
        body.append(f'<path d="M{c - ribbon_w / 2 - 18} {ry + 4}l18 -22h{ribbon_w}l18 22-18 22h-{ribbon_w}z" '
                    f'fill="{rib}"/>')
        body.append(ctx.text(c, ry + nsize * 0.36 - 4, ctx.name, size=nsize, fill=rib_ink, weight=800))
        if ctx.tagline:
            tsize = 16.0
            while estimate_text_width(ctx.tagline, tsize, weight=500) > 170 and tsize > 10:
                tsize -= 1
            body.append(ctx.text(c, c + 78, ctx.tagline, size=tsize, fill=ink, weight=500, family=FONT_STACK_ATTR))
    label = f"{ctx.name} logo: emblem"
    return _svg(size, size, "".join(body), label, defs), "emblem-" + ("filled" if filled else "outline")


def _symbol(ctx: _Ctx, kind: str, x: float, y: float, s: float) -> str:
    p = ctx.p
    cx, cy = x + s / 2, y + s / 2
    cols = (p.primary, p.secondary, p.accent)
    if kind == "venn":
        r = s * 0.27
        out = []
        for k, col in enumerate(cols):
            a = math.radians(-90 + 120 * k)
            out.append(f'<circle cx="{cx + r * 0.62 * math.cos(a):.1f}" cy="{cy + r * 0.62 * math.sin(a):.1f}" '
                       f'r="{r:.1f}" fill="{col}" opacity="0.82" style="mix-blend-mode:multiply"/>')
        return "".join(out)
    if kind == "diamonds":
        out = []
        for k, col in enumerate(cols):
            d = s * (0.46 - 0.12 * k)
            out.append(f'<rect x="{cx - d / 2:.1f}" y="{cy - d / 2 + (k - 1) * s * 0.06:.1f}" width="{d:.1f}" '
                       f'height="{d:.1f}" rx="{d * 0.16:.1f}" fill="{col}" opacity="{1 - 0.12 * k:.2f}" '
                       f'transform="rotate(45 {cx:.1f} {cy + (k - 1) * s * 0.06:.1f})"/>')
        return "".join(out)
    if kind == "bloom":
        out = []
        for k in range(6):
            col = cols[k % 3]
            out.append(f'<ellipse cx="{cx:.1f}" cy="{cy - s * 0.2:.1f}" rx="{s * 0.11:.1f}" ry="{s * 0.21:.1f}" '
                       f'fill="{col}" opacity="0.85" transform="rotate({60 * k} {cx:.1f} {cy:.1f})"/>')
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{s * 0.08:.1f}" fill="{p.surface}"/>')
        return "".join(out)
    if kind == "orbit":
        return (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{s * 0.2:.1f}" fill="{p.primary}"/>'
                f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{s * 0.44:.1f}" ry="{s * 0.17:.1f}" fill="none" '
                f'stroke="{p.secondary}" stroke-width="{s * 0.045:.1f}" transform="rotate(-28 {cx:.1f} {cy:.1f})"/>'
                f'<circle cx="{cx + s * 0.36:.1f}" cy="{cy - s * 0.2:.1f}" r="{s * 0.065:.1f}" fill="{p.accent}"/>')
    if kind == "peaks":
        b = y + s * 0.82
        return (f'<path d="M{x + s * 0.06:.1f} {b:.1f}L{x + s * 0.38:.1f} {y + s * 0.26:.1f}L{x + s * 0.62:.1f} {b:.1f}z" '
                f'fill="{p.primary}"/>'
                f'<path d="M{x + s * 0.36:.1f} {b:.1f}L{x + s * 0.64:.1f} {y + s * 0.4:.1f}L{x + s * 0.94:.1f} {b:.1f}z" '
                f'fill="{p.secondary}" opacity="0.9"/>'
                f'<circle cx="{x + s * 0.74:.1f}" cy="{y + s * 0.22:.1f}" r="{s * 0.09:.1f}" fill="{p.accent}"/>')
    # stack: three offset rounded bars
    out = []
    for k, col in enumerate(cols):
        w = s * (0.78 - 0.16 * k)
        out.append(f'<rect x="{cx - w / 2 + (k - 1) * s * 0.07:.1f}" y="{y + s * (0.18 + 0.23 * k):.1f}" '
                   f'width="{w:.1f}" height="{s * 0.17:.1f}" rx="{s * 0.085:.1f}" fill="{col}"/>')
    return "".join(out)


_SYMBOLS = ("venn", "diamonds", "bloom", "orbit", "peaks", "stack")


def _geometric(ctx: _Ctx, variant: int) -> tuple[str, str]:
    kind = _SYMBOLS[(variant + ctx.rng.randrange(len(_SYMBOLS))) % len(_SYMBOLS)]
    s = 150.0
    name_size = 50.0
    bw = _block_width(ctx, name_size)
    pad = 40.0
    width = max(s, bw) + pad * 2
    block, bh = _name_block(ctx, width / 2, pad + s + 22, name_size=name_size)
    height = pad + s + 22 + bh + pad * 0.8
    body = _symbol(ctx, kind, width / 2 - s / 2, pad, s) + block
    return _svg(width, height, body, f"{ctx.name} logo: geometric symbol"), f"geometric-{kind}"


def _icon_mark(ctx: _Ctx, variant: int) -> tuple[str, str]:
    p = ctx.p
    icon_name = ctx.domain.icons[variant % len(ctx.domain.icons)]
    s, pad, gap = 120.0, 34.0, 28.0
    name_size = 50.0
    bw = _block_width(ctx, name_size)
    width = pad * 2 + s + gap + bw
    height = 190.0
    gid = f"{ctx.uid}-i"
    bx = width - pad - s if ctx.rtl else pad
    by = (height - s) / 2
    ink = best_text_color(mix(p.primary, p.accent, 0.4), ("#ffffff", p.text))
    body = [
        f'<rect x="{bx}" y="{by}" width="{s}" height="{s}" rx="{s / 2}" fill="url(#{gid})"/>',
        f'<g transform="translate({bx + s * 0.22:.1f} {by + s * 0.22:.1f}) scale({s * 0.56 / 24:.3f})" fill="none" '
        f'stroke="{ink}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
        f"{icon_inner(icon_name, color=ink)}</g>",
    ]
    text_cx = (pad + bw / 2) if ctx.rtl else (pad + s + gap + bw / 2)
    block, bh = _name_block(ctx, text_cx, 0, name_size=name_size)
    body.append(f'<g transform="translate(0 {(height - bh) / 2:.1f})">{block}</g>')
    defs = ctx.gradient(gid, p.primary, mix(p.primary, p.accent, 0.55))
    return _svg(width, height, "".join(body), f"{ctx.name} logo: icon mark", defs), f"icon-{icon_name}"


def _stacked(ctx: _Ctx, variant: int) -> tuple[str, str]:
    p = ctx.p
    s = 128.0
    name_size = 46.0
    bw = _block_width(ctx, name_size)
    pad = 40.0
    width = max(s, bw) + pad * 2
    cx = width / 2
    ring = variant % 2 == 0
    mono = ctx.initials
    msize = s * (0.46 if len(mono) == 1 else 0.36)
    body = [
        f'<circle cx="{cx}" cy="{pad + s / 2}" r="{s / 2 - 4}" fill="{p.surface if ring else p.primary}" '
        f'stroke="{p.primary}" stroke-width="{6 if ring else 0}"/>',
        f'<circle cx="{cx + s * 0.32:.1f}" cy="{pad + s * 0.16:.1f}" r="{s * 0.08:.1f}" fill="{p.accent}"/>',
        ctx.text(cx, pad + s / 2 + msize * 0.36, mono, size=msize, fill=p.primary if ring else p.on_primary,
                 weight=800),
    ]
    block, bh = _name_block(ctx, cx, pad + s + 20, name_size=name_size)
    body.append(block)
    height = pad + s + 20 + bh + pad * 0.8
    return _svg(width, height, "".join(body), f"{ctx.name} logo: stacked"), "stacked-" + ("ring" if ring else "solid")


_BUILDERS = {"monogram": _monogram, "wordmark": _wordmark, "emblem": _emblem, "geometric": _geometric,
             "icon": _icon_mark, "stacked": _stacked}
_DESCRIPTIONS = {
    "he": {"monogram": "מונוגרמה בתוך תג – ראשי התיבות של השם", "wordmark": "סמליל טקסט עם סימן הדגשה",
           "emblem": "חותם עגול בסגנון אמבלם", "geometric": "סמל גאומטרי מופשט מעל השם",
           "icon": "אייקון של התחום בתוך עיגול לצד השם", "stacked": "מונוגרמה עגולה מעל השם"},
    "en": {"monogram": "Monogram badge with the brand initials", "wordmark": "Wordmark with an accent mark",
           "emblem": "Circular emblem / seal", "geometric": "Abstract geometric symbol above the name",
           "icon": "Industry icon in a circle beside the name", "stacked": "Round monogram stacked above the name"},
}


def generate_logos(name: str, *, tagline: str = "", palette: Palette | None = None, style: str = "auto", count: int = 4,
                   lang: str | None = None) -> list[Asset]:
    """Distinct logo concepts as SVG assets (deterministic for the same input).

    style "auto" cycles monogram, wordmark, emblem, geometric, icon, stacked; any single concept
    name gives `count` variants of that concept.
    """
    lang = resolve_lang(lang, name)
    palette = palette or generate_palette(f"{name} {tagline}")
    seed = stable_seed("logo", name, tagline, palette.name, style)
    rng = make_rng(seed=seed)
    order = list(CONCEPTS) if style in ("auto", "", None) else [style if style in _BUILDERS else "monogram"]
    offset = rng.randrange(4)
    assets: list[Asset] = []
    seen: set[str] = set()
    i = 0
    while len(assets) < max(0, count) and i < count * 6:
        concept = order[i % len(order)]
        variant = i // len(order) + (0 if concept == "icon" else offset)
        ctx = _Ctx(name, tagline, palette, lang, seed + i)
        svg, tag = _BUILDERS[concept](ctx, variant)
        i += 1
        if tag in seen:
            continue
        seen.add(tag)
        n = len(assets) + 1
        assets.append(Asset(name=f"logo-{n}-{tag}", kind="svg", content=svg,
                            description=_DESCRIPTIONS[lang].get(concept, concept)))
    return assets


def favicon_svg(name: str, palette: Palette | None = None) -> str:
    """A tiny square monogram favicon (64x64 viewBox)."""
    palette = palette or generate_palette(name)
    mono = initials(name, 1)
    ink = palette.on_primary
    rtl = ' direction="rtl" unicode-bidi="embed"' if has_hebrew(mono) else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" '
            f'fill="{palette.primary}"/><circle cx="50" cy="14" r="6" fill="{palette.accent}"/>'
            f'<text x="32" y="44" text-anchor="middle" font-family="{FONT_STACK_ATTR}" font-size="34" '
            f'font-weight="800" fill="{ink}"{rtl}>{esc(mono)}</text></svg>')
