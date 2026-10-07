"""A built-in library of hand-drawn 24x24 stroke icons (outline / filled / duotone).

Icons are stored as small lists of SVG primitives drawn on a 24x24 grid with round caps and
joins (2px stroke by default). Each primitive is tagged:

* ``FILL``   - a closed shape that is filled in the "filled" and "duotone" styles;
* ``LINE``   - a stroke that always uses the icon color;
* ``DETAIL`` - an inner detail drawn on top of a filled shape (knocked out in "filled" style).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from difflib import get_close_matches

from ._util import esc, keyword_in, make_rng, norm
from .assets import Asset
from .domains import detect_domain
from .palette import Palette, generate_palette

LINE, FILL, DETAIL = 0, 1, 2


@dataclass(frozen=True)
class Shape:
    tag: str  # path | circle | rect | ellipse | polyline | polygon
    attrs: tuple[tuple[str, str], ...]
    role: int = LINE

    def attr_dict(self) -> dict[str, str]:
        return dict(self.attrs)


def _n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def P(d: str, role: int = LINE, transform: str | None = None) -> Shape:
    attrs = [("d", d)] + ([("transform", transform)] if transform else [])
    return Shape("path", tuple(attrs), role)


def C(cx: float, cy: float, r: float, role: int = LINE) -> Shape:
    return Shape("circle", (("cx", _n(cx)), ("cy", _n(cy)), ("r", _n(r))), role)


def R(x: float, y: float, w: float, h: float, rx: float = 0, role: int = LINE) -> Shape:
    attrs = [("x", _n(x)), ("y", _n(y)), ("width", _n(w)), ("height", _n(h))]
    if rx:
        attrs.append(("rx", _n(rx)))
    return Shape("rect", tuple(attrs), role)


def E(cx: float, cy: float, rx: float, ry: float, role: int = LINE) -> Shape:
    return Shape("ellipse", (("cx", _n(cx)), ("cy", _n(cy)), ("rx", _n(rx)), ("ry", _n(ry))), role)


def PL(points: str, role: int = LINE) -> Shape:
    return Shape("polyline", (("points", points),), role)


def PG(points: str, role: int = LINE) -> Shape:
    return Shape("polygon", (("points", points),), role)


def _polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def _gear_path(teeth: int = 8, r_out: float = 9.8, r_in: float = 7.6, top: float = 0.36, base: float = 0.56) -> str:
    """A gear outline with flat-topped teeth joined by arcs along the root circle."""
    pitch = 360 / teeth
    parts: list[str] = []
    for i in range(teeth):
        a = i * pitch - 90
        p1 = _polar(12, 12, r_in, a - pitch * base / 2)
        p2 = _polar(12, 12, r_out, a - pitch * top / 2)
        p3 = _polar(12, 12, r_out, a + pitch * top / 2)
        p4 = _polar(12, 12, r_in, a + pitch * base / 2)
        if i == 0:
            parts.append(f"M{_n(p1[0])} {_n(p1[1])}")
        else:
            parts.append(f"A{_n(r_in)} {_n(r_in)} 0 0 1 {_n(p1[0])} {_n(p1[1])}")
        parts.append(f"L{_n(p2[0])} {_n(p2[1])}L{_n(p3[0])} {_n(p3[1])}L{_n(p4[0])} {_n(p4[1])}")
    first = _polar(12, 12, r_in, -90 - pitch * base / 2)
    parts.append(f"A{_n(r_in)} {_n(r_in)} 0 0 1 {_n(first[0])} {_n(first[1])}Z")
    return "".join(parts)


def _star_points(cx: float = 12, cy: float = 12.6, r_out: float = 10, r_in: float = 4.3) -> str:
    pts = [_polar(cx, cy, r_out if k % 2 == 0 else r_in, -90 + k * 36) for k in range(10)]
    return " ".join(f"{_n(x)} {_n(y)}" for x, y in pts)


_TRAY = "M4 16.5v3A1.5 1.5 0 0 0 5.5 21h13a1.5 1.5 0 0 0 1.5-1.5v-3"

ICONS: dict[str, tuple[Shape, ...]] = {
    "home": (P("M3 10.8 12 3.5l9 7.3"), P("M5.5 9v11.5h4.5v-6h4v6h4.5V9", FILL)),
    "search": (C(11, 11, 7, FILL), P("M16.2 16.2 21 21")),
    "settings": (P(_gear_path(), FILL), C(12, 12, 3, DETAIL)),
    "user": (C(12, 8, 4, FILL), P("M4.5 20.5v-.8A5.7 5.7 0 0 1 10.2 14h3.6a5.7 5.7 0 0 1 5.7 5.7v.8", FILL)),
    "users": (C(9, 8, 3.5, FILL), P("M2.5 20v-.5A5.5 5.5 0 0 1 8 14h2a5.5 5.5 0 0 1 5.5 5.5v.5", FILL),
              P("M16 4.6a3.5 3.5 0 0 1 0 6.8"), P("M18.5 14.2a5.5 5.5 0 0 1 3 4.9v.9")),
    "cart": (P("M2.5 3.5h2.2l.8 4"), P("M5.5 7.5H21l-1.9 7.2a1.5 1.5 0 0 1-1.4 1.1H8.5a1.5 1.5 0 0 1-1.5-1.2z", FILL),
             C(9, 20, 1.5, FILL), C(18, 20, 1.5, FILL)),
    "bag": (P("M5 8h14l-1.1 12.1a1 1 0 0 1-1 .9H7.1a1 1 0 0 1-1-.9z", FILL), P("M9 10.5V7a3 3 0 0 1 6 0v3.5")),
    "mail": (R(3, 5, 18, 14, 2, FILL), PL("3.5 6.5 12 13 20.5 6.5", DETAIL)),
    "phone": (P("M5 3.5h3.2l1.6 4.3-2.2 1.4a11 11 0 0 0 7.2 7.2l1.4-2.2 4.3 1.6V19a2 2 0 0 1-2 2A17 17 0 0 1 3 5.5"
                "a2 2 0 0 1 2-2z", FILL),),
    "calendar": (R(3, 4.5, 18, 16.5, 2, FILL), P("M3 9.5h18", DETAIL), P("M8 2.5v4M16 2.5v4"),
                 P("M8 13.5h.01M12 13.5h.01M16 13.5h.01M8 17h.01M12 17h.01", DETAIL)),
    "clock": (C(12, 12, 9, FILL), PL("12 7 12 12 15.5 14", DETAIL)),
    "chart": (P("M3.5 21h17"), R(4.5, 11.5, 2.5, 6.5, 1, FILL), R(10.75, 4, 2.5, 14, 1, FILL),
              R(17, 8, 2.5, 10, 1, FILL)),
    "pie": (P("M11 4.5V13h8.5A8.5 8.5 0 1 1 11 4.5z", FILL), P("M13.5 2.5a8 8 0 0 1 8 8h-8z", FILL)),
    "folder": (P("M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.5h8.5A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1"
                 " 3 18.5z", FILL),),
    "file": (P("M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z", FILL), PL("14 3 14 8 19 8", DETAIL),
             P("M9 13h6M9 17h6", DETAIL)),
    "image": (R(3, 3, 18, 18, 2, FILL), C(8.5, 8.5, 1.8, DETAIL), PL("21 15 16 10 5 21", DETAIL)),
    "camera": (P("M3 8.5A1.5 1.5 0 0 1 4.5 7h2.8l1.7-2.5h6l1.7 2.5h2.8A1.5 1.5 0 0 1 21 8.5v10a1.5 1.5 0 0 1-1.5 1.5"
                 "h-15A1.5 1.5 0 0 1 3 18.5z", FILL), C(12, 13.5, 3.5, DETAIL)),
    "video": (R(2.5, 6, 13, 12, 2, FILL), P("M15.5 10.5 21.5 7v10l-6-3.5", FILL)),
    "music": (P("M9 18V5.5l11-2V16"), C(6.5, 18, 2.5, FILL), C(17.5, 16, 2.5, FILL)),
    "play": (PG("7 4.5 19.5 12 7 19.5", FILL),),
    "pause": (R(6, 4.5, 4, 15, 1, FILL), R(14, 4.5, 4, 15, 1, FILL)),
    "star": (PG(_star_points(), FILL),),
    "heart": (P("M12 20.5S3 15 3 8.8A4.8 4.8 0 0 1 12 6.4a4.8 4.8 0 0 1 9 2.4C21 15 12 20.5 12 20.5z", FILL),),
    "bell": (P("M6 16V10.5a6 6 0 0 1 12 0V16l1.5 2h-15z", FILL), P("M10 20.5a2 2 0 0 0 4 0"), P("M12 2.5v2")),
    "lock": (R(4.5, 10.5, 15, 10.5, 2, FILL), P("M8 10.5V7.5a4 4 0 0 1 8 0v3"), P("M12 14.5v3", DETAIL)),
    "unlock": (R(4.5, 10.5, 15, 10.5, 2, FILL), P("M8 10.5V7.5a4 4 0 0 1 7.6-1.8"), P("M12 14.5v3", DETAIL)),
    "cloud": (P("M7 19h10.5a4.5 4.5 0 0 0 .6-9A6 6 0 0 0 6.3 11.1 4 4 0 0 0 7 19z", FILL),),
    "download": (P("M12 3.5v12"), PL("7 11 12 16 17 11"), P(_TRAY)),
    "upload": (P("M12 16V3.5"), PL("7 8.5 12 3.5 17 8.5"), P(_TRAY)),
    "map-pin": (P("M12 21.5s-7-6.2-7-11.5a7 7 0 0 1 14 0c0 5.3-7 11.5-7 11.5z", FILL), C(12, 10, 2.5, DETAIL)),
    "globe": (C(12, 12, 9, FILL), P("M3 12h18", DETAIL), P("M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18z", DETAIL)),
    "chat": (P("M20.5 11.5a8 8 0 0 1-11.6 7.1L3.5 20.5l1.9-5.1A8 8 0 1 1 20.5 11.5z", FILL),
             P("M8.5 11.5h.01M12 11.5h.01M15.5 11.5h.01", DETAIL)),
    "check": (PL("4.5 12.5 9.5 17.5 19.5 6.5"),),
    "x": (P("M18 6 6 18M6 6l12 12"),),
    "plus": (P("M12 5v14M5 12h14"),),
    "minus": (P("M5 12h14"),),
    "menu": (P("M4 6.5h16M4 12h16M4 17.5h16"),),
    "arrow-left": (P("M19 12H5"), PL("11 6 5 12 11 18")),
    "arrow-right": (P("M5 12h14"), PL("13 6 19 12 13 18")),
    "arrow-up": (P("M12 19V5"), PL("6 11 12 5 18 11")),
    "arrow-down": (P("M12 5v14"), PL("6 13 12 19 18 13")),
    "code": (PL("8 6.5 2.5 12 8 17.5"), PL("16 6.5 21.5 12 16 17.5"), P("M13.8 4.5 10.2 19.5")),
    "terminal": (R(2.5, 4, 19, 16, 2, FILL), PL("7 9.5 10 12.5 7 15.5", DETAIL), P("M12.5 15.5h4.5", DETAIL)),
    "cpu": (R(6, 6, 12, 12, 1.5, FILL), R(9.5, 9.5, 5, 5, 0.5, DETAIL),
            P("M9.5 2.5V6M14.5 2.5V6M9.5 18v3.5M14.5 18v3.5M2.5 9.5H6M2.5 14.5H6M18 9.5h3.5M18 14.5h3.5")),
    "shield": (P("M12 21.5s7.5-3.4 7.5-9.4V5.6L12 2.8 4.5 5.6v6.5c0 6 7.5 9.4 7.5 9.4z", FILL),),
    "lightbulb": (P("M12 2.5a6.5 6.5 0 0 0-4 11.6c.6.5 1 1.3 1 2.1V17h6v-.8c0-.8.4-1.6 1-2.1A6.5 6.5 0 0 0 12 2.5z",
                    FILL), P("M9.5 19.8h5M10.5 22h3")),
    "rocket": (P("M12 2.5c3.2 2.4 4.8 5.8 4.8 9.6V17H7.2v-4.9C7.2 8.3 8.8 4.9 12 2.5z", FILL), C(12, 10, 1.8, DETAIL),
               P("M7.2 12.5 4.5 15.5V19l2.7-1.5M16.8 12.5l2.7 3V19l-2.7-1.5"),
               P("M10 19.5c.4 1.2 1.1 2 2 2.5.9-.5 1.6-1.3 2-2.5")),
    "gift": (R(3.5, 8, 17, 4, 1, FILL), P("M5 12v8a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-8", FILL), P("M12 8v13", DETAIL),
             P("M12 8C10.5 4.5 7 4 7 6.2 7 8 12 8 12 8zM12 8c1.5-3.5 5-4 5-1.8C17 8 12 8 12 8z")),
    "coffee": (P("M4 8.5h12v6a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5z", FILL), P("M16 10h1.5a2.5 2.5 0 0 1 0 5H16"),
               P("M7 2.5c.8.8-.8 1.7 0 2.5M10 2.5c.8.8-.8 1.7 0 2.5M13 2.5c.8.8-.8 1.7 0 2.5"), P("M3 21.5h14")),
    "book": (P("M5 4.5A1.5 1.5 0 0 1 6.5 3H19.5v14.5H6.8A1.8 1.8 0 0 0 5 19.3z", FILL),
             P("M5 19.3A1.8 1.8 0 0 0 6.8 21H19.5v-3.5"), P("M9 7.5h6.5", DETAIL)),
    "briefcase": (R(3, 7, 18, 13, 2, FILL), P("M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7"),
                  P("M3 12.5h18", DETAIL)),
    "truck": (R(2, 4, 12, 11, 1, FILL), P("M14 8h3.8l3.2 3.6V15H14z", FILL), C(6.5, 19.2, 1.8, FILL),
              C(17, 19.2, 1.8, FILL)),
    "edit": (P("M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z", FILL), P("M14.5 5.5l3 3", DETAIL)),
    "trash": (P("M4 6.5h16"), P("M9 6.5V4.5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"),
              P("M6 6.5l1 13a1.5 1.5 0 0 0 1.5 1.5h7a1.5 1.5 0 0 0 1.5-1.5l1-13", FILL), P("M10 11v6M14 11v6", DETAIL)),
    "share": (C(18, 5, 2.5, FILL), C(6, 12, 2.5, FILL), C(18, 19, 2.5, FILL), P("M8.2 10.8l7.6-4.5M8.2 13.2l7.6 4.5")),
    "link": (P("M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1.2 1.2"),
             P("M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1.2-1.2")),
    "filter": (PG("3 4.5 21 4.5 14 12.8 14 19.5 10 21 10 12.8", FILL),),
    "grid": (R(3.5, 3.5, 7, 7, 1.5, FILL), R(13.5, 3.5, 7, 7, 1.5, FILL), R(3.5, 13.5, 7, 7, 1.5, FILL),
             R(13.5, 13.5, 7, 7, 1.5, FILL)),
    "list": (P("M9 6h11M9 12h11M9 18h11"), P("M4.5 6h.01M4.5 12h.01M4.5 18h.01")),
    "eye": (P("M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z", FILL), C(12, 12, 3, DETAIL)),
    "sun": (C(12, 12, 4, FILL),
            P("M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4")),
    "moon": (P("M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a9 9 0 0 0 11 11z", FILL),),
    "zap": (PG("13 2.5 4 13.5 11.5 13.5 10.5 21.5 20 10.5 12.5 10.5", FILL),),
    "award": (C(12, 9, 6, FILL), P("M8.6 13.9 7.3 21.5l4.7-2.8 4.7 2.8-1.3-7.6")),
    "tag": (P("M3 12.2V4.5A1.5 1.5 0 0 1 4.5 3h7.7a1.5 1.5 0 0 1 1.1.4l7.4 7.4a1.5 1.5 0 0 1 0 2.1l-7.6 7.6"
              "a1.5 1.5 0 0 1-2.1 0L3.4 13.3a1.5 1.5 0 0 1-.4-1.1z", FILL), C(7.5, 7.5, 1.5, DETAIL)),
    "credit-card": (R(2.5, 5, 19, 14, 2, FILL), P("M2.5 10h19", DETAIL), P("M6 15h4", DETAIL)),
    "wallet": (R(3, 6, 18, 14, 2.5, FILL), P("M21 10.5h-4.5a2 2 0 0 0 0 4H21", DETAIL), P("M5.5 6 15 3.2l1.2 2.8")),
    "info": (C(12, 12, 9, FILL), P("M12 11v5.5", DETAIL), P("M12 7.5h.01", DETAIL)),
    "alert": (P("M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", FILL),
              P("M12 9v4.5", DETAIL), P("M12 17.5h.01", DETAIL)),
    "help": (C(12, 12, 9, FILL), P("M9.2 9.3a2.9 2.9 0 0 1 5.6 1c0 1.9-2.8 2.7-2.8 2.7", DETAIL),
             P("M12 17h.01", DETAIL)),
    "refresh": (P("M20.5 11A8.5 8.5 0 0 0 5.6 6.4L3.5 8.5"), PL("3.5 3.5 3.5 8.5 8.5 8.5"),
                P("M3.5 13a8.5 8.5 0 0 0 14.9 4.6l2.1-2.1"), PL("20.5 20.5 20.5 15.5 15.5 15.5")),
    "external-link": (P("M18 13.5V19a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 19V8a1.5 1.5 0 0 1 1.5-1.5H11"),
                      PL("15 3.5 20.5 3.5 20.5 9"), P("M10 14 20.5 3.5")),
    "layers": (PG("12 2.5 2.5 7.5 12 12.5 21.5 7.5", FILL), PL("2.5 16.5 12 21.5 21.5 16.5"),
               PL("2.5 12 12 17 21.5 12")),
    "bookmark": (P("M18.5 21 12 16.5 5.5 21V4.5A1.5 1.5 0 0 1 7 3h10a1.5 1.5 0 0 1 1.5 1.5z", FILL),),
    "send": (PG("21.5 2.5 14.5 21.5 10.5 13.5 2.5 9.5", FILL), P("M21.5 2.5 10.5 13.5", DETAIL)),
    "mic": (R(9, 2.5, 6, 11.5, 3, FILL), P("M5.5 11a6.5 6.5 0 0 0 13 0"), P("M12 17.5v4M8.5 21.5h7")),
    "wifi": (P("M2 8.8a15 15 0 0 1 20 0"), P("M5 12.4a10.5 10.5 0 0 1 14 0"), P("M8.5 15.9a5.5 5.5 0 0 1 7 0"),
             P("M12 19.5h.01")),
    "battery": (R(2.5, 7, 17, 10, 2, FILL), P("M22 11v2"), P("M6 10.5v3M9.5 10.5v3M13 10.5v3", DETAIL)),
    "leaf": (P("M4.5 19.5c-1-8.5 4.5-15 15.5-15.5.4 11.5-6.5 16-15.5 15.5z", FILL),
             P("M4.5 19.5C8 15 11.5 12 15 9.5", DETAIL)),
    "palette": (P("M12 3a9 9 0 0 0 0 18c1.1 0 1.7-.8 1.7-1.7 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-.9.8-1.7 1.7-1.7"
                  "h2.1A4.5 4.5 0 0 0 21 10.7C21 6.4 17 3 12 3z", FILL),
                C(7.5, 11, 1.2, DETAIL), C(10, 7, 1.2, DETAIL), C(14.5, 7, 1.2, DETAIL)),
    "sparkles": (P("M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6z", FILL),
                 P("M18.5 14.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z", FILL), P("M5 15.5v4M3 17.5h4")),
    "target": (C(12, 12, 9, FILL), C(12, 12, 5.5, DETAIL), C(12, 12, 2, DETAIL)),
    "trophy": (P("M7 3.5h10v5a5 5 0 0 1-10 0z", FILL), P("M7 5.5H4.5V7A3 3 0 0 0 7 10M17 5.5h2.5V7a3 3 0 0 1-2.5 3"),
               P("M12 13.5v4"), R(8.5, 17.5, 7, 3.5, 1, FILL)),
    "smile": (C(12, 12, 9, FILL), P("M8 14a4.5 4.5 0 0 0 8 0", DETAIL), P("M9 9.5h.01M15 9.5h.01", DETAIL)),
    "wrench": (P("M14.5 3.5a5 5 0 0 0-4.7 6.7L3.6 16.4a2 2 0 0 0 2.8 2.8l6.2-6.2a5 5 0 0 0 6.7-4.7l-2.8 2.8-2.8-.8"
                 "-.8-2.8z", FILL),),
    "database": (E(12, 5.5, 8, 3, FILL), P("M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13", FILL),
                 P("M4 5.5c0 1.7 3.6 3 8 3s8-1.3 8-3", DETAIL), P("M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3", DETAIL)),
    "server": (R(3, 3.5, 18, 7, 1.5, FILL), R(3, 13.5, 18, 7, 1.5, FILL),
               P("M7 7h.01M10 7h.01M7 17h.01M10 17h.01", DETAIL)),
    "monitor": (R(2.5, 3.5, 19, 13, 2, FILL), P("M8.5 21h7M12 16.5V21")),
    "smartphone": (R(6, 2.5, 12, 19, 2.5, FILL), P("M11 18.5h2", DETAIL)),
    "store": (P("M3.5 9.5 5 4h14l1.5 5.5"), P("M3.5 9.5a2.8 2.8 0 0 0 5.7 0 2.8 2.8 0 0 0 5.6 0 2.8 2.8 0 0 0 5.7 0"),
              P("M5 12.3v8.2h14v-8.2", FILL), P("M10 20.5v-5h4v5", DETAIL)),
    "package": (P("M12 2.8 20.5 7.3v9.4L12 21.2l-8.5-4.5V7.3z", FILL), P("M3.5 7.3 12 11.8l8.5-4.5", DETAIL),
                P("M12 11.8v9.4", DETAIL), P("M7.8 5 16.2 9.6", DETAIL)),
    "percent": (P("M19 5 5 19"), C(6.5, 6.5, 2.5, FILL), C(17.5, 17.5, 2.5, FILL)),
    "dollar": (P("M12 2.5v19"), P("M16.5 6.5H10a3 3 0 0 0 0 6h4a3 3 0 0 1 0 6H7")),
    "shekel": (P("M4.5 19.5v-15h6a4 4 0 0 1 4 4v6"), P("M19.5 4.5v15h-6a4 4 0 0 1-4-4v-6")),
    "map": (P("M3 6.5 8.5 4l7 3 5.5-2.5v13l-5.5 2.5-7-3L3 19.5z", FILL), P("M8.5 4v13M15.5 7v13", DETAIL)),
    "school": (P("M2.5 9 12 4.5 21.5 9 12 13.5z", FILL), P("M6.5 11v5c1.5 1.5 3.5 2.3 5.5 2.3s4-.8 5.5-2.3v-5"),
               P("M21.5 9v5")),
    "dumbbell": (P("M7 12h10"), R(4, 6.5, 3, 11, 1, FILL), R(17, 6.5, 3, 11, 1, FILL), P("M2.5 9.5v5M21.5 9.5v5")),
    "car": (P("M5 11l1.7-4.4A2 2 0 0 1 8.6 5.3h6.8a2 2 0 0 1 1.9 1.3L19 11"), R(3, 11, 18, 6, 2, FILL),
            P("M6 17v2.5M18 17v2.5"), P("M7 14h.01M17 14h.01", DETAIL)),
    "plane": (P("M12 2.5c.7 0 1.2.8 1.2 1.7V9.5l8.3 4.5v1.5l-8.3-2.3v5l2.8 2.3v1l-4-1-4 1v-1l2.8-2.3v-5l-8.3 2.3V14"
                "l8.3-4.5V4.2c0-.9.5-1.7 1.2-1.7z", FILL, "rotate(45 12 12)"),),
    "key": (C(7.5, 15.5, 4.5, FILL), P("M10.7 12.3 20.5 2.5"), P("M16.5 6.5l3 3M14 9l2.5 2.5")),
    "ticket": (P("M3 8.5V7a1.5 1.5 0 0 1 1.5-1.5h15A1.5 1.5 0 0 1 21 7v1.5a2.5 2.5 0 0 0 0 5V17a1.5 1.5 0 0 1-1.5 1.5"
                 "h-15A1.5 1.5 0 0 1 3 17v-3.5a2.5 2.5 0 0 0 0-5z", FILL), P("M14.5 5.5v2M14.5 11v2M14.5 16.5v2", DETAIL)),
    "flame": (P("M12 21.5c-3.9 0-7-2.8-7-6.7 0-3.9 3.5-6 4-10.3 2.5 1.4 4 3.8 4 6.3 1-.8 1.6-2 1.8-3.3 2.4 1.8 4.2"
                " 4.4 4.2 7.3 0 3.9-3.1 6.7-7 6.7z", FILL),),
    "flower": (C(12, 7.2, 3.4, FILL), C(16.8, 12, 3.4, FILL), C(12, 16.8, 3.4, FILL), C(7.2, 12, 3.4, FILL),
               C(12, 12, 2.3, DETAIL)),
    "tree": (P("M12 2.5 6 10.5h3L5 15.5h14l-4-5h3z", FILL), P("M12 15.5v6")),
    "paw": (E(12, 16, 4.5, 3.8, FILL), C(5.8, 10.2, 1.8, FILL), C(9.3, 6, 1.8, FILL), C(14.7, 6, 1.8, FILL),
            C(18.2, 10.2, 1.8, FILL)),
    "utensils": (P("M6 2.5v6.5a2.5 2.5 0 0 0 5 0V2.5"), P("M8.5 2.5v19"),
                 P("M18 21.5v-19c-2.2 1.5-3.5 4.5-3.5 7.5v3.5H18", FILL)),
    "shirt": (P("M8.5 3.5 4 5.5 2.5 10.5l3 1.2L7 10v10.5h10V10l1.5 1.7 3-1.2L20 5.5l-4.5-2a3.5 3.5 0 0 1-7 0z",
                FILL),),
    "dress": (P("M9 2.5 12 5l3-2.5 1 4.5-1.5 4 5 10.5h-15l5-10.5L8 7z", FILL), P("M9.5 11h5", DETAIL)),
    "shoe": (P("M2.5 17V8.5h4.2l2 3 4.8 1.6 6.5 1.3a2.4 2.4 0 0 1 1.9 2.3V17z", FILL),
             P("M2.5 17v1.8a1.2 1.2 0 0 0 1.2 1.2h16.6a1.2 1.2 0 0 0 1.2-1.2V17"), P("M10 12.3l1.1-1.7M12.8 13.2l1.1-1.7",
                                                                                   DETAIL)),
    "keyboard": (R(2, 6, 20, 12, 2, FILL), P("M6 10h.01M10 10h.01M14 10h.01M18 10h.01M8 14h8", DETAIL)),
    "building": (R(4, 3, 11, 18, 1, FILL), P("M15 9h4a1 1 0 0 1 1 1v11"), P("M2.5 21h19"),
                 P("M8 7h3M8 11h3M8 15h3", DETAIL)),
    "headphones": (P("M3.5 18v-5.5a8.5 8.5 0 0 1 17 0V18"),
                   P("M20.5 19a2 2 0 0 1-2 2h-1a1 1 0 0 1-1-1v-4.5a1 1 0 0 1 1-1h3z", FILL),
                   P("M3.5 19a2 2 0 0 0 2 2h1a1 1 0 0 0 1-1v-4.5a1 1 0 0 0-1-1h-3z", FILL)),
    "flag": (P("M5 21.5V4"), P("M5 4.5h12.5l-2.5 4 2.5 4H5", FILL)),
}

ICON_ALIASES: dict[str, str] = {
    "gear": "settings", "cog": "settings", "profile": "user", "account": "user", "person": "user", "team": "users",
    "people": "users", "shopping-cart": "cart", "basket": "cart", "shopping-bag": "bag", "email": "mail",
    "envelope": "mail", "call": "phone", "date": "calendar", "schedule": "calendar", "time": "clock",
    "bar-chart": "chart", "stats": "chart", "analytics": "chart", "pie-chart": "pie", "document": "file",
    "photo": "image", "picture": "image", "film": "video", "song": "music", "favorite": "heart", "like": "heart",
    "notification": "bell", "notifications": "bell", "secure": "lock", "security": "shield", "close": "x",
    "add": "plus", "remove": "minus", "hamburger": "menu", "location": "map-pin", "pin": "map-pin", "world": "globe",
    "message": "chat", "comment": "chat", "done": "check", "ok": "check", "back": "arrow-left", "next": "arrow-right",
    "processor": "cpu", "chip": "cpu", "idea": "lightbulb", "bulb": "lightbulb", "launch": "rocket",
    "present": "gift", "cafe": "coffee", "cup": "coffee", "work": "briefcase", "job": "briefcase",
    "delivery": "truck", "shipping": "truck", "pencil": "edit", "delete": "trash", "bin": "trash", "bolt": "zap",
    "lightning": "zap", "price": "tag", "sale": "tag", "card": "credit-card", "payment": "credit-card",
    "money": "wallet", "warning": "alert", "question": "help", "reload": "refresh", "sync": "refresh",
    "sparkle": "sparkles", "magic": "sparkles", "goal": "target", "prize": "trophy", "tool": "wrench",
    "tools": "wrench", "storage": "database", "screen": "monitor", "desktop": "monitor", "mobile": "smartphone",
    "shop": "store", "box": "package", "discount": "percent", "fire": "flame", "plant": "leaf", "pet": "paw",
    "dog": "paw", "restaurant": "utensils", "food": "utensils", "graduation": "school", "education": "school",
    "gym": "dumbbell", "fitness": "dumbbell", "flight": "plane", "travel": "plane", "airplane": "plane",
    "audio": "headphones", "event": "ticket", "clothes": "shirt", "tshirt": "shirt", "t-shirt": "shirt",
    "sneaker": "shoe", "office": "building", "ils": "shekel", "nis": "shekel", "usd": "dollar",
}

ICON_NAMES: tuple[str, ...] = tuple(sorted(ICONS))
STYLES = ("outline", "filled", "duotone")


def resolve_icon(name: str) -> str:
    """Canonical icon name for a name or alias (case/spacing insensitive). Raises KeyError if unknown."""
    key = (name or "").strip().lower().replace("_", "-").replace(" ", "-")
    key = ICON_ALIASES.get(key, key)
    if key not in ICONS:
        hint = get_close_matches(key, list(ICONS) + list(ICON_ALIASES), n=3)
        raise KeyError(f"unknown icon {name!r}" + (f"; did you mean {', '.join(hint)}?" if hint else ""))
    return key


def icon_shapes(name: str) -> tuple[Shape, ...]:
    return ICONS[resolve_icon(name)]


def _shape_markup(shape: Shape, *, style: str, color: str, accent: str, knockout: str) -> str:
    attrs = shape.attr_dict()
    extra = ""
    if style == "filled":
        if shape.role == FILL:
            extra = f' fill="{color}"'
        elif shape.role == DETAIL:
            extra = f' stroke="{knockout}"'
    elif style == "duotone" and shape.role == FILL:
        extra = f' fill="{accent}" fill-opacity="0.28"'
    body = " ".join(f'{k}="{esc(v)}"' for k, v in attrs.items())
    return f"<{shape.tag} {body}{extra}/>"


def icon_inner(name: str, *, style: str = "outline", color: str = "currentColor", accent: str | None = None,
               knockout: str = "#ffffff") -> str:
    """The icon's primitives without the outer <svg> (for sprites and inline composition)."""
    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}")
    shapes = icon_shapes(name)
    ordered = sorted(shapes, key=lambda s: 1 if s.role == DETAIL else 0) if style == "filled" else shapes
    return "".join(_shape_markup(s, style=style, color=color, accent=accent or color, knockout=knockout)
                   for s in ordered)


