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


def open_turn_record(symbiote_id: str, session_id: str) -> tuple[TurnRecord, contextvars.Token]:
    record = TurnRecord(symbiote_id=symbiote_id, session_id=session_id)
    return record, _current.set(record)


def close_turn_record(token: contextvars.Token) -> None:
    _current.reset(token)
