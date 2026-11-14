"""PostgreSQL storage adapter (psycopg 3), for deployments with several
replicas of the host sharing one database.

Install with the ``postgres`` extra (``pip install 'symbiote[postgres]'``).

    from symbiote.adapters.storage.postgres import PostgresAdapter

    storage = PostgresAdapter("postgresql://user:pass@host/db", schema="symbiote")
    kernel = SymbioteKernel(config, storage=storage)

The kernel sends the same portable SQL it sends to SQLite (see
``StoragePort``); this adapter only rewrites the ``?`` placeholders. The
schema is created and upgraded by ``init_schema`` through the versioned
migrations in ``postgres_migrations``; replicas starting at the same time
are serialized by an advisory lock.
"""

from __future__ import annotations

import contextlib
import functools
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from symbiote.adapters.storage.postgres_migrations import MIGRATIONS, statements
from symbiote.core.exceptions import StorageIntegrityError

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "PostgresAdapter needs psycopg 3 and psycopg_pool: "
        "pip install 'symbiote[postgres]'"
    ) from exc

if TYPE_CHECKING:
    from psycopg import Connection

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
# Arbitrary constant: every replica takes this advisory lock to migrate.
_MIGRATION_LOCK = 0x53594D42  # "SYMB"
_ROWID = re.compile(r"\browid\b", re.IGNORECASE)
_FROM = re.compile(r"\bFROM\b", re.IGNORECASE)


class PostgresIntegrityError(StorageIntegrityError):
    """``StorageIntegrityError`` raised by this adapter."""


@dataclass(frozen=True)
class _WriteResult:
    rowcount: int


@functools.lru_cache(maxsize=2048)
def translate(sql: str) -> str:
    """Rewrite the kernel's portable SQL for psycopg: ``?`` placeholders
    outside string literals and ``--`` comments become ``%s`` and every
    literal ``%`` is doubled."""
    out: list[str] = []
    in_literal = in_comment = False
    previous = ""
    for char in sql:
        if in_comment:
            in_comment = char != "\n"
        elif char == "'":
            in_literal = not in_literal
        elif char == "-" and previous == "-" and not in_literal:
            in_comment = True
        if char == "%":
            out.append("%%")
        elif char == "?" and not in_literal and not in_comment:
            out.append("%s")
        else:
            out.append(char)
        previous = char
    return "".join(out)


@functools.lru_cache(maxsize=2048)
def _selects_rowid(sql: str) -> bool:
    """Whether the statement names ``rowid`` in its select list."""
    match = _FROM.search(sql)
    head = sql[: match.start()] if match else sql
    return _ROWID.search(head) is not None


