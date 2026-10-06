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
from jarvis.permissions import analyze_command
from jarvis.permissions.models import PathUse
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult
from jarvis.tools.shell import process_result, run_in_context

C = Capability
IS_WINDOWS = sys.platform == "win32"


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


class RunPythonTool(Tool):
    name = "run_python"
    description = ("Run Python code or a .py file and return stdout/stderr. Uses the project's virtual "
                   "environment when the folder has one.")
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
        if params.file:
            paths.append(PathUse(path=str(ctx.resolve(params.file)), access="read"))
        risky = re.search(r"\b(shutil\.rmtree|os\.remove|os\.unlink|subprocess|os\.system|winreg|ctypes|requests\.post|"
                          r"httpx\.post|urllib\.request|socket\.)", params.code)
        return ActionPlan(summary=f"Run Python {'file ' + Path(params.file).name if params.file else 'code'}",
                          paths=paths, risk=RiskLevel.HIGH if risky else None,
                          risk_reasons=[f"code uses {risky.group(1)}"] if risky else [],
                          details={"code": params.code[:2000]} if params.code else {"file": params.file})

    async def run(self, params: RunPythonParams, ctx: ToolContext) -> ToolResult:
        if not params.code and not params.file:
            return ToolResult.fail("Give code or a file to run.")
        cwd = self._cwd(params, ctx)
        python = venv_python(project_root(cwd)) or find_python()
        if python is None:
            return ToolResult.fail("Python is not installed on this computer (install it from python.org).")
        if params.file:
            target = ctx.resolve(params.file)
            if not target.is_file():
                return ToolResult.fail(f"File not found: {target}")
            argv = [*python, "-X", "utf8", str(target), *params.args]
        else:
            argv = [*python, "-X", "utf8", "-c", params.code, *params.args]
        res = await run_in_context(argv, ctx, cwd=cwd, timeout=params.timeout_s,
                                   stdin=params.stdin.encode() if params.stdin else None)
        return process_result(res, "Python")


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


