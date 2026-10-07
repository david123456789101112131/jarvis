"""Host adapters: app index, Windows/POSIX helpers and adapter behaviour.

* app index — query cleaning, bilingual matching (Hebrew/English, aliases, articles, fuzzy,
  phonetic), merging, catalog annotation, the JSON cache (TTL) and the async store;
* Windows helpers as pure logic on any OS — Get-StartApps JSON, .lnk/.url shortcuts, App Paths,
  launch plans, PE headers, command lines, volume-key math, power/toast/shell argv;
* `WindowsHost` / `PosixHost` behaviour with a fake `run_process`, fake tools on PATH and fake
  processes (monkeypatched), and `get_host_adapter()`;
* smoke tests against the real Windows APIs, marked `windows` (skipped elsewhere).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import struct
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from jarvis.common.errors import PermissionDenied
from jarvis.host import monitor, posix, winapi, windows
from jarvis.host.app_index import (
    DEFAULT_TTL_S,
    INDEX_VERSION,
    AppIndex,
    AppIndexCache,
    AppIndexStore,
    DelayedActions,
    annotate_with_catalog,
    app_from_json,
    candidate_process_names,
    catalog_lookup,
    clean_query,
    command_head,
    compact_key,
    match_key,
    merge_apps,
    name_forms,
    query_variants,
    screenshot_target,
    skeleton,
    validate_web_url,
)
from jarvis.host.base import HostUnsupported, InstalledApp, get_host_adapter
from jarvis.host.posix import (
    DesktopEntry,
    PosixHost,
    desktop_app,
    desktop_exec_argv,
    parse_amixer,
    parse_brightnessctl,
    parse_desktop_entry,
    parse_percent,
    parse_user_dirs,
    parse_wmctrl,
    power_argv_posix,
    scan_desktop_files,
)
from jarvis.host.windows import (
    BATCH_LAUNCH_FLAGS,
    CONSOLE_LAUNCH_FLAGS,
    EXPLORER_WINDOW_CLASSES,
    GUI_LAUNCH_FLAGS,
    PS_UTF8_PREFIX,
    LaunchPlan,
    ShellLink,
    WindowsHost,
    app_paths_to_apps,
    catalog_apps_windows,
    expand_windows_vars,
    is_auxiliary_name,
    launch_flags_for,
    link_process_name,
    mute_key_plan,
    parse_lnk_bytes,
    parse_start_apps,
    parse_url_shortcut,
    pe_subsystem,
    plan_launch,
    power_argv,
    powershell_argv,
    prefer_real_exe,
    reveal_command_line,
    scan_start_menu,
    split_windows_args,
    toast_xml,
    volume_key_plan,
)
from jarvis.security.process import ProcessResult

IS_WINDOWS = sys.platform == "win32"
not_windows = pytest.mark.skipif(IS_WINDOWS, reason="checks the behaviour off Windows")
VK_UP, VK_DOWN, VK_MUTE = winapi.VK_VOLUME_UP, winapi.VK_VOLUME_DOWN, winapi.VK_VOLUME_MUTE
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

PACTL_VOLUME = (
    "Volume: front-left: 32768 /  50% / -18.06 dB,   front-right: 32768 /  50% / -18.06 dB\n"
    "        balance 0.00\n"
)
AMIXER_MASTER = (
    "Simple mixer control 'Master',0\n"
    "  Capabilities: pvolume pvolume-joined pswitch pswitch-joined\n"
    "  Playback channels: Mono\n"
    "  Limits: Playback 0 - 87\n"
    "  Mono: Playback 37 [42%] [-37.50dB] [on]\n"
)


# =============================================================================================
# Fakes and builders
# =============================================================================================
def proc_result(stdout: str = "", *, code: int = 0, stderr: str = "") -> ProcessResult:
    return ProcessResult(args=[], exit_code=code, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Stands in for `run_process`: records every call and answers by argv prefix (longest wins).

    An answer is a ProcessResult or a callable(argv, kwargs) returning one; unknown commands succeed.
    """

    def __init__(self) -> None:
        self.answers: dict[tuple[str, ...], Any] = {}
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    async def __call__(self, args: Sequence[str], **kwargs: Any) -> ProcessResult:
        argv = [str(a) for a in args]
        self.calls.append((argv, kwargs))
        for prefix in sorted(self.answers, key=len, reverse=True):
            if tuple(argv[: len(prefix)]) == prefix:
                answer = self.answers[prefix]
                return answer(argv, kwargs) if callable(answer) else answer
        return proc_result()

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


def fake_which(*tools: str) -> Callable[..., str | None]:
    available = set(tools)

    def which(name: str, *_args: Any, **_kwargs: Any) -> str | None:
        return f"/usr/bin/{name}" if name in available else None

    return which


class FakeProc:
    """Just enough of psutil.Process for the close logic."""

    def __init__(self, pid: int, name: str) -> None:
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def children(self, recursive: bool = False) -> list[FakeProc]:
        return []


def fake_wait_procs(*rounds: set[int]) -> Callable[..., tuple[list[Any], list[Any]]]:
    """psutil.wait_procs stand-in: call N reports the pids in rounds[N] as exited."""
    pending = list(rounds)

    def wait_procs(procs: Sequence[Any], timeout: float | None = None) -> tuple[list[Any], list[Any]]:
        gone_pids = pending.pop(0) if pending else set()
        return [p for p in procs if p.pid in gone_pids], [p for p in procs if p.pid not in gone_pids]

    return wait_procs


