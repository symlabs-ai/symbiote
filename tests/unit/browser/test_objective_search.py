"""Unit tests for ObjectiveSearchViaSymGateway — no network, httpx is mocked."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register
from symbiote.browser.config import SearchOptions
from symbiote.browser.search.providers.symgateway_objective import (
    ObjectiveSearchViaSymGateway,
)
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

# What POST /v1/objectives/invoke answers for a service bound to Brave: the
# service output is Brave's own JSON.
_BRAVE_OUTPUT = {
    "type": "search",
    "web": {
        "results": [
            {"title": "Title 1", "url": "https://example.com/1", "description": "Snippet 1"},
            {"title": "Title 2", "url": "https://example.com/2", "description": "Snippet 2"},
        ],
    },
}


def _envelope(output: object) -> dict:
    return {
        "request_id": "sr_test",
        "objective": "web_seek",
        "service": "websearch",
        "output": output,
        "status_code": 200,
        "fallback_used": False,
    }


@pytest.fixture
def opts() -> SearchOptions:
    return SearchOptions(
        symgateway_base_url="https://gw.example.com/v1",
        symgateway_api_key="sk-test-xxx",
        symgateway_objective="web_seek",
    )


def _mock_post(payload: dict, status: int = 200):
    """An AsyncClient-like factory whose post() answers ``payload``."""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            _FakeClient.last_url = url
            _FakeClient.last_headers = headers
            _FakeClient.last_json = json
            return httpx.Response(status, json=payload, request=httpx.Request("POST", url))

    return _FakeClient


@pytest.mark.asyncio
async def test_search_invokes_the_objective_service(opts):
    fake = _mock_post(_envelope(_BRAVE_OUTPUT))
    with patch("httpx.AsyncClient", fake):
        results = await ObjectiveSearchViaSymGateway(opts).search("ia no brasil", limit=2)

    # /v1 is part of this endpoint, whether or not the configured URL has it.
    assert fake.last_url == "https://gw.example.com/v1/objectives/invoke"
    assert fake.last_headers == {"Authorization": "Bearer sk-test-xxx"}
    assert fake.last_json == {
        "objective": "web_seek",
        "service": "websearch",
        "input": {"q": "ia no brasil", "count": 2},
    }
    assert results == [
        {"url": "https://example.com/1", "title": "Title 1", "snippet": "Snippet 1"},
        {"url": "https://example.com/2", "title": "Title 2", "snippet": "Snippet 2"},
    ]


@pytest.mark.asyncio
async def test_service_name_and_limit_follow_the_options(opts):
    custom = opts.model_copy(update={"symgateway_objective_service": "llmcontext"})
    fake = _mock_post(_envelope(_BRAVE_OUTPUT))
    with patch("httpx.AsyncClient", fake):
        provider = ObjectiveSearchViaSymGateway(custom)
        await provider.search("x", limit=99)
        assert fake.last_json["service"] == "llmcontext"
        assert fake.last_json["input"]["count"] == 20
        await provider.search("x", limit=0)
        assert fake.last_json["input"]["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        [{"url": "https://a.example", "title": "A", "snippet": "sa"}],
        {"results": [{"url": "https://a.example", "title": "A", "description": "sa"}]},
    ],
)
async def test_accepts_an_output_the_objective_already_normalized(opts, output):
    with patch("httpx.AsyncClient", _mock_post(_envelope(output))):
        results = await ObjectiveSearchViaSymGateway(opts).search("x")
    assert results == [{"url": "https://a.example", "title": "A", "snippet": "sa"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [None, {}, {"web": {}}, "texto solto", [1, "a"]])
async def test_unexpected_output_gives_no_results(opts, output):
    with patch("httpx.AsyncClient", _mock_post(_envelope(output))):
        assert await ObjectiveSearchViaSymGateway(opts).search("x") == []


@pytest.mark.asyncio
async def test_a_refusal_carries_the_gateway_reason(opts):
    refusal = {
        "error": {
            "code": "auth_insufficient",
            "message": "Not authorized for external_services.invoke (credential_action_ceiling)",
            "type": "gateway_error",
        }
    }
    with patch("httpx.AsyncClient", _mock_post(refusal, status=403)), pytest.raises(RuntimeError) as err:
        await ObjectiveSearchViaSymGateway(opts).search("x")
    message = str(err.value)
    assert "web_seek" in message and "HTTP 403" in message
    assert "external_services.invoke" in message
    assert "sk-test-xxx" not in message


def test_the_objective_name_is_required():
    opts = SearchOptions(symgateway_base_url="https://gw.example.com", symgateway_api_key="k")
    with pytest.raises(ValueError, match="symgateway_objective"):
        ObjectiveSearchViaSymGateway(opts)


@pytest.mark.asyncio
async def test_web_search_tool_uses_the_objective_backend(tmp_path):
    kernel = SymbioteKernel(
        KernelConfig(db_path=str(tmp_path / "t.sqlite")),
        llm=MockLLMAdapter(default_response="ok"),
    )
    register(
        kernel,
        search_backend="symgateway_objective",
        search_options={
            "symgateway_base_url": "http://localhost:18090",
            "symgateway_api_key": "sk-test-xxx",
            "symgateway_objective": "web_seek",
        },
    )
    bot = kernel.create_symbiote(name="search-test", role="t")
    kernel.environment.configure(symbiote_id=bot.id, tools=["web_search"])

    fake = _mock_post(_envelope(_BRAVE_OUTPUT))
    with patch("httpx.AsyncClient", fake):
        result = await kernel._tool_gateway.execute_async(  # noqa: SLF001
            symbiote_id=bot.id,
            session_id=None,
            tool_id="web_search",
            params={"query": "symlabs", "limit": 3},
            timeout=30.0,
        )

    assert result.success, result.error
    assert fake.last_url == "http://localhost:18090/v1/objectives/invoke"
    assert result.output["count"] == 2
    assert result.output["results"][0]["url"] == "https://example.com/1"
