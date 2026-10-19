"""Images and videos made on this computer: the worker's protocol, the engine that runs it, the turns on the
graphics card, the installer's checks, and the screens' jobs with the local engine (the default).

The models are stood in for by `fixtures/fake_media_worker.py`, which speaks the real protocol (`worker.serve`)
from a real separate process; the real models run in `tests/integration/test_local_media_real.py`. Nothing here
uses the network: a test that would reach xAI fails.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.common.gpu import GpuTurns
from jarvis.config.settings import MediaSettings
from jarvis.media import catalog
from jarvis.media.local import bench
from jarvis.media.local import install as inst
from jarvis.media.local import worker as worker_module
from jarvis.media.local.engine import (
    IMAGE_DIR,
    TRANSLATE_DIR,
    VIDEO_DIR,
    LocalEngine,
    LocalFiles,
    LocalMediaError,
)
from jarvis.media.local.worker import (
    ENCODE_ROOM,
    GIB,
    HEADROOM,
    RAM_FLOOR,
    RAM_SPARE,
    STREAM_RAM,
    MemoryWatch,
    WorkerError,
    choose,
    load_order,
    needs,
    process_memory,
    serve,
    translate_lines,
    weights,
)
from jarvis.security.process import ProcessRegistry
from jarvis.tools.local_media_tools import failure_text

FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_media_worker.py"


def lines_of(log: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []


def installed(root: Path) -> Path:
    """A models folder that looks installed (the fake worker stands in for what is in it)."""
    python = root / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    for folder, marker in ((IMAGE_DIR, "model_index.json"), (VIDEO_DIR, "model_index.json"),
                           (TRANSLATE_DIR, "config.json")):
        (root / folder).mkdir(parents=True)
        (root / folder / marker).write_text("{}", encoding="utf-8")
    return root


def engine_for(tmp_path: Path, *, gpu: GpuTurns | None = None, no_gpu: bool = False,
               idle_minutes: float = 5.0) -> tuple[LocalEngine, Path, ProcessRegistry]:
    log = tmp_path / "requests.log"
    processes = ProcessRegistry()
    args = [sys.executable, "-u", str(FAKE), "--log", str(log), *(["--no-gpu"] if no_gpu else [])]
    engine = LocalEngine(MediaSettings(idle_minutes=idle_minutes), installed(tmp_path / "models"), processes,
                         tmp_path / "logs", gpu=gpu, command=lambda: args)
    return engine, log, processes


# =========================================================================== the protocol
class Stand:
    def info(self) -> dict[str, Any]:
        return {"gpu": True}

    def image(self, req: dict[str, Any], progress: Any) -> dict[str, Any]:
        progress("generate", 2.0, step=1, steps=1)              # clamped to 1
        return {"files": [{"path": "x.png"}]}

    def video(self, req: dict[str, Any], progress: Any) -> dict[str, Any]:
        raise WorkerError("no", "oom")

    def translate(self, text: str) -> str:
        return text.upper()

    def plan(self, family: str) -> dict[str, Any]:
        raise WorkerError("not enough free memory", "low_memory", need_ram=1, free_ram=0)

    def unload(self) -> None:
        raise RuntimeError("boom: CUDA out of memory")


class TestProtocol:
    def test_answers_each_request_by_its_id_then_quits(self) -> None:
        sent = io.StringIO()
        requests = ['{"id": 1, "op": "image"}', "", "not json", '{"id": 2, "op": "video"}',
                    '{"id": 3, "op": "translate", "text": "hi"}', '{"id": 4, "op": "unload"}',
                    '{"id": 5, "op": "paint"}', '{"id": 8, "op": "plan"}', '{"id": 6, "op": "quit"}',
                    '{"id": 7, "op": "info"}']
        assert serve(Stand, requests, sent) == 0
        got = [json.loads(x) for x in sent.getvalue().splitlines()]
        assert got[0]["event"] == "ready" and got[0]["protocol"] == 1 and got[0]["gpu"] is True
        assert got[1] == {"id": 1, "event": "progress", "phase": "generate", "fraction": 1.0, "step": 1, "steps": 1}
        assert got[2]["id"] == 1 and got[2]["event"] == "done" and got[2]["files"] == [{"path": "x.png"}]
        assert got[3]["id"] is None and got[3]["kind"] == "bad_request"
        assert (got[4]["id"], got[4]["kind"]) == (2, "oom")
        assert got[5]["text"] == "HI"
        assert (got[6]["id"], got[6]["kind"]) == (4, "oom")            # an unexpected error, worded as out of memory
        assert (got[7]["id"], got[7]["kind"]) == (5, "bad_request")
        assert got[8] == {"id": 8, "event": "error", "kind": "low_memory", "message": "not enough free memory",
                          "need_ram": 1, "free_ram": 0}                   # the numbers travel with the refusal
        assert got[9] == {"id": 6, "event": "done"} and len(got) == 10  # nothing after quit

    def test_a_backend_that_cannot_start_says_why(self) -> None:
        def no_card() -> Stand:
            raise WorkerError("no NVIDIA graphics card", "no_gpu")

        sent = io.StringIO()
        assert serve(no_card, ['{"id": 1, "op": "image"}'], sent) == 2
        assert json.loads(sent.getvalue()) == {"event": "error", "kind": "no_gpu", "message": "no NVIDIA graphics card"}

    def test_running_out_of_ram_midway_is_low_memory_and_ends_the_worker(self) -> None:
        class Short(Stand):
            def image(self, req: dict[str, Any], progress: Any) -> dict[str, Any]:
                raise RuntimeError("[enforce fail at alloc_cpu.cpp:117] data. DefaultCPUAllocator: not enough memory: "
                                   "you tried to allocate 1073741824 bytes.")

            def video(self, req: dict[str, Any], progress: Any) -> dict[str, Any]:
                raise OSError("[WinError 1455] The paging file is too small for this operation to complete")

            def translate(self, text: str) -> str:
                raise MemoryError()

        sent = io.StringIO()
        serve(Short, ['{"id": 1, "op": "image"}', '{"id": 2, "op": "video"}', '{"id": 3, "op": "translate"}'], sent)
        got = [json.loads(x) for x in sent.getvalue().splitlines()][1:]
        assert [(m["id"], m["kind"], m["stopped"]) for m in got] == [(1, "low_memory", True), (2, "low_memory", True),
                                                                     (3, "low_memory", True)]
        assert "כמעט נגמר" in failure_text("he", LocalMediaError(got[0]["message"], "low_memory", {"stopped": True}))

    def test_hebrew_lines_are_translated_sentence_by_sentence_and_labels_stay(self) -> None:
        asked: list[list[str]] = []

        def fake(pieces: list[str]) -> list[str]:
            asked.append(pieces)
            return [f"EN{i}" for i, _ in enumerate(pieces)]

        text = "Scene: כוס קפה על שולחן. אדים עולים!\nMotion: slow pan\nStyle: אור בוקר"
        assert translate_lines(text, fake) == "Scene: EN0 EN1\nMotion: slow pan\nStyle: EN2"
        assert asked == [["כוס קפה על שולחן.", "אדים עולים!", "אור בוקר"]]
        assert translate_lines("all English", fake) == "all English" and len(asked) == 1


def test_the_worker_imports_nothing_heavy_at_the_top() -> None:
    # JARVIS reads the folder names and drives the protocol with its own Python, which has no PyTorch.
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import worker; "
            "print(sorted(m for m in ('torch', 'diffusers', 'transformers') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(Path(worker_module.__file__).parent)],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# =========================================================================== memory
def safetensors(path: Path, tensors: dict[str, list[int]]) -> None:
    """A .safetensors file with only a header: all the worker reads to work out sizes."""
    header = json.dumps({"__metadata__": {"format": "pt"},
                         **{name: {"dtype": "F32", "shape": shape, "data_offsets": [0, 0]}
                            for name, shape in tensors.items()}}).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(header)) + header)


# Z-Image-Turbo's parts as the PC's files have them (numbers, and how many stay bf16 in 8-bit modes).
Z_IMAGE = {"transformer": (6_150_000_000, 40_000_000), "text_encoder": (4_020_000_000, 390_000_000),
           "vae": (84_000_000, 1_000_000)}
WAN = {"transformer": (5_000_000_000, 30_000_000), "text_encoder": (5_680_000_000, 2_050_000_000),
       "vae": (705_000_000, 1_000_000)}


class TestMemory:
    def test_sizes_come_from_the_file_headers(self, tmp_path: Path) -> None:
        folder = tmp_path / "Z-Image-Turbo"
        safetensors(folder / "transformer" / "a.safetensors", {"layers.0.attention.to_q.weight": [8, 8],
                                                               "layers.0.attention.norm_q.weight": [8],
                                                               "t_embedder.mlp.0.weight": [4, 8]})
        safetensors(folder / "transformer" / "b.safetensors", {"layers.1.feed_forward.w1.weight": [16, 8],
                                                               "layers.1.feed_forward.w1.bias": [16]})
        safetensors(folder / "text_encoder" / "model.safetensors", {"embed_tokens.weight": [10, 4],
                                                                    "layers.0.mlp.up_proj.weight": [6, 4],
                                                                    "layers.0.input_layernorm.weight": [4]})
        safetensors(folder / "vae" / "x.safetensors", {"decoder.conv_in.weight": [2, 2, 3, 3]})
        (folder / "tokenizer").mkdir()
        assert weights(folder) == {"transformer": (64 + 8 + 32 + 128 + 16, 8 + 32 + 16),
                                   "text_encoder": (40 + 24 + 4, 40 + 4), "vae": (36, 0)}

    def test_what_each_mode_needs(self) -> None:
        eight = {"transformer": 6_110_000_000 + 80_000_000, "text_encoder": 3_630_000_000 + 780_000_000}
        vae = 84_000_000 * 2
        # On the card: the weights stream in a tensor at a time (little RAM). The card holds each part whole, in
        # bf16, beside the ones already made 8-bit, until it shrinks.
        ram, vram = needs("image", Z_IMAGE, "gpu8")
        assert ram == STREAM_RAM
        assert vram == max(eight["transformer"] + 4_020_000_000 * 2,
                           eight["transformer"] + eight["text_encoder"] + vae + HEADROOM["image"])
        # seq8: the text encoder alone on the card to read the description; then the rest, in 8 bits.
        ram, vram = needs("video", WAN, "seq8")
        assert ram == STREAM_RAM
        assert vram == max(5_680_000_000 * 2 + ENCODE_ROOM, 5_000_000_000 * 2,
                           (4_970_000_000 + 60_000_000) + 705_000_000 * 4 + HEADROOM["video"])
        ram, vram = needs("image", Z_IMAGE, "model8")
        # The transformer is made 8-bit before the text encoder loads whole beside it.
        assert ram == eight["transformer"] + 4_020_000_000 * 2
        assert vram == eight["transformer"] + HEADROOM["image"]
        ram, _ = needs("image", Z_IMAGE, "model")
        assert ram == (6_150_000_000 + 4_020_000_000 + 84_000_000) * 2
        assert needs("video", WAN, "group8")[1] == GIB + HEADROOM["video"]
        assert needs("video", WAN, "gpu")[1] > needs("video", WAN, "gpu8")[1] > needs("video", WAN, "seq8")[1]

    def test_the_fastest_mode_that_fits_is_chosen(self) -> None:
        # The PC with nothing else big open (16 GB card, 32 GB of RAM): all of it on the card, in 8 bits.
        assert choose("image", Z_IMAGE, ram_free=20 * GIB, vram_free=15 * GIB) == "gpu8"
        assert choose("image", Z_IMAGE, ram_free=60 * GIB, vram_free=46 * GIB) == "gpu"
        assert choose("image", Z_IMAGE, ram_free=60 * GIB, vram_free=11 * GIB) == "model8"
        assert choose("image", Z_IMAGE, ram_free=60 * GIB, vram_free=6 * GIB) == "group8"
        assert choose("image", Z_IMAGE, ram_free=60 * GIB, vram_free=46 * GIB, offload="model") == "model"
        assert choose("image", Z_IMAGE, ram_free=20 * GIB, vram_free=15 * GIB, floor=2) == "seq8"
        assert choose("image", Z_IMAGE, ram_free=20 * GIB, vram_free=15 * GIB, floor=3) == "model8"
        # What fits on the card is not enough: loading must leave RAM_SPARE free.
        tight = needs("image", Z_IMAGE, "gpu8")[0] + RAM_SPARE
        assert choose("image", Z_IMAGE, ram_free=tight, vram_free=15 * GIB) == "gpu8"
        with pytest.raises(WorkerError):
            choose("image", Z_IMAGE, ram_free=tight - 1, vram_free=15 * GIB)

    def test_beside_a_big_program_it_refuses_with_the_numbers(self) -> None:
        # As measured on the PC with AvodaAssistant open: 9.6 GB of RAM and about 9.7 GB of the card free.
        with pytest.raises(WorkerError) as err:
            choose("image", Z_IMAGE, ram_free=int(9.6 * GIB), vram_free=int(9.7 * GIB))
        assert err.value.kind == "low_memory"
        d = err.value.details
        assert d["free_ram"] == int(9.6 * GIB) and d["need_ram"] == needs("image", Z_IMAGE, "group8")[0] + RAM_SPARE
        assert d["need_ram"] > d["free_ram"] and "GB of RAM" in str(err.value)

    def test_the_part_that_shrinks_most_loads_first(self) -> None:
        # Wan's text encoder has more numbers, but its transformer shrinks more in 8 bits, so the transformer goes
        # first: 15.3 GB at the peak instead of 16.5, which is what lets a video load with AvodaAssistant closed.
        assert load_order("video", WAN, True) == ["transformer", "text_encoder", "vae"]
        assert load_order("video", WAN, False) == ["text_encoder", "transformer", "vae"]       # nothing shrinks
        ram, vram = needs("video", WAN, "model8")
        assert ram == (5_000_000_000 - 30_000_000 + 60_000_000) + 5_680_000_000 * 2
        assert vram == (5_680_000_000 - 2_050_000_000 + 4_100_000_000) + HEADROOM["video"]
        # The PC with AvodaAssistant closed, as measured: 19.2 GB of RAM and 14.8 GB of the card free. Video reads
        # the description first and then makes the frames, straight on the card; it would also load with only
        # the 2.5 GB of streaming RAM plus the spare free (the PC once had 14 GB free and 1.2 GB left to commit).
        assert choose("video", WAN, ram_free=int(19.2 * GIB), vram_free=int(14.8 * GIB)) == "seq8"
        assert choose("video", WAN, ram_free=STREAM_RAM + RAM_SPARE, vram_free=int(14.8 * GIB)) == "seq8"
        assert choose("image", Z_IMAGE, ram_free=int(19.2 * GIB), vram_free=int(14.8 * GIB)) == "gpu8"

    def test_the_watch_stops_the_worker_when_ram_stays_short(self) -> None:
        readings = iter([{"ram_free": 6 * GIB}, {"ram_free": 4 * GIB},
                         {"ram_free": RAM_FLOOR - 1},           # short once ...
                         {"ram_free": RAM_FLOOR + GIB},         # ... then not
                         {"ram_free": RAM_FLOOR - 1},
                         {"ram_free": RAM_FLOOR - 2}])          # twice running: stop
        watch = MemoryWatch(measure=lambda: next(readings))
        assert [watch.check() for _ in range(5)] == [False] * 5
        assert watch.lowest == RAM_FLOOR - 1
        assert watch.check() is True and watch.lowest == RAM_FLOOR - 2
        watch.reset()
        assert watch.lowest is None

    def test_this_process_memory_is_measured(self) -> None:
        own = process_memory()
        assert own["peak_set"] >= own["set"] > 0 and 0 < own["private_set"] <= own["set"]
        assert worker_module.ram_peak() >= own["set"]
        now = worker_module.memory()
        assert 0 < now["ram_free"] <= now["ram_total"]

    def test_the_refusal_in_the_users_words(self) -> None:
        exc = LocalMediaError("x", "low_memory", {"need_ram": 18 * GIB, "free_ram": int(9.6 * GIB),
                                                  "need_vram": 5 * GIB, "free_vram": 10 * GIB})
        he = failure_text("he", exc)
        assert "18GB" in he and "9.6GB" in he and "לא התחלתי" in he
        assert "18 GB of RAM" in failure_text("en", exc)
        assert "כמעט נגמר" in failure_text("he", LocalMediaError("x", "low_memory", {"stopped": True}))


# =========================================================================== the catalog
class TestLocalCatalog:
    def test_sizes_the_models_make(self) -> None:
        for aspect in catalog.image_model(catalog.LOCAL_IMAGE_MODEL).aspects:
            w, h = catalog.local_image_size(aspect)
            assert w % 16 == 0 and h % 16 == 0 and 0.9e6 < w * h < 1.2e6
        assert catalog.local_image_size("1:1") == (1024, 1024)
        assert catalog.local_video_size("16:9", "480p") == (832, 480)
        assert catalog.local_video_size("16:9", "720p") == (1280, 704)
        for aspect in catalog.VIDEO_ASPECTS:
            w, h = catalog.local_video_size(aspect, "720p")
            assert w % 32 == 0 and h % 32 == 0
        assert [catalog.local_frames(s) for s in (1, 3, 5)] == [25, 73, 121]
        assert all((f - 1) % 4 == 0 for f in (catalog.local_frames(s) for s in range(1, 6)))

    def test_free_and_roughly_how_long(self) -> None:
        assert catalog.image_cost(catalog.LOCAL_IMAGE_MODEL, "1k", 4) == 0
        assert catalog.video_cost(catalog.LOCAL_VIDEO_MODEL, "720p", 5) == 0
        assert catalog.local_estimate_s("image", count=2, loaded=True) == 2 * catalog.LOCAL_IMAGE_S
        assert catalog.local_estimate_s("video", seconds=3, resolution="480p") == pytest.approx(
            catalog.LOCAL_LOAD_S + 3 * catalog.LOCAL_VIDEO_S_PER_SECOND["480p"])
        assert catalog.local_change_prompt("a fox", "make it blue") == "a fox\nmake it blue"


# =========================================================================== the engine
class TestEngine:
    async def test_starts_on_the_first_request_and_reports_progress(self, tmp_path: Path) -> None:
        engine, log, processes = engine_for(tmp_path)
        seen: list[dict[str, Any]] = []

        async def progress(msg: dict[str, Any]) -> None:
            seen.append(msg)

        assert not engine.running
        try:
            out = tmp_path / "media" / "a.png"
            result = await engine.request("image", {"prompt": "fox", "width": 1024, "height": 1024,
                                                    "out": [str(out)], "steps": 3}, progress=progress)
            assert engine.running and engine.info["device"] == "Fake GPU" and not engine.busy()
            assert [p["fraction"] for p in seen if p["phase"] == "generate"] == pytest.approx([1 / 3, 2 / 3, 1.0], abs=1e-3)
            assert result["files"][0]["path"] == str(out) and out.read_bytes().startswith(b"\x89PNG")
            assert [p["owner"] for p in processes.list()] == ["media-model"]
            assert (await engine.request("translate", {"text": "שועל"}))["text"] == "<en:4>"
            assert [r.get("op") for r in lines_of(log)] == ["image", "translate"]
        finally:
            await engine.aclose()
        assert not engine.running and processes.list() == []

    async def test_stopping_midway_ends_the_worker(self, tmp_path: Path) -> None:
        engine, _log, processes = engine_for(tmp_path)
        started = asyncio.Event()

        async def progress(msg: dict[str, Any]) -> None:
            started.set()

        task = asyncio.create_task(engine.request("video", {"prompt": "slow", "width": 832, "height": 480,
                                                            "frames": 73, "out": str(tmp_path / "v.mp4"), "steps": 2},
                                                  progress=progress))
        await asyncio.wait_for(started.wait(), 30)
        assert engine.busy()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not engine.running and not engine.busy() and processes.list() == []
        # The next request starts it again.
        try:
            await engine.request("image", {"prompt": "fox", "width": 64, "height": 64, "out": [str(tmp_path / "b.png")]})
            assert engine.running
        finally:
            await engine.aclose()

    async def test_a_crash_says_so_and_the_next_request_starts_afresh(self, tmp_path: Path) -> None:
        engine, _log, _p = engine_for(tmp_path)
        try:
            with pytest.raises(LocalMediaError) as err:
                await engine.request("image", {"prompt": "crash", "width": 64, "height": 64, "out": ["x"]})
            assert err.value.kind == "crashed" and "exit code 3" in str(err.value) and not engine.running
            with pytest.raises(LocalMediaError) as err:
                await engine.request("image", {"prompt": "oom", "width": 64, "height": 64, "out": ["x"]})
            assert err.value.kind == "oom" and engine.running            # an answered error keeps the worker
        finally:
            await engine.aclose()

    async def test_short_of_memory(self, tmp_path: Path) -> None:
        engine, _log, processes = engine_for(tmp_path)
        try:
            with pytest.raises(LocalMediaError) as err:          # refused before loading: the worker stays
                await engine.request("image", {"prompt": "lowmem", "width": 64, "height": 64, "out": ["x"]})
            assert err.value.kind == "low_memory" and err.value.details["need_ram"] == 18 * GIB and engine.running
            with pytest.raises(LocalMediaError) as err:          # the memory watch ended it midway
                await engine.request("image", {"prompt": "ramout", "width": 64, "height": 64, "out": ["x"]})
            assert err.value.kind == "low_memory" and err.value.details == {"stopped": True}
            assert not engine.running and processes.list() == []
        finally:
            await engine.aclose()

    async def test_the_bench_measures_each_mode_in_a_fresh_worker(self, tmp_path: Path) -> None:
        made: list[LocalEngine] = []

        def make(mode: str) -> LocalEngine:
            engine, _log, _p = engine_for(tmp_path / mode)
            made.append(engine)
            return engine

        said: list[str] = []
        rows = await bench.run(make, modes=["auto", "gpu8"], kinds=["image", "video"], out=tmp_path / "out",
                               image_steps=2, video_steps=2, say=said.append)
        assert [(r["mode"], r["kind"], r["number"]) for r in rows] == [
            ("auto", "image", 1), ("auto", "image", 2), ("auto", "video", 1),
            ("gpu8", "image", 1), ("gpu8", "image", 2), ("gpu8", "video", 1)]
        assert all(Path(r["path"]).is_file() for r in rows) and rows[2]["size"] == "832x480x73"
        assert not any(e.running for e in made) and "== gpu8" in said
        assert any("gpu8    needs 12.0 GB RAM, 14.0 GB on the card" in line for line in said)
        assert any(line.strip() == "→ gpu8" for line in said)

    async def test_no_graphics_card(self, tmp_path: Path) -> None:
        engine, _log, processes = engine_for(tmp_path, no_gpu=True)
        with pytest.raises(LocalMediaError) as err:
            await engine.request("image", {"prompt": "fox", "width": 64, "height": 64, "out": ["x"]})
        assert err.value.kind == "no_gpu" and not engine.running and processes.list() == []

    async def test_an_idle_worker_is_stopped(self, tmp_path: Path) -> None:
        engine, _log, _p = engine_for(tmp_path, idle_minutes=0.5)
        try:
            await engine.request("info")
            assert not await engine.stop_if_idle()
            engine._last_used = time.monotonic() - 31
            assert await engine.stop_if_idle() and not engine.running
        finally:
            await engine.aclose()

    async def test_turns_on_the_graphics_card(self, tmp_path: Path) -> None:
        gpu = GpuTurns()
        computer = {"busy": True, "released": 0}

        async def release() -> None:
            computer["released"] += 1

        gpu.register("computer", busy=lambda: computer["busy"], release=release)
        engine, _log, _p = engine_for(tmp_path, gpu=gpu)
        gpu.register("media", busy=engine.busy, release=engine.stop)
        try:
            with pytest.raises(LocalMediaError) as err:          # computer control is working: nothing starts
                await engine.request("image", {"prompt": "fox", "width": 64, "height": 64, "out": ["x"]})
            assert err.value.kind == "gpu_busy" and not engine.running and computer["released"] == 0
            computer["busy"] = False                              # idle: its model lets go first
            await engine.request("image", {"prompt": "fox", "width": 64, "height": 64,
                                           "out": [str(tmp_path / "f.png")]})
            assert engine.running and computer["released"] == 1
            assert await gpu.take("computer") is None and not engine.running     # and the other way round
        finally:
            await engine.aclose()

    def test_what_is_missing(self, tmp_path: Path) -> None:
        files = LocalFiles(tmp_path / "nothing")
        assert files.missing("image") == ["runtime", "image"] and files.missing("video") == ["runtime", "video"]
        assert LocalFiles(installed(tmp_path / "m")).missing("video") == []


# =========================================================================== the installer
def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestInstaller:
    def test_an_environment_missing_a_package_is_completed_not_called_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # As on the PC: an environment made before ftfy was pinned imports torch fine but lacks it.
        here = Path(sys.executable).parents[1]                     # JARVIS's own environment: no torch, no ftfy
        version = __import__("importlib.metadata").metadata.version("httpx")
        wrong, versions = inst.runtime_problems(here, (f"httpx=={version}", "httpx==0.0.1", "ftfy==6.3.1"))
        assert wrong == ["httpx==0.0.1", "ftfy==6.3.1"] and versions == ""
        assert inst.runtime_problems(tmp := here / "no-such-venv")[0] == [inst.TORCH, *inst.PACKAGES]
        assert not tmp.exists()

        ran: list[list[str]] = []
        answers = iter([(["ftfy==6.3.1"], ""), ([], "2.14.1 0.41.0 5.20.1")])
        monkeypatch.setattr(inst, "runtime_problems", lambda venv: next(answers))
        monkeypatch.setattr(inst, "_run", lambda args, progress: ran.append([str(a) for a in args]))
        monkeypatch.setattr(inst, "venv_python", lambda venv: Path(sys.executable))
        assert inst.install_runtime(Path("/media-root"), lambda line: None) == ["ftfy==6.3.1"]
        assert [r[3:] for r in ran] == [["install", "-q", "--upgrade", "pip"], ["install", "-q", "ftfy==6.3.1"]]

    def test_only_what_the_pipelines_read(self) -> None:
        image, _video, words = inst.REPOS
        assert inst.wanted(image, "transformer/diffusion_pytorch_model-00001-of-00003.safetensors")
        assert inst.wanted(image, "model_index.json") and inst.wanted(image, "README.md")
        assert not inst.wanted(image, "assets/showcase.jpg") and not inst.wanted(image, "assets/paper.pdf")
        assert not inst.wanted(image, ".gitattributes")
        assert not inst.wanted(_video, "examples/i2v_input.JPG")
        assert inst.wanted(words, "model.safetensors") and inst.wanted(words, "source.spm")
        assert not inst.wanted(words, "pytorch_model.bin") and not inst.wanted(words, "tf_model.h5")
        assert {r.folder for r in inst.REPOS} == {IMAGE_DIR, VIDEO_DIR, TRANSLATE_DIR}
        assert all(len(r.revision) == 40 for r in inst.REPOS)            # pinned to a commit, never "main"

    def test_files_are_checked_and_what_is_already_here_is_kept(self, tmp_path: Path) -> None:
        weights, config = b"W" * 1000, b'{"a": 1}'
        repo = inst.Repo("Thing", "org/thing", "a" * 40, "apache-2.0")
        tree = [{"type": "file", "path": "transformer/w.safetensors", "size": len(weights),
                 "lfs": {"oid": f"sha256:{sha(weights)}"}},
                {"type": "file", "path": "model_index.json", "size": len(config)},
                {"type": "file", "path": "assets/pic.png", "size": 3}]
        asked: list[str] = []

        def answer(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.path)
            if "/api/models/" in request.url.path:
                assert request.url.path.endswith(f"/tree/{'a' * 40}")
                return httpx.Response(200, json=tree)
            if request.url.path.endswith("w.safetensors"):
                return httpx.Response(200, content=weights)
            return httpx.Response(200, content=config)

        client = httpx.Client(transport=httpx.MockTransport(answer))
        got, kept = inst.install_model(client, tmp_path, repo, progress=lambda _m: None)
        assert sorted(got) == ["model_index.json", "transformer/w.safetensors"] and kept == []
        assert (tmp_path / "Thing" / "transformer" / "w.safetensors").read_bytes() == weights
        assert not (tmp_path / "Thing" / "assets").exists()
        asked.clear()
        got, kept = inst.install_model(client, tmp_path, repo, progress=lambda _m: None)      # again: all checked
        assert got == [] and sorted(kept) == ["model_index.json", "transformer/w.safetensors"]
        assert len(asked) == 1                                                                 # only the list
        (tmp_path / "Thing" / "transformer" / "w.safetensors").write_bytes(b"X" * 1000)     # damaged: fetched again
        got, _kept = inst.install_model(client, tmp_path, repo, progress=lambda _m: None)
        assert got == ["transformer/w.safetensors"]
        tree[0]["lfs"] = {"oid": "sha256:" + "0" * 64}                                       # a wrong file is refused
        (tmp_path / "Thing" / "transformer" / "w.safetensors").unlink()
        with pytest.raises(OSError, match="checksum mismatch"):
            inst.install_model(client, tmp_path, repo, progress=lambda _m: None)


# =========================================================================== the screens' jobs, local
@pytest.fixture
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The whole app with the local engine (the default) on a models folder the fake worker stands in for."""
    from fakes import FakeHost
    from jarvis.ai.router import ProviderRouter
    from jarvis.app import JarvisApp
    from jarvis.config.settings import Settings

    home = tmp_path / "home"
    (home / "Pictures").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home() == home
    settings = Settings()
    assert settings.media.engine == "local"
    settings.media.models_dir = str(installed(tmp_path / "models"))
    jarvis = JarvisApp(settings, host=FakeHost(), router=ProviderRouter(settings.ai, []), voice=False, use_keyring=False,
                       serve=False)
    log = tmp_path / "requests.log"
    args = [sys.executable, "-u", str(FAKE), "--log", str(log)]
    jarvis.media.engine._command = lambda: args

    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"nothing may leave the computer: {request.url}")

    jarvis.services._http = httpx.AsyncClient(transport=httpx.MockTransport(no_network))
    await jarvis.start()
    jarvis.request_log = log
    try:
        yield jarvis
    finally:
        await jarvis.shutdown()


