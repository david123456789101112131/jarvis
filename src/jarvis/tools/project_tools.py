"""Project tools: create a project from a template, list templates, check a project's files
without running it, and start a background program (a dev server) that stays tracked so the
kill switch can stop it."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.common.errors import PermissionDenied
from jarvis.permissions.models import PathUse
from jarvis.permissions.risk import command_scope
from jarvis.security.secrets import sanitized_env
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult
from jarvis.tools.dev import changed_since_check, find_python, venv_python
from jarvis.tools.web import url_payload, url_risk

C = Capability
IS_WINDOWS = sys.platform == "win32"
TemplateKind = Literal["website", "store", "webapp", "game", "api", "script", "bot", "node-api"]


def project_slug(name: str) -> str:
    slug = re.sub(r"[^\w\-]+", "-", name.strip().lower(), flags=re.UNICODE).strip("-")
    return slug[:60] or f"project-{datetime.now():%Y%m%d-%H%M}"


def command_line(argv: list[str]) -> str:
    """A run spec's argv as a command line ({python} and {node} become the plain program names)."""
    parts = []
    for part in argv:
        part = {"{python}": "python", "{node}": "node"}.get(part, part)
        parts.append(f'"{part}"' if " " in part else part)
    return " ".join(parts)


def resolve_argv(argv: list[str], project: Path) -> list[str] | None:
    """Fill the {python} / {node} placeholders of a template's run/test command."""
    out: list[str] = []
    for part in argv:
        if part == "{python}":
            py = venv_python(project) or find_python()
            if py is None:
                return None
            out.extend(py)
        elif part == "{node}":
            node = shutil.which("node")
            if node is None:
                return None
            out.append(node)
        else:
            out.append(part)
    return out


# ------------------------------------------------------------------------------------------ create
PROJECT_KIND_HE = {"website": "אתר", "store": "חנות אונליין", "webapp": "אפליקציית ווב", "game": "משחק",
                   "api": "שרת API", "script": "סקריפט", "bot": "בוט", "node-api": "שרת Node API"}


class CreateProjectParams(BaseModel):
    kind: TemplateKind = Field(..., description="website, store, webapp, game, api, script, bot or node-api")
    name: str = Field(..., description="Project name (also the folder name)")
    description: str = Field("", description="What the project is about; used for texts, colors and demo data")
    language: Literal["he", "en"] = "he"
    path: str = Field("", description="Destination folder (default: projects/<name>)")
    variables: dict[str, Any] = Field(default_factory=dict, description="Template variables to override")
    brand: str = Field("", description="Brand name shown in the project (default: from the description)")
    palette: dict[str, Any] = Field(default_factory=dict,
                                    description="A palette from create_design (its 'palette' data) to use as colors")
    overwrite: bool = False


