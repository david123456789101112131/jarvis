"""The computer-control backend on Windows, with ctypes only.

* the screen: Pillow's ImageGrab of the primary monitor, in physical pixels;
* what is where: WindowFromPoint / the foreground window, and UI Automation for the element itself (its name,
  its kind, whether it is a password field), asked with a short time limit so a hung program cannot stall JARVIS;
* input: SendInput with normalized absolute coordinates on the primary monitor (they do not depend on DPI
  scaling), text as Unicode key events (any language, whatever the keyboard layout);
* "a person is here": GetLastInputInfo and the cursor position; Esc twice, watched with GetAsyncKeyState
  (a polling thread Windows cannot silently unhook, unlike a low-level keyboard hook);
* the user's window: the frontmost one Alt+Tab would offer that is not JARVIS's (EnumWindows goes front to
  back), put in front again with SetForegroundWindow when a task starts.

Programs running as administrator do not take input from JARVIS (Windows' UIPI), and the UAC prompt is on a
desktop JARVIS cannot reach at all.
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from ctypes import wintypes as w
from typing import Any

import psutil

from jarvis.computer.backend import Shot, Target, plain
from jarvis.computer.policy import is_jarvis_title
from jarvis.host.winapi import (
    COINIT_MULTITHREADED,
    GUID,
    INPUT,
    INPUT_KEYBOARD,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    S_FALSE,
    S_OK,
    _com_method,
    _com_release,
    _proto,
)

log = logging.getLogger(__name__)

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_UNICODE = 0x0004
WHEEL_DELTA = 120
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
DWMWA_CLOAKED = 14
SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                           "Windows.UI.Core.CoreWindow"})        # the desktop, the taskbars, Start and search
SM_CXSCREEN, SM_CYSCREEN = 0, 1
MAPVK_VK_TO_VSC = 0
VK_ESCAPE = 0x1B
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
CLSCTX_INPROC_SERVER = 0x1

CLSID_CUIAUTOMATION = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
IID_IUIAUTOMATION = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"
# vtable slots (IUnknown takes 0-2), in the order of UIAutomationClient.h
_UIA_ELEMENT_FROM_POINT = 7          # IUIAutomation::ElementFromPoint(POINT, IUIAutomationElement**)
_UIA_GET_FOCUSED_ELEMENT = 8         # IUIAutomation::GetFocusedElement(IUIAutomationElement**)
_EL_CONTROL_TYPE = 21                # IUIAutomationElement::get_CurrentControlType(CONTROLTYPEID*)
_EL_NAME = 23                        # IUIAutomationElement::get_CurrentName(BSTR*)
_EL_IS_PASSWORD = 35                 # IUIAutomationElement::get_CurrentIsPassword(BOOL*)
UIA_TIMEOUT_S = 1.5

CONTROL_TYPES = {
    50000: "button", 50002: "check box", 50003: "combo box", 50004: "edit", 50005: "link", 50006: "image",
    50007: "list item", 50008: "list", 50009: "menu", 50010: "menu bar", 50011: "menu item", 50013: "radio button",
    50014: "scroll bar", 50015: "slider", 50018: "tab", 50019: "tab item", 50020: "text", 50021: "tool bar",
    50023: "tree", 50024: "tree item", 50026: "group", 50028: "data grid", 50029: "data item", 50030: "document",
    50031: "split button", 50032: "window", 50033: "pane", 50037: "title bar",
}

# key name → (virtual key, extended)
VK: dict[str, tuple[int, bool]] = {
    "ctrl": (0x11, False), "shift": (0x10, False), "alt": (0x12, False), "win": (0x5B, True),
    "enter": (0x0D, False), "tab": (0x09, False), "esc": (0x1B, False), "space": (0x20, False),
    "backspace": (0x08, False), "delete": (0x2E, True), "insert": (0x2D, True), "home": (0x24, True),
    "end": (0x23, True), "pageup": (0x21, True), "pagedown": (0x22, True), "left": (0x25, True), "up": (0x26, True),
    "right": (0x27, True), "down": (0x28, True), "capslock": (0x14, False), "printscreen": (0x2C, True),
    "contextmenu": (0x5D, True), "numlock": (0x90, True), "scrolllock": (0x91, False), "pause": (0x13, False),
    ";": (0xBA, False), "=": (0xBB, False), "+": (0xBB, False), ",": (0xBC, False), "-": (0xBD, False),
    ".": (0xBE, False), "/": (0xBF, False), "`": (0xC0, False), "[": (0xDB, False), "\\": (0xDC, False),
    "]": (0xDD, False), "'": (0xDE, False),
    **{f"f{n}": (0x6F + n, False) for n in range(1, 25)},
    **{chr(c): (ord(chr(c).upper()), False) for c in range(ord("a"), ord("z") + 1)},
    **{str(d): (0x30 + d, False) for d in range(10)},
}


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


class _Api:
    def __init__(self) -> None:
        dll = ctypes.WinDLL  # type: ignore[attr-defined]
        self.user32 = u = dll("user32", use_last_error=True)
        self.kernel32 = k = dll("kernel32", use_last_error=True)
        self.ole32 = o = dll("ole32", use_last_error=True)
        self.oleaut32 = dll("oleaut32", use_last_error=True)
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)  # type: ignore[attr-defined]
        _proto(u.SendInput, w.UINT, w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        _proto(u.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
        _proto(u.WindowFromPoint, w.HWND, w.POINT)
        _proto(u.GetAncestor, w.HWND, w.HWND, w.UINT)
        _proto(u.GetForegroundWindow, w.HWND)
        _proto(u.GetCursorPos, w.BOOL, ctypes.POINTER(w.POINT))
        _proto(u.GetLastInputInfo, w.BOOL, ctypes.POINTER(LASTINPUTINFO))
        _proto(u.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
        _proto(u.MapVirtualKeyW, w.UINT, w.UINT, w.UINT)
        _proto(u.GetWindowThreadProcessId, w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
        _proto(u.GetWindowTextLengthW, ctypes.c_int, w.HWND)
        _proto(u.GetWindowTextW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        _proto(u.GetClassNameW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        _proto(u.IsWindowVisible, w.BOOL, w.HWND)
        _proto(u.IsIconic, w.BOOL, w.HWND)
        _proto(u.EnumWindows, w.BOOL, self.WNDENUMPROC, w.LPARAM)
        _proto(u.EnumChildWindows, w.BOOL, w.HWND, self.WNDENUMPROC, w.LPARAM)
        _proto(u.IsWindow, w.BOOL, w.HWND)
        _proto(u.GetWindowLongW, w.LONG, w.HWND, ctypes.c_int)
        _proto(u.SetForegroundWindow, w.BOOL, w.HWND)
        _proto(u.BringWindowToTop, w.BOOL, w.HWND)
        _proto(u.AttachThreadInput, w.BOOL, w.DWORD, w.DWORD, w.BOOL)
        _proto(k.GetCurrentThreadId, w.DWORD)
        try:
            self.dwm_attribute: Any = dll("dwmapi").DwmGetWindowAttribute
            _proto(self.dwm_attribute, ctypes.c_long, w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD)
        except (OSError, AttributeError):
            self.dwm_attribute = None
        self.set_dpi_context = getattr(u, "SetThreadDpiAwarenessContext", None)   # Windows 10 1607 and later
        if self.set_dpi_context is not None:
            _proto(self.set_dpi_context, ctypes.c_void_p, ctypes.c_void_p)
        _proto(o.CoInitializeEx, ctypes.c_long, ctypes.c_void_p, w.DWORD)
        _proto(o.CoUninitialize, None)
        _proto(o.CoCreateInstance, ctypes.c_long, ctypes.POINTER(GUID), ctypes.c_void_p, w.DWORD,
               ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        _proto(self.oleaut32.SysFreeString, None, ctypes.c_void_p)


_API: _Api | None = None
_API_LOCK = threading.Lock()


def _api() -> _Api:
    global _API
    if _API is None:
        with _API_LOCK:
            if _API is None:
                _API = _Api()
    return _API


@contextlib.contextmanager
def _physical(api: _Api) -> Iterator[None]:
    """This thread works in physical pixels (per-monitor DPI awareness), whatever the rest of the process uses."""
    set_context = api.set_dpi_context
    previous = None
    if set_context is not None:
        previous = set_context(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    try:
        yield
    finally:
        if set_context is not None and previous:
            set_context(ctypes.c_void_p(previous))


def _text(api: _Api, hwnd: int) -> str:
    length = api.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    api.user32.GetWindowTextW(hwnd, buf, length + 1)
    return plain(buf.value)          # Hebrew Windows wraps titles in bidi marks


def _class(api: _Api, hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    api.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(api: _Api, hwnd: int) -> int:
    pid = w.DWORD(0)
    api.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _app_pid(api: _Api, hwnd: int, pid: int) -> int:
    """Store apps draw inside ApplicationFrameHost; their own process owns a child window."""
    found = [pid]

    def visit(child: int | None, _lparam: int) -> bool:
        other = _pid(api, child or 0)
        if other and other != pid:
            found[0] = other
            return False
        return True

    api.user32.EnumChildWindows(hwnd, api.WNDENUMPROC(visit), 0)
    return found[0]


def _cloaked(api: _Api, hwnd: int) -> bool:
    """Shown as far as the window APIs tell, but not drawn: on another virtual desktop, or a suspended Store app."""
    get = api.dwm_attribute
    if get is None:
        return False
    value = w.DWORD(0)
    return get(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)) == S_OK and value.value != 0


def _switchable(api: _Api, hwnd: int) -> bool:
    """A window Alt+Tab would offer: on screen, not minimized, with a title; no tool palette or shell surface."""
    u = api.user32
    if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd) or u.GetWindowTextLengthW(hwnd) <= 0:
        return False
    if u.GetWindowLongW(hwnd, GWL_EXSTYLE) & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
        return False
    return _class(api, hwnd) not in SHELL_CLASSES and not _cloaked(api, hwnd)


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.Error, OSError, ValueError):
        return ""


def _uia(api: _Api, point: tuple[int, int] | None) -> tuple[str, str, bool]:
    """(name, kind, is password) of the element at `point` (None: the one with the keyboard focus)."""
    hr = api.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    initialized = hr in (S_OK, S_FALSE)
    automation, element, name = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        clsid, iid = GUID.from_string(CLSID_CUIAUTOMATION), GUID.from_string(IID_IUIAUTOMATION)
        if api.ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER, ctypes.byref(iid),
                                      ctypes.byref(automation)) < 0 or not automation.value:
            return "", "", False
        if point is None:
            get = _com_method(automation, _UIA_GET_FOCUSED_ELEMENT, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
            hr = get(automation, ctypes.byref(element))
        else:
            get = _com_method(automation, _UIA_ELEMENT_FROM_POINT, ctypes.c_long, w.POINT,
                              ctypes.POINTER(ctypes.c_void_p))
            hr = get(automation, w.POINT(*point), ctypes.byref(element))
        if hr < 0 or not element.value:
            return "", "", False
        text = ""
        if _com_method(element, _EL_NAME, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                element, ctypes.byref(name)) >= 0 and name.value:
            text = ctypes.wstring_at(name.value)
        kind, secret = ctypes.c_int(0), ctypes.c_int(0)
        _com_method(element, _EL_CONTROL_TYPE, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(element, ctypes.byref(kind))
        _com_method(element, _EL_IS_PASSWORD, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(element,
                                                                                           ctypes.byref(secret))
        return plain(text)[:200], CONTROL_TYPES.get(kind.value, ""), bool(secret.value)
    finally:
        if name.value:
            api.oleaut32.SysFreeString(name)
        for obj in (element, automation):
            with contextlib.suppress(Exception):
                _com_release(obj)
        if initialized:
            api.ole32.CoUninitialize()


def _uia_bounded(api: _Api, point: tuple[int, int] | None) -> tuple[str, str, bool]:
    """UI Automation asks the program itself; a hung program must not hang the check (then: unknown)."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            with _physical(api):
                box["info"] = _uia(api, point)
        except Exception as exc:              # unknown element: the window rules still apply
            log.debug("UI Automation failed: %s", exc)

    thread = threading.Thread(target=work, name="jarvis-uia", daemon=True)
    thread.start()
    thread.join(UIA_TIMEOUT_S)
    return box.get("info") or ("", "", False)


