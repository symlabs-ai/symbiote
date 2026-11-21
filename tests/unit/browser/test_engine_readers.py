"""The search engine readers (``duckduckgo``, ``yahoo``) and the ``chrome``
fetcher (B-107). No network: the pages are the ones the engines served on
2026-11-21, cut down to what the readers look at.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from symbiote.browser import FetchedPage, register
from symbiote.browser.config import SearchOptions
from symbiote.browser.search import fetch_chrome, registry
from symbiote.browser.search.fetch import FetchError, resolve_fetcher
from symbiote.browser.search.fetch_chrome import ChromeLikeFetcher
from symbiote.browser.search.outcomes import SearchRefusedError, SearchUnrelatedError
from symbiote.browser.search.providers.engine_readers import (
    DuckDuckGoReader,
    YahooReader,
)
from tests.support import ScriptedLLM, make_kernel

DDG_PAGE = """<div class="results">
<div class="result results_links web-result"><h2><a class="result__a"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fbackup%3Fb%3D1&amp;rut=x">Backup <b>em casa</b></a></h2>
  <a class="result__snippet">As   melhores práticas de backup</a></div>
<div class="result result--ad"><a class="result__a" href="https://ad.example">Anúncio de backup</a></div>
<div class="result"><a class="result__a" href="//duckduckgo.com/y.js?ad=1">Outro anúncio</a></div>
<div class="result"><a class="result__a" href="https://direct.example/casa">Direto: backup em casa</a></div>
</div>"""

YAHOO_PAGE = """<ol>
<li><div class="dd algo algo-sr"><div class="compTitle"><a
  href="https://r.search.yahoo.com/_ylt=A;_ylu=B/RV=2/RE=1/RO=10/RU=https%3a%2f%2fexample.org%2fbackup/RK=2/RS=x"><h3>Backup em casa</h3></a></div>
  <div class="compText"><p>Melhores práticas</p></div></div></li>
