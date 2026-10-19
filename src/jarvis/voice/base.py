"""Voice subsystem contracts shared by the voice package and the rest of JARVIS.

The app gives the voice service two things:
  * a `CommandHandler` — async callable that takes the final transcript and returns what
    JARVIS should say (or None to stay silent); the core does the actual work,
  * a `VoiceEventSink` — receives state changes, mic levels, transcripts and pipeline steps
    so the UI orb can animate (idle → wake → listening → processing → speaking).
"""

from __future__ import annotations

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
