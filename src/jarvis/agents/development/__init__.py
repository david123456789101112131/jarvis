"""Development Agent: builds apps and websites, writes and fixes code, runs tests, Git and APIs.

* `create_project` creates a complete, runnable project from one of the built-in templates
  (website, online store, web app, game, Python API, script, Telegram bot, Node API) with texts,
  colors and demo data from the request; with an AI provider it then adapts the code to the
  request with the file and test tools.
* `verify_project` checks it for real: every page and link, JavaScript syntax, and the project's
  own automated tests (Python dependencies are installed only after the user approves).
* `run_project`, `run_tests` and `git` cover day-to-day work; `write_code` (AI) writes, fixes,
  refactors and reviews code and proves it by running it.
"""

from __future__ import annotations

import getpass
import re
from pathlib import Path
from typing import Any

from jarvis.agents.base import AgentResult, AgentTask, BaseAgent, RunContext, skill, t
from jarvis.agents.common import dependency, dependency_value, launch_project, project_details
from jarvis.agents.planner import template_kind
from jarvis.agents.planner.planning import KIND_NAMES, project_name
from jarvis.common.enums import AgentId, ResultStatus
from jarvis.common.errors import ProviderError, ProviderRefusal
from jarvis.core.intents import I
from jarvis.tools.base import ToolResult
from jarvis.tools.dev import count_text
from jarvis.tools.project_tools import project_slug

WEB_KINDS = ("website", "store", "webapp", "game")
_MISSING_MODULE = re.compile(r"No module named ['\"]?([\w.\-]+)")
_NO_PROGRAM = re.compile(r"Program not found: (\S+)|is not recognized as an internal or external command", re.I)


def _output(res: ToolResult) -> str:
    data = res.data if isinstance(res.data, dict) else {}
    return f"{data.get('stdout') or ''}\n{data.get('stderr') or ''}\n{res.error or ''}"


