"""The voice pipeline: wake word → listen → transcribe → hand to the core → speak → follow-up.

State machine (`VoiceState`)::

    IDLE ──wake word──► WAKE ─► LISTENING ──end of speech──► PROCESSING ──reply──► SPEAKING
      ▲                  (ack "Yes?")   │ no speech                │ None / no follow-up   │
      │                                 ▼                          ▼                       │
      └──────────────────────────────── IDLE ◄─────────────────────┴── follow-up window ◄──┘
                                                                   (LISTENING, no wake word)

* IDLE feeds every 80 ms frame to the wake engine (on its own thread).
* On wake: WAKE, the cached "listening" acknowledgement starts at once (or a chime while it is
  not cached yet), then LISTENING with the end-pointer. Audio captured while the ack plays is
  treated as possible echo (see `vad.Endpointer` hold-off) and a leading "Yes?/כן?" echo is
  removed from the transcript.
* LISTENING emits mic levels (≤ 15/s) and, when the STT engine supports it, partial
  transcripts; the final utterance goes to PROCESSING.
* PROCESSING emits pipeline step "transcribing", transcribes, strips the wake word, and hands
  the `Transcript` to the `CommandHandler` (which returns `SpeechReply | str | None`).
* SPEAKING plays the reply: cached acknowledgement first (instant), then the text synthesized
  sentence by sentence — the next sentence is synthesized while the current one plays.
  The wake engine keeps running (threshold slightly raised against our own echo): saying the
  wake word stops playback at once and goes back to LISTENING (barge-in).
* `follow_up=True` → LISTENING again for `follow_up_window_s` without the wake word.
* Ctrl on its own (`talk_key()`, from `hotkey.py`) is the wake word without speaking it: the same "listening"
  acknowledgement and one command; pressed again while listening, it stops listening.
* Push-to-talk skips the wake word; `cancel()`, `mute()/unmute()` and `speak()` (proactive
  announcements, queued until the pipeline is quiet unless `interrupt=True`) are available.
* Errors put the pipeline in ERROR for a moment, then back to IDLE; the frame loop never dies.
  Microphone failures are retried with back-off (DISABLED when there is no microphone at all).

All state transitions are synchronous methods (no `await` inside), so they are atomic on the
event loop: the frame loop, the turn task and API calls can never interleave half-way through
a transition. A turn carries a sequence number; a superseded turn (barge-in, cancel, PTT) never
touches the state again, while the command handler itself is shielded and runs to completion.

Performance (targets → how they are met):

* Wake word continuously on CPU (< 5 % of a core): openWakeWord ONNX, single-threaded, one
  inference per 80 ms frame ≈ 1-2 ms → ~2 % of one core; the pipeline itself only moves
  numpy frames around. The microphone stays closed when nothing needs it (muted, PTT-only).
* Ack within ~150 ms of wake: acknowledgements are pre-synthesized at start-up and kept in
  memory, playback starts in the same loop iteration as the detection (no synthesis, no file
  I/O), the wake model reset after a detection is queued on the wake thread instead of awaited,
  and the output stream is opened with PortAudio's low-latency setting.
* STT for a 3 s command well under 1.5 s with "small"/int8 on a modern CPU: see `stt.py`
  (greedy decoding, no timestamps, model loaded and warmed up at start-up; "base" is faster).
  End-of-speech detection adds `end_silence_ms` (700 ms default) before transcription starts.
* Cached acknowledgements play instantly: `PhraseCache` (memory + WAV files).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.common.text import detect_language, normalize
from jarvis.voice.audio import (
    FRAME_SECONDS,
    AudioClip,
    AudioDeviceError,
    AudioError,
    AudioSink,
    AudioSource,
    earcon,
    refresh_devices,
    rms_level,
)
from jarvis.voice.base import (
    ACK_PHRASES,
    CommandHandler,
    NullVoiceEvents,
    SpeechReply,
    SpeechStream,
    Transcript,
    VoiceEventSink,
    VoiceState,
)
from jarvis.voice.stt import STTEngine, is_noise_transcript
from jarvis.voice.tts import PhraseCache, TTSEngine, prepare_speech_text, resolve_language, split_sentences
from jarvis.voice.vad import EndpointConfig, Endpointer, EndpointEvent, Utterance
from jarvis.voice.wakeword import NullWakeEngine, WakeDetection, WakeWordEngine, match_wake_phrase

if TYPE_CHECKING:  # pragma: no cover
    from jarvis.config.settings import VoiceSettings

log = logging.getLogger(__name__)

_LABELS = {
    "transcribing": {"he": "מתמלל…", "en": "Transcribing…"},
    "done": {"he": "הושלם", "en": "Done"},
    "error": {"he": "שגיאה", "en": "Error"},
}
_TOKEN_PUNCT_RE = re.compile(r"[,.!?;:…\"“”«»()\[\]{}–—\-]+")


def _short(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return text.splitlines()[0][:240]


def _label(step: str, language: str) -> str:
    labels = _LABELS.get(step, {})
    return labels.get(language) or labels.get("en") or step


def coerce_reply(reply: SpeechReply | str | None) -> SpeechReply | None:
    """Normalize what a `CommandHandler` returned."""
    if reply is None:
        return None
    if isinstance(reply, SpeechReply):
        return reply
    if isinstance(reply, str):
        text = reply.strip()
        return SpeechReply(text=text) if text else None
    raise TypeError(f"Command handler returned {type(reply).__name__}; expected SpeechReply, str or None")


def strip_echo(text: str, echo: str) -> str:
    """Remove our own acknowledgement ("כן?", "Yes?") when the microphone caught it at the start."""
    echo_words = [w for w in (normalize(_TOKEN_PUNCT_RE.sub("", t)) for t in (echo or "").split()) if w]
    if not echo_words:
        return text
    tokens = [(m.start(), normalize(_TOKEN_PUNCT_RE.sub("", m.group()))) for m in re.finditer(r"\S+", text or "")]
    tokens = [t for t in tokens if t[1]]
    if len(tokens) <= len(echo_words) or [w for _, w in tokens[: len(echo_words)]] != echo_words:
        return text
    return (text or "")[tokens[len(echo_words)][0] :].strip()


@dataclass
class PipelineOptions:
    no_speech_timeout_s: float = 5.0  # after an explicit wake word, give up after this much silence
    follow_up_window_s: float = 6.0
    end_silence_ms: int = 700
    max_utterance_s: float = 15.0
    ack_on_wake: bool = True
    speak_responses: bool = True
    always_listening: bool = True  # False: push-to-talk only, microphone closed while idle
    partial_transcripts: bool = True
    partial_interval_s: float = 1.0
    partial_min_s: float = 1.0
    barge_in: bool = True
    barge_in_threshold_boost: float = 0.15  # stricter wake threshold while JARVIS is talking (echo)
    echo_guard_s: float = 0.25  # after our own audio stops, the room still rings for a moment
    error_display_s: float = 1.5
    handler_timeout_s: float = 120.0
    stt_language: str = "auto"
    default_language: str = "he"
    max_retries: int = 1  # "didn't catch that" re-prompts after an explicit wake word
    stall_timeout_s: float = 3.0  # no audio for this long → the microphone is considered lost
    level_rate_hz: float = 15.0
    min_speech_ms: int = 200
    max_speech_chars: int = 900

    @classmethod
    def from_settings(cls, settings: VoiceSettings, **overrides: Any) -> PipelineOptions:
        values: dict[str, Any] = {
            "follow_up_window_s": settings.follow_up_window_s,
            "end_silence_ms": settings.end_silence_ms,
            "max_utterance_s": settings.max_utterance_s,
            "speak_responses": settings.speak_responses,
            "always_listening": settings.always_listening,
            "partial_transcripts": settings.partial_transcripts,
            "stt_language": settings.stt_language,
            "default_language": settings.stt_language if settings.stt_language in ("he", "en") else "he",
        }
        values.update(overrides)
        return cls(**values)


class _EventDispatcher:
    """Delivers events to the sink in order on its own task, so a slow UI never stalls audio.
    Consecutive level updates are coalesced."""

    def __init__(self, sink: VoiceEventSink) -> None:
        self.sink = sink
        self._queue: collections.deque[tuple[str, tuple]] = collections.deque()
        self._wakeup = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: asyncio.Task | None = None
        self._closed = False

    def emit(self, method: str, *args: Any) -> None:
        if self._closed or not hasattr(self.sink, method):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no event loop (e.g. called during interpreter shutdown)
            return
        if method == "on_level" and self._queue and self._queue[-1][0] == "on_level":
            self._queue[-1] = (method, args)
        else:
            self._queue.append((method, args))
        self._idle.clear()
        self._wakeup.set()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run(), name="jarvis-voice-events")

    async def _run(self) -> None:
        while True:
            while self._queue:
                method, args = self._queue.popleft()
                try:
                    await getattr(self.sink, method)(*args)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug("Voice event sink %s failed", method, exc_info=True)
            self._idle.set()
            self._wakeup.clear()
            if self._queue:
                continue
            await self._wakeup.wait()

    async def flush(self, timeout: float = 2.0) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout)

    async def close(self) -> None:
        await self.flush(1.0)
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


@dataclass
class _Listen:
    id: int
    trigger: str  # "wake" | "key" | "ptt" | "follow_up" | "retry"
    explicit: bool
    retries: int = 0
    echo_text: str | None = None
    last_partial_frames: int = 0


@dataclass
class _Turn:
    seq: int
    trigger: str
    explicit: bool
    utterance: Utterance | None = None
    transcript: Transcript | None = None
    retries: int = 0
    echo_text: str | None = None


class VoicePipeline:
    def __init__(
        self,
        *,
        source: AudioSource,
        sink: AudioSink,
        wake: WakeWordEngine | None,
        stt: STTEngine,
        tts: TTSEngine,
        handler: CommandHandler,
        events: VoiceEventSink | None = None,
        options: PipelineOptions | None = None,
        phrase_cache: PhraseCache | None = None,
        endpointer: Endpointer | None = None,
    ) -> None:
        self.source = source
        self.sink = sink
        self.wake: WakeWordEngine = wake or NullWakeEngine()
        self.stt = stt
        self.tts = tts
        self.handler = handler
        self.options = options or PipelineOptions()
        self.cache = phrase_cache
        o = self.options
        self.endpointer = endpointer or Endpointer(
            EndpointConfig(
                end_silence_ms=o.end_silence_ms,
                max_utterance_s=o.max_utterance_s,
                no_speech_timeout_s=o.no_speech_timeout_s,
                min_speech_ms=o.min_speech_ms,
            )
        )
        self._events = _EventDispatcher(events or NullVoiceEvents())
        self.state = VoiceState.IDLE
        self.muted = False
        self.language = o.default_language if o.default_language in ("he", "en") else "he"
        self.last_transcript: Transcript | None = None
        self.last_error: str | None = None
        self.disabled_reason: str | None = None
        self.stats = {"wakes": 0, "talk_keys": 0, "barge_ins": 0, "turns": 0, "early_replies": 0, "errors": 0,
                      "frames": 0}
        self._listen: _Listen | None = None
        self._listen_seq = 0
        self._turn_seq = 0
        self._turn_task: asyncio.Task | None = None
        self._ack_task: asyncio.Task | None = None
        self._partial_task: asyncio.Task | None = None
        self._speech_lock = asyncio.Lock()
        self._capture_changed = asyncio.Event()
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._state_version = 0
        self._wake_failures = 0
        self._last_level_at = 0.0
        self._source_problem: str | None = None
        self._rescan = False                        # re-scan the devices before the microphone opens again
        self._disabled_by_source = False
        self._tts_warned_at = 0.0

    # ------------------------------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    @property
    def busy(self) -> bool:
        """In a conversation (listening, processing or speaking)."""
        return self._listen is not None or self._turn_active() or self.state in (VoiceState.SPEAKING, VoiceState.WAKE)

    def start(self) -> asyncio.Task:
        if self._run_task is None or self._run_task.done():
            self._loop = asyncio.get_running_loop()
            self._run_task = self._loop.create_task(self.run(), name="jarvis-voice-pipeline")
        return self._run_task

    async def run(self) -> None:
        """Main loop: read frames until `stop()` (or a finite source runs out)."""
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        if self.busy:  # e.g. push-to-talk pressed between start() and the first step of this loop
            self._set_state(self.state)
        else:
            self._to_rest()
        backoff = 1.0
        try:
            while not self._stopping:
                if not self._capture_wanted():
                    if self.source.running:
                        await self._stop_source()
                    self._capture_changed.clear()
                    if self._capture_wanted() or self._stopping:
                        continue
                    await self._capture_changed.wait()
                    continue
                if not self.source.running:
                    if self._rescan:
                        self._rescan = False
                        await self._refresh_devices()
                    try:
                        await self.source.start()
                    except Exception as exc:
                        self._source_failed(exc)
                        await self._pause(backoff)
                        backoff = min(30.0, backoff * 2)
                        if isinstance(exc, AudioError):  # PortAudio caches the device list: re-scan
                            await self._refresh_devices()
                        continue
                    backoff = 1.0
                    self._source_ok()
                try:
                    frame = await asyncio.wait_for(self.source.read(), timeout=self.options.stall_timeout_s)
                except TimeoutError:
                    self._source_failed(AudioError("The microphone stopped sending audio"))
                    await self._stop_source()
                    continue
                if frame is None:
                    if self.source.exhausted:
                        break
                    continue
                self.stats["frames"] += 1
                try:
                    await self._handle_frame(frame)
                except Exception as exc:
                    log.exception("Voice frame handling failed")
                    self.stats["errors"] += 1
                    self._flash_error(_short(exc))
        finally:
            await self._stop_source()

    async def stop(self) -> None:
        if self._stopping and not self.running:
            return
        self._stopping = True
        self._interrupt()
        self._capture_changed.set()
        with contextlib.suppress(Exception):
            await self.source.stop()
        task = self._run_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 5.0)
            except Exception:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        pending = [t for t in self._tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self._events.close()

    def push_to_talk_start(self) -> None:
        self._call(self._ptt_start)

    def push_to_talk_stop(self) -> None:
        self._call(self._ptt_stop)

    def talk_key(self) -> None:
        """The talk key (Ctrl on its own) was tapped: listen for one command; tapped again, stop listening."""
        self._call(self._talk_key_now)

    def cancel(self) -> None:
        self._call(self._cancel_now)

    def mute(self) -> None:
        self._call(self._mute_now)

    def unmute(self) -> None:
        self._call(self._unmute_now)

    def reopen_source(self) -> None:
        """Open the microphone again on the current default device (Windows switched the default)."""

        def apply() -> None:
            if self._stopping:
                return
            self._rescan = True
            if self.source.running:
                self._spawn(self.source.stop())

        self._call(apply)

    async def _refresh_devices(self) -> None:
        if await asyncio.to_thread(refresh_devices):
            self.sink.devices_changed()

    def set_wake_engine(self, engine: WakeWordEngine) -> None:
        def apply() -> None:
            self.wake = engine
            self._wake_failures = 0
            self._capture_changed.set()

        self._call(apply)

    def set_disabled(self, reason: str | None, *, source: bool = False) -> None:
        """Mark voice input unusable — or usable again with None. `source=True`: the reason is
        the microphone (e.g. none found), cleared automatically once the microphone opens."""

        def apply() -> None:
            if (self.disabled_reason or None) == (reason or None) and self._disabled_by_source == bool(reason and source):
                return
            self.disabled_reason = reason or None
            self._disabled_by_source = bool(reason and source)
            if not self.busy:
                self._to_rest()

        self._call(apply)

    @property
    def input_problem(self) -> bool:
        """Disabled because of the microphone (missing / not installed)."""
        return self._disabled_by_source and self.disabled_reason is not None

    async def speak(self, text: str, language: str | None = None, interrupt: bool = False, *, wait: bool = True) -> bool:
        """Say something proactively. Waits until the pipeline is quiet unless `interrupt=True`.
        Returns True when it was spoken (or queued, with `wait=False`)."""
        text = (text or "").strip()
        if not text or self._stopping or not self.options.speak_responses:
            return False
        lang = resolve_language(text, language, default=self.language)
        if interrupt:
            self._interrupt()
            if self.state in (VoiceState.LISTENING, VoiceState.WAKE, VoiceState.PROCESSING):
                self._to_rest()
        task = self._spawn(self._announce(text, lang, interrupt))
        if not wait:
            return True
        return await asyncio.shield(task)

    async def flush_events(self, timeout: float = 2.0) -> None:
        await self._events.flush(timeout)

    async def wait_quiet(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._wait_quiet(), timeout)

    # ------------------------------------------------------------------------------------------
    # Commands (synchronous: atomic on the event loop)
    # ------------------------------------------------------------------------------------------
    def _call(self, fn: Callable[[], None]) -> None:
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or running is loop or not loop.is_running():
            fn()
        else:  # called from another thread (e.g. a global hot-key handler)
            loop.call_soon_threadsafe(fn)

    def _ptt_start(self) -> None:
        if self._stopping:
            return
        log.info("Push-to-talk: listening")
        self._interrupt()
        self._begin_listen("ptt", explicit=True)

    def _ptt_stop(self) -> None:
        listen = self._listen
        if listen is None or listen.trigger != "ptt":
            return
        utterance = self.endpointer.finish()
        self._listen = None
        if not utterance.has_speech:
            log.debug("Push-to-talk released without speech")
            self._to_rest()
            return
        self._start_turn(listen, utterance)

    def _talk_key_now(self) -> None:
        if self._stopping:
            return
        if self.disabled_reason:  # e.g. no microphone: saying "yes?" would promise what cannot happen
            log.info("Talk key ignored: %s", self.disabled_reason)
            return
        listen = self._listen
        if listen is not None and listen.explicit:  # pressed again while listening: never mind
            log.info("Talk key: stopped listening")
            self._cancel_now()
            return
        self.stats["talk_keys"] += 1
        log.info("Talk key: listening")
        self._interrupt()
        self._set_state(VoiceState.WAKE, trigger="key")
        holdoff, echo = self._play_ack_now("listening", self.language)
        self._begin_listen("key", explicit=True, holdoff_s=holdoff, timeout_s=self.options.no_speech_timeout_s,
                           echo_text=echo)

    def _cancel_now(self) -> None:
        self._interrupt()
        self._to_rest()

    def _mute_now(self) -> None:
        if self.muted:
            return
        self.muted = True
        listen = self._listen
        if listen is not None and listen.trigger != "ptt":
            self._listen = None
            self._to_rest()
        else:
            self._set_state(self.state)  # re-announce with muted=True
        self._capture_changed.set()
        if self.source.running and not self._capture_wanted():
            self._spawn(self.source.stop())

    def _unmute_now(self) -> None:
        if not self.muted:
            return
        self.muted = False
        self._set_state(self.state)
        self._capture_changed.set()

    # ------------------------------------------------------------------------------------------
    # Frame handling
    # ------------------------------------------------------------------------------------------
    def _capture_wanted(self) -> bool:
        if self._stopping:
            return False
        if self._listen is not None:
            return True
        if self.muted or not self.options.always_listening:
            return False
        return self.wake.ready

    async def _handle_frame(self, frame: Any) -> None:
        if self._listen is not None:
            self._listen_frame(frame)
            return
        self.endpointer.observe(frame)  # keep the noise floor current between sessions
        if self.muted or not self.options.always_listening or not self.wake.ready:
            return
        speaking = self.state is VoiceState.SPEAKING
        if speaking and not (self.options.barge_in and self.wake.supports_barge_in):
            return
        boost = self.options.barge_in_threshold_boost if speaking else 0.0
        try:
            detection = await self.wake.feed(frame, threshold_boost=boost)
        except Exception as exc:
            self._wake_failed(exc)
            return
        self._wake_failures = 0
        if detection is not None and self._listen is None and not self._stopping and not self.muted:
            self._on_wake(detection)

    def _wake_failed(self, exc: BaseException) -> None:
        self._wake_failures += 1
        log.warning("Wake-word engine error (%d): %s", self._wake_failures, _short(exc))
        if self._wake_failures >= 5:
            reason = f"Wake word disabled after repeated errors: {_short(exc)}"
            log.error(reason)
            self.wake = NullWakeEngine(reason)
            self.last_error = reason
            self._flash_error(reason)

    def _listen_frame(self, frame: Any) -> None:
        listen = self._listen
        if listen is None:  # pragma: no cover - guarded by the caller
            return
        event = self.endpointer.process(frame)
        self._emit_level(rms_level(frame))
        if event in (EndpointEvent.SPEECH_END, EndpointEvent.MAX_LENGTH):
            utterance = self.endpointer.finish()
            self._listen = None
            self._start_turn(listen, utterance)
            return
        if event is EndpointEvent.NO_SPEECH:
            self._listen = None
            log.info("No speech after %s", listen.trigger)
            self._to_rest()
            return
        self._maybe_partial(listen)

    def _emit_level(self, level: float) -> None:
        now = time.monotonic()
        if now - self._last_level_at >= 1.0 / max(1.0, self.options.level_rate_hz):
            self._last_level_at = now
            self._events.emit("on_level", float(level))

    # ------------------------------------------------------------------------------------------
    # Wake and listening sessions
    # ------------------------------------------------------------------------------------------
    def _on_wake(self, detection: WakeDetection) -> None:
        self.stats["wakes"] += 1
        if self.state is VoiceState.SPEAKING:
            self.stats["barge_ins"] += 1
            log.info("Barge-in: wake word while speaking (%s %.2f)", detection.engine, detection.score)
        else:
            log.info("Wake word detected (%s %.2f)", detection.engine, detection.score)
        self._interrupt()
        lang = detection.language if detection.language in ("he", "en") else self.language
        self._set_state(VoiceState.WAKE, trigger="wake")
        if detection.command:  # "ג'ארוויס פתח את כרום" — the command came with the wake word
            heard = detection.transcript
            transcript = Transcript(
                text=detection.command,
                language=lang,
                final=True,
                confidence=heard.confidence if heard else None,
                duration_s=heard.duration_s if heard else None,
            )
            self._turn_seq += 1
            self._launch_turn(_Turn(seq=self._turn_seq, trigger="wake", explicit=True, transcript=transcript))
            return
        holdoff, echo = self._play_ack_now("listening", lang)
        self._begin_listen("wake", explicit=True, holdoff_s=holdoff, timeout_s=self.options.no_speech_timeout_s, echo_text=echo)

    def _play_ack_now(self, key: str, language: str) -> tuple[float, str | None]:
        """Start an acknowledgement immediately (cached phrase, else a chime). Returns
        (seconds to treat as possible echo, the phrase text)."""
        if not (self.options.ack_on_wake and self.options.speak_responses):
            return 0.0, None
        clip = self.cache.lookup(key, language) if self.cache is not None else None
        text = ACK_PHRASES.get(key, {}).get(language)
        if clip is None:
            clip, text = earcon(key), None
            if self.cache is not None:  # have it ready next time
                self._spawn(self._quietly(self.cache.get(key, language)))
        self._ack_task = self._spawn(self.sink.play(clip))
        return clip.duration_s + self.options.echo_guard_s, text

    def _begin_listen(
        self,
        trigger: str,
        *,
        explicit: bool,
        holdoff_s: float = 0.0,
        timeout_s: float | None = None,
        retries: int = 0,
        echo_text: str | None = None,
    ) -> None:
        self._listen_seq += 1
        self._listen = _Listen(self._listen_seq, trigger, explicit, retries, echo_text)
        if trigger == "ptt":
            self.endpointer.reset(manual=True, max_utterance_s=max(30.0, 2 * self.options.max_utterance_s))
        else:
            self.endpointer.reset(no_speech_timeout_s=timeout_s, holdoff_s=holdoff_s)
        self._set_state(VoiceState.LISTENING, trigger=trigger)
        self._capture_changed.set()

    def _maybe_partial(self, listen: _Listen) -> None:
        o = self.options
        if not (o.partial_transcripts and self.stt.supports_partials and self.stt.ready):
            return
        ep = self.endpointer
        if not ep.in_speech or not ep.last_voiced:
            return
        frames = ep.utterance_frames
        if frames * FRAME_SECONDS < o.partial_min_s or (frames - listen.last_partial_frames) * FRAME_SECONDS < o.partial_interval_s:
            return
        if self._partial_task is not None and not self._partial_task.done():
            return
        listen.last_partial_frames = frames
        self._partial_task = self._spawn(self._partial(listen, ep.current_audio()))

    async def _partial(self, listen: _Listen, audio: Any) -> None:
        try:
            transcript = await self.stt.transcribe(audio, self.options.stt_language, partial=True)
        except Exception as exc:
            log.debug("Partial transcription failed: %s", exc)
            return
        if self._listen is not listen:
            return
        text, _ = self._clean_command(transcript.text, listen.echo_text)
        if text and not is_noise_transcript(text, transcript.confidence):
            self._events.emit(
                "on_transcript",
                Transcript(text=text, language=transcript.language, final=False, confidence=transcript.confidence),
            )

    # ------------------------------------------------------------------------------------------
    # Turns: transcribe → handler → speak → follow-up
    # ------------------------------------------------------------------------------------------
    def _start_turn(self, listen: _Listen, utterance: Utterance) -> None:
        log.info("Heard %.1f s of speech after %s", utterance.duration_s, listen.trigger)
        self._turn_seq += 1
        echo = listen.echo_text if utterance.started_in_holdoff else None
        self._launch_turn(
            _Turn(self._turn_seq, listen.trigger, listen.explicit, utterance=utterance, retries=listen.retries, echo_text=echo)
        )

    def _launch_turn(self, turn: _Turn) -> None:
        self._set_state(VoiceState.PROCESSING, trigger=turn.trigger)
        self.wake.schedule_reset()  # its buffer holds stale audio from before the command
        self._turn_task = self._spawn(self._run_turn(turn))

    def _is_current(self, turn: _Turn) -> bool:
        return turn.seq == self._turn_seq and not self._stopping

    def _turn_active(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    async def _run_turn(self, turn: _Turn) -> None:
        try:
            await self._turn_body(turn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats["errors"] += 1
            log.warning("Voice command failed: %s", _short(exc), exc_info=log.isEnabledFor(logging.DEBUG))
            if self._is_current(turn):
                self._events.emit("on_pipeline", "done", "error", _label("error", self.language))
                self._flash_error(_short(exc))

    async def _turn_body(self, turn: _Turn) -> None:
        o = self.options
        heard_at = time.perf_counter()               # the user just stopped speaking
        transcript = turn.transcript
        if transcript is None:
            utterance = turn.utterance
            if utterance is None or utterance.speech_s * 1000.0 < o.min_speech_ms:
                transcript = Transcript(text="", language=self.language, duration_s=utterance.duration_s if utterance else 0.0)
            else:
                self._events.emit("on_pipeline", "transcribing", "active", _label("transcribing", self.language))
                try:
                    transcript = await self.stt.transcribe(utterance.audio, o.stt_language)
                except Exception:
                    if self._is_current(turn):
                        self._events.emit("on_pipeline", "transcribing", "error", _label("error", self.language))
                    raise
                self._events.emit("on_timing", "voice.stt", (time.perf_counter() - heard_at) * 1000, True)
                if not self._is_current(turn):
                    return
                self._events.emit("on_pipeline", "transcribing", "done", _label("transcribing", transcript.language))
        language = transcript.language if transcript.language in ("he", "en") else self.language
        text, had_wake = self._clean_command(transcript.text, turn.echo_text)
        noise = is_noise_transcript(text, transcript.confidence)
        if had_wake and noise and turn.retries <= o.max_retries:  # just "JARVIS?" — listen again
            holdoff, echo = self._play_ack_now("listening", language)
            self._begin_listen(
                "retry", explicit=True, holdoff_s=holdoff, timeout_s=o.no_speech_timeout_s, retries=turn.retries + 1, echo_text=echo
            )
            return
        if noise:
            log.info("Ignoring an empty or noise transcript after %s", turn.trigger)
            if turn.explicit and turn.retries < o.max_retries:
                await self._say(None, "not_understood", language, guard=lambda: self._is_current(turn), trigger="reply")
                if self._is_current(turn):
                    self._begin_listen(
                        "retry", explicit=True, holdoff_s=o.echo_guard_s, timeout_s=o.no_speech_timeout_s, retries=turn.retries + 1
                    )
                return
            self._finish_turn(turn)
            return
        language = transcript.language if transcript.language in ("he", "en") else detect_language(text, default=language)
        final = Transcript(text=text, language=language, final=True, confidence=transcript.confidence, duration_s=transcript.duration_s)
        self.last_transcript = final
        self.language = language
        self.stats["turns"] += 1
        self._events.emit("on_transcript", final)
        # The reply can start sounding while the AI is still writing it: the handler puts it in `stream`.
        measured = None if turn.transcript is not None else heard_at
        stream = SpeechStream(max_chars=o.max_speech_chars) if o.speak_responses else None
        final.stream = stream
        early = self._spawn(self._say_early(stream, language, lambda: self._is_current(turn), measured)) \
            if stream is not None else None
        try:
            reply = coerce_reply(await self._call_handler(final))
        except Exception as exc:
            log.warning("Voice command handler failed: %s", _short(exc), exc_info=True)
            if stream is not None and early is not None:
                stream.close()
                await early
            if not self._is_current(turn):
                return
            self.stats["errors"] += 1
            self.last_error = f"Command failed: {_short(exc)}"
            self._events.emit("on_pipeline", "done", "error", _label("error", language))
            await self._say(None, "error", language, guard=lambda: self._is_current(turn), trigger="reply")
            if self._is_current(turn):
                self._flash_error(self.last_error)
            return
        finally:
            if stream is not None:
                stream.close()
        spoke_early = bool(await early) if early is not None else False
        if not self._is_current(turn):
            return
        self._events.emit("on_pipeline", "done", "done", _label("done", language))
        if reply is None:
            self._finish_turn(turn)
            return
        reply_language = reply.language if reply.language in ("he", "en") else (
            detect_language(reply.text, default=language) if reply.text else language
        )
        if spoke_early and stream is not None:          # say only what is left, with no acknowledgement first
            self.stats["early_replies"] += 1
            await self._say(stream.rest_of(reply.text or ""), None, reply_language, guard=lambda: self._is_current(turn),
                            trigger="reply", display=reply.text)
        else:
            await self._say(reply.text, reply.ack_key, reply_language, guard=lambda: self._is_current(turn),
                            trigger="reply", heard_at=measured)
        if not self._is_current(turn):
            return
        if reply.follow_up:
            self._begin_listen("follow_up", explicit=False, holdoff_s=o.echo_guard_s, timeout_s=o.follow_up_window_s)
        else:
            self._finish_turn(turn)

    def _finish_turn(self, turn: _Turn) -> None:
        if self._is_current(turn) and self._listen is None:
            self._to_rest()

    async def _call_handler(self, transcript: Transcript) -> SpeechReply | str | None:
        # The handler is the core's work: shield it so a barge-in or cancel only drops the reply.
        task = asyncio.ensure_future(self.handler(transcript))
        task.add_done_callback(_consume_exception)
        return await asyncio.wait_for(asyncio.shield(task), timeout=self.options.handler_timeout_s)

    def _clean_command(self, text: str, echo_text: str | None) -> tuple[str, bool]:
        cleaned = (text or "").strip()
        if echo_text:
            cleaned = strip_echo(cleaned, echo_text)
        found, rest = match_wake_phrase(cleaned)
        if found:
            return rest.strip(), True
        return cleaned, False

    # ------------------------------------------------------------------------------------------
    # Speech output
    # ------------------------------------------------------------------------------------------
    async def _announce(self, text: str, language: str, interrupt: bool) -> bool:
        if not interrupt:
            await self._wait_quiet()
        if self._stopping:
            return False
        seq = self._turn_seq

        def guard() -> bool:
            return self._turn_seq == seq and not self._stopping

        spoke = await self._say(text, None, language, guard=guard, trigger="announcement")
        if guard() and self._listen is None and self.state is VoiceState.SPEAKING:
            self._to_rest()
        return spoke and guard()

    async def _wait_quiet(self) -> None:
        # A brief error flash counts as busy; a lasting microphone problem does not.
        while not self._stopping and (self.busy or (self.state is VoiceState.ERROR and self._source_problem is None)):
            await asyncio.sleep(0.05)

    async def _say_early(self, stream: SpeechStream, language: str, guard: Callable[[], bool],
                         heard_at: float | None) -> bool:
        """Speak the reply's sentences as the AI writes them, while the handler is still at work: each piece is
        synthesized as soon as it is complete and played after the one before. True when something was spoken."""
        piece = await stream.next()
        if piece is None or not guard():
            return False
        o = self.options
        ready: asyncio.Queue[tuple[str, str, asyncio.Task] | None] = asyncio.Queue()

        async def synthesize_pieces(first: str) -> None:
            text: str | None = first
            while text is not None:
                body = prepare_speech_text(text, max_chars=o.max_speech_chars)
                if body:
                    lang = detect_language(body, default=language)
                    ready.put_nowait((body, lang, self._spawn(self._synthesize(body, lang))))
                text = await stream.next()
            ready.put_nowait(None)

        spoken: list[str] = []
        async with self._speech_lock:
            producer = self._spawn(synthesize_pieces(piece))
            try:
                while (item := await ready.get()) is not None:
                    body, lang, job = item
                    clip = await job
                    if not guard():
                        break
                    if clip is None:
                        continue
                    spoken.append(body)
                    if len(spoken) == 1:
                        self._speaking(body, lang, "reply", False, heard_at)
                    else:                             # the text on screen grows with the speech
                        self._events.emit("on_speech", " ".join(spoken), lang)
                    if not await self._play(clip):
                        break
            finally:
                producer.cancel()
                while not ready.empty():
                    left = ready.get_nowait()
                    if left is not None:
                        left[2].cancel()
        return bool(spoken)

    async def _say(self, text: str | None, ack_key: str | None, language: str, *, guard: Callable[[], bool], trigger: str,
                   heard_at: float | None = None, display: str | None = None) -> bool:
        """Speak an acknowledgement and/or text. Returns False when interrupted or superseded. `heard_at`: when
        the user stopped speaking, to measure how long the reply took to start sounding. `display`: the text shown
        while speaking, when it is more than what is said (the rest of a reply that started early)."""
        o = self.options
        if not o.speak_responses:
            return True
        ack_text = ACK_PHRASES.get(ack_key, {}).get(language) if ack_key else None
        if ack_key and ack_text is None:
            log.warning("Unknown acknowledgement key %r", ack_key)
        body = prepare_speech_text(text, max_chars=o.max_speech_chars) if text else ""
        if not ack_text and not body:
            return True
        spoken = prepare_speech_text(display, max_chars=o.max_speech_chars) if display else \
            " ".join(p for p in (ack_text, body) if p)
        chunks = split_sentences(body) if body else []
        async with self._speech_lock:
            if not guard():
                return False
            started = False
            prefetch: asyncio.Task | None = None
            try:
                if ack_text and ack_key:
                    clip = await self._ack_clip(ack_key, language)
                    if clip is not None:
                        if not guard():
                            return False
                        started = self._speaking(spoken, language, trigger, started, heard_at)
                        if not await self._play(clip):
                            return False
                if chunks:
                    prefetch = self._spawn(self._synthesize(chunks[0], language))
                for i in range(len(chunks)):
                    clip = await prefetch if prefetch is not None else None
                    prefetch = self._spawn(self._synthesize(chunks[i + 1], language)) if i + 1 < len(chunks) else None
                    if clip is None:
                        continue
                    if not guard():
                        return False
                    started = self._speaking(spoken, language, trigger, started, heard_at)
                    if not await self._play(clip):
                        return False
                return guard()
            finally:
                if prefetch is not None and not prefetch.done():
                    prefetch.cancel()

    def _speaking(self, spoken: str, language: str, trigger: str, started: bool, heard_at: float | None = None) -> bool:
        if not started:
            self._set_state(VoiceState.SPEAKING, trigger=trigger)
            self._events.emit("on_speech", spoken, language)
            if heard_at is not None:
                self._events.emit("on_timing", "voice.reply", (time.perf_counter() - heard_at) * 1000, True)
        return True

    async def _ack_clip(self, key: str, language: str) -> AudioClip | None:
        if self.cache is None:
            text = ACK_PHRASES.get(key, {}).get(language)
            return await self._synthesize(text, language) if text else None
        clip = self.cache.lookup(key, language)
        if clip is not None:
            return clip
        try:
            return await self.cache.get(key, language)
        except Exception as exc:
            self._tts_failed(exc)
            return None

    async def _synthesize(self, text: str, language: str) -> AudioClip | None:
        started = time.perf_counter()
        try:
            clip = await self.tts.synthesize(text, language)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._tts_failed(exc)
            return None
        self._events.emit("on_timing", f"voice.tts.{clip.engine or 'speech'}", (time.perf_counter() - started) * 1000, True)
        return clip

    def _tts_failed(self, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._tts_warned_at > 60.0:
            self._tts_warned_at = now
            log.warning("Speech synthesis failed: %s", _short(exc))

    async def _play(self, clip: AudioClip) -> bool:
        levels = self._spawn(self._level_feed(clip.levels(FRAME_SECONDS)))
        try:
            return await self.sink.play(clip)
        except AudioError as exc:
            log.warning("Playback failed: %s", _short(exc))
            return True  # keep the conversation going; the UI still shows the text
        finally:
            levels.cancel()

    async def _level_feed(self, levels: list[float]) -> None:
        start = time.monotonic()
        try:
            for i, level in enumerate(levels):
                delay = start + i * FRAME_SECONDS - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._events.emit("on_level", level)
        finally:
            self._events.emit("on_level", 0.0)

    # ------------------------------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------------------------------
    def _interrupt(self) -> None:
        """Stop what the voice channel is doing: playback, listening and the pending reply."""
        self._turn_seq += 1  # supersedes the current turn and any announcement in progress
        task, self._turn_task = self._turn_task, None
        if task is not None and not task.done():
            task.cancel()
        ack, self._ack_task = self._ack_task, None
        if ack is not None and not ack.done():
            ack.cancel()
        self._listen = None
        self.sink.stop()

    def _rest_state(self) -> VoiceState:
        return VoiceState.DISABLED if self.disabled_reason else VoiceState.IDLE

    def _to_rest(self) -> None:
        self._set_state(self._rest_state())

    def _set_state(self, state: VoiceState, *, trigger: str | None = None, error: str | None = None) -> None:
        self.state = state
        self._state_version += 1
        if error is None and state is VoiceState.DISABLED:
            error = self.disabled_reason
        self._events.emit("on_state", state, {"muted": self.muted, "trigger": trigger, "error": error})

    def _flash_error(self, message: str) -> None:
        self.last_error = message
        self._set_state(VoiceState.ERROR, error=message)
        version = self._state_version
        self._spawn(self._clear_error(version))

    async def _clear_error(self, version: int) -> None:
        await asyncio.sleep(self.options.error_display_s)
        if self._state_version == version and self.state is VoiceState.ERROR:
            self._to_rest()

    def _source_failed(self, exc: BaseException) -> None:
        message = _short(exc)
        changed = message != self._source_problem
        if changed:
            log.warning("Microphone unavailable: %s", message)
        self._source_problem = message
        self.last_error = message
        missing = isinstance(exc, AudioDeviceError) and exc.missing
        if missing:
            changed = changed or self.disabled_reason != message
            self.disabled_reason = message
            self._disabled_by_source = True
        if self._listen is not None:  # e.g. push-to-talk without a microphone
            self._listen = None
            changed = True
        if self._turn_active() or self.state is VoiceState.SPEAKING:
            return
        target = VoiceState.DISABLED if missing else VoiceState.ERROR
        if not changed and self.state is target:
            return  # the retry loop calls this again and again: announce only changes
        if missing:
            self._to_rest()
        else:
            self._set_state(VoiceState.ERROR, error=message)

    def _source_ok(self) -> None:
        if self._source_problem is None:
            return
        log.info("Microphone available: %s", self.source.device_name or self.source.name)
        self._source_problem = None
        if self._disabled_by_source:
            self.disabled_reason = None
            self._disabled_by_source = False
        if self.state in (VoiceState.DISABLED, VoiceState.ERROR) and not self.busy:
            self._to_rest()

    async def _stop_source(self) -> None:
        if self.source.running:
            with contextlib.suppress(Exception):
                await self.source.stop()

    async def _pause(self, seconds: float) -> None:
        self._capture_changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._capture_changed.wait(), seconds)

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.debug("Voice background task failed: %s", task.exception())

    @staticmethod
    async def _quietly(coro: Coroutine[Any, Any, Any]) -> None:
        try:
            await coro
        except Exception as exc:
            log.debug("Background voice job failed: %s", exc)


def _consume_exception(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


__all__ = ["PipelineOptions", "VoicePipeline", "coerce_reply", "strip_echo"]
