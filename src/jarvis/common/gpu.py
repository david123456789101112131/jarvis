"""Turns on the graphics card for JARVIS's big local models.

Computer control (the Holo model, about 12 GB), image and video creation (Z-Image-Turbo or Wan 2.2, up to about
14 GB) and the chat model (Gemma 4 12B, about 8 GB) do not fit on a 16 GB card together, so before one loads, the
others let go of the card: each idle one stops its model. A model that is in the middle of work is never stopped for
another; `take` names it instead and the caller says the card is busy. Speech recognition is small (about 1 GB) and
stays.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

# What each user is doing while it holds the card, for "the graphics card is busy ...".
DOING = {"computer": "using the computer", "media": "making an image or a video", "chat": "answering"}


def busy_text(name: str) -> str:
    return f"the graphics card is busy {DOING.get(name, f'({name})')}; try again when it is done."


@dataclass
class _User:
    busy: Callable[[], bool]
    release: Callable[[], Awaitable[object]]


class GpuTurns:
    def __init__(self) -> None:
        self._users: dict[str, _User] = {}

    def register(self, name: str, *, busy: Callable[[], bool], release: Callable[[], Awaitable[object]]) -> None:
        self._users[name] = _User(busy, release)

    async def take(self, name: str) -> str | None:
        """Free the card for `name`. Returns the name of another user still working (nothing is released then),
        or None once every other user has let go."""
        others = {n: u for n, u in self._users.items() if n != name}
        for other, user in others.items():
            if user.busy():
                return other
        for other, user in others.items():
            with contextlib.suppress(Exception):
                await user.release()
                log.debug("%s let go of the graphics card for %s", other, name)
        return None


__all__ = ["DOING", "GpuTurns", "busy_text"]
