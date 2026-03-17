"""Acceptance Gate — validate PRD ACs against real interfaces.

interface_type: mixed → CLI + HTTP API must both be validated.
Tests exercise ACs from PRD User Stories against the actual running interfaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.http import app as fastapi_app
from symbiote.api.http import get_adapter
from symbiote.cli.main import app as cli_app
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

cli_runner = CliRunner()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "acceptance.db"


@pytest.fixture()
def kernel(db_path: Path) -> SymbioteKernel:
    config = KernelConfig(db_path=db_path)
    llm = MockLLMAdapter(default_response="Acceptance test response")
    k = SymbioteKernel(config=config, llm=llm)
    yield k
    k.shutdown()


@pytest.fixture()
def http_client(db_path: Path) -> TestClient:
    """TestClient with overridden adapter pointing to tmp DB."""
    adapter = SQLiteAdapter(db_path=db_path, check_same_thread=False)
    adapter.init_schema()
    fastapi_app.dependency_overrides[get_adapter] = lambda: adapter
    client = TestClient(fastapi_app)
    yield client
    adapter.close()
    fastapi_app.dependency_overrides.clear()


def cli(*args: str, db_path: Path) -> object:
    cmd = ["--db-path", str(db_path)] + list(args)
    return cli_runner.invoke(cli_app, cmd)


def extract_id(output: str) -> str:
    for line in output.strip().splitlines():
        for token in line.split():
            if token.count("-") >= 4 and len(token) >= 32:
                return token
    raise ValueError(f"No ID in: {output}")


# ══════════════════════════════════════════════════════════════════════════════
# HTTP API — Acceptance Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestHTTPHealth:
    """B-1: Health check endpoint for Docker container."""

    def test_health_returns_ok(self, http_client: TestClient) -> None:
        resp = http_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestHTTPCreateSymbiote:
    """US-01 AC-01: create symbiote via API."""

    def test_create_returns_201(self, http_client: TestClient) -> None:
        resp = http_client.post("/symbiotes", json={"name": "Atlas", "role": "assistant"})
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["name"] == "Atlas"
        assert data["role"] == "assistant"

    def test_get_created_symbiote(self, http_client: TestClient) -> None:
        create = http_client.post("/symbiotes", json={"name": "Bot", "role": "helper"})
        sym_id = create.json()["id"]
        resp = http_client.get(f"/symbiotes/{sym_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Bot"

    def test_get_nonexistent_returns_404(self, http_client: TestClient) -> None:
        resp = http_client.get("/symbiotes/nonexistent")
        assert resp.status_code == 404


class TestHTTPSessions:
    """US-02 ACs: session lifecycle via API."""

    def test_create_session(self, http_client: TestClient) -> None:
        sym = http_client.post("/symbiotes", json={"name": "Bot", "role": "helper"}).json()
        resp = http_client.post("/sessions", json={"symbiote_id": sym["id"], "goal": "test"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "active"
        assert data["goal"] == "test"

    def test_add_message(self, http_client: TestClient) -> None:
        sym = http_client.post("/symbiotes", json={"name": "Bot", "role": "helper"}).json()
        sess = http_client.post("/sessions", json={"symbiote_id": sym["id"]}).json()
        resp = http_client.post(
            f"/sessions/{sess['id']}/messages",
            json={"role": "user", "content": "hello"},
        )
        assert resp.status_code == 201
        assert resp.json()["content"] == "hello"

    def test_close_session(self, http_client: TestClient) -> None:
        sym = http_client.post("/symbiotes", json={"name": "Bot", "role": "helper"}).json()
        sess = http_client.post("/sessions", json={"symbiote_id": sym["id"]}).json()
        http_client.post(f"/sessions/{sess['id']}/messages", json={"role": "user", "content": "hi"})
        resp = http_client.post(f"/sessions/{sess['id']}/close")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "closed"
        assert data["summary"]

    def test_get_session(self, http_client: TestClient) -> None:
        sym = http_client.post("/symbiotes", json={"name": "Bot", "role": "helper"}).json()
        sess = http_client.post("/sessions", json={"symbiote_id": sym["id"], "goal": "find"}).json()
        resp = http_client.get(f"/sessions/{sess['id']}")
        assert resp.status_code == 200
        assert resp.json()["goal"] == "find"

    def test_get_nonexistent_session_404(self, http_client: TestClient) -> None:
        resp = http_client.get("/sessions/nonexistent")
        assert resp.status_code == 404

    def test_close_nonexistent_session_404(self, http_client: TestClient) -> None:
        resp = http_client.post("/sessions/nonexistent/close")
        assert resp.status_code == 404

    def test_message_nonexistent_session_404(self, http_client: TestClient) -> None:
        resp = http_client.post("/sessions/nonexistent/messages", json={"role": "user", "content": "hi"})
        assert resp.status_code == 404


class TestHTTPMemorySearch:
    """US-06/US-07: memory search via API."""

    def test_search_empty(self, http_client: TestClient) -> None:
        resp = http_client.get("/memory/search", params={"query": "nothing"})
        assert resp.status_code == 200
        assert resp.json() == []


# ══════════════════════════════════════════════════════════════════════════════
# CLI — Acceptance Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestCLICreateAndList:
    """US-01/US-14 AC-02: CLI interface for symbiote management."""

    def test_create_via_cli(self, db_path: Path) -> None:
        result = cli("create", "--name", "CLIBot", "--role", "tester", db_path=db_path)
        assert result.exit_code == 0
        assert "-" in result.output

    def test_list_via_cli(self, db_path: Path) -> None:
        cli("create", "--name", "CLIBot", "--role", "tester", db_path=db_path)
        result = cli("list", db_path=db_path)
        assert result.exit_code == 0
        assert "CLIBot" in result.output


class TestCLISessions:
    """US-02 AC: session lifecycle via CLI."""

    def test_session_lifecycle_via_cli(self, db_path: Path) -> None:
        # Create
        cr = cli("create", "--name", "Bot", "--role", "helper", db_path=db_path)
        sym_id = extract_id(cr.output)

        # Start session
        sr = cli("session", "start", sym_id, "--goal", "CLI test", db_path=db_path)
        assert sr.exit_code == 0
        sess_id = extract_id(sr.output)

        # Close session
        result = cli("session", "close", sess_id, db_path=db_path)
        assert result.exit_code == 0


class TestCLIValueTracks:
    """US-11: all 6 capabilities accessible via CLI."""

    def test_chat_via_cli(self, db_path: Path) -> None:
        cr = cli("create", "--name", "Bot", "--role", "helper", db_path=db_path)
        sym_id = extract_id(cr.output)
        sr = cli("session", "start", sym_id, db_path=db_path)
        sess_id = extract_id(sr.output)

        result = cli("--llm", "mock", "chat", sess_id, "hello", db_path=db_path)
        assert result.exit_code == 0

    def test_learn_via_cli(self, db_path: Path) -> None:
        cr = cli("create", "--name", "Bot", "--role", "helper", db_path=db_path)
        sym_id = extract_id(cr.output)
        sr = cli("session", "start", sym_id, db_path=db_path)
        sess_id = extract_id(sr.output)

        result = cli("learn", sess_id, "Test fact", db_path=db_path)
        assert result.exit_code == 0

    def test_teach_via_cli(self, db_path: Path) -> None:
        cr = cli("create", "--name", "Bot", "--role", "helper", db_path=db_path)
        sym_id = extract_id(cr.output)
        sr = cli("session", "start", sym_id, db_path=db_path)
        sess_id = extract_id(sr.output)

        result = cli("teach", sess_id, "something", db_path=db_path)
        assert result.exit_code == 0

    def test_show_via_cli(self, db_path: Path) -> None:
        cr = cli("create", "--name", "Bot", "--role", "helper", db_path=db_path)
        sym_id = extract_id(cr.output)
        sr = cli("session", "start", sym_id, db_path=db_path)
        sess_id = extract_id(sr.output)

        result = cli("show", sess_id, "data", db_path=db_path)
        assert result.exit_code == 0

    def test_reflect_via_cli(self, db_path: Path) -> None:
        cr = cli("create", "--name", "Bot", "--role", "helper", db_path=db_path)
        sym_id = extract_id(cr.output)
        sr = cli("session", "start", sym_id, db_path=db_path)
        sess_id = extract_id(sr.output)

        result = cli("reflect", sess_id, db_path=db_path)
        assert result.exit_code == 0


class TestCLIExport:
    """US-13: export via CLI."""

    def test_export_session_via_cli(self, db_path: Path) -> None:
        cr = cli("create", "--name", "Bot", "--role", "helper", db_path=db_path)
        sym_id = extract_id(cr.output)
        sr = cli("session", "start", sym_id, db_path=db_path)
        sess_id = extract_id(sr.output)

        result = cli("export", "session", sess_id, db_path=db_path)
        assert result.exit_code == 0
