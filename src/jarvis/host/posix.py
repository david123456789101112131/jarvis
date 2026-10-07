"""POSIX host adapter (`PosixHost`) for Linux and macOS (development and CI).

Best-effort real implementations with the usual desktop tools:

* apps — catalog commands found on PATH plus freedesktop `.desktop` files (Linux: Name,
  Name[he], Exec without field codes; Hidden/NoDisplay/Terminal entries skipped) or `.app`
  bundles (macOS, started with `open -a`); indexed and cached by `jarvis.host.app_index`;
* open / reveal — xdg-open (gio, kde-open) / the FileManager1 D-Bus API; `open` / `open -R`;
* close — psutil: SIGTERM first, SIGKILL only with force; critical processes refused;
* volume — pactl (PulseAudio/PipeWire) → amixer (ALSA) → osascript; brightness — brightnessctl;
  media — playerctl; lock — loginctl / xdg-screensaver / … / pmset; power — systemctl /
  loginctl / osascript;
* screenshot — Pillow ImageGrab, then gnome-screenshot / spectacle / grim / scrot / maim /
  ImageMagick import / screencapture; notify — notify-send / osascript;
* clipboard — wl-copy/wl-paste, xclip, xsel, pbcopy/pbpaste.

A missing tool raises `HostUnsupported` naming what to install.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import psutil

from jarvis.common.errors import PermissionDenied
from jarvis.host.app_index import (
    POSIX_WRAPPERS,
    AppIndexStore,
    DelayedActions,
    annotate_with_catalog,
    candidate_process_names,
    catalog_lookup,
    clean_query,
    match_key,
    merge_apps,
    screenshot_target,
    validate_web_url,
)
from jarvis.host.base import (
    HostAdapter,
    HostUnsupported,
    InstalledApp,
    LaunchResult,
    MediaAction,
    PowerAction,
)
from jarvis.host.catalog import APPS
from jarvis.security.process import ProcessRegistry, ProcessResult, run_process
from jarvis.security.secrets import sanitized_env

log = logging.getLogger(__name__)

POSIX_CRITICAL_PROCESSES = frozenset(
    {
        "systemd", "init", "launchd", "kernel_task", "kthreadd", "loginwindow", "windowserver", "xorg", "xwayland",
        "gnome-shell", "plasmashell", "kwin_x11", "kwin_wayland", "mutter", "dbus-daemon", "dbus-broker", "sshd",
        "login", "gdm", "gdm3", "sddm", "lightdm", "systemd-logind", "polkitd", "pipewire", "wireplumber",
        "pulseaudio", "finder", "dock", "systemuiserver",
    }
)
VOLUME_HINT = "Volume control needs pactl (PulseAudio/PipeWire) or amixer (ALSA)."
BRIGHTNESS_HINT = "Brightness control needs brightnessctl (and a laptop/internal backlight)."
CLIPBOARD_HINT = "Clipboard access needs wl-clipboard (Wayland), xclip or xsel (X11)."
PLAYERCTL_COMMANDS = {"play_pause": "play-pause", "next": "next", "previous": "previous", "stop": "stop"}


# =============================================================================================
# freedesktop .desktop files
# =============================================================================================
@dataclass
class DesktopEntry:
    name: str
    exec: str = ""
    names: dict[str, str] = field(default_factory=dict)      # locale → localized Name
    try_exec: str = ""
    type: str = "Application"
    terminal: bool = False
    no_display: bool = False
    hidden: bool = False

    def localized(self, lang: str) -> str | None:
        if lang in self.names:
            return self.names[lang]
        for locale, value in self.names.items():
            if re.split(r"[_@.]", locale, maxsplit=1)[0] == lang:
                return value
        return None


_DESKTOP_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
_FIELD_CODE_LETTERS = set("fFuUdDnNickvm")


def _unescape_desktop(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value) and value[i + 1] in _DESKTOP_ESCAPES:
            out.append(_DESKTOP_ESCAPES[value[i + 1]])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _desktop_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "1")


def parse_desktop_entry(text: str) -> DesktopEntry | None:
    """The [Desktop Entry] group of a .desktop file (None when it has no Name)."""
    section: str | None = None
    values: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "Desktop Entry" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key.strip(), value.strip())
    name = values.get("Name")
    if not name:
        return None
    names = {}
    for key, value in values.items():
        m = re.fullmatch(r"Name\[([^\]]+)\]", key)
        if m and value:
            names[m.group(1)] = _unescape_desktop(value)
    return DesktopEntry(
        name=_unescape_desktop(name),
        exec=_unescape_desktop(values.get("Exec", "")),
        names=names,
        try_exec=_unescape_desktop(values.get("TryExec", "")),
        type=values.get("Type", "Application"),
        terminal=_desktop_bool(values.get("Terminal")),
        no_display=_desktop_bool(values.get("NoDisplay")),
        hidden=_desktop_bool(values.get("Hidden")),
    )


def desktop_exec_argv(exec_value: str) -> list[str]:
    """argv of an Exec= value with field codes (%f %U %i …) removed and %% unescaped."""
    try:
        parts = shlex.split(exec_value or "")
    except ValueError:
        parts = (exec_value or "").split()
    argv: list[str] = []
    for part in parts:
        if part in ("@@", "@@u", "@@f"):           # flatpak file-forwarding markers
            continue
        cleaned = re.sub(
            r"%(.)",
            lambda m: "%" if m.group(1) == "%" else ("" if m.group(1) in _FIELD_CODE_LETTERS else m.group(0)),
            part,
        )
        if cleaned:
            argv.append(cleaned)
    return argv


def _program_exists(program: str, which: Callable[[str], str | None]) -> bool:
    if "/" in program:
        return os.path.isfile(program) and os.access(program, os.X_OK)
    return which(program) is not None


def desktop_app(entry: DesktopEntry, *, which: Callable[[str], str | None] = shutil.which) -> InstalledApp | None:
    """InstalledApp for a launchable, visible application entry."""
    if entry.type != "Application" or entry.no_display or entry.hidden or entry.terminal:
        return None
    argv = desktop_exec_argv(entry.exec)
    if not argv:
        return None
    if entry.try_exec and not _program_exists(entry.try_exec, which):
        return None
    if not _program_exists(argv[0], which):
        return None
    head = posixpath.basename(argv[0]).lower()
    aliases = tuple(v for v in (entry.localized("he"),) if v and v != entry.name)
    return InstalledApp(entry.name, shlex.join(argv), "command", "desktop",
                        process_names=(head,) if head not in POSIX_WRAPPERS else (), aliases=aliases)


def desktop_dirs(env: Mapping[str, str] | None = None, home: Path | None = None) -> list[Path]:
    """XDG application folders, highest precedence first (user, system, flatpak, snap)."""
    env = os.environ if env is None else env
    home = home or Path.home()
    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    data_dirs = [Path(p) for p in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if p]
    candidates = [
        data_home / "applications",
        *(d / "applications" for d in data_dirs),
        home / ".local" / "share" / "flatpak" / "exports" / "share" / "applications",
        Path("/var/lib/flatpak/exports/share/applications"),
        Path("/var/lib/snapd/desktop/applications"),
    ]
    out: list[Path] = []
    for path in candidates:
        if path not in out:
            out.append(path)
    return out


def scan_desktop_files(dirs: Iterable[Path], *, which: Callable[[str], str | None] = shutil.which) -> list[InstalledApp]:
    apps: list[InstalledApp] = []
    seen: set[str] = set()
    for base in dirs:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.desktop")):
            desktop_id = str(path.relative_to(base)).replace("/", "-")
            if desktop_id in seen:          # a user entry overrides (or hides) the system one
                continue
            seen.add(desktop_id)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            entry = parse_desktop_entry(text)
            app = desktop_app(entry, which=which) if entry else None
            if app is not None:
                apps.append(app)
    return apps


def mac_app_dirs(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    return [Path("/Applications"), Path("/Applications/Utilities"), Path("/System/Applications"),
            Path("/System/Applications/Utilities"), home / "Applications"]


def scan_mac_apps(dirs: Iterable[Path]) -> list[InstalledApp]:
    apps: list[InstalledApp] = []
    for base in dirs:
        if not base.is_dir():
            continue
        for bundle in sorted(base.glob("*.app")):
            apps.append(InstalledApp(bundle.stem, str(bundle), "exe", "applications", process_names=(bundle.stem.lower(),)))
    return apps


def catalog_apps_posix(*, which: Callable[[str], str | None] = shutil.which) -> list[InstalledApp]:
    """Catalog apps whose POSIX command exists on PATH."""
    apps: list[InstalledApp] = []
    for entry in APPS:
        for command in entry.posix:
            try:
                argv = shlex.split(command)
            except ValueError:
                continue
            if argv and which(argv[0]):
                head = posixpath.basename(argv[0])
                processes = (*entry.process_names, *((head,) if head not in POSIX_WRAPPERS else ()))
                apps.append(InstalledApp(entry.display, command, "command", "catalog", id=entry.id,
                                         process_names=tuple(dict.fromkeys(processes)), aliases=entry.aliases))
                break
    return apps


# =============================================================================================
# Output parsers
# =============================================================================================
_XDG_DIR_KEYS = {"DESKTOP": "desktop", "DOWNLOAD": "downloads", "DOCUMENTS": "documents", "MUSIC": "music",
                 "PICTURES": "pictures", "VIDEOS": "videos", "TEMPLATES": "templates", "PUBLICSHARE": "public"}


def parse_user_dirs(text: str, home: Path) -> dict[str, Path]:
    """~/.config/user-dirs.dirs → {"desktop": Path(...), "downloads": …} (localized folder names)."""
    out: dict[str, Path] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        m = re.fullmatch(r"XDG_([A-Z]+)_DIR", key.strip())
        if not m or m.group(1) not in _XDG_DIR_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        if value.startswith("$HOME"):
            path = home / value[len("$HOME"):].lstrip("/")
        elif value.startswith("/"):
            path = Path(value)
        else:
            continue
        out[_XDG_DIR_KEYS[m.group(1)]] = path
    return out


def parse_percent(text: str) -> int | None:
    m = re.search(r"(\d{1,3})\s*%", text or "")
    return max(0, min(100, int(m.group(1)))) if m else None


def parse_amixer(text: str) -> tuple[int | None, bool | None]:
    """(volume %, muted) from `amixer get Master`."""
    volume = re.search(r"\[(\d{1,3})%\]", text or "")
    state = re.search(r"\[(on|off)\]", text or "")
    return (int(volume.group(1)) if volume else None, (state.group(1) == "off") if state else None)


def parse_brightnessctl(text: str) -> int | None:
    """Percent from `brightnessctl -m` ("intel_backlight,backlight,12000,50%,24000")."""
    for line in (text or "").splitlines():
        fields = line.strip().split(",")
        if len(fields) >= 4 and fields[3].endswith("%") and fields[3][:-1].isdigit():
            return max(0, min(100, int(fields[3][:-1])))
    return parse_percent(text)


_WMCTRL_RE = re.compile(r"^(0x[0-9a-fA-F]+)\s+(-?\d+)\s+(\d+)\s+(\S+)\s?(.*)$")


def parse_wmctrl(text: str) -> list[tuple[int, str]]:
    """(pid, title) of normal windows from `wmctrl -lp`."""
    rows: list[tuple[int, str]] = []
    for line in (text or "").splitlines():
        m = _WMCTRL_RE.match(line.strip())
        if m and m.group(2) != "-1" and int(m.group(3)) > 0:     # desktop -1 = sticky panels/docks
            rows.append((int(m.group(3)), m.group(5).strip()))
    return rows


def power_argv_posix(
    action: str,
    *,
    macos: bool,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    env = os.environ if env is None else env
    if macos:
        verbs = {"shutdown": "shut down", "restart": "restart", "sleep": "sleep", "logoff": "log out"}
        if action not in verbs:
            raise HostUnsupported(f"Unknown power action: {action!r}")
        return ["osascript", "-e", f'tell application "System Events" to {verbs[action]}']
    if action == "logoff":
        session = env.get("XDG_SESSION_ID")
        if session and which("loginctl"):
            return ["loginctl", "terminate-session", session]
        if which("gnome-session-quit"):
            return ["gnome-session-quit", "--logout", "--no-prompt"]
        raise HostUnsupported("Logging out needs loginctl (systemd) or gnome-session-quit.")
    verbs = {"shutdown": "poweroff", "restart": "reboot", "sleep": "suspend"}
    if action not in verbs:
        raise HostUnsupported(f"Unknown power action: {action!r}")
    if not which("systemctl"):
        raise HostUnsupported("Power actions need systemd's systemctl.")
    return ["systemctl", verbs[action]]


# =============================================================================================
# Process helpers
# =============================================================================================
def _protected_pids() -> set[int]:
    pids = {os.getpid(), 0, 1}
    with contextlib.suppress(psutil.Error):
        pids.update(p.pid for p in psutil.Process().parents())
    return pids


def _grab_with_pillow(target: Path) -> None:
    from PIL import ImageGrab  # optional dependency

    image = ImageGrab.grab(all_screens=True)
    image.save(target, "PNG")


def _prepare_screenshot(path: Path) -> Path:
    target = screenshot_target(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _non_empty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _existing_path(path: Path) -> Path:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"No such file or folder: {p}")
    return p.resolve()


def _feed_detached(argv: Sequence[str], data: bytes, timeout: float = 5.0) -> int:
    """Pipe data into a clipboard tool. xclip/xsel/wl-copy fork a daemon that keeps serving the
    selection, so the output can't be captured (the pipe would stay open) — it goes to /dev/null."""
    proc = subprocess.Popen(  # list argv, no shell
        list(argv), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        start_new_session=True, env=sanitized_env(),
    )
    try:
        assert proc.stdin is not None
        with contextlib.suppress(BrokenPipeError):
            proc.stdin.write(data)
        with contextlib.suppress(BrokenPipeError):
            proc.stdin.close()
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return 0            # still running in the foreground = serving the clipboard; that's fine


