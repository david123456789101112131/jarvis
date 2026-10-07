"""Draw the JARVIS icon: the HUD's glowing orb (the same design as ui/public/favicon.svg) on a dark disc,
so it reads on light and dark taskbars alike.

    python scripts/make_icon.py

writes src/jarvis/assets/jarvis.ico (16-256 px, each size drawn on its own, with thicker lines for the
small ones) and src/jarvis/assets/jarvis.png (256 px, for notifications). Needs Pillow only.
"""

from __future__ import annotations

import functools
import itertools
import math
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "jarvis" / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
MASTER = 512                    # every size is drawn at 512 px and scaled down (anti-aliasing)

BACKDROP = (5, 11, 22)          # --bg-1
# The favicon's radial gradient (offset, RGB, alpha), centred at (50%, 45%) of the orb, radius 55%.
STOPS = ((0.0, (216, 246, 255), 1.0), (0.35, (89, 208, 255), 1.0), (0.75, (18, 87, 184), 1.0),
         (1.0, (3, 6, 13), 0.0))
RING = (159, 230, 255)          # inner ring  #9fe6ff
ORBIT = (89, 208, 255)          # the tilted orbit  #59d0ff


def _gradient_at(t: float) -> tuple[int, int, int, int]:
    t = min(max(t, 0.0), 1.0)
    for (o1, c1, a1), (o2, c2, a2) in itertools.pairwise(STOPS):
        if t <= o2:
            f = (t - o1) / (o2 - o1)
            rgb = [round(c1[i] + (c2[i] - c1[i]) * f) for i in range(3)]
            return rgb[0], rgb[1], rgb[2], round(255 * (a1 + (a2 - a1) * f))
    return (*STOPS[-1][1], 0)


@functools.cache
def _orb(scale: float) -> Image.Image:
    """The gradient orb: circle r=30 at (32, 32) of a 64-unit square."""
    img = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    px = img.load()
    cx, cy, r = 32 * scale, (4 + 0.45 * 56) * scale, 0.55 * 56 * scale     # objectBoundingBox of the orb
    edge = 30 * scale
    for y in range(MASTER):
        for x in range(MASTER):
            if math.hypot(x + 0.5 - 32 * scale, y + 0.5 - 32 * scale) <= edge:
                px[x, y] = _gradient_at(math.hypot(x + 0.5 - cx, y + 0.5 - cy) / r)
    return img


def _stroke_layer(draw_shape: Callable[[ImageDraw.ImageDraw], None], opacity: float) -> Image.Image:
    layer = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    draw_shape(ImageDraw.Draw(layer))
    alpha = layer.getchannel("A").point(lambda a: round(a * opacity))
    layer.putalpha(alpha)
    return layer


def draw(size: int) -> Image.Image:
    scale = MASTER / 64
    thick = 1.9 if size <= 24 else 1.45 if size <= 48 else 1.0     # lines stay visible when small

    img = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([1 * scale, 1 * scale, 63 * scale, 63 * scale], fill=(*BACKDROP, 255))
    img.alpha_composite(_orb(scale))

    w = 2 * thick * scale
    img.alpha_composite(_stroke_layer(
        lambda d: d.ellipse([(32 - 21) * scale, (32 - 21) * scale, (32 + 21) * scale, (32 + 21) * scale],
                            outline=(*RING, 255), width=round(w)), 0.85))

    def orbit(d: ImageDraw.ImageDraw) -> None:
        d.ellipse([(32 - 29) * scale, (32 - 9) * scale, (32 + 29) * scale, (32 + 9) * scale],
                  outline=(*ORBIT, 255), width=round(1.6 * thick * scale))

    tilted = _stroke_layer(orbit, 0.8).rotate(18, resample=Image.Resampling.BICUBIC, center=(32 * scale, 32 * scale))
    img.alpha_composite(tilted)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    images = {size: draw(size) for size in SIZES}
    big = images[256]
    big.save(ASSETS / "jarvis.ico", format="ICO", sizes=[(s, s) for s in SIZES],
             append_images=[images[s] for s in SIZES if s != 256])
    big.save(ASSETS / "jarvis.png", format="PNG", optimize=True)
    print(f"wrote {ASSETS / 'jarvis.ico'} ({', '.join(map(str, SIZES))} px) and {ASSETS / 'jarvis.png'}")


if __name__ == "__main__":
    main()
