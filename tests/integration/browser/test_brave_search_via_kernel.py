"""Integration test: web_search registered on the kernel, real SymGateway call.

This DOES hit production SymGateway and costs ~$0.003 per run. It requires
explicit ``SYMBIOTE_BRAVE_LIVE=1`` opt-in so credentials in a local ``.env``
never make the default test suite call an external production service.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

# Load .env credentials only after an explicit live-test opt-in.
_RUN_LIVE = os.getenv("SYMBIOTE_BRAVE_LIVE") == "1"
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
if _RUN_LIVE and _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


pytestmark = pytest.mark.skipif(
    not (
        _RUN_LIVE
        and os.getenv("SYMGATEWAY_BASE_URL")
        and os.getenv("SYMGATEWAY_API_KEY")
    ),
    reason=(
        "live Brave smoke disabled; set SYMBIOTE_BRAVE_LIVE=1 with "
        "SYMGATEWAY_BASE_URL and SYMGATEWAY_API_KEY"
    ),
)


@pytest.mark.asyncio
async def test_web_search_returns_real_results(tmp_path):
    """End-to-end: kernel.tool_gateway.execute_async('web_search', ...) returns hits."""
    kernel = SymbioteKernel(
        KernelConfig(db_path=str(tmp_path / "t.sqlite")),
        llm=MockLLMAdapter(default_response="ok"),
    )
    register(kernel, search_backend="brave")

    bot = kernel.create_symbiote(name="search-test", role="t")
    kernel.environment.configure(symbiote_id=bot.id, tools=["web_search"])

    result = await kernel._tool_gateway.execute_async(  # noqa: SLF001
        symbiote_id=bot.id,
        session_id=None,
        tool_id="web_search",
        params={"query": "symlabs", "limit": 3},
        timeout=30.0,
    )

    assert result.success, result.error
    output = result.output
    assert "results" in output
    assert "count" in output
    assert output["count"] >= 1
    for item in output["results"]:
        assert item["url"].startswith(("http://", "https://"))
        assert item["title"]
        assert item["snippet"] or item["snippet"] == ""
