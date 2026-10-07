"""Wake-word engines and the factory that picks one.

* `OpenWakeWordEngine` — openWakeWord "hey_jarvis" (ONNX). Runs continuously on its own thread;
  one 80 ms frame costs ~1-2 ms on one core of a modern CPU (melspectrogram + embedding +
  wake model, all single-threaded ONNX), i.e. ~2 % of a core. Models are fetched on first use
  into `<cache>/models/openwakeword` (or taken from the openwakeword package when present).
* `PorcupineEngine` — Picovoice Porcupine built-in keyword "jarvis" (needs `pvporcupine`
  and the PICOVOICE_ACCESS_KEY secret).
* `TranscriptWakeEngine` — fallback without a wake model: VAD-segments short utterances,
  transcribes them and looks for the wake word at the start. When the same utterance already
  holds the command ("ג'ארוויס פתח את כרום"), the command is returned with the detection, so
  the pipeline handles it with no second listening round trip.
* `NullWakeEngine` — no wake word (push-to-talk only).

`create_wake_engine()` honours `voice.wake_word_engine` (auto | openwakeword | porcupine |
transcript | none) and falls back gracefully, returning a human-readable reason.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.common.errors import VoiceError
from jarvis.common.text import WAKE_WORD_FORMS, normalize
from jarvis.voice.audio import FRAME_SECONDS, np_module, to_int16
from jarvis.voice.base import Transcript
from jarvis.voice.vad import EndpointConfig, Endpointer, EndpointEvent

if TYPE_CHECKING:  # pragma: no cover
    from jarvis.config.paths import AppPaths
    from jarvis.config.settings import VoiceSettings
    from jarvis.security.secrets import SecretStore
    from jarvis.voice.stt import STTEngine

log = logging.getLogger(__name__)


class WakeWordError(VoiceError):
    code = "wake_word_error"


@dataclass
class WakeDetection:
    engine: str
    score: float
    keyword: str = ""
    command: str | None = None  # what was said after the wake word in the same breath
    transcript: Transcript | None = None
    language: str | None = None


# ---------------------------------------------------------------------------------------------
# Wake phrase matching on transcripts (shared with the pipeline)
# ---------------------------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[,.!?;:…\"“”«»()\[\]{}–—\-]+")
_FILLERS = {"אה", "אהה", "אמ", "אממ", "אום", "נו", "יא", "um", "uh", "uhm", "hmm", "so", "oh", "yo", "well"}
_GREETINGS = {"hey", "hi", "ok", "okay", "היי", "הי"}
_WAKE_TOKEN_FORMS = sorted({tuple(normalize(f).split()) for f in WAKE_WORD_FORMS}, key=len, reverse=True)
_CORE_FORMS = sorted({form[-1] for form in _WAKE_TOKEN_FORMS})  # "jarvis", "ג'ארוויס", ...


def _levenshtein(a: str, b: str, limit: int = 2) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _fuzzy_core(token: str) -> bool:
    """'jervis', 'ג'ארויס' … — one edit away from a known spelling of the name."""
    if len(token) < 5:
        return False
    return any(_levenshtein(token, form, 1) <= 1 for form in _CORE_FORMS)


def match_wake_phrase(text: str) -> tuple[bool, str]:
    """Does `text` start with the wake word? Returns (found, the rest of the original text).

    Tolerates punctuation ("Hey, JARVIS!"), one leading filler ("אה, ג'ארוויס"), and spellings
    one edit away from a known form ("Jervis", "ג'ארויס").
    """
    tokens = [(m.start(), m.end(), normalize(_PUNCT_RE.sub("", m.group()))) for m in re.finditer(r"\S+", text or "")]
    tokens = [t for t in tokens if t[2]]
    words = [t[2] for t in tokens]
    for skip in (0, 1):
        if skip and not (words and words[0] in _FILLERS):
            break
        i = skip
        consumed = 0
        for form in _WAKE_TOKEN_FORMS:
            if tuple(words[i : i + len(form)]) == form:
                consumed = len(form)
                break
        if not consumed and i < len(words):
            if _fuzzy_core(words[i]):
                consumed = 1
            elif words[i] in _GREETINGS and i + 1 < len(words) and _fuzzy_core(words[i + 1]):
                consumed = 2
        if consumed:
            end = i + consumed
            if end >= len(tokens):
                return True, ""
            rest = (text or "")[tokens[end][0] :]
            return True, rest.strip().lstrip(",.:;!-—– ").strip()
    return False, ""


