"""JARVIS's local image and video worker: free open models on this computer's graphics card.

JARVIS starts this file with the media runtime's own Python (`<media>/venv`, made by `jarvis media install`),
never with its own, so PyTorch and diffusers stay out of JARVIS. It reads one JSON request per line on stdin and
answers with JSON lines on stdout: "progress" while it works, then "done" or "error" with the request's id. One
request at a time. A model loads when it is first needed and stays loaded until JARVIS asks to unload it or ends
this process, which is also how a job is stopped midway (ending the process frees the graphics card at once).

Nothing here uses the network: the weights are read from the folders under `--root` only (HF_HUB_OFFLINE), and
what is made is written only to the paths JARVIS names, inside its own media folder.

  image      Z-Image-Turbo (Tongyi-MAI, Apache 2.0) through diffusers' ZImagePipeline; a change to an image, or a
             variation of it, through ZImageImg2ImgPipeline on the same weights (no reload).
  video      Wan 2.2 TI2V-5B (Wan-AI, Apache 2.0) through diffusers' WanPipeline, or WanImageToVideoPipeline when a
             first frame is given; written as H.264 MP4 (yuv420p), which every player and WebView2 open.
  translate  Hebrew to English with opus-mt-tc-big-he-en (Helsinki-NLP, CC-BY 4.0) on the processor, since both
             models follow English best.

Memory: before a model loads, the worker works out from the files' headers how much RAM and graphics memory each
mode would take (`plan` shows it) and picks the fastest that fits what is free; when none fits it refuses
("low_memory") instead of pushing the computer into swapping, and a watch ends the process if free RAM still falls
below RAM_FLOOR while it works. The weights are read a tensor at a time, never through a memory map: on Windows a
mapped file is charged in full against RAM plus the paging file, and the fp32 checkpoints these models ship (25 GB
for Z-Image's transformer) would hold all of it while they load.

Only the standard library is imported at the top, so JARVIS's tests can drive the protocol (`serve`) with a
stand-in backend. The folder names below are the ones `jarvis.media.local` installs to (a test keeps them equal).
"""

from __future__ import annotations

import argparse
import functools
import gc
import io
import json
import os
import re
import struct
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import IO, Any, Protocol

PROTOCOL = 1
IMAGE_DIR = "Z-Image-Turbo"
VIDEO_DIR = "Wan2.2-TI2V-5B"
TRANSLATE_DIR = "opus-mt-tc-big-he-en"

# The modes a model can run in, from the fastest down. An "8" keeps the big parts' weights in 8 bits (fp8, turned
# back into bf16 one layer at a time as it runs): half the memory, for a little detail.
#   gpu, gpu8      all of it on the graphics card
#   model, model8  each part moved onto the card only while it works; the rest waits in RAM ("model" offload)
#   group8         layers streamed onto the card as they run: little graphics memory, but all of it held in RAM
# What decides is the memory free when the model loads, RAM as much as the card's: a computer that runs out of
# RAM stalls for everyone, so a mode that would not leave RAM_SPARE free is never chosen.
MODES = ("gpu", "gpu8", "model", "model8", "group8")
GIB = 1 << 30
HEADROOM = {"image": int(2.5 * GIB), "video": 4 * GIB}     # graphics memory for the work itself, beyond the weights
RAM_SPARE = 3 * GIB               # RAM left free for Windows and the programs already open
RAM_FLOOR = 2 * GIB               # below this much free RAM the worker ends itself rather than let the computer stall
LOW_MEMORY_EXIT = 75              # its exit code then, which JARVIS words as "low_memory"
# How running out of RAM reads: PyTorch's allocator, and Windows when the commit limit (RAM plus the paging file) is
# reached, which can come before the RAM itself runs out.
RAM_SHORT = ("defaultcpuallocator: not enough memory", "paging file is too small")
EIGHT_BIT_PARTS = ("transformer", "text_encoder")
# In the text encoders, the normalisations and the word tables stay in bf16 (diffusers' own models say this
# themselves), and so does T5's last feed-forward layer ("wo"), whose dtype T5 reads to cast what goes into it.
TEXT_KEEP = ("norm", "embed_tokens", "shared", "relative_attention_bias", r"\.wo$")
# What stays in bf16 per part in 8-bit modes, for working out the memory beforehand: the above for the text
# encoders, and for the transformers diffusers' defaults plus the model's own list (Z-Image's and Wan's).
KEEP = {"text_encoder": TEXT_KEEP,
        "transformer": ("pos_embed", "patch_embed", "norm", "^proj_in$", "^proj_out$", "t_embedder", "cap_embedder",
                        "patch_embedding", "condition_embedder")}

