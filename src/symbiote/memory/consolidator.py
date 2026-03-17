"""MemoryConsolidator — summarize old messages via LLM when tokens exceed threshold.

Consolidation runs in a background thread to avoid blocking the chat response.
Working memory is trimmed immediately; LLM summarization happens asynchronously.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING

from symbiote.core.models import MemoryEntry
from symbiote.core.ports import LLMPort, MemoryPort

if TYPE_CHECKING:
    from symbiote.memory.working import WorkingMemory

_log = logging.getLogger(__name__)

_CONSOLIDATION_PROMPT = """\
Summarize the following conversation messages into a concise set of key facts, \
decisions, and preferences expressed. Focus on durable information that would be \
useful in future conversations. Ignore greetings, acknowledgments, and noise.

Return ONLY a JSON array of objects with these fields:
- "content": the fact or decision (one sentence)
- "type": one of "factual", "preference", "constraint", "procedural", "decision"
- "importance": float 0.0–1.0 (how important to remember)

Messages:
{messages}

JSON array:"""


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

        # Sync mode: summarize and persist in current thread
        facts = self._summarize(to_consolidate)
        return self._persist_facts(facts, symbiote_id, session_id)

    def consolidate_sync(
        self,
        working_memory: WorkingMemory,
        symbiote_id: str,
    ) -> int:
        """Synchronous consolidation (for testing or when blocking is acceptable).

        Returns the number of facts persisted.
        """
        tokens = self._estimate_tokens(working_memory)
        if tokens <= self._token_threshold:
            return 0

        msgs = working_memory.recent_messages
        if len(msgs) <= self._keep_recent:
            return 0

        to_consolidate = list(msgs[: -self._keep_recent])
        to_keep = msgs[-self._keep_recent :]

        facts = self._summarize(to_consolidate)
        persisted = self._persist_facts(facts, symbiote_id, working_memory.session_id)

        working_memory.recent_messages = list(to_keep)
        return persisted

    def _background_consolidate(
        self, messages: list, symbiote_id: str, session_id: str
    ) -> None:
        """Run in background thread: summarize and persist."""
        try:
            facts = self._summarize(messages)
            self._persist_facts(facts, symbiote_id, session_id)
        except Exception as exc:
            _log.warning("Background consolidation failed: %s", exc)

    def _persist_facts(
        self, facts: list[dict], symbiote_id: str, session_id: str
    ) -> int:
        """Persist extracted facts as memory entries."""
        persisted = 0
        for fact in facts:
            entry = MemoryEntry(
                symbiote_id=symbiote_id,
                session_id=session_id,
                type=fact.get("type", "factual"),
                scope="session",
                content=fact.get("content", ""),
                importance=float(fact.get("importance", 0.5)),
                source="system",
            )
            self._memory.store(entry)
            persisted += 1
        return persisted

    def _estimate_tokens(self, working_memory: WorkingMemory) -> int:
        """Rough token estimate: total chars // 4."""
        total_chars = sum(
            len(m.content) for m in working_memory.recent_messages
        )
        return total_chars // 4

    def _summarize(self, messages: list) -> list[dict]:
        """Use LLM to extract durable facts from messages."""
        msg_text = "\n".join(
            f"[{m.role}]: {m.content}" for m in messages
        )
        prompt = _CONSOLIDATION_PROMPT.format(messages=msg_text)

        try:
            response = self._llm.complete(
                [{"role": "user", "content": prompt}]
            )
            return self._parse_facts(response)
        except Exception:
            # Fallback: create a single summary fact
            summary = " | ".join(
                m.content[:100] for m in messages if len(m.content) > 10
            )
            if summary:
                return [{"content": summary, "type": "factual", "importance": 0.4}]
            return []

    @staticmethod
    def _parse_facts(response: str) -> list[dict]:
        """Parse LLM JSON response into fact dicts."""
        # Try to extract JSON array from response
        text = response.strip()

        # Handle markdown code blocks
        if "```" in text:
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                text = text[start:end]

        try:
            facts = json.loads(text)
            if isinstance(facts, list):
                return [
                    f for f in facts
                    if isinstance(f, dict) and "content" in f
                ]
        except json.JSONDecodeError:
            pass
        return []
