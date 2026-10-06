"""Process-wide logging: rotating JSON-lines file + readable console, both redacted."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path

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


_CONFIGURED = False


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
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "jarvis.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
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
