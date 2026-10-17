"""Start JARVIS in the background when the user signs in to Windows.

JARVIS keeps its own entry under HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run (for this user
only, no administrator rights). The windowed app writes or removes it to match the
`general.start_with_windows` setting, at start and when the setting changes; a JARVIS started for
tests, `jarvis check` or `--no-window` leaves it alone. The entry starts JARVIS with `--background`:
no main window, only the mini orb, listening for the wake word.

When the user turned JARVIS off in Task Manager's "Startup apps", Windows keeps the entry but does
not run it; `status()` reports that, and JARVIS never overrides it.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "JARVIS"
BACKGROUND_FLAG = "--background"


def supported() -> bool:
    return sys.platform == "win32"


def launch_argv() -> list[str]:
    """How this JARVIS is started at sign-in: the installed JARVIS.exe, or pythonw -m jarvis from its venv."""
    if getattr(sys, "frozen", False):
        return [sys.executable, BACKGROUND_FLAG]
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else exe), "-m", "jarvis", BACKGROUND_FLAG]


def command_line() -> str:
    return subprocess.list2cmdline(launch_argv())


def _winreg() -> Any:
    import winreg

    return winreg


def read() -> str | None:
    """The command Windows runs for JARVIS at sign-in, None when there is no entry."""
    if not supported():
        return None
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:                                   # FileNotFoundError: no entry
        return None
    return str(value) if value else None


def disabled_in_task_manager() -> bool:
    """Task Manager's "Startup apps" switch: an odd first byte in StartupApproved means turned off."""
    if not supported():
        return False
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return isinstance(value, bytes | bytearray) and len(value) > 0 and value[0] % 2 == 1


def apply(enabled: bool) -> str:
    """Make the entry match `enabled`: "added", "updated", "removed", "unchanged" or "unsupported"."""
    if not supported():
        return "unsupported"
    winreg = _winreg()
    current = read()
    if enabled:
        wanted = command_line()
        if current == wanted:
            return "unchanged"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, wanted)
        log.info("JARVIS starts with Windows: %s", wanted)
        return "updated" if current else "added"
    if current is None:
        return "unchanged"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.DeleteValue(key, VALUE_NAME)
    log.info("JARVIS no longer starts with Windows")
    return "removed"


def status() -> dict[str, Any]:
    current = read()
    return {
        "supported": supported(),
        "registered": current is not None,
        "command": current,
        "this_jarvis": current == command_line() if current else False,
        "disabled_in_task_manager": disabled_in_task_manager(),
    }
