"""Security primitives: secrets, redaction, kill switches, path guard, sandboxed processes."""

from jarvis.security.killswitch import KillEvent, KillSwitch
from jarvis.security.path_guard import PathGuard, is_within, normalize_path
from jarvis.security.process import ProcessRegistry, ProcessResult, kill_tree, run_process
from jarvis.security.redaction import Redactor, default_redactor
from jarvis.security.secrets import KNOWN_SECRETS, SecretStore, sanitized_env

__all__ = [
    "KNOWN_SECRETS",
    "KillEvent",
    "KillSwitch",
    "PathGuard",
    "ProcessRegistry",
    "ProcessResult",
    "Redactor",
    "SecretStore",
    "default_redactor",
    "is_within",
    "kill_tree",
    "normalize_path",
    "run_process",
    "sanitized_env",
]
