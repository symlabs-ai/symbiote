"""WorkingMemory — immediate operational state for a session."""

from __future__ import annotations

from symbiote.core.models import Decision, Message


class WorkingMemory:
    """Maintains in-memory operational state for a single session.

    Not persisted directly; use :meth:`snapshot` to export state
    for context assembly or storage.
    """

    def __init__(self, session_id: str, max_messages: int = 20) -> None:
        self.session_id = session_id
        self._max_messages = max_messages

        self.recent_messages: list[Message] = []
        self.current_goal: str | None = None
        self.active_plan: str | None = None
        self.active_files: list[str] = []
        self.recent_decisions: list[Decision] = []

    # ── mutations ─────────────────────────────────────────────────────

    def update_message(self, message: Message) -> None:
        """Append a message, trimming oldest when exceeding max_messages."""
        self.recent_messages.append(message)
        if len(self.recent_messages) > self._max_messages:
            self.recent_messages = self.recent_messages[-self._max_messages:]

    def update_goal(self, goal: str) -> None:
        self.current_goal = goal

    def update_plan(self, plan: str) -> None:
        self.active_plan = plan

    def add_active_file(self, path: str) -> None:
        if path not in self.active_files:
            self.active_files.append(path)

    def remove_active_file(self, path: str) -> None:
        if path in self.active_files:
            self.active_files.remove(path)

    def add_decision(self, decision: Decision) -> None:
        self.recent_decisions.append(decision)

    # ── queries ───────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Export full state as a plain dict for context assembly."""
        return {
            "session_id": self.session_id,
            "recent_messages": [
                {"role": m.role, "content": m.content}
                for m in self.recent_messages
            ],
            "current_goal": self.current_goal,
            "active_plan": self.active_plan,
            "active_files": list(self.active_files),
            "recent_decisions": [
                {"title": d.title, "description": d.description}
                for d in self.recent_decisions
            ],
        }

    def clear(self) -> None:
        """Reset all state except session_id."""
        self.recent_messages = []
        self.current_goal = None
        self.active_plan = None
        self.active_files = []
        self.recent_decisions = []
