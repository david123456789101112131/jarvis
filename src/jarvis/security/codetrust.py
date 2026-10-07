"""Code JARVIS generated from its own templates may run without asking, as long as nobody changed it.

Running a project's code (its tests, its server, its script) is the same as running any program:
an agent that was talked into writing a malicious test could use it to do anything. So by
default it needs the user's approval. The exception is a project JARVIS just generated from a
built-in template: its files are known. `CodeTrust` records a fingerprint of every file in such a
project; while the fingerprint still matches, its code runs without asking. One changed, added
or removed file (by an agent, the AI, anyone) and the project is treated like any other code.

Only folders the agents' file tools can never write into are left out of the fingerprint
(see `path_guard.managed_folder`): the ones package managers, git and the test tools own
(node_modules, .git, __pycache__, .pytest_cache and other caches) and real virtual environments
(a folder with a `pyvenv.cfg`, a file the agents may not create). Only approved installs and the
code itself write there. Every other folder counts, `dist`, `build` and `logs` included: test
runners such as `node --test` would run a test file planted in any of them. Also left out are
log files and the JSON files in the project's `data` folder, which the generated servers and bots
save their records to: no test runner or interpreter runs a .json or .log file, and the generated
code only parses them.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterable
from pathlib import Path

from jarvis.security.path_guard import _key, is_within, normalize_path

SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
# Lock files only pin what an approved install chose; they are rewritten by every install.
SKIP_FILES = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml", ".ds_store", "thumbs.db", "desktop.ini"})
MAX_FILES = 3000
MAX_BYTES = 50_000_000


def _venv(folder: Path) -> bool:
    return (folder / "pyvenv.cfg").is_file()


def counted(root: Path, path: Path) -> bool:
    """Whether `path` (inside `root`) is part of the project's fingerprint."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    name = parts[-1].lower() if parts else ""
    if name in SKIP_FILES or name.endswith((".pyc", ".log")):
        return False
    if any(part.lower() in SKIP_DIRS for part in parts[:-1]):
        return False
    if any(_venv(root.joinpath(*parts[:i])) for i in range(1, len(parts))):
        return False
    return not (len(parts) > 1 and parts[0].lower() == "data" and name.endswith(".json"))


def project_files(root: Path) -> list[Path] | None:
    """Every file of the project (sorted), or None when it is too big to fingerprint."""
    out: list[Path] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS and not _venv(Path(dirpath) / d))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if not counted(root, path):
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
            out.append(path)
            if len(out) > MAX_FILES or total > MAX_BYTES:
                return None
    return out


def fingerprint(root: Path) -> str | None:
    files = project_files(root)
    if files is None:
        return None
    digest = hashlib.sha256()
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        digest.update(path.relative_to(root).as_posix().encode("utf-8", "surrogatepass") + b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


class CodeTrust:
    def __init__(self) -> None:
        self._roots: dict[str, tuple[Path, str, frozenset[str]]] = {}
        self._lock = threading.Lock()

    def trust(self, root: Path, written: Iterable[str | Path], commands: Iterable[str] = ()) -> bool:
        """Trust a freshly generated project: only when its files are exactly the ones just written."""
        root = normalize_path(root)
        self.forget(root)
        files = project_files(root)
        expected = {_key(p) for p in (normalize_path(w) for w in written) if counted(root, p)}
        if files is None or {_key(normalize_path(p)) for p in files} != expected:
            return False
        fp = fingerprint(root)
        if fp is None:
            return False
        with self._lock:
            self._roots[_key(root)] = (root, fp, frozenset(c.strip() for c in commands if c.strip()))
        return True

    def forget(self, root: Path) -> None:
        with self._lock:
            self._roots.pop(_key(normalize_path(root)), None)

    def trusted_root(self, path: str | Path) -> Path | None:
        """The trusted project that contains `path`, if its files are still exactly as generated."""
        target = normalize_path(path)
        with self._lock:
            candidates = [(r, fp) for r, fp, _ in self._roots.values() if is_within(target, r)]
        for root, fp in candidates:
            if fingerprint(root) == fp:
                return root
            self.forget(root)          # changed once: never trusted again
        return None

    def allows_command(self, cwd: str | Path, command: str) -> bool:
        """True for the generated project's own start command, run in that (unchanged) project."""
        root = self.trusted_root(cwd)
        if root is None or _key(normalize_path(cwd)) != _key(root):
            return False
        with self._lock:
            entry = self._roots.get(_key(root))
        return entry is not None and command.strip() in entry[2]


__all__ = ["CodeTrust", "counted", "fingerprint", "project_files"]
