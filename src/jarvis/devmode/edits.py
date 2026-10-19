"""A proposed code change: its files, the diff the user reviews, and the checks JARVIS runs before anything
is written.

The AI proposes edits as search/replace pairs (or the whole text of a new file). They are applied here, in
memory, to the files as they are on disk now; nothing is written until the user applies the change
(`dev_apply`, through the tool executor). Each file then carries its unified diff and what JARVIS found
wrong with it:

* problems block applying that file: a `find` that is not in the file (or is there more than once), a
  Python or JSON syntax error, an edit that touches a hidden secret, a path outside the project;
* warnings are shown next to the diff: an import of a library the project does not declare, a rewrite of
  most of a working file, mixed line endings that will be unified.

The text is kept with "\n" line endings; each file remembers its own line ending, byte-order mark and
encoding, so the bytes written keep the project's form, and the original bytes are kept for undo.
"""

from __future__ import annotations

import ast
import base64
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.common.clock import utcnow
from jarvis.common.i18n import t
from jarvis.common.ids import new_id

MASK = "[REDACTED"
BROAD_SHARE = 0.4          # a modify that removes more than this share of a file's lines is a broad rewrite
BROAD_MIN_LINES = 30


# ============================================================================ what the AI returns
class Replacement(BaseModel):
    find: str = Field(description="Exact text copied from the file (whole lines), found exactly once.")
    replace: str = Field(description="The text that replaces it.")


class ProposedEdit(BaseModel):
    path: str = Field(description="The file's path relative to the project folder, with forward slashes.")
    action: Literal["modify", "create"] = Field(description="modify an existing file or create a new one")
    why: str = Field(default="", description="One short sentence: why this file changes.")
    replacements: list[Replacement] = Field(default_factory=list, description="For modify: search/replace pairs.")
    content: str = Field(default="", description="For create: the whole text of the new file.")


class TestAdvice(BaseModel):
    added: bool = Field(default=False, description="True when the edits add or update tests.")
    command: str = Field(default="", description="The project's own command that runs the tests, if known.")
    notes: str = Field(default="", description="What the tests check, or why none were added.")


class Proposal(BaseModel):
    understood: str = Field(description="What you understood the user wants, in one or two sentences.")
    plan: list[str] = Field(description="The short plan: 2-6 steps.")
    assumptions: list[str] = Field(default_factory=list, description="What you assumed where the request was unclear.")
    questions: list[str] = Field(default_factory=list, description="What you need from the user, if anything.")
    edits: list[ProposedEdit] = Field(default_factory=list)
    tests: TestAdvice = Field(default_factory=TestAdvice)
    summary: str = Field(description="What changes, in which files, in a few sentences.")
    risks: list[str] = Field(default_factory=list, description="What could break, or what still needs checking.")


# ============================================================================ the change JARVIS keeps
class Note(BaseModel):
    """One thing JARVIS found about a file: `code` says what kind (the screen shows `text`)."""

    code: str
    text: str


FileStatus = Literal["proposed", "applied", "undone", "conflict"]
ChangeStatus = Literal["proposed", "applied", "partial", "undone", "discarded"]


class FileChange(BaseModel):
    path: str
    action: Literal["modify", "create"]
    why: str = ""
    old: str | None = None            # the text on disk when proposed ("\n" line endings); None: a new file
    new: str = ""
    old_sha: str | None = None        # sha256 of the bytes on disk when proposed
    old_raw: str | None = None        # those bytes (base64), written back by undo
    eol: str = "\n"
    bom: bool = False
    encoding: str = "utf-8"
    include: bool = True
    edited: bool = False              # the user changed the proposed text
    problems: list[Note] = Field(default_factory=list)
    warnings: list[Note] = Field(default_factory=list)
    added: int = 0
    removed: int = 0
    diff: str = ""
    status: FileStatus = "proposed"
    applied_sha: str | None = None    # sha256 of what was written: undo only replaces exactly that

    def encoded(self) -> bytes:
        """The new text as the bytes to write, in the file's own line ending, encoding and BOM."""
        text = self.new.replace("\n", self.eol) if self.eol != "\n" else self.new
        data = text.encode(self.encoding, errors="strict")
        return (b"\xef\xbb\xbf" + data) if self.bom and self.encoding == "utf-8" else data

    def original(self) -> bytes | None:
        return base64.b64decode(self.old_raw) if self.old_raw is not None else None

    @property
    def blocked(self) -> bool:
        return bool(self.problems)


