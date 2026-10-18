"""ctypes bindings for the Windows APIs `WindowsHost` uses — no pywin32 / comtypes needed.

* known folders (`SHGetKnownFolderPath`: real Desktop/Documents/… incl. OneDrive redirection
  and localized names),
* simulated media / volume keys (`SendInput`, falling back to `keybd_event`),
* master volume and mute through Core Audio (`IAudioEndpointVolume`, called through raw COM
  vtables, so it works without comtypes),
* lock (`LockWorkStation`), sleep (`SetSuspendState`), Unicode clipboard text, top-level
  window enumeration / closing, file version descriptions and URL-protocol lookups.

Every public function raises `HostUnsupported` on other platforms, so this module imports
everywhere and its pure parts (structures, GUID parsing) are unit-tested on Linux. Failing
Win32 calls raise `OSError` with the system error text.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from jarvis.host.base import HostUnsupported

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

# ----------------------------------------------------------------------------- virtual keys
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

_own_keys_until = 0.0          # time.monotonic() until which the keys pressed are JARVIS's own


def note_own_keys(seconds: float = 0.5) -> None:
    """JARVIS is about to press keys itself (computer control): Ctrl to talk ignores them for `seconds`."""
    global _own_keys_until
    _own_keys_until = max(_own_keys_until, time.monotonic() + seconds)


def own_keys_recent() -> bool:
    return time.monotonic() < _own_keys_until


# ----------------------------------------------------------------------------- misc constants
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
WM_CLOSE = 0x0010
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
DWMWA_CLOAKED = 14
MB_OK = 0x00000000
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002
ERROR_NOT_ALL_ASSIGNED = 1300
ASSOCF_IS_PROTOCOL = 0x00001000
ASSOCSTR_COMMAND = 1
ASSOCSTR_FRIENDLYAPPNAME = 4

S_OK = 0
S_FALSE = 1
RPC_E_CHANGED_MODE = 0x80010106 - (1 << 32)       # as the signed value a c_long restype returns
COINIT_MULTITHREADED = 0x0
CLSCTX_ALL = 0x1 | 0x2 | 0x4 | 0x10
E_RENDER = 0                                        # EDataFlow.eRender
E_CAPTURE = 1                                       # EDataFlow.eCapture
E_CONSOLE = 0                                       # ERole.eConsole

CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAUDIO_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"

# IUnknown occupies vtable slots 0-2; interface methods follow in header order.
_VT_RELEASE = 2
_VT_ENUM_GET_DEFAULT_AUDIO_ENDPOINT = 4             # IMMDeviceEnumerator
_VT_DEVICE_ACTIVATE = 3                             # IMMDevice
_VT_DEVICE_GET_ID = 5
_VT_SET_MASTER_VOLUME_SCALAR = 7                    # IAudioEndpointVolume
_VT_GET_MASTER_VOLUME_SCALAR = 9
_VT_SET_MUTE = 14
_VT_GET_MUTE = 15

KNOWN_FOLDER_IDS: dict[str, str] = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "home": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",            # FOLDERID_Profile
    "startmenu": "{625B53C3-AB48-4EC1-BA1F-A1EF4146FC19}",
    "appdata": "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",          # FOLDERID_RoamingAppData
    "localappdata": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    "programs": "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",         # user Start menu\Programs
    "common_programs": "{0139D44E-6AFE-49F2-8690-3DAFCAE6FFB8}",  # all users Start menu\Programs
    "common_startmenu": "{A4115719-D62E-491D-AA7C-E74B8BE3B067}",
    "program_files": "{905E63B6-C1BF-494E-B29C-65B732D3D21A}",
    "program_files_x64": "{6D809377-6AF0-444B-8957-A3773F02200E}",
    "program_files_x86": "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}",
    "user_program_files": "{5CD7AEE2-2219-4A67-B85D-6C9CE15660CB}",
    "system": "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}",
    "system_x86": "{D65231B0-B2F1-4857-A4CE-A8E7C6EA7D27}",
    "windows": "{F38BF404-1D43-42F2-9305-67DE0B28FC23}",
    "program_data": "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}",
}

# Window classes that belong to the shell itself, never to an "app".
_SHELL_WINDOW_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})


# =============================================================================================
# Structures (fixed-width fields so sizes match Windows on any 64/32-bit host)
# =============================================================================================
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, text: str) -> GUID:
        """Parse "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}" (braces optional)."""
        return cls.from_buffer_copy(uuid.UUID(text.strip()).bytes_le)

    def __str__(self) -> str:
        return "{" + str(uuid.UUID(bytes_le=bytes(self))).upper() + "}"


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16), ("wParamH", ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    # All three members are required: SendInput checks cbSize == sizeof(INPUT).
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", ctypes.c_uint32)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", ctypes.c_uint32), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str


# =============================================================================================
# Lazy DLL bindings
# =============================================================================================
def _proto(fn: Any, restype: Any, *argtypes: Any) -> Any:
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


class _Win32:
    """Win32 functions with explicit argtypes/restype, bound once per process."""

    def __init__(self) -> None:
        from ctypes import wintypes as w

        self.w = w
        win_dll = ctypes.WinDLL  # type: ignore[attr-defined]
        self.user32 = win_dll("user32", use_last_error=True)
        self.kernel32 = win_dll("kernel32", use_last_error=True)
        self.shell32 = win_dll("shell32", use_last_error=True)
        self.ole32 = win_dll("ole32", use_last_error=True)
        self.advapi32 = win_dll("advapi32", use_last_error=True)
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)  # type: ignore[attr-defined]
        self._optional: dict[str, Any] = {}
        self._optional_lock = threading.Lock()

        u, k, s, o, a = self.user32, self.kernel32, self.shell32, self.ole32, self.advapi32
        _proto(u.SendInput, w.UINT, w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        _proto(u.keybd_event, None, w.BYTE, w.BYTE, w.DWORD, ctypes.c_size_t)
        _proto(u.LockWorkStation, w.BOOL)
        _proto(u.MessageBeep, w.BOOL, w.UINT)
        _proto(u.OpenClipboard, w.BOOL, w.HWND)
        _proto(u.CloseClipboard, w.BOOL)
        _proto(u.EmptyClipboard, w.BOOL)
        _proto(u.IsClipboardFormatAvailable, w.BOOL, w.UINT)
        _proto(u.GetClipboardData, w.HANDLE, w.UINT)
        _proto(u.SetClipboardData, w.HANDLE, w.UINT, w.HANDLE)
        _proto(u.CreateWindowExW, w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
               ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
        _proto(u.DestroyWindow, w.BOOL, w.HWND)
        _proto(u.EnumWindows, w.BOOL, self.WNDENUMPROC, w.LPARAM)
        _proto(u.EnumChildWindows, w.BOOL, w.HWND, self.WNDENUMPROC, w.LPARAM)
        _proto(u.IsWindowVisible, w.BOOL, w.HWND)
        _proto(u.GetWindowTextLengthW, ctypes.c_int, w.HWND)
        _proto(u.GetWindowTextW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        _proto(u.GetClassNameW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        _proto(u.GetWindowThreadProcessId, w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
        _proto(u.GetWindow, w.HWND, w.HWND, w.UINT)
        _proto(u.PostMessageW, w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        # GetWindowLongPtrW only exists in 64-bit user32; 32-bit Windows exports GetWindowLongW.
        self.get_window_long = _proto(getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW,
                                      ctypes.c_ssize_t, w.HWND, ctypes.c_int)

        _proto(k.GlobalAlloc, w.HGLOBAL, w.UINT, ctypes.c_size_t)
        _proto(k.GlobalLock, w.LPVOID, w.HGLOBAL)
        _proto(k.GlobalUnlock, w.BOOL, w.HGLOBAL)
        _proto(k.GlobalFree, w.HGLOBAL, w.HGLOBAL)
        _proto(k.GetCurrentProcess, w.HANDLE)
        _proto(k.CloseHandle, w.BOOL, w.HANDLE)

        _proto(s.SHGetKnownFolderPath, ctypes.c_long, ctypes.POINTER(GUID), w.DWORD, w.HANDLE,
               ctypes.POINTER(ctypes.c_void_p))

        _proto(o.CoTaskMemFree, None, ctypes.c_void_p)
        _proto(o.CoInitializeEx, ctypes.c_long, ctypes.c_void_p, w.DWORD)
        _proto(o.CoUninitialize, None)
        _proto(o.CoCreateInstance, ctypes.c_long, ctypes.POINTER(GUID), ctypes.c_void_p, w.DWORD,
               ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))

        _proto(a.OpenProcessToken, w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
        _proto(a.LookupPrivilegeValueW, w.BOOL, w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(LUID))
        _proto(a.AdjustTokenPrivileges, w.BOOL, w.HANDLE, w.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES), w.DWORD,
               ctypes.c_void_p, ctypes.c_void_p)

    def dll(self, name: str) -> Any:
        """Optional DLLs (powrprof, dwmapi, version, shlwapi), loaded and bound on first use."""
        with self._optional_lock:
            lib = self._optional.get(name)
            if lib is not None:
                return lib
            w = self.w
            lib = ctypes.WinDLL(name, use_last_error=True)  # type: ignore[attr-defined]
            if name == "powrprof":
                _proto(lib.SetSuspendState, w.BOOLEAN, w.BOOLEAN, w.BOOLEAN, w.BOOLEAN)
            elif name == "dwmapi":
                _proto(lib.DwmGetWindowAttribute, ctypes.c_long, w.HWND, w.DWORD, w.LPVOID, w.DWORD)
            elif name == "version":
                _proto(lib.GetFileVersionInfoSizeW, w.DWORD, w.LPCWSTR, w.LPDWORD)
                _proto(lib.GetFileVersionInfoW, w.BOOL, w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID)
                _proto(lib.VerQueryValueW, w.BOOL, w.LPCVOID, w.LPCWSTR, ctypes.POINTER(w.LPVOID), w.PUINT)
            elif name == "shlwapi":
                _proto(lib.AssocQueryStringW, ctypes.c_long, w.DWORD, ctypes.c_int, w.LPCWSTR, w.LPCWSTR,
                       w.LPWSTR, w.LPDWORD)
            self._optional[name] = lib
            return lib


_API: _Win32 | None = None
_API_LOCK = threading.Lock()


def _api(feature: str) -> _Win32:
    global _API
    if not IS_WINDOWS:
        raise HostUnsupported(f"{feature} is only available on Windows.")
    if _API is None:
        with _API_LOCK:
            if _API is None:
                _API = _Win32()
    return _API


def _win_error(what: str) -> OSError:
    code = ctypes.get_last_error()  # type: ignore[attr-defined]
    detail = ctypes.FormatError(code).strip() if code else "unknown error"  # type: ignore[attr-defined]
    return OSError(code, f"{what} failed: {detail}")


def _check_hr(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


# =============================================================================================
# Known folders
# =============================================================================================
def known_folder_path(folder: str) -> str:
    """Path of a known folder by key ("desktop", "downloads"…) or KNOWNFOLDERID string."""
    api = _api("Known folders")
    try:
        guid = GUID.from_string(KNOWN_FOLDER_IDS.get(folder.lower(), folder))
    except ValueError as exc:
        raise HostUnsupported(f"Unknown known folder: {folder}") from exc
    out = ctypes.c_void_p()
    hr = api.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
    try:
        _check_hr(hr, f"SHGetKnownFolderPath({folder})")
        if not out.value:
            raise OSError(f"SHGetKnownFolderPath({folder}) returned no path")
        return ctypes.wstring_at(out.value)
    finally:
        if out.value:                      # must be freed even when the call failed
            api.ole32.CoTaskMemFree(out)


# =============================================================================================
# Keyboard
# =============================================================================================
def press_keys(plan: Sequence[tuple[int, int]], *, chunk: int = 10, pause_s: float = 0.012) -> int:
    """Tap virtual keys: [(VK_VOLUME_DOWN, 50), (VK_VOLUME_UP, 25)]. Returns the number of taps."""
    api = _api("Simulated key presses")
    taps = 0
    for vk, count in plan:
        remaining = max(0, int(count))
        while remaining:
            n = min(max(1, chunk), remaining)
            _tap(api, int(vk), n)
            remaining -= n
            taps += n
            if remaining:
                time.sleep(pause_s)      # lets the shell keep up with long volume sweeps
    return taps


def _tap(api: _Win32, vk: int, times: int) -> None:
    inputs = (INPUT * (2 * times))()
    for i in range(times):
        down, up = inputs[2 * i], inputs[2 * i + 1]
        down.type = up.type = INPUT_KEYBOARD
        down.ki.wVk = up.ki.wVk = vk
        down.ki.dwFlags = KEYEVENTF_EXTENDEDKEY
        up.ki.dwFlags = KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP
    sent = api.user32.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))
    if sent == 0:
        log.debug("SendInput failed (%s); falling back to keybd_event", ctypes.get_last_error())  # type: ignore[attr-defined]
        for _ in range(times):
            api.user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
            api.user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


# =============================================================================================
# Session / power
# =============================================================================================
def lock_workstation() -> None:
    api = _api("Locking the screen")
    if not api.user32.LockWorkStation():
        raise _win_error("LockWorkStation")


def _enable_privilege(api: _Win32, name: str) -> bool:
    token = api.w.HANDLE()
    if not api.advapi32.OpenProcessToken(api.kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                         ctypes.byref(token)):
        return False
    try:
        luid = LUID()
        if not api.advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        privileges = TOKEN_PRIVILEGES()
        privileges.PrivilegeCount = 1
        privileges.Privileges[0].Luid = luid
        privileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        if not api.advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None):
            return False
        return ctypes.get_last_error() != ERROR_NOT_ALL_ASSIGNED  # type: ignore[attr-defined]
    finally:
        api.kernel32.CloseHandle(token)


def suspend(*, hibernate: bool = False) -> None:
    """Put the computer to sleep (or hibernate) via powrprof!SetSuspendState."""
    api = _api("Sleep")
    if not _enable_privilege(api, "SeShutdownPrivilege"):
        log.debug("SeShutdownPrivilege could not be enabled; trying SetSuspendState anyway")
    powrprof = api.dll("powrprof")
    if not powrprof.SetSuspendState(bool(hibernate), True, False):
        raise _win_error("SetSuspendState")


def message_beep(kind: int = MB_OK) -> None:
    api = _api("Beeping")
    api.user32.MessageBeep(kind)


# =============================================================================================
# Clipboard
# =============================================================================================
@contextlib.contextmanager
def _open_clipboard(api: _Win32, attempts: int = 25, delay_s: float = 0.02) -> Iterator[None]:
    # A real (hidden) owner window is needed: with OpenClipboard(NULL), EmptyClipboard sets the
    # owner to NULL and SetClipboardData then fails.
    hwnd = api.user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
    try:
        for _ in range(attempts):
            if api.user32.OpenClipboard(hwnd):
                break
            time.sleep(delay_s)      # another app has it open right now
        else:
            raise _win_error("OpenClipboard (the clipboard is busy)")
        try:
            yield
        finally:
            api.user32.CloseClipboard()
    finally:
        if hwnd:
            api.user32.DestroyWindow(hwnd)


def get_clipboard_text() -> str:
    api = _api("The clipboard")
    with _open_clipboard(api):
        if not api.user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = api.user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = api.kernel32.GlobalLock(handle)
        if not ptr:
            raise _win_error("GlobalLock")
        try:
            return ctypes.wstring_at(ptr)
        finally:
            api.kernel32.GlobalUnlock(handle)


def set_clipboard_text(text: str) -> None:
    api = _api("The clipboard")
    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    with _open_clipboard(api):
        if not api.user32.EmptyClipboard():
            raise _win_error("EmptyClipboard")
        hmem = api.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not hmem:
            raise _win_error("GlobalAlloc")
        try:
            ptr = api.kernel32.GlobalLock(hmem)
            if not ptr:
                raise _win_error("GlobalLock")
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                api.kernel32.GlobalUnlock(hmem)
            if not api.user32.SetClipboardData(CF_UNICODETEXT, hmem):
                raise _win_error("SetClipboardData")
        except BaseException:
            api.kernel32.GlobalFree(hmem)   # on success the system owns the memory
            raise


# =============================================================================================
# Windows
# =============================================================================================
def _window_pid(api: _Win32, hwnd: int) -> int:
    pid = api.w.DWORD(0)
    api.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _window_text(api: _Win32, hwnd: int) -> str:
    length = api.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    api.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(api: _Win32, hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    api.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _is_cloaked(api: _Win32, hwnd: int) -> bool:
    try:
        dwm = api.dll("dwmapi")
    except OSError:
        return False
    value = ctypes.c_uint32(0)
    hr = dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return hr == S_OK and value.value != 0


def _is_app_window(api: _Win32, hwnd: int, class_name: str) -> bool:
    """The Alt-Tab rule: unowned (or WS_EX_APPWINDOW), not a tool window, not cloaked."""
    if class_name in _SHELL_WINDOW_CLASSES:
        return False
    ex_style = api.get_window_long(hwnd, GWL_EXSTYLE)
    if ex_style & WS_EX_TOOLWINDOW and not ex_style & WS_EX_APPWINDOW:
        return False
    if api.user32.GetWindow(hwnd, GW_OWNER) and not ex_style & WS_EX_APPWINDOW:
        return False
    return not _is_cloaked(api, hwnd)


def _uwp_child_pid(api: _Win32, hwnd: int, frame_pid: int) -> int:
    """Store apps draw inside ApplicationFrameHost; the app's own process owns a child window."""
    found = [frame_pid]

    def visit(child: int | None, _lparam: int) -> bool:
        pid = _window_pid(api, child or 0)
        if pid and pid != frame_pid:
            found[0] = pid
            return False
        return True

    api.user32.EnumChildWindows(hwnd, api.WNDENUMPROC(visit), 0)
    return found[0]


