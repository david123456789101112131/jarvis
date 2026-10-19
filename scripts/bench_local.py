"""Measure how fast JARVIS answers on this computer, with nothing leaving it and nothing touched:

    python scripts/bench_local.py                    # this checkout
    python scripts/bench_local.py --src <other>/src  # another version, for a before/after comparison
    python scripts/bench_local.py --json             # the numbers as JSON

JARVIS runs in a fresh temporary home (its own memory and settings, deleted afterwards) with no AI
provider at all and voice off, so no request goes to a cloud AI and nothing on the desktop changes. It
measures: importing the code, building and starting the app, understanding a command, and the time from
an everyday command (time, date, greetings, help, remembering and recalling, the computer's status) to
JARVIS's first reply; and how many of a list of everyday wordings are understood on the spot (the rest
would need a cloud AI). Every command and wording is listed below, so the same run can be repeated on an
older version (`--src`) for a real comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

COMMANDS = [
    ("time", "מה השעה?"),
    ("time_en", "what time is it?"),
    ("date", "איזה יום היום?"),
    ("greeting", "שלום ג'רוויס"),
    ("thanks", "תודה"),
    ("help", "מה אתה יודע לעשות?"),
    ("remember", "תזכור שהצבע האהוב עליי הוא כחול"),
    ("recall", "מה הצבע האהוב עליי?"),
    ("status", "מה מצב המחשב?"),
]

# Everyday wordings and what each one asks for; "understood on the spot" means the local detector got it.
WORDINGS: dict[str, list[str]] = {
    "greeting": ["שלום ג'רוויס", "היי ג'רוויס", "בוקר טוב ג'רוויס", "ערב טוב ג'רוויס", "מה נשמע ג'רוויס?",
                 "מה שלומך?", "מה שלומך ג'רוויס?", "hello jarvis", "hi jarvis", "good morning jarvis", "hey jarvis",
                 "how are you?", "how are you jarvis?", "לילה טוב", "good night jarvis"],
    "thanks": ["תודה ג'רוויס", "תודה רבה ג'רוויס", "thanks jarvis", "thank you jarvis", "תודה רבה לך",
               "thank you so much", "תודה רבה רבה"],
    "help": ["מה אתה יודע לעשות?", "מה אתה יכול לעשות?", "במה אתה יכול לעזור לי?", "what can you do?",
             "what can you do for me?", "what do you know how to do?", "מה אתה יודע לעשות ג'רוויס?",
             "איך משתמשים בך?", "how do i use you?"],
    "time": ["מה השעה עכשיו?", "כמה השעה?", "what's the time?", "what time is it now?", "מה השעה ג'רוויס?"],
    "date": ["מה התאריך היום?", "איזה יום היום?", "what day is it today?", "what's today's date?",
             "איזה תאריך היום?"],
    "recall": ["מה אתה זוכר עליי?", "מה אתה יודע עליי?", "מה אתה זוכר?", "what do you remember about me?",
               "what do you know about me?", "מה אתה זוכר עלי?", "what do you remember?"],
    "system_status": ["מה מצב המחשב?", "איך המחשב?", "כמה זיכרון פנוי?", "how's the computer doing?",
                      "how much free disk space?", "מה מצב המחשב ג'רוויס?"],
}


def cold_import_ms(src: Path) -> float:
    code = "import time; t = time.perf_counter(); import jarvis.app; print((time.perf_counter() - t) * 1000)"
    env = {**os.environ, "PYTHONPATH": str(src)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    return float(out.stdout.strip().splitlines()[-1])


def wordings(detect: Any) -> dict[str, Any]:
    missed = [f"{want}: {text}" for want, texts in WORDINGS.items() for text in texts if detect(text).name != want]
    total = sum(len(v) for v in WORDINGS.values())
    return {"understood": total - len(missed), "total": total, "missed": missed}


async def run(rounds: int) -> dict[str, Any]:
    from jarvis.ai.router import ProviderRouter
    from jarvis.app import JarvisApp
    from jarvis.config.settings import Settings

    out: dict[str, Any] = {}
    settings = Settings()
    started = time.perf_counter()
    app = JarvisApp(settings, router=ProviderRouter(settings.ai, []), voice=False, use_keyring=False, port=0)
    out["build_ms"] = round((time.perf_counter() - started) * 1000, 1)
    started = time.perf_counter()
    await app.start()
    out["start_ms"] = round((time.perf_counter() - started) * 1000, 1)
    try:
        replies: asyncio.Queue[tuple[float, str]] = asyncio.Queue()

        def on_chat(msg: Any) -> None:
            if msg.data.get("role") == "jarvis":
                replies.put_nowait((time.perf_counter(), str(msg.data.get("text", ""))))

        app.bus.subscribe("chat.message", on_chat, name="bench")
        detect_ms: list[float] = []
        first: dict[str, list[float]] = {}
        said: dict[str, str] = {}
        await asyncio.sleep(1.0)                 # the background start-up work (the app index) settles first
        for n in range(rounds):
            for key, text in COMMANDS:
                while not replies.empty():
                    replies.get_nowait()
                t0 = time.perf_counter()
                app.core.detector.detect(text)
                detect_ms.append((time.perf_counter() - t0) * 1000)
                t0 = time.perf_counter()
                await app.core.handle_input(text, source="text")
                try:
                    at, reply = await asyncio.wait_for(replies.get(), timeout=20)
                    first.setdefault(key, []).append((at - t0) * 1000)
                except TimeoutError:
                    reply = "(no reply)"
                if n == 0:
                    said[key] = reply.replace("\n", " ")[:90]
                await asyncio.sleep(0.05)
        out["understand_ms"] = round(statistics.median(detect_ms), 2)
        out["first_reply_ms"] = {k: round(statistics.median(v), 1) for k, v in first.items()}
        out["replies"] = said
        out["wordings"] = wordings(app.core.detector.detect)
    finally:
        await app.shutdown()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", type=Path, default=ROOT / "src", help="the src folder of the version to measure")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    src = args.src.resolve()
    home = Path(tempfile.mkdtemp(prefix="jarvis-bench-"))
    os.environ["JARVIS_HOME"] = str(home)
    os.environ["JARVIS_KEYRING"] = "0"
    sys.path.insert(0, str(src))
    try:
        cold = [cold_import_ms(src) for _ in range(3)]
        result = {"src": str(src), "import_ms": round(statistics.median(cold), 1), **asyncio.run(run(args.rounds))}
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    print(f"version: {result['src']}")
    print(f"import {result['import_ms']} ms · build {result['build_ms']} ms · start {result['start_ms']} ms · "
          f"understanding a command {result['understand_ms']} ms")
    for key, ms in result["first_reply_ms"].items():
        print(f"  {key:10} first reply after {ms:7.1f} ms   {result['replies'].get(key, '')}")
    w = result["wordings"]
    print(f"everyday wordings understood on the spot: {w['understood']} of {w['total']}")
    for line in w["missed"]:
        print(f"  needs a cloud AI: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
