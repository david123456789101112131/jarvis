"""Items stored in a JSON file: thread-safe, with atomic writes so a crash never leaves half a file."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    return data
            except (OSError, ValueError):
                pass
        return {"next_id": 1, "items": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._data["items"]]

    def get(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            for item in self._data["items"]:
                if item["id"] == item_id:
                    return dict(item)
        return None

    def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            item = {**fields, "id": self._data["next_id"], "created_at": now, "updated_at": now}
            self._data["next_id"] += 1
            self._data["items"].append(item)
            self._save()
            return dict(item)

    def update(self, item_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            for item in self._data["items"]:
                if item["id"] == item_id:
                    item.update(changes)
                    item["updated_at"] = datetime.now(UTC).isoformat()
                    self._save()
                    return dict(item)
        return None

    def delete(self, item_id: int) -> bool:
        with self._lock:
            before = len(self._data["items"])
            self._data["items"] = [i for i in self._data["items"] if i["id"] != item_id]
            if len(self._data["items"]) == before:
                return False
            self._save()
            return True
