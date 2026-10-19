"""The previews the image and video screens made: which file is which, what made it, and where it was saved.

The files themselves are in `<workspace>/media/<job id>/`, written there by the creation tools through the
executor. The index of them is JARVIS's own data (`<data>/media_items.json`, out of the agents' reach). Every
path the index hands out is checked to be inside the media folder first, so a changed index can never point the
file route, or a deletion, anywhere else.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from jarvis.common.clock import utcnow
from jarvis.common.ids import new_id
from jarvis.security.path_guard import is_within, normalize_path

log = logging.getLogger(__name__)

KEEP = 1000                                  # items kept in the index (the oldest drop out of it first)
_JOB_DIR = re.compile(r"[^A-Za-z0-9_-]+")


class MediaItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("media"))
    kind: Literal["image", "video"]
    job_id: str
    path: str
    format: str                              # "PNG", "JPEG", "WEBP", "MP4", or "SVG" for a demonstration
    mime: str
    bytes: int = 0
    width: int | None = None
    height: int | None = None
    seconds: float | None = None
    real: bool = True                        # False: a demonstration placeholder, never a real image or video
    description: str = ""                    # what the user wrote
    prompt: str = ""                         # exactly what was sent to the service
    params: dict[str, Any] = Field(default_factory=dict)    # model, aspect ratio, resolution, style, …
    usd: float | None = None                 # what the service reported it charged for the request
    cost: str | None = None                  # the estimate shown before the request
    parent: str | None = None                # the item this one was made from (a refinement or a variation)
    saved_to: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    def view(self) -> dict[str, Any]:
        return {**self.model_dump(mode="json"), "name": Path(self.path).name, "url": f"/api/media/file/{self.id}"}


class MediaStore:
    def __init__(self, index_file: Path, folder: Path) -> None:
        self.index_file = index_file
        self.folder = normalize_path(folder)
        self._lock = threading.RLock()
        self._items: dict[str, MediaItem] = {}
        self._load()

    # ------------------------------------------------------------------ where files go
    def job_folder(self, job_id: str) -> Path:
        name = _JOB_DIR.sub("_", job_id).strip("_") or "job"
        return self.folder / name

    def inside(self, path: str | Path) -> bool:
        """True for a path below the media folder (after resolving links and `..`)."""
        try:
            resolved, root = normalize_path(path), normalize_path(self.folder)
        except (OSError, ValueError):
            return False
        return is_within(resolved, root) and not is_within(root, resolved)

    # ------------------------------------------------------------------ the index
    def add(self, items: Iterable[MediaItem]) -> list[MediaItem]:
        added = [item for item in items if self.inside(item.path)]
        with self._lock:
            for item in added:
                self._items[item.id] = item
            while len(self._items) > KEEP:
                self._items.pop(next(iter(self._items)))
        self._save()
        return added

    def get(self, item_id: str) -> MediaItem | None:
        with self._lock:
            return self._items.get(item_id)

    def items(self, kind: str | None = None, limit: int = 200) -> list[MediaItem]:
        with self._lock:
            found = [i for i in reversed(self._items.values()) if kind is None or i.kind == kind]
        return found[:limit]

    def file(self, item_id: str) -> MediaItem | None:
        """The item, when its file is still there and inside the media folder."""
        item = self.get(item_id)
        if item is None or not self.inside(item.path) or not Path(item.path).is_file():
            return None
        return item

    def mark_saved(self, item_id: str, target: str) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return
            if target not in item.saved_to:
                item.saved_to.append(target)
        self._save()

    def remove(self, item_id: str) -> bool:
        """Forget an item and delete its preview file (only ever a file inside the media folder)."""
        with self._lock:
            item = self._items.pop(item_id, None)
        if item is None:
            return False
        self._delete_file(item.path)
        self._save()
        return True

    def clear(self, kind: str | None = None) -> int:
        with self._lock:
            gone = [i for i in self._items.values() if kind is None or i.kind == kind]
            for item in gone:
                self._items.pop(item.id, None)
        for item in gone:
            self._delete_file(item.path)
        self._save()
        return len(gone)

    def _delete_file(self, path: str) -> None:
        if not self.inside(path):
            log.warning("Not deleting %s: it is outside the media folder", path)
            return
        target = Path(path)
        try:
            if target.is_file() and not target.is_symlink():
                target.unlink()
            folder = target.parent
            if folder != self.folder and self.inside(folder) and not any(folder.iterdir()):
                folder.rmdir()
        except OSError as exc:
            log.warning("Could not delete the preview %s: %s", path, exc)

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.index_file.is_file():
            return
        try:
            raw = json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Could not read %s; starting with no previews", self.index_file)
            return
        for entry in raw.get("items", []) if isinstance(raw, dict) else []:
            try:
                item = MediaItem.model_validate(entry)
            except ValidationError:
                continue
            if self.inside(item.path) and Path(item.path).is_file():     # gone (or never ours): forget it
                self._items[item.id] = item

    def _save(self) -> None:
        with self._lock:
            data = {"items": [i.model_dump(mode="json") for i in self._items.values()]}
        try:
            self.index_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.index_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.index_file)
        except OSError:
            log.exception("Could not save the media index")


__all__ = ["MediaItem", "MediaStore"]
