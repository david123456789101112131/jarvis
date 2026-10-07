"""What the git tool lets an agent run.

git can start programs in many ways besides its hooks: options such as `--upload-pack` or
`rebase --exec`, settings such as `core.fsmonitor`, `core.sshCommand` or a diff/filter driver,
`<transport>::` addresses, and a bare repository planted in an ordinary folder. It can also read
or write files outside the repository (`diff --no-index`, `--output`, `commit -F`). So the git
tool:

* runs only everyday subcommands, and `check_git_args` refuses every option that runs a program,
  reads or writes files outside the repository or moves git's folders (long options are matched
  the way git matches them, unique abbreviations included);
* always starts git with `GIT_SAFETY_CONFIG` and `GIT_SAFETY_ENV`, which switch those features
  off for the run (command-line settings beat the repository's own);
* changes only a short list of harmless settings (`git config user.name …`);
* shows the user the settings and hooks of a repository that would make git run a program
  (`repo_config_risks`) before git touches it, and where `git push` really sends the code
  (`push_destination`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

READ = frozenset({"status", "log", "diff", "show", "blame", "ls-files", "ls-tree", "rev-parse", "rev-list",
                  "describe", "shortlog", "grep", "cat-file", "whatchanged", "count-objects", "check-ignore",
                  "merge-base", "name-rev", "for-each-ref", "show-ref", "version"})
WRITE = frozenset({"init", "add", "commit", "checkout", "switch", "restore", "reset", "merge", "rebase",
                   "cherry-pick", "revert", "rm", "mv", "clean", "stash", "tag", "branch", "remote", "apply",
                   "notes", "reflog", "config"})
NETWORK = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})
SUBCOMMANDS = READ | WRITE | NETWORK

GIT_SAFETY_CONFIG: tuple[str, ...] = (
    "-c", "core.fsmonitor=false",                 # a setting that names a program to run on every status
    "-c", "safe.bareRepository=explicit",         # a bare repository planted in a folder is never picked up
    "-c", "protocol.allow=never",                 # no ext::, fd:: or other transport helpers…
    "-c", "protocol.ext.allow=never", "-c", "protocol.fd.allow=never",
    "-c", "protocol.https.allow=always", "-c", "protocol.http.allow=always",   # …only the real transports
    "-c", "protocol.ssh.allow=always", "-c", "protocol.git.allow=always", "-c", "protocol.file.allow=always",
    "-c", "core.quotepath=off",
)
# The environment beats core.editor / core.pager / sequence.editor from a repository's settings.
GIT_SAFETY_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "GIT_EDITOR": ":",
                  "GIT_SEQUENCE_EDITOR": ":", "GIT_MERGE_AUTOEDIT": "no"}

# Long options that run a program, read or write files anywhere, or move git's folders.
BLOCKED_LONG = ("--upload-pack", "--receive-pack", "--exec", "--extcmd", "--output", "--output-directory",
                "--template", "--git-dir", "--work-tree", "--separate-git-dir", "--config", "--tool", "--contents",
                "--no-index", "--file", "--global", "--system", "--open-files-in-pager", "--ext-diff",
                "--unsafe-paths", "--pathspec-from-file", "--reference", "--reference-if-able",
                "--build-fake-ancestor")
# Short options with the same effect, per subcommand, and the short options that take a value there
# (the rest of a cluster such as `-mfix` is that value, not more options).
BLOCKED_SHORT = {"commit": "Ft", "tag": "F", "merge": "F", "notes": "F", "rebase": "x", "grep": "O",
                 "clone": "uc", "fetch": "u", "pull": "u", "ls-remote": "u"}
VALUE_SHORT = {"commit": "mCcSu", "tag": "mun", "merge": "msXS", "notes": "mCc", "rebase": "sXSC",
               "grep": "efABCm", "clone": "obj", "fetch": "j", "pull": "sXSj", "log": "nSG", "show": "nSG",
               "diff": "USG", "stash": "m", "remote": "tm", "checkout": "bB", "switch": "cC", "init": "b",
               "push": "o", "clean": "e"}
STRATEGIES = frozenset({"ort", "recursive", "resolve", "octopus", "ours", "subtree"})
_STRATEGY_SHORT = frozenset({"merge", "pull", "rebase"})       # `-s` names a strategy only for these
_STRATEGY_LONG = _STRATEGY_SHORT | {"cherry-pick", "revert"}
SAFE_CONFIG_KEYS = frozenset({"user.name", "user.email", "init.defaultbranch", "core.autocrlf", "core.eol",
                              "core.safecrlf", "pull.rebase", "pull.ff", "push.autosetupremote", "push.default",
                              "fetch.prune", "merge.ff", "rebase.autostash", "color.ui", "core.quotepath"})
_CONFIG_READ_FLAGS = ("--get", "--get-all", "--get-regexp", "--list", "-l")
_CONFIG_FLAGS = ("--local", "--show-origin", "--show-scope", "--null", "-z", "--name-only", "--bool", "--int",
                 "--path", "--all", "--includes", "--no-includes", "--unset", "--type", "--default")
_ABSOLUTE = re.compile(r"^(?:[a-zA-Z]:|[\\/]|~)")
# Settings in a repository's own config that make git run a program or work in another folder.
_RISKY_KEYS = frozenset({"core.sshcommand", "core.askpass", "core.hookspath", "core.gitproxy",
                         "core.alternaterefscommand", "core.worktree", "diff.external", "credential.helper",
                         "gpg.program", "include.path", "uploadpack.packobjectshook"})
_RISKY_SUBKEYS = frozenset({("diff", "textconv"), ("diff", "command"), ("filter", "clean"), ("filter", "smudge"),
                            ("filter", "process"), ("merge", "driver"), ("credential", "helper"),
                            ("gpg", "program"), ("includeif", "path"), ("remote", "uploadpack"),
                            ("remote", "receivepack"), ("remote", "vcs"), ("protocol", "allow")})
# The hooks (programs in `.git/hooks`) each subcommand can make git run. Any command that writes the
# index or a ref may run the first group.
_HOOKS_ANY = frozenset({"post-index-change", "reference-transaction", "pre-auto-gc"})
_HOOKS_COMMIT = frozenset({"pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-rewrite"})
_HOOKS_BY_SUB: dict[str, frozenset[str]] = {
    "commit": _HOOKS_COMMIT, "cherry-pick": _HOOKS_COMMIT, "revert": _HOOKS_COMMIT,
    "merge": frozenset({"pre-merge-commit", "prepare-commit-msg", "commit-msg", "post-merge"}),
    "pull": frozenset({"pre-merge-commit", "prepare-commit-msg", "commit-msg", "post-merge", "post-checkout",
                       "pre-rebase", "post-rewrite"}),
    "rebase": frozenset({"pre-rebase", "post-rewrite", "post-checkout", "prepare-commit-msg", "commit-msg",
                         "post-commit"}),
    "checkout": frozenset({"post-checkout"}), "switch": frozenset({"post-checkout"}),
    "push": frozenset({"pre-push"}),
}
# Subcommands that never run a hook (everything else may: commit, checkout, merge, push, even status).
_HOOK_FREE = frozenset({"log", "show", "blame", "ls-files", "ls-tree", "rev-parse", "rev-list", "describe",
                        "shortlog", "grep", "cat-file", "whatchanged", "count-objects", "check-ignore", "merge-base",
                        "name-rev", "for-each-ref", "show-ref", "version", "diff"})


class GitRefused(ValueError):
    """The git call is not one the git tool runs; `en`/`he` say why."""

    def __init__(self, en: str, he: str) -> None:
        super().__init__(en)
        self.en, self.he = en, he


@dataclass
class GitCheck:
    sub: str
    kind: str                                                      # "read", "write" or "network"
    paths: list[tuple[str, str]] = field(default_factory=list)     # (path as given, "read" | "write")
    irreversible: list[tuple[str, str]] = field(default_factory=list)   # (English, Hebrew) reasons


def _matches(name: str, option: str) -> bool:
    """`name` is `option` or an abbreviation git would accept for it (`--e` for push's `--exec`)."""
    return name == option or (len(name) > 2 and option.startswith(name))


def _refuse_option(sub: str, arg: str) -> GitRefused:
    return GitRefused(f"the git tool does not run `git {sub} … {arg}` (that option can run a program or reach "
                      "files outside the repository); use run_command so the user can review it",
                      f"כלי ה-git לא מריץ `git {sub} … {arg}` (האפשרות הזו יכולה להריץ תוכנה או להגיע לקבצים "
                      "מחוץ למאגר); אפשר להריץ אותה עם run_command כדי שהמשתמש יבדוק אותה")


def _check_strategy(value: str) -> None:
    if value.strip().lower() not in STRATEGIES:
        raise GitRefused(f"unknown merge strategy '{value}' (git would run a program named git-merge-{value})",
                         f"אסטרטגיית מיזוג לא מוכרת '{value}' (git היה מריץ תוכנה בשם git-merge-{value})")


def path_like(value: str) -> bool:
    """An absolute path, or one that climbs out with `..` (anything else stays inside the repository)."""
    return bool(_ABSOLUTE.match(value)) or ".." in re.split(r"[\\/]", value)


def is_url(value: str) -> bool:
    return "://" in value or bool(re.match(r"^[\w.\-]+@[\w.\-]+:", value))


def check_git_args(args: list[str]) -> GitCheck:
    """Validate a git call (without the leading `git`); raises GitRefused when the tool must not run it."""
    if not args:
        raise GitRefused("no git subcommand given", "לא צוינה פקודת git")
    sub = args[0]
    if sub.startswith("-") or sub not in SUBCOMMANDS:
        raise GitRefused(f"the git tool does not run `git {sub}`; use run_command so the user can review it",
                         f"כלי ה-git לא מריץ `git {sub}`; אפשר להריץ אותה עם run_command כדי שהמשתמש יבדוק אותה")
    check = GitCheck(sub=sub, kind="network" if sub in NETWORK else "write" if sub in WRITE else "read")
    rest = args[1:]
    positional: list[str] = []
    pending = ""                    # what the next argument is: "value", "strategy" or ""
    options_done = False
    for arg in rest:
        if pending:
            if pending == "strategy":
                _check_strategy(arg)
            elif path_like(arg):
                check.paths.append((arg, "read"))
            pending = ""
            continue
        if "::" in arg:
            raise GitRefused(f"git transport helpers ({arg.split('::', 1)[0][-40:]}::) are not allowed",
                             f"כתובות git מסוג {arg.split('::', 1)[0][-40:]}:: אינן מותרות")
        if options_done or arg == "-" or not arg.startswith("-"):
            positional.append(arg)
            continue
        if arg == "--":
            options_done = True
            continue
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            name = name.lower()
            if any(_matches(name, opt) for opt in BLOCKED_LONG):
                raise _refuse_option(sub, arg)
            if name == "--message" and not eq:
                pending = "value"
            elif sub in _STRATEGY_LONG and name == "--strategy":
                if eq:
                    _check_strategy(value)
                else:
                    pending = "strategy"
            elif eq and path_like(value):
                check.paths.append((value, "read"))
            continue
        letters = arg[1:]                                  # a cluster of short options: -am, -mfix, -sours
        blocked, takes_value = BLOCKED_SHORT.get(sub, ""), VALUE_SHORT.get(sub, "")
        for j, letter in enumerate(letters):
            if letter in blocked:
                raise _refuse_option(sub, arg)
            attached = letters[j + 1:]
            if sub in _STRATEGY_SHORT and letter == "s":
                if attached:
                    _check_strategy(attached)
                else:
                    pending = "strategy"
                break
            if letter in takes_value:
                if not attached and letter not in "Su":      # -S / -u take only an attached value
                    pending = "value"
                elif attached and path_like(attached):
                    check.paths.append((attached, "read"))
                break
    if sub == "config" and _check_config(rest, positional):
        check.kind = "read"
    if sub == "reflog" and positional[:1] and positional[0] in ("expire", "delete"):
        check.irreversible.append(("throws away git's record of earlier states",
                                   "מוחק את התיעוד של git למצבים קודמים"))
    if sub in ("clone", "init"):
        # Whatever is not an address may be the folder git creates: every one must be writable.
        check.paths.extend((arg, "write") for arg in positional if not is_url(arg))
    else:
        check.paths.extend((arg, "read") for arg in positional if path_like(arg) and not is_url(arg))
    return check


def _check_config(rest: list[str], positional: list[str]) -> bool:
    """True for a read (`--get`, `--list`, one key); a change must be to a harmless setting."""
    flags = [a for a in rest if a.startswith("-")]
    for flag in flags:
        name = flag.partition("=")[0].lower()
        if name not in _CONFIG_READ_FLAGS and name not in _CONFIG_FLAGS:
            raise _refuse_option("config", flag)
    reading = any(f in _CONFIG_READ_FLAGS for f in flags) or (positional[:1] in (["get"], ["list"]))
    if reading or (len(positional) == 1 and "--unset" not in flags):
        return True
    words = positional[1:] if positional[:1] in (["set"], ["unset"]) else positional
    key = (words[0] if words else "").lower()
    if key not in SAFE_CONFIG_KEYS:
        allowed = ", ".join(sorted(SAFE_CONFIG_KEYS))
        raise GitRefused(f"the git tool changes only harmless settings ({allowed}); '{key}' can make git run "
                         "programs or work elsewhere",
                         f"כלי ה-git משנה רק הגדרות בטוחות ({allowed}); '{key}' יכולה לגרום ל-git להריץ תוכנות "
                         "או לעבוד במקום אחר")
    return False


# ------------------------------------------------------------------------------------------ repository
def find_repo(start: Path) -> tuple[Path, Path] | None:
    """(work folder, .git folder) of the repository `start` is in, following a `.git` file's `gitdir:`."""
    folder = start if start.is_dir() else start.parent
    for candidate in (folder, *folder.parents):
        dot = candidate / ".git"
        try:
            if dot.is_dir():
                return candidate, dot
            if dot.is_file():
                text = dot.read_text(encoding="utf-8", errors="replace").strip()
                if not text.lower().startswith("gitdir:"):
                    return None
                target = Path(text.split(":", 1)[1].strip())
                return candidate, (target if target.is_absolute() else candidate / target)
        except OSError:
            return None
    return None


_SECTION = re.compile(r'^\[\s*([A-Za-z0-9.\-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*(.*)$')
_ENTRY = re.compile(r"^([A-Za-z][A-Za-z0-9\-]*)\s*(?:=\s*(.*))?$")


def read_config(path: Path) -> list[tuple[str, str | None, str, str]]:
    """(section, subsection, key, value) entries of a git config file; names lower-case."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[tuple[str, str | None, str, str]] = []
    section, sub = "", None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        m = _SECTION.match(line)
        if m:
            name = m.group(1)
            if "." in name:                                   # [remote.origin] (old style)
                section, sub = name.split(".", 1)[0].lower(), name.split(".", 1)[1]
            else:
                section, sub = name.lower(), m.group(2)
            line = m.group(3).strip()
            if not line:
                continue
        e = _ENTRY.match(line)
        if e and section:
            out.append((section, sub, e.group(1).lower(), (e.group(2) if e.group(2) is not None else "true").strip()))
    return out


def _config_files(git_dir: Path) -> list[Path]:
    files = [git_dir / "config", git_dir / "config.worktree"]
    commondir = git_dir / "commondir"
    try:
        if commondir.is_file():
            common = Path(commondir.read_text(encoding="utf-8").strip())
            files.append((common if common.is_absolute() else git_dir / common) / "config")
    except OSError:
        pass
    return files


def active_hooks(git_dir: Path, sub: str) -> list[Path]:
    """The hooks `git <sub>` would run from the repository's hooks folder (`.sample` files are inactive,
    and outside Windows so is a file that is not executable)."""
    names = _HOOKS_ANY | _HOOKS_BY_SUB.get(sub, frozenset())
    folders = [git_dir / "hooks"]
    commondir = git_dir / "commondir"
    try:
        if commondir.is_file():
            common = Path(commondir.read_text(encoding="utf-8").strip())
            folders.append((common if common.is_absolute() else git_dir / common) / "hooks")
    except OSError:
        pass
    found: list[Path] = []
    for folder in folders:
        try:
            found.extend(p for p in folder.iterdir()
                         if p.is_file() and p.name.lower().removesuffix(".exe") in names
                         and (os.name == "nt" or os.access(p, os.X_OK)))
        except OSError:
            continue
    return sorted(set(found))


def repo_config_risks(root: Path, sub: str) -> list[str]:
    """Settings and hooks that would make `git <sub>` here run a program: the repository's own, plus
    a hooks folder set in the user's global settings."""
    found: list[str] = []
    repo = find_repo(root)
    if repo is not None and sub not in _HOOK_FREE:
        found.extend(f"hook {hook.name} ({hook})" for hook in active_hooks(repo[1], sub))
    for path in _config_files(repo[1]) if repo else []:
        for section, subsection, key, value in read_config(path):
            name = f"{section}.{key}"
            label = f"{section}.{subsection}.{key}" if subsection else name
            if name == "core.hookspath" and sub in _HOOK_FREE:
                continue
            if name in _RISKY_KEYS or (section, key) in _RISKY_SUBKEYS:
                found.append(f"{label} = {value[:120]}")
            elif section == "remote" and key in ("url", "pushurl") and "::" in value:
                found.append(f"{label} = {value[:120]}")
    if sub not in _HOOK_FREE:
        for path in (Path.home() / ".gitconfig", Path.home() / ".config" / "git" / "config"):
            for section, _subsection, key, value in read_config(path):
                if (section, key) == ("core", "hookspath"):
                    found.append(f"core.hooksPath = {value[:120]} ({path.name})")
    return sorted(set(found))


def push_destination(root: Path, args: list[str]) -> str | None:
    """Where `git push <args>` sends the code: the URL given, or the remote's push URL."""
    remote: str | None = None
    rest = iter(args[1:])
    positional: list[str] = []
    for arg in rest:
        name, eq, value = arg.partition("=")
        if arg.startswith("--") and len(name) > 4 and "--repo".startswith(name.lower()):
            remote = value if eq else next(rest, None)
        elif not arg.startswith("-"):
            positional.append(arg)
    remote = remote or (positional[0] if positional else "origin")
    if is_url(remote) or _ABSOLUTE.match(remote):
        return remote
    repo = find_repo(root)
    if repo is None:
        return None
    urls: dict[str, str] = {}
    for section, subsection, key, value in read_config(repo[1] / "config"):
        if section == "remote" and subsection == remote and key in ("url", "pushurl"):
            urls[key] = value
    return urls.get("pushurl") or urls.get("url") or remote


__all__ = ["GIT_SAFETY_CONFIG", "GIT_SAFETY_ENV", "GitCheck", "GitRefused", "check_git_args", "find_repo",
           "is_url", "path_like", "push_destination", "read_config", "repo_config_risks"]
