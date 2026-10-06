"""SQLite implementation of the memory store (WAL mode, FTS5 full-text search).

The search index uses the FTS5 `trigram` tokenizer when available: it matches substrings,
which suits Hebrew, where prefixes attach to words ("הפרויקט" matches "פרויקט").
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.common.clock import parse_iso, utcnow
from jarvis.common.errors import MemoryError_
from jarvis.memory.models import (
    ChatRecord,
    CommandRecord,
    Fact,
    FileRecord,
    ProjectRecord,
    ResultRecord,
    SearchHit,
    TaskRecord,
)
from jarvis.memory.store import MemoryStore

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (
    id TEXT PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL, ts TEXT NOT NULL,
    source TEXT, language TEXT, kind TEXT, task_id TEXT, intent TEXT
);
CREATE INDEX IF NOT EXISTS chat_ts ON chat(ts);
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY, text TEXT NOT NULL, category TEXT NOT NULL, source TEXT, importance INTEGER,
    task_id TEXT, project_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_category ON facts(category);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT, description TEXT, status TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, meta TEXT
);
CREATE INDEX IF NOT EXISTS projects_name ON projects(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT, goal TEXT, status TEXT, priority TEXT, mode TEXT, source TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    summary TEXT, error TEXT, project_id TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created_at);
CREATE TABLE IF NOT EXISTS task_context (
    task_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, key)
);
CREATE TABLE IF NOT EXISTS results (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, subtask_id TEXT, agent_id TEXT, status TEXT,
    summary TEXT, data TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS results_task ON results(task_id);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT, size INTEGER, kind TEXT, task_id TEXT,
    agent_id TEXT, project_id TEXT, important INTEGER, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS files_created ON files(created_at);
CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT, intent TEXT, task_id TEXT, ok INTEGER, ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS commands_ts ON commands(ts);
"""

