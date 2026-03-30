"""ReflectionEngine — extract durable facts from session messages."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from symbiote.core.models import MemoryEntry
from symbiote.core.ports import MemoryPort, MessagePort


class ReflectionResult(BaseModel):
    """Result of a reflection pass over session messages."""

    session_id: str
    summary: str
    extracted_facts: list[dict] = Field(default_factory=list)
    discarded_count: int = 0
    persisted_count: int = 0


# Keywords that signal durable facts, mapped to fact type
_KEYWORD_MAP: dict[str, str] = {
    "prefer": "preference",
    "always": "constraint",
    "never": "constraint",
    "rule": "constraint",
    "convention": "procedural",
    "procedure": "procedural",
    "constraint": "constraint",
}

# Noise patterns — short or common acknowledgments
_NOISE_PATTERNS: set[str] = {
    "ok",
    "okay",
    "yes",
    "no",
    "thanks",
    "thank you",
    "got it",
    "sure",
    "right",
    "yep",
    "nope",
    "cool",
    "nice",
    "great",
    "done",
    "alright",
    "understood",
    "noted",
    "ack",
    "k",
}

_NOISE_RE = re.compile(
    r"^(" + "|".join(re.escape(p) for p in _NOISE_PATTERNS) + r")[.!?]*$",
    re.IGNORECASE,
)


class ReflectionEngine:
    """Extracts durable knowledge from session messages and persists it."""

    def __init__(self, memory_store: MemoryPort, messages: MessagePort) -> None:
        self._memory_store = memory_store
        self._messages = messages

    # ── public API ─────────────────────────────────────────────────────

    def reflect_session(
        self, session_id: str, symbiote_id: str, *, limit: int = 50
    ) -> ReflectionResult:
        """Run full reflection on a session's messages."""
        messages = self._fetch_messages(session_id, limit)

        discarded_count = sum(1 for m in messages if self._is_noise(m["content"]))
        facts = self._extract_durable_facts(messages)
        summary = self._generate_summary(messages)

        persisted_count = 0
        for fact in facts:
            # Use the fact type directly when it matches a MemoryEntry type,
            # otherwise fall back to "reflection"
            mem_type = fact["type"] if fact["type"] in (
                "preference", "constraint", "procedural", "factual", "decision",
            ) else "reflection"
            entry = MemoryEntry(
                symbiote_id=symbiote_id,
                session_id=session_id,
                type=mem_type,
                scope="global",
                content=fact["content"],
                tags=[fact["type"]],
                importance=fact["importance"],
                source="reflection",
            )
            self._memory_store.store(entry)
            persisted_count += 1

        return ReflectionResult(
            session_id=session_id,
            summary=summary,
            extracted_facts=facts,
            discarded_count=discarded_count,
            persisted_count=persisted_count,
        )

    def reflect_task(
        self,
        session_id: str,
        symbiote_id: str,
        task_description: str,
        *,
        limit: int = 20,
    ) -> ReflectionResult:
        """Lighter reflection scoped to task context."""
        messages = self._fetch_messages(session_id, limit)
        facts = self._extract_durable_facts(messages)
        summary = f"Task: {task_description}\n{self._generate_summary(messages)}"

        discarded_count = sum(1 for m in messages if self._is_noise(m["content"]))

        persisted_count = 0
        for fact in facts:
            mem_type = fact["type"] if fact["type"] in (
                "preference", "constraint", "procedural", "factual", "decision",
            ) else "reflection"
            entry = MemoryEntry(
                symbiote_id=symbiote_id,
                session_id=session_id,
                type=mem_type,
                scope="global",
                content=fact["content"],
                tags=[fact["type"], "task"],
                importance=fact["importance"],
                source="reflection",
            )
            self._memory_store.store(entry)
            persisted_count += 1

        return ReflectionResult(
            session_id=session_id,
            summary=summary,
            extracted_facts=facts,
            discarded_count=discarded_count,
            persisted_count=persisted_count,
        )

    # ── internal methods ───────────────────────────────────────────────

    def _extract_durable_facts(self, messages: list[dict]) -> list[dict]:
        """Heuristic extraction: look for keyword signals in message content."""
        facts: list[dict] = []
        for msg in messages:
            content = msg["content"]
            if self._is_noise(content):
                continue
            content_lower = content.lower()
            for keyword, fact_type in _KEYWORD_MAP.items():
                if keyword in content_lower:
                    importance = 0.8 if fact_type == "constraint" else 0.6
                    facts.append(
                        {
                            "content": content,
                            "type": fact_type,
                            "importance": importance,
                        }
                    )
                    break  # one fact per message
        return facts

    def _is_noise(self, content: str) -> bool:
        """Return True if message is too short or matches noise patterns."""
        stripped = content.strip()
        if len(stripped) < 10:
            return True
        return bool(_NOISE_RE.match(stripped))

    def _generate_summary(self, messages: list[dict]) -> str:
        """Concatenate non-noise messages into a summary paragraph."""
        parts = [
            msg["content"]
            for msg in messages
            if not self._is_noise(msg["content"])
        ]
        return " ".join(parts)

    # ── private helpers ────────────────────────────────────────────────

    def _fetch_messages(self, session_id: str, limit: int) -> list[dict]:
        """Fetch last N messages for a session via MessagePort."""
        return self._messages.get_messages(session_id, limit)
