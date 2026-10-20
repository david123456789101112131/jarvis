"""Which files of a project a request is about, found without an AI.

The request's words (Hebrew words through a small programming glossary, so "כפתור התחברות" finds
`LoginButton.tsx`), the names and identifiers it mentions, the file paths in a pasted error or traceback,
and recent edits are scored against the file paths and, for small text files, their contents. The result
is a short list with the reason each file was picked, which the user can change before anything is sent.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.devmode.inspect import FileInfo, read_text

# Hebrew words people use for code things -> the English words code uses for them.
GLOSSARY: dict[str, tuple[str, ...]] = {
    "התחברות": ("login", "signin", "auth"), "כניסה": ("login", "signin"), "התנתקות": ("logout", "signout"),
    "הרשמה": ("register", "signup"), "משתמש": ("user",), "משתמשים": ("user", "users"), "סיסמה": ("password",),
    "כפתור": ("button", "btn"), "כפתורים": ("button",), "טופס": ("form",), "שדה": ("field", "input"),
    "דף": ("page",), "עמוד": ("page",), "מסך": ("screen", "view", "page"), "חלון": ("window", "dialog", "modal"),
    "תפריט": ("menu", "nav"), "ניווט": ("nav", "router", "route"), "קישור": ("link",), "כותרת": ("header", "title"),
    "תחתית": ("footer",), "סרגל": ("sidebar", "toolbar", "bar"), "רשימה": ("list",), "טבלה": ("table",),
    "כרטיס": ("card",), "חיפוש": ("search",), "סינון": ("filter",), "מיון": ("sort",), "עיצוב": ("style", "css", "theme"),
    "צבע": ("color", "colour", "theme"), "צבעים": ("color", "theme"), "גופן": ("font",), "כהה": ("dark", "theme"),
    "בהיר": ("light", "theme"), "אייקון": ("icon",), "תמונה": ("image", "img"), "תמונות": ("image", "images"),
    "סרטון": ("video",), "וידאו": ("video",), "קול": ("voice", "audio", "sound"), "שמע": ("audio", "sound"),
    "מוזיקה": ("music",), "הגדרות": ("settings", "config"), "הגדרה": ("setting", "config"), "תצורה": ("config",),
    "שפה": ("language", "lang", "i18n"), "תרגום": ("translation", "i18n"), "עברית": ("hebrew", "rtl", "he"),
    "תאריך": ("date",), "שעה": ("time",), "זמן": ("time", "timer"), "שעון": ("clock", "timer"),
    "הודעה": ("message",), "הודעות": ("message", "messages"), "התראה": ("notification", "alert", "toast"),
    "צאט": ("chat",), "צ'אט": ("chat",), "בוט": ("bot",), "שרת": ("server", "api"), "לקוח": ("client", "customer"),
    "מסד": ("database", "db"), "נתונים": ("data", "db"), "שמירה": ("save", "store"), "טעינה": ("load",),
    "מחיקה": ("delete", "remove"), "עריכה": ("edit", "update"), "הוספה": ("add", "create"), "יצירה": ("create",),
    "העלאה": ("upload",), "הורדה": ("download",), "ייצוא": ("export",), "ייבוא": ("import",), "קובץ": ("file",),
    "קבצים": ("file", "files"), "תיקייה": ("folder", "dir"), "דוח": ("report",), "גרף": ("chart", "graph"),
    "סל": ("cart", "basket"), "עגלה": ("cart",), "סכום": ("sum", "total"), "חישוב": ("calc", "compute", "calculate"),
    "מספר": ("number", "count"), "מספרים": ("number", "numbers"), "ספירה": ("count",), "ממוצע": ("average", "mean"), "מוצר": ("product",), "מוצרים": ("product", "products"),
    "הזמנה": ("order",), "הזמנות": ("order", "orders"), "תשלום": ("payment", "checkout", "pay"), "מחיר": ("price",),
    "קופה": ("checkout",), "חנות": ("shop", "store"), "מלאי": ("inventory", "stock"), "חשבון": ("account", "invoice"),
    "פרופיל": ("profile",), "אבטחה": ("security", "auth"), "הרשאה": ("permission",), "הרשאות": ("permission",),
    "בדיקה": ("test", "check", "validate"), "בדיקות": ("test", "tests"), "אימות": ("validate", "verify", "auth"),
    "שגיאה": ("error", "exception"), "שגיאות": ("error", "errors"), "באג": ("bug", "fix"), "קריסה": ("crash", "error"),
    "לוג": ("log", "logger"), "לוגים": ("log", "logs"), "ביצועים": ("performance", "perf"), "מהירות": ("speed", "performance"),
    "מטמון": ("cache",), "משחק": ("game",), "שחקן": ("player",), "ניקוד": ("score",), "שלב": ("level", "step"),
    "אויב": ("enemy",), "מפה": ("map",), "תנועה": ("movement", "move", "motion"), "קפיצה": ("jump",),
    "התנגשות": ("collision",), "אנימציה": ("animation", "animate"), "מודל": ("model", "modal"), "מערכת": ("system",),
    "רכיב": ("component",), "רכיבים": ("component", "components"), "פונקציה": ("function",), "מחלקה": ("class",),
    "ממשק": ("ui", "interface", "api"), "מקלדת": ("keyboard", "key", "hotkey"), "עכבר": ("mouse", "click"),
    "לחיצה": ("click",), "גלילה": ("scroll",), "טיימר": ("timer",), "תזכורת": ("reminder",), "משימה": ("task",),
    "משימות": ("task", "tasks", "todo"), "יומן": ("calendar",), "מייל": ("email", "mail"), "אימייל": ("email", "mail"),
    "טלגרם": ("telegram",), "וואטסאפ": ("whatsapp",), "דיסקורד": ("discord",), "מחשבון": ("calculator", "calc"),
    "סתם": ("stam",), "ספר": ("book",), "מילה": ("word",), "מילים": ("word", "words"), "אות": ("letter",),
    "אותיות": ("letter", "letters"), "טקסט": ("text",), "גודל": ("size",), "רוחב": ("width",), "גובה": ("height",),
    "ראשי": ("main", "home", "index"), "בית": ("home", "index"), "אודות": ("about",), "צור": ("contact",),
    "פוטר": ("footer",), "הדר": ("header",), "ממשק משתמש": ("ui",), "API": ("api",),
}
STOP_WORDS = {
    "the", "a", "an", "to", "of", "in", "on", "and", "or", "for", "with", "it", "is", "be", "that", "this", "my", "me",
    "add", "make", "fix", "change", "update", "please", "should", "can", "you", "when", "from", "into", "new",
    "את", "של", "על", "עם", "זה", "זאת", "כל", "גם", "אם", "כדי", "שלי", "לי", "אני", "יש", "אין", "תוסיף", "תעשה",
    "תתקן", "תשנה", "צריך", "רוצה", "שיהיה", "שהוא", "שזה", "אבל", "או", "רק", "עוד", "יותר", "פחות", "בבקשה",
}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}|[֐-׿']{2,}")
_IDENT_SPLIT = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
# File paths in tracebacks and compiler/test output: File "x.py", line 3 / at x.ts:12:3 / x.py:14: error
_TRACE = re.compile(r"""(?:File\s+"(?P<py>[^"]+)",\s+line\s+(?P<pyline>\d+))|(?P<path>[\w./\\\-]+\.[A-Za-z]{1,5}):(?P<line>\d+)""")
_HE_PREFIX = ("וה", "שה", "וב", "וכ", "ול", "ומ", "ה", "ו", "ב", "ל", "מ", "ש", "כ")


