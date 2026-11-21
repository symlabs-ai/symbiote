"""Reading and searching the web without a paid service (B-107): who brings
the page is the host's choice, ``http`` reads a page itself, ``search_backend``
takes a list, and ``web_search`` keeps its outcomes apart.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from symbiote.browser import FetchedPage, register, register_fetcher
from symbiote.browser.config import SearchOptions
from symbiote.browser.search import fetch as fetch_mod
from symbiote.browser.search.fetch import FetchError, HttpFetcher, resolve_fetcher
from symbiote.browser.search.html_text import page_text, page_title, parse_html
from symbiote.browser.search.outcomes import (
    SearchRefusedError,
    SearchUnrelatedError,
    query_coverage,
    require_related,
)
from symbiote.browser.search.providers.http_extract import (
    ACCEPT,
    HttpExtract,
    clip_middle,
)
from symbiote.browser.search.tools import web_search_descriptor
from tests.support import ScriptedLLM, make_kernel

PAGE = "https://example.com/page"
HTML = """<!doctype html><html><head><title>The  Guide</title><style>p{color:red}</style></head>
<body><nav><a href="/menu">Menu</a></nav>
<main><h1>Guide</h1><p>Read <a href="/next?a=1">the next part</a>.<br>Second line</p>
<ul><li>one</li><li>two</li></ul>
<pre>def f():
    return 1</pre><script>track()</script></main>
<footer>Footer text</footer></body></html>"""


@pytest.fixture()
def kernel(tmp_path: Path):
    k = make_kernel(tmp_path, ScriptedLLM("ok"))
    yield k
    k.shutdown()


@pytest.fixture(autouse=True)
def _clean_fetchers(monkeypatch):
    monkeypatch.setattr(fetch_mod, "_FETCHERS", dict(fetch_mod._FETCHERS))


class HostFetcher:
    """A host's own way of bringing a page (a proxy, an app elsewhere)."""

    def __init__(self, page: FetchedPage | Exception) -> None:
        self.page = page
        self.calls: list[tuple[str, dict]] = []

    async def fetch(self, url, *, headers=None):
        self.calls.append((url, dict(headers or {})))
        if isinstance(self.page, Exception):
            raise self.page
        return self.page


def _page(body: str | bytes, content_type: str, *, status: int = 200, url: str = PAGE) -> FetchedPage:
    raw = body if isinstance(body, bytes) else body.encode()
    return FetchedPage(status=status, body=raw, url=url, headers={"Content-Type": content_type})


async def _run(kernel, tool_id: str, **params):
    bot = kernel.create_symbiote(name=f"bot-{tool_id}", role="t")
    kernel.environment.configure(symbiote_id=bot.id, tools=[tool_id])
    return await kernel.tool_gateway.execute_async(
        symbiote_id=bot.id, session_id=None, tool_id=tool_id, params=params, timeout=10.0
    )


# ── the host says who brings the page ───────────────────────────────────


@pytest.mark.asyncio
async def test_the_http_extractor_reads_through_the_hosts_fetcher(kernel) -> None:
    fetcher = HostFetcher(_page(HTML, "text/html; charset=utf-8"))

    register(kernel, extract_backend="http", search_options={"fetcher": fetcher})
    result = await _run(kernel, "web_extract", url=PAGE)

    assert result.success, result.error
    assert fetcher.calls == [(PAGE, {"Accept": ACCEPT})]
    assert ACCEPT.startswith("text/markdown")
    out = result.output
    assert (out["url"], out["title"]) == (PAGE, "The Guide")
    assert out["metadata"] == {"content_type": "text/html", "status": 200}
    assert "truncated" not in out


def test_a_fetcher_is_an_object_or_a_registered_name() -> None:
    mine = HostFetcher(_page("", "text/plain"))
    register_fetcher("mine", lambda options: mine)

    assert resolve_fetcher(SearchOptions(fetcher=mine)) is mine
    assert resolve_fetcher(SearchOptions(fetcher="mine")) is mine
    assert isinstance(resolve_fetcher(SearchOptions()), HttpFetcher)
    with pytest.raises(ValueError, match="Unknown fetcher 'nope'"):
        resolve_fetcher(SearchOptions(fetcher="nope"))
    with pytest.raises(TypeError, match=r"async fetch\(url"):
        resolve_fetcher(SearchOptions(fetcher=object()))


