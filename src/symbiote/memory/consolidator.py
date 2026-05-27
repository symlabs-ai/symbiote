"""MemoryConsolidator — compress old messages into a session_summary when tokens exceed threshold.

Compression runs in a background thread to avoid blocking the chat response.
Working memory is trimmed immediately; LLM summarization happens asynchronously.

This component is intentionally narrow: it ONLY produces a single
``session_summary`` MemoryEntry per consolidation. Durable fact extraction
(preference/constraint/procedural/decision) is the sole responsibility of
``ReflectionEngine`` in ``core/reflection.py``, which runs on close_session
with the engineered prompt + anti-patterns from ``core/_review_prompts.py``.
Splitting the two concerns avoids redundant LLM calls and duplicate entries
in ``memory_entries``.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from symbiote.core._review_prompts import COMPRESSION_PROMPT, render_prompt
from symbiote.core.models import MemoryEntry
from symbiote.core.ports import LLMPort, MemoryPort

if TYPE_CHECKING:
    from symbiote.memory.working import WorkingMemory

_log = logging.getLogger(__name__)

_MAX_SUMMARY_CHARS = 2400  # ~300 words * ~8 chars/word, leaves slack


class MemoryConsolidator:
    """Monitors working memory size and consolidates via LLM when threshold exceeded."""

    def __init__(
        self,
        llm: LLMPort,
        memory_store: MemoryPort,
        *,
        token_threshold: int = 2000,
        keep_recent: int = 6,
        async_mode: bool = True,
    ) -> None:
        self._llm = llm
        self._memory = memory_store
        self._token_threshold = token_threshold
        self._keep_recent = keep_recent
        self._async_mode = async_mode
        self._last_thread: threading.Thread | None = None

    def consolidate_if_needed(
        self,
        working_memory: WorkingMemory,
        symbiote_id: str,
    ) -> int:
        """Check token estimate and consolidate if over threshold.

        Trims working memory immediately (non-blocking), then runs
        LLM summarization in a background thread.

        Returns 0 if no consolidation needed, -1 if background task started.
        """
        tokens = self._estimate_tokens(working_memory)
        if tokens <= self._token_threshold:
            return 0

        msgs = working_memory.recent_messages
        if len(msgs) <= self._keep_recent:
            return 0

        # Split: old messages to consolidate, recent to keep
        to_consolidate = list(msgs[: -self._keep_recent])
        to_keep = msgs[-self._keep_recent :]

        # Trim working memory immediately (non-blocking)
        working_memory.recent_messages = list(to_keep)

        session_id = working_memory.session_id

        if self._async_mode:
            # Run LLM summarization in background thread
            thread = threading.Thread(
                target=self._background_consolidate,
                args=(to_consolidate, symbiote_id, session_id),
                daemon=True,
                name=f"consolidator-{session_id[:8]}",
            )
            thread.start()
            self._last_thread = thread
            return -1  # background task started

        # Sync mode: compress and persist in current thread
        summary = self._compress(to_consolidate)
        return self._persist_summary(summary, symbiote_id, session_id)

    def consolidate_sync(
        self,
        working_memory: WorkingMemory,
        symbiote_id: str,
    ) -> int:
        """Synchronous consolidation (for testing or when blocking is acceptable).

        Returns 1 if a session_summary was persisted, 0 otherwise.
        """
        tokens = self._estimate_tokens(working_memory)
        if tokens <= self._token_threshold:
            return 0

        msgs = working_memory.recent_messages
        if len(msgs) <= self._keep_recent:
            return 0

        to_consolidate = list(msgs[: -self._keep_recent])
        to_keep = msgs[-self._keep_recent :]

        summary = self._compress(to_consolidate)
        persisted = self._persist_summary(summary, symbiote_id, working_memory.session_id)

        working_memory.recent_messages = list(to_keep)
        return persisted

    def _background_consolidate(
        self, messages: list, symbiote_id: str, session_id: str
    ) -> None:
        """Run in background thread: compress and persist a session_summary."""
        try:
            summary = self._compress(messages)
            if summary:
                self._persist_summary(summary, symbiote_id, session_id)
        except Exception as exc:
            _log.warning("Background consolidation failed: %s", exc)

    def _persist_summary(
        self, summary: str, symbiote_id: str, session_id: str
    ) -> int:
        """Persist a compressed narrative as a single session_summary entry.

        Returns 1 on success (always one entry per consolidation), 0 if
        the summary was empty/blank.
        """
        if not summary or not summary.strip():
            return 0
        entry = MemoryEntry(
            symbiote_id=symbiote_id,
            session_id=session_id,
            type="session_summary",
            scope="session",
            content=summary.strip()[:_MAX_SUMMARY_CHARS],
            importance=0.5,
            source="system",
        )
        self._memory.store(entry)
        return 1

    def _estimate_tokens(self, working_memory: WorkingMemory) -> int:
        """Rough token estimate: total chars // 4."""
        total_chars = sum(
            len(m.content) for m in working_memory.recent_messages
        )
        return total_chars // 4

    def _compress(self, messages: list) -> str:
        """Use LLM to compress messages into a narrative summary.

        Falls back to a naive pipe-joined truncation if the LLM call fails,
        so consolidation never silently drops the window without a trace.
        """
        msg_text = "\n".join(
            f"[{m.role}]: {m.content}" for m in messages
        )
        prompt = render_prompt(COMPRESSION_PROMPT, messages=msg_text)

        try:
            response = self._llm.complete(
                [{"role": "user", "content": prompt}]
            )
            # LLMPort may return str or LLMResponse — extract text
            text = response if isinstance(response, str) else getattr(response, "content", "")
            return text.strip()
        except Exception as exc:
            _log.warning("LLM compression failed, falling back to naive: %s", exc)
            return " | ".join(
                m.content[:100] for m in messages if len(m.content) > 10
            )
