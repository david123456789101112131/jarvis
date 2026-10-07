"""Development tools: run Python, run a project's tests, install packages, Git."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.common.errors import PermissionDenied
from jarvis.permissions import analyze_command
from jarvis.permissions.models import PathUse
from jarvis.permissions.risk import SHELL_COMMAND_REASON, command_scope
from jarvis.security.codecheck import pure_python_problem
from jarvis.security.gitcheck import (
    GIT_SAFETY_CONFIG,
    GIT_SAFETY_ENV,
    GitRefused,
    check_git_args,
    find_repo,
    push_destination,
    repo_config_risks,
)
from jarvis.security.path_guard import _key, normalize_path
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult
from jarvis.tools.shell import process_result, run_in_context

C = Capability
IS_WINDOWS = sys.platform == "win32"
_ABSOLUTE_ARG = re.compile(r"^(?:[a-zA-Z]:|[\\/]|~)")


def _usable(path: str | None) -> bool:
    # The Microsoft Store "python.exe" alias in WindowsApps opens the Store instead of running Python.
    return bool(path) and "windowsapps" not in str(path).lower()


def find_python() -> list[str] | None:
    """argv prefix of a real Python 3 interpreter on this machine."""
    if not getattr(sys, "frozen", False) and sys.executable:
        return [sys.executable]
    if IS_WINDOWS and _usable(shutil.which("py")):
        return [shutil.which("py") or "py", "-3"]
    for name in ("python3", "python"):
        exe = shutil.which(name)
        if _usable(exe):
            return [exe]  # type: ignore[list-item]
    return None


def venv_python(project: Path) -> list[str] | None:
    for folder in (".venv", "venv", "env"):
        exe = project / folder / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
        if exe.is_file():
            return [str(exe)]
    return None


def project_root(start: Path) -> Path:
    """Nearest folder (start or a parent) that looks like a project root."""
    markers = ("pyproject.toml", "package.json", "requirements.txt", "setup.py", ".git", "pom.xml", "build.gradle")
    p = start if start.is_dir() else start.parent
    for candidate in (p, *p.parents):
        if any((candidate / m).exists() for m in markers):
            return candidate
    return p


# ------------------------------------------------------------------------------------------ python
class RunPythonParams(BaseModel):
    code: str = Field("", description="Python code to run (use this or file)")
    file: str = Field("", description="A .py file to run")
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    stdin: str = ""
    timeout_s: float = Field(120, ge=1, le=1800)


MAX_CHECKED_FILE = 200_000          # a .py file up to this size is read and checked before it runs


def _read_code(path: Path) -> str | None:
    try:
        if path.is_file() and path.stat().st_size <= MAX_CHECKED_FILE:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return None


class RunPythonTool(Tool):
    name = "run_python"
    description = ("Run Python code or a .py file and return stdout/stderr. Uses the project's virtual "
                   "environment when the folder has one. Pure calculations run at once; code that can touch "
                   "files, programs or the network asks the user first.")
    capabilities = (C.CODE_EXECUTE,)
    Params = RunPythonParams
    risk = RiskLevel.MEDIUM
    timeout_s = 1900.0

    def _cwd(self, params: RunPythonParams, ctx: ToolContext) -> Path:
        if params.cwd:
            return ctx.resolve(params.cwd)
        if params.file:
            return ctx.resolve(params.file).parent
        return ctx.cwd or ctx.services.paths.workspace

    async def plan(self, params: RunPythonParams, ctx: ToolContext) -> ActionPlan:
        cwd = self._cwd(params, ctx)
        paths = [PathUse(path=str(cwd), access="read")]
        code: str | None = params.code
        if params.file:
            target = ctx.resolve(params.file)
            paths.append(PathUse(path=str(target), access="read"))
            code = await asyncio.to_thread(_read_code, target)
        problem = pure_python_problem(code) if code is not None else "the file could not be checked"
        # The run executes exactly the code that was checked here (a file could change in between).
        ctx.extra["checked_code"] = code if problem is None else None
        summary = ctx.t(f"הרצת הקובץ {Path(params.file).name} ב-Python" if params.file else "הרצת קוד Python",
                        f"Run Python {'file ' + Path(params.file).name if params.file else 'code'}")
        details = {"code": (code or "")[:4000]} if code else {"file": params.file}
        if problem is None:
            return ActionPlan(summary=summary, paths=paths, risk=RiskLevel.LOW, details=details)
        if params.file and not params.code and await asyncio.to_thread(self._generated_run, params, cwd, ctx):
            ctx.extra["trusted_run"] = True
            return ActionPlan(summary=summary, paths=paths, details=details)
        return ActionPlan(summary=summary, paths=paths, risk=RiskLevel.HIGH, details=details, remember_scope="python",
                          risk_reasons=[ctx.t(f"קוד שיכול לגעת בקבצים, בתוכנות או ברשת ({problem})",
                                              f"code that can touch files, programs or the network ({problem})")])

    @staticmethod
    def _generated_run(params: RunPythonParams, cwd: Path, ctx: ToolContext) -> bool:
        """The script's own run command in a project that is exactly as JARVIS generated it."""
        from jarvis.tools.project_tools import command_line

        trust = ctx.services.code_trust
        target = ctx.resolve(params.file)
        root = trust.trusted_root(target)
        if root is None or _key(root) != _key(cwd):
            return False
        try:
            rel = target.relative_to(root).as_posix()
        except ValueError:
            return False
        return trust.allows_command(cwd, command_line(["python", rel, *params.args]))

    async def run(self, params: RunPythonParams, ctx: ToolContext) -> ToolResult:
        if not params.code and not params.file:
            return ToolResult.fail(ctx.t("צריך קוד או קובץ להרצה.", "Give code or a file to run."))
        cwd = self._cwd(params, ctx)
        checked = ctx.extra.get("checked_code")
        # A pure calculation needs nothing from a project's virtual environment (only the standard library),
        # so it runs on JARVIS's own interpreter, never on one a project folder provides.
        python = find_python() if checked is not None else (venv_python(project_root(cwd)) or find_python())
        if python is None:
            return ToolResult.fail(ctx.t("Python לא מותקן במחשב (אפשר להתקין אותו מ-python.org).",
                                         "Python is not installed on this computer (install it from python.org)."))
        if ctx.extra.get("trusted_run") and not await asyncio.to_thread(self._generated_run, params, cwd, ctx):
            return changed_since_check(ctx)
        if checked is not None:
            # A pure calculation: isolated mode, so a planted json.py next to it can never be imported.
            argv = [*python, "-I", "-X", "utf8", "-c", checked, *params.args]
        elif params.file:
            target = ctx.resolve(params.file)
            if not target.is_file():
                return ToolResult.fail(ctx.t(f"הקובץ לא נמצא: {target}", f"File not found: {target}"))
            argv = [*python, "-X", "utf8", str(target), *params.args]
        else:
            argv = [*python, "-X", "utf8", "-c", params.code, *params.args]
        res = await run_in_context(argv, ctx, cwd=cwd, timeout=params.timeout_s,
                                   stdin=params.stdin.encode() if params.stdin else None)
        return process_result(res, "Python", ctx.lang)


