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
from jarvis.common import llama
from jarvis.common.errors import ProviderUnavailable
from jarvis.common.gpu import GpuTurns
from jarvis.common.llama import (
    BLOCKED_TEXT,
    EXE_NAME,
    Probe,
    ProbeCache,
    Runtime,
    blocked_by_windows,
    ollama_runtimes,
)
from jarvis.config.settings import AISettings, LocalModelSettings, Settings
from jarvis.security.process import ProcessRegistry

FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_llama_server.py"
GGUF = "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"
FLAGS = "--fit --kv-unified --chat-template-kwargs --no-webui --parallel --list-devices"

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


def fake_exe(path: Path, body: str | None = None) -> Path:
    """An executable llama-server: the stand-in server, or a script with `body` (one that cannot run)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\n{body}\n' if body is not None else f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n',
                    encoding="utf-8")
    path.chmod(0o755)
    return path


def ollama_folder(root: Path, *backends: str) -> Path:
    """Ollama's install folder: lib/ollama/llama-server and a folder per GPU backend (cuda_v13/ggml-cuda.dll, ...)."""
    lib = root / "lib" / "ollama"
    fake_exe(lib / EXE_NAME)
    for backend in backends:
        (lib / backend).mkdir(parents=True)
        dll = "ggml-vulkan.dll" if backend == "vulkan" else "ggml-hip.dll" if backend.startswith("rocm") \
            else "ggml-cuda.dll"
        (lib / backend / dll).write_bytes(b"\x00")
        (lib / backend / "cudart64_13.dll").write_bytes(b"\x00")
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

    def test_a_newer_build_turns_thinking_off_its_own_way(self, tmp_path: Path) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        server._port, server._api_key = 5555, "k"
        server.flags = {"--fit", "--reasoning", "--chat-template-kwargs"}    # the template switch is deprecated there
        args = server.command()
        assert args[args.index("--reasoning") + 1] == "off" and "--chat-template-kwargs" not in args
        server.settings.thinking = True
        assert server.command()[server.command().index("--reasoning") + 1] == "on"

    def test_an_older_build_gets_only_what_it_knows(self, tmp_path: Path) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path, gpu_layers=-1)
        server._port, server._api_key = 5555, "k"
        args = server.command()                                     # its --help listed none of the newer options
        assert args[args.index("-ngl") + 1] == "999"
        assert not {"--kv-unified", "--no-webui", "--chat-template-kwargs"} & set(args)
        server.settings.gpu_layers = 20
        assert server.command()[server.command().index("-ngl") + 1] == "20"


