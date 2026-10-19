"""What JARVIS keeps on this computer, and deleting it (the "Privacy & data" screen).

`inventory()` lists every kind of data JARVIS saves: how much there is and where, and whether it can be deleted
here; what is kept on purpose, and why; and what can reach the microphone, the screen and the internet.
`clear(keys)` deletes the kinds asked for, for good. Only counts, sizes, places and names leave this module,
never the data itself (no message, fact, path of a user's file or key value).

Work in progress is never cut short: tasks still running (or interrupted, which can still be continued) and
running jobs stay, and the saved work of a screen that is busy right now is left until it is done.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from jarvis.common.i18n import t

if TYPE_CHECKING:
    from jarvis.app import JarvisApp

log = logging.getLogger(__name__)

#: Everything `clear` can delete, in the order the screen lists it.
CLEARABLE = ("chat", "commands", "tasks", "memory", "projects", "files", "jobs", "code", "media", "speed", "logs",
             "temp", "approvals")
#: The kinds kept in the memory database, and what the memory store calls each one.
_MEMORY_KINDS = {"chat": "chat", "commands": "commands", "tasks": "tasks", "memory": "facts",
                 "projects": "projects", "files": "files"}
#: The screens whose saved work a running job of theirs still needs.
_BUSY_AREAS = {"code": ("code",), "media": ("images", "video")}
_LOG_FILES = ("activity.log", "audit.jsonl", "jarvis.log", "computer-model.log")
_AI_KEYS = {"ANTHROPIC_API_KEY": "Claude (Anthropic)", "XAI_API_KEY": "Grok (xAI)", "OPENAI_API_KEY": "OpenAI"}
_ONLINE_STT = {"openai"}                 # speech recognition that sends the recorded command out
_ONLINE_TTS = {"edge"}                   # speech that sends the reply's text out
_NAMES = {
    "chat": ("השיחה", "the conversation"),
    "commands": ("היסטוריית הפקודות", "the command history"),
    "tasks": ("המשימות שהסתיימו", "the finished tasks"),
    "memory": ("מה ש-JARVIS זוכר עליך", "what JARVIS remembers about you"),
    "projects": ("רשימת הפרויקטים", "the list of projects"),
    "files": ("רשימת הקבצים", "the list of files"),
    "jobs": ("היסטוריית העבודה", "the work history"),
    "code": ("שינויי הקוד השמורים", "the saved code changes"),
    "media": ("התמונות והסרטונים השמורים", "the saved images and videos"),
    "speed": ("מדידות המהירות", "the speed measurements"),
    "logs": ("היומנים", "the logs"),
    "temp": ("הקבצים הזמניים", "the temporary files"),
    "approvals": ("האישורים שנשמרו", "the remembered approvals"),
}


class Busy(Exception):
    """The data is in use by work that is still running; nothing was deleted."""


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _files(folder: Path, pattern: str) -> list[Path]:
    try:
        return [p for p in folder.glob(pattern) if p.is_file()] if folder.is_dir() else []
    except OSError:
        return []


def _total(paths: Iterable[Path]) -> int:
    return sum(_size(p) or 0 for p in paths)


def _empty(path: Path) -> bool:
    """Empty a file in place (whoever appends to it keeps doing so)."""
    try:
        with path.open("r+b") as fh:
            fh.truncate(0)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _same_file(a: str | Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _empty_log(path: Path) -> bool:
    """Empty a log file a logging handler keeps open: its stream is closed first (the handler opens the file again
    with its next line), so on Windows nothing holds the file and the next rotation counts from zero."""
    handler = next((h for h in logging.getLogger().handlers
                    if isinstance(h, logging.FileHandler) and _same_file(h.baseFilename, path)), None)
    if handler is None:
        return _empty(path)
    handler.acquire()
    try:
        if handler.stream is not None:
            with contextlib.suppress(Exception):
                handler.stream.close()
            handler.stream = None  # type: ignore[assignment]
        return _empty(path)
    finally:
        handler.release()


class PrivacyService:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app

    # ------------------------------------------------------------------ what is kept
    async def inventory(self) -> dict[str, Any]:
        stats = await self.app.memory.stats()
        files = await asyncio.to_thread(self._file_rows)
        db = str(self.app.paths.db)
        rows = [
            self._row("chat", stats.get("chat", 0), where=[db]),
            self._row("commands", stats.get("commands", 0), where=[db]),
            self._row("tasks", stats.get("finished_tasks", 0), where=[db],
                      kept=stats.get("tasks", 0) - stats.get("finished_tasks", 0)),
            self._row("memory", stats.get("facts", 0), where=[db]),
            self._row("projects", stats.get("projects", 0), where=[db]),
            self._row("files", stats.get("files", 0) + files["organize"]["count"], where=[db, files["organize"]["where"]],
                      size=files["organize"]["bytes"] or None),
            *files["rows"],
            self._row("approvals", len(self._remembered_grants()), where=[str(self.app.paths.data / "permissions.json")]),
        ]
        order = {key: i for i, key in enumerate(CLEARABLE)}
        rows.sort(key=lambda r: order[r["key"]])
        return {
            "items": rows,
            "database": {"where": db, "bytes": _total(Path(db + suffix) for suffix in ("", "-wal", "-shm"))},
            "kept": self._kept(),
            "senses": self._senses(),
        }

    def _row(self, key: str, count: int, *, where: list[str], size: int | None = None, **extra: Any) -> dict[str, Any]:
        busy = any(j.area in _BUSY_AREAS.get(key, ()) for j in self.app.jobs.running())
        return {"key": key, "count": int(count), "bytes": size, "where": where, "can_clear": True, "busy": busy,
                **extra}

    def _file_rows(self) -> dict[str, Any]:
        """The kinds kept in files (counted in a worker thread: it touches the disk)."""
        app, paths = self.app, self.app.paths
        organize = _files(paths.data / "organize-logs", "*.json")
        jobs_file = paths.data / "jobs.jsonl"
        finished = [j for j in app.jobs.recent(limit=1_000_000) if j.status != "running"]
        dev = app.dev.inventory()
        speed = sum(int(s.get("count", 0)) for s in app.metrics.summary().values())
        logs = self._log_paths()
        temp = self._temp_paths()
        rows = [
            self._row("jobs", len(finished), where=[str(jobs_file)], size=_size(jobs_file),
                      running=len(app.jobs.running())),
            self._row("code", int(dev.get("changes", 0)), where=[str(app.dev.dir)], size=int(dev.get("bytes", 0)) or None,
                      cached=int(dev.get("cached_projects", 0))),
            self._row("speed", speed, where=[str(app.metrics.file)] if app.metrics.file else [],
                      size=_size(app.metrics.file) if app.metrics.file else None),
            self._row("logs", len(logs), where=[str(paths.logs)], size=_total(logs)),
            self._row("temp", len(temp), where=[str(paths.tts_cache), str(paths.cache)], size=_total(temp)),
        ]
        media = self._media_inventory()
        if media is not None:
            rows.append(self._row("media", int(media.get("count", 0)), where=[str(w) for w in media.get("where", [])],
                                  size=media.get("bytes")))
        return {"rows": rows, "organize": {"count": len(organize), "bytes": _total(organize),
                                           "where": str(paths.data / "organize-logs")}}

    def _media_inventory(self) -> dict[str, Any] | None:
        inventory = getattr(self.app.media, "inventory", None)
        if inventory is None:
            return None
        try:
            return dict(inventory())
        except Exception:
            log.exception("Counting the saved images and videos failed")
            return None

    def _log_paths(self) -> list[Path]:
        folder = self.app.paths.logs
        found = [folder / name for name in _LOG_FILES if (_size(folder / name) or 0) > 0]
        return found + _files(folder, "*.log.[0-9]*")

    def _temp_paths(self) -> list[Path]:
        return _files(self.app.paths.tts_cache, "*.wav") + _files(self.app.paths.cache, "app_index_*.json")

    def _remembered_grants(self) -> list[Any]:
        return [g for g in self.app.permissions.grants(include_expired=True) if g.source != "policy"]

    def _kept(self) -> list[dict[str, Any]]:
        """What this screen does not delete, and where it is (the screen says why for each)."""
        status = self.app.secrets.status()
        keys = [info["label"] for info in status.values() if info.get("configured")]
        sources = sorted({str(info.get("source")) for info in status.values() if info.get("configured")})
        paths = self.app.paths
        return [
            {"key": "keys", "count": len(keys), "names": keys, "where": sources},
            {"key": "settings", "where": [str(self.app.config_file)]},
            {"key": "work", "where": [str(paths.workspace), str(paths.projects)]},
            {"key": "models", "where": list(dict.fromkeys([str(paths.models), str(self.app.computer.root())]))},
        ]

    def _senses(self) -> dict[str, Any]:
        """What can reach the microphone, the screen and the internet, as it is set up right now."""
        s = self.app.settings
        voice = self.app.voice_info()
        engines = voice.get("engines") or {}
        stt, tts = str(engines.get("stt") or "none"), str(engines.get("tts") or "none")
        remote = s.computer.server_url.strip()
        status = self.app.secrets.status()
        return {
            "microphone": {
                "enabled": bool(voice.get("enabled")), "state": voice.get("state"), "muted": bool(voice.get("muted")),
                "wake_word": voice.get("wake_word") or "", "always_listening": s.voice.always_listening,
                "ctrl_to_talk": s.voice.ctrl_to_talk, "stt": stt, "stt_online": stt in _ONLINE_STT, "tts": tts,
                "tts_online": tts in _ONLINE_TTS,
            },
            "screen": {
                "computer_enabled": s.computer.enabled,
                "model": "remote" if remote else "local",
                "remote_host": (urlparse(remote).hostname or remote) if remote else None,
            },
            "cloud": {
                "only_when_told": s.ai.cloud_only_when_told,
                "ai": [{"name": label, "configured": bool(status.get(key, {}).get("configured"))}
                       for key, label in _AI_KEYS.items()],
            },
        }

    # ------------------------------------------------------------------ deleting
    async def clear(self, keys: Iterable[str]) -> dict[str, Any]:
        """Delete each kind asked for; one result per kind ({ok, count?, reason?}), then the new inventory."""
        wanted = list(dict.fromkeys(keys))
        unknown = [k for k in wanted if k not in CLEARABLE]
        if unknown:
            raise ValueError(f"unknown kind of data: {', '.join(unknown)}")
        results: dict[str, dict[str, Any]] = {}
        memory_kinds = [k for k in wanted if k in _MEMORY_KINDS]
        if memory_kinds:
            results.update(await self._clear_memory(memory_kinds))
        for key in wanted:
            if key in _MEMORY_KINDS:
                continue
            try:
                results[key] = {"ok": True, **await self._clear_one(key)}
            except Busy:
                results[key] = {"ok": False, "reason": "busy"}
            except Exception:
                log.exception("Deleting %s failed", key)
                results[key] = {"ok": False, "reason": "error"}
        done = [k for k in wanted if results.get(k, {}).get("ok")]
        if done:
            lang = self.app.lang
            names = ", ".join(_NAMES[k][0 if lang == "he" else 1] for k in done)
            await self.app.activity.log(t(lang, f"נמחקו לבקשתך: {names}", f"Deleted at your request: {names}"),
                                        source="JARVIS", kind="privacy")
            if self.app.hub is not None:
                self.app.hub.broadcast("state", await self.app.state_snapshot())
        return {"results": results, "inventory": await self.inventory()}

    async def _clear_memory(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        if "chat" in keys:
            await self.app.core.clear_chat()
        try:
            counts = await self.app.memory.clear([_MEMORY_KINDS[k] for k in keys])
        except Exception:
            log.exception("Deleting from memory failed")
            return {k: {"ok": False, "reason": "error"} for k in keys}
        out: dict[str, dict[str, Any]] = {}
        for key in keys:
            out[key] = {"ok": True, "count": counts.get(_MEMORY_KINDS[key], 0)}
        if "tasks" in keys:
            self.app.orchestrator.forget_finished()
        if "files" in keys:
            removed = await asyncio.to_thread(self._clear_organize_logs)
            out["files"]["count"] += removed
        return out

    async def _clear_one(self, key: str) -> dict[str, Any]:
        app = self.app
        if any(j.area in _BUSY_AREAS.get(key, ()) for j in app.jobs.running()):
            raise Busy(key)
        if key == "jobs":
            return {"count": app.jobs.clear_history(), "kept": len(app.jobs.running())}
        if key == "code":
            out = app.dev.clear()
            return {"count": int(out.get("changes", 0))}
        if key == "media":
            clear = getattr(app.media, "clear", None)
            if clear is None:
                return {"count": 0}
            out = await clear() if asyncio.iscoroutinefunction(clear) else clear()
            return {"count": int(dict(out or {}).get("count", 0))}
        if key == "speed":
            count = sum(int(s.get("count", 0)) for s in app.metrics.summary().values())
            app.metrics.clear()
            return {"count": count}
        if key == "logs":
            return await asyncio.to_thread(self._clear_logs)
        if key == "temp":
            return await asyncio.to_thread(self._clear_temp)
        if key == "approvals":
            grants = self._remembered_grants()
            return {"count": sum(int(app.permissions.revoke(g.id)) for g in grants)}
        raise ValueError(key)

    def _clear_organize_logs(self) -> int:
        removed = 0
        for path in _files(self.app.paths.data / "organize-logs", "*.json"):
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
        return removed

    def _clear_logs(self) -> dict[str, Any]:
        """Empty the log files in place (JARVIS goes on writing to them) and delete the older rotated copies.
        The computer-control model's log waits while that model runs: it writes there right now."""
        app, folder = self.app, self.app.paths.logs
        count, in_use = 0, []
        for name in _LOG_FILES:
            path = folder / name
            if not (_size(path) or 0):
                continue
            if name == "computer-model.log" and app.computer.server.running:
                in_use.append(name)
                continue
            count += int(_empty_log(path) if name == "jarvis.log" else _empty(path))
        for backup in _files(folder, "*.log.[0-9]*"):
            with contextlib.suppress(OSError):
                backup.unlink()
                count += 1
        app.activity.clear()
        app.audit.clear()
        return {"count": count, "in_use": in_use}

    def _clear_temp(self) -> dict[str, Any]:
        count = 0
        for path in self._temp_paths():
            with contextlib.suppress(OSError):
                path.unlink()
                count += 1
        return {"count": count}


__all__ = ["CLEARABLE", "Busy", "PrivacyService"]
