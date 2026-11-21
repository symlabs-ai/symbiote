"""A small HTML reader on the standard library: a tree to look things up in,
and the readable text of a page.

Enough for the providers that read pages themselves (the ``http`` extractor
and the search engine readers) without adding a parser to the dependencies.
It is not a browser: nothing runs, and a page that builds itself with
JavaScript yields little or no text.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})
# Never text a person reads.
_SKIPPED = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "head"})
# Around the content of a page, not the content.
_CHROME = frozenset({"nav", "footer", "aside", "form", "button", "dialog"})
# The same, said with an ARIA role instead of a tag.
_CHROME_ROLES = frozenset({"navigation", "banner", "contentinfo", "search", "complementary"})
_BLOCKS = frozenset({
    "p", "div", "section", "article", "main", "header", "ul", "ol", "table", "tr",
    "blockquote", "pre", "figure", "figcaption", "dl", "dt", "dd", "details", "summary",
    "address", "fieldset",
})
# A start tag that closes an element left open: tag -> (the open tags it
# closes, where the search for one stops).
_P_BOUNDARY = frozenset({
    "div", "section", "article", "main", "body", "td", "th", "li", "ul", "ol", "table",
    "blockquote", "#document",
})
_IMPLIED_END: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol", "#document"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl", "#document"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl", "#document"})),
    "tr": (frozenset({"tr"}), frozenset({"table", "tbody", "thead", "tfoot", "#document"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table", "#document"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table", "#document"})),
    "option": (frozenset({"option"}), frozenset({"select", "#document"})),
}
_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}
# Inline in the markup, but a word boundary in the text.
_APART = frozenset({"br", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"})
_SPACES = re.compile(r"[ \t\r\f\v ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class Node:
    """An element: its tag, attributes and children (elements and strings)."""

    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None = None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node | str] = []

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def get(self, name: str) -> str:
        return self.attrs.get(name, "")

    def iter(self):  # noqa: ANN201 — generator of Node
        """This element's descendants, in document order."""
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, tag: str | None = None, cls: str | None = None) -> list[Node]:
        """Descendants with this tag and/or this class."""
        return [
            node
            for node in self.iter()
            if (tag is None or node.tag == tag) and (cls is None or cls in node.classes)
        ]

    def find(self, tag: str | None = None, cls: str | None = None) -> Node | None:
        for node in self.iter():
            if (tag is None or node.tag == tag) and (cls is None or cls in node.classes):
                return node
        return None

    def text(self) -> str:
        """The element's text on one line, spaces collapsed."""
        parts: list[str] = []
        _collect(self, parts)
        return " ".join("".join(parts).split())


def _collect(node: Node, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
        elif child.tag not in _SKIPPED:
            apart = child.tag in _BLOCKS or child.tag in _APART
            if apart:
                out.append(" ")
            _collect(child, out)
            if apart:
                out.append(" ")


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {})
        self._open = self.root

    def _close_implied(self, tag: str) -> None:
        """``<p>one<p>two`` and ``<li>a<li>b``: the second closes the first."""
        if tag in _IMPLIED_END:
            closes, boundary = _IMPLIED_END[tag]
        elif tag in _BLOCKS or tag in _HEADINGS or tag in _CHROME or tag == "hr":
            closes, boundary = frozenset({"p"}), _P_BOUNDARY
        else:
            return
        node: Node | None = self._open
        while node is not None and node.tag not in boundary:
            if node.tag in closes and node.parent is not None:
                self._open = node.parent
                return
            node = node.parent

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_implied(tag)
        node = Node(tag, {k: v or "" for k, v in attrs}, self._open)
        self._open.children.append(node)
        if tag not in _VOID:
            self._open = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open.children.append(Node(tag, {k: v or "" for k, v in attrs}, self._open))

    def handle_endtag(self, tag: str) -> None:
        node: Node | None = self._open
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self._open = node.parent  # closes whatever was left open inside

    def handle_data(self, data: str) -> None:
        self._open.children.append(data)


def parse_html(html: str) -> Node:
    """The page as a tree. Never raises: broken markup gives a best-effort tree."""
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:  # noqa: BLE001 — a page is never a reason to fail here
        pass
    return builder.root


def page_title(root: Node) -> str:
    title = root.find("title")
    if title is not None and title.text():
        return title.text()
    heading = root.find("h1")
    return heading.text() if heading is not None else ""


def page_text(root: Node, *, base_url: str = "") -> str:
    """The readable content of a page as light markdown: headings with ``#``,
    list items with ``-``, links as ``[text](address)``, one blank line
    between blocks.

    Reads ``<main>`` (or ``role="main"``) or the only ``<article>`` when the
    page has one, else the body, and leaves out navigation, footers, forms
    and scripts.
    """
    body = root.find("main") or next(
        (node for node in root.iter() if node.get("role") == "main"), None
    )
    if body is None:
        articles = root.find_all("article")
        body = articles[0] if len(articles) == 1 else root.find("body") or root
    out: list[str | _Verbatim] = []
    _render(body, out, base_url)
    # Text between code blocks is tidied; a code block keeps its own spacing.
    pieces: list[str] = []
    run: list[str] = []
    for part in [*out, _Verbatim("")]:
        if isinstance(part, str):
            run.append(part)
            continue
        lines = [_SPACES.sub(" ", line).strip() for line in "".join(run).split("\n")]
        pieces.append("\n".join(lines))
        pieces.append(part.text)
        run = []
    return _BLANK_LINES.sub("\n\n", "".join(pieces)).strip()


class _Verbatim:
    """Text that goes to the output exactly as it is (a code block)."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


def _render(node: Node, out: list[str | _Verbatim], base_url: str) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(child.replace("\n", " "))
            continue
        tag = child.tag
        if tag in _SKIPPED or tag in _CHROME or child.get("role") in _CHROME_ROLES:
            continue
        if tag in _HEADINGS:
            text = child.text()
            if text:
                out.append(f"\n\n{_HEADINGS[tag]} {text}\n\n")
        elif tag == "li":
            if child.text():
                out.append("\n- ")
                _render(child, out, base_url)
        elif tag == "br":
            out.append("\n")
        elif tag == "pre":
            parts: list[str] = []
            _raw(child, parts)
            out.append(_Verbatim("\n\n```\n" + "".join(parts).strip("\n") + "\n```\n\n"))
        elif tag == "a":
            text = child.text()
            href = child.get("href")
            if text and href and not href.startswith(("#", "javascript:", "mailto:")):
                out.append(f"[{text}]({urljoin(base_url, href) if base_url else href})")
            elif text:
                out.append(text)
        elif tag in ("td", "th"):
            _render(child, out, base_url)
            out.append(" | ")
        elif tag in _BLOCKS:
            out.append("\n\n")
            _render(child, out, base_url)
            out.append("\n\n")
        else:
            _render(child, out, base_url)


def _raw(node: Node, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
        else:
            _raw(child, out)


__all__ = ["Node", "page_text", "page_title", "parse_html"]
