"""Small helpers shared by the agents: folder hints, projects (finding, opening and starting them)
and the results of earlier steps."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.agents.base import AgentResult, AgentRuntime, AgentTask, RunContext


def slugify(text: str, limit: int = 48, default: str = "item") -> str:
    slug = re.sub(r"[^\w֐-׿]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:limit].rstrip("-") or default


def _pick_project(projects_dir: Path, name: str | None, candidates: list[str]) -> Path | None:
    if name:
        direct = Path(name).expanduser()
        if direct.is_absolute() and direct.is_dir():
            return direct
    for c in candidates:
        if Path(c).is_dir():
            return Path(c)
    if name and projects_dir.is_dir():
        want = slugify(name, 60, name)
        for folder in (projects_dir / name, projects_dir / want):
            if folder.is_dir():
                return folder
        for child in projects_dir.iterdir():
            if child.is_dir() and (child.name.lower() == name.lower() or slugify(child.name, 60) == want):
                return child
    return None


async def find_project(runtime: AgentRuntime, name: str | None) -> Path | None:
    """A project folder by name (memory first, then the projects folder), or the most recent project."""
    memory = runtime.memory
    candidates: list[str] = []
    if memory is not None:
        if name:
            record = await memory.project(name)
            if record is not None and record.path:
                candidates.append(record.path)
        else:
            last = memory.short.get("last_project")
            if last:
                record = await memory.project(str(last))
                if record is not None and record.path:
                    candidates.append(record.path)
            candidates.extend(r.path for r in await memory.projects(limit=5) if r.path)
    return await asyncio.to_thread(_pick_project, runtime.paths.projects, name, candidates)


async def resolve_folder(runtime: AgentRuntime, hint: str | None, default: str = "workspace") -> str:
    """Turn a spoken folder reference into something the file tools understand."""
    from jarvis.core.intents import _location_key

    text = (hint or "").strip().strip("\"'")
    if not text:
        return default
    key = _location_key(text)
    if key:
        return key
    if re.match(r"^(?:[a-zA-Z]:[\\/]|/|~|\\\\)", text):
        return text
    project = await find_project(runtime, re.sub(r"^(?:ה)?(?:פרויקט|project)\s+", "", text, flags=re.I))
    if project is not None:
        return str(project)
    return text


def dependency(task: AgentTask, key: str | None = None, agent: str | None = None) -> dict[str, Any] | None:
    """The result brief of an earlier step (by plan key or agent) passed in the task context."""
    for dep in task.context.get("dependencies") or []:
        if (key is None or dep.get("key") == key) and (agent is None or dep.get("agent") == agent):
            return dep
    return None


def dependency_value(task: AgentTask, field: str, *, key: str | None = None, agent: str | None = None) -> Any:
    dep = dependency(task, key, agent)
    if dep is None:
        return None
    result = dep.get("result")
    if isinstance(result, dict) and field in result:
        return result[field]
    return dep.get(field)


# ------------------------------------------------------------------------------------------ projects
def detect_run_spec(folder: Path) -> dict[str, Any]:
    """How to run a project JARVIS did not create (or whose record was lost), judged from its files."""
    import json

    def has(*names: str) -> bool:
        return any((folder / n).exists() for n in names)

    if has("package.json"):
        try:
            scripts = json.loads((folder / "package.json").read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, ValueError):
            scripts = {}
        if scripts.get("dev"):
            return {"kind": "node-app", "run": {"argv": ["npm", "run", "dev"], "kind": "node", "url": None}}
        if scripts.get("start"):
            return {"kind": "node-app", "run": {"argv": ["npm", "start"], "kind": "node", "url": None}}
        if has("server.js"):
            return {"kind": "node-app", "run": {"argv": ["{node}", "server.js"], "kind": "node", "url": None}}
    if has("manage.py"):
        return {"kind": "django", "run": {"argv": ["{python}", "manage.py", "runserver"], "kind": "python",
                                          "url": "http://127.0.0.1:8000"}}
    if has("app/main.py"):
        return {"kind": "api", "run": {"argv": ["{python}", "-m", "uvicorn", "app.main:app", "--port", "8000"],
                                       "kind": "python", "url": "http://127.0.0.1:8000/docs"}}
    if has("index.html"):
        return {"kind": "website", "entry": "index.html", "run": {"argv": [], "kind": "static", "url": "index.html"}}
    if has("main.py"):
        return {"kind": "script", "entry": "main.py", "run": {"argv": ["{python}", "main.py"], "kind": "python"}}
    return {}


async def project_details(runtime: AgentRuntime, name_or_path: str | None) -> tuple[Path | None, dict[str, Any]]:
    """The folder of a project and what JARVIS knows about running it (kind, entry, run and test commands)."""
    folder = await find_project(runtime, name_or_path)
    if folder is None:
        return None, {}
    meta: dict[str, Any] = {}
    memory = runtime.memory
    if memory is not None:
        record = await memory.project(str(folder))
        if record is None and name_or_path:
            record = await memory.project(name_or_path)
        if record is not None and record.path and Path(record.path) == folder:
            meta = {**record.meta, "name": record.name, "description": record.description}
    if not meta.get("run"):
        meta = {**await asyncio.to_thread(detect_run_spec, folder), **meta}
    meta.setdefault("name", folder.name)
    return folder, meta


def command_line(argv: list[str]) -> str:
    """A run spec's argv as a command line ({python} and {node} become the plain program names)."""
    parts = []
    for part in argv:
        part = {"{python}": "python", "{node}": "node"}.get(part, part)
        parts.append(f'"{part}"' if " " in part else part)
    return " ".join(parts)


