"""Secret storage.

Lookup order for a secret: OS keyring (Windows Credential Manager) → environment variable →
`<data>/secrets.env` (only used when no keyring backend works, file is created with
user-only permissions). Values are registered with the redactor the moment they are read so
they can never appear in logs or the UI. Only `status()` is exposed to the UI.

`JARVIS_KEYRING=0` keeps a JARVIS away from the keyring (the tests and the temporary JARVIS of
`jarvis check --full` use it, so they never read or change the user's stored keys).
"""

from __future__ import annotations

import logging
import os
import re
import stat
import threading
from pathlib import Path

from jarvis.security.redaction import Redactor, default_redactor, looks_like_secret_name

log = logging.getLogger(__name__)

SERVICE = "JARVIS"
KEYRING_ENV = "JARVIS_KEYRING"

KNOWN_SECRETS: dict[str, str] = {
    "ANTHROPIC_API_KEY": "Anthropic (Claude) API key",
    "OPENAI_API_KEY": "OpenAI API key",
    "LOCAL_MODEL_API_KEY": "Local model server API key (optional)",
    "BRAVE_API_KEY": "Brave Search API key (optional)",
    "TAVILY_API_KEY": "Tavily Search API key (optional)",
    "PICOVOICE_ACCESS_KEY": "Picovoice access key for the Porcupine wake word (optional)",
    "GITHUB_TOKEN": "GitHub token for the Development agent (optional)",
}

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class SecretStore:
    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        redactor: Redactor | None = None,
        use_keyring: bool = True,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._redactor = redactor or default_redactor()
        self._file = (data_dir / "secrets.env") if data_dir else None
        self._lock = threading.Lock()
        self._memory: dict[str, str] = {}
        self._environ = environ if environ is not None else os.environ
        if os.environ.get(KEYRING_ENV, "").strip().lower() in ("0", "false", "no", "off"):
            use_keyring = False
        self._keyring = self._init_keyring() if use_keyring else None

    # -- backends ------------------------------------------------------------------------
    @staticmethod
    def _init_keyring():
        try:
            import keyring
            from keyring.backends import fail

            backend = keyring.get_keyring()
            if isinstance(backend, fail.Keyring) or "fail" in type(backend).__module__:
                return None
            name = type(backend).__name__.lower()
            if "chainer" in name and not getattr(backend, "backends", None):
                return None
            return keyring
        except Exception:  # pragma: no cover - depends on platform backends
            return None

    @property
    def backend_name(self) -> str:
        if self._keyring is not None:
            return "keyring"
        if self._file is not None:
            return "file"
        return "memory"

    def _read_file(self) -> dict[str, str]:
        if not self._file or not self._file.exists():
            return {}
        out: dict[str, str] = {}
        for line in self._file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
        return out

    def _write_file(self, values: dict[str, str]) -> None:
        if not self._file:
            return
        self._file.parent.mkdir(parents=True, exist_ok=True)
        body = "# JARVIS secrets (fallback store; prefer the OS keyring)\n" + "".join(
            f"{k}={v}\n" for k, v in sorted(values.items())
        )
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - Windows ignores POSIX modes
            pass
        os.replace(tmp, self._file)

    # -- public API --------------------------------------------------------------------------
    @staticmethod
    def validate_name(name: str) -> str:
        name = (name or "").strip().upper()
        if not _NAME_RE.match(name):
            raise ValueError(f"Invalid secret name: {name!r}")
        return name

    def get(self, name: str) -> str | None:
        """Internal use only (AI providers, secret-aware tools). Never return this to the UI."""
        name = self.validate_name(name)
        value = self._lookup(name)
        if value:
            self._redactor.register(name, value)
        return value or None

    def _lookup(self, name: str) -> str | None:
        with self._lock:
            if name in self._memory:
                return self._memory[name]
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(SERVICE, name)
                if value:
                    return value
            except Exception as exc:  # pragma: no cover - backend specific
                log.warning("Keyring read failed for %s: %s", name, type(exc).__name__)
        env_value = self._environ.get(name)
        if env_value:
            return env_value
        return self._read_file().get(name)

    def source(self, name: str) -> str | None:
        name = self.validate_name(name)
        with self._lock:
            if name in self._memory:
                return "memory"
        if self._keyring is not None:
            try:
                if self._keyring.get_password(SERVICE, name):
                    return "keyring"
            except Exception:  # pragma: no cover
                pass
        if self._environ.get(name):
            return "env"
        if name in self._read_file():
            return "file"
        return None

    def set(self, name: str, value: str, *, persist: bool = True) -> str:
        name = self.validate_name(name)
        value = (value or "").strip()
        if not value:
            raise ValueError("Secret value is empty")
        self._redactor.register(name, value)
        if not persist:
            with self._lock:
                self._memory[name] = value
            return "memory"
        if self._keyring is not None:
            try:
                self._keyring.set_password(SERVICE, name, value)
                return "keyring"
            except Exception as exc:  # pragma: no cover
                log.warning("Keyring write failed for %s (%s); using file store", name, type(exc).__name__)
        if self._file is not None:
            with self._lock:
                values = self._read_file()
                values[name] = value
                self._write_file(values)
            return "file"
        with self._lock:
            self._memory[name] = value
        return "memory"

    def delete(self, name: str) -> bool:
        name = self.validate_name(name)
        removed = False
        with self._lock:
            removed = self._memory.pop(name, None) is not None or removed
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE, name)
                removed = True
            except Exception:
                pass
        if self._file is not None:
            with self._lock:
                values = self._read_file()
                if name in values:
                    values.pop(name)
                    self._write_file(values)
                    removed = True
        return removed

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def status(self) -> dict[str, dict[str, object]]:
        """Safe summary for the UI: which secrets are configured and where. Never values."""
        out: dict[str, dict[str, object]] = {}
        for name, label in KNOWN_SECRETS.items():
            src = self.source(name)
            out[name] = {"label": label, "configured": src is not None, "source": src}
        return out

    def load_known(self) -> None:
        """Register every configured secret with the redactor (call once at startup)."""
        for name in KNOWN_SECRETS:
            self.get(name)


def sanitized_env(extra: dict[str, str] | None = None, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for child processes with every secret-looking variable removed."""
    source = dict(os.environ if environ is None else environ)
    clean = {k: v for k, v in source.items() if k not in KNOWN_SECRETS and not looks_like_secret_name(k)}
    # Git reads GIT_CONFIG_COUNT/KEY_n/VALUE_n as a set (values can carry auth headers): drop them together.
    for k in [k for k in clean if k.startswith("GIT_CONFIG_")]:
        clean.pop(k)
    clean["PYTHONIOENCODING"] = "utf-8"
    clean["PYTHONUTF8"] = "1"
    if extra:
        clean.update({k: v for k, v in extra.items() if k not in KNOWN_SECRETS})
    return clean
