"""Unit tests for ForgeScraperProvider — forge_scraper module mocked."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from symbiote.browser.config import SearchOptions
from symbiote.browser.search.providers.forge_scraper import ForgeScraperProvider


def _fake_content_info(**kwargs):
    obj = types.SimpleNamespace()
    obj.title = kwargs.get("title", "")
    obj.content = kwargs.get("content", "")
    obj.language = kwargs.get("language")
    obj.metadata = kwargs.get("metadata", {})
    if "platform" in kwargs:
        plat = types.SimpleNamespace(value=kwargs["platform"])
        obj.platform = plat
    # forge_scraper >= 0.11.0 — propagated when set; older versions don't
    # expose these attributes at all (getattr default kicks in).
    if "content_quality" in kwargs:
        obj.content_quality = kwargs["content_quality"]
    if "quality_reason" in kwargs:
        obj.quality_reason = kwargs["quality_reason"]
    return obj


@pytest.fixture
def mocked_forge(monkeypatch):
    """Inject a fake `forge_scraper` module so the provider can import it lazily."""
    fake_module = types.ModuleType("forge_scraper")
    fake_module.get_content = MagicMock()
    monkeypatch.setitem(sys.modules, "forge_scraper", fake_module)
    return fake_module


@pytest.mark.asyncio
async def test_extract_returns_normalized_shape(mocked_forge):
    mocked_forge.get_content.return_value = _fake_content_info(
        title="Test Article",
        content="# Heading\n\nBody text.",
        language="en",
        platform="GENERIC",
        metadata={"source": "blog"},
    )
    provider = ForgeScraperProvider(SearchOptions())
    result = await provider.extract("https://example.com/article")

    assert result == {
        "url": "https://example.com/article",
        "title": "Test Article",
        "content": "# Heading\n\nBody text.",
        "language": "en",
        "platform": "GENERIC",
        "metadata": {"source": "blog"},
        "content_quality": None,
        "quality_reason": None,
    }
    mocked_forge.get_content.assert_called_once_with(
        url="https://example.com/article", verbose=False
    )


@pytest.mark.asyncio
async def test_extract_handles_missing_fields(mocked_forge):
    mocked_forge.get_content.return_value = _fake_content_info(content="bare")
    provider = ForgeScraperProvider(SearchOptions())
    result = await provider.extract("https://x.com")
    assert result["title"] == ""
    assert result["content"] == "bare"
    assert result["language"] is None
    assert result["metadata"] == {}
    assert result["platform"] is None
    # Older forge_scraper (<0.11.0) doesn't set these — getattr defaults to None.
    assert result["content_quality"] is None
    assert result["quality_reason"] is None


@pytest.mark.asyncio
async def test_extract_propagates_quality_signal(mocked_forge):
    """forge_scraper >= 0.11.0 exposes content_quality + quality_reason.

    The provider must forward both verbatim so downstream consumers
    (e.g. Jitto tool loop) can react to "low" quality (likely_js_rendered,
    meta_only) by trying a different extractor or telling the LLM the
    page is JS-rendered.
    """
    mocked_forge.get_content.return_value = _fake_content_info(
        title="ge.globo.com",
        content="A short meta-description-only blob.",
        platform="GENERIC",
        content_quality="low",
        quality_reason="likely_js_rendered",
    )
    provider = ForgeScraperProvider(SearchOptions())
    result = await provider.extract("https://ge.globo.com/futebol")
    assert result["content_quality"] == "low"
    assert result["quality_reason"] == "likely_js_rendered"


@pytest.mark.asyncio
async def test_extract_propagates_lib_errors(mocked_forge):
    mocked_forge.get_content.side_effect = RuntimeError("upstream broken")
    provider = ForgeScraperProvider(SearchOptions())
    with pytest.raises(RuntimeError, match="upstream broken"):
        await provider.extract("https://example.com")


def test_lazy_import_raises_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "forge_scraper", None)
    provider = ForgeScraperProvider(SearchOptions())
    import asyncio

    with pytest.raises(ImportError, match=r"pip install.*\[extract\]"):
        asyncio.run(provider.extract("https://x.com"))
