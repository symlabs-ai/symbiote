"""Regression: a process resumes from its persisted step and work() routes a
'name: details' task to the process (report item E-2)."""

from __future__ import annotations

from pathlib import Path

from symbiote.process.engine import ProcessEngine
from tests.support import ScriptedLLM, make_kernel


def test_advance_continues_from_persisted_step(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="r")
    session = kernel.start_session(sym.id)
    engine = ProcessEngine(kernel._storage)
    name = engine.list_definitions()[0]
    steps = [s.name for s in engine._definitions[name].steps]
    assert len(steps) >= 3

    instance = engine.start(session.id, name)
    instance = engine.advance(instance.id)
    assert instance.current_step == steps[1]

    restarted = ProcessEngine(kernel._storage)  # new process, empty caches
    instance = restarted.advance(instance.id)

    assert instance.current_step == steps[2]
    assert [log["step"] for log in instance.logs] == steps[:2]
    kernel.shutdown()


def test_invalidated_cache_does_not_rewind(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="r")
    session = kernel.start_session(sym.id)
    engine = ProcessEngine(kernel._storage)
    name = engine.list_definitions()[0]
    steps = [s.name for s in engine._definitions[name].steps]

    instance = engine.start(session.id, name)
    engine.advance(instance.id)
    engine.invalidate_cache()
    instance = engine.advance(instance.id)

    assert instance.current_step == steps[2]
    kernel.shutdown()


def test_work_with_details_runs_the_process(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="r")
    session = kernel.start_session(sym.id)

    result = kernel.capabilities.work(sym.id, session.id, "research_task: map competitors")

    assert result["success"] is True, result
    assert result["output"]["process_name"] == "research_task"
    assert result["output"]["state"] == "completed"
    kernel.shutdown()
