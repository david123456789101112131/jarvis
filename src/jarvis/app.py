"""Composition root: builds every JARVIS service, wires them together and runs their lifecycle.

    settings → paths → secrets/redactor → bus → logs → path guard → permissions → approvals
    → kill switch → tools → host adapter → AI router → memory → agents → orchestrator → core
    → voice → local server (REST + WebSocket) → system monitor

Nothing else constructs services: tests and the CLI build a `JarvisApp` (optionally with a
fake host or a scripted AI router) and call `start()` / `shutdown()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from jarvis import __version__
from jarvis.agents.base import AgentRuntime
from jarvis.agents.manager import AgentManager
from jarvis.ai.router import ProviderRouter
from jarvis.bus import MessageBus
from jarvis.bus.message import Message, event
from jarvis.common.clock import iso
from jarvis.common.enums import Mode, TaskStatus
from jarvis.config.loader import load_settings, save_settings, update_settings
from jarvis.config.paths import AppPaths, default_config_file, resolve_paths
from jarvis.config.settings import Settings
from jarvis.core.core import JarvisCore
from jarvis.logs.activity import ActivityLog
from jarvis.logs.audit import AuditLog
from jarvis.memory import create_memory
from jarvis.orchestration.orchestrator import TaskOrchestrator
from jarvis.permissions import ConfirmationBroker, PermissionManager
from jarvis.security import KillSwitch, PathGuard
from jarvis.security.path_guard import (
    default_denied_roots,
    default_user_write_roots,
    is_within,
    normalize_path,
)
from jarvis.security.redaction import default_redactor
from jarvis.security.secrets import KNOWN_SECRETS, SecretStore
from jarvis.tools import ToolExecutor, ToolServices
from jarvis.tools.builtin import create_registry
from jarvis.tools.fs import RUNS_CODE_EXT

if TYPE_CHECKING:
    from jarvis.agents.base import BaseAgent
    from jarvis.host.base import HostAdapter
    from jarvis.orchestration.task import JarvisTask
    from jarvis.permissions.confirm import ConfirmationRequest
    from jarvis.server.hub import EventHub
    from jarvis.server.runner import LocalServer
    from jarvis.voice.base import SpeechReply, Transcript
    from jarvis.voice.service import VoiceService

log = logging.getLogger(__name__)

STATS_INTERVAL_S = 2.0
RUNTIME_FILE = "runtime.json"
# Files the UI only shows in Explorer, never opens: opening them would run code.
REVEAL_ONLY_EXT = RUNS_CODE_EXT
UI_KEYS = ("theme_color", "orb_size", "orb_opacity", "follow_mouse", "always_active", "mini_orb")
# Changing these needs a restart (they shape objects built once at start-up).
RESTART_SECTIONS = ("paths", "server", "memory")

QUICK_TOOLS = (
    ("chrome", "Chrome", "כרום", "chrome", "open Chrome", "פתח את כרום"),
    ("youtube", "YouTube", "יוטיוב", "youtube", "open YouTube", "פתח את יוטיוב"),
    ("calculator", "Calculator", "מחשבון", "calculator", "open Calculator", "פתח את המחשבון"),
    ("settings", "Settings", "הגדרות", "settings", "open Settings", "פתח את ההגדרות"),
    ("files", "Files", "קבצים", "folder", "open File Explorer", "פתח את סייר הקבצים"),
)

AgentFactory = Callable[[AgentRuntime], "list[BaseAgent]"]
WindowHandler = Callable[[str], "Awaitable[bool] | bool"]


def app_protected_roots() -> list[Path]:
    """JARVIS's own program files: agents may never change the assistant itself."""
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    else:
        package = Path(__file__).resolve().parent
        roots.append(package)
        if package.parent.name == "src":                 # running from a source checkout
            roots.append(package.parent.parent)
    if sys.prefix != sys.base_prefix:                    # the virtual environment JARVIS runs in
        roots.append(Path(sys.prefix).resolve())
    return roots


