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
