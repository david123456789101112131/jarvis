"""The media files themselves: what format an image or video really is, its size in pixels, and the
base64 form an image takes when it is sent to xAI. No imaging library is needed for any of it."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path

EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "MP4": ".mp4"}
MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "MP4": "video/mp4"}
# Extensions that already name the format (a ".jpeg" file is left as it is).
SAME_FORMAT = {"JPEG": (".jpg", ".jpeg"), "PNG": (".png",), "WEBP": (".webp",), "MP4": (".mp4",)}
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass(frozen=True)
class Probe:
    format: str                     # "JPEG", "PNG", "WEBP", "MP4"
    width: int | None = None
    height: int | None = None

    @property
    def mime(self) -> str:
        return MIME[self.format]

    @property
    def ext(self) -> str:
        return EXTENSIONS[self.format]


def probe(data: bytes) -> Probe | None:
    """The format (and pixel size, for images) from the first bytes; None when it is none of the four."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return Probe("PNG", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    if data[:3] == b"\xff\xd8\xff":
        size = _jpeg_size(data)
        return Probe("JPEG", *(size or (None, None)))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        size = _webp_size(data)
        return Probe("WEBP", *(size or (None, None)))
    if data[4:8] == b"ftyp":
        return Probe("MP4")
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:                                   # fill byte
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # markers without a length
            i += 2
            continue
        if marker in _SOF:
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return (width, height) if width and height else None
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25:
        b = data[21:25]
        return 1 + (b[0] | (b[1] & 0x3F) << 8), 1 + ((b[1] >> 6) | (b[2] << 2) | ((b[3] & 0x0F) << 10))
    if chunk == b"VP8X" and len(data) >= 30:
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    return None


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def safe_name(text: str, fallback: str = "jarvis") -> str:
    """A file name from free text: no folder separators or characters Windows refuses, at most 80 characters."""
    name = _UNSAFE_NAME.sub(" ", text).strip(" .")
    name = " ".join(name.split())[:80].strip(" .")
    return name or fallback


def with_format(path: Path, fmt: str) -> tuple[Path, bool]:
    """`path` with an extension that names `fmt` (unchanged when it already does), and whether it changed."""
    if path.suffix.lower() in SAME_FORMAT.get(fmt, ()):
        return path, False
    ext = EXTENSIONS[fmt]
    stem = path.name if path.suffix.lower() in (".", "") or len(path.suffix) > 6 else path.stem
    return path.with_name(stem + ext), True


__all__ = ["EXTENSIONS", "MIME", "Probe", "data_uri", "probe", "safe_name", "with_format"]
