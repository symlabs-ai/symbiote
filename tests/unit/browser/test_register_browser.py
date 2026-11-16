"""register_browser (a second family of browser tools) and
register_user_browser (the person's own Chrome). Nothing is launched here."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register, register_browser, register_user_browser
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

# The module: the package exports a function of the same name.
register_mod = importlib.import_module("symbiote.browser.register")

ACTIONS = ["navigate", "snapshot", "click", "fill", "close", "screenshot", "wait_for"]


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SymbioteKernel:
    # Registration only: no Playwright needed.
    monkeypatch.setattr(register_mod, "_module_available", lambda n: True)
    k = SymbioteKernel(KernelConfig(db_path=str(tmp_path / "k.db")), llm=MockLLMAdapter(default_response="ok"))
    yield k
    k.shutdown()


def test_a_second_family_lives_next_to_the_first(kernel: SymbioteKernel) -> None:
    register(kernel, browser_backend="chromium")
    ids = register_browser(kernel, prefix="conta", options={"cdp_url": "http://127.0.0.1:9333"})

    assert ids == [f"conta_{a}" for a in ACTIONS]
    gw = kernel.tool_gateway
    for action in ACTIONS:
        assert gw.get_descriptor(f"browser_{action}") is not None
        assert gw.get_descriptor(f"conta_{action}") is not None
    click = gw.get_descriptor("conta_click")
    # Its own tag, so a symbiote filtered on "browser" does not see it.
    assert "conta" in click.tags and "browser" not in click.tags
    assert click.name.startswith("Conta")
    # Attached: clicks and typing need a person; the isolated family does not.
    assert click.requires_approval and click.risk_level == "high"
    assert gw.get_descriptor("conta_fill").requires_approval
    assert not gw.get_descriptor("conta_navigate").requires_approval
    assert not gw.get_descriptor("browser_click").requires_approval
    assert "never type passwords" in click.description

    assert register_browser(kernel, prefix="conta") == ids  # idempotent per prefix


def test_prefix_must_be_an_identifier(kernel: SymbioteKernel) -> None:
    with pytest.raises(ValueError):
        register_browser(kernel, prefix="minha conta")


def test_the_user_browser_needs_a_site_list_and_a_browser(
    kernel: SymbioteKernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        register_user_browser(kernel, allowlist=[], binary="/bin/true", profile_dir=tmp_path)
    monkeypatch.setattr(register_mod, "find_chrome", lambda: None)
    with pytest.raises(RuntimeError, match="No Chrome"):
        register_user_browser(kernel, allowlist=["calendar.google.com"], profile_dir=tmp_path)


def test_the_user_browser_launches_its_own_profile_on_a_local_port(
    kernel: SymbioteKernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}
    real = register_mod.register_browser

    def spy(k, **kwargs):
        seen.update(kwargs)
        return real(k, **kwargs)

    monkeypatch.setattr(register_mod, "register_browser", spy)
    ids = register_user_browser(kernel, allowlist=["calendar.google.com"], port=9444,
                                binary="/opt/chrome", profile_dir=tmp_path / "perfil")

    assert ids[0] == "user_browser_navigate"
    opts = seen["options"]
    assert opts.cdp_url == "http://127.0.0.1:9444"
    assert opts.cdp_launch[0] == "/opt/chrome"
    assert "--remote-debugging-port=9444" in opts.cdp_launch
    assert f"--user-data-dir={tmp_path / 'perfil'}" in opts.cdp_launch
    assert opts.allowlist == ["calendar.google.com"]
    assert (tmp_path / "perfil").is_dir()
    assert kernel.tool_gateway.get_descriptor("user_browser_click").requires_approval


def test_launch_needs_an_address() -> None:
    from pydantic import ValidationError

    from symbiote.browser.config import BrowserOptions

    with pytest.raises(ValidationError, match="cdp_url"):
        BrowserOptions(cdp_launch=["chrome"])


def test_the_open_session_attaches_to_the_everyday_profile(
    kernel: SymbioteKernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}
    real = register_mod.register_browser

    def spy(k, **kwargs):
        seen.update(kwargs)
        return real(k, **kwargs)

    monkeypatch.setattr(register_mod, "register_browser", spy)
    register_user_browser(kernel, session="open", allowlist=["calendar.google.com"],
                          profile_dir=tmp_path / "google-chrome")
    opts = seen["options"]
    assert opts.cdp_port_file == str(tmp_path / "google-chrome" / "DevToolsActivePort")
    assert opts.cdp_url is None and opts.cdp_launch is None  # nothing is opened
    assert opts.cdp_connect_timeout_s >= 60  # time to answer Chrome's dialog
    assert kernel.tool_gateway.get_descriptor("user_browser_click").requires_approval

    with pytest.raises(ValueError, match="session"):
        register_user_browser(kernel, session="other", allowlist=["x.com"], prefix="outro")  # type: ignore[arg-type]
