"""Ctrl to talk: a short press of Ctrl on its own, anywhere in Windows, starts listening for one command.

The keyboard is polled with GetAsyncKeyState on a thread, like computer control's Esc watch (Windows can silently
unhook a low-level keyboard hook; a poll keeps working). A press counts only when it was a tap of Ctrl and nothing
else:

* no other key or mouse button was down while Ctrl was (Ctrl+C, Ctrl+click and Shift+Ctrl do nothing, nor does
  Ctrl while a key is held in a game);
* no other input arrived without the mouse moving, which is how a wheel turn shows (Ctrl+wheel zooms);
* it was let go within `MAX_HOLD_S`, and before Windows would start repeating the key;
* JARVIS did not press it itself (computer control marks its own keys with `note_own_keys`).

Polling costs one GetAsyncKeyState call every 20 ms while Ctrl is up; the other keys are read only while it is down.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

VK_CONTROL, VK_LCONTROL, VK_RCONTROL = 0x11, 0xA2, 0xA3
OTHER_KEYS = tuple(vk for vk in range(0x01, 0xFF) if vk not in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL))
SPI_GETKEYBOARDDELAY = 0x0016
MAX_HOLD_S = 0.5
POLL_S = 0.02


class KeyReader(Protocol):
    def ctrl_down(self) -> bool: ...

    def others_down(self) -> bool: ...

    def last_input(self) -> int: ...

    def cursor(self) -> tuple[int, int]: ...


@dataclass
class _Press:
    started: float
    tick: int
    cursor: tuple[int, int]
    spoiled: bool


class TapDetector:
    """The decision without Windows: `feed` what the keyboard looked like at each poll; True means a tap ended."""

    def __init__(self, *, max_hold_s: float = MAX_HOLD_S) -> None:
        self.max_hold_s = max_hold_s
        self._press: _Press | None = None
        self._held_at_start = True             # a Ctrl already down when watching starts is not a tap

    @property
    def needs_details(self) -> bool:
        """Whether the other keys, the input clock and the cursor matter now (not once a press is spoiled)."""
        return not self._held_at_start and (self._press is None or not self._press.spoiled)

    def feed(self, now: float, ctrl: bool, others: bool = False, tick: int = 0, cursor: tuple[int, int] = (0, 0),
             own: bool = False) -> bool:
        press = self._press
        if not ctrl:
            self._held_at_start = False
            self._press = None
            return press is not None and not press.spoiled and not own and now - press.started <= self.max_hold_s
        if press is None:
            if not self._held_at_start:
                self._press = _Press(now, tick, cursor, spoiled=others or own)
            return False
        if others or own or now - press.started > self.max_hold_s:
            press.spoiled = True
        elif cursor != press.cursor:            # the mouse moved: that input is fine
            press.tick, press.cursor = tick, cursor
        elif tick != press.tick:                # input with no key down and the mouse still: the wheel
            press.spoiled = True
        return False


class CtrlTapWatch:
    """Calls `on_tap` on its own thread for each tap of Ctrl alone, until `stop()`."""

    def __init__(self, on_tap: Callable[[], None], keys: KeyReader, *, max_hold_s: float = MAX_HOLD_S,
                 poll_s: float = POLL_S, own: Callable[[], bool] = lambda: False) -> None:
        self.on_tap, self.keys, self.poll_s, self.own = on_tap, keys, poll_s, own
        self.detector = TapDetector(max_hold_s=max_hold_s)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jarvis-ctrl-watch", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        keys, detector = self.keys, self.detector
        failing = False
        while not self._stop.wait(self.poll_s):
            try:
                ctrl = keys.ctrl_down()
                if ctrl and detector.needs_details:
                    tapped = detector.feed(time.monotonic(), True, keys.others_down(), keys.last_input(),
                                           keys.cursor(), self.own())
                else:
                    tapped = detector.feed(time.monotonic(), ctrl, own=self.own())
            except Exception as exc:
                if not failing:
                    log.warning("Reading the keyboard for Ctrl to talk failed: %s", exc)
                failing = True
                continue
            failing = False
            if tapped:
                try:
                    self.on_tap()
                except Exception:
                    log.exception("The Ctrl-to-talk handler failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(1.0)


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


class _WindowsKeys:
    def __init__(self) -> None:
        from ctypes import wintypes as w

        from jarvis.host.winapi import _proto

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._key = _proto(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
        self._last = _proto(user32.GetLastInputInfo, w.BOOL, ctypes.POINTER(_LastInput))
        self._pos = _proto(user32.GetCursorPos, w.BOOL, ctypes.POINTER(w.POINT))
        self._spi = _proto(user32.SystemParametersInfoW, w.BOOL, w.UINT, w.UINT, ctypes.c_void_p, w.UINT)
        self._info = _LastInput(ctypes.sizeof(_LastInput), 0)
        self._point = w.POINT()

    def ctrl_down(self) -> bool:
        return bool(self._key(VK_CONTROL) & 0x8000)

    def others_down(self) -> bool:
        key = self._key
        return any(key(vk) & 0x8000 for vk in OTHER_KEYS)

    def last_input(self) -> int:
        self._last(ctypes.byref(self._info))
        return int(self._info.dwTime)

    def cursor(self) -> tuple[int, int]:
        self._pos(ctypes.byref(self._point))
        return int(self._point.x), int(self._point.y)

    def repeat_delay_s(self) -> float:
        """How long a key is held before Windows repeats it (Control Panel > Keyboard: 250 ms to 1 s)."""
        value = ctypes.c_int(1)
        if not self._spi(SPI_GETKEYBOARDDELAY, 0, ctypes.byref(value), 0):
            return 0.5
        return 0.25 * (min(3, max(0, value.value)) + 1)


def watch_ctrl_taps(on_tap: Callable[[], None]) -> CtrlTapWatch | None:
    """Start watching (Windows only; None elsewhere, or when the keyboard cannot be read)."""
    if sys.platform != "win32":
        return None
    try:
        keys = _WindowsKeys()
        max_hold = min(MAX_HOLD_S, keys.repeat_delay_s())
    except (OSError, AttributeError) as exc:
        log.warning("Ctrl to talk is not available: %s", exc)
        return None
    from jarvis.host.winapi import own_keys_recent

    return CtrlTapWatch(on_tap, keys, max_hold_s=max_hold, own=own_keys_recent)


__all__ = ["MAX_HOLD_S", "CtrlTapWatch", "KeyReader", "TapDetector", "watch_ctrl_taps"]
