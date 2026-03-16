"""Tests for PolicyGate — T-13."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate, PolicyResult, ToolResult


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "policy_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="PolicyBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def gate(env_manager: EnvironmentManager, adapter: SQLiteAdapter) -> PolicyGate:
    return PolicyGate(env_manager=env_manager, storage=adapter)


# ── check() ───────────────────────────────────────────────────────────────


class TestCheck:
    def test_authorized_tool_allowed(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["git", "pytest"])
        result = gate.check(symbiote_id, "git")
        assert isinstance(result, PolicyResult)
        assert result.allowed is True

    def test_unauthorized_tool_blocked(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["git"])
        result = gate.check(symbiote_id, "docker")
        assert result.allowed is False
        assert "docker" in result.reason

    def test_no_config_defaults_to_blocked(
        self, gate: PolicyGate
    ) -> None:
        result = gate.check("nonexistent-symbiote", "git")
        assert result.allowed is False
        assert result.reason  # should have a reason

    def test_check_with_workspace(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(
            symbiote_id=symbiote_id,
            workspace_id="ws-1",
            tools=["docker"],
        )
        result = gate.check(symbiote_id, "docker", workspace_id="ws-1")
        assert result.allowed is True


# ── execute_with_policy() ─────────────────────────────────────────────────


class TestExecuteWithPolicy:
    def test_authorized_calls_action_and_logs_success(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["git"])
        called = []

        def action_fn(params: dict) -> str:
            called.append(params)
            return "ok"

        result = gate.execute_with_policy(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="git",
            params={"cmd": "status"},
            action_fn=action_fn,
        )
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.tool_id == "git"
        assert result.output == "ok"
        assert result.error is None
        assert len(called) == 1
        assert called[0] == {"cmd": "status"}

    def test_blocked_does_not_call_action(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["git"])
        called = []

        def action_fn(params: dict) -> str:
            called.append(True)
            return "should not run"

        result = gate.execute_with_policy(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="docker",
            params={},
            action_fn=action_fn,
        )
        assert result.success is False
        assert result.tool_id == "docker"
        assert result.error is not None
        assert "blocked" in result.error.lower() or "not allowed" in result.error.lower()
        assert len(called) == 0

    def test_action_raises_logs_error(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["git"])

        def action_fn(params: dict) -> str:
            raise RuntimeError("connection failed")

        result = gate.execute_with_policy(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="git",
            params={"cmd": "push"},
            action_fn=action_fn,
        )
        assert result.success is False
        assert result.tool_id == "git"
        assert "connection failed" in result.error


# ── get_audit_log() ───────────────────────────────────────────────────────


class TestGetAuditLog:
    def test_returns_entries_in_desc_order(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["git", "pytest"])

        gate.execute_with_policy(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="git",
            params={"cmd": "status"},
            action_fn=lambda p: "ok",
        )
        gate.execute_with_policy(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="pytest",
            params={"args": "-v"},
            action_fn=lambda p: "passed",
        )

        log = gate.get_audit_log(symbiote_id)
        assert len(log) >= 2
        # Most recent first
        assert log[0]["tool_id"] == "pytest"
        assert log[1]["tool_id"] == "git"

    def test_audit_log_contains_correct_fields(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["git"])

        gate.execute_with_policy(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="git",
            params={"cmd": "status"},
            action_fn=lambda p: "ok",
        )

        log = gate.get_audit_log(symbiote_id)
        assert len(log) >= 1
        entry = log[0]
        assert entry["tool_id"] == "git"
        assert entry["action"] == "execute"
        assert entry["result"] == "success"
        assert "cmd" in entry["params_json"]

    def test_blocked_action_logged(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["git"])

        gate.execute_with_policy(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="docker",
            params={},
            action_fn=lambda p: "nope",
        )

        log = gate.get_audit_log(symbiote_id)
        assert len(log) >= 1
        entry = log[0]
        assert entry["tool_id"] == "docker"
        assert entry["action"] == "blocked"
        assert entry["result"] == "blocked"

    def test_error_action_logged(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["git"])

        gate.execute_with_policy(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="git",
            params={},
            action_fn=lambda p: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        log = gate.get_audit_log(symbiote_id)
        assert len(log) >= 1
        entry = log[0]
        assert entry["action"] == "execute"
        assert entry["result"].startswith("error:")

    def test_limit_parameter(
        self,
        gate: PolicyGate,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["git"])

        for i in range(5):
            gate.execute_with_policy(
                symbiote_id=symbiote_id,
                session_id="sess-1",
                tool_id="git",
                params={"i": i},
                action_fn=lambda p: "ok",
            )

        log = gate.get_audit_log(symbiote_id, limit=3)
        assert len(log) == 3
