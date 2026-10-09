"""Text-to-speech engines and the acknowledgement phrase cache.

* `EdgeTTS` — Microsoft neural voices through edge-tts (network): `tts_voice_he` /
  `tts_voice_en`, rate `tts_rate`. The MP3 stream is collected and decoded with miniaudio.
* `SapiTTS` — offline pyttsx3: SAPI5 on Windows (a Hebrew voice such as "Microsoft Asaf" when
  installed), espeak-ng on Linux, NSSpeech on macOS. pyttsx3 is blocking and not thread-safe,
  so it lives on one dedicated worker thread (COM initialised there on Windows) and renders to
  a temporary WAV file.
* `NullTTS` — silent (reports why).
* `FallbackTTS` — tries engines in order on every call (edge → sapi); an engine that just
  failed is skipped for a minute so an offline PC does not pay a network timeout per sentence.

`PhraseCache` pre-synthesizes the `ACK_PHRASES` in Hebrew and English at start-up into
`<cache>/tts` (WAV, keyed by engine+voice+rate+text hash) and keeps them in memory, so
acknowledgements play instantly. The language of any text is chosen with `detect_language`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import importlib.util
import logging
import os
import re
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.common.errors import VoiceError
from jarvis.common.text import detect_language
from jarvis.voice.audio import AudioClip, decode_audio_bytes, read_wav, write_wav
from jarvis.voice.base import ACK_PHRASES

if TYPE_CHECKING:  # pragma: no cover
    from jarvis.config.settings import VoiceSettings

log = logging.getLogger(__name__)

LANGUAGE_NAMES = {"he": "Hebrew", "en": "English"}


class TTSError(VoiceError):
    code = "tts_error"


class TTSUnavailable(TTSError):
    """The engine cannot speak this at all (not installed, no voice for the language...)."""

    code = "tts_unavailable"


def _short(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return text.splitlines()[0][:240]


def _have(*modules: str) -> bool:
    try:
        return all(importlib.util.find_spec(m) is not None for m in modules)
    except (ImportError, ValueError):
        return False


def resolve_language(text: str, language: str | None = None, default: str = "en") -> str:
    if language in ("he", "en"):
        return language
    return detect_language(text or "", default=default)


# ---------------------------------------------------------------------------------------------
# Speech text helpers
# ---------------------------------------------------------------------------------------------
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_LINE_MARK_RE = re.compile(r"^\s*(?:#{1,6}|>+|[*\-•])\s+", re.M)
_EMPHASIS_RE = re.compile(r"(\*{1,3}|_{2,3}|~~)")
_SYMBOLS_RE = re.compile("[\U0001f300-\U0001faff☀-➿️]")
_SENTENCE_END = ".!?…:;,"


def prepare_speech_text(text: str, *, max_chars: int = 900) -> str:
    """Make chat-style text speakable: drop code, URLs, markdown marks and emoji; one line per
    sentence; cut at a sentence boundary when longer than `max_chars`."""
    t = _CODE_BLOCK_RE.sub(" ", text or "")
    t = _LINK_RE.sub(r"\1", t)
    t = _URL_RE.sub(" ", t)
    t = _INLINE_CODE_RE.sub(r"\1", t)
    t = _LINE_MARK_RE.sub("", t)
    t = _EMPHASIS_RE.sub("", t)
    t = _SYMBOLS_RE.sub("", t)
    lines = []
    for raw in t.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if line[-1] not in _SENTENCE_END:
            line += "."
        lines.append(line)
    out = " ".join(lines).strip()
    if len(out) > max_chars:
        cut = out[:max_chars]
        boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        out = cut[: boundary + 1] if boundary > max_chars // 3 else cut.rsplit(" ", 1)[0] + "…"
    return out


def split_sentences(text: str, *, max_chars: int = 220) -> list[str]:
    """Chunks for pipelined synthesis: the first sentence plays while the next is synthesized."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+|\n+", text or "") if p and p.strip()]
    chunks: list[str] = []
    for part in parts:
        while len(part) > max_chars:  # very long sentence: split at a comma or a space
            cut = max(part.rfind(", ", 0, max_chars), part.rfind(" ", 0, max_chars))
            cut = cut if cut > max_chars // 3 else max_chars
            chunks.append(part[:cut].strip(" ,"))
            part = part[cut:].strip(" ,")
        if part:
            if chunks and len(chunks[-1]) + len(part) + 1 <= min(60, max_chars):  # glue tiny sentences together
                chunks[-1] = f"{chunks[-1]} {part}"
            else:
                chunks.append(part)
    return chunks


