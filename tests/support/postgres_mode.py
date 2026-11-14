"""Run the whole suite against PostgreSQL.

With ``SYMBIOTE_TEST_POSTGRES_DSN`` set, ``tests/conftest.py`` calls
``install``: every ``SQLiteAdapter(db_path)`` built by a test or by the
kernel becomes a ``PostgresAdapter`` on a schema of its own (derived from
``db_path``, so reopening the same path reaches the same data, as a file
would). Tests of SQLite-only behaviour carry the ``sqlite_only`` marker and
are skipped.

Use ``scripts/test-postgres.sh`` to start a throwaway ``postgres:15-alpine``
container and run the suite on it.
"""

from __future__ import annotations

import contextlib
import hashlib
import sys
import weakref
from pathlib import Path
from typing import Any

_dsn: str | None = None
_live: weakref.WeakSet[Any] = weakref.WeakSet()
_schemas: set[str] = set()
_admin: Any = None
# The real SQLiteAdapter, for tests that compare the two databases.
real_sqlite_adapter: Any = None


def enabled() -> bool:
    return _dsn is not None


def dsn() -> str:
    assert _dsn is not None
    return _dsn


def track_schema(schema: str) -> str:
    """Have ``cleanup`` drop a schema a test created by hand."""
    _schemas.add(schema)
    return schema


def schema_for(db_path: Any) -> str:
    digest = hashlib.sha1(str(Path(db_path).resolve()).encode()).hexdigest()  # noqa: S324
    return f"t_{digest[:24]}"


def install(dsn: str) -> None:
    """Replace ``SQLiteAdapter`` by a PostgreSQL-backed stand-in."""
    global _dsn, real_sqlite_adapter
    import psycopg
    from psycopg_pool import ConnectionPool

    import symbiote.adapters.storage.sqlite as sqlite_mod
    from symbiote.adapters.storage.postgres import PostgresAdapter

    real = real_sqlite_adapter = sqlite_mod.SQLiteAdapter
    _dsn = dsn

    class PostgresForTests(PostgresAdapter):
        """``SQLiteAdapter``'s constructor signature over PostgreSQL."""

        def __init__(self, db_path: Any, *, check_same_thread: bool = False) -> None:
            self._db_path = Path(db_path)
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            schema = schema_for(db_path)
            pool = ConnectionPool(
                dsn,
                min_size=1,
                max_size=8,
                num_workers=1,
                kwargs={"autocommit": True, "options": f"-c search_path={schema}"},
                open=True,
            )
            super().__init__(schema=schema, pool=pool)
            self._test_pool = pool
            _schemas.add(schema)
            _live.add(self)

        def close(self) -> None:
            self._test_pool.close()

    sqlite_mod.SQLiteAdapter = PostgresForTests  # type: ignore[misc,assignment]
    # Modules imported before this point hold the real class by name.
    for name, module in list(sys.modules.items()):
        if name.startswith(("symbiote", "tests", "demo")) and (
            getattr(module, "SQLiteAdapter", None) is real
        ):
            module.SQLiteAdapter = PostgresForTests  # type: ignore[attr-defined]

    global _admin
    _admin = psycopg.connect(dsn, autocommit=True)
    _admin.execute("SET lock_timeout = '2s'")


def cleanup() -> None:
    """Close the adapters a test opened and drop their schemas."""
    for adapter in list(_live):
        with contextlib.suppress(Exception):
            adapter.close()
    _live.clear()
    while _schemas:
        schema = _schemas.pop()
        # A background thread may still hold a lock: leave the schema behind
        # (the container is thrown away) rather than block the suite.
        with contextlib.suppress(Exception):
            _admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
