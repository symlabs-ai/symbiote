"""Tool call parser — extract tool calls from LLM response text."""

from __future__ import annotations

import json
import re

from symbiote.environment.descriptors import ToolCall

# Canonical fenced format: ```tool_call\n{...}\n``` or ```tool_call\n{...}```
_TOOL_CALL_PATTERN = re.compile(
    r"```tool_call\s*\n(.*?)```",
    re.DOTALL,
)

# Fallback: bare JSON that looks like a tool call (LLM omitted fencing)
_BARE_TOOL_CALL_PATTERN = re.compile(
    r'(?:^|\n)\s*(\{"tool"\s*:\s*"[^"]+"\s*,\s*"params"\s*:\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}\s*\})',
)


def parse_tool_calls(text: str) -> tuple[str, list[ToolCall]]:
    """Extract tool_call blocks from LLM response text.

    Returns (clean_text, tool_calls) where clean_text has the blocks removed.

    Expected format in LLM output::

        ```tool_call
        {"tool": "yn_publish", "params": {"id": "123"}}
        ```

    Also handles bare JSON tool calls (without fencing) as a fallback.
    """
    # 1. Try canonical fenced format first
    calls = _extract_from_pattern(_TOOL_CALL_PATTERN, text)
    if calls:
        clean = _TOOL_CALL_PATTERN.sub("", text).strip()
        return clean, calls

    # 2. Fallback: bare JSON tool calls
    calls = _extract_from_pattern(_BARE_TOOL_CALL_PATTERN, text)
    if calls:
        clean = _BARE_TOOL_CALL_PATTERN.sub("", text).strip()
        return clean, calls

    return text, []


def _extract_from_pattern(pattern: re.Pattern, text: str) -> list[ToolCall]:
    """Extract tool calls matching a regex pattern."""
    calls: list[ToolCall] = []
    for match in pattern.finditer(text):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        tool_id = data.get("tool")
        if not tool_id:
            continue
        calls.append(ToolCall(tool_id=tool_id, params=data.get("params", {})))
    return calls
