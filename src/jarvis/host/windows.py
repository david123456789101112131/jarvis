"""Windows host adapter (`WindowsHost`).

How each operation works:

* **installed apps** — Start-menu shortcuts (`.lnk` parsed in pure Python for their target
  exe, `.url` launchers such as Steam games), `Get-StartApps` (Store/UWP apps and every Start
  entry with its AppUserModelID), the App Paths registry (HKLM 64/32-bit views + HKCU) and
  catalog apps found on PATH or through a registered URL protocol. Merged, linked to catalog
  ids and cached (JSON, 24 h) by `jarvis.host.app_index`; slow parts run in worker threads.
* **launching** — shortcuts/URIs with `os.startfile` (ShellExecute), executables with
  `subprocess.Popen` (list argv, never a shell; detached, own process group, console apps get
  a new console), Store apps through `explorer.exe shell:AppsFolder\\<AppID>`.
* **closing** — catalog/app process names → psutil; graceful `taskkill /PID` (WM_CLOSE) first,
  `TerminateProcess` only with force; critical system processes are refused and File Explorer
  windows are closed instead of killing explorer.exe.
* **volume** — Core Audio (`IAudioEndpointVolume` via ctypes COM), falling back to volume keys
  (2 % per press); **brightness** — WMI through PowerShell; **media / lock / sleep / clipboard**
  — Win32 via `jarvis.host.winapi`; **shutdown / restart / log off** — `shutdown.exe`.
* **screenshots** — Pillow `ImageGrab` (all monitors) or a PowerShell/System.Drawing fallback;
  **notifications** — WinRT toasts from Windows PowerShell 5.1.

Every helper process goes through `jarvis.security.process.run_process` (sanitized env,
timeouts, no console window); only detached app launches use `Popen` directly. The module
imports on any OS: pure helpers are unit-tested on Linux.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ntpath
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from xml.sax.saxutils import escape as _xml_escape

import psutil

from jarvis.common.errors import PermissionDenied
from jarvis.common.text import normalize
from jarvis.host import winapi
from jarvis.host.app_index import (
    WINDOWS_WRAPPER_EXES,
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
IS_WINDOWS = sys.platform == "win32"

# Process-creation flags (subprocess only defines them on Windows).
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
GUI_LAUNCH_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
CONSOLE_LAUNCH_FLAGS = CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP
BATCH_LAUNCH_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
IMAGE_SUBSYSTEM_WINDOWS_CUI = 3
ERROR_ELEVATION_REQUIRED = 740
SHUTDOWN_NOTHING_TO_ABORT = 1116
MAX_SHUTDOWN_DELAY_S = 315_360_000          # shutdown.exe /t limit (10 years)
VOLUME_KEY_STEP = 2                         # percent per VK_VOLUME_UP/DOWN press
VOLUME_KEY_FULL_SWEEP = 50                  # presses that take any level to 0

PS_UTF8_PREFIX = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
POWERSHELL_AUMID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
APP_PATHS_KEY = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths"
EXPLORER_WINDOW_CLASSES = ("CabinetWClass", "ExploreWClass")

# Never closed by JARVIS: killing them crashes or logs off Windows, or breaks the shell.
CRITICAL_PROCESSES = frozenset(
    {
        "system", "system idle process", "registry", "memory compression", "secure system", "smss.exe",
        "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe", "svchost.exe",
        "dwm.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe", "spoolsv.exe", "audiodg.exe", "wudfhost.exe",
        "logonui.exe", "userinit.exe", "explorer.exe", "applicationframehost.exe", "startmenuexperiencehost.exe",
        "shellexperiencehost.exe", "searchhost.exe", "textinputhost.exe", "msmpeng.exe", "conhost.exe",
    }
)
# Background helpers left out of the psutil fallback of `running_apps`.
_BACKGROUND_PROCESSES = frozenset(
    {"runtimebroker.exe", "dllhost.exe", "backgroundtaskhost.exe", "taskhostw.exe", "smartscreen.exe",
     "searchindexer.exe", "searchprotocolhost.exe", "searchfilterhost.exe", "securityhealthsystray.exe",
     "widgets.exe", "widgetservice.exe", "useroobebroker.exe", "lockapp.exe", "gamebar.exe", "gamebarftserver.exe",
     "crashpad_handler.exe", "nvcontainer.exe", "onedrive.standalone.updater.exe"}
)


# =============================================================================================
# Small path / environment helpers
# =============================================================================================
_ENV_VAR_RE = re.compile(r"%([^%\s]+)%")


def expand_windows_vars(text: str, env: Mapping[str, str] | None = None) -> str:
    """Expand %VAR% references (case-insensitive); unknown variables are left as they are."""
    source = os.environ if env is None else env
    lookup = {k.upper(): v for k, v in source.items()}
    return _ENV_VAR_RE.sub(lambda m: lookup.get(m.group(1).upper(), m.group(0)), text or "")


def _windows_dir() -> str:
    return os.environ.get("SystemRoot") or os.environ.get("windir") or "C:\\Windows"


def _system32_exe(name: str) -> str:
    path = ntpath.join(_windows_dir(), "System32", name)
    return path if os.path.isfile(path) else name


def _powershell_exe() -> str:
    path = ntpath.join(_windows_dir(), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.isfile(path) else "powershell.exe"


def _explorer_exe() -> str:
    path = ntpath.join(_windows_dir(), "explorer.exe")
    return path if os.path.isfile(path) else "explorer.exe"


def split_windows_args(text: str) -> list[str]:
    """Split a command line the way CommandLineToArgvW / the MSVC runtime do."""
    args: list[str] = []
    current: list[str] = []
    in_quotes = have_arg = False
    i, n = 0, len(text or "")
    while i < n:
        ch = text[i]
        if ch == "\\":
            j = i
            while j < n and text[j] == "\\":
                j += 1
            slashes = j - i
            if j < n and text[j] == '"':
                current.append("\\" * (slashes // 2))
                if slashes % 2:                 # \" is a literal quote
                    current.append('"')
                    j += 1
                i = j
            else:
                current.append("\\" * slashes)
                i = j
            have_arg = True
            continue
        if ch == '"':
            if in_quotes and i + 1 < n and text[i + 1] == '"':
                current.append('"')             # "" inside quotes is a literal quote
                i += 2
            else:
                in_quotes = not in_quotes
                i += 1
            have_arg = True
            continue
        if ch in " \t" and not in_quotes:
            if have_arg:
                args.append("".join(current))
                current, have_arg = [], False
            i += 1
            continue
        current.append(ch)
        have_arg = True
        i += 1
    if have_arg:
        args.append("".join(current))
    return args


def pe_subsystem(path: str) -> int | None:
    """PE optional-header Subsystem of an executable (2 = GUI, 3 = console), None if unreadable."""
    try:
        with open(path, "rb") as fh:
            dos = fh.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return None
            (e_lfanew,) = struct.unpack_from("<I", dos, 0x3C)
            if not 0 < e_lfanew < 0x1000_0000:
                return None
            fh.seek(e_lfanew)
            nt = fh.read(4 + 20 + 70)
    except OSError:
        return None
    if len(nt) < 4 + 20 + 70 or nt[:4] != b"PE\x00\x00":
        return None
    (magic,) = struct.unpack_from("<H", nt, 24)
    if magic not in (0x10B, 0x20B):             # PE32 / PE32+: Subsystem is at offset 68 in both
        return None
    (subsystem,) = struct.unpack_from("<H", nt, 24 + 68)
    return subsystem


def launch_flags_for(path: str, *, subsystem: Callable[[str], int | None] = pe_subsystem) -> int:
    """GUI apps start detached; console apps get their own console; batch launchers stay hidden."""
    if path.lower().endswith((".cmd", ".bat")):
        return BATCH_LAUNCH_FLAGS
    return CONSOLE_LAUNCH_FLAGS if subsystem(path) == IMAGE_SUBSYSTEM_WINDOWS_CUI else GUI_LAUNCH_FLAGS


# =============================================================================================
# Shortcuts (.lnk per MS-SHLLINK, .url)
# =============================================================================================
_LNK_CLSID = uuid.UUID("00021401-0000-0000-C000-000000000046").bytes_le
_HAS_ID_LIST = 0x01
_HAS_LINK_INFO = 0x02
_HAS_NAME = 0x04
_HAS_RELATIVE_PATH = 0x08
_HAS_WORKING_DIR = 0x10
_HAS_ARGUMENTS = 0x20
_HAS_ICON_LOCATION = 0x40
_IS_UNICODE = 0x80
_FORCE_NO_LINK_INFO = 0x100
_ENV_BLOCK_SIGNATURE = 0xA0000001
_DARWIN_BLOCK_SIGNATURE = 0xA0000006
_ANSI = "mbcs" if IS_WINDOWS else "cp1252"


@dataclass
class ShellLink:
    target: str | None = None
    arguments: str = ""
    working_dir: str | None = None
    relative_path: str | None = None
    description: str | None = None
    icon_location: str | None = None
    advertised: bool = False        # MSI "advertised" shortcut: Darwin descriptor, no plain target


def _cstr_a(buf: bytes, offset: int) -> str:
    end = buf.find(b"\x00", offset)
    raw = buf[offset:] if end < 0 else buf[offset:end]
    return raw.decode(_ANSI, "replace")


def _cstr_w(buf: bytes, offset: int) -> str:
    end = offset
    while end + 1 < len(buf) and buf[end:end + 2] != b"\x00\x00":
        end += 2
    return buf[offset:end].decode("utf-16-le", "replace")


def _link_info_target(info: bytes) -> str | None:
    if len(info) < 0x1C:
        return None
    _size, header_size, flags, _volume, base_off, net_off, suffix_off = struct.unpack_from("<7I", info, 0)
    base_u = suffix_u = 0
    if header_size >= 0x24 and len(info) >= 0x24:
        base_u, suffix_u = struct.unpack_from("<2I", info, 0x1C)
    suffix = _cstr_w(info, suffix_u) if suffix_u else (_cstr_a(info, suffix_off) if suffix_off else "")
    if flags & 0x1 and (base_u or base_off):                  # VolumeIDAndLocalBasePath
        base = _cstr_w(info, base_u) if base_u else _cstr_a(info, base_off)
        return (base + suffix) or None
    if flags & 0x2 and net_off and len(info) >= net_off + 0x14:   # CommonNetworkRelativeLink
        net = info[net_off:]
        _nsize, _nflags, name_off, _device_off, _provider = struct.unpack_from("<5I", net, 0)
        name = ""
        if name_off > 0x14 and len(net) >= 0x1C:
            (name_u,) = struct.unpack_from("<I", net, 0x14)
            name = _cstr_w(net, name_u) if name_u else ""
        name = name or _cstr_a(net, name_off)
        if not name:
            return None
        return name + ("\\" + suffix if suffix and not name.endswith("\\") else suffix)
    return None


def parse_lnk_bytes(data: bytes, *, env: Mapping[str, str] | None = None, link_dir: str | None = None) -> ShellLink | None:
    """Parse a shell link (.lnk). Returns None for anything that isn't a readable shortcut."""
    try:
        return _parse_lnk(data, env, link_dir)
    except (struct.error, IndexError, ValueError):
        return None


