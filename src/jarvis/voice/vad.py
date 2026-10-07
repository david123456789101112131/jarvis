"""Voice activity detection and utterance end-pointing.

`VoiceActivityDetector` decides speech / no-speech for each 80 ms frame:

* **webrtcvad** (aggressiveness 2) on 20 ms sub-frames (10/30 ms also supported; leftover
  samples carry over to the next frame), gated by an energy floor — webrtcvad alone happily
  calls steady fan noise or a hum "speech";
* an **adaptive energy** detector when webrtcvad is not installed.

The noise floor uses minimum statistics: the quietest frame of the last ~3 s. Speech always
has gaps between syllables/words, so the floor stays at the room level while someone talks,
but steady noise (fan, vacuum) raises it within a few seconds and stops counting as speech.

`Endpointer` turns frames into one utterance: it starts after `start_frames` consecutive
voiced frames (clicks and taps are shorter), keeps ~300 ms of pre-roll, ends after
`end_silence_ms` of silence or at `max_utterance_s`, and gives up with NO_SPEECH when nothing
is said within the no-speech timeout. A *hold-off* period (while JARVIS's "Yes?" plays) cannot
start an utterance — that audio is mostly our own echo — but when the user was already
talking through it and keeps talking afterwards, the contiguous speech is kept, so
"Hey JARVIS open Chrome" in one breath loses nothing. Manual mode is push-to-talk: collect
everything until `finish()`.
"""

from __future__ import annotations

import collections
import logging
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from jarvis.common.errors import VoiceError
from jarvis.voice.audio import FRAME_SAMPLES, SAMPLE_RATE, np_module, rms, to_int16

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

log = logging.getLogger(__name__)

INITIAL_NOISE_FLOOR = 0.003  # ≈ -50 dBFS, a quiet room through a typical PC microphone
MIN_NOISE_FLOOR = 0.0002
MAX_NOISE_FLOOR = 0.05
MIN_RMS_WEBRTC = 0.0015  # below ≈ -56 dBFS nothing counts as speech
MIN_RMS_ENERGY = 0.004  # ≈ -48 dBFS
WEBRTC_FLOOR_RATIO = 1.5  # webrtc decides; the energy gate only rejects near-floor audio
ENERGY_FLOOR_RATIO = 3.0  # ≈ 9.5 dB above the floor when energy is all we have
NOISE_WINDOW_FRAMES = 38  # ≈ 3 s of 80 ms frames


class VoiceActivityDetector:
    """Per-frame speech decision (webrtcvad + energy gate, or adaptive energy only)."""

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        aggressiveness: int = 2,
        subframe_ms: int = 20,
        backend: str = "auto",
    ) -> None:
        if subframe_ms not in (10, 20, 30):
            raise ValueError("subframe_ms must be 10, 20 or 30")
        if sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError("webrtcvad supports 8/16/32/48 kHz")
        self.sample_rate = int(sample_rate)
        self.aggressiveness = max(0, min(3, int(aggressiveness)))
        self.subframe = self.sample_rate * subframe_ms // 1000
        self.backend = "energy"
        self._vad: Any = None
        if backend in ("auto", "webrtc", "webrtcvad"):
            try:
                import webrtcvad

                self._vad = webrtcvad.Vad(self.aggressiveness)
                self.backend = "webrtcvad"
            except ImportError as exc:
                if backend != "auto":
                    raise VoiceError("webrtcvad is not installed") from exc
                log.info("webrtcvad is not installed; using the energy-based voice detector")
        elif backend != "energy":
            raise ValueError(f"Unknown VAD backend {backend!r}")
        self._carry: Any = None
        self._levels: collections.deque[float] = collections.deque(maxlen=NOISE_WINDOW_FRAMES)
        self.noise_floor = INITIAL_NOISE_FLOOR
        self.last_rms = 0.0
        self.last_ratio = 0.0

    def reset(self) -> None:
        """Drop sub-frame carry-over (the learned noise floor is kept)."""
        self._carry = None

    def observe(self, frame: Any) -> float:
        """Update the noise floor from a frame without classifying it (cheap; used while idle)."""
        level = rms(frame)
        self._track(level)
        return level

    def is_speech(self, frame: Any) -> bool:
        x = to_int16(frame)
        level = rms(x)
        self.last_rms = level
        if self._vad is not None:
            ratio = self._webrtc_ratio(x)
            voiced = ratio >= 0.5 and level >= max(MIN_RMS_WEBRTC, self.noise_floor * WEBRTC_FLOOR_RATIO)
        else:
            voiced = level >= max(MIN_RMS_ENERGY, self.noise_floor * ENERGY_FLOOR_RATIO)
            ratio = 1.0 if voiced else 0.0
        self.last_ratio = ratio
        self._track(level)
        return voiced

    def _webrtc_ratio(self, x: np.ndarray) -> float:
        np = np_module()
        if self._carry is not None and len(self._carry):
            x = np.concatenate((self._carry, x))
        n = self.subframe
        count = len(x) // n
        self._carry = x[count * n :]
        if count == 0:
            return self.last_ratio
        voiced = 0
        for i in range(count):
            try:
                if self._vad.is_speech(x[i * n : (i + 1) * n].tobytes(), self.sample_rate):
                    voiced += 1
            except Exception:  # pragma: no cover - only on invalid frame sizes
                continue
        return voiced / count

    def _track(self, level: float) -> None:
        self._levels.append(level)
        floor = min(self._levels)
        if len(self._levels) < 6:  # not enough history yet: never above the default floor
            floor = min(floor, INITIAL_NOISE_FLOOR)
        self.noise_floor = min(MAX_NOISE_FLOOR, max(MIN_NOISE_FLOOR, floor))


