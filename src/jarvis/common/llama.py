"""One llama.cpp `llama-server` process that JARVIS owns: started when first needed, healthy before it is used,
stopped again after a spell of not being used.

The server listens on 127.0.0.1 only, with a random API key, so nothing on the network, or another program on the
machine, can drive it. Computer control (the Holo vision model, `jarvis.computer.server`) and chat (the model JARVIS
talks and plans with, `jarvis.ai.local_server`) each own one; they take turns on the graphics card (`GpuTurns`).

Which program runs is picked before the first start (`Runtime`, `probe`): the llama.cpp build JARVIS installed,
else the copy of llama-server that ships inside Ollama. That copy is signed (Ollama Inc.), so it still runs where
Windows' Smart App Control blocks unsigned programs, and the downloaded llama.cpp build is not signed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import httpx

from jarvis.common.gpu import GpuTurns, busy_text
from jarvis.security.process import ProcessRegistry, kill_tree
from jarvis.security.secrets import sanitized_env

log = logging.getLogger(__name__)

EXE_NAME = "llama-server.exe" if sys.platform == "win32" else "llama-server"
GPU_BACKEND = re.compile(r"^(?:lib)?ggml-(cuda|hip|rocm|vulkan)\.(?:dll|so)$", re.IGNORECASE)
_CREATE_NO_WINDOW = 0x08000000
# Windows refuses a program, or a DLL it loads, that Smart App Control (or another code-integrity policy) does not
# trust: starting it fails with ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION and its neighbours (4550-4559), or the
# process ends at once with STATUS_SYSTEM_INTEGRITY_POLICY_VIOLATION (0xC0E90002) or STATUS_INVALID_IMAGE_HASH.
BLOCKED_WINERRORS = frozenset(range(4550, 4560))
BLOCKED_EXITS = frozenset({0xC0E90002, 0xC0000428})
BLOCKED_TEXT = ("Windows' Smart App Control blocks llama.cpp, the program that runs the model on this computer "
                "(it is not signed), and no signed copy was found. Installing Ollama (ollama.com) fixes it: JARVIS "
                "then uses the signed llama-server inside it, with no Windows setting changed.")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def find_llama_server(*roots: Path) -> Path | None:
    """The `llama-server` executable anywhere under `<root>/llama.cpp/` (or the root itself), first root first."""
    for root in roots:
        for base in (root / "llama.cpp", root):
            if base.is_dir():
                for found in sorted(base.rglob(EXE_NAME)):
                    return found
    return None


def find_gguf(root: Path, *must_contain: str, without: tuple[str, ...] = ()) -> Path | None:
    """The first .gguf under `<root>/gguf/` (or the root) whose name has every piece of `must_contain` and none of
    `without` (case does not matter)."""
    for base in (root / "gguf", root):
        if not base.is_dir():
            continue
        for found in sorted(base.glob("*.gguf")):
            name = found.name.lower()
            if all(p.lower() in name for p in must_contain) and not any(p.lower() in name for p in without):
                return found
    return None


def blocked_by_windows(code: int | None = None, error: OSError | None = None) -> bool:
    """Did Windows' code-integrity check (Smart App Control) refuse the program: an exit code, or a start error?"""
    if error is not None:
        return getattr(error, "winerror", None) in BLOCKED_WINERRORS
    return code is not None and (code & 0xFFFFFFFF) in BLOCKED_EXITS


@dataclass(frozen=True)
class Runtime:
    """A llama-server program to start, with what its environment needs: Ollama's copy finds its GPU backend only
    when told where it is (GGML_BACKEND_PATH, and that backend's CUDA libraries first on PATH)."""

    exe: Path
    kind: str = "own"                                 # "own": the llama.cpp JARVIS installed; "ollama": Ollama's copy
    path_dirs: tuple[Path, ...] = ()
    env: tuple[tuple[str, str], ...] = ()

    def environ(self) -> dict[str, str]:
        extra = dict(self.env)
        if self.path_dirs:
            extra["PATH"] = os.pathsep.join([*(str(d) for d in self.path_dirs), os.environ.get("PATH", "")])
        return sanitized_env(extra)

    def describe(self) -> str:
        backend = next((Path(v).parent.name for k, v in self.env if k == "GGML_BACKEND_PATH"), "")
        if self.kind == "ollama":
            return f"Ollama's signed llama-server ({backend or 'processor only'})"
        return f"llama.cpp {self.exe.parent.name}"


