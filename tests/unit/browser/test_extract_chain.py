"""Unit tests for ExtractWithFallback — provider chain behavior."""

from __future__ import annotations

import pytest

from symbiote.browser.search.extract_chain import ExtractWithFallback


class _StubProvider:
    """ExtractProvider stub configurable per test."""

    def __init__(
        self,
        name: str,
        *,
        content: str = "",
        title: str = "",
        raises: Exception | None = None,
    ):
        self.name = name
        self._content = content
        self._title = title
        self._raises = raises
        self.calls = 0

    async def extract(self, url: str):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return {
            "url": url,
            "title": self._title,
            "content": self._content,
            "language": None,
            "platform": None,
            "metadata": {},
        }


@pytest.mark.asyncio
async def test_first_provider_wins_when_content_present():
    a = _StubProvider("a", content="first", title="A")
    b = _StubProvider("b", content="second", title="B")
    chain = ExtractWithFallback([a, b])
    result = await chain.extract("https://x.com")
    assert result["content"] == "first"
    assert result["extracted_by"] == "a"
    assert a.calls == 1
    assert b.calls == 0


@pytest.mark.asyncio
async def test_falls_through_when_first_returns_empty():
    a = _StubProvider("a", content="")
    b = _StubProvider("b", content="second", title="B")
    chain = ExtractWithFallback([a, b])
    result = await chain.extract("https://x.com")
    assert result["content"] == "second"
    assert result["extracted_by"] == "b"
    assert a.calls == 1
    assert b.calls == 1


@pytest.mark.asyncio
async def test_falls_through_when_first_raises():
    a = _StubProvider("a", raises=RuntimeError("network"))
    b = _StubProvider("b", content="recovered", title="B")
    chain = ExtractWithFallback([a, b])
    result = await chain.extract("https://x.com")
    assert result["content"] == "recovered"
    assert result["extracted_by"] == "b"


@pytest.mark.asyncio
async def test_returns_empty_when_all_empty():
    a = _StubProvider("a", content="")
    b = _StubProvider("b", content="")
    chain = ExtractWithFallback([a, b])
    result = await chain.extract("https://x.com")
    assert result["content"] == ""
    assert result["extracted_by"] is None


@pytest.mark.asyncio
async def test_reraises_last_error_when_all_fail():
    a = _StubProvider("a", raises=RuntimeError("net 1"))
    b = _StubProvider("b", raises=RuntimeError("net 2"))
    chain = ExtractWithFallback([a, b])
    with pytest.raises(RuntimeError, match="net 2"):
        await chain.extract("https://x.com")


def test_empty_chain_rejected():
    with pytest.raises(ValueError, match="at least one provider"):
        ExtractWithFallback([])


def test_name_reflects_chain():
    a = _StubProvider("forge_scraper", content="x")
    b = _StubProvider("firecrawl", content="y")
    chain = ExtractWithFallback([a, b])
    assert chain.name == "forge_scraper+firecrawl"
