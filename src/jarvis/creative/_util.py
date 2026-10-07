"""Small shared helpers for the creative generators (stdlib only)."""

from __future__ import annotations

import functools
import hashlib
import html
import random
import re
from collections.abc import Iterable

from jarvis.common.text import normalize

FONT_STACK = '"Heebo", "Segoe UI", Arial, sans-serif'
# The same stack for use inside a double-quoted XML/SVG attribute.
FONT_STACK_ATTR = "Heebo, 'Segoe UI', Arial, sans-serif"
SERIF_STACK = '"Frank Ruhl Libre", "Playfair Display", Georgia, "Times New Roman", serif'
SERIF_STACK_ATTR = "'Frank Ruhl Libre', 'Playfair Display', Georgia, 'Times New Roman', serif"
MONO_STACK = '"Cascadia Code", Consolas, "SF Mono", Menlo, monospace'

_HEBREW_RE = re.compile(r"[֐-׿יִ-ﭏ]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_HEB_PREFIXES = "[והבלמשכ]{0,3}"


def stable_seed(*parts: object) -> int:
    """A deterministic 64-bit seed from any printable parts (unlike hash(), stable across runs)."""
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:16], 16)


def make_rng(*parts: object, seed: int | None = None) -> random.Random:
    """Random generator seeded by `seed` when given, else by a hash of `parts`."""
    return random.Random(seed if seed is not None else stable_seed(*parts))


def has_hebrew(text: str) -> bool:
    return bool(_HEBREW_RE.search(text or ""))


def resolve_lang(lang: str | None, text: str = "", default: str = "he") -> str:
    """Return 'he' or 'en': the explicit language, else the language the text is written in."""
    if lang:
        low = lang.lower()
        if low.startswith("he") or low in ("iw", "hebrew", "עברית"):
            return "he"
        if low.startswith("en") or low == "english":
            return "en"
    he = len(_HEBREW_RE.findall(text or ""))
    en = len(_LATIN_RE.findall(text or ""))
    if he == 0 and en == 0:
        return default
    return "he" if he >= en * 0.6 else "en"


def text_dir(lang: str) -> str:
    return "rtl" if lang == "he" else "ltr"


def esc(value: object) -> str:
    """HTML/XML escape (also quotes) for text and attribute values."""
    return html.escape(str(value), quote=True)


def norm(text: str) -> str:
    """Normalized lower-case text used for keyword matching."""
    return normalize(text or "")


@functools.lru_cache(maxsize=4096)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    kw = norm(keyword)
    if has_hebrew(kw):
        return re.compile(rf"(?<![א-ת]){_HEB_PREFIXES}{re.escape(kw)}(?![א-ת])")
    return re.compile(rf"(?<![a-z0-9]){re.escape(kw)}(?:s|es)?(?![a-z0-9])")


def keyword_in(text: str, keyword: str, *, normalized: bool = False) -> bool:
    """Whole-word keyword match. Hebrew keywords may carry the usual one-letter prefixes (ה, ו, ב, ל, מ, ש, כ)."""
    hay = text if normalized else norm(text)
    return bool(_keyword_pattern(keyword).search(hay))


def count_hits(text: str, keywords: Iterable[str]) -> int:
    hay = norm(text)
    return sum(1 for kw in keywords if keyword_in(hay, kw, normalized=True))


def pick(rng: random.Random, options: list[str] | tuple[str, ...], k: int) -> list[str]:
    """Up to k distinct options in a deterministic shuffled order."""
    items = list(dict.fromkeys(options))
    rng.shuffle(items)
    return items[: max(0, k)]


def chunk_words(text: str, max_chars: int) -> list[str]:
    """Greedy word wrap by character count (used where no font metrics are available)."""
    words = (text or "").split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


# Approximate advance widths (in em) of a typical bold sans-serif, used to lay out SVG text
# without font metrics. Errs slightly wide so text does not overflow its box.
_NARROW = set("fijlrtI.,:;'|!()[]{} ")
_WIDE = set("mwMW@%")
_UPPER_WIDE = set("ABCDGHKNOQRUVXY&")


def estimate_text_width(text: str, size: float, *, weight: int = 700, letter_spacing: float = 0.0) -> float:
    total = 0.0
    for ch in text:
        if ch in _NARROW:
            w = 0.32
        elif ch in _WIDE:
            w = 0.9
        elif ch in _UPPER_WIDE:
            w = 0.74
        elif ch.isupper():
            w = 0.66
        elif _HEBREW_RE.match(ch):
            w = 0.62
        elif ch.isdigit():
            w = 0.6
        else:
            w = 0.58
        total += w
    factor = 1.0 + max(0, weight - 400) / 8000
    return total * size * factor + letter_spacing * max(0, len(text) - 1)
