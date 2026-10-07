"""Folder organizing: move a folder's loose files into subfolders by type or by month.

One tool call covers the whole operation, so the executor sees the real size of the change:
twenty or more files moved counts as a broad change and asks the user first. A move log is
written to JARVIS's data folder so the change can be reviewed or reversed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.permissions.models import PathUse
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult

C = Capability

CATEGORIES: dict[str, tuple[str, set[str]]] = {
    "Images": ("תמונות", {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".heic", ".ico", ".tif", ".tiff",
                          ".raw", ".psd", ".ai"}),
    "Documents": ("מסמכים", {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".epub"}),
    "Spreadsheets": ("גיליונות", {".xls", ".xlsx", ".csv", ".ods", ".tsv"}),
    "Presentations": ("מצגות", {".ppt", ".pptx", ".odp", ".key"}),
    "Archives": ("ארכיונים", {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"}),
    "Installers": ("התקנות", {".exe", ".msi", ".msix", ".appx", ".dmg", ".pkg", ".deb", ".rpm"}),
    "Audio": ("שמע", {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus"}),
    "Video": ("וידאו", {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv"}),
    "Code": ("קוד", {".py", ".js", ".ts", ".html", ".htm", ".css", ".json", ".java", ".c", ".cpp", ".h", ".cs", ".go",
                     ".rs", ".php", ".rb", ".sh", ".ps1", ".bat", ".sql", ".xml", ".yml", ".yaml", ".ipynb"}),
    "Fonts": ("גופנים", {".ttf", ".otf", ".woff", ".woff2"}),
}
OTHER = ("Other", "אחר")
PROJECT_MARKERS = {"package.json", "pyproject.toml", "setup.py", ".git", "pom.xml", "build.gradle", "Cargo.toml",
                   "go.mod", "composer.json", "Gemfile"}
PROJECT_SUFFIXES = (".sln", ".csproj", ".vcxproj")
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


def category_for(path: Path, lang: str) -> str:
    ext = path.suffix.lower()
    for en, (he, exts) in CATEGORIES.items():
        if ext in exts:
            return he if lang == "he" else en
    return OTHER[1] if lang == "he" else OTHER[0]


def looks_like_project(folder: Path) -> bool:
    try:
        names = {p.name for p in folder.iterdir()}
    except OSError:
        return False
    return bool(names & PROJECT_MARKERS) or any(n.lower().endswith(PROJECT_SUFFIXES) for n in names)


def plan_moves(root: Path, mode: str, lang: str, include_hidden: bool = False) -> list[dict[str, str]]:
    moves: list[dict[str, str]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file() or entry.is_symlink():
            continue
        if entry.name.lower() in SKIP_NAMES or (entry.name.startswith(".") and not include_hidden):
            continue
        if entry.suffix.lower() in (".lnk", ".url"):  # shortcuts stay where the user put them
            continue
        if entry.name.endswith((".crdownload", ".part", ".partial", ".tmp")):
            continue  # still downloading
        if mode == "date":
            folder = datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m")
        else:
            folder = category_for(entry, lang)
        moves.append({"source": str(entry), "folder": folder})
    return moves


def _free_name(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    n = 2
    while True:
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


class OrganizeParams(BaseModel):
    path: str = Field(..., description="Folder to tidy, e.g. downloads, desktop or a full path")
    mode: Literal["type", "date"] = Field("type", description="Group files by type (Images, Documents…) or by month")
    dry_run: bool = Field(False, description="Only show what would move")
    language: Literal["he", "en"] = Field("en", description="Language of the new folder names")
    include_hidden: bool = False


class OrganizeFolderTool(Tool):
    name = "organize_folder"
    description = ("Tidy a folder: move its loose files into subfolders by type (Images, Documents, Installers…) "
                   "or by month. Project folders (code) are never rearranged. dry_run previews the moves.")
    capabilities = (C.FS_WRITE, C.FS_DELETE)
    Params = OrganizeParams
    timeout_s = 900.0

    async def plan(self, params: OrganizeParams, ctx: ToolContext) -> ActionPlan:
        root = ctx.resolve(params.path)
        moves: list[dict[str, str]] = []
        if root.is_dir() and not looks_like_project(root):
            moves = await asyncio.to_thread(plan_moves, root, params.mode, params.language, params.include_hidden)
        folders = sorted({m["folder"] for m in moves})
        if params.dry_run:
            return ActionPlan(summary=ctx.t(f"תצוגה מקדימה של סידור {root}", f"Preview organizing {root}"),
                              capabilities=[C.FS_READ], paths=[PathUse(path=str(root), access="read")])
        broad = len(moves) >= ctx.services.settings.security.broad_delete_threshold
        names = ", ".join(folders[:8])
        files, dirs = ctx.n(len(moves), "קובץ אחד", "קבצים", "file", "files"), ctx.n(len(folders), "תיקייה אחת", "תיקיות",
                                                                                         "folder", "folders")
        return ActionPlan(summary=ctx.t(f"סידור {files} ב-{root} לתוך {dirs}: {names}",
                                        f"Move {files} in {root} into {dirs}: {names}"),
                          paths=[PathUse(path=str(root), access="write")], affected_count=len(moves),
                          risk=RiskLevel.HIGH if broad else None,
                          risk_reasons=[ctx.t(f"מעביר {len(moves)} קבצים לתיקיות חדשות",
                                              f"moves {len(moves)} files into new folders")] if broad else [],
                          details={"folders": folders, "examples": [Path(m["source"]).name for m in moves[:10]]})

    async def run(self, params: OrganizeParams, ctx: ToolContext) -> ToolResult:
        root = ctx.resolve(params.path)
        if not root.is_dir():
            return ToolResult.fail(ctx.t(f"התיקייה לא נמצאה: {root}", f"Folder not found: {root}"))
        if await asyncio.to_thread(looks_like_project, root):
            return ToolResult.fail(ctx.t(f"{root} נראית כמו פרויקט תוכנה, ולכן הקבצים שבה לא מסודרים לפי סוג.",
                                         f"{root} looks like a software project; its files are not rearranged by type."),
                                   data={"project": True})
        moves = await asyncio.to_thread(plan_moves, root, params.mode, params.language, params.include_hidden)
        groups: dict[str, list[str]] = {}
        for m in moves:
            groups.setdefault(m["folder"], []).append(Path(m["source"]).name)
        if params.dry_run or not moves:
            files = ctx.n(len(moves), "קובץ אחד", "קבצים", "file", "files")
            dirs = ctx.n(len(groups), "תיקייה אחת", "תיקיות", "folder", "folders")
            summary = (ctx.t(f"{'יועבר' if len(moves) == 1 else 'יועברו'} {files} לתוך {dirs}",
                             f"{files} would move into {dirs}") if moves
                       else ctx.t("אין מה לסדר: אין קבצים פזורים", "Nothing to organize: no loose files"))
            return ToolResult(summary=summary, data={"path": str(root), "groups": groups, "moved": 0,
                                                     "dry_run": params.dry_run})
        done: list[dict[str, str]] = []
        errors: list[str] = []

        def do_moves() -> None:
            for m in moves:
                ctx.check_cancelled()
                src = Path(m["source"])
                dest_dir = root / m["folder"]
                try:
                    dest_dir.mkdir(exist_ok=True)
                    target = _free_name(dest_dir / src.name)
                    shutil.move(str(src), str(target))
                    done.append({"from": str(src), "to": str(target)})
                except OSError as exc:
                    errors.append(f"{src.name}: {exc.strerror or exc}")

        await asyncio.to_thread(do_moves)
        log_file = self._write_log(ctx, root, done)
        counts = {k: len(v) for k, v in groups.items()}
        files = ctx.n(len(done), "קובץ אחד", "קבצים", "file", "files")
        dirs = ctx.n(len(counts), "תיקייה אחת", "תיקיות", "folder", "folders")
        failed = ctx.t(f"; {'אחד נכשל' if len(errors) == 1 else f'{len(errors)} נכשלו'}",
                       f"; {len(errors)} failed") if errors else ""
        summary = ctx.t(f"{'הועבר' if len(done) == 1 else 'הועברו'} {files} לתוך {dirs}{failed}",
                        f"Moved {files} into {dirs}{failed}")
        res = ToolResult(ok=bool(done) or not errors, summary=summary,
                         actions=[f"organize {root}: {', '.join(f'{k} ({v})' for k, v in sorted(counts.items()))}"],
                         files_modified=[str(root)],
                         data={"path": str(root), "moved": len(done), "groups": counts, "errors": errors[:20],
                               "log": str(log_file) if log_file else None})
        if not res.ok:
            res.error = "; ".join(errors[:3])
        return res

    @staticmethod
    def _write_log(ctx: ToolContext, root: Path, done: list[dict[str, Any]]) -> Path | None:
        if not done:
            return None
        folder = ctx.services.paths.data / "organize-logs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}.json"
            path.write_text(json.dumps({"root": str(root), "moves": done}, ensure_ascii=False, indent=1),
                            encoding="utf-8")
            return path
        except OSError:
            return None


ORGANIZE_TOOLS: tuple[type[Tool], ...] = (OrganizeFolderTool,)
