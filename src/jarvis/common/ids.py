"""Identifier helpers.

Ids are short, URL-safe and roughly time-sortable: a millisecond timestamp in base36
followed by random characters, with a readable prefix ("task_", "msg_", ...).
"""

from __future__ import annotations

import secrets
import string
import time

_ALPHABET = string.digits + string.ascii_lowercase


def _base36(value: int) -> str:
    if value == 0:
        return "0"
    out = []
    while value:
        value, rem = divmod(value, 36)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def new_id(prefix: str = "", random_len: int = 8) -> str:
    stamp = _base36(int(time.time() * 1000))
    rand = "".join(secrets.choice(_ALPHABET) for _ in range(random_len))
    core = f"{stamp}{rand}"
    return f"{prefix}_{core}" if prefix else core


def new_token(nbytes: int = 24) -> str:
    """Secret token for local API authentication."""
    return secrets.token_urlsafe(nbytes)