# ---------------------------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------------------------
class TTSEngine(ABC):
    name = "none"

    def __init__(self) -> None:
        self.reason = ""

    @property
    def available(self) -> bool:
        return True

    def voice_for(self, language: str) -> str:
        return ""

    def cache_id(self, language: str) -> str:
        """Identifies the exact voice configuration (part of the phrase-cache key)."""
        return f"{self.name}|{self.voice_for(language)}"

    def cache_ids(self, language: str) -> list[str]:
        return [self.cache_id(language)]

    async def prepare(self) -> None:
        """Initialise lazily-started parts (e.g. pick SAPI voices). Never raises."""
        return None

    @abstractmethod
    async def synthesize(self, text: str, language: str | None = None) -> AudioClip: ...

    async def close(self) -> None:
        return None


class NullTTS(TTSEngine):
    name = "none"

    def __init__(self, reason: str = "") -> None:
        super().__init__()
        self.reason = reason or "Speech output is not available."

    @property
    def available(self) -> bool:
        return False

    async def synthesize(self, text: str, language: str | None = None) -> AudioClip:
        raise TTSUnavailable(self.reason)


_RATE_RE = re.compile(r"^[+-]?\d{1,3}%$")


def _percent(rate: str) -> int:
    r = (rate or "").strip().replace(" ", "")
    if not _RATE_RE.match(r):
        return 0
    return int(r.rstrip("%"))


class EdgeTTS(TTSEngine):
    name = "edge"

    def __init__(
        self,
        voice_he: str = "he-IL-AvriNeural",
        voice_en: str = "en-GB-RyanNeural",
        rate: str = "+0%",
        *,
        timeout_s: float = 15.0,
        connect_timeout_s: float = 5.0,
    ) -> None:
        super().__init__()
        self.voice_he = voice_he or "he-IL-AvriNeural"
        self.voice_en = voice_en or "en-GB-RyanNeural"
        pct = _percent(rate)
        self.rate = f"{'+' if pct >= 0 else '-'}{abs(pct)}%"
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.reason = f"edge-tts ({self.voice_he} / {self.voice_en})"

    @property
    def available(self) -> bool:
        return _have("edge_tts", "miniaudio")

    def voice_for(self, language: str) -> str:
        return self.voice_he if language == "he" else self.voice_en

    def cache_id(self, language: str) -> str:
        return f"edge|{self.voice_for(language)}|{self.rate}"

    async def synthesize(self, text: str, language: str | None = None) -> AudioClip:
        text = (text or "").strip()
        if not text:
            raise TTSError("Nothing to say")
        lang = resolve_language(text, language)
        try:
            import edge_tts
        except ImportError as exc:
            raise TTSUnavailable("edge-tts is not installed") from exc
        voice = self.voice_for(lang)
        audio = bytearray()

        async def collect() -> None:
            communicate = edge_tts.Communicate(
                text,
                voice,
                rate=self.rate,
                connect_timeout=max(1, int(self.connect_timeout_s)),
                receive_timeout=max(1, int(self.timeout_s)),
            )
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    audio.extend(chunk["data"])

        try:
            await asyncio.wait_for(collect(), timeout=self.timeout_s)
        except TimeoutError as exc:
            raise TTSError("edge-tts timed out") from exc
        except Exception as exc:
            raise TTSError(f"edge-tts failed: {_short(exc)}") from exc
        if not audio:
            raise TTSError("edge-tts returned no audio")
        clip = await asyncio.to_thread(decode_audio_bytes, bytes(audio), sample_rate=24_000)
        clip.text, clip.language, clip.engine, clip.voice = text, lang, self.name, voice
        clip.source_id = self.cache_id(lang)
        return clip


