"""Load and save settings.

Precedence (lowest → highest): model defaults → user `config.toml` → environment variables
`JARVIS__SECTION__KEY[__SUBKEY]` (values parsed as JSON when possible, else as strings).
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import ValidationError

from jarvis.common.errors import ConfigError
from jarvis.config.paths import default_config_file
from jarvis.config.settings import Settings

log = logging.getLogger(__name__)
ENV_PREFIX = "JARVIS__"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _parse_env_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        low = raw.strip().lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        return raw


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    environ = dict(os.environ if environ is None else environ)
    out: dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX):].split("__") if p]
        if not parts:
            continue
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                break
        else:
            node[parts[-1]] = _parse_env_value(raw)
    return out


def read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc


def load_settings(
    config_file: Path | None = None,
    *,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    path = config_file or default_config_file()
    data: dict[str, Any] = {}
    data = _deep_merge(data, read_toml(path))
    data = _deep_merge(data, env_overrides(environ))
    if overrides:
        data = _deep_merge(data, overrides)
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc


def save_settings(settings: Settings, config_file: Path | None = None) -> Path:
    path = config_file or default_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.model_dump(mode="json", exclude_defaults=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(tomli_w.dumps(payload).encode("utf-8"))
    os.replace(tmp, path)
    log.info("Settings saved to %s", path)
    return path


def update_settings(settings: Settings, patch: dict[str, Any]) -> Settings:
    """Return a new validated Settings with `patch` deep-merged in."""
    merged = _deep_merge(settings.model_dump(mode="json"), patch)
    try:
        return Settings.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"Invalid settings update: {exc}") from exc
