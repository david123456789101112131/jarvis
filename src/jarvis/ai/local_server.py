"""The chat model on this computer: JARVIS's own `llama-server` with Gemma 4 12B, for chat, the agents and planning.

Free, and nothing leaves the computer. `jarvis chat install` puts the weights (Google's Gemma 4 12B, quantization-
aware trained, 4-bit: about 7 GB, Apache 2.0) in the model folder; the llama.cpp runtime is computer control's copy
when that is installed, or one installed beside the model. The server starts while JARVIS starts (`preload`) or at the first
request, and stays loaded (or stops after `idle_minutes` unused, when set) until computer control or image and video
creation need the graphics card ("chat" in `GpuTurns`); the next request starts it again (several seconds). Where Windows blocks the downloaded llama.cpp (Smart
App Control), the signed llama-server inside Ollama runs the same model file (`jarvis.common.llama.Runtime`).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from jarvis.common.llama import LlamaServer, find_gguf, find_llama_server, ollama_runtimes
from jarvis.config.settings import LocalModelSettings, Settings
from jarvis.security.process import ProcessRegistry

OWNER = "chat-model"
GPU_NAME = "chat"
MODEL_HINT = "gemma-4-12b"            # the weights' file name has this (any case)
RESCAN_S = 15.0                       # a missing model is looked for again this often (it may have been installed)
# Google's advice for Gemma 4, for every kind of use.
SAMPLING = ("--temp", "1.0", "--top-p", "0.95", "--top-k", "64")


def chat_root(settings: Settings, models: Path) -> Path:
    """The chat model's folder: `ai.local.models_dir`, else "chat" beside computer control's models (one models
    folder for JARVIS), else <cache>/models/chat."""
    if settings.ai.local.models_dir:
        return Path(settings.ai.local.models_dir)
    if settings.computer.models_dir:
        return Path(settings.computer.models_dir).parent / "chat" \
            if Path(settings.computer.models_dir).name == "computer" else Path(settings.computer.models_dir) / "chat"
    return models / "chat"


def runtime_roots(settings: Settings, models: Path) -> tuple[Path, ...]:
    """Other folders that may hold llama.cpp: computer control's models folder."""
    return (Path(settings.computer.models_dir) if settings.computer.models_dir else models / "computer",)


def attach(router: Any, settings: Settings, models: Path, processes: ProcessRegistry, logs: Path) -> ChatServer | None:
    """Give the router's local provider JARVIS's own chat server (unless `ai.local.base_url` names another one)."""
    from jarvis.ai.openai_provider import LocalModelProvider

    local = router.providers.get("local")
    if not isinstance(local, LocalModelProvider) or settings.ai.local.base_url:
        return None
    local.server = ChatServer(settings.ai.local, chat_root(settings, models), processes, logs,
                              runtimes=runtime_roots(settings, models))
    return local.server


class ChatFiles:
    """Where the server executable and the weights are; `problem` says what is missing."""

    def __init__(self, root: Path, *runtimes: Path) -> None:
        self.root = root
        self.runtimes = runtimes
        self.exe = find_llama_server(root, *runtimes)
        self.model = find_gguf(root, MODEL_HINT, without=("mmproj", "mtp"))

    def problem(self) -> str | None:
        if self.model is None:
            return "the chat model is not installed (run: jarvis chat install)."
        if self.exe is None and not ollama_runtimes():
            return "llama-server is not installed (run: jarvis chat install)."
        return None


class ChatServer(LlamaServer):
    owner = OWNER
    gpu_name = GPU_NAME
    label = "the chat model server"
    log_name = "chat-model.log"
    health_timeout_s = 180.0

    def __init__(self, settings: LocalModelSettings, root: Path, processes: ProcessRegistry, logs_dir: Path,
                 *, runtimes: tuple[Path, ...] = ()) -> None:
        super().__init__(processes, logs_dir)
        self.settings = settings
        self.files = ChatFiles(root, *runtimes)
        self._scanned = time.monotonic()

    def problem(self) -> str | None:
        problem = self.files.problem()
        if problem is not None and time.monotonic() - self._scanned >= RESCAN_S:
            self.files = ChatFiles(self.files.root, *self.files.runtimes)
            self._scanned = time.monotonic()
            problem = self.files.problem()
        return problem

    @property
    def exe(self) -> Path | None:
        return self.files.exe

    def wanted_port(self) -> int:
        return self.settings.port

    def command(self) -> list[str]:
        """The llama-server command line, for the port and key of this start. Options a build may not have are
        passed only when its --help lists them (`flags`)."""
        exe = self.runtime.exe if self.runtime is not None else self.files.exe
        assert exe and self.files.model
        s = self.settings
        args = [str(exe), "-m", str(self.files.model), "--host", "127.0.0.1", "--port", str(self._port),
                "--api-key", self._api_key, "-c", str(s.context), "-fa", "on", "--jinja",
                "--parallel", str(max(1, s.parallel)), *SAMPLING]
        if s.gpu_layers >= 0:
            args += ["-ngl", str(s.gpu_layers)]
        elif "--fit" not in self.flags:        # a build that cannot fit the layers itself: all on the card
            args += ["-ngl", "999"]
        if "--kv-unified" in self.flags:        # one request may use the whole context, not its share of it
            args.append("--kv-unified")
        if "--reasoning" in self.flags:        # newer builds (Ollama's): the template switch is deprecated there
            args += ["--reasoning", "on" if s.thinking else "off"]
        elif "--chat-template-kwargs" in self.flags:
            args += ["--chat-template-kwargs", json.dumps({"enable_thinking": bool(s.thinking)})]
        if "--no-webui" in self.flags:
            args.append("--no-webui")
        return args

    async def stop_if_idle(self) -> bool:
        if self.settings.idle_minutes <= 0:          # stays loaded while JARVIS runs
            return False
        return await self.stop_after(self.settings.idle_minutes * 60)


__all__ = ["ChatFiles", "ChatServer", "attach", "chat_root", "runtime_roots"]