def icon_svg(name: str, *, size: int | float = 24, stroke_width: float = 2, color: str = "currentColor",
             style: str = "outline", accent: str | None = None, label: str | None = None,
             css_class: str | None = None, knockout: str = "#ffffff") -> str:
    """A complete, scalable SVG for one icon.

    style: "outline" (strokes only), "filled" (solid shapes, details knocked out in `knockout`)
    or "duotone" (strokes plus a translucent `accent` fill). With `label` the SVG is announced
    to screen readers; without it the icon is decorative (aria-hidden).
    """
    canonical = resolve_icon(name)
    inner = icon_inner(canonical, style=style, color=color, accent=accent, knockout=knockout)
    a11y = f'role="img" aria-label="{esc(label)}"' if label else 'aria-hidden="true" focusable="false"'
    cls = f' class="{esc(css_class)}"' if css_class else ""
    title = f"<title>{esc(label)}</title>" if label else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_n(size)}" height="{_n(size)}" viewBox="0 0 24 24" '
        f'fill="none" stroke="{esc(color)}" stroke-width="{_n(stroke_width)}" stroke-linecap="round" '
        f'stroke-linejoin="round" {a11y}{cls} data-icon="{canonical}">{title}{inner}</svg>'
    )


def icon_data_uri(name: str, **kwargs: object) -> str:
    """The icon as a data: URI (handy for CSS backgrounds)."""
    from urllib.parse import quote

    return "data:image/svg+xml;charset=utf-8," + quote(icon_svg(name, **kwargs))  # type: ignore[arg-type]


