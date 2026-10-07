"""Speech-to-text engines.

* `FasterWhisperSTT` — local Whisper via CTranslate2 (default model "small", int8). Runs on a
  dedicated worker thread so the event loop never blocks. Speed choices: greedy decoding
  (beam 1), no timestamps, no previous-text conditioning, no internal VAD (we end-point
  ourselves) and one temperature fallback at most. With `stt_language = "auto"` the language
  is detected first and restricted to Hebrew/English (Whisper sometimes guesses Arabic or
  Yiddish for short Hebrew), which costs one extra encoder pass; pinning "he" or "en" in the
  settings saves it. Typical CPU timings for a 3 s command (8-thread desktop CPU, int8):
  "base" ≈ 0.3 s, "small" ≈ 0.6-1.0 s. The model is loaded and warmed up in the background at
  start-up, first trying the local cache (no network) and downloading into
  `<cache>/models/whisper` only when missing. CUDA is used when available, with an automatic
  fall back to CPU when the CUDA libraries are missing.
* `OpenAISTT` — cloud fallback through the OpenAI SDK ("gpt-4o-mini-transcribe", falling back
  to "whisper-1"), sending a 16 kHz WAV built in memory.
* `NullSTT` — no recognition (reports why).
* `FallbackSTT` — a primary engine with a backup used when the primary cannot load or fails.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import logging
import math
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.common.errors import VoiceError
from jarvis.common.text import detect_language, normalize
from jarvis.voice.audio import SAMPLE_RATE, np_module, to_float32, wav_bytes
from jarvis.voice.base import Transcript

if TYPE_CHECKING:  # pragma: no cover
    from jarvis.config.paths import AppPaths
    from jarvis.config.settings import VoiceSettings
    from jarvis.security.secrets import SecretStore

log = logging.getLogger(__name__)

# Biases recognition towards names JARVIS hears all the time (Whisper "initial prompt").
PROMPTS = {
    "he": "ג'ארוויס, כרום, יוטיוב, ספוטיפיי, וואטסאפ, וורד, אקסל. JARVIS, Chrome, YouTube.",
    "en": "JARVIS, Chrome, YouTube, Spotify, WhatsApp, Visual Studio Code, Word, Excel.",
    "auto": "JARVIS, ג'ארוויס, Chrome, כרום, YouTube, יוטיוב.",
}

# Phrases Whisper is known to invent for noise/silence.
_HALLUCINATIONS = {
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "you",
    "bye",
    "subtitles by the amara.org community",
    "תודה",
    "תודה רבה",
    "תודה שצפיתם",
    "תודה רבה שצפיתם",
    "כתוביות",
}
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


class STTError(VoiceError):
    code = "stt_error"


class STTUnavailable(STTError):
    code = "stt_unavailable"


def _short(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return text.splitlines()[0][:240]


def _have(*modules: str) -> bool:
    try:
        return all(importlib.util.find_spec(m) is not None for m in modules)
    except (ImportError, ValueError):
        return False


def _norm_lang(language: str | None) -> str:
    return language if language in ("he", "en") else "auto"


def is_noise_transcript(text: str, confidence: float | None = None) -> bool:
    """True for empty/punctuation-only text and for Whisper's classic hallucinations."""
    words = _WORD_RE.findall(normalize(text or ""))
    if not words:
        return True
    if len("".join(words)) < 2:
        return True
    phrase = " ".join(words)
    return phrase in _HALLUCINATIONS and (confidence is None or confidence < 0.6)


def echoes_prompt(text: str, prompt: str | None, confidence: float | None = None) -> bool:
    """True when Whisper just repeated (part of) the initial prompt instead of hearing speech."""
    if not prompt:
        return False
    words = _WORD_RE.findall(normalize(text or ""))
    if not words:
        return False
    vocabulary = set(_WORD_RE.findall(normalize(prompt)))
    if not all(w in vocabulary for w in words):
        return False
    return len(words) >= 3 or confidence is None or confidence < 0.5