# =============================================================================================
# The adapter
# =============================================================================================
class PosixHost(HostAdapter):
    platform: ClassVar[str] = "posix"
    graceful_close_timeout_s: ClassVar[float] = 3.0

    def __init__(self, *, cache_dir: Path | None = None, processes: ProcessRegistry | None = None) -> None:
        self._processes = processes
        self.is_macos = sys.platform == "darwin"
        flavour = "macos" if self.is_macos else "linux"
        cache_file = Path(cache_dir) / f"app_index_{flavour}.json" if cache_dir is not None else None
        self._apps = AppIndexStore(self._build_index, cache_file=cache_file, platform=flavour)
        self._delayed = DelayedActions()
        self._launched: list[subprocess.Popen[bytes]] = []
        self._launched_lock = threading.Lock()

    # ------------------------------------------------------------------------------ folders
    def special_folder(self, key: str) -> Path | None:
        k = (key or "").strip().lower()
        home = Path.home()
        if k == "home":
            return home
        if self.is_macos:
            mapping = {"desktop": home / "Desktop", "documents": home / "Documents", "downloads": home / "Downloads",
                       "pictures": home / "Pictures", "music": home / "Music", "videos": home / "Movies",
                       "startmenu": Path("/Applications"), "appdata": home / "Library" / "Application Support"}
        else:
            config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
            data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
            mapping = {"desktop": home / "Desktop", "documents": home / "Documents", "downloads": home / "Downloads",
                       "pictures": home / "Pictures", "music": home / "Music", "videos": home / "Videos",
                       "startmenu": data_home / "applications", "appdata": config_home}
            with contextlib.suppress(OSError):
                text = (config_home / "user-dirs.dirs").read_text(encoding="utf-8")
                mapping.update(parse_user_dirs(text, home))
        path = mapping.get(k)
        return path if path is not None and path.exists() else None

    # ------------------------------------------------------------------------------ app index
    async def installed_apps(self, refresh: bool = False) -> list[InstalledApp]:
        index = await self._apps.get(refresh=refresh)
        return list(index.apps)

    async def find_app(self, name: str) -> InstalledApp | None:
        if not match_key(name):
            return None
        index = await self._apps.get()
        return index.find(name)

    async def search_apps(self, name: str, limit: int = 5) -> list[InstalledApp]:
        index = await self._apps.get()
        return [m.app for m in index.search(name, limit)]

    async def _build_index(self) -> list[InstalledApp]:
        return await asyncio.to_thread(self._collect_apps)

    def _collect_apps(self) -> list[InstalledApp]:
        groups: list[list[InstalledApp]] = []
        sources: tuple[Callable[[], list[InstalledApp]], ...] = (
            catalog_apps_posix,
            (lambda: scan_mac_apps(mac_app_dirs())) if self.is_macos else (lambda: scan_desktop_files(desktop_dirs())),
        )
        for collect in sources:
            try:
                groups.append(collect())
            except Exception:
                log.warning("Could not list installed apps from %s", collect, exc_info=True)
        return annotate_with_catalog(merge_apps(*groups, case_insensitive_targets=False))

    # ------------------------------------------------------------------------------ launching
    async def launch_app(self, app: InstalledApp, args: Sequence[str] = ()) -> LaunchResult:
        return await asyncio.to_thread(self._launch_sync, app, [str(a) for a in args])

    def _launch_sync(self, app: InstalledApp, args: list[str]) -> LaunchResult:
        if app.kind in ("lnk", "appx"):
            raise HostUnsupported(f"{app.name} is a Windows shortcut/Store app and can't be started here.")
        if app.kind == "uri":
            argv, method = [*self._opener_argv(), app.target], "uri"   # the opener was already found by which()
        elif app.kind == "exe" and app.target.endswith(".app"):
            argv, method = ["open", "-a", app.target, *(["--args", *args] if args else [])], "open -a"
        else:
            try:
                base = shlex.split(app.target) if app.kind == "command" else [app.target]
            except ValueError as exc:
                return LaunchResult(False, app.name, "popen", detail=f"Bad command for {app.name}: {exc}")
            if app.id == "explorer" and not args:
                args = [str(Path.home())]            # "xdg-open" alone does nothing
            argv, method = [*base, *args], "popen"
        if method != "uri" and (not argv or not _program_exists(argv[0], shutil.which)):
            return LaunchResult(False, app.name, method, detail=f"{argv[0] if argv else app.target} is not installed")
        try:
            proc = self._spawn(argv)
        except OSError as exc:
            return LaunchResult(False, app.name, method, detail=f"Couldn't start {app.name}: {exc}")
        return LaunchResult(True, app.name, method, pid=proc.pid)

    def _spawn(self, argv: Sequence[str]) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(  # list argv, no shell; detached from JARVIS's session
            list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True, env=sanitized_env(),
        )
        with self._launched_lock:
            self._launched = [p for p in self._launched if p.poll() is None][-50:]
            self._launched.append(proc)
        return proc

    def _opener_argv(self) -> list[str]:
        if self.is_macos:
            return ["open"]
        for argv in (["xdg-open"], ["gio", "open"], ["kde-open5"], ["kde-open"], ["gnome-open"]):
            exe = shutil.which(argv[0])
            if exe:
                return [exe, *argv[1:]]
        raise HostUnsupported("Opening links and files needs xdg-open (install the xdg-utils package).")

    async def open_url(self, url: str) -> None:
        target = validate_web_url(url)
        await asyncio.to_thread(lambda: self._spawn([*self._opener_argv(), target]))

    async def open_path(self, path: Path) -> None:
        p = await asyncio.to_thread(_existing_path, Path(path))
        await asyncio.to_thread(lambda: self._spawn([*self._opener_argv(), str(p)]))

    async def reveal(self, path: Path) -> None:
        p = await asyncio.to_thread(_existing_path, Path(path))
        if self.is_macos:
            await asyncio.to_thread(self._spawn, ["open", "-R", str(p)])
            return
        if shutil.which("dbus-send"):
            result = await self._run(
                ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1", "--type=method_call",
                 "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems", f"array:string:{p.as_uri()}",
                 "string:"],
                timeout=10.0,
            )
            if result.ok:
                return
        folder = p if await asyncio.to_thread(p.is_dir) else p.parent
        await asyncio.to_thread(lambda: self._spawn([*self._opener_argv(), str(folder)]))

    # ------------------------------------------------------------------------------ closing
    async def close_app(self, name: str, *, force: bool = False) -> list[str]:
        if not clean_query(name):
            return []
        entry = catalog_lookup(name)
        app: InstalledApp | None = None
        try:
            app = (await self._apps.get()).find(name)
        except Exception:
            log.debug("App index unavailable while closing %s", name, exc_info=True)
        targets = candidate_process_names(name, app=app, entry=entry, windows=False)
        spoken = set(candidate_process_names(name, windows=False))     # what the user literally named
        blocked = [t for t in targets if t in POSIX_CRITICAL_PROCESSES]
        targets = [t for t in targets if t not in POSIX_CRITICAL_PROCESSES]
        if blocked and (not targets or spoken.intersection(blocked)):
            raise PermissionDenied(f"{blocked[0]} is a critical system process; JARVIS will not close it.")
        if not targets:
            return []
        procs = await asyncio.to_thread(self._find_processes, targets)
        if not procs:
            return []
        return await asyncio.to_thread(self._terminate, procs, force)

    @staticmethod
    def _find_processes(targets: Sequence[str]) -> list[psutil.Process]:
        wanted = {t.lower() for t in targets}
        protected = _protected_pids()
        uid = os.getuid() if hasattr(os, "getuid") else None
        found: list[psutil.Process] = []
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "uids"]):
            info = proc.info
            names = {(info.get("name") or "").lower()}
            if info.get("exe"):
                names.add(posixpath.basename(info["exe"]).lower())
            cmdline = info.get("cmdline") or []
            if cmdline:
                names.add(posixpath.basename(cmdline[0]).lower())
            names.discard("")
            if not names & wanted or names & POSIX_CRITICAL_PROCESSES or proc.pid in protected:
                continue
            uids = info.get("uids")
            if uid is not None and (uids is None or uids.real != uid):
                continue                                  # only the user's own processes
            found.append(proc)
        return found

    def _terminate(self, procs: list[psutil.Process], force: bool) -> list[str]:
        names = {p.pid: str(p.info.get("name") or "") for p in procs}
        for proc in procs:
            with contextlib.suppress(psutil.Error):
                proc.terminate()                          # SIGTERM: lets the app save and quit
        gone, alive = psutil.wait_procs(procs, timeout=self.graceful_close_timeout_s)
        closed = list(gone)
        if alive and force:
            for proc in alive:
                with contextlib.suppress(psutil.Error):
                    proc.kill()
            gone_now, _ = psutil.wait_procs(alive, timeout=3.0)
            closed.extend(gone_now)
        return sorted({names[p.pid] for p in closed if names.get(p.pid)}, key=str.lower)

    async def running_apps(self) -> list[dict[str, Any]]:
        if not self.is_macos and shutil.which("wmctrl"):
            result = await self._run(["wmctrl", "-lp"], timeout=10.0)
            rows = parse_wmctrl(result.stdout) if result.ok else []
            if rows:
                return await asyncio.to_thread(self._describe_windows, rows)
        return await asyncio.to_thread(self._process_apps)

    @staticmethod
    def _describe_windows(rows: Sequence[tuple[int, str]]) -> list[dict[str, Any]]:
        apps: dict[int, dict[str, Any]] = {}
        for pid, title in rows:
            if pid in apps:
                apps[pid]["windows"] += 1
                continue
            try:
                proc = psutil.Process(pid)
                name, rss = proc.name(), proc.memory_info().rss
            except psutil.Error:
                continue
            apps[pid] = {"name": name, "pid": pid, "title": title, "memory_mb": round(rss / 1_048_576, 1), "windows": 1}
        return sorted(apps.values(), key=lambda e: (e["name"].lower(), e["pid"]))

    @staticmethod
    def _process_apps(limit: int = 40) -> list[dict[str, Any]]:
        uid = os.getuid() if hasattr(os, "getuid") else None
        rows: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "exe", "uids", "memory_info"]):
            info = proc.info
            name = str(info.get("name") or "")
            if not name or not info.get("exe") or name.lower() in POSIX_CRITICAL_PROCESSES:
                continue                                   # kernel threads have no exe
            uids = info.get("uids")
            if uid is not None and (uids is None or uids.real != uid):
                continue
            mem = info.get("memory_info")
            rows.append({"name": name, "pid": proc.pid, "title": None,
                         "memory_mb": round((mem.rss if mem else 0) / 1_048_576, 1)})
        rows.sort(key=lambda r: r["memory_mb"], reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------------------ audio
    async def get_volume(self) -> int | None:
        if self.is_macos:
            result = await self._osascript("output volume of (get volume settings)")
            text = (result.stdout or "").strip()
            return max(0, min(100, int(text))) if result.ok and text.isdigit() else None
        if shutil.which("pactl"):
            result = await self._run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
            value = parse_percent(result.stdout) if result.ok else None
            if value is not None:
                return value
        if shutil.which("amixer"):
            result = await self._run(["amixer", "get", "Master"])
            if result.ok:
                return parse_amixer(result.stdout)[0]
        return None

    async def set_volume(self, level: int) -> int | None:
        level = max(0, min(100, int(level)))
        if self.is_macos:
            self._ensure(await self._osascript(f"set volume output volume {level}"), "set the volume")
            return level
        if shutil.which("pactl"):
            result = await self._run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
            if result.ok:
                return level
        if shutil.which("amixer"):
            self._ensure(await self._run(["amixer", "-q", "set", "Master", f"{level}%"]), "set the volume")
            return level
        raise HostUnsupported(VOLUME_HINT)

    async def change_volume(self, delta: int) -> int | None:
        delta = int(delta)
        if self.is_macos:
            current = await self.get_volume()
            if current is None:
                raise HostUnsupported("Couldn't read the current volume.")
            return await self.set_volume(current + delta)
        if shutil.which("pactl"):
            sign = "+" if delta >= 0 else "-"
            result = await self._run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{sign}{abs(delta)}%"])
            if result.ok:
                new = await self.get_volume()
                if new is not None and new >= 100 and delta > 0:
                    return await self.set_volume(100)    # pactl happily goes past 100 %
                return new
        if shutil.which("amixer"):
            step = f"{abs(delta)}%{'+' if delta >= 0 else '-'}"
            self._ensure(await self._run(["amixer", "-q", "set", "Master", step]), "change the volume")
            return await self.get_volume()
        raise HostUnsupported(VOLUME_HINT)

    async def set_mute(self, mute: bool) -> None:
        what = "mute" if mute else "unmute"
        if self.is_macos:
            self._ensure(await self._osascript(f"set volume output muted {'true' if mute else 'false'}"), what)
            return
        result: ProcessResult | None = None
        if shutil.which("pactl"):
            result = await self._run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if mute else "0"])
            if result.ok:
                return
        if shutil.which("amixer"):
            result = await self._run(["amixer", "-q", "set", "Master", what])
        if result is None:
            raise HostUnsupported(VOLUME_HINT)
        self._ensure(result, what)

    # ------------------------------------------------------------------------------ display
    async def get_brightness(self) -> int | None:
        if not shutil.which("brightnessctl"):
            raise HostUnsupported(BRIGHTNESS_HINT)
        result = await self._run(["brightnessctl", "-m"])
        value = parse_brightnessctl(result.stdout) if result.ok else None
        if value is None:
            raise HostUnsupported(f"No controllable backlight was found. {result.summary(160)}")
        return value

    async def set_brightness(self, level: int) -> int | None:
        level = max(0, min(100, int(level)))
        if not shutil.which("brightnessctl"):
            raise HostUnsupported(BRIGHTNESS_HINT)
        self._ensure(await self._run(["brightnessctl", "-q", "set", f"{level}%"]), "change the brightness")
        return level

    async def media(self, action: MediaAction) -> None:
        command = PLAYERCTL_COMMANDS.get(action)
        if command is None:
            raise HostUnsupported(f"Unknown media action: {action!r}")
        if not shutil.which("playerctl"):
            raise HostUnsupported("Media control needs playerctl (MPRIS).")
        result = await self._run(["playerctl", command])
        if not result.ok:
            raise HostUnsupported(f"No media player responded. {result.summary(160)}")

    # ------------------------------------------------------------------------------ session / power
    async def lock_screen(self) -> None:
        if self.is_macos:
            candidates = [["pmset", "displaysleepnow"]]
        else:
            candidates = [["loginctl", "lock-session"], ["xdg-screensaver", "lock"], ["gnome-screensaver-command", "-l"],
                          ["dm-tool", "lock"], ["xflock4"], ["light-locker-command", "-l"]]
        tried: list[str] = []
        for argv in candidates:
            if not shutil.which(argv[0]):
                continue
            tried.append(argv[0])
            result = await self._run(argv)
            if result.ok:
                return
        raise HostUnsupported("Couldn't lock the screen" + (f" (tried {', '.join(tried)})." if tried else
                                                             ": no screen locker is installed."))

    async def power(self, action: PowerAction, delay_s: int = 0) -> None:
        argv = power_argv_posix(action, macos=self.is_macos, which=shutil.which)   # validates first
        delay = max(0, int(delay_s))
        if delay > 0:
            self._delayed.schedule(delay, lambda: self.power(action, 0), label=action)
            return
        self._ensure(await self._run(argv, timeout=30.0), action)

    async def cancel_power(self) -> None:
        self._delayed.cancel_all()

    # ------------------------------------------------------------------------------ misc
    async def screenshot(self, path: Path) -> Path:
        target = await asyncio.to_thread(_prepare_screenshot, Path(path))
        try:
            await asyncio.to_thread(_grab_with_pillow, target)
            return target
        except ImportError:
            pass
        except Exception as exc:
            log.debug("Pillow screenshot failed: %s", exc)
        for argv in self._screenshot_commands(target):
            result = await self._run(argv, timeout=30.0)
            if result.ok and await asyncio.to_thread(_non_empty_file, target):
                return target
        raise HostUnsupported("Couldn't take a screenshot: no graphical session or no screenshot tool "
                              "(install gnome-screenshot, grim, scrot or ImageMagick).")

    def _screenshot_commands(self, target: Path) -> list[list[str]]:
        t = str(target)
        if self.is_macos:
            return [["screencapture", "-x", t]]
        candidates = [["gnome-screenshot", "-f", t], ["spectacle", "-b", "-n", "-f", "-o", t], ["grim", t],
                      ["scrot", "-o", t], ["maim", t], ["import", "-window", "root", t],
                      ["xfce4-screenshooter", "-f", "-s", t]]
        return [argv for argv in candidates if shutil.which(argv[0])]

    async def notify(self, title: str, message: str) -> None:
        title = title or "JARVIS"
        if self.is_macos:
            argv = ["osascript", "-e", "on run argv", "-e",
                    "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                    title, message or ""]
        else:
            if not shutil.which("notify-send"):
                raise HostUnsupported("Desktop notifications need notify-send (libnotify).")
            argv = ["notify-send", "--app-name=JARVIS", "--", title, message or ""]
        self._ensure(await self._run(argv), "show a notification")

    def _clipboard_tools(self) -> tuple[list[str], list[str]]:
        if self.is_macos:
            return ["pbpaste"], ["pbcopy"]
        wayland = (["wl-paste", "--no-newline"], ["wl-copy"])
        x11 = [(["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"]),
               (["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"])]
        options = [wayland, *x11] if os.environ.get("WAYLAND_DISPLAY") else [*x11, wayland]
        for get_argv, set_argv in options:
            if shutil.which(get_argv[0]) and shutil.which(set_argv[0]):
                return get_argv, set_argv
        raise HostUnsupported(CLIPBOARD_HINT)

    async def get_clipboard(self) -> str:
        get_argv, _ = self._clipboard_tools()
        result = await self._run(get_argv, timeout=10.0)
        if result.ok:
            return result.stdout
        if re.search(r"nothing is copied|not available|no selection|empty", result.stderr or "", re.I):
            return ""
        raise HostUnsupported(f"Couldn't read the clipboard. {result.summary(160)}")

    async def set_clipboard(self, text: str) -> None:
        _, set_argv = self._clipboard_tools()
        code = await asyncio.to_thread(_feed_detached, set_argv, (text or "").encode("utf-8"))
        if code != 0:
            raise HostUnsupported(f"Couldn't write the clipboard ({set_argv[0]} exited with {code}).")

    def shell_argv(self, command: str, shell: str | None = None) -> list[str]:
        kind = (shell or self.default_shell).strip().lower()
        if kind == "bash":
            return ["bash", "-lc", command]
        if kind in ("sh", "dash"):
            return [kind, "-c", command]
        if kind == "zsh":
            return ["zsh", "-lc", command]
        if kind in ("pwsh", "powershell"):
            pwsh = shutil.which("pwsh")
            if pwsh is None:
                raise HostUnsupported("PowerShell (pwsh) isn't installed.")
            return [pwsh, "-NoProfile", "-NonInteractive", "-Command", command]
        raise HostUnsupported(f"Unknown shell: {shell!r} (use bash, sh or zsh).")

    @property
    def default_shell(self) -> str:
        return "bash"

    # ------------------------------------------------------------------------------ helpers
    async def _run(self, argv: Sequence[str], *, timeout: float = 15.0) -> ProcessResult:
        return await run_process(argv, timeout=timeout, registry=self._processes, owner="host")

    async def _osascript(self, script: str) -> ProcessResult:
        return await self._run(["osascript", "-e", script])

    @staticmethod
    def _ensure(result: ProcessResult, what: str) -> None:
        if not result.ok:
            raise HostUnsupported(f"Couldn't {what}: {result.summary(240)}")
