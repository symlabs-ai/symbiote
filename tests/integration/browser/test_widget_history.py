"""``<symbiote-chat>`` loads its history from an adapter set late (B-82).

In inline mode the panel opens when the element connects. The history was
then "loaded" from the ``NoopAdapter`` and marked as loaded, so a host that
set its adapter afterwards (its configuration came from the server) never
got the history on screen.
"""

from __future__ import annotations

import pytest

from symbiote.ui import widget_path

pytest.importorskip("playwright")

_ADAPTER = """
window.calls = {load: 0, clear: 0};
window.makeAdapter = () => ({
  async sendMessage() { return {response: 'ok'}; },
  async loadHistory() {
    window.calls.load += 1;
    return {messages: [
      {role: 'user', content: 'first question'},
      {role: 'assistant', content: 'first answer'},
    ]};
  },
  async clearHistory() { window.calls.clear += 1; },
});
window.shown = () => Array.from(
  document.querySelector('symbiote-chat').shadowRoot.querySelectorAll('.symbiote-msg')
).map(el => el.textContent.trim());
"""


@pytest.fixture()
def page():
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Error:
            pytest.skip("Chromium binary not installed (playwright install chromium)")
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        page.add_script_tag(path=str(widget_path()))
        page.add_script_tag(content=_ADAPTER)
        yield page
        browser.close()


def _mount(page, attrs: str = "") -> None:
    page.evaluate(
        "attrs => { document.body.innerHTML = `<symbiote-chat ${attrs}></symbiote-chat>`; }",
        attrs,
    )


def _set_adapter(page) -> None:
    page.evaluate("document.querySelector('symbiote-chat').adapter = window.makeAdapter()")


def _wait_for_history(page) -> None:
    page.wait_for_function("window.shown().length === 2")


def test_inline_panel_loads_history_when_the_adapter_arrives_later(page) -> None:
    _mount(page, 'mode="inline"')
    assert page.evaluate("window.shown()") == []

    _set_adapter(page)
    _wait_for_history(page)

    assert page.evaluate("window.shown()") == ["first question", "first answer"]
    assert page.evaluate("window.calls.load") == 1


def test_floating_panel_opened_before_the_adapter_loads_history_too(page) -> None:
    _mount(page)
    page.evaluate("document.querySelector('symbiote-chat').open()")

    _set_adapter(page)
    _wait_for_history(page)

    assert page.evaluate("window.calls.load") == 1


def test_history_is_loaded_once_when_the_adapter_was_set_first(page) -> None:
    _mount(page)
    _set_adapter(page)

    page.evaluate("document.querySelector('symbiote-chat').open()")
    _wait_for_history(page)
    page.evaluate(
        "() => { const c = document.querySelector('symbiote-chat'); c.close(); c.open(); }"
    )
    page.evaluate("document.querySelector('symbiote-chat').adapter = window.makeAdapter()")

    assert page.evaluate("window.calls.load") == 1
    assert len(page.evaluate("window.shown()")) == 2


def test_reload_shows_the_history_again_without_erasing_it(page) -> None:
    _mount(page, 'mode="inline"')
    _set_adapter(page)
    _wait_for_history(page)
    page.evaluate("document.querySelector('symbiote-chat').addMessage('assistant', 'extra')")
    assert len(page.evaluate("window.shown()")) == 3

    page.evaluate("document.querySelector('symbiote-chat').reload()")
    page.wait_for_function("window.calls.load === 2 && window.shown().length === 2")

    assert page.evaluate("window.calls.clear") == 0