class STTEngine(ABC):
    """`await transcribe(int16 16 kHz audio, "auto"|"he"|"en") -> Transcript`."""

    name = "none"
    supports_partials = False

    def __init__(self) -> None:
        self.reason = ""
        self.last_language = "he"

    @property
    def ready(self) -> bool:
        """Model loaded and usable right now."""
        return False

    @property
    def loading(self) -> bool:
        return False

    def load(self) -> None:
        """Load the model (may download it). Blocking — call from a worker thread."""
        return None

    async def warm_up(self) -> None:
        """Load in the background (start-up) so the first command is fast."""
        await asyncio.to_thread(self.load)

    @abstractmethod
    async def transcribe(self, audio: Any, language: str = "auto", *, hint: bool = True, partial: bool = False) -> Transcript:
        """Transcribe int16 mono 16 kHz audio. `hint=False` disables the name-biasing prompt;
        `partial=True` asks for a cheap interim result (may return empty text)."""

    def close(self) -> None:
        return None


class NullSTT(STTEngine):
    name = "none"

    def __init__(self, reason: str = "") -> None:
        super().__init__()
        self.reason = reason or "Speech recognition is not available."

    def load(self) -> None:
        raise STTUnavailable(self.reason)

    async def transcribe(self, audio: Any, language: str = "auto", *, hint: bool = True, partial: bool = False) -> Transcript:
        raise STTUnavailable(self.reason)


