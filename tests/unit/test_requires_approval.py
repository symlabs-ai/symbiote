"""A tool whose descriptor sets requires_approval runs only with a person's
yes: asked whatever its risk, refused when nobody can be asked."""

from __future__ import annotations

from pathlib import Path

from symbiote.environment.descriptors import ToolDescriptor
from tests.support import ScriptedLLM, make_kernel, tool_call_block


def _kernel(tmp_path: Path, *, requires_approval: bool, risk: str = "low"):
    ran: list[dict] = []

    def fn(i: int, messages: list[dict]) -> str:
        return tool_call_block("act", x=1) if i == 0 else "done"

    kernel = make_kernel(tmp_path, ScriptedLLM(fn))
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.tool_gateway.register_descriptor(
        ToolDescriptor(tool_id="act", name="act", description="acts as the user",
                       parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
                       risk_level=risk, requires_approval=requires_approval),
        lambda params: ran.append(params) or {"ok": True},
    )
    kernel.environment.configure(symbiote_id=sym.id, tools=["act"])
    return kernel, kernel.start_session(sym.id).id, ran


def _results(kernel, session_id: str) -> list:
    kernel.message(session_id, "faça")
    return kernel._latest_trace(session_id).steps  # noqa: SLF001


def test_without_a_callback_it_is_refused_not_run(tmp_path: Path) -> None:
    kernel, session, ran = _kernel(tmp_path, requires_approval=True)
    steps = _results(kernel, session)
    assert ran == []
    assert steps and steps[0].success is False
    assert "approval" in (steps[0].error or "")
    kernel.shutdown()


def test_with_a_callback_it_is_asked_even_at_low_risk(tmp_path: Path) -> None:
    kernel, session, ran = _kernel(tmp_path, requires_approval=True, risk="low")
    asked: list[tuple] = []
    kernel.set_approval_callback(lambda tool_id, params, risk: asked.append((tool_id, risk)) or True)
    _results(kernel, session)
    assert asked == [("act", "low")]
    assert ran == [{"x": 1}]
    kernel.shutdown()


def test_a_no_keeps_it_from_running(tmp_path: Path) -> None:
    kernel, session, ran = _kernel(tmp_path, requires_approval=True)
    kernel.set_approval_callback(lambda *a: False)
    steps = _results(kernel, session)
    assert ran == [] and steps[0].success is False
    kernel.shutdown()


def test_without_the_flag_nothing_changes(tmp_path: Path) -> None:
    # A plain low-risk tool runs with no callback, as before.
    kernel, session, ran = _kernel(tmp_path, requires_approval=False)
    _results(kernel, session)
    assert ran == [{"x": 1}]
    kernel.shutdown()
