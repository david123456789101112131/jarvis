"""Secret redaction for logs, bus messages, UI payloads and memory.

Two layers:
1. Exact values registered at runtime (every API key the SecretStore loads).
2. Patterns for well-known credential formats, `key=value` style assignments and private keys.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*")),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+\-.]*://[^\s:/@]+:)[^\s@/]{3,}(@)")),
]

# key=value / "key": "value" assignments for secret-looking keys.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>\b[\w\-]*(?:api[_\-]?key|secret|token|password|passwd|pwd|access[_\-]?key|client[_\-]?secret|auth)[\w\-]*)
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?P<value>[^\s"',;&]{6,})
    """
)

SECRET_KEY_HINTS = ("key", "token", "secret", "password", "passwd", "pwd", "credential", "auth", "cookie")


def looks_like_secret_name(name: str) -> bool:
    low = name.lower()
    if low in {"path", "pathext", "psmodulepath", "keyboard_layout"}:
        return False
    return any(h in low for h in SECRET_KEY_HINTS)


class Redactor:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = threading.Lock()
        self._compiled: re.Pattern[str] | None = None

    def register(self, name: str, value: str | None) -> None:
        if not value or len(value) < 6:
            return
        with self._lock:
            self._values[value] = name
            self._compiled = None

    def unregister_value(self, value: str) -> None:
        with self._lock:
            self._values.pop(value, None)
            self._compiled = None

    def _value_pattern(self) -> re.Pattern[str] | None:
        with self._lock:
            if self._compiled is None and self._values:
                alts = sorted(self._values, key=len, reverse=True)
                self._compiled = re.compile("|".join(re.escape(v) for v in alts))
            return self._compiled

    def redact_text(self, text: str) -> str:
        if not text or not isinstance(text, str):
            return text
        pattern = self._value_pattern()
        if pattern is not None:
            text = pattern.sub(lambda m: f"[REDACTED:{self._values.get(m.group(0), 'secret')}]", text)
        for name, pat in _PATTERNS:
            if name == "url_credentials":
                text = pat.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(2)}", text)
            else:
                text = pat.sub(REDACTED, text)
        text = _ASSIGNMENT.sub(lambda m: f"{m.group('key')}{m.group('sep')}{REDACTED}", text)
        return text

    def redact(self, obj: Any, _depth: int = 0) -> Any:
        """Recursively redact strings inside dicts/lists/tuples. Secret-named keys are masked."""
        if _depth > 12:
            return obj
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(k, str) and looks_like_secret_name(k) and isinstance(v, str) and v:
                    out[k] = REDACTED
                else:
                    out[k] = self.redact(v, _depth + 1)
            return out
        if isinstance(obj, list):
            return [self.redact(v, _depth + 1) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.redact(v, _depth + 1) for v in obj)
        return obj

    def find_secrets(self, text: str) -> list[str]:
        """Names of secrets/patterns present in `text` (used to block outgoing data)."""
        if not text:
            return []
        found: list[str] = []
        pattern = self._value_pattern()
        if pattern is not None:
            for m in pattern.finditer(text):
                found.append(self._values.get(m.group(0), "secret"))
        for name, pat in _PATTERNS:
            if name in ("bearer", "url_credentials"):
                continue
            if pat.search(text):
                found.append(name)
        return sorted(set(found))

    def known_names(self) -> Iterable[str]:
        with self._lock:
            return sorted(set(self._values.values()))


_default = Redactor()


def default_redactor() -> Redactor:
    return _default
