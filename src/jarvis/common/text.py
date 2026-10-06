"""Language and text helpers (Hebrew / English)."""

from __future__ import annotations

import re
import unicodedata

_HEBREW_RE = re.compile(r"[֐-׿]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_NIQQUD_RE = re.compile(r"[֑-ׇ]")

# Spoken/written forms of the wake word that may prefix a command.
WAKE_WORD_FORMS = (
    "hey jarvis",
    "hi jarvis",
    "ok jarvis",
    "okay jarvis",
    "jarvis",
    "ג'ארוויס",
    "ג׳ארוויס",
    "ג'רוויס",
    "ג׳רוויס",
    "גארוויס",
    "גרוויס",
    "ג'רביס",
    "ג׳רביס",
    "ג'ארביס",
    "ג׳ארביס",
    "היי ג'ארוויס",
    "היי ג׳ארוויס",
    "היי ג'רוויס",
    "היי ג׳רוויס",
)


def detect_language(text: str, default: str = "en") -> str:
    """Return 'he' when the text is mostly Hebrew, 'en' when mostly Latin."""
    he = len(_HEBREW_RE.findall(text or ""))
    en = len(_LATIN_RE.findall(text or ""))
    if he == 0 and en == 0:
        return default
    return "he" if he >= en * 0.6 else "en"


def strip_niqqud(text: str) -> str:
    return _NIQQUD_RE.sub("", text)


def normalize(text: str, *, lower: bool = True) -> str:
    """Unify quotes/geresh, strip niqqud, collapse whitespace and (by default) lower-case."""
    text = unicodedata.normalize("NFKC", text or "")
    text = strip_niqqud(text)
    text = text.replace("׳", "'").replace("’", "'").replace("`", "'").replace("״", '"')
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower() if lower else text


def strip_wake_word(text: str) -> tuple[str, bool]:
    """Remove a leading wake word ("JARVIS, open chrome" -> "open chrome").

    Returns the remaining text and whether a wake word was present.
    """
    raw = (text or "").strip()
    norm = normalize(raw)
    for form in sorted(WAKE_WORD_FORMS, key=len, reverse=True):
        f = normalize(form)
        if norm == f:
            return "", True
        if norm.startswith(f) and (len(norm) == len(f) or not norm[len(f)].isalnum()):
            rest = raw[len(form):] if raw.lower().startswith(form) else _cut_prefix(raw, len(f))
            return rest.lstrip(" ,.:;!-—").strip(), True
    return raw, False


def _cut_prefix(raw: str, n_norm_chars: int) -> str:
    # Fallback when normalization changed lengths: drop words until the normalized prefix is consumed.
    words = raw.split()
    consumed = 0
    out_index = 0
    for i, w in enumerate(words):
        consumed += len(normalize(w)) + (1 if i else 0)
        out_index = i + 1
        if consumed >= n_norm_chars:
            break
    return " ".join(words[out_index:])


def truncate(text: str, limit: int = 400, suffix: str = "…") -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - len(suffix))] + suffix


def slugify(text: str, fallback: str = "item", max_len: int = 48) -> str:
    """Filesystem-friendly name. Keeps Hebrew letters (valid on Windows/NTFS)."""
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", " ", text)
    text = re.sub(r"[^\w֐-׿\- ]+", " ", text)
    text = re.sub(r"\s+", "-", text).strip("-._ ").lower()
    text = text[:max_len].strip("-")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if not text or text in reserved:
        return fallback
    return text
