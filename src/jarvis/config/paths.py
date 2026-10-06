"""Resolve where JARVIS keeps data, logs, caches and the agents' workspace.

Defaults (Windows):
    data/config/logs/cache:  %LOCALAPPDATA%\\JARVIS\\...
    workspace/projects:      %USERPROFILE%\\JARVIS\\workspace, %USERPROFILE%\\JARVIS\\projects

`JARVIS_HOME` relocates everything under one folder (portable mode and tests).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir, user_data_dir, user_log_dir

from jarvis.config.settings import PathsSettings

APP_DIR_NAME = "JARVIS"


@dataclass(frozen=True)
class AppPaths:
    home: Path
    data: Path
    config_file: Path
    logs: Path
    cache: Path
    workspace: Path
    projects: Path
    db: Path

    def ensure(self) -> AppPaths:
        for p in (self.home, self.data, self.logs, self.cache, self.workspace, self.projects):
            p.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def models(self) -> Path:
        return self.cache / "models"

    @property
    def tts_cache(self) -> Path:
        return self.cache / "tts"


def _pick(value: str, default: Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve() if value else default


def default_config_file() -> Path:
    base = os.environ.get("JARVIS_HOME")
    if base:
        return Path(base).expanduser().resolve() / "data" / "config.toml"
    return Path(user_data_dir(APP_DIR_NAME, appauthor=False, roaming=False)) / "config.toml"


def resolve_paths(paths: PathsSettings, db_path: str = "") -> AppPaths:
    portable = os.environ.get("JARVIS_HOME") or paths.home_dir
    if portable:
        root = Path(os.path.expanduser(portable)).resolve()
        home = root
        data_default = root / "data"
        logs_default = root / "logs"
        cache_default = root / "cache"
        work_root = root
    else:
        home = Path.home() / APP_DIR_NAME
        data_default = Path(user_data_dir(APP_DIR_NAME, appauthor=False, roaming=False))
        logs_default = Path(user_log_dir(APP_DIR_NAME, appauthor=False))
        cache_default = Path(user_cache_dir(APP_DIR_NAME, appauthor=False))
        work_root = home

    data = _pick(paths.data_dir, data_default)
    logs = _pick(paths.log_dir, logs_default)
    cache = _pick(paths.cache_dir, cache_default)
    workspace = _pick(paths.workspace_dir, work_root / "workspace")
    projects = _pick(paths.projects_dir, work_root / "projects")
    db = _pick(db_path, data / "jarvis.db")
    return AppPaths(
        home=home,
        data=data,
        config_file=data / "config.toml",
        logs=logs,
        cache=cache,
        workspace=workspace,
        projects=projects,
        db=db,
    )
