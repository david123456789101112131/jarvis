"""Motion design without a browser toolchain: CSS keyframe snippets, SMIL loaders, animated logos.

Everything is plain text (CSS, SVG, HTML) that works offline in any modern browser. All
animations respect `prefers-reduced-motion`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from typing import Literal

from ._util import FONT_STACK, MONO_STACK, esc, stable_seed, text_dir
from .assets import Asset
from .palette import Palette, generate_palette, hex_to_rgb, normalize_hex

AnimationKind = Literal["pulse", "fade-in", "slide-up", "spin", "glow", "bounce", "float", "shimmer"]
LoaderKind = Literal["orbit", "dots", "ring"]
LogoAnimation = Literal["draw", "fade", "pulse"]

ANIMATIONS: tuple[str, ...] = ("pulse", "fade-in", "slide-up", "spin", "glow", "bounce", "float", "shimmer")
LOADERS: tuple[str, ...] = ("orbit", "dots", "ring")
LOGO_ANIMATIONS: tuple[str, ...] = ("draw", "fade", "pulse")
# Animations that play once (the rest loop).
ONE_SHOT: frozenset[str] = frozenset({"fade-in", "slide-up"})

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_SELECTOR_RE = re.compile(r"^[^{}<>;@\\]+$")
_COLOR_RE = re.compile(
    r"^(?:#[0-9a-fA-F]{3,8}|currentColor|transparent|[a-zA-Z]{3,24}|"
    r"(?:rgb|rgba|hsl|hsla|oklch|oklab)\([0-9.,%\s/+-]+(?:deg)?[0-9.,%\s/+-]*\))$"
)

# --------------------------------------------------------------------------------------------
# CSS animations
# --------------------------------------------------------------------------------------------

_KEYFRAMES: dict[str, str] = {
    "pulse": """@keyframes jv-pulse {
  0% { transform: scale(1); box-shadow: 0 0 0 0 var(--jv-ring, rgba(0, 0, 0, .25)); }
  70% { transform: scale(1.05); box-shadow: 0 0 0 18px var(--jv-ring-0, rgba(0, 0, 0, 0)); }
  100% { transform: scale(1); box-shadow: 0 0 0 0 var(--jv-ring-0, rgba(0, 0, 0, 0)); }
}""",
    "fade-in": """@keyframes jv-fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}""",
    "slide-up": """@keyframes jv-slide-up {
  from { opacity: 0; transform: translateY(28px); }
  to { opacity: 1; transform: translateY(0); }
}""",
    "spin": """@keyframes jv-spin {
  to { transform: rotate(360deg); }
}""",
    "glow": """@keyframes jv-glow {
  0%, 100% { box-shadow: 0 0 4px 0 var(--jv-glow-soft, rgba(56, 189, 248, .35)); filter: brightness(1); }
  50% { box-shadow: 0 0 22px 6px var(--jv-glow, rgba(56, 189, 248, .75)); filter: brightness(1.08); }
}""",
    "bounce": """@keyframes jv-bounce {
  0%, 20%, 53%, 100% { transform: translateY(0); animation-timing-function: cubic-bezier(.215, .61, .355, 1); }
  40%, 43% { transform: translateY(-22px); animation-timing-function: cubic-bezier(.755, .05, .855, .06); }
  70% { transform: translateY(-11px); animation-timing-function: cubic-bezier(.755, .05, .855, .06); }
  80% { transform: translateY(0); }
  90% { transform: translateY(-4px); }
}""",
    "float": """@keyframes jv-float {
  0%, 100% { transform: translateY(0) rotate(0deg); }
  50% { transform: translateY(-10px) rotate(-1.5deg); }
}""",
    "shimmer": """@keyframes jv-shimmer {
  from { background-position: 100% 0; }
  to { background-position: 0 0; }
}""",
}

_TIMING: dict[str, str] = {
    "pulse": "cubic-bezier(.4, 0, .6, 1) infinite",
    "fade-in": "ease-out both",
    "slide-up": "cubic-bezier(.2, .7, .2, 1) both",
    "spin": "linear infinite",
    "glow": "ease-in-out infinite",
    "bounce": "infinite both",
    "float": "ease-in-out infinite",
    "shimmer": "linear infinite",
}


def _check_color(color: str) -> str:
    value = color.strip()
    try:
        return normalize_hex(value)
    except ValueError:
        pass
    if not _COLOR_RE.match(value):
        raise ValueError(f"unsupported CSS color: {color!r}")
    return value


def _alpha(color: str, alpha: float) -> str:
    """`color` at the given opacity: rgba() for hex colors, color-mix() for anything else."""
    try:
        r, g, b = hex_to_rgb(color)
        return f"rgba({r}, {g}, {b}, {alpha:g})"
    except ValueError:
        return f"color-mix(in srgb, {color} {round(alpha * 100)}%, transparent)"


def css_animation(kind: AnimationKind, *, selector: str = ".animated", duration_s: float = 1.2,
                  color: str | None = None, delay_s: float = 0.0) -> str:
    """A ready-to-paste CSS snippet: the @keyframes, the rule for `selector`, and a
    reduced-motion override. `color` tints pulse rings, glows and shimmer highlights."""
    if kind not in _KEYFRAMES:
        raise ValueError(f"unknown animation {kind!r}; choose one of {', '.join(ANIMATIONS)}")
    selector = (selector or ".animated").strip()
    if not _SELECTOR_RE.match(selector):
        raise ValueError(f"unsafe CSS selector: {selector!r}")
    if not 0 < duration_s <= 120:
        raise ValueError("duration_s must be between 0 and 120 seconds")
    delay_s = max(0.0, min(float(delay_s), 60.0))
    col = _check_color(color) if color else None

    decls: list[str] = []
    if col:
        if kind == "pulse":
            decls += [f"--jv-ring: {_alpha(col, 0.45)};", f"--jv-ring-0: {_alpha(col, 0)};"]
        elif kind == "glow":
            decls += [f"--jv-glow: {_alpha(col, 0.75)};", f"--jv-glow-soft: {_alpha(col, 0.3)};"]
    if kind == "shimmer":
        base = col or "#94a3b8"
        decls += [
            f"background: linear-gradient(90deg, {_alpha(base, .14)} 25%, {_alpha(base, .32)} 37%, "
            f"{_alpha(base, .14)} 63%);",
            "background-size: 400% 100%;",
        ]
    if kind in ("pulse", "float", "spin", "bounce"):
        decls.append("will-change: transform;")
    if kind == "spin":
        decls.append("transform-origin: center;")
    delay = f" {delay_s:g}s" if delay_s else ""
    decls.append(f"animation: jv-{kind} {duration_s:g}s{delay} {_TIMING[kind]};")
    body = "\n".join(f"  {d}" for d in decls)
    return (
        f"{_KEYFRAMES[kind]}\n"
        f"{selector} {{\n{body}\n}}\n"
        f"@media (prefers-reduced-motion: reduce) {{\n"
        f"  {selector} {{ animation: none; }}\n"
        f"}}\n"
    )


def animation_stylesheet(palette: Palette | None = None, *, prefix: str = "jv-") -> str:
    """Every animation as a utility class (`.jv-pulse`, `.jv-fade-in`, …) in one stylesheet."""
    palette = palette or generate_palette("animation")
    durations = {"pulse": 2.0, "fade-in": 0.8, "slide-up": 0.8, "spin": 1.1, "glow": 2.4, "bounce": 1.6,
                 "float": 4.0, "shimmer": 1.6}
    colors = {"pulse": palette.primary, "glow": palette.accent, "shimmer": palette.muted}
    parts = [f"/* JARVIS animation utilities – palette: {palette.name} */"]
    for kind in ANIMATIONS:
        parts.append(css_animation(kind, selector=f".{prefix}{kind}", duration_s=durations[kind],  # type: ignore[arg-type]
                                   color=colors.get(kind)))
    parts.append("\n".join(f".{prefix}delay-{i} {{ animation-delay: {i * 0.15:.2f}s; }}" for i in range(1, 6)))
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------------------------
# Loaders (SMIL – no CSS or script needed, works inside <img>)
# --------------------------------------------------------------------------------------------

_LOADING = {"he": "טוען…", "en": "Loading…"}


def animated_loader_svg(palette: Palette | None = None, kind: LoaderKind = "orbit", *, size: int = 64,
                        label: str | None = None, lang: str = "en") -> str:
    """A self-animating loading indicator (SMIL), usable inline or as an <img> source."""
    if kind not in LOADERS:
        raise ValueError(f"unknown loader {kind!r}; choose one of {', '.join(LOADERS)}")
    palette = palette or generate_palette("loader")
    p, s, a, track = palette.primary, palette.secondary, palette.accent, palette.border
    text = esc(label or _LOADING["he" if lang == "he" else "en"])
    uid = f"ld{stable_seed(kind, palette.name, p, s, a) % 16**6:06x}"
    spline = 'calcMode="spline" keySplines=".45 0 .55 1;.45 0 .55 1"'
    if kind == "orbit":
        body = (
            f'<circle cx="50" cy="50" r="36" fill="none" stroke="{track}" stroke-width="2" stroke-dasharray="2 7" '
            f'stroke-linecap="round"/>'
            f'<circle cx="50" cy="50" r="20" fill="none" stroke="{track}" stroke-width="1.5" opacity=".8"/>'
            f'<circle cx="50" cy="50" r="9" fill="url(#{uid}-g)">'
            f'<animate attributeName="r" values="8;11;8" dur="1.8s" repeatCount="indefinite" {spline}/></circle>'
            f'<g><circle cx="50" cy="14" r="7" fill="{s}"/>'
            f'<animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="1.6s" '
            f'repeatCount="indefinite"/></g>'
            f'<g><circle cx="50" cy="70" r="4.5" fill="{a}"/>'
            f'<animateTransform attributeName="transform" type="rotate" from="360 50 50" to="0 50 50" dur="1.1s" '
            f'repeatCount="indefinite"/></g>'
        )
    elif kind == "dots":
        dots = []
        for i, (cx, col) in enumerate(((22, p), (50, s), (78, a))):
            begin = f"{i * 0.16:.2f}s"
            dots.append(
                f'<circle cx="{cx}" cy="50" r="8" fill="{col}">'
                f'<animate attributeName="r" values="6;11;6" dur="1.05s" begin="{begin}" repeatCount="indefinite" '
                f'{spline}/>'
                f'<animate attributeName="opacity" values=".45;1;.45" dur="1.05s" begin="{begin}" '
                f'repeatCount="indefinite" {spline}/>'
                f'<animate attributeName="cy" values="50;42;50" dur="1.05s" begin="{begin}" repeatCount="indefinite" '
                f'{spline}/></circle>'
            )
        body = "".join(dots)
    else:  # ring
        body = (
            f'<circle cx="50" cy="50" r="38" fill="none" stroke="{track}" stroke-width="8"/>'
            f'<g><circle cx="50" cy="50" r="38" fill="none" stroke="url(#{uid}-g)" stroke-width="8" '
            f'stroke-linecap="round" stroke-dasharray="1 239" transform="rotate(-90 50 50)">'
            f'<animate attributeName="stroke-dasharray" values="1 239;150 239;150 239" dur="1.5s" '
            f'repeatCount="indefinite"/>'
            f'<animate attributeName="stroke-dashoffset" values="0;-60;-238" dur="1.5s" repeatCount="indefinite"/>'
            f'</circle>'
            f'<animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="2s" '
            f'repeatCount="indefinite"/></g>'
        )
    defs = (f'<defs><linearGradient id="{uid}-g" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{p}"/><stop offset="1" stop-color="{a}"/></linearGradient></defs>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{int(size)}" height="{int(size)}" '
            f'role="img" aria-label="{text}" data-loader="{kind}"><title>{text}</title>{defs}{body}</svg>')


# --------------------------------------------------------------------------------------------
# Animated logos
# --------------------------------------------------------------------------------------------

_SHAPES = {"path", "rect", "circle", "ellipse", "polygon", "polyline", "line", "text"}
_SKIP = {"defs", "clipPath", "mask", "linearGradient", "radialGradient", "symbol", "pattern", "marker", "filter",
         "style", "title", "desc", "metadata", "script"}
_NON_VISUAL = {"defs", "style", "title", "desc", "metadata", "script"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _viewbox_size(root: ET.Element) -> tuple[float, float]:
    vb = root.get("viewBox")
    if vb:
        try:
            parts = [float(v) for v in re.split(r"[\s,]+", vb.strip())]
            if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
                return parts[2], parts[3]
        except ValueError:
            pass
    try:
        return float(root.get("width", "100").rstrip("px")), float(root.get("height", "100").rstrip("px"))
    except ValueError:
        return 100.0, 100.0


def _add_class(el: ET.Element, cls: str) -> None:
    current = el.get("class", "").split()
    if cls not in current:
        el.set("class", " ".join([*current, cls]))


def _add_style(el: ET.Element, decl: str) -> None:
    style = el.get("style", "").strip().rstrip(";")
    el.set("style", f"{style}; {decl}" if style else decl)


def _wrap_children(root: ET.Element, cls: str, *, each: bool) -> None:
    """Move the visual children of root into new <g class=cls> wrappers (one per child when
    `each`, else one for all) so CSS transforms never clobber their own transform attributes."""
    kids = list(root)
    visual = [k for k in kids if _local(k.tag) not in _NON_VISUAL]
    if not visual:
        return
    if each:
        for i, child in enumerate(visual):
            idx = list(root).index(child)
            root.remove(child)
            g = ET.Element(f"{{{_SVG_NS}}}g", {"class": cls, "style": f"--i:{i}"})
            g.append(child)
            root.insert(idx, g)
    else:
        idx = list(root).index(visual[0])
        g = ET.Element(f"{{{_SVG_NS}}}g", {"class": cls})
        for child in visual:
            root.remove(child)
            g.append(child)
        root.insert(idx, g)


def _prepare_draw(root: ET.Element, stroke_width: float) -> int:
    count = 0

    def walk(el: ET.Element, inherited_fill: str | None, inherited_stroke: str | None) -> None:
        nonlocal count
        for child in el:
            name = _local(child.tag)
            if name in _SKIP:
                continue
            fill = child.get("fill", inherited_fill)
            stroke = child.get("stroke", inherited_stroke)
            if name in _SHAPES:
                has_stroke = stroke not in (None, "none")
                has_fill = fill not in ("none",)
                if not has_stroke and has_fill:
                    child.set("stroke", fill or "#000000")
                    child.set("stroke-width", f"{stroke_width:.2f}")
                    _add_class(child, "jv-f")
                if name != "text":
                    child.set("pathLength", "1")
                _add_class(child, "jv-d")
                _add_style(child, f"--i:{count}")
                count += 1
            walk(child, fill, stroke)

    walk(root, root.get("fill"), root.get("stroke"))
    return count


def animate_logo_svg(svg: str, kind: LogoAnimation = "draw", *, duration_s: float = 1.6, loop: bool = False) -> str:
    """Return the SVG with an embedded CSS animation.

    draw  – outlines are traced, then the fills fade in (a classic logo reveal);
    fade  – each part rises and fades in, one after another;
    pulse – the whole mark breathes gently (always loops).
    The result stays a valid standalone SVG and respects prefers-reduced-motion.
    """
    if kind not in LOGO_ANIMATIONS:
        raise ValueError(f"unknown logo animation {kind!r}; choose one of {', '.join(LOGO_ANIMATIONS)}")
    if not 0 < duration_s <= 60:
        raise ValueError("duration_s must be between 0 and 60 seconds")
    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("xlink", _XLINK_NS)
    try:
        root = ET.fromstring(svg.strip())
    except ET.ParseError as exc:
        raise ValueError(f"not a valid SVG document: {exc}") from exc
    if _local(root.tag) != "svg":
        raise ValueError("the root element must be <svg>")

    uid = f"jv{stable_seed(svg, kind, duration_s, loop) % 16**6:06x}"
    _add_class(root, uid)
    w, h = _viewbox_size(root)
    d = duration_s
    loop_rule = "infinite alternate" if loop else "both"

    if kind == "draw":
        n = _prepare_draw(root, max(1.0, min(w, h) / 120))
        step = min(0.12, d * 0.6 / max(1, n))
        css = (
            f".{uid} .jv-d {{ stroke-dasharray: 1; stroke-dashoffset: 1; fill-opacity: 0; "
            f"animation: {uid}-draw {d:g}s cubic-bezier(.65, 0, .35, 1) {loop_rule}, "
            f"{uid}-fill {d * 0.45:.2f}s ease {loop_rule}; "
            f"animation-delay: calc(var(--i) * {step:.3f}s), calc({d * 0.75:.2f}s + var(--i) * {step:.3f}s); }}\n"
            f".{uid} text.jv-d {{ stroke-dasharray: 900; stroke-dashoffset: 900; }}\n"
            f"@keyframes {uid}-draw {{ to {{ stroke-dashoffset: 0; }} }}\n"
            f"@keyframes {uid}-fill {{ to {{ fill-opacity: 1; }} }}\n"
            f".{uid} .jv-f {{ animation-name: {uid}-draw, {uid}-fillf; }}\n"
            f"@keyframes {uid}-fillf {{ to {{ fill-opacity: 1; stroke-opacity: 0; }} }}\n"
            f"@media (prefers-reduced-motion: reduce) {{ .{uid} .jv-d {{ animation: none; stroke-dashoffset: 0; "
            f"fill-opacity: 1; }} .{uid} .jv-f {{ stroke-opacity: 0; }} }}"
        )
    elif kind == "fade":
        _wrap_children(root, "jv-u", each=True)
        css = (
            f".{uid} .jv-u {{ transform-box: fill-box; transform-origin: center; "
            f"animation: {uid}-in {d * 0.6:.2f}s cubic-bezier(.2, .7, .2, 1) {loop_rule}; "
            f"animation-delay: calc(var(--i) * {d * 0.18:.2f}s); }}\n"
            f"@keyframes {uid}-in {{ from {{ opacity: 0; transform: translateY(6%) scale(.94); }} "
            f"to {{ opacity: 1; transform: none; }} }}\n"
            f"@media (prefers-reduced-motion: reduce) {{ .{uid} .jv-u {{ animation: none; }} }}"
        )
    else:  # pulse
        _wrap_children(root, "jv-p", each=False)
        css = (
            f".{uid} .jv-p {{ transform-box: fill-box; transform-origin: center; "
            f"animation: {uid}-pulse {max(d, 1.2) * 1.4:.2f}s ease-in-out infinite; }}\n"
            f"@keyframes {uid}-pulse {{ 0%, 100% {{ transform: scale(1); opacity: 1; }} "
            f"50% {{ transform: scale(1.035); opacity: .92; }} }}\n"
            f"@media (prefers-reduced-motion: reduce) {{ .{uid} .jv-p {{ animation: none; }} }}"
        )

    style = ET.Element(f"{{{_SVG_NS}}}style")
    style.text = "\n" + css + "\n"
    insert_at = 0
    for i, child in enumerate(list(root)):
        if _local(child.tag) in ("title", "desc"):
            insert_at = i + 1
    root.insert(insert_at, style)
    root.set("data-animation", kind)
    return ET.tostring(root, encoding="unicode")


# --------------------------------------------------------------------------------------------
# Demo page
# --------------------------------------------------------------------------------------------

_DEMO_TEXT = {
    "he": {
        "title": "ספריית אנימציות", "lead": "אנימציות CSS ו-SVG מוכנות להעתקה – בלי ספריות ובלי חיבור לאינטרנט.",
        "css": "אנימציות CSS", "loaders": "אנימציות טעינה", "logo": "לוגו מונפש", "replay": "הפעלה מחדש",
        "copy": "העתקת CSS", "copied": "הועתק!", "code": "קוד", "reduced": "המערכת מבקשת פחות תנועה – האנימציות כבויות.",
        "footer": "נוצר אופליין על ידי JARVIS · כל האנימציות מכבדות את ההגדרה 'הפחתת תנועה'",
        "names": {
            "pulse": ("פעימה", "מושכת תשומת לב לכפתור או להתראה"),
            "fade-in": ("הופעה הדרגתית", "כניסה רכה של תוכן לעמוד"),
            "slide-up": ("החלקה למעלה", "כרטיסים שעולים למקומם"),
            "spin": ("סיבוב", "טעינה ופעולות מתמשכות"),
            "glow": ("זוהר", "הדגשה בסגנון ניאון"),
            "bounce": ("קפיצה", "הזמנה לגלול או ללחוץ"),
            "float": ("ריחוף", "תנועה עדינה לאיורים ואייקונים"),
            "shimmer": ("שלד טעינה", "מציין מקום מנצנץ בזמן שהתוכן נטען"),
        },
        "loader_names": {"orbit": "מסלול", "dots": "נקודות", "ring": "טבעת"},
        "logo_names": {"draw": "ציור קווים", "fade": "הופעה בשלבים", "pulse": "נשימה"},
        "card_title": "כרטיס לדוגמה", "card_text": "תוכן שנכנס בתנועה עדינה.", "cta": "לחצו כאן",
    },
    "en": {
        "title": "Animation library", "lead": "Copy-ready CSS and SVG animations: no libraries, no internet connection.",
        "css": "CSS animations", "loaders": "Loaders", "logo": "Animated logo", "replay": "Replay",
        "copy": "Copy CSS", "copied": "Copied!", "code": "Code", "reduced": "Your system asks for reduced motion, so animations are off.",
        "footer": "Generated offline by JARVIS · every animation honours 'reduce motion'",
        "names": {
            "pulse": ("Pulse", "Draws attention to a button or notification"),
            "fade-in": ("Fade in", "A soft entrance for page content"),
            "slide-up": ("Slide up", "Cards that rise into place"),
            "spin": ("Spin", "Loading and ongoing actions"),
            "glow": ("Glow", "Neon-style emphasis"),
            "bounce": ("Bounce", "An invitation to scroll or tap"),
            "float": ("Float", "Gentle motion for illustrations and icons"),
            "shimmer": ("Shimmer", "A skeleton placeholder while content loads"),
        },
        "loader_names": {"orbit": "Orbit", "dots": "Dots", "ring": "Ring"},
        "logo_names": {"draw": "Line drawing", "fade": "Staggered fade", "pulse": "Breathing"},
        "card_title": "Sample card", "card_text": "Content that eases into view.", "cta": "Tap here",
    },
}

_DEMO_ICONS = {"pulse": "bell", "spin": "settings", "glow": "zap", "bounce": "arrow-down", "float": "rocket"}
_DEMO_DURATIONS = {"pulse": 2.0, "fade-in": 1.0, "slide-up": 0.9, "spin": 1.4, "glow": 2.4, "bounce": 1.8,
                   "float": 4.0, "shimmer": 1.6}


def _demo_target(kind: str, t: dict, palette: Palette) -> str:
    from .icons import icon_svg

    if kind in ("fade-in", "slide-up"):
        return (f'<div class="target mini-card"><span class="dot"></span><b>{esc(t["card_title"])}</b>'
                f'<small>{esc(t["card_text"])}</small></div>')
    if kind == "shimmer":
        return ('<div class="target-wrap skeleton"><span class="target sk-avatar"></span>'
                '<span class="sk-lines"><span class="target sk-line"></span><span class="target sk-line short">'
                '</span></span></div>')
    icon = icon_svg(_DEMO_ICONS[kind], size=34, stroke_width=2, color="currentColor")
    if kind == "glow":
        return f'<button type="button" class="target pill">{icon}<span>{esc(t["cta"])}</span></button>'
    return f'<div class="target bubble">{icon}</div>'


def demo_page(animations: Sequence[str] | None = None, palette: Palette | None = None, lang: str = "he", *,
              title: str | None = None, logo_svg: str | None = None, brief: str = "") -> str:
    """A self-contained showcase page: every CSS animation (with copyable code), the three
    loaders and an animated logo in its three reveal styles."""
    lang = "he" if lang != "en" else "en"
    t = _DEMO_TEXT[lang]
    palette = palette or generate_palette(brief or title or "animation")
    kinds = [k for k in (animations or ANIMATIONS) if k in _KEYFRAMES]
    if not kinds:
        raise ValueError("no known animations requested")
    page_title = title or t["title"]

    css_blocks: list[str] = []
    cards: list[str] = []
    for kind in kinds:
        color = palette.accent if kind == "glow" else (palette.primary if kind == "pulse" else palette.muted)
        snippet = css_animation(kind, selector=".animated", duration_s=_DEMO_DURATIONS[kind],  # type: ignore[arg-type]
                                color=color if kind in ("pulse", "glow", "shimmer") else None)
        css_blocks.append(css_animation(kind, selector=f".demo-{kind} .target",  # type: ignore[arg-type]
                                        duration_s=_DEMO_DURATIONS[kind],
                                        color=color if kind in ("pulse", "glow", "shimmer") else None))
        name, desc = t["names"][kind]
        replay = (f'<button type="button" class="ghost replay" data-replay>{esc(t["replay"])}</button>'
                  if kind in ONE_SHOT else "")
        cards.append(
            f'<article class="card demo-{kind}" data-kind="{kind}">'
            f'<div class="stage">{_demo_target(kind, t, palette)}</div>'
            f'<div class="meta"><h3>{esc(name)} <code>{kind}</code></h3><p>{esc(desc)}</p>'
            f'<div class="actions">{replay}<button type="button" class="ghost" data-copy '
            f'data-copied="{esc(t["copied"])}">{esc(t["copy"])}</button>'
            f'</div><details><summary>{esc(t["code"])}</summary><pre dir="ltr"><code>{esc(snippet)}</code></pre>'
            f'</details></div></article>'
        )

    loaders = "".join(
        f'<figure class="loader">{animated_loader_svg(palette, k, size=72, lang=lang)}'  # type: ignore[arg-type]
        f'<figcaption>{esc(t["loader_names"][k])}</figcaption></figure>'
        for k in LOADERS
    )

    if logo_svg is None:
        from .logo import generate_logos

        logo_svg = generate_logos(page_title if brief == "" else brief.split(",")[0][:40], palette=palette,
                                  count=1, lang=lang)[0].read_text()
    from .assets import inline_svg

    logos = "".join(
        f'<figure class="logo-demo" data-logo="{k}"><div class="logo-stage">'
        f'{inline_svg(animate_logo_svg(logo_svg, k), f"lg{i}")}</div>'  # type: ignore[arg-type]
        f'<figcaption>{esc(t["logo_names"][k])} <button type="button" class="ghost" data-replay-logo>'
        f'{esc(t["replay"])}</button></figcaption></figure>'
        for i, k in enumerate(LOGO_ANIMATIONS)
    )

    css = f"""{palette.to_css()}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{margin:0;font-family:{FONT_STACK};background:var(--color-background);color:var(--color-text);line-height:1.55}}
