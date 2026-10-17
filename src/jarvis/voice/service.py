"""`VoiceService` — the voice subsystem as the rest of JARVIS sees it.

It builds the engines from `VoiceSettings` (with graceful fallbacks), runs the
`VoicePipeline`, loads the slow parts in the background and reports a `VoiceInfo` snapshot
for the UI. `start()` never raises: when voice cannot work the state is "disabled" (or
"error") with a human-readable reason, and speech output keeps working when only the
microphone is missing.

Start-up sequence (`start()` returns as soon as the pipeline runs, ~instant):

1. engines are constructed (cheap, nothing loaded) and the output device is resolved;
2. background: acknowledgement phrases are synthesized/loaded into the phrase cache, the
   wake-word model is loaded (downloaded on first use) and the STT model is loaded and warmed
   up; until the wake word is ready the microphone stays closed and push-to-talk works;
3. the microphone is checked; while none is found the state is "disabled" ("No microphone
   found") and devices are re-scanned every 20 s, so plugging in a headset just works.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import sys
import time
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from jarvis.voice.base import CommandHandler, NullVoiceEvents, VoiceEventSink, VoiceState

if TYPE_CHECKING:  # pragma: no cover
    from jarvis.config.paths import AppPaths
    from jarvis.config.settings import VoiceSettings
    from jarvis.security.secrets import SecretStore
    from jarvis.voice.audio import AudioSink, AudioSource
    from jarvis.voice.pipeline import PipelineOptions, VoicePipeline
    from jarvis.voice.stt import STTEngine
    from jarvis.voice.tts import PhraseCache, TTSEngine
    from jarvis.voice.wakeword import WakeWordEngine

log = logging.getLogger(__name__)

INPUT_RECHECK_S = 20.0
DEFAULT_MIC_RECHECK_S = 3.0    # how often Windows' default microphone is compared with the open one
FOLLOW_DEFAULT_MIC = sys.platform == "win32"
_CORE_MODULES = ("numpy",)


def _short(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return text.splitlines()[0][:240]


def _close_loaded_engine(future: asyncio.Future) -> None:
    if future.cancelled() or future.exception() is not None:
        return
    with contextlib.suppress(Exception):
        future.result()[0].close()


def default_microphone_id() -> str | None:
    """Windows' default microphone (None on other systems, or when it cannot be read)."""
    if sys.platform != "win32":
        return None
    try:
        from jarvis.host.winapi import default_microphone_id as read

        return read()
    except Exception:
        log.debug("Reading the default microphone failed", exc_info=True)
        return None


def _missing_modules(names: tuple[str, ...]) -> list[str]:
    missing = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