@dataclass
class Probe:
    """What running a llama-server's `--help` and `--list-devices` showed."""

    runtime: Runtime
    flags: set[str] = field(default_factory=set)
    devices: list[str] = field(default_factory=list)      # "CUDA0: NVIDIA GeForce RTX 5060 Ti (16283 MiB, ...)"
    blocked: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.blocked and not self.error

    @property
    def gpu(self) -> bool:
        return any(not d.upper().startswith("CPU") for d in self.devices)


def _run(runtime: Runtime, *args: str, timeout: float = 30.0) -> tuple[int | None, str, OSError | None]:
    kwargs: dict[str, Any] = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        done = subprocess.run([str(runtime.exe), *args], capture_output=True, timeout=timeout, env=runtime.environ(),
                              cwd=str(runtime.exe.parent), stdin=subprocess.DEVNULL, check=False, **kwargs)
    except OSError as exc:
        return None, "", exc
    except subprocess.SubprocessError as exc:
        return None, "", OSError(f"{runtime.exe.name} {' '.join(args)}: {exc}")
    text = (done.stdout or b"").decode("utf-8", "replace") + (done.stderr or b"").decode("utf-8", "replace")
    return done.returncode, text, None


def probe(runtime: Runtime) -> Probe:
    """Does this llama-server run here, which options does it know (so a newer one is passed only where it exists)
    and which devices does it see? Blocking: a few seconds (`--list-devices` loads the GPU backend)."""
    code, text, error = _run(runtime, "--help")
    if blocked_by_windows(code, error):
        return Probe(runtime, blocked=True, error="blocked by Windows (Smart App Control)")
    if error is not None:
        return Probe(runtime, error=str(error) or type(error).__name__)
    flags = set(re.findall(r"--[a-z][a-z0-9-]*", text))
    if code != 0 and not flags:
        return Probe(runtime, error=f"it did not start (exit code {(code or 0) & 0xFFFFFFFF:#x})")
    found = Probe(runtime, flags)
    if "--list-devices" in flags:
        code, text, error = _run(runtime, "--list-devices")
        if blocked_by_windows(code, error):          # the GPU backend's DLL was refused
            return Probe(runtime, blocked=True, error="blocked by Windows (Smart App Control)")
        found.devices = [m.group(1).strip() for m in re.finditer(r"^\s*([A-Za-z]+\d+:\s.+)$", text, re.MULTILINE)]
    return found


PROBE_TTL_S = 7 * 24 * 3600         # a remembered probe is trusted this long, while the program file is unchanged


