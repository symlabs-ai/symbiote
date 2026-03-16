"""Tool call parser — extract tool calls from LLM response text."""

from __future__ import annotations

import json
import re

from symbiote.environment.descriptors import ToolCall

_TOOL_CALL_PATTERN = re.compile(
    r"```tool_call\s*\n(.*?)\n```",
    re.DOTALL,
)


def parse_tool_calls(text: str) -> tuple[str, list[ToolCall]]:
    """Extract tool_call blocks from LLM response text.

    Returns (clean_text, tool_calls) where clean_text has the blocks removed.

    Expected format in LLM output::

        ```tool_call
        {"tool": "yn_publish", "params": {"id": "123"}}
        ```
    """
    calls: list[ToolCall] = []

    for match in _TOOL_CALL_PATTERN.finditer(text):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        tool_id = data.get("tool")
        if not tool_id:
            continue

        calls.append(
            ToolCall(
                tool_id=tool_id,
                params=data.get("params", {}),
            )
        )

    clean = _TOOL_CALL_PATTERN.sub("", text).strip()
    return clean, calls