class EndpointEvent(StrEnum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"
    MAX_LENGTH = "max_length"
    NO_SPEECH = "no_speech"


@dataclass
class EndpointConfig:
    sample_rate: int = SAMPLE_RATE
    frame_samples: int = FRAME_SAMPLES
    aggressiveness: int = 2
    subframe_ms: int = 20
    backend: str = "auto"
    start_frames: int = 2  # 160 ms of consecutive speech starts an utterance
    end_silence_ms: int = 700
    max_utterance_s: float = 15.0
    no_speech_timeout_s: float | None = 5.0
    preroll_ms: int = 300
    trailing_ms: int = 240
    max_gap_ms: int = 240  # pause tolerated inside a speech run when looking back for its start
    min_speech_ms: int = 200  # less voiced audio than this is noise, not a command

    @property
    def frame_ms(self) -> float:
        return 1000.0 * self.frame_samples / self.sample_rate

    def frames(self, ms: float) -> int:
        return max(0, math.ceil(ms / self.frame_ms - 1e-9))


@dataclass
class Utterance:
    audio: Any  # int16 mono
    sample_rate: int = SAMPLE_RATE
    reason: str = ""
    speech_s: float = 0.0
    started_in_holdoff: bool = False  # began while our own acknowledgement was playing

    @property
    def duration_s(self) -> float:
        return len(self.audio) / float(self.sample_rate) if self.sample_rate else 0.0

    @property
    def has_speech(self) -> bool:
        return self.speech_s > 0


_UNSET: Any = object()


class Endpointer:
    """Finds where one spoken utterance starts and ends in a stream of 80 ms frames.

    Call `reset()` before each listening session, `process(frame)` for every frame (returns an
    `EndpointEvent` when something happens) and `finish()` to get the `Utterance`.
    """

    def __init__(self, config: EndpointConfig | None = None, *, vad: VoiceActivityDetector | None = None) -> None:
        self.config = config or EndpointConfig()
        c = self.config
        self.vad = vad or VoiceActivityDetector(
            sample_rate=c.sample_rate, aggressiveness=c.aggressiveness, subframe_ms=c.subframe_ms, backend=c.backend
        )
        self.reset()

    @property
    def backend(self) -> str:
        return self.vad.backend

    @property
    def frame_seconds(self) -> float:
        return self.config.frame_samples / float(self.config.sample_rate)

    def reset(
        self,
        *,
        no_speech_timeout_s: float | None = _UNSET,
        holdoff_s: float = 0.0,
        manual: bool = False,
        max_utterance_s: float | None = None,
        end_silence_ms: int | None = None,
    ) -> None:
        c = self.config
        timeout = c.no_speech_timeout_s if no_speech_timeout_s is _UNSET else no_speech_timeout_s
        self._timeout_frames = c.frames(timeout * 1000.0) if timeout else 0
        self._end_frames = max(1, c.frames(end_silence_ms if end_silence_ms is not None else c.end_silence_ms))
        self._max_frames = max(2, c.frames(1000.0 * (max_utterance_s or c.max_utterance_s)))
        self._holdoff_frames = c.frames(max(0.0, holdoff_s) * 1000.0)
        self._preroll_frames = c.frames(c.preroll_ms)
        self._trailing_frames = c.frames(c.trailing_ms)
        self._gap_frames = c.frames(c.max_gap_ms)
        self._ring: collections.deque = collections.deque(
            maxlen=self._holdoff_frames + self._preroll_frames + c.start_frames + 16
        )
        self._frames: list[Any] = []
        self._flags: list[bool] = []
        self._run = 0
        self._silence = 0
        self._waited = 0
        self._seen = 0
        self._start_index: int | None = None
        self._any_voiced = False
        self.manual = manual
        self.started = manual
        self.done = False
        self.event: EndpointEvent | None = None
        self.last_voiced = False
        self.vad.reset()

    # -- streaming -------------------------------------------------------------------------
    def observe(self, frame: Any) -> None:
        """Track background noise while not listening (keeps the floor current between sessions)."""
        self.vad.observe(frame)

    def process(self, frame: Any) -> EndpointEvent | None:
        if self.done:
            return None
        frame = to_int16(frame)
        voiced = self.vad.is_speech(frame)
        self.last_voiced = voiced
        index = self._seen
        self._seen += 1
        if self.manual:
            self._frames.append(frame)
            self._flags.append(voiced)
            if len(self._frames) >= self._max_frames:
                return self._end(EndpointEvent.MAX_LENGTH)
            if voiced and not self._any_voiced:
                self._any_voiced = True
                return EndpointEvent.SPEECH_START
            return None
        if not self.started:
            self._ring.append((frame, voiced, index))
            self._run = self._run + 1 if voiced else 0
            if index < self._holdoff_frames:
                return None
            if self._run >= self.config.start_frames:
                self._begin()
                return EndpointEvent.SPEECH_START
            self._waited += 1
            if self._timeout_frames and self._waited >= self._timeout_frames:
                return self._end(EndpointEvent.NO_SPEECH)
            return None
        self._frames.append(frame)
        self._flags.append(voiced)
        self._silence = 0 if voiced else self._silence + 1
        if self._silence >= self._end_frames:
            return self._end(EndpointEvent.SPEECH_END)
        if len(self._frames) >= self._max_frames:
            return self._end(EndpointEvent.MAX_LENGTH)
        return None

    def _end(self, event: EndpointEvent) -> EndpointEvent:
        self.done = True
        self.event = event
        return event

    def _begin(self) -> None:
        items = list(self._ring)
        i = len(items) - 1
        start = i
        gap = 0
        while i >= 0:  # walk back over the contiguous speech run (short pauses allowed)
            if items[i][1]:
                start = i
                gap = 0
            else:
                gap += 1
                if gap > self._gap_frames:
                    break
            i -= 1
        start = max(0, start - self._preroll_frames)
        chosen = items[start:]
        self._frames = [f for f, _, _ in chosen]
        self._flags = [v for _, v, _ in chosen]
        self._start_index = chosen[0][2] if chosen else None
        self._ring.clear()
        self.started = True
        self._silence = 0

    # -- results ---------------------------------------------------------------------------
    @property
    def in_speech(self) -> bool:
        return self.started and not self.done and (not self.manual or self._any_voiced)

    @property
    def utterance_frames(self) -> int:
        return len(self._frames) if self.started else 0

    @property
    def speech_seconds(self) -> float:
        return sum(self._flags) * self.frame_seconds

    def current_audio(self) -> np.ndarray:
        np = np_module()
        return np.concatenate(self._frames) if self._frames else np.zeros(0, dtype=np.int16)

    def finish(self) -> Utterance:
        """The utterance collected so far (also ends a manual / push-to-talk session)."""
        np = np_module()
        c = self.config
        reason = str(self.event) if self.event else ("manual" if self.manual else "stopped")
        self.done = True
        frames, flags = self._frames, self._flags
        if not frames or not any(flags):
            return Utterance(np.zeros(0, dtype=np.int16), c.sample_rate, reason, 0.0)
        first = flags.index(True)
        last = len(flags) - 1 - flags[::-1].index(True)
        lo = max(0, first - self._preroll_frames) if self.manual else 0
        hi = min(len(frames), last + 1 + self._trailing_frames)
        speech_s = sum(flags[lo:hi]) * self.frame_seconds
        in_holdoff = self._start_index is not None and self._start_index < self._holdoff_frames
        return Utterance(np.concatenate(frames[lo:hi]), c.sample_rate, reason, speech_s, in_holdoff)