class RunTestsTool(Tool):
    name = "run_tests"
    description = "Run a project's automated tests (pytest or npm test, detected automatically)."
    capabilities = (C.CODE_EXECUTE,)
    Params = RunTestsParams
    risk = RiskLevel.MEDIUM
    timeout_s = 3700.0

    async def plan(self, params: RunTestsParams, ctx: ToolContext) -> ActionPlan:
        root = ctx.resolve(params.path)
        return ActionPlan(summary=f"Run tests in {root.name}", paths=[PathUse(path=str(root), access="read")])

    async def run(self, params: RunTestsParams, ctx: ToolContext) -> ToolResult:
        root = project_root(ctx.resolve(params.path))
        if params.command:
            from jarvis.tools.shell import shell_argv

            argv, label = shell_argv(params.command, "auto", ctx.services.host), params.command
            if analyze_command(params.command).risk > RiskLevel.MEDIUM:
                return ToolResult.fail("That test command looks unsafe; use run_command so it can be reviewed.",
                                       denied="permission")
        else:
            detected = await asyncio.to_thread(detect_test_command, root)
            if detected is None:
                return ToolResult.fail(f"No tests found in {root} (no pytest tests or npm test script).")
            argv, label = detected
        res = await run_in_context(argv, ctx, cwd=root, timeout=params.timeout_s)
        result = process_result(res, f"Tests ({label})")
        out = (res.stdout or "") + "\n" + (res.stderr or "")
        counts = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|error|errors|skipped)", out)}
        result.data = {**(result.data or {}), "counts": counts, "runner": label}
        if counts:
            result.summary = f"Tests ({label}): " + ", ".join(f"{v} {k}" for k, v in counts.items())
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
        return ActionPlan(summary=f"Install {what} with {params.manager} in {root.name}",
                          paths=[PathUse(path=str(root), access="write")],
                          details={"manager": params.manager, "packages": params.packages, "path": str(root)})

    async def run(self, params: InstallParams, ctx: ToolContext) -> ToolResult:
        bad = [p for p in params.packages if not _PKG_RE.match(p) or p.startswith("-")]
        if bad:
            return ToolResult.fail(f"Invalid package name(s): {', '.join(bad)}")
        root = ctx.resolve(params.path)
        if not root.is_dir():
            return ToolResult.fail(f"Project folder not found: {root}")
        if params.manager == "npm":
            npm = shutil.which("npm.cmd" if IS_WINDOWS else "npm")
            if not npm:
                return ToolResult.fail("Node.js/npm is not installed (install it from nodejs.org).")
            argv = [npm, "install", *(["--save-dev"] if params.dev and params.packages else []), *params.packages]
            res = await run_in_context(argv, ctx, cwd=root, timeout=1800)
            return process_result(res, "npm install")
        python = venv_python(root)
        created: list[str] = []
        if python is None:
            base = find_python()
            if base is None:
                return ToolResult.fail("Python is not installed on this computer.")
            res = await run_in_context([*base, "-m", "venv", str(root / ".venv")], ctx, cwd=root, timeout=300)
            if not res.ok:
                return process_result(res, "Creating the virtual environment")
            created.append(str(root / ".venv"))
            python = venv_python(root)
            if python is None:
                return ToolResult.fail("The virtual environment was created but has no Python executable.")
        if params.packages:
            argv = [*python, "-m", "pip", "install", "--disable-pip-version-check", *params.packages]
        elif (root / "requirements.txt").is_file():
            argv = [*python, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"]
        elif (root / "pyproject.toml").is_file():
            argv = [*python, "-m", "pip", "install", "--disable-pip-version-check", "-e", "."]
        else:
            return ToolResult.fail("Nothing to install: no packages given and no requirements file.")
        res = await run_in_context(argv, ctx, cwd=root, timeout=1800)
        result = process_result(res, "pip install")
        result.files_created.extend(created)
        return result


# ------------------------------------------------------------------------------------------ git
class GitParams(BaseModel):
    args: list[str] = Field(..., min_length=1, description="git arguments, e.g. ['status'] or ['commit', '-m', 'msg']")
    path: str = Field(..., description="Repository folder")
    timeout_s: float = Field(120, ge=1, le=1800)


class GitTool(Tool):
    name = "git"
    description = "Run a git command in a repository (init, status, add, commit, log, diff, branch, push, ...)."
    capabilities = (C.GIT,)
    Params = GitParams
    timeout_s = 1900.0

    async def plan(self, params: GitParams, ctx: ToolContext) -> ActionPlan:
        root = ctx.resolve(params.path)
        line = "git " + " ".join(params.args)
        a = analyze_command(line)
        write_ops = {"init", "add", "commit", "checkout", "switch", "merge", "rebase", "reset", "clean", "stash", "pull",
                     "push", "rm", "mv", "restore", "tag", "branch", "clone", "fetch", "cherry-pick", "revert"}
        writes = params.args[0] in write_ops
        risk = a.risk if a.risk > RiskLevel.MEDIUM else (RiskLevel.LOW if writes else RiskLevel.SAFE)
        return ActionPlan(summary=line[:200], paths=[PathUse(path=str(root), access="write" if writes else "read")],
                          risk=risk, risk_reasons=[r for r in a.reasons if r != "runs a shell command"],
                          outbound=None, destination="remote" if params.args[0] in ("push", "pull", "fetch", "clone")
                          else None)

    async def run(self, params: GitParams, ctx: ToolContext) -> ToolResult:
        git = shutil.which("git")
        if git is None:
            return ToolResult.fail("Git is not installed (install it from git-scm.com).")
        root = ctx.resolve(params.path)
        if params.args[0] not in ("init", "clone") and not root.is_dir():
            return ToolResult.fail(f"Folder not found: {root}")
        if params.args[0] == "init":
            await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        env = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat"}
        res = await run_in_context([git, "-c", "core.quotepath=off", *params.args], ctx, cwd=root,
                                   timeout=params.timeout_s, extra_env=env)
        return process_result(res, "git " + params.args[0])


DEV_TOOLS: tuple[type[Tool], ...] = (RunPythonTool, RunTestsTool, InstallPackagesTool, GitTool)

__all__ = ["DEV_TOOLS", "detect_test_command", "find_python", "project_root", "venv_python"]
