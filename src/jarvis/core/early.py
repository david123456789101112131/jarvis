"""A reply that is spoken while the model is still writing it.

The chat model answers in JSON (`ChatTurn`): `EarlyReply` reads the "reply" string out of the JSON written so far
and hands it to the voice channel, which speaks each sentence as soon as it is complete. The full answer still
arrives as usual; the voice channel then speaks only what it has not said yet (`jarvis.voice.base.SpeechStream`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

log = logging.getLogger(__name__)

# What the voice channel is given: the reply so far, and whether it is complete.
Say = Callable[[str, bool], None]

_REPLY = re.compile(r'"reply"\s*:\s*"')
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def partial_json_string(text: str, start: int) -> tuple[str, bool]:
    """The JSON string whose characters begin at `start` (just after its opening quote), decoded as far as it is
    written: (value, closed). An escape cut off at the end is left out until the rest of it arrives."""
    out: list[str] = []
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            return "".join(out), True
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            break
        kind = text[i + 1]
        if kind != "u":
            out.append(_ESCAPES.get(kind, kind))
            i += 2
            continue
        if i + 6 > n:
            break
        try:
            code = int(text[i + 2:i + 6], 16)
        except ValueError:                          # not JSON after all: stop here
            return "".join(out), True
        if 0xD800 <= code < 0xDC00:                 # the first half of a surrogate pair: needs the second
            if i + 12 > n:
                break
            try:
                low = int(text[i + 8:i + 12], 16) if text[i + 6:i + 8] == "\\u" else -1
            except ValueError:
                low = -1
            if 0xDC00 <= low < 0xE000:
                out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                i += 12
                continue
        out.append(chr(code))
        i += 6
    return "".join(out), False


class EarlyReply:
    """The `on_text` of a chat request: passes the reply written so far to `say`. `structured`: the answer is
    `ChatTurn` JSON (its "reply" field is spoken); otherwise the whole text is the reply."""

    def __init__(self, say: Say, *, structured: bool = True) -> None:
        self.say = say
        self.structured = structured
        self._said = ("", False)

    def __deepcopy__(self, memo: dict[int, object]) -> EarlyReply:
        return self                                 # a callback, not data: a copied request still calls it

    def __call__(self, raw: str) -> None:
        if self.structured:
            found = _REPLY.search(raw)
            if found is None:
                return
            value, closed = partial_json_string(raw, found.end())
        else:
            value, closed = raw, False
        if (value, closed) == self._said:
            return
        self._said = (value, closed)
        try:
            self.say(value, closed)
        except Exception:                           # the voice channel's problem must not stop the answer
            log.debug("Early speech failed", exc_info=True)


__all__ = ["EarlyReply", "Say", "partial_json_string"]
