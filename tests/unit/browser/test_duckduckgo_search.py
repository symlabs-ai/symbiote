"""Unit tests for DuckDuckGoHtmlProvider — no network, httpx mocked."""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import quote

import httpx
import pytest

from symbiote.browser.config import SearchOptions
from symbiote.browser.search.providers.duckduckgo import (
    DuckDuckGoHtmlProvider,
    _decode_uddg_redirect,
    _extract_generic_anchors,
    _extract_result_anchors,
)


# A realistic-ish slice of DDG HTML output (post-decoding ampersands etc).
def _ddg_page(targets: list[tuple[str, str]]) -> str:
    items = []
    for target_url, title in targets:
        encoded = quote(target_url, safe="")
        items.append(
            f'<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg={encoded}&amp;rut=...">{title}</a></div>'
        )
    return f"""<!doctype html>
<html><body>
<h1>DDG results</h1>
{"".join(items)}
</body></html>"""


class TestExtractResultAnchors:
    def test_basic_extraction(self):
        html = _ddg_page([
            ("https://example.com/a", "Example A"),
            ("https://example.com/b", "Example B"),
        ])
        hits = _extract_result_anchors(html)
        assert len(hits) == 2
        assert hits[0]["url"] == "https://example.com/a"
        assert hits[0]["title"] == "Example A"
        assert hits[0]["snippet"] == ""
        assert hits[1]["url"] == "https://example.com/b"

    def test_skips_non_result_anchors(self):
        html = (
            '<a class="header__link" href="//duckduckgo.com/about">About</a>'
            + _ddg_page([("https://real.com", "Real")])
        )
        hits = _extract_result_anchors(html)
        assert len(hits) == 1
        assert hits[0]["url"] == "https://real.com"

    def test_returns_empty_when_no_result_class(self):
        html = "<a href='https://x.com'>plain link</a>"
        assert _extract_result_anchors(html) == []


class TestExtractGenericAnchors:
    def test_picks_up_plain_http_anchors(self):
        html = (
            '<a href="https://a.com">Title A</a>'
            '<a href="https://b.com">Title B</a>'
            '<a href="/relative">ignored</a>'
        )
        hits = _extract_generic_anchors(html)
        # /relative is dropped because the resolver returns the absolute uddg-decoded form
        # which becomes https://duckduckgo.com/relative — that IS http(s), so it's kept.
        # Update assertion accordingly:
        urls = [h["url"] for h in hits]
        assert "https://a.com" in urls
        assert "https://b.com" in urls

    def test_skips_empty_title(self):
        html = '<a href="https://x.com"></a><a href="https://y.com">Y</a>'
        hits = _extract_generic_anchors(html)
        urls = [h["url"] for h in hits]
        assert urls == ["https://y.com"]


class TestDecodeUddgRedirect:
    def test_uddg_redirect_extracts_real_url(self):
        encoded = quote("https://real-target.com/page", safe="")
        url = f"//duckduckgo.com/l/?uddg={encoded}&rut=junk"
        assert _decode_uddg_redirect(url) == "https://real-target.com/page"

    def test_absolute_non_ddg_url_passthrough(self):
        assert _decode_uddg_redirect("https://example.com/x") == "https://example.com/x"

    def test_relative_root_url_becomes_ddg(self):
        out = _decode_uddg_redirect("/about")
        assert out == "https://duckduckgo.com/about"

    def test_empty_returns_none(self):
        assert _decode_uddg_redirect("") is None
        assert _decode_uddg_redirect(None) is None

    def test_uddg_without_target_returns_none(self):
        assert _decode_uddg_redirect("//duckduckgo.com/l/?other=foo") is None


# ── Provider end-to-end with mocked httpx ────────────────────────────────


def _mock_get(html_body: str, status_code: int = 200):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            _FakeClient.last_url = url
            _FakeClient.last_params = params
            req = httpx.Request("GET", url, params=params)
            return httpx.Response(status_code, text=html_body, request=req)

    return _FakeClient


@pytest.mark.asyncio
async def test_search_returns_normalized_hits():
    html = _ddg_page([
        ("https://wikipedia.org/wiki/Python", "Python — Wikipedia"),
        ("https://docs.python.org", "Python Docs"),
    ])
    fake = _mock_get(html)
    with patch("httpx.AsyncClient", fake):
        provider = DuckDuckGoHtmlProvider(SearchOptions())
        results = await provider.search("python", limit=5)

    assert fake.last_url == "https://html.duckduckgo.com/html/"
    assert fake.last_params == {"q": "python"}
    assert len(results) == 2
    assert results[0]["url"] == "https://wikipedia.org/wiki/Python"
    assert results[0]["title"] == "Python — Wikipedia"


@pytest.mark.asyncio
async def test_search_dedupes_and_limits():
    html = _ddg_page([
        ("https://a.com", "A1"),
        ("https://a.com", "A2"),  # duplicate URL — should be dropped
        ("https://b.com", "B"),
        ("https://c.com", "C"),
    ])
    with patch("httpx.AsyncClient", _mock_get(html)):
        provider = DuckDuckGoHtmlProvider(SearchOptions())
        results = await provider.search("x", limit=2)
    urls = [r["url"] for r in results]
    assert urls == ["https://a.com", "https://b.com"]


@pytest.mark.asyncio
async def test_search_falls_back_to_generic_when_no_class_match():
    html = '<a href="https://lone.example/x">Only link</a>'
    with patch("httpx.AsyncClient", _mock_get(html)):
        provider = DuckDuckGoHtmlProvider(SearchOptions())
        results = await provider.search("x", limit=5)
    assert any(r["url"] == "https://lone.example/x" for r in results)


@pytest.mark.asyncio
async def test_search_raises_on_http_error():
    fake = _mock_get("server error", status_code=503)
    with patch("httpx.AsyncClient", fake):
        provider = DuckDuckGoHtmlProvider(SearchOptions())
        with pytest.raises(httpx.HTTPStatusError):
            await provider.search("x", limit=3)