_VOICE_LANG_HINTS = {"he": ("he",), "en": ("en",)}
_VOICE_NAME_HINTS = {
    "he": ("hebrew", "asaf", "hila", "he-il", "he_il", "עברית"),
    "en": ("english", "en-us", "en-gb", "en_us", "en_gb", "david", "zira", "mark", "hazel", "george", "susan"),
}


def _voice_languages(voice: Any) -> list[str]:
    out = []
    for item in getattr(voice, "languages", None) or []:
        if isinstance(item, bytes):
            item = item.decode("utf-8", "ignore")
        item = "".join(ch for ch in str(item) if ch.isprintable()).strip().lower().replace("_", "-")
        if item:
            out.append(item)
    return out


def pick_voice(voices: list[Any], language: str, preferred: str = "") -> tuple[str, str] | None:
    """Choose a pyttsx3 voice for `language` → (id, name), or None when there is none."""
    if preferred:
        low = preferred.lower()
        for v in voices:
            if low in str(getattr(v, "name", "")).lower() or low in str(getattr(v, "id", "")).lower():
                return str(v.id), str(v.name)
    prefix = _VOICE_LANG_HINTS[language][0]
    by_lang = [v for v in voices if any(lang.split("-")[0] == prefix for lang in _voice_languages(v))]
    if by_lang:
        order = {"en": ("en-us", "en-gb"), "he": ("he-il", "he")}[language]
        by_lang.sort(key=lambda v: next((i for i, tag in enumerate(order) if tag in _voice_languages(v)), len(order)))
        return str(by_lang[0].id), str(by_lang[0].name)
    hints = _VOICE_NAME_HINTS[language]
    for v in voices:
        blob = f"{getattr(v, 'name', '')} {getattr(v, 'id', '')}".lower()
        if any(h in blob for h in hints):
            return str(v.id), str(v.name)
    return None


def _com_initialize() -> None:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        try:
            import pythoncom

            pythoncom.CoInitialize()
        except Exception:
            with contextlib.suppress(Exception):
                import comtypes

                comtypes.CoInitialize()


