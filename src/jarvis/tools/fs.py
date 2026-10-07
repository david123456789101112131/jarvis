"""File and folder tools.

Paths are resolved per call (special folder names in Hebrew/English, ~, %VARS%, relative to
the task folder) and every path is declared in the plan, so the permission manager checks
folder scopes, protected locations and broad deletes before anything happens. Deletions go
to the Recycle Bin unless a permanent delete is explicitly requested (which always asks).
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.common.errors import ToolError
from jarvis.permissions.models import PathUse
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult

C = Capability
TEXT_LIMIT = 60_000
EXECUTABLE_EXT = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".msi", ".msix", ".scr",
                  ".com", ".lnk", ".reg", ".hta", ".jar", ".appx", ".appxbundle"}
# Opening one of these with its default program runs code or changes the system: scripts, installers,
# shortcuts, control-panel items, disk images, help files that run scripts, search and library links.
RUNS_CODE_EXT = EXECUTABLE_EXT | {
    ".py", ".pyw", ".psm1", ".psd1", ".ps1xml", ".cpl", ".msc", ".scf", ".inf", ".application", ".appref-ms",
    ".settingcontent-ms", ".chm", ".iso", ".img", ".vhd", ".vhdx", ".url", ".website", ".library-ms", ".search-ms",
    ".searchconnector-ms", ".pif", ".wsh", ".msp", ".diagcab", ".xll", ".gadget", ".jnlp", ".mde", ".ade", ".adp",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "$recycle.bin", "system volume information",
             ".cache", ".idea", ".vs", "dist", "build"}


def to_recycle_bin(path: Path) -> None:
    from send2trash import send2trash

    send2trash(str(path))


def keeps_old_version(ctx: ToolContext, target: Path) -> bool:
    """Replacing a file in the user's own folders first moves the old one to the Recycle Bin, so an
    overwrite can always be undone. (JARVIS's workspace and projects are the agents' working area.)"""
    return ctx.services.guard.zone(target) == "user" and ctx.services.settings.security.use_recycle_bin


def overwrite_risk(ctx: ToolContext, targets: list[Path]) -> tuple[RiskLevel | None, list[str], bool]:
    """(risk, reasons, irreversible) for replacing existing files in the user's folders."""
    existing = [t for t in targets if t.exists() and ctx.services.guard.zone(t) == "user"]
    if not existing:
        return None, [], False
    names = ", ".join(t.name for t in existing[:3]) + ("…" if len(existing) > 3 else "")
    if ctx.services.settings.security.use_recycle_bin:
        return None, [ctx.t(f"הגרסה הקודמת של {names} תעבור לסל המחזור", f"the old {names} goes to the Recycle Bin")], False
    return RiskLevel.HIGH, [ctx.t(f"דורס את {names} בלי אפשרות לשחזר (סל המחזור כבוי בהגדרות)",
                                  f"replaces {names} for good (the Recycle Bin is off in the settings)")], True


def program_landing(ctx: ToolContext, src: Path | None, target: Path) -> list[str]:
    """Why to ask when programs, scripts or shortcuts would land in the user's own folders, where a
    double-click runs them (a planted "invoice.pdf.lnk" on the Desktop is a classic trick)."""
    if ctx.services.guard.zone(target) != "user":
        return []
    names: list[str] = []
    if src is not None and src.is_dir():
        for i, (_folder, _dirs, files) in enumerate(os.walk(src)):
            names.extend(n for n in files if Path(n).suffix.lower() in RUNS_CODE_EXT)
            if len(names) > 3 or i >= 500:
                break
    elif target.suffix.lower() in RUNS_CODE_EXT:
        names.append(target.name)
    if not names:
        return []
    shown = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
    return [ctx.t(f"שם בתיקיות שלך קובץ של תוכנה, סקריפט או קיצור דרך ({shown})",
                  f"puts a program, script or shortcut in your folders ({shown})")]


def _fmt_size(n: int | None) -> str:
    if n is None:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # pragma: no cover


def _entry(p: Path, base: Path | None = None) -> dict[str, Any]:
    try:
        st = p.stat()
        is_dir = p.is_dir()
        size = None if is_dir else st.st_size
        modified = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    except OSError:
        is_dir, size, modified = p.is_dir(), None, None
    name = str(p.relative_to(base)) if base else p.name
    return {"name": name, "path": str(p), "is_dir": is_dir, "size": size, "modified": modified}


