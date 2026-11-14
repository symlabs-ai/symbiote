"""Live token streaming (B-73): text reaches on_token as the model writes it."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from symbiote.core.context import AssembledContext
from symbiote.environment.descriptors import LLMResponse, ToolCallResult
from symbiote.runners.chat import ChatRunner
from symbiote.runners.long_run import LongRunRunner
from symbiote.runners.token_stream import MarkupFilter, TokenSink

TOOL_FENCE = '```tool_call\n{"tool": "search", "params": {"q": "x"}}\n```'


def _filtered(chunks: list[str]) -> tuple[list[str], bool]:
    out: list[str] = []
    f = MarkupFilter(out.append)
    for chunk in chunks:
        f.feed(chunk)
    f.finish()
    return out, f.blocked


# ── MarkupFilter ─────────────────────────────────────────────────────────────


class TestMarkupFilter:
    def test_plain_text_goes_out_chunk_by_chunk(self) -> None:
        assert _filtered(["Olá, ", "tudo ", "bem?"]) == (["Olá, ", "tudo ", "bem?"], False)

    def test_tool_fence_split_across_chunks_is_dropped(self) -> None:
        out, blocked = _filtered(["Vou verificar.\n\n``", "`tool", "_call\n{\"tool\": ", "\"search\"}\n```"])
        assert "".join(out) == "Vou verificar.\n\n"
        assert blocked

    def test_other_fences_are_released(self) -> None:
        out, blocked = _filtered(["Exemplo:\n```py", "thon\nprint(1)\n```\nFim."])
        assert "".join(out) == "Exemplo:\n```python\nprint(1)\n```\nFim."
        assert not blocked

    def test_inline_code_is_released(self) -> None:
        assert "".join(_filtered(["Use `ls` ", "aqui."])[0]) == "Use `ls` aqui."

    def test_bare_tool_json_at_line_start_is_dropped(self) -> None:
        out, blocked = _filtered(["Vou buscar.\n", '{"tool": "search", ', '"params": {}}'])
        assert "".join(out) == "Vou buscar.\n"
        assert blocked

    def test_braces_mid_line_are_not_held(self) -> None:
        out, _ = _filtered(["O conjunto {a, b}", " tem 2 itens."])
        assert out == ["O conjunto {a, b}", " tem 2 itens."]

    def test_other_json_is_released_at_the_end(self) -> None:
        out, blocked = _filtered(["Aqui:\n", '{"nome": "Ana"}'])
        assert "".join(out) == 'Aqui:\n{"nome": "Ana"}'
        assert not blocked


# ── TokenSink ────────────────────────────────────────────────────────────────


def test_live_release_sends_only_what_the_stream_did_not_show() -> None:
    sent: list[str] = []
    sink = TokenSink(sent.append, "live")
    feed = sink.begin_round()
    assert feed is not None
    feed("Olá ")
    feed("mundo")
    sink.release("Olá mundo!", ["Olá ", "mundo"])
    assert sent == ["Olá ", "mundo", "!"]


def test_live_release_of_a_replaced_text_starts_a_new_paragraph() -> None:
    sent: list[str] = []
    sink = TokenSink(sent.append, "live")
    sink.begin_round()("Vou criar:\n")  # type: ignore[misc]
    sink.release("Criei a tarefa.", None)
    assert "".join(sent) == "Vou criar:\n\n\nCriei a tarefa."


# ── ChatRunner ───────────────────────────────────────────────────────────────


class _StreamingLLM:
    """Streams one scripted reply per call; records when each chunk is yielded."""

    def __init__(self, replies: list[list[str]], log: list[str]) -> None:
        self._replies = list(replies)
        self._log = log

    def complete(self, messages, config=None, tools=None):
        return "".join(self._replies.pop(0))

    def stream(self, messages, config=None, tools=None) -> Iterator[str]:
        for chunk in self._replies.pop(0):
            self._log.append(f"llm:{chunk}")
            yield chunk


class _Gateway:
    def execute_tool_calls(self, *, symbiote_id, session_id, calls, timeout=None):
        return [ToolCallResult(tool_id=c.tool_id, success=True, output="ok") for c in calls]

    async def execute_tool_calls_async(self, **kwargs):
        return self.execute_tool_calls(**kwargs)

    def get_risk_level(self, tool_id: str) -> str:
        return "low"


def _context(tool_mode: str = "brief") -> AssembledContext:
    return AssembledContext(
        symbiote_id="sym", session_id="sess", user_input="Quantas tarefas?",
        available_tools=[{"tool_id": "search", "name": "Search", "description": "d", "parameters": {}}],
        tool_mode=tool_mode,
    )


REPLIES = [
    ["Vou ", "verificar.\n\n", TOOL_FENCE],
    ["Você tem ", "3 tarefas."],
]


def _run(mode: str) -> tuple[str, list[str]]:
    log: list[str] = []
    runner = ChatRunner(llm=_StreamingLLM(REPLIES, log), tool_gateway=_Gateway(), token_streaming=mode)
    result = runner.run(_context(), on_token=lambda t: log.append(f"tok:{t}"))
    assert result.success
    return result.output["text"], log


def test_live_mode_streams_every_round_as_it_arrives() -> None:
    text, log = _run("live")
    assert text == "Você tem 3 tarefas."
    assert log == [
        "llm:Vou ", "tok:Vou ",
        "llm:verificar.\n\n", "tok:verificar.\n\n",
        f"llm:{TOOL_FENCE}",                      # the tool call never reaches on_token
        "llm:Você tem ", "tok:\n\n", "tok:Você tem ",  # new round, new paragraph
        "llm:3 tarefas.", "tok:3 tarefas.",
    ]


def test_final_mode_is_unchanged() -> None:
    text, log = _run("final")
    assert text == "Você tem 3 tarefas."
    tokens = [e for e in log if e.startswith("tok:")]
    assert tokens == ["tok:Você tem ", "tok:3 tarefas."]
    assert log.index("tok:Você tem ") > log.index("llm:3 tarefas.")  # released after the call


def test_live_mode_in_run_async_keeps_order() -> None:
    log: list[str] = []
    runner = ChatRunner(llm=_StreamingLLM(REPLIES, log), tool_gateway=_Gateway(), token_streaming="live")

    async def go() -> list[str]:
        seen: list[str] = []
        await runner.run_async(_context(), on_token=seen.append)
        return seen

    assert "".join(asyncio.run(go())) == "Vou verificar.\n\n\n\nVocê tem 3 tarefas."


def test_live_mode_instant_does_not_repeat_the_narration() -> None:
    log: list[str] = []
    llm = _StreamingLLM([["Abrindo ", "o editor.\n", TOOL_FENCE]], log)
    runner = ChatRunner(llm=llm, tool_gateway=_Gateway(), token_streaming="live")
    seen: list[str] = []
    result = runner.run(_context("instant"), on_token=seen.append)
    assert result.output["text"].strip() == "Abrindo o editor."
    assert "".join(seen).strip() == "Abrindo o editor."


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="token_streaming"):
        ChatRunner(llm=MagicMock(), token_streaming="fast")
    with pytest.raises(ValueError, match="token_streaming"):
        LongRunRunner(llm=MagicMock()).set_token_streaming("fast")


def test_long_run_blocks_inherit_the_mode() -> None:
    runner = LongRunRunner(llm=MagicMock())
    runner.set_token_streaming("live")
    assert runner._token_streaming == "live"


# ── ForgeLLMAdapter(stream_tool_calls=True) ──────────────────────────────────


def test_forge_adapter_streams_text_and_collects_native_tool_calls() -> None:
    pytest.importorskip("forge_llm")
    from symbiote.adapters.llm.forge import ForgeLLMAdapter

    adapter = ForgeLLMAdapter.__new__(ForgeLLMAdapter)
    adapter._stream_tool_calls = True
    adapter._agent = MagicMock()
    adapter._agent.stream_chat.return_value = iter([
        SimpleNamespace(content="Vou ", tool_calls=None, finish_reason=None, usage=None),
        SimpleNamespace(content="buscar.", tool_calls=None, finish_reason=None, usage=None),
        SimpleNamespace(
            content="", finish_reason="tool_calls", usage={"input_tokens": 5, "output_tokens": 2},
            tool_calls=[{"id": "c1", "type": "function",
                         "function": {"name": "search", "arguments": '{"q": "x"}'}}],
        ),
    ])
    items = list(adapter.stream([{"role": "user", "content": "oi"}], tools=[{"type": "function"}]))

    assert items[:2] == ["Vou ", "buscar."]
    final = items[2]
    assert isinstance(final, LLMResponse)
    assert final.content == "Vou buscar."
    assert [(c.call_id, c.tool_id, c.params) for c in final.tool_calls] == [("c1", "search", {"q": "x"})]
    assert final.usage is not None and final.usage.total_tokens == 7
    assert adapter._agent.stream_chat.call_args.kwargs["auto_execute_tools"] is False


def test_forge_adapter_without_the_flag_still_falls_back_to_complete() -> None:
    pytest.importorskip("forge_llm")
    from symbiote.adapters.llm.forge import ForgeLLMAdapter

    adapter = ForgeLLMAdapter.__new__(ForgeLLMAdapter)
    adapter._stream_tool_calls = False
    adapter._agent = MagicMock()
    adapter.complete = MagicMock(return_value="tudo certo")  # type: ignore[method-assign]
    assert list(adapter.stream([], tools=[{"type": "function"}])) == ["tudo certo"]
    adapter._agent.stream_chat.assert_not_called()
