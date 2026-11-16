"""browser_* tools attached to a Chrome that is already running (cdp_url).

The "person's browser" here is a real Chromium started with a debugging port
and a profile of its own, as `demo/chrome-logado.sh` does; a cookie set in it
before the tools attach stands for a login. No internet: a local server.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register
from symbiote.browser.config import BrowserOptions
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        importlib.util.find_spec("playwright") is None,
        reason="needs the 'browser' extra",
    ),
]

TOOLS = ["browser_navigate", "browser_snapshot", "browser_click", "browser_fill", "browser_close"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if path == "/login":
            headers["Set-Cookie"] = "session=rodrigo; Path=/"
            body = "<h1>Entrou</h1>"
        elif path == "/whoami":
            cookie = self.headers.get("Cookie") or ""
            who = cookie.split("session=")[1].split(";")[0] if "session=" in cookie else "ninguém"
            body = f"<h1>Logado como {who}</h1>"
        elif path == "/links":
            port = self.server.server_address[1]
            body = (f'<a href="/whoami">Perfil</a> '
                    f'<a href="http://localhost:{port}/whoami">Outro site</a>')
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args, **kwargs):
        return


@pytest.fixture
def site() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _devtools(base: str, path: str):
    with urllib.request.urlopen(f"{base}{path}", timeout=2) as r:
        return json.loads(r.read())


@pytest.fixture
def persons_chrome(tmp_path: Path) -> Iterator[str]:
    """A Chromium with a debugging port and its own profile, like the user's."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        binary = p.chromium.executable_path
    if not Path(binary).exists():
        pytest.skip("Chromium binary not installed (playwright install chromium)")
    port = _free_port()
    proc = subprocess.Popen(
        [binary, "--headless=new", f"--remote-debugging-port={port}",
         f"--user-data-dir={tmp_path / 'profile'}", "--no-first-run",
         "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                _devtools(base, "/json/version")
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.skip("Chromium did not open its debugging port")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def _log_in(chrome: str, site: str) -> None:
    """The person logs in on their own browser before the tools attach."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(chrome)
        page = await browser.contexts[0].new_page()
        await page.goto(f"{site}/login")
        await page.close()


def _tabs(chrome: str) -> list[str]:
    return [t["url"] for t in _devtools(chrome, "/json/list") if t["type"] == "page"]


@pytest.fixture
def make_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from symbiote.security import network

    monkeypatch.setattr(network, "validate_url", lambda url: url)  # the site is local

    def make(**browser_options) -> tuple[SymbioteKernel, str]:
        kernel = SymbioteKernel(
            KernelConfig(db_path=str(tmp_path / "k.sqlite")),
            llm=MockLLMAdapter(default_response="ok"),
        )
        register(kernel, browser_backend="chromium",
                 browser_options=BrowserOptions(timeout_ms=10_000, **browser_options))
        bot = kernel.create_symbiote(name="pesquisador", role="r")
        kernel.environment.configure(symbiote_id=bot.id, tools=TOOLS)
        return kernel, bot.id

    return make


async def _call(kernel: SymbioteKernel, bot: str, tool: str, **params):
    return await kernel._tool_gateway.execute_async(  # noqa: SLF001
        symbiote_id=bot, session_id=None, tool_id=tool, params=params, timeout=20.0
    )


@pytest.mark.asyncio
async def test_the_tab_opens_in_the_logged_in_profile_and_closing_keeps_the_browser(
    persons_chrome: str, site: str, make_kernel
) -> None:
    await _log_in(persons_chrome, site)
    kernel, bot = make_kernel(cdp_url=persons_chrome)
    before = _tabs(persons_chrome)

    nav = await _call(kernel, bot, "browser_navigate", url=f"{site}/whoami", task_id="t")
    assert nav.success, nav.error
    snap = await _call(kernel, bot, "browser_snapshot", task_id="t")
    assert "Logado como rodrigo" in snap.output["text"]  # the person's session
    assert len(_tabs(persons_chrome)) == len(before) + 1  # a tab of its own

    closed = await _call(kernel, bot, "browser_close", task_id="t")
    assert closed.success
    assert _tabs(persons_chrome) == before  # only its tab went away
    assert _devtools(persons_chrome, "/json/version")  # the browser is still up


@pytest.mark.asyncio
async def test_shutting_down_disconnects_without_closing_the_browser(
    persons_chrome: str, site: str
) -> None:
    from symbiote.browser.browser.providers.chromium_local import ChromiumProvider

    provider = ChromiumProvider(BrowserOptions(cdp_url=persons_chrome, timeout_ms=10_000))
    before = _tabs(persons_chrome)
    session = await provider.get_or_create_session("x")
    await session.navigate(f"{site}/whoami")

    await provider.close_all_async()

    assert _tabs(persons_chrome) == before
    assert _devtools(persons_chrome, "/json/version")


@pytest.mark.asyncio
async def test_clicks_and_typing_need_approval_only_in_the_persons_browser(
    persons_chrome: str, make_kernel
) -> None:
    attached, _ = make_kernel(cdp_url=persons_chrome)
    gw = attached.tool_gateway
    assert gw.get_risk_level("browser_click") == "high"
    assert gw.get_risk_level("browser_fill") == "high"
    assert gw.get_risk_level("browser_navigate") == "medium"
    assert "logged in to their accounts" in gw.get_descriptor("browser_click").description

    isolated, _ = make_kernel()
    assert isolated.tool_gateway.get_risk_level("browser_click") == "medium"


@pytest.mark.asyncio
async def test_the_allowlist_covers_navigation_and_links_clicked(
    persons_chrome: str, site: str, make_kernel
) -> None:
    kernel, bot = make_kernel(cdp_url=persons_chrome, allowlist=["127.0.0.1"])

    refused = await _call(kernel, bot, "browser_navigate",
                          url=site.replace("127.0.0.1", "localhost") + "/whoami", task_id="t")
    assert not refused.success and "allowlist" in (refused.error or "")

    assert (await _call(kernel, bot, "browser_navigate", url=f"{site}/links", task_id="t")).success
    snap = await _call(kernel, bot, "browser_snapshot", task_id="t")
    refs = snap.output["refs"]
    assert len(refs) == 2  # "Perfil" (same site) and "Outro site" (localhost)

    out = await _call(kernel, bot, "browser_click", ref=refs[1], task_id="t")
    assert not out.success and "allowlist" in (out.error or "")
    still = await _call(kernel, bot, "browser_snapshot", task_id="t")
    assert "Outro site" in still.output["text"]  # never left the page

    ok = await _call(kernel, bot, "browser_click", ref=refs[0], task_id="t")
    assert ok.success, ok.error
    await _call(kernel, bot, "browser_close", task_id="t")


@pytest.mark.asyncio
async def test_no_browser_listening_gives_a_clear_error(make_kernel) -> None:
    kernel, bot = make_kernel(cdp_url=f"http://127.0.0.1:{_free_port()}")
    out = await _call(kernel, bot, "browser_navigate", url="https://example.com", task_id="t")
    assert not out.success
    assert "No browser to attach to" in (out.error or "")


@pytest.mark.parametrize("url", ["http://10.0.0.5:9222", "http://example.com:9222", "file:///tmp/x"])
def test_only_a_browser_on_this_machine(url: str) -> None:
    with pytest.raises(ValidationError):
        BrowserOptions(cdp_url=url)
    assert BrowserOptions(cdp_url="http://localhost:9222").cdp_url == "http://localhost:9222"
    assert BrowserOptions(cdp_url="").cdp_url is None


@pytest.mark.asyncio
async def test_kernel_shutdown_closes_its_tabs_from_another_thread(
    persons_chrome: str, site: str, make_kernel
) -> None:
    # The demo shuts the kernel down in a worker thread while the server's
    # loop (where the tabs were opened) keeps running.
    import asyncio

    kernel, bot = make_kernel(cdp_url=persons_chrome)
    before = _tabs(persons_chrome)
    assert (await _call(kernel, bot, "browser_navigate", url=f"{site}/whoami", task_id="t")).success
    assert len(_tabs(persons_chrome)) == len(before) + 1

    await asyncio.to_thread(kernel.shutdown)

    assert _tabs(persons_chrome) == before
    assert _devtools(persons_chrome, "/json/version")


def _kill_browser_using(profile: Path) -> None:
    """Stop the browser a test launched detached (found by its profile dir)."""
    import contextlib
    import os
    import signal

    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if str(profile).encode() in cmdline:
            with contextlib.suppress(OSError):
                os.kill(int(proc.name), signal.SIGTERM)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="finds the launched browser through /proc")
@pytest.mark.asyncio
async def test_nothing_listening_starts_the_browser_and_it_outlives_the_kernel(
    tmp_path: Path, site: str, make_kernel
) -> None:
    import asyncio

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        binary = p.chromium.executable_path
    port = _free_port()
    profile = tmp_path / "perfil-lancado"
    chrome = f"http://127.0.0.1:{port}"
    kernel, bot = make_kernel(
        cdp_url=chrome,
        cdp_launch=[binary, "--headless=new", f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile}", "--no-first-run", "about:blank"],
    )
    try:
        nav = await _call(kernel, bot, "browser_navigate", url=f"{site}/whoami", task_id="t")
        assert nav.success, nav.error
        assert _devtools(chrome, "/json/version")  # started by the provider

        await asyncio.to_thread(kernel.shutdown)
        # The person's browser now: the kernel leaves it running.
        assert _devtools(chrome, "/json/version")
        assert all("whoami" not in url for url in _tabs(chrome))  # its tab is gone
    finally:
        _kill_browser_using(profile)


@pytest.mark.asyncio
async def test_the_chrome_already_open_is_found_through_its_profile(
    tmp_path: Path, site: str, make_kernel
) -> None:
    """The person's open Chrome publishes its address in DevToolsActivePort
    (here: a Chromium on its own profile, port chosen by Chrome)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        binary = p.chromium.executable_path
    profile = tmp_path / "perfil-do-dia-a-dia"
    proc = subprocess.Popen(
        [binary, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
         "--no-first-run", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.1)
        chrome = f"http://127.0.0.1:{port_file.read_text().split()[0]}"
        await _log_in(chrome, site)
        before = _tabs(chrome)

        kernel, bot = make_kernel(cdp_port_file=str(port_file))
        nav = await _call(kernel, bot, "browser_navigate", url=f"{site}/whoami", task_id="t")
        assert nav.success, nav.error
        snap = await _call(kernel, bot, "browser_snapshot", task_id="t")
        assert "Logado como rodrigo" in snap.output["text"]
        await _call(kernel, bot, "browser_close", task_id="t")
        assert _tabs(chrome) == before
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.asyncio
async def test_remote_debugging_off_says_how_to_allow_it(tmp_path: Path, make_kernel) -> None:
    kernel, bot = make_kernel(cdp_port_file=str(tmp_path / "DevToolsActivePort"))
    out = await _call(kernel, bot, "browser_navigate", url="https://example.com", task_id="t")
    assert not out.success
    assert "chrome://inspect/#remote-debugging" in (out.error or "")