# Wan's own list of what to avoid (the English one diffusers' examples use).
VIDEO_NEGATIVE = ("Bright tones, overexposed, static, blurred details, subtitles, style, works, paintings, images, "
                  "static, overall gray, worst quality, low quality, JPEG compression residue, ugly, incomplete, "
                  "extra fingers, poorly drawn hands, poorly drawn faces, deformed, disfigured, misshapen limbs, "
                  "fused fingers, still picture, messy background, three legs, many people in the background, "
                  "walking backwards")

HEBREW = re.compile(r"[֐-׿]")
LABEL = re.compile(r"^([A-Za-z][A-Za-z ]{0,30}:\s*)(.*)$")       # "Scene: …", "Style: …" stay as they are
SENTENCE = re.compile(r"(?<=[.!?;])\s+")

Progress = Callable[..., None]


class WorkerError(Exception):
    """A request that cannot be done, with a kind JARVIS words for the user (missing, oom, low_memory, bad_request,
    failed) and any numbers it needs for that (`details`)."""

    def __init__(self, message: str, kind: str = "failed", **details: Any) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details


class Backend(Protocol):
    def info(self) -> dict[str, Any]: ...
    def image(self, req: dict[str, Any], progress: Progress) -> dict[str, Any]: ...
    def video(self, req: dict[str, Any], progress: Progress) -> dict[str, Any]: ...
    def translate(self, text: str) -> str: ...
    def plan(self, family: str) -> dict[str, Any]: ...
    def unload(self) -> None: ...


# ============================================================================ the protocol
def serve(make_backend: Callable[[], Backend], lines: Iterable[str], out: IO[str]) -> int:
    """Answer requests until "quit" or the end of the input. Returns the exit code."""
    def send(obj: dict[str, Any]) -> None:
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        out.flush()

    try:
        backend = make_backend()
    except WorkerError as exc:
        send({"event": "error", "kind": exc.kind, "message": str(exc), **exc.details})
        return 2
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        send({"event": "error", "kind": "start", "message": f"{type(exc).__name__}: {exc}"[:2000]})
        return 2
    send({"event": "ready", "protocol": PROTOCOL, "pid": os.getpid(), **backend.info()})
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("a request is a JSON object")
        except ValueError as exc:
            send({"id": None, "event": "error", "kind": "bad_request", "message": str(exc)})
            continue
        rid, op = req.get("id"), req.get("op")
        if op == "quit":
            send({"id": rid, "event": "done"})
            return 0
        started = time.perf_counter()

        def progress(phase: str, fraction: float, *, rid: Any = rid, **extra: Any) -> None:
            send({"id": rid, "event": "progress", "phase": phase, "fraction": round(min(max(fraction, 0.0), 1.0), 4),
                  **extra})

        try:
            if op == "image":
                result = backend.image(req, progress)
            elif op == "video":
                result = backend.video(req, progress)
            elif op == "translate":
                result = {"text": backend.translate(str(req.get("text") or ""))}
            elif op == "plan":
                result = backend.plan(str(req.get("family") or "image"))
            elif op == "unload":
                backend.unload()
                result = {}
            elif op == "info":
                result = backend.info()
            else:
                raise WorkerError(f"unknown request {op!r}", "bad_request")
            send({"id": rid, "event": "done", "seconds": round(time.perf_counter() - started, 3), **result})
        except WorkerError as exc:
            send({"id": rid, "event": "error", "kind": exc.kind, "message": str(exc), **exc.details})
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            text = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, MemoryError) or any(m in text.lower() for m in RAM_SHORT):
                # Out of RAM (or of what Windows lets programs reserve): JARVIS ends this process, freeing it all.
                send({"id": rid, "event": "error", "kind": "low_memory", "message": text[:2000], "stopped": True})
                continue
            kind = "oom" if "out of memory" in text.lower() else "failed"
            send({"id": rid, "event": "error", "kind": kind, "message": text[:2000]})
    return 0


# ============================================================================ memory
def memory() -> dict[str, int]:
    """This computer's RAM: free ("available", as Task Manager shows it) and total bytes, and on Windows how much
    more can be committed at all (RAM and paging file together)."""
    if sys.platform == "win32":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        *((name, ctypes.c_ulonglong) for name in ("total", "free", "commit_total", "commit_free",
                                                                   "virtual_total", "virtual_free", "extended"))]

        status = Status()
        status.length = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return {"ram_free": int(status.free), "ram_total": int(status.total), "commit_free": int(status.commit_free)}
    values: dict[str, int] = {}
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            name, _, rest = line.partition(":")
            values[name] = int(rest.split()[0]) * 1024
    return {"ram_free": values["MemAvailable"], "ram_total": values["MemTotal"]}


