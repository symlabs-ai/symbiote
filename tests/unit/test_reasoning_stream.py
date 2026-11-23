"""The model's reasoning while it thinks (B-113, SymbiOS request).

An adapter built with ``stream_reasoning=True`` yields the reasoning the
provider streams as ``ReasoningDelta`` items, apart from the text. The kernel
hands them to ``on_reasoning(text, iteration)`` during every model call of a
turn and keeps them out of the answer, the history and the memory. Without
the option and the callback nothing changes: neither the body of a call nor
the items of a stream.

No model is called: the gateway is ``httpx.MockTransport`` and the forge_llm
agent a mock.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from symbiote.adapters.llm.symgateway import SymGatewayLLMAdapter
from symbiote.core.context import AssembledContext
from symbiote.core.exceptions import LLMError
from symbiote.core.llm_usage import LLMCallRecord, MeteredLLM
from symbiote.environment.descriptors import LLMResponse, ReasoningDelta, ToolCallResult
from symbiote.runners.chat import ChatRunner
from symbiote.runners.long_run import LongRunRunner
from tests.support import make_kernel

MESSAGES = [{"role": "user", "content": "oi"}]
TOOL_FENCE = '```tool_call\n{"tool": "search", "params": {"q": "x"}}\n```'


# ── SymGatewayLLMAdapter ─────────────────────────────────────────────────────


def chunk(**delta) -> dict:
    return {"id": "cmpl-1", "model": "gpt-x", "choices": [{"index": 0, "delta": delta}]}


def end(reason: str = "stop", usage: dict | None = None) -> list[dict]:
    return [
        {"id": "cmpl-1", "model": "gpt-x",
         "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
        {"id": "cmpl-1", "model": "gpt-x", "choices": [],
         "usage": usage or {"input_tokens": 10, "output_tokens": 40}},
    ]


def sse(*chunks: dict) -> bytes:
    lines = [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]
    return "".join(line + "\n\n" for line in lines).encode()


THINKING = sse(
    chunk(reasoning_content="Pesando "),
    # vLLM sends the same piece in both fields: read once.
    chunk(reasoning_content="as opções.", reasoning="as opções."),
    chunk(reasoning="Decidido."),
    chunk(content="ol"),
    chunk(content="á"),
    *end(),
)


class Gateway:
    def __init__(self, stream: bytes = THINKING) -> None:
        self.stream = stream
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_api/work-sessions":
            return httpx.Response(201, json={"uid": "ws-1"})
        self.bodies.append(json.loads(request.content))
        return httpx.Response(
            200, content=self.stream, headers={"content-type": "text/event-stream"}
        )


def gateway_adapter(gw: Gateway, **kw) -> SymGatewayLLMAdapter:
    client = httpx.Client(transport=httpx.MockTransport(gw))
    return SymGatewayLLMAdapter("sk-test", "gpt-x", client=client, streaming=True, **kw)


class TestSymGatewayAdapter:
    def test_without_the_option_the_stream_and_the_body_do_not_change(self) -> None:
        plain, asked = Gateway(), Gateway()
        items = list(gateway_adapter(plain).stream(MESSAGES))
        list(gateway_adapter(asked, stream_reasoning=True).stream(MESSAGES))

        assert items == ["ol", "á"]
        assert plain.bodies == asked.bodies

    def test_reasoning_comes_apart_and_before_the_text(self) -> None:
        items = list(gateway_adapter(Gateway(), stream_reasoning=True).stream(MESSAGES))

        assert items == [
            ReasoningDelta(text="Pesando "),
            ReasoningDelta(text="as opções."),
            ReasoningDelta(text="Decidido."),
            "ol",
            "á",
        ]

    def test_complete_returns_the_text_alone(self) -> None:
        assert gateway_adapter(Gateway(), stream_reasoning=True).complete(MESSAGES) == "olá"

    def test_tool_calls_keep_the_reasoning_out_of_the_reply(self) -> None:
        stream = sse(
            chunk(reasoning_content="Preciso buscar."),
            chunk(tool_calls=[{"index": 0, "id": "c1",
                               "function": {"name": "search", "arguments": '{"q": "x"}'}}]),
            *end("tool_calls", {"input_tokens": 10, "output_tokens": 40,
                                "completion_tokens_details": {"reasoning_tokens": 25}}),
        )
        items = list(gateway_adapter(Gateway(stream), stream_reasoning=True).stream(MESSAGES))

        assert items[0] == ReasoningDelta(text="Preciso buscar.")
        reply = items[1]
        assert isinstance(reply, LLMResponse)
        assert reply.content == ""
        assert reply.reasoning_content is None
        assert [c.tool_id for c in reply.tool_calls] == ["search"]
        assert reply.usage is not None and reply.usage.reasoning_tokens == 25

    def test_reasoning_tokens_of_a_streamed_text_reach_the_usage_record(self) -> None:
        stream = sse(
            chunk(reasoning_content="Hmm."),
            chunk(content="olá"),
            *end(usage={"input_tokens": 10, "output_tokens": 40,
                        "output_tokens_details": {"reasoning_tokens": 30}}),
        )
        llm = gateway_adapter(Gateway(stream), stream_reasoning=True)
        records: list[LLMCallRecord] = []

        items = list(MeteredLLM(llm, records.append).stream(MESSAGES))

        assert items[-1] == "olá"
        assert records[0].reasoning_tokens == 30

    def test_the_option_needs_streaming(self) -> None:
        with pytest.raises(LLMError, match="streaming=True"):
            SymGatewayLLMAdapter("sk-test", "gpt-x", stream_reasoning=True)

    def test_with_model_keeps_the_option(self) -> None:
        other = gateway_adapter(Gateway(), stream_reasoning=True).with_model("gpt-y")
        assert ReasoningDelta(text="Pesando ") in list(other.stream(MESSAGES))


STATE = {"v": 1, "protocol": "anthropic_messages", "blocks": [
    {"type": "thinking", "thinking": "Preciso buscar.", "signature": "sig"},
    {"type": "redacted_thinking", "data": "xyz"},
]}
CALL = {"index": 0, "id": "c1", "function": {"name": "search", "arguments": "{}"}}


class Sequence(Gateway):
    """One stream (or whole reply) per call, in order."""

    def __init__(self, *replies: bytes | dict) -> None:
        super().__init__()
        self.replies = list(replies)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_api/work-sessions":
            return httpx.Response(201, json={"uid": "ws-1"})
        self.bodies.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if isinstance(reply, dict):
            return httpx.Response(200, json=reply)
        return httpx.Response(200, content=reply, headers={"content-type": "text/event-stream"})


TOOL_ROUND = sse(
    chunk(reasoning_content="Preciso buscar."),
    chunk(tool_calls=[CALL]),
    {"id": "cmpl-1", "model": "gpt-x", "choices": [
        {"index": 0, "delta": {"reasoning_state": STATE}, "finish_reason": "tool_calls"}]},
    {"id": "cmpl-1", "model": "gpt-x", "choices": [], "usage": {"input_tokens": 1, "output_tokens": 1}},
)
ANSWER_ROUND = sse(chunk(content="Achei."), *end())


class TestGatewayReplayState:
    def test_the_terminal_chunk_state_comes_back_unchanged(self) -> None:
        items = list(gateway_adapter(Sequence(TOOL_ROUND)).stream(MESSAGES, tools=[{"type": "function"}]))

        reply = items[-1]
        assert isinstance(reply, LLMResponse)
        assert reply.reasoning_state == STATE
        assert reply.reasoning_content is None

    def test_a_whole_reply_carries_it_too(self) -> None:
        whole = {"id": "cmpl-1", "model": "gpt-x", "choices": [{"index": 0, "finish_reason": "tool_calls",
                 "message": {"role": "assistant", "content": "", "reasoning_state": STATE,
                             "tool_calls": [{"id": "c1", "type": "function",
                                             "function": {"name": "search", "arguments": "{}"}}]}}]}
        client = httpx.Client(transport=httpx.MockTransport(Sequence(whole)))
        llm = SymGatewayLLMAdapter("sk-test", "gpt-x", client=client)

        reply = llm.complete(MESSAGES, tools=[{"type": "function"}])

        assert isinstance(reply, LLMResponse) and reply.reasoning_state == STATE

    def test_the_state_goes_back_only_with_the_tool_calls(self) -> None:
        gw = Sequence(ANSWER_ROUND)
        called = {"role": "assistant", "content": "", "reasoning_content": "Preciso buscar.",
                  "reasoning_state": STATE,
                  "tool_calls": [{"id": "c1", "type": "function",
                                  "function": {"name": "search", "arguments": "{}"}}]}
        flattened = {"role": "assistant", "content": "[Called tool: search]\n{}",
                     "reasoning_state": STATE}

        list(gateway_adapter(gw).stream([*MESSAGES, called, flattened]))

        sent = gw.bodies[0]["messages"]
        assert sent[1]["reasoning_state"] == STATE
        assert "reasoning_content" not in sent[1]
        assert "reasoning_state" not in sent[2]

    def test_a_tool_loop_sends_the_state_back_in_the_next_round(self) -> None:
        gw = Sequence(TOOL_ROUND, ANSWER_ROUND)
        runner = ChatRunner(llm=gateway_adapter(gw, stream_reasoning=True),
                            tool_gateway=Tools(), native_tools=True)
        seen: list[tuple[str, int]] = []
        runner.set_progress_callbacks(on_reasoning=lambda text, it: seen.append((text, it)))

        result = runner.run(context())

        assert result.output["text"] == "Achei."
        assert seen == [("Preciso buscar.", 1)]
        assistant = next(m for m in gw.bodies[1]["messages"]
                         if m["role"] == "assistant" and m.get("tool_calls"))
        assert assistant["reasoning_state"] == STATE

    def test_without_state_the_next_round_is_the_same(self) -> None:
        plain_round = sse(chunk(tool_calls=[CALL]), *end("tool_calls"))
        gw = Sequence(plain_round, ANSWER_ROUND)
        runner = ChatRunner(llm=gateway_adapter(gw), tool_gateway=Tools(), native_tools=True)

        runner.run(context())

        assert all("reasoning_state" not in m for m in gw.bodies[1]["messages"])


# ── ForgeLLMAdapter ──────────────────────────────────────────────────────────


def forge_adapter(*, stream_reasoning: bool, stream_tool_calls: bool = False):
    pytest.importorskip("forge_llm")
    from symbiote.adapters.llm.forge import ForgeLLMAdapter

    adapter = ForgeLLMAdapter.__new__(ForgeLLMAdapter)
    adapter._stream_tool_calls = stream_tool_calls
    adapter._stream_reasoning = stream_reasoning
    adapter._agent = MagicMock()
    return adapter


def forge_chunks(*, tool_call: bool = False) -> list[SimpleNamespace]:
    def piece(content: str = "", **more) -> SimpleNamespace:
        fields = {"content": content, "tool_calls": None, "finish_reason": None, "usage": None}
        return SimpleNamespace(**{**fields, **more})

    last = piece(
        finish_reason="tool_calls" if tool_call else "stop",
        usage={"raw": {"input_tokens": 5, "output_tokens": 20,
                       "output_tokens_details": {"reasoning_tokens": 12}}},
        tool_calls=[{"id": "c1", "type": "function",
                     "function": {"name": "search", "arguments": "{}"}}] if tool_call else None,
    )
    return [
        piece(reasoning_content="Pensando."),
        piece(reasoning="Ainda pensando."),
        piece("Vou "),
        piece("buscar."),
        last,
    ]


class TestForgeAdapter:
    def test_plain_stream_yields_the_reasoning_apart(self) -> None:
        adapter = forge_adapter(stream_reasoning=True)
        adapter._agent.stream_chat.return_value = iter(forge_chunks())

        assert list(adapter.stream(MESSAGES)) == [
            ReasoningDelta(text="Pensando."),
            ReasoningDelta(text="Ainda pensando."),
            "Vou ",
            "buscar.",
        ]

    def test_without_the_option_the_stream_does_not_change(self) -> None:
        adapter = forge_adapter(stream_reasoning=False)
        adapter._agent.stream_chat.return_value = iter(forge_chunks())

        assert list(adapter.stream(MESSAGES)) == ["Vou ", "buscar."]

    def test_stream_with_tools_yields_the_reasoning_and_its_usage(self) -> None:
        adapter = forge_adapter(stream_reasoning=True, stream_tool_calls=True)
        adapter._agent.stream_chat.return_value = iter(forge_chunks(tool_call=True))

        items = list(adapter.stream(MESSAGES, tools=[{"type": "function"}]))

        assert items[:4] == [
            ReasoningDelta(text="Pensando."),
            ReasoningDelta(text="Ainda pensando."),
            "Vou ",
            "buscar.",
        ]
        reply = items[4]
        assert isinstance(reply, LLMResponse)
        assert reply.content == "Vou buscar."
        assert reply.usage is not None and reply.usage.reasoning_tokens == 12

    def test_the_fallback_to_complete_yields_the_whole_reasoning_first(self) -> None:
        adapter = forge_adapter(stream_reasoning=True)
        reply = LLMResponse(content="Vou buscar.", reasoning_content="Pensei tudo.")
        adapter.complete = MagicMock(return_value=reply)  # type: ignore[method-assign]

        items = list(adapter.stream(MESSAGES, tools=[{"type": "function"}]))

        assert items == [ReasoningDelta(text="Pensei tudo."), "Vou buscar.", reply]

    def test_the_fallback_without_the_option_does_not_change(self) -> None:
        adapter = forge_adapter(stream_reasoning=False)
        reply = LLMResponse(content="Vou buscar.", reasoning_content="Pensei tudo.")
        adapter.complete = MagicMock(return_value=reply)  # type: ignore[method-assign]

        assert list(adapter.stream(MESSAGES, tools=[{"type": "function"}])) == [
            "Vou buscar.", reply,
        ]


class TestForgeProviderOptions:
    def test_the_options_go_in_the_extra_of_every_call(self) -> None:
        adapter = forge_adapter(stream_reasoning=False)
        adapter.provider_options = {"thinking": {"type": "enabled", "budget_tokens": 2000}}
        adapter._agent.chat.return_value = SimpleNamespace(content="ok", tool_calls=None)

        adapter.complete(MESSAGES, config={"provider_options": {"x": 1}, "max_tokens": 9000})

        cfg = adapter._agent.chat.call_args.kwargs["config"]
        assert cfg.extra == {"thinking": {"type": "enabled", "budget_tokens": 2000}, "x": 1}
        assert cfg.max_tokens == 9000

    def test_without_options_the_call_does_not_change(self) -> None:
        adapter = forge_adapter(stream_reasoning=False)
        adapter._agent.chat.return_value = SimpleNamespace(content="ok", tool_calls=None)

        adapter.complete(MESSAGES)

        assert "config" not in adapter._agent.chat.call_args.kwargs

    def test_the_tool_stream_keeps_what_the_provider_needs_back(self) -> None:
        adapter = forge_adapter(stream_reasoning=False, stream_tool_calls=True)
        chunks = forge_chunks(tool_call=True)
        chunks[-1].reasoning_state = {"anthropic_thinking": [{"type": "thinking"}]}
        adapter._agent.stream_chat.return_value = iter(chunks)

        items = list(adapter.stream(MESSAGES, tools=[{"type": "function"}]))

        assert not any(isinstance(i, ReasoningDelta) for i in items)
        reply = items[-1]
        assert isinstance(reply, LLMResponse)
        assert reply.reasoning_content == "Pensando.Ainda pensando."
        assert reply.reasoning_state == {"anthropic_thinking": [{"type": "thinking"}]}


def test_anthropic_thinking_through_forge_llm_end_to_end() -> None:
    """forge_llm and the pinned anthropic SDK themselves, over a fake HTTP
    transport: the request asks for thinking, the stream yields it, and the
    next round sends the blocks back before the tool call."""
    pytest.importorskip("forge_llm")
    from anthropic import Anthropic

    from symbiote.adapters.llm.forge import ForgeLLMAdapter

    thinking = {"type": "enabled", "budget_tokens": 8000}
    message = {"id": "m", "type": "message", "role": "assistant", "model": "claude-x",
               "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 5, "output_tokens": 1}}
    events = [
        {"type": "message_start", "message": {**message, "content": []}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "Preciso buscar."}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "tu_1", "name": "search", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 9}},
        {"type": "message_stop"},
    ]
    stream_body = "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
    ).encode()
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, content=stream_body, headers={"content-type": "text/event-stream"}
        )

    adapter = ForgeLLMAdapter(
        provider="anthropic", model="claude-x", api_key="k",
        stream_tool_calls=True, stream_reasoning=True,
        provider_options={"thinking": thinking},
    )
    adapter._agent._get_provider()._client = Anthropic(
        api_key="k", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    tools = [{"type": "function", "function": {
        "name": "search", "description": "d", "parameters": {"type": "object"}}}]

    items = list(adapter.stream(MESSAGES, tools=tools))

    assert bodies[0]["thinking"] == thinking and bodies[0]["max_tokens"] > 8000
    assert items[0] == ReasoningDelta(text="Preciso buscar.")
    reply = items[-1]
    assert isinstance(reply, LLMResponse)
    assert [c.tool_id for c in reply.tool_calls] == ["search"]
    assert reply.reasoning_state == {"anthropic_thinking": [
        {"type": "thinking", "thinking": "Preciso buscar.", "signature": "sig"},
    ]}

    replay = [
        *MESSAGES,
        {"role": "assistant", "content": "", "reasoning_state": reply.reasoning_state,
         "tool_calls": [{"id": "tu_1", "type": "function",
                         "function": {"name": "search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tu_1", "content": "ok"},
    ]
    list(adapter.stream(replay, tools=tools))

    assert bodies[1]["messages"][1]["content"] == [
        {"type": "thinking", "thinking": "Preciso buscar.", "signature": "sig"},
        {"type": "tool_use", "id": "tu_1", "name": "search", "input": {}},
    ]


# ── ChatRunner ───────────────────────────────────────────────────────────────


class ThinkingLLM:
    """Streams one scripted reply per call; logs each item as it is yielded."""

    def __init__(self, replies: list[list[str | ReasoningDelta]], log: list[str]) -> None:
        self._replies = list(replies)
        self.log = log
        self.completes = 0

    def complete(self, messages, config=None, tools=None):
        self.completes += 1
        reply = self._replies.pop(0)
        return "".join(i for i in reply if isinstance(i, str))

    def stream(self, messages, config=None, tools=None) -> Iterator[str | ReasoningDelta]:
        for item in self._replies.pop(0):
            self.log.append(f"llm:{item.text if isinstance(item, ReasoningDelta) else item}")
            yield item


class Tools:
    def execute_tool_calls(self, *, symbiote_id, session_id, calls, timeout=None):
        return [ToolCallResult(tool_id=c.tool_id, success=True, output="ok") for c in calls]

    async def execute_tool_calls_async(self, **kwargs):
        return self.execute_tool_calls(**kwargs)

    def get_risk_level(self, tool_id: str) -> str:
        return "low"


def context(tool_mode: str = "brief") -> AssembledContext:
    return AssembledContext(
        symbiote_id="sym", session_id="sess", user_input="Quantas tarefas?",
        available_tools=[{"tool_id": "search", "name": "Search", "description": "d", "parameters": {}}],
        tool_mode=tool_mode,
    )


def replies() -> list[list[str | ReasoningDelta]]:
    return [
        [ReasoningDelta(text="Preciso "), ReasoningDelta(text="buscar."),
         "Vou verificar.\n\n", TOOL_FENCE],
        [ReasoningDelta(text="Achei 3."), "Você tem ", "3 tarefas."],
    ]


def run_turn(*, on_reasoning, on_token=None, tool_mode="brief", mode="live"):
    log: list[str] = []
    llm = ThinkingLLM(replies(), log)
    runner = ChatRunner(llm=llm, tool_gateway=Tools(), token_streaming=mode)
    runner.set_progress_callbacks(on_reasoning=on_reasoning)
    result = runner.run(context(tool_mode), on_token=on_token)
    assert result.success
    return result, llm


class TestChatRunner:
    def test_each_call_hands_its_reasoning_before_its_first_token(self) -> None:
        log: list[str] = []
        result, _ = run_turn(
            on_reasoning=lambda text, it: log.append(f"think{it}:{text}"),
            on_token=lambda t: log.append(f"tok:{t}"),
        )

        assert result.output["text"] == "Você tem 3 tarefas."
        assert log == [
            "think1:Preciso ", "think1:buscar.",
            "tok:Vou verificar.\n\n",
            "think2:Achei 3.",
            "tok:\n\n", "tok:Você tem ", "tok:3 tarefas.",
        ]

    def test_the_answer_is_the_same_with_and_without_the_callback(self) -> None:
        for mode in ("live", "final"):
            tokens_on: list[str] = []
            tokens_off: list[str] = []
            with_it, _ = run_turn(on_reasoning=lambda *_: None, on_token=tokens_on.append, mode=mode)
            without, _ = run_turn(on_reasoning=None, on_token=tokens_off.append, mode=mode)

            assert with_it.output == without.output
            assert tokens_on == tokens_off

    def test_the_callback_alone_makes_the_calls_stream(self) -> None:
        seen: list[tuple[str, int]] = []
        result, llm = run_turn(on_reasoning=lambda text, it: seen.append((text, it)))

        assert result.output["text"] == "Você tem 3 tarefas."
        assert llm.completes == 0
        assert seen == [("Preciso ", 1), ("buscar.", 1), ("Achei 3.", 2)]

    def test_without_the_callback_nothing_changes(self) -> None:
        result, llm = run_turn(on_reasoning=None)

        assert result.output["text"] == "Você tem 3 tarefas."
        assert llm.completes == 2  # no on_token, no callback: complete() as before

    def test_instant_mode_numbers_its_call(self) -> None:
        seen: list[tuple[str, int]] = []
        log: list[str] = []
        llm = ThinkingLLM([[ReasoningDelta(text="Direto."), "Pronto, feito."]], log)
        runner = ChatRunner(llm=llm, tool_gateway=Tools())
        runner.set_progress_callbacks(on_reasoning=lambda text, it: seen.append((text, it)))

        result = runner.run(context("instant"))

        assert result.output == "Pronto, feito."
        assert seen == [("Direto.", 1)]

    def test_run_async_hands_the_reasoning_too(self) -> None:
        seen: list[tuple[str, int]] = []
        log: list[str] = []
        runner = ChatRunner(llm=ThinkingLLM(replies(), log), tool_gateway=Tools())
        runner.set_progress_callbacks(on_reasoning=lambda text, it: seen.append((text, it)))

        result = asyncio.run(runner.run_async(context()))

        assert result.output["text"] == "Você tem 3 tarefas."
        assert seen == [("Preciso ", 1), ("buscar.", 1), ("Achei 3.", 2)]


class TestLongRun:
    def test_each_block_hands_its_reasoning(self) -> None:
        seen: list[tuple[str, int]] = []
        log: list[str] = []
        runner = LongRunRunner(llm=ThinkingLLM(replies(), log), tool_gateway=Tools())
        runner.set_progress_callbacks(on_reasoning=lambda text, it: seen.append((text, it)))

        block = runner._execute_block(context(), "Conte as tarefas.", 0, "contar")

        assert block.output == "Você tem 3 tarefas."
        assert seen == [("Preciso ", 1), ("buscar.", 1), ("Achei 3.", 2)]


# ── The kernel ───────────────────────────────────────────────────────────────


class TestKernel:
    def turn(self, tmp_path: Path, *, on_reasoning) -> tuple[str, list[str], list[dict]]:
        tokens: list[str] = []
        kernel = make_kernel(tmp_path, gateway_adapter(Gateway(), stream_reasoning=True))
        try:
            kernel.set_progress_callbacks(on_reasoning=on_reasoning)
            symbiote = kernel.create_symbiote(name="a", role="assistant")
            session = kernel.start_session(symbiote.id)
            answer = asyncio.run(
                kernel.message_async(session.id, "oi", on_token=tokens.append)
            )
            history = [
                {"role": m["role"], "content": m["content"]}
                for m in kernel.inspector.list_messages(session.id)
            ]
        finally:
            kernel.shutdown()
        return answer, tokens, history

    def test_the_host_gets_the_reasoning_and_the_answer_stays_the_same(
        self, tmp_path: Path
    ) -> None:
        seen: list[tuple[str, int]] = []
        answer, tokens, history = self.turn(
            tmp_path / "on", on_reasoning=lambda text, it: seen.append((text, it))
        )
        plain_answer, plain_tokens, plain_history = self.turn(
            tmp_path / "off", on_reasoning=None
        )

        assert seen == [("Pesando ", 1), ("as opções.", 1), ("Decidido.", 1)]
        assert (answer, tokens, history) == (plain_answer, plain_tokens, plain_history)
        assert answer == "olá"
        assert all("Pesando" not in m["content"] for m in history)
