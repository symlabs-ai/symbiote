"""Streaming in ``SymGatewayLLMAdapter`` (B-111, SymCoder request).

Built with ``streaming=True`` the adapter asks the gateway for an event
stream, in ``complete()`` and in ``stream()``, and reads it into the shape of
a reply that was not streamed. Without the option nothing changes: the same
body, one whole reply, and no ``stream()`` for the kernel to call.

The gateway here is fake (``httpx.MockTransport``): no model is called.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from symbiote.adapters.llm.symgateway import SESSION_HEADER, SymGatewayLLMAdapter
from symbiote.core.exceptions import LLMError, is_transient_llm_error
from symbiote.core.llm_usage import LLMCallRecord, MeteredLLM
from symbiote.environment.descriptors import LLMResponse
from tests.support import make_kernel

MESSAGES = [{"role": "user", "content": "oi"}]
USAGE = {"input_tokens": 2400, "output_tokens": 50, "cached_input_tokens": 2000}


def text(piece: str, **more) -> dict:
    return {"id": "cmpl-1", "model": "gpt-x",
            "choices": [{"index": 0, "delta": {"content": piece}, **more}]}


def tool(**part) -> dict:
    return {"id": "cmpl-1", "model": "gpt-x",
            "choices": [{"index": 0, "delta": {"tool_calls": [part]}}]}


def finish(reason: str = "stop", usage: dict | None = None) -> list[dict]:
    """The chunk that ends the reply and the one that carries its usage."""
    return [
        {"id": "cmpl-1", "model": "gpt-x",
         "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
        {"id": "cmpl-1", "model": "gpt-x", "choices": [], "usage": usage or USAGE},
    ]


def sse(*chunks: dict | str, done: bool = True) -> bytes:
    lines = [c if isinstance(c, str) else f"data: {json.dumps(c)}" for c in chunks]
    if done:
        lines.append("data: [DONE]")
    return "".join(line + "\n\n" for line in lines).encode()


HELLO = sse(text("ol"), text("á"), *finish())


class FakeGateway:
    def __init__(self, stream: bytes | Iterator[bytes] = HELLO) -> None:
        self.requests: list[httpx.Request] = []
        self.sessions = 0
        self.reject_session: str | None = None
        self.stream = stream

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/_api/work-sessions":
            self.sessions += 1
            return httpx.Response(201, json={"uid": f"ws-{self.sessions}"})
        if path == "/v1/chat/completions":
            if request.headers.get(SESSION_HEADER) == self.reject_session:
                return httpx.Response(404, json={"detail": "session not found"})
            if not json.loads(request.content).get("stream"):
                return httpx.Response(200, json={
                    "id": "cmpl-1", "model": "gpt-x",
                    "choices": [{"message": {"role": "assistant", "content": "olá"},
                                 "finish_reason": "stop"}],
                    "usage": USAGE,
                })
            return httpx.Response(
                200, content=self.stream, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(404)

    def calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/v1/chat/completions"]

    def body(self, index: int = -1) -> dict:
        return json.loads(self.calls()[index].content)


def make(gw, **kw) -> SymGatewayLLMAdapter:
    client = httpx.Client(transport=httpx.MockTransport(gw))
    return SymGatewayLLMAdapter("sk-test", "gpt-x", client=client, **kw)


def streaming(stream: bytes | Iterator[bytes] = HELLO, **kw) -> SymGatewayLLMAdapter:
    return make(FakeGateway(stream), streaming=True, **kw)


class TestWithoutTheOption:
    def test_the_body_and_the_reply_are_the_ones_of_before(self) -> None:
        gw = FakeGateway()
        ticks: list[int] = []
        llm = make(gw, on_activity=lambda: ticks.append(1))

        assert llm.complete(MESSAGES) == "olá"

        assert gw.body() == {"model": "gpt-x", "messages": MESSAGES}
        assert ticks == []

    def test_the_adapter_has_no_stream_so_the_kernel_calls_complete(
        self, tmp_path: Path
    ) -> None:
        gw = FakeGateway()
        llm = make(gw)
        assert not hasattr(llm, "stream")
        with pytest.raises(AttributeError, match="streaming=True"):
            llm.stream  # noqa: B018

        tokens: list[str] = []
        kernel = make_kernel(tmp_path, llm)
        try:
            symbiote = kernel.create_symbiote(name="a", role="assistant")
            session = kernel.start_session(symbiote.id)
            asyncio.run(kernel.message_async(session.id, "oi", on_token=tokens.append))
        finally:
            kernel.shutdown()

        assert tokens == ["olá"]
        assert "stream" not in gw.body()


class TestComplete:
    def test_asks_for_a_stream_with_its_usage(self) -> None:
        gw = FakeGateway()

        assert make(gw, streaming=True).complete(MESSAGES, config={"max_tokens": 9}) == "olá"

        assert gw.body() == {
            "model": "gpt-x", "messages": MESSAGES, "max_tokens": 9,
            "stream": True, "stream_options": {"include_usage": True},
        }

    def test_plain_text_reports_the_usage_of_the_last_chunk(self) -> None:
        records: list[LLMCallRecord] = []

        MeteredLLM(streaming(), records.append).complete(MESSAGES)

        (record,) = records
        assert (record.provider, record.model) == ("symgateway", "gpt-x")
        assert (record.prompt_tokens, record.completion_tokens) == (2400, 50)
        assert record.cache_read_tokens == 2000

    def test_tool_calls_come_back_as_llm_response_with_usage(self) -> None:
        llm = streaming(sse(
            text("vou ver"),
            tool(index=0, id="c1", function={"name": "ler", "arguments": ""}),
            tool(index=0, function={"arguments": "{\"path\": "}),
            tool(index=0, function={"name": "ignored", "arguments": "\"a.py\"}"}),
            tool(index=1, id="c2", function={"name": "listar", "arguments": "{}"}),
            *finish("tool_calls"),
        ))

        result = llm.complete(MESSAGES, tools=[{}])

        assert isinstance(result, LLMResponse)
        assert result.content == "vou ver"
        assert [(c.call_id, c.tool_id, c.params) for c in result.tool_calls] == [
            ("c1", "ler", {"path": "a.py"}), ("c2", "listar", {}),
        ]
        assert result.finish_reason == "tool_calls"
        assert result.usage is not None and result.usage.prompt_tokens == 2400
        assert result.metadata["work_session"] == "ws-1"

    def test_tool_call_parts_without_an_index(self) -> None:
        """A part with an id opens a call; one without continues the latest."""
        llm = streaming(sse(
            tool(id="c1", function={"name": "ler", "arguments": "{\"n\""}),
            tool(function={"arguments": ": 1}"}),
            tool(id="c2", function={"name": "listar", "arguments": "{}"}),
            *finish("tool_calls"),
        ))

        result = llm.complete(MESSAGES, tools=[{}])

        assert isinstance(result, LLMResponse)
        assert [(c.call_id, c.tool_id, c.params) for c in result.tool_calls] == [
            ("c1", "ler", {"n": 1}), ("c2", "listar", {}),
        ]

    def test_arguments_cut_short_are_reported_not_guessed(self) -> None:
        llm = streaming(sse(
            tool(index=0, id="c1", function={"name": "ler", "arguments": "{\"path\": \"a"}),
            *finish("length"),
        ))

        result = llm.complete(MESSAGES, tools=[{}])

        assert isinstance(result, LLMResponse)
        assert result.tool_calls[0].params == {}
        assert result.tool_calls[0].arguments_error is not None


class TestReadingTheStream:
    def test_only_data_lines_of_the_first_choice_count(self) -> None:
        other = {"choices": [{"index": 1, "delta": {"content": "outra"}}]}
        llm = streaming(sse(
            ": keep-alive", "event: message", "data:", text("ol"), other, text("á"),
            *finish(),
        ))

        assert llm.complete(MESSAGES) == "olá"

    def test_done_ends_the_stream_and_the_rest_is_not_read(self) -> None:
        llm = streaming(sse(text("olá")) + sse("data: not json", done=False))

        assert llm.complete(MESSAGES) == "olá"

    def test_a_finish_reason_is_enough_without_done(self) -> None:
        llm = streaming(sse(text("olá", finish_reason="stop"), done=False))

        assert llm.complete(MESSAGES) == "olá"

    @pytest.mark.parametrize(
        ("stream", "reason"),
        [
            (sse(text("ol"), done=False), "ended before the reply was complete"),
            (sse(text("ol"), "data: {not json"), "not JSON"),
            (sse(text("ol"), "data: []"), "not an object"),
            (sse(text("ol"), {"error": {"message": "upstream overloaded"}}),
             "upstream overloaded"),
            (sse({"error": "boom"}), "boom"),
        ],
    )
    def test_a_stream_that_is_not_a_whole_reply_can_be_asked_again(
        self, stream: bytes, reason: str
    ) -> None:
        with pytest.raises(LLMError, match=reason) as info:
            streaming(stream).complete(MESSAGES)

        assert info.value.retryable is True
        assert is_transient_llm_error(info.value)

    def test_on_activity_runs_for_every_line_received(self) -> None:
        ticks: list[int] = []
        stream = sse(": keep-alive", text("olá"), *finish())
        llm = streaming(stream, on_activity=lambda: ticks.append(1))

        llm.complete(MESSAGES)

        # Every line up to [DONE], blank ones included; nothing after it.
        assert len(ticks) == len(stream.decode().splitlines()) - 1


class TestFailuresBeforeAndDuringTheStream:
    @pytest.mark.parametrize(
        ("status", "retryable"), [(400, False), (408, True), (429, True), (502, True)]
    )
    def test_a_refused_request_keeps_its_status(self, status: int, retryable: bool) -> None:
        def gateway(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/_api/work-sessions":
                return httpx.Response(201, json={"uid": "ws-1"})
            return httpx.Response(status, json={"error": {"message": "no"}})

        with pytest.raises(LLMError, match="no") as info:
            make(gateway, streaming=True).complete(MESSAGES)

        assert info.value.status_code == status
        assert info.value.retryable is retryable

    def test_a_timeout_before_the_reply(self) -> None:
        def gateway(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/_api/work-sessions":
                return httpx.Response(201, json={"uid": "ws-1"})
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(LLMError, match="timeout") as info:
            make(gateway, streaming=True).complete(MESSAGES)

        assert info.value.retryable is True

    @pytest.mark.parametrize(
        ("failure", "message"),
        [(httpx.ReadTimeout("silence"), "timeout"), (httpx.ReadError("reset"), "unreachable")],
    )
    def test_a_connection_lost_while_the_model_writes(
        self, failure: Exception, message: str
    ) -> None:
        def cut() -> Iterator[bytes]:
            yield sse(text("ol"), done=False)
            raise failure

        llm = streaming(cut())
        pieces: list[str] = []
        with pytest.raises(LLMError, match=message) as info:
            for item in llm.stream(MESSAGES):
                pieces.append(item)

        assert pieces == ["ol"]
        assert info.value.retryable is True

    def test_a_rejected_work_session_is_replaced_once(self) -> None:
        gw = FakeGateway()
        llm = make(gw, streaming=True)
        llm.complete(MESSAGES)
        gw.reject_session = "ws-1"

        assert llm.complete(MESSAGES) == "olá"

        assert llm.work_sessions == {"__default__": "ws-2"}
        assert [r.headers[SESSION_HEADER] for r in gw.calls()] == ["ws-1", "ws-1", "ws-2"]

    def test_a_session_rejected_twice_is_an_error(self) -> None:
        def gateway(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/_api/work-sessions":
                return httpx.Response(201, json={"uid": "ws"})
            return httpx.Response(410, json={"detail": "session closed"})

        with pytest.raises(LLMError) as info:
            make(gateway, streaming=True).complete(MESSAGES)

        assert info.value.status_code == 410


class TestSessionsAndRouting:
    def test_session_key_chooses_the_work_session(self) -> None:
        gw = FakeGateway()
        current = {"key": "conv-a"}
        llm = make(gw, streaming=True, session_key=lambda: current["key"])

        llm.complete(MESSAGES)
        current["key"] = "conv-b"
        list(llm.stream(MESSAGES))

        assert llm.work_sessions == {"conv-a": "ws-1", "conv-b": "ws-2"}
        assert [r.headers[SESSION_HEADER] for r in gw.calls()] == ["ws-1", "ws-2"]

    def test_routing_comes_from_the_chunks_that_carry_it(self) -> None:
        seen: list[dict] = []
        block = {"request_id": "sr_1", "provider": "openai", "objective": "chat_fast"}
        llm = streaming(
            sse(text("olá"), *finish(), {"choices": [], "symgateway": block}),
            on_routed=seen.append,
        )

        llm.complete(MESSAGES)

        assert seen == [llm.last_routing] == [{
            "model": "gpt-x", "objective": "chat_fast", "provider": "openai",
            "request_id": "sr_1", "work_session": "ws-1",
        }]

    def test_routing_without_the_gateway_block_keeps_what_is_known(self) -> None:
        llm = streaming()

        llm.complete(MESSAGES)

        assert llm.last_routing == {
            "model": "gpt-x", "objective": None, "provider": None,
            "request_id": None, "work_session": "ws-1",
        }


class TestStream:
    def test_text_arrives_in_the_pieces_the_gateway_sent(self) -> None:
        records: list[LLMCallRecord] = []
        seen: list[dict] = []
        llm = streaming(on_routed=seen.append)

        assert list(MeteredLLM(llm, records.append).stream(MESSAGES)) == ["ol", "á"]

        (record,) = records
        assert record.success is True
        assert (record.model, record.prompt_tokens, record.completion_tokens) == (
            "gpt-x", 2400, 50,
        )
        assert [r["work_session"] for r in seen] == ["ws-1"]

    def test_tool_calls_end_the_stream_as_one_llm_response(self) -> None:
        llm = streaming(sse(
            text("vou "), text("ver"),
            tool(index=0, id="c1", function={"name": "ler", "arguments": "{}"}),
            *finish("tool_calls"),
        ))

        *pieces, last = llm.stream(MESSAGES, tools=[{}])

        assert pieces == ["vou ", "ver"]
        assert isinstance(last, LLMResponse)
        assert last.content == "vou ver"
        assert last.tool_calls[0].tool_id == "ler"
        assert last.usage is not None and last.usage.completion_tokens == 50

    def test_with_model_streams_too_and_shares_the_session(self) -> None:
        gw = FakeGateway()
        aux = make(gw, streaming=True).with_model("claude-y")

        assert list(aux.stream(MESSAGES)) == ["ol", "á"]
        assert (gw.body()["model"], gw.body()["stream"]) == ("claude-y", True)

    def test_a_host_subclass_wraps_both_calls(self) -> None:
        """What a host is left with: its own ceiling on a call, nothing of
        the wire."""

        class Bounded(SymGatewayLLMAdapter):
            def complete(self, messages, config=None, tools=None):
                return super().complete(messages, {**(config or {}), "max_tokens": 8}, tools)

            def stream(self, messages, config=None, tools=None):
                return super().stream(messages, {**(config or {}), "max_tokens": 8}, tools)

        gw = FakeGateway()
        llm = Bounded(
            "sk-test", "gpt-x", streaming=True,
            client=httpx.Client(transport=httpx.MockTransport(gw)),
        )

        assert llm.complete(MESSAGES) == "olá"
        assert list(llm.stream(MESSAGES)) == ["ol", "á"]
        assert [gw.body(0)["max_tokens"], gw.body(1)["max_tokens"]] == [8, 8]

    def test_a_host_can_read_the_pieces_inside_its_own_complete(self) -> None:
        """A host that shows a reply it asked whole only once it is known to
        be good reads ``stream()`` in its ``complete()``; the usage of the
        call is reported all the same."""

        class Collecting(SymGatewayLLMAdapter):
            pieces: list[str] = []

            def complete(self, messages, config=None, tools=None):
                items = list(super().stream(messages, config, tools))
                self.pieces = [item for item in items if isinstance(item, str)]
                return "".join(self.pieces)

        llm = Collecting(
            "sk-test", "gpt-x", streaming=True,
            client=httpx.Client(transport=httpx.MockTransport(FakeGateway())),
        )
        records: list[LLMCallRecord] = []

        assert MeteredLLM(llm, records.append).complete(MESSAGES) == "olá"

        assert llm.pieces == ["ol", "á"]
        assert (records[0].prompt_tokens, records[0].completion_tokens) == (2400, 50)

    def test_the_kernel_hands_the_pieces_to_on_token(self, tmp_path: Path) -> None:
        gw = FakeGateway()
        tokens: list[str] = []
        kernel = make_kernel(tmp_path, make(gw, streaming=True))
        try:
            symbiote = kernel.create_symbiote(name="a", role="assistant")
            session = kernel.start_session(symbiote.id)
            answer = asyncio.run(
                kernel.message_async(session.id, "oi", on_token=tokens.append)
            )
            usage = kernel.inspector.usage_totals(session_id=session.id)
        finally:
            kernel.shutdown()

        assert answer == "olá"
        assert tokens == ["ol", "á"]
        assert gw.body()["stream"] is True
        assert (usage["llm_calls"], usage["prompt_tokens"]) == (1, 2400)
