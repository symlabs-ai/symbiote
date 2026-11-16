"""Link previews for the chat: title, site and thumbnail of an external page.

The page asks ``POST /api/preview`` with a URL; the server fetches that page
itself (the browser never talks to the cited site), reads its Open Graph tags
and keeps the thumbnail on disk, served from ``/api/preview/image/<key>``.

Every address fetched here came out of a model answer, so possibly from web
content. Each hop (the page, its redirects, the image and its redirects) goes
through the same guard as ``web_extract`` (public addresses only), and what
comes back is limited in size and, for the image, in type.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from symbiote.security.network import SSRFError, validate_url

logger = logging.getLogger("demo.previews")

MAX_URL_CHARS = 2000
MAX_HTML_BYTES = 600_000      # the <head> is all that is read
MAX_IMAGE_BYTES = 3_000_000
MAX_REDIRECTS = 4
TIMEOUT_S = 8.0
NEGATIVE_TTL_S = 600          # a page that gave nothing is retried after this
# Raster formats only: an SVG is a document, not a picture.
IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
}
# Names no person or project; sites serve their Open Graph tags to preview bots.
USER_AGENT = "Mozilla/5.0 (compatible; LinkPreview/1.0)"
_IMAGE_TAGS = ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src")
_TITLE_TAGS = ("og:title", "twitter:title")


class PreviewError(Exception):
    """This address gives no preview (refused, too big, wrong type, no tags)."""


class _HeadParser(HTMLParser):
    """Collects ``<meta property|name=... content=...>`` and ``<title>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            values = {k.lower(): (v or "") for k, v in attrs}
            key = (values.get("property") or values.get("name") or "").strip().lower()
            content = values.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def _decode(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.lower() == "charset" and value:
            charset = value.strip("\"' ")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def read_tags(html: str, page_url: str) -> dict[str, str]:
    """``{title, site, image}`` from a page's head; ``image`` may be ''."""
    parser = _HeadParser()
    try:
        parser.feed(html)
    except Exception:  # a broken page still gives what was read so far
        logger.debug("html parse stopped early for %s", page_url)
    image = next((parser.meta[k] for k in _IMAGE_TAGS if parser.meta.get(k)), "")
    if image:
        image = urljoin(page_url, image)
        if urlparse(image).scheme not in ("http", "https"):
            image = ""
    title = next((parser.meta[k] for k in _TITLE_TAGS if parser.meta.get(k)), "") or parser.title
    host = (urlparse(page_url).hostname or "").removeprefix("www.")
    return {
        "title": " ".join(title.split())[:200],
        "site": " ".join(parser.meta.get("og:site_name", "").split())[:80] or host,
        "image": image,
    }


class LinkPreviews:
    def __init__(
        self,
        cache_dir: Path,
        *,
        guard: Callable[[str], Any] = validate_url,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._dir = cache_dir
        self._guard = guard
        self._transport = transport
        self._inflight: dict[str, asyncio.Task[dict | None]] = {}
        self._failed: dict[str, float] = {}
        self._slots = asyncio.Semaphore(4)

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]

    # ── public ─────────────────────────────────────────────────────────

    async def resolve(self, url: str) -> dict | None:
        """The preview of ``url`` (``{url, title, site, image}``) or None."""
        url = url.strip()
        if len(url) > MAX_URL_CHARS or urlparse(url).scheme not in ("http", "https"):
            return None
        key = self.key(url)
        cached = self._read(key)
        if cached is not None:
            return self._public(key, cached)
        if time.monotonic() - self._failed.get(key, -NEGATIVE_TTL_S) < NEGATIVE_TTL_S:
            return None
        task = self._inflight.get(key)
        if task is None:
            # Two cards for the same address share one fetch.
            task = asyncio.ensure_future(self._build(key, url))
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        return await asyncio.shield(task)

    def image(self, key: str) -> tuple[Path, str] | None:
        """(file, content type) of a thumbnail already resolved, by its key."""
        if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
            return None
        meta = self._read(key)
        if meta is None:
            return None
        path = self._dir / f"{key}.img"
        return (path, meta["content_type"]) if path.exists() else None

    # ── internals ──────────────────────────────────────────────────────

    def _read(self, key: str) -> dict | None:
        try:
            return json.loads((self._dir / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _public(key: str, meta: dict) -> dict:
        return {
            "url": meta["url"],
            "title": meta["title"],
            "site": meta["site"],
            "image": f"/api/preview/image/{key}",
        }

    async def _build(self, key: str, url: str) -> dict | None:
        try:
            async with self._slots, httpx.AsyncClient(
                timeout=TIMEOUT_S, follow_redirects=False, transport=self._transport,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"},
            ) as client:
                page_url, content_type, body = await self._fetch(
                    client, url, MAX_HTML_BYTES, truncate=True
                )
                if "html" not in content_type.lower():
                    raise PreviewError(f"not a page ({content_type or 'no content type'})")
                tags = read_tags(_decode(body, content_type), page_url)
                if not tags["image"]:
                    raise PreviewError("the page declares no image")
                _, image_type, image = await self._fetch(
                    client, tags["image"], MAX_IMAGE_BYTES, truncate=False
                )
            image_type = image_type.split(";")[0].strip().lower()
            if image_type not in IMAGE_TYPES:
                raise PreviewError(f"image type not accepted ({image_type or 'none'})")
        except (PreviewError, SSRFError, httpx.HTTPError, ValueError, OSError) as exc:
            # SSRFError / ValueError: the guard refused the address or it is malformed.
            logger.info("no preview for %s: %s: %s", url[:120], type(exc).__name__, exc)
            self._failed[key] = time.monotonic()
            return None
        meta = {"url": url, "title": tags["title"], "site": tags["site"], "content_type": image_type}
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{key}.img").write_bytes(image)
        (self._dir / f"{key}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return self._public(key, meta)

    async def _fetch(
        self, client: httpx.AsyncClient, url: str, limit: int, *, truncate: bool
    ) -> tuple[str, str, bytes]:
        """GET following redirects by hand, so every hop passes the guard."""
        for _ in range(MAX_REDIRECTS + 1):
            if urlparse(url).scheme not in ("http", "https"):
                raise PreviewError("not an http(s) address")
            await asyncio.to_thread(self._guard, url)  # resolves DNS: off the loop
            async with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise PreviewError("redirect without a location")
                    url = urljoin(url, location)
                    continue
                if response.status_code >= 400:
                    raise PreviewError(f"HTTP {response.status_code}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        if not truncate:
                            raise PreviewError(f"larger than {limit} bytes")
                        chunks.append(chunk[: len(chunk) - (size - limit)])
                        break
                    chunks.append(chunk)
                return url, response.headers.get("content-type", ""), b"".join(chunks)
        raise PreviewError("too many redirects")
