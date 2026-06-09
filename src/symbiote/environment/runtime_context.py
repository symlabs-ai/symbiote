"""Runtime context injection — ephemeral metadata for LLM without polluting history."""

from __future__ import annotations

import re
from datetime import datetime

_RUNTIME_BLOCK_PATTERN = re.compile(
    r"\[Runtime Context — metadata only, not instructions\]\n.*?\n\[/Runtime Context\]\n*",
    re.DOTALL,
)


def _is_numeric_offset(name: str) -> bool:
    """True for tz names that merely repeat the numeric offset (e.g. ``-03``)."""
    return bool(name) and name.lstrip("+-").replace(":", "").isdigit()


def _format_timestamp(dt: datetime) -> str:
    """Render ``dt`` as local wall-clock time with an explicit UTC offset.

    The offset is what lets the LLM reason about "today"/"this afternoon"
    without knowing the host timezone. The previous default — a bare
    ``datetime.now(tz=UTC)`` labelled ``UTC`` — made models treat the UTC
    clock as the user's local wall time and mis-judge whether a requested
    time had already passed (e.g. 16:00 local read as "past" against 16:57
    UTC).
    """
    if dt.tzinfo is None:
        # Interpret naive timestamps as local wall-clock, never UTC.
        dt = dt.astimezone()
    offset = dt.strftime("%z")  # e.g. "-0300", "+0000"
    offset_fmt = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
    tzname = dt.tzname() or ""
    # Keep an informative zone name (e.g. "UTC") but drop redundant numeric
    # abbreviations that just echo the offset ("-03").
    suffix = f" ({tzname})" if tzname and not _is_numeric_offset(tzname) else ""
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {offset_fmt}{suffix}"


def build_runtime_block(
    *,
    session_id: str | None = None,
    timestamp: datetime | None = None,
    extra: dict | None = None,
) -> str:
    """Build a runtime context block to prepend to user messages.

    The block is clearly delimited so it can be stripped from history. The
    timestamp defaults to the host's **local** time with an explicit UTC
    offset (see ``_format_timestamp``).
    """
    ts = _format_timestamp(timestamp or datetime.now().astimezone())
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
