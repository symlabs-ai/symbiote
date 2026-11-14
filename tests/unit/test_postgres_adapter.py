"""PostgresAdapter: what is specific to it, and its parity with SQLite.

The behaviour common to every adapter is in ``test_storage_contract.py``.
Except for the placeholder rewriting, these tests need a PostgreSQL server:
they run with ``SYMBIOTE_TEST_POSTGRES_DSN`` (``scripts/test-postgres.sh``).
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from symbiote.core.models import MemoryEntry
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from tests.support import postgres_mode

pytest.importorskip("psycopg", exc_type=ImportError)
pytest.importorskip("psycopg_pool", exc_type=ImportError)

from symbiote.adapters.storage.postgres import PostgresAdapter, translate  # noqa: E402
from symbiote.adapters.storage.postgres_migrations import SCHEMA_VERSION  # noqa: E402

needs_postgres = pytest.mark.skipif(
    not postgres_mode.enabled(), reason="needs SYMBIOTE_TEST_POSTGRES_DSN"
)


def _adapter(**kwargs) -> PostgresAdapter:
    schema = postgres_mode.track_schema(f"t_{uuid.uuid4().hex[:20]}")
    return PostgresAdapter(postgres_mode.dsn(), schema=schema, **kwargs)


# ── placeholder rewriting (no server needed) ────────────────────────────


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM t WHERE a = ? AND b = ?", "SELECT * FROM t WHERE a = %s AND b = %s"),
        ("SELECT '?' AS q, a FROM t WHERE b = ?", "SELECT '?' AS q, a FROM t WHERE b = %s"),
        ("SELECT a FROM t WHERE a LIKE '50%' AND b = ?", "SELECT a FROM t WHERE a LIKE '50%%' AND b = %s"),
        ("SELECT a -- why?\nFROM t WHERE b = ?", "SELECT a -- why?\nFROM t WHERE b = %s"),
        ("SELECT 'it''s ?' AS q WHERE 1 = ?", "SELECT 'it''s ?' AS q WHERE 1 = %s"),
        ("SELECT a - -? FROM t", "SELECT a - -%s FROM t"),
    ],
)
def test_translate(sql: str, expected: str) -> None:
    assert translate(sql) == expected


def test_schema_name_is_validated() -> None:
    with pytest.raises(ValueError, match="invalid schema name"):
        PostgresAdapter("", schema='x"; DROP SCHEMA public; --')


# ── schema and migrations ───────────────────────────────────────────────


@needs_postgres
def test_init_schema_records_the_migrations() -> None:
    adapter = _adapter()
    assert adapter.schema_version() == 0
    adapter.init_schema()
    adapter.init_schema()
    assert adapter.schema_version() == SCHEMA_VERSION
    rows = adapter.fetch_all("SELECT version, name FROM schema_migrations ORDER BY version")
    assert rows[0] == {"version": 1, "name": "baseline"}
    assert len(rows) == SCHEMA_VERSION
    adapter.close()


@needs_postgres
def test_replicas_starting_together_migrate_once() -> None:
    first = _adapter()
    replicas = [first] + [
        PostgresAdapter(postgres_mode.dsn(), schema=first._schema) for _ in range(5)
    ]
    errors: list[BaseException] = []
    start = threading.Barrier(len(replicas))

    def boot(adapter: PostgresAdapter) -> None:
        try:
            start.wait(5)
            adapter.init_schema()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=boot, args=(a,)) for a in replicas]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert first.fetch_one("SELECT COUNT(*) AS c FROM schema_migrations")["c"] == SCHEMA_VERSION
    for adapter in replicas:
        adapter.close()


@needs_postgres
def test_two_schemas_do_not_see_each_other() -> None:
    a, b = _adapter(), _adapter()
    a.init_schema()
    b.init_schema()
    a.execute("INSERT INTO symbiotes (id, name) VALUES (?, ?)", ("only-a", "n"))
    assert b.fetch_all("SELECT id FROM symbiotes") == []
    a.close()
    b.close()


@needs_postgres
def test_a_write_on_one_replica_is_read_by_the_other() -> None:
    one = _adapter()
    one.init_schema()
    other = PostgresAdapter(postgres_mode.dsn(), schema=one._schema)
    other.init_schema()
    one.execute("INSERT INTO symbiotes (id, name) VALUES (?, ?)", ("s", "written by one"))
    assert other.fetch_one("SELECT name FROM symbiotes WHERE id = ?", ("s",)) == {
        "name": "written by one"
    }
    one.close()
    other.close()


@needs_postgres
def test_a_failed_statement_does_not_poison_the_pool() -> None:
    adapter = _adapter(min_size=1, max_size=1)
    adapter.init_schema()
    with pytest.raises(Exception, match="no_such_table"):
        adapter.fetch_all("SELECT * FROM no_such_table")
    with pytest.raises(RuntimeError), adapter.transaction():
        adapter.execute("INSERT INTO symbiotes (id, name) VALUES (?, ?)", ("s", "n"))
        raise RuntimeError("boom")
    assert adapter.fetch_one("SELECT COUNT(*) AS c FROM symbiotes") == {"c": 0}
    adapter.close()


@needs_postgres
def test_schema_matches_sqlite(tmp_path: Path) -> None:
    """Same tables, same columns (name, type family, NOT NULL, default) and
    same indexes in both adapters: a migration added to one and forgotten in
    the other fails here."""
    sqlite = postgres_mode.real_sqlite_adapter(tmp_path / "parity.db")
    sqlite.init_schema()
    postgres = _adapter()
    postgres.init_schema()

    def family(sql_type: str) -> str:
        return {"double precision": "real", "bigint": "integer"}.get(
            sql_type.lower(), sql_type.lower()
        )

    def default(value: str | None) -> str | None:
        if value is None or value.upper() == "NULL":
            return None
        value = value.split("::")[0].strip("'")
        try:
            return str(float(value))
        except ValueError:
            return value

    sqlite_columns = {}
    for table in sqlite.table_names():
        for _cid, name, sql_type, notnull, dflt, pk in sqlite._conn.execute(
            f"PRAGMA table_info({table})"
        ):
            # SQLite does not flag a TEXT PRIMARY KEY as NOT NULL; PostgreSQL does.
            sqlite_columns[(table, name)] = (family(sql_type), bool(notnull or pk), default(dflt))
    postgres_columns = {
        (r["table_name"], r["column_name"]): (
            family(r["data_type"]),
            r["is_nullable"] == "NO",
            default(r["column_default"]),
        )
        for r in postgres.fetch_all(
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name <> 'schema_migrations' AND column_name <> 'rowid'"
        )
    }
    assert postgres_columns == sqlite_columns

    sqlite_indexes = {
        row[0]
        for row in sqlite._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    postgres_indexes = {
        r["indexname"]
        for r in postgres.fetch_all(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname LIKE 'idx\\_%'"
        )
    }
    assert postgres_indexes == sqlite_indexes

    collations = postgres.fetch_all(
        "SELECT DISTINCT collation_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND data_type = 'text' "
        "AND table_name <> 'schema_migrations'"
    )
    assert collations == [{"collation_name": "C"}]
    sqlite.close()
    postgres.close()


# ── memory and knowledge recall: the same answers as SQLite ─────────────

_MEMORIES = [
    ("m01", "The Gateway restarts at 3am", ["ops", "Gateway"], 0.9),
    ("m02", "GATEWAY CONFIG lives in /etc", ["ops"], 0.9),
    ("m03", "configuração do roteador", ["rede"], 0.8),
    ("m04", "CONFIGURAÇÃO DO ROTEADOR", ["REDE"], 0.8),
    ("m05", "usage is 100% today", ["metric_a"], 0.7),
    ("m06", "usage is 100 today", ["metricXa"], 0.7),
    ("m07", "set max_tokens to 10", ["llm"], 0.6),
    ("m08", "set maxAtokens to 10", ["llm"], 0.6),
    ("m09", "path is C:\\temp\\x", ["win"], 0.5),
    ("m10", "Ünïcödé and émojis 🙂 end", ["i18n"], 0.5),
    ("m11", "the user prefers short answers", ["style"], 0.5),
    ("m12", "The user prefers SHORT answers in Portuguese", ["style", "pt"], 0.5),
]
_QUERIES = [
    "gateway", "GATEWAY", "Gateway", "configuração", "CONFIGURAÇÃO", "Configuração",
    "roteador", "100%", "100", "%", "_", "max_tokens", "maxAtokens", "C:\\temp", "\\",
    "ünïcödé", "Ünïcödé", "🙂", "short answers", "SHORT", "", " ", "'", "prefers",
]
_TAGS = [["ops"], ["OPS"], ["gateway"], ["rede"], ["REDE"], ["metric_a"], ["style", "win"], ["nope"]]


def _fill(storage) -> tuple[MemoryStore, KnowledgeService]:
    storage.init_schema()
    storage.execute("INSERT INTO symbiotes (id, name) VALUES (?, ?)", ("sym", "n"))
    storage.execute("INSERT INTO symbiotes (id, name) VALUES (?, ?)", ("other", "n"))
    memory = MemoryStore(storage)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i, (mid, content, tags, importance) in enumerate(_MEMORIES):
        for symbiote_id in ("sym", "other"):
            memory.store(
                MemoryEntry(
                    id=f"{symbiote_id}-{mid}", symbiote_id=symbiote_id, type="factual",
                    scope="global", content=content, tags=tags, importance=importance,
                    source="user", created_at=base + timedelta(minutes=i),
                    last_used_at=base + timedelta(minutes=i),
                )
            )
    knowledge = KnowledgeService(storage)
    for i, (mid, content, _tags, _importance) in enumerate(_MEMORIES):
        storage.execute(
            "INSERT INTO knowledge_entries (id, symbiote_id, name, content, type, tags_json, created_at) "
            "VALUES (?, 'sym', ?, ?, 'note', '[]', ?)",
            (f"k-{mid}", f"Doc {mid.upper()}", content, (base + timedelta(minutes=i)).isoformat()),
        )
    return memory, knowledge


@needs_postgres
def test_recall_gives_the_same_results_as_sqlite(tmp_path: Path) -> None:
    """The LIKE-based searches (memory by content and tags, knowledge by
    theme) and the term-based recall return the same entries in the same
    order on SQLite and on PostgreSQL."""
    sqlite = postgres_mode.real_sqlite_adapter(tmp_path / "recall.db")
    postgres = _adapter()
    lite_memory, lite_knowledge = _fill(sqlite)
    pg_memory, pg_knowledge = _fill(postgres)

    def ids(entries) -> list[str]:
        return [e.id for e in entries]

    compared = matched = 0
    for query in _QUERIES:
        for tags in (None, *_TAGS):
            for limit in (3, 50):
                expected = ids(lite_memory.search(query, tags=tags, limit=limit, symbiote_id="sym"))
                got = ids(pg_memory.search(query, tags=tags, limit=limit, symbiote_id="sym"))
                assert got == expected, (query, tags, limit)
                assert all(i.startswith("sym-") for i in got)  # never another symbiote's
                compared += 1
                matched += bool(got)
        for limit in (3, 50):
            assert ids(pg_knowledge.query("sym", query, limit=limit)) == ids(
                lite_knowledge.query("sym", query, limit=limit)
            ), (query, limit)
        assert ids(pg_memory.get_relevant(query, limit=5, symbiote_id="sym")) == ids(
            lite_memory.get_relevant(query, limit=5, symbiote_id="sym")
        ), query
    # The comparison is not vacuous: many searches do find something.
    assert compared == len(_QUERIES) * (len(_TAGS) + 1) * 2
    assert matched > 60
    # Spot checks of the semantics themselves.
    assert ids(pg_memory.search("GATEWAY", limit=50, symbiote_id="sym")) == ["sym-m02", "sym-m01"]
    assert ids(pg_memory.search("CONFIGURAÇÃO", limit=50, symbiote_id="sym")) == ["sym-m04"]
    assert ids(pg_memory.search("100%", limit=50, symbiote_id="sym")) == ["sym-m05"]
    assert ids(pg_memory.search("max_tokens", limit=50, symbiote_id="sym")) == ["sym-m07"]
    sqlite.close()
    postgres.close()
