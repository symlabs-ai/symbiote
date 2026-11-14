"""How the text of a turn reaches ``on_token``.

Two modes, chosen by the host (``kernel.set_token_streaming``):

``final`` (default)
    Each model call is buffered; only the validated text of the answer is
    released, after the call ends. Narration of tool-call rounds ("let me
    check...") never reaches ``on_token``.

``live``
    Text goes out as the model writes it, in every round. Tool-call markup
    (a ``tool_call`` fence, a bare ``{"tool": ...}`` object) is held back
    and dropped, so the narration before a tool call is shown and the call
    itself is not. Rounds are separated by a blank line. When validation
    replaces a round's text, the replacement follows as a new paragraph:
    what was sent cannot be taken back, so the authoritative answer is
    still the turn's return value.
"""

from __future__ import annotations

from collections.abc import Callable

TOKEN_STREAMING_MODES = ("final", "live")

_FENCE = "```"
# Fence info strings that open a tool call ("tool_call", "tool_calls", ...).
_TOOL_FENCE_PREFIX = "tool"
# How much of a line-start "{" is held before deciding it is not a tool call.
_JSON_HOLD_CHARS = 400
_TOOL_KEYS = ('"tool"', '"params"')
_ROUND_SEPARATOR = "\n\n"


class MarkupFilter:
    """Forwards streamed text, holding back what may be tool-call markup.

    A fence is held until its info string is known: ``tool_call`` blocks the
    rest of the round, any other fence is released. A ``{`` at the start of
    a line is held until a tool key shows up (blocks the round), it grows
    past ``_JSON_HOLD_CHARS`` or the round ends.
    """

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._pending = ""
        self._blocked = False
        self._line_start = True  # the next character starts a line

    @property
    def blocked(self) -> bool:
        return self._blocked

    def feed(self, chunk: str) -> None:
        if self._blocked or not chunk:
            return
        self._pending += chunk
        self._drain(final=False)

    def finish(self) -> None:
        """End of the round: release what was held unless it was markup."""
        if not self._blocked:
            self._drain(final=True)
        self._pending = ""

    def _send(self, text: str) -> None:
        if text:
            self._emit(text)
            self._line_start = text.endswith("\n")

    def _candidate(self, text: str) -> int:
        """Index of the first character that may open markup, or -1."""
        line_start = self._line_start
        for i, ch in enumerate(text):
            if ch == "`":
                return i
            if ch == "{" and line_start:
                return i
            if ch == "\n":
                line_start = True
            elif not ch.isspace():
                line_start = False
        return -1

    def _drain(self, *, final: bool) -> None:
        while self._pending and not self._blocked:
            text = self._pending
            i = self._candidate(text)
            if i == -1:
                self._pending = ""
                self._send(text)
                return
            self._send(text[:i])
            rest = text[i:]
            held = self._decide(rest, final=final)
            if held is None:  # undecided: wait for more text
                self._pending = rest
                return
            self._pending = rest[held:]

    def _decide(self, text: str, *, final: bool) -> int | None:
        """Handle markup candidate ``text``; return how much was consumed.

        ``None`` means more text is needed to decide.
        """
        if text.startswith("`"):
            if not text.startswith(_FENCE):
                if not final and _FENCE.startswith(text):
                    return None  # "`" or "``": may still become a fence
                self._send(text[0])
                return 1
            newline = text.find("\n")
            info = (text[len(_FENCE):] if newline == -1 else text[len(_FENCE):newline]).strip()
            if info.lower().startswith(_TOOL_FENCE_PREFIX):
                self._blocked = True
                return len(text)
            if newline == -1 and not final and _TOOL_FENCE_PREFIX.startswith(info.lower()):
                return None  # "```" or "```to": may still be a tool fence
            line = text if newline == -1 else text[: newline + 1]
            self._send(line)
            return len(line)
        # A "{" at the start of a line.
        window = text[:_JSON_HOLD_CHARS]
        if any(key in window for key in _TOOL_KEYS):
            self._blocked = True
            return len(text)
        if not final and len(text) < _JSON_HOLD_CHARS:
            return None
        self._send(text[0])
        return 1


class TokenSink:
    """The ``on_token`` of one turn, in ``final`` or ``live`` mode."""

    def __init__(self, on_token: Callable[[str], None] | None, mode: str = "final") -> None:
        self._on_token = on_token
        self.live = on_token is not None and mode == "live"
        self._sent_any = False
        self._round = ""  # text sent so far from the current model call
        self._filter: MarkupFilter | None = None

    # ── live rounds ──────────────────────────────────────────────────────

    def begin_round(self) -> Callable[[str], None] | None:
        """Start a model call; returns the per-chunk callback in live mode."""
        if not self.live:
            return None
        self._round = ""
        self._filter = MarkupFilter(self._emit_round)
        return self._filter.feed

    def end_round(self) -> None:
        """The call ended in tool calls (or failed): its narration stays."""
        self._filter = None
        self._round = ""

    def _emit_round(self, text: str) -> None:
        if not self._round and self._sent_any:
            self._write(_ROUND_SEPARATOR)
        self._round += text
        self._write(text)

    # ── releasing validated text ─────────────────────────────────────────

    def release(self, text: str, chunks: list[str] | None, *, exact: bool = False) -> None:
        """Release the validated text of a call that ends the turn.

        In ``final`` mode the buffered chunks go out when they hold that text
        (with ``exact``, only when they spell it out; otherwise whenever there
        are chunks, the loop's historical behaviour), else the text at once.
        In ``live`` mode only what the stream has not shown yet goes out.
        """
        if self._on_token is None:
            return
        if not self.live:
            if chunks is not None and (not exact or "".join(chunks).strip() == text.strip()):
                for chunk in chunks:
                    self._write(chunk)
            else:
                self._write(text)
            return
        if self._filter is not None:
            self._filter.finish()
        sent, self._round, self._filter = self._round, "", None
        rest = _remainder(sent, text) if sent else None
        if rest is None:
            self.write_new(text)
        else:
            self._write(rest)

    def write_new(self, text: str) -> None:
        """Send text that is not a continuation of what was streamed."""
        if self._on_token is None or not text:
            return
        if self._sent_any:
            self._write(_ROUND_SEPARATOR)
        self._write(text)

    def _write(self, text: str) -> None:
        if text and self._on_token is not None:
            self._sent_any = True
            self._on_token(text)


def _remainder(sent: str, text: str) -> str | None:
    """What of ``text`` is left after ``sent``; ``None`` if it diverged."""
    if text.startswith(sent):
        return text[len(sent):]
    stripped_sent, stripped_text = sent.strip(), text.strip()
    if stripped_text.startswith(stripped_sent):
        return stripped_text[len(stripped_sent):]
    return None
