"""Voice self-test: ``python -m jarvis.voice.selftest`` (options: ``--help``).

Runs on the user's PC and reports, check by check (a failure never stops the others):

1. packages    — which optional voice packages are installed;
2. devices     — microphones and speakers PortAudio can see, and the configured ones;
3. microphone  — opens the microphone for a moment and measures the level (pure digital
                 silence usually means Windows is blocking microphone access);
4. tts         — synthesizes a Hebrew and an English sentence (which engine/voice was used);
5. playback    — plays both sentences through the configured output (``--no-play`` skips);
6. stt         — loads speech recognition (downloads the model on first use) and transcribes
                 the synthesized sentences back: a TTS → STT round trip with timing;
7. wake        — loads the wake-word engine (downloads the model on first use), measures its
                 CPU cost per 80 ms frame and feeds it a synthesized "Hey JARVIS";
8. phrases     — the acknowledgement cache (instant playback of "כן?" / "Yes?").
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from jarvis.common.checks import CheckReport, format_checks, short_error
from jarvis.common.text import normalize

if TYPE_CHECKING:  # pragma: no cover
    from jarvis.config.paths import AppPaths
    from jarvis.config.settings import VoiceSettings
    from jarvis.security.secrets import SecretStore
    from jarvis.voice.audio import AudioClip
    from jarvis.voice.stt import STTEngine
    from jarvis.voice.tts import TTSEngine

log = logging.getLogger(__name__)

SAMPLES = {
    "he": "שלום, אני ג'ארוויס. הבדיקה הקולית עובדת.",
    "en": "Hello, I am JARVIS. The voice self test is working.",
}
WAKE_SAMPLE = "Hey Jarvis!"

PACKAGES: dict[str, tuple[str, bool]] = {  # module → (what it is for, required for the default setup)
    "numpy": ("audio processing", True),
    "sounddevice": ("microphone and speaker access", True),
    "webrtcvad": ("end-of-speech detection (energy detector is the fallback)", False),
    "openwakeword": ("'Hey JARVIS' wake word", True),
    "onnxruntime": ("wake-word inference", True),
    "faster_whisper": ("local speech recognition", True),
    "edge_tts": ("neural voices (online)", True),
    "miniaudio": ("decoding neural-voice audio", True),
    "pyttsx3": ("offline voices (SAPI5)", True),
    "openai": ("cloud speech recognition (optional)", False),
    "pvporcupine": ("Porcupine wake word (optional)", False),
}

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def _have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def similarity(expected: str, heard: str) -> float:
    """Share of the expected words that were recognised (order-insensitive), 0..1."""
    want = [w.strip("'") for w in _WORD_RE.findall(normalize(expected))]
    want = [w for w in want if w]
    got = {w.strip("'") for w in _WORD_RE.findall(normalize(heard))}
    if not want:
        return 0.0
    return sum(1 for w in want if w in got) / len(want)


async def run_selftest(
    settings: VoiceSettings,
    paths: AppPaths,
    secrets: SecretStore | None,
    *,
    play: bool = True,
    microphone: bool = True,
    stt: bool = True,
    wake: bool = True,
    mic_seconds: float = 1.5,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run every check and return ``{"ok", "checks": [{name, status, detail, seconds, data?}], ...}``.

    status is "ok" | "warn" | "fail" | "skip"; ``ok`` is False when any check failed."""
    report = CheckReport(progress)
    clips: dict[str, AudioClip] = {}
    tts_engine: TTSEngine | None = None
    stt_engine: STTEngine | None = None
    try:
        if not _check_packages(report):
            return report.finish()
        await report.guard("devices", _check_devices(report, settings))
        if microphone:
            await report.guard("microphone", _check_microphone(report, settings, mic_seconds))
        else:
            report.add("microphone", "skip", "skipped (--no-mic)")
        tts_engine = await report.guard("tts", _check_tts(report, settings, clips))
        if play:
            await report.guard("playback", _check_playback(report, settings, clips))
        else:
            report.add("playback", "skip", "skipped (--no-play)")
        if stt:
            stt_engine = await report.guard("stt", _check_stt(report, settings, paths, secrets, clips))
        else:
            report.add("stt", "skip", "skipped (--no-stt)")
        if wake:
            await report.guard("wake", _check_wake(report, settings, paths, secrets, stt_engine, tts_engine))
        else:
            report.add("wake", "skip", "skipped (--no-wake)")
        if tts_engine is not None:
            await report.guard("phrases", _check_phrases(report, paths, tts_engine))
    finally:
        if stt_engine is not None:
            with contextlib.suppress(Exception):
                stt_engine.close()
        if tts_engine is not None:
            with contextlib.suppress(Exception):
                await tts_engine.close()
    return report.finish()