def changed_since_check(ctx: ToolContext) -> ToolResult:
    """The project ran without asking because it was exactly as generated, and it changed after that
    check (another agent wrote to it in between): do not run it now."""
    return ToolResult.fail(ctx.t("הקוד של הפרויקט השתנה אחרי הבדיקה, ולכן הוא לא הורץ. בהרצה הבאה JARVIS יבקש אישור.",
                                 "The project's code changed after it was checked, so it was not run. "
                                 "The next run will ask for approval."), denied="blocked")


# ------------------------------------------------------------------------------------------ tests
class RunTestsParams(BaseModel):
    path: str = Field(..., description="Project folder")
    command: str = Field("", description="Override the detected test command")
    timeout_s: float = Field(600, ge=5, le=3600)


def detect_test_command(root: Path) -> tuple[list[str], str] | None:
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        test = scripts.get("test", "")
        if test and "no test specified" not in test:
            npm = shutil.which("npm.cmd" if IS_WINDOWS else "npm") or "npm"
            return [npm, "test", "--silent"], "npm test"
    has_py_tests = (root / "tests").is_dir() or any(root.glob("test_*.py")) or (root / "pytest.ini").is_file()
    if has_py_tests or (root / "pyproject.toml").is_file():
        python = venv_python(root) or find_python()
        if python:
            return [*python, "-m", "pytest", "-q", "--no-header"], "pytest"
    return None