def _parse_lnk(data: bytes, env: Mapping[str, str] | None, link_dir: str | None) -> ShellLink | None:
    if len(data) < 76 or struct.unpack_from("<I", data, 0)[0] != 0x4C or data[4:20] != _LNK_CLSID:
        return None
    (flags,) = struct.unpack_from("<I", data, 20)
    pos = 76
    link = ShellLink()
    if flags & _HAS_ID_LIST:
        (size,) = struct.unpack_from("<H", data, pos)
        pos += 2 + size
    info_target = None
    if flags & _HAS_LINK_INFO:
        (size,) = struct.unpack_from("<I", data, pos)
        if size < 4 or pos + size > len(data):
            return None
        info_target = _link_info_target(data[pos:pos + size])
        pos += size
    width = 2 if flags & _IS_UNICODE else 1

    def read_string() -> str:
        nonlocal pos
        (count,) = struct.unpack_from("<H", data, pos)
        pos += 2
        raw = data[pos:pos + count * width]
        if len(raw) != count * width:
            raise ValueError("truncated StringData")
        pos += count * width
        return raw.decode("utf-16-le", "replace") if width == 2 else raw.decode(_ANSI, "replace")

    if flags & _HAS_NAME:
        link.description = read_string()
    if flags & _HAS_RELATIVE_PATH:
        link.relative_path = read_string()
    if flags & _HAS_WORKING_DIR:
        link.working_dir = read_string()
    if flags & _HAS_ARGUMENTS:
        link.arguments = read_string()
    if flags & _HAS_ICON_LOCATION:
        link.icon_location = read_string()
    env_target = ""
    while pos + 8 <= len(data):
        size, signature = struct.unpack_from("<II", data, pos)
        if size < 8 or pos + size > len(data):
            break
        block = data[pos:pos + size]
        if signature == _ENV_BLOCK_SIGNATURE and size >= 0x314:
            env_target = _cstr_w(block, 8 + 260) or _cstr_a(block, 8)
        elif signature == _DARWIN_BLOCK_SIGNATURE:
            link.advertised = True
        pos += size

    expanded = expand_windows_vars(env_target, env) if env_target else ""
    if expanded and "%" not in expanded:
        link.target = expanded
    elif info_target and not flags & _FORCE_NO_LINK_INFO:
        link.target = info_target
    elif link.relative_path and link_dir:
        link.target = ntpath.normpath(ntpath.join(link_dir, link.relative_path))
    elif expanded:
        link.target = expanded
    if link.working_dir:
        link.working_dir = expand_windows_vars(link.working_dir, env)
    return link


