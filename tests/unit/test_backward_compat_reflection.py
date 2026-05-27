"""Backward-compat guard for the Sprint 1 LLM-reflection rollout.

Pins the invariant: a kernel created without touching any of the new flags
(reflection_mode default 'keyword', no _evolver_llm set, no opt-in) must
trigger ZERO extra LLM calls on close_session compared to v0.6.0 behaviour.

If this test starts failing, someone changed a default and you're about to
ship surprise inference cost to embedded clients (you_news, sym_talk_lt).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel


class _CountingLLM:
    """LLM that counts calls; returns a trivial assistant response."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, config=None, tools=None):
        self.calls += 1
        return "ok"


@pytest.fixture()
def kernel(tmp_path: Path):
    cfg = KernelConfig(db_path=tmp_path / "compat.db", context_budget=4000)
    llm = _CountingLLM()
    k = SymbioteKernel(config=cfg, llm=llm)
    yield k, llm
    k.shutdown()


class TestNoSurpriseCost:
    def test_default_config_close_session_runs_keyword_reflection(self, kernel):
        """Default mode='keyword' -> ReflectionEngine never calls LLM."""
        k, llm = kernel
        sym = k.create_symbiote(name="compat", role="assistant")
        session = k.start_session(sym.id, goal="test")

        # No EnvironmentManager.configure() call -> default reflection_mode
        baseline = llm.calls
        k.close_session(session.id)

        # Reflection must NOT have invoked the LLM
        assert llm.calls == baseline, (
            f"Expected 0 extra LLM calls in close_session with default config, "
            f"got {llm.calls - baseline}. A default changed — investigate before merge."
        )

    def test_configure_keyword_mode_explicit_also_silent(self, kernel):
        """Explicit reflection_mode='keyword' behaves identically to default."""
        k, llm = kernel
        sym = k.create_symbiote(name="compat2", role="assistant")
        k._environment.configure(symbiote_id=sym.id, reflection_mode="keyword")
        session = k.start_session(sym.id, goal="test")

        baseline = llm.calls
        k.close_session(session.id)
        assert llm.calls == baseline


class TestEvolverRequirementGuard:
    def test_llm_mode_without_evolver_raises_value_error(self, kernel):
        """The Sprint 1 cost-safety contract: fail loud, fail at configure()."""
        k, _llm = kernel
        sym = k.create_symbiote(name="guarded", role="assistant")
        with pytest.raises(ValueError, match=r"set_evolver_llm"):
            k._environment.configure(symbiote_id=sym.id, reflection_mode="llm")

    def test_hybrid_mode_without_evolver_raises_too(self, kernel):
        k, _llm = kernel
        sym = k.create_symbiote(name="guarded2", role="assistant")
        with pytest.raises(ValueError, match=r"set_evolver_llm"):
            k._environment.configure(symbiote_id=sym.id, reflection_mode="hybrid")

    def test_llm_mode_with_evolver_set_succeeds(self, kernel):
        k, _llm = kernel
        k.set_evolver_llm(_CountingLLM())
        sym = k.create_symbiote(name="ok", role="assistant")
        # Should NOT raise
        cfg = k._environment.configure(symbiote_id=sym.id, reflection_mode="llm")
        assert cfg.reflection_mode == "llm"

    def test_llm_main_mode_uses_main_llm(self, kernel):
        """llm_main is the explicit opt-in to paying with the main model."""
        k, _llm = kernel
        # No evolver set, but main is set in fixture
        sym = k.create_symbiote(name="explicit-main", role="assistant")
        cfg = k._environment.configure(symbiote_id=sym.id, reflection_mode="llm_main")
        assert cfg.reflection_mode == "llm_main"
