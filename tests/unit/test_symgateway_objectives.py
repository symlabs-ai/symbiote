"""Objectives in ``SymGatewayLLMAdapter`` (B-93).

The gateway routes a call by a named model or by an Objective (it then
chooses the model) and says which Objective routed it. All of it stays inside
this adapter: ``LLMPort`` and the kernel know nothing about Objectives.
"""

from __future__ import annotations

import json

import httpx
import pytest

from symbiote.adapters.llm.symgateway import SymGatewayLLMAdapter
from symbiote.core.exceptions import LLMError
from symbiote.environment.descriptors import LLMResponse


class FakeGateway:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reply: dict = {"role": "assistant", "content": "olá"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/_api/work-sessions":
            return httpx.Response(201, json={"uid": "ws-1"})
        if path == "/_api/objectives":
            return httpx.Response(200, json=[
                {"name": "chat_fast", "service_type": "llm", "enabled": True,
                 "routing_status": "active"},
                {"name": "summarize", "service_type": "llm", "enabled": True,
                 "routing_status": "active"},
                {"name": "draft", "service_type": "llm", "enabled": True,
                 "routing_status": "pending"},
                {"name": "off", "service_type": "llm", "enabled": False,
                 "routing_status": "disabled"},
                {"name": "web_seek", "service_type": "external_api", "enabled": True,
                 "routing_status": "active"},
            ])
        if path == "/v1/chat/completions":
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "cmpl-1",
                "model": "gpt-routed" if "objective" in body else body["model"],
                "choices": [{"message": self.reply, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                "symgateway": {
                    "request_id": "sr_1", "provider": "openai",
                    "objective": body.get("objective"),
                },
            })
        return httpx.Response(404)

    def body(self, index: int = -1) -> dict:
        calls = [r for r in self.requests if r.url.path == "/v1/chat/completions"]
        return json.loads(calls[index].content)


def make(gw: FakeGateway, *args, **kw) -> SymGatewayLLMAdapter:
    client = httpx.Client(transport=httpx.MockTransport(gw))
    return SymGatewayLLMAdapter("sk-test", *args, client=client, **kw)


MESSAGES = [{"role": "user", "content": "oi"}]


def test_lists_the_llm_objectives_that_can_route() -> None:
    gw = FakeGateway()

    assert make(gw, "gpt-x").list_objectives() == ["chat_fast", "summarize"]
    query = gw.requests[-1].url.params
    assert (query["service_type"], query["enabled"]) == ("llm", "true")


def test_an_objective_takes_the_place_of_the_model() -> None:
    gw = FakeGateway()
    llm = make(gw, objective="chat_fast")

    assert llm.complete(MESSAGES) == "olá"

    assert gw.body() == {"objective": "chat_fast", "messages": MESSAGES}
    assert llm.last_routing == {
        "model": "gpt-routed", "objective": "chat_fast", "provider": "openai",
        "request_id": "sr_1", "work_session": "ws-1",
    }


def test_a_model_is_still_sent_as_before() -> None:
    gw = FakeGateway()
    llm = make(gw, "gpt-x")

    llm.complete(MESSAGES)

    assert gw.body() == {"model": "gpt-x", "messages": MESSAGES}
    assert llm.last_routing["objective"] is None


def test_the_calls_own_config_wins_over_the_adapter() -> None:
    gw = FakeGateway()
    llm = make(gw, "gpt-x")

    llm.complete(MESSAGES, config={"objective": "summarize"})
    assert "model" not in gw.body() and gw.body()["objective"] == "summarize"

    by_objective = make(gw, objective="chat_fast")
    by_objective.complete(MESSAGES, config={"model": "gpt-y"})
    assert "objective" not in gw.body() and gw.body()["model"] == "gpt-y"


def test_with_objective_and_with_model_switch_the_routing() -> None:
    gw = FakeGateway()
    llm = make(gw, "gpt-x")

    llm.with_objective("summarize").complete(MESSAGES)
    assert gw.body()["objective"] == "summarize"

    llm.with_objective("summarize").with_model("gpt-y").complete(MESSAGES)
    assert gw.body() == {"model": "gpt-y", "messages": MESSAGES}
    assert llm.objective is None  # the original adapter is untouched


def test_on_routed_is_told_after_every_call() -> None:
    gw = FakeGateway()
    seen: list[dict] = []
    llm = make(gw, objective="chat_fast", on_routed=seen.append)

    llm.complete(MESSAGES)
    llm.complete(MESSAGES, config={"model": "gpt-y"})

    assert [(r["objective"], r["model"]) for r in seen] == [
        ("chat_fast", "gpt-routed"), (None, "gpt-y"),
    ]


def test_the_routed_objective_is_on_the_llm_response_too() -> None:
    gw = FakeGateway()
    gw.reply = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "agora", "arguments": "{}"}}
    ]}

    response = make(gw, objective="chat_fast").complete(MESSAGES)

    assert isinstance(response, LLMResponse)
    assert response.metadata["objective"] == "chat_fast"
    assert response.model_identity.model == "gpt-routed"


def test_a_model_or_an_objective_is_required() -> None:
    with pytest.raises(LLMError, match="model or an objective"):
        make(FakeGateway())
