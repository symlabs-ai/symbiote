"""SymGatewayLLMAdapter against a fake gateway (httpx.MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from symbiote.adapters.llm.symgateway import (
    PROJECT_HEADER,
    SESSION_HEADER,
    SymGatewayLLMAdapter,
)
from symbiote.core.exceptions import LLMError
from symbiote.environment.descriptors import LLMResponse


class FakeGateway:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sessions = 0
        self.reject_session: str | None = None
        self.reply: dict = {"role": "assistant", "content": "olá"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != "Bearer sk-test":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [
                {"id": "gpt-x", "service_type": "llm"},
                {"id": "whisper", "service_type": "transcription"},
                {"id": "claude-y"},
            ]})
        if path == "/_api/work-sessions":
            self.sessions += 1
            return httpx.Response(201, json={"uid": f"ws-{self.sessions}"})
        if path == "/v1/chat/completions":
            if request.headers.get(SESSION_HEADER) == self.reject_session:
                return httpx.Response(404, json={"detail": "session not found"})
            return httpx.Response(200, json={
                "id": "cmpl-1",
                "model": "gpt-x",
                "choices": [{"message": self.reply, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            })
        return httpx.Response(404)

    def bodies(self, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


def make(gw: FakeGateway, **kw) -> SymGatewayLLMAdapter:
    client = httpx.Client(transport=httpx.MockTransport(gw))
    return SymGatewayLLMAdapter("sk-test", kw.pop("model", "gpt-x"), client=client, **kw)


def test_lists_only_llm_models() -> None:
    assert make(FakeGateway()).list_models() == ["gpt-x", "claude-y"]


def test_session_is_created_once_and_sent_on_every_call() -> None:
    gw = FakeGateway()
    llm = make(gw)
    assert llm.complete([{"role": "user", "content": "oi"}]) == "olá"
    llm.complete([{"role": "user", "content": "de novo"}])
    assert gw.sessions == 1
    calls = [r for r in gw.requests if r.url.path == "/v1/chat/completions"]
    assert [r.headers[SESSION_HEADER] for r in calls] == ["ws-1", "ws-1"]


def test_session_key_maps_conversations_to_work_sessions() -> None:
    gw = FakeGateway()
    current = {"key": "conv-a"}
    llm = make(gw, session_key=lambda: current["key"], session_title="Demo")
    llm.complete([{"role": "user", "content": "a"}])
    current["key"] = "conv-b"
    llm.complete([{"role": "user", "content": "b"}])
    current["key"] = "conv-a"
    llm.complete([{"role": "user", "content": "a2"}])
    assert llm.work_sessions == {"conv-a": "ws-1", "conv-b": "ws-2"}
    assert gw.bodies("/_api/work-sessions")[0] == {"title": "Demo · conv-a"}


def test_with_model_shares_work_sessions() -> None:
    gw = FakeGateway()
    main = make(gw)
    aux = main.with_model("claude-y")
    main.complete([{"role": "user", "content": "a"}])
    aux.complete([{"role": "user", "content": "b"}])
    assert gw.sessions == 1
    assert [b["model"] for b in gw.bodies("/v1/chat/completions")] == ["gpt-x", "claude-y"]


def test_stale_session_is_replaced_once() -> None:
    gw = FakeGateway()
    llm = make(gw)
    llm.complete([{"role": "user", "content": "a"}])
    gw.reject_session = "ws-1"
    assert llm.complete([{"role": "user", "content": "b"}]) == "olá"
    assert llm.work_sessions == {"__default__": "ws-2"}


def test_project_header_only_when_given() -> None:
    gw = FakeGateway()
    make(gw).list_models()
    make(gw, project_slug="acme").list_models()
    assert PROJECT_HEADER.lower() not in gw.requests[0].headers
    assert gw.requests[1].headers[PROJECT_HEADER] == "acme"


def test_body_carries_tools_config_and_clean_messages() -> None:
    gw = FakeGateway()
    llm = make(gw)
    tools = [{"type": "function", "function": {"name": "agora", "parameters": {}}}]
    llm.complete(
        [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": None, "reasoning_content": "secret",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "agora", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "agora", "content": "{}"},
        ],
        config={"max_tokens": 50, "temperature": 0.2, "unknown": 1},
        tools=tools,
    )
    body = gw.bodies("/v1/chat/completions")[0]
    assert body["model"] == "gpt-x"
    assert body["tools"] == tools
    assert body["max_tokens"] == 50 and body["temperature"] == 0.2
    assert "unknown" not in body
    assistant = body["messages"][1]
    assert assistant["content"] == "" and "reasoning_content" not in assistant
    assert body["messages"][2]["tool_call_id"] == "c1"


def test_native_tool_calls_come_back_as_llm_response() -> None:
    gw = FakeGateway()
    gw.reply = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c9", "type": "function",
         "function": {"name": "calcular", "arguments": "{\"expr\": \"2+2\"}"}},
    ]}
    result = make(gw).complete([{"role": "user", "content": "2+2?"}], tools=[{}])
    assert isinstance(result, LLMResponse)
    assert result.tool_calls[0].tool_id == "calcular"
    assert result.tool_calls[0].params == {"expr": "2+2"}
    assert result.usage is not None and result.usage.total_tokens == 5
    assert result.metadata["work_session"] == "ws-1"


def test_http_errors_become_llm_errors() -> None:
    gw = FakeGateway()
    llm = SymGatewayLLMAdapter(
        "sk-wrong", "gpt-x", client=httpx.Client(transport=httpx.MockTransport(gw))
    )
    with pytest.raises(LLMError) as info:
        llm.list_models()
    assert info.value.status_code == 401
    assert info.value.retryable is False
    assert "bad key" in str(info.value)


def test_requires_key_and_model() -> None:
    with pytest.raises(LLMError):
        SymGatewayLLMAdapter("", "m")
    with pytest.raises(LLMError):
        SymGatewayLLMAdapter("k", "")