@pytest.mark.asyncio
async def test_a_page_that_redirected_to_a_denied_site_is_refused(kernel) -> None:
    fetcher = HostFetcher(_page("secret", "text/plain", url="https://blocked.example/x"))

    register(
        kernel,
        extract_backend="http",
        search_options={"fetcher": fetcher},
        policy={"blocklist": ["blocked.example"]},
    )
    result = await _run(kernel, "web_extract", url=PAGE)

    assert not result.success
    assert "blocked.example" in result.error


@pytest.mark.asyncio
async def test_a_slow_fetcher_is_cut_at_the_timeout() -> None:
    import asyncio

    class Slow:
        async def fetch(self, url, *, headers=None):
            await asyncio.sleep(5)

    extractor = HttpExtract(SearchOptions(timeout_seconds=0.05), fetcher=Slow())

    with pytest.raises(FetchError, match="timed out"):
        await extractor.extract(PAGE)


# ── what the http extractor returns, by content type ────────────────────


async def _extract(page: FetchedPage, **options) -> dict:
    return await HttpExtract(SearchOptions(**options), fetcher=HostFetcher(page)).extract(PAGE)


@pytest.mark.asyncio
async def test_html_becomes_light_markdown_without_the_page_chrome() -> None:
    out = await _extract(_page(HTML, "text/html"))

    assert out["content"] == (
        "# Guide\n\n"
        "Read [the next part](https://example.com/next?a=1).\nSecond line\n\n"
        "- one\n- two\n\n"
        "```\ndef f():\n    return 1\n```"
    )


@pytest.mark.asyncio
async def test_markdown_text_and_json_are_returned_as_they_are() -> None:
    markdown = await _extract(_page("# Notes\n\nBody *text*.", "text/markdown; charset=utf-8"))
    plain = await _extract(_page("just text\n", "text/plain"))
    data = await _extract(_page('{"a": [1, 2]}', "application/json"))

    assert (markdown["title"], markdown["content"]) == ("Notes", "# Notes\n\nBody *text*.")
    assert plain["content"] == "just text"
    assert data["content"] == '{\n  "a": [\n    1,\n    2\n  ]\n}'


@pytest.mark.asyncio
async def test_the_charset_of_the_page_is_respected() -> None:
    header = await _extract(_page("ação".encode("latin-1"), "text/plain; charset=iso-8859-1"))
    meta = await _extract(
        _page('<html><head><meta charset="latin-1"></head><body><p>ação</p></body></html>'.encode("latin-1"), "text/html")
    )

    assert header["content"] == "ação"
    assert meta["content"] == "ação"


@pytest.mark.asyncio
async def test_what_cannot_be_read_raises_so_a_chain_moves_on() -> None:
    with pytest.raises(ValueError, match="application/pdf"):
        await _extract(_page(b"%PDF-1.7", "application/pdf"))
    with pytest.raises(FetchError, match="HTTP 403"):
        await _extract(_page("denied", "text/html", status=403))


@pytest.mark.asyncio
async def test_the_address_returned_is_the_one_the_answer_came_from() -> None:
    out = await _extract(_page("moved here", "text/plain", url="https://example.com/final"))

    assert out["url"] == "https://example.com/final"


@pytest.mark.asyncio
async def test_a_long_page_keeps_its_beginning_and_its_end() -> None:
    text = "BEGIN " + "x" * 5000 + " END"

    out = await _extract(_page(text, "text/plain"), max_content_chars=400)

    assert out["content"].startswith("BEGIN xxx")
    assert out["content"].endswith("xxx END")
    assert "characters left out of the middle" in out["content"]
    assert (out["truncated"], out["total_chars"]) == (True, len(text))


def test_clip_middle_leaves_a_short_text_alone() -> None:
    assert clip_middle("short", 100) == ("short", False)
    assert clip_middle("anything", 0) == ("anything", False)
    clipped, cut = clip_middle("a" * 60 + "b" * 60, 40)
    assert cut and clipped.startswith("a" * 30) and clipped.endswith("b" * 10)


@pytest.mark.asyncio
async def test_a_page_larger_than_the_byte_limit_says_it_was_cut() -> None:
    out = await _extract(_page("y" * 500, "text/plain"), max_page_bytes=100)

    assert out["content"].startswith("y" * 100 + "\n\n[The page is larger")
    assert out["truncated"] is True


# ── the package's own client ────────────────────────────────────────────