def list_windows(*, app_windows_only: bool = True) -> list[WindowInfo]:
    """Visible top-level windows ("app windows" = what Alt-Tab shows, with a title)."""
    api = _api("Listing windows")
    found: list[WindowInfo] = []

    def visit(hwnd: int | None, _lparam: int) -> bool:
        try:
            if not hwnd or not api.user32.IsWindowVisible(hwnd):
                return True
            class_name = _class_name(api, hwnd)
            title = _window_text(api, hwnd)
            if app_windows_only and (not title or not _is_app_window(api, hwnd, class_name)):
                return True
            pid = _window_pid(api, hwnd)
            if class_name == "ApplicationFrameWindow":
                pid = _uwp_child_pid(api, hwnd, pid)
            found.append(WindowInfo(int(hwnd), pid, title, class_name))
        except Exception:                   # never let an exception cross the C callback
            log.debug("Window enumeration callback failed", exc_info=True)
        return True

    callback = api.WNDENUMPROC(visit)
    if not api.user32.EnumWindows(callback, 0):
        raise _win_error("EnumWindows")
    return found


def close_windows(*, class_names: Iterable[str] = (), pids: Iterable[int] = ()) -> int:
    """Post WM_CLOSE to visible top-level windows of the given classes / processes (graceful close)."""
    api = _api("Closing windows")
    classes = {c.lower() for c in class_names}
    wanted = {int(p) for p in pids}
    closed = 0
    for win in list_windows(app_windows_only=False):
        if (classes and win.class_name.lower() in classes) or (wanted and win.pid in wanted):
            if api.user32.PostMessageW(win.hwnd, WM_CLOSE, 0, 0):
                closed += 1
    return closed


