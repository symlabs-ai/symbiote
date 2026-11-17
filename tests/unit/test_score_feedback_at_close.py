"""The host's score survives the close of the session (B-80).

``report_feedback`` given before ``close_session`` used to be overwritten:
the close recomputed ``final_score`` from the automatic score alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.core.scoring import compute_final_score
from symbiote.harness.evolver import EVOLVABLE_COMPONENTS
from tests.support import ScriptedLLM, make_kernel


@pytest.fixture()
def kernel(tmp_path: Path):
    k = make_kernel(tmp_path, ScriptedLLM("ok"))
    yield k
    k.shutdown()


def _score(kernel, session_id: str) -> dict:
    return kernel._storage.fetch_one(
        "SELECT * FROM session_scores WHERE session_id = ?", (session_id,)
    )


def test_feedback_before_close_stays_in_the_final_score(kernel) -> None:
    sym = kernel.create_symbiote(name="S", role="assistant")
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "hello")

    kernel.report_feedback(session.id, 0.1)
    kernel.close_session(session.id)

    row = _score(kernel, session.id)
    assert row["user_score"] == 0.1
    assert row["final_score"] == compute_final_score(row["auto_score"], 0.1)
    assert row["final_score"] != row["auto_score"]


def test_feedback_survives_a_reopened_session_closed_again(kernel) -> None:
    sym = kernel.create_symbiote(name="S", role="assistant")
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "hello")
    kernel.close_session(session.id)
    kernel.report_feedback(session.id, 0.2)

    kernel._sessions.resume(session.id)
    kernel.close_session(session.id)

    row = _score(kernel, session.id)
    assert row["user_score"] == 0.2
    assert row["final_score"] == compute_final_score(row["auto_score"], 0.2)


def test_close_without_feedback_scores_as_before(kernel) -> None:
    sym = kernel.create_symbiote(name="S", role="assistant")
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "hello")

    kernel.close_session(session.id)

    row = _score(kernel, session.id)
    assert row["user_score"] is None
    assert row["final_score"] == row["auto_score"]


def _harness_sessions(kernel, symbiote_id: str) -> int:
    """Sessions counted for the built-in harness text of one component."""
    row = kernel.harness_versions.get_baseline(symbiote_id, sorted(EVOLVABLE_COMPONENTS)[0])
    return row["session_count"] if row else 0


def test_session_scored_by_the_host_first_still_counts_for_the_harness_once(kernel) -> None:
    """The harness version score is fed by the first *close* of a session: a
    score row created earlier by ``report_feedback`` must not hide it, and a
    second close must not count it twice."""
    sym = kernel.create_symbiote(name="S", role="assistant")
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "hello")

    kernel.report_feedback(session.id, 0.9)
    kernel.close_session(session.id)
    assert _harness_sessions(kernel, sym.id) == 1

    kernel._sessions.resume(session.id)
    kernel.close_session(session.id)
    assert _harness_sessions(kernel, sym.id) == 1
