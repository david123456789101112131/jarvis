"""What the image and video screens make when no creation service is connected: a placeholder that says, in the
picture itself and in both languages, that it is a demonstration and that no real image or video was made.

The placeholders are SVG drawings built here from fixed shapes and the user's escaped text (no scripts, no links,
nothing loaded from anywhere). The jobs that make them are marked `real=False`, and a demonstration can be neither
saved nor sent anywhere.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

DEMO_IMAGE_HE = "הדגמה — לא נוצרה תמונה אמיתית"
DEMO_IMAGE_EN = "Demonstration — no real image was created"
DEMO_VIDEO_HE = "הדגמה — לא נוצר סרטון אמיתי"
DEMO_VIDEO_EN = "Demonstration — no real video was created"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f￾￿]")
_FONT = "Segoe UI, Arial, Helvetica, sans-serif"


def size_for(aspect: str, long_side: int = 1024) -> tuple[int, int]:
    """Pixel size for an aspect ratio such as "16:9" ("auto" and anything unreadable: a square)."""
    try:
        w_text, h_text = aspect.split(":", 1)
        w, h = float(w_text), float(h_text)
    except ValueError:
        return long_side, long_side
    if w <= 0 or h <= 0:
        return long_side, long_side
    if w >= h:
        return long_side, max(1, round(long_side * h / w))
    return max(1, round(long_side * w / h)), long_side


def _text(value: str) -> str:
    return escape(_CONTROL.sub("", value), {'"': "&quot;", "'": "&apos;"})


def _lines(text: str, width: int, count: int) -> list[str]:
    """`text` wrapped into at most `count` lines of about `width` characters (the last one ends with … when cut)."""
    lines: list[str] = []
    current = ""
    for word in (w[:width] for w in _CONTROL.sub(" ", text).split()):
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= width:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) == count:                  # more words than room: the last line says it was cut
            lines[-1] = lines[-1].rstrip(" .,;") + "…"
            return lines
    if current:
        lines.append(current)
    return lines


def _frame(width: int, height: int, label: str) -> list[str]:
    edge = max(8, min(width, height) // 40)
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{_text(label)}">',
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#04101b"/><stop offset="1" stop-color="#0b2b40"/></linearGradient></defs>',
        f'<rect width="{width}" height="{height}" fill="url(#bg)"/>',
        *(f'<line x1="{x}" y1="0" x2="{x}" y2="{height}" stroke="#38d6ff" stroke-opacity="0.08"/>'
          for x in range(0, width, max(32, width // 16))),
        *(f'<line x1="0" y1="{y}" x2="{width}" y2="{y}" stroke="#38d6ff" stroke-opacity="0.08"/>'
          for y in range(0, height, max(32, width // 16))),
        f'<rect x="{edge}" y="{edge}" width="{width - 2 * edge}" height="{height - 2 * edge}" fill="none" '
        f'stroke="#ffb547" stroke-width="{max(2, edge // 3)}" stroke-dasharray="{edge * 2} {edge}"/>',
    ]


def _banner(width: int, y: int, size: int, he: str, en: str) -> list[str]:
    return [
        f'<text x="{width // 2}" y="{y}" text-anchor="middle" direction="rtl" font-family="{_FONT}" '
        f'font-size="{size}" font-weight="700" fill="#ffb547">{_text(he)}</text>',
        f'<text x="{width // 2}" y="{y + round(size * 1.35)}" text-anchor="middle" font-family="{_FONT}" '
        f'font-size="{round(size * 0.62)}" fill="#ffd38a">{_text(en)}</text>',
    ]


def _paragraph(width: int, y: int, size: int, lines: list[str], color: str = "#cfefff") -> list[str]:
    return [f'<text x="{width // 2}" y="{y + i * round(size * 1.4)}" text-anchor="middle" font-family="{_FONT}" '
            f'font-size="{size}" fill="{color}" style="unicode-bidi:plaintext">{_text(line)}</text>'
            for i, line in enumerate(lines)]


def image_svg(description: str, *, aspect: str = "1:1", style_label: str = "", index: int = 1, count: int = 1) -> str:
    """The placeholder for one image of a demonstration."""
    width, height = size_for(aspect)
    unit = max(14, min(width, height) // 24)
    parts = _frame(width, height, f"{DEMO_IMAGE_HE} / {DEMO_IMAGE_EN}")
    parts += _banner(width, round(height * 0.36), round(unit * 1.6), DEMO_IMAGE_HE, DEMO_IMAGE_EN)
    chars = max(16, int(width / (unit * 0.62)))
    parts += _paragraph(width, round(height * 0.36) + unit * 4, unit, _lines(description, chars, 3))
    footer = " · ".join(x for x in (style_label, aspect, f"{index}/{count}" if count > 1 else "") if x)
    if footer:
        parts += _paragraph(width, height - unit * 2, round(unit * 0.8), [footer], "#7fb8d6")
    parts.append("</svg>")
    return "\n".join(parts)


def storyboard_svg(parts_text: dict[str, str], *, labels: dict[str, str], aspect: str = "16:9", seconds: int = 6) -> str:
    """The placeholder for a video: three frames of a storyboard with what the user described, no motion at all."""
    width, height = size_for(aspect, 1280)
    unit = max(14, min(width, height) // 26)
    body = _frame(width, height, f"{DEMO_VIDEO_HE} / {DEMO_VIDEO_EN}")
    body += _banner(width, round(unit * 3.2), round(unit * 1.5), DEMO_VIDEO_HE, DEMO_VIDEO_EN)
    gap = unit
    top = round(unit * 6.4)
    cell_w = (width - 4 * gap) // 3
    cell_h = max(unit * 4, height - top - unit * 4)
    filled = [(labels.get(k, k), v) for k, v in parts_text.items() if v.strip()]
    chars = max(10, int(cell_w / (unit * 0.55)))
    for i in range(3):
        x = gap + i * (cell_w + gap)
        body.append(f'<rect x="{x}" y="{top}" width="{cell_w}" height="{cell_h}" rx="{unit // 2}" fill="#06192a" '
                    f'stroke="#38d6ff" stroke-opacity="0.45"/>')
        body.append(f'<text x="{x + cell_w // 2}" y="{top + unit * 2}" text-anchor="middle" font-family="{_FONT}" '
                    f'font-size="{round(unit * 0.9)}" fill="#38d6ff">{i + 1}/3</text>')
        if i < len(filled):
            label, text = filled[i]
            body.append(f'<text x="{x + cell_w // 2}" y="{top + unit * 3.6:.0f}" text-anchor="middle" '
                        f'font-family="{_FONT}" font-size="{round(unit * 0.8)}" fill="#ffd38a" '
                        f'style="unicode-bidi:plaintext">{_text(label)}</text>')
            lines = _lines(text, chars, max(1, (cell_h - unit * 5) // round(unit * 1.2)))
            body += [f'<text x="{x + cell_w // 2}" y="{top + unit * 5 + j * round(unit * 1.1)}" text-anchor="middle" '
                     f'font-family="{_FONT}" font-size="{round(unit * 0.75)}" fill="#cfefff" '
                     f'style="unicode-bidi:plaintext">{_text(line)}</text>' for j, line in enumerate(lines)]
    body += _paragraph(width, height - unit * 2, round(unit * 0.8), [f"{aspect} · {seconds}s"], "#7fb8d6")
    body.append("</svg>")
    return "\n".join(body)


__all__ = ["DEMO_IMAGE_EN", "DEMO_IMAGE_HE", "DEMO_VIDEO_EN", "DEMO_VIDEO_HE", "image_svg", "size_for",
           "storyboard_svg"]
