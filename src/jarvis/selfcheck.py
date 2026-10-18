"""`jarvis check`: is JARVIS ready on this computer?

Runs where JARVIS runs (the user's Windows PC) and reports check by check; a failure never stops the
others, and nothing here changes the computer (the few temporary files are removed again):

  python     the interpreter (3.11+, 64-bit) and the Windows version
  folders    JARVIS's folders exist and can be written; free disk space
  memory     the database opens and its full-text search (substring matching, for Hebrew) works
  server     a local port on 127.0.0.1 can be opened; whether a JARVIS is running and answers
  interface  the built interface and the window component (pywebview with the WebView2 runtime)
  desktop    the Windows integration: Start-menu apps, volume, brightness, the known folders
  keys       where API keys are kept and which are configured (names, never values)
  ai         which AI provider would answer (no request is sent)
  tools      Git, Node.js/npm and Python, which the Development Agent uses for projects
  internet   whether the web search JARVIS uses can be reached
  voice      the voice packages; with `--voice`, the whole voice self-test (microphone, speech, wake word)
  computer   whether the local model that controls the mouse and keyboard is installed (it is not started)

`--full` adds the `live` checks: a second JARVIS starts in a temporary folder (no AI, no voice, no
window) and runs real commands end to end: a folder, a system check, a project the agents build and
verify, an approval that is refused and then given, the kill switch, and memory across a restart. It
touches nothing outside that folder, which is deleted at the end.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.common.checks import STATUS_ORDER, CheckReport, format_checks, short_error
from jarvis.common.errors import JarvisError

if TYPE_CHECKING:  # pragma: no cover
    from jarvis.config.paths import AppPaths
    from jarvis.config.settings import Settings
    from jarvis.security.secrets import SecretStore

GB = 1024**3
SEARCH_URL = "https://html.duckduckgo.com/html/"
LIVE_START_S = 90.0
LIVE_TASK_S = 240.0


def _worse(a: str, b: str) -> str:
    return a if STATUS_ORDER[a] >= STATUS_ORDER[b] else b


async def run_checks(settings: Settings, paths: AppPaths, *, full: bool = False, voice: bool = False,
                     progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    from jarvis.security.secrets import SecretStore

    report = CheckReport(progress)
    secrets = SecretStore(paths.data)
    steps: list[tuple[str, Any]] = [
        ("python", _python(report)),
        ("folders", _folders(report, paths)),
        ("memory", _memory(report, settings, paths)),
        ("server", _server(report, paths)),
        ("interface", _interface(report)),
        ("desktop", _desktop(report)),
        ("keys", _keys(report, secrets)),
        ("ai", _ai(report, settings, secrets)),
        ("tools", _tools(report)),
        ("internet", _internet(report, settings)),
        ("voice", _voice(report, settings, paths, secrets, run_all=voice)),
        ("computer", _computer(report, settings, paths)),
    ]
    for name, job in steps:
        report.note(f"checking {name}")
        await report.guard(name, job)
    if full:
        report.note("starting a JARVIS in a temporary folder for the live checks")
        await report.guard("live", _live(report))
    return report.finish()


# ------------------------------------------------------------------------------------------ this computer
async def _python(report: CheckReport) -> None:
    from jarvis.host.monitor import os_name

    bits = struct.calcsize("P") * 8
    status, hints = "ok", []
    if bits != 64:
        status = "warn"
        hints.append("Use 64-bit Python: the speech models need it.")
    if sys.platform == "win32":
        with contextlib.suppress(ValueError, IndexError):
            if int(platform.version().split(".")[-1]) < 17763:
                status = _worse(status, "warn")
                hints.append("Use Windows 10 version 1809 or newer, or Windows 11: older versions may lack the "
                             "window component and the notifications.")
    else:
        status = _worse(status, "warn")
        hints.append("JARVIS is made for Windows; here the Windows-only controls (volume, brightness, apps) are "
                     "limited.")
    frozen = " (installed app)" if getattr(sys, "frozen", False) else ""
    report.add("python", status, f"Python {platform.python_version()} {bits}-bit{frozen} on {os_name()}",
               hints=hints)


async def _folders(report: CheckReport, paths: AppPaths) -> None:
    def probe() -> tuple[list[str], float]:
        problems = []
        for name in ("home", "data", "logs", "cache", "workspace", "projects"):
            folder: Path = getattr(paths, name)
            try:
                folder.mkdir(parents=True, exist_ok=True)
                test = folder / f".jarvis-check-{os.getpid()}"
                test.write_text("ok", encoding="utf-8")
                test.unlink()
            except OSError as exc:
                problems.append(f"{name} ({folder}): {short_error(exc)}")
        return problems, shutil.disk_usage(paths.home).free / GB

    problems, free_gb = await asyncio.to_thread(probe)
    if problems:
        report.add("folders", "fail", "; ".join(problems),
                   hints=["Check the folder permissions, or set another location in Settings > Folders."])
        return
    status = "warn" if free_gb < 2 else "ok"
    report.add("folders", status, f"{paths.home} (workspace {paths.workspace}); {free_gb:.0f} GB free",
               hints=["Free some disk space: projects and speech models need room."] if status == "warn" else [])


async def _memory(report: CheckReport, settings: Settings, paths: AppPaths) -> None:
    from jarvis.memory import create_memory

    memory = create_memory(paths.db, backend=settings.memory.backend)
    await memory.start()
    try:
        stats = await memory.stats()
        await memory.search("בדיקה")
        tokenizer = getattr(memory.store, "tokenizer", "?")
    finally:
        await memory.close()
    status = "ok" if tokenizer == "trigram" else "warn"
    detail = (f"{paths.db.name}: {stats.get('facts', 0)} facts, {stats.get('tasks', 0)} tasks, "
              f"{stats.get('projects', 0)} projects; search: {tokenizer}")
    report.add("memory", status, detail, hints=[] if status == "ok" else [
        "This SQLite has no trigram search: Hebrew words with prefixes are found less often. Update Python."])


async def _server(report: CheckReport, paths: AppPaths) -> None:
    from jarvis.app import read_runtime_file
    from jarvis.server.runner import bind_loopback

    sock = bind_loopback("127.0.0.1", 0, 0)
    port = sock.getsockname()[1]
    sock.close()
    info = read_runtime_file(paths)
    running = ""
    if info is not None:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                r = await client.get(f"http://127.0.0.1:{info['port']}/api/health")
            running = f"; JARVIS {r.json().get('version')} is running on port {info['port']}"
        except Exception:
            running = "; a JARVIS that is no longer running left its runtime file (harmless)"
    report.add("server", "ok", f"127.0.0.1 can listen (tried port {port}){running}")


async def _interface(report: CheckReport) -> None:
    from jarvis.server.api import STATIC_DIR
    from jarvis.ui.desktop import webview2_version

    index = STATIC_DIR / "index.html"
    status, hints, parts = "ok", [], []
    if not index.is_file():
        status = "fail"
        parts.append("the interface is not built")
        hints.append("Build it: in the ui folder run `npm install` and `npm run build`.")
    else:
        assets = re.findall(r'(?:src|href)="(?:\./|/)?([\w./-]+)"', index.read_text(encoding="utf-8"))
        missing = [a for a in assets if not (STATIC_DIR / a).is_file()]
        if missing:
            status = "fail"
            parts.append(f"missing files: {', '.join(missing[:3])}")
            hints.append("Rebuild the interface (`npm run build` in the ui folder).")
        else:
            parts.append(f"built ({len(assets)} files)")
    try:
        import webview  # noqa: F401
        parts.append("window: pywebview")
    except Exception:
        status = _worse(status, "warn")
        parts.append("window: pywebview missing")
        hints.append('Install the desktop extra (pip install "jarvis-assistant[desktop]"); until then use '
                     "`jarvis --browser`.")
    if sys.platform == "win32":
        version = webview2_version()
        if version:
            parts.append(f"WebView2 {version}")
        else:
            status = _worse(status, "warn")
            parts.append("WebView2 runtime not found")
            hints.append("Install the Microsoft Edge WebView2 Runtime (Evergreen) from Microsoft's site.")
    report.add("interface", status, "; ".join(parts), hints=hints)


async def _desktop(report: CheckReport) -> None:
    from jarvis.host.base import get_host_adapter

    host = get_host_adapter()
    parts: list[str] = []
    hints: list[str] = []
    status = "ok"
    apps = await host.installed_apps()
    parts.append(f"{len(apps)} apps found")
    if not apps and sys.platform == "win32":
        status = "warn"
        hints.append("No Start-menu apps were found, so \"open <app>\" can only use the built-in list.")
    volume = brightness = None
    with contextlib.suppress(JarvisError):          # HostUnsupported: no such control on this computer
        volume = await host.get_volume()
    parts.append(f"volume {volume}%" if volume is not None else "volume control unavailable")
    if volume is None and sys.platform == "win32":
        status = _worse(status, "warn")
        hints.append("Windows did not report a speaker (is an audio device connected?); until it does, JARVIS "
                     "changes the volume with the volume keys and cannot read the level.")
    with contextlib.suppress(JarvisError):
        brightness = await host.get_brightness()
    parts.append(f"brightness {brightness}%" if brightness is not None else "no brightness control (normal on "
                 "desktop monitors)")
    known = [k for k in ("desktop", "documents", "downloads") if (folder := host.special_folder(k)) and folder.is_dir()]
    parts.append(f"folders: {', '.join(known) or 'none'}")
    report.add("desktop", status, "; ".join(parts), hints=hints)


async def _keys(report: CheckReport, secrets: SecretStore) -> None:
    status = secrets.status()
    configured = [name for name, st in status.items() if st["configured"]]
    where = {"keyring": "Windows Credential Manager" if sys.platform == "win32" else "the system keyring",
             "file": "a private file in JARVIS's data folder", "memory": "memory only"}[secrets.backend_name]
    detail = f"stored in {where}; configured: {', '.join(configured) or 'none'}"
    report.add("keys", "ok", detail, configured=configured)


async def _ai(report: CheckReport, settings: Settings, secrets: SecretStore) -> None:
    from jarvis.ai.router import ProviderRouter

    router = ProviderRouter.from_settings(settings.ai, secrets)
    try:
        st = router.status()
    finally:
        await router.aclose()
    if st["active"]:
        agents = sorted(set(st["agents"].values()) - {None})
        detail = (f"answers with: {st['active']}; agents work with: {', '.join(agents)}"
                  + ("; Anthropic server-side fallbacks on" if st.get("fallbacks") else ""))
        report.add("ai", "ok", detail, **st)
    else:
        report.add("ai", "warn", "no AI provider: commands, projects and plans work; open conversation and "
                   "AI-written code do not",
                   hints=["Add an xAI (Grok) or Anthropic API key: `jarvis keys set XAI_API_KEY` or "
                          "`jarvis keys set ANTHROPIC_API_KEY`, or in JARVIS: Settings > API keys."], **st)


async def _tools(report: CheckReport) -> None:
    from jarvis.tools.dev import find_python

    def version(argv: list[str]) -> str | None:
        exe = shutil.which(argv[0])
        if exe is None:
            return None
        try:
            out = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return None
        found = re.search(r"\d+(?:\.\d+)+", out.stdout + out.stderr)
        return found.group(0) if found else "?"

    found = {name: await asyncio.to_thread(version, argv) for name, argv in
             (("git", ["git", "--version"]), ("node", ["node", "--version"]),
              ("npm", ["npm.cmd" if sys.platform == "win32" else "npm", "--version"]))}
    python = find_python()
    parts = [f"{name} {v}" if v else f"{name} missing" for name, v in found.items()]
    parts.append("python for projects: " + ("yes" if python else "missing"))
    hints = []
    if not found["node"] or not found["npm"]:
        hints.append("Install Node.js LTS (nodejs.org) so JavaScript projects' tests can run.")
    if not found["git"]:
        hints.append("Install Git (git-scm.com) for the Development Agent's version control.")
    report.add("tools", "warn" if hints or not python else "ok", "; ".join(parts), hints=hints)


async def _internet(report: CheckReport, settings: Settings) -> None:
    import httpx

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True,
                                     headers={"User-Agent": settings.web.user_agent}) as client:
            r = await client.get(SEARCH_URL, params={"q": "JARVIS"})
    except httpx.HTTPError as exc:
        hint = ("A proxy blocks the search site; research will report it." if isinstance(exc, httpx.ProxyError)
                else "Research and weather need the internet; everything else works offline.")
        report.add("internet", "warn", f"web search unreachable: {short_error(exc)}", hints=[hint])
        return
    took = time.monotonic() - started
    if r.status_code >= 400:
        report.add("internet", "warn", f"the search site answered {r.status_code}", seconds=took,
                   hints=["A proxy or firewall may block web search; research will report it."])
    else:
        report.add("internet", "ok", "web search reachable", seconds=took)


async def _voice(report: CheckReport, settings: Settings, paths: AppPaths, secrets: SecretStore, *,
                 run_all: bool) -> None:
    import importlib.util

    from jarvis.voice.selftest import PACKAGES, run_selftest

    def have(module: str) -> bool:
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            return False

    if run_all:                     # the full voice self-test, which checks the packages too
        report.note("running the voice self-test (it plays two sentences)")
        result = await run_selftest(settings.voice, paths, secrets, progress=report.progress)
        report.checks += [{**check, "name": f"voice:{check['name']}"} for check in result["checks"]]
        return
    missing = [m for m, (_what, required) in PACKAGES.items() if required and not have(m)]
    enabled = settings.voice.enabled
    if missing:
        report.add("voice", "warn", f"missing: {', '.join(missing)}" + ("" if enabled else " (voice is off)"),
                   hints=['Install the voice extra: pip install "jarvis-assistant[voice]".'])
    else:
        report.add("voice", "ok", "all voice packages installed" + ("" if enabled else " (voice is off in Settings)"))


async def _computer(report: CheckReport, settings: Settings, paths: AppPaths) -> None:
    from jarvis.computer.backend import get_backend
    from jarvis.computer.server import ModelFiles

    s = settings.computer
    if not s.enabled:
        report.add("computer", "ok", "computer control is off in the settings")
        return
    problem = await asyncio.to_thread(get_backend().problem)
    if problem:
        report.add("computer", "skip", problem)
        return
    if s.server_url:
        report.add("computer", "ok", f"uses the model server at {s.server_url} (screenshots are sent there)")
        return
    root = Path(s.models_dir) if s.models_dir else paths.models / "computer"
    files = await asyncio.to_thread(ModelFiles, root)
    missing = files.problem()
    if missing or files.model is None:
        report.add("computer", "warn", missing or "the model is missing", hints=[
            "Install it: jarvis computer install (about 30 GB), or jarvis computer install --adopt <folder> for "
            "a folder that is already downloaded."])
        return
    report.add("computer", "ok", f"model ready: {files.model.name} in {root}")


# ------------------------------------------------------------------------------------------ live
class _Child:
    """A JARVIS process in a temporary folder, driven over its local API."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.proc: asyncio.subprocess.Process | None = None
        self.client: Any = None
        self.log = home / "check-output.log"

    async def start(self) -> None:
        import httpx

        env = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS__")}
        env.update(JARVIS_HOME=str(self.home), PYTHONUTF8="1", JARVIS__VOICE__ENABLED="false",
                   JARVIS__UI__DESKTOP_NOTIFICATIONS="false",                          # no toasts from the check's JARVIS
                   JARVIS__AI__PROVIDER="none", JARVIS_KEYRING="0")       # never the user's stored keys
        if getattr(sys, "frozen", False):
            argv = [sys.executable]
        else:
            argv = [sys.executable, "-m", "jarvis"]
            env["PYTHONPATH"] = os.pathsep.join(x for x in (_source_root(), env.get("PYTHONPATH", "")) if x)
        runtime = self.home / "data" / "runtime.json"
        with contextlib.suppress(OSError):
            runtime.unlink()
        out = self.log.open("ab")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv, "--no-window", "--quiet", "--port", "0", env=env, stdout=out, stderr=out,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        finally:
            out.close()
        deadline = time.monotonic() + LIVE_START_S
        while time.monotonic() < deadline:
            if self.proc.returncode is not None:
                raise RuntimeError(f"JARVIS exited with code {self.proc.returncode}: {self.tail()}")
            info = _read_runtime(runtime)
            if info and self.proc.pid in (info.get("pid"), info.get("ppid")):
                self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{info['port']}", timeout=60.0,
                                                trust_env=False, headers={"X-JARVIS-Token": info["token"]})
                with contextlib.suppress(httpx.HTTPError):
                    if (await self.client.get("/api/health")).status_code == 200:
                        return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"JARVIS did not start within {LIVE_START_S:.0f} s: {self.tail()}")

    def tail(self) -> str:
        with contextlib.suppress(OSError):
            lines = self.log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            return " | ".join(lines[-4:]) or "(no output)"
        return "(no output)"

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        r = await self.client.request(method, path, json=body, timeout=200.0)
        r.raise_for_status()
        return r.json()

    async def say(self, text: str) -> dict[str, Any]:
        result = await self.call("POST", "/api/command", {"text": text})
        return {"intent": (result.get("intent") or {}).get("name"), "text": (result.get("reply") or {}).get("text")
                or "", "task_id": result.get("task_id")}

    async def task(self, task_id: str, timeout: float = LIVE_TASK_S) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            task = await self.call("GET", f"/api/tasks/{task_id}")
            if task["status"] in ("completed", "failed", "cancelled", "interrupted"):
                return task
            if time.monotonic() > deadline:
                raise RuntimeError(f"the task is still {task['status']} after {timeout:.0f} s")
            await asyncio.sleep(0.3)

    async def approval(self, tool: str, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for req in await self.call("GET", "/api/confirmations"):
                if req["tool"] == tool:
                    return req
            await asyncio.sleep(0.2)
        raise RuntimeError(f"no approval request for {tool} appeared")

    async def stop(self) -> int | None:
        if self.proc is None:
            return None
        if self.proc.returncode is None and self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.post("/api/window/quit")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.proc.wait(), 60)
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        return self.proc.returncode


