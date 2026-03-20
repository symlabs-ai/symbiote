"""Response validator — deterministic check for malformed LLM outputs.

Detects responses where the model leaked tool-call syntax as plain text
instead of using the canonical fenced block format.  All checks are
regex/string based and run in O(n) time with no external dependencies.
"""

from __future__ import annotations

import re

# Matches a bare JSON object/array that starts a line (leaked JSON dump)
_BARE_JSON_LINE = re.compile(r"(?:^|\n)\s*[{\[]")

# Matches the text-serialised tool pattern:  "tool"   :   "something"
_TEXT_TOOL_CALL = re.compile(r'"tool"\s*:\s*"')

# Matches a fenced tool_call block that survived parse_tool_calls — meaning
# it couldn't be parsed (malformed JSON) but is still raw in the text.
_FENCED_TOOL_CALL = re.compile(r"```tool_call", re.IGNORECASE)

_FALLBACK_TEXT = (
    "Desculpe, não consegui formular uma resposta adequada. "
    "Por favor, repita sua pergunta."
)

_REFORMULATE_MSG = (
    "Sua última resposta continha código ou chamadas de ferramenta em formato de texto puro "
    "em vez de uma resposta conversacional. "
    "Por favor, reformule sua resposta usando apenas linguagem natural, "
    "sem JSON, blocos de código ou chamadas de ferramenta embutidas no texto."
)


def is_valid_response(text: str) -> bool:
    """Return True if *text* is a clean, human-readable LLM response.

    A response is considered invalid if it:
    - Is empty or whitespace-only.
    - Contains a line starting with ``{`` or ``[`` (bare JSON).
    - Contains a text-serialised tool call pattern (``"tool": "..."``).
    - Still contains a ``\\`\\`\\`tool_call`` fence (unprocessed tool block).
    """
    if not text or not text.strip():
        return False
    if _BARE_JSON_LINE.search(text):
        return False
    if _TEXT_TOOL_CALL.search(text):
        return False
    return not _FENCED_TOOL_CALL.search(text)


def reformulate_message() -> str:
    """Return the instruction message sent back to the LLM asking it to rephrase."""
    return _REFORMULATE_MSG


def fallback_text() -> str:
    """Return the generic fallback used when all retries are exhausted."""
    return _FALLBACK_TEXT
