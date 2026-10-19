"""`jarvis media install`: the runtime and the free models for making images and videos on this computer.

Everything goes under one folder (`media.models_dir`, by default <cache>/models/media):
  venv/                    a Python environment just for this: PyTorch with CUDA, diffusers, transformers…
  Z-Image-Turbo/           Tongyi-MAI/Z-Image-Turbo (Apache 2.0): images
  Wan2.2-TI2V-5B/          Wan-AI/Wan2.2-TI2V-5B-Diffusers (Apache 2.0): videos
  opus-mt-tc-big-he-en/    Helsinki-NLP/opus-mt-tc-big-he-en (CC-BY 4.0): Hebrew descriptions to English
The models are taken at the revisions verified on the target machine, and every file is checked against Hugging
Face's own SHA-256 (the LFS oid) with the downloader computer control uses (resume, retry). Files already present
and matching are kept, so a folder downloaded another way is verified rather than downloaded again. The packages
are pinned to the versions verified there too; the environment is created from a Python 3.11+ on this computer and
never touches JARVIS's own.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jarvis.computer.install import _client, _digest_hex, download, sha256_file
from jarvis.media.local.engine import IMAGE_DIR, TRANSLATE_DIR, VIDEO_DIR, LocalFiles, venv_python
from jarvis.security.secrets import sanitized_env

Progress = Callable[[str], None]


@dataclass(frozen=True)
class Repo:
    folder: str
    repo: str
    revision: str
    license: str
    skip: tuple[str, ...] = ()            # files the pipeline never reads (other frameworks' copies, pictures)


# Revisions as downloaded and checked on the target PC on 2026-10-19.
REPOS: tuple[Repo, ...] = (
    Repo(IMAGE_DIR, "Tongyi-MAI/Z-Image-Turbo", "f332072aa78be7aecdf3ee76d5c247082da564a6", "apache-2.0"),
    Repo(VIDEO_DIR, "Wan-AI/Wan2.2-TI2V-5B-Diffusers", "b8fff7315c768468a5333511427288870b2e9635", "apache-2.0"),
    Repo(TRANSLATE_DIR, "Helsinki-NLP/opus-mt-tc-big-he-en", "134c5a850dcaa763eec85bd1f4eb25112fecedbb", "cc-by-4.0",
         skip=("tf_model.h5", "flax_model.msgpack", "rust_model.ot", "pytorch_model.bin",   # model.safetensors
               "benchmark_results.txt", "benchmark_translations.zip")),
)
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".pdf")
SKIP_FOLDERS = ("assets/", "examples/")          # the model cards' showcase pictures and videos

TORCH = "torch==2.14.1"
TORCH_INDEX = "https://download.pytorch.org/whl/cu130"      # CUDA 13 builds: they include Blackwell (sm_120)
PACKAGES = ("diffusers==0.41.0", "transformers==5.20.1", "accelerate==1.15.0", "safetensors==0.8.0",
            "sentencepiece==0.2.2", "sacremoses==0.2.0", "imageio==2.38.1", "imageio-ffmpeg==0.6.0",
            "pillow==13.0.0", "numpy==2.5.4", "ftfy==6.3.1")     # ftfy: Wan's image-to-video cleans text with it
CHECK = ("import torch, diffusers, transformers, imageio_ffmpeg, PIL, numpy; "
         "print(torch.__version__, diffusers.__version__, transformers.__version__)")


@dataclass
class InstallResult:
    root: Path
    downloaded: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        files = LocalFiles(self.root)
        return all(files.has(part) for part in ("runtime", "image", "video", "translate"))


# ------------------------------------------------------------------------------------- the runtime
def base_python() -> str:
    """A Python 3.11+ to make the environment with: this one when it is a real interpreter, else one installed."""
    if not getattr(sys, "frozen", False):
        return str(getattr(sys, "_base_executable", None) or sys.executable)
    for candidate in (["py", "-3.13"], ["py", "-3.12"], ["py", "-3.11"], ["python3"], ["python"]):
        exe = shutil.which(candidate[0])
        if exe is None:
            continue
        try:
            out = subprocess.run([exe, *candidate[1:], "-c", "import sys; print(sys.executable); "
                                 "print(sys.version_info >= (3, 11))"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        lines = out.stdout.strip().splitlines()
        if out.returncode == 0 and len(lines) == 2 and lines[1] == "True":
            return lines[0]
    raise OSError("no Python 3.11 or newer was found on this computer to make the media environment with "
                  "(install one from python.org, then run this again)")


def runtime_ready(venv: Path) -> str | None:
    """The versions the environment reports when everything imports, else None."""
    py = venv_python(venv)
    if not py.is_file():
        return None
    proc = subprocess.run([str(py), "-I", "-c", CHECK], env=sanitized_env(), capture_output=True, text=True,
                          timeout=300)
    return proc.stdout.strip() if proc.returncode == 0 else None


def install_runtime(root: Path, progress: Progress) -> list[str]:
    venv = root / "venv"
    have = runtime_ready(venv)
    if have:
        progress(f"  the environment is ready (torch, diffusers, transformers: {have})")
        return []
    py = venv_python(venv)
    if not py.is_file():
        progress("  creating the environment…")
        _run([base_python(), "-m", "venv", str(venv)], progress)
    progress(f"  installing PyTorch with CUDA ({TORCH}, about 3 GB)…")
    _run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"], progress)
    _run([str(py), "-m", "pip", "install", "-q", TORCH, "--index-url", TORCH_INDEX], progress)
    progress("  installing diffusers, transformers and the video writer…")
    _run([str(py), "-m", "pip", "install", "-q", *PACKAGES], progress)
    have = runtime_ready(venv)
    if not have:
        raise OSError("the media environment was installed but does not import (see the messages above)")
    progress(f"  ready: {have}")
    return [TORCH, *PACKAGES]


# ------------------------------------------------------------------------------------- the models
def hf_files(client: httpx.Client, repo: Repo) -> list[dict]:
    resp = client.get(f"https://huggingface.co/api/models/{repo.repo}/tree/{repo.revision}?recursive=1")
    resp.raise_for_status()
    files = [e for e in resp.json() if e.get("type") == "file"]
    return [e for e in files if wanted(repo, str(e["path"]))]


def wanted(repo: Repo, path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return not (name in repo.skip or name.lower().endswith(SKIP_SUFFIXES) or name == ".gitattributes"
                or path.startswith(SKIP_FOLDERS))


def install_model(client: httpx.Client, root: Path, repo: Repo, progress: Progress) -> tuple[list[str], list[str]]:
    """Every file of `repo` at its revision into `root/<folder>`, checked against its SHA-256 (LFS files) or its
    size (small text files, which Hugging Face lists without one)."""
    got: list[str] = []
    kept: list[str] = []
    folder = root / repo.folder
    for entry in hf_files(client, repo):
        path = str(entry["path"])
        dest = folder / path
        sha = _digest_hex((entry.get("lfs") or {}).get("oid"))
        size = int(entry.get("size") or 0)
        if dest.is_file() and dest.stat().st_size == size and (not sha or sha256_file(dest) == sha):
            kept.append(path)
            continue
        url = f"https://huggingface.co/{repo.repo}/resolve/{repo.revision}/{path}"
        dest.unlink(missing_ok=True)
        download(client, url, dest, sha256=sha, progress=progress)
        if not sha and dest.stat().st_size != size:
            raise OSError(f"{repo.repo}/{path}: expected {size} bytes, got {dest.stat().st_size}")
        got.append(path)
    progress(f"  {len(got)} downloaded, {len(kept)} already here and checked")
    return got, kept


def write_manifest(root: Path, runtime: str | None) -> None:
    manifest = {"runtime": runtime, "torch_index": TORCH_INDEX,
                "models": {r.folder: {"repo": r.repo, "revision": r.revision, "license": r.license} for r in REPOS}}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def install(root: Path, *, models: tuple[str, ...] = (IMAGE_DIR, VIDEO_DIR, TRANSLATE_DIR), runtime: bool = True,
            progress: Progress = print) -> InstallResult:
    root.mkdir(parents=True, exist_ok=True)
    result = InstallResult(root=root)
    if runtime:
        progress("The runtime (PyTorch, diffusers)…")
        result.downloaded += install_runtime(root, progress)
    with _client() as client:
        for repo in REPOS:
            if repo.folder not in models:
                continue
            progress(f"{repo.repo} ({repo.license})…")
            got, kept = install_model(client, root, repo, progress)
            result.downloaded += [f"{repo.folder}/{p}" for p in got]
            result.verified += [f"{repo.folder}/{p}" for p in kept]
    write_manifest(root, runtime_ready(root / "venv") if runtime else None)
    if sys.platform != "win32":
        result.notes.append("made for Windows with an NVIDIA graphics card; elsewhere it runs only if PyTorch "
                            "finds a CUDA card")
    return result


# ------------------------------------------------------------------------------------- helpers
def _run(args: list[str], progress: Progress) -> None:
    progress(f"  $ {Path(args[0]).name} {' '.join(args[1:4])}…")
    proc = subprocess.run(args, env=sanitized_env(), capture_output=True, text=True)
    if proc.returncode != 0:
        raise OSError(f"{Path(args[0]).name} failed ({proc.returncode}): {(proc.stderr or proc.stdout)[-800:]}")


__all__ = ["PACKAGES", "REPOS", "TORCH", "TORCH_INDEX", "InstallResult", "Repo", "base_python", "hf_files",
           "install", "install_model", "install_runtime", "runtime_ready", "wanted", "write_manifest"]
