"""Audio I/O for the voice pipeline: sources, sinks, devices, WAV and small DSP helpers.

Everything the pipeline hears or says goes through two small interfaces, so the whole voice
stack can be tested without hardware:

* `AudioSource` — async source of 16 kHz mono int16 frames of 1280 samples (80 ms, the chunk
  openWakeWord is built around). `MicrophoneSource` captures from a real device with
  sounddevice (PortAudio callback thread → bounded asyncio queue, oldest frame dropped on
  overflow); `ArraySource` replays numpy arrays / WAV files, optionally paced in real time,
  and can keep producing silence like an idle microphone (tests and the self-test).
* `AudioSink` — plays an `AudioClip` and can be stopped at any moment (barge-in).
  `SoundDeviceSink` and `MiniaudioSink` play on a real device; `NullSink` records what would
  have been played (optionally simulating the playback time).

Heavy libraries (numpy, sounddevice, miniaudio) are imported lazily, so `import jarvis.voice`
works even when the optional voice extra is not installed.
"""

from __future__ import annotations

import asyncio
import collections
import io
import logging
import math
import os
import threading
import time
import wave
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.common.errors import VoiceError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1_280  # 80 ms at 16 kHz
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

VOICE_EXTRA_HINT = 'pip install "jarvis-assistant[voice]"'


class AudioError(VoiceError):
    code = "audio_error"


class AudioDeviceError(AudioError):
    """No usable audio device. `missing=True` means there is no such device at all."""

    code = "audio_device_error"

    def __init__(self, message: str = "", *, missing: bool = False, details: dict | None = None) -> None:
        super().__init__(message, details=details)
        self.missing = missing


# ---------------------------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------------------------
def np_module():
    """Import numpy lazily (it is part of the optional voice extra)."""
    try:
        import numpy
    except ImportError as exc:
        raise VoiceError(f"numpy is not installed ({VOICE_EXTRA_HINT})") from exc
    return numpy


def _sounddevice():
    try:
        import sounddevice
    except (ImportError, OSError) as exc:  # OSError: the PortAudio library itself is missing
        raise AudioDeviceError(f"Audio devices unavailable: sounddevice/PortAudio is not installed ({VOICE_EXTRA_HINT})", missing=True) from exc
    return sounddevice


def _miniaudio():
    try:
        import miniaudio
    except (ImportError, OSError) as exc:
        raise AudioError(f"miniaudio is not available: {exc}") from exc
    return miniaudio


# ---------------------------------------------------------------------------------------------
# Sample conversion and levels
# ---------------------------------------------------------------------------------------------
def to_float32(audio: Any) -> np.ndarray:
    """Mono float32 in -1..1 from int16/int32/uint8/float samples shaped (n,) or (n, channels)."""
    np = np_module()
    a = np.asarray(audio)
    if a.dtype == np.int16:
        f = a.astype(np.float32) / 32768.0
    elif a.dtype == np.int32:
        f = a.astype(np.float32) / 2147483648.0
    elif a.dtype == np.uint8:
        f = (a.astype(np.float32) - 128.0) / 128.0
    elif np.issubdtype(a.dtype, np.floating):
        f = a.astype(np.float32, copy=False)
    elif np.issubdtype(a.dtype, np.integer):
        f = a.astype(np.float32) / float(np.iinfo(a.dtype).max + 1)
    else:
        raise AudioError(f"Unsupported sample type: {a.dtype}")
    if f.ndim == 2:
        f = f[:, 0] if f.shape[1] == 1 else f.mean(axis=1)
    elif f.ndim != 1:
        raise AudioError(f"Expected mono or (frames, channels) audio, got shape {a.shape}")
    return np.ascontiguousarray(f, dtype=np.float32)


def to_int16(audio: Any) -> np.ndarray:
    """Mono int16 samples from any supported sample format."""
    np = np_module()
    a = np.asarray(audio)
    if a.dtype == np.int16 and a.ndim == 1:
        return np.ascontiguousarray(a)
    f = to_float32(a)
    return (np.clip(f, -1.0, 1.0) * 32767.0).round().astype(np.int16)


def rms(frame: Any) -> float:
    """Root-mean-square amplitude, normalized to 0..1 (full-scale square wave = 1)."""
    np = np_module()
    a = np.asarray(frame)
    if a.size == 0:
        return 0.0
    if a.dtype == np.int16 and a.ndim == 1:
        f = a.astype(np.float32)
        return float(math.sqrt(float(np.dot(f, f)) / f.size) / 32768.0)
    f = to_float32(a)
    return float(math.sqrt(float(np.dot(f, f)) / max(1, f.size)))


def rms_dbfs(frame: Any) -> float:
    return 20.0 * math.log10(max(rms(frame), 1e-7))


def rms_level(frame: Any, *, floor_db: float = -60.0, ceil_db: float = -6.0) -> float:
    """Perceptual level 0..1 for the UI orb: -60 dBFS (quiet room) → 0, -6 dBFS (shouting) → 1."""
    level = (rms_dbfs(frame) - floor_db) / (ceil_db - floor_db)
    return float(min(1.0, max(0.0, level)))


