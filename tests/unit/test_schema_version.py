"""Regression: numbered migrations tracked by PRAGMA user_version — an old
database boots and converges with a fresh one, init_schema is idempotent
(report item Q-5)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from symbiote.adapters.storage.sqlite import SCHEMA_VERSION, SQLiteAdapter

# sessions as created by the first releases (no external_key column).
_OLD_SCHEMA = """
CREATE TABLE symbiotes (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT, owner_id TEXT,
    persona_json TEXT, status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, symbiote_id TEXT REFERENCES symbiotes(id), goal TEXT,
    workspace_id TEXT, status TEXT DEFAULT 'active', started_at TEXT, ended_at TEXT,
    summary TEXT
);
INSERT INTO symbiotes (id, name) VALUES ('s1', 'old');
INSERT INTO sessions (id, symbiote_id, goal) VALUES ('x1', 's1', 'kept');
"""


def _shape(db: Path) -> tuple[dict[str, set[str]], set[str]]:
    conn = sqlite3.connect(db)
    try:
        tables = {
            t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()
    return tables, indexes


def _init(db: Path) -> None:
    adapter = SQLiteAdapter(db_path=db)
    adapter.init_schema()
    adapter.close()


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_old_database_boots_and_converges(tmp_path: Path) -> None:
    fresh, old = tmp_path / "fresh.db", tmp_path / "old.db"
    _init(fresh)
    conn = sqlite3.connect(old)
    conn.executescript(_OLD_SCHEMA)
    conn.close()

    _init(old)

    assert _shape(old) == _shape(fresh)
    conn = sqlite3.connect(old)
    assert conn.execute("SELECT goal FROM sessions WHERE id = 'x1'").fetchone() == ("kept",)
    conn.close()


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "twice.db"
    _init(db)
    first = _shape(db)

    _init(db)

    assert _shape(db) == first
    assert _user_version(db) == SCHEMA_VERSION


def test_user_version_tracks_the_last_migration(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    assert SCHEMA_VERSION >= 1
    _init(db)
    assert _user_version(db) == SCHEMA_VERSION
