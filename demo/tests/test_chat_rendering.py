"""How <symbiote-chat> renders an answer: links the model writes are usable
and safe. Runs the built component in a headless Chromium; skipped where
Playwright or its browser is not installed."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

CHAT_JS = Path(__file__).resolve().parents[2] / "symbiote-ui" / "dist" / "symbiote-chat.js"

ANSWER = """Notícias da semana:

1. **AI.CUBE fecha US$ 50 milhões em nuvem.** Exame, 13/11
   exame.com/inteligencia-artificial/ai-cube-aws-oracle-50-milhoes/
2. [Provão Paulista](https://g1.globo.com/sp/noticia.ghtml), g1, 14/11
3. Veja também www.inmetro.gov.br e g1.globo.com.

Contato: redacao@exame.com (não é link de site). Arquivo `exame.com/em-codigo` fica como está.
[clique aqui](javascript:alert(1)) e a versão v0.8.3/CHANGELOG não viram link.
"""


@pytest.fixture(scope="module")
def links() -> Iterator[dict[str, Any]]:
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content("<symbiote-chat mode='inline' name='Sol'></symbiote-chat>")
            page.add_script_tag(path=str(CHAT_JS))
            found = page.evaluate(
                """(answer) => {
                    const chat = document.querySelector('symbiote-chat');
                    chat._addMessage('assistant', answer);
                    const msg = [...chat.shadowRoot.querySelectorAll('.symbiote-msg.assistant')].pop();
                    return {
                        text: msg.textContent,
                        links: [...msg.querySelectorAll('a')].map((a) => ({
                            text: a.textContent, href: a.getAttribute('href'),
                            target: a.target, rel: a.rel,
                        })),
                    };
                }""",
                ANSWER,
            )
            browser.close()
    except sync_api.Error as exc:  # no browser binary on this machine
        pytest.skip(f"chromium unavailable: {exc}")
    yield found


def test_plain_addresses_become_links(links: dict[str, Any]) -> None:
    by_text = {link["text"]: link["href"] for link in links["links"]}
    assert by_text["exame.com/inteligencia-artificial/ai-cube-aws-oracle-50-milhoes/"] == (
        "https://exame.com/inteligencia-artificial/ai-cube-aws-oracle-50-milhoes/"
    )
    assert by_text["Provão Paulista"] == "https://g1.globo.com/sp/noticia.ghtml"
    # A bare domain, and the sentence's final dot is not part of it.
    assert by_text["g1.globo.com"] == "https://g1.globo.com"
    assert any(href.rstrip("/") in ("http://www.inmetro.gov.br", "https://www.inmetro.gov.br")
               for href in by_text.values())


def test_links_open_outside_the_chat(links: dict[str, Any]) -> None:
    assert links["links"], "no link rendered"
    for link in links["links"]:
        if link["href"].startswith("http"):
            assert link["target"] == "_blank"
            assert "noopener" in link["rel"]


def test_what_is_not_an_address_stays_text(links: dict[str, Any]) -> None:
    texts = [link["text"] for link in links["links"]]
    hrefs = [link["href"] for link in links["links"]]
    assert "AI.CUBE" not in " ".join(texts)  # a name with a dot, not a site
    assert not any("em-codigo" in t for t in texts)  # inside a code span
    assert not any("CHANGELOG" in t for t in texts)  # a version path
    assert not any(h.startswith("https://exame.com") and "redacao" in h for h in hrefs)
    assert not any(h.lower().startswith("javascript") for h in hrefs)  # script link dropped
    assert "clique aqui" in links["text"]  # its words stay



def test_link_previews_follow_the_text_order_and_skip_links_without_one() -> None:
    script = """async () => {
        const chat = document.querySelector('symbiote-chat');
        const asked = [];
        const pixel = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';
        chat.adapter = {
            sendMessage: async () => ({ response: '' }),
            loadHistory: async () => ({ messages: [] }),
            clearHistory: async () => {},
            linkPreview: async (url) => {
                asked.push(url);
                // The second link answers first and the third has no preview.
                if (url.includes('/dois')) return { image: pixel, title: 'Dois', site: 'Site B' };
                await new Promise((r) => setTimeout(r, 60));
                if (url.includes('/tres')) return null;
                return { image: pixel, title: '<b>Um</b>' };
            },
        };
        const links = Array.from({ length: 9 }, (_, i) => `[l${i}](https://s.example/${['um', 'dois', 'tres'][i] || 'n' + i})`);
        chat._addMessage('assistant', links.join(' ') + ' [de novo](https://s.example/um)');
        chat._addMessage('user', 'https://s.example/usuario');
        await new Promise((r) => setTimeout(r, 400));
        const cards = [...chat.shadowRoot.querySelectorAll('.symbiote-preview')];
        return {
            asked,
            cards: cards.map((c) => ({
                href: c.getAttribute('href'), target: c.target, hidden: c.hidden,
                title: c.querySelector('.symbiote-preview-title').textContent,
                site: c.querySelector('.symbiote-preview-site').textContent,
                markup: c.querySelector('.symbiote-preview-title').innerHTML,
                img: !!c.querySelector('img'),
            })),
        };
    }"""
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content("<symbiote-chat mode='inline' name='Sol'></symbiote-chat>")
            page.add_script_tag(path=str(CHAT_JS))
            got = page.evaluate(script)
            browser.close()
    except sync_api.Error as exc:
        pytest.skip(f"chromium unavailable: {exc}")

    # Six distinct links at most, each asked once; the user's own text is not previewed.
    assert len(got["asked"]) == 6 and len(set(got["asked"])) == 6
    assert not any("usuario" in url for url in got["asked"])
    cards = got["cards"]
    assert [c["href"] for c in cards[:2]] == ["https://s.example/um", "https://s.example/dois"]
    assert not any("/tres" in c["href"] for c in cards)  # no preview, no card
    assert all(c["img"] and not c["hidden"] and c["target"] == "_blank" for c in cards)
    assert cards[0]["title"] == "<b>Um</b>" and "<b>" not in cards[0]["markup"]  # text, never markup
    assert cards[0]["site"] == "s.example" and cards[1]["site"] == "Site B"
