"""Regression: smaller browser/search items (report item T-7) — the website
policy covers web_extract and web_crawl, a trailing dot does not bypass it,
registration is tracked on the kernel, a missing extra fails at register(),
and web_crawl accepts a bare domain. No network: providers and the SSRF
validator are replaced by fakes."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import symbiote.security.network as network
from symbiote.browser import register

# ``symbiote.browser.register`` is both the submodule and the re-exported
# function, so ``import ... as`` would bind the function.
register_mod = importlib.import_module("symbiote.browser.register")
from symbiote.browser.config import PolicyConfig
from symbiote.browser.hooks.website_policy import DomainBlockedError, WebsitePolicy
from tests.support import ScriptedLLM, make_kernel


class _FakeExtract:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def extract(self, url: str) -> dict:
        self.urls.append(url)
        return {"url": url, "content": "x"}


class _FakeCrawl:
    def __init__(self) -> None:
        self.domains: list[str] = []

    async def crawl(self, domain: str, *, instruction: str, max_pages: int) -> list:
        self.domains.append(domain)
        return []


@pytest.fixture()
def fakes(monkeypatch):
    extract, crawl, validated = _FakeExtract(), _FakeCrawl(), []

    def fake_validate(url: str) -> None:
        if "://" not in url:
            raise ValueError(f"URL without scheme: {url}")
        validated.append(url)

    monkeypatch.setattr(network, "validate_url", fake_validate)
    monkeypatch.setattr(register_mod, "_build_extract_provider", lambda b, o: extract)
    monkeypatch.setattr(register_mod, "_build_crawl_provider", lambda b, o: crawl)
    return extract, crawl, validated


@pytest.fixture()
def kernel(tmp_path: Path):
    k = make_kernel(tmp_path, ScriptedLLM("ok"))
    yield k
    k.shutdown()


def _authorize(kernel) -> str:
    sid = kernel.create_symbiote(name="a", role="r").id
    kernel.environment.configure(symbiote_id=sid, tools=["web_extract", "web_crawl"])
    return sid


def test_policy_applies_to_extract_and_crawl(kernel, fakes) -> None:
    extract, crawl, _ = fakes
    register(kernel, extract_backend="firecrawl", crawl_backend="firecrawl",
             policy={"blocklist": ["evil.example"]})
    sid = _authorize(kernel)

    ext = kernel.tool_gateway.execute(sid, None, "web_extract", {"url": "https://evil.example/a"})
    crw = kernel.tool_gateway.execute(
        sid, None, "web_crawl", {"domain": "https://sub.evil.example", "instruction": "i"}
    )

    assert not ext.success and "polic" in ext.error.lower()
    assert not crw.success and "polic" in crw.error.lower()
    assert extract.urls == [] and crawl.domains == []


def test_web_crawl_accepts_a_bare_domain(kernel, fakes) -> None:
    _, crawl, _ = fakes
    register(kernel, crawl_backend="firecrawl")
    sid = _authorize(kernel)

    res = kernel.tool_gateway.execute(
        sid, None, "web_crawl", {"domain": "docs.example.org", "instruction": "i"}
    )

    assert res.success, res.error
    assert crawl.domains == ["https://docs.example.org"]


@pytest.mark.parametrize(
    ("pattern", "url"),
    [("evil.example", "https://evil.example./x"), ("evil.example.", "https://evil.example/x"),
     ("*.evil.example", "https://a.evil.example./x")],
)
def test_trailing_dot_does_not_bypass_the_policy(pattern: str, url: str) -> None:
    with pytest.raises(DomainBlockedError):
        WebsitePolicy(PolicyConfig(blocklist=[pattern])).check(url)


def test_registration_is_tracked_on_the_kernel_not_by_id(kernel, fakes) -> None:
    # A stale id() from a kernel that was garbage-collected must not make a
    # new kernel at the same address skip registration.
    stale = getattr(register_mod, "_REGISTERED_KERNELS", None)
    if stale is not None:
        stale.add(id(kernel))

    register(kernel, extract_backend="firecrawl")
    register(kernel, extract_backend="firecrawl")  # second call: no-op

    assert kernel.tool_gateway.has_tool("web_extract")


def test_missing_extra_fails_at_register(kernel, monkeypatch) -> None:
    monkeypatch.setattr(register_mod, "_module_available", lambda name: False, raising=False)

    with pytest.raises(ImportError, match=r"symbiote\[browser\]"):
        register(kernel, browser_backend="chromium")
    with pytest.raises(ImportError, match=r"symbiote\[extract\]"):
        register(kernel, extract_backend="forge_scraper")

    assert not kernel.tool_gateway.has_tool("browser_navigate")
    assert not kernel.tool_gateway.has_tool("web_extract")
