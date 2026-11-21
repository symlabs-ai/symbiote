"""The web tools have no built-in engine (B-91).

``web_search`` and ``web_crawl`` existed only through SymGateway, with Brave
named in the code. The host now passes its own provider or the name of a
registered one, and nothing here needs a gateway.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from symbiote.browser import (
    register,
    register_crawl_provider,
    register_extract_provider,
    register_search_provider,
)
from symbiote.browser.config import SearchOptions
from symbiote.browser.search import registry
from symbiote.browser.search.providers.brave_api import BraveSearchAPI
from symbiote.browser.search.providers.searxng import SearxngSearch
from tests.support import ScriptedLLM, make_kernel


@pytest.fixture(autouse=True)
def _no_gateway_and_clean_registry(monkeypatch):
    """No SymGateway in the environment; names registered by a test go away."""
    for name in ("SYMGATEWAY_BASE_URL", "SYMGATEWAY_API_KEY", "BRAVE_API_KEY", "SEARXNG_URL"):
        monkeypatch.delenv(name, raising=False)
    for kind in ("search", "extract", "crawl"):
        monkeypatch.setitem(registry._FACTORIES, kind, dict(registry._FACTORIES[kind]))


@pytest.fixture()
def kernel(tmp_path: Path):
    k = make_kernel(tmp_path, ScriptedLLM("ok"))
    yield k
    k.shutdown()


class DuckSearch:
    """A host's own search engine."""

    name = "duck"

    def __init__(self) -> None:
        self.queries: list[tuple[str, int]] = []

    async def search(self, query: str, *, limit: int = 5):
        self.queries.append((query, limit))
        return [{"url": "https://example.com/a", "title": "A", "snippet": f"about {query}"}]


class HostExtract:
    name = "host-extract"

    async def extract(self, url: str):
        return {"url": url, "title": "T", "content": "page text"}


class HostCrawl:
    name = "host-crawl"

    async def crawl(self, domain: str, *, instruction: str, max_pages: int = 10):
        return [{"url": f"https://{domain}/x", "title": "X", "content": instruction}]


async def _run(kernel, tool_id: str, **params):
    bot = kernel.create_symbiote(name=f"bot-{tool_id}", role="t")
    kernel.environment.configure(symbiote_id=bot.id, tools=[tool_id])
    return await kernel.tool_gateway.execute_async(
        symbiote_id=bot.id, session_id=None, tool_id=tool_id, params=params, timeout=10.0
    )


# ── the host's own provider ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_provider_object_backs_web_search_without_any_gateway(kernel) -> None:
    engine = DuckSearch()

    register(kernel, search_backend=engine)
    result = await _run(kernel, "web_search", query="symbiotes", limit=3)

    assert result.success, result.error
    assert result.output["results"][0]["url"] == "https://example.com/a"
    assert engine.queries == [("symbiotes", 3)]
    assert kernel.tool_gateway.has_tool("web_search")
    assert not kernel.tool_gateway.has_tool("web_crawl")


@pytest.mark.asyncio
async def test_provider_objects_back_extract_and_crawl_too(kernel) -> None:
    register(kernel, extract_backend=HostExtract(), crawl_backend=HostCrawl())

    extracted = await _run(kernel, "web_extract", url="https://example.com/page")
    crawled = await _run(
        kernel, "web_crawl", domain="example.com", instruction="find prices"
    )

    assert extracted.success, extracted.error
    assert "page text" in str(extracted.output)
    assert crawled.success, crawled.error
    assert "find prices" in str(crawled.output)


def test_an_object_without_the_method_is_refused(kernel) -> None:
    with pytest.raises(TypeError, match=r"async search\(\) method"):
        register(kernel, search_backend=object())


# ── registered names ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_registered_name_builds_the_provider_with_the_options(kernel) -> None:
    seen: list[SearchOptions] = []
    engine = DuckSearch()

    def factory(options: SearchOptions):
        seen.append(options)
        return engine

    register_search_provider("duck", factory)
    register(
        kernel,
        search_backend="duck",
        search_options={"provider_options": {"duck": {"region": "br"}}},
    )
    result = await _run(kernel, "web_search", query="x")

    assert result.success, result.error
    assert seen[0].options_for("duck") == {"region": "br"}
    assert seen[0].options_for("other") == {}


def test_names_can_be_registered_for_extract_and_crawl(kernel) -> None:
    register_extract_provider("host-extract", lambda _o: HostExtract())
    register_crawl_provider("host-crawl", lambda _o: HostCrawl())

    register(kernel, extract_backend=["host-extract"], crawl_backend="host-crawl")

    assert kernel.tool_gateway.has_tool("web_extract")
    assert kernel.tool_gateway.has_tool("web_crawl")


