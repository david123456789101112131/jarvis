"""`jarvis media bench`: how fast this computer makes an image and a short video, and how much memory it takes.

It drives the real worker, through the same engine the screens use, once per mode asked for ("auto" = whatever the
worker would choose now). For each one it shows the plan first (the RAM and graphics memory each mode needs against
what is free), then times a first image (the load included) and a second one, and one short video. Fixed prompts
and seeds, so two runs, or two modes, can be compared picture by picture. Nothing leaves the computer; what is made
goes to a temporary folder that is deleted at the end, unless `--keep` names one.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis.media import catalog
from jarvis.media.local.engine import LocalEngine, LocalMediaError

IMAGE_PROMPT = ("A friendly helper robot at a desk with a glowing blue holographic screen, warm evening light, "
                "detailed, photographic")
VIDEO_PROMPT = "A cup of hot coffee on a wooden table, steam rising slowly, soft morning light from the window"
SEED = 42
MEASURED = ("vram_peak", "ram_peak", "ram_free_min", "vram_peak_frames", "vram_peak_decode", "tiled_decode",
            "frames_s", "decode_s", "vram_cap")
GB = 2**30


def _gb(value: Any) -> str:
    return f"{float(value) / GB:.1f}" if isinstance(value, (int, float)) else "-"


def describe_plan(plan: dict[str, Any]) -> list[str]:
    lines = [f"  {plan['family']}: free now {_gb(plan.get('ram_free'))} GB RAM, {_gb(plan.get('vram_free'))} GB "
             f"on the card (spare kept: {_gb(plan.get('ram_spare'))} GB RAM)"]
    if plan.get("commit_free") is not None:
        lines.append(f"    Windows can still reserve {_gb(plan['commit_free'])} GB (RAM and paging file together)")
    for mode, need in (plan.get("needs") or {}).items():
        lines.append(f"    {mode:7} needs {_gb(need['ram'])} GB RAM, {_gb(need['vram'])} GB on the card")
    lines.append(f"    → {plan.get('mode') or 'none fits: ' + str(plan.get('refusal'))}")
    return lines


async def run(make_engine: Callable[[str], LocalEngine], *, modes: list[str], kinds: list[str], out: Path,
              image_steps: int, video_steps: int, seconds: int = 3, resolution: str = "480p",
              say: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """Measure each mode in its own worker (so each load is a cold one). Returns one row per thing made."""
    rows: list[dict[str, Any]] = []
    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
    for mode in modes:
        say(f"== {mode}")
        engine = make_engine(mode)
        try:
            for kind in kinds:
                plan = await engine.request("plan", {"family": kind})
                for line in describe_plan(plan):
                    say(line)
                if not plan.get("mode"):
                    rows.append({"mode": mode, "kind": kind, "error": plan.get("refusal")})
                    continue
                for row in await (_images(engine, mode, out, image_steps) if kind == "image" else
                                  _video(engine, mode, out, video_steps, seconds, resolution)):
                    rows.append(row)
                    say("  " + summary(row))
                await engine.stop()                  # the next kind starts in a fresh worker, as in use
        except LocalMediaError as exc:
            rows.append({"mode": mode, "error": f"{exc.kind}: {exc}"})
            say(f"  failed ({exc.kind}): {exc}")
        finally:
            await engine.stop()
    return rows


async def _images(engine: LocalEngine, mode: str, out: Path, steps: int) -> list[dict[str, Any]]:
    width, height = catalog.local_image_size("1:1")
    rows = []
    for number in (1, 2):
        path = out / f"{mode}-image-{number}.png"
        began = time.perf_counter()
        result = await engine.request("image", {"prompt": IMAGE_PROMPT, "width": width, "height": height,
                                                "out": [str(path)], "steps": steps, "seed": SEED})
        total = time.perf_counter() - began
        made = float(sum(result.get("per_image_s") or [total]))
        rows.append({"mode": mode, "ran_as": result.get("mode"), "kind": "image", "number": number,
                     "size": f"{width}x{height}", "steps": steps, "total_s": round(total, 1),
                     "load_s": round(total - made, 1), "make_s": round(made, 1), "path": str(path),
                     **{k: result.get(k) for k in MEASURED}})
    return rows


async def _video(engine: LocalEngine, mode: str, out: Path, steps: int, seconds: int,
                 resolution: str) -> list[dict[str, Any]]:
    width, height = catalog.local_video_size("16:9", resolution)
    frames = catalog.local_frames(seconds)
    path = out / f"{mode}-video.mp4"
    began = time.perf_counter()
    result = await engine.request("video", {"prompt": VIDEO_PROMPT, "width": width, "height": height,
                                            "out": str(path), "frames": frames, "fps": catalog.LOCAL_FPS,
                                            "steps": steps, "seed": SEED})
    total = time.perf_counter() - began
    made = float(result.get("generate_s") or total)
    return [{"mode": mode, "ran_as": result.get("mode"), "kind": "video", "number": 1,
             "size": f"{width}x{height}x{frames}", "steps": steps, "total_s": round(total, 1),
             "load_s": round(max(0.0, total - made), 1), "make_s": round(made, 1), "path": str(path),
             **{k: result.get(k) for k in MEASURED}}]


def summary(row: dict[str, Any]) -> str:
    return (f"{row['kind']} {row['number']} ({row['size']}, {row['steps']} steps, ran as {row['ran_as']}): "
            f"{row['total_s']} s in all, {row['load_s']} s loading, {row['make_s']} s making; card peak "
            f"{_gb(row.get('vram_peak'))} GB, worker RAM peak {_gb(row.get('ram_peak'))} GB, least RAM free "
            f"{_gb(row.get('ram_free_min'))} GB"
            + (f"; frames {row.get('frames_s')} s, card {_gb(row.get('vram_peak_frames'))} GB; decoding "
               f"{row.get('decode_s')} s, card {_gb(row.get('vram_peak_decode'))} GB"
               f"{' (tiled)' if row.get('tiled_decode') else ''}; card cap {_gb(row.get('vram_cap'))} GB"
               if row.get("vram_peak_decode") else ""))


__all__ = ["IMAGE_PROMPT", "VIDEO_PROMPT", "describe_plan", "run", "summary"]
