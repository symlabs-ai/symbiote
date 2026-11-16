"""A host can choose which tool risk levels need a human answer."""

from pathlib import Path

import pytest

from symbiote.environment.descriptors import ToolDescriptor
from tests.support import ScriptedLLM, make_kernel, tool_call_block


@pytest.mark.parametrize(
    ("risk", "threshold", "required", "asked"),
    [
        ("low", "high", False, False),
        ("medium", "high", False, False),
        ("high", "high", False, True),
        ("low", "medium", False, False),
        ("medium", "medium", False, True),
        ("low", "low", False, True),
        ("low", "high", True, True),
    ],
)
def test_approval_threshold(
    tmp_path: Path, risk: str, threshold: str, required: bool, asked: bool
) -> None:
    calls: list[str] = []
    approvals: list[str] = []

    def script(index: int, messages: list[dict]) -> str:
        return tool_call_block("action") if index == 0 else "done"

    kernel = make_kernel(tmp_path, ScriptedLLM(script))
    symbiote = kernel.create_symbiote(name="agent", role="assistant")
    kernel.tool_gateway.register_descriptor(
        ToolDescriptor(
            tool_id="action", name="action", description="test action",
            risk_level=risk, requires_approval=required,
        ),
        lambda params: calls.append("ran") or {"ok": True},
    )
    kernel.environment.configure(symbiote_id=symbiote.id, tools=["action"])
    kernel.set_approval_callback(
        lambda tool_id, params, level: approvals.append(level) or True,
        risk_threshold=threshold,
    )
    try:
        session = kernel.start_session(symbiote.id)
        kernel.message(session.id, "run")
        assert approvals == ([risk] if asked else [])
        assert calls == ["ran"]
    finally:
        kernel.shutdown()


def test_invalid_approval_threshold_is_rejected(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM(lambda i, messages: "done"))
    try:
        with pytest.raises(ValueError, match="Unknown approval risk threshold"):
            kernel.set_approval_callback(lambda *args: True, risk_threshold="unknown")
    finally:
        kernel.shutdown()