@functools.cache
def _windows_counters() -> tuple[Any, Any, Any]:
    """GetProcessMemoryInfo with its real types. (Untyped, ctypes passes GetCurrentProcess's pseudo-handle as a 32-bit
    int, the call fails, and every number reads 0.)"""
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):                # PROCESS_MEMORY_COUNTERS_EX2
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                    *((name, ctypes.c_size_t) for name in ("peak_set", "set", "peak_paged", "paged", "peak_nonpaged",
                                                           "nonpaged", "commit", "peak_commit", "private",
                                                           "private_set", "shared_commit"))]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = ()
    info = kernel32.K32GetProcessMemoryInfo
    info.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
    info.restype = wintypes.BOOL
    return Counters, kernel32.GetCurrentProcess, info


def process_memory() -> dict[str, int]:
    """This process's RAM (bytes): the most it has held so far (peak_set), what it holds now (set), and how much of
    that is its own rather than shared with files on disk (private_set; 0 where Windows cannot tell)."""
    if sys.platform == "win32":
        import ctypes

        cls, current, info = _windows_counters()
        counters = cls()
        counters.cb = ctypes.sizeof(cls)
        if not info(current(), ctypes.byref(counters), counters.cb):
            counters.cb = cls.private_set.offset          # Windows before 10 1809: no private working set
            if not info(current(), ctypes.byref(counters), counters.cb):
                return {"peak_set": 0, "set": 0, "private_set": 0}
        return {"peak_set": int(counters.peak_set), "set": int(counters.set), "private_set": int(counters.private_set)}
    rss = {}
    with open("/proc/self/status", encoding="ascii") as fh:
        for line in fh:
            name, _, rest = line.partition(":")
            if name in ("VmHWM", "VmRSS", "RssAnon"):
                rss[name] = int(rest.split()[0]) * 1024
    return {"peak_set": rss.get("VmHWM", 0), "set": rss.get("VmRSS", 0), "private_set": rss.get("RssAnon", 0)}


def ram_peak() -> int:
    """The most RAM this process has held so far (bytes)."""
    return process_memory()["peak_set"]


class MemoryWatch:
    """Looks at the free RAM twice a second. Remembers the lowest it saw (for the measurements), and if the computer
    is about to run out, ends this process: whatever it was making is lost, but the computer does not stall."""

    def __init__(self, floor: int = RAM_FLOOR, every: float = 0.5,
                 measure: Callable[[], dict[str, int]] | None = None) -> None:
        self.floor = floor
        self.every = every
        self.measure = measure or memory
        self.lowest: int | None = None
        self.free = 0
        self._low = 0

    def start(self) -> MemoryWatch:
        threading.Thread(target=self._loop, name="memory-watch", daemon=True).start()
        return self

    def reset(self) -> None:
        self.lowest = None

    def check(self) -> bool:
        """One look. True when free RAM has been below the floor twice running: time to stop."""
        self.free = self.measure()["ram_free"]
        self.lowest = self.free if self.lowest is None else min(self.lowest, self.free)
        self._low = self._low + 1 if self.free < self.floor else 0
        return self._low >= 2

    def _loop(self) -> None:
        while True:
            try:
                stop = self.check()
            except Exception:
                return
            if stop:
                sys.stderr.write(f"stopping: the computer has only {self.free / GIB:.1f} GB of RAM free\n")
                sys.stderr.flush()
                os._exit(LOW_MEMORY_EXIT)
            time.sleep(self.every)


# What the next weights read turns floating-point tensors into: set around each part's load (see `_build`).
_READ: dict[str, Any] = {}


def read_safetensors(filename: Any, device: Any = "cpu", **_: Any) -> dict[str, Any]:
    """safetensors.torch.load_file, as diffusers calls it, without the memory map: each tensor is read on its own
    (pread) and made the dtype the part loads in right away, except in the modules the model keeps in fp32 (the same
    test diffusers applies). So loading a part holds that part once, in its final dtype, plus one tensor: not the
    file mapped beside it (on Windows charged in full, and kept alive by any fp32 tensor still pointing into it)."""
    from safetensors import safe_open

    dtype, keep = _READ.get("dtype"), tuple(_READ.get("keep") or ())
    out: dict[str, Any] = {}
    with safe_open(str(filename), framework="pt", device="cpu", backend="pread") as fh:
        for name in fh.keys():
            tensor = fh.get_tensor(name)
            if (dtype is not None and tensor.is_floating_point() and tensor.dtype != dtype
                    and not any(m in name.split(".") for m in keep)):
                tensor = tensor.to(dtype)
            out[name] = tensor if str(device) == "cpu" else tensor.to(device)
    return out


