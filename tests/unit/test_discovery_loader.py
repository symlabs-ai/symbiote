"""Tests for DiscoveredToolLoader — bridge approved tools → ToolGateway."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.discovery.loader import DiscoveredToolLoader
from symbiote.discovery.models import DiscoveredTool
from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "loader_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    return IdentityManager(storage=adapter).create(name="Clark", role="assistant").id


@pytest.fixture()
def repo(adapter: SQLiteAdapter) -> DiscoveredToolRepository:
    return DiscoveredToolRepository(adapter)


@pytest.fixture()
def gateway(adapter: SQLiteAdapter) -> ToolGateway:
    env = EnvironmentManager(adapter)
    policy = PolicyGate(env, adapter)
    return ToolGateway(policy)


@pytest.fixture()
def loader(repo: DiscoveredToolRepository, gateway: ToolGateway) -> DiscoveredToolLoader:
    return DiscoveredToolLoader(repo, gateway)


def _make_tool(symbiote_id: str, tool_id: str, status: str, method: str = "GET",
               url: str = "{base_url}/api/items", handler_type: str = "http") -> DiscoveredTool:
    from datetime import UTC, datetime
    from uuid import uuid4
    return DiscoveredTool(
        id=str(uuid4()),
        symbiote_id=symbiote_id,
        tool_id=tool_id,
        name=tool_id.replace("_", " ").title(),
        description=f"Tool: {tool_id}",
        handler_type=handler_type,
        method=method,
        url_template=url,
        status=status,
        discovered_at=datetime.now(tz=UTC).isoformat(),
    )


class TestLoad:
    def test_loads_approved_http_tools(
        self, loader: DiscoveredToolLoader, repo: DiscoveredToolRepository,
        gateway: ToolGateway, symbiote_id: str,
    ) -> None:
        repo.save(_make_tool(symbiote_id, "get_items", "approved", "GET", "{base_url}/items"))
        repo.save(_make_tool(symbiote_id, "post_publish", "approved", "POST", "{base_url}/publish"))

        result = loader.load(symbiote_id, base_url="http://localhost:8000")

        assert set(result) == {"get_items", "post_publish"}
        assert gateway.get_descriptor("get_items") is not None
        assert gateway.get_descriptor("post_publish") is not None

    def test_skips_pending_tools(
        self, loader: DiscoveredToolLoader, repo: DiscoveredToolRepository,
        gateway: ToolGateway, symbiote_id: str,
    ) -> None:
        repo.save(_make_tool(symbiote_id, "get_items", "pending"))

        result = loader.load(symbiote_id, base_url="http://localhost:8000")

        assert result == []
        assert gateway.get_descriptor("get_items") is None

    def test_skips_disabled_tools(
        self, loader: DiscoveredToolLoader, repo: DiscoveredToolRepository,
        gateway: ToolGateway, symbiote_id: str,
    ) -> None:
        repo.save(_make_tool(symbiote_id, "get_items", "disabled"))

        result = loader.load(symbiote_id, base_url="http://localhost:8000")

        assert result == []

    def test_skips_custom_handler_type(
        self, loader: DiscoveredToolLoader, repo: DiscoveredToolRepository,
        gateway: ToolGateway, symbiote_id: str,
    ) -> None:
        repo.save(_make_tool(symbiote_id, "symbiote_init", "approved",
                             handler_type="custom", method="", url=""))

        result = loader.load(symbiote_id, base_url="http://localhost:8000")

        assert result == []
        assert gateway.get_descriptor("symbiote_init") is None

    def test_resolves_base_url_placeholder(
        self, loader: DiscoveredToolLoader, repo: DiscoveredToolRepository,
        gateway: ToolGateway, symbiote_id: str,
    ) -> None:
        repo.save(_make_tool(symbiote_id, "get_items", "approved", "GET",
                             "{base_url}/items"))

        loader.load(symbiote_id, base_url="http://localhost:8000")

        config = gateway.get_http_config("get_items")
        assert config is not None
        assert config.url_template == "http://localhost:8000/items"

    def test_base_url_trailing_slash_stripped(
        self, loader: DiscoveredToolLoader, repo: DiscoveredToolRepository,
        gateway: ToolGateway, symbiote_id: str,
    ) -> None:
        repo.save(_make_tool(symbiote_id, "get_items", "approved", "GET",
                             "{base_url}/items"))

        loader.load(symbiote_id, base_url="http://localhost:8000/")

        config = gateway.get_http_config("get_items")
        assert config.url_template == "http://localhost:8000/items"

    def test_allow_internal_set_on_registered_tools(
        self, loader: DiscoveredToolLoader, repo: DiscoveredToolRepository,
        gateway: ToolGateway, symbiote_id: str,
    ) -> None:
        repo.save(_make_tool(symbiote_id, "get_items", "approved"))

        loader.load(symbiote_id, base_url="http://127.0.0.1:8000")

        config = gateway.get_http_config("get_items")
        assert config.allow_internal is True

    def test_empty_when_no_approved_tools(
        self, loader: DiscoveredToolLoader, symbiote_id: str,
    ) -> None:
        result = loader.load(symbiote_id, base_url="http://localhost:8000")
        assert result == []

    def test_mixed_statuses_only_loads_approved(
        self, loader: DiscoveredToolLoader, repo: DiscoveredToolRepository,
        gateway: ToolGateway, symbiote_id: str,
    ) -> None:
        repo.save(_make_tool(symbiote_id, "approved_tool", "approved"))
        repo.save(_make_tool(symbiote_id, "pending_tool", "pending"))
        repo.save(_make_tool(symbiote_id, "disabled_tool", "disabled"))

        result = loader.load(symbiote_id)

        assert result == ["approved_tool"]
        assert gateway.get_descriptor("approved_tool") is not None
        assert gateway.get_descriptor("pending_tool") is None
        assert gateway.get_descriptor("disabled_tool") is None


class TestKernelIntegration:
    def test_kernel_load_discovered_tools(self, tmp_path: Path) -> None:
        """Integration: kernel.load_discovered_tools registers and authorizes tools."""
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel
        from symbiote.discovery.repository import DiscoveredToolRepository

        kernel = SymbioteKernel(config=KernelConfig(db_path=tmp_path / "k.db"))
        clark = kernel.create_symbiote(name="Clark", role="assistant")

        # Pre-seed approved tool directly in the DB
        repo = DiscoveredToolRepository(kernel._storage)
        repo.save(_make_tool(clark.id, "post_publish", "approved", "POST",
                             "{base_url}/items/publish"))

        tool_ids = kernel.load_discovered_tools(clark.id, base_url="http://127.0.0.1:8000")

        assert tool_ids == ["post_publish"]
        assert kernel.tool_gateway.get_descriptor("post_publish") is not None

        kernel.shutdown()

    def test_kernel_load_returns_empty_when_none_approved(self, tmp_path: Path) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        kernel = SymbioteKernel(config=KernelConfig(db_path=tmp_path / "k2.db"))
        clark = kernel.create_symbiote(name="Clark", role="assistant")

        result = kernel.load_discovered_tools(clark.id, base_url="http://127.0.0.1:8000")

        assert result == []
        kernel.shutdown()
