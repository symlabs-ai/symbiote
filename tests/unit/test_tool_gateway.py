"""Tests for ToolGateway — T-16."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate, ToolResult
from symbiote.environment.tools import ToolGateway


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "tool_gw_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="ToolBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def gate(env_manager: EnvironmentManager, adapter: SQLiteAdapter) -> PolicyGate:
    return PolicyGate(env_manager=env_manager, storage=adapter)


@pytest.fixture()
def gw(gate: PolicyGate) -> ToolGateway:
    return ToolGateway(policy_gate=gate)


# ── register / list / has ─────────────────────────────────────────────────


class TestRegistry:
    def test_register_tool_appears_in_list(self, gw: ToolGateway) -> None:
        gw.register_tool("echo", lambda p: p)
        assert "echo" in gw.list_tools()

    def test_has_tool_true_after_register(self, gw: ToolGateway) -> None:
        gw.register_tool("echo", lambda p: p)
        assert gw.has_tool("echo") is True

    def test_has_tool_false_for_unknown(self, gw: ToolGateway) -> None:
        assert gw.has_tool("nonexistent") is False

    def test_list_tools_includes_builtins(self, gw: ToolGateway) -> None:
        tools = gw.list_tools()
        assert "fs_read" in tools
        assert "fs_write" in tools
        assert "fs_list" in tools


# ── execute ───────────────────────────────────────────────────────────────


class TestExecute:
    def test_registered_and_authorized_returns_success(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        gw.register_tool("echo", lambda p: p.get("msg", ""))
        env_manager.configure(symbiote_id=symbiote_id, tools=["echo"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="echo",
            params={"msg": "hello"},
        )
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.output == "hello"

    def test_registered_but_unauthorized_returns_blocked(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        gw.register_tool("echo", lambda p: p.get("msg", ""))
        env_manager.configure(symbiote_id=symbiote_id, tools=["git"])  # echo NOT listed

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="echo",
            params={"msg": "hello"},
        )
        assert result.success is False
        assert result.error is not None
        assert "blocked" in result.error.lower() or "not allowed" in result.error.lower()

    def test_unregistered_tool_returns_error(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["nope"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="nope",
            params={},
        )
        assert result.success is False
        assert result.error == "Tool not registered"

    def test_execute_with_workspace(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        gw.register_tool("ping", lambda p: "pong")
        env_manager.configure(
            symbiote_id=symbiote_id, workspace_id="ws-1", tools=["ping"]
        )

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="ping",
            params={},
            workspace_id="ws-1",
        )
        assert result.success is True
        assert result.output == "pong"


# ── built-in: fs_read ────────────────────────────────────────────────────


class TestFsRead:
    def test_reads_file_content(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "hello.txt"
        target.write_text("world", encoding="utf-8")

        env_manager.configure(symbiote_id=symbiote_id, tools=["fs_read"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="fs_read",
            params={"path": str(target)},
        )
        assert result.success is True
        assert result.output == "world"

    def test_missing_file_returns_error(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
        tmp_path: Path,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["fs_read"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="fs_read",
            params={"path": str(tmp_path / "no_such_file.txt")},
        )
        assert result.success is False
        assert result.error is not None


# ── built-in: fs_write ───────────────────────────────────────────────────


class TestFsWrite:
    def test_writes_file_to_disk(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "out.txt"
        env_manager.configure(symbiote_id=symbiote_id, tools=["fs_write"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="fs_write",
            params={"path": str(target), "content": "data"},
        )
        assert result.success is True
        assert result.output == "ok"
        assert target.read_text(encoding="utf-8") == "data"


# ── built-in: fs_list ────────────────────────────────────────────────────


class TestFsList:
    def test_lists_directory_contents(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
        tmp_path: Path,
    ) -> None:
        sub = tmp_path / "listing"
        sub.mkdir()
        (sub / "a.txt").write_text("a")
        (sub / "b.txt").write_text("b")
        env_manager.configure(symbiote_id=symbiote_id, tools=["fs_list"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="fs_list",
            params={"path": str(sub)},
        )
        assert result.success is True
        assert sorted(result.output) == ["a.txt", "b.txt"]
