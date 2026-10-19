"""The real local media worker (diffusers' own Z-Image and Wan pipelines) on tiny random models, on the processor.

The pictures are noise; what is checked is everything between a request and the files: the worker's loading (part
by part, 8-bit or not), the switch to "change a picture" and "start from a first frame" on the same weights, the
memory plan, PNG and H.264 MP4 writing, through the same engine the screens use.

Needs a Python with the media runtime (torch, diffusers, transformers, imageio-ffmpeg), which JARVIS's own does not
have: JARVIS_TEST_MEDIA_PYTHON=<that python>. Skipped otherwise. Nothing is downloaded.
"""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from jarvis.config.settings import MediaSettings
from jarvis.media.local import worker as worker_module
from jarvis.media.local.engine import LocalEngine, LocalMediaError
from jarvis.security.process import ProcessRegistry

MEDIA_PYTHON = os.environ.get("JARVIS_TEST_MEDIA_PYTHON", "")
TINY = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_media.py"

pytestmark = pytest.mark.skipif(not MEDIA_PYTHON, reason="set JARVIS_TEST_MEDIA_PYTHON to a Python with the media "
                                                          "runtime to run the real worker")


@pytest.fixture(scope="module")
def tiny_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("tiny-media")
    subprocess.run([MEDIA_PYTHON, str(TINY), str(root)], check=True, capture_output=True, timeout=600)
    return root


def engine(root: Path, logs: Path, offload: str) -> LocalEngine:
    command = [MEDIA_PYTHON, "-I", "-u", str(Path(worker_module.__file__)), "--root", str(root), "--allow-cpu",
               "--offload", offload]
    return LocalEngine(MediaSettings(offload=offload), root, ProcessRegistry(), logs, command=lambda: command)


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def is_mp4(path: Path) -> bool:
    data = path.read_bytes()
    return len(data) > 100 and data[4:8] == b"ftyp" and b"avc1" in data      # H.264 in an MP4


@pytest.mark.parametrize("offload", ["auto", "gpu8"])
async def test_images_and_videos_from_the_real_pipelines(tiny_root: Path, tmp_path: Path, offload: str) -> None:
    eng = engine(tiny_root, tmp_path / "logs", offload)
    seen: list[dict[str, Any]] = []

    async def progress(msg: dict[str, Any]) -> None:
        seen.append(msg)

    try:
        plan = await eng.request("plan", {"family": "image"})
        assert set(plan["parts"]) == {"transformer", "text_encoder", "vae"} and plan["mode"] == "cpu"
        assert set(plan["needs"]) == set(worker_module.MODES)

        first = tmp_path / "out" / "a.png"
        result = await eng.request("image", {"prompt": "a fox", "width": 96, "height": 64, "out": [str(first)],
                                             "steps": 2, "seed": 1}, progress=progress)
        assert png_size(first) == (96, 64) and result["mode"] == ("cpu8" if offload == "gpu8" else "cpu")
        assert result["ram_peak"] > 0
        assert [m["phase"] for m in seen][:1] == ["load"] and any(m["phase"] == "generate" for m in seen)

        changed = tmp_path / "out" / "b.png"
        await eng.request("image", {"prompt": "a fox in the snow", "width": 96, "height": 64, "out": [str(changed)],
                                    "steps": 2, "seed": 1, "source": str(first), "strength": 0.7})
        assert png_size(changed) == (96, 64)

        video = tmp_path / "out" / "v.mp4"
        made = await eng.request("video", {"prompt": "steam rising", "width": 64, "height": 64, "out": str(video),
                                           "frames": 9, "fps": 8, "steps": 2, "seed": 1})
        assert is_mp4(video) and made["files"][0]["frames"] == 9
        from_frame = tmp_path / "out" / "w.mp4"
        await eng.request("video", {"prompt": "steam rising", "width": 64, "height": 64, "out": str(from_frame),
                                    "frames": 9, "fps": 8, "steps": 2, "seed": 1, "image": str(first)})
        assert is_mp4(from_frame)
        assert not list((tmp_path / "out").glob("*.part*"))                  # nothing half-written is left
    finally:
        await eng.aclose()


async def test_a_bad_request_is_refused_and_the_worker_goes_on(tiny_root: Path, tmp_path: Path) -> None:
    eng = engine(tiny_root, tmp_path / "logs", "auto")
    try:
        with pytest.raises(LocalMediaError) as err:
            await eng.request("video", {"prompt": "x", "width": 64, "height": 64, "out": str(tmp_path / "v.mp4"),
                                        "frames": 10})
        assert err.value.kind == "bad_request" and eng.running
        with pytest.raises(LocalMediaError) as err:
            await eng.request("plan", {"family": "music"})
        assert err.value.kind == "bad_request"
    finally:
        await eng.aclose()

