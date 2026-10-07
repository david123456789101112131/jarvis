"""Color palettes: OKLCH-based generation, WCAG contrast guarantees, CSS/JSON/SVG export.

`generate_palette("אתר לבית קפה")` gives warm browns, `generate_palette("AI startup")` blues and
cyans, `generate_palette("luxury jewelry")` black and gold. Text on background always reaches at
least 4.5:1 contrast (WCAG AA), muted text too, and the text color drawn on primary buttons is
picked for contrast as well.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ._util import FONT_STACK_ATTR, esc, keyword_in, make_rng, norm
from .domains import detect_domain

MOODS: tuple[str, ...] = ("calm", "energetic", "luxury", "nature", "tech", "playful", "minimal", "warm", "elegant")
ROLES: tuple[str, ...] = ("primary", "secondary", "accent", "background", "surface", "text", "muted", "border",
                          "on_primary")

# --------------------------------------------------------------------------------------------
# Color math (sRGB <-> OKLab/OKLCH, HSL, WCAG luminance). No third-party dependencies.
# --------------------------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalize_hex(value: str) -> str:
    """'#ABC' / 'aabbcc' / '#AABBCC' -> '#aabbcc'. Raises ValueError for anything else."""
    m = _HEX_RE.match((value or "").strip())
    if not m:
        raise ValueError(f"not a hex color: {value!r}")
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.lower()


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    h = normalize_hex(value)
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _cbrt(x: float) -> float:
    return math.copysign(abs(x) ** (1 / 3), x)


def rgb_to_oklab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = (_to_linear(c / 255) for c in rgb)
    l_ = _cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m_ = _cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s_ = _cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _oklab_to_linear(lab: tuple[float, float, float]) -> tuple[float, float, float]:
    L, a, b = lab
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )


def hex_to_oklch(value: str) -> tuple[float, float, float]:
    """(L 0..1, C ~0..0.37, h degrees)."""
    L, a, b = rgb_to_oklab(hex_to_rgb(value))
    C = math.hypot(a, b)
    h = math.degrees(math.atan2(b, a)) % 360 if C > 1e-6 else 0.0
    return L, C, h


def _in_gamut(lin: tuple[float, float, float], eps: float = 1e-4) -> bool:
    return all(-eps <= c <= 1 + eps for c in lin)


def oklch_to_hex(L: float, C: float, h: float) -> str:
    """OKLCH -> sRGB hex, reducing chroma (keeping lightness and hue) until the color fits sRGB."""
    L = max(0.0, min(1.0, L))
    C = max(0.0, C)
    rad = math.radians(h)

    def lab(c: float) -> tuple[float, float, float]:
        return L, c * math.cos(rad), c * math.sin(rad)

    lin = _oklab_to_linear(lab(C))
    if not _in_gamut(lin):
        lo, hi = 0.0, C
        for _ in range(24):
            mid = (lo + hi) / 2
            if _in_gamut(_oklab_to_linear(lab(mid))):
                lo = mid
            else:
                hi = mid
        lin = _oklab_to_linear(lab(lo))
    return rgb_to_hex(tuple(_to_srgb(max(0.0, min(1.0, c))) * 255 for c in lin))  # type: ignore[arg-type]


def rgb_to_hsl(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """(h degrees, s %, l %)."""
    r, g, b = (c / 255 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    light = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, round(light * 100, 1)
    d = mx - mn
    sat = d / (2 - mx - mn) if light > 0.5 else d / (mx + mn)
    if mx == r:
        hue = (g - b) / d + (6 if g < b else 0)
    elif mx == g:
        hue = (b - r) / d + 2
    else:
        hue = (r - g) / d + 4
    return round(hue * 60, 1), round(sat * 100, 1), round(light * 100, 1)


def hsl_to_hex(h: float, s: float, light: float) -> str:
    """HSL (degrees, %, %) -> hex."""
    s /= 100
    light /= 100
    c = (1 - abs(2 * light - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = light - c / 2
    sector = int((h % 360) // 60)
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][sector]
    return rgb_to_hex(((r + m) * 255, (g + m) * 255, (b + m) * 255))


def relative_luminance(value: str) -> float:
    r, g, b = (_to_linear(c / 255) for c in hex_to_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str | Color, b: str | Color) -> float:
    """WCAG 2.x contrast ratio between two colors (1.0 .. 21.0)."""
    la = relative_luminance(a.hex if isinstance(a, Color) else a)
    lb = relative_luminance(b.hex if isinstance(b, Color) else b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: str, b: str, t: float) -> str:
    """Linear mix in OKLab: t=0 -> a, t=1 -> b."""
    la, lb = rgb_to_oklab(hex_to_rgb(a)), rgb_to_oklab(hex_to_rgb(b))
    lab = tuple(x + (y - x) * t for x, y in zip(la, lb, strict=True))
    lin = _oklab_to_linear(lab)  # type: ignore[arg-type]
    return rgb_to_hex(tuple(_to_srgb(max(0.0, min(1.0, c))) * 255 for c in lin))  # type: ignore[arg-type]


def best_text_color(background: str, candidates: tuple[str, ...] = ("#ffffff", "#111111")) -> str:
    """The candidate with the highest contrast on `background`."""
    return max(candidates, key=lambda c: contrast_ratio(c, background))


def ensure_contrast(fg: str, bg: str, min_ratio: float = 4.5) -> str:
    """Return `fg`, darkened or lightened just enough (keeping hue) to reach `min_ratio` on `bg`.

    Falls back to pure black or white (whichever is better) when no shade of the hue is enough.
    """
    fg = normalize_hex(fg)
    if contrast_ratio(fg, bg) >= min_ratio:
        return fg
    L, C, h = hex_to_oklch(fg)
    directions = (0.0, 1.0) if relative_luminance(bg) > 0.18 else (1.0, 0.0)
    for target in directions:
        if contrast_ratio(oklch_to_hex(target, C, h), bg) < min_ratio:
            continue
        lo, hi = L, target  # lo fails, hi passes
        for _ in range(30):
            mid = (lo + hi) / 2
            if contrast_ratio(oklch_to_hex(mid, C, h), bg) >= min_ratio:
                hi = mid
            else:
                lo = mid
        return oklch_to_hex(hi, C, h)
    return best_text_color(bg, ("#000000", "#ffffff"))


# --------------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------------

_HUE_NAMES = ((15, "Crimson"), (40, "Coral"), (62, "Cinnamon"), (80, "Amber"), (100, "Gold"), (125, "Olive"),
              (150, "Forest"), (178, "Jade"), (200, "Teal"), (225, "Lagoon"), (248, "Azure"), (270, "Indigo"),
              (295, "Violet"), (320, "Orchid"), (345, "Rose"), (361, "Crimson"))
_NEUTRALS = ((0.22, "Ink"), (0.36, "Graphite"), (0.55, "Slate"), (0.75, "Silver"), (0.93, "Mist"), (1.01, "Snow"))


def hue_name(h: float) -> str:
    for limit, name in _HUE_NAMES:
        if h % 360 < limit:
            return name
    return "Crimson"


def describe_color(value: str) -> str:
    """A short human name such as 'Deep Azure' or 'Mist'."""
    L, C, h = hex_to_oklch(value)
    if C < 0.03:
        return next(name for limit, name in _NEUTRALS if L < limit)
    shade = "Deep " if L < 0.32 else "Dark " if L < 0.46 else "" if L < 0.66 else "Soft " if L < 0.8 else "Light "
    if L >= 0.92:
        shade = "Pale "
    return f"{shade}{hue_name(h)}"


@dataclass(frozen=True)
class Color:
    name: str
    hex: str
    role: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "hex", normalize_hex(self.hex))

    def __str__(self) -> str:
        return self.hex

    @property
    def rgb(self) -> tuple[int, int, int]:
        return hex_to_rgb(self.hex)

    @property
    def hsl(self) -> tuple[float, float, float]:
        return rgb_to_hsl(self.rgb)

    @property
    def oklch(self) -> tuple[float, float, float]:
        return hex_to_oklch(self.hex)

    @property
    def luminance(self) -> float:
        return relative_luminance(self.hex)

    def rgba(self, alpha: float) -> str:
        r, g, b = self.rgb
        return f"rgba({r}, {g}, {b}, {alpha:g})"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "hex": self.hex, "role": self.role, "rgb": list(self.rgb), "hsl": list(self.hsl)}


@dataclass
class Palette:
    name: str
    colors: list[Color]
    mood: str
    base_hue: float
    dark: bool = False
    description: str = field(default="")

    # -- access ---------------------------------------------------------------------------
    def get(self, role: str) -> Color:
        for c in self.colors:
            if c.role == role:
                return c
        raise KeyError(f"palette has no {role!r} color")

    def hex(self, role: str, default: str | None = None) -> str:
        try:
            return self.get(role).hex
        except KeyError:
            if default is None:
                raise
            return default

    @property
    def primary(self) -> str:
        return self.hex("primary")

    @property
    def secondary(self) -> str:
        return self.hex("secondary")

    @property
    def accent(self) -> str:
        return self.hex("accent")

    @property
    def background(self) -> str:
        return self.hex("background")

    @property
    def surface(self) -> str:
        return self.hex("surface")

    @property
    def text(self) -> str:
        return self.hex("text")

    @property
    def muted(self) -> str:
        return self.hex("muted", self.text)

    @property
    def border(self) -> str:
        return self.hex("border", self.surface)

    @property
    def on_primary(self) -> str:
        return self.hex("on_primary", best_text_color(self.primary))

    @property
    def roles(self) -> dict[str, str]:
        return {c.role: c.hex for c in self.colors}

    def contrast_report(self) -> dict[str, float]:
        return {
            "text/background": round(contrast_ratio(self.text, self.background), 2),
            "text/surface": round(contrast_ratio(self.text, self.surface), 2),
            "muted/background": round(contrast_ratio(self.muted, self.background), 2),
            "on_primary/primary": round(contrast_ratio(self.on_primary, self.primary), 2),
            "primary/background": round(contrast_ratio(self.primary, self.background), 2),
        }

    # -- export ---------------------------------------------------------------------------
    def css_variables(self, prefix: str = "--color-") -> dict[str, str]:
        out: dict[str, str] = {}
        for c in self.colors:
            key = prefix + c.role.replace("_", "-")
            out[key] = c.hex
            out[key + "-rgb"] = ", ".join(str(v) for v in c.rgb)
        return out

    def to_css(self, selector: str = ":root", prefix: str = "--color-") -> str:
        """CSS custom properties, e.g. `--color-primary: #1d4ed8;` plus `--color-primary-rgb: 29, 78, 216;`."""
        lines = [f"{selector} {{", f"  color-scheme: {'dark' if self.dark else 'light'};"]
        lines += [f"  {k}: {v};" for k, v in self.css_variables(prefix).items()]
        lines.append("}")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mood": self.mood,
            "base_hue": round(self.base_hue, 1),
            "dark": self.dark,
            "description": self.description,
            "colors": [c.to_dict() for c in self.colors],
            "css_variables": {k: v for k, v in self.css_variables().items() if not k.endswith("-rgb")},
            "contrast": self.contrast_report(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Palette:
        colors = [Color(c.get("name", c["role"]), c["hex"], c["role"]) for c in data["colors"]]
        return cls(name=data.get("name", "Palette"), colors=colors, mood=data.get("mood", "minimal"),
                   base_hue=float(data.get("base_hue", 0.0)), dark=bool(data.get("dark", False)),
                   description=data.get("description", ""))

    @classmethod
    def from_json(cls, text: str) -> Palette:
        return cls.from_dict(json.loads(text))

    def to_svg(self, *, swatch_width: int = 120, swatch_height: int = 168, title: bool = True) -> str:
        """A swatch card: one tall swatch per color with its role and hex code."""
        pad, gap = 24, 12
        top = 64 if title else pad
        n = len(self.colors)
        width = pad * 2 + n * swatch_width + (n - 1) * gap
        height = top + swatch_height + pad
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" role="img" aria-label="{esc(self.name)} palette">',
            f"<title>{esc(self.name)} palette</title>",
            f'<rect width="{width}" height="{height}" rx="20" fill="{self.surface}" stroke="{self.border}"/>',
        ]
        if title:
            parts.append(
                f'<text x="{pad}" y="40" font-family="{FONT_STACK_ATTR}" font-size="22" font-weight="700" '
                f'fill="{self.text}">{esc(self.name)}</text>'
                f'<text x="{width - pad}" y="40" text-anchor="end" font-family="{FONT_STACK_ATTR}" font-size="14" '
                f'fill="{self.muted}">{esc(self.mood)}{" · dark" if self.dark else ""}</text>'
            )
        for i, c in enumerate(self.colors):
            x = pad + i * (swatch_width + gap)
            ink = best_text_color(c.hex, ("#ffffff", "#141414"))
            parts.append(
                f'<g><rect x="{x}" y="{top}" width="{swatch_width}" height="{swatch_height}" rx="14" fill="{c.hex}" '
                f'stroke="{self.border}"/>'
                f'<text x="{x + 12}" y="{top + swatch_height - 34}" font-family="{FONT_STACK_ATTR}" font-size="13" '
                f'font-weight="700" fill="{ink}">{esc(c.role.replace("_", " "))}</text>'
                f'<text x="{x + 12}" y="{top + swatch_height - 14}" font-family="Consolas, monospace" font-size="13" '
                f'fill="{ink}">{c.hex}</text></g>'
            )
        parts.append("</svg>")
        return "".join(parts)


# --------------------------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Profile:
    hues: tuple[float, ...]
    primary: tuple[float, float]  # (L, C) in light mode
    secondary: tuple[float, float, float]  # (hue offset, L, C)
    accent: tuple[float, float, float]
    bg_chroma: float


_PROFILES: dict[str, _Profile] = {
    "calm": _Profile((195, 210, 228, 175, 250), (0.55, 0.09), (35, 0.66, 0.07), (-150, 0.72, 0.10), 0.010),
    "energetic": _Profile((30, 45, 15, 355, 60), (0.62, 0.20), (40, 0.70, 0.17), (175, 0.62, 0.15), 0.012),
    "luxury": _Profile((85, 75, 95), (0.21, 0.012), (-25, 0.45, 0.03), (0, 0.75, 0.12), 0.014),
    "nature": _Profile((145, 130, 155, 120), (0.50, 0.11), (-70, 0.58, 0.08), (-100, 0.70, 0.13), 0.012),
    "tech": _Profile((255, 245, 265, 230), (0.55, 0.20), (-50, 0.70, 0.13), (60, 0.65, 0.20), 0.010),
    "playful": _Profile((330, 20, 300, 60, 200), (0.63, 0.20), (120, 0.72, 0.17), (-120, 0.82, 0.16), 0.016),
    "minimal": _Profile((250, 30, 150, 0, 200), (0.28, 0.02), (0, 0.55, 0.02), (0, 0.60, 0.16), 0.004),
    "warm": _Profile((55, 40, 30, 65), (0.45, 0.09), (-25, 0.62, 0.12), (20, 0.75, 0.14), 0.016),
    "elegant": _Profile((330, 300, 350, 280, 15), (0.42, 0.08), (-40, 0.64, 0.06), (100, 0.74, 0.09), 0.010),
}

_MOOD_WORDS: dict[str, tuple[str, ...]] = {
    "calm": ("calm", "serene", "peaceful", "soft", "relaxing", "רגוע", "רגועה", "שליו", "שלווה", "מרגיע"),
    "energetic": ("energetic", "vibrant", "bold", "dynamic", "energy", "אנרגטי", "אנרגטית", "תוסס", "נועז", "דינמי",
                  "אנרגיה"),
    "luxury": ("luxury", "luxurious", "premium", "exclusive", "יוקרתי", "יוקרתית", "יוקרה", "אקסקלוסיבי", "פרימיום"),
    "nature": ("natural", "earthy", "organic", "טבעי", "טבעית", "אדמתי", "אורגני"),
    "tech": ("futuristic", "neon", "cyber", "digital", "high-tech", "עתידני", "עתידנית", "ניאון", "דיגיטלי", "טכנולוגי"),
    "playful": ("playful", "fun", "colorful", "cheerful", "happy", "שמח", "שמחה", "צבעוני", "צבעונית", "כיפי", "שובב"),
    "minimal": ("minimal", "minimalist", "minimalistic", "clean", "monochrome", "מינימליסטי", "מינימליסטית",
                "מינימלי", "נקי"),
    "warm": ("warm", "cozy", "cosy", "homey", "rustic", "חם", "חמה", "חמים", "ביתי", "כפרי"),
    "elegant": ("elegant", "classy", "sophisticated", "refined", "chic", "אלגנטי", "אלגנטית", "מעודן", "מתוחכם", "שיק"),
}

_COLOR_WORDS: tuple[tuple[tuple[str, ...], float, str | None], ...] = (
    (("red", "crimson", "אדום", "אדומה", "אדומים"), 27, None),
    (("orange", "כתום", "כתומה"), 55, None),
    (("yellow", "צהוב", "צהובה"), 95, None),
    (("gold", "golden", "זהב", "זהוב", "זהובה"), 85, "luxury"),
    (("green", "ירוק", "ירוקה", "ירוקים"), 145, None),
    (("teal", "turquoise", "טורקיז"), 190, None),
    (("cyan", "sky blue", "light blue", "תכלת"), 225, None),
    (("blue", "navy", "כחול", "כחולה", "נייבי"), 258, None),
    (("purple", "violet", "lilac", "סגול", "סגולה", "לילך"), 300, None),
    (("pink", "magenta", "ורוד", "ורודה"), 350, None),
    (("brown", "chocolate", "חום", "חומה", "שוקולד"), 55, "warm"),
    (("black", "שחור", "שחורה", "monochrome"), 260, "minimal"),
)

_DARK_WORDS = ("dark mode", "dark theme", "dark background", "night mode", "מצב כהה", "ערכת נושא כהה", "רקע כהה",
               "רקע שחור", "מצב לילה", "עיצוב כהה", "תצוגה כהה")

_MOOD_ADJ = {"calm": "Calm", "energetic": "Electric", "luxury": "Noir", "nature": "Verdant", "tech": "Digital",
             "playful": "Candy", "minimal": "Pure", "warm": "Toasted", "elegant": "Velvet"}


def detect_mood(brief: str) -> str | None:
    """Explicit mood word in the brief, else the mood of its domain (None when unknown)."""
    text = norm(brief)
    for mood, words in _MOOD_WORDS.items():
        if any(keyword_in(text, w, normalized=True) for w in words):
            return mood
    for words, _, color_mood in _COLOR_WORDS:
        if color_mood and any(keyword_in(text, w, normalized=True) for w in words):
            return color_mood
    return detect_domain(brief).mood


def detect_hue(brief: str) -> float | None:
    """Hue (OKLCH degrees) of a color named in the brief, else of its domain (None when unknown)."""
    text = norm(brief)
    for words, hue, _ in _COLOR_WORDS:
        if any(keyword_in(text, w, normalized=True) for w in words):
            return hue
    return detect_domain(brief).hue


def wants_dark(brief: str) -> bool:
    text = norm(brief)
    return any(keyword_in(text, w, normalized=True) for w in _DARK_WORDS)


def generate_palette(brief: str = "", *, base_color: str | None = None, mood: str | None = None, dark: bool = False,
                     seed: int | None = None) -> Palette:
    """Build a harmonious, accessible palette from a free-text brief (Hebrew or English).

    `base_color` pins the primary color, `mood` overrides the detected mood, `dark` builds a dark
    theme (also enabled by phrases such as "dark mode" / "מצב כהה"), `seed` replaces the
    brief-derived seed. The same arguments always produce the same palette.
    """
    dark = dark or wants_dark(brief)
    rng = make_rng("palette", brief, base_color, mood, dark, seed=seed)
    chosen_mood = (mood or "").strip().lower()
    if chosen_mood not in _PROFILES:
        chosen_mood = detect_mood(brief) or rng.choice(MOODS)
    prof = _PROFILES[chosen_mood]

    base_hex = normalize_hex(base_color) if base_color else None
    if base_hex:
        _, bC, base_hue = hex_to_oklch(base_hex)
        if bC < 0.02:  # a grey base color carries no hue; keep the mood's hue for the accents
            named = detect_hue(brief)
            base_hue = named if named is not None else rng.choice(prof.hues)
    else:
        kw_hue = detect_hue(brief)
        base_hue = (kw_hue if kw_hue is not None else rng.choice(prof.hues)) + rng.uniform(-6, 6)
    base_hue %= 360

    def jit(v: float, amount: float = 0.02) -> float:
        return v + rng.uniform(-amount, amount)

    pL, pC = prof.primary
    s_off, sL, sC = prof.secondary
    a_off, aL, aC = prof.accent
    pC *= rng.uniform(0.92, 1.08)
    sC *= rng.uniform(0.9, 1.1)
    aC *= rng.uniform(0.9, 1.1)
    s_hue = (base_hue + s_off) % 360
    a_hue = (base_hue + a_off) % 360
    if chosen_mood == "luxury":
        a_hue = 85 + rng.uniform(-4, 4)  # gold stays gold whatever the base hue
    tint = base_hue

    if not dark:
        background = oklch_to_hex(0.985, prof.bg_chroma, tint)
        surface = oklch_to_hex(0.997, prof.bg_chroma * 0.4, tint)
        if chosen_mood == "luxury":
            background = oklch_to_hex(0.972, 0.013, 85)
            surface = oklch_to_hex(0.992, 0.006, 85)
        text = oklch_to_hex(0.23, min(0.03, max(0.01, pC * 0.2)), tint)
        muted = oklch_to_hex(0.50, 0.025, tint)
        border = oklch_to_hex(0.90, 0.014, tint)
        primary = base_hex or oklch_to_hex(jit(pL), pC, base_hue)
        secondary = oklch_to_hex(jit(sL), sC, s_hue)
        accent = oklch_to_hex(jit(aL), aC, a_hue)
    else:
        background = oklch_to_hex(0.17, 0.018, tint)
        surface = oklch_to_hex(0.215, 0.022, tint)
        if chosen_mood == "luxury":
            background = oklch_to_hex(0.145, 0.006, 85)
            surface = oklch_to_hex(0.19, 0.009, 85)
        text = oklch_to_hex(0.955, 0.012, tint)
        muted = oklch_to_hex(0.76, 0.02, tint)
        border = oklch_to_hex(0.33, 0.022, tint)
        if chosen_mood == "luxury":
            primary = base_hex or oklch_to_hex(0.78, 0.12, 85)
        elif chosen_mood == "minimal":
            primary = base_hex or oklch_to_hex(0.93, 0.012, base_hue)
        else:
            primary = base_hex or oklch_to_hex(max(jit(pL), 0.70), pC, base_hue)
        secondary = oklch_to_hex(max(jit(sL), 0.74), sC, s_hue)
        accent = oklch_to_hex(max(jit(aL), 0.78), aC, a_hue)

    # Accessibility guarantees.
    text = ensure_contrast(text, background, 7.0)
    text = ensure_contrast(text, surface, 4.5)
    muted = ensure_contrast(muted, background, 4.6)
    muted = ensure_contrast(muted, surface, 4.6)
    if not base_hex:
        primary = ensure_contrast(primary, background, 3.0)  # buttons/links are UI components (1.4.11)
        if not dark and contrast_ratio("#ffffff", primary) < 4.5:
            # Saturated mid-tones look best with white labels: deepen the primary a little if that is enough.
            deeper = ensure_contrast(primary, "#ffffff", 4.5)
            if hex_to_oklch(primary)[0] - hex_to_oklch(deeper)[0] <= 0.1:
                primary = deeper
    on_primary = best_text_color(primary, ("#ffffff", text if not dark else background, "#111111"))
    if contrast_ratio(on_primary, primary) < 4.5:
        on_primary = best_text_color(primary, ("#000000", "#ffffff"))

    roles = {"primary": primary, "secondary": secondary, "accent": accent, "background": background,
             "surface": surface, "text": text, "muted": muted, "border": border, "on_primary": on_primary}
    colors = [Color(describe_color(roles[r]), roles[r], r) for r in ROLES]
    name = f"{_MOOD_ADJ[chosen_mood]} {hue_name(base_hue)}" + (" Night" if dark else "")
    desc = f"{chosen_mood} palette around {hue_name(base_hue).lower()} (hue {base_hue:.0f}°)" + (", dark" if dark else "")
    return Palette(name=name, colors=colors, mood=chosen_mood, base_hue=round(base_hue, 1), dark=dark,
                   description=desc)