# ---------------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------------
def _check_packages(report: CheckReport) -> bool:
    installed = {m: _have(m) for m in PACKAGES}
    missing_required = [m for m, ok in installed.items() if not ok and PACKAGES[m][1]]
    missing_optional = [m for m, ok in installed.items() if not ok and not PACKAGES[m][1]]
    data: dict[str, Any] = {"installed": [m for m, ok in installed.items() if ok],
                            "missing": missing_required + missing_optional}
    if not installed["numpy"]:
        report.add("packages", "fail", 'numpy is missing — install the voice extra: pip install "jarvis-assistant[voice]"', **data)
        return False
    if missing_required:
        detail = "missing: " + ", ".join(f"{m} ({PACKAGES[m][0]})" for m in missing_required)
        report.add("packages", "warn", detail, **data)
    else:
        optional = f" (optional, not installed: {', '.join(missing_optional)})" if missing_optional else ""
        report.add("packages", "ok", "all voice packages installed" + optional, **data)
    return True


async def _check_devices(report: CheckReport, settings: VoiceSettings) -> None:
    from jarvis.voice.audio import AudioError, list_devices, resolve_device

    report.note("Listing audio devices…")
    devices = await asyncio.to_thread(list_devices)
    inputs = [d["name"] for d in devices["input"]]
    outputs = [d["name"] for d in devices["output"]]
    chosen: dict[str, str | None] = {"input": None, "output": None}
    problems = []
    for kind, spec in (("input", settings.input_device), ("output", settings.output_device)):
        try:
            device = await asyncio.to_thread(resolve_device, spec or None, kind)
            chosen[kind] = f"{device.name} [{device.hostapi}]"
        except AudioError as exc:
            problems.append(exc.message)
    data = {"inputs": inputs, "outputs": outputs, "default_input": devices["default_input"],
            "default_output": devices["default_output"], "selected": chosen}
    detail = f"microphone: {chosen['input'] or 'none'}; output: {chosen['output'] or 'none'}"
    detail += f" ({len(inputs)} inputs, {len(outputs)} outputs)"
    if devices.get("error") and not inputs and not outputs:
        report.add("devices", "fail", f"{devices['error']}", **data)
    elif chosen["input"] is None:
        report.add("devices", "fail", "No microphone found — " + detail, **data)
    elif chosen["output"] is None:
        report.add("devices", "warn", "No speaker/headphones found for PortAudio — " + detail, **data)
    else:
        report.add("devices", "ok", detail, **data)