def parse_lnk(path: str | Path, *, env: Mapping[str, str] | None = None) -> ShellLink | None:
    try:
        with open(path, "rb") as fh:
            data = fh.read(1 << 20)               # real shortcuts are a few KB
    except OSError:
        return None
    return parse_lnk_bytes(data, env=env, link_dir=ntpath.dirname(str(path)))


_SQUIRREL_RE = re.compile(r"--processStart(?:AndWait)?\s+\"?([^\"\s]+\.exe)", re.I)


def link_process_name(link: ShellLink | None) -> str | None:
    """Process the shortcut ends up running (lower-case), when that can be known."""
    if link is None or not link.target:
        return None
    base = ntpath.basename(link.target).lower()
    if not base.endswith(".exe"):
        return None
    if base in ("update.exe", "squirrel.exe"):       # Squirrel: Update.exe --processStart "Slack.exe"
        m = _SQUIRREL_RE.search(link.arguments or "")
        return m.group(1).lower() if m else None
    if base in WINDOWS_WRAPPER_EXES and (link.arguments or "").strip():
        return None                                  # cmd.exe /c …, explorer.exe shell:…, wsl.exe -d …
    return base


def parse_url_shortcut(text: str) -> str | None:
    """URL= of the [InternetShortcut] section of a .url file."""
    in_section = False
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("["):
            in_section = s.lower() == "[internetshortcut]"
        elif in_section and s.lower().startswith("url="):
            return s[4:].strip() or None
    return None


def read_url_shortcut(path: str | Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(64 * 1024)
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(_ANSI, "replace")
    return parse_url_shortcut(text)


_AUX_RE = re.compile(
    r"\b(?:uninstall\w*|unins\d*|uninstaller|remove|help|readme|read me|documentation|manual|license|licence|"
    r"release notes|changelog|change log|what'?s new|eula|faq|website|web site|homepage|home page|user guide|"
    r"online guide|getting started|troubleshoot\w*)\b"
    r"|\b(?:הסרת|הסר|עזרה|קרא אותי|רישיון|תיעוד)\b",
    re.I,
)
_DOCUMENT_EXTS = frozenset(
    {".txt", ".pdf", ".chm", ".hlp", ".htm", ".html", ".mht", ".mhtml", ".rtf", ".doc", ".docx", ".md", ".xml",
     ".ini", ".log", ".url", ".json", ".csv", ".xps", ".odt"}
)
_UNINSTALLER_RE = re.compile(r"^(?:unins\d*|uninstall\w*|uninst\w*)\.exe$", re.I)
_WEB_SCHEME_RE = re.compile(r"^(?:https?|file|ftp|mailto):", re.I)
_SKIP_START_MENU_DIRS = frozenset({"startup"})


def is_auxiliary_name(name: str) -> bool:
    """Uninstallers, help files, readmes, licences, websites… (not apps)."""
    return bool(_AUX_RE.search(normalize(name)))


def _start_menu_entry(path: str, stem: str, ext: str, env: Mapping[str, str] | None) -> InstalledApp | None:
    if ext == ".url":
        url = read_url_shortcut(path)
        if not url or _WEB_SCHEME_RE.match(url):
            return None                     # web pages / docs; game launchers (steam://…) are kept
        return InstalledApp(stem, path, "lnk", "startmenu")
    link = parse_lnk(path, env=env)
    if link is not None and link.target:
        base = ntpath.basename(link.target).lower()
        if ntpath.splitext(base)[1] in _DOCUMENT_EXTS or _UNINSTALLER_RE.match(base):
            return None
    process = link_process_name(link)
    return InstalledApp(stem, path, "lnk", "startmenu", process_names=(process,) if process else ())


def scan_start_menu(roots: Iterable[str | Path], *, env: Mapping[str, str] | None = None) -> list[InstalledApp]:
    """Apps from Start-menu folders (recursive); display name = shortcut file name."""
    apps: list[InstalledApp] = []
    seen: set[str] = set()
    for root in roots:
        root_str = str(root)
        if not os.path.isdir(root_str):
            continue
        for dirpath, dirnames, filenames in os.walk(root_str):
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_START_MENU_DIRS]
            for filename in sorted(filenames):
                stem, ext = os.path.splitext(filename)
                ext = ext.lower()
                if ext not in (".lnk", ".url") or not stem.strip() or is_auxiliary_name(stem):
                    continue
                full = os.path.join(dirpath, filename)
                key = os.path.normcase(full)
                if key in seen:
                    continue
                seen.add(key)
                app = _start_menu_entry(full, stem.strip(), ext, env)
                if app is not None:
                    apps.append(app)
    return apps


# =============================================================================================
# Get-StartApps
# =============================================================================================
_URI_APP_ID_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)
_EXE_IN_APP_ID_RE = re.compile(r"([A-Za-z0-9_\-]+\.exe)(?![A-Za-z0-9_])", re.I)


def _process_from_app_id(app_id: str) -> str | None:
    if "\\" in app_id or "/" in app_id:
        base = ntpath.basename(app_id).lower()
        return base if base.endswith(".exe") and base not in WINDOWS_WRAPPER_EXES else None
    m = _EXE_IN_APP_ID_RE.search(app_id)      # "Microsoft.Office.WINWORD.EXE.15" → winword.exe
    return m.group(1).lower() if m else None


def parse_start_apps(text: str) -> list[InstalledApp]:
    """Parse `Get-StartApps | ConvertTo-Json` output (a single object or an array)."""
    raw = (text or "").strip().lstrip("\ufeff")
    starts = [i for i in (raw.find("["), raw.find("{")) if i >= 0]
    if not starts:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(raw, min(starts))
    except json.JSONDecodeError:
        log.debug("Get-StartApps returned invalid JSON")
        return []
    items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
    apps: list[InstalledApp] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        app_id = str(item.get("AppID") or item.get("AppId") or item.get("AppUserModelId") or "").strip()
        if not name or not app_id or is_auxiliary_name(name) or _WEB_SCHEME_RE.match(app_id):
            continue
        if _URI_APP_ID_RE.match(app_id):                         # steam://rungameid/…
            apps.append(InstalledApp(name, app_id, "uri", "appx"))
            continue
        process = _process_from_app_id(app_id)
        if re.match(r"^[a-z]:\\", app_id, re.I) and app_id.lower().endswith(".exe"):
            apps.append(InstalledApp(name, app_id, "exe", "appx", process_names=(process,) if process else ()))
            continue
        apps.append(InstalledApp(name, app_id, "appx", "appx", process_names=(process,) if process else ()))
    return apps


# =============================================================================================
# App Paths registry
# =============================================================================================
_INSTALLER_STEM_RE = re.compile(r"^(?:setup|install|installer|unins\w*|uninst\w*|update)$", re.I)
_GENERIC_DESCRIPTIONS = frozenset({"application", "app", "launcher", "setup", "installer", "main", "program",
                                   "executable", "bootstrapper", "stub"})


