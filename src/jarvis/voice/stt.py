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
  `<cache>/models/whisper` only when missing.
  On an NVIDIA graphics card a larger model runs instead (`stt_model_gpu`, "large-v3-turbo"
  int8_float16: far better Hebrew, and faster than "small" on the CPU). It needs NVIDIA's CUDA 12
  libraries, which `pip install jarvis-assistant[gpu]` puts in site-packages/nvidia (setup.ps1 does
  it when it finds such a card); `use_nvidia_dlls` lets CTranslate2 find them. Start-up never
  waits for the card or a download: the CPU model serves first, and the larger one takes over in
  the background once it is loaded and warmed up on the card (about 10 s, and up to a minute the
  first time, while the driver prepares its code for the card), downloaded first when it is
  missing. When the graphics card fails (missing libraries, or out of memory while a game or the
  computer-control model fills it), the CPU model takes over.
* `OpenAISTT` — cloud fallback through the OpenAI SDK ("gpt-4o-mini-transcribe", falling back
  to "whisper-1"), sending a 16 kHz WAV built in memory.
* `NullSTT` — no recognition (reports why).
* `FallbackSTT` — a primary engine with a backup used when the primary cannot load or fails.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import importlib.util
import logging
import math
import os
import re
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
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
    "he": "ג'ארוויס, גרוק, כרום, יוטיוב, ספוטיפיי, וואטסאפ, וורד, אקסל. JARVIS, Chrome, YouTube.",
    "en": "JARVIS, Grok, Chrome, YouTube, Spotify, WhatsApp, Visual Studio Code, Word, Excel.",
    "auto": "JARVIS, ג'ארוויס, גרוק, Chrome, כרום, YouTube, יוטיוב.",
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


# What CTranslate2 loads to run Whisper on an NVIDIA card (CUDA 12: cuBLAS, and cuDNN 9 for the convolutions).
CUDA_DLLS = ("cublas64_12.dll", "cudnn64_9.dll")
_dll_dirs_added: set[str] = set()
_dll_handles: list[Any] = []


def nvidia_dll_dirs() -> list[Path]:
    """Folders of NVIDIA's CUDA libraries installed as Python packages (nvidia-cublas-cu12, nvidia-cudnn-cu12),
    whose Windows wheels keep the DLLs in site-packages/nvidia/<library>/bin."""
    try:
        spec = importlib.util.find_spec("nvidia")
    except (ImportError, ValueError):
        return []
    found: list[Path] = []
    for root in (spec.submodule_search_locations or []) if spec is not None else []:
        with contextlib.suppress(OSError):
            found += [sub / "bin" for sub in sorted(Path(root).iterdir()) if (sub / "bin").is_dir()]
    return found


def use_nvidia_dlls() -> list[Path]:
    """Let CTranslate2 find those libraries (Windows). It loads them by name the first time a model runs on the card,
    so their folders go first on PATH, and into Python's own DLL search as well."""
    if sys.platform != "win32":
        return []
    # The driver compiles CTranslate2's code for newer cards on first use (about a minute) and keeps it in its cache;
    # a cache too small for it (and for the computer-control model's) would mean that minute again.
    os.environ.setdefault("CUDA_CACHE_MAXSIZE", str(4 * 1024**3))
    dirs = nvidia_dll_dirs()
    current = os.environ.get("PATH", "")
    missing = [str(d) for d in dirs if str(d) not in current.split(os.pathsep)]
    if missing:
        os.environ["PATH"] = os.pathsep.join([*missing, current])
    for d in map(str, dirs):
        if d not in _dll_dirs_added and hasattr(os, "add_dll_directory"):
            with contextlib.suppress(OSError):
                _dll_handles.append(os.add_dll_directory(d))
            _dll_dirs_added.add(d)
    return dirs


def _not_downloaded(exc: BaseException) -> bool:
    """The model is not (all) on disk: not in the cache yet, or a download that was cut off half way."""
    return isinstance(exc, FileNotFoundError) or "EntryNotFound" in type(exc).__name__ or "Unable to open file" in str(exc)


def cuda_libraries_problem() -> str | None:
    """What is missing for Whisper on the graphics card (Windows), or None. Elsewhere the load itself tells."""
    if sys.platform != "win32":
        return None
    folders = [*map(str, nvidia_dll_dirs()), *os.environ.get("PATH", "").split(os.pathsep)]
    missing = [dll for dll in CUDA_DLLS if not any(f and os.path.isfile(os.path.join(f, dll)) for f in folders)]
    if not missing:
        return None
    return f"{' and '.join(missing)} not found (install them with: pip install jarvis-assistant[gpu])"


def gpu_note(device: str = "auto", gpu_model: str = "") -> str | None:
    """For `jarvis check`: where speech recognition runs when there is an NVIDIA graphics card (None without one)."""
    if device == "cpu" or not _have("ctranslate2"):
        return None
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() <= 0:
            return None
    except Exception:
        return None
    problem = cuda_libraries_problem()
    if problem:
        return f"speech recognition stays on the CPU: {problem}"
    return "speech recognition runs on the graphics card" + (f" ('{gpu_model}')" if gpu_model else "")


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

    def describe(self) -> str:
        """What recognizes speech right now. It can change after start-up: a better model that finished
        loading, or the CPU instead of the graphics card."""
        return self.reason or self.name

    def settle(self, timeout: float | None = None) -> bool:
        """Wait for a better model that is still loading in the background. True when none is left."""
        return True

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


