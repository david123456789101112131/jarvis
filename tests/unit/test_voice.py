"""Voice subsystem tests.

Everything runs without a microphone, speakers, network or downloaded models: audio comes
from numpy arrays / espeak-ng WAVs through `ArraySource` (paced like a real microphone, but
time-scaled), playback goes to a recording `NullSink`, and the wake word / STT / TTS engines
are small fakes. Tests that need real models are marked `voice_models` and skip unless the
models are already on disk (`JARVIS_TEST_MODELS_DIR=<dir with openwakeword/ and whisper/>`,
or the app's default cache); tests that need the internet are marked `network`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jarvis.common.text import detect_language
from jarvis.config.paths import resolve_paths
from jarvis.config.settings import PathsSettings, VoiceSettings
from jarvis.security.secrets import SecretStore
from jarvis.voice import audio as audio_mod
from jarvis.voice import stt as stt_mod
from jarvis.voice import tts as tts_mod
from jarvis.voice import wakeword as wake_mod
from jarvis.voice.audio import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    ArraySource,
    AudioClip,
    AudioDeviceError,
    AudioSource,
    NullSink,
)
from jarvis.voice.base import ACK_PHRASES, SpeechReply, Transcript, VoiceState
from jarvis.voice.hotkey import CtrlTapWatch, TapDetector, watch_ctrl_taps
from jarvis.voice.pipeline import PipelineOptions, VoicePipeline, coerce_reply, strip_echo
from jarvis.voice.service import VoiceService
from jarvis.voice.stt import (
    FallbackSTT,
    FasterWhisperSTT,
    NullSTT,
    OpenAISTT,
    STTEngine,
    STTError,
    STTUnavailable,
)
from jarvis.voice.tts import (
    FallbackTTS,
    NullTTS,
    OneCoreTTS,
    PhraseCache,
    SapiTTS,
    TTSEngine,
    TTSError,
    prepare_speech_text,
    resolve_language,
    split_sentences,
)
from jarvis.voice.vad import EndpointConfig, Endpointer, EndpointEvent, VoiceActivityDetector
from jarvis.voice.wakeword import (
    NameWakeEngine,
    NullWakeEngine,
    TranscriptWakeEngine,
    WakeWordEngine,
    WakeWordError,
    create_wake_engine,
    match_wake_phrase,
)

try:
    import numpy as np
except ImportError:  # pragma: no cover - the voice extra is not installed
    np = None  # type: ignore[assignment]

SRC = Path(__file__).resolve().parents[2] / "src"
needs_numpy = pytest.mark.skipif(np is None, reason="numpy (voice extra) is not installed")


def _have(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


ESPEAK = shutil.which("espeak-ng")
needs_espeak = pytest.mark.skipif(ESPEAK is None, reason="espeak-ng is not installed")
VAD_BACKENDS = ["energy", pytest.param("webrtcvad", marks=pytest.mark.skipif(not _have("webrtcvad"), reason="no webrtcvad"))]


# =============================================================================================
# Synthetic audio and fake engines
# =============================================================================================
MARKER = 12345  # a frame starting with two MARKER samples is "the wake word" for MarkerWake


def marker_frame() -> Any:
    frame = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    frame[:2] = MARKER
    return frame


def speechlike(seconds: float, *, f0: float = 170.0, amp: float = 0.3) -> Any:
    """Harmonic tone with a 4 Hz syllable envelope — voiced for webrtcvad and the energy VAD."""
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 6))
    x = sig * (0.6 + 0.4 * np.sin(2 * np.pi * 4.0 * t))
    return (x / np.max(np.abs(x)) * amp * 32767).astype(np.int16)


def silence(seconds: float) -> Any:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.int16)


def frames_of(*parts: Any) -> list[Any]:
    return list(audio_mod.iter_frames(np.concatenate(parts)))


def espeak_wav(path: Path, text: str, voice: str = "en", speed: int = 150) -> Path:
    subprocess.run([ESPEAK, "-v", voice, "-s", str(speed), "-w", str(path), text], check=True, capture_output=True, timeout=30)
    return path


class MarkerWake(WakeWordEngine):
    name = "fake"
    keyword = "hey jarvis"
    threaded = False

    def __init__(self) -> None:
        super().__init__(threshold=0.5, refractory_s=0.0)
        self._ready = True
        self.resets = 0

    def process(self, frame: Any) -> float:
        return 1.0 if int(frame[0]) == MARKER and int(frame[1]) == MARKER else 0.0

    def reset(self) -> None:
        self.resets += 1


class ScriptedWake(WakeWordEngine):
    """Returns scores from a list (then 0)."""

    name = "scripted"
    threaded = False

    def __init__(self, scores: list[float], *, threshold: float = 0.5, refractory_s: float = 0.0) -> None:
        super().__init__(threshold=threshold, refractory_s=refractory_s)
        self.scores = list(scores)
        self._ready = True

    def process(self, frame: Any) -> float:
        return self.scores.pop(0) if self.scores else 0.0


class FakeSTT(STTEngine):
    name = "fake_stt"

    def __init__(self, texts: Any = (), *, delay: float = 0.0, error: Exception | None = None) -> None:
        super().__init__()
        self.reason = "fake speech recognition"
        self.texts = list(texts)
        self.delay = delay
        self.error = error
        self.calls: list[dict[str, Any]] = []

    @property
    def ready(self) -> bool:
        return True

    async def transcribe(self, audio: Any, language: str = "auto", *, hint: bool = True, partial: bool = False) -> Transcript:
        self.calls.append({"samples": len(audio), "language": language, "hint": hint, "partial": partial})
        if partial:
            return Transcript(text="", language=self.last_language, final=False)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        text = self.texts.pop(0) if self.texts else ""
        language = detect_language(text, default="en") if text else self.last_language
        return Transcript(text=text, language=language, confidence=0.9, duration_s=len(audio) / SAMPLE_RATE)


class HeardSTT(FakeSTT):
    """FakeSTT that also answers the quick check of how a stretch of speech starts (`partial=True`) with `head`."""

    def __init__(self, texts: Any = (), *, head: str = "", online: bool = False) -> None:
        super().__init__(texts)
        self.head = head
        self.online = online

    async def transcribe(self, audio: Any, language: str = "auto", *, hint: bool = True, partial: bool = False) -> Transcript:
        if partial:
            self.calls.append({"samples": len(audio), "language": language, "hint": hint, "partial": True})
            return Transcript(text=self.head, language="he", final=False, confidence=0.9)
        return await super().transcribe(audio, language, hint=hint, partial=partial)


async def hear(engine: WakeWordEngine, *parts: Any, busy: bool = False) -> list[Any]:
    """Feed the audio frame by frame, as the pipeline does, letting the background checks run in between."""
    hits = []
    for frame in frames_of(*parts):
        hit = await engine.feed(frame, busy=busy)
        if hit is not None:
            hits.append(hit)
        for _ in range(3):
            await asyncio.sleep(0)
    return hits


class FailingSTT(STTEngine):
    name = "broken_stt"

    def load(self) -> None:
        raise STTUnavailable("model could not be loaded")

    async def transcribe(self, audio: Any, language: str = "auto", *, hint: bool = True, partial: bool = False) -> Transcript:
        raise STTUnavailable("model could not be loaded")


class FakeTTS(TTSEngine):
    def __init__(self, name: str = "fake_tts", *, seconds_per_char: float = 0.03, fail: bool = False) -> None:
        super().__init__()
        self.name = name
        self.reason = f"{name} voices"
        self.seconds_per_char = seconds_per_char
        self.fail = fail
        self.calls: list[tuple[str, str | None]] = []

    def voice_for(self, language: str) -> str:
        return f"{self.name}-{language}"

    async def synthesize(self, text: str, language: str | None = None) -> AudioClip:
        self.calls.append((text, language))
        if self.fail:
            raise TTSError(f"{self.name} is down")
        lang = resolve_language(text, language)
        n = max(1, int(SAMPLE_RATE * max(0.1, len(text) * self.seconds_per_char)))
        return AudioClip(np.full(n, 800, dtype=np.int16), SAMPLE_RATE, text=text, language=lang, engine=self.name,
                         voice=self.voice_for(lang), source_id=self.cache_id(lang))


class BrokenSource(AudioSource):
    name = "broken"

    def __init__(self, *, missing: bool = True) -> None:
        super().__init__()
        self.missing = missing
        self.attempts = 0

    @property
    def running(self) -> bool:
        return False

    async def start(self) -> None:
        self.attempts += 1
        raise AudioDeviceError("No microphone found" if self.missing else "Device is busy", missing=self.missing)

    async def stop(self) -> None:
        return None

    async def read(self) -> Any:
        return None


class Recorder:
    """VoiceEventSink that records everything."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def on_state(self, state: VoiceState, info: dict[str, Any]) -> None:
        self.events.append(("state", state.value, info.get("trigger"), info.get("muted"), info.get("error")))

    async def on_info(self, info: dict[str, Any]) -> None:
        self.events.append(("info", info["engines"]["wake"], info["wake_word"]))

    async def on_level(self, level: float) -> None:
        self.events.append(("level", level))

    async def on_transcript(self, transcript: Transcript) -> None:
        self.events.append(("transcript", transcript.text, transcript.final, transcript.language))

    async def on_pipeline(self, step: str, status: str, label: str) -> None:
        self.events.append(("pipeline", step, status, label))

    async def on_speech(self, text: str, language: str) -> None:
        self.events.append(("speech", text, language))

    async def on_timing(self, name: str, ms: float, ok: bool) -> None:
        self.events.append(("timing", name, ms, ok))

    def of(self, kind: str) -> list[tuple]:
        return [e[1:] for e in self.events if e[0] == kind]

    def states(self) -> list[str]:
        return [e[1] for e in self.events if e[0] == "state"]

    def transitions(self) -> list[str]:
        """States without consecutive duplicates (re-announcements, e.g. on mute)."""
        out: list[str] = []
        for s in self.states():
            if not out or out[-1] != s:
                out.append(s)
        return out


