"""Settings: the project name and description from project.json, the data folder from the environment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    title: str
    description: str
    lang: str
    data_file: Path


def load_settings() -> Settings:
    info: dict[str, str] = {}
    project = ROOT / "project.json"
    if project.is_file():
        info = json.loads(project.read_text(encoding="utf-8"))
    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    return Settings(
        title=info.get("title") or "Items API",
        description=info.get("description") or "A REST API for items.",
        lang=info.get("lang") or "en",
        data_file=data_dir / "items.json",
    )