def _registry_exe_path(value: str, env: Mapping[str, str] | None) -> str:
    text = expand_windows_vars((value or "").strip(), env)
    if text.startswith('"'):
        return text[1:].split('"', 1)[0].strip()
    m = re.match(r"(.+?\.exe)(?:\s|$)", text, re.I)
    return (m.group(1) if m else text).strip()


def _useful_description(text: str | None) -> bool:
    if not text:
        return False
    low = text.strip().lower()
    return 2 <= len(low) <= 80 and low not in _GENERIC_DESCRIPTIONS and not low.endswith((".exe", ".dll"))


def read_app_paths_registry() -> list[tuple[str, str]]:
    """(subkey, default value) rows of HKLM (64- and 32-bit views) and HKCU App Paths."""
    import winreg  # Windows-only module

    rows: list[tuple[str, str]] = []
    views = (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, 0),
    )
    for hive, view in views:
        try:
            root = winreg.OpenKey(hive, APP_PATHS_KEY, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name, 0, winreg.KEY_READ | view) as sub:
                        value, value_type = winreg.QueryValueEx(sub, "")
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    if value_type == winreg.REG_EXPAND_SZ:
                        value = winreg.ExpandEnvironmentStrings(value)
                    rows.append((name, value))
    return rows


def app_paths_to_apps(
    rows: Iterable[tuple[str, str]],
    *,
    isfile: Callable[[str], bool] = os.path.isfile,
    describe: Callable[[str], str | None] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[InstalledApp]:
    """InstalledApp entries for App Paths rows whose executable exists."""
    apps: list[InstalledApp] = []
    for key_name, value in rows:
        path = _registry_exe_path(value, env)
        if not path or not path.lower().endswith(".exe") or not isfile(path):
            continue
        stem = ntpath.splitext(ntpath.basename(key_name))[0] or ntpath.splitext(ntpath.basename(path))[0]
        if not stem or is_auxiliary_name(stem) or _INSTALLER_STEM_RE.match(stem):
            continue
        description: str | None = None
        if describe is not None:
            try:
                description = describe(path)
            except Exception:
                description = None
        name = description.strip() if description and _useful_description(description) else stem
        aliases = (stem,) if match_key(name) != match_key(stem) else ()
        apps.append(InstalledApp(name, path, "exe", "apppaths", process_names=(ntpath.basename(path).lower(),),
                                 aliases=aliases))
    return apps


# =============================================================================================
# Catalog apps (PATH / URL protocols)
# =============================================================================================
def prefer_real_exe(path: str, isfile: Callable[[str], bool] = os.path.isfile) -> str:
    """For a .cmd/.bat launcher (VS Code's bin\\code.cmd) prefer the real exe next to it."""
    if not path.lower().endswith((".cmd", ".bat")):
        return path
    folder = ntpath.dirname(path)
    stem = ntpath.splitext(ntpath.basename(path))[0]
    for candidate in (ntpath.join(ntpath.dirname(folder), stem + ".exe"), ntpath.join(folder, stem + ".exe")):
        if isfile(candidate):
            return candidate
    return path


def catalog_apps_windows(
    *,
    which: Callable[[str], str | None] = shutil.which,
    uri_ok: Callable[[str], bool] = lambda _uri: False,
    isfile: Callable[[str], bool] = os.path.isfile,
) -> list[InstalledApp]:
    """Catalog apps that are launchable here: an executable on PATH, else a registered URL protocol."""
    apps: list[InstalledApp] = []
    for entry in APPS:
        found: InstalledApp | None = None
        for exe in entry.windows:
            if exe.lower() in ("update.exe", "squirrel.exe"):
                continue                    # generic Squirrel launcher names: PATH hits would be other apps
            path = which(exe)
            if path:
                found = InstalledApp(entry.display, prefer_real_exe(path, isfile), "exe", "catalog", id=entry.id,
                                     process_names=entry.process_names, aliases=entry.aliases)
                break
        if found is None and entry.uri and uri_ok(entry.uri):
            found = InstalledApp(entry.display, entry.uri, "uri", "catalog", id=entry.id,
                                 process_names=entry.process_names, aliases=entry.aliases)
        if found is not None:
            apps.append(found)
    return apps


def uri_registered(uri: str) -> bool:
    """Whether a URL protocol ("ms-settings:", "spotify:") has a handler on this machine."""
    scheme = (uri or "").split(":", 1)[0].strip().lower()
    if not scheme:
        return False
    try:
        if winapi.protocol_handler(scheme):
            return True
    except (HostUnsupported, OSError):
        pass
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme) as key:
            winreg.QueryValueEx(key, "URL Protocol")
            return True
    except OSError:
        return False


# =============================================================================================
# Launch planning
# =============================================================================================
@dataclass
class LaunchPlan:
    method: str                      # startfile | uri | popen | shell:AppsFolder
    target: str = ""                 # startfile target
    arguments: str = ""              # startfile arguments (already a command-line string)
    argv: tuple[str, ...] = ()       # Popen argv
    cwd: str | None = None
    creationflags: int = 0
    note: str = ""


_BATCH_UNSAFE_RE = re.compile(r"[\"&|<>^%!\r\n]")


def plan_launch(
    app: InstalledApp,
    args: Sequence[str] = (),
    *,
    which: Callable[[str], str | None] = shutil.which,
    read_link: Callable[[str], ShellLink | None] = parse_lnk,
    isfile: Callable[[str], bool] = os.path.isfile,
    subsystem: Callable[[str], int | None] = pe_subsystem,
) -> LaunchPlan:
    """Decide how to start an app. Never builds a shell command string."""
    args = tuple(str(a) for a in args)
    if app.kind == "appx":
        note = "arguments are not passed to Store apps" if args else ""
        return LaunchPlan("shell:AppsFolder", argv=(_explorer_exe(), f"shell:AppsFolder\\{app.target}"),
                          creationflags=GUI_LAUNCH_FLAGS, note=note)
    if app.kind == "uri":
        return LaunchPlan("uri", target=app.target, note="arguments are not passed to URL launches" if args else "")
    if app.kind == "lnk":
        if not args:
            return LaunchPlan("startfile", target=app.target)
        link = read_link(app.target) if app.target.lower().endswith(".lnk") else None
        if link is not None and link.target and link.target.lower().endswith(".exe") and isfile(link.target):
            argv = (link.target, *split_windows_args(link.arguments), *args)
            cwd = link.working_dir or ntpath.dirname(link.target)
            return LaunchPlan("popen", argv=argv, cwd=cwd, creationflags=launch_flags_for(link.target, subsystem=subsystem))
        return LaunchPlan("startfile", target=app.target, arguments=subprocess.list2cmdline(args))
    if app.kind in ("exe", "command"):
        path = app.target if ntpath.isabs(app.target) and isfile(app.target) else (which(app.target) or "")
        if not path:
            # Not on disk / PATH: ShellExecute still resolves App Paths names ("winword.exe").
            return LaunchPlan("startfile", target=app.target, arguments=subprocess.list2cmdline(args))
        path = prefer_real_exe(path, isfile)
        if path.lower().endswith((".cmd", ".bat")):
            unsafe = [a for a in args if _BATCH_UNSAFE_RE.search(a)]
            if unsafe:
                raise HostUnsupported(f"{app.name} starts through a batch file, which can't receive arguments "
                                      f"with special characters safely: {unsafe[0]!r}")
            return LaunchPlan("popen", argv=(path, *args), cwd=ntpath.dirname(path), creationflags=BATCH_LAUNCH_FLAGS)
        return LaunchPlan("popen", argv=(path, *args), cwd=ntpath.dirname(path) or None,
                          creationflags=launch_flags_for(path, subsystem=subsystem))
    raise HostUnsupported(f"Don't know how to start {app.name} (kind {app.kind!r}).")


