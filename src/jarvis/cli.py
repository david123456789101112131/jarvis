"""`jarvis` command line.

    jarvis                     start JARVIS with its window (or bring the running one forward)
    jarvis --no-window         run the core and the local server only; prints the address of the UI
    jarvis open                open the running JARVIS in the default browser
    jarvis say "<command>"     send a command to the running JARVIS and print the answer
    jarvis status              is JARVIS running? agents, tasks, voice, AI provider
    jarvis stop                stop the running JARVIS (it saves unfinished tasks first)
    jarvis keys                which API keys are configured (never their values)
    jarvis keys set NAME       store an API key in Windows Credential Manager (typed hidden)
    jarvis keys delete NAME    (--all: every stored key)
    jarvis check               is JARVIS ready on this PC? (--full: real commands in a temporary JARVIS;
                               --voice: the voice self-test too)
    jarvis voice-test          check the microphone, speakers, wake word and speech recognition
    jarvis paths               where JARVIS keeps its data, logs, workspace and projects
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import signal
import sys
import threading
import time
import webbrowser
from typing import Any

from jarvis import __version__
from jarvis.common.console import ensure_std_streams, show_error

log = logging.getLogger("jarvis")

EXIT_OK, EXIT_ERROR, EXIT_NOT_RUNNING = 0, 1, 3


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jarvis", description="JARVIS — desktop AI assistant with five agents.")
    p.add_argument("--version", action="version", version=f"JARVIS {__version__}")
    p.add_argument("--no-window", action="store_true", help="run without the desktop window")
    p.add_argument("--no-voice", action="store_true", help="start with voice turned off")
    p.add_argument("--port", type=int, default=None, help="local server port (default 8765, next free if busy)")
    p.add_argument("--debug", action="store_true", help="verbose logs and start in DEBUG mode")
    p.add_argument("--quiet", action="store_true", help="no log output on the console")
    p.add_argument("--browser", action="store_true", help="open the UI in the default browser instead of a window")
    p.add_argument("--background", action="store_true",
                   help="start with only the mini orb (how Windows starts JARVIS at sign-in)")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="start JARVIS (default)")
    sub.add_parser("open", help="open the running JARVIS in the browser")
    say = sub.add_parser("say", help="send a command to the running JARVIS")
    say.add_argument("text", nargs="+")
    sub.add_parser("status", help="show whether JARVIS is running and what it is doing")
    sub.add_parser("stop", help="stop the running JARVIS")
    keys = sub.add_parser("keys", help="API keys (Windows Credential Manager)")
    keys.add_argument("action", nargs="?", choices=["list", "set", "delete"], default="list")
    keys.add_argument("name", nargs="?")
    keys.add_argument("--all", action="store_true", help="with delete: remove every stored key")
    check = sub.add_parser("check", help="check that JARVIS is ready on this PC")
    check.add_argument("--full", action="store_true", help="also run real commands in a temporary JARVIS")
    check.add_argument("--voice", action="store_true", help="also run the voice self-test (plays two sentences)")
    check.add_argument("--json", action="store_true", help="print the report as JSON")
    sub.add_parser("voice-test", help="check the voice stack on this PC")
    comp = sub.add_parser("computer", help="the local model that lets JARVIS control the computer")
    comp.add_argument("action", nargs="?", choices=["install", "status"], default="status")
    comp.add_argument("--dir", help="where to install (default: JARVIS's models folder)")
    comp.add_argument("--adopt", help="use a model folder that is already downloaded, without downloading")
    comp.add_argument("--no-convert", action="store_true", help="download only, skip the GGUF conversion")
    med = sub.add_parser("media", help="the free models that make images and videos on this computer")
    med.add_argument("action", nargs="?", choices=["install", "status", "bench"], default="status")
    med.add_argument("--dir", help="where to install (default: JARVIS's models folder)")
    med.add_argument("--only", choices=["image", "video"], help="install (or measure) one kind only")
    med.add_argument("--no-runtime", action="store_true", help="models only: leave the Python environment as it is")
    med.add_argument("--modes", default="auto", help="bench: the modes to measure, comma separated "
                                                     "(auto, gpu, gpu8, model, model8, group8)")
    med.add_argument("--keep", help="bench: keep what is made in this folder (otherwise it is deleted)")
    med.add_argument("--resolution", choices=["480p", "720p"], default="480p", help="bench: the video's size")
    med.add_argument("--seconds", type=int, choices=range(1, 6), default=3, help="bench: the video's length")
    sub.add_parser("paths", help="show JARVIS's folders")
    sub.add_parser("autostart", help="show whether JARVIS starts with Windows")
    return p


def main(argv: list[str] | None = None) -> int:
    ensure_std_streams()                    # pythonw.exe / the windowed JARVIS.exe have no console
    _utf8_console()
    args = build_parser().parse_args(argv)
    command = args.command or "run"
    if command == "voice-test":
        from jarvis.voice.selftest import main as voice_test

        return voice_test([])
    from jarvis.common.errors import ConfigError
    from jarvis.config.loader import load_settings
    from jarvis.config.paths import resolve_paths

    try:
        settings = load_settings()
    except ConfigError as exc:
        show_error(f"יש שגיאה בהגדרות של JARVIS:\n{exc}\n\nThe JARVIS configuration has an error.")
        return EXIT_ERROR
    paths = resolve_paths(settings.paths, settings.memory.db_path).ensure()
    if command == "paths":
        for name in ("home", "data", "config_file", "logs", "cache", "workspace", "projects", "db"):
            print(f"{name:12} {getattr(paths, name)}")
        return EXIT_OK
    if command == "keys":
        return _keys(paths, args.action, args.name, every=args.all)
    if command == "autostart":
        return _autostart(settings)
    if command == "check":
        from jarvis.selfcheck import main as check

        return check(settings, paths, full=args.full, voice=args.voice, as_json=args.json)
    if command == "computer":
        return _computer(settings, paths, args)
    if command == "media":
        return _media(settings, paths, args)
    if command in ("open", "say", "status", "stop"):
        running = _running(paths)
        if running is None:
            print("JARVIS is not running.", file=sys.stderr)
            return EXIT_NOT_RUNNING
        if command == "open":
            webbrowser.open(f"http://127.0.0.1:{running['port']}/?launch={running['token']}")
            return EXIT_OK
        if command == "say":
            return _say(running, " ".join(args.text))
        if command == "stop":
            return _stop(running)
        return _status(running)
    return _run(settings, paths, args)


# ================================================================================ talking to a running JARVIS
def _running(paths: Any) -> dict[str, Any] | None:
    from jarvis.app import read_runtime_file

    info = read_runtime_file(paths)
    if info is None:
        return None
    try:
        _call(info, "GET", "/api/health", timeout=2.0)
    except Exception:
        return None
    return info


def _call(info: dict[str, Any], method: str, path: str, body: Any = None, timeout: float = 30.0) -> Any:
    import httpx

    r = httpx.request(method, f"http://127.0.0.1:{info['port']}{path}", json=body, timeout=timeout,
                      headers={"X-JARVIS-Token": info["token"]})
    r.raise_for_status()
    return r.json()


def _say(info: dict[str, Any], text: str) -> int:
    try:
        res = _call(info, "POST", "/api/command", {"text": text}, timeout=180.0)
    except Exception as exc:
        print(f"JARVIS did not answer: {exc}", file=sys.stderr)
        return EXIT_ERROR
    reply = res.get("reply") or {}
    print(reply.get("text") or ("OK" if res.get("accepted") else "Not accepted."))
    if res.get("task_id"):
        print(f"(task {res['task_id']}: JARVIS reports the result in its window)")
    return EXIT_OK


def _status(info: dict[str, Any]) -> int:
    try:
        state = _call(info, "GET", "/api/state", timeout=10.0)
    except Exception as exc:
        print(f"JARVIS did not answer: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"JARVIS {state['version']} is running on http://127.0.0.1:{info['port']} (pid {info['pid']})")
    print(f"Mode: {state['mode']}  ·  AI: {state['providers'].get('active') or 'not configured'}  ·  "
          f"voice: {state['voice']['state']}")
    for a in state["agents"]:
        current = f" — {a['current']['title']}" if a.get("current") else ""
        print(f"  {a['number']}. {a['name']:<18} {a['status']}{current}")
    active = state["tasks"]["active"]
    print(f"Active tasks: {len(active)}" + "".join(f"\n  • {t['title']} ({t['status']}, {t['progress']:.0%})"
                                                   for t in active))
    if state["kill_switch"]["global"]:
        print("The kill switch is ENGAGED.")
    return EXIT_OK


def _stop(info: dict[str, Any], timeout: float = 60.0) -> int:
    """Ask the running JARVIS to quit (as its window's Quit does) and wait until its process has ended."""
    import psutil

    try:
        _call(info, "POST", "/api/window/quit", timeout=10.0)
    except Exception as exc:
        print(f"JARVIS did not answer: {exc}", file=sys.stderr)
        return EXIT_ERROR
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if psutil.Process(int(info["pid"])).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.Error:                    # the process is gone
            break
        time.sleep(0.2)
    else:
        print(f"JARVIS (pid {info['pid']}) was asked to stop but is still running.", file=sys.stderr)
        return EXIT_ERROR
    print("JARVIS stopped.")
    return EXIT_OK


def _autostart(settings: Any) -> int:
    from jarvis.host import autostart

    st = autostart.status()
    if not st["supported"]:
        print("Starting with Windows is only available on Windows.")
        return EXIT_OK
    wanted = "on" if settings.general.start_with_windows else "off"
    print(f"Setting (Settings > General): {wanted}")
    if not st["registered"]:
        print("Windows entry: none" + (" (the JARVIS window adds it when it starts)" if wanted == "on" else ""))
    else:
        print(f"Windows entry: {st['command']}")
        if not st["this_jarvis"]:
            print("  It starts another copy of JARVIS; the JARVIS window updates it when it starts.")
        if st["disabled_in_task_manager"]:
            print("  Turned off in Task Manager > Startup apps: Windows does not run it.")
    return EXIT_OK


def _computer(settings: Any, paths: Any, args: argparse.Namespace) -> int:
    from pathlib import Path

    from jarvis.computer import install as inst
    from jarvis.computer.server import ModelFiles
    from jarvis.config.loader import save_settings, update_settings

    default_root = Path(settings.computer.models_dir) if settings.computer.models_dir else paths.models / "computer"
    if args.action == "status":
        problem = ModelFiles(default_root).problem()
        print(f"Model folder: {default_root}")
        print("Status: ready to control the computer." if problem is None else f"Not installed yet: {problem}")
        print("Install with:  jarvis computer install" if problem else "")
        return EXIT_OK

    adopt = Path(args.adopt).expanduser() if args.adopt else None
    root = Path(args.dir).expanduser() if args.dir else default_root
    try:
        result = inst.install(root, adopt=adopt, do_convert=not args.no_convert, progress=lambda m: print(m, flush=True))
    except (OSError, RuntimeError) as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    final = result.root
    if str(final) != str(paths.models / "computer"):      # remember where the model lives, for every launch
        save_settings(update_settings(settings, {"computer": {"models_dir": str(final)}}))
        print(f"JARVIS will use the model in {final}.")
    if result.ok:
        print("Done. JARVIS can now control the computer (it asks before each action; Esc twice stops it).")
        return EXIT_OK
    print("The model folder is still incomplete; see the messages above.", file=sys.stderr)
    return EXIT_ERROR


def _media(settings: Any, paths: Any, args: argparse.Namespace) -> int:
    from pathlib import Path

    from jarvis.config.loader import save_settings, update_settings
    from jarvis.media.local import install as inst
    from jarvis.media.local.engine import IMAGE_DIR, TRANSLATE_DIR, VIDEO_DIR, LocalFiles

    default_root = Path(settings.media.models_dir) if settings.media.models_dir else paths.models / "media"
    if args.action == "status":
        files = LocalFiles(default_root)
        print(f"Model folder: {default_root}")
        print(f"Engine in use: {settings.media.engine}" + ("" if settings.media.engine == "local" else
                                                          " (Grok Imagine, paid; set media.engine to local)"))
        for part, label in (("runtime", "runtime (PyTorch, diffusers)"), ("image", "images: Z-Image-Turbo"),
                            ("video", "videos: Wan 2.2 TI2V-5B"), ("translate", "Hebrew to English")):
            print(f"  {'✓' if files.has(part) else '·'} {label}")
        if not all(files.has(p) for p in ("runtime", "image", "video")):
            print("Install with:  jarvis media install")
        return EXIT_OK

    if args.action == "bench":
        return _media_bench(settings, paths, Path(args.dir).expanduser() if args.dir else default_root, args)

    root = Path(args.dir).expanduser() if args.dir else default_root
    models = {"image": (IMAGE_DIR, TRANSLATE_DIR), "video": (VIDEO_DIR, TRANSLATE_DIR)}.get(
        args.only or "", (IMAGE_DIR, VIDEO_DIR, TRANSLATE_DIR))
    try:
        result = inst.install(root, models=models, runtime=not args.no_runtime,
                              progress=lambda m: print(m, flush=True))
    except (OSError, RuntimeError) as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if str(root) != str(paths.models / "media"):            # remember where the models live, for every launch
        save_settings(update_settings(settings, {"media": {"models_dir": str(root)}}))
        print(f"JARVIS will use the models in {root}.")
    for note in result.notes:
        print(f"Note: {note}")
    files = LocalFiles(root)
    if files.has("runtime") and any(files.has(k) for k in ("image", "video")):
        print("Done. The Images and Video screens now make them on this computer, for free.")
        return EXIT_OK
    print("The media folder is still incomplete; see the messages above.", file=sys.stderr)
    return EXIT_ERROR


def _media_bench(settings: Any, paths: Any, root: Any, args: argparse.Namespace) -> int:
    import asyncio
    import json
    import shutil
    import tempfile
    from pathlib import Path

    from jarvis.media.local import bench
    from jarvis.media.local.engine import LocalEngine, LocalFiles
    from jarvis.media.local.worker import MODES
    from jarvis.security.process import ProcessRegistry

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if not modes or any(m not in ("auto", *MODES) for m in modes):
        print(f"Unknown mode in {args.modes!r}; choose from auto, {', '.join(MODES)}.", file=sys.stderr)
        return EXIT_ERROR
    if not LocalFiles(root).has("runtime"):
        print(f"The media runtime is not installed in {root}. Install with:  jarvis media install", file=sys.stderr)
        return EXIT_ERROR
    out = Path(args.keep).expanduser() if args.keep else Path(tempfile.mkdtemp(prefix="jarvis-media-bench-"))
    processes = ProcessRegistry()

    def engine(mode: str) -> LocalEngine:
        return LocalEngine(settings.media.model_copy(update={"offload": mode}), root, processes, paths.logs)

    try:
        rows = asyncio.run(bench.run(engine, modes=modes, kinds=[args.only] if args.only else ["image", "video"],
                                     out=out, image_steps=settings.media.image_steps,
                                     video_steps=settings.media.video_steps, seconds=args.seconds,
                                     resolution=args.resolution, say=lambda line: print(line, flush=True)))
    finally:
        if not args.keep:
            shutil.rmtree(out, ignore_errors=True)
    print(json.dumps(rows, ensure_ascii=False))
    if args.keep:
        print(f"What was made is in {out}")
    return EXIT_OK if rows and not any("error" in r for r in rows) else EXIT_ERROR


def _keys(paths: Any, action: str, name: str | None, *, every: bool = False) -> int:
    from jarvis.security.secrets import KNOWN_SECRETS, SecretFormatError, SecretStore

    store = SecretStore(paths.data)
    if action == "list":
        for key, st in store.status().items():
            mark = "✓" if st["configured"] else "·"
            print(f"{mark} {key:<22} {st['label']}" + (f"  [{st['source']}]" if st["source"] else ""))
        return EXIT_OK
    if action == "delete" and every:
        removed = [key for key in KNOWN_SECRETS if store.delete(key)]
        print(f"Removed: {', '.join(removed)}." if removed else "No stored keys.")
        return EXIT_OK
    if not name or name not in KNOWN_SECRETS:
        print("Name one of: " + ", ".join(KNOWN_SECRETS), file=sys.stderr)
        return EXIT_ERROR
    if action == "delete":
        store.delete(name)
        print(f"{name} removed.")
        return EXIT_OK
    value = getpass.getpass(f"{KNOWN_SECRETS[name]} (input hidden): ").strip()
    if not value:
        print("Nothing entered; nothing changed.", file=sys.stderr)
        return EXIT_ERROR
    try:
        where = store.set(name, value)
    except SecretFormatError as exc:
        print(f"Not saved: {exc}.", file=sys.stderr)
        return EXIT_ERROR
    print(f"{name} saved ({where}). A running JARVIS picks it up within 15 seconds.")
    return EXIT_OK


# ================================================================================ running JARVIS
def _run(settings: Any, paths: Any, args: argparse.Namespace) -> int:
    from jarvis.logs.setup import setup_logging

    setup_logging(paths.logs, level=logging.DEBUG if args.debug else logging.INFO, console=not args.quiet)
    running = _running(paths)
    if running is not None and args.background:
        return EXIT_OK                       # already running (e.g. started by hand before signing in again)
    if running is not None:
        # One JARVIS per user: bring the running one forward instead of starting another.
        try:
            _call(running, "POST", "/api/window/show", timeout=5.0)
        except Exception:
            webbrowser.open(f"http://127.0.0.1:{running['port']}/?launch={running['token']}")
        print("JARVIS is already running.")
        return EXIT_OK
    if args.debug:
        from jarvis.common.enums import Mode

        settings.general.default_mode = Mode.DEBUG

    def make_app() -> Any:
        from jarvis.app import JarvisApp

        return JarvisApp(settings, voice=False if args.no_voice else None, port=args.port)

    if args.no_window or args.browser:
        return asyncio.run(_run_headless(make_app, open_browser=args.browser))
    from jarvis.ui.desktop import run_desktop

    return run_desktop(make_app, background=args.background)


async def _run_headless(make_app: Any, *, open_browser: bool) -> int:
    app = make_app()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_stop_signals(loop, stop)

    async def on_window(action: str) -> bool:
        if action == "quit":
            stop.set()
            return True
        if action in ("show", "toggle", "full"):
            webbrowser.open(app.launch_url)
            return True
        return False

    try:
        await app.start()
        app.window_handler = on_window
        print(f"JARVIS is running. Open: {app.launch_url}", flush=True)
        if open_browser:
            webbrowser.open(app.launch_url)
        await stop.wait()
    finally:
        await app.shutdown()
    return EXIT_OK


def _install_stop_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def request_stop(*_: Any) -> None:
        loop.call_soon_threadsafe(stop.set)

    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):     # Windows: no loop signal handlers
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, request_stop)


if __name__ == "__main__":
    raise SystemExit(main())