# =============================================================================================
# Files and associations
# =============================================================================================
def file_description(path: str) -> str | None:
    """FileDescription from an executable's version resource ("chrome.exe" → "Google Chrome")."""
    api = _api("Reading file versions")
    version = api.dll("version")
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buf):
        return None
    ptr = ctypes.c_void_p()
    length = ctypes.c_uint(0)
    languages: list[tuple[int, int]] = []
    if version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(length)):
        if ptr.value and length.value >= 4:
            count = length.value // 4
            pairs = (ctypes.c_uint16 * (count * 2)).from_address(ptr.value)
            languages = [(pairs[2 * i], pairs[2 * i + 1]) for i in range(count)]
    languages += [(0x0409, 0x04B0), (0x0409, 0x04E4), (0x0000, 0x04B0)]
    for lang, codepage in languages:
        key = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
        if version.VerQueryValueW(buf, key, ctypes.byref(ptr), ctypes.byref(length)) and ptr.value and length.value:
            text = ctypes.wstring_at(ptr.value, length.value).split("\x00", 1)[0].strip()
            if text:
                return text
    return None


def protocol_handler(scheme: str) -> str | None:
    """Name of the app registered for a URL protocol ("ms-settings" → "Settings"), None if none is."""
    api = _api("URL protocol lookup")
    shlwapi = api.dll("shlwapi")
    scheme = scheme.rstrip(":")
    for what in (ASSOCSTR_FRIENDLYAPPNAME, ASSOCSTR_COMMAND):
        size = api.w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        hr = shlwapi.AssocQueryStringW(ASSOCF_IS_PROTOCOL, what, scheme, None, buf, ctypes.byref(size))
        if hr == S_OK and buf.value:
            return buf.value
    return None