class Change(BaseModel):
    id: str = Field(default_factory=lambda: new_id("chg"))
    session_id: str
    root: str
    request: str
    lang: str = "he"
    created_at: Any = Field(default_factory=utcnow)
    understood: str = ""
    plan: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    summary: str = ""
    risks: list[str] = Field(default_factory=list)
    tests: TestAdvice = Field(default_factory=TestAdvice)
    files: list[FileChange] = Field(default_factory=list)
    status: ChangeStatus = "proposed"
    provider: str = ""
    model: str = ""
    effort: str = ""
    usage: dict[str, int] = Field(default_factory=dict)
    cost_usd: float | None = None
    sent: list[str] = Field(default_factory=list)       # the files whose text went to the AI
    masked: int = 0                                     # secret-looking values hidden before sending
    parent_id: str | None = None                        # a fix proposed on top of this earlier change
    error_text: str = ""                                # the error the fix was asked for (trimmed)
    applied_at: Any = None
    undone_at: Any = None

    def file(self, path: str) -> FileChange | None:
        return next((f for f in self.files if f.path == path), None)

    def to_apply(self) -> list[FileChange]:
        return [f for f in self.files if f.include and f.status in ("proposed", "conflict", "undone")]

    def to_undo(self) -> list[FileChange]:
        return [f for f in self.files if f.status == "applied"]

    def digest(self, *, undo: bool = False) -> str:
        """What an approval covers: exactly these files with exactly this text (a later edit changes it)."""
        h = hashlib.sha256()
        for f in (self.to_undo() if undo else self.to_apply()):
            h.update(f"{f.path}\0{f.action}\0{f.applied_sha if undo else f.old_sha}\0{f.eol}\0{f.encoding}\0{f.bom}\0".encode())
            h.update(f.new.encode("utf-8", "surrogatepass") if not undo else (f.original() or b"\0new"))
        return h.hexdigest()[:32]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ============================================================================ reading what is on disk
def read_source(path: Path) -> tuple[str, str, bool, str, bytes]:
    """(text with "\n" endings, line ending, BOM, encoding, raw bytes) of a text file on disk."""
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    encoding = "utf-8"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        for candidate in ("cp1255", "cp1252"):
            try:
                text, encoding = body.decode(candidate), candidate
                break
            except UnicodeDecodeError:
                continue
        else:
            text, encoding = body.decode("latin-1"), "latin-1"
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    eol = "\r\n" if crlf and crlf >= lf else "\n"
    return text.replace("\r\n", "\n"), eol, bom, encoding, raw


def mixed_endings(raw: bytes) -> bool:
    crlf = raw.count(b"\r\n")
    return bool(crlf) and crlf != raw.count(b"\n")


# ============================================================================ building the change
def safe_relative(path: str) -> str | None:
    """A project-relative path with forward slashes, or None when it leaves the folder or is not plain."""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    pure = PurePosixPath(p)
    if not p or pure.is_absolute() or re.match(r"^[A-Za-z]:", p) or any(part in ("..", "") for part in pure.parts):
        return None
    if any(ch in p for ch in '<>:"|?*\0'):
        return None
    return pure.as_posix()