def _adapt(value: Any) -> Any:
    """Parameters as SQLite would store them: booleans as 0/1, datetimes as
    ISO text."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, datetime):
        return value.isoformat(" ")
    return value


def _row(row: dict[str, Any], keep_rowid: bool) -> dict[str, Any]:
    if not keep_rowid:
        row.pop("rowid", None)
    for key, value in row.items():
        if isinstance(value, Decimal):  # AVG / SUM of integers
            row[key] = float(value)
    return row


class PostgresAdapter:
    """``StoragePort`` on PostgreSQL, with a connection pool.

    Args:
        conninfo: libpq connection string or URL.
        schema: schema that holds the kernel's tables (created if missing).
            ``None`` uses the connection's default ``search_path``.
        min_size, max_size: bounds of the connection pool. Each thread
            running a statement uses one connection for its duration; a
            ``transaction()`` block holds one until it ends.
        timeout: seconds to wait for a free connection.
        pool: a ``psycopg_pool.ConnectionPool`` owned by the host, used
            instead of creating one. Its connections must be in autocommit
            mode; ``close()`` does not close it.
    """

    def __init__(
        self,
        conninfo: str = "",
        *,
        schema: str | None = None,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 30.0,
        pool: ConnectionPool | None = None,
    ) -> None:
        if schema is not None and not _IDENTIFIER.fullmatch(schema):
            raise ValueError(f"invalid schema name: {schema!r}")
        self._schema = schema
        self._local = threading.local()
        self._owns_pool = pool is None
        if pool is None:
            kwargs: dict[str, Any] = {"autocommit": True}
            if schema is not None:
                kwargs["options"] = f"-c search_path={schema}"
            pool = ConnectionPool(
                conninfo,
                min_size=min_size,
                max_size=max_size,
                timeout=timeout,
                kwargs=kwargs,
                open=True,
            )
        self._pool = pool

    # ── schema ─────────────────────────────────────────────────────────

    def init_schema(self) -> None:
        """Create the schema and apply the migrations not applied yet.

        Runs in one transaction under an advisory lock, so replicas that
        start together migrate one at a time and a failed migration leaves
        nothing half applied.
        """
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
            if self._schema is not None:
                conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
                conn.execute(f'SET LOCAL search_path TO "{self._schema}"')
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for version, name, script in MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements(script):
                    conn.execute(statement)  # type: ignore[arg-type]
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, %s)",
                    (version, name, datetime.now(tz=UTC).isoformat()),
                )

    def schema_version(self) -> int:
        """Version of the last migration applied (0 before ``init_schema``)."""
        try:
            row = self.fetch_one("SELECT MAX(version) AS version FROM schema_migrations")
        except psycopg.errors.UndefinedTable:
            return 0
        return int(row["version"] or 0) if row else 0

    def table_names(self) -> list[str]:
        """Names of the kernel's tables."""
        rows = self.fetch_all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name <> 'schema_migrations' "
            "ORDER BY table_name"
        )
        return [row["table_name"] for row in rows]

    # ── statements ─────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Connection[Any]]:
        """The connection of this thread's transaction, or one from the pool."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            yield conn
            return
        with self._pool.connection() as pooled:
            yield pooled

    def _run(self, conn: Connection[Any], sql: str, params: tuple | None) -> psycopg.Cursor[Any]:
        cursor = psycopg.ClientCursor(conn, row_factory=dict_row)
        try:
            if params:
                return cursor.execute(
                    translate(sql),  # type: ignore[arg-type]
                    tuple(_adapt(p) for p in params),
                )
            return cursor.execute(sql)  # type: ignore[arg-type]
        except psycopg.errors.IntegrityError as exc:
            raise PostgresIntegrityError(str(exc)) from exc

    def execute(self, sql: str, params: tuple | None = None) -> _WriteResult:
        """Run an INSERT / UPDATE / DELETE. Outside ``transaction()`` it is
        committed before the call returns."""
        with self._connection() as conn:
            return _WriteResult(rowcount=self._run(conn, sql, params).rowcount)

    def fetch_one(self, sql: str, params: tuple | None = None) -> dict | None:
        """Return a single row as a dict, or ``None``."""
        with self._connection() as conn:
            row = self._run(conn, sql, params).fetchone()
        return None if row is None else _row(row, _selects_rowid(sql))

    def fetch_all(self, sql: str, params: tuple | None = None) -> list[dict]:
        """Return all matching rows as a list of dicts."""
        with self._connection() as conn:
            rows = self._run(conn, sql, params).fetchall()
        keep = _selects_rowid(sql)
        return [_row(row, keep) for row in rows]

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the statements of the block as one unit: all or nothing.

        The thread keeps one connection for the whole block. Nested blocks
        join the outer one: an exception that leaves the outermost block
        rolls everything back. After a statement fails, PostgreSQL refuses
        the rest of the transaction, so let the exception leave the block.
        """
        if getattr(self._local, "conn", None) is not None:
            yield
            return
        with self._pool.connection() as conn, conn.transaction():
            self._local.conn = conn
            try:
                yield
            finally:
                self._local.conn = None

    def close(self) -> None:
        """Close the pool (unless the host owns it)."""
        if self._owns_pool:
            self._pool.close()


__all__ = ["PostgresAdapter", "PostgresIntegrityError", "translate"]