# ---------------------------------------------------------------------------------------------
# Engine interface
# ---------------------------------------------------------------------------------------------
class WakeWordEngine(ABC):
    """`process(frame) -> score` (blocking, 80 ms int16 frame) plus `feed()` for the pipeline.

    `feed()` runs `process()` on a dedicated single worker thread (so the event loop never
    blocks on inference), applies the threshold and a refractory period, and resets the model
    after a detection.
    """

    name = "none"
    keyword = ""
    threaded = True
    supports_barge_in = True

    def __init__(self, *, threshold: float = 0.5, refractory_s: float = 1.5) -> None:
        self.threshold = float(threshold)
        self.refractory_frames = max(0, round(refractory_s / FRAME_SECONDS))
        self.reason = ""
        self.last_score = 0.0
        self._ready = False
        self._frames = 0
        self._last_detection: int | None = None
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None

    @property
    def ready(self) -> bool:
        return self._ready

    def load(self) -> None:
        """Load (and if needed download) the model. Blocking — call from a worker thread."""
        self._ready = True

    @abstractmethod
    def process(self, frame: Any) -> float:
        """Wake score 0..1 for one frame. Blocking; called on the wake-word thread."""

    def reset(self) -> None:
        """Clear internal audio state (after a detection, or after the stream was paused)."""
        return None

    def detected(self, score: float, *, threshold_boost: float = 0.0) -> bool:
        return score >= min(0.99, self.threshold + threshold_boost)

    async def feed(self, frame: Any, *, threshold_boost: float = 0.0) -> WakeDetection | None:
        if not self._ready:
            return None
        if self.threaded:
            score = await asyncio.get_running_loop().run_in_executor(self._worker(), self.process, frame)
        else:
            score = self.process(frame)
        self._frames += 1
        self.last_score = float(score)
        if self._last_detection is not None and self._frames - self._last_detection < self.refractory_frames:
            return None
        if not self.detected(score, threshold_boost=threshold_boost):
            return None
        self._last_detection = self._frames
        self.schedule_reset()
        return WakeDetection(engine=self.name, score=float(score), keyword=self.keyword)

    def schedule_reset(self) -> None:
        """Reset without waiting: queued on the wake thread ahead of the next `process()`."""
        if not self._ready:
            return
        if not self.threaded:
            self.reset()
            return
        future = self._worker().submit(self.reset)
        future.add_done_callback(_log_future_error)

    def _worker(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=f"jarvis-wake-{self.name}")
        return self._executor

    def close(self) -> None:
        self._ready = False
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def describe(self) -> str:
        return self.reason or self.name


def _log_future_error(future: concurrent.futures.Future) -> None:
    if not future.cancelled() and future.exception() is not None:
        log.warning("Wake-word reset failed: %s", future.exception())


class NullWakeEngine(WakeWordEngine):
    name = "none"
    threaded = False
    supports_barge_in = False

    def __init__(self, reason: str = "") -> None:
        super().__init__(threshold=1.0, refractory_s=0.0)
        self.reason = reason or "No wake word (push-to-talk only)."

    @property
    def ready(self) -> bool:
        return False

    def load(self) -> None:
        return None

    def process(self, frame: Any) -> float:
        return 0.0

    async def feed(self, frame: Any, *, threshold_boost: float = 0.0) -> WakeDetection | None:
        return None


# ---------------------------------------------------------------------------------------------
# openWakeWord
# ---------------------------------------------------------------------------------------------
def _resolve_oww_key(model: str, registry: dict[str, Any]) -> str:
    raw = (model or "hey_jarvis").strip().lower().replace(" ", "_").replace("-", "_")
    raw = re.sub(r"_v\d+(\.\d+)*$", "", raw)
    if raw in registry:
        return raw
    for key in registry:
        if key in raw or raw in key:
            return key
    raise WakeWordError(f"Unknown openWakeWord model {model!r} (built-in: {', '.join(sorted(registry))})")


