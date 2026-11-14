"""Regression: kernel.shutdown() waits for background work before closing
the database (report item M-9)."""

from __future__ import annotations

import time
from pathlib import Path

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from tests.support import ScriptedLLM, make_kernel


class SlowEvolver:
    def __init__(self, delay: float) -> None:
        self.delay = delay

    def complete(self, messages, config=None, tools=None):
        time.sleep(self.delay)
        return "[]"


def _kernel_with_review(tmp_path: Path, delay: float):
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    kernel.set_evolver_llm(SlowEvolver(delay))
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.environment.configure(symbiote_id=sym.id, skill_review_enabled=True)
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "we fixed the cache by clearing it")
    kernel.close_session(session.id)  # spawns the final skill review
    return kernel


def test_shutdown_waits_for_review(tmp_path: Path, caplog) -> None:
    kernel = _kernel_with_review(tmp_path, delay=0.5)

    kernel.shutdown()

    reopened = SQLiteAdapter(tmp_path / "symbiote.db")
    audits = reopened.fetch_one("SELECT COUNT(*) AS c FROM skill_review_audit")["c"]
    reopened.close()
    assert audits == 1
    assert "closed database" not in caplog.text


def test_shutdown_timeout_returns_on_time(tmp_path: Path) -> None:
    kernel = _kernel_with_review(tmp_path, delay=2.0)

    start = time.monotonic()
    kernel.shutdown(timeout=0.1)

    assert time.monotonic() - start < 1.0
