"""Regression: ordinary answers (citations, links, checklists, requested JSON)
are not mistaken for leaked tool calls (report item E-8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.runners.response_validator import fallback_text, is_valid_response
from tests.support import ScriptedLLM, make_kernel


@pytest.mark.parametrize(
    "text",
    [
        "According to the sources:\n[1] Annual report 2025\n[2] Technical note 14",
        "[Docs](https://example.com/docs) explain the setup.",
        "- item\n[x] done\n[ ] pending",
        'Here is the example you asked for:\n```json\n{"tool": "x", "params": {}}\n```',
        '{"name": "Ana", "age": 31}',
        '["red", "green"]',
    ],
)
def test_legitimate_answers_are_valid(text: str) -> None:
    assert is_valid_response(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "```tool_call\n{bad json}\n```",
        'I will call {"tool": "search", "params": {"q": "x"}} now.',
        '{ "tool" : "some_tool", "params": {} }',
        'Calling now:\n{"params": {"id": 3}}',
    ],
)
def test_leaked_tool_calls_are_invalid(text: str) -> None:
    assert is_valid_response(text) is False


def _text(out) -> str:
    return out["text"] if isinstance(out, dict) else out


def test_citation_answer_costs_one_call_and_arrives_intact(tmp_path: Path) -> None:
    answer = "According to the sources:\n[1] Annual report 2025\n[2] Technical note 14"
    llm = ScriptedLLM(answer)
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)

    out = kernel.message(session.id, "what are the sources?")

    assert _text(out) == answer
    assert len(llm.calls) == 1
    kernel.shutdown()


def test_unfixed_answer_is_returned_instead_of_apology(tmp_path: Path) -> None:
    text = 'Use {"tool": "search", "params": {}} like this to search.'
    llm = ScriptedLLM(text)
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)

    out = kernel.message(session.id, "how do I search?")

    assert _text(out) == text
    assert len(llm.calls) == 3  # original + 2 reformulation attempts
    kernel.shutdown()


def test_raw_tool_fence_still_gets_the_fallback(tmp_path: Path) -> None:
    llm = ScriptedLLM("```tool_call\n{bad json}\n```")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)

    out = kernel.message(session.id, "hi")

    assert _text(out) == fallback_text()
    kernel.shutdown()
