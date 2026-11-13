"""Regression: harness rollback goes to the right version, the first evolved
version can be rolled back, and evolution guards count real sessions
(report item M-7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.harness.evolver import MIN_SESSIONS_FOR_VERDICT, HarnessEvolver
from symbiote.harness.tuner import ParameterTuner
from tests.support import ScriptedLLM, make_kernel

COMP = "tool_instructions"
DEFAULT = "Use tools carefully.\nCRITICAL: never invent tool names.\nAnswer briefly."


def _active(kernel, sid: str):
    row = kernel.harness_versions.get_active_version(sid, COMP)
    return row["content"] if row else None


def test_rollback_goes_to_parent_not_previous_number(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="s", role="x").id
    hv = kernel.harness_versions
    hv.create_version(sid, COMP, "v1 GOOD", parent_version=None)
    hv.create_version(sid, COMP, "v2 BAD", parent_version=1)
    assert hv.rollback(sid, COMP)
    assert _active(kernel, sid) == "v1 GOOD"
    hv.create_version(sid, COMP, "v3 WORSE", parent_version=1)

    assert hv.rollback(sid, COMP)

    assert _active(kernel, sid) == "v1 GOOD"
    kernel.shutdown()


def test_rolling_back_a_version_built_from_default_restores_default(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="s", role="x").id
    hv = kernel.harness_versions
    hv.create_version(sid, COMP, "v1 from default", parent_version=None)

    assert hv.rollback(sid, COMP)

    assert _active(kernel, sid) is None  # the built-in default text applies
    kernel.shutdown()


def test_manual_versions_chain_their_parent(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="s", role="x").id
    hv = kernel.harness_versions
    hv.create_version(sid, COMP, "manual 1")
    hv.create_version(sid, COMP, "manual 2")

    assert hv.rollback(sid, COMP)

    assert _active(kernel, sid) == "manual 1"
    kernel.shutdown()


def test_first_evolved_version_is_compared_with_default_baseline(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="s", role="x").id
    hv = kernel.harness_versions
    for _ in range(MIN_SESSIONS_FOR_VERDICT):
        hv.update_score(sid, COMP, 0.9)  # sessions under the default text
    evolver = HarnessEvolver(kernel._storage, hv)
    hv.create_version(sid, COMP, "v1 TERRIBLE", parent_version=evolver_baseline(hv, sid))
    for _ in range(MIN_SESSIONS_FOR_VERDICT):
        hv.update_score(sid, COMP, 0.0)

    check = evolver.check_rollback(sid, COMP)

    assert check.should_rollback is True
    kernel.shutdown()


def evolver_baseline(hv, sid: str):
    return hv.BASELINE_VERSION


def _session(kernel, sid: str, score: float, n_traces: int, stop: str, now: str) -> None:
    session_id = kernel.start_session(sid).id
    kernel._storage.execute(
        "INSERT INTO session_scores (id, session_id, symbiote_id, auto_score, final_score, "
        "stop_reason, computed_at) VALUES (?,?,?,?,?,?,?)",
        (str(uuid4()), session_id, sid, score, score, stop, now),
    )
    for _ in range(n_traces):
        kernel._storage.execute(
            "INSERT INTO execution_traces (id, session_id, symbiote_id, total_iterations, "
            "total_tool_calls, total_elapsed_ms, stop_reason, steps_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid4()), session_id, sid, 3, 3, 10, stop, "[]", now),
        )


def test_traces_do_not_inflate_session_counts(tmp_path: Path) -> None:
    proposer = MockLLMAdapter(DEFAULT.replace("carefully.", "carefully!"))
    kernel = make_kernel(tmp_path, proposer)
    sid = kernel.create_symbiote(name="s", role="x").id
    now = datetime.now(tz=UTC).isoformat()
    _session(kernel, sid, 0.1, 5, "max_iterations", now)  # 1 failed session, 5 traces
    _session(kernel, sid, 0.9, 3, "end_turn", now)  # 1 good session, 3 traces

    result = kernel.evolve_harness(sid, COMP, DEFAULT)

    assert result.success is False
    assert "failed sessions (1/" in result.reason
    kernel.shutdown()


def test_minimum_interval_between_evolutions(tmp_path: Path) -> None:
    proposer = MockLLMAdapter(DEFAULT.replace("carefully.", "carefully!"))
    kernel = make_kernel(tmp_path, proposer)
    sid = kernel.create_symbiote(name="s", role="x").id
    now = datetime.now(tz=UTC).isoformat()
    for _ in range(6):
        _session(kernel, sid, 0.1, 1, "max_iterations", now)
    for _ in range(4):
        _session(kernel, sid, 0.9, 1, "end_turn", now)

    first = kernel.evolve_harness(sid, COMP, DEFAULT)
    second = kernel.evolve_harness(sid, COMP, DEFAULT)

    assert first.success is True
    assert second.success is False
    assert "interval" in second.reason or "sessions" in second.reason
    assert len(kernel.harness_versions.list_versions(sid, COMP)) == 1
    _ = timedelta  # keep import for readers adjusting dates
    kernel.shutdown()


def test_tuner_does_not_lower_a_limit_most_sessions_hit(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="s", role="x").id
    kernel.environment.configure(symbiote_id=sid, max_tool_iterations=10)
    now = datetime.now(tz=UTC).isoformat()

    def trace(stop: str, iters: int) -> None:
        kernel._storage.execute(
            "INSERT INTO execution_traces (id, session_id, symbiote_id, total_iterations, "
            "total_tool_calls, total_elapsed_ms, stop_reason, steps_json, tool_mode, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(uuid4()), "x", sid, iters, iters, 10, stop, "[]", "brief", now),
        )

    for _ in range(25):
        trace("max_iterations", 10)
    for _ in range(5):
        trace("end_turn", 1)

    result = ParameterTuner(kernel._storage).analyze(sid)

    assert result.adjustments.get("max_tool_iterations", 10) >= 10
    kernel.shutdown()