class _EscapeWatch:
    """Esc twice within `window_s` calls `on_double`. Esc presses JARVIS itself sends are not counted."""

    def __init__(self, backend: WindowsBackend, on_double: Callable[[], None], *, window_s: float = 0.8,
                 poll_s: float = 0.015) -> None:
        self.backend, self.on_double, self.window_s, self.poll_s = backend, on_double, window_s, poll_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jarvis-esc-watch", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        api = _api()
        was_down = True                       # an Esc already held when watching starts is not a press
        last = 0.0
        while not self._stop.wait(self.poll_s):
            down = bool(api.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
            if down and not was_down and time.monotonic() > self.backend.own_escape_until:
                now = time.monotonic()
                if now - last <= self.window_s:
                    last = 0.0
                    try:
                        self.on_double()
                    except Exception:
                        log.exception("The Esc-twice handler failed")
                else:
                    last = now
            was_down = down

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(1.0)


class WindowsBackend:
    def __init__(self) -> None:
        self.own_escape_until = 0.0          # Esc presses JARVIS sends itself, not the user's emergency stop

    def problem(self) -> str | None:
        try:
            _api()
        except OSError as exc:
            return f"The Windows input APIs are not available: {exc}"
        try:
            import PIL.ImageGrab  # noqa: F401
        except ImportError:
            return "Pillow is not installed (pip install pillow)."
        width, height = self.screen_size()
        if width <= 0 or height <= 0:
            return "No screen is attached to this Windows session."
        return None

    # ------------------------------------------------------------------ looking
    def screen_size(self) -> tuple[int, int]:
        api = _api()
        with _physical(api):
            return int(api.user32.GetSystemMetrics(SM_CXSCREEN)), int(api.user32.GetSystemMetrics(SM_CYSCREEN))

    def capture(self, max_width: int) -> Shot:
        from PIL import Image, ImageGrab

        width, height = self.screen_size()
        # physical pixels; layered windows too, or menus and tooltips would be missing from the picture
        image = ImageGrab.grab(include_layered_windows=True, all_screens=False).convert("RGB")
        if max_width and image.width > max_width:
            image = image.resize((max_width, round(image.height * max_width / image.width)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, "PNG", compress_level=3)
        return Shot(buf.getvalue(), image.width, image.height, width or image.width, height or image.height)

    def _describe(self, hwnd: int, point: tuple[int, int] | None) -> Target:
        api = _api()
        if not hwnd:
            return Target()
        pid = _pid(api, hwnd)
        title, cls = _text(api, hwnd), _class(api, hwnd)
        if cls == "ApplicationFrameWindow":
            pid = _app_pid(api, hwnd, pid)
        own = pid == os.getpid() or is_jarvis_title(title)
        name, kind, secret = ("", "", False) if own else _uia_bounded(api, point)
        return Target(hwnd=int(hwnd), pid=pid, process=_process_name(pid), title=title, class_name=cls, element=name,
                      control=kind, password=secret, own=own)

    def target_at(self, x: int, y: int) -> Target:
        api = _api()
        with _physical(api):
            hwnd = api.user32.WindowFromPoint(w.POINT(x, y))
            root = api.user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None
        return self._describe(int(root or hwnd or 0), (x, y))

    def focus_target(self) -> Target:
        api = _api()
        hwnd = api.user32.GetForegroundWindow()
        root = api.user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None
        return self._describe(int(root or hwnd or 0), None)

    def input_state(self) -> tuple[int, int, int]:
        api = _api()
        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        api.user32.GetLastInputInfo(ctypes.byref(info))
        point = w.POINT()
        with _physical(api):
            api.user32.GetCursorPos(ctypes.byref(point))
        return int(info.dwTime), int(point.x), int(point.y)

    def own_windows_visible(self) -> bool:
        api = _api()
        me = os.getpid()
        found = [False]

        def visit(hwnd: int | None, _lparam: int) -> bool:
            try:
                if hwnd and api.user32.IsWindowVisible(hwnd) and not api.user32.IsIconic(hwnd) \
                        and _pid(api, hwnd) == me and _text(api, hwnd):
                    found[0] = True
                    return False
            except Exception:                # never let an exception cross the C callback
                pass
            return True

        api.user32.EnumWindows(api.WNDENUMPROC(visit), 0)
        return found[0]

    def working_window(self) -> int:
        api = _api()
        me = os.getpid()
        found = [0]

        def visit(hwnd: int | None, _lparam: int) -> bool:   # top-level windows, front to back
            try:
                if hwnd and _switchable(api, hwnd) and _pid(api, hwnd) != me \
                        and not is_jarvis_title(_text(api, hwnd)):
                    found[0] = int(hwnd)
                    return False
            except Exception:                # never let an exception cross the C callback
                pass
            return True

        api.user32.EnumWindows(api.WNDENUMPROC(visit), 0)
        return found[0]

    def bring_to_front(self, hwnd: int) -> bool:
        api = _api()
        u = api.user32
        if not hwnd or not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
            return False                     # closed or minimized since: leave it be
        if not u.SetForegroundWindow(hwnd):
            # Windows lets only the program used last move the focus. Sharing the input state of the window
            # in front for a moment lifts that, the way task switchers do it.
            front = u.GetForegroundWindow()
            theirs = int(u.GetWindowThreadProcessId(front, None)) if front else 0
            mine = int(api.kernel32.GetCurrentThreadId())
            if theirs and theirs != mine and u.AttachThreadInput(mine, theirs, True):
                try:
                    u.BringWindowToTop(hwnd)
                    u.SetForegroundWindow(hwnd)
                finally:
                    u.AttachThreadInput(mine, theirs, False)
        return int(u.GetForegroundWindow() or 0) == int(hwnd)

    # ------------------------------------------------------------------ mouse
    def _send(self, inputs: Any, count: int) -> None:
        sent = _api().user32.SendInput(count, inputs, ctypes.sizeof(INPUT))
        if sent != count:
            raise OSError(ctypes.get_last_error(),  # type: ignore[attr-defined]
                          "Windows did not take the input (another program may be blocking it)")

    def _mouse(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
        inputs = (INPUT * 1)()
        inputs[0].type = INPUT_MOUSE
        inputs[0].mi.dx, inputs[0].mi.dy = dx, dy
        inputs[0].mi.mouseData = data & 0xFFFFFFFF
        inputs[0].mi.dwFlags = flags
        self._send(inputs, 1)

    def move(self, x: int, y: int) -> None:
        width, height = self.screen_size()
        x, y = max(0, min(width - 1, x)), max(0, min(height - 1, y))
        nx = min(65535, (x * 65536 + width - 1) // width)
        ny = min(65535, (y * 65536 + height - 1) // height)
        self._mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny)

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> None:
        down, up = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
                    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}[button]
        self.move(x, y)
        time.sleep(0.04)
        for i in range(max(1, count)):
            self._mouse(down)
            time.sleep(0.03)
            self._mouse(up)
            if i + 1 < count:
                time.sleep(0.06)

    def scroll(self, x: int | None, y: int | None, direction: str, amount: int) -> None:
        if x is not None and y is not None:
            self.move(x, y)
            time.sleep(0.04)
        flag = MOUSEEVENTF_HWHEEL if direction in ("left", "right") else MOUSEEVENTF_WHEEL
        sign = 1 if direction in ("up", "right") else -1
        for _ in range(max(1, amount)):
            self._mouse(flag, data=sign * WHEEL_DELTA)
            time.sleep(0.03)

    def drag(self, x: int, y: int, to_x: int, to_y: int) -> None:
        self.move(x, y)
        time.sleep(0.05)
        self._mouse(MOUSEEVENTF_LEFTDOWN)
        try:
            steps = 12
            for i in range(1, steps + 1):
                self.move(x + (to_x - x) * i // steps, y + (to_y - y) * i // steps)
                time.sleep(0.02)
            time.sleep(0.05)
        finally:
            self._mouse(MOUSEEVENTF_LEFTUP)

    # ------------------------------------------------------------------ keyboard
    def _key(self, item: Any, vk: int, *, up: bool) -> None:
        code, extended = VK_BY_CODE.get(vk, (vk, False))
        item.type = INPUT_KEYBOARD
        item.ki.wVk = code
        item.ki.wScan = _api().user32.MapVirtualKeyW(code, MAPVK_VK_TO_VSC)
        item.ki.dwFlags = (KEYEVENTF_EXTENDEDKEY if extended else 0) | (KEYEVENTF_KEYUP if up else 0)

    def _tap(self, vk: int) -> None:
        inputs = (INPUT * 2)()
        self._key(inputs[0], vk, up=False)
        self._key(inputs[1], vk, up=True)
        self._send(inputs, 2)

    def press_keys(self, combos: Sequence[Sequence[str]]) -> bool:
        user32 = _api().user32
        window = user32.GetForegroundWindow()
        for n, combo in enumerate(combos):
            if n and user32.GetForegroundWindow() != window:
                return False          # the rest was checked for the window that had the focus, not this one
            codes = [VK[k][0] for k in combo]
            if VK_ESCAPE in codes:
                self.own_escape_until = time.monotonic() + 0.4
            downs = (INPUT * len(codes))()
            ups = (INPUT * len(codes))()
            for i, code in enumerate(codes):
                self._key(downs[i], code, up=False)
                self._key(ups[i], code, up=True)
            reversed_ups = (INPUT * len(codes))(*reversed(list(ups)))
            self._send(downs, len(codes))
            time.sleep(0.03)
            self._send(reversed_ups, len(codes))
            if n + 1 < len(combos):
                time.sleep(0.06)
        return True

    def type_text(self, text: str, stop: threading.Event | None = None) -> bool:
        user32 = _api().user32
        window = user32.GetForegroundWindow()
        units: list[int] = []

        def flush() -> bool:
            if not units:
                return True
            if user32.GetForegroundWindow() != window:
                return False          # the focus moved: not one more key goes to a window nobody checked
            inputs = (INPUT * (2 * len(units)))()
            for i, unit in enumerate(units):
                for j, up in ((2 * i, False), (2 * i + 1, True)):
                    inputs[j].type = INPUT_KEYBOARD
                    inputs[j].ki.wVk = 0
                    inputs[j].ki.wScan = unit
                    inputs[j].ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
            self._send(inputs, len(inputs))
            units.clear()
            time.sleep(0.012)
            return True

        for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
            if stop is not None and stop.is_set():
                return False
            if ch in ("\n", "\t"):
                if not flush() or user32.GetForegroundWindow() != window:
                    return False
                self._tap(0x0D if ch == "\n" else 0x09)
            else:
                data = ch.encode("utf-16-le")
                units.extend(int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2))
                if len(units) >= 16 and not flush():
                    return False
        return flush()

    def watch_escape(self, on_double: Callable[[], None]) -> _EscapeWatch:
        return _EscapeWatch(self, on_double)


VK_BY_CODE: dict[int, tuple[int, bool]] = {code: (code, extended) for code, extended in VK.values()}

__all__ = ["VK", "WindowsBackend"]
