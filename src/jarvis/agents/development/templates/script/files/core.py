"""The tool's logic as plain, testable functions (no printing, no argument parsing)."""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

WORD = re.compile(r"[A-Za-z\u0590-\u05FF][A-Za-z\u0590-\u05FF'\u05F3\-]*")
STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "are", "was", "you", "your", "but", "not", "have", "has",
    "of", "to", "in", "is", "it", "on", "a", "an", "or", "as", "be", "by", "at",
    "של", "את", "על", "עם", "זה", "זו", "גם", "כי", "לא", "הוא", "היא", "אני", "אבל", "או", "אם", "יש", "כל", "מה",
}


@dataclass(frozen=True)
class FileEntry:
    path: Path
    size: int
    modified: datetime

    @property
    def extension(self) -> str:
        return self.path.suffix.lower() or "(none)"


def scan(root: Path, *, include_hidden: bool = False) -> list[FileEntry]:
    """Every file under `root` (recursively), skipping hidden files and folders unless asked."""
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a folder: {root}")
    entries: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not include_hidden and name.startswith("."):
                continue
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append(FileEntry(path, st.st_size, datetime.fromtimestamp(st.st_mtime)))
    return entries


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def summarize(entries: list[FileEntry], *, top: int = 5) -> dict[str, object]:
    by_ext: Counter[str] = Counter()
    size_by_ext: Counter[str] = Counter()
    for e in entries:
        by_ext[e.extension] += 1
        size_by_ext[e.extension] += e.size
    return {
        "files": len(entries),
        "total_size": sum(e.size for e in entries),
        "by_extension": [{"extension": ext, "files": n, "size": size_by_ext[ext]} for ext, n in by_ext.most_common()],
        "largest": [{"path": str(e.path), "size": e.size} for e in sorted(entries, key=lambda e: -e.size)[:top]],
        "newest": [{"path": str(e.path), "modified": e.modified.isoformat(timespec="seconds")}
                   for e in sorted(entries, key=lambda e: e.modified, reverse=True)[:top]],
    }


def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def find_duplicates(entries: list[FileEntry]) -> list[list[Path]]:
    """Groups of identical files (same size first, then the same SHA-256)."""
    by_size: dict[int, list[FileEntry]] = {}
    for e in entries:
        if e.size > 0:
            by_size.setdefault(e.size, []).append(e)
    groups: list[list[Path]] = []
    for same in by_size.values():
        if len(same) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for e in same:
            try:
                by_hash.setdefault(file_hash(e.path), []).append(e.path)
            except OSError:
                continue
        groups.extend(sorted(paths) for paths in by_hash.values() if len(paths) > 1)
    return sorted(groups)


def top_words(text: str, n: int = 10, *, min_length: int = 2) -> list[tuple[str, int]]:
    words = [w.lower().strip("'-") for w in WORD.findall(text)]
    counts = Counter(w for w in words if len(w) >= min_length and w not in STOPWORDS)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