_COUNT_HE = {"passed": "עברו", "failed": "נכשלו", "error": "שגיאות", "errors": "שגיאות", "skipped": "דולגו"}
_COUNT_HE_ONE = {"passed": "עברה", "failed": "נכשלה", "error": "שגיאה", "errors": "שגיאה", "skipped": "דולגה"}
_NODE_COUNTS = {"pass": "passed", "fail": "failed", "skipped": "skipped", "skip": "skipped"}


def parse_test_counts(output: str) -> dict[str, int]:
    """Passed/failed/skipped counts from pytest, jest or vitest ("5 passed") or Node's test runner ("# pass 5")."""
    counts = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|error|errors|skipped)", output)}
    if not counts:
        for k, v in re.findall(r"^\s*(?:#|ℹ)\s*(pass|fail|skipped|skip)\s+(\d+)\s*$", output, re.M):
            counts[_NODE_COUNTS[k]] = int(v)
    return {k: v for k, v in counts.items() if v or k == "passed"}


def count_text(counts: dict[str, int], lang: str) -> str:
    """"18 passed, 1 skipped" in the user's language."""
    if lang != "he":
        return ", ".join(f"{v} {k}" for k, v in counts.items())
    return ", ".join(f"{v} {(_COUNT_HE_ONE if v == 1 else _COUNT_HE).get(k, k)}" for k, v in counts.items())


class RunTestsTool(Tool):
    name = "run_tests"
    description = "Run a project's automated tests (pytest or npm test, detected automatically)."
    capabilities = (C.CODE_EXECUTE,)
    Params = RunTestsParams
    risk = RiskLevel.MEDIUM
    timeout_s = 3700.0

    async def plan(self, params: RunTestsParams, ctx: ToolContext) -> ActionPlan:
        root = await asyncio.to_thread(project_root, ctx.resolve(params.path))
        summary = ctx.t(f"הרצת הבדיקות של {root.name}", f"Run tests in {root.name}")
        paths = [PathUse(path=str(root), access="read")]
        details = {"path": str(root), **({"command": params.command} if params.command else {})}
        if params.command:
            # A command line of its own is vetted like run_command (the permission check reads `command`).
            return ActionPlan(summary=summary, paths=paths, risk=RiskLevel.HIGH, details=details,
                              capabilities=[C.CODE_EXECUTE, C.SHELL_EXECUTE],
                              risk_reasons=[ctx.t("מריץ את הקוד של הפרויקט עצמו (הבדיקות שלו)",
                                                  "runs the project's own code (its tests)")],
                              remember_scope=f"{root} {command_scope(params.command)}")
        trusted = await asyncio.to_thread(ctx.services.code_trust.trusted_root, root)
        if trusted is not None and _key(trusted) == _key(root):
            ctx.extra["trusted_run"] = True
            return ActionPlan(summary=summary, paths=paths, details=details)   # exactly as JARVIS generated it
        # Tests are the project's own code: they can do anything the project's code can.
        return ActionPlan(summary=summary, paths=paths, risk=RiskLevel.HIGH, details=details,
                          risk_reasons=[ctx.t("מריץ את הקוד של הפרויקט עצמו (הבדיקות שלו)",
                                              "runs the project's own code (its tests)")],
                          remember_scope=str(root))

    async def run(self, params: RunTestsParams, ctx: ToolContext) -> ToolResult:
        root = project_root(ctx.resolve(params.path))
        if ctx.extra.get("trusted_run") and await asyncio.to_thread(ctx.services.code_trust.trusted_root, root) is None:
            return changed_since_check(ctx)
        if params.command:
            from jarvis.tools.shell import shell_argv

            argv, label = shell_argv(params.command, "auto", ctx.services.host), params.command
        else:
            detected = await asyncio.to_thread(detect_test_command, root)
            if detected is None:
                return ToolResult.fail(ctx.t(f"לא נמצאו בדיקות ב-{root} (אין בדיקות pytest או סקריפט npm test).",
                                             f"No tests found in {root} (no pytest tests or npm test script)."))
            argv, label = detected
        res = await run_in_context(argv, ctx, cwd=root, timeout=params.timeout_s)
        result = process_result(res, f"Tests ({label})", ctx.lang, f"הבדיקות ({label})")
        out = (res.stdout or "") + "\n" + (res.stderr or "")
        counts = parse_test_counts(out)
        result.data = {**(result.data or {}), "counts": counts, "runner": label}
        if counts:
            result.summary = ctx.t(f"הבדיקות ({label}): {count_text(counts, 'he')}",
                                   f"Tests ({label}): {count_text(counts, 'en')}")
            if not result.ok:
                result.error = result.summary + "\n" + res.summary(1500)
        return result


