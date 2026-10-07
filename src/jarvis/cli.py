"""`jarvis` command line.

    jarvis                     start JARVIS with its window (or bring the running one forward)
    jarvis --no-window         run the core and the local server only; prints the address of the UI
    jarvis open                open the running JARVIS in the default browser
    jarvis say "<command>"     send a command to the running JARVIS and print the answer
    jarvis status              is JARVIS running? agents, tasks, voice, AI provider
    jarvis keys                which API keys are configured (never their values)
    jarvis keys set NAME       store an API key in Windows Credential Manager (typed hidden)
    jarvis keys delete NAME
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
import webbrowser
from typing import Any

from jarvis import __version__

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
    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="start JARVIS (default)")
    sub.add_parser("open", help="open the running JARVIS in the browser")
    say = sub.add_parser("say", help="send a command to the running JARVIS")
    say.add_argument("text", nargs="+")
    sub.add_parser("status", help="show whether JARVIS is running and what it is doing")
    keys = sub.add_parser("keys", help="API keys (Windows Credential Manager)")
    keys.add_argument("action", nargs="?", choices=["list", "set", "delete"], default="list")
    keys.add_argument("name", nargs="?")
    sub.add_parser("voice-test", help="check the voice stack on this PC")
    sub.add_parser("paths", help="show JARVIS's folders")
    return p


def main(argv: list[str] | None = None) -> int:
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
        print(f"The JARVIS configuration has an error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    paths = resolve_paths(settings.paths, settings.memory.db_path).ensure()
    if command == "paths":
        for name in ("home", "data", "config_file", "logs", "cache", "workspace", "projects", "db"):
            print(f"{name:12} {getattr(paths, name)}")
        return EXIT_OK
    if command == "keys":
        return _keys(paths, args.action, args.name)
    if command in ("open", "say", "status"):
        running = _running(paths)
        if running is None:
            print("JARVIS is not running.", file=sys.stderr)
            return EXIT_NOT_RUNNING
        if command == "open":
            webbrowser.open(f"http://127.0.0.1:{running['port']}/?launch={running['token']}")
            return EXIT_OK
        if command == "say":
            return _say(running, " ".join(args.text))
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


def _keys(paths: Any, action: str, name: str | None) -> int:
    from jarvis.security.secrets import KNOWN_SECRETS, SecretStore

    store = SecretStore(paths.data)
    if action == "list":
        for key, st in store.status().items():
            mark = "✓" if st["configured"] else "·"
            print(f"{mark} {key:<22} {st['label']}" + (f"  [{st['source']}]" if st["source"] else ""))
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
    where = store.set(name, value)
    print(f"{name} saved ({where}). A running JARVIS picks it up within 15 seconds.")
    return EXIT_OK


# ================================================================================ running JARVIS
def _run(settings: Any, paths: Any, args: argparse.Namespace) -> int:
    from jarvis.logs.setup import setup_logging

    setup_logging(paths.logs, level=logging.DEBUG if args.debug else logging.INFO, console=not args.quiet)
    running = _running(paths)
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

    return run_desktop(make_app)


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