def download_file(url: str, dest: Path, *, timeout_s: float = 60.0, min_bytes: int = 1024) -> Path:
    """Download `url` to `dest` atomically (checks the HTTP status and a minimum size)."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    verify: Any = True
    try:  # use the OS certificate store when available (corporate proxies, Windows root CAs)
        import ssl

        import truststore

        verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=timeout_s, verify=verify) as resp:
            resp.raise_for_status()
            with part.open("wb") as fh:
                for chunk in resp.iter_bytes(65536):
                    fh.write(chunk)
        if part.stat().st_size < min_bytes:
            raise WakeWordError(f"{dest.name}: download too small ({part.stat().st_size} bytes)")
        os.replace(part, dest)
    except WakeWordError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise WakeWordError(f"Could not download {dest.name}: {exc}") from exc
    return dest


def openwakeword_files(model: str = "hey_jarvis") -> dict[str, tuple[str, str | None]]:
    """{role: (file name or absolute path, download URL)} for the ONNX files `model` needs."""
    import openwakeword

    custom = Path(os.path.expanduser(model or ""))
    if custom.suffix.lower() == ".onnx":
        if not custom.is_file():
            raise WakeWordError(f"Wake-word model file not found: {custom}")
        wake: tuple[str, str | None] = (str(custom.resolve()), None)
    else:
        key = _resolve_oww_key(model, openwakeword.MODELS)
        url = openwakeword.MODELS[key]["download_url"].replace(".tflite", ".onnx")
        wake = (url.rsplit("/", 1)[-1], url)
    out: dict[str, tuple[str, str | None]] = {"model": wake}
    for role, key in (("melspectrogram", "melspectrogram"), ("embedding", "embedding")):
        url = openwakeword.FEATURE_MODELS[key]["download_url"].replace(".tflite", ".onnx")
        out[role] = (url.rsplit("/", 1)[-1], url)
    return out


def ensure_openwakeword_models(
    model: str, target_dir: Path | None, *, download: bool = True, timeout_s: float = 60.0
) -> dict[str, Path]:
    """Find the ONNX files for `model` in `target_dir` or the openwakeword package, downloading
    missing ones into `target_dir` (the official openWakeWord release URLs).

    We download ourselves instead of `openwakeword.utils.download_models()` because that helper
    does not check HTTP status (a proxy error page would be saved as a "model" and never be
    re-downloaded) and draws tqdm bars on stderr, which is None in the windowed Windows app.
    """
    import openwakeword

    package_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
    search = [d for d in (target_dir, package_dir) if d is not None]
    found: dict[str, Path] = {}
    missing: dict[str, tuple[str, str | None]] = {}
    for role, (name, url) in openwakeword_files(model).items():
        if os.path.isabs(name):
            found[role] = Path(name)
            continue
        hit = next((d / name for d in search if (d / name).is_file() and (d / name).stat().st_size > 1024), None)
        if hit is not None:
            found[role] = hit
        else:
            missing[role] = (name, url)
    if missing:
        names = ", ".join(n for n, _ in missing.values())
        if not download or target_dir is None:
            raise WakeWordError(f"openWakeWord model files missing: {names}")
        log.info("Downloading openWakeWord models (%s) into %s", names, target_dir)
        for role, (name, url) in missing.items():
            if url is None:  # pragma: no cover - custom paths never reach here
                raise WakeWordError(f"No download URL for {name}")
            found[role] = download_file(url, target_dir / name, timeout_s=timeout_s)
    return found


class OpenWakeWordEngine(WakeWordEngine):
    name = "openwakeword"

    def __init__(
        self,
        model: str = "hey_jarvis",
        *,
        threshold: float = 0.5,
        models_dir: Path | str | None = None,
        auto_download: bool = True,
        refractory_s: float = 1.5,
        download_timeout_s: float = 60.0,
    ) -> None:
        super().__init__(threshold=threshold, refractory_s=refractory_s)
        self.model_name = model or "hey_jarvis"
        self.models_dir = Path(models_dir) if models_dir else None
        self.auto_download = auto_download
        self.download_timeout_s = download_timeout_s
        stem = Path(self.model_name).stem if self.model_name.lower().endswith(".onnx") else self.model_name
        self.keyword = re.sub(r"_v\d+(\.\d+)*$", "", stem).replace("_", " ").strip().lower() or "hey jarvis"
        self._model: Any = None
        self._key: str | None = None

    def load(self) -> None:
        if self._model is not None:
            return
        np_module()
        for module in ("openwakeword", "onnxruntime"):
            if importlib.util.find_spec(module) is None:
                raise WakeWordError(f"{module} is not installed")
        files = ensure_openwakeword_models(
            self.model_name, self.models_dir, download=self.auto_download, timeout_s=self.download_timeout_s
        )
        from openwakeword.model import Model

        try:
            model = Model(
                wakeword_models=[str(files["model"])],
                inference_framework="onnx",
                melspec_model_path=str(files["melspectrogram"]),
                embedding_model_path=str(files["embedding"]),
                ncpu=1,
            )
        except Exception as exc:
            raise WakeWordError(f"openWakeWord could not load {self.model_name!r}: {exc}") from exc
        self._model = model
        self._key = next(iter(model.models))
        self._ready = True
        self.reason = f"openWakeWord '{self.keyword}' (threshold {self.threshold:.2f})"

    def process(self, frame: Any) -> float:
        predictions = self._model.predict(to_int16(frame))
        if self._key in predictions:
            return float(predictions[self._key])
        return float(max(predictions.values(), default=0.0))

    def reset(self) -> None:
        if self._model is not None:
            self._model.reset()

    def close(self) -> None:
        super().close()
        self._model = None


# ---------------------------------------------------------------------------------------------
# Porcupine
# ---------------------------------------------------------------------------------------------
class PorcupineEngine(WakeWordEngine):
    name = "porcupine"

    def __init__(self, access_key: str, *, keyword: str = "jarvis", sensitivity: float = 0.5, refractory_s: float = 1.5) -> None:
        super().__init__(threshold=0.5, refractory_s=refractory_s)
        self.keyword = keyword
        self.sensitivity = min(1.0, max(0.0, float(sensitivity)))
        self._access_key = access_key
        self._handle: Any = None
        self._frame_length = 512
        self._carry: Any = None

    def load(self) -> None:
        if self._handle is not None:
            return
        np = np_module()
        try:
            import pvporcupine
        except ImportError as exc:
            raise WakeWordError("pvporcupine is not installed") from exc
        try:
            handle = pvporcupine.create(access_key=self._access_key, keywords=[self.keyword], sensitivities=[self.sensitivity])
        except Exception as exc:
            raise WakeWordError(f"Porcupine failed to start: {exc}") from exc
        if int(getattr(handle, "sample_rate", 16000)) != 16000:  # pragma: no cover - Porcupine is 16 kHz
            handle.delete()
            raise WakeWordError("Porcupine expects an unexpected sample rate")
        self._handle = handle
        self._frame_length = int(getattr(handle, "frame_length", 512))
        self._carry = np.zeros(0, dtype=np.int16)
        self._ready = True
        self.reason = f"Porcupine '{self.keyword}' (sensitivity {self.sensitivity:.2f})"

    def process(self, frame: Any) -> float:
        np = np_module()
        x = np.concatenate((self._carry, to_int16(frame)))
        n = self._frame_length
        count = len(x) // n
        hit = False
        for i in range(count):
            if self._handle.process(x[i * n : (i + 1) * n]) >= 0:
                hit = True
        self._carry = x[count * n :]
        return 1.0 if hit else 0.0

    def reset(self) -> None:
        np = np_module()
        self._carry = np.zeros(0, dtype=np.int16)

    def close(self) -> None:
        super().close()
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.delete()
            except Exception:  # pragma: no cover
                pass


# ---------------------------------------------------------------------------------------------
# Transcript-based fallback
# ---------------------------------------------------------------------------------------------
class TranscriptWakeEngine(WakeWordEngine):
    """Wake word by speech recognition: segment speech with the VAD, transcribe each short
    segment and check whether it starts with the wake word.

    Costs one STT call per spoken segment (so more CPU than a wake model) and reacts after the
    segment ends, but needs no extra model and also understands "ג'ארוויס" said the Hebrew way.
    """

    name = "transcript"
    keyword = "jarvis"
    threaded = False
    supports_barge_in = False  # would transcribe JARVIS's own voice

    def __init__(
        self,
        stt: STTEngine,
        *,
        language: str = "auto",
        end_silence_ms: int = 450,
        max_segment_s: float = 6.0,
        min_speech_ms: int = 250,
        min_confidence: float = 0.3,
    ) -> None:
        super().__init__(threshold=0.5, refractory_s=0.0)
        self.stt = stt
        self.language = language
        self.min_speech_ms = int(min_speech_ms)
        self.min_confidence = float(min_confidence)
        self._endpointer = Endpointer(
            EndpointConfig(end_silence_ms=end_silence_ms, max_utterance_s=max_segment_s, no_speech_timeout_s=None)
        )
        self._endpointer.reset(no_speech_timeout_s=None)

    def load(self) -> None:
        try:
            self.stt.load()
        except Exception as exc:
            raise WakeWordError(f"speech recognition is not available: {_short(exc)}") from exc
        if not self.stt.ready:
            raise WakeWordError(f"speech recognition is not available ({self.stt.reason or self.stt.name})")
        self._ready = True
        self.reason = f"wake word by speech recognition ({self.stt.name})"

    def process(self, frame: Any) -> float:
        return self.last_score

    def reset(self) -> None:
        self._endpointer.reset(no_speech_timeout_s=None)

    async def feed(self, frame: Any, *, threshold_boost: float = 0.0) -> WakeDetection | None:
        if not self._ready:
            return None
        event = self._endpointer.process(frame)
        if event not in (EndpointEvent.SPEECH_END, EndpointEvent.MAX_LENGTH):
            return None
        utterance = self._endpointer.finish()
        self._endpointer.reset(no_speech_timeout_s=None)
        if utterance.speech_s * 1000.0 < self.min_speech_ms:
            return None
        try:
            transcript = await self.stt.transcribe(utterance.audio, self.language, hint=False)
        except Exception as exc:
            log.debug("Transcript wake: STT failed: %s", exc)
            return None
        self.last_score = 0.0
        if transcript.confidence is not None and transcript.confidence < self.min_confidence:
            return None
        found, command = match_wake_phrase(transcript.text)
        if not found:
            return None
        self.last_score = 1.0
        return WakeDetection(
            engine=self.name,
            score=1.0,
            keyword=self.keyword,
            command=command or None,
            transcript=transcript,
            language=transcript.language,
        )


# ---------------------------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------------------------
def _short(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return text.splitlines()[0][:200]


def _have(*modules: str) -> bool:
    try:
        return all(importlib.util.find_spec(m) is not None for m in modules)
    except (ImportError, ValueError):
        return False


def _build_wake(
    kind: str, settings: VoiceSettings, paths: AppPaths, secrets: SecretStore | None, stt: STTEngine | None, explicit: bool
) -> tuple[WakeWordEngine | None, str]:
    if kind == "porcupine":
        key = None
        if secrets is not None:
            try:
                key = secrets.get("PICOVOICE_ACCESS_KEY")
            except Exception:  # pragma: no cover - keyring backend errors
                key = None
        if not _have("pvporcupine"):
            return None, "pvporcupine is not installed" if (explicit or key) else ""
        if not key:
            return None, "PICOVOICE_ACCESS_KEY is not set" if explicit else ""
        return PorcupineEngine(key, keyword="jarvis", sensitivity=1.0 - settings.wake_threshold), ""
    if kind == "openwakeword":
        missing = [m for m in ("numpy", "openwakeword", "onnxruntime") if not _have(m)]
        if missing:
            return None, f"{', '.join(missing)} not installed"
        return OpenWakeWordEngine(
            settings.wake_word_model, threshold=settings.wake_threshold, models_dir=paths.models / "openwakeword"
        ), ""
    if kind == "transcript":
        if stt is None or stt.name == "none":
            return None, "no speech recognition engine"
        return TranscriptWakeEngine(stt, language=settings.stt_language), ""
    return None, f"unknown engine {kind!r}"


_WAKE_ORDER = {
    "auto": ("porcupine", "openwakeword", "transcript"),
    "porcupine": ("porcupine", "openwakeword", "transcript"),
    "openwakeword": ("openwakeword", "transcript"),
    "transcript": ("transcript",),
}


def create_wake_engine(
    settings: VoiceSettings,
    paths: AppPaths,
    secrets: SecretStore | None,
    stt: STTEngine | None,
    *,
    load: bool = False,
) -> tuple[WakeWordEngine, str]:
    """Pick the wake-word engine for `settings.wake_word_engine`, falling back gracefully.

    With `load=True` (call from a worker thread — it may download models) each candidate is
    loaded and the next one is tried when loading fails. Returns (engine, reason).
    """
    choice = settings.wake_word_engine
    if choice == "none":
        off = NullWakeEngine("Wake word is turned off in settings (push-to-talk only).")
        return off, off.reason
    notes: list[str] = []
    for kind in _WAKE_ORDER.get(choice, _WAKE_ORDER["auto"]):
        try:
            engine_or_none, why_not = _build_wake(kind, settings, paths, secrets, stt, explicit=(kind == choice))
        except Exception as exc:
            engine_or_none, why_not = None, _short(exc)
        if engine_or_none is None:
            if why_not:
                notes.append(f"{kind}: {why_not}")
            continue
        engine = engine_or_none
        if load:
            try:
                engine.load()
            except Exception as exc:
                log.warning("Wake-word engine %s unavailable: %s", kind, _short(exc))
                notes.append(f"{kind}: {_short(exc)}")
                engine.close()
                continue
        reason = engine.describe()
        if notes:
            reason += " — fallback because " + "; ".join(notes)
        engine.reason = reason
        return engine, reason
    engine = NullWakeEngine("No wake-word engine available (" + "; ".join(notes or ["none configured"]) + "). Push-to-talk still works.")
    return engine, engine.reason