@dataclass
class Ranked:
    path: str
    score: float
    size: int
    reasons: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)     # lines named in an error, when there was one


def terms(request: str) -> set[str]:
    """Lower-case search terms: English words and the parts of identifiers, plus glossary words for Hebrew."""
    out: set[str] = set()
    for word in _WORD.findall(request):
        if re.match(r"[֐-׿]", word):
            for candidate in _hebrew_forms(word):
                out.update(GLOSSARY.get(candidate, ()))
            continue
        low = word.lower()
        if low in STOP_WORDS:
            continue
        out.add(low)
        parts = [p.lower() for p in _IDENT_SPLIT.findall(word) if len(p) > 1]
        if len(parts) > 1:
            out.update(p for p in parts if p not in STOP_WORDS)
        for piece in low.split("_"):
            if len(piece) > 2 and piece not in STOP_WORDS:
                out.add(piece)
    return {t for t in out if len(t) > 1}


def _hebrew_forms(word: str) -> list[str]:
    """The word and the word without one-letter prefixes (ה, ו, ב, ל, מ, ש, כ): "בכפתור" -> "כפתור"."""
    forms = [word]
    for prefix in _HE_PREFIX:
        if word.startswith(prefix) and len(word) - len(prefix) >= 2:
            forms.append(word[len(prefix):])
    return forms


