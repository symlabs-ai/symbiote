"""Unit tests for BraveViaSymGateway — no network, httpx is mocked."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from symbiote.browser.config import SearchOptions
from symbiote.browser.search.providers.brave import BraveViaSymGateway

# A trimmed but realistic SymGateway envelope around a Brave search response.
_FAKE_ENVELOPE = {
    "request_id": "sr_test",
    "status_code": 200,
    "body": {
        "type": "search",
        "query": {"original": "test"},
        "web": {
            "type": "search",
            "results": [
                {
                    "title": "Title 1",
                    "url": "https://example.com/1",
                    "description": "Snippet 1",
                },
                {
                    "title": "Title 2",
                    "url": "https://example.com/2",
                    "description": "Snippet 2",
                },
            ],
        },
    },
    "elapsed_ms": 123,
    "cost_usd": 0.003,
}


@pytest.fixture
def opts():
    return SearchOptions(
        symgateway_base_url="https://gw.example.com/v1",
        symgateway_api_key="sk-test-xxx",
    )


def _mock_post(envelope: dict):
    """Return an AsyncClient-like factory whose post() yields a fake Response."""

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
            req = httpx.Request("POST", url)
            return httpx.Response(200, json=envelope, request=req)

    return _FakeClient


@pytest.mark.asyncio
async def test_search_calls_proxy_endpoint(opts):
    fake = _mock_post(_FAKE_ENVELOPE)
    with patch("httpx.AsyncClient", fake):
        provider = BraveViaSymGateway(opts)
        results = await provider.search("test", limit=2)

    assert fake.last_url == "https://gw.example.com/proxy/brave/web-search"
    assert fake.last_headers == {"Authorization": "Bearer sk-test-xxx"}
    assert fake.last_json == {
        "method": "GET",
        "path": "/res/v1/web/search",
        "query_params": {"q": "test", "count": "2"},
    }
    assert results == [
        {"url": "https://example.com/1", "title": "Title 1", "snippet": "Snippet 1"},
        {"url": "https://example.com/2", "title": "Title 2", "snippet": "Snippet 2"},
    ]


@pytest.mark.asyncio
async def test_strips_v1_suffix_from_gateway_url():
    opts_v1 = SearchOptions(
        symgateway_base_url="https://gw.example.com/v1/",
        symgateway_api_key="k",
    )
    opts_no_v1 = SearchOptions(
        symgateway_base_url="https://gw.example.com/",
        symgateway_api_key="k",
    )
    assert opts_v1.resolved_gateway_url() == "https://gw.example.com"
    assert opts_no_v1.resolved_gateway_url() == "https://gw.example.com"


@pytest.mark.asyncio
async def test_limit_clamped_to_valid_range(opts):
    fake = _mock_post(_FAKE_ENVELOPE)
    with patch("httpx.AsyncClient", fake):
        provider = BraveViaSymGateway(opts)
        await provider.search("test", limit=999)
    assert fake.last_json["query_params"]["count"] == "20"

    fake2 = _mock_post(_FAKE_ENVELOPE)
    with patch("httpx.AsyncClient", fake2):
        provider2 = BraveViaSymGateway(opts)
        await provider2.search("test", limit=0)
    assert fake2.last_json["query_params"]["count"] == "1"


@pytest.mark.asyncio
async def test_handles_empty_results(opts):
    envelope = json.loads(json.dumps(_FAKE_ENVELOPE))
    envelope["body"]["web"]["results"] = []
    with patch("httpx.AsyncClient", _mock_post(envelope)):
        provider = BraveViaSymGateway(opts)
        results = await provider.search("nothing here")
    assert results == []


@pytest.mark.asyncio
async def test_raises_on_upstream_error(opts):
    envelope = {
        "request_id": "sr_x",
        "status_code": 500,
        "body": {"error": "upstream failed"},
        "elapsed_ms": 12,
    }
    with patch("httpx.AsyncClient", _mock_post(envelope)):
        provider = BraveViaSymGateway(opts)
        with pytest.raises(RuntimeError, match="Brave search via SymGateway failed"):
            await provider.search("oops")


def test_missing_env_raises_clear_error(monkeypatch):
    monkeypatch.delenv("SYMGATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("SYMGATEWAY_API_KEY", raising=False)
    opts = SearchOptions()  # no overrides
    with pytest.raises(ValueError, match="SymGateway base URL"):
        opts.resolved_gateway_url()


def test_missing_key_raises_clear_error(monkeypatch):
    monkeypatch.setenv("SYMGATEWAY_BASE_URL", "https://gw.example.com/v1")
    monkeypatch.delenv("SYMGATEWAY_API_KEY", raising=False)
    opts = SearchOptions()
    with pytest.raises(ValueError, match="SymGateway API key"):
        opts.resolved_api_key()
