"""Ports — abstract interfaces for the kernel's external dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from symbiote.environment.descriptors import LLMResponse


class StoragePort(Protocol):
    """Structural interface every storage adapter must satisfy."""

    def execute(self, sql: str, params: tuple | None = None) -> Any: ...

    def fetch_one(self, sql: str, params: tuple | None = None) -> dict | None: ...

    def fetch_all(self, sql: str, params: tuple | None = None) -> list[dict]: ...

    def close(self) -> None: ...


@runtime_checkable
class LLMPort(Protocol):
    """Structural interface for LLM adapters.

    Adapters may return either:
    - ``str`` — plain text (backward compatible, text-based tool parsing applies)
    - ``LLMResponse`` — structured response with optional native tool_calls

    Hosts that don't use native function calling can keep returning ``str``.
    """

    def complete(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> str | LLMResponse: ...


class MemoryPort(Protocol):
    """Structural interface for memory storage operations."""

    def store(self, entry: Any) -> str: ...

    def get(self, memory_id: str) -> Any: ...

    def search(self, query: str, scope: str | None = None, tags: list[str] | None = None, limit: int = 10) -> list: ...

    def get_relevant(self, intent: str, session_id: str | None = None, limit: int = 5) -> list: ...

    def get_by_type(self, symbiote_id: str, entry_type: str, limit: int = 20) -> list: ...

    def deactivate(self, memory_id: str) -> None: ...


class MessagePort(Protocol):
    """Structural interface for message retrieval (isolates SQL from consumers)."""

    def get_messages(
        self, session_id: str, limit: int = 50
    ) -> list[dict]:
        """Return messages for a session as dicts with 'role' and 'content', chronological order."""
        ...


class KnowledgePort(Protocol):
    """Structural interface for knowledge operations."""

    def query(self, symbiote_id: str, theme: str, limit: int = 10) -> list: ...


class FeedbackPort(Protocol):
    """Port for reporting session quality feedback.

    The host calls ``report()`` when it has a signal about session quality
    (user thumbs up/down, task completion, etc.).  The kernel composes
    this with the auto_score from LoopTrace to compute a final_score.
    """

    def report(self, session_id: str, score: float, source: str = "user") -> None:
        """Report a quality score (0.0–1.0) for a session."""
        ...


class SessionRecallPort(Protocol):
    """Port for searching past session transcripts.

    The kernel defines this contract; the host decides the implementation
    (FTS5, Elasticsearch, embedding search, etc.).  This keeps the kernel
    free of search-engine opinions while enabling persistent recall.

    Usage by the host::

        class MySessionRecall:
            def search_messages(self, query, **kwargs):
                # FTS5 / embedding / whatever
                return results

        kernel.set_session_recall(MySessionRecall())
    """

    def search_messages(
        self,
        query: str,
        symbiote_id: str | None = None,
        session_id: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search past messages across sessions.

        Returns dicts with at least: session_id, role, content, timestamp.
        Hosts may include additional fields (score, highlights, etc.).
        """
        ...

    def search_sessions(
        self,
        query: str,
        symbiote_id: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """Search sessions by goal, summary, or content.

        Returns dicts with at least: session_id, goal, summary, started_at.
        """
        ...
