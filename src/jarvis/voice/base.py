"""Voice subsystem contracts shared by the voice package and the rest of JARVIS.

The app gives the voice service two things:
  * a `CommandHandler` — async callable that takes the final transcript and returns what
    JARVIS should say (or None to stay silent); the core does the actual work,
  * a `VoiceEventSink` — receives state changes, mic levels, transcripts and pipeline steps
    so the UI orb can animate (idle → wake → listening → processing → speaking).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class VoiceState(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"              # waiting for the wake word
    WAKE = "wake"              # wake word just detected
    LISTENING = "listening"    # capturing the command
    PROCESSING = "processing"  # transcribing / handing the command to the core
    SPEAKING = "speaking"      # playing JARVIS's answer
    ERROR = "error"


@dataclass
class Transcript:
    text: str
    language: str = "he"
    final: bool = True
    confidence: float | None = None
    duration_s: float | None = None
    # Where the handler can put the reply while it is still being written (the pipeline speaks it at once).
    stream: SpeechStream | None = field(default=None, compare=False, repr=False)


_SENTENCE_END = re.compile(r"[.!?…:](?=\s)|\n")


class SpeechStream:
    """A reply that is still being written: `update()` is given the text so far (from the core, as the AI writes
    it), and the voice pipeline takes each piece that ends a sentence with `next()` and speaks it, while the rest
    is still coming. When the handler returns its full reply, `said` is what was spoken already.

    A piece is at least `min_chars` long (a tiny first sentence waits for the next one), so the first sound comes
    with the first complete sentence. Nothing more is handed out after `max_chars` (a long reply is cut there).
    """

    def __init__(self, *, min_chars: int = 12, max_chars: int = 900) -> None:
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.text = ""
        self.taken = 0                 # characters of `text` handed out
        self.complete = False          # the writer finished the reply
        self.closed = False            # no more pieces (the handler returned, or the text changed under us)
        self._changed = asyncio.Event()

    @property
    def said(self) -> str:
        return self.text[:self.taken]

    def update(self, text: str, done: bool = False) -> None:
        if self.closed:
            return
        if not text.startswith(self.said):        # rewritten (another attempt): the full reply decides the rest
            self.close()
            return
        self.text = text
        self.complete = self.complete or done
        self._changed.set()

    def close(self) -> None:
        self.closed = True
        self._changed.set()

    def _cut(self) -> int | None:
        rest = self.text[self.taken:]
        if not rest.strip() or self.taken >= self.max_chars:
            return None
        for end in _SENTENCE_END.finditer(rest):
            if len(rest[:end.end()].strip()) >= self.min_chars:
                return self.taken + end.end()
        return len(self.text) if self.complete else None

    async def next(self) -> str | None:
        """The next piece to speak; None once nothing more will come before the full reply."""
        while True:
            cut = None if self.closed else self._cut()
            if cut is not None:
                piece, self.taken = self.text[self.taken:cut], cut
                return piece
            if self.closed or self.complete:
                return None
            self._changed.clear()
            await self._changed.wait()

    def rest_of(self, reply: str) -> str:
        """What is left to say of the full `reply` once the pieces already handed out were spoken."""
        said, full = self.said.strip(), (reply or "").strip()
        if not said:
            return full
        if full.startswith(said):
            return full[len(said):].strip()
        if said.startswith(full.rstrip("…").rstrip()):  # the reply was cut shorter than what was said
            return ""
        return full                                      # a different reply (an error): say it whole


@dataclass
class SpeechReply:
    """What the command handler wants spoken. `ack_key` plays a cached phrase instantly."""

    text: str | None = None
    language: str | None = None
    ack_key: str | None = None
    follow_up: bool = True     # keep listening briefly for a follow-up without the wake word
    extra: dict[str, Any] = field(default_factory=dict)


CommandHandler = Callable[[Transcript], Awaitable[SpeechReply | str | None]]


class VoiceEventSink(Protocol):
    async def on_state(self, state: VoiceState, info: dict[str, Any]) -> None: ...

    async def on_level(self, level: float) -> None: ...

    async def on_transcript(self, transcript: Transcript) -> None: ...

    async def on_pipeline(self, step: str, status: str, label: str) -> None: ...

    async def on_speech(self, text: str, language: str) -> None: ...

    # Optional: `async def on_info(self, info: dict) -> None` gets `VoiceService.info()` once a model has loaded.


class NullVoiceEvents:
    async def on_state(self, state: VoiceState, info: dict[str, Any]) -> None:
        return None

    async def on_level(self, level: float) -> None:
        return None

    async def on_transcript(self, transcript: Transcript) -> None:
        return None

    async def on_pipeline(self, step: str, status: str, label: str) -> None:
        return None

    async def on_speech(self, text: str, language: str) -> None:
        return None

    async def on_timing(self, name: str, ms: float, ok: bool) -> None:
        """How long a step took ("voice.stt", "voice.reply", "voice.tts.<engine>"); optional for a sink."""
        return None


# Short phrases pre-synthesized at start-up so acknowledgements play with no delay.
ACK_PHRASES: dict[str, dict[str, str]] = {
    "listening": {"he": "כן?", "en": "Yes?"},
    "thinking": {"he": "רק רגע.", "en": "One moment."},       # the AI is still writing the answer
    "thinking_2": {"he": "שנייה.", "en": "Just a second."},
    "ok": {"he": "בסדר.", "en": "Okay."},
    "on_it": {"he": "מיד.", "en": "On it."},
    "done": {"he": "בוצע.", "en": "Done."},
    "opened": {"he": "נפתח.", "en": "Opened."},
    "complex_ack": {"he": "קיבלתי. אני מחלק את המשימה בין הסוכנים.", "en": "Got it. I'm splitting the task between the agents."},
    "single_ack": {"he": "קיבלתי, מתחיל לעבוד על זה.", "en": "Got it, working on it."},
    "error": {"he": "משהו השתבש.", "en": "Something went wrong."},
    "not_understood": {"he": "לא הבנתי, אפשר לחזור על זה?", "en": "Sorry, I didn't catch that."},
    "cancelled": {"he": "בוטל.", "en": "Cancelled."},
    "stopped": {"he": "עצרתי הכול.", "en": "Everything is stopped."},
    "confirm": {"he": "צריך את האישור שלך.", "en": "I need your approval."},
}
