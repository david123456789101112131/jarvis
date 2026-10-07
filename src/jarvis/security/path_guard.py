r"""Folder access control for every filesystem-touching tool.

* Hard-denied locations (system folders, credential stores, browser profiles, JARVIS's own
  data folder with the secrets/config/database) can never be read or written by agents.
* Writes are allowed only inside the configured write roots (workspace, projects and the
  user's common folders by default) or inside folders an explicit grant adds.
* Paths are normalised and resolved (symlinks, `..`, env vars) before any decision, so
  traversal tricks cannot escape a root.
* Windows path tricks are refused before Windows ever opens the path: device paths (`\\?\`,
  `\\.\`), network paths (opening `\\server\share` can send the Windows sign-in to that
  server) unless the share is allowed, alternate data streams (`file.txt:hidden`), reserved
  device names (`CON`, `NUL`, `COM1`…) and names ending in a dot or a space.
* Folders that tools manage (`.git`, `node_modules`, `__pycache__`, virtual environments) are
  never written by the agents' file tools: code planted there would run behind everyone's back.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from jarvis.common.errors import PermissionDenied

IS_WINDOWS = sys.platform == "win32"

_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\", "//?/", "//./", "\\\\?/", "\\\\./")
_RESERVED_NAME = re.compile(r"^(?:con|prn|aux|nul|com[0-9\u00b9\u00b2\u00b3]|lpt[0-9\u00b9\u00b2\u00b3]|conin\$|conout\$)$", re.I)
_DRIVE = re.compile(r"^[a-zA-Z]:")
MANAGED_DIRS = {".git": ("Git's own folder", "התיקייה הפנימית של Git"),
                "node_modules": ("the installed Node.js packages", "חבילות Node.js המותקנות"),
                "__pycache__": ("Python's compiled-code cache", "המטמון של Python"),
                ".pytest_cache": ("pytest's cache", "המטמון של pytest"),
                ".ruff_cache": ("ruff's cache", "המטמון של ruff"),
                ".mypy_cache": ("mypy's cache", "המטמון של mypy")}
VENV_REASON = ("a Python virtual environment", "סביבה וירטואלית של Python")


def _unc_share(text: str) -> str | None:
    """`\\\\server\\share\\x` → `\\\\server\\share` (lower-case); None when it is not a network path."""
    s = text.replace("/", "\\")
    if s[:8].upper() == "\\\\?\\UNC\\":                     # the long form \\?\UNC\server\share
        s = "\\\\" + s[8:]
    if not s.startswith("\\\\") or s.startswith(("\\\\?\\", "\\\\.\\")):
        return None
    parts = [p for p in s[2:].split("\\") if p]
    return ("\\\\" + "\\".join(parts[:2])).lower() if len(parts) >= 2 else "\\\\" + "\\".join(parts).lower()


def windows_path_problem(text: str, allowed_shares: Iterable[str] = ()) -> tuple[str, str] | None:
    """Why a Windows path must not be opened at all, as (English, Hebrew), or None. Pure: tested on every OS."""
    s = str(text).strip().strip('"')
    share = _unc_share(s)
    if share is None and s.replace("/", "\\").startswith(_DEVICE_PREFIXES):
        return ("it is a device path (\\\\?\\ or \\\\.\\)", "זה נתיב התקן (\\\\?\\ או \\\\.\\)")
    if share is not None:
        allowed = {(_unc_share(a) or a).lower().rstrip("\\") for a in allowed_shares}
        if share not in allowed:
            return ("it is a network path, and opening one can send your Windows sign-in to that computer "
                    "(allow the share in the security settings to use it)",
                    "זה נתיב רשת, ופתיחה שלו יכולה לשלוח את פרטי הכניסה שלך ל-Windows למחשב ההוא "
                    "(אפשר להתיר את התיקייה המשותפת בהגדרות האבטחה)")
        rest = s.replace("/", "\\")[len(share):]
        if s[:8].upper() == "\\\\?\\UNC\\":
            rest = s.replace("/", "\\")[len(share) + 6:]
    else:
        rest = s[2:] if _DRIVE.match(s) else s
    if ":" in rest:
        return ("it names a hidden data stream (a ':' inside the name)", "הוא מפנה לזרם נתונים נסתר (':' בתוך השם)")
    for part in re.split(r"[\\/]+", rest):
        if not part or part in (".", ".."):
            continue
        if part[-1] in " .":
            return (f"the name {part!r} ends with a dot or a space, which Windows silently drops",
                    f"השם {part!r} מסתיים בנקודה או ברווח, ש-Windows משמיט בשקט")
        if _RESERVED_NAME.match(part.split(".")[0].rstrip(" ")):
            return (f"{part} is a reserved Windows device name", f"{part} הוא שם התקן שמור של Windows")
    return None


def looks_like_windows_path(text: str) -> bool:
    s = str(text).strip().strip('"')
    return s.startswith(("\\\\", "//?/", "//./")) or bool(re.match(r"^[a-zA-Z]:[\\/]", s))


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
            os.path.join(appdata, "Microsoft", "Windows", "PowerShell", "PSReadLine"),   # typed commands, passwords
            os.path.join(local, "Microsoft", "Credentials"),
            os.path.join(local, "Microsoft", "Vault"),
            os.path.join(local, "Google", "Chrome", "User Data"),
            os.path.join(local, "Microsoft", "Edge", "User Data"),
            os.path.join(local, "BraveSoftware"),
            os.path.join(local, "Vivaldi", "User Data"),
            os.path.join(appdata, "Mozilla", "Firefox", "Profiles"),
            os.path.join(appdata, "Thunderbird", "Profiles"),
            os.path.join(appdata, "Opera Software"),
            os.path.join(appdata, "FileZilla"),
            os.path.join(appdata, "Telegram Desktop", "tdata"),
            os.path.join(appdata, "discord"),
            os.path.join(appdata, "Slack"),
            os.path.join(appdata, "Signal"),
            os.path.join(appdata, "Bitwarden"),
            os.path.join(appdata, "KeePass"),
            os.path.join(local, "1Password"),
            os.path.join(appdata, "gcloud"),
            "C:\\System Volume Information",
            "C:\\$Recycle.Bin",
        ]
    else:
        out += ["/etc", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/boot", "/sys", "/proc", "/dev", "/var/lib", "/snap"]
        out += [str(home / ".mozilla"), str(home / ".thunderbird"), str(home / ".config" / "google-chrome"),
                str(home / ".config" / "chromium"), str(home / ".config" / "BraveSoftware"),
                str(home / ".local" / "share" / "keyrings"), str(home / ".password-store"),
                str(home / ".config" / "gcloud"), str(home / ".bash_history"), str(home / ".zsh_history")]
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
        allowed_shares: Iterable[str] = (),
    ) -> None:
        self.workspace = normalize_path(workspace)
        self.projects = normalize_path(projects)
        self.write_roots = [self.workspace, self.projects, *(normalize_path(p) for p in write_roots)]
        self.read_roots = None if read_roots is None else [normalize_path(p) for p in read_roots] + self.write_roots
        self.denied_roots = list(default_denied_roots() if denied_roots is None else (normalize_path(p) for p in denied_roots))
        self.protected_roots = [normalize_path(p) for p in protected_roots]
        # Network shares that may be opened: the ones the user allowed, and any write root on a share.
        shares = {(_unc_share(s) or s).lower().rstrip("\\") for s in allowed_shares}
        shares.update(share for r in self.write_roots if (share := _unc_share(str(r))))
        self.allowed_shares = sorted(shares)

    def path_problem(self, path: str | Path) -> tuple[str, str] | None:
        """Why `path` must not be opened at all (device/network path, data stream, reserved name), or None."""
        text = str(path)
        if not (IS_WINDOWS or looks_like_windows_path(text)):
            return None
        return windows_path_problem(text, self.allowed_shares)

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
        if self.is_denied(p) or self.path_problem(p) is not None:
            return False
        if self.read_roots is None:
            return True
        return any(is_within(p, r) for r in [*self.read_roots, *(normalize_path(x) for x in extra_roots)])

    def can_write(self, path: str | Path, extra_roots: Iterable[Path] = ()) -> bool:
        p = normalize_path(path)
        if self.is_denied(p) or self.path_problem(p) is not None or self.managed_folder(p, "write") is not None:
            return False
        roots = [*self.write_roots, *(normalize_path(x) for x in extra_roots)]
        return any(is_within(p, r) for r in roots)

    @staticmethod
    def managed_folder(path: str | Path, access: str = "write") -> tuple[str, str] | None:
        """(English, Hebrew) name of the tool-managed folder `path` is in, or None.

        Writing inside `.git`, `node_modules`, `__pycache__`, the test tools' caches or a virtual
        environment is refused (a planted hook, package or compiled file would run unseen), and so is
        creating a `pyvenv.cfg`, which would make a folder count as a virtual environment. Deleting
        the whole folder is fine: it only removes what an install or git can rebuild."""
        p = normalize_path(path)
        parts = [x.lower() for x in p.parts]
        inside = parts[:-1] if access == "delete" else parts
        for name in inside:
            if name in MANAGED_DIRS:
                return MANAGED_DIRS[name]
        if access != "delete" and parts and parts[-1] == "pyvenv.cfg":
            return VENV_REASON
        for parent in list(p.parents)[:12]:
            if (parent / "pyvenv.cfg").is_file():
                return VENV_REASON
        return None

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
