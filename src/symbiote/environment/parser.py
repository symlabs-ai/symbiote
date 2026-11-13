"""Tool call parser — extract tool calls from LLM response text."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from symbiote.environment.descriptors import ToolCall

_FENCE_OPEN = re.compile(r"```tool_call[ \t]*\r?\n?")
_FENCE_CLOSE = re.compile(r"\s*```")

# Legacy non-greedy form, used only to strip fenced blocks whose content is
# not valid JSON (so the raw block does not leak into the user-facing text).
_TOOL_CALL_PATTERN = re.compile(
    r"```tool_call\s*\n(.*?)```",
    re.DOTALL,
)

# Fallback: bare JSON that looks like a tool call (LLM omitted fencing)
_BARE_TOOL_CALL_PATTERN = re.compile(
    r'(?:^|\n)\s*(\{"tool"\s*:\s*"[^"]+"\s*,\s*"params"\s*:\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}\s*\})',
)

_DECODER = json.JSONDecoder()


def parse_tool_calls(text: str) -> tuple[str, list[ToolCall]]:
    """Extract tool_call blocks from LLM response text.

    Returns (clean_text, tool_calls) where clean_text has the blocks removed.
    Never raises: malformed entries are skipped.

    Expected format in LLM output::

        ```tool_call
        {"tool": "yn_publish", "params": {"id": "123"}}
        ```

    A block may also hold a JSON list of such objects. The JSON is decoded
    from the opening fence, so a ``````` inside a string value (code in a
    note, markdown) does not end the block early. Bare JSON tool calls
    (without fencing) are handled as a fallback.
    """
    # 1. Canonical fenced format
    calls, spans = _extract_fenced(text)
    if calls:
        return _remove_spans(text, spans), calls

    # 2. Fallback: bare JSON tool calls
    calls = []
    for match in _BARE_TOOL_CALL_PATTERN.finditer(text):
        calls.extend(_calls_from_raw(match.group(1).strip()))
    if calls:
        clean = _BARE_TOOL_CALL_PATTERN.sub("", text).strip()
        return clean, calls

    return text, []


def _extract_fenced(text: str) -> tuple[list[ToolCall], list[tuple[int, int]]]:
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        opening = _FENCE_OPEN.search(text, pos)
        if opening is None:
            break
        start = opening.end()
        while start < len(text) and text[start] in " \t\r\n":
            start += 1
        try:
            data, end = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            legacy = _TOOL_CALL_PATTERN.match(text, opening.start())
            if legacy is not None:
                spans.append((legacy.start(), legacy.end()))
                pos = legacy.end()
            else:
                pos = opening.end()
            continue
        closing = _FENCE_CLOSE.match(text, end)
        block_end = closing.end() if closing else end
        spans.append((opening.start(), block_end))
        calls.extend(_calls_from_data(data))
        pos = block_end
    return calls, spans


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    parts: list[str] = []
    last = 0
    for start, end in spans:
        parts.append(text[last:start])
        last = end
    parts.append(text[last:])
    return "".join(parts).strip()


def _calls_from_raw(raw: str) -> list[ToolCall]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return _calls_from_data(data)


def _calls_from_data(data: Any) -> list[ToolCall]:
    items = data if isinstance(data, list) else [data]
    calls: list[ToolCall] = []
    for item in items:
        call = _to_call(item)
        if call is not None:
            calls.append(call)
    return calls


def _to_call(item: Any) -> ToolCall | None:
    if not isinstance(item, dict):
        return None
    tool_id = item.get("tool")
    if not isinstance(tool_id, str) or not tool_id:
        return None
    params = item.get("params")
    if params is None:
        params = {}
    elif isinstance(params, str):
        # Some models double-encode the params object.
        try:
            params = json.loads(params)
        except json.JSONDecodeError:
            return None
    if not isinstance(params, dict):
        return None
    try:
        return ToolCall(tool_id=tool_id, params=params)
    except ValidationError:
        return None
