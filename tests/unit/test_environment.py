"""Tests for EnvironmentManager — T-12."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.core.models import EnvironmentConfig
from symbiote.environment.manager import EnvironmentManager


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "env_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="TestBot", role="assistant")
    return sym.id


@pytest.fixture()
def manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


# ── Configure new ──────────────────────────────────────────────────────────


class TestConfigureNew:
    def test_configure_new_persisted_and_retrievable(
        self, manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        cfg = manager.configure(
            symbiote_id=symbiote_id,
            tools=["git", "pytest"],
        )
        assert isinstance(cfg, EnvironmentConfig)
        assert cfg.symbiote_id == symbiote_id
        assert cfg.workspace_id is None
        assert cfg.tools == ["git", "pytest"]

        fetched = manager.get_config(symbiote_id)
        assert fetched is not None
        assert fetched.id == cfg.id
        assert fetched.tools == ["git", "pytest"]

    def test_configure_with_workspace(
        self, manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        cfg = manager.configure(
            symbiote_id=symbiote_id,
            workspace_id="ws-1",
            tools=["docker"],
        )
        assert cfg.workspace_id == "ws-1"
        assert cfg.tools == ["docker"]


# ── Configure existing (update) ───────────────────────────────────────────


class TestConfigureExisting:
    def test_configure_existing_updates_in_place(
        self, manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        cfg1 = manager.configure(
            symbiote_id=symbiote_id,
            tools=["git"],
        )
        cfg2 = manager.configure(
            symbiote_id=symbiote_id,
            tools=["git", "pytest"],
            services=["redis"],
        )
        # Same record updated, not a new one
        assert cfg2.id == cfg1.id
        assert cfg2.tools == ["git", "pytest"]
        assert cfg2.services == ["redis"]

        fetched = manager.get_config(symbiote_id)
        assert fetched is not None
        assert fetched.tools == ["git", "pytest"]
        assert fetched.services == ["redis"]


# ── Get config with workspace fallback ────────────────────────────────────


class TestGetConfigFallback:
    def test_get_config_workspace_specific(
        self, manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        manager.configure(symbiote_id=symbiote_id, tools=["base-tool"])
        manager.configure(
            symbiote_id=symbiote_id, workspace_id="ws-1", tools=["ws-tool"]
        )

        result = manager.get_config(symbiote_id, workspace_id="ws-1")
        assert result is not None
        assert result.tools == ["ws-tool"]

    def test_get_config_falls_back_to_symbiote_level(
        self, manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        manager.configure(symbiote_id=symbiote_id, tools=["base-tool"])

        result = manager.get_config(symbiote_id, workspace_id="ws-nonexistent")
        assert result is not None
        assert result.tools == ["base-tool"]

    def test_get_config_nonexistent_returns_none(
        self, manager: EnvironmentManager
    ) -> None:
        assert manager.get_config("no-such-symbiote") is None


# ── List tools ─────────────────────────────────────────────────────────────


class TestListTools:
    def test_list_tools_returns_correct_list(
        self, manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        manager.configure(symbiote_id=symbiote_id, tools=["git", "pytest", "docker"])
        result = manager.list_tools(symbiote_id)
        assert result == ["git", "pytest", "docker"]

    def test_list_tools_no_config_returns_empty(
        self, manager: EnvironmentManager
    ) -> None:
        assert manager.list_tools("no-such-symbiote") == []


# ── is_tool_enabled ────────────────────────────────────────────────────────


class TestIsToolEnabled:
    def test_tool_enabled_true(
        self, manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        manager.configure(symbiote_id=symbiote_id, tools=["git", "pytest"])
        assert manager.is_tool_enabled(symbiote_id, "git") is True

    def test_tool_enabled_false(
        self, manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        manager.configure(symbiote_id=symbiote_id, tools=["git"])
        assert manager.is_tool_enabled(symbiote_id, "docker") is False

    def test_tool_enabled_no_config(self, manager: EnvironmentManager) -> None:
        assert manager.is_tool_enabled("no-such", "git") is False


# ── Configure with all fields ─────────────────────────────────────────────


class TestConfigureAllFields:
    def test_all_fields_stored_and_retrieved(
        self, manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        cfg = manager.configure(
            symbiote_id=symbiote_id,
            workspace_id="ws-full",
            tools=["git", "pytest"],
            services=["redis", "postgres"],
            humans=["alice", "bob"],
            policies={"max_tokens": 4096, "allow_exec": False},
            resources={"cpu": "2cores", "mem": "4gb"},
        )
        assert cfg.tools == ["git", "pytest"]
        assert cfg.services == ["redis", "postgres"]
        assert cfg.humans == ["alice", "bob"]
        assert cfg.policies == {"max_tokens": 4096, "allow_exec": False}
        assert cfg.resources == {"cpu": "2cores", "mem": "4gb"}

        fetched = manager.get_config(symbiote_id, workspace_id="ws-full")
        assert fetched is not None
        assert fetched.tools == ["git", "pytest"]
        assert fetched.services == ["redis", "postgres"]
        assert fetched.humans == ["alice", "bob"]
        assert fetched.policies == {"max_tokens": 4096, "allow_exec": False}
        assert fetched.resources == {"cpu": "2cores", "mem": "4gb"}
