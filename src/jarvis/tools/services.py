"""Shared services every tool can use (built once by the composition root)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from jarvis.config.paths import AppPaths
from jarvis.config.settings import Settings
from jarvis.security.path_guard import PathGuard, normalize_path
from jarvis.security.process import ProcessRegistry
from jarvis.security.redaction import Redactor, default_redactor
from jarvis.security.secrets import SecretStore

if TYPE_CHECKING:
    from jarvis.ai.router import ProviderRouter
    from jarvis.bus import MessageBus
    from jarvis.host.base import HostAdapter
    from jarvis.memory.manager import MemoryManager

_SPECIAL = {
    "desktop": ("desktop", "שולחן העבודה", "שולחן עבודה", "דסקטופ"),
    "documents": ("documents", "my documents", "מסמכים", "המסמכים"),
    "downloads": ("downloads", "הורדות", "ההורדות"),
    "pictures": ("pictures", "photos", "תמונות", "התמונות"),
    "music": ("music", "מוזיקה"),
    "videos": ("videos", "סרטונים", "וידאו"),
    "home": ("home", "~", "תיקיית הבית"),
    "workspace": ("workspace", "סביבת העבודה"),
    "projects": ("projects", "פרויקטים", "הפרויקטים"),
}


@dataclass
class ToolServices:
    settings: Settings
    paths: AppPaths
    guard: PathGuard
    processes: ProcessRegistry = field(default_factory=ProcessRegistry)
    redactor: Redactor = field(default_factory=default_redactor)
    secrets: SecretStore | None = None
    host: HostAdapter | None = None
    memory: MemoryManager | None = None
    ai: ProviderRouter | None = None
    bus: MessageBus | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    _http: httpx.AsyncClient | None = None

    def http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=self.settings.web.timeout_s,
                follow_redirects=True,
                headers={"User-Agent": self.settings.web.user_agent},
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def special_folder(self, name: str) -> Path | None:
        key = name.strip().lower().strip("\\/ ")
        for folder, aliases in _SPECIAL.items():
            if key == folder or key in aliases:
                if folder == "workspace":
                    return self.paths.workspace
                if folder == "projects":
                    return self.paths.projects
                if folder == "home":
                    return Path.home()
                if self.host is not None:
                    found = self.host.special_folder(folder)
                    if found is not None:
                        return found
                return normalize_path(Path.home() / folder.capitalize())
        return None
