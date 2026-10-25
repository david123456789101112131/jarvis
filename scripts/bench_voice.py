"""Measure how soon JARVIS starts answering a spoken question on this computer, with its free models:

    python scripts/bench_voice.py              # the questions below, two rounds each
    python scripts/bench_voice.py --compare    # also as before: speaking only once the whole answer is written
    python scripts/bench_voice.py --voice      # through the whole voice path, as the user hears it
    python scripts/bench_voice.py --json

JARVIS runs in a fresh temporary home (its own memory and settings, deleted afterwards) with the models the
real settings name and the real model cache (the chat model, speech recognition and the voice) and paid AI off. No microphone is opened
and nothing is played: each question is first spoken by JARVIS's own voice into memory, and that recording is
what JARVIS hears. Only the voice (edge-tts, when online) leaves the computer.

Measured from the moment the "user" stops speaking:
  stt     the question recognised (speech to text)
  text    the first piece of the answer written (a sentence, or the first clause of a long one)
  sound   that sentence synthesized, ready to play: when the user hears JARVIS start
  whole   the whole answer written
(the end-of-speech pause, `voice.end_silence_ms`, comes before all of these and is not included).

`--voice` instead runs JARVIS's real voice pipeline: the talk key, the question fed as microphone audio at its
real pace, the end-of-speech pause, speech recognition, the answer and the voice, into a silent speaker that
takes as long as the sound would. Measured from the last sound of the question (the pause included):
  first   the first sound JARVIS makes: the short "one moment" cue, or the answer itself
  answer  the answer starts
(`--compare` adds the same without the cue.)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

QUESTIONS = [
    ("joke", "ספר לי בדיחה קצרה"),
    ("explain", "מה ההבדל בין זיכרון RAM לזיכרון של כרטיס מסך?"),
    ("advice", "תן לי טיפ אחד לשינה טובה"),
    ("time", "מה השעה?"),                     # answered by JARVIS itself, without the model
]


def real_settings() -> dict[str, Any]:
    """The parts of the user's settings that say where the models are and how the voice works."""
    from jarvis.config.loader import load_settings
    from jarvis.config.paths import resolve_paths

    s = load_settings()
    cache = resolve_paths(s.paths).cache                 # the speech recognition and wake models already downloaded
    return {"ai": {"local": s.ai.local.model_dump(mode="json"), "allow_paid": False},
            "computer": {"models_dir": s.computer.models_dir or str(cache / "models" / "computer")},
            "paths": {"cache_dir": str(cache)},
            "voice": s.voice.model_dump(mode="json")}


async def run(rounds: int, real: dict[str, Any], *, compare: bool = False) -> dict[str, Any]:
    from jarvis.app import JarvisApp
    from jarvis.config.loader import update_settings
    from jarvis.config.settings import Settings
    from jarvis.voice.audio import SAMPLE_RATE, resample
    from jarvis.voice.base import SpeechStream, Transcript
    from jarvis.voice.stt import create_stt_engine
    from jarvis.voice.tts import create_tts_engine, prepare_speech_text, split_sentences

    settings = update_settings(Settings(), real)
    out: dict[str, Any] = {}
    app = JarvisApp(settings, voice=False, use_keyring=False, serve=False)
    started = time.perf_counter()
    await app.start()
    stt, out["stt_engine"] = create_stt_engine(settings.voice, app.paths, None)
    tts, out["tts_engine"] = create_tts_engine(settings.voice)
    try:
        await stt.warm_up()
        await asyncio.to_thread(stt.settle, 300)        # the graphics card's model, when it loads after the CPU one
        await tts.prepare()
        out["stt_engine"] = stt.describe()
        server = app.chat_server
        warming = [t for t in app._tasks if t.get_name() == "jarvis-chat-preload"]
        if warming:                                      # the start-up warm-up: as when the user asks a while later
            await asyncio.wait(warming, timeout=300)
        out["ready_s"] = round(time.perf_counter() - started, 1)
        out["preload_ms"] = (app.metrics.summary("chat.preload").get("chat.preload") or {}).get("last_ms")
        out["chat_model"] = server.runtime.describe() if server is not None and server.runtime else "none"
        rows: dict[str, list[dict[str, float]]] = {}
        said: dict[str, dict[str, str]] = {}
        modes = ["early", "whole"] if compare else ["early"]
        for n in range(rounds):
            for key, question in QUESTIONS:
              for mode in modes:
                clip = await tts.synthesize(question, "he")
                audio = resample(clip.samples, clip.sample_rate, SAMPLE_RATE)
                t0 = time.perf_counter()
                heard = await stt.transcribe(audio, settings.voice.stt_language)
                t_stt = time.perf_counter()
                stream = SpeechStream(max_chars=900) if mode == "early" else None
                handler = asyncio.ensure_future(app._voice_command(Transcript(
                    text=heard.text, language=heard.language or "he", stream=stream)))
                waiting = asyncio.ensure_future(stream.next() if stream is not None else asyncio.sleep(3600))
                await asyncio.wait({waiting, handler}, return_when=asyncio.FIRST_COMPLETED)
                if stream is not None and waiting.done() and waiting.result():
                    first, how = str(waiting.result()), "while writing"
                else:
                    reply = await handler
                    text = prepare_speech_text((reply.text if reply else "") or "")
                    first, how = (split_sentences(text) or [""])[0], "whole"
                t_text = time.perf_counter()
                body = prepare_speech_text(first)
                if body:
                    await tts.synthesize(body, "he")
                t_sound = time.perf_counter()
                reply = await handler
                if stream is not None:
                    stream.close()
                waiting.cancel()
                t_whole = time.perf_counter()
                key_mode = key if mode == "early" else f"{key} (as before)"
                rows.setdefault(key_mode, []).append({
                    "stt": (t_stt - t0) * 1000, "text": (t_text - t0) * 1000, "sound": (t_sound - t0) * 1000,
                    "whole": (t_whole - t0) * 1000})
                if n == 0:
                    said[key_mode] = {"heard": heard.text, "first": first.strip(), "how": how,
                                      "reply": ((reply.text if reply else "") or "").replace("\n", " ")[:160]}
                await asyncio.sleep(0.3)
        out["ms"] = {k: {m: round(statistics.median(r[m] for r in v)) for m in ("stt", "text", "sound", "whole")}
                     for k, v in rows.items()}
        out["said"] = said
    finally:
        for engine in (stt, tts):
            result = engine.close()
            if asyncio.iscoroutine(result):
                await result
        await app.shutdown()
    return out


