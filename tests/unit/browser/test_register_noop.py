"""Backward-compat: without register() the kernel behaves exactly as before."""

from __future__ import annotations

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register
from symbiote.browser.config import (
    BrowserOptions,
    PolicyConfig,
    SearchOptions,
    SearchRouting,
)
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel


@pytest.fixture
def kernel(tmp_path):
    cfg = KernelConfig(db_path=str(tmp_path / "test.sqlite"))
    return SymbioteKernel(cfg, llm=MockLLMAdapter(default_response="ok"))


def _browser_tool_ids(kernel: SymbioteKernel) -> set[str]:
    return {
        tool_id
        for tool_id in kernel._tool_gateway._descriptors  # noqa: SLF001
        if tool_id.startswith(("web_", "browser_"))
    }


def test_kernel_has_no_browser_tools_when_register_not_called(kernel):
    assert _browser_tool_ids(kernel) == set()


def test_register_with_all_none_is_noop(kernel):
    before = set(kernel._tool_gateway._descriptors)  # noqa: SLF001
    register(kernel)
    after = set(kernel._tool_gateway._descriptors)  # noqa: SLF001
    assert before == after


def test_register_is_idempotent(kernel):
    register(kernel, browser_backend="chromium")
    register(kernel, browser_backend="chromium")
    register(kernel, search_backend="tavily")
    # No exception, no duplicate registrations.


def test_register_accepts_dict_or_model_for_options(kernel):
    register(
        kernel,
        search_options={"max_results_default": 10},
        browser_options=BrowserOptions(headed=True),
        policy={"blocklist": ["ads.com"]},
    )


def test_config_models_validate_fields():
    opts = BrowserOptions(headed=True, slow_mo=500)
    assert opts.headed is True
    assert opts.slow_mo == 500

    pol = PolicyConfig(blocklist=["a.com"], allowlist=["b.com"])
    assert pol.allowlist == ["b.com"]

    sopts = SearchOptions(compress_results=False)
    assert sopts.compress_results is False

    routing = SearchRouting(web_search="tavily", web_extract="firecrawl")
    assert routing.web_search == "tavily"
    assert routing.web_crawl is None
