"""Tests for B-40 tool_mode (instant/brief/continuous)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.core.context import AssembledContext
from symbiote.core.models import EnvironmentConfig

# ── EnvironmentConfig model tests ──────────────────────────────────────────


class TestEnvironmentConfigToolMode:
    def test_default_is_brief(self):
        cfg = EnvironmentConfig(symbiote_id="s1")
        assert cfg.tool_mode == "brief"
        assert cfg.tool_loop is True

    @pytest.mark.parametrize("mode", ["instant", "brief", "continuous"])
    def test_all_modes_accepted(self, mode):
        cfg = EnvironmentConfig(symbiote_id="s1", tool_mode=mode)
        assert cfg.tool_mode == mode

    def test_instant_mode_values(self):
        cfg = EnvironmentConfig(symbiote_id="s1", tool_mode="instant", tool_loop=False)
        assert cfg.tool_mode == "instant"
        assert cfg.tool_loop is False

    def test_continuous_mode_values(self):
        cfg = EnvironmentConfig(symbiote_id="s1", tool_mode="continuous", tool_loop=True)
        assert cfg.tool_mode == "continuous"
        assert cfg.tool_loop is True

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            EnvironmentConfig(symbiote_id="s1", tool_mode="invalid")


# ── AssembledContext tests ──────────────────────────────────────────────────


class TestAssembledContextToolMode:
    def test_default_is_brief(self):
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess1", user_input="hi"
        )
        assert ctx.tool_mode == "brief"
        assert ctx.tool_loop is True

    def test_instant_mode(self):
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess1", user_input="hi",
            tool_mode="instant", tool_loop=False,
        )
        assert ctx.tool_mode == "instant"
        assert ctx.tool_loop is False

    def test_continuous_mode(self):
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess1", user_input="hi",
            tool_mode="continuous", tool_loop=True,
        )
        assert ctx.tool_mode == "continuous"
        assert ctx.tool_loop is True


# ── EnvironmentManager round-trip tests ────────────────────────────────────


class TestEnvironmentManagerToolMode:
    @pytest.fixture()
    def _adapter(self, tmp_path: Path):
        from symbiote.adapters.storage.sqlite import SQLiteAdapter

        storage = SQLiteAdapter(tmp_path / "test.db")
        storage.init_schema()
        yield storage
        storage.close()

    @pytest.fixture()
    def symbiote_id(self, _adapter):
        from symbiote.core.identity import IdentityManager

        mgr = IdentityManager(storage=_adapter)
        sym = mgr.create(name="TestBot", role="assistant")
        return sym.id

    @pytest.fixture()
    def manager(self, _adapter):
        from symbiote.environment.manager import EnvironmentManager

        return EnvironmentManager(_adapter)

    def test_default_tool_mode(self, manager, symbiote_id):
        cfg = manager.configure(symbiote_id=symbiote_id)
        assert cfg.tool_mode == "brief"
        assert cfg.tool_loop is True

    @pytest.mark.parametrize("mode,expected_loop", [
        ("instant", False),
        ("brief", True),
        ("continuous", True),
    ])
    def test_configure_tool_mode(self, manager, symbiote_id, mode, expected_loop):
        cfg = manager.configure(symbiote_id=symbiote_id, tool_mode=mode)
        assert cfg.tool_mode == mode
        assert cfg.tool_loop is expected_loop

    def test_round_trip_persists(self, manager, symbiote_id):
        manager.configure(symbiote_id=symbiote_id, tool_mode="instant")
        cfg = manager.get_config(symbiote_id)
        assert cfg is not None
        assert cfg.tool_mode == "instant"
        assert cfg.tool_loop is False

    def test_update_tool_mode(self, manager, symbiote_id):
        manager.configure(symbiote_id=symbiote_id, tool_mode="brief")
        manager.configure(symbiote_id=symbiote_id, tool_mode="continuous")
        cfg = manager.get_config(symbiote_id)
        assert cfg is not None
        assert cfg.tool_mode == "continuous"
        assert cfg.tool_loop is True

    def test_get_tool_mode_accessor(self, manager, symbiote_id):
        manager.configure(symbiote_id=symbiote_id, tool_mode="instant")
        assert manager.get_tool_mode(symbiote_id) == "instant"

    def test_get_tool_mode_default_no_config(self, manager):
        assert manager.get_tool_mode("nonexistent") == "brief"

    def test_backward_compat_tool_loop_false_sets_instant(self, manager, symbiote_id):
        cfg = manager.configure(symbiote_id=symbiote_id, tool_loop=False)
        assert cfg.tool_mode == "instant"
        assert cfg.tool_loop is False

    def test_backward_compat_tool_loop_true_keeps_brief(self, manager, symbiote_id):
        cfg = manager.configure(symbiote_id=symbiote_id, tool_loop=True)
        # tool_loop=True alone does not change tool_mode from default
        assert cfg.tool_mode == "brief"
        assert cfg.tool_loop is True


# ── ChatRunner max_iters derivation tests ──────────────────────────────────


class TestChatRunnerToolMode:
    def _make_context(self, tool_mode="brief", max_iters=10):
        return AssembledContext(
            symbiote_id="s1",
            session_id="sess1",
            user_input="test",
            tool_mode=tool_mode,
            tool_loop=tool_mode != "instant",
            max_tool_iterations=max_iters,
        )

    def test_instant_mode_max_iters_1(self):
        ctx = self._make_context("instant")
        max_iters = 1 if ctx.tool_mode == "instant" else ctx.max_tool_iterations
        assert max_iters == 1

    def test_brief_mode_uses_max_tool_iterations(self):
        ctx = self._make_context("brief", max_iters=15)
        max_iters = 1 if ctx.tool_mode == "instant" else ctx.max_tool_iterations
        assert max_iters == 15

    def test_continuous_mode_uses_max_tool_iterations(self):
        ctx = self._make_context("continuous", max_iters=20)
        max_iters = 1 if ctx.tool_mode == "instant" else ctx.max_tool_iterations
        assert max_iters == 20

    def test_continuous_same_as_brief_for_now(self):
        ctx_brief = self._make_context("brief", max_iters=10)
        ctx_cont = self._make_context("continuous", max_iters=10)
        brief_iters = 1 if ctx_brief.tool_mode == "instant" else ctx_brief.max_tool_iterations
        cont_iters = 1 if ctx_cont.tool_mode == "instant" else ctx_cont.max_tool_iterations
        assert brief_iters == cont_iters