async def run_voice_path(rounds: int, real: dict[str, Any], *, compare: bool = False) -> dict[str, Any]:
    from jarvis.app import JarvisApp
    from jarvis.config.loader import update_settings
    from jarvis.config.settings import Settings
    from jarvis.voice.audio import SAMPLE_RATE, ArraySource, AudioClip, NullSink, resample
    from jarvis.voice.base import Transcript, VoiceState
    from jarvis.voice.pipeline import PipelineOptions, VoicePipeline
    from jarvis.voice.stt import create_stt_engine
    from jarvis.voice.tts import PhraseCache, create_tts_engine
    from jarvis.voice.wakeword import NullWakeEngine

    class Speaker(NullSink):
        """Plays nothing, for as long as the sound lasts; notes when each clip starts."""

        def __init__(self) -> None:
            super().__init__(realtime=True)
            self.starts: list[float] = []

        async def _play(self, clip: AudioClip) -> bool:
            self.starts.append(time.perf_counter())
            return await super()._play(clip)

    class Events:
        def __init__(self) -> None:
            self.state: VoiceState | None = None
            self.heard = ""

        async def on_state(self, state: VoiceState, info: dict[str, Any]) -> None:
            self.state = state

        async def on_level(self, level: float) -> None: ...
        async def on_pipeline(self, step: str, status: str, label: str) -> None: ...
        async def on_speech(self, text: str, language: str) -> None: ...

        async def on_transcript(self, transcript: Transcript) -> None:
            if transcript.final:
                self.heard = transcript.text

    def words_only(samples: Any) -> Any:
        """The recording without its silent start and end: the voice adds up to a second of quiet after the words,
        and JARVIS stops listening during it (that pause is the end of the question)."""
        import numpy as np

        loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > 300)
        if not len(loud):
            return samples
        edge = int(0.04 * SAMPLE_RATE)
        return samples[max(0, int(loud[0]) - edge): int(loud[-1]) + edge]

    async def until(check: Any, what: str, timeout: float = 120.0) -> None:
        deadline = time.perf_counter() + timeout
        while not check():
            if time.perf_counter() > deadline:
                raise TimeoutError(f"waited too long for {what}")
            await asyncio.sleep(0.01)

    settings = update_settings(Settings(), real)
    out: dict[str, Any] = {}
    app = JarvisApp(settings, voice=False, use_keyring=False, serve=False)
    started = time.perf_counter()
    await app.start()
    stt, out["stt_engine"] = create_stt_engine(settings.voice, app.paths, None)
    tts, out["tts_engine"] = create_tts_engine(settings.voice)
    source = ArraySource(realtime=True, fill_silence=True, name="bench")
    speaker, events = Speaker(), Events()
    pipeline: VoicePipeline | None = None
    try:
        await stt.warm_up()
        await asyncio.to_thread(stt.settle, 300)
        cache = PhraseCache(tts, app.paths.tts_cache)
        await cache.warm(("he",))
        out["stt_engine"] = stt.describe()
        server = app.chat_server
        warming = [t for t in app._tasks if t.get_name() == "jarvis-chat-preload"]
        if warming:
            await asyncio.wait(warming, timeout=300)
        out["ready_s"] = round(time.perf_counter() - started, 1)
        out["preload_ms"] = (app.metrics.summary("chat.preload").get("chat.preload") or {}).get("last_ms")
        out["chat_model"] = server.runtime.describe() if server is not None and server.runtime else "none"
        options = PipelineOptions.from_settings(settings.voice)
        out["end_silence_ms"] = options.end_silence_ms
        pipeline = VoicePipeline(source=source, sink=speaker, wake=NullWakeEngine("bench"), stt=stt, tts=tts,
                                 handler=app._voice_command, events=events, options=options,
                                 phrase_cache=cache)
        pipeline.start()
        await until(lambda: events.state is VoiceState.IDLE, "the voice to start", 60)
        cue_s = options.thinking_cue_s
        rows: dict[str, list[dict[str, float]]] = {}
        said: dict[str, dict[str, str]] = {}
        cut: dict[str, int] = {}
        modes = ["cue", "no cue"] if compare else ["cue"]
        for n in range(rounds):
            for key, question in QUESTIONS:
                clip = await tts.synthesize(question, "he")
                audio = words_only(resample(clip.samples, clip.sample_rate, SAMPLE_RATE))
                for mode in modes:
                    options.thinking_cue_s = cue_s if mode == "cue" else 0.0
                    await until(lambda: events.state is VoiceState.IDLE and not speaker.playing, "quiet")
                    cues = pipeline.stats["cues"]
                    pipeline.talk_key()                                  # Ctrl tapped: "yes?" and listening
                    await until(lambda: events.state is VoiceState.LISTENING and not speaker.playing, "listening")
                    await asyncio.sleep(0.3)
                    source.append(audio)
                    await until(lambda: not source.pending or events.state is not VoiceState.LISTENING,
                                "the question to be heard", 60)
                    end = time.perf_counter()                            # the question's last sound
                    was_cut = source.clear() > 0                         # stopped listening in the middle of it
                    await until(lambda: events.state in (VoiceState.SPEAKING, VoiceState.IDLE, VoiceState.ERROR)
                                and events.state is not VoiceState.PROCESSING, "the answer")
                    await until(lambda: events.state is not VoiceState.SPEAKING and not speaker.playing, "the end")
                    if events.state is VoiceState.LISTENING:             # a question back: stop listening
                        pipeline.cancel()
                    starts = [t - end for t in speaker.starts if t >= end]
                    cued = pipeline.stats["cues"] > cues
                    if was_cut or not starts:                            # not timed: say so instead
                        cut[key] = cut.get(key, 0) + 1
                        speaker.starts.clear()
                        continue
                    answer = starts[1] if cued and len(starts) > 1 else starts[0]
                    key_mode = key if mode == "cue" else f"{key} (no cue)"
                    rows.setdefault(key_mode, []).append({"first": starts[0] * 1000, "answer": answer * 1000})
                    if n == 0:
                        said[key_mode] = {"heard": events.heard, "cue": "yes" if cued else "no"}
                    speaker.starts.clear()
                    await asyncio.sleep(0.5)
        out["ms"] = {k: {m: round(statistics.median(r[m] for r in v)) for m in ("first", "answer")}
                     for k, v in rows.items()}
        out["said"] = said
        out["cut"] = cut
    finally:
        if pipeline is not None:
            await pipeline.stop()
        for engine in (stt, tts):
            result = engine.close()
            if asyncio.iscoroutine(result):
                await result
        await app.shutdown()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--compare", action="store_true", help="also measure speaking only after the whole answer")
    parser.add_argument("--voice", action="store_true", help="through the whole voice path, as the user hears it")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    real = real_settings()                               # read before JARVIS_HOME points at the temporary home
    home = Path(tempfile.mkdtemp(prefix="jarvis-voice-bench-"))
    os.environ["JARVIS_HOME"] = str(home)
    os.environ["JARVIS_KEYRING"] = "0"
    try:
        bench = run_voice_path if args.voice else run
        result = asyncio.run(bench(args.rounds, real, compare=args.compare))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    warm = f" · first-run warm-up {result['preload_ms'] / 1000:.1f} s" if result.get("preload_ms") else ""
    print(f"ready after {result['ready_s']} s{warm} · chat model: {result['chat_model']}")
    print(f"speech recognition: {result['stt_engine']} · voice: {result['tts_engine']}")
    if args.voice:
        print(f"from the question's last sound (the {result['end_silence_ms']} ms end-of-speech pause included):")
        for key, ms in result["ms"].items():
            s = result["said"].get(key, {})
            print(f"  {key:20} first sound {ms['first']:5} ms · answer starts {ms['answer']:5} ms"
                  f"  (cue: {s.get('cue', '')}; heard: {s.get('heard', '')!r})")
        for key, n in result.get("cut", {}).items():
            print(f"  {key:20} not timed {n}x: JARVIS stopped listening before the question ended, or said nothing")
        return 0
    for key, ms in result["ms"].items():
        s = result["said"].get(key, {})
        print(f"  {key:20} stt {ms['stt']:5} ms · first sentence {ms['text']:5} ms · first sound ready {ms['sound']:5} ms"
              f" · whole answer {ms['whole']:5} ms  ({s.get('how', '')})")
        print(f"{'':23}heard: {s.get('heard', '')!r}; first: {s.get('first', '')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
