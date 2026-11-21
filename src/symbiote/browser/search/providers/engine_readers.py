"""``duckduckgo`` and ``yahoo`` — web search by reading the results page of a
search engine. No key, no service of the host's.

    register(kernel, search_backend=["duckduckgo", "yahoo"],
             search_options={"fetcher": "chrome"})

**Read this before choosing them.**

- They are never chosen for the host: reading a results page this way goes
  against the terms of the engines, and it is the host that answers for it.
- It is best effort. The engines refuse what does not look like a person
  with a browser: by the address the request leaves from (a datacenter is
  refused early; a home connection, at one person's pace, is not) and by how
  the connection is opened. The package's plain client is refused by
  DuckDuckGo every time; use the ``"chrome"`` fetcher or the host's own.
- The selectors were taken from real pages on 2026-11-21 and break when an
  engine changes its HTML. That is why the readers tell a page they could
  not read from a search that found nothing, and why they go in a list.

Each reader brings one page through the fetcher and ends in one of four
ways: results; ``[]`` when the page itself says nothing was found;
``SearchRefusedError`` when the engine refused or the page could not be
read; ``SearchUnrelatedError`` when it came full of results about something
else (``min_relevance``).
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from symbiote.browser.config import SearchOptions
from symbiote.browser.search.fetch import fetch_page, resolve_fetcher
from symbiote.browser.search.html_text import Node, parse_html
from symbiote.browser.search.outcomes import SearchRefusedError, require_related

_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_DEFAULT_LANGUAGE = "en-US,en;q=0.9"


def _web(url: str) -> str | None:
    """``url`` when it is an http(s) address, else nothing."""
    return url if urlparse(url).scheme in ("http", "https") else None


def _add(out: list[dict[str, Any]], url: str | None, title: str, snippet: str) -> None:
    if url and title and all(r["url"] != url for r in out):
        out.append({"url": url, "title": title, "snippet": snippet})


class EngineReader:
    """SearchProvider that reads one engine's results page."""

    name: ClassVar[str]
    # Text an engine puts on the page when it refuses -> what to call it.
    block_marks: ClassVar[dict[str, str]] = {}
    # Text an engine puts on the page when the search found nothing.
    empty_marks: ClassVar[tuple[str, ...]] = ()

    def __init__(self, options: SearchOptions | None = None, *, fetcher: Any = None) -> None:
        self._options = options or SearchOptions()
        self._own = self._options.options_for(self.name)
        self._fetcher = fetcher if fetcher is not None else resolve_fetcher(self._options)

    def search_url(self, query: str) -> str:
        raise NotImplementedError

    def parse(self, root: Node) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        limit = max(1, min(20, int(limit)))
        page = await fetch_page(
            self._fetcher,
            self.search_url(query),
            headers={
                "Accept": _ACCEPT,
                "Accept-Language": str(self._own.get("accept_language") or _DEFAULT_LANGUAGE),
            },
            timeout=self._options.timeout_seconds,
            max_bytes=self._options.max_page_bytes,
        )
        # 202 is the "accepted, no answer" DuckDuckGo sends with its challenge.
        if page.status == 202 or page.status >= 400:
            raise SearchRefusedError(f"refused (HTTP {page.status})")
        body = page.body.decode("utf-8", errors="replace")
        for mark, label in self.block_marks.items():
            if mark in body:
                raise SearchRefusedError(f"refused ({label})")
        results = self.parse(parse_html(body))
        if not results:
            lowered = body.lower()
            if "captcha" in lowered or "unusual traffic" in lowered:
                raise SearchRefusedError("refused (captcha)")
            if any(mark.lower() in lowered for mark in self.empty_marks):
                return []
            raise SearchRefusedError("answered with a page that has no results to read")
        require_related(
            query, results, minimum=float(self._own.get("min_relevance", self._options.min_relevance))
        )
        return results[:limit]


class DuckDuckGoReader(EngineReader):
    """``html.duckduckgo.com/html/``, the version of the page without JavaScript."""

    name = "duckduckgo"
    block_marks: ClassVar[dict[str, str]] = {"anomaly-modal": "challenge", "anomaly.js": "challenge"}
    # Seen on 2026-11-21: <div class="no-results__container">No results found for ...
    empty_marks: ClassVar[tuple[str, ...]] = ("no-results", "No results found for")

    def search_url(self, query: str) -> str:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        region = self._own.get("region")  # e.g. "br-pt", "us-en"
        return f"{url}&kl={quote_plus(str(region))}" if region else url

    def parse(self, root: Node) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in root.find_all("div", "result"):
            if "result--ad" in item.classes:
                continue
            link = item.find("a", "result__a")
            if link is None:
                continue
            snippet = item.find(cls="result__snippet")
            _add(out, self._target(link.get("href")), link.text(), snippet.text() if snippet else "")
        return out

    @staticmethod
    def _target(href: str) -> str | None:
        """A result goes through ``duckduckgo.com/l/?uddg=<target>``; an ad
        (``/y.js``) has no target."""
        absolute = f"https:{href}" if href.startswith("//") else href
        parsed = urlparse(absolute)
        if (parsed.hostname or "").endswith("duckduckgo.com"):
            target = parse_qs(parsed.query).get("uddg")
            return _web(target[0]) if target else None
        return _web(absolute)


class YahooReader(EngineReader):
    """``search.yahoo.com/search``."""

    name = "yahoo"
    # Not seen live: on 2026-11-21 Yahoo answered a query with no match with
    # loosely related results, which end as unrelated, not as no results.
    empty_marks: ClassVar[tuple[str, ...]] = ("We did not find results for",)

    def search_url(self, query: str) -> str:
        return f"https://search.yahoo.com/search?p={quote_plus(query)}"

    def parse(self, root: Node) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in root.find_all("div", "algo-sr"):
            box = item.find("div", "compTitle")
            link = box.find("a") if box is not None else None
            if link is None:
                continue
            heading = item.find("h3")
            snippet = item.find("div", "compText")
            _add(
                out,
                self._target(link.get("href")),
                (heading or link).text(),
                snippet.text() if snippet else "",
            )
        return out

    @staticmethod
    def _target(href: str) -> str | None:
        """Without JavaScript a result goes through
        ``r.search.yahoo.com/.../RU=<target>/RK=...``; Yahoo's own links
        (images, videos) are not results."""
        if "/RU=" in href:
            encoded = href.split("/RU=", 1)[1].split("/RK=", 1)[0]
            return _web(unquote(encoded))
        if (urlparse(href).hostname or "").endswith("yahoo.com"):
            return None
        return _web(href)
