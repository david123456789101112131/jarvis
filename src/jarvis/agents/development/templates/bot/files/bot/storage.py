"""Per-user to-do lists kept in a JSON file."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class TodoStore:
    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict[str, list[dict[str, Any]]] = {}
        if self.path and self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = loaded
            except (OSError, ValueError):
                self._data = {}

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def tasks(self, user: int) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self._data.get(str(user), [])]

    def add(self, user: int, title: str) -> int:
        with self._lock:
            items = self._data.setdefault(str(user), [])
            items.append({"title": title[:300], "done": False})
            self._save()
            return sum(1 for t in items if not t["done"])

    def mark_done(self, user: int, number: int) -> str | None:
        with self._lock:
            items = self._data.get(str(user), [])
            if not 1 <= number <= len(items):
                return None
            items[number - 1]["done"] = True
            self._save()
            return str(items[number - 1]["title"])

    def clear_done(self, user: int) -> int:
        with self._lock:
            items = self._data.get(str(user), [])
            keep = [t for t in items if not t["done"]]
            self._data[str(user)] = keep
            self._save()
            return len(items) - len(keep)