_WORD = re.compile(r"[\w'\-\.]+", re.UNICODE)


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _dt(value: str | None) -> datetime | None:
    return parse_iso(value) if value else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class SQLiteMemoryStore(MemoryStore):
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._db: sqlite3.Connection | None = None
        self.tokenizer = "trigram"

    # -- lifecycle -----------------------------------------------------------------------------
    def open(self) -> None:
        if self._db is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        except sqlite3.Error as exc:
            raise MemoryError_(f"Cannot open the memory database {self.path}: {exc}") from exc
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(_SCHEMA)
        self._create_index(db)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.commit()
        self._db = db

    def _create_index(self, db: sqlite3.Connection) -> None:
        row = db.execute("SELECT sql FROM sqlite_master WHERE name='search_index'").fetchone()
        if row is not None:
            self.tokenizer = "trigram" if "trigram" in (row["sql"] or "") else "unicode61"
            return
        try:
            db.execute("CREATE VIRTUAL TABLE search_index USING fts5(layer UNINDEXED, ref UNINDEXED, "
                       "ts UNINDEXED, text, tokenize='trigram')")
            self.tokenizer = "trigram"
        except sqlite3.OperationalError:
            db.execute("CREATE VIRTUAL TABLE search_index USING fts5(layer UNINDEXED, ref UNINDEXED, "
                       "ts UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')")
            self.tokenizer = "unicode61"

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.commit()
                self._db.close()
            finally:
                self._db = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.open()
        assert self._db is not None
        return self._db

    def _index(self, layer: str, ref: str, text: str, ts: datetime | None) -> None:
        self.db.execute("DELETE FROM search_index WHERE layer=? AND ref=?", (layer, ref))
        if text.strip():
            self.db.execute("INSERT INTO search_index(layer, ref, ts, text) VALUES (?,?,?,?)",
                            (layer, ref, _ts(ts), text))

    def _unindex(self, layer: str, ref: str) -> None:
        self.db.execute("DELETE FROM search_index WHERE layer=? AND ref=?", (layer, ref))

    # -- chat ----------------------------------------------------------------------------------
    def add_chat(self, rec: ChatRecord) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO chat(id, role, text, ts, source, language, kind, task_id, intent) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (rec.id, rec.role, rec.text, _ts(rec.ts), rec.source, rec.language, rec.kind, rec.task_id, rec.intent),
        )
        self._index("conversation", rec.id, f"{rec.role}: {rec.text}", rec.ts)
        self.db.commit()

    def recent_chat(self, limit: int, *, before: str | None = None) -> list[ChatRecord]:
        if before:
            rows = self.db.execute("SELECT * FROM chat WHERE ts < ? ORDER BY ts DESC LIMIT ?", (before, limit))
        else:
            rows = self.db.execute("SELECT * FROM chat ORDER BY ts DESC LIMIT ?", (limit,))
        out = [ChatRecord(id=r["id"], role=r["role"], text=r["text"], ts=_dt(r["ts"]), source=r["source"] or "text",
                          language=r["language"] or "he", kind=r["kind"] or "answer", task_id=r["task_id"],
                          intent=r["intent"]) for r in rows]
        return list(reversed(out))

    # -- facts ---------------------------------------------------------------------------------
    def upsert_fact(self, fact: Fact) -> Fact:
        fact.updated_at = utcnow()
        self.db.execute(
            "INSERT OR REPLACE INTO facts(id, text, category, source, importance, task_id, project_id, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (fact.id, fact.text, fact.category, fact.source, fact.importance, fact.task_id, fact.project_id,
             _ts(fact.created_at), _ts(fact.updated_at)),
        )
        self._index("long_term", fact.id, f"[{fact.category}] {fact.text}", fact.updated_at)
        self.db.commit()
        return fact

    def delete_fact(self, fact_id: str) -> bool:
        cur = self.db.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        self._unindex("long_term", fact_id)
        self.db.commit()
        return cur.rowcount > 0

    def _fact(self, r: sqlite3.Row) -> Fact:
        return Fact(id=r["id"], text=r["text"], category=r["category"], source=r["source"] or "user",
                    importance=r["importance"] or 1, task_id=r["task_id"], project_id=r["project_id"],
                    created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]))

    def list_facts(self, *, category: str | None = None, query: str | None = None, limit: int = 100,
                   project_id: str | None = None) -> list[Fact]:
        sql = "SELECT * FROM facts WHERE 1=1"
        args: list[Any] = []
        if category:
            sql += " AND category=?"
            args.append(category)
        if project_id:
            sql += " AND project_id=?"
            args.append(project_id)
        if query:
            sql += " AND text LIKE ?"
            args.append(f"%{query}%")
        sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        args.append(limit)
        return [self._fact(r) for r in self.db.execute(sql, args)]

    # -- projects ------------------------------------------------------------------------------
    def upsert_project(self, project: ProjectRecord) -> ProjectRecord:
        project.updated_at = utcnow()
        self.db.execute(
            "INSERT OR REPLACE INTO projects(id, name, path, description, status, created_at, updated_at, meta) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (project.id, project.name, project.path, project.description, project.status, _ts(project.created_at),
             _ts(project.updated_at), _json(project.meta)),
        )
        self._index("project", project.id, f"{project.name} {project.description} {project.path or ''}",
                    project.updated_at)
        self.db.commit()
        return project

    def _project(self, r: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(id=r["id"], name=r["name"], path=r["path"], description=r["description"] or "",
                             status=r["status"] or "active", created_at=_dt(r["created_at"]),
                             updated_at=_dt(r["updated_at"]), meta=_load(r["meta"], {}))

    def get_project(self, project_id: str) -> ProjectRecord | None:
        r = self.db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self._project(r) if r else None

    def find_project(self, name_or_path: str) -> ProjectRecord | None:
        key = name_or_path.strip()
        r = self.db.execute(
            "SELECT * FROM projects WHERE name=? COLLATE NOCASE OR path=? COLLATE NOCASE ORDER BY updated_at DESC",
            (key, key),
        ).fetchone()
        if r is None:
            r = self.db.execute("SELECT * FROM projects WHERE name LIKE ? ORDER BY updated_at DESC",
                                (f"%{key}%",)).fetchone()
        return self._project(r) if r else None

    def list_projects(self, limit: int = 100) -> list[ProjectRecord]:
        return [self._project(r) for r in self.db.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC LIMIT ?", (limit,))]

    # -- tasks ---------------------------------------------------------------------------------
    def save_task(self, task: TaskRecord) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tasks(id, title, goal, status, priority, mode, source, created_at, updated_at, "
            "started_at, finished_at, summary, error, project_id, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task.id, task.title, task.goal, task.status, task.priority, task.mode, task.source, _ts(task.created_at),
             _ts(task.updated_at), _ts(task.started_at), _ts(task.finished_at), task.summary, task.error,
             task.project_id, _json(task.data)),
        )
        self._index("task", task.id, " ".join(x for x in (task.title, task.goal, task.summary or "") if x),
                    task.updated_at)
        self.db.commit()

    def _task(self, r: sqlite3.Row) -> TaskRecord:
        return TaskRecord(id=r["id"], title=r["title"] or "", goal=r["goal"] or "", status=r["status"] or "pending",
                          priority=r["priority"] or "normal", mode=r["mode"] or "normal", source=r["source"] or "text",
                          created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),
                          started_at=_dt(r["started_at"]), finished_at=_dt(r["finished_at"]), summary=r["summary"],
                          error=r["error"], project_id=r["project_id"], data=_load(r["data"], {}))

    def get_task(self, task_id: str) -> TaskRecord | None:
        r = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(r) if r else None

    def list_tasks(self, *, statuses: list[str] | None = None, limit: int = 50) -> list[TaskRecord]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            rows = self.db.execute(f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY created_at DESC LIMIT ?",
                                   (*statuses, limit))
        else:
            rows = self.db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._task(r) for r in rows]

    def set_context(self, task_id: str, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO task_context(task_id, key, value, updated_at) VALUES (?,?,?,?)",
                        (task_id, key, _json(value), _ts(utcnow())))
        self.db.commit()

    def get_context(self, task_id: str) -> dict[str, Any]:
        rows = self.db.execute("SELECT key, value FROM task_context WHERE task_id=? ORDER BY updated_at", (task_id,))
        return {r["key"]: _load(r["value"], None) for r in rows}

    def add_result(self, result: ResultRecord) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO results(id, task_id, subtask_id, agent_id, status, summary, data, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (result.id, result.task_id, result.subtask_id, result.agent_id, result.status, result.summary,
             _json(result.data), _ts(result.created_at)),
        )
        self._index("task", f"result:{result.id}", result.summary, result.created_at)
        self.db.commit()

    def list_results(self, task_id: str) -> list[ResultRecord]:
        rows = self.db.execute("SELECT * FROM results WHERE task_id=? ORDER BY created_at", (task_id,))
        return [ResultRecord(id=r["id"], task_id=r["task_id"], subtask_id=r["subtask_id"], agent_id=r["agent_id"],
                             status=r["status"] or "success", summary=r["summary"] or "", data=_load(r["data"], {}),
                             created_at=_dt(r["created_at"])) for r in rows]

    # -- files ---------------------------------------------------------------------------------
    def add_file(self, rec: FileRecord) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO files(id, path, name, size, kind, task_id, agent_id, project_id, important, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rec.id, rec.path, rec.name, rec.size, rec.kind, rec.task_id, rec.agent_id, rec.project_id,
             int(rec.important), _ts(rec.created_at)),
        )
        self._index("file", rec.id, f"{rec.name} {rec.path}", rec.created_at)
        self.db.commit()

    def list_files(self, *, limit: int = 30, task_id: str | None = None, project_id: str | None = None,
                   important: bool | None = None) -> list[FileRecord]:
        sql = "SELECT * FROM files WHERE 1=1"
        args: list[Any] = []
        if task_id:
            sql += " AND task_id=?"
            args.append(task_id)
        if project_id:
            sql += " AND project_id=?"
            args.append(project_id)
        if important is not None:
            sql += " AND important=?"
            args.append(int(important))
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [FileRecord(id=r["id"], path=r["path"], name=r["name"] or "", size=r["size"], kind=r["kind"] or "",
                           task_id=r["task_id"], agent_id=r["agent_id"], project_id=r["project_id"],
                           important=bool(r["important"]), created_at=_dt(r["created_at"]))
                for r in self.db.execute(sql, args)]

    # -- commands ------------------------------------------------------------------------------
    def add_command(self, rec: CommandRecord) -> None:
        self.db.execute("INSERT OR REPLACE INTO commands(id, text, source, intent, task_id, ok, ts) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (rec.id, rec.text, rec.source, rec.intent, rec.task_id,
                         None if rec.ok is None else int(rec.ok), _ts(rec.ts)))
        self._index("command", rec.id, rec.text, rec.ts)
        self.db.commit()

    def update_command(self, command_id: str, *, ok: bool | None = None, task_id: str | None = None,
                       intent: str | None = None) -> None:
        sets, args = [], []
        for col, val in (("ok", None if ok is None else int(ok)), ("task_id", task_id), ("intent", intent)):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        if sets:
            self.db.execute(f"UPDATE commands SET {', '.join(sets)} WHERE id=?", (*args, command_id))
            self.db.commit()

    def list_commands(self, limit: int = 50) -> list[CommandRecord]:
        rows = self.db.execute("SELECT * FROM commands ORDER BY ts DESC LIMIT ?", (limit,))
        return [CommandRecord(id=r["id"], text=r["text"], source=r["source"] or "text", intent=r["intent"],
                              task_id=r["task_id"], ok=None if r["ok"] is None else bool(r["ok"]), ts=_dt(r["ts"]))
                for r in rows]

    # -- search --------------------------------------------------------------------------------
    def _match_expr(self, query: str) -> str | None:
        words = [w.strip(".-'") for w in _WORD.findall(query)]
        if self.tokenizer == "trigram":
            terms = [w for w in words if len(w) >= 3]
            return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms) or None
        terms = [w for w in words if len(w) >= 2]
        return " OR ".join('"' + t.replace('"', '""') + '"*' for t in terms) or None

    def search(self, query: str, *, limit: int = 20, layers: list[str] | None = None) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        layer_sql, layer_args = "", []
        if layers:
            layer_sql = f" AND layer IN ({','.join('?' * len(layers))})"
            layer_args = list(layers)
        expr = self._match_expr(query)
        try:
            if expr:
                rows = self.db.execute(
                    f"SELECT layer, ref, ts, text, bm25(search_index) AS rank FROM search_index "
                    f"WHERE search_index MATCH ?{layer_sql} ORDER BY rank LIMIT ?",
                    (expr, *layer_args, limit),
                ).fetchall()
            else:
                rows = self.db.execute(
                    f"SELECT layer, ref, ts, text, 0.0 AS rank FROM search_index WHERE text LIKE ?{layer_sql} "
                    f"ORDER BY ts DESC LIMIT ?",
                    (f"%{query}%", *layer_args, limit),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            log.warning("memory search failed for %r: %s", query, exc)
            return []
        return [SearchHit(layer=r["layer"], id=r["ref"], text=r["text"], score=round(-float(r["rank"]), 6),
                          ts=_dt(r["ts"])) for r in rows]

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("chat", "facts", "projects", "tasks", "results", "files", "commands"):
            out[table] = int(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return out