def reveal_command_line(path: str, explorer: str = "explorer.exe") -> str:
    """`explorer.exe /select,"<path>"` as a CreateProcess command line (Windows paths can't contain quotes)."""
    if '"' in path:
        raise HostUnsupported("A Windows path can't contain double quotes.")
    return f'{subprocess.list2cmdline([explorer])} /select,"{path}"'


# =============================================================================================
# Volume keys, power, notifications, shells
# =============================================================================================
def volume_key_plan(*, level: int | None = None, delta: int | None = None) -> list[tuple[int, int]]:
    """Key presses for a volume change when Core Audio isn't available (2 % per press).

    set 50 → 50× DOWN (reach 0) then 25× UP; change +10 → 5× UP; change -5 → 3× DOWN.
    """
    if level is not None:
        level = max(0, min(100, int(level)))
        ups = (level + 1) // 2                         # round half up
        plan = [(winapi.VK_VOLUME_DOWN, VOLUME_KEY_FULL_SWEEP)]
        if ups:
            plan.append((winapi.VK_VOLUME_UP, ups))
        return plan
    if delta:
        presses = min(VOLUME_KEY_FULL_SWEEP, max(1, (abs(int(delta)) + 1) // 2))
        return [(winapi.VK_VOLUME_UP if delta > 0 else winapi.VK_VOLUME_DOWN, presses)]
    return []


def mute_key_plan(mute: bool) -> list[tuple[int, int]]:
    """Deterministic mute without reading the state: a volume key always unmutes, then toggle once."""
    plan = [(winapi.VK_VOLUME_UP, 1), (winapi.VK_VOLUME_DOWN, 1)]
    if mute:
        plan.append((winapi.VK_VOLUME_MUTE, 1))
    return plan


MEDIA_KEYS: dict[str, int] = {
    "play_pause": winapi.VK_MEDIA_PLAY_PAUSE,
    "next": winapi.VK_MEDIA_NEXT_TRACK,
    "previous": winapi.VK_MEDIA_PREV_TRACK,
    "stop": winapi.VK_MEDIA_STOP,
}


def power_argv(action: str, delay_s: int = 0, *, shutdown_exe: str = "shutdown.exe") -> list[str]:
    delay = max(0, min(int(delay_s), MAX_SHUTDOWN_DELAY_S))
    if action == "shutdown":
        return [shutdown_exe, "/s", "/t", str(delay)]
    if action == "restart":
        return [shutdown_exe, "/r", "/t", str(delay)]
    if action == "logoff":
        return [shutdown_exe, "/l"]
    raise HostUnsupported(f"No shutdown.exe command for {action!r}.")


_XML_INVALID_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def toast_xml(title: str, message: str) -> str:
    """ToastGeneric XML with escaped text."""

    def clean(text: str, limit: int) -> str:
        value = _XML_INVALID_RE.sub("", text or "").strip()[:limit]
        return _xml_escape(value, {'"': "&quot;", "'": "&apos;"})

    return (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{clean(title, 200) or 'JARVIS'}</text>"
        f"<text>{clean(message, 1500)}</text>"
        "</binding></visual></toast>"
    )


def powershell_argv(command: str, exe: str = "powershell.exe") -> list[str]:
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", PS_UTF8_PREFIX + command]


GET_START_APPS_PS = "Get-StartApps | ConvertTo-Json -Compress"

BRIGHTNESS_GET_PS = (
    "$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | "
    "Select-Object -First 1; if ($null -eq $m) { exit 3 }; [int]$m.CurrentBrightness"
)


def brightness_set_ps(level: int) -> str:
    level = max(0, min(100, int(level)))
    return (
        "$ms = @(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop); "
        "if ($ms.Count -eq 0) { exit 3 }; "
        "foreach ($m in $ms) { Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
        f"-Arguments @{{Timeout=[uint32]1; Brightness=[byte]{level}}} -ErrorAction Stop | Out-Null }}; $ms.Count"
    )


# The output path arrives in an environment variable, so nothing user-controlled is parsed as code.
SCREENSHOT_PS = (
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class JarvisDpi "
    "{ [DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware(); }'; "
    "[void][JarvisDpi]::SetProcessDPIAware(); "
    "Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
    "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
    "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
    "$g = [System.Drawing.Graphics]::FromImage($bmp); "
    "try { $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
    "$bmp.Save($env:JARVIS_SHOT_PATH, [System.Drawing.Imaging.ImageFormat]::Png) } "
    "finally { $g.Dispose(); $bmp.Dispose() }"
)

# Toast XML and AppId arrive in environment variables (JARVIS_TOAST_XML / JARVIS_TOAST_APP).
TOAST_PS = (
    "$ErrorActionPreference = 'Stop'; "
    "$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]; "
    "$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]; "
    "$shown = $false; $err = $null; "
    f"foreach ($id in @($env:JARVIS_TOAST_APP, '{POWERSHELL_AUMID}')) {{ "
    "if (-not $id -or $shown) { continue }; "
    "try { $x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:JARVIS_TOAST_XML); "
    "$t = New-Object Windows.UI.Notifications.ToastNotification $x; "
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show($t); $shown = $true } "
    "catch { $err = $_ } }; "
    "if (-not $shown) { if ($err) { Write-Error $err }; exit 3 }"
)


def register_toast_app_id(app_id: str, display_name: str, icon: str | None = None) -> bool:
    """Register an AppUserModelID for toasts (HKCU\\Software\\Classes\\AppUserModelId\\<id>)."""
    try:
        import winreg
    except ImportError:
        return False

    def read(key: Any, name: str) -> str:
        try:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value or "")
        except OSError:
            return ""

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f"Software\\Classes\\AppUserModelId\\{app_id}", 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as key:
            if not read(key, "DisplayName"):
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
            if icon and not read(key, "IconUri"):
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon)
        return True
    except OSError as exc:
        log.debug("Could not register toast AppUserModelID %s: %s", app_id, exc)
        return False


def _find_app_icon() -> str | None:
    package = Path(__file__).resolve().parents[1]
    for rel in ("assets/jarvis.png", "assets/icon.png", "assets/jarvis.ico", "assets/icon.ico",
                "ui/static/favicon.png", "ui/static/icon.png"):
        candidate = package / rel
        if candidate.is_file():
            return str(candidate)
    return None