def apply_replacements(text: str, replacements: list[Replacement], lang: str) -> tuple[str, list[Note]]:
    """The text after each find → replace, and a problem for each pair that did not apply exactly once."""
    problems: list[Note] = []
    for i, rep in enumerate(replacements, 1):
        find = rep.find.replace("\r\n", "\n")
        replace = rep.replace.replace("\r\n", "\n")
        if MASK in find or MASK in replace:
            problems.append(Note(code="masked", text=t(
                lang, f"שינוי {i} נוגע בערך סודי שהוסתר לפני השליחה; צריך לערוך אותו ידנית",
                f"edit {i} touches a secret that was hidden before sending; edit it by hand")))
            continue
        if not find:
            problems.append(Note(code="empty", text=t(lang, f"שינוי {i} ריק", f"edit {i} is empty")))
            continue
        count = text.count(find)
        if count == 0:
            # A common slip: trailing spaces differ. Look for the same lines ignoring them (only those lines change).
            span = _loose_span(text, find)
            if span is not None:
                text = text[:span[0]] + replace + text[span[1]:]
                continue
            problems.append(Note(code="not_found", text=t(lang, f"שינוי {i}: הקטע להחלפה לא נמצא בקובץ",
                                                          f"edit {i}: the text to replace is not in the file")))
        elif count > 1:
            problems.append(Note(code="ambiguous", text=t(
                lang, f"שינוי {i}: הקטע להחלפה מופיע {count} פעמים, לא ברור איזה",
                f"edit {i}: the text to replace appears {count} times, so which one is unclear")))
        else:
            text = text.replace(find, replace, 1)
    return text, problems


def _loose_span(text: str, find: str) -> tuple[int, int] | None:
    """Where `find`'s lines are in `text` when only trailing spaces differ: (start, end) offsets, or None
    unless they are there exactly once."""
    want = [line.rstrip() for line in find.split("\n")]
    with_newline = find.endswith("\n")
    if with_newline:
        want = want[:-1]
    if not want or not any(want):
        return None
    lines = text.split("\n")
    n = len(want)
    hits = [i for i in range(len(lines) - n + 1) if [line.rstrip() for line in lines[i:i + n]] == want]
    if len(hits) != 1:
        return None
    i = hits[0]
    start = sum(len(line) + 1 for line in lines[:i])
    end = start + sum(len(line) + 1 for line in lines[i:i + n]) - 1        # the end of the last line
    if with_newline:
        if i + n >= len(lines):
            return None
        end += 1
    return start, end


def build_file(root: Path, edit: ProposedEdit, lang: str, facts: dict[str, Any]) -> FileChange:
    """One proposed file: applied in memory to what is on disk now, with its diff and its checks."""
    rel = safe_relative(edit.path)
    if rel is None:
        return FileChange(path=edit.path, action=edit.action, why=edit.why, problems=[Note(code="path", text=t(
            lang, "הנתיב יוצא מתיקיית הפרויקט או לא תקין", "the path leaves the project folder or is not valid"))])
    target = root / rel
    fc = FileChange(path=rel, action=edit.action, why=edit.why)
    if target.is_file():
        try:
            old, eol, bom, encoding, raw = read_source(target)
        except OSError as exc:
            fc.problems.append(Note(code="read", text=t(lang, f"לא הצלחתי לקרוא את הקובץ: {exc}",
                                                        f"could not read the file: {exc}")))
            return fc
        fc.old, fc.eol, fc.bom, fc.encoding = old, eol, bom, encoding
        fc.old_sha, fc.old_raw = sha256(raw), base64.b64encode(raw).decode()
        if mixed_endings(raw):
            fc.warnings.append(Note(code="eol", text=t(lang, "בקובץ יש סיומות שורה מעורבות; הן יאוחדו",
                                                       "the file mixes line endings; they will be unified")))
        if edit.action == "create":
            fc.action = "modify"
            fc.new = edit.content.replace("\r\n", "\n")
            fc.warnings.append(Note(code="exists", text=t(
                lang, "התבקש קובץ חדש אבל הוא כבר קיים: כל התוכן שלו יוחלף",
                "a new file was proposed but it exists: its whole text would be replaced")))
        else:
            fc.new, problems = apply_replacements(old, edit.replacements, lang)
            fc.problems += problems
            if not edit.replacements:
                fc.problems.append(Note(code="empty", text=t(lang, "אין שינויים לקובץ הזה", "no edits for this file")))
    else:
        if edit.action == "modify":
            fc.problems.append(Note(code="missing", text=t(lang, "הקובץ לא קיים בפרויקט",
                                                           "the file does not exist in the project")))
            return fc
        fc.new = edit.content.replace("\r\n", "\n")
        style = facts.get("style", {}) if isinstance(facts, dict) else {}
        fc.eol = "\r\n" if style.get("line_endings") == "CRLF" else "\n"
        if MASK in fc.new:
            fc.problems.append(Note(code="masked", text=t(lang, "התוכן כולל ערך סודי שהוסתר; צריך לכתוב אותו ידנית",
                                                          "the text includes a hidden secret; write that part by hand")))
    refresh(fc, lang, facts)
    return fc


