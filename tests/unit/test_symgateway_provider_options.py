"""Provider options and the model listing of ``SymGatewayLLMAdapter``
(B-111, SymCoder request).

The gateway forwards ``provider_options`` to the provider behind it (prompt
cache, reasoning effort). The adapter carries them; which model takes which
option is the host's decision, made from ``models()``, which says the
provider that serves each one.
"""

from __future__ import annotations

import json

import httpx

from symbiote.adapters.llm.symgateway import SymGatewayLLMAdapter

MESSAGES = [{"role": "user", "content": "oi"}]
CACHE = {"cache_control": {"type": "ephemeral"}}
CATALOG = [
    {"id": "claude-y", "canonical_id": "anthropic/claude-y", "service_type": "llm",
     "provider": "anthropic"},
    {"id": "gpt-x", "service_type": "llm", "provider": "openai"},
    {"id": "whisper", "service_type": "transcription", "provider": "openai"},
    {"id": "legacy"},
    "not an entry",
]


class FakeGateway:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": CATALOG})
        if path == "/_api/work-sessions":
            return httpx.Response(201, json={"uid": "ws-1"})
        if path == "/v1/chat/completions":
            return httpx.Response(200, json={
                "model": "claude-y",
                "choices": [{"message": {"role": "assistant", "content": "olá"}}],
            })
        return httpx.Response(404)

    def body(self, index: int = -1) -> dict:
        calls = [r for r in self.requests if r.url.path == "/v1/chat/completions"]
        return json.loads(calls[index].content)


def make(gw: FakeGateway, model: str = "claude-y", **kw) -> SymGatewayLLMAdapter:
    client = httpx.Client(transport=httpx.MockTransport(gw))
    return SymGatewayLLMAdapter("sk-test", model, client=client, **kw)


def test_no_options_no_field() -> None:
    gw = FakeGateway()

    make(gw).complete(MESSAGES)

    assert gw.body() == {"model": "claude-y", "messages": MESSAGES}


def test_the_adapters_options_go_on_every_call() -> None:
    gw = FakeGateway()
    llm = make(gw, provider_options=CACHE)

    llm.complete(MESSAGES)
    llm.complete(MESSAGES, config={"max_tokens": 5})

    assert gw.body(0)["provider_options"] == CACHE
    assert gw.body(1)["provider_options"] == CACHE


def test_the_calls_own_options_are_merged_over_the_adapters() -> None:
    gw = FakeGateway()
    llm = make(gw, provider_options={**CACHE, "output_config": {"effort": "low"}})

    llm.complete(MESSAGES, config={"provider_options": {
        "output_config": {"effort": "high"}, "thinking": {"type": "adaptive"},
    }})
    llm.complete(MESSAGES, config={"provider_options": "not a mapping"})

    assert gw.body(0)["provider_options"] == {
        **CACHE, "output_config": {"effort": "high"}, "thinking": {"type": "adaptive"},
    }
    assert gw.body(1)["provider_options"] == {**CACHE, "output_config": {"effort": "low"}}
    assert llm.provider_options == {**CACHE, "output_config": {"effort": "low"}}


def test_options_of_one_call_alone() -> None:
    gw = FakeGateway()

    make(gw).complete(MESSAGES, config={"provider_options": CACHE})

    assert gw.body()["provider_options"] == CACHE


def test_options_can_be_set_after_reading_the_listing() -> None:
    """The host's rule, with the public API only: Anthropic's options go to
    the models Anthropic serves."""
    gw = FakeGateway()
    llm = make(gw)

    served_by = {m["id"]: m.get("provider") for m in llm.models()}
    if served_by.get(llm.model) == "anthropic":
        llm.provider_options = CACHE
    llm.complete(MESSAGES)

    assert gw.body()["provider_options"] == CACHE


def test_models_returns_the_gateways_entries() -> None:
    llm = make(FakeGateway())

    assert llm.models() == [m for m in CATALOG if isinstance(m, dict)]
    assert llm.list_models() == ["claude-y", "gpt-x", "legacy"]


def test_with_model_keeps_the_options_unless_told_otherwise() -> None:
    gw = FakeGateway()
    main = make(gw, provider_options=CACHE)

    main.with_model("claude-z").complete(MESSAGES)
    main.with_model("gpt-x", provider_options=None).complete(MESSAGES)
    main.with_model("gpt-x", provider_options={"reasoning_effort": "low"}).complete(MESSAGES)
    main.with_objective("summarize", provider_options=None).complete(MESSAGES)

    assert gw.body(0)["provider_options"] == CACHE
    assert "provider_options" not in gw.body(1)
    assert gw.body(2)["provider_options"] == {"reasoning_effort": "low"}
    assert "provider_options" not in gw.body(3)
    assert main.provider_options == CACHE  # the original adapter is untouched


def test_a_copys_options_are_its_own() -> None:
    main = make(FakeGateway(), provider_options=CACHE)
    aux = main.with_model("claude-z")

    aux.provider_options["output_config"] = {"effort": "low"}

    assert main.provider_options == CACHE
