"""Installed-app index shared by the host adapters.

The index combines the bilingual catalog (`jarvis.host.catalog`) with what the platform
reports as installed (Start menu, Store apps, App Paths, PATH, .desktop files…):

* `merge_apps` de-duplicates entries found by several sources (best source wins, process
  names and aliases are pooled) and `annotate_with_catalog` links discovered apps to catalog
  ids, so "כרום" finds the Start-menu "Google Chrome" shortcut;
* `AppIndexCache` / `AppIndexStore` persist the index as JSON with a TTL (24 h by default)
  and rebuild it on demand;
* `AppIndex.match` resolves spoken/typed names in Hebrew or English. Stages, first hit wins:

  1. exact catalog alias ("כרום", "גוגל כרום", "vs code", "וורד") → the installed app with
     that catalog id (a known app that is not installed is *not* replaced by another one);
  2. exact installed name, case/space/punctuation-insensitive, also without version /
     vendor words ("Notepad++", "photoshop" ≡ "Adobe Photoshop 2024");
  3. token / prefix match ("vlc player" → "VLC media player");
  4. difflib similarity with a length-aware cutoff ("spotfy" → Spotify);
  5. Hebrew → Latin phonetic skeletons for apps the catalog doesn't know ("בלנדר" → Blender).

  Queries are cleaned first: filler words ("the", "app", "את", "האפליקציה", "בבקשה"…) are
  dropped and Hebrew prefixes (definite article ה, ו/ל/ב) are tried with and without.

The end of the module holds small helpers both host adapters share (process-name mapping,
URL validation, screenshot paths, delayed power actions).
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import logging
import ntpath
import os
import posixpath
import re
import shlex
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jarvis.common.text import normalize
from jarvis.host import catalog
from jarvis.host.base import HostUnsupported, InstalledApp
from jarvis.host.catalog import APPS, AppEntry

log = logging.getLogger(__name__)

INDEX_VERSION = 1
DEFAULT_TTL_S = 24 * 3600.0
APP_KINDS = frozenset({"exe", "lnk", "appx", "uri", "command"})
# Lower is better when two entries describe the same app.
SOURCE_PRIORITY: dict[str, int] = {
    "startmenu": 0,
    "desktop": 0,
    "applications": 0,
    "appx": 1,
    "apppaths": 2,
    "path": 3,
    "catalog": 4,
}

# =============================================================================================
# Text keys
# =============================================================================================
_HEBREW_RE = re.compile(r"[\u0590-\u05ff]")
_WORD_RE = re.compile(r"[^\W_]+")
_SKELETON_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]*)*")
_BRACKETS_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_SYMBOL_WORDS = (("++", " plus plus "), ("+", " plus "), ("&", " and "), ("#", " sharp "))
_VERSION_TOKEN_RE = re.compile(r"^(?:v?\d+|x64|x86|amd64|arm64|win64|win32|bit)$")
_VENDORS = frozenset(
    {"microsoft", "ms", "google", "adobe", "mozilla", "apple", "jetbrains", "autodesk", "oracle", "windows", "gnu",
     "kde", "gnome", "the"}
)
# Words people wrap around an app name: "the calculator", "Spotify app", "את האפליקציה של ספוטיפיי".
_FILLERS = frozenset(
    {
        "the", "a", "an", "my", "app", "apps", "application", "program", "programme", "software", "browser", "window",
        "please", "for", "me", "now", "of",
        "את", "לי", "של", "בבקשה", "עכשיו", "אפליקציה", "האפליקציה", "אפליקציית", "אפליקציות", "תוכנה", "התוכנה",
        "תוכנת", "תכנה", "התכנה", "תוכנית", "התוכנית", "דפדפן", "הדפדפן", "חלון", "החלון",
    }
)
# Letters Hebrew glues to the first word: article, "and", "to", "in" ("הכרום", "לכרום", "והכרום").
_HE_PREFIXES = ("וה", "ה", "ו", "ל", "ב")


def has_hebrew(text: str) -> bool:
    return bool(_HEBREW_RE.search(text or ""))


def match_key(text: str) -> str:
    """Lower-case words only: "Notepad++" → "notepad plus plus", "VS-Code" → "vs code"."""
    t = normalize(text).replace("'", "").replace('"', "")
    for symbol, word in _SYMBOL_WORDS:
        t = t.replace(symbol, word)
    return " ".join(_WORD_RE.findall(t))


def compact_key(text: str) -> str:
    """`match_key` without spaces: "VS Code" ≡ "vscode" ≡ "Vs-Code"."""
    return match_key(text).replace(" ", "")


def tokens(text: str) -> list[str]:
    return match_key(text).split()


def clean_query(text: str) -> str:
    """Drop filler words around a name: "the calculator" → "calculator", "Spotify app" → "spotify"."""
    toks = tokens(text)
    while len(toks) > 1 and toks[0] in _FILLERS:
        toks.pop(0)
    while len(toks) > 1 and toks[-1] in _FILLERS:
        toks.pop()
    return " ".join(toks)


def query_variants(text: str, *, prefixes: bool = True) -> list[str]:
    """Candidate keys for a query, most literal first.

    "את הכרום" → ["הכרום", "כרום", "את הכרום"]; "פנקס הרשימות" → [..., "פנקס רשימות"].
    With `prefixes=False` only the definite article is stripped (used by fuzzy stages, where
    aggressive stripping would create noise).
    """
    out: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in out:
            out.append(value)

    cleaned = clean_query(text)
    toks = cleaned.split()
    add(cleaned)
    if toks and has_hebrew(toks[0]):
        for prefix in _HE_PREFIXES if prefixes else ("ה",):
            if toks[0].startswith(prefix) and len(toks[0]) - len(prefix) >= 2:
                add(" ".join([toks[0][len(prefix):], *toks[1:]]))
    if len(toks) > 1:
        inner = [toks[0], *(t[1:] if t.startswith("ה") and len(t) >= 4 and has_hebrew(t) else t for t in toks[1:])]
        add(" ".join(inner))
    add(match_key(text))
    return out


def name_forms(name: str) -> list[str]:
    """Match keys an installed name answers to.

    "Adobe Photoshop 2024" → ["adobe photoshop 2024", "adobe photoshop", "photoshop"];
    "Python 3.12 (64-bit)" → ["python 3 12 64 bit", "python"]. The first token is never
    dropped as a version ("7-Zip File Manager" stays whole).
    """
    forms: list[str] = []

    def add(value: str) -> None:
        if value and value not in forms:
            forms.append(value)

    add(match_key(name))
    bare = match_key(_BRACKETS_RE.sub(" ", normalize(name))).split()
    if bare:
        core = [bare[0], *(t for t in bare[1:] if not _VERSION_TOKEN_RE.match(t))]
        add(" ".join(core))
        if len(core) > 1 and core[0] in _VENDORS:
            add(" ".join(core[1:]))
    return forms


# ---------------------------------------------------------------------------------- phonetics
_HE_SKELETON = {
    "ב": "B", "ג": "G", "ד": "D", "ז": "S", "ח": "H", "ט": "T", "כ": "K", "ך": "K", "ל": "L", "מ": "M", "ם": "M",
    "נ": "N", "ן": "N", "ס": "S", "פ": "P", "ף": "P", "צ": "TS", "ץ": "TS", "ק": "K", "ר": "R", "ש": "S", "ת": "T",
}
_HE_GERESH = {"ג": "J", "ז": "S", "צ": "C", "ץ": "C", "ת": "T"}
_LATIN_SKELETON = {
    "b": "B", "v": "B", "w": "B", "f": "P", "p": "P", "k": "K", "q": "K", "g": "G", "j": "J", "z": "S", "s": "S",
    "d": "D", "t": "T", "l": "L", "m": "M", "n": "N", "r": "R", "h": "H",
}
_LATIN_DIGRAPHS = (("tion", "SN"), ("sion", "SN"), ("sch", "S"), ("sh", "S"), ("ph", "P"), ("th", "T"),
                   ("wh", "B"), ("ck", "K"), ("qu", "K"))


def _hebrew_word_skeleton(word: str) -> str:
    out: list[str] = []
    i, n = 0, len(word)
    while i < n:
        ch = word[i]
        nxt = word[i + 1] if i + 1 < n else ""
        if ch == "'":
            i += 1
        elif nxt == "'" and ch in _HE_GERESH:      # ג' = j, צ' = ch, ז' = zh, ת' = th
            out.append(_HE_GERESH[ch])
            i += 2
        elif ch == "ו":                            # וו = v/w, word-initial ו = v, otherwise a vowel
            if nxt == "ו":
                out.append("B")
                i += 2
                continue
            if i == 0:
                out.append("B")
            i += 1
        elif ch == "ה":                            # final ה is a silent vowel letter
            if i < n - 1:
                out.append("H")
            i += 1
        else:                                      # א ע י are vowels / carriers
            if ch in _HE_SKELETON:
                out.append(_HE_SKELETON[ch])
            elif ch.isdigit():
                out.append(ch)
            i += 1
    return "".join(out)


def _latin_word_skeleton(word: str) -> str:
    out: list[str] = []
    i, n = 0, len(word)
    while i < n:
        rest = word[i:]
        digraph = next(((dg, code) for dg, code in _LATIN_DIGRAPHS if rest.startswith(dg)), None)
        if digraph is not None:
            out.append(digraph[1])
            i += len(digraph[0])
            continue
        ch = word[i]
        nxt = word[i + 1] if i + 1 < n else ""
        if ch == "c" and nxt == "h":               # "chrome" = k, "chat" = ch
            after = word[i + 2] if i + 2 < n else ""
            out.append("K" if after in ("r", "l") else "C")
            i += 2
            continue
        if ch == "g" and nxt == "h":               # silent except at the start ("ghost")
            if i == 0:
                out.append("G")
            i += 2
            continue
        if ch == "c":
            out.append("S" if nxt in ("e", "i", "y") else "K")
        elif ch == "x":
            out.append("KS")
        elif ch in _LATIN_SKELETON:
            out.append(_LATIN_SKELETON[ch])
        elif ch.isdigit():
            out.append(ch)
        i += 1                                     # vowels (a e i o u y) are dropped
    return "".join(out)


def skeleton(text: str) -> str:
    """Script-neutral consonant skeleton: "בלנדר" and "Blender" → "BLNDR", "נושן"/"Notion" → "NSN"."""
    t = unicodedata.normalize("NFKD", normalize(text))
    t = "".join(c for c in t if not unicodedata.combining(c))
    parts = [
        _hebrew_word_skeleton(w) if has_hebrew(w) else _latin_word_skeleton(w) for w in _SKELETON_WORD_RE.findall(t)
    ]
    return re.sub(r"(.)\1+", r"\1", "".join(parts))


# =============================================================================================
# Catalog helpers
# =============================================================================================
def _build_catalog_keys() -> dict[str, AppEntry]:
    keys: dict[str, AppEntry] = {}
    for entry in APPS:
        for text in (entry.id, entry.display, *entry.aliases):
            key = compact_key(text)
            if key and key not in keys:
                keys[key] = entry
    return keys


_CATALOG_KEYS = _build_catalog_keys()


def catalog_lookup(text: str) -> AppEntry | None:
    """Exact catalog match for a spoken/typed name (aliases, display names and ids)."""
    if not match_key(text):
        return None
    entry = catalog.find_app(text)
    if entry is not None:
        return entry
    for variant in query_variants(text):
        entry = _CATALOG_KEYS.get(variant.replace(" ", ""))
        if entry is not None:
            return entry
    return None


def _entry_process_keys(entry: AppEntry) -> set[str]:
    """Lower-case process names of a catalog entry, with and without ".exe"."""
    out: set[str] = set()
    for name in entry.process_names:
        low = name.lower()
        out.add(low)
        out.add(low[:-4] if low.endswith(".exe") else low + ".exe")
    return out


def _target_exe_evidence(app: InstalledApp) -> set[str]:
    names = {n.lower() for n in app.process_names if n}
    if app.kind == "exe":
        base = exe_basename(app.target)
        if base and base not in WINDOWS_WRAPPER_EXES and base not in POSIX_WRAPPERS:
            names.add(base)
    elif app.kind == "command":
        first = command_head(app.target)
        if first and first not in POSIX_WRAPPERS and first not in WINDOWS_WRAPPER_EXES:
            names.add(first)
    return names


def catalog_entry_for(app: InstalledApp) -> AppEntry | None:
    """The catalog entry a discovered app corresponds to (by process/exe name, name or URI)."""
    evidence = _target_exe_evidence(app)
    if evidence:
        for entry in APPS:
            if evidence & _entry_process_keys(entry):
                return entry
    for form in name_forms(app.name):
        entry = _CATALOG_KEYS.get(form.replace(" ", ""))
        if entry is None:
            continue
        # An exe that contradicts the name ("Calculator" → foocalc.exe) is a different app.
        if evidence and entry.process_names and not evidence & _entry_process_keys(entry):
            continue
        return entry
    if app.kind == "uri":
        target = app.target.lower()
        for entry in APPS:
            if entry.uri and target.startswith(entry.uri.lower()):
                return entry
    return None


def annotate_with_catalog(apps: Iterable[InstalledApp]) -> list[InstalledApp]:
    """Set `id`, pool process names and aliases from the catalog (mutates and returns the apps)."""
    out = list(apps)
    by_id = {e.id: e for e in APPS}
    for app in out:
        entry = by_id.get(app.id) if app.id else catalog_entry_for(app)
        if entry is None:
            continue
        app.id = entry.id
        app.process_names = _unique((*app.process_names, *entry.process_names))
        app.aliases = _unique((*app.aliases, *entry.aliases))
    return out


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not item:
            continue
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return tuple(out)


# =============================================================================================
# Merging
# =============================================================================================
def _absorb(keep: InstalledApp, other: InstalledApp) -> None:
    keep.id = keep.id or other.id
    extra_alias = (other.name,) if compact_key(other.name) != compact_key(keep.name) else ()
    keep.process_names = _unique((*keep.process_names, *other.process_names))
    keep.aliases = _unique((*keep.aliases, *extra_alias, *other.aliases))


def merge_apps(*groups: Iterable[InstalledApp], case_insensitive_targets: bool = True) -> list[InstalledApp]:
    """Merge app lists from several sources into one de-duplicated list.

    Two entries are the same app when they share kind+target, or their names are equal
    ignoring case, spaces and punctuation. The entry from the better source (see
    `SOURCE_PRIORITY`) is kept; ids, process names and aliases are pooled. Inputs are not
    mutated.
    """
    ordered = sorted(
        (app for group in groups for app in group),
        key=lambda a: SOURCE_PRIORITY.get(a.source, 9),
    )
    by_target: dict[tuple[str, str], InstalledApp] = {}
    by_name: dict[str, InstalledApp] = {}
    out: list[InstalledApp] = []
    for app in ordered:
        if not app.name or not app.target or app.kind not in APP_KINDS:
            continue
        target = app.target.strip()
        tkey = (app.kind, target.lower() if case_insensitive_targets else target)
        nkey = compact_key(app.name)
        existing = by_target.get(tkey) or (by_name.get(nkey) if nkey else None)
        if existing is not None:
            _absorb(existing, app)
            by_target.setdefault(tkey, existing)
            continue
        copy = replace(app, target=target, process_names=tuple(app.process_names), aliases=tuple(app.aliases))
        out.append(copy)
        by_target[tkey] = copy
        if nkey:
            by_name[nkey] = copy
    return out


# =============================================================================================
# Matching
# =============================================================================================
@dataclass(frozen=True)
class AppMatch:
    app: InstalledApp
    score: float
    stage: str          # catalog | exact | token | fuzzy | phonetic


@dataclass(frozen=True)
class _Prepared:
    app: InstalledApp
    priority: int
    forms: tuple[str, ...]                    # name forms (match keys); forms[0] is the full name
    form_compacts: tuple[str, ...]
    alias_compacts: frozenset[str]
    token_sets: tuple[tuple[str, ...], ...]   # forms + aliases, tokenized
    skeletons: tuple[str, ...]                # phonetic skeletons of Latin-script forms


def _prepare(app: InstalledApp) -> _Prepared:
    forms = tuple(name_forms(app.name))
    alias_keys = [match_key(a) for a in app.aliases]
    token_sets = tuple(dict.fromkeys(tuple(f.split()) for f in (*forms, *alias_keys) if f))
    skeletons = tuple(dict.fromkeys(skeleton(f) for f in forms if f and not has_hebrew(f)))
    return _Prepared(
        app=app,
        priority=SOURCE_PRIORITY.get(app.source, 9),
        forms=forms,
        form_compacts=tuple(f.replace(" ", "") for f in forms),
        alias_compacts=frozenset(k.replace(" ", "") for k in alias_keys if k),
        token_sets=token_sets,
        skeletons=tuple(s for s in skeletons if s),
    )


def _rank(score: float, prepared: _Prepared) -> tuple[float, int, int]:
    return (round(score, 6), -prepared.priority, -len(prepared.app.name))


def _token_score(query: tuple[str, ...], name: tuple[str, ...]) -> float:
    if not query or not name:
        return 0.0
    qs, ns = set(query), set(name)
    best = 0.0
    if qs <= ns:                                             # every spoken word is in the name
        best = max(best, 0.80 + 0.15 * len(qs) / len(ns))
    if ns <= qs and sum(len(t) for t in ns) >= 3:            # the whole name, plus extra words
        best = max(best, 0.75 + 0.15 * len(ns) / len(qs))
    cq, cn = "".join(query), "".join(name)
    if len(cq) >= 3 and cn.startswith(cq):                   # "photo" → "photoshop"
        best = max(best, 0.60 + 0.30 * len(cq) / len(cn))
    if len(query) <= len(name) and all(len(t) >= 2 for t in query):   # "vis stu" → "visual studio"
        j = 0
        for t in query:
            while j < len(name) and not name[j].startswith(t):
                j += 1
            if j == len(name):
                break
            j += 1
        else:
            best = max(best, 0.55 + 0.30 * sum(map(len, query)) / sum(map(len, name)))
    return best


def _fuzzy_cutoff(length: int) -> float:
    if length <= 4:
        return 0.88
    if length <= 7:
        return 0.8
    return 0.75


TOKEN_THRESHOLD = 0.6
PHONETIC_CUTOFF = 0.85


class AppIndex:
    """Immutable snapshot of launchable apps with bilingual matching."""

    def __init__(self, apps: Iterable[InstalledApp] = (), *, built_at: float | None = None, platform: str = "") -> None:
        self.apps: list[InstalledApp] = sorted(
            apps, key=lambda a: (a.name.casefold(), SOURCE_PRIORITY.get(a.source, 9), a.target)
        )
        self.built_at = time.time() if built_at is None else float(built_at)
        self.platform = platform
        self._prepared = [_prepare(a) for a in self.apps]
        self._by_identity = {id(p.app): p for p in self._prepared}

    def __len__(self) -> int:
        return len(self.apps)

    def __repr__(self) -> str:
        return f"AppIndex({len(self.apps)} apps, platform={self.platform!r}, built_at={self.built_at:.0f})"

    # ------------------------------------------------------------------------------ freshness
    def age(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.built_at

    def is_fresh(self, ttl_s: float = DEFAULT_TTL_S, now: float | None = None) -> bool:
        age = self.age(now)
        return -300.0 <= age < ttl_s          # a timestamp far in the future means a broken clock/cache

    # ------------------------------------------------------------------------------ lookups
    def find(self, query: str) -> InstalledApp | None:
        m = self.match(query)
        return m.app if m else None

    def match(self, query: str) -> AppMatch | None:
        if not match_key(query):
            return None
        entry = catalog_lookup(query)
        if entry is not None:
            app = self.for_catalog(entry)
            # A known app that isn't installed must not be swapped for a look-alike ("word" ≠ WordPad).
            return AppMatch(app, 1.0, "catalog") if app is not None else None
        variants = query_variants(query)
        soft_variants = query_variants(query, prefixes=False)
        return (
            self._exact(variants)
            or self._token(variants)
            or self._fuzzy(soft_variants)
            or self._phonetic(soft_variants)
        )

    def for_catalog(self, entry: AppEntry) -> InstalledApp | None:
        """Installed app for a catalog entry: same id first, then same name; best source wins."""
        keys = {compact_key(t) for t in (entry.id, entry.display, *entry.aliases)} - {""}
        best: InstalledApp | None = None
        best_rank: tuple[int, int, int] | None = None
        for p in self._prepared:
            name_hit = any(c in keys for c in p.form_compacts)
            if p.app.id != entry.id and not (name_hit and p.app.id is None):
                continue
            rank = (0 if name_hit else 1, p.priority, len(p.app.name))
            if best_rank is None or rank < best_rank:
                best, best_rank = p.app, rank
        return best

    def search(self, query: str, limit: int = 5) -> list[AppMatch]:
        """Closest apps for suggestions ("did you mean…"), best first."""
        variants = query_variants(query, prefixes=False)
        if not variants:
            return []
        scored: list[tuple[tuple[float, int, int], AppMatch]] = []
        q_tokens = [tuple(v.split()) for v in variants]
        q_compacts = [v.replace(" ", "") for v in variants]
        q_skeletons = [skeleton(v) for v in variants if has_hebrew(v)]
        for p in self._prepared:
            best = 0.0
            for qt in q_tokens:
                for nt in p.token_sets:
                    best = max(best, _token_score(qt, nt))
            for qc in q_compacts:
                for c in (*p.form_compacts, *p.alias_compacts):
                    best = max(best, difflib.SequenceMatcher(None, qc, c).ratio() * 0.9)
            for qs in q_skeletons:
                for s in p.skeletons:
                    best = max(best, difflib.SequenceMatcher(None, qs, s).ratio() * 0.85)
            if best >= 0.5:
                scored.append((_rank(best, p), AppMatch(p.app, round(best, 3), "search")))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [m for _, m in scored[: max(0, limit)]]

    # ------------------------------------------------------------------------------ stages
    def _exact(self, variants: Sequence[str]) -> AppMatch | None:
        keys = [v.replace(" ", "") for v in variants if v]
        # A long Hebrew transliteration that sounds exactly like a Latin name is as good as the
        # name itself ("נוטפד פלוס פלוס" = Notepad++), and must beat partial word matches.
        sounds = {s for s in (skeleton(v) for v in variants if has_hebrew(v)) if len(s) >= 5}
        best: tuple[tuple[float, int, int], AppMatch] | None = None
        for p in self._prepared:
            score, stage = 0.0, "exact"
            for key in keys:
                if key == p.form_compacts[0]:
                    score = max(score, 1.0)
                elif key in p.form_compacts:
                    score = max(score, 0.97)
                elif key in p.alias_compacts:
                    score = max(score, 0.95)
            if not score and sounds and sounds.intersection(p.skeletons):
                score, stage = 0.9, "phonetic"
            if score and (best is None or _rank(score, p) > best[0]):
                best = (_rank(score, p), AppMatch(p.app, score, stage))
        return best[1] if best else None

    def _token(self, variants: Sequence[str]) -> AppMatch | None:
        queries = [tuple(v.split()) for v in variants if v]
        queries = [q for q in queries if sum(map(len, q)) >= 2]
        best: tuple[tuple[float, int, int], AppMatch] | None = None
        for p in self._prepared:
            score = max((_token_score(q, n) for q in queries for n in p.token_sets), default=0.0)
            if score >= TOKEN_THRESHOLD and (best is None or _rank(score, p) > best[0]):
                best = (_rank(score, p), AppMatch(p.app, round(score, 3), "token"))
        return best[1] if best else None

    def _fuzzy(self, variants: Sequence[str]) -> AppMatch | None:
        best: tuple[tuple[float, int, int], AppMatch] | None = None
        matcher = difflib.SequenceMatcher(None, autojunk=False)
        for variant in variants:
            cq = variant.replace(" ", "")
            if len(cq) < 3:
                continue
            cutoff = _fuzzy_cutoff(len(cq))
            matcher.set_seq2(cq)
            for p in self._prepared:
                for candidate in (*p.form_compacts, *p.alias_compacts):
                    matcher.set_seq1(candidate)
                    if matcher.real_quick_ratio() < cutoff or matcher.quick_ratio() < cutoff:
                        continue
                    ratio = matcher.ratio()
                    if ratio >= cutoff:
                        score = ratio * 0.9
                        if best is None or _rank(score, p) > best[0]:
                            best = (_rank(score, p), AppMatch(p.app, round(score, 3), "fuzzy"))
            # Misspelled catalog names ("crome", "ספוטיפאי") resolve to the installed catalog app.
            for key, entry in _CATALOG_KEYS.items():
                matcher.set_seq1(key)
                if matcher.real_quick_ratio() < cutoff or matcher.quick_ratio() < cutoff:
                    continue
                ratio = matcher.ratio()
                if ratio < cutoff:
                    continue
                app = self.for_catalog(entry)
                if app is None:
                    continue
                prepared = self._by_identity[id(app)]
                score = ratio * 0.9
                if best is None or _rank(score, prepared) > best[0]:
                    best = (_rank(score, prepared), AppMatch(app, round(score, 3), "fuzzy"))
        return best[1] if best else None

    def _phonetic(self, variants: Sequence[str]) -> AppMatch | None:
        queries = [s for s in (skeleton(v) for v in variants if has_hebrew(v)) if len(s) >= 3]
        if not queries:
            return None
        best: tuple[tuple[float, int, int], AppMatch] | None = None
        for p in self._prepared:
            for qs in queries:
                for s in p.skeletons:
                    if len(s) < 3 or s[0] != qs[0]:
                        continue
                    ratio = difflib.SequenceMatcher(None, qs, s).ratio()
                    if ratio >= PHONETIC_CUTOFF:
                        score = ratio * 0.85
                        if best is None or _rank(score, p) > best[0]:
                            best = (_rank(score, p), AppMatch(p.app, round(score, 3), "phonetic"))
        return best[1] if best else None

    # ------------------------------------------------------------------------------ JSON
    def to_json(self) -> dict[str, Any]:
        return {
            "version": INDEX_VERSION,
            "platform": self.platform,
            "built_at": self.built_at,
            "apps": [app_to_json(a) for a in self.apps],
        }

    @classmethod
    def from_json(cls, data: Any) -> AppIndex:
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            raise ValueError("unsupported app index format")
        apps = [a for a in (app_from_json(item) for item in data.get("apps") or []) if a is not None]
        return cls(apps, built_at=float(data.get("built_at") or 0.0), platform=str(data.get("platform") or ""))


def app_to_json(app: InstalledApp) -> dict[str, Any]:
    return {
        "name": app.name,
        "target": app.target,
        "kind": app.kind,
        "source": app.source,
        "id": app.id,
        "process_names": list(app.process_names),
        "aliases": list(app.aliases),
    }


def app_from_json(item: Any) -> InstalledApp | None:
    if not isinstance(item, dict):
        return None
    name, target, kind = item.get("name"), item.get("target"), item.get("kind")
    if not (isinstance(name, str) and name and isinstance(target, str) and target and kind in APP_KINDS):
        return None
    app_id = item.get("id")
    return InstalledApp(
        name=name,
        target=target,
        kind=kind,
        source=str(item.get("source") or "catalog"),
        id=app_id if isinstance(app_id, str) and app_id else None,
        process_names=tuple(str(x) for x in item.get("process_names") or () if x),
        aliases=tuple(str(x) for x in item.get("aliases") or () if x),
    )


# =============================================================================================
# Cache and store
# =============================================================================================
class AppIndexCache:
    """JSON file holding the last built index (written atomically)."""

    def __init__(self, path: Path | None, *, ttl_s: float = DEFAULT_TTL_S, platform: str = "") -> None:
        self.path = Path(path) if path is not None else None
        self.ttl_s = ttl_s
        self.platform = platform

    def load(self, *, allow_stale: bool = False, now: float | None = None) -> AppIndex | None:
        if self.path is None:
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            index = AppIndex.from_json(data)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as exc:
            log.info("Ignoring unreadable app index cache %s: %s", self.path, exc)
            return None
        if self.platform and index.platform and index.platform != self.platform:
            return None
        if not allow_stale and not index.is_fresh(self.ttl_s, now):
            return None
        return index

    def save(self, index: AppIndex) -> bool:
        if self.path is None:
            return False
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(index.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.warning("Could not write app index cache %s: %s", self.path, exc)
            with contextlib.suppress(OSError):
                tmp.unlink()
            return False

    def clear(self) -> None:
        if self.path is not None:
            with contextlib.suppress(OSError):
                self.path.unlink()


class AppIndexStore:
    """In-memory index backed by `AppIndexCache`; rebuilt with `builder` when stale or on refresh."""

    def __init__(
        self,
        builder: Callable[[], Awaitable[Sequence[InstalledApp]]],
        *,
        cache_file: Path | None = None,
        ttl_s: float = DEFAULT_TTL_S,
        platform: str = "",
    ) -> None:
        self._builder = builder
        self._cache = AppIndexCache(cache_file, ttl_s=ttl_s, platform=platform)
        self.ttl_s = ttl_s
        self.platform = platform
        self._index: AppIndex | None = None
        self._lock: asyncio.Lock | None = None

    @property
    def current(self) -> AppIndex | None:
        return self._index

    @property
    def cache(self) -> AppIndexCache:
        return self._cache

    def invalidate(self) -> None:
        self._index = None
        self._cache.clear()

    async def get(self, *, refresh: bool = False) -> AppIndex:
        if not refresh and self._index is not None and self._index.is_fresh(self.ttl_s):
            return self._index
        if self._lock is None:
            self._lock = asyncio.Lock()
        requested_at = time.time()
        async with self._lock:
            current = self._index
            if current is not None and current.is_fresh(self.ttl_s) and (not refresh or current.built_at >= requested_at):
                return current                  # built (or rebuilt) by another caller while we waited
            if not refresh:
                cached = await asyncio.to_thread(self._cache.load)
                if cached is not None:
                    self._index = cached
                    return cached
            try:
                apps = await self._builder()
            except Exception:
                stale = self._index or await asyncio.to_thread(self._cache.load, allow_stale=True)
                if stale is not None:
                    log.exception("Rebuilding the app index failed; using the previous index")
                    self._index = stale
                    return stale
                raise
            index = AppIndex(apps, platform=self.platform)
            await asyncio.to_thread(self._cache.save, index)
            self._index = index
            log.info("App index built: %d apps", len(index))
            return index


# =============================================================================================
# Helpers shared by the host adapters
# =============================================================================================
# Executables that only launch something else: a shortcut to them says nothing about the
# process that ends up running (and closing them by name would hit unrelated programs).
WINDOWS_WRAPPER_EXES = frozenset(
    {
        "explorer.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "rundll32.exe",
        "mshta.exe", "msiexec.exe", "conhost.exe", "javaw.exe", "java.exe", "python.exe", "pythonw.exe", "py.exe",
        "pyw.exe", "update.exe", "squirrel.exe", "control.exe", "mmc.exe", "wt.exe", "wsl.exe", "bash.exe",
    }
)
POSIX_WRAPPERS = frozenset(
    {"env", "sh", "bash", "dash", "zsh", "flatpak", "snap", "python", "python3", "java", "gtk-launch", "open",
     "xdg-open", "gio", "dbus-launch", "sudo", "pkexec", "nohup", "exec", "kioclient5", "kioclient"}
)


def exe_basename(path: str) -> str:
    """Lower-case file name of a Windows or POSIX path ("C:\\X\\Chrome.EXE" → "chrome.exe")."""
    text = (path or "").strip().strip('"')
    return ntpath.basename(text).lower() if text else ""


def command_head(command: str) -> str:
    """Lower-case program name of a command line ("/usr/bin/gedit %U" → "gedit", "notepad.exe" → "notepad.exe")."""
    text = (command or "").strip()
    if not text:
        return ""
    if "\\" in text:                                   # Windows path: quoted, or up to the ".exe"
        if text.startswith('"'):
            first = text[1:].split('"', 1)[0]
        else:
            m = re.match(r"(.+?\.(?:exe|com|bat|cmd))(?:\s|$)", text, re.I)
            first = m.group(1) if m else text.split()[0]
        return ntpath.basename(first).lower()
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    return posixpath.basename(parts[0]).lower() if parts else ""


def _windows_process_name(name: str) -> str | None:
    n = exe_basename(name)
    if not n or n.endswith(".exe"):
        return n or None
    if re.search(r"\.(?:cmd|bat|lnk|url|com|msc|ps1|vbs|js|app)$", n):
        return None
    return n + ".exe"


def _posix_process_name(name: str) -> str | None:
    n = posixpath.basename((name or "").strip().strip('"')).lower()
    if n.endswith(".exe"):
        n = n[:-4]
    return n or None


def candidate_process_names(
    name: str,
    *,
    app: InstalledApp | None = None,
    entry: AppEntry | None = None,
    windows: bool,
) -> list[str]:
    """Process names that belong to an app the user named (lower-case; ".exe" on Windows).

    Explicit process names (catalog / discovered) are trusted; a launch target only counts
    when it is not a generic launcher (cmd.exe, Update.exe, env, flatpak…), and the spoken
    name itself counts when it is a single ASCII word ("obsidian" → obsidian.exe).
    """
    to_proc = _windows_process_name if windows else _posix_process_name
    wrappers = WINDOWS_WRAPPER_EXES if windows else POSIX_WRAPPERS
    raw: list[str] = []
    if entry is not None:
        raw.extend(entry.process_names)
        if not entry.process_names:
            heads = (command_head(t) for t in (entry.windows if windows else entry.posix))
            raw.extend(h for h in heads if h and h not in wrappers)
    if app is not None:
        raw.extend(app.process_names)
        if app.kind == "exe":
            head = exe_basename(app.target)
        elif app.kind == "command":
            head = command_head(app.target)
        else:
            head = ""
        if head and head not in wrappers:
            raw.append(head)
    cleaned = clean_query(name)
    if cleaned and " " not in cleaned and cleaned.isascii():
        raw.append(cleaned)
    spoken = normalize(name).strip().strip('"')
    if windows and spoken.endswith(".exe") and "\\" not in spoken and "/" not in spoken:
        raw.append(spoken)
    out: list[str] = []
    for item in raw:
        proc = to_proc(item) if item else None
        if proc and proc not in out:
            out.append(proc)
    return out


def validate_web_url(url: str) -> str:
    """Return a normalized http(s) URL or raise `HostUnsupported` ("youtube.com" → "https://youtube.com")."""
    text = (url or "").strip()
    if not text or any(ord(ch) < 32 or ch.isspace() for ch in text):
        raise HostUnsupported(f"That doesn't look like a web address: {url!r}")
    if "://" not in text and catalog.looks_like_url(text):
        text = catalog.to_url(text)
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise HostUnsupported(f"Only http:// and https:// links can be opened (got {url!r}).")
    return text


def screenshot_target(path: Path | str, *, now: float | None = None) -> Path:
    """Where a screenshot should be written: a folder gets a timestamped file, and the suffix is .png."""
    target = Path(path).expanduser()
    if target.is_dir():
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        return target / f"screenshot-{stamp}.png"
    if target.suffix.lower() != ".png":
        target = target.with_suffix(".png")
    return target


class DelayedActions:
    """Timers for delayed power actions (sleep / log off in N seconds); `cancel_all` stops them."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending(self) -> int:
        return sum(1 for t in self._tasks if not t.done())

    def schedule(self, delay_s: float, action: Callable[[], Awaitable[None]], label: str = "action") -> asyncio.Task[None]:
        async def runner() -> None:
            await asyncio.sleep(max(0.0, delay_s))
            try:
                await action()
            except Exception:
                log.exception("Delayed %s failed", label)

        task = asyncio.get_running_loop().create_task(runner(), name=f"jarvis-delayed-{label}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def cancel_all(self) -> int:
        cancelled = 0
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                cancelled += 1
        self._tasks.clear()
        return cancelled