def refresh(fc: FileChange, lang: str, facts: dict[str, Any]) -> None:
    """Recompute the diff, the counts and the content checks after the text changed."""
    fc.diff = unified(fc.path, fc.old, fc.new)
    fc.added, fc.removed = _counts(fc.diff)
    fc.problems = [p for p in fc.problems if p.code not in ("syntax", "encoding")] + check_syntax(fc.path, fc.new, lang)
    try:
        fc.encoded()
    except UnicodeEncodeError:
        fc.problems.append(Note(code="encoding", text=t(
            lang, f"בטקסט החדש יש תווים שאי אפשר לשמור בקידוד של הקובץ ({fc.encoding})",
            f"the new text has characters the file's encoding ({fc.encoding}) cannot hold")))
    fc.warnings = [w for w in fc.warnings if w.code not in ("import", "broad")]
    fc.warnings += check_imports(fc.path, fc.old or "", fc.new, facts, lang)
    if fc.old is not None:
        old_lines = fc.old.count("\n") + 1
        if old_lines >= BROAD_MIN_LINES and fc.removed > old_lines * BROAD_SHARE:
            share = round(100 * fc.removed / old_lines)
            fc.warnings.append(Note(code="broad", text=t(
                lang, f"שכתוב רחב: {share}% מהשורות בקובץ מוחלפות. כדאי לבדוק שזה נחוץ",
                f"a broad rewrite: {share}% of the file's lines are replaced. Check that it is needed")))


def unified(path: str, old: str | None, new: str) -> str:
    a = (old or "").splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    if a and not a[-1].endswith("\n"):
        a[-1] += "\n\\ No newline at end of file\n"
    if b and not b[-1].endswith("\n"):
        b[-1] += "\n\\ No newline at end of file\n"
    return "".join(difflib.unified_diff(a, b, fromfile="/dev/null" if old is None else f"a/{path}",
                                        tofile=f"b/{path}", n=3))