class CreateProjectTool(Tool):
    name = "create_project"
    description = ("Create a complete, runnable project from a built-in template (website, online store, web app, "
                   "game, Python API, script, bot, Node API) with texts, colors and demo data from the description.")
    capabilities = (C.FS_WRITE,)
    Params = CreateProjectParams
    timeout_s = 300.0

    def _dest(self, params: CreateProjectParams, ctx: ToolContext) -> Path:
        if params.path:
            return ctx.resolve(params.path)
        return ctx.services.paths.projects / project_slug(params.name)

    async def plan(self, params: CreateProjectParams, ctx: ToolContext) -> ActionPlan:
        dest = self._dest(params, ctx)
        replacing = params.overwrite and dest.is_dir() and any(dest.iterdir())
        kind_he = PROJECT_KIND_HE.get(params.kind, params.kind)
        return ActionPlan(summary=ctx.t(f"יצירת פרויקט {kind_he} '{params.name}' ב-{dest}",
                                        f"Create a {params.kind} project '{params.name}' in {dest}"),
                          paths=[PathUse(path=str(dest), access="write")],
                          risk=RiskLevel.HIGH if replacing else None,
                          risk_reasons=[ctx.t("דורס קבצים בתיקייה קיימת", "overwrites files in an existing folder")]
                          if replacing else [])

    async def run(self, params: CreateProjectParams, ctx: ToolContext) -> ToolResult:
        from jarvis.agents.development import scaffold

        dest = self._dest(params, ctx)
        palette = None
        if params.palette:
            from jarvis.creative.palette import Palette

            try:
                palette = Palette.from_dict(params.palette)
            except (KeyError, TypeError, ValueError) as exc:
                return ToolResult.fail(ctx.t(f"פלטת הצבעים לא תקינה: {exc}", f"The palette is not valid: {exc}"))
        variables = await asyncio.to_thread(scaffold.default_variables, params.kind, params.description or params.name,
                                            params.language, palette=palette, brand=params.brand or None)
        variables = {**variables, "title": params.name, **params.variables}
        if params.description:
            variables.setdefault("description", params.description)
        try:
            result = await asyncio.to_thread(scaffold.render_template, params.kind, dest, variables,
                                             overwrite=params.overwrite)
        except (FileExistsError, ValueError, KeyError) as exc:
            return ToolResult.fail(ctx.t(f"לא הצלחתי ליצור את הפרויקט: {exc}", f"Could not create the project: {exc}"))
        info = result.info
        files = [str(p) for p in result.files]
        memory = ctx.services.memory
        run_spec = _as_dict(info.run)
        test_spec = _as_dict(info.test) if info.test else None
        # Exactly what the built-in template generated (no variables from the model): its tests and its own
        # start command may run without asking for as long as nobody changes a file (see CodeTrust).
        trusted = False
        if not params.variables:
            commands = [command_line(info.run.argv)] if info.run.kind != "static" and info.run.argv else []
            trusted = await asyncio.to_thread(ctx.services.code_trust.trust, dest, result.files, commands)
        else:
            ctx.services.code_trust.forget(dest)
        if memory is not None:
            await memory.upsert_project(params.name, path=dest, description=params.description or info.description,
                                        meta={"kind": params.kind, "language": info.language, "entry": info.entry,
                                              "run": run_spec, "test": test_spec})
        return ToolResult(
            summary=ctx.t(f"נוצר פרויקט {PROJECT_KIND_HE.get(params.kind, params.kind)} '{params.name}' עם "
                          f"{ctx.n(len(files), 'קובץ אחד', 'קבצים', 'file', 'files')} ב-{dest}",
                          f"Created {info.name} '{params.name}' with {len(files)} file{'' if len(files) == 1 else 's'} in {dest}"),
            files_created=files, actions=[f"create {params.kind} project {dest}"],
            data={"path": str(dest), "kind": params.kind, "entry": info.entry, "run": run_spec, "test": test_spec,
                  "language": info.language, "files": len(files), "as_generated": trusted},
        )


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return {k: getattr(obj, k) for k in ("argv", "cwd", "url", "kind") if hasattr(obj, k)}


class ListTemplatesParams(BaseModel):
    pass


class ListTemplatesTool(Tool):
    name = "list_templates"
    description = "List the project templates JARVIS can create (kind, description, how it runs and is tested)."
    capabilities = (C.FS_READ,)
    Params = ListTemplatesParams
    read_only = True
    risk = RiskLevel.SAFE

    async def plan(self, params: ListTemplatesParams, ctx: ToolContext) -> ActionPlan:
        return ActionPlan(summary=ctx.t("רשימת תבניות הפרויקטים", "List the project templates"))

    async def run(self, params: ListTemplatesParams, ctx: ToolContext) -> ToolResult:
        from jarvis.agents.development import scaffold

        items: list[dict[str, Any]] = []
        for info in await asyncio.to_thread(scaffold.list_templates):
            items.append({"kind": info.kind, "name": info.name, "description": info.description,
                          "language": info.language, "run": _as_dict(info.run), "test": _as_dict(info.test)})
        return ToolResult(summary=f"{len(items)} templates: " + ", ".join(i["kind"] for i in items), data={"templates": items})


