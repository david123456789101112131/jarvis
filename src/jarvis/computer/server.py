"""Start and stop the local model server (llama.cpp's `llama-server`) for computer control.

The server (`jarvis.common.llama.LlamaServer`) listens on 127.0.0.1 only, with a random API key. It loads the
Holo weights onto the GPU once and stays up while JARVIS is using the computer; it is stopped again after a spell
of not being used (`idle_minutes`).

Files are found under the models folder, however `jarvis computer install` or an existing download laid
them out: the `llama-server` executable anywhere under `llama.cpp/`, and the Q8_0 weights and the vision
projector (`mmproj`) under `gguf/` (or the folder itself).
"""

from __future__ import annotations

import logging
from pathlib import Path

from jarvis.common.llama import EXE_NAME, LlamaServer, find_gguf, find_llama_server, ollama_runtimes
from jarvis.config.settings import ComputerSettings
from jarvis.security.process import ProcessRegistry

log = logging.getLogger(__name__)

OWNER = "computer-model"
GPU_NAME = "computer"
IMAGE_MIN_TOKENS = 1024        # the least picture detail the model grounds clicks well with


class ModelFiles:
    """Where the server executable and the weights are; `problem` says what is missing."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.exe = find_llama_server(root)
        self.model = find_gguf(root, "Holo", "Q8")
        self.mmproj = find_gguf(root, "mmproj")

    def problem(self) -> str | None:
        if self.exe is None and not ollama_runtimes():
            return "llama-server is not installed (run: jarvis computer install)."
        if self.model is None:
            return "the computer-control model weights are missing (run: jarvis computer install)."
        if self.mmproj is None:
            return "the model's vision projector (mmproj) is missing (run: jarvis computer install)."
        return None


class ModelServer(LlamaServer):
    """The Holo model's `llama-server`: started for a task, stopped when idle (`stop_if_idle`)."""

    owner = OWNER
    gpu_name = GPU_NAME
    label = "the computer-control model server"
    log_name = "computer-model.log"

    def __init__(self, settings: ComputerSettings, root: Path, processes: ProcessRegistry, logs_dir: Path) -> None:
        super().__init__(processes, logs_dir)
        self.settings = settings
        self.files = ModelFiles(root)

    def problem(self) -> str | None:
        return self.files.problem()

    @property
    def exe(self) -> Path | None:
        return self.files.exe

    def wanted_port(self) -> int:
        return self.settings.port

    def command(self) -> list[str]:
        """The llama-server command line, for the port and key of this start."""
        exe = self.runtime.exe if self.runtime is not None else self.files.exe
        assert exe and self.files.model and self.files.mmproj
        args = [
            str(exe), "-m", str(self.files.model), "--mmproj", str(self.files.mmproj),
            "--host", "127.0.0.1", "--port", str(self._port),
            "-ngl", str(self.settings.gpu_layers), "-c", str(self.settings.context),
            "-fa", "on", "--jinja", "--parallel", "1", "--api-key", self._api_key,
        ]
        # Qwen-VL based models place clicks badly on pictures of fewer than 1024 tokens (llama-server warns about it
        # at load); smaller screenshots (a small or square screen) are scaled up to that. Passed where the build has
        # it (a build that was not asked, in a test, gets it too).
        if not self.flags or "--image-min-tokens" in self.flags:
            args += ["--image-min-tokens", str(IMAGE_MIN_TOKENS)]
        return args

    async def stop_if_idle(self) -> bool:
        return await self.stop_after(self.settings.idle_minutes * 60)


__all__ = ["EXE_NAME", "ModelFiles", "ModelServer"]
