"""Smoke: register(extract_backend='forge_scraper') and extract a real URL.

forge_scraper is free and works on public sites, so this test runs by
default. Skipable via SKIP_EXTRACT_SMOKE=1.
"""

from __future__ import annotations

import os

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_EXTRACT_SMOKE") == "1",
    reason="extract smoke disabled via SKIP_EXTRACT_SMOKE=1",
)


@pytest.mark.asyncio
async def test_forge_scraper_extracts_example_com(tmp_path):
    kernel = SymbioteKernel(
        KernelConfig(db_path=str(tmp_path / "t.sqlite")),
        llm=MockLLMAdapter(default_response="ok"),
    )
    register(kernel, extract_backend="forge_scraper")

    bot = kernel.create_symbiote(name="extract-test", role="t")
    kernel.environment.configure(symbiote_id=bot.id, tools=["web_extract"])

    result = await kernel._tool_gateway.execute_async(  # noqa: SLF001
        symbiote_id=bot.id,
        session_id=None,
        tool_id="web_extract",
        params={"url": "https://example.com/"},
        timeout=30.0,
    )

    assert result.success, result.error
    output = result.output
    assert output["url"] == "https://example.com/"
    # forge_scraper.get_content on a generic site should return some content
    # plus a title — exact strings depend on the lib version but the shape is fixed.
    assert isinstance(output["content"], str)
    assert "title" in output
    assert "metadata" in output


@pytest.mark.asyncio
async def test_extract_chain_falls_through_on_failure(tmp_path, monkeypatch):
    """Chain forge_scraper → firecrawl: first fails, second succeeds (mocked)."""
    from symbiote.browser.search.providers import firecrawl as fc_mod
    from symbiote.browser.search.providers import forge_scraper as fs_mod

    async def fs_fail(self, url):  # noqa: ARG001
        raise RuntimeError("forge_scraper boom")

    async def fc_ok(self, url):
        return {
            "url": url,
            "title": "From Firecrawl",
            "content": "# fallback content",
            "language": None,
            "platform": "firecrawl",
            "metadata": {},
        }

    monkeypatch.setattr(fs_mod.ForgeScraperProvider, "extract", fs_fail)
    monkeypatch.setattr(fc_mod.FirecrawlViaSymGateway, "extract", fc_ok)
    monkeypatch.setenv("SYMGATEWAY_BASE_URL", "https://gw.example.com/v1")
    monkeypatch.setenv("SYMGATEWAY_API_KEY", "sk-x")

    kernel = SymbioteKernel(
        KernelConfig(db_path=str(tmp_path / "t2.sqlite")),
        llm=MockLLMAdapter(default_response="ok"),
    )
    register(kernel, extract_backend=["forge_scraper", "firecrawl"])

    bot = kernel.create_symbiote(name="chain-test", role="t")
    kernel.environment.configure(symbiote_id=bot.id, tools=["web_extract"])

    result = await kernel._tool_gateway.execute_async(  # noqa: SLF001
        symbiote_id=bot.id,
        session_id=None,
        tool_id="web_extract",
        params={"url": "https://example.com/"},
        timeout=10.0,
    )

    assert result.success, result.error
    assert result.output["content"] == "# fallback content"
    assert result.output["extracted_by"] == "firecrawl"
