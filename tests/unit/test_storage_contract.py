"""Contract of ``StoragePort``, checked on whatever database the suite runs
on (SQLite by default, PostgreSQL with ``SYMBIOTE_TEST_POSTGRES_DSN``).

The expected values are literals: both databases have to produce exactly
these, which is what keeps the kernel's SQL portable.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.exceptions import StorageIntegrityError
from symbiote.core.sql import contains, differs, like_contains


@pytest.fixture()
def storage(tmp_path: Path):
    adapter = SQLiteAdapter(tmp_path / "contract.db", check_same_thread=False)
    adapter.init_schema()
    yield adapter
    adapter.close()


def _symbiote(storage, sid: str, name: str = "n", created_at: str = "2026-01-01T00:00:00+00:00"):
    storage.execute(
        "INSERT INTO symbiotes (id, name, created_at) VALUES (?, ?, ?)", (sid, name, created_at)
    )


def _count(storage) -> int:
    return storage.fetch_one("SELECT COUNT(*) AS c FROM symbiotes")["c"]


# ── schema ──────────────────────────────────────────────────────────────


def test_init_schema_is_idempotent(storage) -> None:
    _symbiote(storage, "a")
    storage.init_schema()
    assert _count(storage) == 1
    assert {"symbiotes", "sessions", "messages", "memory_entries", "api_keys", "http_tools"} <= set(
        storage.table_names()
    )


def test_same_path_reaches_the_same_data(tmp_path: Path) -> None:
    first = SQLiteAdapter(tmp_path / "shared.db")
    first.init_schema()
    _symbiote(first, "a")
    second = SQLiteAdapter(tmp_path / "shared.db")
    second.init_schema()
    assert _count(second) == 1
    first.close()
    second.close()


# ── writes, values, errors ──────────────────────────────────────────────


def test_execute_reports_rowcount(storage) -> None:
    for sid in ("a", "b", "c"):
        _symbiote(storage, sid)
    assert storage.execute("UPDATE symbiotes SET role = ? WHERE id <> ?", ("r", "c")).rowcount == 2
    assert storage.execute("DELETE FROM symbiotes WHERE id = ?", ("zzz",)).rowcount == 0


def test_values_round_trip(storage) -> None:
    _symbiote(storage, "s")
    storage.execute(
        "INSERT INTO memory_entries (id, symbiote_id, type, scope, content, importance, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("m", "s", "factual", "global", "ação 100% _ok_ \\ 'quoted' ? ", 0.1 + 0.2, True),
    )
    row = storage.fetch_one("SELECT * FROM memory_entries WHERE id = ?", ("m",))
    assert row["content"] == "ação 100% _ok_ \\ 'quoted' ? "
    assert row["importance"] == 0.1 + 0.2  # 8-byte float, not truncated
    assert row["is_active"] == 1 and isinstance(row["is_active"], int)
    assert row["session_id"] is None
    assert row["tags_json"] == "[]"  # column default
    assert "rowid" not in row


def test_aggregates_are_plain_numbers(storage) -> None:
    _symbiote(storage, "s")
    for i, importance in enumerate((0.25, 0.75)):
        storage.execute(
            "INSERT INTO memory_entries (id, symbiote_id, type, scope, content, importance, is_active) "
            "VALUES (?, 's', 't', 'g', 'c', ?, ?)",
            (f"m{i}", importance, i),
        )
    row = storage.fetch_one(
        "SELECT COUNT(*) AS n, AVG(importance) AS avg_f, AVG(is_active) AS avg_i, "
        "SUM(is_active) AS sum_i, MAX(importance) AS top FROM memory_entries"
    )
    assert row == {"n": 2, "avg_f": 0.5, "avg_i": 0.5, "sum_i": 1, "top": 0.75}
    assert type(row["avg_i"]) is float and type(row["sum_i"]) is int


def test_unique_violation_is_a_storage_integrity_error(storage) -> None:
    _symbiote(storage, "a")
    with pytest.raises(StorageIntegrityError):
        _symbiote(storage, "a")
    assert _count(storage) == 1
    _symbiote(storage, "b")  # the adapter is still usable
    assert _count(storage) == 2


def test_foreign_key_violation_is_a_storage_integrity_error(storage) -> None:
    with pytest.raises(StorageIntegrityError):
        storage.execute(
            "INSERT INTO sessions (id, symbiote_id, status) VALUES (?, ?, ?)",
            ("sess", "no-such-symbiote", "active"),
        )


def test_upsert(storage) -> None:
    sql = (
        "INSERT INTO http_tools (tool_id, symbiote_id, descriptor_json, config_json, created_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(tool_id) DO UPDATE SET "
        "descriptor_json = excluded.descriptor_json"
    )
    storage.execute(sql, ("t", "s", "one", "{}", "2026"))
    storage.execute(sql, ("t", "s", "two", "{}", "2027"))
    rows = storage.fetch_all("SELECT descriptor_json, created_at FROM http_tools")
    assert rows == [{"descriptor_json": "two", "created_at": "2026"}]


# ── ordering and comparison ─────────────────────────────────────────────


def test_text_is_ordered_by_code_point(storage) -> None:
    for i, name in enumerate(["b", "B", "a", "Z", "á", "_", "10", "9"]):
        _symbiote(storage, f"s{i}", name)
    names = [r["name"] for r in storage.fetch_all("SELECT name FROM symbiotes ORDER BY name")]
    assert names == sorted(["b", "B", "a", "Z", "á", "_", "10", "9"])
    assert names == ["10", "9", "B", "Z", "_", "a", "b", "á"]


def test_iso_timestamps_compare_as_text(storage) -> None:
    stamps = [
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00.000001+00:00",
        "2026-01-01 00:00:00",
        "2026-01-02T00:00:00+00:00",
    ]
    for i, stamp in enumerate(stamps):
        _symbiote(storage, f"s{i}", created_at=stamp)
    rows = storage.fetch_all(
        "SELECT created_at FROM symbiotes WHERE created_at >= ? ORDER BY created_at",
        ("2026-01-01T00:00:00+00:00",),
    )
    assert [r["created_at"] for r in rows] == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00.000001+00:00",
        "2026-01-02T00:00:00+00:00",
    ]


def test_rowid_breaks_ties_in_insertion_order(storage) -> None:
    for sid in ("m", "a", "z", "c"):
        _symbiote(storage, sid)  # same created_at for all
    newest_first = storage.fetch_all("SELECT id FROM symbiotes ORDER BY created_at DESC, rowid DESC")
    assert [r["id"] for r in newest_first] == ["c", "z", "a", "m"]
    assert storage.fetch_one("SELECT rowid, id FROM symbiotes ORDER BY rowid LIMIT 1")["id"] == "m"


def test_coalesce_puts_nulls_last_when_descending(storage) -> None:
    _symbiote(storage, "s")
    for mid, used in (("never", None), ("old", "2026-01-01"), ("new", "2026-02-01")):
        storage.execute(
            "INSERT INTO memory_entries (id, symbiote_id, type, scope, content, last_used_at) "
            "VALUES (?, 's', 't', 'g', 'c', ?)",
            (mid, used),
        )
    rows = storage.fetch_all(
        "SELECT id FROM memory_entries ORDER BY COALESCE(last_used_at, '') DESC"
    )
    assert [r["id"] for r in rows] == ["new", "old", "never"]


def test_differs_is_null_safe(storage) -> None:
    for sid, role, owner in (("eq", "x", "x"), ("ne", "x", "y"), ("n1", None, "y"), ("nn", None, None)):
        storage.execute(
            "INSERT INTO symbiotes (id, name, role, owner_id) VALUES (?, 'n', ?, ?)",
            (sid, role, owner),
        )
    rows = storage.fetch_all(
        f"SELECT id FROM symbiotes WHERE {differs('role', 'owner_id')} ORDER BY id"
    )
    assert [r["id"] for r in rows] == ["n1", "ne"]


# ── substring search ────────────────────────────────────────────────────

_TEXTS = {
    "plain": "The Gateway restarts at 3am",
    "upper": "GATEWAY CONFIG",
    "accent_lower": "configuração do roteador",
    "accent_upper": "CONFIGURAÇÃO DO ROTEADOR",
    "percent": "usage is 100% today",
    "underscore": "set max_tokens to 10",
    "backslash": "path is C:\\temp\\x",
    "almost": "usage is 100 today, maxAtokens",
}


@pytest.mark.parametrize(
    ("needle", "expected"),
    [
        ("gateway", ["plain", "upper"]),  # ASCII: case-insensitive
        ("GATEWAY", ["plain", "upper"]),
        ("configuração", ["accent_lower"]),  # non-ASCII: case-sensitive
        ("CONFIGURAÇÃO", ["accent_upper"]),
        ("ROTEADOR", ["accent_lower", "accent_upper"]),
        ("100%", ["percent"]),  # % is literal
        ("max_tokens", ["underscore"]),  # _ is literal
        ("C:\\temp", ["backslash"]),  # \ is literal
        ("%", ["percent"]),
        ("nothing-here", []),
    ],
)
def test_contains_gives_the_same_matches_everywhere(storage, needle, expected) -> None:
    _symbiote(storage, "s")
    for mid, text in _TEXTS.items():
        storage.execute(
            "INSERT INTO memory_entries (id, symbiote_id, type, scope, content) "
            "VALUES (?, 's', 't', 'g', ?)",
            (mid, text),
        )
    rows = storage.fetch_all(
        f"SELECT id FROM memory_entries WHERE {contains('content')} ORDER BY id",
        (like_contains(needle),),
    )
    assert [r["id"] for r in rows] == sorted(expected)


# ── transactions ────────────────────────────────────────────────────────


def test_transaction_commits_all(storage) -> None:
    with storage.transaction():
        _symbiote(storage, "a")
        _symbiote(storage, "b")
        assert _count(storage) == 2  # reads its own writes
    assert _count(storage) == 2


def test_transaction_rolls_everything_back(storage) -> None:
    _symbiote(storage, "before")
    with pytest.raises(RuntimeError), storage.transaction():
        _symbiote(storage, "a")
        storage.execute("UPDATE symbiotes SET name = ? WHERE id = ?", ("changed", "before"))
        raise RuntimeError("boom")
    assert storage.fetch_all("SELECT id, name FROM symbiotes") == [{"id": "before", "name": "n"}]


def test_integrity_error_rolls_the_transaction_back(storage) -> None:
    with pytest.raises(StorageIntegrityError), storage.transaction():
        _symbiote(storage, "a")
        _symbiote(storage, "a")
    assert _count(storage) == 0
    _symbiote(storage, "a")
    assert _count(storage) == 1


def test_nested_transactions_join_the_outer_one(storage) -> None:
    with pytest.raises(RuntimeError), storage.transaction():
        _symbiote(storage, "outer")
        with storage.transaction():
            _symbiote(storage, "inner")
        raise RuntimeError("boom")
    assert _count(storage) == 0

    with storage.transaction():
        with storage.transaction():
            _symbiote(storage, "inner")
        _symbiote(storage, "outer")
    assert _count(storage) == 2


def test_uncommitted_writes_are_invisible_to_other_threads(storage) -> None:
    inside = threading.Event()
    release = threading.Event()
    seen: list[int] = []

    def writer() -> None:
        with storage.transaction():
            _symbiote(storage, "a")
            inside.set()
            release.wait(5)

    def reader() -> None:
        seen.append(_count(storage))

    w = threading.Thread(target=writer)
    w.start()
    assert inside.wait(5)
    r = threading.Thread(target=reader)
    r.start()
    r.join(0.3)  # SQLite makes the reader wait; PostgreSQL answers 0 at once
    release.set()
    w.join(5)
    r.join(5)
    assert seen in ([0], [1])  # never a half-done transaction: before or after
    if seen == [1]:
        assert not r.is_alive()
    assert _count(storage) == 1


def test_other_threads_are_not_part_of_my_rollback(storage) -> None:
    def other() -> None:
        _symbiote(storage, "other")

    t = threading.Thread(target=other)
    with pytest.raises(RuntimeError), storage.transaction():
        _symbiote(storage, "mine")
        t.start()
        t.join(0.3)  # SQLite: waits for this block; PostgreSQL: done already
        raise RuntimeError("boom")
    t.join(5)
    assert [r["id"] for r in storage.fetch_all("SELECT id FROM symbiotes")] == ["other"]


def test_concurrent_writers(storage) -> None:
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                with storage.transaction():
                    _symbiote(storage, f"w{n}-{i}")
                    storage.execute(
                        "UPDATE symbiotes SET role = ? WHERE id = ?", ("done", f"w{n}-{i}")
                    )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    row = storage.fetch_one("SELECT COUNT(*) AS c FROM symbiotes WHERE role = ?", ("done",))
    assert row["c"] == 160