def _counts(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


# ============================================================================ checks
def check_syntax(path: str, text: str, lang: str) -> list[Note]:
    """Python and JSON are parsed (never run); a syntax error blocks applying the file."""
    low = path.lower()
    if low.endswith((".py", ".pyw")):
        try:
            ast.parse(text, filename=path)
        except SyntaxError as exc:
            where = f"{exc.lineno}" if exc.lineno else "?"
            return [Note(code="syntax", text=t(lang, f"שגיאת תחביר בשורה {where}: {exc.msg}",
                                               f"syntax error on line {where}: {exc.msg}"))]
    elif low.endswith(".json") and not low.endswith(("tsconfig.json", "jsconfig.json")):
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return [Note(code="syntax", text=t(lang, f"JSON לא תקין בשורה {exc.lineno}: {exc.msg}",
                                               f"invalid JSON on line {exc.lineno}: {exc.msg}"))]
    return []


# pip name -> the module it installs, where they differ
PIP_MODULES = {
    "pillow": "PIL", "opencv-python": "cv2", "opencv-python-headless": "cv2", "beautifulsoup4": "bs4",
    "pyyaml": "yaml", "scikit-learn": "sklearn", "python-dateutil": "dateutil", "python-dotenv": "dotenv",
    "pyside6": "PySide6", "pyqt5": "PyQt5", "pyqt6": "PyQt6", "discord.py": "discord", "pywin32": "win32api",
    "python-telegram-bot": "telegram", "google-api-python-client": "googleapiclient", "attrs": "attr",
    "pymupdf": "fitz", "python-docx": "docx", "python-pptx": "pptx", "pyjwt": "jwt", "pycryptodome": "Crypto",
    "faster-whisper": "faster_whisper", "openwakeword": "openwakeword", "edge-tts": "edge_tts",
    "send2trash": "send2trash", "pywebview": "webview", "typing-extensions": "typing_extensions",
}
NODE_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "crypto", "dgram", "dns", "events", "fs", "http", "http2",
    "https", "net", "os", "path", "perf_hooks", "process", "querystring", "readline", "stream", "string_decoder",
    "timers", "tls", "tty", "url", "util", "v8", "vm", "worker_threads", "zlib", "test", "module", "async_hooks",
}
_JS_IMPORT = re.compile(r"""(?:^|\s)(?:import\s+(?:[^'"]*?\sfrom\s+)?|export\s+[^'"]*?\sfrom\s+|require\(\s*|import\(\s*)['"]([^'"]+)['"]""", re.M)


def _python_imports(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


def _js_imports(text: str) -> set[str]:
    out: set[str] = set()
    for spec in _JS_IMPORT.findall(text):
        if spec.startswith((".", "/", "node:", "http:", "https:", "data:", "~", "@/", "#")) or spec in ("vite", ""):
            continue
        parts = spec.split("/")
        out.add("/".join(parts[:2]) if spec.startswith("@") else parts[0])
    return out


def check_imports(path: str, old: str, new: str, facts: dict[str, Any], lang: str) -> list[Note]:
    """New imports of libraries the project does not declare: shown, so nothing is assumed to be installed."""
    low = path.lower()
    py = facts.get("python", {}) if isinstance(facts, dict) else {}
    node = facts.get("node", {}) if isinstance(facts, dict) else {}
    local = set(facts.get("local_modules", [])) if isinstance(facts, dict) else set()
    unknown: list[str] = []
    if low.endswith((".py", ".pyw")):
        added = _python_imports(new) - _python_imports(old)
        declared = {PIP_MODULES.get(d, d).lower() for d in (*py.get("dependencies", []), *py.get("dev_dependencies", []))}
        declared |= {d.replace("-", "_").lower() for d in (*py.get("dependencies", []), *py.get("dev_dependencies", []))}
        stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
        for name in sorted(added):
            if name in stdlib or name.lower() in declared or name in local:
                continue
            unknown.append(name)
    elif low.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")):
        added = _js_imports(new) - _js_imports(old)
        declared = set(node.get("dependencies", [])) | set(node.get("dev_dependencies", []))
        for name in sorted(added):
            if name in declared or name in NODE_BUILTINS or name in local:
                continue
            unknown.append(name)
    if not unknown:
        return []
    names = ", ".join(unknown)
    return [Note(code="import", text=t(
        lang, f"ייבוא של {names}, שלא מופיע בתלויות הפרויקט: צריך להתקין ולהוסיף לתלויות, או לוותר עליו",
        f"imports {names}, which the project does not declare: install it and add it to the dependencies, or "
        "drop it"))]


__all__ = [
    "Change", "FileChange", "Note", "Proposal", "ProposedEdit", "Replacement", "TestAdvice", "apply_replacements",
    "build_file", "check_imports", "check_syntax", "read_source", "refresh", "safe_relative", "sha256", "unified",
]