# Keyword -> icon hints (Hebrew + English) beyond the domain icon sets.
_ICON_KEYWORDS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("contact", "צור קשר", "יצירת קשר", "contact us"), ("mail", "phone", "map-pin")),
    (("shop", "store", "חנות", "e-commerce", "קנייה", "קניות", "shopping"), ("cart", "bag", "tag", "credit-card")),
    (("search", "חיפוש", "find"), ("search", "filter")),
    (("settings", "הגדרות", "preferences", "העדפות"), ("settings",)),
    (("profile", "account", "פרופיל", "חשבון", "login", "התחברות"), ("user", "lock")),
    (("calendar", "schedule", "booking", "יומן", "תורים", "הזמנת תור", "לוח זמנים"), ("calendar", "clock")),
    (("map", "location", "מפה", "מיקום", "address", "כתובת"), ("map-pin", "map")),
    (("security", "privacy", "אבטחה", "פרטיות", "secure"), ("shield", "lock")),
    (("chat", "support", "צ'אט", "תמיכה", "messages", "הודעות"), ("chat", "help")),
    (("photo", "photos", "gallery", "תמונות", "גלריה"), ("image", "camera")),
    (("video", "videos", "וידאו", "סרטונים"), ("video", "play")),
    (("notifications", "התראות", "alerts"), ("bell",)),
    (("files", "documents", "קבצים", "מסמכים"), ("file", "folder", "download", "upload")),
    (("analytics", "statistics", "reports", "סטטיסטיקה", "דוחות", "נתונים", "dashboard", "דשבורד"),
     ("chart", "pie", "layers")),
    (("weather", "מזג אוויר"), ("sun", "cloud", "moon")),
    (("code", "developer", "programming", "קוד", "תכנות", "פיתוח"), ("code", "terminal", "cpu")),
    (("money", "payments", "price", "כסף", "תשלומים", "מחיר", "מחירים"), ("wallet", "credit-card", "shekel")),
    (("delivery", "shipping", "משלוח", "משלוחים"), ("truck", "package")),
    (("favorites", "מועדפים", "love", "אהבה"), ("heart", "bookmark")),
    (("tasks", "todo", "משימות", "to-do"), ("check", "list", "calendar", "flag")),
    (("music", "מוזיקה", "podcast", "פודקאסט"), ("music", "headphones", "mic")),
    (("ideas", "רעיונות", "creative", "יצירתי"), ("lightbulb", "sparkles", "palette")),
)
_DEFAULT_ICONS = ("home", "search", "user", "settings", "mail", "phone", "heart", "star", "bell", "calendar",
                  "chat", "cart", "check", "menu")