def traced_files(text: str, root: Path, known: set[str]) -> dict[str, list[int]]:
    """Project files named in an error or traceback, with the line numbers it names."""
    found: dict[str, list[int]] = {}
    root_s = str(root).replace("\\", "/").rstrip("/").lower()
    for m in _TRACE.finditer(text or ""):
        raw = (m.group("py") or m.group("path") or "").replace("\\", "/")
        line = int(m.group("pyline") or m.group("line") or 0)
        rel = raw
        if raw.lower().startswith(root_s + "/"):
            rel = raw[len(root_s) + 1:]
        while rel.startswith("./"):
            rel = rel[2:]
        rel = rel.lstrip("/")
        match = rel if rel in known else next((k for k in known if k.endswith("/" + rel) or rel.endswith("/" + k)), None)
        if match:
            found.setdefault(match, [])
            if line and line not in found[match]:
                found[match].append(line)
    return found


def rank_files(root: Path, files: list[FileInfo], request: str, *, error_text: str = "", limit: int = 8,
               content_budget: int = 6_000_000, now: float | None = None, lang: str = "he") -> list[Ranked]:
    """The files most likely to matter for `request` (and `error_text`), best first, each with its reasons."""
    from jarvis.common.i18n import t

    now = now or time.time()
    words = terms(request + "\n" + error_text[:2000])
    candidates = [f for f in files if f.text and not f.secret]
    known = {f.path for f in candidates}
    traced = traced_files(error_text, root, known) if error_text else {}
    scored: dict[str, Ranked] = {}

    def bump(f: FileInfo, points: float, reason: str) -> None:
        r = scored.setdefault(f.path, Ranked(path=f.path, score=0.0, size=f.size))
        r.score += points
        if reason and reason not in r.reasons:
            r.reasons.append(reason)

    read_bytes = 0
    for f in candidates:
        low = f.path.lower()
        stem = Path(low).stem
        parts = set(re.split(r"[/._\-]+", low)) | {p.lower() for p in _IDENT_SPLIT.findall(Path(f.path).stem)}
        if f.path in traced:
            bump(f, 100.0, t(lang, "מופיע בשגיאה", "named in the error"))
            scored[f.path].lines = traced[f.path][:10]
        name_hits = sorted(w for w in words if w in parts or (len(w) > 3 and w in stem))
        if name_hits:
            bump(f, 12.0 * len(name_hits), t(lang, f"השם מתאים ל-{', '.join(name_hits[:3])}",
                                              f"the name matches {', '.join(name_hits[:3])}"))
        if words and f.language and read_bytes < content_budget and f.size <= 200_000:
            text = read_text(root / f.path, 200_000)
            read_bytes += f.size
            if text:
                low_text = text.lower()
                hits = {w: low_text.count(w) for w in words if len(w) > 2}
                hits = {w: c for w, c in hits.items() if c}
                if hits:
                    points = sum(min(c, 12) for c in hits.values()) * 1.5 + 3 * len(hits)
                    top = sorted(hits, key=lambda w: -hits[w])[:3]
                    bump(f, points, t(lang, f"מכיל {', '.join(top)}", f"contains {', '.join(top)}"))
        if f.path in scored and f.language and now - f.mtime < 2 * 86400:
            bump(f, 4.0, t(lang, "נערך לאחרונה", "edited recently"))
    if not words & {"test", "tests", "spec", "testing"}:
        # The code a request is about comes before its tests (they still come along, below).
        for r in scored.values():
            if r.path not in traced and _is_test_path(r.path):
                r.score *= 0.6
    ranked = sorted(scored.values(), key=lambda r: (-r.score, r.path))
    if not ranked and candidates:
        # Nothing matched: the entry points and the most recently edited source files are the best guess.
        entry = ("main.py", "app.py", "src/main.py", "index.html", "src/App.tsx", "src/App.jsx", "src/main.ts",
                 "src/main.tsx", "src/index.js", "index.js", "manage.py", "bot.py")
        for f in candidates:
            if f.path in entry:
                bump(f, 5.0, t(lang, "נקודת הכניסה של הפרויקט", "the project's entry point"))
        for f in sorted((f for f in candidates if f.language), key=lambda f: -f.mtime)[:4]:
            bump(f, 2.0, t(lang, "נערך לאחרונה", "edited recently"))
        ranked = sorted(scored.values(), key=lambda r: (-r.score, r.path))
    return _with_tests(ranked[:limit], candidates, lang)


