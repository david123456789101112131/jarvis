"""The process's console. `pythonw.exe` and the windowed JARVIS.exe start without one: there
`sys.stdout` and `sys.stderr` are None, and a library that writes to them or asks `isatty()`
(uvicorn's log formatter does) would crash JARVIS before its window appears.
"""

from __future__ import annotations

import contextlib
import os
import sys

_no_console = False


def ensure_std_streams() -> bool:
    """Give a missing stdout/stderr a sink (the logs still go to the log file); True without a console."""
    global _no_console
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))     # open for the life of the process
            _no_console = True
    return _no_console


def has_console() -> bool:
    return not _no_console


def show_error(message: str, title: str = "JARVIS") -> None:
    """An error the user has to see: a message box when nobody can read the console (Windows), else stderr."""
    if not has_console() and sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            mb_iconerror, mb_topmost, mb_right, mb_rtlreading = 0x10, 0x40000, 0x80000, 0x100000
            ctypes.windll.user32.MessageBoxW(None, message, title,
                                             mb_iconerror | mb_topmost | mb_right | mb_rtlreading)
            return
    print(f"{title}: {message}", file=sys.stderr)
