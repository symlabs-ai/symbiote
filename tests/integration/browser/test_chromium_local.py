"""End-to-end Chromium tests against a local HTTP server (no internet)."""

from __future__ import annotations

import asyncio

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register
from symbiote.browser.config import BrowserOptions
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

pytestmark = pytest.mark.integration


_BROWSER_TOOLS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_fill",
    "browser_close",
]


@pytest.fixture
def kernel(tmp_path):
    cfg = KernelConfig(db_path=str(tmp_path / "test.sqlite"))
    return SymbioteKernel(cfg, llm=MockLLMAdapter(default_response="ok"))


@pytest.fixture
def kernel_with_browser(kernel):
    register(
        kernel,
        browser_backend="chromium",
        browser_options=BrowserOptions(headed=False, timeout_ms=10_000),
    )
    yield kernel


def _bot_with_tools(kernel, name: str = "t"):
    bot = kernel.create_symbiote(name=name, role="tester")
    kernel.environment.configure(symbiote_id=bot.id, tools=_BROWSER_TOOLS)
    return bot


async def _run_async(kernel, tool_id: str, params: dict, bot, allow_internal: bool = True):
    if allow_internal:
        from symbiote.security import network

        orig = network.validate_url
        network.validate_url = lambda u: u  # noqa: E731
        try:
            return await kernel._tool_gateway.execute_async(  # noqa: SLF001
                symbiote_id=bot.id,
                session_id=None,
                tool_id=tool_id,
                params=params,
                timeout=20.0,
            )
        finally:
            network.validate_url = orig
    return await kernel._tool_gateway.execute_async(  # noqa: SLF001
        symbiote_id=bot.id,
        session_id=None,
        tool_id=tool_id,
        params=params,
        timeout=20.0,
    )


@pytest.mark.asyncio
async def test_navigate_to_local_site(kernel_with_browser, local_site):
    bot = _bot_with_tools(kernel_with_browser, "nav")
    result = await _run_async(kernel_with_browser, "browser_navigate", {
        "url": f"{local_site}/",
        "task_id": "t1",
    }, bot)
    assert result.success, result.error
    assert "127.0.0.1" in result.output["url"]
    await _run_async(kernel_with_browser, "browser_close", {"task_id": "t1"}, bot)


@pytest.mark.asyncio
async def test_snapshot_returns_text_with_refs(kernel_with_browser, local_site):
    bot = _bot_with_tools(kernel_with_browser, "snap")
    await _run_async(kernel_with_browser, "browser_navigate", {"url": f"{local_site}/", "task_id": "t1"}, bot)
    snap = await _run_async(kernel_with_browser, "browser_snapshot", {"task_id": "t1"}, bot)
    assert snap.success
    assert "Test site" in snap.output["text"]
    assert len(snap.output["refs"]) >= 1
    await _run_async(kernel_with_browser, "browser_close", {"task_id": "t1"}, bot)


@pytest.mark.asyncio
async def test_click_navigates_to_target(kernel_with_browser, local_site):
    bot = _bot_with_tools(kernel_with_browser, "click")
    await _run_async(kernel_with_browser, "browser_navigate", {"url": f"{local_site}/", "task_id": "t2"}, bot)
    snap = await _run_async(kernel_with_browser, "browser_snapshot", {"task_id": "t2"}, bot)
    assert snap.success
    link_ref = snap.output["refs"][0] if snap.output["refs"] else None
    assert link_ref is not None

    await _run_async(kernel_with_browser, "browser_click", {"ref": link_ref, "task_id": "t2"}, bot)
    after = await _run_async(kernel_with_browser, "browser_snapshot", {"task_id": "t2"}, bot)
    assert after.success
    assert "Arrived" in after.output["text"] or "made it" in after.output["text"]
    await _run_async(kernel_with_browser, "browser_close", {"task_id": "t2"}, bot)


@pytest.mark.asyncio
async def test_close_releases_session(kernel_with_browser, local_site):
    bot = _bot_with_tools(kernel_with_browser, "cl")
    await _run_async(kernel_with_browser, "browser_navigate", {"url": f"{local_site}/", "task_id": "t3"}, bot)
    result = await _run_async(kernel_with_browser, "browser_close", {"task_id": "t3"}, bot)
    assert result.success


@pytest.mark.asyncio
async def test_ssrf_blocks_localhost_by_default(kernel_with_browser):
    bot = _bot_with_tools(kernel_with_browser, "ssrf")
    result = await kernel_with_browser._tool_gateway.execute_async(  # noqa: SLF001
        symbiote_id=bot.id,
        session_id=None,
        tool_id="browser_navigate",
        params={"url": "http://127.0.0.1:1/", "task_id": "ssrf"},
        timeout=10.0,
    )
    assert not result.success
    err = (result.error or "").lower()
    assert "blocked" in err or "ssrf" in err or "private" in err