def _is_test_path(path: str) -> bool:
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or ".test." in name or ".spec." in name \
        or low.startswith(("tests/", "test/")) or "/tests/" in low or "/__tests__/" in f"/{low}"


def _with_tests(ranked: list[Ranked], candidates: list[FileInfo], lang: str) -> list[Ranked]:
    """Each chosen source file's own test file comes along (test_<name>.py, <name>.test.ts)."""
    from jarvis.common.i18n import t

    chosen = {r.path for r in ranked}
    by_name: dict[str, FileInfo] = {}
    for f in candidates:
        name = f.path.rsplit("/", 1)[-1].lower()
        by_name.setdefault(name, f)
    extra: list[Ranked] = []
    for r in list(ranked):
        p = Path(r.path)
        stem = p.stem.lower()
        if stem.startswith("test_") or ".test" in stem or ".spec" in stem:
            continue
        for name in _test_names(r.path):
            test = by_name.get(name)
            if test is not None and test.path not in chosen:
                chosen.add(test.path)
                extra.append(Ranked(path=test.path, score=r.score * 0.5, size=test.size,
                                    reasons=[t(lang, f"הבדיקות של {p.name}", f"the tests of {p.name}")]))
                break
    return ranked + extra[:3]


def _test_names(path: str) -> tuple[str, ...]:
    p = Path(path)
    stem, ext = p.stem.lower(), p.suffix.lower()
    return (f"test_{stem}{ext}", f"{stem}_test{ext}", f"{stem}.test{ext}", f"{stem}.spec{ext}")


def _imports(stem: str) -> re.Pattern[str]:
    """`import cart`, `from shop.cart import x`, `from . import cart`, `from './cart'`, `require('../cart.js')`."""
    s = re.escape(stem)
    python = rf"^\s*(?:from\s+[\w.]*\b{s}\b|from\s+[\w.]+\s+import\s+[\w, ()]*\b{s}\b|import\s+[\w., ]*\b{s}\b)"
    script = rf"""(?:\bfrom|\brequire\(|\bimport\(?)\s*['"][^'"]*\b{s}(?:\.[cm]?[jt]sx?)?['"]"""
    return re.compile(f"{python}|{script}", re.M | re.I)


def related_tests(root: Path, files: list[FileInfo], chosen: list[str], *, limit: int = 3,
                  max_read: int = 200) -> list[tuple[str, str]]:
    """The project's tests for the chosen source files, as (test file, the file it tests): a test named after
    the file first, then tests that import it. They show the AI what the code is meant to do."""
    picked = set(chosen)
    sources = [c for c in chosen if not _is_test_path(c) and Path(c).stem]
    tests = [f for f in files if f.text and not f.secret and _is_test_path(f.path) and f.path not in picked]
    if not sources or not tests:
        return []
    found: list[tuple[str, str]] = []

    def take(test: str, source: str) -> bool:
        if test not in picked:
            picked.add(test)
            found.append((test, source))
        return len(found) >= limit

    by_name = {f.path.rsplit("/", 1)[-1].lower(): f for f in tests}
    for source in sources:
        for name in _test_names(source):
            if name in by_name and take(by_name[name].path, source):
                return found
    patterns = [(source, _imports(Path(source).stem)) for source in sources]
    for f in sorted(tests, key=lambda f: f.path)[:max_read]:
        if f.path in picked or f.size > 200_000:
            continue
        text = read_text(root / f.path, 200_000) or ""
        imported = next((src for src, pattern in patterns if pattern.search(text)), None)
        if imported is not None and take(f.path, imported):
            break
    return found


__all__ = ["GLOSSARY", "Ranked", "rank_files", "related_tests", "terms", "traced_files"]