class SapiTTS(TTSEngine):
    name = "sapi"

    def __init__(self, *, rate: str = "+0%", voice_he: str = "", voice_en: str = "", driver: str | None = None) -> None:
        super().__init__()
        self.rate_pct = _percent(rate)
        self.voice_he_pref = voice_he if voice_he and "Neural" not in voice_he else ""
        self.voice_en_pref = voice_en if voice_en and "Neural" not in voice_en else ""
        self.driver = driver
        self.voices: dict[str, tuple[str, str] | None] = {}
        self.init_error: str | None = None
        self._engine: Any = None
        self._words_per_minute: int | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="jarvis-sapi", initializer=_com_initialize)
        self.reason = "pyttsx3 (SAPI5 on Windows, espeak-ng on Linux)"

    @property
    def available(self) -> bool:
        return _have("pyttsx3")

    def voice_for(self, language: str) -> str:
        chosen = self.voices.get(language)
        return chosen[1] if chosen else f"default-{language}"

    def cache_id(self, language: str) -> str:
        chosen = self.voices.get(language)
        return f"sapi|{chosen[0] if chosen else language}|{self.rate_pct:+d}%"

    async def prepare(self) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._init_sync)
        except Exception as exc:
            log.info("Offline speech (pyttsx3) unavailable: %s", _short(exc))

    def _init_sync(self) -> None:
        if self._engine is not None:
            return
        try:
            import pyttsx3
        except ImportError as exc:
            self.init_error = "pyttsx3 is not installed"
            raise TTSUnavailable(self.init_error) from exc
        try:
            engine = pyttsx3.init(self.driver) if self.driver else pyttsx3.init()
            voices = list(engine.getProperty("voices") or [])
            default_id = engine.getProperty("voice")
            base_rate = int(engine.getProperty("rate") or 200)
        except Exception as exc:
            self.init_error = f"pyttsx3 could not start: {_short(exc)}"
            raise TTSUnavailable(self.init_error) from exc
        self.voices = {"he": pick_voice(voices, "he", self.voice_he_pref), "en": pick_voice(voices, "en", self.voice_en_pref)}
        if self.voices["en"] is None and default_id:
            default = next((v for v in voices if v.id == default_id), None)
            self.voices["en"] = (str(default_id), str(getattr(default, "name", default_id)))
        self._words_per_minute = max(80, int(base_rate * (1 + self.rate_pct / 100.0)))
        self._engine = engine
        names = {k: (v[1] if v else None) for k, v in self.voices.items()}
        self.reason = f"pyttsx3 voices: Hebrew={names['he'] or 'none'}, English={names['en'] or 'none'}"
        log.info("Offline speech ready (%s)", self.reason)

    async def synthesize(self, text: str, language: str | None = None) -> AudioClip:
        text = (text or "").strip()
        if not text:
            raise TTSError("Nothing to say")
        lang = resolve_language(text, language)
        return await asyncio.get_running_loop().run_in_executor(self._executor, self._synthesize_sync, text, lang)

    def _synthesize_sync(self, text: str, lang: str) -> AudioClip:
        self._init_sync()
        voice = self.voices.get(lang)
        if voice is None:
            hint = " (Windows: Settings → Time & language → Speech → Add voices)" if sys.platform == "win32" else ""
            raise TTSUnavailable(f"No {LANGUAGE_NAMES.get(lang, lang)} voice is installed for offline speech{hint}")
        engine = self._engine
        engine.setProperty("voice", voice[0])
        if self._words_per_minute:
            engine.setProperty("rate", self._words_per_minute)
        fd, tmp = tempfile.mkstemp(prefix="jarvis-tts-", suffix=".wav")
        os.close(fd)
        try:
            self._save_to_file(engine, text, tmp)
            samples, rate = read_wav(tmp)
        except TTSError:
            raise
        except Exception as exc:
            raise TTSError(f"Offline speech failed: {_short(exc)}") from exc
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)
        if len(samples) == 0:
            raise TTSError("Offline speech produced no audio")
        return AudioClip(samples, rate, text=text, language=lang, engine=self.name, voice=voice[1], source_id=self.cache_id(lang))

    @staticmethod
    def _save_to_file(engine: Any, text: str, path: str) -> None:
        # pyttsx3's SAPI5 driver writes the file synchronously, but its runAndWait() loop only ends on an
        # EndStream event that (pyttsx3 2.99) never arrives after the first file: the second call hangs forever.
        # So on SAPI5 call the driver directly and just drain the COM events it queued.
        driver = getattr(getattr(engine, "proxy", None), "_driver", None)
        if sys.platform == "win32" and type(driver).__name__ == "SAPI5Driver" and hasattr(driver, "save_to_file"):
            driver.save_to_file(text, path)
            with contextlib.suppress(Exception):
                import pythoncom

                pythoncom.PumpWaitingMessages()
            return
        engine.save_to_file(text, path)
        engine.runAndWait()

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class FallbackTTS(TTSEngine):
    """Tries `engines` in order on every call; skips an engine for `cooldown_s` after it failed."""

    def __init__(self, engines: list[TTSEngine], *, cooldown_s: float = 60.0) -> None:
        super().__init__()
        if not engines:
            raise ValueError("FallbackTTS needs at least one engine")
        self.engines = engines
        self.cooldown_s = cooldown_s
        self.last_engine: str | None = None
        self.last_errors: list[str] = []
        self._down_until: dict[int, float] = {}
        self.reason = " → ".join(e.name for e in engines)

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.engines[0].name

    @property
    def available(self) -> bool:
        return any(e.available for e in self.engines)

    def voice_for(self, language: str) -> str:
        return self.engines[0].voice_for(language)

    def cache_id(self, language: str) -> str:
        return self.engines[0].cache_id(language)

    def cache_ids(self, language: str) -> list[str]:
        return [cid for e in self.engines for cid in e.cache_ids(language)]

    async def prepare(self) -> None:
        for engine in self.engines:
            await engine.prepare()

    async def synthesize(self, text: str, language: str | None = None) -> AudioClip:
        errors: list[str] = []
        now = time.monotonic()
        fresh = [i for i in range(len(self.engines)) if self._down_until.get(i, 0.0) <= now]
        cooling = [i for i in range(len(self.engines)) if i not in fresh]
        for i in fresh + cooling:  # engines that recently failed are only tried as a last resort
            engine = self.engines[i]
            try:
                clip = await engine.synthesize(text, language)
            except TTSUnavailable as exc:  # e.g. no Hebrew voice — not a transient failure
                errors.append(f"{engine.name}: {_short(exc)}")
                continue
            except Exception as exc:
                errors.append(f"{engine.name}: {_short(exc)}")
                self._down_until[i] = time.monotonic() + self.cooldown_s
                log.warning("TTS engine %s failed (%s)", engine.name, _short(exc))
                continue
            self._down_until.pop(i, None)
            self.last_engine = engine.name
            self.last_errors = errors
            return clip
        self.last_errors = errors
        raise TTSError("No speech engine could say this: " + "; ".join(errors))

    async def close(self) -> None:
        for engine in self.engines:
            await engine.close()