class ProbeCache:
    """Probes remembered between runs, in a small JSON file: probing again costs seconds at every start (Windows
    checks an unsigned program each time, and listing the devices loads the GPU backend). A program that changed
    (another size or date) is probed again; so is everything after `PROBE_TTL_S`. A failure that is not Windows'
    refusal is not remembered (it may be passing)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    @staticmethod
    def key(runtime: Runtime) -> str:
        try:
            st = runtime.exe.stat()
            stamp = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            stamp = "missing"
        return json.dumps([str(runtime.exe), stamp, [str(d) for d in runtime.path_dirs], [list(e) for e in runtime.env]])

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, runtime: Runtime) -> Probe | None:
        entry = self._load().get(self.key(runtime))
        if not isinstance(entry, dict) or time.time() - float(entry.get("at", 0)) > PROBE_TTL_S:
            return None
        return Probe(runtime, set(entry.get("flags") or ()), list(entry.get("devices") or ()),
                     blocked=bool(entry.get("blocked")), error=str(entry.get("error") or ""))

    def put(self, found: Probe) -> None:
        if self.path is None or (found.error and not found.blocked):
            return
        data = {k: v for k, v in self._load().items()
                if isinstance(v, dict) and time.time() - float(v.get("at", 0)) <= PROBE_TTL_S}
        data[self.key(found.runtime)] = {"at": time.time(), "flags": sorted(found.flags), "devices": found.devices,
                                         "blocked": found.blocked, "error": found.error}
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


def help_flags(exe: Path) -> set[str]:
    """The options this llama-server build knows (from its --help). Empty when it cannot run."""
    return probe(Runtime(exe)).flags


def ollama_roots() -> list[Path]:
    """Where Ollama is installed on this computer (Windows): its per-user and all-users folders, and `ollama` on PATH."""
    if sys.platform != "win32":
        return []
    roots = [Path(base) / sub for var, sub in (("LOCALAPPDATA", "Programs/Ollama"), ("ProgramFiles", "Ollama"))
             if (base := os.environ.get(var))]
    if which := shutil.which("ollama"):
        roots.append(Path(which).parent)
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _version(folder: Path) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", folder.name)) or (0,)


def ollama_runtimes(roots: Iterable[Path] | None = None) -> list[Runtime]:
    """The llama-server that ships inside Ollama (`lib/ollama`), once for each GPU backend it has (the newest CUDA
    first, then ROCm and Vulkan) and once for the processor alone. JARVIS starts it with its own model file and
    options; Ollama itself, its service and its models are left alone."""
    found: list[Runtime] = []
    for root in ollama_roots() if roots is None else roots:
        lib = root / "lib" / "ollama"
        exe = lib / EXE_NAME
        if not exe.is_file():
            continue
        backends = []
        for folder in (d for d in lib.iterdir() if d.is_dir()):
            dll = next((f for f in sorted(folder.iterdir()) if GPU_BACKEND.match(f.name)), None)
            if dll is not None:
                cuda = "cuda" in dll.name.lower()
                backends.append(((cuda, _version(folder)), folder, dll))
        for _, folder, dll in sorted(backends, key=lambda b: b[0], reverse=True):
            found.append(Runtime(exe, "ollama", (folder,), (("GGML_BACKEND_PATH", str(dll)),)))
        found.append(Runtime(exe, "ollama"))
    return found


def alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return True


class LlamaServer:
    """Owns one `llama-server` process. Subclasses say what is missing (`problem`) and how to start it (`command`)."""

    owner = "llama-server"            # its name in the process registry (the kill switch stops it)
    gpu_name = "llama"                # its name in GpuTurns
    label = "the local model server"  # in messages
    log_name = "llama-server.log"
    health_timeout_s = 240.0          # loading ~10 GB onto the graphics card takes a while the first time

    def __init__(self, processes: ProcessRegistry, logs_dir: Path) -> None:
        self.processes = processes
        self.logs_dir = logs_dir
        self._pid: int | None = None
        self._port = 0
        self._api_key = ""
        self._log: IO[bytes] | None = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()
        self.gpu: GpuTurns | None = None          # set by the app: the other local models let go of the card first
        self.flags: set[str] = set()              # what the chosen program's --help lists (read before the first start)
        self.runtime: Runtime | None = None       # the program that runs (or ran last), picked by `pick_runtime`
        self.fallbacks: Callable[[], list[Runtime]] = ollama_runtimes
        self.probe_cache = ProbeCache(logs_dir / "llama-runtimes.json")
        self._probes: dict[Runtime, Probe] = {}
        self._proc: asyncio.subprocess.Process | None = None

    # -- what subclasses fill in
    def problem(self) -> str | None:
        return None

    def command(self) -> list[str]:
        raise NotImplementedError

    def wanted_port(self) -> int:
        return 0

    @property
    def exe(self) -> Path | None:
        return None

    def runtimes(self) -> list[Runtime]:
        """The programs to try, best first: the llama.cpp JARVIS installed (`exe`), then Ollama's signed copy."""
        return ([Runtime(self.exe)] if self.exe is not None else []) + self.fallbacks()

    def can_run(self) -> bool:
        """Is there a program to start (without starting anything)?"""
        return self.exe is not None or bool(self.fallbacks())

    async def pick_runtime(self) -> Runtime:
        """The first program that runs here and sees a graphics card, else the first that runs at all. Each one is
        tried once (`probe`); a program Windows refused is skipped. Raises RuntimeError when none runs."""
        candidates = self.runtimes()
        if self.runtime in candidates and self.runtime in self._probes:
            return self.runtime
        probes: list[Probe] = []
        for runtime in candidates:
            found = self._probes.get(runtime) or await asyncio.to_thread(self.probe_cache.get, runtime)
            if found is None:
                found = await asyncio.to_thread(probe, runtime)
                await asyncio.to_thread(self.probe_cache.put, found)
            self._probes[runtime] = found
            probes.append(found)
            if found.ok and found.gpu:
                break
        chosen = next((p for p in probes if p.ok and p.gpu), None) or next((p for p in probes if p.ok), None)
        if chosen is None:
            if any(p.blocked for p in probes):
                raise RuntimeError(BLOCKED_TEXT)
            why = "; ".join(f"{p.runtime.describe()}: {p.error}" for p in probes) or "llama-server is not installed"
            raise RuntimeError(f"{self.label} cannot run ({why}).")
        for p in probes:
            if p.blocked:
                log.warning("Windows blocks %s (Smart App Control); using %s instead", p.runtime.exe,
                            chosen.runtime.describe())
        self.runtime, self.flags = chosen.runtime, chosen.flags
        log.info("%s: %s on %s", self.label, chosen.runtime.describe(),
                 ", ".join(chosen.devices) if chosen.devices else "the processor")
        return chosen.runtime

    # -- state
    @property
    def running(self) -> bool:
        return self._pid is not None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def api_key(self) -> str:
        return self._api_key

    def touch(self) -> None:
        self._last_used = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_used if self.running else 0.0

    # -- starting and stopping
    async def ensure_running(self) -> None:
        """Start the server if it is not up yet; several callers may await this at once."""
        async with self._lock:
            self.touch()
            if self.running and self._pid is not None and alive(self._pid):
                return
            if self.running:                       # it stopped by itself (out of memory, closed from outside)
                await self.stop()
            problem = self.problem()
            if problem is not None:
                raise RuntimeError(problem)
            if self.gpu is not None:
                busy = await self.gpu.take(self.gpu_name)
                if busy is not None:
                    raise RuntimeError(busy_text(busy))
            await self._start()

    async def _start(self) -> None:
        runtime = await self.pick_runtime()
        self._port = self.wanted_port() or free_port()
        self._api_key = secrets.token_hex(24)
        args = self.command()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._log = (self.logs_dir / self.log_name).open("ab")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        log.info("Starting %s on 127.0.0.1:%d", self.label, self._port)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(runtime.exe.parent), env=runtime.environ(),
                stdin=asyncio.subprocess.DEVNULL, stdout=self._log, stderr=self._log, **kwargs,
            )
        except OSError as exc:
            await self.stop()
            if blocked_by_windows(error=exc):
                self._forget(runtime)
                raise RuntimeError(f"Windows blocked {runtime.describe()} (Smart App Control).") from exc
            raise RuntimeError(f"{self.label} could not start: {exc}") from exc
        self._pid, self._proc = proc.pid, proc
        self.processes.register(proc.pid, owner=self.owner)
        try:
            await self._wait_healthy()
        except BaseException:
            await self.stop()
            raise
        self.touch()

    async def _wait_healthy(self) -> None:
        deadline = time.monotonic() + self.health_timeout_s
        async with httpx.AsyncClient(timeout=5.0, trust_env=False,
                                     headers={"Authorization": f"Bearer {self._api_key}"}) as client:
            while time.monotonic() < deadline:
                if self._pid is not None and not alive(self._pid):
                    raise RuntimeError(await self._died_text())
                with contextlib.suppress(httpx.HTTPError):
                    resp = await client.get(f"{self.base_url}/health")
                    if resp.status_code == 200:
                        return
                await asyncio.sleep(1.0)
        raise RuntimeError(f"{self.label} did not become ready in time.")

    async def _died_text(self) -> str:
        """Why the server ended while starting: Windows refused one of its files, or see its log."""
        code = None
        if self._proc is not None:
            with contextlib.suppress(Exception):
                code = await asyncio.wait_for(self._proc.wait(), 5.0)
        if blocked_by_windows(code) and self.runtime is not None:
            runtime = self.runtime
            self._forget(runtime)                    # the next start tries the next program
            return f"Windows blocked a file of {runtime.describe()} (Smart App Control)."
        exit_note = f", exit code {code & 0xFFFFFFFF:#x}" if code else ""
        return f"{self.label} stopped while starting (see its log, {self.log_name}{exit_note})."

    def _forget(self, runtime: Runtime) -> None:
        refused = self._probes[runtime] = Probe(runtime, blocked=True, error="blocked by Windows (Smart App Control)")
        self.probe_cache.put(refused)
        if self.runtime == runtime:
            self.runtime = None

    async def stop(self) -> None:
        pid, self._pid = self._pid, None
        self._proc = None
        if pid is not None:
            await asyncio.to_thread(kill_tree, pid)
            self.processes.unregister(pid)
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log.close()
            self._log = None
        self._port, self._api_key = 0, ""

    async def stop_after(self, idle_s: float) -> bool:
        """Stop the server once it has gone `idle_s` seconds unused. True = it was stopped."""
        if self.running and self.idle_seconds() >= idle_s:
            log.info("Stopping %s: unused for %.0f minutes", self.label, idle_s / 60)
            await self.stop()
            return True
        return False


__all__ = ["BLOCKED_TEXT", "EXE_NAME", "LlamaServer", "Probe", "ProbeCache", "Runtime", "alive", "blocked_by_windows", "find_gguf",
           "find_llama_server", "free_port", "help_flags", "ollama_roots", "ollama_runtimes", "probe"]
