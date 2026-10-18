"""Screen, windows and input as an interface, so the session and the safety rules run (and are tested) anywhere.

`get_backend()` returns the Win32 implementation on Windows (`jarvis.computer.windows`) and, elsewhere, one
that says computer control is not available. Coordinates are physical pixels of the primary monitor.
"""

from __future__ import annotations

import re
import sys
import threading
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from jarvis.host.base import HostUnsupported


@dataclass(frozen=True)
class Shot:
    """One look at the screen: a PNG of the primary monitor (scaled down when it is very large)."""

    png: bytes
    width: int             # the image
    height: int
    screen_width: int      # the monitor in physical pixels: what clicks are aimed at
    screen_height: int


@dataclass(frozen=True)
class Target:
    """What an action would reach: the top-level window, and the element there when UI Automation tells."""

    hwnd: int = 0
    pid: int = 0
    process: str = ""          # program file name in lower case ("notepad.exe")
    title: str = ""
    class_name: str = ""
    element: str = ""          # the element's accessible name ("Send"); "" when unknown
    control: str = ""          # its kind ("button", "edit", "list item"…); "" when unknown
    password: bool = False     # a password field
    own: bool = False          # one of JARVIS's own windows

    def same_window(self, other: Target) -> bool:
        return self.hwnd == other.hwnd and self.pid == other.pid


class EscapeWatch(Protocol):
    def stop(self) -> None: ...


class ComputerBackend(Protocol):
    def problem(self) -> str | None:
        """None when the screen and input can be used here, else why not (English)."""

    def screen_size(self) -> tuple[int, int]: ...
    def capture(self, max_width: int) -> Shot: ...
    def target_at(self, x: int, y: int) -> Target: ...
    def focus_target(self) -> Target: ...

    def input_state(self) -> tuple[int, int, int]:
        """(last input time, cursor x, cursor y): when it changes between actions, a person used the computer."""

    def own_windows_visible(self) -> bool: ...
    def move(self, x: int, y: int) -> None: ...
    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> None: ...
    def scroll(self, x: int | None, y: int | None, direction: str, amount: int) -> None: ...
    def drag(self, x: int, y: int, to_x: int, to_y: int) -> None: ...

    def type_text(self, text: str, stop: threading.Event | None = None) -> bool:
        """Type `text` (any language: sent as Unicode, whatever the keyboard layout; a newline presses Enter).
        False when it stopped early: `stop` was set, or the focus moved to another window, which then gets
        none of the rest."""

    def press_keys(self, combos: Sequence[Sequence[str]]) -> bool:
        """Press key combinations one after the other: [["ctrl", "s"], ["enter"]]. False when it stopped early
        because the focus moved to another window (an earlier combination or the user switched windows)."""

    def watch_escape(self, on_double: Callable[[], None]) -> EscapeWatch:
        """Call `on_double` (from another thread) when the user presses Esc twice in a row."""


# ----------------------------------------------------------------------------------------------- keys
KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "lctrl": "ctrl", "rctrl": "ctrl", "option": "alt", "menu": "alt",
    "cmd": "win", "command": "win", "super": "win", "meta": "win", "windows": "win", "start": "win",
    "return": "enter", "escape": "esc", "del": "delete", "ins": "insert", "bksp": "backspace", "back": "backspace",
    "pgup": "pageup", "page_up": "pageup", "page up": "pageup", "prior": "pageup", "pgdn": "pagedown",
    "page_down": "pagedown", "page down": "pagedown", "next": "pagedown", "arrowleft": "left", "arrowright": "right",
    "arrowup": "up", "arrowdown": "down", "spacebar": "space", "apps": "contextmenu", "prtsc": "printscreen",
    "print": "printscreen", "capslock": "capslock", "caps": "capslock", "plus": "+", "minus": "-",
}
MODIFIERS = ("ctrl", "shift", "alt", "win")
NAMED_KEYS = frozenset({
    "enter", "tab", "esc", "space", "backspace", "delete", "insert", "home", "end", "pageup", "pagedown", "left",
    "right", "up", "down", "capslock", "printscreen", "contextmenu", "numlock", "scrolllock", "pause",
    *MODIFIERS, *(f"f{n}" for n in range(1, 25)),
})
PUNCTUATION = frozenset(";=,-./`[\\]'+")
_SEP = re.compile(r"\s*,\s*|\s+")
MAX_COMBOS = 12


def parse_keys(text: str) -> list[list[str]]:
    """'ctrl+shift+s' → [['ctrl', 'shift', 's']]; 'down down enter' → [['down'], ['down'], ['enter']].

    Raises ValueError for a key JARVIS does not know, so nothing unexpected is ever pressed."""
    raw = (text or "").strip().lower()
    for spaced in ("page up", "page down"):
        raw = raw.replace(spaced, spaced.replace(" ", "_"))
    combos: list[list[str]] = []
    for chunk in (c for c in _SEP.split(raw) if c):
        # "ctrl+s" → ctrl, s; a "+" at the very end is the plus key itself ("ctrl++").
        parts = ["+"] if chunk == "+" else [p for p in re.split(r"\+(?!$)", chunk) if p]
        keys: list[str] = []
        for part in parts:
            key = KEY_ALIASES.get(part, part)
            if not (key in NAMED_KEYS or (len(key) == 1 and ((key.isalnum() and key.isascii()) or key in PUNCTUATION))):
                raise ValueError(f"unknown key: {part}")
            if key not in keys:
                keys.append(key)
        mods = [k for k in MODIFIERS if k in keys]
        combos.append([*mods, *(k for k in keys if k not in MODIFIERS)])
    if not combos:
        raise ValueError("no keys given")
    if len(combos) > MAX_COMBOS:
        raise ValueError(f"at most {MAX_COMBOS} key presses at a time")
    return combos


def combo_text(combo: Sequence[str]) -> str:
    return "+".join(combo)


# ----------------------------------------------------------------------------------------------- text
def _hebrew_point(ch: str) -> bool:
    return "\u0591" <= ch <= "\u05c7" and unicodedata.category(ch) == "Mn"


def strip_marks(text: str) -> str:
    """`text` as a person reads it, so the safety rules match what is on the screen: no invisible formatting
    (Hebrew Windows wraps window titles in bidi marks, U+202A…U+202C; a page can hide zero-width characters
    inside a word), no Hebrew vowel points, compatibility forms folded (NFKC). Line breaks are kept."""
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf" and not _hebrew_point(ch))


def plain(text: str) -> str:
    """`strip_marks` with the whitespace collapsed: for window titles and element names."""
    return " ".join(strip_marks(text).split())


# ----------------------------------------------------------------------------------------------- elsewhere
class UnsupportedBackend:
    """Computer control needs a Windows desktop session."""

    def __init__(self, reason: str = "Computer control works on Windows only.") -> None:
        self.reason = reason

    def problem(self) -> str | None:
        return self.reason

    def _no(self, *_: object, **__: object) -> None:
        raise HostUnsupported(self.reason)

    screen_size = capture = target_at = focus_target = input_state = _no  # type: ignore[assignment]
    move = click = scroll = drag = type_text = press_keys = watch_escape = _no  # type: ignore[assignment]

    def own_windows_visible(self) -> bool:
        return False


def get_backend() -> ComputerBackend:
    if sys.platform == "win32":
        from jarvis.computer.windows import WindowsBackend

        return WindowsBackend()
    return UnsupportedBackend()  # type: ignore[return-value]


__all__ = ["ComputerBackend", "EscapeWatch", "Shot", "Target", "UnsupportedBackend", "combo_text", "get_backend",
           "parse_keys"]
