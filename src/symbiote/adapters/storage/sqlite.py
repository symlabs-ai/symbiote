"""SQLite storage adapter — stdlib sqlite3, no ORM."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS symbiotes (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT,
    owner_id    TEXT,
    persona_json TEXT,
    status      TEXT DEFAULT 'active',
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    symbiote_id  TEXT REFERENCES symbiotes(id),
    goal         TEXT,
    workspace_id TEXT,
    status       TEXT DEFAULT 'active',
    started_at   TEXT,
    ended_at     TEXT,
    summary      TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    session_id  TEXT REFERENCES sessions(id),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS memory_entries (
    id            TEXT PRIMARY KEY,
    symbiote_id   TEXT REFERENCES symbiotes(id),
    session_id    TEXT,
    type          TEXT NOT NULL,
    scope         TEXT NOT NULL,
    content       TEXT NOT NULL,
    tags_json     TEXT DEFAULT '[]',
    importance    REAL DEFAULT 0.5,
    source        TEXT,
    confidence    REAL DEFAULT 1.0,
    created_at    TEXT,
    last_used_at  TEXT,
    is_active     INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS knowledge_entries (
    id            TEXT PRIMARY KEY,
    symbiote_id   TEXT REFERENCES symbiotes(id),
    name          TEXT NOT NULL,
    source_path   TEXT,
    content       TEXT,
    type          TEXT,
    tags_json     TEXT DEFAULT '[]',
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS workspaces (
    id            TEXT PRIMARY KEY,
    symbiote_id   TEXT REFERENCES symbiotes(id),
    name          TEXT NOT NULL,
    root_path     TEXT,
    type          TEXT DEFAULT 'general',
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY,
    session_id    TEXT REFERENCES sessions(id),
    workspace_id  TEXT REFERENCES workspaces(id),
    path          TEXT,
    type          TEXT,
    description   TEXT,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS environment_configs (
    id              TEXT PRIMARY KEY,
    symbiote_id     TEXT REFERENCES symbiotes(id),
    workspace_id    TEXT,
    tools_json      TEXT DEFAULT '[]',
    services_json   TEXT DEFAULT '[]',
    humans_json     TEXT DEFAULT '[]',
    policies_json   TEXT DEFAULT '{}',
    resources_json  TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    session_id  TEXT REFERENCES sessions(id),
    title       TEXT NOT NULL,
    description TEXT,
    tags_json   TEXT DEFAULT '[]',
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS process_instances (
    id            TEXT PRIMARY KEY,
    session_id    TEXT REFERENCES sessions(id),
    process_name  TEXT NOT NULL,
    state         TEXT DEFAULT 'running',
    current_step  TEXT,
    logs_json     TEXT DEFAULT '[]',
    created_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            TEXT PRIMARY KEY,
    symbiote_id   TEXT REFERENCES symbiotes(id),
    session_id    TEXT,
    tool_id       TEXT NOT NULL,
    action        TEXT NOT NULL,
    params_json   TEXT,
    result        TEXT,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS persona_audit (
    id               TEXT PRIMARY KEY,
    symbiote_id      TEXT NOT NULL,
    old_persona_json TEXT NOT NULL,
    new_persona_json TEXT NOT NULL,
    changed_at       TEXT NOT NULL
);
"""


class SQLiteAdapter:
    """Thin wrapper around stdlib ``sqlite3`` with WAL + foreign keys."""

    def __init__(self, db_path: Path, *, check_same_thread: bool = True) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=check_same_thread
        )
        self._conn.row_factory = sqlite3.Row

        # Enable WAL mode and foreign key enforcement.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    # ── public API ─────────────────────────────────────────────────────

    def init_schema(self) -> None:
        """Create all tables if they don't already exist."""
        self._conn.executescript(_SCHEMA_SQL)

    def execute(self, sql: str, params: tuple | None = None) -> sqlite3.Cursor:
        """Run an INSERT / UPDATE / DELETE and auto-commit."""
        cur = self._conn.execute(sql, params or ())
        self._conn.commit()
        return cur

    def fetch_one(self, sql: str, params: tuple | None = None) -> dict | None:
        """Return a single row as a dict, or ``None``."""
        cur = self._conn.execute(sql, params or ())
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def fetch_all(self, sql: str, params: tuple | None = None) -> list[dict]:
        """Return all matching rows as a list of dicts."""
        cur = self._conn.execute(sql, params or ())
        return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()