class JarvisApp:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        config_file: Path | None = None,
        host: HostAdapter | None = None,
        router: ProviderRouter | None = None,
        agents: AgentFactory | None = None,
        voice: bool | None = None,
        use_keyring: bool = True,
        serve: bool = True,
        port: int | None = None,
        dev_origins: tuple[str, ...] = (),
    ) -> None:
        self.config_file = config_file or default_config_file()
        self.settings = settings or load_settings(self.config_file)
        s = self.settings
        self.paths: AppPaths = resolve_paths(s.paths, s.memory.db_path).ensure()
        self.redactor = default_redactor()
        self.secrets = SecretStore(self.paths.data, redactor=self.redactor, use_keyring=use_keyring)
        self.secrets.load_known()

        self.bus = MessageBus(sanitizer=self._sanitize)
        self.activity = ActivityLog(self.paths.logs, redactor=self.redactor)
        self.audit = AuditLog(self.paths.logs, redactor=self.redactor)
        extra_denied = [normalize_path(p) for p in s.security.protected_paths]
        self.guard = PathGuard(
            workspace=self.paths.workspace, projects=self.paths.projects,
            write_roots=[normalize_path(p) for p in s.paths.extra_write_roots],
            read_roots=None,
            denied_roots=default_denied_roots() + extra_denied,
            protected_roots=[self.paths.data, self.paths.logs, self.paths.cache, self.paths.db.parent,
                             self.config_file.parent, *app_protected_roots()],
            allowed_shares=s.security.allowed_network_shares,
        )
        self.user_roots = default_user_write_roots()
        self.permissions = PermissionManager(self.guard, s.security, store_file=self.paths.data / "permissions.json",
                                             user_roots=self.user_roots)
        self.broker = ConfirmationBroker(self.bus, timeout_s=s.security.confirm_timeout_s)
        self.kill_switch = KillSwitch()
        self.registry = create_registry()
        if host is None:
            from jarvis.host.base import get_host_adapter

            host = get_host_adapter(cache_dir=self.paths.cache)
        self.host = host
        self.router = router or ProviderRouter.from_settings(s.ai, self.secrets)
        self.memory = create_memory(self.paths.db, backend=s.memory.backend, short_term_turns=s.memory.short_term_turns,
                                    recall_limit=s.memory.recall_limit, redactor=self.redactor)
        self.services = ToolServices(settings=s, paths=self.paths, guard=self.guard, redactor=self.redactor,
                                     secrets=self.secrets, host=host, memory=self.memory, ai=self.router, bus=self.bus)
        self.executor = ToolExecutor(self.registry, self.services, self.permissions, self.broker, self.kill_switch,
                                     audit=self.audit, activity=self.activity, mode_provider=self._mode)
        self.runtime = AgentRuntime(settings=s, executor=self.executor, registry=self.registry,
                                    permissions=self.permissions, bus=self.bus, paths=self.paths, router=self.router,
                                    memory=self.memory, activity=self.activity)
        self.manager = AgentManager(self.runtime, self.kill_switch, broker=self.broker,
                                    agents=agents(self.runtime) if agents else None)
        self.orchestrator = TaskOrchestrator(self.manager, mode_provider=self._mode)
        self.core = JarvisCore(settings=s, manager=self.manager, orchestrator=self.orchestrator, memory=self.memory,
                               bus=self.bus, broker=self.broker, kill_switch=self.kill_switch, activity=self.activity,
                               router=lambda: self.runtime.router)

        self.voice: VoiceService | None = None
        self.voice_wanted = s.voice.enabled if voice is None else voice
        self._level_listeners: list[Callable[[float], None]] = []

        from jarvis.server.auth import ServerAuth

        self.auth = ServerAuth(extra_origins=dev_origins)
        self.serve_api = serve
        self.port_wanted = s.server.port if port is None else port
        self.hub: EventHub | None = None
        self.server: LocalServer | None = None

        self.last_stats: dict[str, Any] | None = None
        self._monitor: Any = None
        self.window_handler: WindowHandler | None = None
        self.ui_visible = False
        self.started_at: float | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._started = False
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # ================================================================== properties
    def _mode(self) -> Mode:
        return self.core.mode if hasattr(self, "core") else Mode(self.settings.general.default_mode)

    @property
    def port(self) -> int | None:
        return self.server.port if self.server else None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def launch_url(self) -> str:
        """Opens the UI with a fresh session (the token is swapped for a cookie at once)."""
        return f"http://127.0.0.1:{self.port}/?launch={self.auth.token}"

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    # ================================================================== lifecycle
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        self.started_at = time.monotonic()
        self.bus.bind_loop()
        self.activity.add_sink(self._activity_to_bus)
        self.audit.add_sink(self._audit_to_bus)
        self.kill_switch.add_listener(self._kill_to_bus)
        self.broker.add_listener(self._on_confirm_request)
        self.orchestrator.add_listener(self._on_task_event)

        await self.memory.start()
        self.spawn(self._warm_host(), "warm-host")
        await self.manager.start()
        await self.core.start()
        if self.voice_wanted:
            await self._start_voice()
        if self.serve_api:
            await self._start_server()
        self.spawn(self._monitor_loop(), "system-monitor")
        await self.activity.log(f"JARVIS {__version__} is online"
                                + (f" ({self.url})" if self.server else ""), source="JARVIS", level="success",
                                kind="system")

    async def shutdown(self) -> None:
        if not self._started or self._stopping:
            return
        self._stopping = True
        log.info("JARVIS is shutting down")
        if self.voice is not None:
            with contextlib.suppress(Exception):
                await self.voice.stop()
        with contextlib.suppress(Exception):
            await self.core.shutdown()
        with contextlib.suppress(Exception):
            await self.manager.shutdown()
        with contextlib.suppress(Exception):
            self.broker.deny_all("JARVIS is shutting down")
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
        if self.hub is not None:
            with contextlib.suppress(Exception):
                await self.hub.close()
        if self.server is not None:
            with contextlib.suppress(Exception):
                await self.server.stop()
        await asyncio.to_thread(self.services.processes.kill)
        with contextlib.suppress(Exception):
            await self.services.aclose()
        with contextlib.suppress(Exception):
            await self.router.aclose()
        with contextlib.suppress(Exception):
            await self.memory.close()
        self._remove_runtime_file()
        log.info("JARVIS stopped")

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=f"jarvis-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Background job %s failed", task.get_name(), exc_info=task.exception())

    async def _warm_host(self) -> None:
        """Index the installed apps in the background so the first "open X" is instant."""
        with contextlib.suppress(Exception):
            await self.host.installed_apps()

    async def _start_server(self) -> None:
        from jarvis.server.api import create_api
        from jarvis.server.hub import EventHub
        from jarvis.server.runner import LocalServer

        self.hub = EventHub(self.bus)
        self._level_listeners.append(lambda level: self.hub.broadcast("voice.level", {"level": level})
                                     if self.hub else None)
        s = self.settings.server
        self.server = LocalServer(create_api(self), host=s.host, port=self.port_wanted, span=s.port_fallback_range)
        port = await self.server.start()
        self.auth.port = port
        self.services.extras["server_port"] = port        # the agents' web tools never call this server
        self._write_runtime_file()

    async def _start_voice(self) -> None:
        from jarvis.voice.service import VoiceService

        self.voice = VoiceService(self.settings.voice, self.paths, self.secrets, handler=self._voice_command,
                                  events=_VoiceEvents(self))
        self.core.set_speaker(self._speak_reply)
        await self.voice.start()

    # ================================================================== voice
    async def _voice_command(self, transcript: Transcript) -> SpeechReply | None:
        from jarvis.voice.base import SpeechReply

        result = await self.core.voice_command(transcript.text, transcript.language)
        speech = result.speech
        if not speech:
            return None
        return SpeechReply(text=speech.get("text"), language=speech.get("language"), ack_key=speech.get("ack_key"),
                           follow_up=bool(speech.get("follow_up", True)))

    async def _speak_reply(self, text: str, language: str) -> None:
        if self.voice is not None:
            await self.voice.speak(text, language, wait=False)

    async def speak(self, text: str) -> bool:
        if self.voice is None:
            return False
        return await self.voice.speak(text, None, wait=False)

    def voice_info(self) -> dict[str, Any]:
        if self.voice is not None:
            return self.voice.info()
        return {"state": "disabled", "enabled": False, "muted": False, "wake_word": "",
                "engines": {"wake": "none", "stt": "none", "tts": "none"}, "devices": {"input": None, "output": None},
                "last_transcript": None,
                "error": "Voice is turned off in the settings." if not self.voice_wanted else "Voice is not running."}

    async def voice_action(self, action: str) -> dict[str, Any]:
        if action == "start":
            self.voice_wanted = True
            if self.voice is None:
                await self._start_voice()
            else:
                await self.voice.start()
        elif self.voice is not None:
            if action == "stop":
                await self.voice.stop()
            elif action == "mute":
                self.voice.mute()
            elif action == "unmute":
                self.voice.unmute()
            elif action == "ptt_start":
                self.voice.push_to_talk_start()
            elif action == "ptt_stop":
                self.voice.push_to_talk_stop()
            elif action == "cancel":
                self.voice.cancel()
        return self.voice_info()

    def add_level_listener(self, listener: Callable[[float], None]) -> None:
        self._level_listeners.append(listener)

    # ================================================================== state for the UI
    async def state_snapshot(self) -> dict[str, Any]:
        s = self.settings
        recent = await self.orchestrator.list_tasks(limit=30)
        return {
            "version": __version__,
            "assistant_name": s.general.assistant_name,
            "user_name": s.general.user_name,
            "language": s.general.language,
            "mode": self.core.mode.value,
            "voice": self.voice_info(),
            "ui": self.ui_settings(),
            "agents": self.manager.views(),
            "tasks": {"active": [t.summary_view() for t in self.orchestrator.active_tasks()], "recent": recent},
            "chat": await self.core.chat_history(100),
            "activity": [e.model_dump(mode="json") for e in self.activity.recent(200)],
            "confirmations": [r.public() for r in self.broker.pending()],
            "kill_switch": self.kill_switch.snapshot(),
            "system": self.last_stats,
            "providers": self.router.status(),
            "files": {"workspace": str(self.paths.workspace), "projects": str(self.paths.projects)},
        }

    def ui_settings(self) -> dict[str, Any]:
        ui = self.settings.ui.model_dump(mode="json")
        return {k: ui[k] for k in UI_KEYS}

    def quick_tools(self, lang: str | None = None) -> list[dict[str, Any]]:
        lang = lang if lang in ("he", "en") else ("en" if self.settings.general.language == "en" else "he")
        return [{"id": i, "label": label, "label_he": label_he, "icon": icon, "command": he if lang == "he" else en}
                for i, label, label_he, icon, en, he in QUICK_TOOLS]

    async def sample_system(self) -> dict[str, Any]:
        if self._monitor is None:
            from jarvis.host.monitor import SystemMonitor

            self._monitor = await asyncio.to_thread(SystemMonitor)
        stats = await asyncio.to_thread(self._monitor.sample)
        self.last_stats = stats
        return stats

    async def _monitor_loop(self) -> None:
        while True:
            try:
                stats = await self.sample_system()
                await self.bus.publish(event("system.stats", stats, sender="monitor"))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("System monitor sample failed", exc_info=True)
            await asyncio.sleep(STATS_INTERVAL_S)

    # ================================================================== settings & keys
    async def update_settings(self, patch: dict[str, Any]) -> list[str]:
        """Apply a partial settings change to the live settings, save it, and tell the parts that care.
        Returns the sections that need a restart to take effect."""
        fresh = update_settings(self.settings, patch)        # validates the whole result first
        _apply_in_place(self.settings, fresh, patch)
        await asyncio.to_thread(save_settings, self.settings, self.config_file)
        restart = [k for k in patch if k in RESTART_SECTIONS]
        if "ui" in patch:
            await self.bus.publish(event("settings", {"ui": self.ui_settings()}, sender="settings"))
        if "voice" in patch and self.voice is not None:
            self.spawn(self.voice.restart(self.settings.voice), "voice-restart")
        if "ai" in patch:
            self.router.refresh()
        if "security" in patch:
            self.broker.timeout_s = self.settings.security.confirm_timeout_s
        await self.activity.log("Settings updated: " + ", ".join(sorted(patch)), source="Settings", kind="settings")
        if restart:
            await self.notify("info", "JARVIS", "Some changes apply after JARVIS restarts: " + ", ".join(restart))
        return restart

    async def set_secret(self, name: str, value: str) -> str:
        if name not in KNOWN_SECRETS:
            raise KeyError(name)
        source = await asyncio.to_thread(self.secrets.set, name, value)
        self.router.refresh()
        await self.activity.log(f"{KNOWN_SECRETS[name]} saved ({source})", source="Settings", kind="settings")
        return source

    async def delete_secret(self, name: str) -> None:
        await asyncio.to_thread(self.secrets.delete, name)
        self.router.refresh()
        await self.activity.log(f"{KNOWN_SECRETS.get(name, name)} removed", source="Settings", kind="settings")

    # ================================================================== files (UI)
    def _browse_roots(self) -> list[Path]:
        return [self.paths.workspace, self.paths.projects]

    async def list_folder(self, path: str | None) -> dict[str, Any]:
        target = normalize_path(path) if path else self.paths.workspace
        if not any(is_within(target, r) for r in self._browse_roots()) or self.guard.is_denied(target):
            raise PermissionError("Only the JARVIS workspace and projects folders can be browsed here.")
        if not target.is_dir():
            raise FileNotFoundError(f"No folder {target}")

        def scan() -> list[dict[str, Any]]:
            rows = []
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        st = entry.stat()
                        is_dir = entry.is_dir()
                    except OSError:
                        continue
                    rows.append({"name": entry.name, "path": str(Path(entry.path)), "is_dir": is_dir,
                                 "size": None if is_dir else st.st_size,
                                 "modified": iso(_from_ts(st.st_mtime))})
            rows.sort(key=lambda r: (not r["is_dir"], r["name"].lower()))
            return rows[:1000]

        return {"path": str(target), "entries": await asyncio.to_thread(scan)}

    async def open_path(self, path: str, *, reveal: bool = False) -> str:
        """Open a file or folder the agents produced (or show it in Explorer). Returns "opened"/"revealed"."""
        target = normalize_path(path)
        allowed = [*self._browse_roots(), *self.user_roots]
        if self.guard.is_denied(target) or not any(is_within(target, r) for r in allowed):
            raise PermissionError("JARVIS opens only files in its workspace, its projects and your personal folders.")
        if not target.exists():
            raise FileNotFoundError(f"{target} does not exist")
        if not reveal and target.is_file() and target.suffix.lower() in REVEAL_ONLY_EXT:
            reveal = True                                  # never run a program from a click in the UI
        if reveal:
            await self.host.reveal(target)
        else:
            await self.host.open_path(target)
        return "revealed" if reveal else "opened"

    # ================================================================== window & notifications
    async def window_action(self, action: str) -> bool:
        handler = self.window_handler
        if handler is None:
            return False
        result = handler(action)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            result = await result
        return bool(result)

    async def notify(self, level: str, title: str, message: str, *, toast: bool | None = None) -> None:
        """A toast in the UI; a Windows notification too when the window is not on screen."""
        await self.bus.publish(event("notification", {"id": f"n{time.monotonic_ns()}", "level": level,
                                                      "title": title, "message": message}, sender="jarvis"))
        if toast if toast is not None else not self.ui_visible:
            try:
                await self.host.notify(title, message[:240])
            except Exception as exc:
                log.debug("Desktop notification failed: %s", exc)

    async def _on_task_event(self, kind: str, task: JarvisTask) -> None:
        if kind != "finished" or self._stopping:
            return
        if task.status == TaskStatus.COMPLETED:
            level = "success"
        elif task.status == TaskStatus.FAILED:
            level = "error"
        else:
            return
        summary = task.outcome.summary if task.outcome else (task.error or "")
        await self.notify(level, task.title, summary)

    async def _on_confirm_request(self, req: ConfirmationRequest) -> None:
        if not self.ui_visible:
            title = "JARVIS מבקש אישור" if self.settings.general.language != "en" else "JARVIS needs your approval"
            with contextlib.suppress(Exception):
                await self.host.notify(title, req.summary[:240])

    # ================================================================== bus plumbing
    def _sanitize(self, msg: Message) -> Message:
        """Every bus message (and so every UI event) is redacted: no secret ever leaves the core."""
        r = self.redactor
        update: dict[str, Any] = {}
        if msg.data:
            update["data"] = r.redact(msg.data)
        if msg.instruction:
            update["instruction"] = r.redact_text(msg.instruction)
        if msg.context:
            update["context"] = r.redact(msg.context)
        if msg.result is not None:
            update["result"] = r.redact(msg.result)
        return msg.model_copy(update=update) if update else msg

    async def _activity_to_bus(self, entry: Any) -> None:
        await self.bus.publish(event("activity", entry.model_dump(mode="json"), sender="activity",
                                     task_id=entry.task_id, agent_id=entry.agent_id))

    async def _audit_to_bus(self, entry: Any) -> None:
        await self.bus.publish(event("audit", entry.model_dump(mode="json"), sender="audit", task_id=entry.task_id,
                                     agent_id=entry.agent_id))

    async def _kill_to_bus(self, evt: Any) -> None:
        await self.bus.publish(event("killswitch", self.kill_switch.snapshot(), sender="security"))

    def _emit_level(self, level: float) -> None:
        for listener in list(self._level_listeners):
            with contextlib.suppress(Exception):
                listener(level)

    # ================================================================== runtime file (single instance)
    @property
    def runtime_file(self) -> Path:
        return self.paths.data / RUNTIME_FILE

    def _write_runtime_file(self) -> None:
        info = {"pid": os.getpid(), "port": self.port, "token": self.auth.token, "version": __version__,
                "started_at": iso()}
        path = self.runtime_file
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        os.replace(tmp, path)

    def _remove_runtime_file(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            info = json.loads(self.runtime_file.read_text(encoding="utf-8"))
            if info.get("pid") == os.getpid():
                self.runtime_file.unlink()


class _VoiceEvents:
    """Voice pipeline → UI: state, transcripts, steps and speech on the bus; mic levels straight to the UI."""

    def __init__(self, app: JarvisApp) -> None:
        self.app = app

    async def on_state(self, state: Any, info: dict[str, Any]) -> None:
        await self.app.bus.publish(event("voice.state", {"state": getattr(state, "value", str(state)),
                                                         "muted": bool(info.get("muted")), "error": info.get("error"),
                                                         "trigger": info.get("trigger")}, sender="voice"))

    async def on_level(self, level: float) -> None:
        self.app._emit_level(round(float(level), 3))

    async def on_transcript(self, transcript: Any) -> None:
        await self.app.bus.publish(event("voice.transcript", {"text": transcript.text, "final": transcript.final,
                                                              "language": transcript.language}, sender="voice"))

    async def on_pipeline(self, step: str, status: str, label: str) -> None:
        await self.app.bus.publish(event("voice.pipeline", {"step": step, "status": status, "label": label},
                                         sender="voice"))

    async def on_speech(self, text: str, language: str) -> None:
        await self.app.bus.publish(event("speech", {"text": text, "language": language}, sender="voice"))


def _apply_in_place(live: Any, fresh: Any, patch: dict[str, Any]) -> None:
    """Copy the patched fields from a validated copy into the live settings objects, so every service
    holding a section (the router, the voice service, the permission manager) sees the change."""
    for key, value in patch.items():
        if not hasattr(live, key):
            continue
        current = getattr(live, key)
        new = getattr(fresh, key)
        if isinstance(value, dict) and isinstance(current, BaseModel):
            _apply_in_place(current, new, value)
        else:
            setattr(live, key, new)


def _from_ts(ts: float) -> Any:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC)


def read_runtime_file(paths: AppPaths) -> dict[str, Any] | None:
    """The running instance's {pid, port, token, ...}, when one is alive."""
    path = paths.data / RUNTIME_FILE
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        import psutil

        if not psutil.pid_exists(int(info.get("pid", -1))):
            return None
    except Exception:
        return None
    return info


__all__ = ["JarvisApp", "app_protected_roots", "read_runtime_file"]