class DevelopmentAgent(BaseAgent):
    id = AgentId.DEVELOPMENT
    persona = (
        "Your job: build working software. You create apps, websites, online stores, games, APIs, bots and scripts; "
        "write, fix, refactor and review code; run tests; use Git. Start real projects from JARVIS's templates "
        "(create_project) when one fits, then adapt them. Always prove the work: run the tests, run the code or "
        "check the pages (check_project) before saying it works, and fix what fails. Keep code clean, readable and "
        "secure: no hard-coded secrets (use environment variables or .env files that are git-ignored), validate "
        "input, handle errors. Prefer small, focused edits; read a file before changing it. Installing packages "
        "always asks the user first."
    )
    llm_tools = (
        "list_dir", "read_file", "write_file", "create_folder", "copy_path", "move_path", "delete_path", "find_files",
        "file_info", "create_project", "list_templates", "check_project", "run_tests", "run_python",
        "install_packages", "git", "run_command", "start_process", "open_path", "web_search", "fetch_url", "recall",
        "remember", "project_info",
    )

    # ------------------------------------------------------------------ projects
    @skill(description="Create a complete, runnable project (website, store, web app, game, API, bot, script)",
           intents=(I.BUILD_APP,))
    async def create_project(self, task: AgentTask, run: RunContext, kind: str = "", name: str = "",
                             description: str = "", language: str = "", path: str = "",
                             variables: dict[str, Any] | None = None, adapt: bool = True) -> AgentResult:
        lang = task.language
        description = (description or str(task.context.get("goal") or "") or task.instruction).strip()
        template = template_kind(kind or "app")
        name = (name or project_name(description, template, lang)).strip()
        params: dict[str, Any] = {"kind": template, "name": name, "description": description,
                                  "language": language if language in ("he", "en") else lang,
                                  "variables": dict(variables or {})}
        palette = self._design_palette(task)
        if palette:
            params["palette"] = palette
        if path:
            params["path"] = path
        await run.progress(0.1, "creating the project")
        res = await run.tool("create_project", **params)
        number = 2
        while not res.ok and not path and "already exists" in (res.error or "") and number <= 9:
            params["path"] = f"projects/{project_slug(name)}-{number}"
            res = await run.tool("create_project", **params)
            number += 1
        if not res.ok:
            return run.result(status=ResultStatus.FAILED, progress=0.0, summary=t(
                lang, f"לא הצלחתי ליצור את הפרויקט: {res.error}", f"Could not create the project: {res.error}"))
        if number > 2:
            run.errors.clear()  # the "already exists" attempts were resolved by picking a new folder
        data = dict(res.data or {})
        folder = str(data.get("path"))
        kind_he, kind_en = KIND_NAMES.get(template, ("פרויקט", "project"))
        summary = t(lang, f"יצרתי {kind_he} בשם '{name}' עם {data.get('files', 0)} קבצים ב-{folder}.",
                    f"Created a {kind_en.lower()} called '{name}' with {data.get('files', 0)} files in {folder}.")
        result: dict[str, Any] = {"path": folder, "name": name, "kind": template, "entry": data.get("entry"),
                                  "run": data.get("run"), "test": data.get("test"), "files": data.get("files")}
        adapted: AgentResult | None = None
        adapt_error: str | None = None
        if adapt and task.allow_llm and self.ai_available:
            await run.progress(0.4, "adapting the project")
            try:
                adapted = await self.run_llm(task, run, prompt=self._adapt_prompt(task, name, template, folder,
                                                                                  description, res.files_created))
            except ProviderRefusal:
                raise
            except ProviderError as exc:  # the project exists; keep it as the template made it
                adapt_error = exc.message
                await run.say(t(lang, f"התאמת הפרויקט בעזרת מודל ה-AI נכשלה ({exc.message}); הפרויקט נשאר כפי "
                                      "שנוצר מהתבנית.",
                                f"Adapting the project with the AI model failed ({exc.message}); it stays as the "
                                "template made it."), level="warning")
            if adapted is not None and adapted.summary:
                summary += " " + adapted.summary
        await run.progress(1.0)
        status = ResultStatus.SUCCESS
        if adapt_error or (adapted is not None and adapted.status == ResultStatus.FAILED):
            status = ResultStatus.PARTIAL
        if adapt_error:
            summary += t(lang, " ההתאמה לבקשה לא בוצעה כי מודל ה-AI לא היה זמין.",
                         " It was not adapted to the request because the AI model was unavailable.")
        return run.result(
            status=status, summary=summary, details=adapted.details if adapted else None, result=result,
            data={**data, "adapted": adapted is not None}, used_llm=adapted is not None,
            errors=[adapt_error] if adapt_error else [],
            next_step=t(lang, "לבדוק את הפרויקט ולפתוח אותו לצפייה.", "Verify the project and open it."),
        )

    def _design_palette(self, task: AgentTask) -> dict[str, Any] | None:
        dep = dependency(task, key="design") or dependency(task, agent=AgentId.CREATIVE.value)
        if not dep:
            return None
        result = dep.get("result")
        palette = result.get("palette") if isinstance(result, dict) else None
        return palette if isinstance(palette, dict) and palette.get("colors") else None

    def _adapt_prompt(self, task: AgentTask, name: str, kind: str, folder: str, request: str,
                      files: list[str]) -> str:
        rel = [Path(f).relative_to(folder).as_posix() if f.startswith(folder) else f for f in files][:60]
        lang = "Hebrew" if task.language == "he" else "English"
        checks = "check_project and run_tests" if kind in WEB_KINDS else "run_tests"
        parts = [
            f"The {kind} project '{name}' was just created from JARVIS's template in: {folder}",
            "Files:\n" + "\n".join(f"- {r}" for r in rel),
            f"The user's request: {request}",
            "Adapt the project to the request:\n"
            "- Read the main files first (README.md, the entry page or module, and data files such as "
            "data/products.js).\n"
            "- Change texts, sample data, sections and features so they fit the request; add what the user clearly "
            "asked for that is missing. Remove nothing that works.\n"
            "- Keep the file structure, scripts and tests working. Edit with write_file (whole files). Keep the "
            f"content in {lang} and the page direction as it is.\n"
            "- Do not install packages, start servers or delete files.\n"
            f"- When done, run {checks} on {folder} and fix anything that fails.",
        ]
        deps = task.context.get("dependencies") or []
        if deps:
            parts.append("Results from the other agents (plan, research, design):\n"
                         + "\n".join(str(d)[:2500] for d in deps))
        return "\n\n".join(parts)

    @skill(description="Verify a project for real: pages and links, JavaScript syntax and its automated tests",
           intents=(I.TEST_PROJECT,))
    async def verify_project(self, task: AgentTask, run: RunContext, name: str = "", path: str = "",
                             project: str = "", install: bool = True, fix: bool = True) -> AgentResult:
        lang = task.language
        built_path = dependency_value(task, "path", key="build")
        folder, meta = await project_details(self.runtime, path or (str(built_path) if built_path else "") or name
                                             or project or None)
        if folder is None:
            return AgentResult.failed(t(lang, f"לא מצאתי את הפרויקט {name or project or path}.",
                                        f"I could not find the project {name or project or path}."),
                                      data={"code": "project_not_found"})
        report, problems = await self._check(run, folder, meta, install=install)
        fixed: AgentResult | None = None
        if (problems or report["tests"].get("ok") is False) and fix and task.allow_llm and self.ai_available:
            await run.progress(0.6, "fixing problems")
            try:
                fixed = await self.run_llm(task, run, prompt=self._fix_prompt(folder, problems, report))
            except ProviderRefusal:
                raise
            except ProviderError as exc:
                await run.say(t(lang, f"תיקון אוטומטי לא אפשרי כרגע: מודל ה-AI לא זמין ({exc.message}).",
                                f"No automatic fix right now: the AI model is unavailable ({exc.message})."),
                              level="warning")
            else:
                report, problems = await self._check(run, folder, meta, install=False)
                report["fixed_by_ai"] = True
        tests = report["tests"]
        pages = len((report.get("static") or {}).get("pages") or [])
        parts_he, parts_en = [], []
        if report.get("static") is not None:
            parts_he.append(f"{pages} עמודים נבדקו" if pages else "הקבצים נבדקו")
            parts_en.append(f"checked {pages} pages" if pages else "checked the files")
        if tests.get("ok"):
            counts = tests.get("counts") or {}
            passed = counts.get("passed") or counts.get("pass")
            parts_he.append(f"{passed} בדיקות עברו" if passed and passed > 1 else
                            "בדיקה אחת עברה" if passed == 1 else "הבדיקות עברו")
            parts_en.append(f"{passed} test{'s' if passed > 1 else ''} passed" if passed else "tests passed")
        elif tests.get("ok") is False:
            parts_he.append("יש בדיקות שנכשלו")
            parts_en.append("some tests failed")
        elif tests.get("reason"):
            parts_he.append(tests["reason_he"])
            parts_en.append(tests["reason"])
        if problems:
            parts_he.append(f"{len(problems)} בעיות")
            parts_en.append(f"{len(problems)} problem(s)")
        if problems or tests.get("ok") is False:
            status = ResultStatus.FAILED if not fixed else ResultStatus.PARTIAL
        elif tests.get("ok") is None:
            status = ResultStatus.PARTIAL
        else:
            status = ResultStatus.SUCCESS
        if status == ResultStatus.SUCCESS:
            run.errors.clear()  # earlier failures (missing modules before an approved install) were resolved
        summary = t(lang, f"בדקתי את {folder.name}: " + ", ".join(parts_he) + ".",
                    f"Verified {folder.name}: " + ", ".join(parts_en) + ".")
        details = "\n".join(f"- {p['file']}: {p['kind']} {p.get('detail', '')}" for p in problems[:30]) or None
        if tests.get("output") and tests.get("ok") is False:
            details = (details + "\n\n" if details else "") + str(tests["output"])[-3000:]
        return run.result(status=status, summary=summary, details=details, result=report,
                          progress=1.0 if status == ResultStatus.SUCCESS else 0.8,
                          next_step=tests.get("next_step") or (t(lang, "לתקן את הבעיות שנמצאו.", "Fix the problems found.")
                                                               if problems else None),
                          used_llm=fixed is not None)

    async def _check(self, run: RunContext, folder: Path, meta: dict[str, Any], *,
                     install: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        report: dict[str, Any] = {"path": str(folder), "kind": meta.get("kind")}
        problems: list[dict[str, Any]] = []
        await run.progress(0.2, "checking files")
        static = await run.tool("check_project", path=str(folder))
        if static.ok and isinstance(static.data, dict):
            report["static"] = {k: static.data.get(k) for k in ("pages", "checked", "problems", "node",
                                                                "syntax_checked")}
            problems = list(static.data.get("problems") or [])
        await run.progress(0.4, "running tests")
        report["tests"] = await self._run_tests(run, folder, install=install)
        return report, problems

    async def _run_tests(self, run: RunContext, folder: Path, *, install: bool) -> dict[str, Any]:
        lang = run.lang
        res = await run.tool("run_tests", path=str(folder))
        if res.ok:
            return {"ok": True, **{k: (res.data or {}).get(k) for k in ("counts", "runner")}}
        out = _output(res)
        if "No tests found" in (res.error or ""):
            return {"ok": None, "reason": "the project has no automated tests", "reason_he": "אין בפרויקט בדיקות אוטומטיות"}
        if res.denied:
            return {"ok": None, "reason": "running the tests was not allowed", "reason_he": "הרצת הבדיקות לא אושרה"}
        missing_program = _NO_PROGRAM.search(out)
        if missing_program:
            program = (missing_program.group(1) or "").lower()
            if "npm" in program or "node" in program or "npm" in out:
                return {"ok": None, "reason": "Node.js is not installed, so the JavaScript tests could not run",
                        "reason_he": "Node.js לא מותקן ולכן בדיקות ה-JavaScript לא רצו",
                        "next_step": t(lang, "להתקין Node.js (גרסת LTS) מ-nodejs.org ולהריץ שוב את הבדיקה.",
                                       "Install Node.js (LTS) from nodejs.org and verify again.")}
            return {"ok": None, "reason": f"{program} is not installed", "reason_he": f"{program} לא מותקן"}
        missing = _MISSING_MODULE.search(out)
        if missing:
            module = missing.group(1).split(".")[0]
            if not install:
                return {"ok": None, "reason": f"the dependency {module} is not installed",
                        "reason_he": f"התלות {module} לא מותקנת",
                        "next_step": t(lang, "להתקין את התלויות של הפרויקט.", "Install the project's dependencies.")}
            await run.say(t(lang, f"חסרה ספרייה ({module}); מבקש אישור להתקין את התלויות של הפרויקט.",
                            f"A library is missing ({module}); asking to install the project's dependencies."))
            installed = await run.tool("install_packages", manager="pip", path=str(folder))
            if not installed.ok:
                return {"ok": None, "reason": "the dependencies were not installed", "reason_he": "התלויות לא הותקנו",
                        "next_step": t(lang, "לאשר התקנה של התלויות ואז לבדוק שוב.",
                                       "Approve installing the dependencies, then verify again.")}
            return await self._run_tests(run, folder, install=False)
        data = res.data if isinstance(res.data, dict) else {}
        return {"ok": False, "counts": data.get("counts"), "runner": data.get("runner"), "output": out[-4000:]}

    def _fix_prompt(self, folder: Path, problems: list[dict[str, Any]], report: dict[str, Any]) -> str:
        lines = [f"Project folder: {folder}", "The checks found problems. Fix them in the code (read each file first), "
                 "then run check_project and run_tests again to confirm. Do not weaken or delete tests."]
        if problems:
            lines.append("Problems:\n" + "\n".join(f"- {p['file']}: {p['kind']} {p.get('detail', '')}"
                                                   for p in problems[:40]))
        tests = report.get("tests") or {}
        if tests.get("ok") is False:
            lines.append("Test output (end):\n" + str(tests.get("output") or "")[-3000:])
        return "\n\n".join(lines)

    @skill(description="Run (or open) a project: a website in the browser, a server or bot in the background",
           intents=(I.RUN_PROJECT,))
    async def run_project(self, task: AgentTask, run: RunContext, project: str = "", name: str = "", path: str = "",
                          install: bool = True) -> AgentResult:
        lang = task.language
        folder, meta = await project_details(self.runtime, path or project or name or None)
        if folder is None:
            return AgentResult.failed(t(lang, "לא מצאתי פרויקט להפעיל. איזה פרויקט?", "I found no project to run. Which one?"),
                                      data={"code": "project_not_found"})
        result = await launch_project(run, folder, meta, allow_code=True)
        if result.data.get("code") == "missing_modules" and install:
            installed = await run.tool("install_packages", manager="pip", path=str(folder))
            if installed.ok:
                run.errors.clear()
                result = await launch_project(run, folder, meta, allow_code=True)
        if self.runtime.memory is not None and result.ok:
            self.runtime.memory.short.note("last_project", str(meta.get("name") or folder.name))
        return result

    @skill(description="Run a project's automated tests and report the results", intents=(I.RUN_TESTS,))
    async def run_tests(self, task: AgentTask, run: RunContext, path: str = "", project: str = "",
                        name: str = "") -> AgentResult:
        lang = task.language
        folder, _meta = await project_details(self.runtime, path or project or name or None)
        if folder is None:
            return AgentResult.failed(t(lang, "לא מצאתי את הפרויקט.", "I could not find the project."))
        tests = await self._run_tests(run, folder, install=True)
        if tests.get("ok"):
            counts = dict(tests.get("counts") or {})
            passed = counts.pop("passed", 0)
            extra = f" ({count_text(counts, lang)})" if counts else ""
            if passed:
                summary = t(lang, (f"{passed} בדיקות של {folder.name} עברו" if passed > 1
                                   else f"הבדיקה של {folder.name} עברה") + f"{extra}.",
                            f"{passed} test{'s' if passed > 1 else ''} of {folder.name} passed{extra}.")
            else:
                summary = t(lang, f"הבדיקות של {folder.name} עברו{extra}.", f"The tests of {folder.name} passed{extra}.")
            return run.result(summary=summary, result=tests)
        if tests.get("ok") is None:
            return run.result(status=ResultStatus.PARTIAL, progress=0.5,
                              summary=t(lang, f"הבדיקות לא רצו: {tests.get('reason_he')}.",
                                        f"The tests did not run: {tests.get('reason')}."),
                              result=tests, next_step=tests.get("next_step"))
        return run.result(status=ResultStatus.FAILED, progress=0.5,
                          summary=t(lang, f"יש בדיקות שנכשלו ב-{folder.name}.", f"Some tests failed in {folder.name}."),
                          details=str(tests.get("output") or "")[-3000:] or None, result=tests)

    # ------------------------------------------------------------------ code
    @skill(description="Write, fix, refactor or review code (needs an AI model)", intents=(I.WRITE_CODE,),
           needs_llm=True)
    async def write_code(self, task: AgentTask, run: RunContext, request: str = "", language: str | None = None,
                         path: str = "") -> AgentResult:
        extra = ("Unless the user named a project or file, save new code in its own folder under workspace/code/. "
                 "Before reporting, prove the code works: run it (run_python) or its tests (run_tests); for a bug "
                 "fix, reproduce the problem first when you can. Mention the full paths of the files you created or "
                 "changed.")
        if language:
            extra += f" The code language is {language}."
        if path:
            extra += f" Work in: {path}."
        return await self.run_llm(task, run, extra_system=extra)

    @skill(description="Git for a project: status, init, commit (a snapshot of all changes), log or diff")
    async def git(self, task: AgentTask, run: RunContext, action: str = "status", path: str = "", project: str = "",
                  name: str = "", message: str = "") -> AgentResult:
        lang = task.language
        folder, _meta = await project_details(self.runtime, path or project or name or None)
        if folder is None:
            return AgentResult.failed(t(lang, "לא מצאתי את הפרויקט.", "I could not find the project."))
        where = str(folder)
        action = action.lower().strip()
        if action in ("init", "commit", "snapshot", "save"):
            status = await run.tool("git", args=["status", "--porcelain"], path=where)
            if not status.ok and "not a git repository" in _output(status).lower():
                run.errors.clear()
                init = await run.tool("git", args=["init"], path=where)
                if not init.ok:
                    return run.result(status=ResultStatus.FAILED, progress=0.0,
                                      summary=t(lang, f"git init נכשל: {init.error}", f"git init failed: {init.error}"))
                if action == "init":
                    return run.result(summary=t(lang, f"יצרתי מאגר Git ב-{folder.name}.",
                                                f"Created a Git repository in {folder.name}."))
            elif action == "init":
                return run.result(summary=t(lang, f"ב-{folder.name} כבר יש מאגר Git.",
                                            f"{folder.name} already has a Git repository."))
            add = await run.tool("git", args=["add", "-A"], path=where)
            if not add.ok:
                return run.result(status=ResultStatus.FAILED, progress=0.0, summary=f"git add: {add.error}")
            text = message or t(lang, "שמירת מצב על ידי JARVIS", "Snapshot by JARVIS")
            commit = await run.tool("git", args=["commit", "-m", text], path=where)
            out = _output(commit)
            if not commit.ok and ("tell me who you are" in out.lower() or "user.email" in out):
                user = re.sub(r"[^\w.\-]", "", getpass.getuser()) or "jarvis"
                await run.tool("git", args=["config", "user.name", user], path=where)
                await run.tool("git", args=["config", "user.email", f"{user}@localhost"], path=where)
                commit = await run.tool("git", args=["commit", "-m", text], path=where)
                out = _output(commit)
                if commit.ok:
                    run.errors.clear()
            if not commit.ok:
                if "nothing to commit" in out.lower():
                    run.errors.clear()
                    return run.result(summary=t(lang, "אין שינויים לשמור.", "There are no changes to commit."))
                return run.result(status=ResultStatus.FAILED, progress=0.0, summary=f"git commit: {commit.error}")
            return run.result(summary=t(lang, f"שמרתי את השינויים ב-Git: {text}", f"Committed the changes: {text}"),
                              details=out.strip()[-2000:] or None)
        args = {"status": ["status", "--short", "--branch"], "log": ["log", "--oneline", "-15"],
                "diff": ["diff", "--stat"]}.get(action, ["status", "--short", "--branch"])
        res = await run.tool("git", args=args, path=where)
        if not res.ok:
            return run.result(status=ResultStatus.FAILED, progress=0.0, summary=f"git {args[0]}: {res.error}")
        out = ((res.data or {}).get("stdout") or "").strip()
        return run.result(summary=t(lang, f"git {args[0]} ב-{folder.name}:", f"git {args[0]} in {folder.name}:")
                          + (" " + out.splitlines()[0] if out else ""), details=out[-4000:] or None)


__all__ = ["DevelopmentAgent"]
