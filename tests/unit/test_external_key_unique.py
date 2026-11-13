"""Regression: concurrent get_or_create_session with a new external_key
creates exactly one session (report item M-10)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from tests.support import ScriptedLLM, make_kernel


def test_concurrent_get_or_create_makes_one_session(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="r")
    created_per_round = []
    for rnd in range(20):
        key = f"user-{rnd}:chat"
        ids: list[str] = []
        barrier = threading.Barrier(8)

        def worker(key=key, ids=ids, barrier=barrier) -> None:
            barrier.wait()
            ids.append(kernel.get_or_create_session(sym.id, external_key=key).id)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        created_per_round.append(len(set(ids)))

    assert set(created_per_round) == {1}
    kernel.shutdown()


def test_unique_index_exists(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    rows = kernel._storage.fetch_all(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'sessions'"
    )
    assert any(r["sql"] and "UNIQUE" in r["sql"].upper() and "external_key" in r["sql"] for r in rows)
    kernel.shutdown()


def test_existing_duplicates_do_not_break_boot(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.shutdown()
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX IF EXISTS idx_sessions_symbiote_external_key")
    for i in range(2):
        conn.execute(
            "INSERT INTO sessions (id, symbiote_id, external_key, status, started_at) "
            "VALUES (?, ?, 'dup', 'active', '2026-01-01')",
            (f"s{i}", sym.id),
        )
    conn.commit()
    conn.close()

    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)  # must not raise
    assert kernel.get_or_create_session(sym.id, external_key="dup").id in {"s0", "s1"}
    kernel.shutdown()