async def _check_microphone(report: CheckReport, settings: VoiceSettings, seconds: float) -> None:
    from jarvis.voice.audio import FRAME_SECONDS, AudioError, MicrophoneSource, np_module, rms_level

    np = np_module()
    report.note(f"Recording {seconds:.1f} s from the microphone (say something)…")
    source = MicrophoneSource(settings.input_device or None)
    started = time.monotonic()
    try:
        await source.start()
    except AudioError as exc:
        report.add("microphone", "fail", exc.message, seconds=time.monotonic() - started)
        return
    frames: list[Any] = []
    try:
        wanted = max(1, round(seconds / FRAME_SECONDS))
        while len(frames) < wanted:
            frame = await asyncio.wait_for(source.read(), timeout=2.0)
            if frame is None:
                break
            frames.append(frame)
    except TimeoutError:
        pass
    finally:
        await source.stop()
    elapsed = time.monotonic() - started
    name = source.device_name or "microphone"
    if not frames:
        report.add("microphone", "fail", f"{name}: no audio arrived", seconds=elapsed)
        return
    audio = np.concatenate(frames)
    levels = [rms_level(f) for f in frames]
    data = {"device": name, "device_rate": source.device_rate, "frames": len(frames), "peak_level": round(max(levels), 3),
            "mean_level": round(sum(levels) / len(levels), 3), "dropped": source.dropped}
    if not np.any(audio):
        report.add(
            "microphone",
            "warn",
            f"{name} delivers pure silence — Windows may be blocking it: Settings → Privacy & security → "
            "Microphone → allow desktop apps; also check the input volume/mute",
            seconds=elapsed,
            **data,
        )
        return
    report.add(
        "microphone",
        "ok",
        f"{name} @ {source.device_rate} Hz: {len(frames)} frames, level peak {max(levels):.2f} (0..1)",
        seconds=elapsed,
        **data,
    )


async def _check_tts(report: CheckReport, settings: VoiceSettings, clips: dict[str, AudioClip]) -> TTSEngine | None:
    from jarvis.voice.tts import FallbackTTS, create_tts_engine

    tts, reason = create_tts_engine(settings)
    report.note(f"Speech synthesis: {reason}…")
    started = time.monotonic()
    await tts.prepare()
    results: dict[str, Any] = {}
    errors: list[str] = []
    for lang, text in SAMPLES.items():
        t0 = time.monotonic()
        try:
            clip = await tts.synthesize(text, lang)
        except Exception as exc:
            errors.append(f"{lang}: {short_error(exc)}")
            continue
        clips[lang] = clip
        results[lang] = {"engine": clip.engine, "voice": clip.voice, "audio_s": round(clip.duration_s, 2),
                         "seconds": round(time.monotonic() - t0, 2)}
        if isinstance(tts, FallbackTTS) and tts.last_errors:
            results[lang]["fallback_because"] = tts.last_errors
    elapsed = time.monotonic() - started
    parts = [f"{lang}: {r['engine']} '{r['voice']}' {r['audio_s']} s audio in {r['seconds']} s" for lang, r in results.items()]
    detail = "; ".join(parts + errors) or reason
    if not results:
        report.add("tts", "fail", f"no speech could be synthesized ({detail})", seconds=elapsed, reason=reason, errors=errors)
        return tts
    fell_back = any("fallback_because" in r for r in results.values())
    status = "warn" if errors or fell_back else "ok"
    report.add("tts", status, detail, seconds=elapsed, reason=reason, results=results, errors=errors)
    return tts


async def _check_playback(report: CheckReport, settings: VoiceSettings, clips: dict[str, AudioClip]) -> None:
    from jarvis.voice.audio import AudioError, NullSink, create_sink

    if not clips:
        report.add("playback", "skip", "nothing to play (speech synthesis failed)")
        return
    sink, reason = await asyncio.to_thread(create_sink, settings.output_device)
    if isinstance(sink, NullSink):
        report.add("playback", "fail", reason)
        return
    report.note(f"Playing the test sentences on {sink.device_name}…")
    started = time.monotonic()
    try:
        for clip in clips.values():
            await sink.play(clip)
    except AudioError as exc:
        report.add("playback", "fail", f"{sink.device_name}: {exc.message}", seconds=time.monotonic() - started)
        return
    finally:
        await sink.close()
    report.add("playback", "ok", f"played {len(clips)} sentence(s) on {reason}", seconds=time.monotonic() - started)


