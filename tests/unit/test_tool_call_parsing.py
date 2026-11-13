"""Regression: malformed tool calls never crash the turn and never run a tool
with made-up arguments (report item E-7)."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from symbiote.adapters.llm.forge import ForgeLLMAdapter, _extract_tool_calls
from symbiote.environment.parser import parse_tool_calls
from tests.support import ScriptedLLM, add_tool, make_kernel


def _block(payload: str) -> str:
    return "```tool_call\n" + payload + "\n```"


def test_list_of_calls_in_one_block() -> None:
    text = _block(json.dumps([{"tool": "a", "params": {"x": 1}}, {"tool": "b", "params": {}}]))
    clean, calls = parse_tool_calls(text)
    assert [(c.tool_id, c.params) for c in calls] == [("a", {"x": 1}), ("b", {})]
    assert clean == ""


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"tool": "a", "params": null}', [("a", {})]),
        ('{"tool": "a"}', [("a", {})]),
        ('{"tool": "a", "params": "{\\"x\\": 1}"}', [("a", {"x": 1})]),
        ('{"tool": "a", "params": "not json"}', []),
        ('{"tool": 123, "params": {}}', []),
        ('{"tool": "", "params": {}}', []),
        ("[1, 2, 3]", []),
        ('"just a string"', []),
        ("{not json", []),
    ],
)
def test_malformed_payloads_do_not_raise(payload: str, expected) -> None:
    _clean, calls = parse_tool_calls("Sure.\n" + _block(payload))
    assert [(c.tool_id, c.params) for c in calls] == expected


def test_fence_inside_a_param_value_is_kept() -> None:
    note = "Use:\n```python\nprint(1)\n```\nthen run it."
    text = "Saving.\n" + _block(json.dumps({"tool": "save_note", "params": {"text": note}}))
    clean, calls = parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].params["text"] == note
    assert clean == "Saving."
    assert "```" not in clean


def test_malformed_block_through_the_kernel(tmp_path: Path) -> None:
    replies = iter(
        [
            _block(json.dumps([{"tool": "save", "params": {"t": "a"}}])),
            _block('{"tool": "save", "params": null}'),
            "Saved.",
        ]
    )
    llm = ScriptedLLM(lambda i, m: next(replies))
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    seen: list[dict] = []
    add_tool(kernel, sym.id, "save", lambda p: seen.append(p) or "ok")
    session = kernel.start_session(sym.id)

    out = kernel.message(session.id, "save the note")

    assert seen == [{"t": "a"}, {}]
    assert (out["text"] if isinstance(out, dict) else out) == "Saved."
    kernel.shutdown()


def _resp(arguments):
    return types.SimpleNamespace(
        content="",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "delete_records", "arguments": arguments}}
        ],
        metadata=None,
    )


@pytest.mark.parametrize("arguments", ['{"filter": {"status": "arch', "[1, 2]", "null"])
def test_native_invalid_arguments_are_flagged(arguments) -> None:
    (call,) = _extract_tool_calls(_resp(arguments))
    assert call.params == {}
    assert call.arguments_error


def test_native_valid_arguments_are_not_flagged() -> None:
    (call,) = _extract_tool_calls(_resp('{"filter": {"status": "archived"}}'))
    assert call.params == {"filter": {"status": "archived"}}
    assert call.arguments_error is None


class _StubAgent:
    def __init__(self, resp) -> None:
        self.resp = resp

    def chat(self, **kwargs):
        return self.resp


def _adapter(resp) -> ForgeLLMAdapter:
    adapter = ForgeLLMAdapter.__new__(ForgeLLMAdapter)
    adapter._agent = _StubAgent(resp)
    return adapter


def test_truncated_native_call_is_not_executed(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, _adapter(_resp('{"filter": {"status": "arch')))
    kernel.set_native_tools(True)
    sym = kernel.create_symbiote(name="a", role="assistant")
    deleted: list[dict] = []
    add_tool(
        kernel,
        sym.id,
        "delete_records",
        lambda p: deleted.append(p) or "deleted",
        parameters={"type": "object", "properties": {"filter": {"type": "object"}}},
        tool_mode="instant",
    )
    session = kernel.start_session(sym.id)

    out = kernel.message(session.id, "delete the archived ones")

    assert deleted == []
    result = out["tool_results"][0]
    assert result["success"] is False
    assert "argument" in result["error"].lower()
    kernel.shutdown()


def test_none_content_from_adapter_does_not_crash(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, _adapter(types.SimpleNamespace(content=None, tool_calls=None, metadata=None)))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)

    out = kernel.message(session.id, "hi")

    assert isinstance(out, str)
    kernel.shutdown()