def count_files(path: Path, cap: int = 100_000) -> int:
    """Number of files under `path` (1 for a file), stopping at `cap`."""
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        return 1
    total = 0
    for _root, _dirs, files in os.walk(path):
        total += len(files) + 1
        if total >= cap:
            return cap
    return total


def decode_text(data: bytes) -> str:
    """Decode text files written by Windows tools: UTF-8 (with/without BOM), UTF-16, Hebrew ANSI."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1255", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def extract_text(path: Path, max_bytes: int = 20_000_000) -> str:
    """Readable text of a file: plain text, .docx, .pdf, .pptx, .xlsx (shared strings)."""
    suffix = path.suffix.lower()
    if path.stat().st_size > max_bytes:
        raise ToolError(f"{path.name} is too large to read ({_fmt_size(path.stat().st_size)}).")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise ToolError("Reading PDF files needs the 'pypdf' package.") from exc
        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages)
    if suffix in (".docx", ".pptx", ".xlsx"):
        with zipfile.ZipFile(path) as zf:
            if suffix == ".docx":
                parts = ["word/document.xml"]
            elif suffix == ".pptx":
                parts = sorted((n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                               key=lambda n: int(re.findall(r"\d+", n)[-1]))
            else:
                parts = [n for n in zf.namelist() if n == "xl/sharedStrings.xml"]
            chunks = []
            for part in parts:
                xml = zf.read(part).decode("utf-8", errors="replace")
                xml = re.sub(r"</w:p>|</a:p>|</si>", "\n", xml)
                chunks.append(re.sub(r"<[^>]+>", "", xml))
            text = "\n".join(chunks)
            return re.sub(r"\n{3,}", "\n\n", text).strip()
    data = path.read_bytes()
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ToolError(f"{path.name} is a binary file, not text.")
    return decode_text(data)


# ------------------------------------------------------------------------------------------ list
class ListDirParams(BaseModel):
    path: str = Field("workspace", description="Folder to list. Also accepts desktop, documents, downloads, ...")
    pattern: str = Field("*", description="Glob filter for names, e.g. *.py")
    recursive: bool = False
    max_entries: int = Field(200, ge=1, le=2000)


class ListDirTool(Tool):
    name = "list_dir"
    description = "List the files and folders in a folder (optionally recursive, with a name filter)."
    capabilities = (C.FS_READ,)
    Params = ListDirParams
    read_only = True
    risk = RiskLevel.SAFE

    async def plan(self, params: ListDirParams, ctx: ToolContext) -> ActionPlan:
        p = ctx.resolve(params.path)
        return ActionPlan(summary=ctx.t(f"הצגת התוכן של {p}", f"List {p}"), paths=[PathUse(path=str(p), access="read")])

    async def run(self, params: ListDirParams, ctx: ToolContext) -> ToolResult:
        root = ctx.resolve(params.path)
        if not root.exists():
            return ToolResult.fail(ctx.t(f"התיקייה לא נמצאה: {root}", f"Folder not found: {root}"))
        if not root.is_dir():
            return ToolResult(summary=ctx.t(f"{root} הוא קובץ", f"{root} is a file"), data={"entries": [_entry(root)]})

        def walk() -> tuple[list[dict[str, Any]], bool]:
            out: list[dict[str, Any]] = []
            if params.recursive:
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS)
                    for name in sorted(dirnames) + sorted(filenames):
                        if fnmatch.fnmatch(name.lower(), params.pattern.lower()):
                            out.append(_entry(Path(dirpath) / name, root))
                            if len(out) >= params.max_entries:
                                return out, True
                return out, False
            items = sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            for item in items:
                if fnmatch.fnmatch(item.name.lower(), params.pattern.lower()):
                    out.append(_entry(item))
                    if len(out) >= params.max_entries:
                        return out, True
            return out, False

        entries, truncated = await asyncio.to_thread(walk)
        dirs = sum(1 for e in entries if e["is_dir"])
        files = len(entries) - dirs
        summary = ctx.t(f"{root}: {dirs} תיקיות, {files} קבצים" + (" (רשימה חלקית)" if truncated else ""),
                        f"{root}: {dirs} folders, {files} files" + (" (truncated)" if truncated else ""))
        return ToolResult(summary=summary, data={"path": str(root), "entries": entries, "truncated": truncated})


# ------------------------------------------------------------------------------------------ read
class ReadFileParams(BaseModel):
    path: str
    offset: int = Field(0, ge=0, description="Character offset to start from (for long files)")
    max_chars: int = Field(20000, ge=100, le=TEXT_LIMIT)


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a text file or the text of a .docx/.pdf/.pptx/.xlsx document."
    capabilities = (C.FS_READ,)
    Params = ReadFileParams
    read_only = True
    risk = RiskLevel.SAFE

    async def plan(self, params: ReadFileParams, ctx: ToolContext) -> ActionPlan:
        p = ctx.resolve(params.path)
        return ActionPlan(summary=ctx.t(f"קריאת {p.name}", f"Read {p.name}"), paths=[PathUse(path=str(p), access="read")])

    async def run(self, params: ReadFileParams, ctx: ToolContext) -> ToolResult:
        p = ctx.resolve(params.path)
        if not p.is_file():
            return ToolResult.fail(ctx.t(f"הקובץ לא נמצא: {p}", f"File not found: {p}"))
        text = await asyncio.to_thread(extract_text, p)
        chunk = text[params.offset: params.offset + params.max_chars]
        remaining = max(0, len(text) - params.offset - len(chunk))
        return ToolResult(summary=ctx.t(f"נקרא הקובץ {p.name} ({len(text)} תווים)", f"Read {p.name} ({len(text)} characters)"),
                          data={"path": str(p), "content": chunk, "total_chars": len(text), "remaining_chars": remaining,
                                "next_offset": params.offset + len(chunk) if remaining else None})


# ------------------------------------------------------------------------------------------ write
class WriteFileParams(BaseModel):
    path: str
    content: str
    mode: Literal["overwrite", "append", "create_only"] = "overwrite"
    encoding: str = "utf-8"


class WriteFileTool(Tool):
    name = "write_file"
    description = ("Create or overwrite a text file (code, markdown, html, csv, json, ...). Parent folders are "
                   "created. mode=append adds to the end; create_only fails if the file exists.")
    capabilities = (C.FS_WRITE,)
    Params = WriteFileParams

    async def plan(self, params: WriteFileParams, ctx: ToolContext) -> ActionPlan:
        p = ctx.resolve(params.path)
        exists = await asyncio.to_thread(p.exists)
        risk, reasons, irreversible = None, [], False
        if params.mode == "append":
            summary = ctx.t(f"הוספת תוכן לקובץ {p.name}", f"Append to {p.name}")
        elif exists:
            summary = ctx.t(f"שכתוב הקובץ {p.name}", f"Overwrite {p.name}")
            risk, reasons, irreversible = await asyncio.to_thread(overwrite_risk, ctx, [p])
        else:
            summary = ctx.t(f"יצירת הקובץ {p.name}", f"Create {p.name}")
        landing = await asyncio.to_thread(program_landing, ctx, None, p)
        if landing:
            risk, reasons = RiskLevel.HIGH, [*reasons, *landing]
        return ActionPlan(summary=summary, paths=[PathUse(path=str(p), access="write")], risk=risk,
                          risk_reasons=reasons, irreversible=irreversible,
                          details={"bytes": len(params.content.encode(params.encoding, errors="replace")),
                                   "exists": exists})

    async def run(self, params: WriteFileParams, ctx: ToolContext) -> ToolResult:
        p = ctx.resolve(params.path)

        def write() -> bool:
            existed = p.exists()
            if existed and params.mode == "create_only":
                raise ToolError(ctx.t(f"{p} כבר קיים", f"{p} already exists"))
            p.parent.mkdir(parents=True, exist_ok=True)
            if params.mode == "append":
                with p.open("a", encoding=params.encoding, newline="") as fh:
                    fh.write(params.content)
            else:
                tmp = p.with_name(p.name + ".jarvis-tmp")
                tmp.write_text(params.content, encoding=params.encoding, newline="")
                if existed and keeps_old_version(ctx, p):
                    to_recycle_bin(p)
                os.replace(tmp, p)
            return existed

        existed = await asyncio.to_thread(write)
        result = ToolResult(summary=ctx.t(f"{'עודכן' if existed else 'נוצר'} הקובץ {p}", f"{'Updated' if existed else 'Created'} {p}"),
                            data={"path": str(p), "chars": len(params.content)})
        (result.files_modified if existed else result.files_created).append(str(p))
        return result


class CreateFolderParams(BaseModel):
    path: str


class CreateFolderTool(Tool):
    name = "create_folder"
    description = "Create a folder (and any missing parent folders)."
    capabilities = (C.FS_WRITE,)
    Params = CreateFolderParams

    async def plan(self, params: CreateFolderParams, ctx: ToolContext) -> ActionPlan:
        p = ctx.resolve(params.path)
        return ActionPlan(summary=ctx.t(f"יצירת התיקייה {p}", f"Create folder {p}"), paths=[PathUse(path=str(p), access="write")])

    async def run(self, params: CreateFolderParams, ctx: ToolContext) -> ToolResult:
        p = ctx.resolve(params.path)
        existed = await asyncio.to_thread(p.exists)
        if existed and not p.is_dir():
            return ToolResult.fail(ctx.t(f"כבר קיים קובץ בשם הזה: {p}", f"A file with that name already exists: {p}"))
        await asyncio.to_thread(p.mkdir, parents=True, exist_ok=True)
        summary = ctx.t(f"{'התיקייה כבר קיימת' if existed else 'נוצרה התיקייה'}: {p}",
                        f"Folder {'already exists' if existed else 'created'}: {p}")
        res = ToolResult(summary=summary, data={"path": str(p)})
        if not existed:
            res.files_created.append(str(p))
        return res


# ------------------------------------------------------------------------------------------ copy / move
class CopyMoveParams(BaseModel):
    source: str
    destination: str
    overwrite: bool = False


class CopyPathTool(Tool):
    name = "copy_path"
    description = "Copy a file or folder to a new location."
    capabilities = (C.FS_READ, C.FS_WRITE)
    Params = CopyMoveParams

    async def plan(self, params: CopyMoveParams, ctx: ToolContext) -> ActionPlan:
        src, dst = ctx.resolve(params.source), ctx.resolve(params.destination)
        n = await asyncio.to_thread(count_files, src)
        risk, reasons, irreversible = None, [], False
        if params.overwrite:
            replaced = await asyncio.to_thread(_replaced_files, src, dst)
            risk, reasons, irreversible = await asyncio.to_thread(overwrite_risk, ctx, replaced)
        target = dst / src.name if dst.is_dir() else dst
        landing = await asyncio.to_thread(program_landing, ctx, src, target)
        if landing:
            risk, reasons = RiskLevel.HIGH, [*reasons, *landing]
        return ActionPlan(summary=ctx.t(f"העתקת {src.name} אל {dst}", f"Copy {src.name} to {dst}"),
                          affected_count=n if params.overwrite else None, risk=risk, risk_reasons=reasons,
                          irreversible=irreversible,
                          paths=[PathUse(path=str(src), access="read"), PathUse(path=str(target), access="write")])

    async def run(self, params: CopyMoveParams, ctx: ToolContext) -> ToolResult:
        src, dst = ctx.resolve(params.source), ctx.resolve(params.destination)

        def copy() -> Path:
            if not src.exists():
                raise ToolError(ctx.t(f"לא נמצא: {src}", f"Not found: {src}"))
            target = dst / src.name if dst.is_dir() else dst
            if target.exists() and not params.overwrite:
                raise ToolError(ctx.t(f"{target} כבר קיים (כדי להחליף אותו צריך overwrite)",
                                      f"{target} already exists (set overwrite to replace it)"))
            target.parent.mkdir(parents=True, exist_ok=True)
            if params.overwrite:
                for old in _replaced_files(src, dst):
                    if keeps_old_version(ctx, old):
                        to_recycle_bin(old)
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=params.overwrite)
            else:
                shutil.copy2(src, target)
            return target

        target = await asyncio.to_thread(copy)
        return ToolResult(summary=ctx.t(f"הועתק {src.name} אל {target}", f"Copied {src.name} to {target}"),
                          files_created=[str(target)],
                          data={"path": str(target)})


class MovePathTool(Tool):
    name = "move_path"
    description = "Move or rename a file or folder."
    capabilities = (C.FS_WRITE, C.FS_DELETE)
    Params = CopyMoveParams

    async def plan(self, params: CopyMoveParams, ctx: ToolContext) -> ActionPlan:
        src, dst = ctx.resolve(params.source), ctx.resolve(params.destination)
        n = await asyncio.to_thread(count_files, src)
        risk, reasons, irreversible = None, [], False
        target = dst / src.name if dst.is_dir() else dst
        if params.overwrite:
            risk, reasons, irreversible = await asyncio.to_thread(overwrite_risk, ctx, [target])
        landing = await asyncio.to_thread(program_landing, ctx, src, target)
        if landing:
            risk, reasons = RiskLevel.HIGH, [*reasons, *landing]
        # The data is relocated, not destroyed: both ends are judged as writes (broad moves still ask).
        return ActionPlan(summary=ctx.t(f"העברת {src} אל {dst}", f"Move {src} to {dst}"), affected_count=n,
                          risk=risk, risk_reasons=reasons, irreversible=irreversible,
                          paths=[PathUse(path=str(src), access="write"), PathUse(path=str(target), access="write")])

    async def run(self, params: CopyMoveParams, ctx: ToolContext) -> ToolResult:
        src, dst = ctx.resolve(params.source), ctx.resolve(params.destination)

        def move() -> Path:
            if not src.exists():
                raise ToolError(ctx.t(f"לא נמצא: {src}", f"Not found: {src}"))
            target = dst / src.name if dst.is_dir() else dst
            if target.exists() and not params.overwrite:
                raise ToolError(ctx.t(f"{target} כבר קיים (כדי להחליף אותו צריך overwrite)",
                                      f"{target} already exists (set overwrite to replace it)"))
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and keeps_old_version(ctx, target):
                to_recycle_bin(target)            # replaced, so the move puts src exactly at target
            return Path(shutil.move(str(src), str(target)))

        target = await asyncio.to_thread(move)
        return ToolResult(summary=ctx.t(f"הועבר {src.name} אל {target}", f"Moved {src.name} to {target}"),
                          files_created=[str(target)],
                          files_deleted=[str(src)], data={"path": str(target)})


def _replaced_files(src: Path, dst: Path) -> list[Path]:
    """The existing files a copy of `src` to `dst` would replace."""
    target = dst / src.name if dst.is_dir() else dst
    if not src.is_dir():
        return [target] if target.is_file() else []
    out: list[Path] = []
    for folder, _dirs, files in os.walk(src):
        for name in files:
            existing = target / Path(folder).relative_to(src) / name
            if existing.is_file():
                out.append(existing)
    return out


# ------------------------------------------------------------------------------------------ delete
class DeleteParams(BaseModel):
    paths: list[str] = Field(..., min_length=1, max_length=200)
    permanent: bool = Field(False, description="Skip the Recycle Bin (always asks the user first)")


class DeletePathTool(Tool):
    name = "delete_path"
    description = ("Delete files or folders. They go to the Recycle Bin so they can be restored; broad deletes and "
                   "permanent deletes ask the user first.")
    capabilities = (C.FS_DELETE,)
    Params = DeleteParams
    risk = RiskLevel.MEDIUM

    async def plan(self, params: DeleteParams, ctx: ToolContext) -> ActionPlan:
        paths = [ctx.resolve(x) for x in params.paths]
        total = sum(await asyncio.gather(*(asyncio.to_thread(count_files, p) for p in paths)))
        permanent = params.permanent or not ctx.services.settings.security.use_recycle_bin
        names = ", ".join(p.name or str(p) for p in paths[:5]) + ("…" if len(paths) > 5 else "")
        return ActionPlan(
            summary=ctx.t(f"{'מחיקה לצמיתות' if permanent else 'העברה לסל המחזור'}: {names}",
                          f"{'Permanently delete' if permanent else 'Move to Recycle Bin'}: {names}")
            + f" ({ctx.n(total, 'פריט אחד', 'פריטים', 'item', 'items')})",
            paths=[PathUse(path=str(p), access="delete") for p in paths],
            affected_count=total,
            risk=RiskLevel.HIGH if permanent else None,
            risk_reasons=[ctx.t("מחיקה לצמיתות (אי אפשר לבטל אותה)", "permanent delete (cannot be undone)")] if permanent else [],
            irreversible=permanent,
            details={"items": total, "permanent": permanent},
        )

    async def run(self, params: DeleteParams, ctx: ToolContext) -> ToolResult:
        paths = [ctx.resolve(x) for x in params.paths]
        permanent = params.permanent or not ctx.services.settings.security.use_recycle_bin

        def delete() -> tuple[list[str], list[str]]:
            done, missing = [], []
            for p in paths:
                ctx.check_cancelled()
                if not p.exists() and not p.is_symlink():
                    missing.append(str(p))
                    continue
                if permanent:
                    if p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                else:
                    from send2trash import send2trash

                    send2trash(str(p))
                done.append(str(p))
            return done, missing

        done, missing = await asyncio.to_thread(delete)
        n = len(done)
        if ctx.lang == "he":
            verb = ("נמחק לצמיתות" if n == 1 else "נמחקו לצמיתות") if permanent else (
                "הועבר לסל המחזור" if n == 1 else "הועברו לסל המחזור")
            summary = ("פריט אחד" if n == 1 else f"{n} פריטים") + f" {verb}" + (
                f"; לא נמצאו: {', '.join(missing)}" if missing else "")
        else:
            where = "deleted permanently" if permanent else "moved to the Recycle Bin"
            summary = f"{n} item(s) {where}" + (f"; not found: {', '.join(missing)}" if missing else "")
        return ToolResult(ok=bool(done) or not missing, summary=summary, files_deleted=done,
                          error=None if done or not missing else summary,
                          data={"missing": missing, "permanent": permanent})


# ------------------------------------------------------------------------------------------ find
class FindFilesParams(BaseModel):
    root: str = Field("home", description="Where to search (folder path or desktop/documents/downloads/...)")
    name: str = Field("", description="Part of the file name or a glob like *.pdf")
    contains: str = Field("", description="Text that must appear inside the file (text files only)")
    extensions: list[str] = Field(default_factory=list, description="e.g. ['.pdf', '.docx']")
    modified_within_days: int | None = None
    max_results: int = Field(50, ge=1, le=500)


class FindFilesTool(Tool):
    name = "find_files"
    description = "Search for files by name, extension, modification date and/or contained text."
    capabilities = (C.FS_READ,)
    Params = FindFilesParams
    read_only = True
    risk = RiskLevel.SAFE
    timeout_s = 120.0

    async def plan(self, params: FindFilesParams, ctx: ToolContext) -> ActionPlan:
        root = ctx.resolve(params.root)
        return ActionPlan(summary=ctx.t(f"חיפוש {params.name or params.contains or 'קבצים'} ב-{root}",
                                        f"Search {root} for {params.name or params.contains or 'files'}"),
                          paths=[PathUse(path=str(root), access="read")])

    async def run(self, params: FindFilesParams, ctx: ToolContext) -> ToolResult:
        root = ctx.resolve(params.root)
        if not root.is_dir():
            return ToolResult.fail(ctx.t(f"התיקייה לא נמצאה: {root}", f"Folder not found: {root}"))
        name = params.name.lower().strip()
        glob = name if any(ch in name for ch in "*?[") else None
        exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in params.extensions}
        since = None
        if params.modified_within_days:
            since = datetime.now().timestamp() - params.modified_within_days * 86400
        needle = params.contains.lower()
        guard = ctx.services.guard

        def search() -> tuple[list[dict[str, Any]], int]:
            hits: list[dict[str, Any]] = []
            scanned = 0
            for dirpath, dirnames, filenames in os.walk(root):
                ctx.check_cancelled()
                dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")
                               and not guard.is_denied(Path(dirpath) / d)]
                for fn in filenames:
                    scanned += 1
                    low = fn.lower()
                    if glob and not fnmatch.fnmatch(low, glob):
                        continue
                    if name and not glob and name not in low:
                        continue
                    p = Path(dirpath) / fn
                    if exts and p.suffix.lower() not in exts:
                        continue
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    if since and st.st_mtime < since:
                        continue
                    if needle:
                        try:
                            if st.st_size > 5_000_000 or needle not in extract_text(p).lower():
                                continue
                        except Exception:
                            continue
                    hits.append(_entry(p))
                    if len(hits) >= params.max_results:
                        return hits, scanned
            return hits, scanned

        hits, scanned = await asyncio.to_thread(search)
        hits.sort(key=lambda e: e.get("modified") or "", reverse=True)
        found = ctx.t("נמצא קובץ אחד" if len(hits) == 1 else f"נמצאו {len(hits)} קבצים",
                      f"Found {len(hits)} file{'' if len(hits) == 1 else 's'}")
        return ToolResult(summary=ctx.t(f"{found} ב-{root}", f"{found} in {root}"), data={"results": hits, "scanned": scanned})


class FileInfoParams(BaseModel):
    path: str


class FileInfoTool(Tool):
    name = "file_info"
    description = "Size, dates and type of a file or folder (folder size is computed)."
    capabilities = (C.FS_READ,)
    Params = FileInfoParams
    read_only = True
    risk = RiskLevel.SAFE

    async def plan(self, params: FileInfoParams, ctx: ToolContext) -> ActionPlan:
        p = ctx.resolve(params.path)
        return ActionPlan(summary=ctx.t(f"בדיקת הפרטים של {p}", f"Inspect {p}"), paths=[PathUse(path=str(p), access="read")])

    async def run(self, params: FileInfoParams, ctx: ToolContext) -> ToolResult:
        p = ctx.resolve(params.path)

        def info() -> dict[str, Any]:
            if not p.exists():
                raise ToolError(ctx.t(f"לא נמצא: {p}", f"Not found: {p}"))
            data = _entry(p)
            if p.is_dir():
                size = files = 0
                for dirpath, _d, fns in os.walk(p):
                    for fn in fns:
                        files += 1
                        try:
                            size += (Path(dirpath) / fn).stat().st_size
                        except OSError:
                            pass
                data.update(size=size, files=files)
            data["size_text"] = _fmt_size(data.get("size"))
            data["type"] = "folder" if data["is_dir"] else (p.suffix.lower() or "file")
            return data

        data = await asyncio.to_thread(info)
        kind = ctx.t("תיקייה", "folder") if data["is_dir"] else (p.suffix.lower() or ctx.t("קובץ", "file"))
        return ToolResult(summary=f"{p.name}: {kind}, {data['size_text']}", data=data)


# ------------------------------------------------------------------------------------------ open
class OpenPathParams(BaseModel):
    path: str
    reveal: bool = Field(False, description="Show the item selected in File Explorer instead of opening it")


class OpenPathTool(Tool):
    name = "open_path"
    description = "Open a file with its default program, open a folder in File Explorer, or reveal an item."
    capabilities = (C.FS_READ, C.PROCESS_LAUNCH)
    Params = OpenPathParams

    async def plan(self, params: OpenPathParams, ctx: ToolContext) -> ActionPlan:
        p = ctx.resolve(params.path)
        runs_code = p.suffix.lower() in RUNS_CODE_EXT and not params.reveal
        return ActionPlan(
            summary=ctx.t(f"{'הצגה בסייר הקבצים' if params.reveal else 'פתיחה'}: {p}", f"{'Show' if params.reveal else 'Open'} {p}"),
            paths=[PathUse(path=str(p), access="read")],
            risk=RiskLevel.HIGH if runs_code else None,
            risk_reasons=[ctx.t("פותח קובץ של תוכנה או סקריפט", "opens a program or script file")] if runs_code else [],
        )

    async def run(self, params: OpenPathParams, ctx: ToolContext) -> ToolResult:
        p = ctx.resolve(params.path)
        host = ctx.services.host
        if host is None:
            return ToolResult.fail(ctx.t("פתיחת קבצים לא זמינה במערכת הזו.", "Opening files is not available on this system."))
        if not await asyncio.to_thread(p.exists):
            return ToolResult.fail(ctx.t(f"לא נמצא: {p}", f"Not found: {p}"))
        if params.reveal:
            await host.reveal(p)
        else:
            await host.open_path(p)
        ctx.services.extras.setdefault("recent_opened", []).append(str(p))
        return ToolResult(summary=ctx.t(f"{'מוצג בסייר הקבצים' if params.reveal else 'נפתח'}: {p.name or p}",
                                        f"{'Showing' if params.reveal else 'Opened'} {p.name or p}"),
                          actions=[f"open {p}"], data={"path": str(p)})


FS_TOOLS: tuple[type[Tool], ...] = (
    ListDirTool, ReadFileTool, WriteFileTool, CreateFolderTool, CopyPathTool, MovePathTool, DeletePathTool,
    FindFilesTool, FileInfoTool, OpenPathTool,
)