def _transport(routes: dict[str, httpx.Response], seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return routes[str(request.url)]

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_the_plain_client_follows_redirects_checking_every_hop() -> None:
    seen: list[str] = []
    checked: list[str] = []
    routes = {
        "https://a.example/": httpx.Response(302, headers={"Location": "/b"}),
        "https://a.example/b": httpx.Response(301, headers={"Location": "https://c.example/end"}),
        "https://c.example/end": httpx.Response(200, text="arrived", headers={"Content-Type": "text/plain"}),
    }
    fetcher = HttpFetcher(check=checked.append, transport=_transport(routes, seen))

    page = await fetcher.fetch("https://a.example/", headers={"Accept": "text/plain"})

    assert (page.status, page.body, page.url) == (200, b"arrived", "https://c.example/end")
    assert page.header("content-type") == "text/plain"
    assert checked == seen == list(routes)


@pytest.mark.asyncio
async def test_a_hop_to_an_internal_address_is_not_opened() -> None:
    seen: list[str] = []
    routes = {
        "https://a.example/": httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"}),
    }
    fetcher = HttpFetcher(transport=_transport(routes, seen))
    # a.example does not resolve: let the first hop pass, keep the real check after it
    real = fetch_mod.check_hop
    fetcher._check = lambda url: None if "a.example" in url else real(url)

    with pytest.raises(Exception, match="127.0.0.1"):
        await fetcher.fetch("https://a.example/")

    assert seen == ["https://a.example/"]


def test_only_http_addresses_pass_the_hop_check() -> None:
    with pytest.raises(FetchError, match="not an http"):
        fetch_mod.check_hop("file:///etc/passwd")


@pytest.mark.asyncio
async def test_the_plain_client_stops_at_the_size_limit_and_at_redirect_loops() -> None:
    big = {"https://a.example/": httpx.Response(200, content=b"z" * 5000)}
    loop = {"https://a.example/": httpx.Response(302, headers={"Location": "/"})}

    page = await HttpFetcher(max_bytes=1000, check=lambda url: None, transport=_transport(big, [])).fetch(
        "https://a.example/"
    )
    assert (len(page.body), page.truncated) == (1000, True)

    with pytest.raises(FetchError, match="redirects"):
        await HttpFetcher(check=lambda url: None, transport=_transport(loop, [])).fetch("https://a.example/")


# ── search providers in the host's order ────────────────────────────────


class Engine:
    def __init__(self, name: str, answer) -> None:
        self.name = name
        self.answer = answer
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 5):
        self.queries.append(query)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


HIT = [{"url": "https://example.com/a", "title": "A", "snippet": "about it"}]


@pytest.mark.asyncio
async def test_a_list_of_search_backends_is_tried_in_order(kernel) -> None:
    first = Engine("first", SearchRefusedError("refused (HTTP 202)"))
    second = Engine("second", HIT)
    third = Engine("third", HIT)

    register(kernel, search_backend=[first, second, third])
    result = await _run(kernel, "web_search", query="symbiotes")

    assert result.success, result.error
    assert result.output == {
        "results": HIT,
        "count": 1,
        "outcome": "results",
        "source": "second",
        "attempts": ["first: refused (HTTP 202)"],
    }
    assert (first.queries, second.queries, third.queries) == (["symbiotes"], ["symbiotes"], [])


@pytest.mark.asyncio
async def test_when_every_source_fails_the_search_did_not_run(kernel) -> None:
    register(
        kernel,
        search_backend=[
            Engine("first", SearchRefusedError("refused (HTTP 202)")),
            Engine("second", SearchUnrelatedError("results unrelated to the query")),
            Engine("third", RuntimeError("boom")),
        ],
    )
    result = await _run(kernel, "web_search", query="symbiotes")

    assert not result.success
    for part in (
        "first: refused (HTTP 202)",
        "second: results unrelated to the query",
        "third: RuntimeError: boom",
    ):
        assert part in result.error


@pytest.mark.asyncio
async def test_no_results_is_not_a_failure(kernel) -> None:
    register(
        kernel,
        search_backend=[Engine("first", SearchRefusedError("refused")), Engine("second", [])],
    )
    result = await _run(kernel, "web_search", query="zzzz")

    assert result.success, result.error
    assert (result.output["outcome"], result.output["count"]) == ("no_results", 0)
    assert "found nothing" in result.output["note"]
    assert result.output["attempts"] == ["first: refused", "second: no results"]


@pytest.mark.asyncio
async def test_a_single_provider_gets_the_outcome_too(kernel) -> None:
    register(kernel, search_backend=Engine("only", []))
    empty = await _run(kernel, "web_search", query="zzzz")

    assert empty.output["outcome"] == "no_results"
    assert "source" not in empty.output


