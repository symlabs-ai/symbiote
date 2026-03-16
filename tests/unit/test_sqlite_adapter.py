"""Tests for SQLiteAdapter — T-02."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter

EXPECTED_TABLES = [
    "symbiotes",
    "sessions",
    "messages",
    "memory_entries",
    "knowledge_entries",
    "workspaces",
    "artifacts",
    "environment_configs",
    "decisions",
    "process_instances",
    "audit_log",
]


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    """Return an initialised SQLiteAdapter backed by a temp file."""
    db = tmp_path / "data" / "test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


# ── Schema creation ────────────────────────────────────────────────────────


class TestSchemaCreation:
    def test_all_tables_exist(self, adapter: SQLiteAdapter) -> None:
        rows = adapter.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = {r["name"] for r in rows}
        for table in EXPECTED_TABLES:
            assert table in table_names, f"Missing table: {table}"

    def test_init_schema_is_idempotent(self, adapter: SQLiteAdapter) -> None:
        # calling a second time must not raise
        adapter.init_schema()
        rows = adapter.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = {r["name"] for r in rows}
        for table in EXPECTED_TABLES:
            assert table in table_names


# ── Connection pragmas ─────────────────────────────────────────────────────


class TestConnectionPragmas:
    def test_wal_mode_is_active(self, adapter: SQLiteAdapter) -> None:
        row = adapter.fetch_one("PRAGMA journal_mode")
        assert row is not None
        assert row["journal_mode"] == "wal"

    def test_foreign_keys_enabled(self, adapter: SQLiteAdapter) -> None:
        row = adapter.fetch_one("PRAGMA foreign_keys")
        assert row is not None
        assert row["foreign_keys"] == 1


# ── CRUD operations ────────────────────────────────────────────────────────


class TestCRUD:
    def test_insert_and_fetch_symbiote(self, adapter: SQLiteAdapter) -> None:
        adapter.execute(
            "INSERT INTO symbiotes (id, name, role, status) VALUES (?, ?, ?, ?)",
            ("s1", "TestBot", "coder", "active"),
        )
        row = adapter.fetch_one("SELECT * FROM symbiotes WHERE id = ?", ("s1",))
        assert row is not None
        assert row["name"] == "TestBot"
        assert row["role"] == "coder"
        assert row["status"] == "active"

    def test_fetch_one_returns_none_for_missing(self, adapter: SQLiteAdapter) -> None:
        row = adapter.fetch_one(
            "SELECT * FROM symbiotes WHERE id = ?", ("nonexistent",)
        )
        assert row is None

    def test_fetch_all_returns_empty_list(self, adapter: SQLiteAdapter) -> None:
        rows = adapter.fetch_all("SELECT * FROM symbiotes")
        assert rows == []

    def test_fetch_all_returns_list_of_dicts(self, adapter: SQLiteAdapter) -> None:
        adapter.execute(
            "INSERT INTO symbiotes (id, name, role) VALUES (?, ?, ?)",
            ("s1", "A", "r1"),
        )
        adapter.execute(
            "INSERT INTO symbiotes (id, name, role) VALUES (?, ?, ?)",
            ("s2", "B", "r2"),
        )
        rows = adapter.fetch_all("SELECT * FROM symbiotes ORDER BY name")
        assert len(rows) == 2
        assert rows[0]["name"] == "A"
        assert rows[1]["name"] == "B"

    def test_update_symbiote(self, adapter: SQLiteAdapter) -> None:
        adapter.execute(
            "INSERT INTO symbiotes (id, name, role) VALUES (?, ?, ?)",
            ("s1", "Old", "coder"),
        )
        adapter.execute(
            "UPDATE symbiotes SET name = ? WHERE id = ?", ("New", "s1")
        )
        row = adapter.fetch_one("SELECT * FROM symbiotes WHERE id = ?", ("s1",))
        assert row is not None
        assert row["name"] == "New"

    def test_delete_symbiote(self, adapter: SQLiteAdapter) -> None:
        adapter.execute(
            "INSERT INTO symbiotes (id, name, role) VALUES (?, ?, ?)",
            ("s1", "Del", "coder"),
        )
        adapter.execute("DELETE FROM symbiotes WHERE id = ?", ("s1",))
        row = adapter.fetch_one("SELECT * FROM symbiotes WHERE id = ?", ("s1",))
        assert row is None


# ── Foreign key enforcement ────────────────────────────────────────────────


class TestForeignKeys:
    def test_session_fk_rejects_invalid_symbiote(
        self, adapter: SQLiteAdapter
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            adapter.execute(
                "INSERT INTO sessions (id, symbiote_id, status) "
                "VALUES (?, ?, ?)",
                ("sess1", "no_such_symbiote", "active"),
            )

    def test_message_fk_rejects_invalid_session(
        self, adapter: SQLiteAdapter
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            adapter.execute(
                "INSERT INTO messages (id, session_id, role, content) "
                "VALUES (?, ?, ?, ?)",
                ("m1", "no_such_session", "user", "hi"),
            )


# ── Parent directory creation ──────────────────────────────────────────────


class TestDirectoryCreation:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c" / "test.db"
        adp = SQLiteAdapter(db_path=deep)
        adp.init_schema()
        assert deep.parent.exists()
        adp.close()


# ── Protocol conformance (structural check) ───────────────────────────────


class TestProtocol:
    def test_has_required_methods(self, adapter: SQLiteAdapter) -> None:
        assert callable(getattr(adapter, "execute", None))
        assert callable(getattr(adapter, "fetch_one", None))
        assert callable(getattr(adapter, "fetch_all", None))
        assert callable(getattr(adapter, "close", None))
