"""The chat model on this computer: JARVIS's own llama-server (Gemma 4 12B), the local provider that uses it, its
turn on the graphics card and its installer.

The server is `tests/fixtures/fake_llama_server.py` behind an executable named llama-server: a real process on a
real port, with the same command line, API key and OpenAI-compatible answers. Nothing is downloaded and no model
is loaded; the real model is checked on the PC.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.ai import local_install
from jarvis.ai.local_server import ChatFiles, ChatServer, attach, chat_root
from jarvis.ai.openai_provider import LocalModelProvider
from jarvis.ai.router import ProviderRouter
from jarvis.ai.types import AIMessage, CompletionRequest, ToolSpec
from jarvis.common.errors import ProviderUnavailable
from jarvis.common.gpu import GpuTurns
from jarvis.common.llama import EXE_NAME
from jarvis.config.settings import AISettings, LocalModelSettings, Settings
from jarvis.security.process import ProcessRegistry

FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_llama_server.py"
GGUF = "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"
FLAGS = "--fit --kv-unified --chat-template-kwargs --no-webui --parallel"

needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="the stand-in server is a shell script")


def model_folder(root: Path, *, exe: bool = True) -> Path:
    (root / "gguf").mkdir(parents=True, exist_ok=True)
    (root / "gguf" / GGUF).write_bytes(b"\x00")
    if exe:
        (root / "llama.cpp" / "b1").mkdir(parents=True, exist_ok=True)
        script = root / "llama.cpp" / "b1" / EXE_NAME
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
        script.chmod(0o755)
    return root


def server_for(root: Path, tmp: Path, **settings: Any) -> ChatServer:
    return ChatServer(LocalModelSettings(**settings), root, ProcessRegistry(), tmp / "logs")


def request(text: str = "מה שלומך?", **kw: Any) -> CompletionRequest:
    return CompletionRequest(messages=[AIMessage.user(text)], max_tokens=200, role="chat", **kw)


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    log = tmp_path / "starts.jsonl"
    monkeypatch.setenv("FAKE_LLAMA_FLAGS", FLAGS)
    monkeypatch.setenv("FAKE_LLAMA_LOG", str(log))
    return log


# =========================================================================== where the files are
class TestFiles:
    def test_the_model_and_the_runtime_are_found(self, tmp_path: Path) -> None:
        root = model_folder(tmp_path / "chat", exe=False)
        assert "llama-server" in (ChatFiles(root).problem() or "")
        runtime = model_folder(tmp_path / "computer")                 # computer control's llama.cpp is used
        files = ChatFiles(root, runtime)
        assert files.problem() is None and files.model is not None and files.model.name == GGUF
        assert files.exe is not None and runtime in files.exe.parents
        (root / "gguf" / "mmproj-gemma-4-12b-F16.gguf").write_bytes(b"")  # a projector is not the model
        assert ChatFiles(root, runtime).model.name == GGUF              # type: ignore[union-attr]
        assert "not installed" in (ChatFiles(tmp_path / "empty", runtime).problem() or "")

    def test_the_folder_sits_beside_the_other_models(self, tmp_path: Path) -> None:
        s = Settings()
        assert chat_root(s, tmp_path / "models") == tmp_path / "models" / "chat"
        s.computer.models_dir = str(tmp_path / "JARVIS-models")                     # as on the PC
        assert chat_root(s, tmp_path / "models") == tmp_path / "JARVIS-models" / "chat"
        s.ai.local.models_dir = str(tmp_path / "mine")
        assert chat_root(s, tmp_path / "models") == tmp_path / "mine"

    def test_a_model_installed_later_is_noticed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import jarvis.ai.local_server as ls

        server = server_for(tmp_path / "chat", tmp_path)
        assert server.problem() is not None
        model_folder(tmp_path / "chat")
        monkeypatch.setattr(ls, "RESCAN_S", 0.0)
        assert server.problem() is None


# =========================================================================== the command line
class TestCommand:
    def test_local_only_with_a_key_and_the_options_this_build_has(self, tmp_path: Path) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        server._port, server._api_key = 5555, "k" * 48
        server.flags = set(FLAGS.split())
        args = server.command()
        assert args[args.index("--host") + 1] == "127.0.0.1" and args[args.index("--api-key") + 1] == "k" * 48
        assert args[args.index("-c") + 1] == "32768" and args[args.index("--parallel") + 1] == "2"
        assert "-ngl" not in args                                  # the build fits the layers to the free memory
        assert "--kv-unified" in args and "--no-webui" in args and "--jinja" in args
        assert json.loads(args[args.index("--chat-template-kwargs") + 1]) == {"enable_thinking": False}
        assert args[args.index("--temp") + 1] == "1.0" and args[args.index("--top-k") + 1] == "64"

    def test_an_older_build_gets_only_what_it_knows(self, tmp_path: Path) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path, gpu_layers=-1)
        server._port, server._api_key = 5555, "k"
        args = server.command()                                     # its --help listed none of the newer options
        assert args[args.index("-ngl") + 1] == "999"
        assert not {"--kv-unified", "--no-webui", "--chat-template-kwargs"} & set(args)
        server.settings.gpu_layers = 20
        assert server.command()[server.command().index("-ngl") + 1] == "20"


# =========================================================================== the provider and the server
@needs_posix
class TestServing:
    async def test_the_first_request_starts_it_and_it_answers(self, tmp_path: Path, fake_env: Path) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        local = LocalModelProvider(server.settings, server=server)
        try:
            assert local.available() and not server.running
            response = await local.complete(request("מה שלומך?"))
            assert response.text == "שלום! מה שלומך?" and response.provider == "local" and server.running
            assert not local.busy()
            started = [json.loads(line) for line in fake_env.read_text(encoding="utf-8").splitlines()]
            assert len(started) == 1 and "<key>" in started[0] and "--kv-unified" in started[0]
            assert server.processes.list()[0]["owner"] == "chat-model"
            await local.complete(request("ועוד?"))
            assert len(fake_env.read_text(encoding="utf-8").splitlines()) == 1          # still the same server
        finally:
            await server.stop()
            await local.aclose()
        assert not server.running and server.processes.list() == []

    async def test_tools_and_json_replies(self, tmp_path: Path, fake_env: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        local = LocalModelProvider(server.settings, server=server)
        weather = ToolSpec(name="get_weather", description="the weather in a city",
                           input_schema={"type": "object", "properties": {"city": {"type": "string"}},
                                         "required": ["city"]})
        schema = {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
                  "required": ["steps"]}
        try:
            called = await local.complete(request("מה מזג האוויר בירושלים?", tools=[weather]))
            assert called.stop_reason == "tool_use" and called.tool_calls[0].name == "get_weather"
            assert called.tool_calls[0].input == {"city": "ירושלים"}
            planned = await local.complete(request("תכנן", json_schema=schema))
            assert planned.parse_json() == {"steps": ["a", "b"]}                    # the server holds it to the schema
            # A build whose grammar cannot take a schema: JSON mode, with the schema in the prompt, from then on.
            await server.stop()
            monkeypatch.setenv("FAKE_LLAMA_NO_SCHEMA", "1")
            fallback = await local.complete(request("תכנן", json_schema=schema))
            assert fallback.text.startswith("שלום!") and local.json_mode_for("x") == "json_object"
            assert local.errors == 0
        finally:
            await server.stop()
            await local.aclose()

    async def test_it_takes_turns_on_the_graphics_card(self, tmp_path: Path, fake_env: Path) -> None:
        gpu = GpuTurns()
        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        server.gpu = gpu
        local = LocalModelProvider(server.settings, server=server)
        gpu.register("chat", busy=local.busy, release=local.release_gpu)
        media = {"busy": False, "released": 0}

        async def release_media() -> None:
            media["released"] += 1

        gpu.register("media", busy=lambda: media["busy"], release=release_media)
        try:
            await local.complete(request())
            assert media["released"] == 1                           # the idle image model let go of the card
            assert await gpu.take("media") is None and not server.running   # and the chat model lets go for it
            media["busy"] = True                                    # a video is being made
            with pytest.raises(ProviderUnavailable, match="busy making an image or a video"):
                await local.complete(request())
            assert not server.running and not local.busy()
            media["busy"] = False
            assert (await local.complete(request("שוב"))).text == "שלום! שוב"
        finally:
            await server.stop()
            await local.aclose()

    async def test_a_server_that_died_is_started_again(self, tmp_path: Path, fake_env: Path) -> None:
        from jarvis.security.process import kill_tree

        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        local = LocalModelProvider(server.settings, server=server)
        try:
            await local.complete(request())
            assert server._pid is not None
            kill_tree(server._pid)                                  # closed from outside, or out of memory
            assert (await local.complete(request("עדיין שם?"))).text == "שלום! עדיין שם?"
            assert len(fake_env.read_text(encoding="utf-8").splitlines()) == 2
        finally:
            await server.stop()
            await local.aclose()

    async def test_idle_it_stops_and_frees_the_card(self, tmp_path: Path, fake_env: Path) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path, idle_minutes=0.5)
        local = LocalModelProvider(server.settings, server=server)
        try:
            await local.complete(request())
            assert not await server.stop_if_idle() and server.running
            server._last_used -= 31
            assert await server.stop_if_idle() and not server.running
        finally:
            await server.stop()
            await local.aclose()


# =========================================================================== the router
class TestRouting:
    def test_installed_it_answers_everything_for_free(self, tmp_path: Path) -> None:
        settings = Settings()
        settings.ai.local.models_dir = str(model_folder(tmp_path / "chat"))
        router = ProviderRouter.from_settings(settings.ai, None)
        assert router.chain("chat") == [] and router.status()["local"] is False
        server = attach(router, settings, tmp_path / "models", ProcessRegistry(), tmp_path / "logs")
        assert server is not None
        for role in ("chat", "intent", "planner", "agent", "verifier", "synthesis"):
            assert [p.name for p in router.chain(role)] == ["local"]
        assert router.status()["active"] == "local" and router.asks_first("agent") is None
        assert set(router.status()["agents"].values()) == {"local"}

    def test_another_server_is_used_as_it_is(self, tmp_path: Path) -> None:
        settings = Settings(ai=AISettings(local=LocalModelSettings(base_url="http://127.0.0.1:11434/v1")))
        router = ProviderRouter.from_settings(settings.ai, None)
        assert attach(router, settings, tmp_path, ProcessRegistry(), tmp_path) is None
        local = router.providers["local"]
        assert local.available() and local.json_mode_for("m") == "json_object"     # type: ignore[attr-defined]
        assert not LocalModelProvider(LocalModelSettings(enabled=False), server=None).available()

    def test_hebrew_requests_are_asked_for_hebrew_only(self) -> None:
        from jarvis.ai.openai_provider import HEBREW_ONLY

        plan = CompletionRequest(messages=[AIMessage.user("תכנן לי שלושה צעדים לסידור החדר")], system="Plan it.")
        ruled = LocalModelProvider.in_hebrew(plan)
        assert ruled.system == f"Plan it.\n\n{HEBREW_ONLY}" and plan.system == "Plan it."
        assert LocalModelProvider.in_hebrew(ruled).system == ruled.system                 # once
        english = CompletionRequest(messages=[AIMessage.user("plan my week")], system="Plan it.")
        assert LocalModelProvider.in_hebrew(english) is english


# =========================================================================== the installer
class TestInstall:
    def test_the_model_is_downloaded_once_and_checked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        weights = b"GGUF" + os.urandom(2000)
        tree = [{"type": "file", "path": GGUF, "size": len(weights),
                 "lfs": {"oid": f"sha256:{hashlib.sha256(weights).hexdigest()}"}},
                {"type": "file", "path": "README.md", "size": 10}]
        asked: list[str] = []

        def answer(req: httpx.Request) -> httpx.Response:
            asked.append(req.url.path)
            if "/api/models/" in req.url.path:
                assert req.url.path == (f"/api/models/{local_install.HF_REPO}/tree/{local_install.HF_REVISION}")
                return httpx.Response(200, json=tree)
            assert req.url.path.endswith(f"/resolve/{local_install.HF_REVISION}/{GGUF}")
            return httpx.Response(200, content=weights)

        monkeypatch.setattr(local_install.base, "_client",
                            lambda: httpx.Client(transport=httpx.MockTransport(answer)))
        runtime = model_folder(tmp_path / "computer")
        result = local_install.install(tmp_path / "chat", runtimes=(runtime,), progress=lambda _m: None)
        assert result.ok and result.downloaded == [GGUF]               # llama.cpp is computer control's
        assert (tmp_path / "chat" / "gguf" / GGUF).read_bytes() == weights
        asked.clear()
        assert local_install.install(tmp_path / "chat", runtimes=(runtime,), progress=lambda _m: None).ok
        assert len(asked) == 1                                         # only the list: the file checked out whole
        tree[0]["lfs"] = {"oid": "sha256:" + "0" * 64}
        (tmp_path / "chat" / "gguf" / GGUF).unlink()
        with pytest.raises(OSError, match="checksum mismatch"):
            local_install.install(tmp_path / "chat", runtimes=(runtime,), progress=lambda _m: None)