async def _check_stt(
    report: CheckReport, settings: VoiceSettings, paths: AppPaths, secrets: SecretStore | None, clips: dict[str, AudioClip]
) -> STTEngine | None:
    from jarvis.voice.audio import SAMPLE_RATE, np_module
    from jarvis.voice.stt import create_stt_engine

    np = np_module()
    engine, reason = create_stt_engine(settings, paths, secrets)
    report.note(f"Speech recognition: {reason} (the model is downloaded on first use; this can take minutes)…")
    started = time.monotonic()
    try:
        await asyncio.to_thread(engine.load)
        await asyncio.to_thread(engine.settle, 300.0)  # the graphics card's model takes over in the background
    except Exception as exc:
        report.add("stt", "fail", f"{reason}: {short_error(exc)}", seconds=time.monotonic() - started)
        return engine
    load_s = time.monotonic() - started
    if not clips:
        report.add("stt", "warn", f"{engine.describe() or reason} loaded in {load_s:.1f} s; no audio to transcribe", seconds=load_s)
        return engine
    results: dict[str, Any] = {}
    worst = "ok"
    for lang, clip in clips.items():
        pad = np.zeros(int(0.3 * SAMPLE_RATE), dtype=np.int16)
        audio = np.concatenate((pad, clip.resampled(SAMPLE_RATE).samples, pad))
        t0 = time.monotonic()
        try:
            heard = await engine.transcribe(audio, settings.stt_language)
        except Exception as exc:
            results[lang] = {"error": short_error(exc)}
            worst = "fail"
            continue
        took = time.monotonic() - t0
        score = similarity(SAMPLES[lang], heard.text)
        results[lang] = {"heard": heard.text, "language": heard.language, "similarity": round(score, 2),
                         "audio_s": round(len(audio) / SAMPLE_RATE, 2), "seconds": round(took, 2)}
        if score < (0.6 if lang == "en" else 0.4) and worst == "ok":
            worst = "warn"
    detail = f"{engine.describe() or reason}, loaded in {load_s:.1f} s; " + "; ".join(
        f"{lang}: {r['error']}" if "error" in r else f"{lang}: \"{r['heard']}\" ({r['similarity']:.0%} match, "
        f"{r['seconds']} s for {r['audio_s']} s audio)"
        for lang, r in results.items()
    )
    report.add("stt", worst, detail, seconds=time.monotonic() - started, load_s=round(load_s, 2), results=results)
    return engine


async def _check_wake(
    report: CheckReport,
    settings: VoiceSettings,
    paths: AppPaths,
    secrets: SecretStore | None,
    stt_engine: STTEngine | None,
    tts: TTSEngine | None,
) -> None:
    from jarvis.voice.audio import FRAME_SECONDS, SAMPLE_RATE, iter_frames, np_module
    from jarvis.voice.wakeword import NullWakeEngine, TranscriptWakeEngine, create_wake_engine

    np = np_module()
    report.note("Wake word: loading the engine (the model is downloaded on first use)…")
    started = time.monotonic()
    engine, reason = await asyncio.to_thread(create_wake_engine, settings, paths, secrets, stt_engine, load=True)
    load_s = time.monotonic() - started
    try:
        if isinstance(engine, NullWakeEngine):
            report.add("wake", "warn" if settings.wake_word_engine == "none" else "fail", reason, seconds=load_s)
            return
        data: dict[str, Any] = {"engine": engine.name, "keyword": engine.keyword, "load_s": round(load_s, 2)}
        detail = f"{reason} (loaded in {load_s:.1f} s)"
        if engine.threaded:  # measure the continuous CPU cost on silence-level noise
            rng = np.random.default_rng(1)
            noise = (rng.standard_normal(SAMPLE_RATE * 2) * 60).astype(np.int16)
            frames = list(iter_frames(noise))
            t0 = time.perf_counter()
            for frame in frames:
                engine.process(frame)
            per_frame = (time.perf_counter() - t0) / len(frames)
            engine.reset()
            data["ms_per_frame"] = round(per_frame * 1000, 2)
            data["cpu_percent_of_core"] = round(100 * per_frame / FRAME_SECONDS, 1)
            detail += f"; {per_frame * 1000:.1f} ms per 80 ms frame ≈ {data['cpu_percent_of_core']}% of one core"
        clip = None
        if tts is not None:
            with contextlib.suppress(Exception):
                clip = await tts.synthesize(WAKE_SAMPLE, "en")
        if clip is not None:
            pad = np.zeros(SAMPLE_RATE, dtype=np.int16)
            audio = np.concatenate((pad, clip.resampled(SAMPLE_RATE).samples, pad))
            if isinstance(engine, TranscriptWakeEngine):
                hit = None
                for frame in iter_frames(audio):
                    hit = hit or await engine.feed(frame)
                score = 1.0 if hit else 0.0
            else:
                score = 0.0
                for frame in iter_frames(audio):
                    score = max(score, float(await asyncio.to_thread(engine.process, frame)))
            detected = engine.detected(score)
            data.update({"synthetic_score": round(score, 3), "synthetic_detected": detected})
            detail += (
                f"; synthesized \"{WAKE_SAMPLE}\": score {score:.2f} → {'detected' if detected else 'not detected'}"
                + ("" if detected else " (synthetic voices do not always trigger it — try saying it yourself)")
            )
        report.add("wake", "ok", detail, seconds=time.monotonic() - started, **data)
    finally:
        engine.close()


