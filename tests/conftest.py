"""Global test configuration."""

import os

# Enable dev mode for API auth middleware during tests
os.environ.setdefault("SYMBIOTE_DEV_MODE", "1")

# Never let a test fall back to the default database path (.symbiote/ under
# the current directory — in the repo root that is a developer's local
# instance). Tests that need a database set their own under tmp_path.
import tempfile  # noqa: E402

os.environ.setdefault(
    "SYMBIOTE_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="symbiote-tests-"), "default.db")
)

# Shared fixtures (see tests/support). Imported after the env var above so
# the dev-mode default is in place before any symbiote module is loaded.
import pytest  # noqa: E402

# With SYMBIOTE_TEST_POSTGRES_DSN the whole suite runs on PostgreSQL (see
# tests/support/postgres_mode.py); installed before the kernel is imported.
from tests.support import postgres_mode  # noqa: E402

if os.environ.get("SYMBIOTE_TEST_POSTGRES_DSN"):
    postgres_mode.install(os.environ["SYMBIOTE_TEST_POSTGRES_DSN"])

from tests.support import api_client  # noqa: E402,F401


def pytest_collection_modifyitems(config, items):
    if not postgres_mode.enabled():
        return
    skip = pytest.mark.skip(reason="SQLite-specific behaviour")
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _postgres_cleanup():
    """PostgreSQL mode: close the adapters the test opened, drop their schemas."""
    yield
    if postgres_mode.enabled():
        postgres_mode.cleanup()


@pytest.fixture(autouse=True)
def _isolate_api_singletons(monkeypatch, tmp_path_factory):
    """Restore symbiote.api.http module singletons after every test, so a
    kernel/adapter created by one test is never reused by the next, and give
    each test its own default database (state such as registered HTTP tools
    is persisted and must not leak between tests)."""
    import symbiote.api.http as http_mod
    import symbiote.api.middleware as mw_mod

    monkeypatch.setenv(
        "SYMBIOTE_DB_PATH", str(tmp_path_factory.mktemp("api-db") / "default.db")
    )

    names = ("_adapter", "_kernel", "_tool_gateway", "_key_manager", "_local_admin_key")
    saved = {name: getattr(http_mod, name) for name in names}
    saved_mw = mw_mod._key_manager
    yield
    for name, value in saved.items():
        setattr(http_mod, name, value)
    mw_mod._key_manager = saved_mw
