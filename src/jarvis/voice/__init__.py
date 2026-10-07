"""Voice subsystem: wake word → listen (VAD end-pointing) → STT → command handler → TTS.

`import jarvis.voice` is light: the contracts in `base` are imported eagerly, everything else
on first attribute access, and the heavy libraries (numpy, sounddevice, openwakeword,
faster-whisper, edge-tts, pyttsx3, miniaudio, webrtcvad) only when an engine needs them. Without
the voice extra installed `VoiceService.start()` reports state "disabled" with the reason.

Entry points: `VoiceService` (what the app uses), `VoicePipeline` (the state machine),
the engine factories (`create_wake_engine`, `create_stt_engine`, `create_tts_engine`,
`create_sink`) and the self-test (`python -m jarvis.voice.selftest`).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from jarvis.voice.base import (
    ACK_PHRASES,
    CommandHandler,
    NullVoiceEvents,
    SpeechReply,
    Transcript,
    VoiceEventSink,
    VoiceState,
)

_LAZY: dict[str, str] = {
    # service / pipeline
    "VoiceService": "service",
    "VoicePipeline": "pipeline",
    "PipelineOptions": "pipeline",
    # audio I/O
    "AudioClip": "audio",
    "AudioDevice": "audio",
    "AudioError": "audio",
    "AudioDeviceError": "audio",
    "AudioSource": "audio",
    "AudioSink": "audio",
    "MicrophoneSource": "audio",
    "ArraySource": "audio",
    "SoundDeviceSink": "audio",
    "MiniaudioSink": "audio",
    "NullSink": "audio",
    "create_sink": "audio",
    "list_devices": "audio",
    "rms_level": "audio",
    "resample": "audio",
    "read_wav": "audio",
    "write_wav": "audio",
    "SAMPLE_RATE": "audio",
    "FRAME_SAMPLES": "audio",
    # voice activity / end-pointing
    "VoiceActivityDetector": "vad",
    "Endpointer": "vad",
    "EndpointConfig": "vad",
    "EndpointEvent": "vad",
    "Utterance": "vad",
    # wake word
    "WakeWordEngine": "wakeword",
    "WakeDetection": "wakeword",
    "OpenWakeWordEngine": "wakeword",
    "PorcupineEngine": "wakeword",
    "TranscriptWakeEngine": "wakeword",
    "NullWakeEngine": "wakeword",
    "create_wake_engine": "wakeword",
    "match_wake_phrase": "wakeword",
    # speech to text
    "STTEngine": "stt",
    "FasterWhisperSTT": "stt",
    "OpenAISTT": "stt",
    "FallbackSTT": "stt",
    "NullSTT": "stt",
    "create_stt_engine": "stt",
    # text to speech
    "TTSEngine": "tts",
    "EdgeTTS": "tts",
    "SapiTTS": "tts",
    "FallbackTTS": "tts",
    "NullTTS": "tts",
    "PhraseCache": "tts",
    "create_tts_engine": "tts",
    # self-test
    "run_selftest": "selftest",
}

if TYPE_CHECKING:  # pragma: no cover
    from jarvis.voice.audio import (
        FRAME_SAMPLES,
        SAMPLE_RATE,
        ArraySource,
        AudioClip,
        AudioDevice,
        AudioDeviceError,
        AudioError,
        AudioSink,
        AudioSource,
        MicrophoneSource,
        MiniaudioSink,
        NullSink,
        SoundDeviceSink,
        create_sink,
        list_devices,
        read_wav,
        resample,
        rms_level,
        write_wav,
    )
    from jarvis.voice.pipeline import PipelineOptions, VoicePipeline
    from jarvis.voice.selftest import run_selftest
    from jarvis.voice.service import VoiceService
    from jarvis.voice.stt import (
        FallbackSTT,
        FasterWhisperSTT,
        NullSTT,
        OpenAISTT,
        STTEngine,
        create_stt_engine,
    )
    from jarvis.voice.tts import (
        EdgeTTS,
        FallbackTTS,
        NullTTS,
        PhraseCache,
        SapiTTS,
        TTSEngine,
        create_tts_engine,
    )
    from jarvis.voice.vad import EndpointConfig, Endpointer, EndpointEvent, Utterance, VoiceActivityDetector
    from jarvis.voice.wakeword import (
        NullWakeEngine,
        OpenWakeWordEngine,
        PorcupineEngine,
        TranscriptWakeEngine,
        WakeDetection,
        WakeWordEngine,
        create_wake_engine,
        match_wake_phrase,
    )


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'jarvis.voice' has no attribute {name!r}")
    value = getattr(importlib.import_module(f"jarvis.voice.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "ACK_PHRASES",
    "FRAME_SAMPLES",
    "SAMPLE_RATE",
    "ArraySource",
    "AudioClip",
    "AudioDevice",
    "AudioDeviceError",
    "AudioError",
    "AudioSink",
    "AudioSource",
    "CommandHandler",
    "EdgeTTS",
    "EndpointConfig",
    "EndpointEvent",
    "Endpointer",
    "FallbackSTT",
    "FallbackTTS",
    "FasterWhisperSTT",
    "MicrophoneSource",
    "MiniaudioSink",
    "NullSTT",
    "NullSink",
    "NullTTS",
    "NullVoiceEvents",
    "NullWakeEngine",
    "OpenAISTT",
    "OpenWakeWordEngine",
    "PhraseCache",
    "PipelineOptions",
    "PorcupineEngine",
    "STTEngine",
    "SapiTTS",
    "SoundDeviceSink",
    "SpeechReply",
    "TTSEngine",
    "Transcript",
    "TranscriptWakeEngine",
    "Utterance",
    "VoiceActivityDetector",
    "VoiceEventSink",
    "VoicePipeline",
    "VoiceService",
    "VoiceState",
    "WakeDetection",
    "WakeWordEngine",
    "create_sink",
    "create_stt_engine",
    "create_tts_engine",
    "create_wake_engine",
    "list_devices",
    "match_wake_phrase",
    "read_wav",
    "resample",
    "rms_level",
    "run_selftest",
    "write_wav",
]
