"""Tests for DiscoveredTool model and DiscoveredToolRepository."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.discovery.models import DiscoveredTool
from symbiote.discovery.repository import DiscoveredToolRepository


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "disc_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    return mgr.create(name="Clark", role="assistant").id


@pytest.fixture()
def repo(adapter: SQLiteAdapter) -> DiscoveredToolRepository:
    return DiscoveredToolRepository(adapter)


def _make_tool(symbiote_id: str, tool_id: str = "search") -> DiscoveredTool:
    return DiscoveredTool(
        id=f"dt-{tool_id}",
        symbiote_id=symbiote_id,
        tool_id=tool_id,
        name=f"Tool {tool_id}",
        description="A discovered tool",
        method="GET",
        url_template="http://localhost:8000/api/{q}",
        source_path="app/routes.py",
    )


class TestDiscoveredToolModel:
    def test_defaults(self, symbiote_id: str) -> None:
        t = _make_tool(symbiote_id)
        assert t.status == "pending"
        assert t.handler_type == "http"
        assert t.approved_at is None

    def test_all_statuses_valid(self, symbiote_id: str) -> None:
        for status in ("pending", "approved", "disabled"):
            t = DiscoveredTool(
                id="x", symbiote_id=symbiote_id, tool_id="t",
                name="T", status=status,
            )
            assert t.status == status


class TestDiscoveredToolRepository:
    def test_save_and_get(self, repo: DiscoveredToolRepository, symbiote_id: str) -> None:
        tool = _make_tool(symbiote_id)
        repo.save(tool)
        fetched = repo.get(symbiote_id, "search")
        assert fetched is not None
        assert fetched.tool_id == "search"
        assert fetched.status == "pending"

    def test_save_upserts_on_conflict(
        self, repo: DiscoveredToolRepository, symbiote_id: str
    ) -> None:
        tool = _make_tool(symbiote_id)
        repo.save(tool)
        updated = tool.model_copy(update={"name": "Updated Name"})
        repo.save(updated)
        fetched = repo.get(symbiote_id, "search")
        assert fetched.name == "Updated Name"

    def test_list_all(self, repo: DiscoveredToolRepository, symbiote_id: str) -> None:
        repo.save(_make_tool(symbiote_id, "search"))
        repo.save(_make_tool(symbiote_id, "publish"))
        tools = repo.list(symbiote_id)
        assert len(tools) == 2

    def test_list_filtered_by_status(
        self, repo: DiscoveredToolRepository, symbiote_id: str
    ) -> None:
        repo.save(_make_tool(symbiote_id, "search"))
        repo.save(_make_tool(symbiote_id, "publish"))
        repo.set_status(symbiote_id, "search", "approved")
        approved = repo.list(symbiote_id, status="approved")
        pending = repo.list(symbiote_id, status="pending")
        assert len(approved) == 1
        assert len(pending) == 1

    def test_set_status_approved(
        self, repo: DiscoveredToolRepository, symbiote_id: str
    ) -> None:
        repo.save(_make_tool(symbiote_id))
        result = repo.set_status(symbiote_id, "search", "approved")
        assert result is True
        fetched = repo.get(symbiote_id, "search")
        assert fetched.status == "approved"
        assert fetched.approved_at is not None

    def test_set_status_returns_false_for_unknown(
        self, repo: DiscoveredToolRepository, symbiote_id: str
    ) -> None:
        result = repo.set_status(symbiote_id, "nonexistent", "approved")
        assert result is False

    def test_delete(self, repo: DiscoveredToolRepository, symbiote_id: str) -> None:
        repo.save(_make_tool(symbiote_id))
        assert repo.delete(symbiote_id, "search") is True
        assert repo.get(symbiote_id, "search") is None

    def test_delete_nonexistent_returns_false(
        self, repo: DiscoveredToolRepository, symbiote_id: str
    ) -> None:
        assert repo.delete(symbiote_id, "ghost") is False

    def test_list_empty_for_new_symbiote(
        self, repo: DiscoveredToolRepository, symbiote_id: str
    ) -> None:
        assert repo.list(symbiote_id) == []

    def test_parameters_roundtrip(
        self, repo: DiscoveredToolRepository, symbiote_id: str
    ) -> None:
        params = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        tool = _make_tool(symbiote_id)
        tool = tool.model_copy(update={"parameters": params})
        repo.save(tool)
        fetched = repo.get(symbiote_id, "search")
        assert fetched.parameters == params