def icons_for_brief(brief: str, n: int = 8) -> list[str]:
    """Icon names that fit a brief, most relevant first (Hebrew or English), always `n` long."""
    text = norm(brief)
    found: list[str] = []
    for words, icons in _ICON_KEYWORDS:
        if any(keyword_in(text, w, normalized=True) for w in words):
            found.extend(icons)
    for alias, canonical in ICON_ALIASES.items():
        if len(alias) > 3 and keyword_in(text, alias, normalized=True):
            found.append(canonical)
    for name in ICONS:
        if len(name) > 3 and keyword_in(text, name.replace("-", " "), normalized=True):
            found.append(name)
    found.extend(detect_domain(brief).icons)
    found.extend(_DEFAULT_ICONS)
    rest = [i for i in ICON_NAMES if i not in found]
    make_rng("icons", brief).shuffle(rest)
    found.extend(rest)
    return list(dict.fromkeys(found))[: max(0, n)]


def icon_sprite(names: list[str], *, style: str = "outline", color: str = "currentColor", accent: str | None = None,
                columns: int = 6, background: str = "#ffffff", label_color: str = "#555555",
                title: str = "Icons") -> str:
    """One SVG holding every icon as a <symbol id="icon-NAME"> (usable as `sprite.svg#icon-home`)
    plus a labelled contact sheet so the file previews nicely when opened directly."""
    canon = list(dict.fromkeys(resolve_icon(n) for n in names))
    cell_w, cell_h, pad = 104, 96, 24
    cols = max(1, min(columns, len(canon) or 1))
    rows = math.ceil(len(canon) / cols) if canon else 1
    width = pad * 2 + cols * cell_w
    height = pad * 2 + 40 + rows * cell_h
    draw_color = color if color != "currentColor" else "#1f2937"
    symbols = "".join(
        f'<symbol id="icon-{n}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">'
        f'{icon_inner(n, style=style, color="currentColor", accent=accent, knockout=background)}</symbol>'
        for n in canon
    )
    cells = []
    for i, n in enumerate(canon):
        x = pad + (i % cols) * cell_w
        y = pad + 40 + (i // cols) * cell_h
        cells.append(
            f'<g><rect x="{x + 6}" y="{y}" width="{cell_w - 12}" height="{cell_h - 12}" rx="14" fill="none" '
            f'stroke="{label_color}" stroke-opacity="0.18"/>'
            f'<use href="#icon-{n}" x="{x + cell_w / 2 - 16}" y="{y + 14}" width="32" height="32" '
            f'style="color:{draw_color}"/>'
            f'<text x="{x + cell_w / 2}" y="{y + cell_h - 26}" text-anchor="middle" font-size="11" '
            f'font-family="Consolas, monospace" fill="{label_color}">{n}</text></g>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {width} '
        f'{height}" width="{width}" height="{height}" role="img" aria-label="{esc(title)}">'
        f"<title>{esc(title)}</title><defs>{symbols}</defs>"
        f'<rect width="{width}" height="{height}" rx="18" fill="{background}"/>'
        f'<text x="{pad + 6}" y="{pad + 22}" font-size="18" font-weight="700" '
        f"font-family=\"Heebo, 'Segoe UI', Arial, sans-serif\" fill=\"{draw_color}\">{esc(title)}</text>"
        + "".join(cells)
        + "</svg>"
    )


def generate_icon_set(names: list[str] | None = None, *, brief: str = "", palette: Palette | None = None,
                      style: str = "outline", size: int = 48) -> list[Asset]:
    """Individual icon SVGs colored with the palette, plus one sprite sheet (`icons-sprite.svg`)."""
    palette = palette or generate_palette(brief)
    chosen = list(dict.fromkeys(resolve_icon(n) for n in (names or icons_for_brief(brief, 12))))
    color, accent = palette.primary, palette.accent
    assets = [
        Asset(name=f"icon-{n}", kind="svg",
              content=icon_svg(n, size=size, color=color, style=style, accent=accent, label=n,
                               knockout=palette.surface),
              description=f"{n} icon ({style})")
        for n in chosen
    ]
    sprite = icon_sprite(chosen, style=style, color=color, accent=accent, background=palette.surface,
                         label_color=palette.muted, title=f"Icon set · {style}")
    assets.append(Asset(name="icons-sprite", kind="svg", content=sprite,
                        description=f"Sprite sheet with {len(chosen)} icons (use sprite.svg#icon-NAME)"))
    return assets