async def finished(app: Any, job: Any, timeout: float = 30.0) -> Any:
    done = await app.jobs.wait(job.id, timeout)
    assert done is not None and done.status != "running"
    return done


class TestScreensLocal:
    async def test_the_status_offers_the_free_models_only(self, app: Any) -> None:
        st = app.media.status("he")
        assert st["engine"] == "local" and st["available"] and st["kinds"]["video"]["available"]
        assert st["missing"] == [] and st["host"] == "" and st["local"]["translate"]
        assert [m["id"] for m in st["offer"]["image"]["models"]] == [catalog.LOCAL_IMAGE_MODEL]
        assert st["offer"]["video"]["models"][0]["seconds"] == {"min": 1, "max": 5, "default": 3}

    async def test_an_image_is_made_here_with_nothing_sent(self, app: Any) -> None:
        from jarvis.media.service import ImageRequest

        job = await app.media.create_image(ImageRequest(description="a red fox", aspect_ratio="16:9", lang="en"))
        assert job.sends == [] and "nothing leaves the computer" in job.plan[0] and job.cost.startswith("Free")
        done = await finished(app, job)
        assert done.status == "done", done.error
        (item,) = done.result["items"]
        assert item["real"] and item["format"] == "PNG" and item["params"]["engine"] == "local"
        assert item["usd"] is None and item["prompt"] == "a red fox"
        (req,) = lines_of(app.request_log)
        assert (req["op"], req["width"], req["height"], req["steps"]) == ("image", 1360, 768, 9)
        (entry,) = [e for e in app.audit.recent(task_id=job.id) if e.tool == "create_image_local"]
        assert (entry.agent_id, entry.decision, entry.outcome) == ("creative", "allowed", "ok")

    async def test_a_hebrew_description_is_translated_here_first(self, app: Any) -> None:
        from jarvis.media.service import ImageRequest

        job = await app.media.create_image(ImageRequest(description="שועל אדום", lang="he"))
        assert any("לתרגם" in line for line in job.plan)
        done = await finished(app, job)
        (item,) = done.result["items"]
        assert item["prompt"] == "<en:9>" and item["params"]["asked"] == "שועל אדום" and item["description"] == "שועל אדום"
        assert [r["op"] for r in lines_of(app.request_log)] == ["translate", "image"]
        assert lines_of(app.request_log)[1]["prompt"] == "<en:9>"

    async def test_a_change_redraws_the_preview(self, app: Any) -> None:
        from jarvis.media.service import ImageRequest, RefineImageRequest

        first = await finished(app, await app.media.create_image(ImageRequest(description="a fox", lang="en")))
        (item,) = first.result["items"]
        done = await finished(app, await app.media.refine_image(RefineImageRequest(item_id=item["id"],
                                                                                   change="in the snow", lang="en")))
        assert done.status == "done", done.error
        req = lines_of(app.request_log)[-1]
        assert req["source"] == item["path"] and req["strength"] == catalog.LOCAL_IMAGE_STRENGTH["change"]
        assert req["prompt"] == "a fox\nin the snow"
        assert done.result["items"][0]["parent"] == item["id"]

    async def test_a_video_is_made_here(self, app: Any) -> None:
        from jarvis.media.service import VideoRequest

        job = await app.media.create_video(VideoRequest(scene="a cup of coffee", motion="steam rising", lang="en"))
        assert job.sends == [] and job.params["seconds"] is None
        done = await finished(app, job)
        assert done.status == "done", done.error
        (item,) = done.result["items"]
        assert item["format"] == "MP4" and item["params"]["seconds"] == 3 and item["seconds"] == pytest.approx(73 / 24, 0.01)
        req = lines_of(app.request_log)[-1]
        assert (req["width"], req["height"], req["frames"], req["fps"], req["steps"]) == (832, 480, 73, 24, 30)
        assert req["prompt"] == "Scene: a cup of coffee\nMotion: steam rising"

    async def test_what_the_local_models_cannot_do_is_refused_first(self, app: Any) -> None:
        from jarvis.media.service import ImageRequest, MediaRequestError, VideoRequest

        for bad in (lambda: app.media.create_image(ImageRequest(description="x", model="grok-imagine-image")),
                    lambda: app.media.create_video(VideoRequest(scene="x", seconds=6)),
                    lambda: app.media.create_video(VideoRequest(scene="x", resolution="1080p")),
                    lambda: app.media.create_image(ImageRequest(description="x", resolution="2k")),
                    lambda: app.media.create_image(ImageRequest(description="x", aspect_ratio="auto"))):
            with pytest.raises(MediaRequestError) as err:
                await bad()
            assert err.value.status == 400
        with pytest.raises(MediaRequestError) as err:
            await app.media.create_image(ImageRequest(description="x", model="grok-imagine-image", lang="en"))
        assert err.value.code == "engine" and "Grok is off" in str(err.value)
        assert lines_of(app.request_log) == []

    async def test_stopping_a_video_stops_the_worker(self, app: Any) -> None:
        from jarvis.media.service import VideoRequest

        job = await app.media.create_video(VideoRequest(scene="slow", lang="en"))
        deadline = time.monotonic() + 20
        while not app.media.engine.busy():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        app.jobs.cancel(job.id, "stopped by the user")
        done = await finished(app, job)
        assert done.status in ("cancelled", "stopped") and not app.media.engine.running
        assert app.media.items("video") == []

    async def test_not_installed_offers_a_demonstration(self, app: Any, tmp_path: Path) -> None:
        from jarvis.media.service import ImageRequest, MediaRequestError

        app.media.engine.files = LocalFiles(tmp_path / "empty")
        st = app.media.status("en")
        assert not st["available"] and st["demo"] and any("jarvis media install" in m for m in st["missing"])
        with pytest.raises(MediaRequestError) as err:
            await app.media.create_image(ImageRequest(description="x", lang="en"))
        assert err.value.status == 409 and err.value.code == "unavailable"
        done = await finished(app, await app.media.create_image(ImageRequest(description="x", demo=True, lang="en")))
        assert done.status == "done" and not done.result["items"][0]["real"]
        assert lines_of(app.request_log) == []
