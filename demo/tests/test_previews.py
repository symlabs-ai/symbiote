"""Link previews: what the demo server fetches for a link shown in the chat,
and what it refuses to fetch. No network: httpx answers from a fake site."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from demo import previews as previews_mod
from demo.previews import LinkPreviews, read_tags

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PAGE = """<!doctype html><html><head>
<title>  Título   da aba </title>
<meta property="og:title" content="AI.CUBE fecha US$ 50 milhões &amp; mais">
<meta property="og:site_name" content="Exame">
<meta property="og:image" content="/img/capa.png">
</head><body>corpo</body></html>"""


class Site:
    """A fake web: path → (status, headers, body). Records what was asked."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        self.routes = routes
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(str(request.url))
        status, headers, body = self.routes.get(str(request.url), (404, {}, b""))
        return httpx.Response(status, headers=headers, content=body)


def make(tmp_path: Path, site: Site, **kwargs: object) -> LinkPreviews:
    kwargs.setdefault("guard", lambda url: url)
    return LinkPreviews(tmp_path / "previews", transport=httpx.MockTransport(site), **kwargs)  # type: ignore[arg-type]


HTML = {"content-type": "text/html; charset=utf-8"}


@pytest.fixture
def site() -> Site:
    return Site({
        "https://exame.com/noticia": (200, HTML, PAGE.encode()),
        "https://exame.com/img/capa.png": (200, {"content-type": "image/png"}, PNG),
    })


@pytest.mark.asyncio
async def test_a_page_with_og_tags_gives_title_site_and_a_local_thumbnail(tmp_path: Path, site: Site) -> None:
    previews = make(tmp_path, site)
    got = await previews.resolve("https://exame.com/noticia")
    assert got is not None
    assert got["title"] == "AI.CUBE fecha US$ 50 milhões & mais"
    assert got["site"] == "Exame"
    # The browser gets a path on the demo server, never the site's address.
    key = previews.key("https://exame.com/noticia")
    assert got["image"] == f"/api/preview/image/{key}"
    path, content_type = previews.image(key)  # type: ignore[misc]
    assert path.read_bytes() == PNG and content_type == "image/png"

    # A second ask is answered from disk, even by a new instance.
    asked = len(site.asked)
    assert await previews.resolve("https://exame.com/noticia") == got
    assert await make(tmp_path, site).resolve("https://exame.com/noticia") == got
    assert len(site.asked) == asked


@pytest.mark.asyncio
async def test_no_image_no_preview_and_the_failure_is_not_refetched_at_once(tmp_path: Path) -> None:
    site = Site({"https://blog.example/post": (200, HTML, b"<html><head><title>So texto</title></head></html>")})
    previews = make(tmp_path, site)
    assert await previews.resolve("https://blog.example/post") is None
    assert await previews.resolve("https://blog.example/post") is None
    assert site.asked == ["https://blog.example/post"]


@pytest.mark.parametrize(
    ("image_headers", "image_body"),
    [
        ({"content-type": "image/svg+xml"}, b"<svg onload='alert(1)'/>"),   # a document, not a picture
        ({"content-type": "text/html"}, b"<script>alert(1)</script>"),
        ({"content-type": "image/png"}, b"\x00" * (previews_mod.MAX_IMAGE_BYTES + 1)),  # too big
    ],
)
@pytest.mark.asyncio
async def test_an_image_of_the_wrong_kind_or_size_is_refused(
    tmp_path: Path, image_headers: dict[str, str], image_body: bytes
) -> None:
    site = Site({
        "https://exame.com/noticia": (200, HTML, PAGE.encode()),
        "https://exame.com/img/capa.png": (200, image_headers, image_body),
    })
    previews = make(tmp_path, site)
    assert await previews.resolve("https://exame.com/noticia") is None
    assert previews.image(previews.key("https://exame.com/noticia")) is None


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "ftp://x.example/a", "", "x" * 3000])
@pytest.mark.asyncio
async def test_only_http_addresses_are_looked_at(tmp_path: Path, site: Site, url: str) -> None:
    assert await make(tmp_path, site).resolve(url) is None
    assert site.asked == []


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8010/api/status", "http://169.254.169.254/latest/meta-data/", "http://localhost/x",
     "http://10.0.0.5/admin", "http://[::1]/"],
)
@pytest.mark.asyncio
async def test_internal_addresses_are_never_fetched(tmp_path: Path, url: str) -> None:
    site = Site({})
    previews = LinkPreviews(tmp_path / "p", transport=httpx.MockTransport(site))  # the real guard
    assert await previews.resolve(url) is None
    assert site.asked == []


@pytest.mark.asyncio
async def test_a_redirect_to_an_internal_address_stops_there(tmp_path: Path) -> None:
    # 93.184.216.34 is a public address: the real guard lets the first hop through.
    site = Site({
        "http://93.184.216.34/go": (302, {"location": "http://127.0.0.1:8010/api/status"}, b""),
        "http://93.184.216.34/page": (200, HTML, PAGE.replace("/img/capa.png", "http://192.168.0.1/x.png").encode()),
    })
    previews = LinkPreviews(tmp_path / "p", transport=httpx.MockTransport(site))
    assert await previews.resolve("http://93.184.216.34/go") is None
    assert await previews.resolve("http://93.184.216.34/page") is None  # the image is internal
    assert site.asked == ["http://93.184.216.34/go", "http://93.184.216.34/page"]


@pytest.mark.asyncio
async def test_redirects_are_followed_a_few_times_only(tmp_path: Path) -> None:
    routes = {f"https://a.example/{i}": (302, {"location": f"/{i + 1}"}, b"") for i in range(10)}
    site = Site(routes)
    assert await make(tmp_path, site).resolve("https://a.example/0") is None
    assert len(site.asked) == previews_mod.MAX_REDIRECTS + 1

    site = Site({
        "https://short.example/x": (301, {"location": "https://exame.com/noticia"}, b""),
        "https://exame.com/noticia": (200, HTML, PAGE.encode()),
        "https://exame.com/img/capa.png": (200, {"content-type": "image/png"}, PNG),
    })
    got = await make(tmp_path, site).resolve("https://short.example/x")
    assert got is not None and got["url"] == "https://short.example/x"


def test_read_tags_falls_back_and_resolves_relative_images() -> None:
    tags = read_tags(
        '<head><meta name="twitter:image" content="thumb.jpg"><title>Só a aba</title></head>',
        "https://www.site.com.br/a/b",
    )
    assert tags == {"title": "Só a aba", "site": "site.com.br", "image": "https://www.site.com.br/a/thumb.jpg"}
    assert read_tags('<meta property="og:image" content="data:image/png;base64,AAAA">', "https://s.example/")["image"] == ""


def test_image_keys_are_opaque(tmp_path: Path) -> None:
    previews = LinkPreviews(tmp_path / "p")
    for key in ("../../demo.env", "a" * 31, "A" * 32, "g" * 32, ""):
        assert previews.image(key) is None
