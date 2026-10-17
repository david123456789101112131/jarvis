"""System Agent: files and folders, apps and processes, PowerShell, settings, automation and
system health. Its skills are the fast path for simple commands ("open Chrome", "volume 30")."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jarvis.agents.base import AgentResult, AgentTask, BaseAgent, RunContext, skill, t
from jarvis.agents.common import dependency_value, launch_project, project_details, resolve_folder
from jarvis.common.enums import AgentId, ResultStatus
from jarvis.common.i18n import plural
from jarvis.core.intents import I, place_from_text
from jarvis.tools.base import ToolResult

_ABSOLUTE = re.compile(r"^(?:[a-z]:[\\/]|\\\\|/|~)", re.I)

FOLDER_NAMES_HE = {
    "desktop": "שולחן העבודה", "documents": "המסמכים", "downloads": "ההורדות", "pictures": "התמונות",
    "music": "המוזיקה", "videos": "הסרטונים", "home": "תיקיית הבית", "workspace": "סביבת העבודה",
    "projects": "הפרויקטים",
}
FOLDER_NAMES_EN = {
    "desktop": "the Desktop", "documents": "Documents", "downloads": "Downloads", "pictures": "Pictures",
    "music": "Music", "videos": "Videos", "home": "your home folder", "workspace": "the workspace",
    "projects": "the projects folder",
}


def folder_label(folder: str, lang: str) -> str:
    """A folder key ("desktop") in words for the user; a path stays as it is."""
    return (FOLDER_NAMES_HE if lang == "he" else FOLDER_NAMES_EN).get(folder, folder)


def fail(run: RunContext, res: ToolResult, he: str, en: str, **kw: Any) -> AgentResult:
    """A failed result whose summary is in the user's language with the tool's reason."""
    reason = res.error or res.summary or ""
    lang = run.lang
    if res.denied == "confirmation":
        summary = t(lang, f"{he}: לא אושר.", f"{en}: not approved.")
    elif res.denied:
        bare = re.sub(r"^(?:אין הרשאה|Not allowed):\s*", "", reason)   # the executor's own prefix
        summary = t(lang, f"{he}: אין הרשאה ({bare}).", f"{en}: not allowed ({bare}).")
    else:
        summary = t(lang, f"{he}: {reason}", f"{en}: {reason}")
    return run.result(status=ResultStatus.FAILED, progress=0.0, summary=summary, errors=[reason] if reason else [],
                      data={"denied": res.denied, **(res.data if isinstance(res.data, dict) else {})}, **kw)


def status_report(stats: dict[str, Any], lang: str, focus: str = "all") -> str:
    cpu, mem = stats["cpu"], stats["memory"]
    disks = stats.get("disks") or []
    net = stats.get("network") or {}
    bat = stats.get("battery")
    if lang == "he":
        parts = {
            "cpu": f"המעבד עובד ב-{cpu['percent']:.0f}% ({cpu['cores']} ליבות)",
            "memory": f"הזיכרון בשימוש {mem['percent']:.0f}% ({mem['used_gb']} מתוך {mem['total_gb']} GB)",
            "disk": "דיסקים: " + ", ".join(f"{d['mount']} {d['percent']:.0f}% מלא ({d.get('free_gb', '?')} GB פנויים)"
                                          for d in disks),
            "network": f"רשת: העלאה {net.get('up_kbps', 0):.0f} ‏kbps, הורדה {net.get('down_kbps', 0):.0f} ‏kbps",
            "battery": (f"סוללה {bat['percent']}%{' בטעינה' if bat['plugged'] else ''}" if bat else "אין סוללה"),
        }
    else:
        parts = {
            "cpu": f"CPU at {cpu['percent']:.0f}% ({cpu['cores']} cores)",
            "memory": f"memory {mem['percent']:.0f}% used ({mem['used_gb']} of {mem['total_gb']} GB)",
            "disk": "disks: " + ", ".join(f"{d['mount']} {d['percent']:.0f}% full ({d.get('free_gb', '?')} GB free)"
                                         for d in disks),
            "network": f"network up {net.get('up_kbps', 0):.0f} kbps, down {net.get('down_kbps', 0):.0f} kbps",
            "battery": (f"battery {bat['percent']}%{' (charging)' if bat['plugged'] else ''}" if bat else "no battery"),
        }
    if focus in parts:
        return parts[focus] + "."
    text = "; ".join(parts[k] for k in ("cpu", "memory", "disk") if parts[k]) + "."
    return text[0].upper() + text[1:] if lang != "he" else text


def findings_report(findings: list[dict[str, str]], stats: dict[str, Any], top: list[dict[str, Any]], lang: str) -> str:
    if not findings:
        return t(lang, "המחשב במצב טוב: אין עומס חריג, יש מספיק זיכרון ומקום בדיסק.",
                 "Your computer looks healthy: no unusual load, enough memory and disk space.")
    warnings = sum(1 for f in findings if f.get("level") == "warning")
    notes = len(findings) - warnings
    if lang != "he":
        head = (f"{plural(lang, warnings, '', '', 'warning', 'warnings')}"
                + (f" and {plural(lang, notes, '', '', 'note', 'notes')}" if notes else "") + ":"
                if warnings else "A few notes:")
        return head + " " + " ".join(f["message"] for f in findings)
    areas = {f.get("area") for f in findings}
    lines: list[str] = []
    if "cpu" in areas:
        lines.append(f"המעבד עמוס מאוד ({stats['cpu']['percent']:.0f}%).")
    if "memory" in areas:
        lines.append(f"הזיכרון בשימוש גבוה ({stats['memory']['percent']:.0f}%).")
    if "disk" in areas:
        for d in stats.get("disks") or []:
            if d["percent"] >= 85 or d.get("free_gb", 99) < 5:
                lines.append(f"הכונן {d['mount']} מלא ב-{d['percent']:.0f}% ({d.get('free_gb', '?')} GB פנויים).")
    if "battery" in areas:
        lines.append(f"הסוללה חלשה ({(stats.get('battery') or {}).get('percent', '?')}%).")
    if "process" in areas:
        for proc in top[:3]:
            if proc.get("cpu", 0) >= 50:
                lines.append(f"{proc['name']} משתמש ב-{proc['cpu']:.0f}% מהמעבד.")
            if proc.get("memory_mb", 0) >= 4000:
                lines.append(f"{proc['name']} משתמש ב-{proc['memory_mb'] / 1024:.1f} GB זיכרון.")
    if "uptime" in areas:
        days = stats["uptime_s"] // 86400
        lines.append(f"המחשב פועל כבר {'יום אחד' if days == 1 else f'{days} ימים'}; הפעלה מחדש יכולה לעזור.")
    if warnings:
        found = "אזהרה אחת" if warnings == 1 else f"{warnings} אזהרות"
        found += "" if not notes else " והערה אחת" if notes == 1 else f" ו-{notes} הערות"
        head = ("נמצאה " if warnings == 1 and not notes else "נמצאו ") + found + ":"
    else:
        head = "כמה הערות:"
    return head + " " + " ".join(lines)


class SystemAgent(BaseAgent):
    id = AgentId.SYSTEM
    persona = (
        "Your job: operate the user's Windows computer safely. You manage files and folders, open and close "
        "programs, manage processes, run PowerShell commands, change settings (volume, brightness), automate "
        "repetitive actions and check system health. Prefer the dedicated tools over shell commands; use "
        "run_command only when no tool fits, and keep commands read-only unless the task requires a change. "
        "Use use_computer only for a task that can be done solely through a program's window (clicking and "
        "typing on screen) when no direct tool or command fits; prefer the direct tools whenever one does. "
        "Never delete, move or overwrite more than the task needs. Report exactly what changed on the computer."
    )
    llm_tools = (
        "list_dir", "read_file", "write_file", "create_folder", "copy_path", "move_path", "delete_path", "find_files",
        "file_info", "open_path", "organize_folder", "open_app", "open_url", "close_app", "running_apps",
        "list_processes", "kill_process", "run_command", "start_process", "system_info", "health_check", "volume",
        "brightness", "media", "lock_screen", "power", "cancel_shutdown", "screenshot", "use_computer", "notify",
        "clipboard", "recall", "remember", "project_info",
    )

    # ------------------------------------------------------------------ apps and sites
    @skill(description="Open an installed app (Hebrew or English name)", intents=(I.OPEN_APP,))
    async def open_app(self, task: AgentTask, run: RunContext, app: str = "", app_name: str = "", name: str = "",
                       args: list[str] | None = None) -> AgentResult:
        target = app or name or app_name or task.instruction
        res = await run.tool("open_app", name=target, args=list(args or []))
        display = app_name or target
        if not res.ok:
            return fail(run, res, f"לא הצלחתי לפתוח את {display}", f"Could not open {display}")
        data = res.data or {}
        display = (data.get("app") or {}).get("name") or display
        return run.result(summary=t(run.lang, f"פתחתי את {display}.", f"Opened {display}."), data=data, result=data)

    @skill(description="Open a website in the default browser", intents=(I.OPEN_WEBSITE,))
    async def open_website(self, task: AgentTask, run: RunContext, url: str = "", site: str = "",
                           search: bool = False) -> AgentResult:
        res = await run.tool("open_url", url=url or site or task.instruction)
        label = site or url
        if not res.ok:
            return fail(run, res, f"לא הצלחתי לפתוח את {label}", f"Could not open {label}")
        text = t(run.lang, f"פתחתי חיפוש של {label} בדפדפן." if search else f"פתחתי את {label}.",
                 f"Opened a search for {label}." if search else f"Opened {label}.")
        return run.result(summary=text, data=res.data, result=res.data)

    @skill(description="Search Google or YouTube in the browser", intents=(I.WEB_SEARCH,))
    async def web_search(self, task: AgentTask, run: RunContext, query: str = "", engine: str = "google",
                         url: str = "") -> AgentResult:
        from urllib.parse import quote_plus

        target = url or (f"https://www.youtube.com/results?search_query={quote_plus(query)}" if engine == "youtube"
                         else f"https://www.google.com/search?q={quote_plus(query or task.instruction)}")
        res = await run.tool("open_url", url=target)
        where = "YouTube" if engine == "youtube" else "Google"
        if not res.ok:
            return fail(run, res, f"לא הצלחתי לחפש ב{where}", f"Could not search {where}")
        return run.result(summary=t(run.lang, f"חיפשתי ב{where}: {query}", f"Searched {where} for: {query}"),
                          data={"url": target})

    @skill(description="Close an app by name", intents=(I.CLOSE_APP,))
    async def close_app(self, task: AgentTask, run: RunContext, app: str = "", app_name: str = "",
                        force: bool = False) -> AgentResult:
        target = app or app_name or task.instruction
        display = app_name or target
        res = await run.tool("close_app", name=target, force=force)
        if not res.ok:
            return fail(run, res, f"לא סגרתי את {display}", f"Did not close {display}")
        return run.result(summary=t(run.lang, f"סגרתי את {display}.", f"Closed {display}."), data=res.data)

    # ------------------------------------------------------------------ files and folders
    @skill(description="Open a folder (desktop, documents, downloads… or a path) in File Explorer",
           intents=(I.OPEN_FOLDER,))
    async def open_folder(self, task: AgentTask, run: RunContext, location: str = "", path: str = "") -> AgentResult:
        target = path or location or "workspace"
        res = await run.tool("open_path", path=target)
        label = folder_label(target, run.lang)
        if not res.ok:
            return fail(run, res, f"לא הצלחתי לפתוח את {label}", f"Could not open {label}")
        return run.result(summary=t(run.lang, f"פתחתי את {label}.", f"Opened {label}."), data=res.data)

    @skill(description="Create a folder", intents=(I.CREATE_FOLDER,))
    async def create_folder(self, task: AgentTask, run: RunContext, name: str | None = None,
                            location: str = "workspace", path: str = "") -> AgentResult:
        folder_name = (name or t(run.lang, "תיקייה חדשה", "New folder")).strip().strip("/\\")
        target = path or f"{location.rstrip('/').rstrip(chr(92))}/{folder_name}"
        res = await run.tool("create_folder", path=target)
        if not res.ok:
            return fail(run, res, f"לא יצרתי את התיקייה {folder_name}", f"Did not create the folder {folder_name}")
        created = (res.data or {}).get("path", target)
        existed = not res.files_created
        where = folder_label(location, run.lang)
        text = t(run.lang,
                 f"התיקייה {folder_name} כבר קיימת ב{where}." if existed else f"יצרתי את התיקייה {folder_name} ב{where}.",
                 f"The folder {folder_name} already exists in {where}." if existed
                 else f"Created the folder {folder_name} in {where}.")
        return run.result(summary=text, data={"path": created}, result={"path": created})

    @skill(description="Delete a file or folder: to the Recycle Bin, or permanently when asked (that always asks first)",
           intents=(I.DELETE_PATH,))
    async def delete_path(self, task: AgentTask, run: RunContext, name: str = "", location: str | None = None,
                          path: str = "", kind: str | None = None, permanent: bool = False) -> AgentResult:
        lang = run.lang
        name = name.strip().strip("\"'")
        if not (path or name):
            return run.result(status=ResultStatus.FAILED, progress=0.0,
                              summary=t(lang, "מה למחוק? צריך לציין שם של קובץ או תיקייה.",
                                        "What should I delete? Name a file or a folder."))
        place = place_from_text(location) if location else None
        full_path = path or (name if _ABSOLUTE.match(name) else "")
        if full_path:
            target, where = full_path, ""
        else:
            folder = place or (location or "").strip() or "workspace"
            target = f"{folder.rstrip('/').rstrip(chr(92))}/{name}"
            where = folder_label(folder, lang)
        label = name or Path(target).name
        info = await run.tool("file_info", path=target)
        if not info.ok:
            if info.denied:
                return fail(run, info, f"לא מחקתי את {label}", f"Did not delete {label}")
            # Not there: ask where it is instead of guessing, unless the user already said.
            ask = not location and not full_path
            he = f"לא מצאתי את {label}" + (f" ב{where}" if where else "") + ("." + (" איפה זה נמצא?" if ask else ""))
            en = f"I could not find {label}" + (f" in {where}" if where else "") + ("." + (" Where is it?" if ask else ""))
            return run.result(status=ResultStatus.FAILED, progress=0.0, summary=t(lang, he, en),
                              data={"code": "need_location" if ask else "not_found", "path": target})
        is_dir = bool((info.data or {}).get("is_dir"))
        if kind and kind != ("folder" if is_dir else "file"):
            # The user described something else than what is there: never delete the wrong thing.
            return run.result(status=ResultStatus.FAILED, progress=0.0, summary=t(
                lang, f"{label} היא תיקייה ולא קובץ, ולכן לא מחקתי אותה." if is_dir
                else f"{label} הוא קובץ ולא תיקייה, ולכן לא מחקתי אותו.",
                f"{label} is a folder, not a file, so I did not delete it." if is_dir
                else f"{label} is a file, not a folder, so I did not delete it."), data={"path": target})
        res = await run.tool("delete_path", paths=[target], permanent=permanent)
        if not res.ok:
            return fail(run, res, f"לא מחקתי את {label}", f"Did not delete {label}")
        what_he = ("התיקייה" if is_dir else "הקובץ") + f" {label}"
        what_en = ("the folder" if is_dir else "the file") + f" {label}"
        gone_for_good = bool((res.data or {}).get("permanent", permanent))
        text = t(lang, f"מחקתי לצמיתות את {what_he}." if gone_for_good else f"העברתי את {what_he} לסל המחזור.",
                 f"Permanently deleted {what_en}." if gone_for_good else f"Moved {what_en} to the Recycle Bin.")
        return run.result(summary=text, data={"path": target, "permanent": gone_for_good})

    @skill(description="Find files by name", intents=(I.FIND_FILES,))
    async def find_files(self, task: AgentTask, run: RunContext, query: str = "", location: str | None = None,
                         extensions: list[str] | None = None, contains: str = "") -> AgentResult:
        name = query.strip()
        exts = list(extensions or [])
        words = name.lower().split()
        known = {"pdf", "docx", "doc", "xlsx", "xls", "pptx", "txt", "csv", "jpg", "png", "mp3", "mp4", "zip", "py"}
        if words and words[0].lstrip(".") in known and (len(words) == 1 or words[1] in ("files", "file", "קבצי", "קבצים")):
            exts.append("." + words[0].lstrip("."))
            name = ""
        res = await run.tool("find_files", root=location or "home", name=name, extensions=exts, contains=contains,
                             max_results=50)
        if not res.ok:
            return fail(run, res, "החיפוש נכשל", "The search failed")
        hits = (res.data or {}).get("results", [])
        if not hits:
            return run.result(status=ResultStatus.PARTIAL, summary=t(run.lang, f"לא מצאתי קבצים שמתאימים ל-{query}.",
                                                                     f"No files matched {query}."), data=res.data)
        names = ", ".join(h.get("name", Path(h.get("path", "")).name) for h in hits[:5])
        found = plural(run.lang, len(hits), "קובץ אחד", "קבצים", "file", "files")
        return run.result(summary=t(run.lang, f"מצאתי {found}: {names}", f"Found {found}: {names}"),
                          data=res.data, result=hits)

    @skill(description="Tidy a folder: sort its loose files into subfolders by type or month")
    async def organize_folder(self, task: AgentTask, run: RunContext, path: str = "downloads", mode: str = "type",
                              dry_run: bool = False, project: str | None = None, location: str | None = None) -> AgentResult:
        hint = path if path != "downloads" or not (project or location) else (project or location or path)
        target = await resolve_folder(self.runtime, hint, default="downloads")
        preview = await run.tool("organize_folder", path=target, mode=mode, dry_run=True, language=run.lang)
        if not preview.ok:
            if (preview.data or {}).get("project"):
                return run.result(status=ResultStatus.PARTIAL, summary=t(
                    run.lang, "זו תיקיית פרויקט תוכנה, ולכן לא העברתי בה קבצים לפי סוג (זה היה שובר את הפרויקט).",
                    "This is a software project folder, so I did not move its files by type (that would break it)."),
                    data=preview.data)
            return fail(run, preview, "לא הצלחתי לארגן את התיקייה", "Could not organize the folder")
        groups = (preview.data or {}).get("groups", {})
        total = sum(len(v) for v in groups.values())
        if total == 0:
            return run.result(summary=t(run.lang, "התיקייה כבר מסודרת: אין קבצים פזורים.",
                                        "The folder is already tidy: no loose files."), data=preview.data)
        if dry_run:
            listing = ", ".join(f"{k} ({len(v)})" for k, v in sorted(groups.items()))
            files = plural(run.lang, total, "קובץ אחד", "קבצים", "file", "files")
            return run.result(summary=t(run.lang, f"אפשר להעביר {files} לתיקיות: {listing}",
                                        f"{files} can move into: {listing}"), data=preview.data)
        res = await run.tool("organize_folder", path=target, mode=mode, dry_run=False, language=run.lang)
        if not res.ok:
            return fail(run, res, "הארגון לא בוצע", "The folder was not organized")
        counts = (res.data or {}).get("groups", {})
        listing = ", ".join(f"{k} ({v})" for k, v in sorted(counts.items()))
        moved = plural(run.lang, res.data.get("moved", 0), "קובץ אחד", "קבצים", "file", "files")
        return run.result(summary=t(run.lang, f"סידרתי {moved}: {listing}",
                                    f"Organized {moved}: {listing}"), data=res.data)

    @skill(description="Open a project so the user can see it: a website in the browser, a server started")
    async def open_project(self, task: AgentTask, run: RunContext, name: str = "", project: str = "",
                           path: str = "") -> AgentResult:
        built = dependency_value(task, "path", key="build")
        folder, meta = await project_details(self.runtime, path or (str(built) if built else "") or name or project
                                             or None)
        if folder is None:
            return AgentResult.failed(t(run.lang, f"לא מצאתי את הפרויקט {name or project or path}.",
                                        f"I could not find the project {name or project or path}."),
                                      data={"code": "project_not_found"})
        return await launch_project(run, folder, meta, allow_code=False)

    # ------------------------------------------------------------------ system state
    @skill(description="System status or a full health check", intents=(I.SYSTEM_STATUS,))
    async def system_status(self, task: AgentTask, run: RunContext, focus: str | None = None) -> AgentResult:
        if focus:
            res = await run.tool("system_info", focus=focus)
            if not res.ok:
                return fail(run, res, "לא הצלחתי לקרוא את מצב המערכת", "Could not read the system status")
            return run.result(summary=status_report(res.data, run.lang, focus), data=res.data, result=res.data)
        res = await run.tool("health_check")
        if not res.ok:
            return fail(run, res, "בדיקת המערכת נכשלה", "The health check failed")
        data = res.data or {}
        stats = data.get("stats") or {}
        text = status_report(stats, run.lang) + " " + findings_report(data.get("findings") or [], stats,
                                                                      data.get("top_processes") or [], run.lang)
        return run.result(summary=text, data=data, result={"findings": data.get("findings"), "stats": stats})

    @skill(description="Show the busiest processes")
    async def list_processes(self, task: AgentTask, run: RunContext, sort: str = "cpu", limit: int = 10) -> AgentResult:
        res = await run.tool("list_processes", sort=sort, limit=limit)
        if not res.ok:
            return fail(run, res, "לא הצלחתי לקרוא את התהליכים", "Could not list processes")
        rows = (res.data or {}).get("processes", [])
        top = ", ".join(f"{r['name']} {r['cpu']}%" for r in rows[:5])
        return run.result(summary=t(run.lang, f"התהליכים הכבדים: {top}", f"Busiest processes: {top}"), data=res.data)

    @skill(description="Change or read the volume", intents=(I.VOLUME,))
    async def volume(self, task: AgentTask, run: RunContext, action: str = "get", level: int | None = None,
                     step: int = 10) -> AgentResult:
        res = await run.tool("volume", action=action, level=level, step=step)
        if not res.ok:
            return fail(run, res, "לא הצלחתי לשנות את עוצמת הקול", "Could not change the volume")
        lvl = (res.data or {}).get("level")
        if action == "mute":
            text = t(run.lang, "השתקתי את הקול.", "Muted.")
        elif action == "unmute":
            text = t(run.lang, "ביטלתי את ההשתקה.", "Unmuted.")
        else:
            text = t(run.lang, f"עוצמת הקול {lvl}%." if lvl is not None else "שיניתי את עוצמת הקול.",
                     f"Volume is {lvl}%." if lvl is not None else "Volume changed.")
        return run.result(summary=text, data=res.data)

    @skill(description="Change or read the screen brightness", intents=(I.BRIGHTNESS,))
    async def brightness(self, task: AgentTask, run: RunContext, action: str = "get", level: int | None = None,
                         step: int = 10) -> AgentResult:
        res = await run.tool("brightness", action=action, level=level, step=step)
        if not res.ok:
            return fail(run, res, "לא הצלחתי לשנות את הבהירות", "Could not change the brightness")
        lvl = (res.data or {}).get("level")
        return run.result(summary=t(run.lang, f"הבהירות {lvl}%." if lvl is not None else "שיניתי את הבהירות.",
                                    f"Brightness is {lvl}%." if lvl is not None else "Brightness changed."),
                          data=res.data)

    @skill(description="Media keys: play/pause, next, previous", intents=(I.MEDIA,))
    async def media(self, task: AgentTask, run: RunContext, action: str = "play_pause") -> AgentResult:
        res = await run.tool("media", action=action)
        if not res.ok:
            return fail(run, res, "פקודת המדיה נכשלה", "The media command failed")
        he = {"next": "עברתי לשיר הבא.", "previous": "חזרתי לשיר הקודם.", "stop": "עצרתי את הניגון."}
        en = {"next": "Next track.", "previous": "Previous track.", "stop": "Stopped playback."}
        return run.result(summary=t(run.lang, he.get(action, "הפעלתי/השהיתי את הניגון."),
                                    en.get(action, "Toggled play/pause.")))

    @skill(description="Lock the computer", intents=(I.LOCK_SCREEN,))
    async def lock_screen(self, task: AgentTask, run: RunContext) -> AgentResult:
        res = await run.tool("lock_screen")
        if not res.ok:
            return fail(run, res, "לא הצלחתי לנעול את המחשב", "Could not lock the computer")
        return run.result(summary=t(run.lang, "נעלתי את המחשב.", "Locked the computer."))

    @skill(description="Shut down, restart, sleep or sign out (always asks first)", intents=(I.POWER,))
    async def power(self, task: AgentTask, run: RunContext, action: str = "shutdown", delay_s: int = 30) -> AgentResult:
        res = await run.tool("power", action=action, delay_s=delay_s)
        he = {"shutdown": "כיבוי", "restart": "הפעלה מחדש", "sleep": "שינה", "logoff": "התנתקות"}
        if not res.ok:
            return fail(run, res, f"{he.get(action, action)} לא בוצע", f"{action.capitalize()} was not done")
        when = delay_s if action in ("shutdown", "restart") and delay_s else 0
        return run.result(summary=t(run.lang, f"{he.get(action, action)} בעוד {when} שניות. אפשר לבטל: 'בטל כיבוי'."
                                    if when else f"מבצע {he.get(action, action)}.",
                                    f"{action.capitalize()} in {when} seconds. Say 'cancel shutdown' to stop it."
                                    if when else f"{action.capitalize()} now."))

    @skill(description="Cancel a scheduled shutdown or restart")
    async def cancel_shutdown(self, task: AgentTask, run: RunContext) -> AgentResult:
        res = await run.tool("cancel_shutdown")
        if not res.ok:
            return fail(run, res, "לא הצלחתי לבטל", "Could not cancel")
        return run.result(summary=t(run.lang, "ביטלתי את הכיבוי המתוכנן.", "Cancelled the scheduled shutdown."))

    @skill(description="Take a screenshot", intents=(I.SCREENSHOT,))
    async def screenshot(self, task: AgentTask, run: RunContext, path: str = "") -> AgentResult:
        res = await run.tool("screenshot", path=path)
        if not res.ok:
            return fail(run, res, "צילום המסך נכשל", "The screenshot failed")
        saved = (res.data or {}).get("path", "")
        return run.result(summary=t(run.lang, f"צילמתי את המסך ושמרתי: {saved}", f"Screenshot saved: {saved}"),
                          data=res.data)

    @skill(description="Control the computer by looking at the screen (mouse and keyboard), for a GUI-only task",
           intents=(I.COMPUTER_TASK,))
    async def use_computer(self, task: AgentTask, run: RunContext, goal: str = "") -> AgentResult:
        goal = (goal or task.instruction or "").strip()
        service = run.runtime.executor.services.extras.get("computer")
        problem = service.problem() if service is not None else "computer control is not available"
        if problem:
            return run.result(
                status=ResultStatus.FAILED, progress=0.0,
                summary=t(run.lang, "שליטה במחשב עדיין לא מותקנת. אפשר להתקין עם: jarvis computer install",
                          "Computer control is not installed yet. Install it with: jarvis computer install"),
                errors=[problem], data={"code": "computer_unavailable", "next_step": "jarvis computer install"})
        res = await run.tool("use_computer", goal=goal)
        if res.ok:
            return run.result(summary=res.summary or t(run.lang, "סיימתי את המשימה על המסך.",
                                                       "Finished the on-screen task."), data=res.data)
        if res.denied == "stopped":
            return run.result(status=ResultStatus.FAILED, progress=0.0, summary=res.summary,
                              data={"code": "user_stopped"})
        return fail(run, res, "לא הצלחתי לבצע את המשימה על המסך", "Could not do the on-screen task")

    @skill(description="Run a PowerShell/shell command and report its output")
    async def run_command(self, task: AgentTask, run: RunContext, command: str = "", shell: str = "auto",
                          cwd: str | None = None, timeout_s: float = 120) -> AgentResult:
        if not command:
            if task.allow_llm and self.ai_available:
                return await self.run_llm(task, run)
            return AgentResult.failed(t(run.lang, "לא צוינה פקודה להרצה.", "No command was given."))
        res = await run.tool("run_command", command=command, shell=shell, cwd=cwd, timeout_s=timeout_s)
        if not res.ok:
            return fail(run, res, "הפקודה נכשלה", "The command failed")
        out = ((res.data or {}).get("stdout") or "").strip()
        return run.result(summary=t(run.lang, "הפקודה רצה בהצלחה.", "The command ran successfully."),
                          details=out[-4000:] or None, data=res.data)


__all__ = ["SystemAgent", "status_report"]