def create_tts_engine(settings: VoiceSettings) -> tuple[TTSEngine, str]:
    """Pick the TTS engine for `settings.tts_engine` (auto | edge | sapi | none).

    auto/edge: edge-tts with a per-call fall back to offline pyttsx3; sapi: offline only.
    """
    choice = settings.tts_engine
    if choice == "none":
        engine = NullTTS("Speech output is turned off in settings.")
        return engine, engine.reason
    edge_ok, sapi_ok = _have("edge_tts", "miniaudio", "numpy"), _have("pyttsx3", "numpy")

    def edge() -> EdgeTTS:
        return EdgeTTS(settings.tts_voice_he, settings.tts_voice_en, settings.tts_rate)

    def sapi() -> SapiTTS:
        return SapiTTS(rate=settings.tts_rate)

    if choice in ("auto", "edge"):
        if edge_ok and sapi_ok:
            return FallbackTTS([edge(), sapi()]), "edge-tts neural voices (offline pyttsx3 as fallback)"
        if edge_ok:
            return edge(), "edge-tts neural voices (no offline fallback: pyttsx3 not installed)"
        if sapi_ok:
            return sapi(), "offline pyttsx3 voices (edge-tts/miniaudio not installed)"
    else:  # sapi
        if sapi_ok:
            return sapi(), "offline pyttsx3 voices"
        if edge_ok:
            return edge(), "edge-tts neural voices (pyttsx3 not installed)"
    engine = NullTTS("No speech output engine is installed (edge-tts or pyttsx3).")
    return engine, engine.reason


