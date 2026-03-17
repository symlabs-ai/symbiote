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
