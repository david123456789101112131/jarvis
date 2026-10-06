"""Folder access control for every filesystem-touching tool.

* Hard-denied locations (system folders, credential stores, browser profiles, JARVIS's own
  data folder with the secrets/config/database) can never be read or written by agents.
* Writes are allowed only inside the configured write roots (workspace, projects and the
  user's common folders by default) or inside folders an explicit grant adds.
* Paths are normalised and resolved (symlinks, `..`, env vars) before any decision, so
  traversal tricks cannot escape a root.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path

from jarvis.common.errors import PermissionDenied

IS_WINDOWS = sys.platform == "win32"


def normalize_path(path: str | os.PathLike[str], base: Path | None = None) -> Path:
    raw = os.path.expandvars(os.path.expanduser(str(path).strip().strip('"')))
    p = Path(raw)
    if not p.is_absolute() and base is not None:
        p = base / p
    try:
        return p.resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(p))


def _key(p: Path) -> str:
    s = str(p)
    return os.path.normcase(s) if IS_WINDOWS else s


def is_within(path: Path, root: Path) -> bool:
    p, r = _key(path), _key(root)
    if p == r:
        return True
    sep = os.sep
    return p.startswith(r if r.endswith(sep) else r + sep)


def default_denied_roots(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    out: list[str] = []
    if IS_WINDOWS:
        env = os.environ
        for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            if env.get(var):
                out.append(env[var])
        if env.get("ProgramData"):
            out.append(os.path.join(env["ProgramData"], "Microsoft"))
        appdata = env.get("APPDATA", str(home / "AppData" / "Roaming"))
        local = env.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        out += [
            os.path.join(appdata, "Microsoft", "Credentials"),
            os.path.join(appdata, "Microsoft", "Protect"),
            os.path.join(appdata, "Microsoft", "Crypto"),
            os.path.join(appdata, "Microsoft", "SystemCertificates"),
            os.path.join(local, "Microsoft", "Credentials"),
            os.path.join(local, "Google", "Chrome", "User Data"),
            os.path.join(local, "Microsoft", "Edge", "User Data"),
            os.path.join(local, "BraveSoftware"),
            os.path.join(appdata, "Mozilla", "Firefox", "Profiles"),
            os.path.join(appdata, "Opera Software"),
            "C:\\System Volume Information",
            "C:\\$Recycle.Bin",
        ]
    else:
        out += ["/etc", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/boot", "/sys", "/proc", "/dev", "/var/lib", "/snap"]
        out += [str(home / ".mozilla"), str(home / ".config" / "google-chrome"), str(home / ".config" / "chromium")]
    out += [
        str(home / ".ssh"),
        str(home / ".aws"),
        str(home / ".azure"),
        str(home / ".gnupg"),
        str(home / ".kube"),
        str(home / ".docker"),
        str(home / ".git-credentials"),
        str(home / ".netrc"),
        str(home / ".config" / "gh"),
        str(home / ".pypirc"),
        str(home / ".npmrc"),
    ]
    return [normalize_path(p) for p in out]


def default_user_write_roots(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]
    roots = [home / n for n in names]
    one_drive = os.environ.get("OneDrive")
    if IS_WINDOWS and one_drive:
        roots += [Path(one_drive) / n for n in ("Desktop", "Documents", "Pictures")]
    return [normalize_path(r) for r in roots]


class PathGuard:
    def __init__(
        self,
        *,
        workspace: Path,
        projects: Path,
        write_roots: Iterable[Path] = (),
        read_roots: Iterable[Path] | None = None,
        denied_roots: Iterable[Path] | None = None,
        protected_roots: Iterable[Path] = (),
    ) -> None:
        self.workspace = normalize_path(workspace)
        self.projects = normalize_path(projects)
        self.write_roots = [self.workspace, self.projects, *(normalize_path(p) for p in write_roots)]
        self.read_roots = None if read_roots is None else [normalize_path(p) for p in read_roots] + self.write_roots
        self.denied_roots = list(default_denied_roots() if denied_roots is None else (normalize_path(p) for p in denied_roots))
        self.protected_roots = [normalize_path(p) for p in protected_roots]

    # -- classification ------------------------------------------------------------------
    def is_denied(self, path: Path) -> bool:
        p = normalize_path(path)
        return any(is_within(p, d) for d in [*self.denied_roots, *self.protected_roots])

    def zone(self, path: str | Path) -> str:
        p = normalize_path(path)
        if self.is_denied(p):
            return "denied"
        if is_within(p, self.workspace):
            return "workspace"
        if is_within(p, self.projects):
            return "projects"
        if any(is_within(p, r) for r in self.write_roots):
            return "user"
        return "external"

    def can_read(self, path: str | Path, extra_roots: Iterable[Path] = ()) -> bool:
        p = normalize_path(path)
        if self.is_denied(p):
            return False
        if self.read_roots is None:
            return True
        return any(is_within(p, r) for r in [*self.read_roots, *(normalize_path(x) for x in extra_roots)])

    def can_write(self, path: str | Path, extra_roots: Iterable[Path] = ()) -> bool:
        p = normalize_path(path)
        if self.is_denied(p):
            return False
        roots = [*self.write_roots, *(normalize_path(x) for x in extra_roots)]
        return any(is_within(p, r) for r in roots)

    def check_read(self, path: str | Path, extra_roots: Iterable[Path] = ()) -> Path:
        p = normalize_path(path)
        if not self.can_read(p, extra_roots):
            raise PermissionDenied(f"Read access denied: {p}", details={"path": str(p), "zone": self.zone(p)})
        return p

    def check_write(self, path: str | Path, extra_roots: Iterable[Path] = ()) -> Path:
        p = normalize_path(path)
        if not self.can_write(p, extra_roots):
            raise PermissionDenied(
                f"Write access denied outside allowed folders: {p}", details={"path": str(p), "zone": self.zone(p)}
            )
        return p

    def is_root(self, path: str | Path) -> bool:
        p = normalize_path(path)
        return any(_key(p) == _key(r) for r in self.write_roots)

    def describe(self) -> dict:
        return {
            "workspace": str(self.workspace),
            "projects": str(self.projects),
            "write_roots": [str(p) for p in self.write_roots],
            "read_roots": None if self.read_roots is None else [str(p) for p in self.read_roots],
            "denied_roots": [str(p) for p in self.denied_roots],
            "protected_roots": [str(p) for p in self.protected_roots],
        }