# =============================================================================================
# Core Audio (IAudioEndpointVolume of the default output device)
# =============================================================================================
def _com_method(obj: ctypes.c_void_p, index: int, restype: Any, *argtypes: Any) -> Any:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)  # type: ignore[attr-defined]
    return prototype(vtable[index])


def _com_release(obj: ctypes.c_void_p) -> None:
    if obj.value:
        _com_method(obj, _VT_RELEASE, ctypes.c_ulong)(obj)
        obj.value = None


@contextlib.contextmanager
def _default_device(api: _Win32, flow: int) -> Iterator[ctypes.c_void_p | None]:
    """The default IMMDevice for output (E_RENDER) or microphones (E_CAPTURE); None when there is none."""
    hr = api.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    initialized = hr in (S_OK, S_FALSE)
    if not initialized and hr != RPC_E_CHANGED_MODE:      # CHANGED_MODE: COM already set up (STA) — fine
        _check_hr(hr, "CoInitializeEx")
    enumerator, device = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        clsid = GUID.from_string(CLSID_MMDEVICE_ENUMERATOR)
        iid_enumerator = GUID.from_string(IID_IMMDEVICE_ENUMERATOR)
        _check_hr(api.ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL, ctypes.byref(iid_enumerator),
                                             ctypes.byref(enumerator)), "CoCreateInstance(MMDeviceEnumerator)")
        get_default = _com_method(enumerator, _VT_ENUM_GET_DEFAULT_AUDIO_ENDPOINT, ctypes.c_long,
                                  ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
        hr = get_default(enumerator, flow, E_CONSOLE, ctypes.byref(device))
        if flow == E_CAPTURE and hr != S_OK:              # E_NOTFOUND: no microphone at all
            yield None
            return
        _check_hr(hr, "GetDefaultAudioEndpoint")
        yield device
    finally:
        for obj in (device, enumerator):
            with contextlib.suppress(Exception):
                _com_release(obj)
        if initialized:
            api.ole32.CoUninitialize()


@contextlib.contextmanager
def _endpoint_volume(api: _Win32) -> Iterator[ctypes.c_void_p]:
    endpoint = ctypes.c_void_p()
    with _default_device(api, E_RENDER) as device:
        assert device is not None
        try:
            iid_volume = GUID.from_string(IID_IAUDIO_ENDPOINT_VOLUME)
            activate = _com_method(device, _VT_DEVICE_ACTIVATE, ctypes.c_long, ctypes.POINTER(GUID), ctypes.c_uint32,
                                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
            _check_hr(activate(device, ctypes.byref(iid_volume), CLSCTX_ALL, None, ctypes.byref(endpoint)),
                      "IMMDevice::Activate(IAudioEndpointVolume)")
            yield endpoint
        finally:
            with contextlib.suppress(Exception):
                _com_release(endpoint)


def default_microphone_id() -> str | None:
    """Windows' id of the default microphone (it changes when another one becomes the default); None: none."""
    api = _api("Reading the default microphone")
    with _default_device(api, E_CAPTURE) as device:
        if device is None:
            return None
        text = ctypes.c_void_p()
        get_id = _com_method(device, _VT_DEVICE_GET_ID, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
        _check_hr(get_id(device, ctypes.byref(text)), "IMMDevice::GetId")
        try:
            return ctypes.wstring_at(text.value) if text.value else None
        finally:
            api.ole32.CoTaskMemFree(text)


def _read_scalar(endpoint: ctypes.c_void_p) -> int:
    level = ctypes.c_float(0.0)
    get = _com_method(endpoint, _VT_GET_MASTER_VOLUME_SCALAR, ctypes.c_long, ctypes.POINTER(ctypes.c_float))
    _check_hr(get(endpoint, ctypes.byref(level)), "GetMasterVolumeLevelScalar")
    return max(0, min(100, round(level.value * 100)))


def get_master_volume() -> int:
    """Master output volume 0-100."""
    api = _api("Reading the volume")
    with _endpoint_volume(api) as endpoint:
        return _read_scalar(endpoint)


def set_master_volume(level: int) -> int:
    """Set the master output volume (0-100); returns the level read back."""
    api = _api("Setting the volume")
    value = max(0, min(100, int(level))) / 100.0
    with _endpoint_volume(api) as endpoint:
        put = _com_method(endpoint, _VT_SET_MASTER_VOLUME_SCALAR, ctypes.c_long, ctypes.c_float, ctypes.c_void_p)
        _check_hr(put(endpoint, value, None), "SetMasterVolumeLevelScalar")
        return _read_scalar(endpoint)


def get_master_mute() -> bool:
    api = _api("Reading the mute state")
    with _endpoint_volume(api) as endpoint:
        muted = ctypes.c_int(0)
        get = _com_method(endpoint, _VT_GET_MUTE, ctypes.c_long, ctypes.POINTER(ctypes.c_int))
        _check_hr(get(endpoint, ctypes.byref(muted)), "GetMute")
        return bool(muted.value)


def set_master_mute(mute: bool) -> None:
    api = _api("Muting")
    with _endpoint_volume(api) as endpoint:
        put = _com_method(endpoint, _VT_SET_MUTE, ctypes.c_long, ctypes.c_int, ctypes.c_void_p)
        _check_hr(put(endpoint, 1 if mute else 0, None), "SetMute")
