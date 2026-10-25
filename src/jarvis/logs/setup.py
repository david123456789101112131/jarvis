"""Process-wide logging: rotating JSON-lines file + readable console, both redacted.

Next to the log, crash.log gets every thread's stack when JARVIS dies inside native code (Python's fault
handler), and hang.log when its window stops answering (see `jarvis.ui.desktop`): neither leaves a Python error
to log.
"""

from __future__ import annotations

import faulthandler
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import IO

from jarvis.security.redaction import Redactor, default_redactor


class RedactingFilter(logging.Filter):
    """Masks secrets in the final message and in exception text of every record."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self.redactor = redactor or default_redactor()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            message = str(record.msg)
        record.msg = self.redactor.redact_text(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redactor.redact_text(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("task_id", "agent_id", "tool"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False)


class RotatingLog(logging.handlers.RotatingFileHandler):
    """The log file, started afresh at 5 MB. On Windows the rename fails while another program has the file open,
    and the standard handler then drops every record after it; this one keeps writing to the same file instead."""

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError as exc:
            self.maxBytes = 0                      # no more starting afresh in this run
            if self.stream is None:
                self.stream = self._open()
            self.stream.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": "WARNING",
                                          "logger": __name__, "msg": f"Log file not started afresh: {exc}"},
                                         ensure_ascii=False) + self.terminator)


_CONFIGURED = False
_crash_file: IO[str] | None = None
CRASH_LOG_LIMIT = 1_000_000


def keep_crash_traces(log_dir: Path) -> Path | None:
    """Have Python's fault handler write every thread's stack to crash.log when JARVIS dies inside native code (an
    access violation in a library, say). A line per start dates what follows it. The path, or None if it cannot."""
    global _crash_file
    path = log_dir / "crash.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > CRASH_LOG_LIMIT:
            os.replace(path, path.with_name("crash.log.1"))
        if _crash_file is None:
            _crash_file = path.open("a", encoding="utf-8")       # open for the life of the process
        _crash_file.write(f"--- JARVIS started {time.strftime('%Y-%m-%d %H:%M:%S')} (process {os.getpid()})\n")
        _crash_file.flush()
        faulthandler.enable(file=_crash_file, all_threads=True)
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("Crash traces will not be kept: %s", exc)
        return None
    return path


def setup_logging(log_dir: Path | None, *, level: int = logging.INFO, console: bool = True,
                  redactor: Redactor | None = None) -> None:
    """Idempotent. Call again to change level (e.g. entering DEBUG mode)."""
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(level)
    if _CONFIGURED:
        for h in root.handlers:
            if getattr(h, "_jarvis", False) and isinstance(h, logging.StreamHandler) and not isinstance(
                h, logging.handlers.RotatingFileHandler
            ):
                h.setLevel(level)
        return
    redact = RedactingFilter(redactor)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingLog(log_dir / "jarvis.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        fh.addFilter(redact)
        fh.setLevel(logging.DEBUG)
        fh._jarvis = True  # type: ignore[attr-defined]
        root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        ch.addFilter(redact)
        ch.setLevel(level)
        ch._jarvis = True  # type: ignore[attr-defined]
        root.addHandler(ch)
    for noisy in ("httpx", "httpcore", "httpx2", "uvicorn.access", "websockets", "asyncio", "urllib3", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def set_level(level: int) -> None:
    setup_logging(None, level=level)
