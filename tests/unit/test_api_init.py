"""Regression: the API initialises in one place — no 503 on the first
request, one storage connection, no logging side effects on import (report
item A-2)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_import_does_not_touch_root_logger() -> None:
    code = (
        "import logging; root = logging.getLogger(); "
        "before = (root.level, list(root.handlers)); "
        "import symbiote.api.http; "
        "print((root.level, list(root.handlers)) == before)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True"


@pytest.fixture()
def fresh_api(tmp_path: Path, monkeypatch):
    import symbiote.api.http as http_mod
    import symbiote.api.middleware as mw_mod

    monkeypatch.delenv("SYMBIOTE_DEV_MODE", raising=False)
    monkeypatch.setenv("SYMBIOTE_DB_PATH", str(tmp_path / "fresh.db"))
    for name in ("_adapter", "_kernel", "_tool_gateway", "_key_manager", "_local_admin_key"):
        monkeypatch.setattr(http_mod, name, None)
    monkeypatch.setattr(mw_mod, "_key_manager", None)
    yield http_mod
    if http_mod._kernel is not None:
        http_mod._kernel.shutdown()


def test_first_request_is_401_not_503(fresh_api) -> None:
    client = TestClient(fresh_api.app)  # no lifespan: lazy initialisation path
    resp = client.post("/symbiotes", json={"name": "a", "role": "r"})
    assert resp.status_code == 401


def test_adapter_is_the_kernel_storage(fresh_api) -> None:
    assert fresh_api.get_adapter() is fresh_api.get_kernel()._storage


def test_lifespan_initialises_and_shuts_down(fresh_api) -> None:
    with TestClient(fresh_api.app) as client:
        assert fresh_api._kernel is not None
        assert client.get("/health").status_code == 200
    assert fresh_api._kernel is None