async def wait_for(predicate: Any, timeout: float = 8.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def fast_options(**overrides: Any) -> PipelineOptions:
    values: dict[str, Any] = {
        "follow_up_window_s": 1.0,
        "no_speech_timeout_s": 1.0,
        "error_display_s": 0.1,
        "partial_transcripts": False,
    }
    values.update(overrides)
    return PipelineOptions(**values)


class Harness:
    """A pipeline on fake engines with simulated real-time audio (×20 speed by default)."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        texts: Any = (),
        reply: Any = "Done.",
        wake: WakeWordEngine | None = None,
        stt: STTEngine | None = None,
        handler: Any = None,
        options: PipelineOptions | None = None,
        source: AudioSource | None = None,
        time_scale: float = 0.05,
        sink_speed: float = 0.05,
    ) -> None:
        self.source = source or ArraySource(realtime=True, time_scale=time_scale, fill_silence=True)
        self.sink = NullSink(realtime=True, speed=sink_speed)
        self.wake = wake or MarkerWake()
        self.stt = stt or FakeSTT(texts)
        self.tts = FakeTTS()
        self.cache = PhraseCache(self.tts, tmp_path / "tts")
        self.events = Recorder()
        self.reply = reply
        self.handled: list[Transcript] = []
        self.pipeline = VoicePipeline(
            source=self.source,
            sink=self.sink,
            wake=self.wake,
            stt=self.stt,
            tts=self.tts,
            handler=handler or self.handler,
            events=self.events,
            options=options or fast_options(),
            phrase_cache=self.cache,
        )

    async def handler(self, transcript: Transcript) -> Any:
        self.handled.append(transcript)
        return self.reply

    def say(self, seconds: float = 1.0, *, wake: bool = True) -> None:
        if wake:
            self.source.append_frames([marker_frame()])
        self.source.append(speechlike(seconds))

    def played(self) -> list[str]:
        return [c.text for c in self.sink.played]

    @property
    def quiet(self) -> bool:
        p = self.pipeline
        return p.state in (VoiceState.IDLE, VoiceState.DISABLED) and not p.busy

    async def __aenter__(self) -> Harness:
        self.pipeline.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.pipeline.stop()


@pytest.fixture
def paths(jarvis_home: Path):
    return resolve_paths(PathsSettings()).ensure()


@pytest.fixture
def memory_secrets() -> SecretStore:
    return SecretStore(None, use_keyring=False, environ={})


def fake_have(missing: set[str]):
    def _have(*modules: str) -> bool:
        return not any(m in missing for m in modules)

    return _have


# =============================================================================================
# Audio helpers
# =============================================================================================
@needs_numpy
class TestAudio:
    def test_rms_level_scale(self) -> None:
        assert audio_mod.rms_level(silence(0.08)) == 0.0
        loud = audio_mod.rms_level(speechlike(0.08, amp=0.9))
        soft = audio_mod.rms_level(speechlike(0.08, amp=0.01))
        assert 0.0 < soft < loud <= 1.0

    def test_wav_roundtrip_and_resample(self, tmp_path: Path) -> None:
        t = np.arange(48_000) / 48_000
        tone = (np.sin(2 * np.pi * 1000 * t) * 12000).astype(np.int16)
        path = audio_mod.write_wav(tmp_path / "tone.wav", tone, 48_000)
        samples, rate = audio_mod.read_wav(path)
        assert rate == 48_000 and np.array_equal(samples, tone)
        down = audio_mod.resample(samples, 48_000, SAMPLE_RATE)
        assert abs(len(down) - SAMPLE_RATE) <= 1 and down.dtype == np.int16
        spectrum = np.abs(np.fft.rfft(down.astype(np.float32)))
        assert abs(np.argmax(spectrum) * SAMPLE_RATE / len(down) - 1000) < 5  # the tone survived
        clip = AudioClip.from_wav(audio_mod.wav_bytes(tone, 48_000), text="x")
        assert clip.sample_rate == 48_000 and abs(clip.duration_s - 1.0) < 1e-6
        assert clip.resampled(SAMPLE_RATE).sample_rate == SAMPLE_RATE

    def test_stream_resampler_matches_offline(self) -> None:
        x = speechlike(1.0)
        up = audio_mod.resample(x, SAMPLE_RATE, 48_000)
        rs = audio_mod.StreamResampler(48_000, SAMPLE_RATE)
        chunks = [rs.process(up[i : i + 1234]) for i in range(0, len(up), 1234)]
        streamed = np.concatenate(chunks)
        offline = audio_mod.resample(up, 48_000, SAMPLE_RATE)
        n = min(len(streamed), len(offline))
        assert abs(len(streamed) - len(offline)) <= 64  # only the filter tail is still pending
        corr = np.corrcoef(streamed[100 : n - 100].astype(float), offline[100 : n - 100].astype(float))[0, 1]
        assert corr > 0.99  # same signal, aligned (no phase drift across chunk boundaries)

    def test_iter_frames_pads_last_frame(self) -> None:
        frames = list(audio_mod.iter_frames(np.ones(3000, dtype=np.int16)))
        assert [len(f) for f in frames] == [FRAME_SAMPLES] * 3
        assert frames[-1][3000 - 2 * FRAME_SAMPLES :].sum() == 0

    async def test_array_source_paces_and_fills_silence(self) -> None:
        src = ArraySource(speechlike(0.8), realtime=True, time_scale=0.25)
        await src.start()
        t0 = time.monotonic()
        frames = [f async for f in src]
        assert len(frames) == 10 and src.exhausted
        assert time.monotonic() - t0 >= 0.1  # 10 frames × 80 ms × 0.25 ≈ 0.2 s
        filler = ArraySource(speechlike(0.08), fill_silence=True)
        await filler.start()
        first, second = await filler.read(), await filler.read()
        assert first.any() and not second.any() and not filler.exhausted

    async def test_null_sink_stop_interrupts(self) -> None:
        sink = NullSink(realtime=True)
        clip = AudioClip(silence(2.0), SAMPLE_RATE, text="long")
        task = asyncio.create_task(sink.play(clip))
        await asyncio.sleep(0.05)
        assert sink.playing
        sink.stop()
        assert await asyncio.wait_for(task, 1.0) is False
        assert sink.interrupted == [clip]
        assert await sink.play(AudioClip(silence(0.01), SAMPLE_RATE)) is True  # usable again

    def test_earcon_and_decode(self) -> None:
        chime = audio_mod.earcon("listening")
        assert 0.05 < chime.duration_s < 0.6 and chime.source_id.startswith("earcon")
        clip = audio_mod.decode_audio_bytes(audio_mod.wav_bytes(speechlike(0.5), SAMPLE_RATE))
        assert abs(clip.duration_s - 0.5) < 0.01

    def test_list_devices_never_raises(self) -> None:
        devices = audio_mod.list_devices()
        assert set(devices) >= {"input", "output", "default_input", "default_output", "error"}

    def test_create_sink_falls_back_to_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def nothing(self: Any) -> Any:
            raise AudioDeviceError("No audio output device found", missing=True)

        monkeypatch.setattr(audio_mod.SoundDeviceSink, "resolve", nothing)
        monkeypatch.setattr(audio_mod.MiniaudioSink, "resolve", nothing)
        sink, reason = audio_mod.create_sink("")
        assert isinstance(sink, NullSink) and "No audio output device" in reason


# =============================================================================================
# Voice activity detection and end-pointing
# =============================================================================================
@needs_numpy
class TestEndpointer:
    @pytest.mark.parametrize("backend", VAD_BACKENDS)
    def test_burst_start_and_end(self, backend: str) -> None:
        ep = Endpointer(EndpointConfig(backend=backend))
        ep.reset()
        frames = frames_of(silence(1.04), speechlike(1.04), silence(1.6))  # 13 + 13 + 20 frames
        events = [(i, ev) for i, f in enumerate(frames) if (ev := ep.process(f)) is not None]
        assert [ev for _, ev in events] == [EndpointEvent.SPEECH_START, EndpointEvent.SPEECH_END]
        (start, _), (end, _) = events
        assert 13 <= start <= 15  # two consecutive voiced frames
        assert 33 <= end <= 36  # ≈ 700 ms of silence after the burst
        utt = ep.finish()
        assert utt.reason == "speech_end" and not utt.started_in_holdoff
        assert 1.3 <= utt.duration_s <= 1.8  # pre-roll + burst + trailing
        assert 0.9 <= utt.speech_s <= 1.2

    def test_no_speech_timeout(self) -> None:
        ep = Endpointer()
        ep.reset(no_speech_timeout_s=1.0)
        events = [ep.process(f) for f in frames_of(silence(2.0))]
        assert events.index(EndpointEvent.NO_SPEECH) == 12  # the 13th silent frame (1.04 s)
        assert not ep.finish().has_speech

    def test_max_length(self) -> None:
        ep = Endpointer(EndpointConfig(max_utterance_s=1.0))
        ep.reset()
        events = [ep.process(f) for f in frames_of(speechlike(3.0))]
        assert EndpointEvent.MAX_LENGTH in events
        assert ep.finish().duration_s <= 1.2

    def test_click_does_not_start_an_utterance(self) -> None:
        click = silence(0.08)
        click[200:840] = (np.random.default_rng(0).standard_normal(640) * 15000).astype(np.int16)
        ep = Endpointer()
        ep.reset(no_speech_timeout_s=1.0)
        events = [ep.process(f) for f in frames_of(silence(0.4), click, silence(1.0))]
        assert EndpointEvent.SPEECH_START not in events and EndpointEvent.NO_SPEECH in events

    def test_holdoff_keeps_speech_that_continues(self) -> None:
        ep = Endpointer()
        ep.reset(holdoff_s=0.48)  # 6 frames of "our own acknowledgement"
        frames = frames_of(silence(0.16), speechlike(1.2), silence(1.0))
        events = [(i, ev) for i, f in enumerate(frames) if (ev := ep.process(f)) is not None]
        assert events[0] == (6, EndpointEvent.SPEECH_START)  # not before the hold-off ends
        utt = ep.finish()
        assert utt.started_in_holdoff
        assert utt.speech_s >= 1.1  # the part spoken during the hold-off was walked back to

    def test_holdoff_ignores_echo_only(self) -> None:
        ep = Endpointer()
        ep.reset(holdoff_s=0.48, no_speech_timeout_s=0.8)
        events = [ep.process(f) for f in frames_of(speechlike(0.3), silence(1.5))]
        assert EndpointEvent.SPEECH_START not in events and EndpointEvent.NO_SPEECH in events

    def test_manual_mode_collects_until_finish(self) -> None:
        ep = Endpointer()
        ep.reset(manual=True)
        events = [ep.process(f) for f in frames_of(silence(1.0), speechlike(0.8), silence(1.5))]
        assert events.count(EndpointEvent.SPEECH_START) == 1 and EndpointEvent.SPEECH_END not in events
        utt = ep.finish()
        assert utt.reason == "manual" and utt.has_speech
        assert 0.8 <= utt.duration_s <= 1.6  # of 3.3 s: leading silence trimmed to the pre-roll, trailing too

    def test_noise_floor_adapts_to_steady_noise(self) -> None:
        vad = VoiceActivityDetector(backend="energy")
        hum = (np.random.default_rng(1).standard_normal(SAMPLE_RATE * 4) * 300).astype(np.int16)
        decisions = [vad.is_speech(f) for f in audio_mod.iter_frames(hum)]
        assert not any(decisions[-10:])  # steady noise stops counting as speech
        assert vad.is_speech(speechlike(0.08, amp=0.3))

    @needs_espeak
    def test_espeak_sentence(self, tmp_path: Path) -> None:
        speech = audio_mod.load_audio(espeak_wav(tmp_path / "s.wav", "open chrome and search for the weather in tel aviv"))
        length = len(speech) / SAMPLE_RATE
        ep = Endpointer()
        ep.reset()
        events = [ep.process(f) for f in frames_of(silence(1.0), speech, silence(1.5))]
        assert events.count(EndpointEvent.SPEECH_START) == 1 and EndpointEvent.SPEECH_END in events
        utt = ep.finish()
        assert length - 0.4 <= utt.duration_s <= length + 0.8


# =============================================================================================
# Wake word
# =============================================================================================
class TestWakePhrase:
    @pytest.mark.parametrize(
        ("text", "found", "rest"),
        [
            ("ג'ארוויס פתח את כרום", True, "פתח את כרום"),
            ("ג׳ארוויס, מה השעה?", True, "מה השעה?"),
            ("היי ג'ארוויס תפתח יוטיוב", True, "תפתח יוטיוב"),
            ("אה, ג'ארוויס תעצור", True, "תעצור"),
            ("Hey JARVIS, open Chrome", True, "open Chrome"),
            ("Jarvis?", True, ""),
            ("jarvis", True, ""),
            ("um, jarvis what's the time", True, "what's the time"),
            ("Jervis open notepad", True, "open notepad"),
            ("hey jarvi play music", True, "play music"),
            ("open chrome", False, ""),
            ("I told jarvis yesterday", False, ""),
            ("", False, ""),
        ],
    )
    def test_match(self, text: str, found: bool, rest: str) -> None:
        assert match_wake_phrase(text) == (found, rest)


@needs_numpy
class TestWakeEngines:
    async def test_threshold_boost_and_refractory(self) -> None:
        engine = ScriptedWake([0.55, 0.0, 0.9, 0.9, 0.0, 0.0, 0.0, 0.9], refractory_s=0.24)  # 3 frames
        frame = silence(0.08)
        assert await engine.feed(frame, threshold_boost=0.15) is None  # 0.55 < 0.65 while speaking
        assert await engine.feed(frame) is None
        assert (await engine.feed(frame)).score == pytest.approx(0.9)
        assert await engine.feed(frame) is None  # refractory
        for _ in range(3):
            await engine.feed(frame)
        assert await engine.feed(frame) is not None

    async def test_transcript_wake_with_inline_command(self) -> None:
        stt = FakeSTT(["ג'ארוויס פתח את כרום"])
        engine = TranscriptWakeEngine(stt)
        engine.load()
        assert engine.ready and engine.keyword == "jarvis"
        detection = None
        for frame in frames_of(silence(0.5), speechlike(1.2), silence(0.8)):
            detection = detection or await engine.feed(frame)
        assert detection is not None
        assert detection.command == "פתח את כרום" and detection.language == "he"
        assert stt.calls[0]["hint"] is False  # no name-biasing prompt for wake detection

    async def test_transcript_wake_ignores_other_speech(self) -> None:
        engine = TranscriptWakeEngine(FakeSTT(["מה השעה"]))
        engine.load()
        hits = [await engine.feed(f) for f in frames_of(silence(0.3), speechlike(1.0), silence(0.8))]
        assert not any(hits)

    async def test_the_name_alone_wakes_it(self) -> None:
        stt = HeardSTT(["ג'רביס"])
        engine = NameWakeEngine(MarkerWake(), stt, language="he")
        assert engine.ready and engine.keyword == "jarvis"
        hits = await hear(engine, silence(0.4), speechlike(0.8), silence(1.0))
        assert len(hits) == 1 and hits[0].engine == "name" and hits[0].command is None
        assert [c["partial"] for c in stt.calls] == [False] and stt.calls[0]["hint"] is False

    async def test_the_name_and_a_command_in_one_breath(self) -> None:
        stt = HeardSTT(["ג'רביס, מה השעה?"], head="ג'רביס מה")
        engine = NameWakeEngine(MarkerWake(), stt, language="he")
        hits = await hear(engine, silence(0.3), speechlike(2.6), silence(1.0))
        assert [h.command for h in hits] == ["מה השעה?"] and hits[0].language == "he"
        assert [c["partial"] for c in stt.calls] == [True, False]       # its start, then the whole of it

    async def test_other_speech_is_not_the_name(self) -> None:
        stt = HeardSTT(["מה השעה"], head="אז אמרתי לו")
        engine = NameWakeEngine(MarkerWake(), stt, language="he")
        assert await hear(engine, silence(0.3), speechlike(3.0), silence(1.0)) == []
        assert [c["partial"] for c in stt.calls] == [True]              # its start was enough: not the name
        assert await hear(engine, speechlike(0.9), silence(1.0)) == []   # a short one is heard whole, and is not it
        assert len(stt.calls) == 2

    async def test_nothing_is_checked_while_busy_or_with_online_recognition(self) -> None:
        stt = HeardSTT(["ג'רביס"])
        engine = NameWakeEngine(MarkerWake(), stt, language="he")
        assert await hear(engine, speechlike(0.8), silence(1.0), busy=True) == [] and stt.calls == []
        online = HeardSTT(["ג'רביס"], online=True)
        engine = NameWakeEngine(MarkerWake(), online, language="he")
        assert await hear(engine, speechlike(0.8), silence(1.0)) == [] and online.calls == []
        hits = await hear(engine, silence(0.2)) + [await engine.feed(marker_frame())]
        assert [h.engine for h in hits if h] == ["fake"]                 # the wake model's phrase still works

    async def test_a_check_that_comes_back_late_is_dropped(self) -> None:
        stt = HeardSTT(["ג'רביס", "ג'רביס"])
        engine = NameWakeEngine(MarkerWake(), stt, language="he")
        assert await hear(engine, speechlike(0.8), silence(0.56)) == []  # the stretch just ended; its check ran
        engine.schedule_reset()                                          # JARVIS woke another way meanwhile
        assert await hear(engine, silence(0.5)) == []
        assert await hear(engine, speechlike(0.8), silence(0.56)) == []
        assert len(await hear(engine, silence(0.2))) == 1                # not dropped: it wakes JARVIS
        assert len(stt.calls) == 2

    def test_factory_adds_the_name_to_the_wake_model(self, paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wake_mod, "_have", fake_have({"pvporcupine"}))

        def loaded(self: Any) -> None:
            self._ready = True
            self.reason = "openWakeWord 'hey jarvis'"

        monkeypatch.setattr(wake_mod.OpenWakeWordEngine, "load", loaded)
        engine, reason = create_wake_engine(VoiceSettings(), paths, None, FakeSTT(), load=True)
        assert isinstance(engine, NameWakeEngine) and engine.model.name == "openwakeword" and engine.ready
        assert "and the name alone (speech recognition on this computer)" in reason
        off, _ = create_wake_engine(VoiceSettings(wake_by_name=False), paths, None, FakeSTT(), load=True)
        assert off.name == "openwakeword"
        online, _ = create_wake_engine(VoiceSettings(), paths, None, HeardSTT(online=True), load=True)
        assert online.name == "openwakeword"                             # audio would leave the computer: no
        for e in (engine, off, online):
            e.close()

    def test_transcript_wake_needs_stt(self) -> None:
        with pytest.raises(WakeWordError):
            TranscriptWakeEngine(FailingSTT()).load()

    def test_factory_none(self, paths: Any) -> None:
        engine, reason = create_wake_engine(VoiceSettings(wake_word_engine="none"), paths, None, FakeSTT())
        assert isinstance(engine, NullWakeEngine) and "turned off" in reason

    def test_factory_auto_falls_back_to_transcript(self, paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wake_mod, "_have", fake_have({"pvporcupine", "openwakeword"}))
        engine, reason = create_wake_engine(VoiceSettings(), paths, None, FakeSTT(), load=True)
        assert isinstance(engine, TranscriptWakeEngine) and engine.ready
        assert "fallback because openwakeword: openwakeword not installed" in reason
        assert engine.reason == reason

    def test_factory_load_failure_falls_back(self, paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wake_mod, "_have", fake_have({"pvporcupine"}))

        def blocked(self: Any) -> None:
            raise WakeWordError("download blocked")

        monkeypatch.setattr(wake_mod.OpenWakeWordEngine, "load", blocked)
        engine, reason = create_wake_engine(VoiceSettings(), paths, None, FakeSTT(), load=True)
        assert isinstance(engine, TranscriptWakeEngine) and "openwakeword: download blocked" in reason

    def test_factory_porcupine_requested_without_key(self, paths: Any, memory_secrets: SecretStore,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wake_mod, "_have", fake_have(set()))
        engine, reason = create_wake_engine(VoiceSettings(wake_word_engine="porcupine"), paths, memory_secrets, None)
        assert engine.name == "openwakeword" and "porcupine: PICOVOICE_ACCESS_KEY is not set" in reason
        engine.close()

    def test_factory_nothing_available(self, paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wake_mod, "_have", fake_have({"pvporcupine", "openwakeword", "onnxruntime"}))
        engine, reason = create_wake_engine(VoiceSettings(), paths, None, None)
        assert isinstance(engine, NullWakeEngine) and not engine.ready
        assert "Push-to-talk still works" in reason and "transcript: no speech recognition engine" in reason

    def test_download_file_checks_status_and_size(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        httpx = pytest.importorskip("httpx")

        class Response:
            def __init__(self, status: int, body: bytes) -> None:
                self.status, self.body = status, body

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def raise_for_status(self) -> None:
                if self.status >= 400:
                    request = httpx.Request("GET", "https://example.invalid/m.onnx")
                    raise httpx.HTTPStatusError("error", request=request, response=httpx.Response(self.status, request=request))

            def iter_bytes(self, size: int) -> Any:
                yield self.body

        replies = [Response(404, b"<html>blocked</html>"), Response(200, b"tiny"), Response(200, b"\x01" * 4096)]
        monkeypatch.setattr(httpx, "stream", lambda *a, **k: replies.pop(0))
        dest = tmp_path / "models" / "m.onnx"
        with pytest.raises(WakeWordError, match="Could not download"):
            wake_mod.download_file("https://example.invalid/m.onnx", dest)
        with pytest.raises(WakeWordError, match="too small"):
            wake_mod.download_file("https://example.invalid/m.onnx", dest)
        assert not dest.exists() and not list(dest.parent.glob("*.part"))
        wake_mod.download_file("https://example.invalid/m.onnx", dest)
        assert dest.read_bytes() == b"\x01" * 4096

    @pytest.mark.skipif(not _have("openwakeword"), reason="openwakeword not installed")
    def test_openwakeword_files_and_missing_models(self, tmp_path: Path) -> None:
        files = wake_mod.openwakeword_files("hey_jarvis")
        assert set(files) == {"model", "melspectrogram", "embedding"}
        assert all(name.endswith(".onnx") and url.startswith("https://") for name, url in files.values())
        try:
            wake_mod.ensure_openwakeword_models("hey_jarvis", tmp_path, download=False)
        except WakeWordError as exc:
            assert "missing" in str(exc)
        else:  # pragma: no cover - the package ships/has downloaded the models
            pytest.skip("models are present in the openwakeword package")


# =============================================================================================
# Speech to text
# =============================================================================================
class TestSTT:
    def test_noise_transcripts(self) -> None:
        assert stt_mod.is_noise_transcript("")
        assert stt_mod.is_noise_transcript(" ... ")
        assert stt_mod.is_noise_transcript("Thank you.", 0.3)
        assert stt_mod.is_noise_transcript("תודה רבה", None)
        assert not stt_mod.is_noise_transcript("open chrome")
        assert not stt_mod.is_noise_transcript("פתח את כרום")
        assert stt_mod.echoes_prompt("JARVIS, Chrome, YouTube.", stt_mod.PROMPTS["en"])
        assert not stt_mod.echoes_prompt("open chrome", stt_mod.PROMPTS["en"])

    def test_factory_choices(self, paths: Any, memory_secrets: SecretStore, monkeypatch: pytest.MonkeyPatch) -> None:
        engine, reason = stt_mod.create_stt_engine(VoiceSettings(stt_engine="none"), paths, memory_secrets)
        assert isinstance(engine, NullSTT) and "turned off" in reason
        monkeypatch.setattr(stt_mod, "_have", fake_have(set()))
        engine, reason = stt_mod.create_stt_engine(VoiceSettings(), paths, memory_secrets)
        assert isinstance(engine, FasterWhisperSTT) and "no cloud backup" in reason
        assert engine.download_root == paths.models / "whisper" and engine.model_size == "small"
        memory_secrets.set("OPENAI_API_KEY", "sk-test-not-real-0123456789", persist=False)
        engine, reason = stt_mod.create_stt_engine(VoiceSettings(), paths, memory_secrets)      # paid AI off
        assert isinstance(engine, FasterWhisperSTT) and "paid AI is off" in reason
        engine, reason = stt_mod.create_stt_engine(VoiceSettings(stt_engine="openai"), paths, memory_secrets)
        assert isinstance(engine, FasterWhisperSTT) and "OpenAI unavailable: paid AI is off" in reason
        engine, reason = stt_mod.create_stt_engine(VoiceSettings(), paths, memory_secrets, paid=True)
        assert isinstance(engine, FallbackSTT) and "OpenAI as backup" in reason
        monkeypatch.setattr(stt_mod, "_have", fake_have({"faster_whisper"}))
        engine, reason = stt_mod.create_stt_engine(VoiceSettings(), paths, memory_secrets, paid=True)
        assert isinstance(engine, OpenAISTT) and "faster-whisper is not installed" in reason
        engine, reason = stt_mod.create_stt_engine(VoiceSettings(), paths, SecretStore(None, use_keyring=False, environ={}),
                                                   paid=True)
        assert isinstance(engine, NullSTT) and "no OPENAI_API_KEY" in reason

    @needs_numpy
    async def test_fallback_stt_uses_backup(self) -> None:
        engine = FallbackSTT(FailingSTT(), FakeSTT(["open chrome"]))
        result = await engine.transcribe(silence(1.0))
        assert result.text == "open chrome" and engine.name == "fake_stt"
        assert "broken_stt failed" in engine.reason

    @needs_numpy
    async def test_openai_stt_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("openai")
        sent: list[dict[str, Any]] = []

        class ModelMissing(Exception):
            status_code = 404

        class Transcriptions:
            async def create(self, **kwargs: Any) -> Any:
                sent.append(kwargs)
                if kwargs["model"] == "gpt-4o-mini-transcribe":
                    raise ModelMissing("no such model")
                return type("R", (), {"text": "פתח את כרום"})()

        client = type("Client", (), {"audio": type("A", (), {"transcriptions": Transcriptions()})()})()
        engine = OpenAISTT("sk-test-not-real")
        monkeypatch.setattr(engine, "_get_client", lambda: client)
        result = await engine.transcribe(speechlike(0.5), "he")
        assert result.text == "פתח את כרום" and result.language == "he"
        assert [s["model"] for s in sent] == ["gpt-4o-mini-transcribe", "whisper-1"]
        _name, data, mime = sent[-1]["file"]
        assert data[:4] == b"RIFF" and mime == "audio/wav" and sent[-1]["language"] == "he" and sent[-1]["prompt"]
        assert (await engine.transcribe(speechlike(0.5), partial=True)).text == ""  # no paid partials

    @pytest.mark.skipif(not _have("faster_whisper"), reason="faster-whisper not installed")
    def test_faster_whisper_missing_model_without_download(self, tmp_path: Path) -> None:
        engine = FasterWhisperSTT("tiny", device="cpu", download_root=tmp_path, allow_download=False)
        t0 = time.monotonic()
        with pytest.raises(STTUnavailable):
            engine.load()
        assert time.monotonic() - t0 < 30 and not engine.ready
        with pytest.raises(STTUnavailable):  # remembered: no retry storm
            engine.load()
        engine.close()


class FakeWhisperWorld:
    """A stand-in for faster-whisper and the computer under it: which models are on disk, whether downloads work,
    and which devices fail (`broken`: device -> the exception its next inference raises)."""

    class LocalEntryNotFoundError(FileNotFoundError):  # what huggingface_hub raises for a model not in the cache
        pass

    def __init__(self, cached: set[str], *, cuda_devices: int = 1, downloadable: bool = True) -> None:
        self.cached = set(cached)
        self.cuda_devices = cuda_devices
        self.downloadable = downloadable
        self.broken: dict[str, type[Exception]] = {}
        self.loads: list[tuple[str, str, bool]] = []       # (model, device, from the cache only)
        self.runs: list[tuple[str, str]] = []              # (device, language or "detect")
        self.card_gate = threading.Event()                 # a model being loaded on the card waits for this
        self.card_gate.set()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        world = self

        class WhisperModel:
            def __init__(self, name: str, *, device: str, compute_type: str, cpu_threads: int,
                         download_root: str | None, local_files_only: bool) -> None:
                world.loads.append((name, device, local_files_only))
                if name not in world.cached:
                    if local_files_only:
                        raise FakeWhisperWorld.LocalEntryNotFoundError("Cannot find an appropriate cached snapshot")
                    if not world.downloadable:
                        raise ConnectionError("no network")
                    world.cached.add(name)
                if device == "cuda":
                    world.card_gate.wait(10)
                self.name, self.device, self.compute_type = name, device, compute_type
                self.model = type("CT2", (), {"is_multilingual": True})()

            def detect_language(self, audio: Any = None) -> tuple[str, float, list[tuple[str, float]]]:
                world.runs.append((self.device, "detect"))
                return "he", 0.99, [("he", 0.99)]

            def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, Any]:
                world.runs.append((self.device, kwargs.get("language") or "auto"))
                if self.device in world.broken:
                    raise world.broken[self.device]("CUDA failed with error out of memory")
                segment = type("Seg", (), {"text": f" {self.name} on {self.device}", "tokens": [1, 2, 3],
                                           "avg_logprob": -0.1, "no_speech_prob": 0.01})()
                return iter([segment]), None

        fw, ct2 = type(sys)("faster_whisper"), type(sys)("ctranslate2")
        fw.WhisperModel = WhisperModel
        ct2.get_cuda_device_count = lambda: world.cuda_devices
        monkeypatch.setitem(sys.modules, "faster_whisper", fw)
        monkeypatch.setitem(sys.modules, "ctranslate2", ct2)
        monkeypatch.setattr(stt_mod, "cuda_libraries_problem", lambda: None)

    def engine(self, tmp_path: Path, **kwargs: Any) -> FasterWhisperSTT:
        return FasterWhisperSTT("small", gpu_model="large-v3-turbo", download_root=tmp_path, **kwargs)


@needs_numpy
class TestWhisperOnTheGraphicsCard:
    async def test_the_cpu_serves_until_the_card_model_takes_over(self, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        world = FakeWhisperWorld({"small", "large-v3-turbo"})
        world.install(monkeypatch)
        world.card_gate.clear()                            # the card takes its time (its first warm-up: a minute)
        engine = world.engine(tmp_path)
        engine.load()
        assert (engine.model_used, engine.device_used, engine.compute_type_used) == ("small", "cpu", "int8")
        assert not engine.settle(0.05)
        assert (await engine.transcribe(speechlike(0.5), "he")).text == "small on cpu"
        world.card_gate.set()
        assert engine.settle(5)
        assert (engine.model_used, engine.device_used, engine.compute_type_used) == ("large-v3-turbo", "cuda",
                                                                                     "int8_float16")
        assert engine.describe() == "faster-whisper 'large-v3-turbo' on cuda (int8_float16)" and engine.gpu_problem is None
        # The card's warm-up also goes through a real command's steps, so the first command is as quick as the next.
        assert [run for run in world.runs if run[0] == "cuda"] == [("cuda", "en"), ("cuda", "detect"), ("cuda", "he")]
        assert (await engine.transcribe(speechlike(0.5), "he")).text == "large-v3-turbo on cuda"
        assert world.loads == [("small", "cpu", True), ("large-v3-turbo", "cuda", True)]   # nothing downloaded
        engine.close()

    def test_a_missing_card_model_is_downloaded_in_the_background(self, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        world = FakeWhisperWorld({"small"})
        world.install(monkeypatch)
        engine = world.engine(tmp_path)
        engine.load()
        assert engine.settle(5) and engine.model_used == "large-v3-turbo" and engine.device_used == "cuda"
        assert world.loads == [("small", "cpu", True), ("large-v3-turbo", "cuda", True),
                               ("large-v3-turbo", "cuda", False)]
        engine.close()

        quiet = FakeWhisperWorld({"small"})                # downloads off: the CPU model stays
        quiet.install(monkeypatch)
        engine = quiet.engine(tmp_path, allow_download=False)
        engine.load()
        assert engine.settle(5) and engine.model_used == "small" and engine.device_used == "cpu"
        assert quiet.loads == [("small", "cpu", True), ("large-v3-turbo", "cuda", True)]
        engine.close()

    def test_first_run_downloads_the_cpu_model_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        world = FakeWhisperWorld(set())
        world.install(monkeypatch)
        world.card_gate.clear()
        engine = world.engine(tmp_path)
        engine.load()
        assert engine.model_used == "small"                # speech works after the smaller download
        world.card_gate.set()
        assert engine.settle(5) and engine.model_used == "large-v3-turbo"
        assert world.loads == [("small", "cpu", True), ("large-v3-turbo", "cuda", True), ("small", "cpu", False),
                               ("large-v3-turbo", "cuda", True), ("large-v3-turbo", "cuda", False)]
        engine.close()

        only_card = FakeWhisperWorld({"large-v3-turbo"})   # only the card's model on disk: no download at all
        only_card.install(monkeypatch)
        engine = only_card.engine(tmp_path)
        engine.load()
        assert engine.model_used == "large-v3-turbo" and engine.device_used == "cuda" and engine.settle(0)
        assert only_card.loads == [("small", "cpu", True), ("large-v3-turbo", "cuda", True)]
        engine.close()

    def test_a_card_that_fails_leaves_the_cpu(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        world = FakeWhisperWorld({"small", "large-v3-turbo"})
        world.broken["cuda"] = RuntimeError
        world.install(monkeypatch)
        engine = world.engine(tmp_path)
        engine.load()
        assert engine.settle(5) and engine.model_used == "small" and engine.device_used == "cpu"
        assert engine.gpu_problem and "out of memory" in engine.gpu_problem
        engine.close()

        no_cpu_model = FakeWhisperWorld({"large-v3-turbo"})  # the CPU model is downloaded; the card is not tried again
        no_cpu_model.broken["cuda"] = RuntimeError
        no_cpu_model.install(monkeypatch)
        engine = no_cpu_model.engine(tmp_path)
        engine.load()
        assert engine.settle(0) and engine.model_used == "small" and engine.device_used == "cpu"
        assert no_cpu_model.loads == [("small", "cpu", True), ("large-v3-turbo", "cuda", True), ("small", "cpu", False)]
        engine.close()

    def test_missing_cuda_libraries_or_no_card(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        world = FakeWhisperWorld({"small", "large-v3-turbo"})
        world.install(monkeypatch)
        monkeypatch.setattr(stt_mod, "cuda_libraries_problem", lambda: "cublas64_12.dll not found (install them ...)")
        engine = world.engine(tmp_path)
        engine.load()
        assert engine.settle(0) and engine.device_used == "cpu" and "cublas64_12.dll" in (engine.gpu_problem or "")
        assert world.loads == [("small", "cpu", True)]
        engine.close()

        no_card = FakeWhisperWorld({"small", "large-v3-turbo"}, cuda_devices=0)
        no_card.install(monkeypatch)
        engine = no_card.engine(tmp_path)
        engine.load()
        assert engine.settle(0) and engine.device_used == "cpu" and engine.gpu_problem is None
        assert no_card.loads == [("small", "cpu", True)]
        engine.close()

        cpu_only = FakeWhisperWorld({"small", "large-v3-turbo"})  # the settings say CPU
        cpu_only.install(monkeypatch)
        engine = cpu_only.engine(tmp_path, device="cpu")
        engine.load()
        assert engine.settle(0) and cpu_only.loads == [("small", "cpu", True)]
        engine.close()

    async def test_a_card_failure_mid_session_hands_over_to_the_cpu(self, tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
        world = FakeWhisperWorld({"small", "large-v3-turbo"})
        world.install(monkeypatch)
        engine = world.engine(tmp_path)
        engine.load()
        assert engine.settle(5) and engine.device_used == "cuda"
        world.broken["cuda"] = ValueError                  # not a CUDA failure: raised as it is
        with pytest.raises(ValueError):
            await engine.transcribe(speechlike(0.5), "he")
        assert engine.device_used == "cuda"
        world.broken["cuda"] = RuntimeError                # a game filled the card
        assert (await engine.transcribe(speechlike(0.5), "he")).text == "small on cpu"
        assert engine.device_used == "cpu" and "out of memory" in (engine.gpu_problem or "")
        assert (await engine.transcribe(speechlike(0.5), "he")).text == "small on cpu"
        engine.close()

    def test_closed_while_the_card_model_loads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        world = FakeWhisperWorld({"small", "large-v3-turbo"})
        world.install(monkeypatch)
        world.card_gate.clear()
        engine = world.engine(tmp_path)
        engine.load()
        engine.close()
        world.card_gate.set()
        assert engine.settle(5) and not engine.ready and engine.model_used == "small"

    def test_cut_off_downloads_count_as_missing(self) -> None:
        assert stt_mod._not_downloaded(FakeWhisperWorld.LocalEntryNotFoundError("not cached"))
        assert stt_mod._not_downloaded(RuntimeError("Unable to open file 'model.bin' in model 'C:/models/x'"))
        assert not stt_mod._not_downloaded(RuntimeError("CUDA failed with error out of memory"))

    def test_nvidia_libraries_on_windows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "site-packages" / "nvidia"
        for sub in ("cublas/bin", "cudnn/bin", "cuda_nvrtc/lib"):
            (root / sub).mkdir(parents=True)
        (root / "cublas" / "bin" / "cublas64_12.dll").write_bytes(b"")
        real_find_spec = stt_mod.importlib.util.find_spec

        def find_spec(name: str, *args: Any) -> Any:
            if name == "nvidia":
                return type("Spec", (), {"submodule_search_locations": [str(root)]})()
            return real_find_spec(name, *args)

        added: list[str] = []
        windows = type(sys)("sys")
        windows.platform = "win32"
        monkeypatch.setattr(stt_mod.importlib.util, "find_spec", find_spec)
        monkeypatch.setattr(stt_mod, "sys", windows)
        monkeypatch.setattr(stt_mod.os, "add_dll_directory", added.append, raising=False)
        monkeypatch.setattr(stt_mod, "_dll_dirs_added", set())
        monkeypatch.setattr(stt_mod, "_dll_handles", [])
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("CUDA_CACHE_MAXSIZE", "")
        monkeypatch.delenv("CUDA_CACHE_MAXSIZE")
        bins = [str(root / "cublas" / "bin"), str(root / "cudnn" / "bin")]
        assert [str(d) for d in stt_mod.use_nvidia_dlls()] == bins
        assert os.environ["PATH"].split(os.pathsep) == [*bins, "/usr/bin"] and added == bins
        assert os.environ["CUDA_CACHE_MAXSIZE"] == str(4 * 1024**3)
        stt_mod.use_nvidia_dlls()                          # once is enough
        assert os.environ["PATH"].split(os.pathsep) == [*bins, "/usr/bin"] and added == bins
        problem = stt_mod.cuda_libraries_problem()
        assert problem and problem.startswith("cudnn64_9.dll not found") and "jarvis-assistant[gpu]" in problem
        (root / "cudnn" / "bin" / "cudnn64_9.dll").write_bytes(b"")
        assert stt_mod.cuda_libraries_problem() is None

    def test_settings_and_check_note(self, paths: Any, memory_secrets: SecretStore,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(stt_mod, "_have", fake_have(set()))
        engine, _ = stt_mod.create_stt_engine(VoiceSettings(), paths, memory_secrets)
        assert isinstance(engine, FasterWhisperSTT)
        assert (engine.gpu_model, engine.gpu_compute_type) == ("large-v3-turbo", "int8_float16")
        engine, _ = stt_mod.create_stt_engine(VoiceSettings(stt_model_gpu=""), paths, memory_secrets)
        assert isinstance(engine, FasterWhisperSTT) and engine.gpu_model == "small"
        world = FakeWhisperWorld(set())
        world.install(monkeypatch)
        assert stt_mod.gpu_note("auto", "large-v3-turbo") == "speech recognition runs on the graphics card ('large-v3-turbo')"
        assert stt_mod.gpu_note("cpu", "large-v3-turbo") is None
        monkeypatch.setattr(stt_mod, "cuda_libraries_problem", lambda: "cudnn64_9.dll not found")
        assert stt_mod.gpu_note("auto", "large-v3-turbo") == "speech recognition stays on the CPU: cudnn64_9.dll not found"
        world.cuda_devices = 0
        assert stt_mod.gpu_note("auto", "large-v3-turbo") is None

    def test_fallback_describes_what_is_in_use(self) -> None:
        primary, backup = FakeSTT(["a"]), FakeSTT(["b"])
        primary.reason, backup.reason = "faster-whisper 'large-v3-turbo' on cuda (int8_float16)", "OpenAI"
        engine = FallbackSTT(primary, backup)
        engine.load()
        assert engine.describe() == primary.reason and engine.settle(0)
        primary.reason = "faster-whisper 'small' on cpu (int8)"   # the card failed since
        assert engine.describe() == "faster-whisper 'small' on cpu (int8)"


# =============================================================================================
# Text to speech and the phrase cache
# =============================================================================================
class TestTTS:
    def test_prepare_speech_text(self) -> None:
        text = prepare_speech_text("**Done!** See [the docs](https://x.example/a) or https://example.com ```print(1)``` 🎉 ok")
        assert "*" not in text and "http" not in text and "print" not in text and "🎉" not in text
        assert "Done!" in text and "the docs" in text and text.endswith("ok.")
        # list lines become sentences, so the voice pauses between items and they are synthesized apart
        listed = prepare_speech_text("Steps:\n- open the folder\n- run the tests")
        assert listed == "Steps: open the folder. run the tests."
        assert split_sentences(listed, max_chars=30) == ["Steps: open the folder.", "run the tests."]
        assert split_sentences(listed) == [listed]  # short sentences are synthesized together

    def test_split_sentences(self) -> None:
        text = " ".join(f"Sentence number {i} is here." for i in range(30))
        chunks = split_sentences(text, max_chars=120)
        assert all(len(c) <= 120 for c in chunks) and len(chunks) > 3
        assert " ".join(chunks).split() == text.split()

    def test_resolve_language(self) -> None:
        assert resolve_language("פתח את כרום") == "he"
        assert resolve_language("open chrome") == "en"
        assert resolve_language("open chrome", "he") == "he"

    def test_factory_choices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine, reason = tts_mod.create_tts_engine(VoiceSettings(tts_engine="none"))
        assert isinstance(engine, NullTTS) and not engine.available
        monkeypatch.setattr(tts_mod, "_have", fake_have(set()))
        engine, reason = tts_mod.create_tts_engine(VoiceSettings(tts_rate="+5%"))
        offline = ["onecore", "sapi"] if sys.platform == "win32" else ["sapi"]
        assert isinstance(engine, FallbackTTS) and [e.name for e in engine.engines] == ["edge", *offline]
        assert engine.cache_id("he") == "edge|he-IL-AvriNeural|+5%"
        monkeypatch.setattr(tts_mod, "_have", fake_have({"edge_tts"}))
        engine, reason = tts_mod.create_tts_engine(VoiceSettings())
        assert "edge-tts" in reason and (isinstance(engine, SapiTTS) if sys.platform != "win32" else
                                          [e.name for e in engine.engines] == offline)
        monkeypatch.setattr(tts_mod, "sys", type("FakeSys", (), {"platform": "linux"}))   # no Windows voices
        monkeypatch.setattr(tts_mod, "_have", fake_have({"edge_tts", "pyttsx3"}))
        engine, reason = tts_mod.create_tts_engine(VoiceSettings())
        assert isinstance(engine, NullTTS) and "No speech output engine" in reason

    @needs_numpy
    async def test_fallback_tts_and_cooldown(self) -> None:
        edge, sapi = FakeTTS("edge", fail=True), FakeTTS("sapi")
        engine = FallbackTTS([edge, sapi])
        clip = await engine.synthesize("hello", "en")
        assert clip.engine == "sapi" and engine.last_engine == "sapi" and "edge" in engine.last_errors[0]
        await engine.synthesize("again", "en")
        assert len(edge.calls) == 1  # skipped while cooling down
        assert engine.name == "edge" and engine.cache_ids("en") == ["edge|edge-en", "sapi|sapi-en"]

    @needs_numpy
    async def test_phrase_cache_warm_lookup_and_disk(self, tmp_path: Path) -> None:
        tts = FakeTTS()
        cache = PhraseCache(tts, tmp_path)
        stats = await cache.warm()
        expected = sum(len(v) for v in ACK_PHRASES.values())
        assert stats["synthesized"] == expected and stats["failed"] == 0 and len(tts.calls) == expected
        clip = cache.lookup("listening", "he")
        assert clip is not None and clip.text == "כן?" and cache.hits == 1
        assert await asyncio.to_thread(lambda: len(list(tmp_path.glob("*.wav")))) == expected
        again = PhraseCache(tts, tmp_path)  # a new run: from disk, nothing synthesized
        assert (await again.warm())["cached"] == expected and len(tts.calls) == expected
        assert again.lookup("ok", "en").text == "Okay."
        other_voice = PhraseCache(FakeTTS("other"), tmp_path)
        assert other_voice.lookup("ok", "en") is None and other_voice.misses == 1

    @needs_numpy
    async def test_phrase_cache_uses_fallback_copies_when_primary_is_down(self, tmp_path: Path) -> None:
        first = FallbackTTS([FakeTTS("edge", fail=True), FakeTTS("sapi")])
        stats = await PhraseCache(first, tmp_path).warm()
        assert stats["fallback"] and stats["failed"] == 0
        sapi = FakeTTS("sapi")
        offline = FallbackTTS([FakeTTS("edge", fail=True), sapi])
        stats = await PhraseCache(offline, tmp_path).warm()
        expected = sum(len(v) for v in ACK_PHRASES.values())
        assert len(sapi.calls) == 1 and stats["synthesized"] == 1 and stats["cached"] == expected - 1
        assert PhraseCache(offline, tmp_path).lookup("listening", "he").engine == "sapi"

    @needs_numpy
    async def test_windows_voices_through_a_runner(self) -> None:
        import numpy as np

        calls: list[tuple[str, dict[str, str]]] = []

        async def runner(script: str, env: dict[str, str], timeout: float) -> tuple[int, str, str]:
            calls.append((script, env))
            if script == tts_mod.ONECORE_VOICES_PS:        # PowerShell prints one voice as an object, not a list
                return 0, json.dumps({"id": "TOKEN_ASAF", "name": "Microsoft Asaf", "language": "he-IL"}), ""
            if env["JARVIS_TTS_TEXT"] == "boom":
                return 1, "", "SynthesizeTextToStreamAsync failed"
            tts_mod.write_wav(env["JARVIS_TTS_OUT"], np.full(8000, 500, dtype=np.int16), 16000)
            return 0, "", ""

        engine = OneCoreTTS(rate="+50%", runner=runner)
        await engine.prepare()
        assert engine.voices == {"he": ("TOKEN_ASAF", "Microsoft Asaf"), "en": None}
        clip = await engine.synthesize("שלום, אני ג'ארוויס", "he")
        assert (clip.engine, clip.voice, clip.language, round(clip.duration_s, 2)) == ("onecore", "Microsoft Asaf", "he", 0.5)
        speak = calls[-1][1]
        assert speak["JARVIS_TTS_VOICE"] == "TOKEN_ASAF" and speak["JARVIS_TTS_RATE"] == "1.50"
        assert speak["JARVIS_TTS_TEXT"] == "שלום, אני ג'ארוויס" and not os.path.exists(speak["JARVIS_TTS_OUT"])
        assert engine.cache_id("he") == "onecore|TOKEN_ASAF|+50%"
        with pytest.raises(tts_mod.TTSUnavailable, match="Add voices"):      # no English Windows voice
            await engine.synthesize("Hello", "en")
        with pytest.raises(TTSError, match="SynthesizeTextToStreamAsync"):
            await engine.synthesize("boom", "he")
        assert sum(1 for s, _ in calls if s == tts_mod.ONECORE_VOICES_PS) == 1   # listed once

    def test_pick_windows_voice(self) -> None:
        voices = [{"id": "a", "name": "Hazel", "language": "en-GB"}, {"id": "b", "name": "Zira", "language": "en-US"},
                  {"id": "c", "name": "Asaf", "language": "he-IL"}]
        assert tts_mod.pick_onecore_voice(voices, "en") == ("b", "Zira")
        assert tts_mod.pick_onecore_voice(voices, "he") == ("c", "Asaf")
        assert tts_mod.pick_onecore_voice(voices[:1], "he") is None
        assert OneCoreTTS(rate="-80%").speaking_rate == 0.5 and OneCoreTTS(rate="+900%").speaking_rate == 6.0

    @needs_numpy
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows voices")
    async def test_windows_voices_speak(self) -> None:
        engine = OneCoreTTS()
        await engine.prepare()
        if engine.init_error:
            pytest.skip(engine.init_error)
        spoken = 0
        for lang, text in (("en", "Hello, this is JARVIS."), ("he", "שלום, אני ג'ארוויס.")):
            if engine.voices.get(lang) is None:
                with pytest.raises(tts_mod.TTSUnavailable):
                    await engine.synthesize(text, lang)
                continue
            clip = await engine.synthesize(text, lang)
            assert clip.duration_s > 0.4 and clip.engine == "onecore" and clip.language == lang
            spoken += 1
        if not spoken:
            pytest.skip("no Windows voice for English or Hebrew is installed")

    @needs_numpy
    @pytest.mark.skipif(not _have("pyttsx3") or (sys.platform.startswith("linux") and ESPEAK is None),
                        reason="pyttsx3 / espeak-ng not available")
    async def test_offline_voice_synthesizes(self) -> None:
        engine = SapiTTS(rate="+0%")
        await engine.prepare()
        if engine.init_error:
            pytest.skip(engine.init_error)
        try:
            clip = await engine.synthesize("Hello, this is JARVIS.", "en")
            assert clip.duration_s > 0.4 and clip.engine == "sapi"
            assert engine.cache_id("en").startswith("sapi|")
            try:
                hebrew = await engine.synthesize("שלום, אני ג'ארוויס", "he")
            except tts_mod.TTSUnavailable as exc:  # no Hebrew voice installed (common on Windows)
                assert "Hebrew" in str(exc)
            else:
                assert hebrew.duration_s > 0.3 and hebrew.language == "he"
        finally:
            await engine.close()

    @needs_numpy
    @pytest.mark.network
    @pytest.mark.skipif(not (_have("edge_tts") and _have("miniaudio")), reason="edge-tts/miniaudio not installed")
    async def test_edge_tts_neural_voice(self) -> None:
        engine = tts_mod.EdgeTTS(timeout_s=20.0)
        try:
            clip = await engine.synthesize("שלום, אני ג'ארוויס.", "he")
        except TTSError as exc:
            pytest.skip(f"edge-tts service unreachable: {exc}")
        assert clip.duration_s > 0.5 and clip.voice == "he-IL-AvriNeural" and clip.sample_rate == 24_000


# =============================================================================================
# Pipeline state machine
# =============================================================================================
class TestPipelineHelpers:
    def test_coerce_reply(self) -> None:
        assert coerce_reply(None) is None
        assert coerce_reply("  ") is None
        assert coerce_reply("Done.") == SpeechReply(text="Done.")
        reply = SpeechReply(ack_key="ok", follow_up=False)
        assert coerce_reply(reply) is reply
        with pytest.raises(TypeError):
            coerce_reply(42)  # type: ignore[arg-type]

    def test_strip_echo(self) -> None:
        assert strip_echo("כן? פתח את כרום", "כן?") == "פתח את כרום"
        assert strip_echo("Yes, open Chrome", "Yes?") == "open Chrome"
        assert strip_echo("yes", "Yes?") == "yes"  # only the echo: keep (it may be the answer)
        assert strip_echo("open chrome", "Yes?") == "open chrome"

    def test_options_from_settings(self) -> None:
        settings = VoiceSettings(follow_up_window_s=3.0, end_silence_ms=500, speak_responses=False, stt_language="en",
                                 always_listening=False)
        o = PipelineOptions.from_settings(settings, max_retries=0)
        assert (o.follow_up_window_s, o.end_silence_ms, o.speak_responses, o.always_listening) == (3.0, 500, False, False)
        assert o.default_language == "en" and o.stt_language == "en" and o.max_retries == 0


@needs_numpy
class TestPipeline:
    async def test_full_turn(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["open chrome"], reply=SpeechReply(text="Opening Chrome now.", ack_key="on_it")) as h:
            h.source.append_silence(0.5)
            h.say(1.2)
            await wait_for(lambda: h.handled and h.quiet, what="turn to finish")
            await h.pipeline.flush_events()
        assert h.events.transitions() == ["idle", "wake", "listening", "processing", "speaking", "listening", "idle"]
        triggers = [e[1:3] for e in h.events.events if e[0] == "state"]
        assert ("listening", "wake") in triggers and ("listening", "follow_up") in triggers
        assert h.events.of("pipeline") == [
            ("transcribing", "active", "מתמלל…"),
            ("transcribing", "done", "Transcribing…"),
            ("done", "done", "Done"),
        ]
        assert h.events.of("transcript") == [("open chrome", True, "en")]
        assert h.events.of("speech") == [("On it. Opening Chrome now.", "en")]
        assert h.handled[0].text == "open chrome" and h.handled[0].language == "en"
        assert h.played() == ["", "On it.", "Opening Chrome now."]  # chime (cache was cold), ack, reply
        assert h.wake.resets >= 1  # stale wake audio cleared when the command started
        assert h.pipeline.last_transcript.text == "open chrome"
        assert h.pipeline.stats["turns"] == 1 and h.pipeline.stats["wakes"] == 1
        timings = {name: ms for name, ms, _ok in h.events.of("timing")}
        assert {"voice.stt", "voice.reply", "voice.tts.fake_tts"} <= set(timings), timings
        assert 0 <= timings["voice.stt"] <= timings["voice.reply"]   # recognition, then the first sound of the reply

    async def test_follow_up_without_wake_word(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["open chrome", "and youtube"], reply="Sure.") as h:
            h.say(1.0)
            await wait_for(lambda: len(h.handled) == 1 and h.pipeline.state is VoiceState.LISTENING, what="follow-up")
            h.say(1.0, wake=False)
            await wait_for(lambda: len(h.handled) == 2 and h.quiet, what="second command")
        assert [t.text for t in h.handled] == ["open chrome", "and youtube"]
        assert h.pipeline.stats["wakes"] == 1 and h.pipeline.stats["turns"] == 2

    async def test_handler_none_goes_idle_without_follow_up(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["mute the music"], reply=None) as h:
            h.say(1.0)
            await wait_for(lambda: h.handled and h.quiet, what="turn")
            await h.pipeline.flush_events()
        assert h.events.transitions() == ["idle", "wake", "listening", "processing", "idle"]
        assert h.events.of("speech") == [] and h.played() == [""]

    async def test_reply_without_follow_up(self, tmp_path: Path) -> None:
        reply = SpeechReply(text="פותח את כרום", follow_up=False)
        async with Harness(tmp_path, texts=["פתח את כרום"], reply=reply) as h:
            h.say(1.0)
            await wait_for(lambda: h.handled and h.quiet, what="turn")
            await h.pipeline.flush_events()
        assert h.events.transitions()[-3:] == ["processing", "speaking", "idle"]
        assert h.events.of("speech") == [("פותח את כרום.", "he")]  # the text as spoken
        assert h.handled[0].language == "he"

    async def test_noise_after_wake_asks_again_then_rests(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=[""]) as h:
            h.say(1.0)
            await wait_for(lambda: len(h.stt.calls) == 1 and h.quiet, what="give up")
            await h.pipeline.flush_events()
        assert h.handled == []
        assert ACK_PHRASES["not_understood"]["he"] in h.played()
        triggers = [e[2] for e in h.events.events if e[0] == "state" and e[1] == "listening"]
        assert triggers == ["wake", "retry"]

    async def test_wake_word_alone_listens_again(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["Jarvis?", "open chrome"]) as h:
            h.say(0.6)
            await wait_for(lambda: len(h.stt.calls) == 1 and h.pipeline.state is VoiceState.LISTENING, what="re-listen")
            h.say(1.0, wake=False)
            await wait_for(lambda: h.handled and h.quiet, what="command")
        assert [t.text for t in h.handled] == ["open chrome"]

    async def test_no_speech_after_wake_times_out(self, tmp_path: Path) -> None:
        async with Harness(tmp_path) as h:
            h.source.append_frames([marker_frame()])
            await wait_for(lambda: h.pipeline.state is VoiceState.LISTENING, what="listening")
            await wait_for(lambda: h.quiet, what="timeout")
        assert h.stt.calls == [] and h.handled == []

    async def test_cached_acknowledgement_plays_instead_of_chime(self, tmp_path: Path) -> None:
        h = Harness(tmp_path, texts=["open chrome"], reply=None)
        await h.cache.warm()
        synthesized = len(h.tts.calls)
        async with h:
            h.say(1.0)
            await wait_for(lambda: h.handled and h.quiet, what="turn")
        assert h.played()[0] == "כן?"
        assert len(h.tts.calls) == synthesized  # nothing synthesized during the turn
        assert h.cache.hits >= 1

    async def test_barge_in_interrupts_speech(self, tmp_path: Path) -> None:
        long_reply = "This is a long answer that keeps going. " * 6
        async with Harness(tmp_path, texts=["tell me a story", "stop"], reply=long_reply, sink_speed=0.2) as h:
            h.say(1.0)
            await wait_for(lambda: h.pipeline.state is VoiceState.SPEAKING and h.sink.playing, what="speaking")
            h.say(0.8)  # "Hey JARVIS, stop" while JARVIS talks
            await wait_for(lambda: len(h.handled) == 2, what="second command")
            await wait_for(lambda: h.quiet, timeout=15, what="rest")
        assert h.pipeline.stats["barge_ins"] == 1
        assert h.sink.interrupted and h.sink.interrupted[0].text.startswith("This is a long answer")
        assert [t.text for t in h.handled] == ["tell me a story", "stop"]

    async def test_push_to_talk(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["what time is it"], reply="It is noon.") as h:
            h.pipeline.push_to_talk_start()
            assert h.pipeline.state is VoiceState.LISTENING
            h.source.append(speechlike(1.0))
            h.source.append_silence(1.2)  # long pause: push-to-talk does not end on silence
            await h.source.drained(5.0)
            assert h.pipeline.state is VoiceState.LISTENING and h.stt.calls == []
            h.pipeline.push_to_talk_stop()
            await wait_for(lambda: h.handled and h.quiet, what="ptt turn")
            await h.pipeline.flush_events()
        triggers = [e[2] for e in h.events.events if e[0] == "state" and e[1] == "listening"]
        assert triggers[0] == "ptt" and h.pipeline.stats["wakes"] == 0
        assert h.handled[0].text == "what time is it" and "" not in h.played()  # no chime for PTT

    async def test_push_to_talk_without_speech(self, tmp_path: Path) -> None:
        async with Harness(tmp_path) as h:
            h.pipeline.push_to_talk_start()
            h.source.append_silence(0.5)
            await h.source.drained(5.0)
            h.pipeline.push_to_talk_stop()
            assert h.quiet and h.stt.calls == []

    async def test_cancel_during_processing(self, tmp_path: Path) -> None:
        release = asyncio.Event()
        finished: list[str] = []

        async def slow_handler(transcript: Transcript) -> str:
            await release.wait()
            finished.append(transcript.text)
            return "This reply must not be spoken."

        async with Harness(tmp_path, texts=["build a website"], handler=slow_handler) as h:
            h.say(1.0)
            await wait_for(lambda: h.events.of("transcript"), what="transcript")
            assert h.pipeline.state is VoiceState.PROCESSING
            h.pipeline.cancel()
            assert h.pipeline.state is VoiceState.IDLE
            release.set()
            await wait_for(lambda: finished, what="handler to finish")  # the core's work is not cancelled
            await asyncio.sleep(0.1)
        assert "This reply must not be spoken." not in h.played()
        assert h.pipeline.state in (VoiceState.IDLE, VoiceState.DISABLED)

    async def test_mute_closes_microphone(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["open chrome"]) as h:
            await wait_for(lambda: h.source.running, what="microphone open")
            h.pipeline.mute()
            await wait_for(lambda: not h.source.running, what="microphone closed")
            h.say(1.0)  # ignored while muted
            await asyncio.sleep(0.2)
            assert h.handled == [] and h.source.pending > 0
            h.pipeline.unmute()
            await wait_for(lambda: h.handled and h.quiet, what="turn after unmute")
            await h.pipeline.flush_events()
        muted_flags = [e[3] for e in h.events.events if e[0] == "state"]
        assert True in muted_flags and muted_flags[-1] is False

    async def test_speak_announcement(self, tmp_path: Path) -> None:
        async with Harness(tmp_path) as h:
            assert await h.pipeline.speak("The task is finished.") is True
            await h.pipeline.flush_events()
            assert h.pipeline.state is VoiceState.IDLE
        assert h.events.of("speech") == [("The task is finished.", "en")]
        assert "The task is finished." in h.played()

    async def test_speak_interrupt_cuts_the_current_reply(self, tmp_path: Path) -> None:
        long_reply = "A long and winding explanation. " * 6
        async with Harness(tmp_path, texts=["explain"], reply=long_reply, sink_speed=0.2) as h:
            h.say(1.0)
            await wait_for(lambda: h.pipeline.state is VoiceState.SPEAKING and h.sink.playing, what="speaking")
            assert await h.pipeline.speak("עצרתי.", interrupt=True) is True
            await wait_for(lambda: h.quiet, what="rest")
        assert h.sink.interrupted and h.played()[-1] == "עצרתי."
        assert h.pipeline.state is VoiceState.IDLE  # no follow-up after the superseded reply

    async def test_the_reply_starts_while_it_is_still_being_written(self, tmp_path: Path) -> None:
        holder: dict[str, Harness] = {}

        async def writing(transcript: Transcript) -> SpeechReply:
            h, stream = holder["h"], transcript.stream
            assert stream is not None
            stream.update("The first sentence is here. And")             # the AI is still writing
            await wait_for(lambda: "The first sentence is here." in h.played(), what="the first sentence to sound")
            assert h.pipeline.state is VoiceState.SPEAKING
            stream.update("The first sentence is here. And the second one.", True)
            await asyncio.sleep(0.05)
            return SpeechReply(text="The first sentence is here. And the second one. Then a third.", ack_key="on_it",
                               follow_up=False)

        async with Harness(tmp_path, texts=["tell me something"], handler=writing) as h:
            holder["h"] = h
            h.say(1.0)
            await wait_for(lambda: h.pipeline.stats["turns"] and h.quiet, what="turn to finish")
            await h.pipeline.flush_events()
        played = h.played()
        assert played[played.index("The first sentence is here."):] == [
            "The first sentence is here.", "And the second one.", "Then a third."]       # each once, no "On it."
        assert "On it." not in played and h.pipeline.stats["early_replies"] == 1
        speech = [text for text, _lang in h.events.of("speech")]
        assert speech[0] == "The first sentence is here." and speech[-1].endswith("Then a third.")
        timings = {name for name, _ms, _ok in h.events.of("timing")}
        assert "voice.reply" in timings                                   # measured at the first sentence

    async def test_a_reply_that_was_not_written_early_is_spoken_as_before(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["open chrome"],
                           reply=SpeechReply(text="Opening Chrome.", ack_key="on_it", follow_up=False)) as h:
            h.say(1.0)
            await wait_for(lambda: h.pipeline.stats["turns"] and h.quiet, what="turn to finish")
        assert h.played()[-2:] == ["On it.", "Opening Chrome."] and h.pipeline.stats["early_replies"] == 0

    async def test_handler_error_flashes_error(self, tmp_path: Path) -> None:
        async def broken(transcript: Transcript) -> str:
            raise RuntimeError("core exploded")

        async with Harness(tmp_path, texts=["open chrome"], handler=broken) as h:
            h.say(1.0)
            await wait_for(lambda: h.pipeline.state is VoiceState.ERROR, what="error state")
            assert "core exploded" in (h.pipeline.last_error or "")
            await wait_for(lambda: h.pipeline.state is VoiceState.IDLE, what="back to idle")
            await h.pipeline.flush_events()
        assert ("done", "error", "Error") in h.events.of("pipeline")
        assert "Something went wrong." in h.played()
        assert next(e for e in h.events.of("state") if e[0] == "error")[3].startswith("Command failed")

    async def test_stt_failure_flashes_error(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, stt=FakeSTT(error=STTError("model missing"))) as h:
            h.say(1.0)
            await wait_for(lambda: h.pipeline.state is VoiceState.ERROR, what="error")
            await wait_for(lambda: h.pipeline.state is VoiceState.IDLE, what="idle")
            await h.pipeline.flush_events()
        assert ("transcribing", "error", "שגיאה") in h.events.of("pipeline")
        assert h.handled == []

    async def test_inline_command_from_transcript_wake(self, tmp_path: Path) -> None:
        stt = FakeSTT(["ג'ארוויס פתח את כרום"])
        wake = TranscriptWakeEngine(stt)
        wake.load()
        async with Harness(tmp_path, stt=stt, wake=wake, reply=SpeechReply(text="פותח.", follow_up=False)) as h:
            h.source.append(speechlike(1.2))
            await wait_for(lambda: h.handled and h.quiet, what="inline command")
            await h.pipeline.flush_events()
        assert h.handled[0].text == "פתח את כרום" and h.handled[0].language == "he"
        assert h.events.transitions() == ["idle", "wake", "processing", "speaking", "idle"]
        assert h.played() == ["פותח."]  # no "listening" acknowledgement: the command was already said

    async def test_the_name_and_a_command_wake_the_pipeline(self, tmp_path: Path) -> None:
        stt = HeardSTT(["ג'רביס, מה השעה?"])
        wake = NameWakeEngine(MarkerWake(), stt, language="he")
        async with Harness(tmp_path, stt=stt, wake=wake, reply=SpeechReply(text="עשר.", follow_up=False)) as h:
            h.source.append(speechlike(1.2))
            await wait_for(lambda: h.handled and h.quiet, what="the command said with the name")
            await h.pipeline.flush_events()
        assert h.handled[0].text == "מה השעה?" and h.handled[0].language == "he"
        assert h.events.transitions() == ["idle", "wake", "processing", "speaking", "idle"]
        assert h.played() == ["עשר."]
        assert len(stt.calls) == 1                                       # nothing checked while it answered

    async def test_speak_responses_off(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["open chrome"], options=fast_options(speak_responses=False)) as h:
            h.say(1.0)
            await wait_for(lambda: h.handled and h.quiet, what="turn")
            assert await h.pipeline.speak("hello") is False
        assert h.sink.played == [] and h.events.of("speech") == []

    async def test_level_events_are_throttled(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for _ in range(200):
            h.pipeline._emit_level(0.5)
        await asyncio.sleep(0.08)
        h.pipeline._emit_level(0.4)
        await h.pipeline.flush_events()
        assert h.events.of("level") == [(0.5,), (0.4,)]

    async def test_missing_microphone_disables_but_speech_works(self, tmp_path: Path) -> None:
        source = BrokenSource(missing=True)
        async with Harness(tmp_path, source=source) as h:
            await wait_for(lambda: h.pipeline.state is VoiceState.DISABLED, what="disabled")
            assert h.pipeline.disabled_reason == "No microphone found" and h.pipeline.input_problem
            assert await asyncio.wait_for(h.pipeline.speak("I can still talk."), 5.0) is True
            await wait_for(lambda: h.pipeline.state is VoiceState.DISABLED, what="disabled again")
            await h.pipeline.flush_events()
        assert ("disabled", None, False, "No microphone found") in h.events.of("state")

    async def test_busy_microphone_is_an_error_and_does_not_block_speech(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, source=BrokenSource(missing=False)) as h:
            await wait_for(lambda: h.pipeline.state is VoiceState.ERROR, what="error")
            assert await asyncio.wait_for(h.pipeline.speak("Still here."), 5.0) is True


# =============================================================================================
# Ctrl to talk
# =============================================================================================
class ScriptedKeys:
    """A keyboard read from a script: each poll takes the next (ctrl, others, tick, cursor); then nothing is down."""

    def __init__(self, polls: list[tuple[bool, bool, int, tuple[int, int]]], *, failures: int = 0) -> None:
        self.polls = list(polls)
        self.failures = failures
        self.now: tuple[bool, bool, int, tuple[int, int]] = (False, False, 0, (0, 0))
        self.finished = threading.Event()

    def ctrl_down(self) -> bool:
        if self.failures:
            self.failures -= 1
            raise OSError("access denied")
        if self.polls:
            self.now = self.polls.pop(0)
        else:
            self.now = (False, False, self.now[2], self.now[3])
            self.finished.set()
        return self.now[0]

    def others_down(self) -> bool:
        return self.now[1]

    def last_input(self) -> int:
        return self.now[2]

    def cursor(self) -> tuple[int, int]:
        return self.now[3]


class TestCtrlTap:
    """Which presses of Ctrl count as "Ctrl on its own" (the poll-by-poll decision, without Windows)."""

    @staticmethod
    def _press(detector: TapDetector, polls: list[tuple[float, bool, bool, int, tuple[int, int]]],
               *, own: bool = False) -> bool:
        results = [detector.feed(t, ctrl, others, tick, cursor, own) for t, ctrl, others, tick, cursor in polls]
        assert not any(results[:-1])
        return results[-1]

    def _detector(self) -> TapDetector:
        detector = TapDetector()
        assert detector.feed(0.0, False) is False      # Ctrl was up when watching started
        return detector

    def test_a_short_press_alone_counts(self) -> None:
        d = self._detector()
        assert self._press(d, [(1.00, True, False, 5, (10, 10)), (1.04, True, False, 5, (10, 10)),
                               (1.12, False, False, 6, (10, 10))]) is True

    def test_a_shortcut_does_not(self) -> None:
        d = self._detector()      # Ctrl+C: C goes down while Ctrl is held
        assert self._press(d, [(1.00, True, False, 5, (0, 0)), (1.04, True, True, 6, (0, 0)),
                               (1.08, True, False, 7, (0, 0)), (1.12, False, False, 8, (0, 0))]) is False
        # Shift held first, then Ctrl (Shift+Ctrl switches the keyboard language on some computers)
        assert self._press(d, [(2.00, True, True, 9, (0, 0)), (2.10, False, False, 10, (0, 0))]) is False
        # and the next press on its own counts again
        assert self._press(d, [(3.00, True, False, 11, (0, 0)), (3.10, False, False, 12, (0, 0))]) is True

    def test_a_long_hold_does_not(self) -> None:
        d = self._detector()
        assert self._press(d, [(1.0, True, False, 5, (0, 0)), (1.3, True, False, 5, (0, 0)),
                               (1.6, True, False, 5, (0, 0)), (1.7, False, False, 6, (0, 0))]) is False

    def test_the_wheel_does_not_but_moving_the_mouse_does(self) -> None:
        d = self._detector()      # Ctrl+wheel (zoom): input arrives while nothing else is down and the mouse is still
        assert self._press(d, [(1.00, True, False, 5, (40, 40)), (1.04, True, False, 6, (40, 40)),
                               (1.10, False, False, 7, (40, 40))]) is False
        # the hand on the mouse moved it a little while pressing Ctrl: still Ctrl on its own
        assert self._press(d, [(2.00, True, False, 8, (40, 40)), (2.04, True, False, 9, (42, 41)),
                               (2.08, True, False, 9, (42, 41)), (2.12, False, False, 10, (42, 41))]) is True

    def test_ctrl_already_down_when_watching_starts_is_not_a_press(self) -> None:
        d = TapDetector()
        assert self._press(d, [(0.00, True, False, 1, (0, 0)), (0.10, False, False, 2, (0, 0))]) is False
        assert self._press(d, [(1.00, True, False, 3, (0, 0)), (1.10, False, False, 4, (0, 0))]) is True

    def test_ctrl_pressed_by_jarvis_does_not_count(self) -> None:
        d = self._detector()
        assert self._press(d, [(1.00, True, False, 5, (0, 0)), (1.03, False, False, 6, (0, 0))], own=True) is False
        assert d.feed(2.00, True, False, 7, (0, 0)) is False
        assert d.feed(2.03, False, own=True) is False            # JARVIS's own keys were sent meanwhile

    def test_the_marker_for_jarvis_own_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.host import winapi

        monkeypatch.setattr(winapi, "_own_keys_until", 0.0)
        assert winapi.own_keys_recent() is False
        winapi.note_own_keys(5.0)
        assert winapi.own_keys_recent() is True
        winapi.note_own_keys(0.0)                                # a shorter mark never cuts a longer one
        assert winapi.own_keys_recent() is True

    def test_the_watcher_thread_calls_back_once_per_tap(self) -> None:
        keys = ScriptedKeys([(False, False, 0, (0, 0)),
                             (True, False, 1, (0, 0)), (True, False, 1, (0, 0)), (False, False, 2, (0, 0)),
                             (True, False, 3, (0, 0)), (True, True, 4, (0, 0)), (False, False, 5, (0, 0)),
                             (True, False, 6, (0, 0)), (False, False, 7, (0, 0))], failures=3)
        taps: list[str] = []
        watch = CtrlTapWatch(lambda: taps.append(threading.current_thread().name), keys, poll_s=0.001)
        try:
            assert keys.finished.wait(5.0)
            assert watch.running
        finally:
            watch.stop()
        assert taps == ["jarvis-ctrl-watch", "jarvis-ctrl-watch"]   # the Ctrl+key press in between did nothing
        assert not watch.running

    def test_a_failing_handler_does_not_stop_the_watcher(self) -> None:
        keys = ScriptedKeys([(False, False, 0, (0, 0)), (True, False, 1, (0, 0)), (False, False, 2, (0, 0)),
                             (True, False, 3, (0, 0)), (False, False, 4, (0, 0))])
        calls: list[int] = []

        def handler() -> None:
            calls.append(1)
            raise RuntimeError("UI gone")

        watch = CtrlTapWatch(handler, keys, poll_s=0.001)
        try:
            assert keys.finished.wait(5.0)
        finally:
            watch.stop()
        assert calls == [1, 1]

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows has the keyboard to watch")
    def test_only_on_windows(self) -> None:
        assert watch_ctrl_taps(lambda: None) is None


class TestTalkKey:
    """What the pipeline does when Ctrl is tapped on its own."""

    async def test_listens_for_one_command(self, tmp_path: Path) -> None:
        reply = SpeechReply(text="It is noon.", follow_up=False)
        async with Harness(tmp_path, texts=["what time is it"], reply=reply) as h:
            await h.cache.warm()
            h.pipeline.talk_key()
            assert h.pipeline.state is VoiceState.LISTENING
            h.source.append(speechlike(1.0))       # no wake word said
            await wait_for(lambda: h.handled and h.quiet, what="the command")
            await h.pipeline.flush_events()
        assert h.events.transitions() == ["idle", "wake", "listening", "processing", "speaking", "idle"]
        triggers = [e[2] for e in h.events.events if e[0] == "state" and e[1] == "listening"]
        assert triggers == ["key"]
        assert h.played()[0] == "כן?" and h.handled[0].text == "what time is it"
        assert h.pipeline.stats["talk_keys"] == 1 and h.pipeline.stats["wakes"] == 0

    async def test_pressed_again_stops_listening(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["this was not for you"]) as h:
            h.pipeline.talk_key()
            assert h.pipeline.state is VoiceState.LISTENING
            h.pipeline.talk_key()
            assert h.quiet
            h.source.append(speechlike(1.0))       # people talking in the room afterwards
            await h.source.drained(5.0)
            await asyncio.sleep(0.1)
        assert h.handled == [] and h.stt.calls == []

    async def test_during_follow_up_it_keeps_listening(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["open chrome", "and youtube"], reply="Which profile?") as h:
            h.say(1.0)
            await wait_for(lambda: len(h.handled) == 1 and h.pipeline.state is VoiceState.LISTENING, what="follow-up")
            h.pipeline.talk_key()                  # the person presses Ctrl to answer: not a "never mind"
            assert h.pipeline.state is VoiceState.LISTENING
            h.say(1.0, wake=False)
            await wait_for(lambda: len(h.handled) == 2, what="the answer")
        assert [t.text for t in h.handled] == ["open chrome", "and youtube"]

    async def test_stops_speech_and_listens(self, tmp_path: Path) -> None:
        long_reply = "This is a long answer that keeps going. " * 6
        async with Harness(tmp_path, texts=["tell me a story", "stop"], reply=long_reply, sink_speed=0.2) as h:
            h.say(1.0)
            await wait_for(lambda: h.pipeline.state is VoiceState.SPEAKING and h.sink.playing, what="speaking")
            h.pipeline.talk_key()
            assert h.pipeline.state is VoiceState.LISTENING
            h.say(0.8, wake=False)
            await wait_for(lambda: len(h.handled) == 2, what="second command")
        assert h.sink.interrupted and h.sink.interrupted[0].text.startswith("This is a long answer")

    async def test_ignored_without_a_microphone(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, source=BrokenSource(missing=True)) as h:
            await wait_for(lambda: h.pipeline.state is VoiceState.DISABLED, what="disabled")
            h.pipeline.talk_key()
            await h.pipeline.flush_events()
            assert h.pipeline.state is VoiceState.DISABLED
        assert "listening" not in h.events.states() and h.sink.played == []

    async def test_works_while_muted_like_the_microphone_button(self, tmp_path: Path) -> None:
        async with Harness(tmp_path, texts=["open chrome"], reply=SpeechReply(text="OK.", follow_up=False)) as h:
            h.pipeline.mute()
            await wait_for(lambda: not h.source.running, what="microphone closed")
            h.pipeline.talk_key()
            h.source.append(speechlike(1.0))
            await wait_for(lambda: h.handled and h.quiet, what="the command")
            await wait_for(lambda: not h.source.running, what="microphone closed again")


# =============================================================================================
# Service
# =============================================================================================
async def _noop_handler(transcript: Transcript) -> None:
    return None


INFO_KEYS = {"state", "enabled", "muted", "wake_word", "engines", "devices", "last_transcript", "error"}


def _check_info_shape(info: dict[str, Any]) -> None:
    assert set(info) == INFO_KEYS
    assert set(info["engines"]) == {"wake", "stt", "tts"} and set(info["devices"]) == {"input", "output"}
    assert info["state"] in {s.value for s in VoiceState}
    assert isinstance(info["enabled"], bool) and isinstance(info["muted"], bool) and isinstance(info["wake_word"], str)
    json.dumps(info)  # serializable for the API


class TestService:
    async def test_disabled_in_settings(self, paths: Any) -> None:
        events = Recorder()
        service = VoiceService(VoiceSettings(enabled=False), paths, None, _noop_handler, events)
        await service.start()
        info = service.info()
        _check_info_shape(info)
        assert info["state"] == "disabled" and info["enabled"] is False
        assert info["error"] == "Voice is turned off in settings."
        assert events.of("state") == [("disabled", None, False, "Voice is turned off in settings.")]
        assert await service.speak("hello") is False
        service.mute()
        service.push_to_talk_start()  # harmless while disabled
        await service.stop()

    @needs_numpy
    async def test_start_without_microphone_does_not_raise(self, paths: Any) -> None:
        settings = VoiceSettings(stt_engine="none", tts_engine="none", wake_word_engine="none")
        service = VoiceService(settings, paths, None, _noop_handler, Recorder())
        await service.start()
        try:
            info = service.info()
            _check_info_shape(info)
            assert info["enabled"] is True and info["engines"] == {"wake": "none", "stt": "none", "tts": "none"}
            if not audio_mod.list_devices()["input"]:  # e.g. CI / containers
                await wait_for(lambda: service.info()["state"] == "disabled", what="no-microphone state")
                info = service.info()
                assert "microphone" in info["error"].lower() or "audio devices" in info["error"].lower()
                assert info["devices"]["input"] is None
            else:  # pragma: no cover - a machine with a microphone
                assert info["state"] in ("idle", "disabled")
            await wait_for(lambda: "wake" in service.details()["reasons"], what="the wake word reason")  # warm-up sets it
            details = service.details()
            assert "turned off" in details["reasons"]["wake"]
        finally:
            await service.stop()
        info = service.info()
        assert info["state"] == "disabled" and info["enabled"] is False and info["error"] is None

    @needs_numpy
    async def test_end_to_end_with_injected_engines(self, paths: Any) -> None:
        source = ArraySource(realtime=True, time_scale=0.05, fill_silence=True)
        sink = NullSink(realtime=True, speed=0.05)
        stt, tts, events = FakeSTT(["open chrome", "what time is it"]), FakeTTS(), Recorder()
        handled: list[str] = []

        async def handler(transcript: Transcript) -> SpeechReply:
            handled.append(transcript.text)
            return SpeechReply(text="Done.", follow_up=False)

        service = VoiceService(VoiceSettings(follow_up_window_s=1.0), paths, None, handler, events,
                               source=source, sink=sink, wake=MarkerWake(), stt=stt, tts=tts)
        await service.start()
        await service.start()  # idempotent
        try:
            await wait_for(lambda: service.details()["warmup"].get("seconds") is not None, what="warm-up")
            info = service.info()
            _check_info_shape(info)
            assert info["state"] == "idle" and info["enabled"] and info["wake_word"] == "hey jarvis"
            assert info["engines"] == {"wake": "fake", "stt": "fake_stt", "tts": "fake_tts"}
            assert ("fake", "hey jarvis") in events.of("info")      # the UI is told once the wake word is loaded
            assert info["devices"]["input"] == "array"
            assert service.details()["warmup"]["phrases"]["synthesized"] == sum(len(v) for v in ACK_PHRASES.values())
            source.append_frames([marker_frame()])
            source.append(speechlike(1.0))
            await wait_for(lambda: handled == ["open chrome"] and service.info()["state"] == "idle", what="turn")
            assert service.info()["last_transcript"] == "open chrome"
            assert sink.played[0].text == "כן?"  # acknowledgement from the warmed cache
            service.mute()
            assert service.info()["muted"] is True
            service.unmute()
            service.push_to_talk_start()
            assert service.info()["state"] == "listening"
            source.append(speechlike(1.0))
            await source.drained(5.0)
            service.push_to_talk_stop()
            await wait_for(lambda: len(handled) == 2 and service.info()["state"] == "idle", what="ptt turn")
            assert await service.speak("All tasks are complete.") is True
            await service.restart()
            assert service.info()["state"] in ("idle", "listening") and service.info()["enabled"]
        finally:
            await service.stop()
        assert service.info()["state"] == "disabled" and events.states()[-1] == "disabled"

    @needs_numpy
    async def test_ctrl_to_talk(self, paths: Any) -> None:
        source = ArraySource(realtime=True, time_scale=0.05, fill_silence=True)
        handled: list[str] = []

        async def handler(transcript: Transcript) -> SpeechReply:
            handled.append(transcript.text)
            return SpeechReply(text="It is noon.", follow_up=False)

        class FakeWatch:
            def __init__(self, on_tap: Any) -> None:
                self.on_tap, self.stopped = on_tap, False

            def stop(self) -> None:
                self.stopped = True

        made: list[FakeWatch] = []
        service = VoiceService(VoiceSettings(), paths, None, handler, Recorder(), source=source,
                               sink=NullSink(realtime=True, speed=0.05), wake=MarkerWake(), stt=FakeSTT(["what time is it"]),
                               tts=FakeTTS(), talk_key=lambda on_tap: made.append(FakeWatch(on_tap)) or made[-1])
        await service.start()
        try:
            assert len(made) == 1 and service.details()["reasons"]["talk_key"] == "Ctrl"
            tap = threading.Thread(target=made[0].on_tap)   # the watcher calls from its own thread
            tap.start()
            tap.join()
            await wait_for(lambda: service.info()["state"] == "listening", what="listening after Ctrl")
            source.append(speechlike(1.0))
            await wait_for(lambda: handled == ["what time is it"] and service.info()["state"] == "idle", what="turn")
            await service.restart()                          # a restart watches again, with a new watcher
            assert len(made) == 2 and made[0].stopped and not made[1].stopped
        finally:
            await service.stop()
        assert made[1].stopped

    @needs_numpy
    async def test_ctrl_to_talk_off(self, paths: Any) -> None:
        made: list[Any] = []
        service = VoiceService(VoiceSettings(ctrl_to_talk=False), paths, None, _noop_handler, Recorder(),
                               source=ArraySource(realtime=True, time_scale=0.05, fill_silence=True),
                               sink=NullSink(), wake=MarkerWake(), stt=FakeSTT(), tts=FakeTTS(),
                               talk_key=lambda on_tap: made.append(on_tap))
        await service.start()
        try:
            assert made == [] and service.details()["reasons"]["talk_key"] == "off"
        finally:
            await service.stop()

    def test_list_devices(self, paths: Any) -> None:
        devices = VoiceService(VoiceSettings(), paths, None, _noop_handler).list_devices()
        assert set(devices) >= {"input", "output", "error"}

    def test_import_and_start_without_numpy(self) -> None:
        code = textwrap.dedent(
            """
            import asyncio, json, sys
            sys.modules["numpy"] = None  # simulate a PC without the voice extra
            import jarvis.voice as voice
            from jarvis.config.paths import resolve_paths
            from jarvis.config.settings import Settings

            async def handler(transcript):
                return None

            async def main():
                settings = Settings()
                service = voice.VoiceService(settings.voice, resolve_paths(settings.paths), None, handler)
                await service.start()
                info = service.info()
                await service.stop()
                return info

            info = asyncio.run(main())
            heavy = [m for m in ("sounddevice", "openwakeword", "faster_whisper", "edge_tts", "pyttsx3",
                                 "miniaudio", "webrtcvad", "onnxruntime", "ctranslate2") if m in sys.modules]
            print(json.dumps({"info": info, "heavy": heavy}))
            """
        )
        env = {**os.environ, "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env)
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout.strip().splitlines()[-1])
        info = out["info"]
        _check_info_shape(info)
        assert info["state"] == "disabled" and info["enabled"] is False
        assert "numpy" in info["error"] and "pip install" in info["error"]
        assert out["heavy"] == []


# =============================================================================================
# Self-test
# =============================================================================================
class TestSelftest:
    def test_similarity(self) -> None:
        from jarvis.voice.selftest import similarity

        assert similarity("Hello, I am JARVIS.", "hello i am jarvis") == 1.0
        assert similarity("open chrome now", "open chrome") == pytest.approx(2 / 3)
        assert similarity("", "x") == 0.0

    @needs_numpy
    async def test_report_without_hardware_or_models(self, paths: Any) -> None:
        from jarvis.voice.selftest import format_report, run_selftest

        settings = VoiceSettings(tts_engine="none", stt_engine="none", wake_word_engine="none")
        report = await run_selftest(settings, paths, None, play=False, mic_seconds=0.3)
        names = [c["name"] for c in report["checks"]]
        assert names == ["packages", "devices", "microphone", "tts", "playback", "stt", "wake", "phrases"]
        assert all(c["status"] in ("ok", "warn", "fail", "skip") for c in report["checks"])
        check = {c["name"]: c for c in report["checks"]}
        assert check["playback"]["status"] == "skip" and check["tts"]["status"] == "fail"
        assert check["stt"]["status"] == "fail" and "turned off" in check["stt"]["detail"]
        assert report["ok"] is False
        text = format_report(report)
        assert all(name in text for name in names) and "[FAIL]" in text

    @needs_numpy
    @pytest.mark.skipif(not _have("pyttsx3") or (sys.platform.startswith("linux") and ESPEAK is None),
                        reason="pyttsx3 / espeak-ng not available")
    async def test_offline_voice_path(self, paths: Any) -> None:
        from jarvis.voice.selftest import run_selftest

        settings = VoiceSettings(tts_engine="sapi", stt_engine="none", wake_word_engine="none")
        report = await run_selftest(settings, paths, None, play=False, microphone=False, stt=False, wake=False)
        check = {c["name"]: c for c in report["checks"]}
        assert check["tts"]["status"] in ("ok", "warn") and "en" in check["tts"]["data"]["results"]
        assert check["phrases"]["status"] in ("ok", "warn")

    def test_cli_json(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        from jarvis.voice.selftest import main

        monkeypatch.setenv("JARVIS__VOICE__TTS_ENGINE", "none")
        code = main(["--no-play", "--no-mic", "--no-stt", "--no-wake", "--json"])
        report = json.loads(capsys.readouterr().out)
        assert code in (0, 1) and report["checks"][0]["name"] == "packages"


# =============================================================================================
# Real models (skipped unless already downloaded)
# =============================================================================================
def _model_dirs() -> list[Path]:
    dirs = []
    if os.environ.get("JARVIS_TEST_MODELS_DIR"):
        dirs.append(Path(os.environ["JARVIS_TEST_MODELS_DIR"]))
    try:
        from platformdirs import user_cache_dir

        dirs.append(Path(user_cache_dir("JARVIS", appauthor=False)) / "models")  # where the app downloads them
    except Exception:  # pragma: no cover
        pass
    return dirs


def _openwakeword_engine() -> Any:
    if not (_have("openwakeword") and _have("onnxruntime")):
        pytest.skip("openwakeword/onnxruntime not installed")
    for base in [*(d / "openwakeword" for d in _model_dirs()), None]:
        try:
            files = wake_mod.ensure_openwakeword_models("hey_jarvis", base, download=False)
        except WakeWordError:
            continue
        engine = wake_mod.OpenWakeWordEngine("hey_jarvis", models_dir=files["model"].parent, auto_download=False)
        engine.load()
        return engine
    pytest.skip("openWakeWord models not downloaded (run the app or the self-test once, or set JARVIS_TEST_MODELS_DIR)")


@needs_numpy
@needs_espeak
@pytest.mark.voice_models
class TestRealModels:
    def test_openwakeword_detects_hey_jarvis(self, tmp_path: Path) -> None:
        engine = _openwakeword_engine()
        try:
            for voice in ("en", "en-us"):
                speech = audio_mod.load_audio(espeak_wav(tmp_path / f"hj_{voice}.wav", "hey jarvis", voice))
                engine.reset()
                best = max(engine.process(f) for f in frames_of(silence(1.0), speech, silence(1.0)))
                assert engine.detected(best), f"{voice}: score {best:.2f}"
            other = audio_mod.load_audio(espeak_wav(tmp_path / "other.wav", "open the browser please"))
            engine.reset()
            assert max(engine.process(f) for f in frames_of(silence(1.0), other, silence(1.0))) < 0.3
            noise = (np.random.default_rng(0).standard_normal(SAMPLE_RATE * 2) * 60).astype(np.int16)
            t0 = time.perf_counter()
            frames = frames_of(noise)
            for f in frames:
                engine.process(f)
            per_frame = (time.perf_counter() - t0) / len(frames)
            assert per_frame < 0.08 * 0.25  # far below real time (target: < 5 % of a core on a desktop)
        finally:
            engine.close()

    async def test_pipeline_with_real_wake_word(self, tmp_path: Path) -> None:
        engine = _openwakeword_engine()
        wake_audio = audio_mod.load_audio(espeak_wav(tmp_path / "hj.wav", "hey jarvis"))
        command = audio_mod.load_audio(espeak_wav(tmp_path / "cmd.wav", "open chrome"))
        source = ArraySource(realtime=True, time_scale=0.5, fill_silence=True)
        source.append(np.concatenate([silence(1.0), wake_audio, silence(0.6), command, silence(0.3)]))
        try:
            async with Harness(tmp_path, texts=["open chrome"], wake=engine, source=source, reply=None) as h:
                await wait_for(lambda: h.handled and h.quiet, timeout=30, what="wake + command")
        finally:
            engine.close()
        assert h.pipeline.stats["wakes"] == 1 and h.handled[0].text == "open chrome"
        assert h.stt.calls[0]["samples"] >= 0.5 * len(command)  # the command audio was captured

    async def test_faster_whisper_round_trip(self, tmp_path: Path) -> None:
        if not _have("faster_whisper"):
            pytest.skip("faster-whisper not installed")
        engine = None
        for size in ("base", "tiny", "small", "base.en", "tiny.en"):
            for root in [*(d / "whisper" for d in _model_dirs()), None]:
                candidate = FasterWhisperSTT(size, device="cpu", download_root=root, allow_download=False)
                try:
                    candidate.load()
                except STTError:
                    candidate.close()
                    continue
                engine = candidate
                break
            if engine is not None:
                break
        if engine is None:
            pytest.skip("no faster-whisper model downloaded (run the app or the self-test once)")
        try:
            text = "open chrome and search for the weather in tel aviv"
            speech = audio_mod.load_audio(espeak_wav(tmp_path / "cmd.wav", text, speed=140))
            result = await engine.transcribe(np.concatenate([silence(0.3), speech, silence(0.3)]), "auto")
            from jarvis.voice.selftest import similarity

            assert result.language == "en" and similarity(text, result.text) >= 0.5, result.text
        finally:
            engine.close()
