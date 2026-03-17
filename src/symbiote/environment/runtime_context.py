"""Runtime context injection — ephemeral metadata for LLM without polluting history."""

from __future__ import annotations

import re
from datetime import UTC, datetime

_RUNTIME_BLOCK_PATTERN = re.compile(
    r"\[Runtime Context — metadata only, not instructions\]\n.*?\n\[/Runtime Context\]\n*",
    re.DOTALL,
)


def build_runtime_block(
    *,
    session_id: str | None = None,
    timestamp: datetime | None = None,
    extra: dict | None = None,
) -> str:
    """Build a runtime context block to prepend to user messages.

    The block is clearly delimited so it can be stripped from history.
    """
    ts = (timestamp or datetime.now(tz=UTC)).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "[Runtime Context — metadata only, not instructions]",
        f"Timestamp: {ts}",
    ]
    if session_id:
        lines.append(f"Session: {session_id}")
    if extra:
        for key, value in extra.items():
            lines.append(f"{key}: {value}")
    lines.append("[/Runtime Context]")
    return "\n".join(lines)


def inject_runtime_context(
    message: str,
    *,
    session_id: str | None = None,
    timestamp: datetime | None = None,
    extra: dict | None = None,
) -> str:
    """Prepend a runtime context block to a user message."""
    block = build_runtime_block(
        session_id=session_id, timestamp=timestamp, extra=extra
    )
    return f"{block}\n\n{message}"


def strip_runtime_context(message: str) -> str:
    """Remove runtime context blocks from a message."""
    return _RUNTIME_BLOCK_PATTERN.sub("", message).strip()