def _source_root() -> str:
    """The folder that holds the `jarvis` package, so `python -m jarvis` finds this same code."""
    import jarvis

    return str(Path(jarvis.__file__).resolve().parents[1])


def _read_runtime(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _until(check: Callable[[], bool], timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.1)
    return True


async def _live(report: CheckReport) -> None:
    home = Path(tempfile.mkdtemp(prefix="jarvis-check-"))
    workspace = home / "workspace"
    child = _Child(home)

    async def step(name: str, job: Callable[[], Any]) -> bool:
        report.note(f"live: {name}")
        started = time.monotonic()
        try:
            detail = await job()
        except Exception as exc:
            report.add(f"live:{name}", "fail", short_error(exc), seconds=time.monotonic() - started)
            return False
        report.add(f"live:{name}", "ok", str(detail), seconds=time.monotonic() - started)
        return True

    async def start() -> str:
        await child.start()
        return f"a JARVIS started in {home}"

    async def folder() -> str:
        said = await child.say("צור תיקייה בשם בדיקת-מערכת")
        if not await _until(lambda: (workspace / "בדיקת-מערכת").is_dir()):
            raise RuntimeError(f"the folder was not created ({said['text']})")
        return "a folder was created by command"

    async def system() -> str:
        said = await child.say("בדוק את המחשב")
        if said["intent"] != "system_status" or "%" not in said["text"]:
            raise RuntimeError(f"unexpected answer: {said['text'][:200]}")
        return "the system check answered with this computer's numbers"

    async def build() -> str:
        await child.say("עצור את סוכן המערכת")          # its last step would open the site in the browser
        said = await child.say("תבנה לי אתר לחנות פרחים")
        if not said["task_id"]:
            raise RuntimeError(f"no task started ({said['text'][:200]})")
        task = await child.task(said["task_id"])
        steps = task["plan"]["subtasks"] if task.get("plan") else []
        await child.say("הפעל את סוכן המערכת")
        if task["status"] != "completed":
            raise RuntimeError(f"the task ended {task['status']}: {task.get('error') or task.get('summary')}")
        sites = [p for p in (home / "projects").glob("*/index.html")]
        if not sites:
            raise RuntimeError("the project folder has no index.html")
        agents = sorted({s["agent"] for s in steps if s["status"] == "completed"})
        development = [s for s in steps if s["agent"] == "development"]
        verify = development[-1] if len(development) > 1 else None      # build, then test and verify
        checked = f"; {verify['summary']}" if verify and verify.get("summary") else ""
        return f"{len(steps)} steps, done by {', '.join(agents)}{checked}"

    async def approval() -> str:
        target = workspace / "בדיקת-מערכת"
        await child.say("תמחק לצמיתות את התיקייה בדיקת-מערכת")
        req = await child.approval("delete_path")
        await child.call("POST", f"/api/confirmations/{req['id']}", {"approved": False})
        await asyncio.sleep(0.5)
        if not target.is_dir():
            raise RuntimeError("the folder was deleted although the approval was refused")
        await child.say("תמחק לצמיתות את התיקייה בדיקת-מערכת")
        req = await child.approval("delete_path")
        await child.call("POST", f"/api/confirmations/{req['id']}", {"approved": True})
        if not await _until(lambda: not target.exists()):
            raise RuntimeError("the folder is still there after the approval")
        return "a permanent delete waited for approval: refused, nothing happened; approved, done"

    async def kill() -> str:
        await child.call("POST", "/api/killswitch", {"engage": True, "reason": "self-check"})
        said = await child.say("צור תיקייה בשם אחרי-חירום")
        await child.call("POST", "/api/killswitch", {"engage": False})
        if (workspace / "אחרי-חירום").exists():
            raise RuntimeError("a command ran while the kill switch was engaged")
        if "מתג החירום" not in said["text"]:
            raise RuntimeError(f"unexpected answer: {said['text'][:200]}")
        return "with the kill switch engaged, commands were refused"

    async def restart() -> str:
        await child.say("תזכור שמילת הבדיקה היא ירח")
        code = await child.stop()
        if code != 0:
            raise RuntimeError(f"JARVIS exited with code {code}: {child.tail()}")
        await child.start()
        said = await child.say("מה אתה זוכר על מילת הבדיקה")
        if "ירח" not in said["text"]:
            raise RuntimeError(f"the memory was not kept: {said['text'][:200]}")
        return "a fact was remembered across a restart"

    try:
        if await step("start", start):
            for name, job in (("folder", folder), ("system", system), ("build", build), ("approve", approval),
                              ("kill", kill), ("restart", restart)):
                await step(name, job)
    finally:
        code = await child.stop()
        await asyncio.sleep(0.3)                    # Windows releases the database file a moment later
        shutil.rmtree(home, ignore_errors=True)
        if code not in (0, None):
            report.add("live:stop", "warn", f"JARVIS exited with code {code}")


# ------------------------------------------------------------------------------------------ CLI
def format_report(report: dict[str, Any]) -> str:
    return format_checks("JARVIS self-check", report, width=12)


def main(settings: Settings, paths: AppPaths, *, full: bool = False, voice: bool = False,
         as_json: bool = False) -> int:
    def progress(message: str) -> None:
        if not as_json:
            print(f"  … {message}", flush=True)

    report = asyncio.run(run_checks(settings, paths, full=full, voice=voice, progress=progress))
    print(json.dumps(report, ensure_ascii=False, indent=2) if as_json else "\n" + format_report(report))
    return 0 if report["ok"] else 1


__all__ = ["format_report", "main", "run_checks"]