def test_an_empty_list_of_search_backends_is_refused(kernel) -> None:
    with pytest.raises(ValueError, match="search_backend list cannot be empty"):
        register(kernel, search_backend=[])


# ── results unrelated to the query ──────────────────────────────────────


def _result(title: str, snippet: str) -> dict:
    return {"url": "https://example.org/", "title": title, "snippet": snippet}


def test_coverage_measures_how_much_of_the_query_is_in_the_results() -> None:
    query = "melhores práticas de backup em casa"
    good = [_result("Backup doméstico", "As melhores praticas para guardar seus arquivos em casa")]
    unrelated = [_result("Melhores Destinos", "Passagens aéreas baratas")]

    assert query_coverage(query, good) == 1.0
    assert query_coverage(query, unrelated) == 0.25
    assert query_coverage("a de em", unrelated) == 1.0


def test_a_full_list_about_something_else_is_a_refusal() -> None:
    unrelated = [_result("Melhores Destinos", "Passagens aéreas baratas")]

    with pytest.raises(SearchUnrelatedError, match="unrelated to the query"):
        require_related("melhores práticas de backup em casa", unrelated)
    require_related("melhores práticas de backup em casa", unrelated, minimum=0)
    require_related("melhores práticas de backup em casa", [])
    assert issubclass(SearchUnrelatedError, SearchRefusedError)


# ── the description of web_search ───────────────────────────────────────


def _description(kernel) -> str:
    return next(
        d.description for d in kernel.tool_gateway.get_descriptors() if d.tool_id == "web_search"
    )


def test_web_search_names_only_the_reading_tools_that_exist(kernel, tmp_path) -> None:
    register(kernel, search_backend=Engine("only", HIT))
    alone = _description(kernel)
    other = make_kernel(tmp_path / "other", ScriptedLLM("ok"))
    register(other, search_backend=Engine("only", HIT), extract_backend="http")
    with_extract = _description(other)
    other.shutdown()

    assert "browser_" not in alone and "web_extract" not in alone
    assert "read in detail via web_extract." in with_extract
    assert "browser_" not in with_extract
    assert "browser_navigate / browser_snapshot" in web_search_descriptor(
        ["browser_navigate", "browser_snapshot"]
    ).description
    for text in (alone, with_extract):
        assert 'outcome "no_results"' in text


# ── cache ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nothing_is_cached_unless_the_host_asks(kernel) -> None:
    engine = Engine("only", HIT)
    register(kernel, search_backend=engine)

    await _run(kernel, "web_search", query="same")
    await _run(kernel, "web_search", query="same")

    assert engine.queries == ["same", "same"]


@pytest.mark.asyncio
async def test_with_a_ttl_the_same_question_does_not_go_out_again(kernel) -> None:
    engine = Engine("only", HIT)
    fetcher = HostFetcher(_page("text", "text/plain"))
    register(
        kernel,
        search_backend=engine,
        extract_backend="http",
        search_options={"fetcher": fetcher, "cache_ttl_seconds": 60},
    )

    first = await _run(kernel, "web_search", query="same")
    again = await _run(kernel, "web_search", query="same")
    await _run(kernel, "web_search", query="other")
    await _run(kernel, "web_extract", url=PAGE)
    await _run(kernel, "web_extract", url=PAGE)

    assert first.output == again.output
    assert engine.queries == ["same", "other"]
    assert len(fetcher.calls) == 1


# ── the HTML reader ─────────────────────────────────────────────────────


def test_the_tree_survives_broken_markup() -> None:
    root = parse_html('<div class="a b"><p>one<p>two</div><span>after</span>')

    box = root.find("div", "a")
    assert box is not None and box.classes == ["a", "b"]
    assert [p.text() for p in root.find_all("p")] == ["one", "two"]
    items = parse_html("<ul><li>a<li>b<ul><li>c</ul></ul>").find_all("li")
    assert [li.text() for li in items] == ["a", "b c", "c"]
    assert root.find("span").text() == "after"
    assert root.find("table") is None


def test_the_title_falls_back_to_the_first_heading() -> None:
    assert page_title(parse_html("<body><h1>Only heading</h1></body>")) == "Only heading"
    assert page_title(parse_html("<body><p>none</p></body>")) == ""


def test_text_outside_main_is_read_when_there_is_no_main() -> None:
    root = parse_html(
        "<body><header>Site</header><article><p>First</p></article>"
        "<article><p>Second</p></article><aside>Ads</aside></body>"
    )

    assert page_text(root) == "Site\n\nFirst\n\nSecond"
