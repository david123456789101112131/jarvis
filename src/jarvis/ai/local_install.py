"""`jarvis chat install`: the model JARVIS talks, plans and works with, on this computer.

The weights: Google's Gemma 4 12B (instruction-tuned, quantization-aware trained) as a 4-bit GGUF, Unsloth's
UD-Q4_K_XL build (about 6.7 GB, Apache 2.0), at a pinned revision and checked against the publisher's SHA-256.
The runtime: computer control's llama.cpp when that is installed, otherwise the same pinned Windows CUDA build,
downloaded beside the model. Downloads resume and retry (`jarvis.computer.install.download`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jarvis.ai.local_server import ChatFiles
from jarvis.common.llama import find_llama_server
from jarvis.computer import install as base

HF_REPO = "unsloth/gemma-4-12B-it-qat-GGUF"
HF_REVISION = "980b060c40a8539ac159e0501a3e0f66a6365af3"
MODEL_GGUF = "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"


@dataclass
class ChatInstallResult:
    root: Path
    runtimes: tuple[Path, ...] = ()
    downloaded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return ChatFiles(self.root, *self.runtimes).problem() is None


def download_model(client: httpx.Client, root: Path, progress: base.Progress) -> list[str]:
    """The pinned GGUF, verified against its LFS oid (a copy already there and whole is kept)."""
    entry = next((e for e in base.hf_tree(client, HF_REVISION, HF_REPO) if e.get("path") == MODEL_GGUF), None)
    sha = base._digest_hex((entry or {}).get("lfs", {}).get("oid"))
    if entry is None or not sha:
        raise OSError(f"{HF_REPO} at {HF_REVISION[:8]} has no {MODEL_GGUF} with a checksum")
    url = f"https://huggingface.co/{HF_REPO}/resolve/{HF_REVISION}/{MODEL_GGUF}"
    base.download(client, url, root / "gguf" / MODEL_GGUF, sha256=sha, progress=progress)
    return [MODEL_GGUF]


def install(root: Path, *, runtimes: tuple[Path, ...] = (), progress: base.Progress = print) -> ChatInstallResult:
    """Put the chat model under `root`, and llama.cpp too unless one of `runtimes` (computer control's models
    folder) already has it."""
    root.mkdir(parents=True, exist_ok=True)
    result = ChatInstallResult(root=root, runtimes=runtimes)
    files = ChatFiles(root, *runtimes)
    with base._client() as client:
        if files.model is None or files.model.name == MODEL_GGUF:
            progress("Checking the chat model (Gemma 4 12B, about 6.7 GB)…")
            result.downloaded += download_model(client, root, progress)
        else:
            result.notes.append(f"kept the model already there: {files.model.name}")
        if find_llama_server(root, *runtimes) is None:
            if sys.platform != "win32":
                result.notes.append("the prebuilt runtime is Windows-only; download is for the target machine")
            progress("Downloading the runtime (llama.cpp)…")
            result.downloaded += base.install_llama(client, root, progress, source=False)
    return result


__all__ = ["HF_REPO", "HF_REVISION", "MODEL_GGUF", "ChatInstallResult", "download_model", "install"]
