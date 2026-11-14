"""Regression: connections run in WAL with synchronous=NORMAL — durable
across application crashes, without an fsync per commit (report items P-3,
D-13)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter

# SQLite-specific behaviour (file, pragmas, query plans, legacy migrations).
pytestmark = pytest.mark.sqlite_only


def test_new_connection_uses_wal_and_synchronous_normal(tmp_path: Path) -> None:
    adapter = SQLiteAdapter(db_path=tmp_path / "p.db")
    try:
        assert adapter.fetch_one("PRAGMA journal_mode")["journal_mode"] == "wal"
        assert adapter.fetch_one("PRAGMA synchronous")["synchronous"] == 1  # NORMAL
    finally:
        adapter.close()