class VoiceService:
    """Wake word → STT → `handler` → TTS, plus status for the UI.

    `source`, `sink`, `wake`, `stt`, `tts` and `options` can be injected (tests, the self-test);
    injected engines are used as they are and are not closed by `stop()`.
    """

    def __init__(
        self,
        settings: VoiceSettings,
        paths: AppPaths,
        secrets: SecretStore | None,
        handler: CommandHandler,
        events: VoiceEventSink | None = None,
        *,
        source: AudioSource | None = None,
        sink: AudioSink | None = None,
        wake: WakeWordEngine | None = None,
        stt: STTEngine | None = None,
        tts: TTSEngine | None = None,
        options: PipelineOptions | None = None,
    ) -> None:
        self.settings = settings
        self.paths = paths
        self.secrets = secrets
        self.handler = handler
        self.events: VoiceEventSink = events or NullVoiceEvents()
        self._injected: dict[str, Any] = {"source": source, "sink": sink, "wake": wake, "stt": stt, "tts": tts}
        self._options = options
        self.pipeline: VoicePipeline | None = None
        self.source: AudioSource | None = None
        self.sink: AudioSink | None = None
        self.stt: STTEngine | None = None
        self.tts: TTSEngine | None = None
        self.cache: PhraseCache | None = None
        self.muted = False
        self.reasons: dict[str, str] = {}
        self.warmup: dict[str, Any] = {}
        self._started = False
        self._disabled_reason: str | None = None
        self._input_name: str | None = None
        self._output_name: str | None = None
        self._wake_loading = False
        self._owned: list[Any] = []
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._started and self.pipeline is not None and self.pipeline.running

    async def start(self) -> None:
        """Start listening. Never raises; see `info()` for the outcome."""
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            if self._started:
                return
            try:
                await self._start()
            except Exception as exc:
                log.exception("Voice failed to start")
                with contextlib.suppress(Exception):
                    await self._teardown()
                await self._disable(f"Voice could not start: {_short(exc)}")

    async def stop(self) -> None:
        """Stop listening and speaking, close the microphone and release the models."""
        async with self._lock:
            was_running = self._started
            self._started = False
            with contextlib.suppress(Exception):
                await self._teardown()
            self._disabled_reason = None
            if was_running:
                await self._emit_state(VoiceState.DISABLED, None)

    async def restart(self, settings: VoiceSettings | None = None) -> None:
        """Apply new settings (devices, engines, voices...) by restarting the service."""
        await self.stop()
        if settings is not None:
            self.settings = settings
        await self.start()

    async def _start(self) -> None:
        from jarvis.voice.audio import VOICE_EXTRA_HINT

        self._disabled_reason = None
        self.reasons = {}
        self.warmup = {}
        if not self.settings.enabled:
            await self._disable("Voice is turned off in settings.")
            return
        missing = _missing_modules(_CORE_MODULES)
        if missing:
            await self._disable(f"Voice support is not installed ({', '.join(missing)} missing): {VOICE_EXTRA_HINT}")
            return

        from jarvis.voice.audio import MicrophoneSource, create_sink
        from jarvis.voice.pipeline import PipelineOptions, VoicePipeline
        from jarvis.voice.stt import create_stt_engine
        from jarvis.voice.tts import PhraseCache, create_tts_engine
        from jarvis.voice.wakeword import NullWakeEngine

        settings = self.settings
        injected = self._injected
        self._owned = []

        if injected["stt"] is not None:
            self.stt, self.reasons["stt"] = injected["stt"], injected["stt"].reason or injected["stt"].name
        else:
            self.stt, self.reasons["stt"] = create_stt_engine(settings, self.paths, self.secrets)
            self._owned.append(self.stt)
        if injected["tts"] is not None:
            self.tts, self.reasons["tts"] = injected["tts"], injected["tts"].reason or injected["tts"].name
        else:
            self.tts, self.reasons["tts"] = create_tts_engine(settings)
            self._owned.append(self.tts)
        sink: AudioSink
        if injected["sink"] is not None:
            sink = injected["sink"]
            self.reasons["output"] = sink.device_name or sink.name
        else:
            sink, self.reasons["output"] = await asyncio.to_thread(create_sink, settings.output_device)
            self._owned.append(sink)
        self.sink = sink
        self._output_name = sink.device_name
        self.source = injected["source"] or MicrophoneSource(settings.input_device or None)

        wake = injected["wake"]
        self._wake_loading = wake is None and settings.always_listening and settings.wake_word_engine != "none"
        if wake is None:
            wake = NullWakeEngine("Loading the wake word…" if self._wake_loading else "Push-to-talk only.")
        self.cache = PhraseCache(self.tts, self.paths.tts_cache)
        options = self._options or PipelineOptions.from_settings(settings)
        pipeline = VoicePipeline(
            source=self.source,
            sink=sink,
            wake=wake,
            stt=self.stt,
            tts=self.tts,
            handler=self.handler,
            events=self.events,
            options=options,
            phrase_cache=self.cache,
        )
        pipeline.muted = self.muted
        self.pipeline = pipeline
        self._started = True
        pipeline.start()
        log.info(
            "Voice started (stt: %s; tts: %s; output: %s)",
            self.reasons.get("stt"),
            self.reasons.get("tts"),
            self.reasons.get("output"),
        )
        self._spawn(self._warm_up(pipeline))
        if injected["source"] is None:
            self._spawn(self._watch_input(pipeline))

    async def _teardown(self) -> None:
        tasks = [t for t in self._tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is not None:
            await pipeline.stop()
            if pipeline.wake is not self._injected["wake"]:
                with contextlib.suppress(Exception):
                    pipeline.wake.close()
        for engine in self._owned:
            try:
                result = engine.close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # pragma: no cover - best effort
                log.debug("Closing %s failed", type(engine).__name__, exc_info=True)
        self._owned = []
        self._wake_loading = False

    # ------------------------------------------------------------------------------------------
    # Background work
    # ------------------------------------------------------------------------------------------
    async def _warm_up(self, pipeline: VoicePipeline) -> None:
        started = time.monotonic()

        async def speech_output() -> None:
            if self.cache is None or not self.settings.speak_responses:
                return
            try:
                self.warmup["phrases"] = await self.cache.warm()
            except Exception as exc:
                log.warning("Could not prepare the acknowledgement phrases: %s", _short(exc))
                self.warmup["phrases"] = {"error": _short(exc)}

        async def listening() -> None:
            await self._load_wake(pipeline)
            await self._warm_stt()

        await asyncio.gather(speech_output(), listening())
        self.warmup["seconds"] = round(time.monotonic() - started, 2)
        log.info("Voice ready in %.1f s (wake: %s)", self.warmup["seconds"], self.reasons.get("wake"))

    async def _load_wake(self, pipeline: VoicePipeline) -> None:
        from jarvis.voice.wakeword import NullWakeEngine, create_wake_engine

        injected = self._injected["wake"]
        if injected is not None:
            if not injected.ready and not isinstance(injected, NullWakeEngine):
                try:
                    await asyncio.to_thread(injected.load)
                except Exception as exc:
                    injected.reason = f"{injected.name} failed to load: {_short(exc)}"
            self.reasons["wake"] = injected.describe()
            pipeline.set_wake_engine(injected)
            return
        if not self._wake_loading:
            reason = (
                "Wake word is turned off in settings (push-to-talk only)."
                if self.settings.wake_word_engine == "none"
                else "Always-listening is off (push-to-talk only)."
            )
            self.reasons["wake"] = reason
            pipeline.wake.reason = reason
            return
        job = asyncio.ensure_future(
            asyncio.to_thread(create_wake_engine, self.settings, self.paths, self.secrets, self.stt, load=True)
        )
        try:
            engine, reason = await asyncio.shield(job)
        except asyncio.CancelledError:
            job.add_done_callback(_close_loaded_engine)  # stopped while the model was loading
            raise
        except Exception as exc:  # pragma: no cover - the factory already falls back
            engine = NullWakeEngine(f"Wake word unavailable: {_short(exc)}")
            reason = engine.reason
        self._wake_loading = False
        if self.pipeline is not pipeline or not self._started:
            engine.close()  # stopped while loading
            return
        self.reasons["wake"] = reason
        pipeline.set_wake_engine(engine)
        log.info("Wake word: %s", reason)

    async def _warm_stt(self) -> None:
        stt = self.stt
        if stt is None or stt.ready:
            return
        try:
            await stt.warm_up()
            self.reasons["stt"] = stt.reason or stt.name
        except Exception as exc:
            message = f"Speech recognition unavailable: {_short(exc)}"
            self.reasons["stt"] = message
            log.warning(message)

    async def _watch_input(self, pipeline: VoicePipeline) -> None:
        """Keep `devices.input` and the "No microphone found" state current while the
        microphone is closed (the pipeline reports problems itself while it captures).

        With the default microphone selected (no input device in the settings), JARVIS follows
        Windows: when another microphone becomes the default, it is opened instead, between
        conversations."""
        from jarvis.voice.audio import probe_input

        spec = self.settings.input_device or None
        follow = spec is None and FOLLOW_DEFAULT_MIC
        refresh = False  # PortAudio scanned the devices when it was imported
        opened_on: str | None = None  # Windows' default microphone while the open stream started
        next_probe = 0.0
        while self._started and self.pipeline is pipeline:
            if not pipeline.source.running:
                opened_on = None
                if time.monotonic() >= next_probe:
                    ok, message = await asyncio.to_thread(probe_input, spec, refresh=refresh)
                    if self.pipeline is not pipeline:
                        return
                    if ok:
                        self._input_name = message
                        if pipeline.input_problem:
                            pipeline.set_disabled(None)
                    else:
                        self._input_name = None
                        if not pipeline.input_problem or pipeline.disabled_reason != message:
                            pipeline.set_disabled(message, source=True)
                    refresh = not ok  # re-scan (hot-plug) only while no microphone is found
                    next_probe = time.monotonic() + INPUT_RECHECK_S
            elif follow:
                current = await asyncio.to_thread(default_microphone_id)
                if opened_on is None:
                    opened_on = current
                elif current and current != opened_on and not pipeline.busy and self.pipeline is pipeline:
                    log.info("Windows has a new default microphone: switching to it")
                    opened_on = None
                    pipeline.reopen_source()
            await asyncio.sleep(min(INPUT_RECHECK_S, DEFAULT_MIC_RECHECK_S) if follow else INPUT_RECHECK_S)

    # ------------------------------------------------------------------------------------------
    # Controls (safe to call from any thread)
    # ------------------------------------------------------------------------------------------
    async def speak(self, text: str, language: str | None = None, interrupt: bool = False, *, wait: bool = True) -> bool:
        """Say `text` (language auto-detected when None). Waits for the conversation to be quiet
        unless `interrupt`; returns after speaking (True) or when skipped/interrupted (False).
        `wait=False` returns as soon as it is queued."""
        pipeline = self.pipeline
        if pipeline is None or not self._started or not self.settings.speak_responses:
            return False
        try:
            return await pipeline.speak(text, language, interrupt, wait=wait)
        except Exception as exc:
            log.warning("Could not speak: %s", _short(exc))
            return False

    def mute(self) -> None:
        """Stop listening (microphone closed, wake word off). JARVIS can still speak."""
        self.muted = True
        if self.pipeline is not None:
            self.pipeline.mute()
        else:
            self._emit_soon()

    def unmute(self) -> None:
        self.muted = False
        if self.pipeline is not None:
            self.pipeline.unmute()
        else:
            self._emit_soon()

    def push_to_talk_start(self) -> None:
        if self.pipeline is not None and self._started:
            self.pipeline.push_to_talk_start()

    def push_to_talk_stop(self) -> None:
        if self.pipeline is not None and self._started:
            self.pipeline.push_to_talk_stop()

    def cancel(self) -> None:
        """Stop speaking/listening now; the command already handed to the core keeps running."""
        if self.pipeline is not None:
            self.pipeline.cancel()

    # ------------------------------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        """`VoiceInfo` for the UI (docs/API.md)."""
        pipeline = self.pipeline if self._started else None
        if pipeline is None:
            state = VoiceState.DISABLED
            error = self._disabled_reason
        else:
            state = pipeline.state
            error = None
            if state is VoiceState.DISABLED:
                error = pipeline.disabled_reason or pipeline.last_error
            elif state is VoiceState.ERROR:
                error = pipeline.last_error
        return {
            "state": state.value,
            "enabled": bool(self._started and self.settings.enabled),
            "muted": self.muted,
            "wake_word": self._wake_word(pipeline),
            "engines": {
                "wake": self._wake_engine_name(pipeline),
                "stt": self.stt.name if (self.stt is not None and pipeline is not None) else "none",
                "tts": self.tts.name if (self.tts is not None and pipeline is not None) else "none",
            },
            "devices": {"input": self._input_device_name(pipeline), "output": self._output_name if pipeline else None},
            "last_transcript": pipeline.last_transcript.text if pipeline and pipeline.last_transcript else None,
            "error": error,
        }

    def details(self) -> dict[str, Any]:
        """Diagnostics beyond `info()`: why each engine was chosen, warm-up results, counters."""
        pipeline = self.pipeline
        return {
            **self.info(),
            "reasons": dict(self.reasons),
            "warmup": dict(self.warmup),
            "stats": dict(pipeline.stats) if pipeline else {},
            "ready": {
                "wake": bool(pipeline and pipeline.wake.ready),
                "stt": bool(self.stt and self.stt.ready),
                "tts": bool(self.tts and self.tts.available),
            },
        }

    def list_devices(self) -> dict[str, Any]:
        """Audio devices for the settings UI. Never raises."""
        try:
            from jarvis.voice.audio import list_devices

            return list_devices()
        except Exception as exc:
            return {"input": [], "output": [], "default_input": None, "default_output": None, "error": _short(exc)}

    def _wake_word(self, pipeline: VoicePipeline | None) -> str:
        if pipeline is None:
            return ""
        if pipeline.wake.keyword:
            return pipeline.wake.keyword
        if self._wake_loading:
            return (self.settings.wake_word_model or "hey_jarvis").replace("_", " ")
        return ""

    def _wake_engine_name(self, pipeline: VoicePipeline | None) -> str:
        if pipeline is None:
            return "none"
        if self._wake_loading and not pipeline.wake.ready:
            return "loading"
        return pipeline.wake.name

    def _input_device_name(self, pipeline: VoicePipeline | None) -> str | None:
        if pipeline is None:
            return None
        if pipeline.source.running and pipeline.source.device_name:
            return pipeline.source.device_name
        if pipeline.input_problem:
            return None
        return self._input_name or pipeline.source.device_name

    # ------------------------------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------------------------------
    async def _disable(self, reason: str) -> None:
        self._disabled_reason = reason
        log.info("Voice disabled: %s", reason)
        await self._emit_state(VoiceState.DISABLED, reason)

    async def _emit_state(self, state: VoiceState, error: str | None) -> None:
        try:
            await self.events.on_state(state, {"muted": self.muted, "trigger": None, "error": error})
        except Exception:
            log.debug("Voice event sink failed", exc_info=True)

    def _emit_soon(self) -> None:
        """Announce the (disabled) state after mute/unmute while no pipeline runs."""
        coro = self._emit_state(VoiceState.DISABLED, self._disabled_reason)
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            self._spawn(coro)
        elif loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            coro.close()

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        error = None if task.cancelled() else task.exception()
        if error is not None:
            log.warning("Voice background task failed: %s", _short(error))


__all__ = ["INPUT_RECHECK_S", "VoiceService"]
