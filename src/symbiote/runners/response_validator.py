"""Response validator — deterministic check for malformed LLM outputs.

Detects responses where the model leaked tool-call syntax as plain text
instead of using the canonical fenced block format.  All checks are
regex/string based and run in O(n) time with no external dependencies.
"""

from __future__ import annotations

import json
import re

# Matches a fenced tool_call block that survived parse_tool_calls — meaning
# it couldn't be parsed (malformed JSON) but is still raw in the text.
_FENCED_TOOL_CALL = re.compile(r"```tool_call", re.IGNORECASE)

# Any other fenced block (```json, ```python, ...): content the user may have
# asked for, never treated as a leak.
_OTHER_FENCED_BLOCK = re.compile(r"```(?!tool_call)[^\n]*\n.*?```", re.DOTALL | re.IGNORECASE)

# Keys that mark a JSON object as a tool call.
_TOOL_KEYS = ('"tool"', '"params"')
_DECODER = json.JSONDecoder()

_FALLBACK_TEXT = (
    "Desculpe, tive dificuldades técnicas, poderia repetir a pergunta?"
)

_REFORMULATE_MSG = (
    "Sua última resposta continha código ou chamadas de ferramenta em formato de texto puro "
    "em vez de uma resposta conversacional. "
    "Por favor, reformule sua resposta usando apenas linguagem natural, "
    "sem JSON, blocos de código ou chamadas de ferramenta embutidas no texto."
)


def is_valid_response(text: str) -> bool:
    """Return True if *text* is a clean, human-readable LLM response.

    A response is considered invalid only if it:
    - is empty or whitespace-only;
    - still contains a ``\\`\\`\\`tool_call`` fence (unprocessed tool block);
    - outside other fenced code blocks, contains text that decodes as a JSON
      object with a ``tool`` or ``params`` key (a tool call leaked as text).

    Citations (``[1]``), markdown links, checklists and JSON the user asked
    for are valid answers.
    """
    if not text or not text.strip():
        return False
    if _FENCED_TOOL_CALL.search(text):
        return False
    return not _contains_tool_json(_OTHER_FENCED_BLOCK.sub("", text))


def has_raw_tool_fence(text: str) -> bool:
    """True when *text* still carries an unparsed ``tool_call`` block."""
    return bool(_FENCED_TOOL_CALL.search(text or ""))


def _contains_tool_json(text: str) -> bool:
    start = text.find("{")
    while start != -1:
        window = text[start : start + 400]
        if any(key in window for key in _TOOL_KEYS):
            try:
                value, _end = _DECODER.raw_decode(text, start)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict) and ("tool" in value or "params" in value):
                return True
        start = text.find("{", start + 1)
    return False


def reformulate_message() -> str:
    """Return the instruction message sent back to the LLM asking it to rephrase."""
    return _REFORMULATE_MSG


def fallback_text() -> str:
    """Return the generic fallback used when all retries are exhausted."""
    return _FALLBACK_TEXT