# ------------------------------------------------------------------------------------------ background process
class StartProcessParams(BaseModel):
    command: str = Field(..., description="Command line to start, e.g. 'python -m http.server 8000'")
    cwd: str = Field("", description="Working folder")
    name: str = Field("", description="Short name for the log file")
    wait_s: float = Field(2.5, ge=0.5, le=30, description="How long to watch for an early crash")
    url: str = Field("", description="Address to open in the browser once it runs (optional)")


class StartProcessTool(Tool):
    name = "start_process"
    description = ("Start a long-running program in the background (a dev server, a bot) and keep it running. "
                   "Output goes to a log file. The kill switch stops it.")
    capabilities = (C.SHELL_EXECUTE, C.PROCESS_LAUNCH)
    Params = StartProcessParams
    risk = RiskLevel.MEDIUM
    timeout_s = 60.0

    def _cwd(self, params: StartProcessParams, ctx: ToolContext) -> Path:
        return ctx.resolve(params.cwd) if params.cwd else (ctx.cwd or ctx.services.paths.workspace)

    async def plan(self, params: StartProcessParams, ctx: ToolContext) -> ActionPlan:
        if not ctx.services.settings.security.shell_enabled:
            raise PermissionDenied(ctx.t("הרצת תוכנות מושבתת בהגדרות האבטחה.", "Running programs is disabled in the security settings."))
        cwd = self._cwd(params, ctx)
        # A generated project's own start command, while the project is exactly as generated, is not a
        # command line to vet: it starts known code. Anything else is checked like run_command.
        known = await asyncio.to_thread(ctx.services.code_trust.allows_command, cwd, params.command)
        ctx.extra["trusted_run"] = known
        # The address it opens afterwards is checked like open_url: what is in it goes to that site.
        carried, sends = url_payload(params.url) if params.url else (None, False)
        risk, reasons = url_risk(ctx, params.url) if params.url else (None, [])
        return ActionPlan(summary=ctx.t(f"הפעלה ברקע: {params.command[:160]}", f"Start in the background: {params.command[:160]}"),
                          paths=[PathUse(path=str(cwd), access="read")],
                          capabilities=[C.PROCESS_LAUNCH] if known else None,
                          details={"command": params.command, "cwd": str(cwd), **({"url": params.url} if params.url else {})},
                          destination=params.url or None, outbound=carried, sends_data=sends, risk=risk,
                          risk_reasons=reasons, remember_scope=command_scope(params.command))

    async def run(self, params: StartProcessParams, ctx: ToolContext) -> ToolResult:
        cwd = self._cwd(params, ctx)
        if ctx.extra.get("trusted_run") and not await asyncio.to_thread(
                ctx.services.code_trust.allows_command, cwd, params.command):
            return changed_since_check(ctx)
        prepared = await asyncio.to_thread(_prepare, params, cwd, ctx.services.paths.workspace / "logs")
        if isinstance(prepared, str):
            return ToolResult.fail(prepared)
        argv, exe, log_path = prepared
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        def launch() -> subprocess.Popen[bytes]:
            log_file = log_path.open("wb")
            try:
                return subprocess.Popen([exe or argv[0], *argv[1:]], cwd=str(cwd), env=sanitized_env(),
                                        stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
            finally:
                log_file.close()

        proc = await asyncio.to_thread(launch)
        ctx.services.processes.register(proc.pid, owner=ctx.agent_id, task_id=ctx.task_id)
        deadline = time.monotonic() + params.wait_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            ctx.check_cancelled()
            await asyncio.sleep(0.1)
        tail = _tail(log_path)
        if proc.poll() is not None:
            ctx.services.processes.unregister(proc.pid)
            return ToolResult.fail(ctx.t(f"התוכנה נסגרה מיד (קוד {proc.returncode}): {tail[-500:]}",
                                         f"The program exited right away (code {proc.returncode}): {tail[-500:]}"),
                                   data={"exit_code": proc.returncode, "log": str(log_path), "output": tail})
        if params.url and ctx.services.host is not None:
            await ctx.services.host.open_url(params.url)
        return ToolResult(summary=ctx.t(f"הופעל ברקע (pid {proc.pid})" + (f" בכתובת {params.url}" if params.url else ""),
                                        f"Started (pid {proc.pid})" + (f" at {params.url}" if params.url else "")),
                          actions=[f"start {params.command[:120]} (pid {proc.pid})"],
                          data={"pid": proc.pid, "log": str(log_path), "url": params.url or None, "output": tail})


def _prepare(params: StartProcessParams, cwd: Path, logs: Path) -> tuple[list[str], str | None, Path] | str:
    """Resolve the program and the log file; an error message when it cannot start."""
    if not cwd.is_dir():
        return f"Working folder not found: {cwd}"
    argv = _split(params.command)
    if argv and argv[0] in ("python", "python3", "py"):
        py = venv_python(cwd) or find_python()
        if py:
            argv = [*py, *argv[1:]]
    exe = shutil.which(argv[0]) if argv else None
    if not argv or (exe is None and not Path(argv[0]).exists()):
        return f"Program not found: {argv[0] if argv else params.command}"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{project_slug(params.name or Path(argv[0]).stem)}-{datetime.now():%Y%m%d-%H%M%S}.log"
    return argv, exe, log_path


def _split(command: str) -> list[str]:
    import shlex

    try:
        parts = shlex.split(command, posix=not IS_WINDOWS)
    except ValueError:
        parts = command.split()
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()[-limit:]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


# ------------------------------------------------------------------------------------------ static checks
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache", "dist", "build"}
_REMOTE = ("http:", "https:", "mailto:", "tel:", "data:", "javascript:", "blob:", "//", "#")
_CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []
        self.html_attrs: dict[str, str | None] | None = None
        self.title = False
        self.viewport = False
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html" and self.html_attrs is None:
            self.html_attrs = values
        elif tag == "title":
            self.title = True
        elif tag == "meta" and (values.get("name") or "").lower() == "viewport":
            self.viewport = True
        if values.get("id"):
            self.ids.append(str(values["id"]))
        for name in ("href", "src"):
            ref = values.get(name)
            if ref and not (tag == "a" and name == "src"):
                self.refs.append(ref.strip())
        srcset = values.get("srcset")
        if srcset:
            self.refs.extend(part.strip().split(" ")[0] for part in srcset.split(",") if part.strip())


def _local_target(base: Path, ref: str) -> Path | None:
    if not ref or ref.lower().startswith(_REMOTE) or "{" in ref or "${" in ref:
        return None
    clean = unquote(ref.split("#", 1)[0].split("?", 1)[0])
    if not clean:
        return None
    return (base / clean).resolve() if not clean.startswith("/") else None


def inspect_project_files(root: Path, *, max_files: int = 600) -> dict[str, Any]:
    """Static checks of a project folder: broken local links in HTML/CSS, page basics, invalid JSON."""
    problems: list[dict[str, str]] = []
    pages: list[str] = []
    scripts: list[str] = []
    checked = 0
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            suffix = path.suffix.lower()
            if suffix not in (".html", ".htm", ".css", ".json", ".js", ".mjs", ".cjs"):
                continue
            checked += 1
            if checked > max_files:
                break
            if suffix in (".js", ".mjs", ".cjs"):
                scripts.append(str(path))
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                problems.append({"file": rel, "kind": "unreadable", "detail": str(exc)[:200]})
                continue
            if suffix == ".json":
                try:
                    json.loads(text)
                except json.JSONDecodeError as exc:
                    problems.append({"file": rel, "kind": "invalid_json", "detail": str(exc)[:200]})
                continue
            if suffix == ".css":
                refs = _CSS_URL.findall(text)
            else:
                pages.append(rel)
                parser = _PageParser()
                try:
                    parser.feed(text)
                    parser.close()
                except Exception as exc:  # html.parser is lenient; anything raised here is a real problem
                    problems.append({"file": rel, "kind": "invalid_html", "detail": str(exc)[:200]})
                    continue
                refs = parser.refs
                attrs = parser.html_attrs or {}
                if not attrs.get("lang"):
                    problems.append({"file": rel, "kind": "no_lang", "detail": "<html> has no lang attribute"})
                if not parser.title:
                    problems.append({"file": rel, "kind": "no_title", "detail": "the page has no <title>"})
                if not parser.viewport:
                    problems.append({"file": rel, "kind": "no_viewport",
                                     "detail": "no <meta name=viewport> (the page will not fit phones)"})
            for ref in refs:
                target = _local_target(path.parent, ref)
                if target is not None and not target.exists():
                    problems.append({"file": rel, "kind": "missing", "detail": ref})
    return {"root": str(root), "pages": pages, "scripts": scripts, "checked": min(checked, max_files),
            "problems": problems, "truncated": checked > max_files}


class CheckProjectParams(BaseModel):
    path: str = Field(..., description="Project folder")
    syntax: bool = Field(True, description="Also syntax-check JavaScript files with Node.js (parsed, never run)")


class CheckProjectTool(Tool):
    name = "check_project"
    description = ("Check a project folder without running it: every local link, script, style and image in the "
                   "web pages exists, pages declare language/title/viewport, JSON files are valid, and (with "
                   "Node.js installed) every JavaScript file parses. Use after creating or editing a website.")
    capabilities = (C.FS_READ,)
    Params = CheckProjectParams
    read_only = True
    risk = RiskLevel.SAFE
    timeout_s = 300.0

    async def plan(self, params: CheckProjectParams, ctx: ToolContext) -> ActionPlan:
        root = ctx.resolve(params.path)
        return ActionPlan(summary=ctx.t(f"בדיקת הקבצים של {root.name}", f"Check the files of {root.name}"),
                          paths=[PathUse(path=str(root), access="read")])

    async def run(self, params: CheckProjectParams, ctx: ToolContext) -> ToolResult:
        root = ctx.resolve(params.path)
        if not root.is_dir():
            return ToolResult.fail(ctx.t(f"תיקיית הפרויקט לא נמצאה: {root}", f"Project folder not found: {root}"))
        report = await asyncio.to_thread(inspect_project_files, root)
        node = shutil.which("node") if params.syntax else None
        syntax_checked = 0
        if node and report["scripts"]:
            from jarvis.tools.shell import run_in_context

            for script in report["scripts"][:80]:
                ctx.check_cancelled()
                res = await run_in_context([node, "--check", script], ctx, cwd=root, timeout=30)
                syntax_checked += 1
                if res.exit_code != 0 and not (res.cancelled or res.timed_out):
                    detail = "\n".join(line for line in (res.stderr or res.stdout or "").splitlines()[:6])
                    report["problems"].append({"file": Path(script).relative_to(root).as_posix(),
                                               "kind": "syntax", "detail": detail[:400]})
        report["syntax_checked"] = syntax_checked
        report["node"] = bool(node)
        problems = report["problems"]
        if problems:
            first = "; ".join(f"{p['file']}: {p['kind']} {p['detail'][:80]}" for p in problems[:5])
            summary = ctx.t(f"נמצאו {len(problems)} בעיות ב-{root.name}: {first}",
                            f"Found {len(problems)} problem(s) in {root.name}: {first}")
        else:
            pages, scripts = len(report["pages"]), len(report["scripts"])
            summary = ctx.t(f"אין בעיות ב-{root.name}: {pages} דפים, {scripts} סקריפטים" + (" (התחביר נבדק)" if syntax_checked else ""),
                            f"No problems in {root.name}: {pages} page(s), {scripts} script(s)"
                            + (" (syntax checked)" if syntax_checked else ""))
        return ToolResult(summary=summary, data=report)


PROJECT_TOOLS: tuple[type[Tool], ...] = (CreateProjectTool, ListTemplatesTool, StartProcessTool, CheckProjectTool)