class FasterWhisperSTT(STTEngine):
    name = "faster_whisper"
    supports_partials = True
    retry_load_after_s = 300.0

    def __init__(
        self,
        model_size: str = "small",
        *,
        device: str = "auto",
        compute_type: str = "int8",
        download_root: Path | str | None = None,
        cpu_threads: int = 0,
        beam_size: int = 1,
        prompts: dict[str, str] | None = None,
        allow_download: bool = True,
    ) -> None:
        super().__init__()
        self.model_size = model_size or "small"
        self.device = device or "auto"
        self.compute_type = compute_type or "int8"
        self.download_root = Path(download_root) if download_root else None
        self.cpu_threads = int(cpu_threads) or max(2, min(8, (os.cpu_count() or 4) // 2))
        self.beam_size = max(1, min(5, int(beam_size)))
        self.prompts = dict(PROMPTS if prompts is None else prompts)
        self.allow_download = allow_download
        self.device_used: str | None = None
        self.compute_type_used: str | None = None
        self.load_seconds: float | None = None
        self._model: Any = None
        self._loading = False
        self._load_error: str | None = None
        self._load_failed_at = 0.0
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="jarvis-stt")

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def loading(self) -> bool:
        return self._loading

    def _resolve_device(self) -> str:
        if self.device in ("cpu", "cuda"):
            return self.device
        try:
            import ctranslate2

            return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            return "cpu"

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            if self._load_error and time.monotonic() - self._load_failed_at < self.retry_load_after_s:
                raise STTUnavailable(self._load_error)
            self._loading = True
            try:
                self._model = self._load_model()
                self._load_error = None
            except STTError as exc:
                self._load_error, self._load_failed_at = exc.message, time.monotonic()
                raise
            except Exception as exc:  # pragma: no cover - defensive
                self._load_error, self._load_failed_at = _short(exc), time.monotonic()
                raise STTUnavailable(self._load_error) from exc
            finally:
                self._loading = False

    def _load_model(self) -> Any:
        np = np_module()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTUnavailable(f"faster-whisper is not installed: {exc}") from exc
        device = self._resolve_device()
        attempts = [(device, self.compute_type)]
        if device == "cuda":
            attempts.append(("cpu", "int8"))
        root = str(self.download_root) if self.download_root else None
        errors: list[str] = []
        for dev, compute in attempts:
            started = time.monotonic()
            model = None
            for local_only in (True, False) if self.allow_download else (True,):  # cache first: no network at start-up
                if not local_only:
                    log.info("Downloading the Whisper model '%s' into %s (first use)…", self.model_size, root or "the HF cache")
                try:
                    model = WhisperModel(
                        self.model_size,
                        device=dev,
                        compute_type=compute,
                        cpu_threads=self.cpu_threads,
                        download_root=root,
                        local_files_only=local_only,
                    )
                    break
                except Exception as exc:
                    errors.append(f"{dev}/{compute}{' (cache)' if local_only else ''}: {_short(exc)}")
                    log.debug("faster-whisper load attempt failed: %s", errors[-1])
            if model is None:
                continue
            try:
                # One tiny inference: surfaces missing CUDA libraries now (not on the first
                # command) and pays the one-off kernel/allocator warm-up cost up front.
                segments, _info = model.transcribe(
                    np.zeros(SAMPLE_RATE // 2, dtype=np.float32),
                    language="en",
                    beam_size=1,
                    without_timestamps=True,
                    max_new_tokens=4,
                    condition_on_previous_text=False,
                )
                list(segments)
            except Exception as exc:
                errors.append(f"{dev}/{compute} warm-up: {_short(exc)}")
                log.warning("faster-whisper failed on %s (%s)", dev, errors[-1])
                continue
            self.device_used, self.compute_type_used = dev, compute
            self.load_seconds = time.monotonic() - started
            self.reason = f"faster-whisper '{self.model_size}' on {dev} ({compute})"
            log.info("Loaded %s in %.1f s", self.reason, self.load_seconds)
            return model
        raise STTUnavailable(f"faster-whisper could not load model '{self.model_size}': {errors[-1] if errors else 'unknown error'}")

    def prompt_for(self, language: str) -> str | None:
        return self.prompts.get(language) or self.prompts.get("auto")

    async def transcribe(self, audio: Any, language: str = "auto", *, hint: bool = True, partial: bool = False) -> Transcript:
        if self._model is None:
            if partial:
                return Transcript(text="", language=self.last_language, final=False)
            await asyncio.to_thread(self.load)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._transcribe_sync, audio, _norm_lang(language), hint, partial)

    def _detect(self, model: Any, audio: Any) -> tuple[str, float | None]:
        try:
            language, probability, all_probs = model.detect_language(audio=audio)
        except Exception as exc:  # pragma: no cover - depends on the model
            log.debug("Language detection failed: %s", exc)
            return self.last_language, None
        if language in ("he", "en"):
            return language, float(probability)
        probs = dict(all_probs or [])
        he, en = float(probs.get("he", 0.0)), float(probs.get("en", 0.0))
        if he == 0.0 and en == 0.0:
            return self.last_language, None
        return ("he" if he >= en else "en"), max(he, en)

    def _transcribe_sync(self, audio: Any, language: str, hint: bool, partial: bool) -> Transcript:
        model = self._model
        samples = to_float32(audio)
        duration = len(samples) / float(SAMPLE_RATE)
        if len(samples) < SAMPLE_RATE // 10:
            return Transcript(text="", language=self.last_language, final=not partial, duration_s=duration)
        lang = language if language in ("he", "en") else None
        if not getattr(model.model, "is_multilingual", True):
            lang = "en"
        if lang is None:
            lang = self.last_language if partial else self._detect(model, samples)[0]
        prompt = self.prompt_for(lang) if hint else None
        segments, _info = model.transcribe(
            samples,
            language=lang,
            task="transcribe",
            beam_size=self.beam_size,
            best_of=1,
            temperature=0.0 if partial else (0.0, 0.4),
            condition_on_previous_text=False,
            vad_filter=False,
            initial_prompt=prompt,
            without_timestamps=True,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
        texts: list[str] = []
        weighted = 0.0
        weight = 0
        for seg in segments:
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8:
                continue
            text = seg.text.strip()
            if not text:
                continue
            texts.append(text)
            n = max(1, len(seg.tokens))
            weighted += seg.avg_logprob * n
            weight += n
        text = " ".join(texts).strip()
        confidence = math.exp(weighted / weight) if weight else None
        if echoes_prompt(text, prompt, confidence):
            log.debug("Dropped a transcript that only repeats the prompt: %r", text)
            text = ""
        if text and not partial:
            self.last_language = lang
        return Transcript(text=text, language=lang, final=not partial, confidence=confidence, duration_s=duration)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._model = None


class OpenAISTT(STTEngine):
    name = "openai"

    def __init__(
        self,
        api_key: str | Callable[[], str | None] | None,
        *,
        model: str = "gpt-4o-mini-transcribe",
        fallback_model: str = "whisper-1",
        timeout_s: float = 30.0,
        base_url: str | None = None,
        prompts: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self.model = model
        self.fallback_model = fallback_model
        self.timeout_s = timeout_s
        self.base_url = base_url or None
        self.prompts = dict(PROMPTS if prompts is None else prompts)
        self._client: Any = None
        self._client_key: str | None = None
        self.reason = f"OpenAI transcription ({model})"

    def _key(self) -> str | None:
        key = self._api_key() if callable(self._api_key) else self._api_key
        return key or None

    @property
    def ready(self) -> bool:
        return _have("openai") and bool(self._key())

    def load(self) -> None:
        if not _have("openai"):
            raise STTUnavailable("the openai package is not installed")
        if not self._key():
            raise STTUnavailable("OPENAI_API_KEY is not set")

    def _get_client(self) -> Any:
        import openai

        key = self._key()
        if not key:
            raise STTUnavailable("OPENAI_API_KEY is not set")
        if self._client is None or self._client_key != key:
            self._client = openai.AsyncOpenAI(api_key=key, timeout=self.timeout_s, max_retries=1, base_url=self.base_url)
            self._client_key = key
        return self._client

    async def transcribe(self, audio: Any, language: str = "auto", *, hint: bool = True, partial: bool = False) -> Transcript:
        if partial:  # interim results would cost an API call each
            return Transcript(text="", language=self.last_language, final=False)
        self.load()
        client = self._get_client()
        samples = np_module().asarray(audio)
        duration = len(samples) / float(SAMPLE_RATE)
        lang = _norm_lang(language)
        data = wav_bytes(samples, SAMPLE_RATE)
        kwargs: dict[str, Any] = {"file": ("speech.wav", data, "audio/wav")}
        if lang != "auto":
            kwargs["language"] = lang
        if hint:
            kwargs["prompt"] = self.prompts.get(lang) or self.prompts.get("auto", "")
        last: Exception | None = None
        result: Any = None
        for model in dict.fromkeys(m for m in (self.model, self.fallback_model) if m):
            try:
                result = await client.audio.transcriptions.create(model=model, **kwargs)
                break
            except Exception as exc:
                last = exc
                status = getattr(exc, "status_code", None)
                if status not in (400, 403, 404):  # only a model problem is worth a second model
                    break
        if result is None:
            raise STTError(f"OpenAI transcription failed: {_short(last) if last else 'no result'}")
        text = (getattr(result, "text", None) or (result if isinstance(result, str) else "")).strip()
        detected = lang if lang != "auto" else detect_language(text, default=self.last_language)
        if text:
            self.last_language = detected
        return Transcript(text=text, language=detected, final=True, confidence=None, duration_s=duration)


class FallbackSTT(STTEngine):
    """Uses `primary`; switches to `backup` when the primary cannot load or fails at runtime."""

    def __init__(self, primary: STTEngine, backup: STTEngine) -> None:
        super().__init__()
        self.primary = primary
        self.backup = backup
        self._active = primary
        self.primary_error: str | None = None

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._active.name

    @property
    def supports_partials(self) -> bool:  # type: ignore[override]
        return self._active.supports_partials

    @property
    def ready(self) -> bool:
        return self._active.ready

    @property
    def loading(self) -> bool:
        return self._active.loading

    @property
    def last_language(self) -> str:  # type: ignore[override]
        return self._active.last_language

    @last_language.setter
    def last_language(self, value: str) -> None:
        for engine in (getattr(self, "primary", None), getattr(self, "backup", None)):
            if engine is not None:
                engine.last_language = value

    def load(self) -> None:
        try:
            self.primary.load()
            self._active = self.primary
            self.reason = self.primary.reason
            return
        except Exception as exc:
            self.primary_error = _short(exc)
            log.warning("%s unavailable (%s); using %s", self.primary.name, self.primary_error, self.backup.name)
        self.backup.load()
        self._active = self.backup
        self.reason = f"{self.backup.reason or self.backup.name} (because {self.primary.name} failed: {self.primary_error})"

    async def transcribe(self, audio: Any, language: str = "auto", *, hint: bool = True, partial: bool = False) -> Transcript:
        if not self._active.ready and not partial:
            await asyncio.to_thread(self.load)
        try:
            return await self._active.transcribe(audio, language, hint=hint, partial=partial)
        except Exception as exc:
            if self._active is not self.primary or partial or not self.backup.ready:
                raise
            log.warning("%s failed (%s); retrying with %s", self.primary.name, _short(exc), self.backup.name)
            return await self.backup.transcribe(audio, language, hint=hint, partial=partial)

    def close(self) -> None:
        self.primary.close()
        self.backup.close()


def _secret(secrets: SecretStore | None, name: str) -> str | None:
    if secrets is None:
        return None
    try:
        return secrets.get(name)
    except Exception:  # pragma: no cover - keyring backend errors
        return None


def create_stt_engine(settings: VoiceSettings, paths: AppPaths, secrets: SecretStore | None) -> tuple[STTEngine, str]:
    """Pick the STT engine for `settings.stt_engine` (auto | faster_whisper | openai | none).

    Construction is cheap (nothing is loaded); `load()`/`warm_up()` does the heavy work and a
    load failure of faster-whisper falls back to OpenAI when a key is configured.
    """
    choice = settings.stt_engine
    if choice == "none":
        engine = NullSTT("Speech recognition is turned off in settings.")
        return engine, engine.reason
    has_fw = _have("faster_whisper", "numpy")
    has_openai_pkg = _have("openai")
    has_key = bool(_secret(secrets, "OPENAI_API_KEY"))

    def local() -> FasterWhisperSTT:
        return FasterWhisperSTT(
            settings.stt_model,
            device=settings.stt_device,
            compute_type=settings.stt_compute_type,
            download_root=paths.models / "whisper",
        )

    def cloud() -> OpenAISTT:
        return OpenAISTT(lambda: _secret(secrets, "OPENAI_API_KEY"))

    cloud_why = "the openai package is not installed" if not has_openai_pkg else "no OPENAI_API_KEY"
    if choice in ("auto", "faster_whisper"):
        if has_fw:
            if has_openai_pkg and has_key:
                return FallbackSTT(local(), cloud()), f"faster-whisper '{settings.stt_model}' (OpenAI as backup)"
            return local(), f"faster-whisper '{settings.stt_model}' (no cloud backup: {cloud_why})"
        if has_openai_pkg and has_key:
            return cloud(), "OpenAI transcription (faster-whisper is not installed)"
        engine = NullSTT(f"faster-whisper is not installed and OpenAI is unavailable ({cloud_why}).")
        return engine, engine.reason
    # choice == "openai"
    if has_openai_pkg and has_key:
        if has_fw:
            return FallbackSTT(cloud(), local()), f"OpenAI transcription (faster-whisper '{settings.stt_model}' as backup)"
        return cloud(), "OpenAI transcription"
    if has_fw:
        return local(), f"faster-whisper '{settings.stt_model}' (OpenAI unavailable: {cloud_why})"
    engine = NullSTT(f"OpenAI transcription is unavailable ({cloud_why}) and faster-whisper is not installed.")
    return engine, engine.reason