def safetensors_header(path: Path) -> dict[str, Any]:
    """The names and shapes in a .safetensors file, from its header alone (nothing is loaded)."""
    with path.open("rb") as fh:
        (size,) = struct.unpack("<Q", fh.read(8))
        header: dict[str, Any] = json.loads(fh.read(size))
    header.pop("__metadata__", None)
    return header


def weights(folder: Path) -> dict[str, tuple[int, int]]:
    """For each part of a model (transformer, text_encoder, vae): how many numbers it has, and how many of them stay
    in bf16 when the part is made 8-bit (the layers named in KEEP, and every 1-D one: norms and biases)."""
    parts: dict[str, tuple[int, int]] = {}
    for sub in sorted(p for p in folder.iterdir() if p.is_dir()):
        files = sorted(sub.glob("*.safetensors"))
        if not files:
            continue
        keep = [re.compile(pattern) for pattern in KEEP.get(sub.name, ())]
        total = kept = 0
        for file in files:
            for name, meta in safetensors_header(file).items():
                shape = [int(d) for d in meta["shape"]]
                count = 1
                for dim in shape:
                    count *= dim
                total += count
                module = name.rsplit(".", 1)[0]
                if len(shape) < 2 or any(k.search(module) for k in keep):
                    kept += count
        parts[sub.name] = (total, kept)
    return parts


def part_bytes(family: str, part: str, numbers: tuple[int, int], eight: bool) -> int:
    total, kept = numbers
    if part == "vae":
        return total * (4 if family == "video" else 2)        # Wan's decoder stays in full precision
    if eight and part in EIGHT_BIT_PARTS:
        return (total - kept) + kept * 2
    return total * 2


def load_order(family: str, parts: dict[str, tuple[int, int]], eight: bool) -> list[str]:
    """The order that keeps the RAM peak lowest. Each part loads whole and then shrinks (made 8-bit) before the next
    one loads; where the parts stay in RAM, the peak while a part loads is everything kept so far plus that part
    whole, and it is lowest when the parts that shrink most come first (biggest first when none shrinks)."""
    def shrink(p: str) -> int:
        return part_bytes(family, p, parts[p], False) - part_bytes(family, p, parts[p], eight)

    return sorted(parts, key=lambda p: (-shrink(p), -parts[p][0]))


def needs(family: str, parts: dict[str, tuple[int, int]], mode: str) -> tuple[int, int]:
    """(RAM, graphics memory) that `mode` needs at its peak, loading included, in bytes."""
    eight = mode.endswith("8")
    final = {p: part_bytes(family, p, n, eight) for p, n in parts.items()}
    on_card = mode in ("gpu", "gpu8")
    ram = held = 0
    for p in load_order(family, parts, eight):
        ram = max(ram, held + part_bytes(family, p, parts[p], False))   # each part loads whole, then shrinks
        if not on_card:
            held += final[p]
    ram = max(ram, held)
    room = HEADROOM[family]
    if on_card:
        vram = sum(final.values()) + room
    elif mode.startswith("model"):
        vram = max(final.values(), default=0) + room
    else:
        vram = GIB + room
    return ram, vram


def choose(family: str, parts: dict[str, tuple[int, int]], *, ram_free: int, vram_free: int, offload: str = "auto",
           floor: int = 0) -> str:
    """The fastest mode that fits the memory free right now. `offload` names the hungriest mode to try ("auto": any);
    `floor` skips the modes that already ran out of memory. None fits: WorkerError "low_memory", with the numbers."""
    start = floor if offload == "auto" else max(MODES.index(offload), floor)
    candidates = MODES[min(start, len(MODES) - 1):]
    for mode in candidates:
        ram, vram = needs(family, parts, mode)
        if ram + RAM_SPARE <= ram_free and vram <= vram_free:
            return mode
    ram, vram = needs(family, parts, candidates[-1])
    ram += RAM_SPARE
    raise WorkerError(f"not enough free memory to load the {family} model: it needs about {ram / GIB:.0f} GB of RAM "
                      f"and {vram / GIB:.0f} GB on the graphics card, and {ram_free / GIB:.1f} GB and "
                      f"{vram_free / GIB:.1f} GB are free", "low_memory", need_ram=ram, free_ram=ram_free,
                      need_vram=vram, free_vram=vram_free)


# ============================================================================ the real backend
def _need(req: dict[str, Any], key: str) -> Any:
    if key not in req or req[key] is None:
        raise WorkerError(f"the request has no {key!r}", "bad_request")
    return req[key]


