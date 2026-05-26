"""Unit tests for FirecrawlViaSymGateway — httpx mocked, no SymGateway hit."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from symbiote.browser.config import SearchOptions
from symbiote.browser.search.providers.firecrawl import FirecrawlViaSymGateway


@pytest.fixture
def opts():
    return SearchOptions(
        symgateway_base_url="https://gw.example.com/v1",
        symgateway_api_key="sk-test-firecrawl",
    )


def _mock_post(envelope, status=200):
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
            return httpx.Response(status, json=envelope, request=req)

    return _FakeClient


@pytest.mark.asyncio
async def test_extract_calls_proxy_scrape(opts):
    envelope = {
        "status_code": 200,
        "body": {
            "data": {
                "markdown": "# Title\n\nbody",
                "metadata": {"title": "Title", "language": "en"},
            }
        },
        "elapsed_ms": 800,
    }
    fake = _mock_post(envelope)
    with patch("httpx.AsyncClient", fake):
        provider = FirecrawlViaSymGateway(opts)
        result = await provider.extract("https://example.com/article")

    assert fake.last_url == "https://gw.example.com/proxy/firecrawl/scrape"
    assert fake.last_headers == {"Authorization": "Bearer sk-test-firecrawl"}
    assert fake.last_json == {
        "method": "POST",
        "path": "/v1/scrape",
        "body": {"url": "https://example.com/article", "formats": ["markdown"]},
    }
    assert result["content"] == "# Title\n\nbody"
    assert result["title"] == "Title"
    assert result["language"] == "en"


@pytest.mark.asyncio
async def test_crawl_calls_proxy_crawl_and_normalizes_pages(opts):
    envelope = {
        "status_code": 200,
        "body": {
            "data": [
                {
                    "markdown": "page 1",
                    "metadata": {"sourceURL": "https://x.com/a", "title": "A"},
                },
                {
                    "markdown": "page 2",
                    "metadata": {"sourceURL": "https://x.com/b", "title": "B"},
                },
            ]
        },
    }
    fake = _mock_post(envelope)
    with patch("httpx.AsyncClient", fake):
        provider = FirecrawlViaSymGateway(opts)
        results = await provider.crawl(
            "https://x.com", instruction="all blog posts", max_pages=5
        )

    assert fake.last_url == "https://gw.example.com/proxy/firecrawl/crawl"
    assert fake.last_json["body"]["limit"] == 5
    assert fake.last_json["body"]["prompt"] == "all blog posts"
    assert results == [
        {"url": "https://x.com/a", "title": "A", "content": "page 1"},
        {"url": "https://x.com/b", "title": "B", "content": "page 2"},
    ]


@pytest.mark.asyncio
async def test_extract_404_means_provider_not_seeded(opts):
    envelope = {"error": "provider firecrawl not configured"}
    fake = _mock_post(envelope, status=404)
    with patch("httpx.AsyncClient", fake):
        provider = FirecrawlViaSymGateway(opts)
        with pytest.raises(RuntimeError, match=r"not configured in SymGateway"):
            await provider.extract("https://x.com")


@pytest.mark.asyncio
async def test_upstream_error_status_surfaced(opts):
    envelope = {"status_code": 500, "body": {"error": "upstream timeout"}, "elapsed_ms": 12}
    fake = _mock_post(envelope, status=200)
    with patch("httpx.AsyncClient", fake):
        provider = FirecrawlViaSymGateway(opts)
        with pytest.raises(RuntimeError, match=r"Firecrawl via SymGateway failed"):
            await provider.extract("https://x.com")