# =============================================================================================
# Processes
# =============================================================================================
def _protected_pids() -> set[int]:
    """This process and its ancestors (never closed by close_app)."""
    pids = {os.getpid()}
    with contextlib.suppress(psutil.Error):
        pids.update(p.pid for p in psutil.Process().parents())
    return pids


def _current_username() -> str:
    try:
        return (psutil.Process().username() or "").lower()
    except psutil.Error:
        return ""


def _grab_with_pillow(target: Path) -> None:
    from PIL import ImageGrab  # optional dependency

    image = ImageGrab.grab(all_screens=True)
    image.save(target, "PNG")


def _prepare_screenshot(path: Path) -> Path:
    target = screenshot_target(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _startfile(target: str, arguments: str = "", cwd: str | None = None) -> None:
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise HostUnsupported("Opening items with their default app needs Windows.")
    if arguments or cwd:
        startfile(target, arguments=arguments, cwd=cwd)
    else:
        startfile(target)


# =============================================================================================
# The adapter
# =============================================================================================
class WindowsHost(HostAdapter):
    platform: ClassVar[str] = "windows"
    toast_app_id: ClassVar[str] = "JARVIS"
    toast_display_name: ClassVar[str] = "JARVIS"
    graceful_close_timeout_s: ClassVar[float] = 3.0

    def __init__(self, *, cache_dir: Path | None = None, processes: ProcessRegistry | None = None) -> None:
        self._processes = processes
        cache_file = Path(cache_dir) / "app_index_windows.json" if cache_dir is not None else None
        self._apps = AppIndexStore(self._build_index, cache_file=cache_file, platform=self.platform)
        self._delayed = DelayedActions()
        self._launched: list[subprocess.Popen[bytes]] = []
        self._launched_lock = threading.Lock()
        self._toast_id: str | None = None

    # ------------------------------------------------------------------------------ folders
    def special_folder(self, key: str) -> Path | None:
        k = (key or "").strip().lower()
        if k in winapi.KNOWN_FOLDER_IDS:
            with contextlib.suppress(HostUnsupported, OSError, ValueError):
                return Path(winapi.known_folder_path(k))
        env = os.environ
        if k == "home":
            return Path.home()
        if k == "appdata" and env.get("APPDATA"):
            return Path(env["APPDATA"])
        if k == "localappdata" and env.get("LOCALAPPDATA"):
            return Path(env["LOCALAPPDATA"])
        if k == "startmenu" and env.get("APPDATA"):
            return Path(env["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu"
        names = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads", "pictures": "Pictures",
                 "music": "Music", "videos": "Videos"}
        if k in names:
            candidate = Path.home() / names[k]
            return candidate if candidate.is_dir() else None
        return None

    def start_menu_roots(self) -> list[Path]:
        roots: list[Path] = []
        for key in ("programs", "common_programs"):
            with contextlib.suppress(HostUnsupported, OSError, ValueError):
                roots.append(Path(winapi.known_folder_path(key)))
        for var in ("APPDATA", "ProgramData"):
            base = os.environ.get(var)
            if base:
                roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        unique: list[Path] = []
        for root in roots:
            if all(os.path.normcase(str(root)) != os.path.normcase(str(u)) for u in unique):
                unique.append(root)
        return unique

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
        """Closest installed apps for a name (for "did you mean…")."""
        index = await self._apps.get()
        return [m.app for m in index.search(name, limit)]

    async def _build_index(self) -> list[InstalledApp]:
        local, start_apps = await asyncio.gather(asyncio.to_thread(self._collect_local_apps), self._start_apps())
        return annotate_with_catalog(merge_apps(*local, start_apps))

    def _collect_local_apps(self) -> list[list[InstalledApp]]:
        groups: list[list[InstalledApp]] = []
        sources: tuple[tuple[str, Callable[[], list[InstalledApp]]], ...] = (
            ("Start menu", self._scan_start_menu),
            ("App Paths", self._read_app_paths),
            ("catalog", self._catalog_apps),
        )
        for label, collect in sources:
            try:
                groups.append(collect())
            except Exception:
                log.warning("Could not read %s apps", label, exc_info=True)
                groups.append([])
        return groups

    def _scan_start_menu(self) -> list[InstalledApp]:
        return scan_start_menu(self.start_menu_roots())

    def _read_app_paths(self) -> list[InstalledApp]:
        if not IS_WINDOWS:
            return []

        def describe(path: str) -> str | None:
            return winapi.file_description(path)

        return app_paths_to_apps(read_app_paths_registry(), describe=describe)

    def _catalog_apps(self) -> list[InstalledApp]:
        return catalog_apps_windows(uri_ok=uri_registered if IS_WINDOWS else (lambda _uri: False))

    async def _start_apps(self) -> list[InstalledApp]:
        if not IS_WINDOWS:
            return []
        result = await self._powershell(GET_START_APPS_PS, timeout=60.0, max_output=8_000_000)
        if not result.ok:
            log.info("Get-StartApps failed: %s", result.summary(300))
            return []
        return parse_start_apps(result.stdout)

    # ------------------------------------------------------------------------------ launching
    async def launch_app(self, app: InstalledApp, args: Sequence[str] = ()) -> LaunchResult:
        return await asyncio.to_thread(self._launch_sync, app, [str(a) for a in args])

    def _launch_sync(self, app: InstalledApp, args: list[str]) -> LaunchResult:
        plan = plan_launch(app, args)
        try:
            return self._execute_plan(app, plan)
        except HostUnsupported:
            raise
        except FileNotFoundError as exc:
            return LaunchResult(False, app.name, plan.method, detail=f"{app.name} was not found: {exc}")
        except OSError as exc:
            return LaunchResult(False, app.name, plan.method, detail=f"Couldn't start {app.name}: {exc}")

    def _execute_plan(self, app: InstalledApp, plan: LaunchPlan) -> LaunchResult:
        if plan.method in ("startfile", "uri"):
            _startfile(plan.target, plan.arguments, plan.cwd)
            return LaunchResult(True, app.name, plan.method, detail=plan.note)
        try:
            proc = subprocess.Popen(
                list(plan.argv), cwd=plan.cwd or None, creationflags=plan.creationflags, close_fds=True,
                env=sanitized_env(),
            )
        except OSError as exc:
            if getattr(exc, "winerror", None) == ERROR_ELEVATION_REQUIRED:
                # The exe asks for administrator rights: ShellExecute shows the UAC prompt.
                _startfile(plan.argv[0], subprocess.list2cmdline(plan.argv[1:]), plan.cwd)
                return LaunchResult(True, app.name, "startfile", detail="started with elevation (UAC prompt)")
            raise
        self._track(proc)
        if plan.method == "shell:AppsFolder":
            return LaunchResult(True, app.name, plan.method, detail=plan.note,
                                extra={"app_id": app.target, "launcher_pid": proc.pid})
        return LaunchResult(True, app.name, plan.method, pid=proc.pid, detail=plan.note)

    def _track(self, proc: subprocess.Popen[bytes]) -> None:
        with self._launched_lock:
            self._launched = [p for p in self._launched if p.poll() is None][-50:]
            self._launched.append(proc)

    async def open_url(self, url: str) -> None:
        target = validate_web_url(url)
        await asyncio.to_thread(_startfile, target)

    async def open_path(self, path: Path) -> None:
        def run() -> None:
            p = Path(path).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"No such file or folder: {p}")
            _startfile(str(p.resolve()))

        await asyncio.to_thread(run)

    async def reveal(self, path: Path) -> None:
        def run() -> None:
            p = Path(path).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"No such file or folder: {p}")
            command_line = reveal_command_line(str(p.resolve()), _explorer_exe())
            # A CreateProcess command line (no shell): explorer needs /select,"path" verbatim.
            proc = subprocess.Popen(command_line, close_fds=True, creationflags=GUI_LAUNCH_FLAGS)
            self._track(proc)

        await asyncio.to_thread(run)

    # ------------------------------------------------------------------------------ closing
    async def close_app(self, name: str, *, force: bool = False) -> list[str]:
        key = clean_query(name)
        if not key:
            return []
        entry = catalog_lookup(name)
        if (entry is not None and entry.id == "explorer") or key in ("explorer", "explorer exe", "file explorer"):
            # Closing "File Explorer" means its windows; killing explorer.exe would take the taskbar with it.
            closed = await asyncio.to_thread(winapi.close_windows, class_names=EXPLORER_WINDOW_CLASSES)
            return ["explorer.exe"] if closed else []
        app: InstalledApp | None = None
        try:
            app = (await self._apps.get()).find(name)
        except Exception:
            log.debug("App index unavailable while closing %s", name, exc_info=True)
        targets = candidate_process_names(name, app=app, entry=entry, windows=True)
        spoken = set(candidate_process_names(name, windows=True))      # what the user literally named
        blocked = [t for t in targets if t in CRITICAL_PROCESSES]
        targets = [t for t in targets if t not in CRITICAL_PROCESSES]
        if blocked and (not targets or spoken.intersection(blocked)):
            raise PermissionDenied(f"{blocked[0]} is a critical Windows process; JARVIS will not close it.")
        if not targets:
            return []
        procs = await asyncio.to_thread(self._find_processes, targets)
        if not procs:
            return []
        return await self._close_processes(procs, force=force)

    def _find_processes(self, targets: Sequence[str]) -> list[psutil.Process]:
        wanted = {t.lower() for t in targets} - CRITICAL_PROCESSES
        protected = _protected_pids()
        me = _current_username()
        found: list[psutil.Process] = []
        for proc in psutil.process_iter(["pid", "name", "username"]):
            info = proc.info
            name = (info.get("name") or "").lower()
            if name not in wanted or proc.pid in protected:
                continue
            if me and (info.get("username") or "").lower() != me:
                continue                            # other users' / SYSTEM processes are not ours to close
            found.append(proc)
        return found

    async def _close_processes(self, procs: list[psutil.Process], *, force: bool) -> list[str]:
        names = {p.pid: str(p.info.get("name") or "") for p in procs}
        await self._request_close([p.pid for p in procs])
        gone, alive = await asyncio.to_thread(psutil.wait_procs, procs, self.graceful_close_timeout_s)
        closed = list(gone)
        if alive and force:
            await asyncio.to_thread(self._kill, alive)
            gone_now, _ = await asyncio.to_thread(psutil.wait_procs, alive, 3.0)
            closed.extend(gone_now)
        return sorted({names[p.pid] for p in closed if names.get(p.pid)}, key=str.lower)

    async def _request_close(self, pids: Sequence[int]) -> None:
        """Graceful close: taskkill without /F sends WM_CLOSE to the processes' windows."""
        taskkill = _system32_exe("taskkill.exe")
        for start in range(0, len(pids), 40):
            argv = [taskkill]
            for pid in pids[start:start + 40]:
                argv += ["/PID", str(pid)]
            result = await run_process(argv, timeout=15.0, registry=self._processes, owner="host")
            if not result.ok:
                log.debug("taskkill (graceful) reported: %s", result.summary(200))

    @staticmethod
    def _kill(procs: Iterable[psutil.Process]) -> None:
        for proc in procs:
            with contextlib.suppress(psutil.Error):
                children = proc.children(recursive=True)
                for child in children:
                    with contextlib.suppress(psutil.Error):
                        child.kill()
            with contextlib.suppress(psutil.Error):
                proc.kill()

    async def running_apps(self) -> list[dict[str, Any]]:
        try:
            windows = await asyncio.to_thread(winapi.list_windows)
        except Exception as exc:
            log.debug("Window enumeration unavailable (%s); listing processes instead", exc)
            return await asyncio.to_thread(self._process_apps)
        apps = await asyncio.to_thread(self._window_apps, windows)
        # No app windows at all (service / non-interactive session): list the user's processes instead.
        return apps or await asyncio.to_thread(self._process_apps)

    @staticmethod
    def _window_apps(windows: Sequence[winapi.WindowInfo]) -> list[dict[str, Any]]:
        apps: dict[int, dict[str, Any]] = {}
        for win in windows:
            if win.pid <= 0:
                continue
            entry = apps.get(win.pid)
            if entry is None:
                try:
                    proc = psutil.Process(win.pid)
                    name = proc.name()
                    rss = proc.memory_info().rss
                except psutil.Error:
                    continue
                entry = apps[win.pid] = {"name": name, "pid": win.pid, "title": win.title,
                                         "memory_mb": round(rss / 1_048_576, 1), "windows": 0}
            entry["windows"] += 1
        return sorted(apps.values(), key=lambda e: (e["name"].lower(), e["pid"]))

    @staticmethod
    def _process_apps(limit: int = 60) -> list[dict[str, Any]]:
        me = _current_username()
        rows: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "username", "memory_info"]):
            info = proc.info
            name = str(info.get("name") or "")
            low = name.lower()
            if not name or low in CRITICAL_PROCESSES or low in _BACKGROUND_PROCESSES:
                continue
            if me and (info.get("username") or "").lower() != me:
                continue
            mem = info.get("memory_info")
            rows.append({"name": name, "pid": proc.pid, "title": None,
                         "memory_mb": round((mem.rss if mem else 0) / 1_048_576, 1)})
        rows.sort(key=lambda r: r["memory_mb"], reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------------------ audio
    async def get_volume(self) -> int | None:
        try:
            return await asyncio.to_thread(winapi.get_master_volume)
        except (HostUnsupported, OSError) as exc:
            log.debug("Core Audio volume unavailable: %s", exc)
            return None

    async def set_volume(self, level: int) -> int | None:
        level = max(0, min(100, int(level)))
        try:
            return await asyncio.to_thread(winapi.set_master_volume, level)
        except (HostUnsupported, OSError) as exc:
            log.debug("Core Audio volume unavailable (%s); using volume keys", exc)
        plan = volume_key_plan(level=level)
        await asyncio.to_thread(winapi.press_keys, plan)
        return min(100, sum(n for vk, n in plan if vk == winapi.VK_VOLUME_UP) * VOLUME_KEY_STEP)

    async def change_volume(self, delta: int) -> int | None:
        delta = int(delta)
        current = await self.get_volume()
        if current is not None:
            with contextlib.suppress(HostUnsupported, OSError):
                return await asyncio.to_thread(winapi.set_master_volume, max(0, min(100, current + delta)))
        if delta:
            await asyncio.to_thread(winapi.press_keys, volume_key_plan(delta=delta))
        return None

    async def set_mute(self, mute: bool) -> None:
        try:
            await asyncio.to_thread(winapi.set_master_mute, bool(mute))
            return
        except (HostUnsupported, OSError) as exc:
            log.debug("Core Audio mute unavailable (%s); using volume keys", exc)
        await asyncio.to_thread(winapi.press_keys, mute_key_plan(bool(mute)))

    # ------------------------------------------------------------------------------ display
    async def get_brightness(self) -> int | None:
        result = await self._powershell(BRIGHTNESS_GET_PS, timeout=20.0)
        if not result.ok:
            raise HostUnsupported(self._brightness_message(result))
        match = re.search(r"-?\d+", result.stdout or "")
        if match is None:
            raise HostUnsupported(self._brightness_message(result))
        return max(0, min(100, int(match.group(0))))

    async def set_brightness(self, level: int) -> int | None:
        level = max(0, min(100, int(level)))
        result = await self._powershell(brightness_set_ps(level), timeout=20.0)
        if not result.ok:
            raise HostUnsupported(self._brightness_message(result))
        return level

    @staticmethod
    def _brightness_message(result: ProcessResult) -> str:
        return ("This display doesn't support brightness control from Windows (external monitors usually "
                "don't — use the monitor's own buttons). " + f"[{result.summary(160)}]")

    async def media(self, action: MediaAction) -> None:
        vk = MEDIA_KEYS.get(action)
        if vk is None:
            raise HostUnsupported(f"Unknown media action: {action!r}")
        await asyncio.to_thread(winapi.press_keys, [(vk, 1)])

    # ------------------------------------------------------------------------------ session / power
    async def lock_screen(self) -> None:
        await asyncio.to_thread(winapi.lock_workstation)

    async def power(self, action: PowerAction, delay_s: int = 0) -> None:
        if action not in ("shutdown", "restart", "sleep", "logoff"):
            raise HostUnsupported(f"Unknown power action: {action!r}")
        delay = max(0, int(delay_s))
        if action in ("sleep", "logoff") and delay > 0:
            # shutdown.exe has no timer for these: JARVIS waits (cancel_power stops it).
            self._delayed.schedule(delay, lambda: self.power(action, 0), label=action)
            return
        if action == "sleep":
            await asyncio.to_thread(winapi.suspend)
            return
        argv = power_argv(action, delay, shutdown_exe=await asyncio.to_thread(_system32_exe, "shutdown.exe"))
        result = await run_process(argv, timeout=30.0, registry=self._processes, owner="host")
        if not result.ok:
            raise HostUnsupported(f"Windows refused the {action} request: {result.summary(300)}")

    async def cancel_power(self) -> None:
        self._delayed.cancel_all()
        argv = [await asyncio.to_thread(_system32_exe, "shutdown.exe"), "/a"]
        result = await run_process(argv, timeout=15.0, registry=self._processes, owner="host")
        if not result.ok and result.exit_code != SHUTDOWN_NOTHING_TO_ABORT:
            log.info("shutdown /a: %s", result.summary(200))

    # ------------------------------------------------------------------------------ misc
    async def screenshot(self, path: Path) -> Path:
        target = await asyncio.to_thread(_prepare_screenshot, Path(path))
        try:
            await asyncio.to_thread(_grab_with_pillow, target)
            return target
        except ImportError:
            pass
        except Exception as exc:
            log.info("Pillow screenshot failed (%s); trying PowerShell", exc)
        result = await self._powershell(SCREENSHOT_PS, timeout=60.0, extra_env={"JARVIS_SHOT_PATH": str(target)})
        if result.ok and await asyncio.to_thread(target.is_file):
            return target
        raise HostUnsupported(f"Couldn't capture the screen: {result.summary(300)}")

    async def notify(self, title: str, message: str) -> None:
        app_id = await asyncio.to_thread(self._toast_app_id)
        env = {"JARVIS_TOAST_XML": toast_xml(title, message), "JARVIS_TOAST_APP": app_id}
        result = await self._powershell(TOAST_PS, timeout=30.0, extra_env=env)
        if not result.ok:
            raise HostUnsupported(f"Couldn't show a Windows notification: {result.summary(300)}")

    def _toast_app_id(self) -> str:
        """"JARVIS" once registered as an AppUserModelID; empty → the script uses PowerShell's id."""
        if self._toast_id is None:
            ok = IS_WINDOWS and register_toast_app_id(self.toast_app_id, self.toast_display_name, _find_app_icon())
            self._toast_id = self.toast_app_id if ok else ""
        return self._toast_id

    async def get_clipboard(self) -> str:
        return await asyncio.to_thread(winapi.get_clipboard_text)

    async def set_clipboard(self, text: str) -> None:
        await asyncio.to_thread(winapi.set_clipboard_text, text)

    def shell_argv(self, command: str, shell: str | None = None) -> list[str]:
        kind = (shell or self.default_shell).strip().lower()
        if kind in ("powershell", "powershell.exe", "windows powershell", "ps"):
            return powershell_argv(command)
        if kind in ("pwsh", "pwsh.exe", "powershell7", "powershell 7"):
            pwsh = shutil.which("pwsh")
            if pwsh is None:
                log.info("PowerShell 7 (pwsh) isn't installed; using Windows PowerShell")
            return powershell_argv(command, exe=pwsh or "powershell.exe")
        if kind in ("cmd", "cmd.exe"):
            return ["cmd.exe", "/d", "/s", "/c", command]
        if kind in ("bash", "sh", "git-bash"):
            exe = shutil.which("bash")
            if exe is None:
                raise HostUnsupported("bash isn't installed (install Git for Windows or WSL).")
            return [exe, "-lc", command]
        raise HostUnsupported(f"Unknown shell: {shell!r} (use powershell, pwsh or cmd).")

    @property
    def default_shell(self) -> str:
        return "powershell"

    # ------------------------------------------------------------------------------ helpers
    async def _powershell(
        self,
        script: str,
        *,
        timeout: float = 30.0,
        extra_env: dict[str, str] | None = None,
        max_output: int = 200_000,
    ) -> ProcessResult:
        exe = await asyncio.to_thread(_powershell_exe)
        return await run_process(powershell_argv(script, exe=exe), timeout=timeout, extra_env=extra_env,
                                 max_output=max_output, registry=self._processes, owner="host")
