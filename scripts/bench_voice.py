"""Measure how soon JARVIS starts answering a spoken question on this computer, with its free models:

    python scripts/bench_voice.py              # the questions below, two rounds each
    python scripts/bench_voice.py --compare    # also as before: speaking only once the whole answer is written
    python scripts/bench_voice.py --json

JARVIS runs in a fresh temporary home (its own memory and settings, deleted afterwards) with the models the
real settings name (the chat model, speech recognition and the voice) and paid AI off. No microphone is opened
and nothing is played: each question is first spoken by JARVIS's own voice into memory, and that recording is
what JARVIS hears. Only the voice (edge-tts, when online) leaves the computer.

Measured from the moment the "user" stops speaking:
  stt     the question recognised (speech to text)
  text    the first sentence of the answer written
  sound   that sentence synthesized, ready to play: when the user hears JARVIS start
  whole   the whole answer written
(the end-of-speech pause, `voice.end_silence_ms`, comes before all of these and is not included).
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

    s = load_settings()
    return {"ai": {"local": s.ai.local.model_dump(mode="json"), "allow_paid": False},
            "computer": {"models_dir": s.computer.models_dir or str(_default_models() / "computer")},
            "voice": s.voice.model_dump(mode="json")}


def _default_models() -> Path:
    from jarvis.config.paths import resolve_paths
    from jarvis.config.settings import Settings

    return resolve_paths(Settings().paths).models


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
        while server is not None and not server.running and time.perf_counter() - started < 300:
            await asyncio.sleep(0.2)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--compare", action="store_true", help="also measure speaking only after the whole answer")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    real = real_settings()                               # read before JARVIS_HOME points at the temporary home
    home = Path(tempfile.mkdtemp(prefix="jarvis-voice-bench-"))
    os.environ["JARVIS_HOME"] = str(home)
    os.environ["JARVIS_KEYRING"] = "0"
    try:
        result = asyncio.run(run(args.rounds, real, compare=args.compare))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    warm = f" · first-run warm-up {result['preload_ms'] / 1000:.1f} s" if result.get("preload_ms") else ""
    print(f"ready after {result['ready_s']} s{warm} · chat model: {result['chat_model']}")
    print(f"speech recognition: {result['stt_engine']} · voice: {result['tts_engine']}")
    for key, ms in result["ms"].items():
        s = result["said"].get(key, {})
        print(f"  {key:20} stt {ms['stt']:5} ms · first sentence {ms['text']:5} ms · first sound ready {ms['sound']:5} ms"
              f" · whole answer {ms['whole']:5} ms  ({s.get('how', '')})")
        print(f"{'':23}heard: {s.get('heard', '')!r}; first: {s.get('first', '')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