class SpawnRecorder:
    """Replaces PosixHost._spawn."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> Any:
        self.argvs.append(list(argv))
        return SimpleNamespace(pid=4321)


def static_builder(apps: Sequence[InstalledApp]) -> Callable[[], Any]:
    async def build() -> list[InstalledApp]:
        return annotate_with_catalog(merge_apps(apps))

    return build


def write_png(target: Path) -> None:
    Path(target).write_bytes(PNG_BYTES)


def fail_grab(target: Path) -> None:
    raise RuntimeError("no display")


def png_path(path: Path) -> Path:
    return path.with_suffix(".png").resolve()


def write_shot_from_env(argv: list[str], kwargs: dict[str, Any]) -> ProcessResult:
    write_png(Path(kwargs["extra_env"]["JARVIS_SHOT_PATH"]))
    return proc_result()


def write_shot_from_argv(argv: list[str], kwargs: dict[str, Any]) -> ProcessResult:
    write_png(Path(argv[-1]))
    return proc_result()


def write_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------------- .lnk / PE builders
LNK_CLSID = uuid.UUID("00021401-0000-0000-C000-000000000046").bytes_le


def lnk_string(text: str) -> bytes:
    return struct.pack("<H", len(text)) + text.encode("utf-16-le")


def link_info(path: str, *, unicode: bool = False) -> bytes:
    """LinkInfo with VolumeID + LocalBasePath (and the Unicode copies when `unicode`)."""
    header_size = 0x24 if unicode else 0x1C
    volume = struct.pack("<IIII", 0x11, 3, 0x1234ABCD, 0x10) + b"\x00"
    base = path.encode("cp1252", "replace") + b"\x00"
    body = volume + base + b"\x00"                      # LocalBasePath + empty CommonPathSuffix
    volume_off = header_size
    base_off = volume_off + len(volume)
    suffix_off = base_off + len(base)
    unicode_offsets = b""
    if unicode:
        base_u_off = header_size + len(body)
        base_u = path.encode("utf-16-le") + b"\x00\x00"
        unicode_offsets = struct.pack("<II", base_u_off, base_u_off + len(base_u))
        body += base_u + b"\x00\x00"
    size = header_size + len(body)
    return struct.pack("<7I", size, header_size, 0x1, volume_off, base_off, 0, suffix_off) + unicode_offsets + body


def build_lnk(
    *,
    target: str | None = None,
    unicode_info: bool = False,
    description: str | None = None,
    relative: str | None = None,
    working_dir: str | None = None,
    args: str | None = None,
    env_target: str | None = None,
    darwin: bool = False,
    force_no_link_info: bool = False,
    id_list: bool = True,
) -> bytes:
    flags = 0x80                                        # IsUnicode
    for present, bit in (
        (id_list, 0x01), (target is not None, 0x02), (description is not None, 0x04), (relative is not None, 0x08),
        (working_dir is not None, 0x10), (args is not None, 0x20), (force_no_link_info, 0x100),
        (env_target is not None, 0x200), (darwin, 0x1000),
    ):
        if present:
            flags |= bit
    data = struct.pack("<I16sI", 0x4C, LNK_CLSID, flags) + bytes(76 - 24)
    if id_list:
        data += struct.pack("<H", 2) + b"\x00\x00"      # empty IDList (just the terminator)
    if target is not None:
        data += link_info(target, unicode=unicode_info)
    for value in (description, relative, working_dir, args):
        if value is not None:
            data += lnk_string(value)
    if env_target is not None:
        ansi = env_target.encode("cp1252", "replace")[:259].ljust(260, b"\x00")
        wide = env_target.encode("utf-16-le")[:518].ljust(520, b"\x00")
        data += struct.pack("<II", 0x314, 0xA0000001) + ansi + wide
    if darwin:
        data += struct.pack("<II", 0x314, 0xA0000006) + bytes(0x314 - 8)
    return data + b"\x00\x00\x00\x00"                   # TerminalBlock


def make_pe(subsystem: int, *, magic: int = 0x20B, e_lfanew: int = 0x80) -> bytes:
    dos = bytearray(e_lfanew)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, e_lfanew)
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 240, 0x22)
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, magic)
    struct.pack_into("<H", optional, 68, subsystem)
    return bytes(dos) + b"PE\x00\x00" + coff + bytes(optional)


# ------------------------------------------------------------------------- sample installed apps
def sample_windows_apps() -> list[InstalledApp]:
    sm, um = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs", r"C:\Users\me\Start Menu\Programs"
    return [
        InstalledApp("Google Chrome", sm + r"\Google Chrome.lnk", "lnk", "startmenu", process_names=("chrome.exe",)),
        InstalledApp("Google Chrome", "Chrome", "appx", "appx"),
        InstalledApp("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe", "exe", "apppaths",
                     process_names=("chrome.exe",)),
        InstalledApp("Visual Studio Code", um + r"\Visual Studio Code\Visual Studio Code.lnk", "lnk", "startmenu",
                     process_names=("code.exe",)),
        InstalledApp("Word", sm + r"\Word.lnk", "lnk", "startmenu", process_names=("winword.exe",)),
        InstalledApp("WordPad", r"C:\Program Files\Windows NT\Accessories\wordpad.exe", "exe", "apppaths",
                     process_names=("wordpad.exe",)),
        InstalledApp("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "appx", "appx"),
        InstalledApp("Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "appx", "appx"),
        InstalledApp("Notepad", r"C:\Windows\System32\notepad.exe", "exe", "catalog", id="notepad"),
        InstalledApp("Notepad++", sm + r"\Notepad++.lnk", "lnk", "startmenu", process_names=("notepad++.exe",)),
        InstalledApp("Adobe Photoshop 2024", sm + r"\Adobe Photoshop 2024.lnk", "lnk", "startmenu",
                     process_names=("photoshop.exe",)),
        InstalledApp("Blender 4.1", sm + r"\Blender\Blender 4.1.lnk", "lnk", "startmenu",
                     process_names=("blender-launcher.exe",)),
        InstalledApp("Obsidian", um + r"\Obsidian.lnk", "lnk", "startmenu", process_names=("obsidian.exe",)),
        InstalledApp("VLC media player", sm + r"\VideoLAN\VLC media player.lnk", "lnk", "startmenu",
                     process_names=("vlc.exe",)),
        InstalledApp("Python 3.12 (64-bit)", um + r"\Python 3.12\Python 3.12 (64-bit).lnk", "lnk", "startmenu",
                     process_names=("python.exe",)),
        InstalledApp("OBS Studio", sm + r"\OBS Studio\OBS Studio (64bit).lnk", "lnk", "startmenu"),
        InstalledApp("Paint 3D", "Microsoft.MSPaint_8wekyb3d8bbwe!Microsoft.MSPaint", "appx", "appx"),
        InstalledApp("Paint", "Microsoft.Paint_8wekyb3d8bbwe!App", "appx", "appx"),
        InstalledApp("Notion", um + r"\Notion.lnk", "lnk", "startmenu", process_names=("notion.exe",)),
        InstalledApp("Figma", um + r"\Figma.lnk", "lnk", "startmenu", process_names=("figma.exe",)),
        InstalledApp("Slack", um + r"\Slack Technologies Inc\Slack.lnk", "lnk", "startmenu", process_names=("slack.exe",)),
        InstalledApp("Settings", "windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel",
                     "appx", "appx"),
        InstalledApp("Steam", sm + r"\Steam\Steam.lnk", "lnk", "startmenu", process_names=("steam.exe",)),
    ]


@pytest.fixture(scope="module")
def win_index() -> AppIndex:
    return AppIndex(annotate_with_catalog(merge_apps(sample_windows_apps())), platform="windows")


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    return write_file(tmp_path / "docs" / "notes file.txt", "hello").resolve()


@pytest.fixture
def win_host(monkeypatch: pytest.MonkeyPatch) -> tuple[WindowsHost, FakeRunner]:
    """WindowsHost with a fake run_process, deterministic system paths and the sample app index."""
    runner = FakeRunner()
    monkeypatch.setattr(windows, "run_process", runner)
    monkeypatch.setattr(windows, "_system32_exe", lambda name: name)
    monkeypatch.setattr(windows, "_powershell_exe", lambda: "powershell.exe")
    monkeypatch.setattr(windows, "_explorer_exe", lambda: "explorer.exe")
    monkeypatch.setattr(windows, "register_toast_app_id", lambda *_a, **_k: False)   # no registry writes
    host = WindowsHost()
    host._apps = AppIndexStore(static_builder(sample_windows_apps()), platform="windows")
    return host, runner


@pytest.fixture
def popen_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    """Records subprocess.Popen calls instead of starting programs."""
    calls: list[tuple[Any, dict[str, Any]]] = []

    class _FakePopen:
        pid = 4242

        def __init__(self, argv: Any, **kwargs: Any) -> None:
            calls.append((argv, kwargs))

        def poll(self) -> int | None:
            return None

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return calls


@pytest.fixture
def startfile_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str | None]]:
    calls: list[tuple[str, str, str | None]] = []

    def startfile(target: str, operation: str | None = None, arguments: str = "", cwd: str | None = None) -> None:
        calls.append((target, arguments, cwd))

    monkeypatch.setattr(os, "startfile", startfile, raising=False)
    return calls


@pytest.fixture
def px(monkeypatch: pytest.MonkeyPatch) -> tuple[PosixHost, FakeRunner]:
    """PosixHost (Linux flavour) with a fake run_process and no tools on PATH."""
    runner = FakeRunner()
    monkeypatch.setattr(posix, "run_process", runner)
    monkeypatch.setattr(posix.shutil, "which", fake_which())
    host = PosixHost()
    host.is_macos = False
    return host, runner


def use_tools(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setattr(posix.shutil, "which", fake_which(*names))


# =============================================================================================
# Query text
# =============================================================================================
class TestQueryText:
    @pytest.mark.parametrize(
        ("text", "cleaned"),
        [
            ("the calculator", "calculator"),
            ("Spotify app", "spotify"),
            ("open", "open"),
            ("my cool tool please", "cool tool"),
            ("את האפליקציה של ספוטיפיי", "ספוטיפיי"),
            ("תוכנת וורד בבקשה", "וורד"),
            ("app", "app"),
            ("", ""),
        ],
    )
    def test_clean_query(self, text: str, cleaned: str) -> None:
        assert clean_query(text) == cleaned

    def test_query_variants_strip_hebrew_prefixes(self) -> None:
        assert query_variants("את הכרום") == ["הכרום", "כרום", "את הכרום"]
        assert "כרום" in query_variants("לכרום")
        assert "כרום" in query_variants("והכרום")
        assert query_variants("פנקס הרשימות")[:2] == ["פנקס הרשימות", "פנקס רשימות"]
        assert query_variants("הכרום", prefixes=False) == ["הכרום", "כרום"]
        assert query_variants("לכרום", prefixes=False) == ["לכרום"]
        assert query_variants("") == []

    def test_keys_and_name_forms(self) -> None:
        assert match_key("Notepad++") == "notepad plus plus"
        assert match_key("  Google   CHROME ") == "google chrome"
        assert match_key("C#") == "c sharp"
        assert match_key("AT&T") == "at and t"
        assert compact_key("VS-Code") == compact_key("vs code") == "vscode"
        assert name_forms("Adobe Photoshop 2024") == ["adobe photoshop 2024", "adobe photoshop", "photoshop"]
        assert name_forms("Python 3.12 (64-bit)") == ["python 3 12 64 bit", "python"]
        assert name_forms("7-Zip File Manager") == ["7 zip file manager"]

    @pytest.mark.parametrize(
        ("latin", "hebrew"),
        [("Blender", "בלנדר"), ("Notion", "נושן"), ("WhatsApp", "וואטסאפ"), ("PowerPoint", "פאוורפוינט")],
    )
    def test_phonetic_skeletons_match_across_scripts(self, latin: str, hebrew: str) -> None:
        assert skeleton(latin) == skeleton(hebrew) != ""


# =============================================================================================
# Matching
# =============================================================================================
class TestMatching:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("כרום", "Google Chrome"),
            ("גוגל כרום", "Google Chrome"),
            ("Chrome", "Google Chrome"),
            ("לכרום", "Google Chrome"),
            ("את הכרום", "Google Chrome"),
            ("vs code", "Visual Studio Code"),
            ("VS-Code", "Visual Studio Code"),
            ("וי אס קוד", "Visual Studio Code"),
            ("visual studio", "Visual Studio Code"),
            ("וורד", "Word"),
            ("word", "Word"),
            ("wordpad", "WordPad"),
            ("the calculator", "Calculator"),
            ("את המחשבון", "Calculator"),
            ("Spotify app", "Spotify"),
            ("האפליקציה של ספוטיפיי", "Spotify"),
            ("photoshop", "Adobe Photoshop 2024"),
            ("פוטושופ", "Adobe Photoshop 2024"),
            ("Notepad++", "Notepad++"),
            ("notepad plus plus", "Notepad++"),
            ("נוטפד פלוס פלוס", "Notepad++"),
            ("notepad", "Notepad"),
            ("פנקס רשימות", "Notepad"),
            ("vlc player", "VLC media player"),
            ("python", "Python 3.12 (64-bit)"),
            ("paint", "Paint"),
            ("צייר", "Paint"),
            ("paint 3d", "Paint 3D"),
            ("obs", "OBS Studio"),
            ("הגדרות", "Settings"),
            ("סטים", "Steam"),
            ("spotfy", "Spotify"),
            ("crome", "Google Chrome"),
            ("ספוטיפאי", "Spotify"),
            ("בלנדר", "Blender 4.1"),
            ("אובסידיאן", "Obsidian"),
            ("נושן", "Notion"),
            ("פיגמה", "Figma"),
            ("סלאק", "Slack"),
            ("פייתון", "Python 3.12 (64-bit)"),
        ],
    )
    def test_spoken_names_resolve(self, win_index: AppIndex, query: str, expected: str) -> None:
        app = win_index.find(query)
        assert app is not None, query
        assert app.name == expected

    @pytest.mark.parametrize(
        ("query", "stage"),
        [
            ("כרום", "catalog"),
            ("wordpad", "exact"),
            ("python", "exact"),
            ("vlc player", "token"),
            ("spotfy", "fuzzy"),
            ("בלנדר", "phonetic"),
            ("נוטפד פלוס פלוס", "phonetic"),
        ],
    )
    def test_match_stage(self, win_index: AppIndex, query: str, stage: str) -> None:
        match = win_index.match(query)
        assert match is not None and match.stage == stage and 0 < match.score <= 1

    @pytest.mark.parametrize("query", ["excel", "אקסל", "xyz123", "", "   ", "!!!", "את", "the app"])
    def test_unknown_or_not_installed_returns_none(self, win_index: AppIndex, query: str) -> None:
        assert win_index.find(query) is None

    def test_known_app_is_never_swapped_for_a_look_alike(self) -> None:
        index = AppIndex(merge_apps([InstalledApp("WordPad", r"C:\X\wordpad.exe", "exe", "apppaths")]))
        assert index.find("word") is None                 # Word isn't installed; WordPad is a different app
        assert index.find("wordpad") is not None

    def test_catalog_match_prefers_the_best_source(self, win_index: AppIndex) -> None:
        chrome = win_index.find("chrome")
        assert chrome is not None and (chrome.kind, chrome.source, chrome.id) == ("lnk", "startmenu", "chrome")

    def test_search_suggestions(self, win_index: AppIndex) -> None:
        hits = win_index.search("phot")
        assert hits and hits[0].app.name == "Adobe Photoshop 2024"
        assert len(win_index.search("o", limit=2)) <= 2
        assert win_index.search("") == []


# =============================================================================================
# Index JSON, cache and store
# =============================================================================================
class TestIndexPersistence:
    def test_json_roundtrip(self, win_index: AppIndex) -> None:
        again = AppIndex.from_json(json.loads(json.dumps(win_index.to_json(), ensure_ascii=False)))
        assert [a.to_dict() for a in again.apps] == [a.to_dict() for a in win_index.apps]
        assert [(a.process_names, a.aliases) for a in again.apps] == [(a.process_names, a.aliases) for a in win_index.apps]
        assert (again.built_at, again.platform) == (win_index.built_at, "windows")
        found = again.find("כרום")
        assert found is not None and found.name == "Google Chrome"

    def test_from_json_rejects_other_versions_and_skips_bad_rows(self) -> None:
        with pytest.raises(ValueError):
            AppIndex.from_json({"version": INDEX_VERSION + 1, "apps": []})
        with pytest.raises(ValueError):
            AppIndex.from_json(["not", "an", "index"])
        index = AppIndex.from_json({"version": INDEX_VERSION, "built_at": 5, "apps": [
            {"name": "Ok", "target": "ok.exe", "kind": "exe"},
            {"name": "", "target": "x.exe", "kind": "exe"},
            {"name": "Bad kind", "target": "x", "kind": "weird"},
            "junk",
        ]})
        assert [a.name for a in index.apps] == ["Ok"] and index.built_at == 5.0
        app = app_from_json({"name": "X", "target": "x", "kind": "exe", "id": "", "process_names": ["a", ""], "aliases": None})
        assert app is not None and app.id is None and app.process_names == ("a",) and app.aliases == ()

    def test_freshness_window(self) -> None:
        index = AppIndex([], built_at=1000.0)
        assert index.is_fresh(100, now=1050.0)
        assert not index.is_fresh(100, now=1100.0)          # expired
        assert not index.is_fresh(100, now=600.0)           # built "in the future": broken clock or cache

    def test_cache_ttl(self, tmp_path: Path) -> None:
        cache = AppIndexCache(tmp_path / "sub" / "apps.json", ttl_s=100, platform="windows")
        assert cache.save(AppIndex([InstalledApp("Notepad", "notepad.exe", "exe")], built_at=1000.0, platform="windows"))
        loaded = cache.load(now=1050.0)
        assert loaded is not None and [a.name for a in loaded.apps] == ["Notepad"]
        assert cache.load(now=1100.0) is None
        assert cache.load(now=1100.0, allow_stale=True) is not None
        assert not list((tmp_path / "sub").glob("*.tmp"))  # atomic write leaves nothing behind

    def test_cache_ignores_corrupt_files_other_versions_and_platforms(self, tmp_path: Path) -> None:
        path = tmp_path / "apps.json"
        cache = AppIndexCache(path, platform="windows")
        path.write_text("{not json", encoding="utf-8")
        assert cache.load(allow_stale=True) is None
        path.write_text(json.dumps({"version": 99, "apps": []}), encoding="utf-8")
        assert cache.load(allow_stale=True) is None
        AppIndexCache(path, platform="linux").save(AppIndex([InstalledApp("gedit", "gedit", "command")], platform="linux"))
        assert cache.load() is None
        assert AppIndexCache(path, platform="linux").load() is not None
        cache.clear()
        assert not path.exists()
        cache.clear()                                       # clearing twice is fine

    def test_memory_only_cache(self) -> None:
        cache = AppIndexCache(None)
        assert cache.load() is None
        assert cache.save(AppIndex([])) is False


def counting_builder(apps: Sequence[InstalledApp], calls: list[int]) -> Callable[[], Any]:
    async def build() -> list[InstalledApp]:
        calls.append(1)
        await asyncio.sleep(0.01)
        return list(apps)

    return build


def save_index(path: Path, names: Sequence[str], *, age_s: float = 0.0) -> None:
    apps = [InstalledApp(n, f"{n.lower()}.exe", "exe") for n in names]
    AppIndexCache(path, platform="windows").save(AppIndex(apps, built_at=time.time() - age_s, platform="windows"))


class TestAppIndexStore:
    async def test_builds_once_and_reuses_the_index(self, tmp_path: Path) -> None:
        calls: list[int] = []
        builder = counting_builder([InstalledApp("Notepad", "notepad.exe", "exe")], calls)
        store = AppIndexStore(builder, cache_file=tmp_path / "apps.json", platform="windows")
        first, second, third = await asyncio.gather(store.get(), store.get(), store.get())
        assert first is second is third and len(calls) == 1
        assert await store.get() is first and len(calls) == 1
        assert store.cache.load() is not None               # written through to the cache file
        rebuilt = await store.get(refresh=True)
        assert rebuilt is not first and len(calls) == 2 and store.current is rebuilt

    async def test_new_store_loads_the_cache_without_building(self, tmp_path: Path) -> None:
        path = tmp_path / "apps.json"
        save_index(path, ["Cached"])
        calls: list[int] = []
        store = AppIndexStore(counting_builder([InstalledApp("Built", "b.exe", "exe")], calls), cache_file=path,
                              platform="windows")
        assert [a.name for a in (await store.get()).apps] == ["Cached"] and calls == []

    async def test_expired_cache_is_rebuilt(self, tmp_path: Path) -> None:
        path = tmp_path / "apps.json"
        save_index(path, ["Old"], age_s=2 * DEFAULT_TTL_S)
        calls: list[int] = []
        store = AppIndexStore(counting_builder([InstalledApp("Built", "b.exe", "exe")], calls), cache_file=path,
                              platform="windows")
        assert [a.name for a in (await store.get()).apps] == ["Built"] and len(calls) == 1
        reloaded = store.cache.load()
        assert reloaded is not None and [a.name for a in reloaded.apps] == ["Built"]

    async def test_failed_build_falls_back_to_a_stale_index(self, tmp_path: Path) -> None:
        path = tmp_path / "apps.json"
        save_index(path, ["Old"], age_s=2 * DEFAULT_TTL_S)

        async def broken() -> list[InstalledApp]:
            raise RuntimeError("scan failed")

        assert [a.name for a in (await AppIndexStore(broken, cache_file=path, platform="windows").get()).apps] == ["Old"]
        with pytest.raises(RuntimeError):
            await AppIndexStore(broken, platform="windows").get()

    async def test_invalidate_drops_memory_and_cache(self, tmp_path: Path) -> None:
        calls: list[int] = []
        store = AppIndexStore(counting_builder([InstalledApp("A", "a.exe", "exe")], calls),
                              cache_file=tmp_path / "apps.json", platform="windows")
        await store.get()
        store.invalidate()
        assert store.current is None and store.cache.load(allow_stale=True) is None
        await store.get()
        assert len(calls) == 2


# =============================================================================================
# Merging, catalog annotation, process names
# =============================================================================================
class TestMergeAndAnnotate:
    @staticmethod
    def chrome_sources() -> tuple[InstalledApp, ...]:
        start = InstalledApp("Google Chrome", r"C:\SM\Google Chrome.lnk", "lnk", "startmenu", process_names=("chrome.exe",))
        appx = InstalledApp("Google Chrome", "Chrome", "appx", "appx")
        apppaths = InstalledApp("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe", "exe", "apppaths",
                                process_names=("chrome.exe",))
        cat = InstalledApp("Google Chrome", r"C:\PROGRAM FILES\Google\Chrome\Application\CHROME.EXE", "exe", "catalog",
                           id="chrome", process_names=("chrome.exe", "chrome"), aliases=("כרום",))
        return start, appx, apppaths, cat

    def test_merge_keeps_the_best_source_and_pools_details(self) -> None:
        start, appx, apppaths, cat = self.chrome_sources()
        merged = merge_apps([cat], [start, appx], [apppaths])
        assert [(a.name, a.kind, a.source) for a in merged] == [
            ("Google Chrome", "lnk", "startmenu"),          # absorbed the Store entry (same name)
            ("chrome", "exe", "apppaths"),                  # absorbed the catalog entry (same exe, any case)
        ]
        exe = merged[1]
        assert exe.id == "chrome" and exe.process_names == ("chrome.exe", "chrome")
        assert exe.aliases == ("Google Chrome", "כרום")
        # Inputs are not mutated.
        assert merged[0] is not start and start.aliases == () and apppaths.id is None
        assert cat.process_names == ("chrome.exe", "chrome")

    def test_case_sensitive_targets_merge_by_name_instead(self) -> None:
        start, appx, apppaths, cat = self.chrome_sources()
        merged = merge_apps([cat], [start, appx], [apppaths], case_insensitive_targets=False)
        assert [(a.name, a.id) for a in merged] == [("Google Chrome", "chrome"), ("chrome", None)]
        assert merged[0].aliases == ("כרום",)

    def test_merge_drops_invalid_rows(self) -> None:
        rows = [InstalledApp("", "x.exe", "exe"), InstalledApp("X", "", "exe"), InstalledApp("Y", "y", "weird")]  # type: ignore[arg-type]
        assert merge_apps(rows) == []

    def test_annotate_links_catalog_ids(self) -> None:
        apps = annotate_with_catalog([
            InstalledApp("Browser X", r"C:\Apps\chrome.exe", "exe", "apppaths"),
            InstalledApp("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "appx", "appx"),
            InstalledApp("Calculator", r"C:\Tools\foocalc.exe", "exe", "apppaths", process_names=("foocalc.exe",)),
            InstalledApp("Slack", r"C:\Users\me\AppData\Local\slack\Update.exe", "exe", "startmenu"),
            InstalledApp("Spotify Premium", "spotify:", "uri", "appx"),
            InstalledApp("Code", "code --new-window", "command", "desktop"),
            InstalledApp("Mine", "mine.exe", "exe", "catalog", id="notepad"),
        ])
        # exe evidence → chrome; name → calculator; an exe that contradicts the name is another app;
        # Squirrel's Update.exe says nothing (not Discord); URI scheme → spotify; command head → vscode.
        assert [a.id for a in apps] == ["chrome", "calculator", None, None, "spotify", "vscode", "notepad"]
        assert "CalculatorApp.exe" in apps[1].process_names and "מחשבון" in apps[1].aliases
        assert apps[6].process_names[0] == "notepad.exe"


class TestProcessNames:
    def test_windows_mapping(self, win_index: AppIndex) -> None:
        assert candidate_process_names("chrome", entry=catalog_lookup("chrome"), windows=True) == ["chrome.exe"]
        assert candidate_process_names("כרום", entry=catalog_lookup("כרום"), windows=True) == ["chrome.exe"]
        assert candidate_process_names("vs code", entry=catalog_lookup("vs code"), windows=True) == ["code.exe"]
        assert candidate_process_names("calculator", entry=catalog_lookup("calculator"), windows=True)[:2] == [
            "calculatorapp.exe", "calc.exe"]
        assert candidate_process_names("obsidian", app=win_index.find("obsidian"), windows=True) == ["obsidian.exe"]
        assert candidate_process_names("notepad++.exe", windows=True) == ["notepad++.exe"]
        assert candidate_process_names("my cool tool", windows=True) == []
        shortcut = InstalledApp("Slack", r"C:\U\Slack.lnk", "lnk", "startmenu")
        assert candidate_process_names("slack", app=shortcut, windows=True) == ["slack.exe"]
        wrapper = InstalledApp("Dev Tool", r"C:\Windows\System32\cmd.exe", "exe", "apppaths")
        assert candidate_process_names("dev tool", app=wrapper, windows=True) == []

    def test_posix_mapping(self) -> None:
        assert candidate_process_names("chrome", entry=catalog_lookup("chrome"), windows=False) == ["chrome"]
        flatpak = InstalledApp("Spotify", "flatpak run com.spotify.Client", "command", "desktop")
        assert candidate_process_names("spotify music", app=flatpak, windows=False) == []
        gedit = InstalledApp("Text Editor", "/usr/bin/gedit --new-window", "command", "desktop")
        assert candidate_process_names("text editor", app=gedit, windows=False) == ["gedit"]
        assert candidate_process_names("Obsidian", windows=False) == ["obsidian"]

    @pytest.mark.parametrize(
        ("command", "head"),
        [
            ("/usr/bin/gedit %U", "gedit"),
            ("notepad.exe", "notepad.exe"),
            (r'"C:\Program Files\App\app.exe" --flag', "app.exe"),
            (r"C:\Program Files\App\App.EXE --flag", "app.exe"),
            ("env FOO=1 bar", "env"),
            ("", ""),
        ],
    )
    def test_command_head(self, command: str, head: str) -> None:
        assert command_head(command) == head


# =============================================================================================
# Shared helpers
# =============================================================================================
class TestSharedHelpers:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("youtube.com", "https://youtube.com"),
            ("www.ynet.co.il/news", "https://www.ynet.co.il/news"),
            ("http://localhost:8000/x?y=1", "http://localhost:8000/x?y=1"),
            ("HTTPS://Example.com", "HTTPS://Example.com"),
        ],
    )
    def test_validate_web_url_accepts(self, url: str, expected: str) -> None:
        assert validate_web_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        ["", "ftp://example.com", "file:///etc/passwd", "javascript:alert(1)", "https://", "https://exa mple.com",
         "notepad", "C:\\Windows\\notepad.exe", "steam://run/1"],
    )
    def test_validate_web_url_rejects(self, url: str) -> None:
        with pytest.raises(HostUnsupported):
            validate_web_url(url)

    def test_screenshot_target(self, tmp_path: Path) -> None:
        in_folder = screenshot_target(tmp_path, now=0)
        assert in_folder.parent == tmp_path and in_folder.name.startswith("screenshot-") and in_folder.suffix == ".png"
        assert screenshot_target(tmp_path / "shot.jpg") == tmp_path / "shot.png"
        assert screenshot_target(tmp_path / "shot.PNG") == tmp_path / "shot.PNG"
        assert screenshot_target(tmp_path / "shot") == tmp_path / "shot.png"

    async def test_delayed_actions_run_and_cancel(self) -> None:
        done = asyncio.Event()
        ran: list[str] = []
        delayed = DelayedActions()

        async def action() -> None:
            ran.append("ran")
            done.set()

        async def broken() -> None:
            raise RuntimeError("boom")

        delayed.schedule(0.01, broken, "broken")            # a failing action is logged, not raised
        delayed.schedule(0.02, action, "quick")
        await asyncio.wait_for(done.wait(), 2)
        assert ran == ["ran"]
        delayed.schedule(30, action, "later")
        assert delayed.pending == 1
        assert delayed.cancel_all() == 1
        await asyncio.sleep(0)
        assert delayed.pending == 0 and ran == ["ran"]


# =============================================================================================
# Windows helpers (pure logic, any OS)
# =============================================================================================
class TestGetStartApps:
    ROWS = [
        {"Name": "Google Chrome", "AppID": "Chrome"},
        {"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
        {"Name": "Word", "AppID": "Microsoft.Office.WINWORD.EXE.15"},
        {"Name": "Notepad++", "AppID": "{6D809377-6AF0-444B-8957-A3773F02200E}\\Notepad++\\notepad++.exe"},
        {"Name": "Python 3.12 (64-bit)", "AppID": "{6D809377-6AF0-444B-8957-A3773F02200E}\\Python312\\python.exe"},
        {"Name": "Counter-Strike 2", "AppID": "steam://rungameid/730"},
        {"Name": "Portable Tool", "AppID": "C:\\Tools\\portable.exe"},
        {"Name": "Edge docs", "AppID": "https://learn.microsoft.com/edge"},
        {"Name": "Uninstall Foo", "AppID": "Foo.Uninstall"},
        {"Name": "", "AppID": "Nameless"},
        {"Name": "No id"},
    ]

    def test_array(self) -> None:
        apps = parse_start_apps(json.dumps(self.ROWS))
        by_name = {a.name: a for a in apps}
        assert list(by_name) == ["Google Chrome", "Calculator", "Word", "Notepad++", "Python 3.12 (64-bit)",
                                 "Counter-Strike 2", "Portable Tool"]
        assert all(a.source == "appx" for a in apps)
        assert (by_name["Google Chrome"].kind, by_name["Google Chrome"].target) == ("appx", "Chrome")
        assert by_name["Word"].process_names == ("winword.exe",)
        assert by_name["Notepad++"].process_names == ("notepad++.exe",)
        assert by_name["Python 3.12 (64-bit)"].process_names == ()      # python.exe is a launcher
        assert (by_name["Counter-Strike 2"].kind, by_name["Counter-Strike 2"].target) == ("uri", "steam://rungameid/730")
        assert (by_name["Portable Tool"].kind, by_name["Portable Tool"].process_names) == ("exe", ("portable.exe",))

    def test_single_object_bom_and_noise(self) -> None:
        one = json.dumps({"Name": "Spotify", "AppID": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"})
        for text in (one, "\ufeff" + one, "WARNING: profile not loaded\r\n" + one + "\r\n"):
            assert [(a.name, a.kind) for a in parse_start_apps(text)] == [("Spotify", "appx")]

    @pytest.mark.parametrize("text", ["", "   ", "null", "not json at all", "[{broken", '"just a string"', "[1, 2, null]"])
    def test_empty_or_invalid(self, text: str) -> None:
        assert parse_start_apps(text) == []


class TestShortcuts:
    def test_lnk_with_ansi_link_info(self) -> None:
        link = parse_lnk_bytes(build_lnk(
            target=r"C:\Program Files\Mozilla Firefox\firefox.exe", description="Firefox",
            working_dir=r"C:\Program Files\Mozilla Firefox", args="-P work",
        ))
        assert link is not None
        assert link.target == r"C:\Program Files\Mozilla Firefox\firefox.exe"
        assert (link.arguments, link.working_dir, link.description) == ("-P work", r"C:\Program Files\Mozilla Firefox",
                                                                        "Firefox")
        assert not link.advertised

    def test_lnk_with_unicode_link_info(self) -> None:
        target = r"C:\Program Files\אפליקציה\app.exe"
        link = parse_lnk_bytes(build_lnk(target=target, unicode_info=True))
        assert link is not None and link.target == target

    def test_environment_block_wins_when_it_expands(self) -> None:
        data = build_lnk(target=r"C:\Users\old\AppData\Local\Programs\App\app.exe",
                         env_target=r"%LOCALAPPDATA%\Programs\App\app.exe", working_dir=r"%LOCALAPPDATA%\Programs\App")
        link = parse_lnk_bytes(data, env={"LocalAppData": r"C:\Users\me\AppData\Local"})
        assert link is not None
        assert link.target == r"C:\Users\me\AppData\Local\Programs\App\app.exe"
        assert link.working_dir == r"C:\Users\me\AppData\Local\Programs\App"
        unresolved = parse_lnk_bytes(build_lnk(target=r"C:\A\a.exe", env_target=r"%NOPE%\a.exe"), env={})
        assert unresolved is not None and unresolved.target == r"C:\A\a.exe"
        only_env = parse_lnk_bytes(build_lnk(env_target=r"%NOPE%\a.exe"), env={})
        assert only_env is not None and only_env.target == r"%NOPE%\a.exe"

    def test_relative_path_and_force_no_link_info(self) -> None:
        link_dir = r"C:\Users\me\Start Menu\Programs\Sub"
        relative = parse_lnk_bytes(build_lnk(relative=r"..\..\Tools\tool.exe", id_list=False), link_dir=link_dir)
        assert relative is not None and relative.target == r"C:\Users\me\Start Menu\Tools\tool.exe"
        forced = parse_lnk_bytes(build_lnk(target=r"C:\A\a.exe", relative=r"b.exe", force_no_link_info=True),
                                 link_dir=r"C:\B")
        assert forced is not None and forced.target == r"C:\B\b.exe"

    def test_advertised_and_broken_shortcuts(self) -> None:
        advertised = parse_lnk_bytes(build_lnk(darwin=True))
        assert advertised is not None and advertised.advertised and advertised.target is None
        assert parse_lnk_bytes(b"not a shortcut") is None
        assert parse_lnk_bytes(b"\x4c\x00\x00\x00" + bytes(72)) is None                    # wrong CLSID
        assert parse_lnk_bytes(build_lnk(args="abcdef")[:-8]) is None                    # truncated StringData
        info_too_long = bytearray(build_lnk(target=r"C:\A\a.exe"))
        struct.pack_into("<I", info_too_long, 76 + 4, 0xFFFF)                             # LinkInfoSize past the end
        assert parse_lnk_bytes(bytes(info_too_long)) is None

    def test_link_process_name(self) -> None:
        assert link_process_name(ShellLink(r"C:\Users\me\AppData\Local\slack\Update.exe",
                                           '--processStart "slack.exe"')) == "slack.exe"
        assert link_process_name(ShellLink(r"C:\Users\me\AppData\Local\Discord\Update.exe",
                                           "--processStart Discord.exe --process-start-args x")) == "discord.exe"
        assert link_process_name(ShellLink(r"C:\Users\me\AppData\Local\App\Update.exe")) is None
        assert link_process_name(ShellLink(r"C:\Windows\System32\cmd.exe", "/c start x")) is None
        assert link_process_name(ShellLink(r"C:\Windows\System32\cmd.exe")) == "cmd.exe"
        assert link_process_name(ShellLink(r"C:\Program Files\App\App.EXE", "--flag")) == "app.exe"
        assert link_process_name(ShellLink(r"C:\Docs\manual.pdf")) is None
        assert link_process_name(None) is None

    def test_parse_url_shortcut(self) -> None:
        assert parse_url_shortcut("[InternetShortcut]\r\nURL=steam://rungameid/730\r\nIconIndex=0\r\n") == "steam://rungameid/730"
        text = "[{000214A0-0000-0000-C000-000000000046}]\nProp3=19,0\n[InternetShortcut]\nIDList=\nURL=https://example.com/\n"
        assert parse_url_shortcut(text) == "https://example.com/"
        assert parse_url_shortcut("[Other]\nURL=https://x.example\n") is None
        assert parse_url_shortcut("") is None

    def test_scan_start_menu(self, tmp_path: Path) -> None:
        programs = tmp_path / "Start Menu" / "Programs"

        def put(rel: str, data: bytes) -> None:
            path = programs / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        put("Google Chrome.lnk", build_lnk(target=r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
        put("Slack Technologies/Slack.lnk", build_lnk(target=r"C:\Users\me\AppData\Local\slack\Update.exe",
                                                      args='--processStart "slack.exe"'))
        put("Foo/Foo.lnk", build_lnk(target=r"C:\Program Files\Foo\foo.exe"))
        put("Foo/Uninstall Foo.lnk", build_lnk(target=r"C:\Program Files\Foo\unins000.exe"))
        put("Foo/Foo Readme.lnk", build_lnk(target=r"C:\Program Files\Foo\readme.txt"))
        put("Foo/Foo Docs.lnk", build_lnk(target=r"C:\Program Files\Foo\docs.pdf"))
        put("Foo/Foo Cleaner.lnk", build_lnk(target=r"C:\Program Files\Foo\unins001.exe"))
        put("Tools/Dev Shell.lnk", build_lnk(target=r"C:\Windows\System32\cmd.exe", args="/k vcvars64.bat"))
        put("Steam/Counter-Strike 2.url", b"[InternetShortcut]\r\nURL=steam://rungameid/730\r\n")
        put("Example Portal.url", b"[InternetShortcut]\r\nURL=https://example.com\r\n")
        put("Startup/Autostart Thing.lnk", build_lnk(target=r"C:\Program Files\Thing\thing.exe"))
        put("desktop.ini", b"[.ShellClassInfo]\r\n")
        put("Odd.lnk", b"not a shell link")              # unreadable: kept, ShellExecute may still open it
        apps = scan_start_menu([programs, tmp_path / "missing"])
        by_name = {a.name: a for a in apps}
        assert sorted(by_name) == ["Counter-Strike 2", "Dev Shell", "Foo", "Google Chrome", "Odd", "Slack"]
        assert all(a.kind == "lnk" and a.source == "startmenu" for a in apps)
        assert by_name["Google Chrome"].process_names == ("chrome.exe",)
        assert by_name["Slack"].process_names == ("slack.exe",)
        assert by_name["Dev Shell"].process_names == ()
        assert by_name["Foo"].target == str(programs / "Foo" / "Foo.lnk")
        assert by_name["Counter-Strike 2"].target == str(programs / "Steam" / "Counter-Strike 2.url")

    @pytest.mark.parametrize(
        ("name", "auxiliary"),
        [
            ("Uninstall Foo", True), ("Foo Readme", True), ("Release Notes", True), ("What's New", True),
            ("Foo Website", True), ("הסרת התקנה", True), ("Notepad++", False), ("Remote Desktop Connection", False),
            ("Microsoft Edge", False),
        ],
    )
    def test_auxiliary_names(self, name: str, auxiliary: bool) -> None:
        assert is_auxiliary_name(name) is auxiliary


class TestWindowsDiscovery:
    def test_app_paths_to_apps(self) -> None:
        chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        excel = r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"
        files = {chrome, excel, r"C:\Program Files\Mozilla Firefox\firefox.exe", r"C:\Tools\setup.exe",
                 r"C:\Tools\weird.exe", r"C:\Tools\generic.exe", r"C:\Tools\library.dll"}
        descriptions = {chrome: "Google Chrome", excel: "Microsoft Excel", r"C:\Tools\generic.exe": "Application"}

        def describe(path: str) -> str | None:
            if path.endswith("weird.exe"):
                raise OSError("no version resource")
            return descriptions.get(path)

        rows = [
            ("chrome.exe", f'"{chrome}"'),
            ("firefox.exe", r"C:\Program Files\Mozilla Firefox\firefox.exe"),
            ("excel.exe", r"%ProgramFiles%\Microsoft Office\root\Office16\EXCEL.EXE /safe"),
            ("setup.exe", r"C:\Tools\setup.exe"),
            ("weird.exe", r"C:\Tools\weird.exe"),
            ("generic.exe", r"C:\Tools\generic.exe"),
            ("missing.exe", r"C:\Nope\missing.exe"),
            ("library.dll", r"C:\Tools\library.dll"),
        ]
        apps = app_paths_to_apps(rows, isfile=files.__contains__, describe=describe,
                                 env={"ProgramFiles": r"C:\Program Files"})
        by_name = {a.name: a for a in apps}
        assert set(by_name) == {"Google Chrome", "firefox", "Microsoft Excel", "weird", "generic"}
        assert (by_name["Google Chrome"].target, by_name["Google Chrome"].aliases) == (chrome, ("chrome",))
        assert by_name["Google Chrome"].process_names == ("chrome.exe",)
        assert (by_name["Microsoft Excel"].target, by_name["Microsoft Excel"].process_names) == (excel, ("excel.exe",))
        assert by_name["firefox"].aliases == ()
        assert all(a.kind == "exe" and a.source == "apppaths" for a in apps)

    def test_prefer_real_exe(self) -> None:
        cmd = r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd"
        exe = r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\code.exe"
        assert prefer_real_exe(cmd, {exe}.__contains__) == exe
        assert prefer_real_exe(cmd, lambda _p: False) == cmd
        assert prefer_real_exe(exe, lambda _p: False) == exe

    def test_catalog_apps_windows(self) -> None:
        on_path = {
            "chrome.exe": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "code.cmd": r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd",
            "update.exe": r"C:\Somewhere\Update.exe",
        }
        real_code = r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\code.exe"
        apps = {a.id: a for a in catalog_apps_windows(which=lambda name: on_path.get(name.lower()),
                                                      uri_ok=lambda uri: uri == "ms-settings:",
                                                      isfile=lambda p: p.lower() == real_code.lower())}
        assert set(apps) == {"chrome", "vscode", "settings"}          # Update.exe on PATH is not Discord
        assert (apps["chrome"].kind, apps["chrome"].target) == ("exe", on_path["chrome.exe"])
        assert apps["vscode"].target == real_code
        assert (apps["settings"].kind, apps["settings"].target) == ("uri", "ms-settings:")
        assert all(a.source == "catalog" for a in apps.values())

    @not_windows
    def test_uri_registration_is_unknown_off_windows(self) -> None:
        assert windows.uri_registered("ms-settings:") is False
        assert windows.uri_registered("") is False
        assert windows.register_toast_app_id("JARVIS", "JARVIS") is False


class TestLaunchPlans:
    @staticmethod
    def plan(app: InstalledApp, args: Sequence[str] = (), *, on_path: dict[str, str] | None = None,
             link: ShellLink | None = None, files: Sequence[str] = (), subsystem: int = 2) -> LaunchPlan:
        paths = {k.lower(): v for k, v in (on_path or {}).items()}
        existing = {f.lower() for f in files}
        return plan_launch(app, args, which=lambda name: paths.get(name.lower()), read_link=lambda _p: link,
                           isfile=lambda p: p.lower() in existing, subsystem=lambda _p: subsystem)

    def test_store_app_goes_through_apps_folder(self) -> None:
        app = InstalledApp("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "appx", "appx")
        plan = self.plan(app)
        assert plan.method == "shell:AppsFolder" and plan.creationflags == GUI_LAUNCH_FLAGS
        assert plan.argv[0].lower().endswith("explorer.exe")
        assert plan.argv[1:] == ("shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",)
        assert plan.note == "" and self.plan(app, ["x"]).note != ""

    def test_uri_and_plain_shortcut_use_shell_execute(self) -> None:
        assert self.plan(InstalledApp("Settings", "ms-settings:", "uri")) == LaunchPlan("uri", target="ms-settings:")
        lnk = InstalledApp("Chrome", r"C:\SM\Google Chrome.lnk", "lnk", "startmenu")
        assert self.plan(lnk) == LaunchPlan("startfile", target=r"C:\SM\Google Chrome.lnk")

    def test_shortcut_with_arguments_runs_its_target(self) -> None:
        firefox = r"C:\Program Files\Mozilla Firefox\firefox.exe"
        link = ShellLink(firefox, '-P "work profile"', working_dir=r"C:\Program Files\Mozilla Firefox")
        app = InstalledApp("Firefox", r"C:\SM\Firefox.lnk", "lnk", "startmenu")
        plan = self.plan(app, ["https://example.com"], link=link, files=[firefox])
        assert plan.method == "popen" and plan.creationflags == GUI_LAUNCH_FLAGS
        assert plan.argv == (firefox, "-P", "work profile", "https://example.com")
        assert plan.cwd == r"C:\Program Files\Mozilla Firefox"
        unresolved = self.plan(app, ["a b", "c"], link=None)
        assert (unresolved.method, unresolved.target, unresolved.arguments) == ("startfile", app.target, '"a b" c')

    def test_executables(self) -> None:
        tool = r"C:\Tools\tool.exe"
        gui = self.plan(InstalledApp("Tool", tool, "exe"), ["--x"], files=[tool])
        assert (gui.method, gui.argv, gui.cwd, gui.creationflags) == ("popen", (tool, "--x"), r"C:\Tools", GUI_LAUNCH_FLAGS)
        console = self.plan(InstalledApp("Tool", tool, "exe"), files=[tool], subsystem=3)
        assert console.creationflags == CONSOLE_LAUNCH_FLAGS
        notepad = self.plan(InstalledApp("Notepad", "notepad.exe", "exe"), ["a.txt"],
                            on_path={"notepad.exe": r"C:\Windows\System32\notepad.exe"})
        assert notepad.argv == (r"C:\Windows\System32\notepad.exe", "a.txt")
        word = r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"
        missing = self.plan(InstalledApp("Word", word, "exe"), ["my doc.docx"])
        assert (missing.method, missing.target, missing.arguments) == ("startfile", word, '"my doc.docx"')

    def test_batch_launchers(self) -> None:
        cmd = r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd"
        app = InstalledApp("VS Code", "code.cmd", "command")
        batch = self.plan(app, ["notes.txt"], on_path={"code.cmd": cmd}, files=[cmd])
        assert (batch.method, batch.argv, batch.creationflags) == ("popen", (cmd, "notes.txt"), BATCH_LAUNCH_FLAGS)
        with pytest.raises(HostUnsupported):
            self.plan(app, ["a & calc"], on_path={"code.cmd": cmd}, files=[cmd])
        real = r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\code.exe"
        preferred = self.plan(app, ["a & b.txt"], on_path={"code.cmd": cmd}, files=[cmd, real])
        assert preferred.argv == (real, "a & b.txt") and preferred.creationflags == GUI_LAUNCH_FLAGS

    def test_unknown_kind(self) -> None:
        with pytest.raises(HostUnsupported):
            self.plan(InstalledApp("X", "x", "weird"))  # type: ignore[arg-type]


class TestWindowsCommandLines:
    def test_pe_subsystem_and_launch_flags(self, tmp_path: Path) -> None:
        gui, cui, pe32, junk, bad_magic = (tmp_path / n for n in ("gui.exe", "cui.exe", "pe32.exe", "junk.exe", "magic.exe"))
        gui.write_bytes(make_pe(2))
        cui.write_bytes(make_pe(3))
        pe32.write_bytes(make_pe(3, magic=0x10B))
        junk.write_bytes(b"MZ" + bytes(10))
        bad_magic.write_bytes(make_pe(2, magic=0x1234))
        assert pe_subsystem(str(gui)) == 2 and pe_subsystem(str(cui)) == 3 and pe_subsystem(str(pe32)) == 3
        assert pe_subsystem(str(junk)) is None and pe_subsystem(str(bad_magic)) is None
        assert pe_subsystem(str(tmp_path / "missing.exe")) is None
        assert launch_flags_for(str(gui)) == GUI_LAUNCH_FLAGS
        assert launch_flags_for(str(cui)) == CONSOLE_LAUNCH_FLAGS
        assert launch_flags_for(str(junk)) == GUI_LAUNCH_FLAGS
        assert launch_flags_for(str(tmp_path / "run.CMD")) == BATCH_LAUNCH_FLAGS

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ('"a b c" d e', ["a b c", "d", "e"]),
            (r'"ab\"c" "\\" d', ['ab"c', "\\", "d"]),
            (r'a\\\b d"e f"g h', [r"a\\\b", "de fg", "h"]),
            (r"a\\\"b c d", [r"a\"b", "c", "d"]),
            (r'a\\\\"b c" d e', [r"a\\b c", "d", "e"]),
            ('a"b"" c d', ['ab" c d']),
            ('  --flag   "" x  ', ["--flag", "", "x"]),
            ("", []),
        ],
    )
    def test_split_windows_args(self, line: str, expected: list[str]) -> None:
        assert split_windows_args(line) == expected

    def test_reveal_command_line(self) -> None:
        assert reveal_command_line(r"C:\Users\me\My File.txt") == 'explorer.exe /select,"C:\\Users\\me\\My File.txt"'
        assert reveal_command_line(r"C:\x.txt", r"C:\Win Dir\explorer.exe") == '"C:\\Win Dir\\explorer.exe" /select,"C:\\x.txt"'
        with pytest.raises(HostUnsupported):
            reveal_command_line('C:\\bad"name.txt')

    def test_expand_windows_vars(self) -> None:
        env = {"ProgramFiles": r"C:\Program Files", "USERPROFILE": r"C:\Users\me"}
        assert expand_windows_vars(r"%PROGRAMFILES%\App;%userprofile%", env) == r"C:\Program Files\App;C:\Users\me"
        assert expand_windows_vars(r"%NOPE%\x.exe", env) == r"%NOPE%\x.exe"
        assert expand_windows_vars("100% sure", env) == "100% sure"

    def test_volume_key_math(self) -> None:
        assert volume_key_plan(level=50) == [(VK_DOWN, 50), (VK_UP, 25)]
        assert volume_key_plan(level=0) == [(VK_DOWN, 50)]
        assert volume_key_plan(level=100) == [(VK_DOWN, 50), (VK_UP, 50)]
        assert volume_key_plan(level=33) == [(VK_DOWN, 50), (VK_UP, 17)]         # 2 % per press, rounded up
        assert volume_key_plan(level=150) == volume_key_plan(level=100)
        assert volume_key_plan(level=-5) == volume_key_plan(level=0)
        assert volume_key_plan(delta=10) == [(VK_UP, 5)]
        assert volume_key_plan(delta=-5) == [(VK_DOWN, 3)]
        assert volume_key_plan(delta=1) == [(VK_UP, 1)]
        assert volume_key_plan(delta=500) == [(VK_UP, 50)]
        assert volume_key_plan(delta=0) == []

    def test_mute_key_plan_is_deterministic(self) -> None:
        assert mute_key_plan(True) == [(VK_UP, 1), (VK_DOWN, 1), (VK_MUTE, 1)]
        assert mute_key_plan(False) == [(VK_UP, 1), (VK_DOWN, 1)]

    def test_power_argv(self) -> None:
        assert power_argv("shutdown", 60) == ["shutdown.exe", "/s", "/t", "60"]
        assert power_argv("restart") == ["shutdown.exe", "/r", "/t", "0"]
        assert power_argv("restart", -5) == ["shutdown.exe", "/r", "/t", "0"]
        assert power_argv("shutdown", 10**12)[-1] == str(windows.MAX_SHUTDOWN_DELAY_S)
        assert power_argv("logoff", 30) == ["shutdown.exe", "/l"]
        with pytest.raises(HostUnsupported):
            power_argv("sleep")

    def test_toast_xml_is_escaped(self) -> None:
        xml = toast_xml("A & B <c>", 'He said "hi" & \x07bye')
        assert "A &amp; B &lt;c&gt;" in xml and "&quot;hi&quot;" in xml and "\x07" not in xml
        assert [t.text for t in ET.fromstring(xml).iter("text")] == ["A & B <c>", 'He said "hi" & bye']
        assert [t.text for t in ET.fromstring(toast_xml("", "x")).iter("text")] == ["JARVIS", "x"]

    def test_powershell_scripts(self) -> None:
        assert powershell_argv("Get-Date", exe="pwsh.exe") == [
            "pwsh.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", PS_UTF8_PREFIX + "Get-Date"]
        assert "Brightness=[byte]100" in windows.brightness_set_ps(250)
        assert "Brightness=[byte]0" in windows.brightness_set_ps(-3)
        assert "$env:JARVIS_SHOT_PATH" in windows.SCREENSHOT_PS
        assert "$env:JARVIS_TOAST_XML" in windows.TOAST_PS and windows.POWERSHELL_AUMID in windows.TOAST_PS


# =============================================================================================
# WindowsHost behaviour (fakes; runs on any OS)
# =============================================================================================
class TestWindowsHost:
    def test_identity_shells_and_cache_location(self, win_host: tuple[WindowsHost, FakeRunner],
                                                monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        host, _ = win_host
        assert host.platform == "windows" and host.default_shell == "powershell"
        assert host.shell_argv("Get-Date") == [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;Get-Date",
        ]
        assert host.shell_argv("dir", "cmd") == ["cmd.exe", "/d", "/s", "/c", "dir"]
        monkeypatch.setattr(windows.shutil, "which", fake_which("pwsh", "bash"))
        assert host.shell_argv("ls", "pwsh")[0] == "/usr/bin/pwsh"
        assert host.shell_argv("ls", "bash") == ["/usr/bin/bash", "-lc", "ls"]
        monkeypatch.setattr(windows.shutil, "which", fake_which())
        assert host.shell_argv("ls", "pwsh")[0] == "powershell.exe"               # falls back to Windows PowerShell
        with pytest.raises(HostUnsupported):
            host.shell_argv("ls", "bash")
        with pytest.raises(HostUnsupported):
            host.shell_argv("ls", "fish")
        assert WindowsHost(cache_dir=tmp_path)._apps.cache.path == tmp_path / "app_index_windows.json"
        assert WindowsHost()._apps.cache.path is None

    async def test_find_and_search_apps(self, win_host: tuple[WindowsHost, FakeRunner]) -> None:
        host, _ = win_host
        chrome = await host.find_app("כרום")
        assert chrome is not None and chrome.name == "Google Chrome"
        assert await host.find_app("excel") is None
        assert await host.find_app("") is None
        assert next(a.name for a in await host.search_apps("phot")) == "Adobe Photoshop 2024"
        assert len(await host.installed_apps()) == len(await host.installed_apps(refresh=True)) > 10

    @pytest.mark.parametrize("name", ["svchost", "lsass.exe", "csrss", "winlogon.exe", "dwm"])
    async def test_close_app_refuses_critical_processes(self, win_host: tuple[WindowsHost, FakeRunner],
                                                        monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        host, runner = win_host

        def must_not_scan(targets: Sequence[str]) -> list[Any]:
            raise AssertionError(f"looked for {targets}")

        monkeypatch.setattr(host, "_find_processes", must_not_scan)
        with pytest.raises(PermissionDenied):
            await host.close_app(name)
        assert runner.calls == []

    @pytest.mark.parametrize("name", ["explorer", "File Explorer", "סייר הקבצים"])
    async def test_closing_explorer_closes_its_windows_not_the_shell(
        self, win_host: tuple[WindowsHost, FakeRunner], monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        host, runner = win_host
        seen: dict[str, Any] = {}

        def close_windows(**kwargs: Any) -> int:
            seen.update(kwargs)
            return 2

        monkeypatch.setattr(winapi, "close_windows", close_windows)
        assert await host.close_app(name) == ["explorer.exe"]
        assert seen["class_names"] == EXPLORER_WINDOW_CLASSES and runner.calls == []
        monkeypatch.setattr(winapi, "close_windows", lambda **_kw: 0)
        assert await host.close_app(name) == []

    async def test_close_app_is_graceful_first_and_kills_only_with_force(
        self, win_host: tuple[WindowsHost, FakeRunner], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host, runner = win_host
        procs = [FakeProc(101, "chrome.exe"), FakeProc(102, "chrome.exe")]
        asked: list[list[str]] = []

        def find(targets: Sequence[str]) -> list[FakeProc]:
            asked.append(list(targets))
            return procs

        monkeypatch.setattr(host, "_find_processes", find)
        monkeypatch.setattr(psutil, "wait_procs", fake_wait_procs({101}))
        assert await host.close_app("כרום") == ["chrome.exe"]
        assert asked == [["chrome.exe"]]
        assert runner.argvs == [["taskkill.exe", "/PID", "101", "/PID", "102"]]   # no /F: WM_CLOSE first
        assert not any(p.killed for p in procs)
        monkeypatch.setattr(psutil, "wait_procs", fake_wait_procs(set(), {101, 102}))
        assert await host.close_app("chrome", force=True) == ["chrome.exe"]
        assert all(p.killed for p in procs)

    async def test_close_app_with_nothing_running(self, win_host: tuple[WindowsHost, FakeRunner],
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = win_host
        monkeypatch.setattr(host, "_find_processes", lambda _targets: [])
        assert await host.close_app("obsidian") == []
        assert await host.close_app("   ") == []
        assert runner.calls == []

    def test_find_processes_never_returns_jarvis_itself(self) -> None:
        found = WindowsHost()._find_processes([psutil.Process().name()])
        assert os.getpid() not in {p.pid for p in found}
        assert WindowsHost()._find_processes(["definitely-not-running-4242.exe"]) == []

    async def test_volume_uses_core_audio(self, win_host: tuple[WindowsHost, FakeRunner],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        host, _ = win_host
        level = {"value": 30}

        def set_master(value: int) -> int:
            level["value"] = value
            return value

        def no_keys(plan: Any) -> int:
            raise AssertionError("key presses must not be used when Core Audio works")

        monkeypatch.setattr(winapi, "get_master_volume", lambda: level["value"])
        monkeypatch.setattr(winapi, "set_master_volume", set_master)
        monkeypatch.setattr(winapi, "press_keys", no_keys)
        assert await host.get_volume() == 30
        assert await host.set_volume(55) == 55
        assert await host.change_volume(10) == 65
        assert await host.change_volume(50) == 100
        assert await host.change_volume(-200) == 0

    async def test_volume_falls_back_to_key_presses(self, win_host: tuple[WindowsHost, FakeRunner],
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
        host, _ = win_host
        presses: list[list[tuple[int, int]]] = []

        def no_core_audio(*_args: Any) -> Any:
            raise OSError("Core Audio unavailable")

        def press_keys(plan: Sequence[tuple[int, int]]) -> int:
            presses.append(list(plan))
            return sum(n for _vk, n in plan)

        for name in ("get_master_volume", "set_master_volume", "set_master_mute"):
            monkeypatch.setattr(winapi, name, no_core_audio)
        monkeypatch.setattr(winapi, "press_keys", press_keys)
        assert await host.get_volume() is None
        assert await host.set_volume(50) == 50
        assert presses[-1] == [(VK_DOWN, 50), (VK_UP, 25)]
        assert await host.set_volume(0) == 0
        assert presses[-1] == [(VK_DOWN, 50)]
        assert await host.change_volume(10) is None
        assert presses[-1] == [(VK_UP, 5)]
        assert await host.change_volume(-5) is None
        assert presses[-1] == [(VK_DOWN, 3)]
        await host.set_mute(True)
        assert presses[-1] == [(VK_UP, 1), (VK_DOWN, 1), (VK_MUTE, 1)]
        await host.set_mute(False)
        assert presses[-1] == [(VK_UP, 1), (VK_DOWN, 1)]

    @not_windows
    async def test_off_windows_audio_and_win32_report_unsupported(self, win_host: tuple[WindowsHost, FakeRunner]) -> None:
        host, _ = win_host
        assert await host.get_volume() is None
        with pytest.raises(HostUnsupported):
            await host.set_volume(20)
        with pytest.raises(HostUnsupported):
            await host.lock_screen()
        with pytest.raises(HostUnsupported):
            await host.get_clipboard()
        with pytest.raises(HostUnsupported):
            await host.media("next")

    async def test_brightness_through_wmi(self, win_host: tuple[WindowsHost, FakeRunner]) -> None:
        host, runner = win_host
        runner.answers[("powershell.exe",)] = proc_result("70\r\n")
        assert await host.get_brightness() == 70
        assert await host.set_brightness(140) == 100
        script = runner.argvs[-1][-1]
        assert script.startswith(PS_UTF8_PREFIX) and "WmiSetBrightness" in script and "Brightness=[byte]100" in script

    async def test_brightness_unsupported(self, win_host: tuple[WindowsHost, FakeRunner]) -> None:
        host, runner = win_host
        runner.answers[("powershell.exe",)] = proc_result(code=3, stderr="Get-CimInstance : Not supported")
        with pytest.raises(HostUnsupported, match="brightness"):
            await host.get_brightness()
        with pytest.raises(HostUnsupported, match="brightness"):
            await host.set_brightness(50)
        runner.answers[("powershell.exe",)] = proc_result("")
        with pytest.raises(HostUnsupported):
            await host.get_brightness()

    async def test_media_keys(self, win_host: tuple[WindowsHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, _ = win_host
        presses: list[list[tuple[int, int]]] = []
        monkeypatch.setattr(winapi, "press_keys", lambda plan: presses.append(list(plan)) or 1)
        await host.media("next")
        await host.media("play_pause")
        assert presses == [[(winapi.VK_MEDIA_NEXT_TRACK, 1)], [(winapi.VK_MEDIA_PLAY_PAUSE, 1)]]
        with pytest.raises(HostUnsupported):
            await host.media("rewind")  # type: ignore[arg-type]

    async def test_power_commands(self, win_host: tuple[WindowsHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = win_host
        slept: list[dict[str, Any]] = []
        monkeypatch.setattr(winapi, "suspend", lambda **kw: slept.append(kw))
        await host.power("shutdown", 60)
        await host.power("restart")
        await host.power("logoff")
        await host.power("sleep")
        assert runner.argvs == [["shutdown.exe", "/s", "/t", "60"], ["shutdown.exe", "/r", "/t", "0"],
                                ["shutdown.exe", "/l"]]
        assert slept == [{}]
        with pytest.raises(HostUnsupported):
            await host.power("hibernate")  # type: ignore[arg-type]

    async def test_delayed_sleep_and_logoff_are_cancellable(self, win_host: tuple[WindowsHost, FakeRunner],
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = win_host
        slept: list[dict[str, Any]] = []
        monkeypatch.setattr(winapi, "suspend", lambda **kw: slept.append(kw))
        runner.answers[("shutdown.exe", "/a")] = proc_result(
            code=1116, stderr="Unable to abort the system shutdown because no shutdown was in progress.(1116)")
        await host.power("sleep", 600)
        await host.power("logoff", 600)
        assert host._delayed.pending == 2 and runner.calls == []
        await host.cancel_power()                            # 1116 (nothing to abort) is not an error
        assert host._delayed.pending == 0 and slept == []
        assert runner.argvs == [["shutdown.exe", "/a"]]

    async def test_power_failure_is_reported(self, win_host: tuple[WindowsHost, FakeRunner]) -> None:
        host, runner = win_host
        runner.answers[("shutdown.exe",)] = proc_result(code=1190, stderr="A system shutdown has already been scheduled.")
        with pytest.raises(HostUnsupported, match="shutdown"):
            await host.power("shutdown", 30)

    async def test_notify_passes_escaped_toast_xml_through_the_environment(
        self, win_host: tuple[WindowsHost, FakeRunner]
    ) -> None:
        host, runner = win_host
        await host.notify("Build <done>", 'Tests & "lint" passed')
        argv, kwargs = runner.calls[-1]
        env = kwargs["extra_env"]
        assert argv[0] == "powershell.exe" and "ToastNotificationManager" in argv[-1]
        assert "Build" not in argv[-1]                       # user text never becomes PowerShell code
        assert [t.text for t in ET.fromstring(env["JARVIS_TOAST_XML"]).iter("text")] == [
            "Build <done>", 'Tests & "lint" passed']
        assert env["JARVIS_TOAST_APP"] == ""                 # unregistered → PowerShell's AppUserModelID
        runner.answers[("powershell.exe",)] = proc_result(code=3, stderr="toast failed")
        with pytest.raises(HostUnsupported):
            await host.notify("x", "y")

    async def test_screenshot_with_pillow_then_powershell(self, win_host: tuple[WindowsHost, FakeRunner],
                                                         monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        host, runner = win_host
        monkeypatch.setattr(windows, "_grab_with_pillow", write_png)
        assert await host.screenshot(tmp_path / "shot.jpg") == png_path(tmp_path / "shot.jpg")
        assert runner.calls == []
        monkeypatch.setattr(windows, "_grab_with_pillow", fail_grab)
        runner.answers[("powershell.exe",)] = write_shot_from_env
        target = await host.screenshot(tmp_path / "second")
        assert target == png_path(tmp_path / "second")
        assert "CopyFromScreen" in runner.argvs[-1][-1]
        assert runner.calls[-1][1]["extra_env"]["JARVIS_SHOT_PATH"] == str(target)
        runner.answers[("powershell.exe",)] = proc_result(code=1, stderr="no desktop")
        with pytest.raises(HostUnsupported):
            await host.screenshot(tmp_path / "third.png")

    async def test_clipboard_and_lock_use_win32(self, win_host: tuple[WindowsHost, FakeRunner],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
        host, _ = win_host
        board = {"text": "old"}
        locked: list[bool] = []
        monkeypatch.setattr(winapi, "get_clipboard_text", lambda: board["text"])
        monkeypatch.setattr(winapi, "set_clipboard_text", lambda text: board.update(text=text))
        monkeypatch.setattr(winapi, "lock_workstation", lambda: locked.append(True))
        await host.set_clipboard("שלום JARVIS")
        assert await host.get_clipboard() == "שלום JARVIS"
        await host.lock_screen()
        assert locked == [True]

    async def test_running_apps_groups_windows_by_process(self, win_host: tuple[WindowsHost, FakeRunner],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
        host, _ = win_host
        me = os.getpid()
        found = [
            winapi.WindowInfo(1, me, "Editor — a.txt", "Notepad"),
            winapi.WindowInfo(2, me, "Editor — b.txt", "Notepad"),
            winapi.WindowInfo(3, 0, "no process", "X"),
            winapi.WindowInfo(4, 2**31 - 2, "already gone", "Y"),
        ]
        monkeypatch.setattr(winapi, "list_windows", lambda: found)
        apps = await host.running_apps()
        assert [(a["pid"], a["windows"], a["title"]) for a in apps] == [(me, 2, "Editor — a.txt")]
        assert apps[0]["name"] == psutil.Process().name() and apps[0]["memory_mb"] > 0

    async def test_running_apps_falls_back_to_processes(self, win_host: tuple[WindowsHost, FakeRunner],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
        host, _ = win_host
        monkeypatch.setattr(winapi, "list_windows", lambda: [])
        rows = await host.running_apps()
        assert rows and all({"name", "pid", "memory_mb"} <= set(r) for r in rows)

    def test_execute_plan_starts_detached_processes_with_a_clean_environment(
        self, win_host: tuple[WindowsHost, FakeRunner], popen_calls: list[tuple[Any, dict[str, Any]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        host, _ = win_host
        monkeypatch.setenv("JARVIS_TEST_API_TOKEN", "s3cret")
        app = InstalledApp("Tool", r"C:\Tools\tool.exe", "exe")
        plan = LaunchPlan("popen", argv=(r"C:\Tools\tool.exe", "--x"), cwd=r"C:\Tools", creationflags=GUI_LAUNCH_FLAGS)
        result = host._execute_plan(app, plan)
        assert (result.ok, result.pid, result.method) == (True, 4242, "popen")
        argv, kwargs = popen_calls[-1]
        assert argv == [r"C:\Tools\tool.exe", "--x"]
        assert kwargs["creationflags"] == GUI_LAUNCH_FLAGS and kwargs["close_fds"] is True and kwargs["cwd"] == r"C:\Tools"
        assert "JARVIS_TEST_API_TOKEN" not in kwargs["env"]

    def test_elevation_falls_back_to_shell_execute(self, win_host: tuple[WindowsHost, FakeRunner],
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   startfile_calls: list[tuple[str, str, str | None]]) -> None:
        host, _ = win_host

        def needs_admin(argv: Any, **kwargs: Any) -> Any:
            exc = OSError(13, "The requested operation requires elevation")
            exc.winerror = 740  # type: ignore[attr-defined]
            raise exc

        monkeypatch.setattr(subprocess, "Popen", needs_admin)
        plan = LaunchPlan("popen", argv=(r"C:\Tools\admin tool.exe", "a b"), cwd=r"C:\Tools")
        result = host._execute_plan(InstalledApp("Admin", r"C:\Tools\admin tool.exe", "exe"), plan)
        assert result.ok and result.method == "startfile"
        assert startfile_calls == [(r"C:\Tools\admin tool.exe", '"a b"', r"C:\Tools")]

    async def test_launch_store_app_and_report_missing_programs(
        self, win_host: tuple[WindowsHost, FakeRunner], popen_calls: list[tuple[Any, dict[str, Any]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        host, _ = win_host
        result = await host.launch_app(InstalledApp("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                                                    "appx", "appx"))
        assert result.ok and result.method == "shell:AppsFolder" and result.pid is None
        assert result.extra == {"app_id": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "launcher_pid": 4242}
        assert popen_calls[-1][0] == ["explorer.exe", "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"]

        def missing(argv: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError(2, "The system cannot find the file specified")

        monkeypatch.setattr(subprocess, "Popen", missing)
        failed = await host.launch_app(InstalledApp("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "appx"))
        assert not failed.ok and "not found" in failed.detail

    async def test_open_url_only_opens_web_links(self, win_host: tuple[WindowsHost, FakeRunner],
                                                 startfile_calls: list[tuple[str, str, str | None]]) -> None:
        host, _ = win_host
        await host.open_url("youtube.com")
        await host.open_url("https://example.com/a?b=1")
        assert [c[0] for c in startfile_calls] == ["https://youtube.com", "https://example.com/a?b=1"]
        for bad in ("ftp://example.com", "file:///C:/Windows/win.ini", "javascript:alert(1)", "ms-settings:"):
            with pytest.raises(HostUnsupported):
                await host.open_url(bad)
        assert len(startfile_calls) == 2

    async def test_open_path_and_reveal(self, win_host: tuple[WindowsHost, FakeRunner], sample_file: Path,
                                        popen_calls: list[tuple[Any, dict[str, Any]]],
                                        startfile_calls: list[tuple[str, str, str | None]]) -> None:
        host, _ = win_host
        await host.open_path(sample_file)
        assert startfile_calls == [(str(sample_file), "", None)]
        await host.reveal(sample_file)
        line, kwargs = popen_calls[-1]
        assert line == f'explorer.exe /select,"{sample_file}"' and kwargs["creationflags"] == GUI_LAUNCH_FLAGS
        with pytest.raises(FileNotFoundError):
            await host.open_path(sample_file.with_name("missing.txt"))
        with pytest.raises(FileNotFoundError):
            await host.reveal(sample_file.with_name("missing.txt"))


@pytest.fixture
def start_menu_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An %APPDATA% Start menu with a couple of real-format shortcuts."""
    programs = tmp_path / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    (programs / "Visual Studio Code").mkdir(parents=True)
    (programs / "Google Chrome.lnk").write_bytes(build_lnk(target=r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
    (programs / "Visual Studio Code" / "Visual Studio Code.lnk").write_bytes(
        build_lnk(target=r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\Code.exe"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.delenv("ProgramData", raising=False)
    return programs


@not_windows
async def test_windows_host_indexes_start_menu_shortcuts(start_menu_env: Path, tmp_path: Path) -> None:
    host = WindowsHost(cache_dir=tmp_path / "cache")
    names = {a.name for a in await host.installed_apps()}
    assert {"Google Chrome", "Visual Studio Code"} <= names
    chrome = await host.find_app("כרום")
    assert chrome is not None and (chrome.kind, chrome.id) == ("lnk", "chrome")
    code = await host.find_app("vs code")
    assert code is not None and code.process_names[0] == "code.exe"
    assert host._apps.cache.load() is not None


# =============================================================================================
# POSIX helpers
# =============================================================================================
class TestDesktopEntries:
    def test_parse_desktop_entry(self) -> None:
        text = (
            "# comment\n[Desktop Entry]\nType=Application\nName=Visual Studio Code\nName[he]=ויז'ואל סטודיו קוד\n"
            "Name[de_DE]=Code\nExec=/usr/share/code/code --unity-launch %F\nTryExec=code\nTerminal=false\n"
            "[Desktop Action new-empty-window]\nName=New Empty Window\nExec=/usr/share/code/code --new-window %F\n"
        )
        entry = parse_desktop_entry(text)
        assert entry is not None and entry.name == "Visual Studio Code"
        assert entry.localized("he") == "ויז'ואל סטודיו קוד" and entry.localized("de") == "Code"
        assert entry.localized("fr") is None
        assert (entry.exec, entry.try_exec, entry.terminal) == ("/usr/share/code/code --unity-launch %F", "code", False)
        assert parse_desktop_entry("[Desktop Entry]\nType=Application\n") is None
        assert parse_desktop_entry("Name=No group\n") is None

    @pytest.mark.parametrize(
        ("exec_value", "argv"),
        [
            ("gedit %U", ["gedit"]),
            ('"/opt/My App/app" --name="a b" %f', ["/opt/My App/app", "--name=a b"]),
            ("printf 100%% %c", ["printf", "100%"]),
            ("/usr/bin/flatpak run --branch=stable --command=spotify com.spotify.Client @@u %U @@",
             ["/usr/bin/flatpak", "run", "--branch=stable", "--command=spotify", "com.spotify.Client"]),
            ("app --icon %i --caption %c --x%u", ["app", "--icon", "--caption", "--x"]),
            ("", []),
        ],
    )
    def test_exec_field_codes(self, exec_value: str, argv: list[str]) -> None:
        assert desktop_exec_argv(exec_value) == argv

    def test_desktop_app_filters_entries(self) -> None:
        which = fake_which("gedit", "flatpak")

        def entry(**overrides: Any) -> DesktopEntry:
            values: dict[str, Any] = {"name": "Text Editor", "exec": "gedit %U"}
            values.update(overrides)
            return DesktopEntry(**values)

        app = desktop_app(entry(names={"he": "עורך טקסט"}), which=which)
        assert app == InstalledApp("Text Editor", "gedit", "command", "desktop", process_names=("gedit",),
                                   aliases=("עורך טקסט",))
        for hidden in (entry(no_display=True), entry(hidden=True), entry(terminal=True), entry(type="Link"),
                       entry(try_exec="missing-tool"), entry(exec="missing-tool --x"), entry(exec="")):
            assert desktop_app(hidden, which=which) is None
        flatpak = desktop_app(entry(exec="flatpak run org.gimp.GIMP @@ %f @@"), which=which)
        assert flatpak is not None and flatpak.target == "flatpak run org.gimp.GIMP" and flatpak.process_names == ()

    def test_user_entries_override_system_ones(self, tmp_path: Path) -> None:
        user, system = tmp_path / "user", tmp_path / "system"
        write_file(system / "gedit.desktop", "[Desktop Entry]\nType=Application\nName=Gedit System\nExec=gedit\n")
        write_file(user / "gedit.desktop", "[Desktop Entry]\nType=Application\nName=Gedit Mine\nExec=gedit --new-window\n")
        write_file(system / "kde" / "kate.desktop", "[Desktop Entry]\nType=Application\nName=Kate\nExec=kate %U\n")
        write_file(user / "vlc.desktop", "[Desktop Entry]\nName=VLC\nExec=vlc\nHidden=true\n")
        write_file(system / "vlc.desktop", "[Desktop Entry]\nType=Application\nName=VLC media player\nExec=vlc %U\n")
        apps = scan_desktop_files([user, system, tmp_path / "missing"], which=fake_which("gedit", "kate", "vlc"))
        assert sorted((a.name, a.target) for a in apps) == [("Gedit Mine", "gedit --new-window"), ("Kate", "kate")]

    def test_desktop_dirs(self, tmp_path: Path) -> None:
        dirs = posix.desktop_dirs({"XDG_DATA_HOME": str(tmp_path / "data"), "XDG_DATA_DIRS": "/usr/share:/opt/share:"},
                                  home=tmp_path)
        assert dirs[:3] == [tmp_path / "data" / "applications", Path("/usr/share/applications"),
                            Path("/opt/share/applications")]
        assert Path("/var/lib/flatpak/exports/share/applications") in dirs
        assert len(dirs) == len(set(dirs))

    def test_catalog_apps_posix(self) -> None:
        apps = {a.id: a for a in posix.catalog_apps_posix(which=fake_which("google-chrome", "xdg-open", "libreoffice"))}
        assert apps["chrome"].target == "google-chrome"
        assert apps["chrome"].process_names == ("chrome.exe", "chrome", "google-chrome")
        assert apps["explorer"].process_names == ("explorer.exe",)        # xdg-open only launches things
        assert apps["word"].target == "libreoffice --writer"
        assert "firefox" not in apps


class TestPosixParsers:
    def test_parse_user_dirs(self) -> None:
        text = ('# written by xdg-user-dirs-update\nXDG_DESKTOP_DIR="$HOME/Escritorio"\nXDG_DOWNLOAD_DIR="$HOME/הורדות"\n'
                'XDG_MUSIC_DIR="/data/music"\nXDG_FOO_DIR="$HOME/foo"\nXDG_VIDEOS_DIR=relative\n')
        assert parse_user_dirs(text, Path("/home/me")) == {
            "desktop": Path("/home/me/Escritorio"), "downloads": Path("/home/me/הורדות"), "music": Path("/data/music")}

    def test_tool_output_parsers(self) -> None:
        assert parse_percent(PACTL_VOLUME) == 50
        assert parse_percent("no number") is None and parse_percent("150%") == 100
        assert parse_amixer(AMIXER_MASTER) == (42, False)
        assert parse_amixer("Front Left: Playback 0 [0%] [off]") == (0, True)
        assert parse_amixer("") == (None, None)
        assert parse_brightnessctl("intel_backlight,backlight,12000,50%,24000\n") == 50
        assert parse_brightnessctl("Device 'acpi_video0' of class 'backlight':\n\tCurrent brightness: 7 (47%)") == 47
        assert parse_brightnessctl("") is None
        wmctrl = ("0x01e00003  0 4242 host Firefox — Mozilla Firefox\n0x02a00001 -1 1111 host xfce4-panel\n"
                  "0x03000007  1 0 host No pid\ngarbage\n")
        assert parse_wmctrl(wmctrl) == [(4242, "Firefox — Mozilla Firefox")]

    def test_power_argv_posix(self) -> None:
        which = fake_which("systemctl", "loginctl", "gnome-session-quit")
        assert power_argv_posix("shutdown", macos=False, which=which) == ["systemctl", "poweroff"]
        assert power_argv_posix("restart", macos=False, which=which) == ["systemctl", "reboot"]
        assert power_argv_posix("sleep", macos=False, which=which) == ["systemctl", "suspend"]
        assert power_argv_posix("logoff", macos=False, which=which, env={"XDG_SESSION_ID": "3"}) == [
            "loginctl", "terminate-session", "3"]
        assert power_argv_posix("logoff", macos=False, which=which, env={}) == [
            "gnome-session-quit", "--logout", "--no-prompt"]
        assert power_argv_posix("restart", macos=True) == ["osascript", "-e", 'tell application "System Events" to restart']
        with pytest.raises(HostUnsupported):
            power_argv_posix("shutdown", macos=False, which=fake_which())
        with pytest.raises(HostUnsupported):
            power_argv_posix("hibernate", macos=False, which=which)
        with pytest.raises(HostUnsupported):
            power_argv_posix("logoff", macos=False, which=fake_which(), env={})

    @not_windows
    def test_feed_detached_pipes_data_and_reports_failure(self) -> None:
        assert posix._feed_detached(["cat"], b"clipboard text") == 0
        assert posix._feed_detached(["false"], b"") != 0


# =============================================================================================
# PosixHost behaviour (fake run_process / PATH)
# =============================================================================================
class TestPosixHost:
    def test_identity_shells_and_cache(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Path) -> None:
        host, _ = px
        assert host.platform == "posix" and host.default_shell == "bash"
        assert host.shell_argv("ls -la") == ["bash", "-lc", "ls -la"]
        assert host.shell_argv("ls", "sh") == ["sh", "-c", "ls"]
        assert host.shell_argv("ls", "zsh") == ["zsh", "-lc", "ls"]
        with pytest.raises(HostUnsupported):
            host.shell_argv("Get-Date", "powershell")
        with pytest.raises(HostUnsupported):
            host.shell_argv("dir", "cmd")
        use_tools(monkeypatch, "pwsh")
        assert host.shell_argv("Get-Date", "pwsh") == ["/usr/bin/pwsh", "-NoProfile", "-NonInteractive", "-Command",
                                                       "Get-Date"]
        cache = PosixHost(cache_dir=tmp_path)._apps.cache.path
        assert cache is not None and cache.parent == tmp_path and cache.name.startswith("app_index_")
        assert PosixHost()._apps.cache.path is None

    async def test_volume_with_pactl(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        use_tools(monkeypatch, "pactl")
        runner.answers[("pactl", "get-sink-volume")] = proc_result(PACTL_VOLUME)
        assert await host.get_volume() == 50
        assert await host.set_volume(150) == 100
        assert runner.argvs[-1] == ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"]
        assert await host.change_volume(-10) == 50                      # read back from the (fake) sink
        assert ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"] in runner.argvs
        await host.set_mute(True)
        assert runner.argvs[-1] == ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"]

    async def test_pactl_volume_is_capped_at_100(self, px: tuple[PosixHost, FakeRunner],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        use_tools(monkeypatch, "pactl")
        runner.answers[("pactl", "get-sink-volume")] = proc_result("Volume: front-left: 75366 / 115% / 3.64 dB")
        assert await host.change_volume(20) == 100
        assert runner.argvs[-1] == ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"]

    async def test_volume_falls_back_to_amixer(self, px: tuple[PosixHost, FakeRunner],
                                               monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        use_tools(monkeypatch, "pactl", "amixer")
        runner.answers[("pactl",)] = proc_result(code=1, stderr="Connection failure: Connection refused")
        runner.answers[("amixer", "get")] = proc_result(AMIXER_MASTER)
        assert await host.get_volume() == 42
        assert await host.set_volume(30) == 30
        assert runner.argvs[-1] == ["amixer", "-q", "set", "Master", "30%"]
        assert await host.change_volume(-5) == 42
        assert ["amixer", "-q", "set", "Master", "5%-"] in runner.argvs
        await host.set_mute(False)
        assert runner.argvs[-1] == ["amixer", "-q", "set", "Master", "unmute"]

    async def test_volume_without_tools(self, px: tuple[PosixHost, FakeRunner]) -> None:
        host, runner = px
        assert await host.get_volume() is None
        with pytest.raises(HostUnsupported, match="pactl"):
            await host.set_volume(10)
        with pytest.raises(HostUnsupported):
            await host.change_volume(10)
        with pytest.raises(HostUnsupported):
            await host.set_mute(True)
        assert runner.calls == []

    async def test_brightness(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        with pytest.raises(HostUnsupported, match="brightnessctl"):
            await host.get_brightness()
        with pytest.raises(HostUnsupported, match="brightnessctl"):
            await host.set_brightness(50)
        use_tools(monkeypatch, "brightnessctl")
        runner.answers[("brightnessctl", "-m")] = proc_result("intel_backlight,backlight,12000,50%,24000\n")
        assert await host.get_brightness() == 50
        assert await host.set_brightness(-20) == 0
        assert runner.argvs[-1] == ["brightnessctl", "-q", "set", "0%"]
        runner.answers[("brightnessctl", "-m")] = proc_result(code=1, stderr="Failed to read any devices")
        with pytest.raises(HostUnsupported, match="backlight"):
            await host.get_brightness()

    async def test_media_uses_playerctl(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        with pytest.raises(HostUnsupported, match="playerctl"):
            await host.media("next")
        use_tools(monkeypatch, "playerctl")
        await host.media("play_pause")
        await host.media("previous")
        assert runner.argvs == [["playerctl", "play-pause"], ["playerctl", "previous"]]
        runner.answers[("playerctl",)] = proc_result(code=1, stderr="No players found")
        with pytest.raises(HostUnsupported, match="No media player"):
            await host.media("stop")

    async def test_lock_screen_tries_lockers_in_order(self, px: tuple[PosixHost, FakeRunner],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        with pytest.raises(HostUnsupported, match="no screen locker"):
            await host.lock_screen()
        use_tools(monkeypatch, "xdg-screensaver", "dm-tool", "xflock4")
        runner.answers[("xdg-screensaver",)] = proc_result(code=4, stderr="not supported")
        await host.lock_screen()
        assert runner.argvs == [["xdg-screensaver", "lock"], ["dm-tool", "lock"]]

    async def test_notify_send(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        with pytest.raises(HostUnsupported, match="notify-send"):
            await host.notify("Hi", "there")
        use_tools(monkeypatch, "notify-send")
        await host.notify("Build done", "--not-an-option & <b>")
        assert runner.argvs[-1] == ["notify-send", "--app-name=JARVIS", "--", "Build done", "--not-an-option & <b>"]
        await host.notify("", "")
        assert runner.argvs[-1][-2:] == ["JARVIS", ""]
        runner.answers[("notify-send",)] = proc_result(code=1, stderr="Cannot connect to the session bus")
        with pytest.raises(HostUnsupported):
            await host.notify("x", "y")

    async def test_clipboard_tools_follow_the_session_type(self, px: tuple[PosixHost, FakeRunner],
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        use_tools(monkeypatch, "wl-paste", "wl-copy", "xclip")
        fed: list[tuple[list[str], bytes]] = []

        def feed(argv: Sequence[str], data: bytes, timeout: float = 5.0) -> int:
            fed.append((list(argv), data))
            return 0

        monkeypatch.setattr(posix, "_feed_detached", feed)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        runner.answers[("wl-paste",)] = proc_result("שלום")
        assert await host.get_clipboard() == "שלום"
        assert runner.argvs[-1] == ["wl-paste", "--no-newline"]
        await host.set_clipboard("héllo")
        assert fed[-1] == (["wl-copy"], "héllo".encode())
        monkeypatch.delenv("WAYLAND_DISPLAY")
        runner.answers[("xclip",)] = proc_result("x11 text")
        assert await host.get_clipboard() == "x11 text"
        assert runner.argvs[-1] == ["xclip", "-selection", "clipboard", "-o"]
        await host.set_clipboard("y")
        assert fed[-1] == (["xclip", "-selection", "clipboard", "-i"], b"y")

    async def test_clipboard_errors(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        with pytest.raises(HostUnsupported, match="xclip"):
            await host.get_clipboard()
        use_tools(monkeypatch, "wl-paste", "wl-copy")
        runner.answers[("wl-paste",)] = proc_result(code=1, stderr="Nothing is copied\n")
        assert await host.get_clipboard() == ""
        runner.answers[("wl-paste",)] = proc_result(code=1, stderr="Failed to connect to a Wayland server")
        with pytest.raises(HostUnsupported):
            await host.get_clipboard()
        monkeypatch.setattr(posix, "_feed_detached", lambda argv, data, timeout=5.0: 1)
        with pytest.raises(HostUnsupported):
            await host.set_clipboard("x")

    async def test_open_url(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, _ = px
        spawn = SpawnRecorder()
        monkeypatch.setattr(host, "_spawn", spawn)
        with pytest.raises(HostUnsupported, match="xdg-open"):
            await host.open_url("https://example.com")
        use_tools(monkeypatch, "gio")
        await host.open_url("example.com")
        assert spawn.argvs == [["/usr/bin/gio", "open", "https://example.com"]]
        use_tools(monkeypatch, "xdg-open", "gio")
        await host.open_url("http://localhost:8080/x")
        assert spawn.argvs[-1] == ["/usr/bin/xdg-open", "http://localhost:8080/x"]
        with pytest.raises(HostUnsupported):
            await host.open_url("ftp://example.com/file")
        assert len(spawn.argvs) == 2

    async def test_open_path_and_reveal(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch,
                                        sample_file: Path) -> None:
        host, runner = px
        spawn = SpawnRecorder()
        monkeypatch.setattr(host, "_spawn", spawn)
        use_tools(monkeypatch, "xdg-open", "dbus-send")
        await host.open_path(sample_file)
        assert spawn.argvs == [["/usr/bin/xdg-open", str(sample_file)]]
        await host.reveal(sample_file)
        assert runner.argvs[-1][0] == "dbus-send" and f"array:string:{sample_file.as_uri()}" in runner.argvs[-1]
        assert len(spawn.argvs) == 1                                     # the file manager showed it
        runner.answers[("dbus-send",)] = proc_result(code=1, stderr="org.freedesktop.DBus.Error.ServiceUnknown")
        await host.reveal(sample_file)
        assert spawn.argvs[-1] == ["/usr/bin/xdg-open", str(sample_file.parent)]
        host.is_macos = True
        await host.reveal(sample_file)
        assert spawn.argvs[-1] == ["open", "-R", str(sample_file)]
        with pytest.raises(FileNotFoundError):
            await host.open_path(sample_file.with_name("missing.txt"))

    async def test_power_and_cancel(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        with pytest.raises(HostUnsupported, match="systemctl"):
            await host.power("shutdown")
        use_tools(monkeypatch, "systemctl", "loginctl")
        await host.power("shutdown")
        await host.power("restart")
        await host.power("sleep")
        assert runner.argvs == [["systemctl", "poweroff"], ["systemctl", "reboot"], ["systemctl", "suspend"]]
        monkeypatch.setenv("XDG_SESSION_ID", "c2")
        await host.power("logoff")
        assert runner.argvs[-1] == ["loginctl", "terminate-session", "c2"]
        await host.power("restart", 300)
        assert host._delayed.pending == 1 and len(runner.calls) == 4
        await host.cancel_power()
        assert host._delayed.pending == 0
        runner.answers[("systemctl",)] = proc_result(code=1, stderr="Access denied")
        with pytest.raises(HostUnsupported, match="Access denied"):
            await host.power("shutdown")

    async def test_running_apps_from_wmctrl(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, runner = px
        use_tools(monkeypatch, "wmctrl")
        me = os.getpid()
        runner.answers[("wmctrl",)] = proc_result(
            f"0x01e00003  0 {me}   host Terminal — bash\n0x01e00004  0 {me}   host Second window\n"
            "0x02000003 -1 1234 host Desktop panel\n")
        apps = await host.running_apps()
        assert [(a["pid"], a["title"], a["windows"]) for a in apps] == [(me, "Terminal — bash", 2)]

    async def test_running_apps_without_wmctrl_lists_processes(self, px: tuple[PosixHost, FakeRunner]) -> None:
        host, runner = px
        rows = await host.running_apps()
        assert rows and all({"name", "pid", "memory_mb"} <= set(r) for r in rows)
        assert runner.calls == []

    async def test_launch_app(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch) -> None:
        host, _ = px
        spawn = SpawnRecorder()
        monkeypatch.setattr(host, "_spawn", spawn)
        gedit = InstalledApp("Text Editor", "gedit --new-window", "command", "desktop")
        missing = await host.launch_app(gedit, ["notes.txt"])
        assert not missing.ok and "not installed" in missing.detail and spawn.argvs == []
        use_tools(monkeypatch, "gedit", "xdg-open")
        launched = await host.launch_app(gedit, ["my notes.txt"])
        assert (launched.ok, launched.method, launched.pid) == (True, "popen", 4321)
        assert spawn.argvs[-1] == ["gedit", "--new-window", "my notes.txt"]
        steam = await host.launch_app(InstalledApp("Steam", "steam://open/games", "uri"))
        assert steam.method == "uri" and spawn.argvs[-1] == ["/usr/bin/xdg-open", "steam://open/games"]
        await host.launch_app(InstalledApp("File Explorer", "xdg-open", "command", "catalog", id="explorer"))
        assert spawn.argvs[-1][0] == "xdg-open" and len(spawn.argvs[-1]) == 2       # opens the home folder
        with pytest.raises(HostUnsupported):
            await host.launch_app(InstalledApp("Calc", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "appx"))
        bad = await host.launch_app(InstalledApp("Bad", "gedit 'unclosed", "command"))
        assert not bad.ok and "Bad command" in bad.detail

    @pytest.mark.parametrize("name", ["systemd", "Xorg", "pulseaudio", "launchd"])
    async def test_close_app_refuses_critical_processes(self, px: tuple[PosixHost, FakeRunner],
                                                        monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        host, _ = px
        host._apps = AppIndexStore(static_builder([]), platform="linux")

        def must_not_scan(targets: Sequence[str]) -> list[Any]:
            raise AssertionError(f"looked for {targets}")

        monkeypatch.setattr(host, "_find_processes", must_not_scan)
        with pytest.raises(PermissionDenied):
            await host.close_app(name)

    async def test_close_app_terminates_then_kills_with_force(self, px: tuple[PosixHost, FakeRunner],
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
        host, _ = px
        host._apps = AppIndexStore(static_builder([
            InstalledApp("Firefox Web Browser", "firefox", "command", "desktop", process_names=("firefox",))]),
            platform="linux")
        procs = [FakeProc(201, "firefox")]
        asked: list[list[str]] = []

        def find(targets: Sequence[str]) -> list[FakeProc]:
            asked.append(list(targets))
            return procs

        monkeypatch.setattr(host, "_find_processes", find)
        monkeypatch.setattr(psutil, "wait_procs", fake_wait_procs({201}))
        assert await host.close_app("פיירפוקס") == ["firefox"]
        assert asked == [["firefox"]] and procs[0].terminated and not procs[0].killed
        monkeypatch.setattr(psutil, "wait_procs", fake_wait_procs(set(), {201}))
        assert await host.close_app("firefox", force=True) == ["firefox"]
        assert procs[0].killed
        monkeypatch.setattr(host, "_find_processes", lambda _targets: [])
        assert await host.close_app("firefox") == []

    def test_terminate_reports_only_closed_processes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host = PosixHost()
        procs = [FakeProc(1, "gedit"), FakeProc(2, "gedit-helper")]
        monkeypatch.setattr(psutil, "wait_procs", fake_wait_procs({1}))
        assert host._terminate(procs, False) == ["gedit"]
        assert all(p.terminated for p in procs) and not any(p.killed for p in procs)
        monkeypatch.setattr(psutil, "wait_procs", fake_wait_procs(set(), {1, 2}))
        assert host._terminate(procs, True) == ["gedit", "gedit-helper"]
        assert all(p.killed for p in procs)

    def test_find_processes_never_returns_jarvis_itself(self) -> None:
        found = PosixHost._find_processes([psutil.Process().name()])
        assert os.getpid() not in {p.pid for p in found}
        assert PosixHost._find_processes(["definitely-not-running-4242"]) == []

    async def test_screenshot(self, px: tuple[PosixHost, FakeRunner], monkeypatch: pytest.MonkeyPatch,
                              tmp_path: Path) -> None:
        host, runner = px
        monkeypatch.setattr(posix, "_grab_with_pillow", write_png)
        assert await host.screenshot(tmp_path / "a.jpeg") == png_path(tmp_path / "a.jpeg")
        monkeypatch.setattr(posix, "_grab_with_pillow", fail_grab)
        with pytest.raises(HostUnsupported, match="screenshot"):
            await host.screenshot(tmp_path / "b.png")
        use_tools(monkeypatch, "grim", "scrot")
        runner.answers[("grim",)] = proc_result(code=1, stderr="compositor doesn't support wlr-screencopy")
        runner.answers[("scrot",)] = write_shot_from_argv
        assert await host.screenshot(tmp_path / "c") == png_path(tmp_path / "c")
        assert [argv[0] for argv in runner.argvs] == ["grim", "scrot"]

    @not_windows
    def test_special_folders_follow_xdg_user_dirs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home, config = tmp_path / "home", tmp_path / "config"
        (home / "Escritorio").mkdir(parents=True)
        (home / "Downloads").mkdir()
        write_file(config / "user-dirs.dirs", 'XDG_DESKTOP_DIR="$HOME/Escritorio"\n')
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
        host = PosixHost()
        host.is_macos = False
        assert host.special_folder("desktop") == home / "Escritorio"
        assert host.special_folder("downloads") == home / "Downloads"
        assert host.special_folder("home") == home
        assert host.special_folder("appdata") == config
        assert host.special_folder("music") is None                      # doesn't exist
        assert host.special_folder("nonsense") is None


@pytest.fixture
def desktop_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """XDG applications folder with real executables, and the PATH catalog switched off."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("firefox", "gnome-text-editor"):
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
    apps_dir = tmp_path / "applications"
    write_file(apps_dir / "firefox.desktop", "[Desktop Entry]\nType=Application\nName=Firefox Web Browser\n"
               f'Name[he]=דפדפן פיירפוקס\nExec="{bin_dir}/firefox" %u\n')
    write_file(apps_dir / "org.gnome.TextEditor.desktop", "[Desktop Entry]\nType=Application\nName=Text Editor\n"
               f'Name[he]=עורך טקסט\nExec="{bin_dir}/gnome-text-editor" %U\n')
    write_file(apps_dir / "hidden.desktop", "[Desktop Entry]\nType=Application\nName=Hidden Thing\nExec=/bin/true\n"
               "NoDisplay=true\n")
    monkeypatch.setattr(posix, "desktop_dirs", lambda *_a, **_k: [apps_dir])
    monkeypatch.setattr(posix, "catalog_apps_posix", lambda *_a, **_k: [])
    return apps_dir


@not_windows
async def test_posix_host_indexes_desktop_entries(desktop_env: Path, tmp_path: Path) -> None:
    host = PosixHost(cache_dir=tmp_path / "cache")
    host.is_macos = False
    assert sorted(a.name for a in await host.installed_apps()) == ["Firefox Web Browser", "Text Editor"]
    firefox = await host.find_app("פיירפוקס")
    assert firefox is not None and (firefox.id, firefox.source) == ("firefox", "desktop")
    editor = await host.find_app("עורך טקסט")
    assert editor is not None and editor.name == "Text Editor"
    assert await host.find_app("hidden thing") is None
    assert host._apps.cache.load() is not None


# =============================================================================================
# Adapter selection and the Win32 layer off Windows
# =============================================================================================
@not_windows
def test_get_host_adapter_picks_posix_off_windows(tmp_path: Path) -> None:
    host = get_host_adapter(cache_dir=tmp_path)
    assert isinstance(host, PosixHost) and host.default_shell == "bash"
    assert host._apps.cache.path is not None and host._apps.cache.path.parent == tmp_path


class TestWinapi:
    def test_structures_match_the_windows_layout(self) -> None:
        pointer = ctypes.sizeof(ctypes.c_void_p)
        assert ctypes.sizeof(winapi.INPUT) == (40 if pointer == 8 else 28)
        assert ctypes.sizeof(winapi.KEYBDINPUT) == (24 if pointer == 8 else 16)
        assert ctypes.sizeof(winapi.GUID) == 16
        guid = winapi.GUID.from_string(winapi.KNOWN_FOLDER_IDS["desktop"])
        assert guid.Data1 == 0xB4BFCC3A and str(guid) == "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
        assert str(winapi.GUID.from_string("5cdf2c82-841e-4546-9722-0cf74078229a")) == winapi.IID_IAUDIO_ENDPOINT_VOLUME

    @not_windows
    @pytest.mark.parametrize(
        "call",
        [
            lambda: winapi.known_folder_path("desktop"),
            lambda: winapi.press_keys([(winapi.VK_VOLUME_UP, 1)]),
            winapi.lock_workstation,
            winapi.suspend,
            winapi.get_clipboard_text,
            lambda: winapi.set_clipboard_text("x"),
            winapi.list_windows,
            lambda: winapi.close_windows(class_names=["CabinetWClass"]),
            lambda: winapi.file_description(r"C:\Windows\notepad.exe"),
            lambda: winapi.protocol_handler("ms-settings"),
            winapi.get_master_volume,
            lambda: winapi.set_master_volume(50),
            winapi.get_master_mute,
            lambda: winapi.set_master_mute(True),
        ],
    )
    def test_off_windows_every_call_is_unsupported(self, call: Callable[[], Any]) -> None:
        with pytest.raises(HostUnsupported):
            call()


# =============================================================================================
# Real Windows (run on a Windows desktop: pytest -m windows)
# =============================================================================================
@pytest.mark.windows
@pytest.mark.skipif(not IS_WINDOWS, reason="needs a real Windows desktop")
class TestRealWindows:
    def test_adapter_and_process_flags(self) -> None:
        assert isinstance(get_host_adapter(), WindowsHost)
        assert windows.DETACHED_PROCESS == subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        assert windows.CREATE_NEW_PROCESS_GROUP == subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        assert windows.CREATE_NEW_CONSOLE == subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        assert windows.CREATE_NO_WINDOW == subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    def test_known_folders_exist(self) -> None:
        host = WindowsHost()
        for key in ("desktop", "documents", "downloads", "home", "appdata", "startmenu"):
            folder = host.special_folder(key)
            assert folder is not None and folder.is_dir(), key
        assert any(root.is_dir() for root in host.start_menu_roots())

    async def test_installed_apps_and_find_notepad(self, tmp_path: Path) -> None:
        host = WindowsHost(cache_dir=tmp_path)
        apps = await host.installed_apps()
        assert len(apps) >= 5
        names = " | ".join(a.name.lower() for a in apps)
        assert any(word in names for word in ("notepad", "chrome", "edge", "paint", "calculator"))
        notepad = await host.find_app("notepad")
        assert notepad is not None and "notepad" in (notepad.name + notepad.target).lower()
        assert await host.find_app("פנקס רשימות") is not None
        assert host._apps.cache.load() is not None

    async def test_clipboard_roundtrip(self) -> None:
        host = WindowsHost()
        before = await host.get_clipboard()
        try:
            await host.set_clipboard("JARVIS ✓ בדיקה 123")
            assert await host.get_clipboard() == "JARVIS ✓ בדיקה 123"
        finally:
            await host.set_clipboard(before)

    async def test_running_apps_non_empty(self) -> None:
        apps = await WindowsHost().running_apps()
        assert apps and all("pid" in a and "name" in a for a in apps)

    async def test_volume_reads_a_level(self) -> None:
        level = await WindowsHost().get_volume()
        assert level is None or 0 <= level <= 100

    def test_real_shortcuts_parse(self) -> None:
        apps = scan_start_menu(WindowsHost().start_menu_roots())
        assert apps
        links = [windows.parse_lnk(a.target) for a in apps if a.target.lower().endswith(".lnk")]
        assert any(link is not None and link.target for link in links)

    async def test_get_start_apps(self) -> None:
        apps = await WindowsHost()._start_apps()
        assert apps and all(a.name and a.target for a in apps)


# ================================================================================ system monitor
class TestSystemMonitor:
    def test_a_snapshot_has_what_the_hud_shows(self) -> None:
        mon = monitor.SystemMonitor()
        first = mon.sample()
        second = mon.sample()
        assert 0 <= second["cpu"]["percent"] <= 100 and len(second["cpu"]["per_core"]) == second["cpu"]["cores"]
        assert 0 < second["memory"]["percent"] <= 100 and second["memory"]["used_gb"] <= second["memory"]["total_gb"]
        assert second["disks"] and all(0 <= d["percent"] <= 100 for d in second["disks"])
        assert first["network"]["up_kbps"] == 0.0                 # rates need two samples
        assert second["network"]["up_kbps"] >= 0 and second["network"]["down_kbps"] >= 0
        assert second["uptime_s"] > 0 and second["processes"] > 0 and second["os"] and second["hostname"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mount table")
    def test_only_disks_with_the_users_files_are_listed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        part = lambda mount, fstype, opts="rw": SimpleNamespace(  # noqa: E731
            device="/dev/x", mountpoint=mount, fstype=fstype, opts=opts)
        monkeypatch.setattr(psutil, "disk_partitions", lambda all=False: [
            part("/", "ext4"), part("/home", "ext4"), part("/opt/tool", "squashfs", "ro,nodev"),
            part("/mnt/cloud", "fuse.rclone"), part("/tmp", "tmpfs"), part("/boot/efi", "vfat"),
            part("/media/usb", "vfat", "ro"), part("/home", "ext4")])
        usage = SimpleNamespace(percent=40.0, used=40 * monitor.GB, total=100 * monitor.GB, free=60 * monitor.GB)
        monkeypatch.setattr(psutil, "disk_usage", lambda mount: usage)
        assert [d["mount"] for d in monitor._disks()] == ["/", "/home"]
        monkeypatch.setattr(psutil, "disk_partitions", lambda all=False: [part("/", "overlay")])
        assert [d["mount"] for d in monitor._disks()] == ["/"]          # a container's root still shows

    def test_findings_in_plain_language(self) -> None:
        stats = {"cpu": {"percent": 97.0}, "memory": {"percent": 93.0, "total_gb": 16.0},
                 "disks": [{"mount": "C:\\", "percent": 96.0, "free_gb": 3.0}], "battery": None, "uptime_s": 3600}
        areas = {f["area"]: f["level"] for f in monitor.health_findings(stats)}
        assert areas == {"cpu": "warning", "memory": "warning", "disk": "warning"}
        calm = {"cpu": {"percent": 5.0}, "memory": {"percent": 30.0, "total_gb": 16.0},
                "disks": [{"mount": "C:\\", "percent": 40.0, "free_gb": 200.0}], "battery": None, "uptime_s": 3600}
        assert monitor.health_findings(calm) == []
