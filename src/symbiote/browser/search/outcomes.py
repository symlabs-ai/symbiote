"""How a web search can end, and how to tell the outcomes apart.

A search has four ends, and the model must not confuse them:

- **results**: the list.
- **no results**: the source answered and found nothing.
- **refused**: the source did not answer the query (blocked, challenged, an
  HTTP error, a page that could not be read). ``SearchRefusedError``.
- **unrelated**: the source answered with a full list that has nothing to do
  with the query, which is how some engines refuse without saying so.
  ``SearchUnrelatedError``, a kind of refusal. Unchecked, it passes for success.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

# Words that say nothing about the subject of a query.
_LINKING = frozenset({
    "para", "como", "qual", "quais", "sobre", "entre", "pela", "pelo",
    "with", "from", "what", "which", "that", "this", "does", "have",
})
_WORD = re.compile(r"[^\W_]+")


class SearchRefusedError(Exception):
    """The source did not answer the query. Not the same as no results."""


class SearchUnrelatedError(SearchRefusedError):
    """The source answered with results unrelated to the query."""


def _fold(text: str) -> str:
    """Lower case, no accents: ``práticas`` matches ``praticas``."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def query_coverage(query: str, results: Sequence[dict[str, Any]], *, top: int = 5) -> float:
    """How much of the query shows in the first ``top`` results: the share of
    its words (four letters or more, linking words aside) found in the title,
    the snippet or the address of one of them. ``1.0`` when the query has no
    such word, so a short query is never judged."""
    terms = {w for w in _WORD.findall(_fold(query)) if len(w) >= 4 and w not in _LINKING}
    if not terms:
        return 1.0
    haystack = _fold(
        " ".join(
            f"{r.get('title', '')} {r.get('snippet', '')} {r.get('url', '')}"
            for r in results[:top]
        )
    )
    return sum(1 for term in terms if term in haystack) / len(terms)


def require_related(
    query: str, results: Sequence[dict[str, Any]], *, minimum: float = 0.5
) -> None:
    """Raise ``SearchUnrelatedError`` when a non-empty list covers less than
    ``minimum`` of the query (``0`` turns the check off)."""
    if not results or minimum <= 0:
        return
    coverage = query_coverage(query, results)
    if coverage < minimum:
        raise SearchUnrelatedError(
            f"results unrelated to the query ({coverage:.0%} of its words in the first results)"
        )


__all__ = ["SearchRefusedError", "SearchUnrelatedError", "query_coverage", "require_related"]