@dataclass
class _Loaded:
    model: Any
    device: str
    compute_type: str
    name: str
    seconds: float


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
        gpu_model: str = "",
        gpu_compute_type: str = "int8_float16",
        download_root: Path | str | None = None,
        cpu_threads: int = 0,
        beam_size: int = 1,
        prompts: dict[str, str] | None = None,
        allow_download: bool = True,
    ) -> None:
        super().__init__()
        self.model_size = model_size or "small"            # on the CPU
        self.device = device or "auto"
        self.compute_type = compute_type or "int8"
        self.gpu_model = gpu_model or self.model_size      # on an NVIDIA graphics card
        self.gpu_compute_type = gpu_compute_type or "int8_float16"
        self.download_root = Path(download_root) if download_root else None
        self.cpu_threads = int(cpu_threads) or max(2, min(8, (os.cpu_count() or 4) // 2))
        self.beam_size = max(1, min(5, int(beam_size)))
        self.prompts = dict(PROMPTS if prompts is None else prompts)
        self.allow_download = allow_download
        self.device_used: str | None = None
        self.compute_type_used: str | None = None
        self.model_used: str | None = None
        self.load_seconds: float | None = None
        self.gpu_problem: str | None = None                # why the graphics card is not used, when it is not
        self._model: Any = None
        self._current: _Loaded | None = None
        self._loading = False
        self._upgrading = False
        self._settled = threading.Event()
        self._settled.set()
        self._closed = False
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
        if self.device == "cpu":
            return "cpu"
        use_nvidia_dlls()
        if self.device == "cuda":
            return "cuda"
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() <= 0:
                return "cpu"
        except Exception:
            return "cpu"
        self.gpu_problem = cuda_libraries_problem()
        if self.gpu_problem:
            log.info("An NVIDIA graphics card is here, but speech recognition stays on the CPU: %s", self.gpu_problem)
            return "cpu"
        return "cuda"

    def _plans(self) -> list[tuple[str, str, str]]:
        """(device, model, compute type) to try, best first."""
        cpu = ("cpu", self.model_size, self.compute_type)
        return [("cuda", self.gpu_model, self.gpu_compute_type), cpu] if self._resolve_device() == "cuda" else [cpu]

    def _use(self, loaded: _Loaded) -> None:
        self._current = loaded
        self._model = loaded.model
        self.device_used, self.compute_type_used, self.model_used = loaded.device, loaded.compute_type, loaded.name
        self.load_seconds = loaded.seconds
        self.reason = f"faster-whisper '{loaded.name}' on {loaded.device} ({loaded.compute_type})"

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            if self._load_error and time.monotonic() - self._load_failed_at < self.retry_load_after_s:
                raise STTUnavailable(self._load_error)
            self._loading = True
            try:
                self._use(self._load_model())
                self._load_error = None
            except STTError as exc:
                self._load_error, self._load_failed_at = exc.message, time.monotonic()
                raise
            except Exception as exc:  # pragma: no cover - defensive
                self._load_error, self._load_failed_at = _short(exc), time.monotonic()
                raise STTUnavailable(self._load_error) from exc
            finally:
                self._loading = False

    def _load_model(self) -> _Loaded:
        """The model to start with: the CPU one when it is on disk, and the graphics card's model takes over in the
        background (`_upgrade`). Without a card there is only the CPU model."""
        np = np_module()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTUnavailable(f"faster-whisper is not installed: {exc}") from exc
        plans = self._plans()
        best = plans[0]
        order = plans[::-1]                # the CPU model first: quick to load, and the smaller download
        errors: list[str] = []
        card_failed = False                # the card's model is on disk but does not work: no point in more tries
        for plan in order:
            loaded, missing = self._open(WhisperModel, np, plan, local_only=True, errors=errors)
            if loaded is not None:
                if plan != best and not card_failed:
                    self._upgrade_later(best)
                return loaded
            card_failed = card_failed or (plan == best and len(plans) > 1 and not missing)
        if self.allow_download:
            for plan in order:
                if plan == best and card_failed:
                    continue
                loaded, _ = self._open(WhisperModel, np, plan, local_only=False, errors=errors)
                if loaded is not None:
                    if plan != best and not card_failed:
                        self._upgrade_later(best)
                    return loaded
        raise STTUnavailable(f"faster-whisper could not load model '{self.model_size}': {errors[-1] if errors else 'unknown error'}")

    def _open(self, whisper: Any, np: Any, plan: tuple[str, str, str], *, local_only: bool,
              errors: list[str]) -> tuple[_Loaded | None, bool]:
        """Load and warm up one model. The flag says it failed only because it is not downloaded yet."""
        dev, name, compute = plan
        root = str(self.download_root) if self.download_root else None
        started = time.monotonic()
        if not local_only:
            log.info("Downloading the Whisper model '%s' into %s (first use)…", name, root or "the HF cache")
        try:
            model = whisper(name, device=dev, compute_type=compute, cpu_threads=self.cpu_threads, download_root=root,
                            local_files_only=local_only)
        except Exception as exc:
            errors.append(f"{name} {dev}/{compute}{' (cache)' if local_only else ''}: {_short(exc)}")
            log.debug("faster-whisper load attempt failed: %s", errors[-1])
            missing = local_only and _not_downloaded(exc)
            if dev == "cuda" and not missing:
                self.gpu_problem = errors[-1]
            return None, missing
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
            if dev == "cuda":
                # A real command's path as well (language detection, the names prompt, a few more words): the card
                # loads the code for each step on first use, which would otherwise slow the first command by a second.
                quiet = np.zeros(SAMPLE_RATE, dtype=np.float32)
                if hasattr(model, "detect_language"):
                    model.detect_language(audio=quiet)
                segments, _info = model.transcribe(
                    quiet,
                    language="he",
                    beam_size=self.beam_size,
                    initial_prompt=self.prompt_for("he"),
                    without_timestamps=True,
                    max_new_tokens=8,
                    condition_on_previous_text=False,
                    vad_filter=False,
                )
                list(segments)
        except Exception as exc:
            errors.append(f"{name} {dev}/{compute} warm-up: {_short(exc)}")
            log.warning("faster-whisper failed on %s (%s)", dev, errors[-1])
            if dev == "cuda":
                self.gpu_problem = errors[-1]
            return None, False
        loaded = _Loaded(model, dev, compute, name, time.monotonic() - started)
        log.info("Loaded faster-whisper '%s' on %s (%s) in %.1f s", name, dev, compute, loaded.seconds)
        return loaded, False

    def _upgrade_later(self, plan: tuple[str, str, str]) -> None:
        """Load the better model in the background (downloading it when it is missing); it takes over once it works."""
        if self._upgrading:
            return
        self._upgrading = True
        self._settled.clear()
        threading.Thread(target=self._upgrade, args=(plan,), name="jarvis-stt-upgrade", daemon=True).start()

    def _upgrade(self, plan: tuple[str, str, str]) -> None:
        try:
            from faster_whisper import WhisperModel

            errors: list[str] = []
            loaded = self._open_or_download(WhisperModel, plan, errors)
            if loaded is None:
                log.warning("Speech recognition stays on '%s': %s", self.model_used, errors[-1] if errors else "?")
                return
            with self._lock:
                if self._closed:
                    return
                self._use(loaded)
                self.gpu_problem = None
            log.info("Speech recognition now uses %s", self.reason)
        except Exception:  # pragma: no cover - defensive: a background thread must not die loudly
            log.exception("Upgrading the speech recognition model failed")
        finally:
            self._upgrading = False
            self._settled.set()

    def settle(self, timeout: float | None = None) -> bool:
        return self._settled.wait(timeout)

    def _open_or_download(self, whisper: Any, plan: tuple[str, str, str], errors: list[str]) -> _Loaded | None:
        np = np_module()
        loaded, missing = self._open(whisper, np, plan, local_only=True, errors=errors)
        if loaded is None and missing and self.allow_download and not self._closed:
            loaded, _ = self._open(whisper, np, plan, local_only=False, errors=errors)
        return loaded

    def _cpu_instead(self) -> Any:
        """The graphics card failed mid-session: the CPU model from now on (None when it cannot load either)."""
        from faster_whisper import WhisperModel

        errors: list[str] = []
        loaded = self._open_or_download(WhisperModel, ("cpu", self.model_size, self.compute_type), errors)
        if loaded is None:
            log.warning("The CPU model could not load either: %s", errors[-1] if errors else "?")
            return None
        with self._lock:
            self._use(loaded)
        return loaded.model

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
        current = self._current            # the model and its device together (a better one can take over meanwhile)
        model = current.model if current is not None else self._model
        try:
            return self._transcribe_with(model, audio, language, hint, partial)
        except RuntimeError as exc:  # how CTranslate2 reports CUDA failures ("CUDA failed with error out of memory")
            if current is None or current.device != "cuda":
                raise
            self.gpu_problem = _short(exc)
            log.warning("Speech recognition on the graphics card failed (%s); the CPU takes over", self.gpu_problem)
            cpu = self._cpu_instead()
            if cpu is None:
                raise
            return self._transcribe_with(cpu, audio, language, hint, partial)

    def _transcribe_with(self, model: Any, audio: Any, language: str, hint: bool, partial: bool) -> Transcript:
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
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._model = self._current = None


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

    def describe(self) -> str:
        if self._active is self.primary:
            return self.primary.describe()
        return self.reason or self.backup.describe()

    def settle(self, timeout: float | None = None) -> bool:
        return self._active.settle(timeout)

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
            gpu_model=settings.stt_model_gpu,
            gpu_compute_type=settings.stt_compute_type_gpu,
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