async def _check_phrases(report: CheckReport, paths: AppPaths, tts: TTSEngine) -> None:
    from jarvis.voice.tts import PhraseCache

    cache = PhraseCache(tts, paths.tts_cache)
    timings = {}
    failures = []
    for lang in ("he", "en"):
        try:
            clip = await cache.get("listening", lang)
        except Exception as exc:
            failures.append(f"{lang}: {short_error(exc)}")
            continue
        if clip is None:
            continue
        t0 = time.perf_counter()
        cache.lookup("listening", lang)
        timings[lang] = round((time.perf_counter() - t0) * 1000, 3)
    if not timings:
        report.add("phrases", "fail", "acknowledgements could not be cached: " + "; ".join(failures))
        return
    detail = f"acknowledgements cached in {paths.tts_cache}; lookup " + ", ".join(f"{k} {v} ms" for k, v in timings.items())
    report.add("phrases", "warn" if failures else "ok", detail + ("; " + "; ".join(failures) if failures else ""), lookup_ms=timings)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
def format_report(report: dict[str, Any]) -> str:
    return format_checks("JARVIS voice self-test", report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.voice.selftest", description="Check JARVIS's voice stack on this PC.")
    parser.add_argument("--no-play", action="store_true", help="do not play audio")
    parser.add_argument("--no-mic", action="store_true", help="do not open the microphone")
    parser.add_argument("--no-stt", action="store_true", help="skip speech recognition (no model download)")
    parser.add_argument("--no-wake", action="store_true", help="skip the wake-word engine (no model download)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="show log messages")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Hebrew text on a legacy console code page
        with contextlib.suppress(Exception):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    from jarvis.common.errors import JarvisError
    from jarvis.config import load_settings, resolve_paths
    from jarvis.security.secrets import SecretStore

    try:
        settings = load_settings()
        paths = resolve_paths(settings.paths).ensure()
    except (JarvisError, OSError) as exc:
        print(f"Cannot load the JARVIS configuration: {exc}", file=sys.stderr)
        return 2
    secrets = SecretStore(paths.data)

    def progress(message: str) -> None:
        if not args.json:
            print(f"  … {message}", flush=True)

    report = asyncio.run(
        run_selftest(
            settings.voice,
            paths,
            secrets,
            play=not args.no_play,
            microphone=not args.no_mic,
            stt=not args.no_stt,
            wake=not args.no_wake,
            progress=progress,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else "\n" + format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