_MISSING_MODULE = re.compile(r"No module named ['\"]?([\w.\-]+)")


async def launch_project(run: RunContext, folder: Path, meta: dict[str, Any], *,
                         allow_code: bool = False) -> AgentResult:
    """Open or start a project so the user can see it: a website opens in the browser, a server or a
    bot starts in the background (tracked, so the kill switch stops it), a script runs once."""
    from jarvis.agents.base import t
    from jarvis.common.enums import ResultStatus

    lang = run.lang
    name = str(meta.get("name") or folder.name)
    spec = dict(meta.get("run") or {})
    argv = [str(a) for a in spec.get("argv") or []]
    url = spec.get("url")
    kind = str(meta.get("kind") or "")
    if spec.get("kind", "static") == "static" or not argv:
        entry = url if url and not str(url).startswith(("http:", "https:")) else meta.get("entry") or "index.html"
        res = await run.tool("open_path", path=str(folder / str(entry)))
        if not res.ok:
            res = await run.tool("open_path", path=str(folder))
            if not res.ok:
                return run.result(status=ResultStatus.FAILED, progress=0.0,
                                  summary=t(lang, f"לא הצלחתי לפתוח את {name}: {res.error}",
                                            f"Could not open {name}: {res.error}"), data={"path": str(folder)})
            return run.result(summary=t(lang, f"פתחתי את התיקייה של {name}.", f"Opened the folder of {name}."),
                              data={"path": str(folder)}, result={"opened": str(folder)})
        return run.result(summary=t(lang, f"פתחתי את {name} בדפדפן.", f"Opened {name} in the browser."),
                          data={"path": str(folder), "opened": str(folder / str(entry))},
                          result={"opened": str(folder / str(entry))})
    if kind == "script":
        if not allow_code:
            await run.tool("open_path", path=str(folder))
            return run.result(status=ResultStatus.PARTIAL, summary=t(
                lang, f"{name} הוא סקריפט שורת פקודה; פתחתי את התיקייה שלו. סוכן הפיתוח יכול להריץ אותו.",
                f"{name} is a command-line script; I opened its folder. The Development Agent can run it."),
                data={"path": str(folder)})
        script = str(meta.get("entry") or "main.py")
        args = argv[2:] if len(argv) > 2 else ["--help"]
        res = await run.tool("run_python", file=str(folder / script), args=args, cwd=str(folder), timeout_s=120)
        out = ((res.data or {}).get("stdout") or "").strip() if isinstance(res.data, dict) else ""
        if not res.ok:
            return _launch_failure(run, name, res.error or "", (res.data or {}) if isinstance(res.data, dict) else {},
                                   folder)
        return run.result(summary=t(lang, f"הרצתי את {name}.", f"Ran {name}."), details=out[-4000:] or None,
                          data={"path": str(folder), "output": out[-4000:]})
    command = command_line(argv)
    res = await run.tool("start_process", command=command, cwd=str(folder), name=name, wait_s=4.0,
                         url=str(url) if url and str(url).startswith(("http:", "https:")) else "")
    if not res.ok:
        return _launch_failure(run, name, res.error or "", (res.data or {}) if isinstance(res.data, dict) else {},
                               folder)
    data = res.data or {}
    where = f" {url}" if url else ""
    return run.result(
        summary=t(lang, f"הפעלתי את {name} ברקע{(' ופתחתי את' + where) if url else ''}. אפשר לעצור עם מתג החירום או "
                        "'עצור משימה'.",
                  f"Started {name} in the background{(' and opened' + where) if url else ''}. Stop it with the kill "
                  "switch or 'stop task'."),
        data={"path": str(folder), "pid": data.get("pid"), "log": data.get("log"), "url": url, "command": command},
        result={"pid": data.get("pid"), "url": url, "log": data.get("log")},
    )