# ---------------------------------------------------------------------------------------------
# Phrase cache
# ---------------------------------------------------------------------------------------------
class PhraseCache:
    """Pre-synthesized acknowledgement phrases (memory + WAV files) for instant playback."""

    def __init__(
        self,
        tts: TTSEngine,
        cache_dir: Path | str,
        *,
        phrases: dict[str, dict[str, str]] | None = None,
        languages: tuple[str, ...] = ("he", "en"),
    ) -> None:
        self.tts = tts
        self.cache_dir = Path(cache_dir)
        self.phrases = phrases if phrases is not None else ACK_PHRASES
        self.languages = languages
        self.hits = 0
        self.misses = 0
        self._memory: dict[tuple[str, str], AudioClip] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @staticmethod
    def key(source_id: str, text: str) -> str:
        return hashlib.sha1(f"{source_id}\n{text.strip()}".encode()).hexdigest()[:24]

    def path(self, source_id: str, text: str) -> Path:
        return self.cache_dir / f"{self.key(source_id, text)}.wav"

    def phrase(self, key: str, language: str) -> str | None:
        return (self.phrases.get(key) or {}).get(language)

    def lookup_text(self, text: str, language: str, *, primary_only: bool = False) -> AudioClip | None:
        """Cached clip for `text` (memory, then disk) — never synthesizes; instant."""
        clip = self._find(text, language, primary_only)
        if clip is None:
            self.misses += 1
        else:
            self.hits += 1
        return clip

    def _find(self, text: str, language: str, primary_only: bool) -> AudioClip | None:
        ids = self.tts.cache_ids(language)
        for source_id in ids[:1] if primary_only else ids:
            clip = self._memory.get((source_id, text))
            if clip is None:
                path = self.path(source_id, text)
                if path.is_file():
                    try:
                        clip = AudioClip.from_wav(path, text=text, language=language, source_id=source_id)
                    except Exception as exc:
                        log.debug("Unreadable cached phrase %s: %s", path, exc)
                        with contextlib.suppress(OSError):
                            path.unlink()
                        continue
                    clip.engine = source_id.split("|", 1)[0]
                    self._memory[(source_id, text)] = clip
            if clip is not None:
                return clip
        return None

    def lookup(self, key: str, language: str) -> AudioClip | None:
        text = self.phrase(key, language)
        return self.lookup_text(text, language) if text else None

    def store(self, clip: AudioClip, text: str) -> None:
        source_id = clip.source_id or self.tts.cache_id(clip.language or "en")
        self._memory[(source_id, text)] = clip
        try:
            write_wav(self.path(source_id, text), clip.samples, clip.sample_rate)
        except OSError as exc:
            log.debug("Could not write phrase cache file: %s", exc)

    async def get_text(self, text: str, language: str, *, primary_only: bool = False) -> AudioClip:
        """Cached clip, or synthesize it now (and cache it)."""
        clip = self._find(text, language, primary_only)
        if clip is not None:
            return clip
        lock = self._locks.setdefault((language, text), asyncio.Lock())
        async with lock:
            clip = self._find(text, language, primary_only)
            if clip is not None:
                return clip
            clip = await self.tts.synthesize(text, language)
            clip.text = clip.text or text
            await asyncio.to_thread(self.store, clip, text)
            return clip

    async def get(self, key: str, language: str) -> AudioClip | None:
        text = self.phrase(key, language)
        if not text:
            return None
        return await self.get_text(text, language)

    async def warm(self, languages: tuple[str, ...] | None = None) -> dict[str, Any]:
        """Make sure every phrase is cached for the preferred engine (start-up, background).

        When the preferred engine turns out to be unreachable (a fallback engine produced the
        clip, e.g. edge-tts while offline), copies cached from any engine are good enough for
        the remaining phrases instead of re-synthesizing all of them with the fallback."""
        await self.tts.prepare()
        stats: dict[str, Any] = {"cached": 0, "synthesized": 0, "failed": 0, "errors": [], "fallback": False}
        primary_only = True
        for key in self.phrases:
            for language in languages or self.languages:
                text = self.phrase(key, language)
                if not text:
                    continue
                if self._find(text, language, primary_only) is not None:
                    stats["cached"] += 1
                    continue
                try:
                    clip = await self.get_text(text, language, primary_only=primary_only)
                except Exception as exc:
                    stats["failed"] += 1
                    if len(stats["errors"]) < 4:
                        stats["errors"].append(f"{key}/{language}: {_short(exc)}")
                    continue
                stats["synthesized"] += 1
                if primary_only and clip.source_id and clip.source_id != self.tts.cache_id(language):
                    primary_only = False
                    stats["fallback"] = True
        if stats["failed"] or stats["fallback"]:
            log.info("Phrase cache: %s", stats)
        return stats