def test_an_unknown_name_says_what_is_registered(kernel) -> None:
    with pytest.raises(ValueError, match="Unknown search backend 'tavily'") as info:
        register(kernel, search_backend="tavily")

    for name in ("brave", "brave_api", "searxng", "symgateway_objective"):
        assert name in str(info.value)


def test_names_shipped_with_the_package() -> None:
    assert registry.provider_names("search") == [
        "brave", "brave_api", "searxng", "symgateway_objective",
    ]
    assert registry.provider_names("extract") == ["firecrawl", "forge_scraper", "http"]
    assert registry.provider_names("crawl") == ["firecrawl"]


def test_bad_registrations_are_refused() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        register_search_provider("  ", lambda _o: DuckSearch())
    with pytest.raises(TypeError, match="callable"):
        register_search_provider("duck", "not a factory")  # type: ignore[arg-type]


# ── there is no default engine ──────────────────────────────────────────


def test_routing_names_the_search_provider_or_registration_fails(kernel) -> None:
    register_search_provider("duck", lambda _o: DuckSearch())

    with pytest.raises(ValueError, match="without a provider for web_search"):
        register(kernel, search_routing={"web_extract": "duck"})

    register(kernel, search_routing={"web_search": "duck"})
    assert kernel.tool_gateway.has_tool("web_search")


def test_the_gateway_names_still_ask_for_the_gateway(kernel) -> None:
    with pytest.raises(ValueError, match="SymGateway base URL not set"):
        register(kernel, search_backend="brave")


# ── the direct providers ────────────────────────────────────────────────


def _fake_async_client(payload: dict, seen: dict):
    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            seen["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def get(self, url, params=None, headers=None):
            seen.update(url=url, params=params, headers=headers)
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    return _Client


@pytest.mark.asyncio
async def test_brave_api_calls_brave_directly_with_the_hosts_key(monkeypatch) -> None:
    seen: dict = {}
    payload = {"web": {"results": [
        {"title": "T1", "url": "https://a.example", "description": "S1"},
        {"title": "T2", "url": "https://b.example", "description": "S2"},
    ]}}
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(payload, seen))
    provider = BraveSearchAPI(
        SearchOptions(provider_options={"brave_api": {"api_key": "BSA-test"}}, timeout_seconds=7)
    )

    results = await provider.search("symbiotes", limit=1)

    assert results == [{"url": "https://a.example", "title": "T1", "snippet": "S1"}]
    assert seen["url"] == "https://api.search.brave.com/res/v1/web/search"
    assert seen["params"] == {"q": "symbiotes", "count": "1"}
    assert seen["headers"]["X-Subscription-Token"] == "BSA-test"
    assert seen["timeout"] == 7


def test_brave_api_key_comes_from_options_or_environment(monkeypatch) -> None:
    with pytest.raises(ValueError, match="Brave API key not set"):
        BraveSearchAPI(SearchOptions())

    monkeypatch.setenv("BRAVE_API_KEY", "BSA-env")
    provider = BraveSearchAPI(
        SearchOptions(provider_options={"brave_api": {"base_url": "https://proxy.example/"}})
    )

    assert provider._key == "BSA-env"
    assert provider._endpoint == "https://proxy.example/res/v1/web/search"


@pytest.mark.asyncio
async def test_searxng_queries_the_hosts_instance(monkeypatch) -> None:
    seen: dict = {}
    payload = {"results": [
        {"title": "T1", "url": "https://a.example", "content": "S1"},
        {"title": "T2", "url": "https://b.example", "content": "S2"},
        {"title": "T3", "url": "https://c.example", "content": "S3"},
    ]}
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(payload, seen))
    provider = SearxngSearch(
        SearchOptions(provider_options={"searxng": {"base_url": "http://localhost:8888/"}})
    )

    results = await provider.search("symbiotes", limit=2)

    assert [r["url"] for r in results] == ["https://a.example", "https://b.example"]
    assert results[0]["snippet"] == "S1"
    assert seen["url"] == "http://localhost:8888/search"
    assert seen["params"] == {"q": "symbiotes", "format": "json"}


def test_searxng_address_comes_from_options_or_environment(monkeypatch) -> None:
    with pytest.raises(ValueError, match="SearXNG address not set"):
        SearxngSearch(SearchOptions())

    monkeypatch.setenv("SEARXNG_URL", "http://search.internal")
    assert SearxngSearch(SearchOptions())._endpoint == "http://search.internal/search"