def _launch_failure(run: RunContext, name: str, error: str, data: dict[str, Any], folder: Path) -> AgentResult:
    from jarvis.agents.base import t
    from jarvis.common.enums import ResultStatus

    lang = run.lang
    output = f"{data.get('output') or ''}\n{data.get('stderr') or ''}\n{data.get('stdout') or ''}\n{error}"
    missing = _MISSING_MODULE.search(output)
    if missing:
        module = missing.group(1).split(".")[0]
        return run.result(status=ResultStatus.FAILED, progress=0.0, summary=t(
            lang, f"ל-{name} חסרה ספרייה ({module}); צריך להתקין את התלויות של הפרויקט.",
            f"{name} is missing a library ({module}); the project's dependencies need installing."),
            errors=[error[:500]], data={"code": "missing_modules", "module": module, "path": str(folder)},
            next_step=t(lang, "להתקין את התלויות (סוכן הפיתוח יבקש אישור).",
                        "Install the dependencies (the Development Agent will ask first)."))
    if "TELEGRAM_BOT_TOKEN" in output:
        return run.result(status=ResultStatus.PARTIAL, progress=0.5, summary=t(
            lang, f"הבוט {name} מוכן, אבל חסר לו טוקן של Telegram.", f"The bot {name} is ready but has no Telegram token."),
            data={"code": "needs_token", "path": str(folder)},
            next_step=t(lang, "ליצור בוט ב-@BotFather, להעתיק את ‎.env.example ל-‎.env ולהדביק בו את הטוקן.",
                        "Create a bot with @BotFather, copy .env.example to .env and paste the token there."))
    if re.search(r"address already in use|EADDRINUSE|only one usage of each socket address", output, re.I):
        return run.result(status=ResultStatus.FAILED, progress=0.0, summary=t(
            lang, f"הפורט של {name} תפוס: כנראה שהוא (או תוכנה אחרת) כבר רץ.",
            f"{name}'s port is taken: it (or another program) is probably already running."),
            errors=[error[:500]], data={"code": "port_in_use", "path": str(folder)})
    return run.result(status=ResultStatus.FAILED, progress=0.0,
                      summary=t(lang, f"{name} לא עלה: {error[:300]}", f"{name} did not start: {error[:300]}"),
                      errors=[error[:500]], data={"code": "launch_failed", "path": str(folder)})
