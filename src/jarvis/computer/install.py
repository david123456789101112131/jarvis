"""`jarvis computer install`: get everything the computer-control model needs onto this machine.

Three pieces, under one folder (the models folder, or one given with --dir):
  llama.cpp/<tag>/   the runtime (a pinned Windows CUDA build of llama.cpp + its CUDA runtime)
  hf/Holo-3.1-9B/    H Company's open Holo3.1-9B weights (Apache-2.0), at a pinned revision
  gguf/              the weights converted to GGUF (Q8_0) plus the vision projector (F16), which is
                     what llama-server loads

Everything downloaded is checked against the publisher's own SHA-256 (the GitHub release asset digest and
the Hugging Face LFS oid), downloads resume and retry, and the conversion runs in a throwaway virtual
environment so it never touches JARVIS's own one. When the files are already present (for example a folder
downloaded earlier), `--adopt <dir>` just points JARVIS at them and verifies them, with nothing to download.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jarvis.computer.server import ModelFiles
from jarvis.security.secrets import sanitized_env

log = logging.getLogger(__name__)

LLAMA_REPO = "ggml-org/llama.cpp"
LLAMA_TAG = "b11716"                           # the build verified on the target machine
HF_REPO = "Hcompany/Holo-3.1-9B"
HF_REVISION = "bcd6a36af9f100f57cba28e2bf6481ac3feedf33"
MODEL_GGUF = "Holo-3.1-9B-Q8_0.gguf"
MMPROJ_GGUF = "mmproj-Holo-3.1-9B-F16.gguf"
CHUNK = 1 << 20
RETRIES = 5

Progress = Callable[[str], None]


@dataclass
class InstallResult:
    root: Path
    adopted: bool = False
    downloaded: list[str] = field(default_factory=list)
    converted: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return ModelFiles(self.root).problem() is None


# ------------------------------------------------------------------------------------- hashing
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _digest_hex(value: str | None) -> str:
    """GitHub and Hugging Face give a digest as 'sha256:HEX' (or sometimes just HEX)."""
    if not value:
        return ""
    return value.split(":", 1)[1].lower() if ":" in value else value.lower()


# ------------------------------------------------------------------------------------- download
def _client() -> httpx.Client:
    # trust_env so a corporate proxy still applies; the model server, in contrast, is local-only.
    return httpx.Client(timeout=120.0, follow_redirects=True, headers={"User-Agent": "JARVIS-installer"})


def download(client: httpx.Client, url: str, dest: Path, *, sha256: str = "", progress: Progress = print) -> None:
    """Download `url` to `dest`, resuming a partial file and retrying, then verify SHA-256 when given."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if sha256 and dest.is_file() and sha256_file(dest) == sha256.lower():
        progress(f"  already have {dest.name}")
        return
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, RETRIES + 1):
        have = part.stat().st_size if part.is_file() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code not in (200, 206):
                    raise OSError(f"HTTP {resp.status_code} for {url}")
                mode = "ab" if resp.status_code == 206 and have else "wb"
                with part.open(mode) as f:
                    for chunk in resp.iter_bytes(CHUNK):
                        f.write(chunk)
            break
        except (httpx.HTTPError, OSError) as exc:
            if attempt == RETRIES:
                raise
            progress(f"  retry {attempt} for {dest.name} ({exc})")
    if sha256:
        got = sha256_file(part)
        if got != sha256.lower():
            part.unlink(missing_ok=True)
            raise OSError(f"{dest.name}: checksum mismatch (expected {sha256[:12]}…, got {got[:12]}…)")
    part.replace(dest)
    progress(f"  downloaded {dest.name}")


# ------------------------------------------------------------------------------------- sources
def github_release(client: httpx.Client, tag: str) -> list[dict]:
    resp = client.get(f"https://api.github.com/repos/{LLAMA_REPO}/releases/tags/{tag}")
    resp.raise_for_status()
    return list(resp.json().get("assets") or [])


def pick_asset(assets: list[dict], *must: str) -> dict | None:
    for a in assets:
        name = (a.get("name") or "").lower()
        if all(m.lower() in name for m in must):
            return a
    return None


def hf_tree(client: httpx.Client, revision: str, repo: str = HF_REPO) -> list[dict]:
    resp = client.get(f"https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=1")
    resp.raise_for_status()
    return [e for e in resp.json() if e.get("type") == "file"]


# ------------------------------------------------------------------------------------- the steps
def install_llama(client: httpx.Client, root: Path, progress: Progress, *, source: bool = True) -> list[str]:
    """Download and extract the pinned Windows CUDA build of llama.cpp and its CUDA runtime, plus (`source`) the
    source, whose `convert_hf_to_gguf.py` does the conversion."""
    got: list[str] = []
    assets = github_release(client, LLAMA_TAG)
    build = pick_asset(assets, "bin-win-cuda", "x64") or pick_asset(assets, "bin-win-cuda")
    cudart = pick_asset(assets, "cudart", "win", "x64") or pick_asset(assets, "cudart", "win")
    if build is None or cudart is None:
        raise OSError(f"llama.cpp {LLAMA_TAG} has no Windows CUDA build asset (is the tag right?)")
    dest = root / "llama.cpp" / LLAMA_TAG
    for asset in (build, cudart):
        zip_path = dest / str(asset["name"])
        download(client, asset["browser_download_url"], zip_path, sha256=_digest_hex(asset.get("digest")),
                 progress=progress)
        _extract(zip_path, dest, progress)
        got.append(str(asset["name"]))
    if not source:
        return got
    src_zip = dest / f"source-{LLAMA_TAG}.zip"
    download(client, f"https://github.com/{LLAMA_REPO}/archive/refs/tags/{LLAMA_TAG}.zip", src_zip, progress=progress)
    _extract(src_zip, root / "llama.cpp" / f"src-{LLAMA_TAG}", progress)
    return got