# ------------------------------------------------------------------------------------------ install
class InstallParams(BaseModel):
    manager: Literal["pip", "npm"] = "pip"
    packages: list[str] = Field(default_factory=list, description="Package names; empty = install the project's "
                                                                   "requirements / package.json")
    path: str = Field(..., description="Project folder (a .venv is created there for pip if missing)")
    dev: bool = False


_PKG_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._\-/@\[\],<>=!~^*]*$")


class InstallPackagesTool(Tool):
    name = "install_packages"
    description = ("Install packages into a project (pip into the project's .venv, or npm into node_modules). "
                   "Always asks the user first.")
    capabilities = (C.CODE_INSTALL,)
    Params = InstallParams
    risk = RiskLevel.HIGH
    always_confirm = True
    timeout_s = 1900.0

    async def plan(self, params: InstallParams, ctx: ToolContext) -> ActionPlan:
        root = ctx.resolve(params.path)
        what = ", ".join(params.packages) if params.packages else "project dependencies"
        what_he = ", ".join(params.packages) if params.packages else "התלויות של הפרויקט"
        return ActionPlan(summary=ctx.t(f"התקנת {what_he} עם {params.manager} בפרויקט {root.name}",
                                        f"Install {what} with {params.manager} in {root.name}"),
                          paths=[PathUse(path=str(root), access="write")],
                          details={"manager": params.manager, "packages": params.packages, "path": str(root)})

    async def run(self, params: InstallParams, ctx: ToolContext) -> ToolResult:
        bad = [p for p in params.packages if not _PKG_RE.match(p) or p.startswith("-")]
        if bad:
            return ToolResult.fail(ctx.t(f"שמות חבילות לא תקינים: {', '.join(bad)}", f"Invalid package name(s): {', '.join(bad)}"))
        root = ctx.resolve(params.path)
        if not root.is_dir():
            return ToolResult.fail(ctx.t(f"תיקיית הפרויקט לא נמצאה: {root}", f"Project folder not found: {root}"))
        if params.manager == "npm":
            npm = shutil.which("npm.cmd" if IS_WINDOWS else "npm")
            if not npm:
                return ToolResult.fail(ctx.t("Node.js/npm לא מותקן (אפשר להתקין אותו מ-nodejs.org).",
                                             "Node.js/npm is not installed (install it from nodejs.org)."))
            argv = [npm, "install", *(["--save-dev"] if params.dev and params.packages else []), *params.packages]
            res = await run_in_context(argv, ctx, cwd=root, timeout=1800)
            return process_result(res, "npm install", ctx.lang)
        python = venv_python(root)
        created: list[str] = []
        if python is None:
            base = find_python()
            if base is None:
                return ToolResult.fail(ctx.t("Python לא מותקן במחשב.", "Python is not installed on this computer."))
            res = await run_in_context([*base, "-m", "venv", str(root / ".venv")], ctx, cwd=root, timeout=300)
            if not res.ok:
                return process_result(res, "Creating the virtual environment", ctx.lang, "יצירת הסביבה הווירטואלית")
            created.append(str(root / ".venv"))
            python = venv_python(root)
            if python is None:
                return ToolResult.fail(ctx.t("הסביבה הווירטואלית נוצרה, אבל אין בה Python.",
                                             "The virtual environment was created but has no Python executable."))
        if params.packages:
            argv = [*python, "-m", "pip", "install", "--disable-pip-version-check", *params.packages]
        elif (root / "requirements.txt").is_file():
            argv = [*python, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"]
        elif (root / "pyproject.toml").is_file():
            argv = [*python, "-m", "pip", "install", "--disable-pip-version-check", "-e", "."]
        else:
            return ToolResult.fail(ctx.t("אין מה להתקין: לא צוינו חבילות ואין קובץ requirements.",
                                         "Nothing to install: no packages given and no requirements file."))
        res = await run_in_context(argv, ctx, cwd=root, timeout=1800)
        result = process_result(res, "pip install", ctx.lang)
        result.files_created.extend(created)
        return result


# ------------------------------------------------------------------------------------------ git
class GitParams(BaseModel):
    args: list[str] = Field(..., min_length=1, description="git arguments, e.g. ['status'] or ['commit', '-m', 'msg']")
    path: str = Field(..., description="Repository folder")
    timeout_s: float = Field(120, ge=1, le=1800)


class GitTool(Tool):
    name = "git"
    description = ("Run an everyday git command in a repository (status, log, diff, add, commit, branch, checkout, "
                   "merge, init, clone, fetch, pull, push, config user.name/email…). Options that run programs or "
                   "reach files outside the repository are refused; pushing always asks the user.")
    capabilities = (C.GIT,)
    Params = GitParams
    timeout_s = 1900.0

    async def plan(self, params: GitParams, ctx: ToolContext) -> ActionPlan:
        try:
            check = check_git_args(params.args)
        except GitRefused as exc:
            raise PermissionDenied(ctx.t(exc.he, exc.en)) from exc
        root = ctx.resolve(params.path)
        sub = check.sub
        line = "git " + " ".join(params.args)
        a = analyze_command(line, ctx.lang)
        repo = await asyncio.to_thread(find_repo, root) if sub not in ("init", "clone") else None
        work = repo[0] if repo else root
        paths = [PathUse(path=str(root), access="read")]
        if check.kind != "read" and sub != "ls-remote":
            # A write (reset --hard, clean, checkout…) acts on the whole work folder, not only on `path`.
            paths.append(PathUse(path=str(work), access="write"))
        for given, access in check.paths:
            where = ctx.resolve(given) if _ABSOLUTE_ARG.match(given) else normalize_path(root / given)
            paths.append(PathUse(path=str(where), access=access))
        risk = RiskLevel.SAFE if check.kind == "read" else RiskLevel.LOW if check.kind == "write" else RiskLevel.MEDIUM
        risk = max(risk, a.risk) if a.risk > RiskLevel.MEDIUM else risk
        reasons = [r for r in a.reasons if r not in SHELL_COMMAND_REASON]
        irreversible = a.irreversible
        for en, he in check.irreversible:
            reasons.append(ctx.t(he, en))
            risk, irreversible = max(risk, RiskLevel.HIGH), True
        risky = await asyncio.to_thread(repo_config_risks, root, sub) if sub not in ("init", "clone") else []
        if risky:
            risk = max(risk, RiskLevel.HIGH)
            reasons.append(ctx.t(f"המאגר מריץ תוכנות משלו (הגדרות או hooks): {'; '.join(risky)}",
                                 f"the repository runs programs of its own (settings or hooks): {'; '.join(risky)}"))
        details: dict[str, object] = {"path": str(root)}
        destination = None
        if sub == "push":
            destination = await asyncio.to_thread(push_destination, root, params.args)
            details["destination"] = destination or ctx.t("לא ידוע", "unknown")
        return ActionPlan(summary=line[:200] + (f" → {destination}" if destination else ""), paths=paths, risk=risk,
                          risk_reasons=reasons, destination=destination, details=details,
                          irreversible=irreversible, remember_scope=f"git {sub}")

    async def run(self, params: GitParams, ctx: ToolContext) -> ToolResult:
        git = shutil.which("git")
        if git is None:
            return ToolResult.fail(ctx.t("Git לא מותקן (אפשר להתקין אותו מ-git-scm.com).",
                                         "Git is not installed (install it from git-scm.com)."))
        root = ctx.resolve(params.path)
        if params.args[0] not in ("init", "clone") and not root.is_dir():
            return ToolResult.fail(ctx.t(f"התיקייה לא נמצאה: {root}", f"Folder not found: {root}"))
        if params.args[0] in ("init", "clone"):
            await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        res = await run_in_context([git, *GIT_SAFETY_CONFIG, *params.args], ctx, cwd=root,
                                   timeout=params.timeout_s, extra_env=dict(GIT_SAFETY_ENV))
        return process_result(res, "git " + params.args[0], ctx.lang)


DEV_TOOLS: tuple[type[Tool], ...] = (RunPythonTool, RunTestsTool, InstallPackagesTool, GitTool)

__all__ = ["DEV_TOOLS", "detect_test_command", "find_python", "project_root", "venv_python"]
