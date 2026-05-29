"""Tests for SQLiteAdapter — T-02."""

from __future__ import annotations

import sqlite3
import threading
import time
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


# ── Thread safety (B-47 regression) ────────────────────────────────────────


class TestThreadSafety:
    """Concurrent reads/writes against the shared connection must not raise
    ``sqlite3.InterfaceError: bad parameter or other API misuse`` (B-47)."""

    def test_concurrent_writes_and_reads(self, adapter: SQLiteAdapter) -> None:
        adapter.execute(
            "INSERT INTO symbiotes (id, name, role) VALUES (?, ?, ?)",
            ("sym-1", "Atlas", "assistant"),
        )

        threads_n = 12
        per_thread = 25
        errors: list[Exception] = []
        barrier = threading.Barrier(threads_n)

        def writer(tid: int) -> None:
            barrier.wait()  # maximize contention: release all threads at once
            try:
                for i in range(per_thread):
                    adapter.execute(
                        "INSERT INTO memory_entries "
                        "(id, symbiote_id, type, scope, content) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (f"m-{tid}-{i}", "sym-1", "working", "session", "x"),
                    )
                    adapter.fetch_all(
                        "SELECT id FROM memory_entries WHERE symbiote_id = ?",
                        ("sym-1",),
                    )
            except Exception as exc:  # noqa: BLE001 — capture for assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(tid,)) for tid in range(threads_n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"concurrent access raised: {errors!r}"
        # Every insert must have landed — no lost writes under the lock.
        row = adapter.fetch_one("SELECT COUNT(*) AS n FROM memory_entries")
        assert row is not None
        assert row["n"] == threads_n * per_thread

    def test_close_during_concurrent_writes_no_segfault(
        self, tmp_path: Path
    ) -> None:
        """``close()`` concurrent with an in-flight write must not segfault.

        With ``check_same_thread=False`` a background thread can drive the shared
        connection; freeing it mid-``execute`` from another thread is a native
        use-after-free (segfault) that no ``try/except`` can catch. ``close()``
        takes the lock, so it waits for the in-flight statement; a statement that
        arrives *after* close raises a catchable ``ProgrammingError`` instead.

        The process surviving to the assertions is itself the proof that no
        segfault occurred — a crash would take the whole interpreter down.
        """
        adp = SQLiteAdapter(db_path=tmp_path / "close_race.db")
        adp.init_schema()
        adp.execute(
            "INSERT INTO symbiotes (id, name, role) VALUES (?, ?, ?)",
            ("sym-1", "Atlas", "assistant"),
        )

        stop = threading.Event()
        errors: list[Exception] = []
        started = threading.Barrier(5)

        def writer(tid: int) -> None:
            started.wait()
            i = 0
            while not stop.is_set():
                try:
                    adp.execute(
                        "INSERT INTO memory_entries "
                        "(id, symbiote_id, type, scope, content) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (f"m-{tid}-{i}", "sym-1", "working", "session", "x"),
                    )
                except sqlite3.ProgrammingError:
                    # Expected once the connection is closed underneath us.
                    return
                except Exception as exc:  # noqa: BLE001 — any other error is a bug
                    errors.append(exc)
                    return
                i += 1

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        started.wait()  # release all writers together
        time.sleep(0.01)  # let writes get in flight, then close mid-stream
        adp.close()
        stop.set()
        for t in threads:
            t.join()

        # Only ProgrammingError (closed connection) is tolerated; the B-47
        # symptom (InterfaceError) or anything else means the race is unfixed.
        assert errors == [], f"unexpected errors during close race: {errors!r}"