def _dims(req: dict[str, Any], multiple: int) -> tuple[int, int]:
    width, height = int(_need(req, "width")), int(_need(req, "height"))
    if width % multiple or height % multiple or not (64 <= width <= 2048 and 64 <= height <= 2048):
        raise WorkerError(f"{width}x{height} is not a size this model makes (multiples of {multiple})", "bad_request")
    return width, height


def _out_path(raw: Any) -> Path:
    path = Path(str(raw))
    if not path.is_absolute():
        raise WorkerError(f"{raw!r} is not a full path", "bad_request")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class Diffusion:
    """The models themselves: one family (image or video) on the graphics card at a time."""

    def __init__(self, root: Path, *, offload: str = "auto", allow_cpu: bool = False,
                 watch: MemoryWatch | None = None) -> None:
        import torch

        self.torch = torch
        self.root = root
        self.offload = offload
        self.watch = watch
        self.cuda = torch.cuda.is_available()
        if not self.cuda and not allow_cpu:
            raise WorkerError("no NVIDIA graphics card that PyTorch can use was found", "no_gpu")
        self.device = "cuda" if self.cuda else "cpu"
        self.dtype = torch.bfloat16
        self.family: str | None = None
        self.variant: str | None = None
        self.mode: str | None = None
        self.pipe: Any = None
        self._floor: dict[str, int] = {}          # a mode that ran out of memory is not tried again for that family
        self._translator: tuple[Any, Any] | None = None
        import safetensors.torch

        safetensors.torch.load_file = read_safetensors    # what diffusers reads checkpoints with (transformers
                                                          # already reads a tensor at a time on Windows)

    # ------------------------------------------------------------------ facts
    def info(self) -> dict[str, Any]:
        t = self.torch
        info: dict[str, Any] = {"torch": t.__version__, "cuda": t.version.cuda, "gpu": self.cuda,
                                "loaded": self.family, "mode": self.mode}
        try:
            import diffusers

            info["diffusers"] = diffusers.__version__
        except Exception:
            pass
        if self.cuda:
            free, total = t.cuda.mem_get_info()
            info.update(device=t.cuda.get_device_name(0), capability=list(t.cuda.get_device_capability(0)),
                        vram_total=int(total), vram_free=int(free))
        try:
            info.update(memory())
        except Exception:
            pass
        return info

    def _free(self) -> int:
        return int(self.torch.cuda.mem_get_info()[0]) if self.cuda else 0

    # ------------------------------------------------------------------ loading
    def plan(self, family: str) -> dict[str, Any]:
        """What loading `family` takes in each mode, against the memory free right now. Nothing is loaded."""
        if family not in HEADROOM:
            raise WorkerError(f"unknown model family {family!r}", "bad_request")
        folder = self.root / (IMAGE_DIR if family == "image" else VIDEO_DIR)
        if not (folder / "model_index.json").is_file():
            raise WorkerError(f"the {family} model is not installed (expected in {folder})", "missing")
        parts = weights(folder)
        now = memory()
        ram_free, vram_free = now["ram_free"], self._free()
        plan: dict[str, Any] = {
            "family": family, "loaded": self.family, "ram_free": ram_free, "vram_free": vram_free,
            "commit_free": now.get("commit_free"),
            "parts": {name: {"numbers": total, "kept_bf16": kept} for name, (total, kept) in parts.items()},
            "needs": {mode: dict(zip(("ram", "vram"), needs(family, parts, mode), strict=True)) for mode in MODES},
            "ram_spare": RAM_SPARE}
        if not self.cuda:
            plan["mode"] = "cpu"
            return plan
        try:
            plan["mode"] = choose(family, parts, ram_free=ram_free, vram_free=vram_free, offload=self.offload,
                                  floor=self._floor.get(family, 0))
        except WorkerError as exc:
            plan.update(mode=None, refusal=str(exc))
        return plan

    def unload(self) -> None:
        if self.pipe is not None and hasattr(self.pipe, "remove_all_hooks"):
            # Offload hooks keep references to the modules; drop them so the memory really goes.
            try:
                self.pipe.remove_all_hooks()
            except Exception:
                pass
        self.pipe = None
        self.family = self.variant = self.mode = None
        gc.collect()
        if self.cuda:
            self.torch.cuda.empty_cache()

    @staticmethod
    def _class(family: str, variant: str) -> Any:
        """The pipeline for the job: from text, or redrawing a picture / starting from a first frame."""
        import diffusers

        names = {("image", "text"): "ZImagePipeline", ("image", "picture"): "ZImageImg2ImgPipeline",
                 ("video", "text"): "WanPipeline", ("video", "picture"): "WanImageToVideoPipeline"}
        return getattr(diffusers, names[(family, variant)])

    def _load(self, family: str, variant: str, progress: Progress) -> Any:
        if self.family == family and self.pipe is not None:
            if self.variant == variant:
                return self.pipe
            if self.mode != "group8":                    # the same weights in the other pipeline, without reloading
                self.pipe = self._switch(self.pipe, family, variant)
                self.variant = variant
                return self.pipe
        self.unload()
        folder = self.root / (IMAGE_DIR if family == "image" else VIDEO_DIR)
        if not (folder / "model_index.json").is_file():
            raise WorkerError(f"the {family} model is not installed (expected in {folder})", "missing")
        progress("load", 0.0)
        parts = weights(folder)
        if self.cuda:
            mode = choose(family, parts, ram_free=memory()["ram_free"], vram_free=self._free(),
                          offload=self.offload, floor=self._floor.get(family, 0))
        else:
            mode = "cpu8" if self.offload.endswith("8") else "cpu"          # tests: the same code, on the processor
        pipe = self._build(folder, family, variant, mode, parts, progress)
        self.pipe, self.family, self.variant, self.mode = pipe, family, variant, mode
        progress("load", 1.0, mode=mode)
        return pipe

    def _build(self, folder: Path, family: str, variant: str, mode: str, parts: dict[str, tuple[int, int]],
               progress: Progress) -> Any:
        """Load the parts one at a time (in `load_order`), each made 8-bit (when the mode says so) and moved to where
        it will run before the next one loads; then the pipeline around them."""
        import importlib

        torch = self.torch
        index = json.loads((folder / "model_index.json").read_text(encoding="utf-8"))
        eight = mode.endswith("8")
        on_card = mode in ("gpu", "gpu8")
        loaded: dict[str, Any] = {}
        order = load_order(family, parts, eight)
        for number, name in enumerate(order):
            library, class_name = index[name]
            module_cls = getattr(importlib.import_module(library), class_name)
            # Wan's video decoder is kept in full precision, as its makers advise.
            dtype = torch.float32 if (family == "video" and name == "vae") else self.dtype
            _READ.update(dtype=dtype, keep=getattr(module_cls, "_keep_in_fp32_modules", None))
            try:
                module = module_cls.from_pretrained(str(folder / name), dtype=dtype)
            finally:
                _READ.clear()
            if eight and name in EIGHT_BIT_PARTS:
                self._eight_bit(module)
            if on_card:
                module.to("cuda")
            loaded[name] = module
            gc.collect()
            progress("load", (number + 1) / (len(order) + 1), part=name)
        pipe = self._class(family, variant).from_pretrained(str(folder), **loaded, dtype=self.dtype)
        pipe.set_progress_bar_config(disable=True)
        if mode.startswith("model"):
            pipe.enable_model_cpu_offload()
        elif mode == "group8":
            pipe.enable_group_offload(onload_device=torch.device("cuda"), offload_type="leaf_level", use_stream=True)
        return pipe

    def _eight_bit(self, module: Any) -> None:
        """Keep the weights in fp8; each layer is turned back into bf16 just while it runs."""
        storage = self.torch.float8_e4m3fn
        if hasattr(module, "enable_layerwise_casting"):          # diffusers' models know which layers to keep
            module.enable_layerwise_casting(storage_dtype=storage, compute_dtype=self.dtype)
        else:
            from diffusers.hooks import apply_layerwise_casting

            apply_layerwise_casting(module, storage_dtype=storage, compute_dtype=self.dtype,
                                    skip_modules_pattern=TEXT_KEEP)

    def _switch(self, pipe: Any, family: str, variant: str) -> Any:
        """The other pipeline on the same modules. Not `from_pipe`: that casts every part to one dtype, which would
        turn Wan's full-precision decoder (and the parts kept in fp8) into bf16."""
        import inspect

        cls = self._class(family, variant)
        accepted = inspect.signature(cls.__init__).parameters
        parts = {k: v for k, v in pipe.components.items() if k in accepted}
        if "expand_timesteps" in accepted:
            parts["expand_timesteps"] = bool(pipe.config.get("expand_timesteps", False))
        offloaded = (self.mode or "").startswith("model")
        if offloaded:
            pipe.remove_all_hooks()
        new = cls(**parts)
        new.set_progress_bar_config(disable=True)
        if offloaded:
            new.enable_model_cpu_offload()
        return new

    def _retrying(self, family: str, variant: str, progress: Progress,
                  run: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        """Run on the loaded model; out of graphics memory, reload in the next thriftier mode and try again."""
        while True:
            if self.watch is not None:
                self.watch.reset()                     # before loading: the load is when RAM runs lowest
            pipe = self._load(family, variant, progress)
            if self.cuda:
                self.torch.cuda.reset_peak_memory_stats()
            try:
                result = run(pipe)
            except self.torch.cuda.OutOfMemoryError as exc:
                mode = self.mode or "gpu"
                self.unload()
                if mode not in MODES or mode == MODES[-1]:
                    raise WorkerError("there is not enough free memory on the graphics card for this, even "
                                      "streaming the model in parts; close a program that uses the card, or "
                                      "choose a smaller size", "oom") from exc
                self._floor[family] = MODES.index(mode) + 1
                progress("load", 0.0, retry=MODES[self._floor[family]])
                continue
            result["mode"] = self.mode
            if self.cuda:
                result["vram_peak"] = int(self.torch.cuda.max_memory_allocated())
            result["ram_peak"] = ram_peak()
            if self.watch is not None and self.watch.lowest is not None:
                result["ram_free_min"] = self.watch.lowest
            return result

    def _generator(self, seed: int) -> Any:
        return self.torch.Generator(device="cpu").manual_seed(seed)

    def _stepper(self, progress: Progress, steps: int, base: float = 0.0, span: float = 1.0, **extra: Any) -> Any:
        def on_step(pipe: Any, step: int, _timestep: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
            total = int(getattr(pipe, "num_timesteps", 0) or steps)       # a change runs fewer (its strength)
            progress("generate", base + span * (step + 1) / max(total, 1), step=step + 1, steps=total, **extra)
            return kwargs

        return on_step

    # ------------------------------------------------------------------ images
    def image(self, req: dict[str, Any], progress: Progress) -> dict[str, Any]:
        prompt = str(_need(req, "prompt"))
        width, height = _dims(req, 16)
        outs = [_out_path(p) for p in _need(req, "out")]
        steps = int(req.get("steps") or 9)
        seed = int(req["seed"]) if req.get("seed") is not None else int.from_bytes(os.urandom(4), "little")
        source = req.get("source")
        strength = float(req.get("strength") or 0.6)
        picture = None
        if source:
            from PIL import Image, ImageOps

            with Image.open(str(source)) as opened:
                picture = ImageOps.fit(ImageOps.exif_transpose(opened).convert("RGB"), (width, height))

        def run(pipe: Any) -> dict[str, Any]:
            files = []
            timings: list[float] = []
            for index, path in enumerate(outs):
                began = time.perf_counter()
                common = {"prompt": prompt, "height": height, "width": width, "num_inference_steps": steps,
                          "guidance_scale": float(req.get("guidance") or 0.0),
                          "generator": self._generator(seed + index),
                          "callback_on_step_end": self._stepper(progress, steps, index / len(outs), 1 / len(outs),
                                                                image=index + 1, images=len(outs))}
                if picture is not None:
                    made = pipe(image=picture, strength=strength, **common).images[0]
                else:
                    made = pipe(**common).images[0]
                tmp = path.with_name(path.name + ".part")
                made.save(str(tmp), format="PNG")
                tmp.replace(path)
                timings.append(round(time.perf_counter() - began, 3))
                files.append({"path": str(path), "width": made.width, "height": made.height,
                              "seed": seed + index})
            progress("save", 1.0)
            return {"files": files, "seed": seed, "steps": steps, "per_image_s": timings}

        return self._retrying("image", "picture" if picture is not None else "text", progress, run)

    # ------------------------------------------------------------------ videos
    def video(self, req: dict[str, Any], progress: Progress) -> dict[str, Any]:
        prompt = str(_need(req, "prompt"))
        width, height = _dims(req, 32)
        out = _out_path(_need(req, "out"))
        frames = int(_need(req, "frames"))
        if frames < 5 or (frames - 1) % 4:
            raise WorkerError(f"{frames} frames: the model makes 4n+1 frames", "bad_request")
        fps = int(req.get("fps") or 24)
        steps = int(req.get("steps") or 30)
        seed = int(req["seed"]) if req.get("seed") is not None else int.from_bytes(os.urandom(4), "little")
        first = req.get("image")
        picture = None
        if first:
            from PIL import Image, ImageOps

            with Image.open(str(first)) as opened:
                picture = ImageOps.fit(ImageOps.exif_transpose(opened).convert("RGB"), (width, height))

        def run(pipe: Any) -> dict[str, Any]:
            common = {"prompt": prompt, "negative_prompt": str(req.get("negative") or VIDEO_NEGATIVE),
                      "height": height, "width": width, "num_frames": frames, "num_inference_steps": steps,
                      "guidance_scale": float(req.get("guidance") or 5.0), "generator": self._generator(seed),
                      "callback_on_step_end": self._stepper(progress, steps, 0.0, 0.95), "output_type": "np"}
            began = time.perf_counter()
            video = (pipe(image=picture, **common) if picture is not None else pipe(**common)).frames[0]
            made_s = time.perf_counter() - began
            progress("save", 0.97)
            write_mp4(video, out, fps)
            return {"files": [{"path": str(out), "width": width, "height": height, "frames": frames, "fps": fps,
                               "seconds": round(frames / fps, 3)}],
                    "seed": seed, "steps": steps, "generate_s": round(made_s, 3)}

        return self._retrying("video", "picture" if picture is not None else "text", progress, run)

    # ------------------------------------------------------------------ translation
    def translate(self, text: str) -> str:
        if not HEBREW.search(text):
            return text
        if self._translator is None:
            folder = self.root / TRANSLATE_DIR
            if not (folder / "config.json").is_file():
                raise WorkerError(f"the translation model is not installed (expected in {folder})", "missing")
            from transformers import MarianMTModel, MarianTokenizer

            tokenizer = MarianTokenizer.from_pretrained(str(folder))
            model = MarianMTModel.from_pretrained(str(folder)).eval()       # on the processor: it is small
            self._translator = (tokenizer, model)
        tokenizer, model = self._translator
        return translate_lines(text, lambda pieces: _marian(tokenizer, model, pieces, self.torch))


def _marian(tokenizer: Any, model: Any, pieces: list[str], torch: Any) -> list[str]:
    with torch.inference_mode():
        batch = tokenizer(pieces, return_tensors="pt", padding=True, truncation=True, max_length=400)
        made = model.generate(**batch, max_length=512, num_beams=4)       # the model's own limit
    return [s.strip() for s in tokenizer.batch_decode(made, skip_special_tokens=True)]


def translate_lines(text: str, translate: Callable[[list[str]], list[str]]) -> str:
    """Each line with Hebrew in it, sentence by sentence; English lines, and labels such as "Scene:", stay."""
    lines = text.split("\n")
    jobs: list[tuple[int, str, list[str]]] = []
    for index, line in enumerate(lines):
        if not HEBREW.search(line):
            continue
        match = LABEL.match(line)
        label, body = (match.group(1), match.group(2)) if match else ("", line)
        sentences = [s for s in SENTENCE.split(body.strip()) if s.strip()]
        jobs.append((index, label, sentences))
    flat = [s for _i, _l, sentences in jobs for s in sentences]
    done = iter(translate(flat) if flat else [])
    for index, label, sentences in jobs:
        lines[index] = label + " ".join(next(done) for _ in sentences)
    return "\n".join(lines)


def write_mp4(frames: Any, path: Path, fps: int) -> None:
    """H.264 in an MP4, yuv420p (what browsers and Windows play), written beside and then moved into place."""
    import imageio.v2 as imageio
    import numpy as np

    tmp = path.with_name(path.stem + ".part.mp4")
    writer = imageio.get_writer(str(tmp), fps=fps, codec="libx264", quality=8, pixelformat="yuv420p",
                                macro_block_size=16, ffmpeg_log_level="error")
    try:
        for frame in frames:
            array = np.asarray(frame)
            if array.dtype != np.uint8:
                array = (np.clip(array, 0.0, 1.0) * 255).round().astype(np.uint8)
            writer.append_data(array)
    finally:
        writer.close()
    tmp.replace(path)


# ============================================================================ the process
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="the folder the models were installed in")
    parser.add_argument("--offload", default="auto", choices=("auto", *MODES),
                        help="the hungriest mode to try (auto: whatever fits the free memory)")
    parser.add_argument("--allow-cpu", action="store_true", help="run without a graphics card (tests only: slow)")
    args = parser.parse_args(argv)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "DIFFUSERS_NO_ADVISORY_WARNINGS"):
        os.environ[name] = "1"
    # The answers go to the original stdout; anything a library prints lands in the log (stderr) instead.
    answers = io.TextIOWrapper(os.fdopen(os.dup(1), "wb"), encoding="utf-8", line_buffering=True)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    lines = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    root = Path(args.root)
    watch = MemoryWatch().start()
    return serve(lambda: Diffusion(root, offload=args.offload, allow_cpu=args.allow_cpu, watch=watch), lines,
                 answers)


if __name__ == "__main__":
    sys.exit(main())