# =========================================================================== which program runs it
class TestRuntime:
    """Windows' Smart App Control refuses unsigned programs; the downloaded llama.cpp is one. Ollama ships a signed
    llama-server, which runs the same model file."""

    def test_windows_refusing_a_program_is_recognised(self) -> None:
        assert blocked_by_windows(0xC0E90002) and blocked_by_windows(-1058471934)       # unsigned and signed views
        assert blocked_by_windows(0xC0000428) and not blocked_by_windows(1) and not blocked_by_windows(0)
        refused = OSError("An Application Control policy has blocked this file")
        refused.winerror = 4551                                                         # type: ignore[attr-defined]
        assert blocked_by_windows(error=refused) and not blocked_by_windows(error=OSError("missing"))

    def test_ollamas_copy_is_found_with_each_graphics_backend(self, tmp_path: Path) -> None:
        root = ollama_folder(tmp_path / "Ollama", "cuda_v12", "cuda_v13", "vulkan", "rocm_v7_1")
        found = ollama_runtimes([tmp_path / "nothing", root])
        lib = root / "lib" / "ollama"
        assert [r.exe for r in found] == [lib / EXE_NAME] * 5 and {r.kind for r in found} == {"ollama"}
        assert [r.path_dirs for r in found] == [(lib / "cuda_v13",), (lib / "cuda_v12",), (lib / "rocm_v7_1",),
                                                (lib / "vulkan",), ()]            # newest CUDA first, processor last
        assert dict(found[0].env) == {"GGML_BACKEND_PATH": str(lib / "cuda_v13" / "ggml-cuda.dll")}
        env = found[0].environ()
        assert env["PATH"].split(os.pathsep)[0] == str(lib / "cuda_v13") and env["GGML_BACKEND_PATH"]
        assert "signed" in found[0].describe() and "cuda_v13" in found[0].describe()
        assert ollama_runtimes([tmp_path / "nothing"]) == []

    @needs_posix
    def test_probing_reads_the_options_and_the_devices(self, tmp_path: Path, fake_env: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
        exe = fake_exe(tmp_path / "b1" / EXE_NAME)
        cpu = llama.probe(Runtime(exe))
        assert cpu.ok and not cpu.gpu and "--fit" in cpu.flags and cpu.devices == []
        monkeypatch.setenv("FAKE_LLAMA_GPU", "1")
        gpu = llama.probe(Runtime(exe))
        assert gpu.ok and gpu.gpu and gpu.devices == ["CUDA0: Fake GPU (16000 MiB, 15000 MiB free)"]
        broken = llama.probe(Runtime(fake_exe(tmp_path / "b2" / EXE_NAME, "exit 3")))
        assert not broken.ok and "exit code 0x3" in broken.error
        missing = llama.probe(Runtime(tmp_path / "gone" / EXE_NAME))
        assert not missing.ok and not missing.blocked

    async def test_a_blocked_build_gives_way_to_the_signed_copy(self, tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        own = Runtime(server.exe)                                                      # type: ignore[arg-type]
        cuda = Runtime(tmp_path / "ollama" / EXE_NAME, "ollama", (tmp_path / "cuda_v13",),
                       (("GGML_BACKEND_PATH", str(tmp_path / "cuda_v13" / "ggml-cuda.dll")),))
        cpu = Runtime(tmp_path / "ollama" / EXE_NAME, "ollama")
        server.fallbacks = lambda: [cuda, cpu]
        seen: list[Runtime] = []

        def fake_probe(runtime: Runtime) -> Probe:
            seen.append(runtime)
            if runtime == own:
                return Probe(runtime, blocked=True, error="blocked by Windows (Smart App Control)")
            return Probe(runtime, {"--fit", "--jinja"}, ["CUDA0: GPU"] if runtime == cuda else [])

        monkeypatch.setattr(llama, "probe", fake_probe)
        assert await server.pick_runtime() == cuda and server.flags == {"--fit", "--jinja"}
        assert seen == [own, cuda]                                    # the first that runs on the card wins
        server._port, server._api_key = 5555, "k"
        assert server.command()[0] == str(cuda.exe)
        assert await server.pick_runtime() == cuda and len(seen) == 2  # probed once

    async def test_what_was_found_is_remembered_between_starts(self, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[Runtime] = []

        def fake_probe(runtime: Runtime) -> Probe:
            calls.append(runtime)
            return Probe(runtime, {"--fit"}, ["CUDA0: GPU"])

        monkeypatch.setattr(llama, "probe", fake_probe)
        root = model_folder(tmp_path / "chat")

        def fresh() -> ChatServer:                               # a new start of JARVIS
            server = server_for(root, tmp_path)
            server.fallbacks = lambda: []
            return server

        found = await fresh().pick_runtime()
        assert await fresh().pick_runtime() == found and len(calls) == 1      # read back, not probed again
        exe = root / "llama.cpp" / "b1" / EXE_NAME
        os.utime(exe, (exe.stat().st_atime, exe.stat().st_mtime + 10))       # an updated program is probed again
        await fresh().pick_runtime()
        assert len(calls) == 2
        cache = ProbeCache(tmp_path / "probes.json")
        cache.put(Probe(Runtime(exe), error="it did not start"))             # perhaps passing: not remembered
        assert cache.get(Runtime(exe)) is None
        cache.put(Probe(Runtime(exe), blocked=True, error="blocked"))         # Windows' refusal is
        assert (remembered := cache.get(Runtime(exe))) is not None and remembered.blocked
        assert ProbeCache(None).get(Runtime(exe)) is None

    async def test_nothing_that_runs_says_why(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        server.fallbacks = lambda: []
        monkeypatch.setattr(llama, "probe", lambda r: Probe(r, blocked=True, error="blocked"))
        with pytest.raises(RuntimeError) as blocked:
            await server.pick_runtime()
        assert str(blocked.value) == BLOCKED_TEXT and "Ollama" in BLOCKED_TEXT
        other = server_for(model_folder(tmp_path / "chat2"), tmp_path)
        other.fallbacks = lambda: []
        monkeypatch.setattr(llama, "probe", lambda r: Probe(r, error="it did not start (exit code 0x1)"))
        with pytest.raises(RuntimeError, match=r"cannot run .*exit code 0x1"):
            await other.pick_runtime()

    async def test_a_file_refused_while_loading_moves_on_to_the_next_program(self, tmp_path: Path) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        own = Runtime(server.exe)                                                      # type: ignore[arg-type]
        server.runtime = own
        server._probes[own] = Probe(own, {"--fit"})

        class Ended:
            async def wait(self) -> int:
                return 0xC0E90002                                     # a DLL it loads was refused

        server._proc = Ended()                                         # type: ignore[assignment]
        text = await server._died_text()
        assert "Windows blocked" in text and "Smart App Control" in text
        assert server.runtime is None and server._probes[own].blocked   # the next start picks another program

    async def test_the_chat_says_what_blocks_it_in_hebrew(self, tmp_path: Path) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        local = LocalModelProvider(server.settings, server=server)

        async def refused() -> None:
            raise RuntimeError(BLOCKED_TEXT)

        server.ensure_running = refused                               # type: ignore[method-assign]
        with pytest.raises(ProviderUnavailable) as failed:
            await local.complete(request())
        assert failed.value.details["code"] == "local_blocked"
        await local.aclose()


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

    async def test_a_reply_can_be_read_while_it_is_written(self, tmp_path: Path, fake_env: Path) -> None:
        server = server_for(model_folder(tmp_path / "chat"), tmp_path)
        local = LocalModelProvider(server.settings, server=server)
        schema = {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
                  "required": ["steps"]}
        seen: list[str] = []
        try:
            response = await local.complete(request("ספר לי משהו", on_text=seen.append))
            assert response.text == "שלום! ספר לי משהו" and response.stop_reason == "end"
            assert response.usage.output_tokens == 5 and response.provider == "local"
            assert len(seen) >= 3 and seen[-1] == response.text and all(response.text.startswith(t) for t in seen)
            seen.clear()
            planned = await local.complete(request("תכנן", json_schema=schema, on_text=seen.append))
            assert planned.parse_json() == {"steps": ["a", "b"]} and seen[-1] == planned.text and len(seen) > 1
        finally:
            await server.stop()
            await local.aclose()

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

    async def test_ollamas_copy_serves_when_the_own_build_cannot_run(self, tmp_path: Path, fake_env: Path) -> None:
        root = model_folder(tmp_path / "chat", exe=False)
        fake_exe(root / "llama.cpp" / "b1" / EXE_NAME, "exit 1")          # refused by Windows, as on the PC
        ollama = ollama_folder(tmp_path / "Ollama", "cuda_v13")
        server = server_for(root, tmp_path)
        server.fallbacks = lambda: ollama_runtimes([ollama])
        local = LocalModelProvider(server.settings, server=server)
        try:
            assert (await local.complete(request("שלום"))).text == "שלום! שלום"
            assert server.runtime is not None and server.runtime.exe == ollama / "lib" / "ollama" / EXE_NAME
            lines = [json.loads(line) for line in fake_env.read_text(encoding="utf-8").splitlines()]
            assert "--kv-unified" in lines[0] and "-ngl" not in lines[0]          # its own options, read from it
            assert lines[1] == {"backend": str(ollama / "lib" / "ollama" / "cuda_v13" / "ggml-cuda.dll")}
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