header{{padding:56px 24px 40px;background:radial-gradient(1200px 400px at 85% -10%,rgba(var(--color-accent-rgb),.35),transparent 60%),
linear-gradient(135deg,var(--color-primary),var(--color-secondary));color:var(--color-on-primary)}}
.wrap{{max-width:1180px;margin:0 auto}}
header h1{{margin:0 0 8px;font-size:clamp(30px,4.6vw,52px);letter-spacing:-.02em;line-height:1.1}}
header p{{margin:0;font-size:18px;opacity:.92;max-width:60ch}}
main{{padding:32px 24px 8px}}
section{{margin:0 auto 48px;max-width:1180px}}
h2{{font-size:22px;margin:0 0 18px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:18px}}
.card{{background:var(--color-surface);border:1px solid var(--color-border);border-radius:20px;overflow:hidden;
display:flex;flex-direction:column;box-shadow:0 1px 2px rgba(0,0,0,.04),0 10px 30px rgba(0,0,0,.05)}}
.stage{{height:170px;display:grid;place-items:center;padding:20px;
background:radial-gradient(circle at 50% 40%,rgba(var(--color-primary-rgb),.08),transparent 70%)}}
.meta{{padding:14px 18px 18px;border-top:1px solid var(--color-border);display:flex;flex-direction:column;gap:6px}}
.meta h3{{margin:0;font-size:17px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.meta h3 code{{font:600 12px/1.6 {MONO_STACK};color:var(--color-muted);background:var(--color-background);
border:1px solid var(--color-border);border-radius:6px;padding:0 6px}}
.meta p{{margin:0;color:var(--color-muted);font-size:14px}}
.actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}}
.ghost{{font:inherit;font-size:13px;font-weight:600;padding:5px 12px;border-radius:999px;cursor:pointer;
background:transparent;color:var(--color-primary);border:1px solid var(--color-border)}}
.ghost:hover{{border-color:var(--color-primary)}}
.ghost:focus-visible,.pill:focus-visible{{outline:3px solid var(--color-accent);outline-offset:2px}}
details summary{{cursor:pointer;color:var(--color-muted);font-size:13px}}
pre{{margin:8px 0 0;padding:12px;border-radius:12px;background:#0f172a;color:#e2e8f0;font:12px/1.5 {MONO_STACK};
overflow:auto;max-height:240px;text-align:left}}
.bubble{{width:84px;height:84px;border-radius:50%;display:grid;place-items:center;background:var(--color-primary);
color:var(--color-on-primary);box-shadow:0 8px 24px rgba(var(--color-primary-rgb),.35)}}
.demo-spin .bubble{{background:var(--color-surface);color:var(--color-primary);border:2px solid var(--color-border);box-shadow:none}}
.demo-float .bubble{{background:linear-gradient(135deg,var(--color-secondary),var(--color-accent))}}
.pill{{font:inherit;font-weight:700;font-size:16px;display:inline-flex;align-items:center;gap:10px;padding:14px 22px;
border-radius:999px;border:0;cursor:pointer;background:var(--color-accent);color:#111}}
.pill svg{{width:22px;height:22px}}
.mini-card{{width:200px;padding:16px;border-radius:16px;background:var(--color-background);border:1px solid var(--color-border);
display:flex;flex-direction:column;gap:6px;box-shadow:0 10px 24px rgba(0,0,0,.08)}}
.mini-card .dot{{width:30px;height:30px;border-radius:10px;background:linear-gradient(135deg,var(--color-primary),var(--color-accent))}}
.mini-card small{{color:var(--color-muted)}}
.skeleton{{width:220px;display:flex;gap:12px;align-items:center;padding:16px;border-radius:16px;background:var(--color-background);
border:1px solid var(--color-border)}}
.sk-avatar{{width:46px;height:46px;border-radius:50%;flex:none}}
.sk-lines{{flex:1;display:flex;flex-direction:column;gap:8px}}
.sk-line{{height:12px;border-radius:6px;display:block}}
.sk-line.short{{width:60%}}
.loaders{{display:flex;gap:18px;flex-wrap:wrap}}
.loader{{margin:0;flex:1 1 160px;background:var(--color-surface);border:1px solid var(--color-border);border-radius:20px;
padding:24px;display:flex;flex-direction:column;align-items:center;gap:10px}}
.loader figcaption,.logo-demo figcaption{{font-weight:600;display:flex;gap:10px;align-items:center}}
.logos{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:18px}}
.logo-demo{{margin:0;background:var(--color-surface);border:1px solid var(--color-border);border-radius:20px;padding:20px;
display:flex;flex-direction:column;gap:12px;align-items:center}}
.logo-stage{{width:100%;display:grid;place-items:center;min-height:150px}}
.logo-stage svg{{max-width:100%;height:auto;max-height:170px}}
.note{{display:none;margin:0 0 16px;padding:12px 16px;border-radius:12px;background:var(--color-surface);border:1px solid var(--color-border)}}
@media (prefers-reduced-motion: reduce){{.note{{display:block}}}}
footer{{text-align:center;color:var(--color-muted);font-size:13px;padding:24px}}
{"".join(css_blocks)}"""

    script = """
document.querySelectorAll('[data-replay]').forEach(btn => btn.addEventListener('click', () => {
  const stage = btn.closest('.card').querySelector('.stage');
  stage.innerHTML = stage.innerHTML;
}));
document.querySelectorAll('[data-replay-logo]').forEach(btn => btn.addEventListener('click', () => {
  const stage = btn.closest('.logo-demo').querySelector('.logo-stage');
  stage.innerHTML = stage.innerHTML;
}));
document.querySelectorAll('[data-copy]').forEach(btn => btn.addEventListener('click', async () => {
  const code = btn.closest('.card').querySelector('pre code').textContent;
  const label = btn.textContent;
  try { await navigator.clipboard.writeText(code); } catch (e) {
    const area = document.createElement('textarea'); area.value = code; document.body.appendChild(area);
    area.select(); document.execCommand('copy'); area.remove();
  }
  btn.textContent = btn.dataset.copied; setTimeout(() => { btn.textContent = label; }, 1400);
}));
"""
    cards_html = "".join(cards)
    return f"""<!DOCTYPE html>
<html lang="{lang}" dir="{text_dir(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(page_title)}</title>
<style>
{css}
</style>
</head>
<body>
<header><div class="wrap"><h1>{esc(page_title)}</h1><p>{esc(t["lead"])}</p></div></header>
<main>
<section aria-labelledby="h-css"><h2 id="h-css">{esc(t["css"])}</h2><p class="note" role="status">{esc(t["reduced"])}</p>
<div class="grid">{cards_html}</div></section>
<section aria-labelledby="h-loaders"><h2 id="h-loaders">{esc(t["loaders"])}</h2><div class="loaders">{loaders}</div></section>
<section aria-labelledby="h-logo"><h2 id="h-logo">{esc(t["logo"])}</h2><div class="logos">{logos}</div></section>
</main>
<footer>{esc(t["footer"])}</footer>
<script>{script}</script>
</body>
</html>
"""


def generate_animations(brief: str = "", *, palette: Palette | None = None, lang: str = "he",
                        logo_svg: str | None = None, title: str | None = None) -> list[Asset]:
    """The animation kit as assets: a utility stylesheet, three SVG loaders, an animated logo
    (when a logo is given) and the demo page."""
    palette = palette or generate_palette(brief or "animation")
    lang = "he" if lang != "en" else "en"
    assets = [Asset(name="animations", kind="css", content=animation_stylesheet(palette),
                    description="CSS animation utilities (.jv-pulse, .jv-fade-in, ...)")]
    for k in LOADERS:
        assets.append(Asset(name=f"loader-{k}", kind="svg", content=animated_loader_svg(palette, k, lang=lang),  # type: ignore[arg-type]
                            description=f"Animated loader ({k})"))
    if logo_svg:
        assets.append(Asset(name="logo-animated", kind="svg", content=animate_logo_svg(logo_svg, "draw"),
                            description="Logo with a line-drawing reveal"))
    assets.append(Asset(name="animations-demo", kind="html",
                        content=demo_page(None, palette, lang, title=title, logo_svg=logo_svg, brief=brief),
                        description="Interactive showcase of all animations"))
    return assets