<li><div class="dd algo algo-sr"><div class="compTitle"><a href="https://example.org/dois"><h3>Práticas de backup</h3></a></div></div></li>
<li><div class="dd algo algo-sr"><div class="compTitle"><a href="https://images.search.yahoo.com/x"><h3>Imagens</h3></a></div></div></li>
</ol>"""

QUERY = "melhores práticas de backup em casa"


class Pages:
    """A fetcher that answers each engine with a fixed page."""

    def __init__(self, **by_host: tuple[int, str]) -> None:
        self.by_host = by_host
        self.calls: list[tuple[str, dict]] = []

    async def fetch(self, url, *, headers=None):
        self.calls.append((url, dict(headers or {})))
        key = "duckduckgo" if "duckduckgo" in url else "yahoo"
        status, body = self.by_host[key]
        return FetchedPage(status=status, body=body.encode(), url=url)


def _ddg(status: int, body: str, **own) -> DuckDuckGoReader:
    options = SearchOptions(provider_options={"duckduckgo": own})
    return DuckDuckGoReader(options, fetcher=Pages(duckduckgo=(status, body)))


def _yahoo(status: int, body: str) -> YahooReader:
    return YahooReader(SearchOptions(), fetcher=Pages(yahoo=(status, body)))


# ── reading the results ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duckduckgo_takes_the_target_out_of_the_redirect_and_skips_ads() -> None:
    reader = _ddg(200, DDG_PAGE)

    results = await reader.search(QUERY)

    assert results == [
        {
            "url": "https://example.org/backup?b=1",
            "title": "Backup em casa",
            "snippet": "As melhores práticas de backup",
        },
        {"url": "https://direct.example/casa", "title": "Direto: backup em casa", "snippet": ""},
    ]


@pytest.mark.asyncio
async def test_yahoo_reads_the_direct_and_the_redirected_link() -> None:
    results = await _yahoo(200, YAHOO_PAGE).search(QUERY)

    assert [(r["url"], r["title"]) for r in results] == [
        ("https://example.org/backup", "Backup em casa"),
        ("https://example.org/dois", "Práticas de backup"),
    ]
    assert results[0]["snippet"] == "Melhores práticas"


@pytest.mark.asyncio
async def test_the_query_goes_encoded_with_browser_headers_and_the_hosts_options() -> None:
    reader = _ddg(200, DDG_PAGE, region="br-pt", accept_language="pt-BR,pt;q=0.9")

    await reader.search("café & backup em casa", limit=1)

    url, headers = reader._fetcher.calls[0]
    assert url == "https://html.duckduckgo.com/html/?q=caf%C3%A9+%26+backup+em+casa&kl=br-pt"
    assert headers["Accept-Language"] == "pt-BR,pt;q=0.9"
    assert headers["Accept"].startswith("text/html")
    assert _yahoo(200, "").search_url("a b") == "https://search.yahoo.com/search?p=a+b"


@pytest.mark.asyncio
async def test_the_limit_cuts_the_list() -> None:
    assert len(await _ddg(200, DDG_PAGE).search(QUERY, limit=1)) == 1


# ── the four ways a search ends ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (202, "", "refused (HTTP 202)"),
        (403, "", "refused (HTTP 403)"),
        (200, '<div class="anomaly-modal">prove you are human</div>', "refused (challenge)"),
        (200, "<html><body>Please solve this CAPTCHA</body></html>", "refused (captcha)"),
        (200, "<html><body>a new layout</body></html>", "no results to read"),
    ],
)
async def test_a_refusal_is_told_apart_from_no_results(status, body, reason) -> None:
    with pytest.raises(SearchRefusedError, match=reason.replace("(", r"\(").replace(")", r"\)")):
        await _ddg(status, body).search(QUERY)


@pytest.mark.asyncio
async def test_a_page_that_says_nothing_was_found_is_no_results() -> None:
    nothing = '<div class="no-results__container result__title">No results found for <b>zzzz</b></div>'
    assert await _ddg(200, nothing).search(QUERY) == []
    assert await _yahoo(200, "<p>We did not find results for: zzzz</p>").search(QUERY) == []


@pytest.mark.asyncio
async def test_a_full_page_about_something_else_is_unrelated() -> None:
    page = '<div class="result"><a class="result__a" href="https://x.example/">Passagens aéreas baratas</a></div>'

    with pytest.raises(SearchUnrelatedError, match="unrelated to the query"):
        await _ddg(200, page).search(QUERY)

    assert len(await _ddg(200, page, min_relevance=0).search(QUERY)) == 1


# ── through the kernel: an explicit choice, in the host's order ─────────


@pytest.fixture()
def kernel(tmp_path: Path):
    k = make_kernel(tmp_path, ScriptedLLM("ok"))
    yield k
    k.shutdown()


async def _search(kernel, query: str):
    bot = kernel.create_symbiote(name="bot", role="t")
    kernel.environment.configure(symbiote_id=bot.id, tools=["web_search"])
    return await kernel.tool_gateway.execute_async(
        symbiote_id=bot.id, session_id=None, tool_id="web_search", params={"query": query},
        timeout=10.0,
    )


@pytest.mark.asyncio
async def test_duckduckgo_refuses_and_yahoo_answers(kernel) -> None:
    pages = Pages(duckduckgo=(202, ""), yahoo=(200, YAHOO_PAGE))

    register(kernel, search_backend=["duckduckgo", "yahoo"], search_options={"fetcher": pages})
    result = await _search(kernel, QUERY)

    assert result.success, result.error
    assert result.output["source"] == "yahoo"
    assert result.output["attempts"] == ["duckduckgo: refused (HTTP 202)"]
    assert [call[0].split("/")[2] for call in pages.calls] == ["html.duckduckgo.com", "search.yahoo.com"]


@pytest.mark.asyncio
async def test_when_both_refuse_the_model_is_told_the_search_did_not_run(kernel) -> None:
    unrelated = '<div class="dd algo algo-sr"><div class="compTitle"><a href="https://x.example/"><h3>Passagens</h3></a></div></div>'
    pages = Pages(duckduckgo=(202, ""), yahoo=(200, unrelated))

    register(kernel, search_backend=["duckduckgo", "yahoo"], search_options={"fetcher": pages})
    result = await _search(kernel, QUERY)

    assert not result.success
    assert "duckduckgo: refused (HTTP 202)" in result.error
    assert "yahoo: results unrelated to the query" in result.error


def test_no_engine_is_read_unless_the_host_names_it(kernel) -> None:
    register(kernel, extract_backend="http")

    assert not kernel.tool_gateway.has_tool("web_search")
    assert {"duckduckgo", "yahoo"} <= set(registry.provider_names("search"))


# ── the chrome fetcher ──────────────────────────────────────────────────


class _Response:
    def __init__(self, status: int, headers: dict, body: bytes = b"") -> None:
        self.status_code = status
        self.headers = headers
        self._body = body
        self.closed = False

    async def aiter_content(self):
        for start in range(0, len(self._body), 400):
            yield self._body[start : start + 400]

    async def aclose(self) -> None:
        self.closed = True


class _Session:
    routes: dict[str, _Response] = {}
    opened: list[dict] = []
    seen: list[tuple[str, dict]] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def get(self, url, **kwargs):
        type(self).seen.append((url, kwargs))
        answer = type(self).routes[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture()
def session(monkeypatch):
    _Session.routes, _Session.opened, _Session.seen = {}, [], []
    monkeypatch.setattr(fetch_chrome, "_lazy_session", lambda: _Session)
    return _Session


@pytest.mark.asyncio
async def test_the_chrome_fetcher_imitates_chrome_and_checks_every_hop(session) -> None:
    session.routes = {
        "https://a.example/": _Response(302, {"location": "https://b.example/end"}),
        "https://b.example/end": _Response(200, {"content-type": "text/html"}, b"<p>hi</p>"),
    }
    checked: list[str] = []
    fetcher = ChromeLikeFetcher(check=checked.append)

    page = await fetcher.fetch("https://a.example/", headers={"Accept": "text/html"})

    assert (page.status, page.body, page.url) == (200, b"<p>hi</p>", "https://b.example/end")
    assert session.opened[0]["impersonate"] == "chrome"
    assert checked == ["https://a.example/", "https://b.example/end"]
    assert all(
        kw["allow_redirects"] is False and kw["headers"] == {"Accept": "text/html"}
        for _url, kw in session.seen
    )
    assert all(r.closed for r in session.routes.values())


@pytest.mark.asyncio
async def test_the_chrome_fetcher_stops_at_the_size_limit_and_wraps_errors(session) -> None:
    session.routes = {
        "https://a.example/big": _Response(200, {}, b"z" * 5000),
        "https://a.example/down": OSError("connection reset"),
    }
    fetcher = ChromeLikeFetcher(max_bytes=1000, check=lambda url: None)

    page = await fetcher.fetch("https://a.example/big")
    assert (len(page.body), page.truncated) == (1000, True)

    with pytest.raises(FetchError, match="connection reset"):
        await fetcher.fetch("https://a.example/down")


def test_the_chrome_fetcher_is_chosen_by_name_and_says_what_it_needs(session, monkeypatch) -> None:
    assert isinstance(resolve_fetcher(SearchOptions(fetcher="chrome")), ChromeLikeFetcher)

    monkeypatch.undo()
    real_import = builtins.__import__

    def no_curl(name, *args, **kwargs):
        if name.startswith("curl_cffi"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_curl)
    with pytest.raises(ImportError, match=r'pip install "symbiote\[web\]"'):
        resolve_fetcher(SearchOptions(fetcher="chrome"))
