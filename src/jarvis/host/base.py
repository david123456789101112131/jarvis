"""Operating-system adapter interface.

Tools (never agents directly) call the host adapter for anything OS specific: finding and
launching installed apps (Hebrew/English names), opening URLs/folders, closing apps, volume,
brightness, media keys, lock/power, screenshots, notifications and the clipboard.

`WindowsHost` is the production implementation; `PosixHost` keeps development and CI on
Linux/macOS working. Operations a machine cannot do raise `HostUnsupported` with a message
that can be shown to the user as-is.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from jarvis.common.errors import JarvisError

AppKind = Literal["exe", "lnk", "appx", "uri", "command"]
MediaAction = Literal["play_pause", "next", "previous", "stop"]
PowerAction = Literal["shutdown", "restart", "sleep", "logoff"]
FolderKey = Literal["desktop", "documents", "downloads", "pictures", "music", "videos", "home", "startmenu", "appdata"]


class HostUnsupported(JarvisError):
    code = "host_unsupported"


@dataclass
class InstalledApp:
    name: str                       # display name, e.g. "Google Chrome"
    target: str                     # exe path, .lnk path, AppUserModelID, URI or command
    kind: AppKind
    source: str = "catalog"         # startmenu | apppaths | appx | path | catalog | desktop
    id: str | None = None           # catalog id when known ("chrome")
    process_names: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "target": self.target, "kind": self.kind, "source": self.source, "id": self.id}


@dataclass
class LaunchResult:
    ok: bool
    app: str
    method: str                     # how it was started (startfile, popen, shell:AppsFolder, uri…)
    pid: int | None = None
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class HostAdapter(ABC):
    platform: ClassVar[str] = "generic"

    # ---------------------------------------------------------------- folders
    @abstractmethod
    def special_folder(self, key: str) -> Path | None:
        """Real location of a known folder (handles OneDrive redirection / localized names)."""

    # ---------------------------------------------------------------- apps
    @abstractmethod
    async def installed_apps(self, refresh: bool = False) -> list[InstalledApp]:
        """Index of launchable apps (cached; refresh rebuilds it)."""

    @abstractmethod
    async def find_app(self, name: str) -> InstalledApp | None:
        """Best match for a spoken/typed name in Hebrew or English ("כרום", "vs code", "Word")."""

    @abstractmethod
    async def launch_app(self, app: InstalledApp, args: Sequence[str] = ()) -> LaunchResult: ...

    @abstractmethod
    async def open_url(self, url: str) -> None: ...

    @abstractmethod
    async def open_path(self, path: Path) -> None:
        """Open a file or folder with its default application."""

    @abstractmethod
    async def reveal(self, path: Path) -> None:
        """Show a file selected in the file manager."""

    @abstractmethod
    async def close_app(self, name: str, *, force: bool = False) -> list[str]:
        """Close an app by name (graceful first; kill when force). Returns closed process names."""

    @abstractmethod
    async def running_apps(self) -> list[dict[str, Any]]:
        """Visible applications: [{name, pid, title?, memory_mb}]."""

    # ---------------------------------------------------------------- audio / display / media
    @abstractmethod
    async def get_volume(self) -> int | None: ...

    @abstractmethod
    async def set_volume(self, level: int) -> int | None: ...

    @abstractmethod
    async def change_volume(self, delta: int) -> int | None: ...

    @abstractmethod
    async def set_mute(self, mute: bool) -> None: ...

    @abstractmethod
    async def get_brightness(self) -> int | None: ...

    @abstractmethod
    async def set_brightness(self, level: int) -> int | None: ...

    @abstractmethod
    async def media(self, action: MediaAction) -> None: ...

    # ---------------------------------------------------------------- session / power
    @abstractmethod
    async def lock_screen(self) -> None: ...

    @abstractmethod
    async def power(self, action: PowerAction, delay_s: int = 0) -> None: ...

    @abstractmethod
    async def cancel_power(self) -> None: ...

    # ---------------------------------------------------------------- misc
    @abstractmethod
    async def screenshot(self, path: Path) -> Path: ...

    @abstractmethod
    async def notify(self, title: str, message: str) -> None: ...

    @abstractmethod
    async def get_clipboard(self) -> str: ...

    @abstractmethod
    async def set_clipboard(self, text: str) -> None: ...

    @abstractmethod
    def shell_argv(self, command: str, shell: str | None = None) -> list[str]:
        """argv that runs `command` in the platform shell (PowerShell on Windows, bash elsewhere)."""

    @property
    @abstractmethod
    def default_shell(self) -> str: ...


def get_host_adapter(**kwargs: Any) -> HostAdapter:
    if sys.platform == "win32":
        from jarvis.host.windows import WindowsHost

        return WindowsHost(**kwargs)
    from jarvis.host.posix import PosixHost

    return PosixHost(**kwargs)
