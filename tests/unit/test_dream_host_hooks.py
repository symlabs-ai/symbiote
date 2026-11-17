"""What a host can do around a dream cycle (B-85, B-86).

``set_dream_callback`` and ``wait_dream`` replace waiting on the private
``kernel._dream_threads``; ``dream_llm`` picks the model the dream calls.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from symbiote.core.models import MemoryEntry
from symbiote.dream.engine import DreamEngine
from symbiote.dream.models import DreamReport
from tests.support import ScriptedLLM, make_kernel


def _dreamer(tmp_path: Path, llm=None, **config):
    kernel = make_kernel(tmp_path, llm or ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="S", role="assistant")
    config.setdefault("dream_mode", "light")
    config.setdefault("dream_min_sessions", 1)
    kernel.environment.configure(symbiote_id=sym.id, **config)
    return kernel, sym


def _close_a_session(kernel, symbiote_id: str) -> None:
    session = kernel.start_session(symbiote_id)
    kernel.message(session.id, "hello")
    kernel.close_session(session.id)


def test_callback_gets_the_report_of_an_automatic_cycle(tmp_path: Path) -> None:
    kernel, sym = _dreamer(tmp_path)
    seen: list[tuple] = []
    kernel.set_dream_callback(lambda *args: seen.append((*args, threading.current_thread().name)))
    try:
        _close_a_session(kernel, sym.id)

        assert kernel.wait_dream(sym.id, timeout=10) is True
        ((symbiote_id, report, error, thread_name),) = seen
        assert symbiote_id == sym.id
        assert isinstance(report, DreamReport)
        assert report.symbiote_id == sym.id
        assert report.completed_at is not None
        assert error is None
        assert thread_name.startswith("dream-")
    finally:
        kernel.shutdown()


def test_callback_also_runs_for_a_cycle_asked_by_the_host(tmp_path: Path) -> None:
    kernel, sym = _dreamer(tmp_path, dream_mode="off")
    seen: list[tuple] = []
    kernel.set_dream_callback(lambda *args: seen.append(args))
    try:
        report = kernel.dream(sym.id, dry_run=True)

        assert seen == [(sym.id, report, None)]
        assert report.dry_run is True
    finally:
        kernel.shutdown()


def test_callback_is_told_when_the_cycle_failed(tmp_path: Path, monkeypatch) -> None:
    kernel, sym = _dreamer(tmp_path)
    seen: list[tuple] = []
    kernel.set_dream_callback(lambda *args: seen.append(args))

    def broken(self, symbiote_id, dream_mode):
        raise RuntimeError("boom")

    monkeypatch.setattr(DreamEngine, "dream", broken)
    try:
        _close_a_session(kernel, sym.id)
        assert kernel.wait_dream(sym.id, timeout=10) is True
        ((symbiote_id, report, error),) = seen
        assert (symbiote_id, report) == (sym.id, None)
        assert isinstance(error, RuntimeError)

        with pytest.raises(RuntimeError):
            kernel.dream(sym.id)
        assert len(seen) == 2
        assert isinstance(seen[1][2], RuntimeError)
    finally:
        kernel.shutdown()


def test_a_failing_callback_does_not_break_the_cycle(tmp_path: Path) -> None:
    kernel, sym = _dreamer(tmp_path)

    def bad_callback(*_args) -> None:
        raise ValueError("host bug")

    kernel.set_dream_callback(bad_callback)
    try:
        _close_a_session(kernel, sym.id)

        assert kernel.wait_dream(sym.id, timeout=10) is True
        assert kernel.dream(sym.id).completed_at is not None
        # The cycle is over: a later close can start the next one.
        _close_a_session(kernel, sym.id)
        assert kernel.wait_dream(sym.id, timeout=10) is True
    finally:
        kernel.shutdown()


def test_wait_dream_waits_for_the_cycle_and_for_the_callback(tmp_path: Path) -> None:
    kernel, sym = _dreamer(tmp_path)
    release = threading.Event()
    started = threading.Event()

    def slow_callback(*_args) -> None:
        started.set()
        release.wait(timeout=10)

    kernel.set_dream_callback(slow_callback)
    try:
        assert kernel.wait_dream(sym.id, timeout=0.1) is True  # nothing running

        _close_a_session(kernel, sym.id)
        assert started.wait(timeout=10)
        assert kernel.wait_dream(sym.id, timeout=0.05) is False

        release.set()
        assert kernel.wait_dream(sym.id, timeout=10) is True
    finally:
        release.set()
        kernel.shutdown()


def test_removing_the_callback_stops_the_calls(tmp_path: Path) -> None:
    kernel, sym = _dreamer(tmp_path, dream_mode="off")
    seen: list[tuple] = []
    kernel.set_dream_callback(lambda *args: seen.append(args))
    kernel.set_dream_callback(None)
    try:
        kernel.dream(sym.id)
        assert seen == []
    finally:
        kernel.shutdown()


# ── which model dreams (B-86) ────────────────────────────────────────────


def _duplicates(kernel, symbiote_id: str) -> None:
    """Three memories on one theme: the ``generalize`` phase calls the model."""
    for text in (
        "run the tests before deploy",
        "check migrations before deploy",
        "announce the deploy in the channel",
    ):
        kernel.memory.store(
            MemoryEntry(
                symbiote_id=symbiote_id, scope="global", source="reflection",
                type="procedural", tags=["deploy", "checklist"],
                importance=0.6, content=text,
            )
        )


@pytest.mark.parametrize(
    ("dream_llm", "expected"),
    [("aux", "aux"), ("main", "main"), (None, "aux")],
)
def test_dream_calls_the_model_the_symbiote_chose(tmp_path: Path, dream_llm, expected) -> None:
    main, aux = ScriptedLLM("Always check the deploy checklist."), ScriptedLLM("Follow the deploy checklist.")
    kernel = make_kernel(tmp_path, main)
    kernel.set_evolver_llm(aux)
    sym = kernel.create_symbiote(name="S", role="assistant")
    options = {"dream_mode": "full"}
    if dream_llm is not None:
        options["dream_llm"] = dream_llm
    kernel.environment.configure(symbiote_id=sym.id, **options)
    _duplicates(kernel, sym.id)
    try:
        report = kernel.dream(sym.id)

        assert report.total_llm_calls > 0
        called = {"main": len(main.calls), "aux": len(aux.calls)}
        other = "aux" if expected == "main" else "main"
        assert called[expected] == report.total_llm_calls
        assert called[other] == 0
    finally:
        kernel.shutdown()


def test_dream_falls_back_to_the_main_model_without_an_auxiliary_one(tmp_path: Path) -> None:
    main = ScriptedLLM("Always check the deploy checklist.")
    kernel, sym = _dreamer(tmp_path, main, dream_mode="full")
    _duplicates(kernel, sym.id)
    try:
        report = kernel.dream(sym.id)
        assert len(main.calls) == report.total_llm_calls > 0
    finally:
        kernel.shutdown()