def download_model(client: httpx.Client, root: Path, progress: Progress) -> list[str]:
    """Download every file of the Holo model at the pinned revision, each verified against its LFS oid."""
    got: list[str] = []
    hf = root / "hf" / "Holo-3.1-9B"
    for entry in hf_tree(client, HF_REVISION):
        path = str(entry["path"])
        sha = _digest_hex((entry.get("lfs") or {}).get("oid"))
        url = f"https://huggingface.co/{HF_REPO}/resolve/{HF_REVISION}/{path}"
        download(client, url, hf / path, sha256=sha, progress=progress)
        got.append(path)
    return got


def convert(root: Path, progress: Progress) -> list[str]:
    """Convert the weights to GGUF (Q8_0) and the vision projector (F16) in a throwaway virtual environment."""
    src = _one_dir(root / "llama.cpp" / f"src-{LLAMA_TAG}")
    script = src / "convert_hf_to_gguf.py"
    if not script.is_file():
        raise OSError(f"the llama.cpp source is missing {script.name}")
    venv = root / "convert-venv"
    py = _venv_python(venv)
    if not py.is_file():
        progress("  creating the conversion environment…")
        _run([sys.executable, "-m", "venv", str(venv)], progress)
        reqs = src / "requirements" / "requirements-convert_hf_to_gguf.txt"
        _run([str(py), "-m", "pip", "install", "-q", "-r", str(reqs)] if reqs.is_file()
             else [str(py), "-m", "pip", "install", "-q", "gguf", "safetensors", "numpy", "torch", "transformers"],
             progress)
    hf = root / "hf" / "Holo-3.1-9B"
    gguf = root / "gguf"
    gguf.mkdir(parents=True, exist_ok=True)
    model, mmproj = gguf / MODEL_GGUF, gguf / MMPROJ_GGUF
    progress("  converting the weights (Q8_0)…")
    _run([str(py), str(script), str(hf), "--no-mtp", "--outtype", "q8_0", "--outfile", str(model)], progress)
    progress("  converting the vision projector (F16)…")
    _run([str(py), str(script), str(hf), "--mmproj", "--outtype", "f16", "--outfile", str(mmproj)], progress)
    return [MODEL_GGUF, MMPROJ_GGUF]


def write_manifest(root: Path) -> None:
    files = ModelFiles(root)
    manifest = {
        "llama_tag": LLAMA_TAG, "hf_repo": HF_REPO, "hf_revision": HF_REVISION,
        "model": _entry(files.model), "mmproj": _entry(files.mmproj), "server": str(files.exe or ""),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# ------------------------------------------------------------------------------------- orchestrator
def install(root: Path, *, adopt: Path | None = None, do_convert: bool = True,
            progress: Progress = print) -> InstallResult:
    """Put everything under `root` (downloading as needed), or adopt an existing `adopt` folder."""
    if adopt is not None:
        problem = ModelFiles(adopt).problem()
        if problem is not None:
            raise OSError(f"{adopt} is not a complete model folder: {problem}")
        progress(f"Using the model already in {adopt}.")
        return InstallResult(root=adopt, adopted=True, notes=["adopted an existing folder"])

    root.mkdir(parents=True, exist_ok=True)
    result = InstallResult(root=root)
    if ModelFiles(root).problem() is None:
        progress("The computer-control model is already installed.")
        return result
    if sys.platform != "win32":
        result.notes.append("the prebuilt runtime is Windows-only; download is for the target machine")
    with _client() as client:
        progress("Downloading the runtime (llama.cpp)…")
        result.downloaded += install_llama(client, root, progress)
        progress("Downloading the model (Holo3.1-9B)…")
        result.downloaded += download_model(client, root, progress)
    if do_convert:
        progress("Converting the model…")
        result.converted += convert(root, progress)
        write_manifest(root)
    return result


# ------------------------------------------------------------------------------------- helpers
def _extract(zip_path: Path, dest: Path, progress: Progress) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.startswith("/") or ".." in Path(member).parts:   # never let a zip escape its folder
                raise OSError(f"unsafe path in {zip_path.name}: {member}")
        zf.extractall(dest)
    progress(f"  extracted {zip_path.name}")


def _one_dir(parent: Path) -> Path:
    """A GitHub source zip unpacks to a single top folder (llama.cpp-<tag>); return it."""
    subs = [p for p in parent.iterdir() if p.is_dir()] if parent.is_dir() else []
    return subs[0] if len(subs) == 1 else parent


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _run(args: list[str], progress: Progress) -> None:
    import subprocess

    progress(f"  $ {Path(args[0]).name} {' '.join(args[1:3])}…")
    proc = subprocess.run(args, env=sanitized_env(), capture_output=True, text=True)
    if proc.returncode != 0:
        raise OSError(f"{Path(args[0]).name} failed ({proc.returncode}): {proc.stderr[-800:]}")


def _entry(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


__all__ = ["HF_REPO", "HF_REVISION", "LLAMA_TAG", "InstallResult", "convert", "download", "install",
           "pick_asset", "sha256_file", "write_manifest"]
