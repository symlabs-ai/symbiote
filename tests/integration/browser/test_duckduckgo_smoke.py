"""Integration smoke: register backend='duckduckgo' and hit DDG live.

DDG HTML is free and unauthenticated, so this test runs by default. It may
flake under rate limiting or DDG layout changes — gated by the env var
SKIP_DDG_SMOKE=1 if you need to silence it temporarily.
"""

from __future__ import annotations

import os

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DDG_SMOKE") == "1",
    reason="DDG smoke disabled via SKIP_DDG_SMOKE=1",
)


@pytest.mark.asyncio
async def test_web_search_via_duckduckgo_returns_hits(tmp_path):
    kernel = SymbioteKernel(
        KernelConfig(db_path=str(tmp_path / "t.sqlite")),
        llm=MockLLMAdapter(default_response="ok"),
    )
    register(kernel, search_backend="duckduckgo")

    bot = kernel.create_symbiote(name="ddg", role="t")
    kernel.environment.configure(symbiote_id=bot.id, tools=["web_search"])

    result = await kernel._tool_gateway.execute_async(  # noqa: SLF001
        symbiote_id=bot.id,
        session_id=None,
        tool_id="web_search",
        params={"query": "python programming language wikipedia", "limit": 5},
        timeout=30.0,
    )

    assert result.success, result.error
    output = result.output
    assert output["count"] >= 1, f"DDG returned nothing for a generic query: {output}"
    for item in output["results"]:
        assert item["url"].startswith(("http://", "https://"))
        assert item["title"]