def iter_frames(audio: Any, frame_samples: int = FRAME_SAMPLES, *, pad: bool = True) -> Iterator[np.ndarray]:
    """Split mono audio into int16 frames; the last partial frame is zero-padded (or dropped)."""
    np = np_module()
    a = to_int16(audio)
    for start in range(0, len(a), frame_samples):
        frame = a[start : start + frame_samples]
        if len(frame) < frame_samples:
            if not pad:
                break
            frame = np.concatenate((frame, np.zeros(frame_samples - len(frame), dtype=np.int16)))
        yield np.array(frame, dtype=np.int16, copy=True)


# ---------------------------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------------------------
def _lowpass_taps(cutoff: float, ntaps: int = 63) -> np.ndarray:
    """Windowed-sinc FIR low-pass; `cutoff` is a fraction of the input sample rate (0..0.5)."""
    np = np_module()
    n = np.arange(ntaps, dtype=np.float64) - (ntaps - 1) / 2.0
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * n) * np.blackman(ntaps)
    return (h / h.sum()).astype(np.float32)


def resample(audio: Any, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample mono audio. int16 in → int16 out; anything else → float32 out.

    Anti-aliased (FIR low-pass) when downsampling, then linear interpolation — plenty for
    speech recognition and for playing TTS clips at a device's native rate.
    """
    np = np_module()
    a = np.asarray(audio)
    src_rate, dst_rate = int(src_rate), int(dst_rate)
    if src_rate <= 0 or dst_rate <= 0:
        raise AudioError("Sample rates must be positive")
    want_int = a.dtype == np.int16
    if src_rate == dst_rate or a.size == 0:
        return to_int16(a) if want_int else to_float32(a)
    x = to_float32(a)
    if dst_rate < src_rate:
        x = np.convolve(x, _lowpass_taps(0.46 * dst_rate / src_rate), mode="same").astype(np.float32)
    n_out = max(1, round(len(x) * dst_rate / src_rate))
    positions = np.arange(n_out, dtype=np.float64) * (src_rate / dst_rate)
    y = np.interp(positions, np.arange(len(x), dtype=np.float64), x).astype(np.float32)
    return to_int16(y) if want_int else y


class StreamResampler:
    """Streaming resampler for microphone callbacks (keeps filter and phase state across chunks)."""

    def __init__(self, src_rate: int, dst_rate: int, *, ntaps: int = 63) -> None:
        np = np_module()
        self.src_rate, self.dst_rate = int(src_rate), int(dst_rate)
        self._step = self.src_rate / self.dst_rate
        self._taps = _lowpass_taps(0.46 * self.dst_rate / self.src_rate, ntaps) if self.dst_rate < self.src_rate else None
        self._history = np.zeros(ntaps - 1, dtype=np.float32) if self._taps is not None else None
        self._buffer = np.zeros(0, dtype=np.float32)
        # Start at the filter's centre so the output is aligned with the input (no group delay).
        self._pos = (ntaps - 1) / 2.0 if self._taps is not None else 0.0

    def process(self, chunk: Any) -> np.ndarray:
        np = np_module()
        x = to_float32(chunk)
        if self._taps is not None and self._history is not None:
            joined = np.concatenate((self._history, x))
            y = np.convolve(joined, self._taps, mode="valid").astype(np.float32)
            self._history = joined[-(len(self._taps) - 1) :]
        else:
            y = x
        buf = np.concatenate((self._buffer, y)) if self._buffer.size else y
        last = len(buf) - 1
        if last < self._pos:
            self._buffer = buf
            return np.zeros(0, dtype=np.int16)
        n_out = math.floor((last - self._pos) / self._step) + 1
        positions = self._pos + np.arange(n_out, dtype=np.float64) * self._step
        out = np.interp(positions, np.arange(len(buf), dtype=np.float64), buf)
        next_pos = self._pos + n_out * self._step
        # Keep the sample left of the next position for interpolation; when downsampling the
        # next position can lie beyond this buffer (inside the next chunk) — keep that offset.
        drop = min(math.floor(next_pos), len(buf))
        self._buffer = buf[drop:]
        self._pos = next_pos - drop
        return to_int16(out)


# ---------------------------------------------------------------------------------------------
# Clips, WAV and decoding
# ---------------------------------------------------------------------------------------------
@dataclass
class AudioClip:
    """A piece of audio to play: mono int16 PCM + sample rate, plus where it came from.

    `source_id` ("engine|voice|rate") identifies the TTS configuration that produced it; the
    phrase cache keys files by it so a voice change never plays a stale recording.
    """

    samples: Any
    sample_rate: int
    text: str = ""
    language: str = ""
    engine: str = ""
    voice: str = ""
    source_id: str = ""

    def __post_init__(self) -> None:
        self.samples = to_int16(self.samples)
        self.sample_rate = int(self.sample_rate)
        if self.sample_rate <= 0:
            raise AudioError("AudioClip needs a positive sample rate")

    @property
    def duration_s(self) -> float:
        return len(self.samples) / float(self.sample_rate)

    def as_float32(self) -> np.ndarray:
        return to_float32(self.samples)

    def to_wav_bytes(self) -> bytes:
        return wav_bytes(self.samples, self.sample_rate)

    def resampled(self, rate: int) -> AudioClip:
        if int(rate) == self.sample_rate:
            return self
        return AudioClip(
            resample(self.samples, self.sample_rate, rate), rate, self.text, self.language, self.engine, self.voice, self.source_id
        )

    def levels(self, step_s: float = FRAME_SECONDS) -> list[float]:
        """Perceptual level per `step_s` window (drives the orb while JARVIS speaks)."""
        step = max(1, int(self.sample_rate * step_s))
        return [rms_level(self.samples[i : i + step]) for i in range(0, len(self.samples), step)]

    @classmethod
    def from_wav(cls, src: str | Path | bytes, **meta: Any) -> AudioClip:
        samples, rate = read_wav(src)
        return cls(samples, rate, **meta)

    @classmethod
    def silence(cls, seconds: float, sample_rate: int = SAMPLE_RATE) -> AudioClip:
        np = np_module()
        return cls(np.zeros(int(seconds * sample_rate), dtype=np.int16), sample_rate)


def _wav_samples(raw: bytes, width: int, channels: int) -> np.ndarray:
    np = np_module()
    if width == 2:
        a = np.frombuffer(raw, dtype="<i2")
    elif width == 1:
        a = np.frombuffer(raw, dtype=np.uint8)
    elif width == 4:
        a = np.frombuffer(raw, dtype="<i4")
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8)
        b = b[: len(b) - len(b) % 3].reshape(-1, 3).astype(np.int32)
        a = ((b[:, 0] << 8) | (b[:, 1] << 16) | (b[:, 2] << 24)).astype(np.int32)
    else:
        raise AudioError(f"Unsupported WAV sample width: {width} bytes")
    if channels > 1:
        a = a[: len(a) - len(a) % channels].reshape(-1, channels)
    return to_int16(a)


def read_wav(src: str | Path | bytes | bytearray | memoryview) -> tuple[np.ndarray, int]:
    """Read a WAV file (path or bytes) as mono int16 samples and its sample rate."""
    data = bytes(src) if isinstance(src, (bytes, bytearray, memoryview)) else Path(src).read_bytes()
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        # e.g. IEEE-float WAVs, which the stdlib cannot read — miniaudio can.
        try:
            clip = decode_audio_bytes(data)
        except AudioError:
            raise AudioError(f"Cannot read WAV data: {exc}") from exc
        return clip.samples, clip.sample_rate
    return _wav_samples(raw, width, channels), rate


def wav_bytes(samples: Any, sample_rate: int) -> bytes:
    """Encode mono samples as a 16-bit PCM WAV file in memory."""
    a = to_int16(samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(a.astype("<i2").tobytes())
    return buf.getvalue()


def write_wav(path: str | Path, samples: Any, sample_rate: int) -> Path:
    """Write a 16-bit mono WAV atomically (readers never see a half-written file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(wav_bytes(samples, sample_rate))
    os.replace(tmp, path)
    return path


def _probe_rate(data: bytes) -> int | None:
    ma = _miniaudio()
    probes = []
    if data[:4] == b"fLaC":
        probes.append(ma.flac_get_info)
    elif data[:4] == b"OggS":
        probes.append(ma.vorbis_get_info)
    elif data[:4] == b"RIFF":
        probes.append(ma.wav_get_info)
    else:
        probes.append(ma.mp3_get_info)
    for probe in probes:
        try:
            return int(probe(data).sample_rate)
        except Exception:
            continue
    return None


def decode_audio_bytes(data: bytes, *, sample_rate: int | None = None) -> AudioClip:
    """Decode MP3/WAV/FLAC/Ogg-Vorbis bytes to a mono int16 clip (native rate unless given)."""
    if not data:
        raise AudioError("No audio data to decode")
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE" and sample_rate is None:
        try:
            with wave.open(io.BytesIO(data), "rb") as w:
                channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
                raw = w.readframes(w.getnframes())
            return AudioClip(_wav_samples(raw, width, channels), rate)
        except (wave.Error, EOFError):
            pass  # not plain PCM — let miniaudio try
    ma = _miniaudio()
    rate = sample_rate or _probe_rate(data) or 24_000
    try:
        decoded = ma.decode(data, output_format=ma.SampleFormat.SIGNED16, nchannels=1, sample_rate=rate)
    except Exception as exc:
        raise AudioError(f"Could not decode audio: {exc}") from exc
    np = np_module()
    return AudioClip(np.frombuffer(decoded.samples, dtype=np.int16).copy(), decoded.sample_rate)


def load_audio(src: str | Path | bytes, target_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Load any supported audio file/bytes as mono int16 at `target_rate` (16 kHz by default)."""
    data = bytes(src) if isinstance(src, (bytes, bytearray, memoryview)) else Path(src).read_bytes()
    if data[:4] == b"RIFF":
        samples, rate = read_wav(data)
    else:
        clip = decode_audio_bytes(data)
        samples, rate = clip.samples, clip.sample_rate
    return resample(samples, rate, target_rate)


def earcon(kind: str = "listening", sample_rate: int = 24_000) -> AudioClip:
    """A short synthetic chime, used when a spoken acknowledgement is not cached yet."""
    np = np_module()
    tones = {"listening": ((660.0, 0.07), (990.0, 0.09)), "error": ((440.0, 0.09), (330.0, 0.12))}.get(
        kind, ((880.0, 0.08),)
    )
    parts = []
    for freq, dur in tones:
        t = np.arange(int(sample_rate * dur), dtype=np.float32) / sample_rate
        tone = 0.22 * np.sin(2 * np.pi * freq * t)
        ramp = max(1, int(0.008 * sample_rate))
        env = np.ones_like(tone)
        env[:ramp] = np.linspace(0.0, 1.0, ramp)
        env[-ramp:] = np.linspace(1.0, 0.0, ramp)
        parts.append(tone * env)
        parts.append(np.zeros(int(0.015 * sample_rate), dtype=np.float32))
    return AudioClip(np.concatenate(parts), sample_rate, text="", engine="earcon", source_id=f"earcon|{kind}")


# ---------------------------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------------------------
@dataclass
class AudioDevice:
    index: int
    name: str
    hostapi: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    default_input: bool = False
    default_output: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "hostapi": self.hostapi,
            "channels": self.max_input_channels or self.max_output_channels,
            "default_samplerate": self.default_samplerate,
            "default": self.default_input or self.default_output,
        }


# PortAudio is not thread-safe while it re-initialises: device queries, stream creation and
# `refresh_devices()` are serialised (re-entrant: helpers nest).
_DEVICE_LOCK = threading.RLock()
_active_streams = 0  # PortAudio streams currently open by this module (refresh must not kill them)


def _stream_opened() -> None:
    global _active_streams
    with _DEVICE_LOCK:
        _active_streams += 1


def _stream_closed() -> None:
    global _active_streams
    with _DEVICE_LOCK:
        _active_streams = max(0, _active_streams - 1)


def refresh_devices() -> bool:
    """Re-scan audio devices (PortAudio caches the list at start-up). Skipped while streams are open."""
    try:
        sd = _sounddevice()
    except AudioError:
        return False
    with _DEVICE_LOCK:
        if _active_streams:
            return False
        try:
            sd._terminate()
            sd._initialize()
            return True
        except Exception as exc:  # pragma: no cover - platform specific
            log.debug("PortAudio re-initialisation failed: %s", exc)
            return False


def query_devices() -> list[AudioDevice]:
    """All PortAudio devices with their capabilities (raises AudioDeviceError when unavailable)."""
    sd = _sounddevice()
    try:
        with _DEVICE_LOCK:
            raw = list(sd.query_devices())
            hostapis = list(sd.query_hostapis())
            default = sd.default.device
        default_in, default_out = int(default[0]), int(default[1])
    except Exception as exc:
        raise AudioDeviceError(f"Cannot list audio devices: {exc}") from exc
    out = []
    for i, d in enumerate(raw):
        api_index = int(d.get("hostapi", 0))
        api = hostapis[api_index]["name"] if 0 <= api_index < len(hostapis) else ""
        out.append(
            AudioDevice(
                index=int(d.get("index", i)),
                name=str(d.get("name", f"device {i}")),
                hostapi=api,
                max_input_channels=int(d.get("max_input_channels", 0)),
                max_output_channels=int(d.get("max_output_channels", 0)),
                default_samplerate=float(d.get("default_samplerate", 0.0) or 0.0),
                default_input=int(d.get("index", i)) == default_in,
                default_output=int(d.get("index", i)) == default_out,
            )
        )
    return out


def _default_hostapi_name() -> str:
    try:
        sd = _sounddevice()
        return str(sd.query_hostapis(sd.default.hostapi)["name"])
    except Exception:
        return ""


def resolve_device(spec: str | int | None, kind: str = "input", *, strict: bool = False) -> AudioDevice:
    """Pick a device by index ("3"), exact or partial name ("USB"), or the system default ("").

    With `strict=False` an unknown name falls back to the default device (with a warning), so
    an unplugged headset does not silence JARVIS when the laptop microphone still works.
    """
    if kind not in ("input", "output"):
        raise ValueError("kind must be 'input' or 'output'")
    label = "microphone" if kind == "input" else "audio output device"
    devices = query_devices()
    candidates = [d for d in devices if (d.max_input_channels if kind == "input" else d.max_output_channels) > 0]
    if not candidates:
        raise AudioDeviceError(f"No {label} found", missing=True)
    default = next((d for d in candidates if (d.default_input if kind == "input" else d.default_output)), None)
    text = "" if spec is None else str(spec).strip()
    if not text or text.lower() == "default":
        return default or candidates[0]
    match: AudioDevice | None = None
    if text.lstrip("-").isdigit():
        match = next((d for d in candidates if d.index == int(text)), None)
    else:
        low = text.lower()
        named = [d for d in candidates if d.name.lower() == low] or [d for d in candidates if low in d.name.lower()]
        if named:
            preferred_api = _default_hostapi_name()
            named.sort(key=lambda d: (d.hostapi != preferred_api, d.index))
            match = named[0]
    if match is not None:
        return match
    if strict or default is None:
        names = ", ".join(sorted({d.name for d in candidates})[:8])
        raise AudioDeviceError(f"{label.capitalize()} {text!r} not found (available: {names})")
    log.warning("Configured %s %r not found; using the default %r", label, text, default.name)
    return default


def list_devices() -> dict[str, Any]:
    """Input/output devices for the settings UI. Never raises."""
    try:
        devices = query_devices()
    except AudioError as exc:
        return {"input": [], "output": [], "default_input": None, "default_output": None, "error": exc.message}
    inputs = [d for d in devices if d.max_input_channels > 0]
    outputs = [d for d in devices if d.max_output_channels > 0]
    return {
        "input": [d.to_dict() for d in inputs],
        "output": [d.to_dict() for d in outputs],
        "default_input": next((d.name for d in inputs if d.default_input), None),
        "default_output": next((d.name for d in outputs if d.default_output), None),
        "error": None if inputs else "No microphone found",
    }


def probe_input(spec: str | int | None = None, *, refresh: bool = False) -> tuple[bool, str]:
    """(usable, message) for the configured microphone — cheap, opens nothing."""
    if refresh:
        refresh_devices()
    try:
        device = resolve_device(spec, "input")
    except AudioError as exc:
        return False, exc.message
    return True, device.name


# ---------------------------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------------------------
class AudioSource(ABC):
    """Async source of fixed-size frames (int16 mono, `frame_samples` long, at `sample_rate`).

    `read()` returns None when the source was stopped or (for finite sources) ran out;
    `exhausted` tells the two apart.
    """

    name = "source"

    def __init__(self, *, sample_rate: int = SAMPLE_RATE, frame_samples: int = FRAME_SAMPLES) -> None:
        self.sample_rate = int(sample_rate)
        self.frame_samples = int(frame_samples)
        self.device_name: str | None = None

    @property
    @abstractmethod
    def running(self) -> bool: ...

    @property
    def exhausted(self) -> bool:
        return False

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def read(self) -> np.ndarray | None: ...

    def __aiter__(self) -> AsyncIterator[np.ndarray]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[np.ndarray]:
        while True:
            frame = await self.read()
            if frame is None:
                return
            yield frame


class MicrophoneSource(AudioSource):
    """Live microphone capture via sounddevice.

    PortAudio calls `_callback` on its own thread; complete 80 ms frames are handed to the
    event loop with `call_soon_threadsafe` and queued (bounded; the oldest frame is dropped on
    overflow so a stalled consumer never causes unbounded memory growth or stale audio). When
    the device refuses 16 kHz mono, it is opened at its native rate and resampled here.
    """

    name = "microphone"

    def __init__(
        self,
        device: str | int | None = None,
        *,
        sample_rate: int = SAMPLE_RATE,
        frame_samples: int = FRAME_SAMPLES,
        queue_frames: int = 128,
        latency: str | float = "low",
    ) -> None:
        super().__init__(sample_rate=sample_rate, frame_samples=frame_samples)
        self.device_spec = device
        self.device: AudioDevice | None = None
        self.device_rate: int | None = None
        self.overflows = 0
        self.dropped = 0
        self._queue_frames = max(8, int(queue_frames))
        self._latency = latency
        self._stream: Any = None
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._channels = 1
        self._resampler: StreamResampler | None = None
        self._pending: Any = None

    @property
    def running(self) -> bool:
        return self._stream is not None

    async def start(self) -> None:
        if self._stream is not None:
            return
        np_module()
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self._queue_frames)
        device = await asyncio.to_thread(resolve_device, self.device_spec, "input")
        stream = await asyncio.to_thread(self._open, device)
        try:
            await asyncio.to_thread(stream.start)
        except Exception as exc:
            await asyncio.to_thread(_close_stream, stream)
            raise AudioDeviceError(f"Could not start microphone {device.name!r}: {exc}") from exc
        self.device = device
        self.device_name = device.name
        self._stream = stream
        log.info("Microphone open: %s (%s, %s Hz)", device.name, device.hostapi, self.device_rate)

    def _open(self, device: AudioDevice) -> Any:
        sd = _sounddevice()
        np = np_module()
        native = int(device.default_samplerate or 48_000)
        attempts = [(self.sample_rate, 1)]
        if native != self.sample_rate:
            attempts.append((native, 1))
        if device.max_input_channels >= 2:
            attempts.append((native, 2))
        last: Exception | None = None
        for rate, channels in attempts:
            try:
                with _DEVICE_LOCK:
                    sd.check_input_settings(device=device.index, channels=channels, dtype="int16", samplerate=rate)
                    stream = sd.InputStream(
                        device=device.index,
                        samplerate=rate,
                        channels=channels,
                        dtype="int16",
                        blocksize=round(self.frame_samples * rate / self.sample_rate),
                        latency=self._latency,
                        callback=self._callback,
                    )
                    _stream_opened()
            except Exception as exc:
                last = exc
                continue
            self._channels = channels
            self.device_rate = rate
            self._resampler = StreamResampler(rate, self.sample_rate) if rate != self.sample_rate else None
            self._pending = np.zeros(0, dtype=np.int16)
            return stream
        raise AudioDeviceError(f"Could not open microphone {device.name!r}: {last}")

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        # PortAudio thread: keep it short and never raise.
        try:
            if status and getattr(status, "input_overflow", False):
                self.overflows += 1
            np = np_module()
            if self._channels == 1:
                data = np.array(indata[:, 0], dtype=np.int16, copy=True)
            else:
                data = to_int16(indata.astype(np.float32).mean(axis=1) / 32768.0)
            if self._resampler is not None:
                data = self._resampler.process(data)
            buf = np.concatenate((self._pending, data)) if self._pending is not None and self._pending.size else data
            n = self.frame_samples
            full = len(buf) // n
            for i in range(full):
                self._post(buf[i * n : (i + 1) * n].copy())
            self._pending = buf[full * n :]
        except Exception:  # pragma: no cover - defensive: an exception here would kill the stream
            log.debug("Microphone callback error", exc_info=True)

    def _post(self, frame: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._enqueue, frame)
        except RuntimeError:  # loop closed during shutdown
            pass

    def _enqueue(self, frame: Any) -> None:
        queue = self._queue
        if queue is None:
            return
        if queue.full():
            try:
                queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - race-free in the loop thread
                pass
        queue.put_nowait(frame)

    async def read(self) -> np.ndarray | None:
        queue = self._queue
        if queue is None:
            return None
        return await queue.get()

    async def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                await asyncio.to_thread(_close_stream, stream)
            finally:
                _stream_closed()
        if self._queue is not None:
            self._enqueue(None)


def _close_stream(stream: Any, *, abort: bool = False) -> None:
    """Stop and close a PortAudio stream. `abort=True` discards queued output immediately."""
    try:
        stream.abort() if abort else stream.stop()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


class ArraySource(AudioSource):
    """Frames from numpy arrays / WAV files — for tests, the self-test and offline replay.

    * `realtime=True` paces frames at 80 ms × `time_scale` like a real microphone.
    * `fill_silence=True` keeps producing silent frames once the queued audio is used up
      (an idle microphone), so time-outs measured in frames keep advancing.
    * `append()` adds audio while running (e.g. a test "speaking" after JARVIS answered).
    """

    name = "array"

    def __init__(
        self,
        audio: Any = None,
        *,
        sample_rate: int = SAMPLE_RATE,
        realtime: bool = False,
        time_scale: float = 1.0,
        fill_silence: bool = False,
        frame_samples: int = FRAME_SAMPLES,
        name: str = "array",
    ) -> None:
        super().__init__(sample_rate=SAMPLE_RATE, frame_samples=frame_samples)
        self.name = name
        self.device_name = name
        self.realtime = realtime
        self.time_scale = max(0.0, float(time_scale))
        self.fill_silence = fill_silence
        self.frames_read = 0
        self._frames: collections.deque = collections.deque()
        self._running = False
        self._eof = False
        self._next_due: float | None = None
        self._drained: asyncio.Event | None = None
        if audio is not None:
            self.append(audio, sample_rate=sample_rate)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def exhausted(self) -> bool:
        return self._eof and not self._frames and not self.fill_silence

    @property
    def pending(self) -> int:
        return len(self._frames)

    def append(self, audio: Any, *, sample_rate: int | None = None) -> int:
        """Queue audio (array, WAV path or WAV bytes). Returns the number of frames added."""
        if isinstance(audio, (str, Path)):
            path = Path(audio)
            samples, rate = read_wav(path) if path.suffix.lower() == ".wav" else (load_audio(path), SAMPLE_RATE)
        elif isinstance(audio, (bytes, bytearray, memoryview)):
            data = bytes(audio)
            samples, rate = read_wav(data) if data[:4] == b"RIFF" else (load_audio(data), SAMPLE_RATE)
        else:
            samples, rate = to_int16(audio), int(sample_rate or SAMPLE_RATE)
        if rate != SAMPLE_RATE:
            samples = resample(samples, rate, SAMPLE_RATE)
        frames = list(iter_frames(samples, self.frame_samples))
        self._frames.extend(frames)
        self._eof = False
        return len(frames)

    def append_frames(self, frames: Iterable[Any]) -> int:
        """Queue exact frames (each must be `frame_samples` long)."""
        n = 0
        for frame in frames:
            f = to_int16(frame)
            if len(f) != self.frame_samples:
                raise AudioError(f"Frame must have {self.frame_samples} samples, got {len(f)}")
            self._frames.append(f)
            n += 1
        self._eof = False
        return n

    def append_silence(self, seconds: float) -> int:
        np = np_module()
        return self.append(np.zeros(int(seconds * SAMPLE_RATE), dtype=np.int16))

    def clear(self) -> int:
        """Drop the queued frames nobody read (e.g. the microphone closed before the end). Returns how many."""
        n = len(self._frames)
        self._frames.clear()
        return n

    async def drained(self, timeout: float | None = None) -> None:
        """Wait until every queued frame has been read."""
        async def _wait() -> None:
            while self._frames:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(_wait(), timeout)

    async def start(self) -> None:
        self._running = True
        self._next_due = None

    async def stop(self) -> None:
        self._running = False

    async def read(self) -> np.ndarray | None:
        if not self._running:
            return None
        if self.realtime:
            period = FRAME_SECONDS * self.time_scale
            now = time.monotonic()
            if self._next_due is None or now - self._next_due > 1.0:
                self._next_due = now
            delay = self._next_due - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_due += period
        else:
            await asyncio.sleep(0)  # let other tasks run between frames
        if not self._running:
            return None
        if self._frames:
            self.frames_read += 1
            return self._frames.popleft()
        if self.fill_silence:
            np = np_module()
            self.frames_read += 1
            return np.zeros(self.frame_samples, dtype=np.int16)
        self._eof = True
        return None


# ---------------------------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------------------------
class AudioSink(ABC):
    """Plays clips one at a time; `stop()` interrupts the current clip immediately (barge-in)."""

    name = "sink"

    def __init__(self) -> None:
        self.device_name: str | None = None
        self._lock = asyncio.Lock()
        self._stop_event = threading.Event()
        self._playing = False

    @property
    def playing(self) -> bool:
        return self._playing

    def devices_changed(self) -> None:  # noqa: B027 - most sinks keep no device to forget
        """PortAudio re-scanned the devices: forget any device chosen from the old list."""

    async def play(self, clip: AudioClip) -> bool:
        """Play `clip` to the end. Returns False when it was stopped/interrupted."""
        if clip is None or len(clip.samples) == 0:
            return True
        async with self._lock:
            self._stop_event.clear()
            self._playing = True
            try:
                completed = await self._play(clip)
            finally:
                self._playing = False
            return bool(completed) and not self._stop_event.is_set()

    async def play_bytes(self, data: bytes, *, sample_rate: int | None = None) -> bool:
        """Play encoded audio (mp3/wav/flac/ogg) — decoded with miniaudio."""
        clip = await asyncio.to_thread(decode_audio_bytes, data, sample_rate=sample_rate)
        return await self.play(clip)

    def stop(self) -> None:
        """Stop the clip that is playing now (thread-safe, returns immediately)."""
        if self._playing:
            self._stop_event.set()

    async def close(self) -> None:
        self.stop()

    @abstractmethod
    async def _play(self, clip: AudioClip) -> bool: ...


class SoundDeviceSink(AudioSink):
    """Playback with sounddevice (PortAudio). A stream is opened per clip, so no audio stream
    stays open while JARVIS is silent (an idle open stream keeps Windows from sleeping)."""

    name = "sounddevice"

    def __init__(self, device: str | int | None = None, *, latency: str | float = "low") -> None:
        super().__init__()
        self.device_spec = device
        self.device: AudioDevice | None = None
        self._latency = latency

    def resolve(self) -> AudioDevice:
        self.device = resolve_device(self.device_spec, "output")
        self.device_name = self.device.name
        return self.device

    def devices_changed(self) -> None:
        self.device = None          # device indexes can move after a re-scan

    async def _play(self, clip: AudioClip) -> bool:
        sd = _sounddevice()
        device = self.device if self.device is not None else await asyncio.to_thread(self.resolve)
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        try:
            stream, data = await asyncio.to_thread(self._open, sd, device, clip, loop, finished)
        except AudioError:
            self.device = None  # re-resolve next time (headset unplugged, default device changed)
            raise
        try:
            await asyncio.to_thread(stream.start)
            budget = len(data) / float(stream.samplerate) + 3.0
            try:
                await asyncio.wait_for(finished.wait(), timeout=budget)
            except TimeoutError:
                log.warning("Playback did not finish in time on %s", device.name)
                return False
        finally:
            # By the time `finished` fires the buffers have played out, so aborting is instant and
            # loses nothing; on cancellation (barge-in) it silences the device at once.
            _close_stream(stream, abort=True)
            _stream_closed()
        return not self._stop_event.is_set()

    def _open(self, sd: Any, device: AudioDevice, clip: AudioClip, loop: asyncio.AbstractEventLoop, finished: asyncio.Event):
        rate = clip.sample_rate
        try:
            with _DEVICE_LOCK:
                sd.check_output_settings(device=device.index, channels=1, dtype="float32", samplerate=rate)
        except Exception:
            rate = int(device.default_samplerate or 48_000)
        data = clip.resampled(rate).as_float32() if rate != clip.sample_rate else clip.as_float32()
        position = 0
        stop_event = self._stop_event

        def callback(outdata: Any, frames: int, time_info: Any, status: Any) -> None:
            nonlocal position
            if stop_event.is_set():
                outdata.fill(0)
                raise sd.CallbackAbort
            chunk = data[position : position + frames]
            n = len(chunk)
            outdata[:n, 0] = chunk
            if n < frames:
                outdata[n:, :] = 0
                position += n
                raise sd.CallbackStop
            position += n

        def done() -> None:
            try:
                loop.call_soon_threadsafe(finished.set)
            except RuntimeError:  # pragma: no cover - loop closed
                pass

        try:
            with _DEVICE_LOCK:
                stream = sd.OutputStream(
                    device=device.index,
                    samplerate=rate,
                    channels=1,
                    dtype="float32",
                    latency=self._latency,
                    callback=callback,
                    finished_callback=done,
                )
                _stream_opened()
        except Exception as exc:
            raise AudioDeviceError(f"Could not open audio output {device.name!r}: {exc}") from exc
        return stream, data


class MiniaudioSink(AudioSink):
    """Playback with miniaudio's own device layer (no PortAudio needed)."""

    name = "miniaudio"

    def __init__(self, device: str | None = None, *, buffer_ms: int = 60) -> None:
        super().__init__()
        self.device_spec = (device or "").strip()
        self.buffer_ms = int(buffer_ms)
        self._device_id: Any = None

    def resolve(self) -> str:
        """Find the playback device; raises AudioDeviceError when there is none."""
        ma = _miniaudio()
        try:
            playbacks = ma.Devices().get_playbacks()
        except Exception as exc:
            raise AudioDeviceError(f"Cannot list playback devices: {exc}") from exc
        real = [d for d in playbacks if not str(d.get("name", "")).startswith("Discard all samples")]
        if not real:
            raise AudioDeviceError("No audio output device found", missing=True)
        chosen = None
        if self.device_spec:
            low = self.device_spec.lower()
            chosen = next((d for d in real if low in str(d["name"]).lower()), None)
        chosen = chosen or real[0]
        self._device_id = chosen["id"] if self.device_spec else None
        self.device_name = str(chosen["name"])
        return self.device_name

    async def _play(self, clip: AudioClip) -> bool:
        ma = _miniaudio()
        if self.device_name is None:
            await asyncio.to_thread(self.resolve)
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        samples = clip.samples
        stop_event = self._stop_event

        def generator():
            required = yield b""
            position = 0
            try:
                while position < len(samples) and not stop_event.is_set():
                    chunk = samples[position : position + required]
                    position += len(chunk)
                    required = yield chunk.tobytes()
            finally:
                try:
                    loop.call_soon_threadsafe(finished.set)
                except RuntimeError:  # pragma: no cover
                    pass

        def open_device():
            return ma.PlaybackDevice(
                output_format=ma.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=clip.sample_rate,
                buffersize_msec=self.buffer_ms,
                device_id=self._device_id,
            )

        try:
            device = await asyncio.to_thread(open_device)
        except Exception as exc:
            raise AudioDeviceError(f"Could not open audio output: {exc}") from exc
        try:
            gen = generator()
            next(gen)
            device.start(gen)
            try:
                await asyncio.wait_for(finished.wait(), timeout=clip.duration_s + 3.0)
            except TimeoutError:
                return False
            if not stop_event.is_set():
                await asyncio.sleep(2 * self.buffer_ms / 1000.0)  # let the device play out its buffer
        finally:
            try:
                device.close()  # synchronous on purpose: must also run when the task is cancelled
            except Exception:  # pragma: no cover - backend specific
                log.debug("miniaudio close failed", exc_info=True)
        return not stop_event.is_set()


class NullSink(AudioSink):
    """Plays nothing; records clips. `realtime=True` simulates playback time × `speed`."""

    name = "null"

    HISTORY = 200  # clips kept in `played` / `interrupted` (this sink also stands in for a missing device)

    def __init__(self, *, realtime: bool = False, speed: float = 1.0, reason: str = "") -> None:
        super().__init__()
        self.realtime = realtime
        self.speed = max(0.0, float(speed))
        self.reason = reason
        self.played: list[AudioClip] = []
        self.interrupted: list[AudioClip] = []

    def _remember(self, clips: list[AudioClip], clip: AudioClip) -> None:
        clips.append(clip)
        if len(clips) > self.HISTORY:
            del clips[: len(clips) - self.HISTORY]

    async def _play(self, clip: AudioClip) -> bool:
        self._remember(self.played, clip)
        try:
            if not self.realtime:
                await asyncio.sleep(0)
                return not self._stop_event.is_set()
            deadline = time.monotonic() + clip.duration_s * self.speed
            while time.monotonic() < deadline:
                if self._stop_event.is_set():
                    self._remember(self.interrupted, clip)
                    return False
                await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        except asyncio.CancelledError:  # the speaking task was cancelled mid-clip (barge-in)
            self._remember(self.interrupted, clip)
            raise
        return not self._stop_event.is_set()


def create_sink(output_device: str | int | None = "") -> tuple[AudioSink, str]:
    """Best available playback sink for the configured device, with a reason string."""
    problems: list[str] = []
    try:
        sink = SoundDeviceSink(output_device or None)
        device = sink.resolve()
        return sink, f"sounddevice output: {device.name}"
    except AudioError as exc:
        problems.append(f"sounddevice: {exc.message}")
    try:
        msink = MiniaudioSink(str(output_device or ""))
        name = msink.resolve()
        return msink, f"miniaudio output: {name}"
    except AudioError as exc:
        problems.append(f"miniaudio: {exc.message}")
    reason = "No audio output device (" + "; ".join(problems) + ")"
    return NullSink(reason=reason), reason
