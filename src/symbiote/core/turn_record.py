"""What entered the context of the turn in progress.

The kernel opens a ``TurnRecord`` for every ``message()`` /
``message_async()`` and persists it to ``turn_contexts`` when the turn ends,
whether or not the turn used tools and whether or not it succeeded. It lists
the memories, knowledge entries and skills the model was given:

* ``packed``: put into the system prompt when the context was assembled;
* ``on_demand``: returned to the model by ``search_memories`` /
  ``search_knowledge`` during the turn.

It says what the model saw, not what it used.
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnRecord:
    symbiote_id: str
    session_id: str
    # [{"id": ..., "source": "packed" | "on_demand"}], in the order seen.
    memories: list[dict[str, str]] = field(default_factory=list)
    knowledge: list[dict[str, str]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    tokens_estimate: int = 0
    assembled: bool = False
    # Label of the revision the turn ran on (None: the current configuration).
    revision: str | None = None
    # What the model calls made for this turn cost. The calls of a symbiote
    # this turn delegated to (``spawn``) are included: ``parent`` is the turn
    # that delegated, and every call is added up the chain.
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Parts of ``prompt_tokens``: read from / written to the provider's cache.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    parent: TurnRecord | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> None:
        """Count one model call for this turn and for the turns above it."""
        record: TurnRecord | None = self
        while record is not None:
            with record._lock:  # delegations may run in parallel
                record.llm_calls += 1
                record.prompt_tokens += prompt_tokens or 0
                record.completion_tokens += completion_tokens or 0
                record.total_tokens += total_tokens or 0
                record.cache_read_tokens += cache_read_tokens or 0
                record.cache_write_tokens += cache_write_tokens or 0
            record = record.parent

    def usage(self) -> dict[str, int]:
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }

    def add_memories(self, ids: list[str], source: str) -> None:
        _extend(self.memories, ids, source)

    def add_knowledge(self, ids: list[str], source: str) -> None:
        _extend(self.knowledge, ids, source)

    def note_context(self, context: Any) -> None:
        """Record what the ``AssembledContext`` of this turn carries."""
        self.assembled = True
        self.tokens_estimate = int(getattr(context, "total_tokens_estimate", 0) or 0)
        self.add_memories(list(getattr(context, "memory_ids", []) or []), "packed")
        self.add_knowledge(list(getattr(context, "knowledge_ids", []) or []), "packed")
        for skill in getattr(context, "injected_skills", []) or []:
            if skill.name not in self.skills:
                self.skills.append(skill.name)


def _extend(target: list[dict[str, str]], ids: list[str], source: str) -> None:
    seen = {item["id"] for item in target}
    for entry_id in ids:
        if entry_id not in seen:
            seen.add(entry_id)
            target.append({"id": entry_id, "source": source})


_current: contextvars.ContextVar[TurnRecord | None] = contextvars.ContextVar(
    "symbiote_turn_record", default=None
)


def current_turn_record() -> TurnRecord | None:
    """The record of the turn in progress, if the kernel opened one."""
    return _current.get()


def turn_budget_exceeded(max_tokens: int, max_llm_calls: int) -> bool:
    """Whether the turn in progress reached its spending ceiling (0: none).

    Counts what the turn's record holds: every model call made for it so
    far, delegated symbiotes included, and the tokens their adapters
    reported.
    """
    record = _current.get()
    if record is None:
        return False
    if max_llm_calls and record.llm_calls >= max_llm_calls:
        return True
    return bool(max_tokens and record.total_tokens >= max_tokens)


def open_turn_record(symbiote_id: str, session_id: str) -> tuple[TurnRecord, contextvars.Token]:
    record = TurnRecord(symbiote_id=symbiote_id, session_id=session_id, parent=_current.get())
    return record, _current.set(record)


def close_turn_record(token: contextvars.Token) -> None:
    _current.reset(token)
