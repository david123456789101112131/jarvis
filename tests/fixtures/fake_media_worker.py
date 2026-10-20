"""A stand-in for the local media worker's models, behind the real protocol (`worker.serve`), for JARVIS's tests.

It writes real (tiny) PNG files and a file that starts like an MP4, reports progress like the real models, and
logs every request it gets to `--log` (one JSON line each) so a test can see what JARVIS asked for. Special
prompts: "slow" works until it is stopped, "crash" ends the process, "oom" answers out of graphics memory,
"lowmem" refuses for want of RAM (with the numbers), "ramout" ends the way the memory watch ends the real one.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from jarvis.media.local.worker import (
    LOW_MEMORY_EXIT,
    MODES,
    WorkerError,
    serve,
    translate_lines,
)

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Fake:
    def __init__(self, log: Path, gpu: bool) -> None:
        self.log = log
        self.gpu = gpu
        self.loaded: str | None = None

    def _note(self, req: dict[str, Any]) -> None:
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(req, ensure_ascii=False) + "\n")

    def info(self) -> dict[str, Any]:
        return {"torch": "fake", "gpu": self.gpu, "device": "Fake GPU" if self.gpu else None, "loaded": self.loaded,
                "vram_total": 16 * 2**30 if self.gpu else None}

    def _work(self, req: dict[str, Any], family: str, progress: Any) -> None:
        self._note(req)
        # The video screen sends its parts as labelled lines ("Scene: slow"); the special word is the scene.
        prompt = str(req.get("prompt") or "").removeprefix("Scene:").strip()
        if prompt == "crash":
            os._exit(3)
        if prompt == "oom":
            raise WorkerError("CUDA out of memory", "oom")
        if prompt == "lowmem":
            raise WorkerError("not enough free memory", "low_memory", need_ram=18 * 2**30, free_ram=9 * 2**30,
                              need_vram=5 * 2**30, free_vram=10 * 2**30)
        if prompt == "ramout":
            os._exit(LOW_MEMORY_EXIT)
        if self.loaded != family:
            progress("load", 0.0)
            self.loaded = family
            progress("load", 1.0, mode="model")
        steps = int(req.get("steps") or 2)
        for step in range(steps):
            if prompt == "slow":
                time.sleep(0.2)
            progress("generate", (step + 1) / steps, step=step + 1, steps=steps)
        if prompt == "slow":
            while True:
                time.sleep(0.2)

    def image(self, req: dict[str, Any], progress: Any) -> dict[str, Any]:
        self._work(req, "image", progress)
        files = []
        for raw in req["out"]:
            path = Path(raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png(int(req["width"]) // 64, int(req["height"]) // 64))
            files.append({"path": str(path), "width": req["width"], "height": req["height"]})
        progress("save", 1.0)
        return {"files": files, "seed": 7, "steps": req.get("steps"), "mode": "model", "vram_peak": 12 * 2**30}

    def video(self, req: dict[str, Any], progress: Any) -> dict[str, Any]:
        self._work(req, "video", progress)
        path = Path(req["out"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(MP4)
        fps = int(req.get("fps") or 24)
        return {"files": [{"path": str(path), "width": req["width"], "height": req["height"],
                           "frames": req["frames"], "fps": fps, "seconds": round(int(req["frames"]) / fps, 3)}],
                "seed": 7, "steps": req.get("steps"), "mode": "model"}

    def translate(self, text: str) -> str:
        self._note({"op": "translate", "text": text})
        return translate_lines(text, lambda pieces: [f"<en:{len(p)}>" for p in pieces])

    def plan(self, family: str) -> dict[str, Any]:
        self._note({"op": "plan", "family": family})
        return {"family": family, "loaded": self.loaded, "ram_free": 20 * 2**30, "vram_free": 15 * 2**30,
                "ram_spare": 3 * 2**30, "needs": {m: {"ram": 12 * 2**30, "vram": 14 * 2**30} for m in MODES},
                "mode": "gpu8"}

    def unload(self) -> None:
        self.loaded = None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--no-gpu", action="store_true")
    args = parser.parse_args()
    answers = io.TextIOWrapper(os.fdopen(os.dup(1), "wb"), encoding="utf-8", line_buffering=True)
    lines = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    if args.no_gpu:
        def refuse() -> Fake:
            raise WorkerError("no NVIDIA graphics card that PyTorch can use was found", "no_gpu")

        return serve(refuse, lines, answers)
    return serve(lambda: Fake(Path(args.log), gpu=True), lines, answers)


if __name__ == "__main__":
    sys.exit(main())
