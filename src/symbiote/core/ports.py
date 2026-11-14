"""Ports — abstract interfaces for the kernel's external dependencies."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from symbiote.environment.descriptors import LLMResponse


class WriteResult(Protocol):
    """What ``StoragePort.execute`` returns."""

    @property
    def rowcount(self) -> int:
        """Rows changed by the statement."""
        ...


class StoragePort(Protocol):
    """Structural interface every storage adapter must satisfy.

    An adapter can be injected with ``SymbioteKernel(config, storage=...)``.
    The project maintains two: ``SQLiteAdapter`` (the default) and
    ``PostgresAdapter``.

    **Portable SQL.** The kernel sends the same SQL text to every adapter:

    * positional ``?`` placeholders (an adapter rewrites them if its driver
      uses another style);
    * standard statements only: ``INSERT ... ON CONFLICT (...) DO UPDATE``,
      ``COALESCE``, ``REPLACE``, ``LOWER``, ``LIMIT ?``; no database-specific
      functions (dates are computed in Python and compared as ISO text);
    * text is compared and ordered by code point, and ``LOWER`` changes ASCII
      letters only (substring search goes through ``symbiote.core.sql``);
    * booleans are stored as ``0`` / ``1``, timestamps as ISO-8601 text;
    * every table has an insertion-order column ``rowid`` that may be used in
      ``ORDER BY`` (as a tie-break) and is not returned by ``SELECT *``;
    * ``ORDER BY`` on a column that may be NULL goes through ``COALESCE``
      (databases disagree on where NULL sorts), and a null-safe comparison
      through ``symbiote.core.sql.differs``.

    **Errors.** A constraint violation raises ``StorageIntegrityError``.

    **Concurrency.** ``execute`` / ``fetch_*`` may be called from any thread.
    """

    def init_schema(self) -> None:
        """Create or migrate the schema. Idempotent; the kernel calls it once."""
        ...

    def execute(self, sql: str, params: tuple | None = None) -> WriteResult:
        """Run one INSERT / UPDATE / DELETE. Outside ``transaction()`` it is
        committed before the call returns."""
        ...

    def fetch_one(self, sql: str, params: tuple | None = None) -> dict | None: ...

    def fetch_all(self, sql: str, params: tuple | None = None) -> list[dict]: ...

    def transaction(self) -> AbstractContextManager[None]:
        """Statements run by this thread inside the block are committed
        together when it ends, or all rolled back if it raises.

        Nested blocks join the outer one. Reads inside the block see its own
        writes. An exception raised by a statement must leave the block
        (after an error, the rest of the block may not run on every
        database). The block must be short: no LLM or tool call inside.
        """
        ...

    def close(self) -> None: ...


class SessionLockPort(Protocol):
    """Lock that lets one turn at a time run on a session.

    The kernel takes it before a turn and releases it after, whatever the
    outcome. Inject an implementation with
    ``SymbioteKernel(config, session_lock=...)``. The default,
    ``InProcessSessionLock``, serializes the turns of one process; hosts
    with several replicas use ``StorageSessionLock`` (a row in the kernel's
    database) or their own (Redis, advisory locks, ...).

    Contract:

    * ``acquire`` blocks until the lock of ``session_id`` is taken or
      ``wait`` seconds pass (``None`` waits forever, ``0`` tries once). It
      returns an opaque owner token, or ``None`` when the wait ran out.
    * A lock taken with ``ttl`` expires that many seconds after it was taken
      or last renewed, so a replica that died does not hold a session
      forever. ``ttl=None`` asks for the implementation's default
      (``default_ttl``; ``None`` there means the lock never expires).
    * ``renew`` extends the lock and returns ``False`` when ``token`` no
      longer owns it (it expired and another owner took it). While a turn
      runs, the kernel calls ``renew`` every
      ``KernelConfig.session_lock_renew_interval`` seconds; a ``False``, or
      errors for longer than the ttl, stop the turn before its next step.
    * ``release`` gives the lock up if ``token`` still owns it and never
      raises for a lock already lost.

    All three are called from worker threads and must be thread-safe. An
    implementation may also offer ``acquire_async`` and ``release_async``
    (same signatures, awaitable): ``message_async`` uses them when present
    and otherwise runs the blocking ones in a thread.
    """

    def acquire(
        self, session_id: str, *, wait: float | None = None, ttl: float | None = None
    ) -> str | None: ...

    def renew(self, session_id: str, token: str, *, ttl: float | None = None) -> bool: ...

    def release(self, session_id: str, token: str) -> None: ...


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

    def get(self, memory_id: str, *, symbiote_id: str | None = None) -> Any: ...

    # ``symbiote_id`` is mandatory: memory must never be read across symbiotes.
    def search(
        self,
        query: str,
        scope: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        *,
        symbiote_id: str,
    ) -> list: ...

    def get_relevant(
        self,
        intent: str,
        session_id: str | None = None,
        limit: int = 5,
        *,
        symbiote_id: str,
    ) -> list: ...

    def get_by_type(self, symbiote_id: str, entry_type: str, limit: int = 20) -> list: ...

    def deactivate(self, memory_id: str, *, symbiote_id: str | None = None) -> None: ...

    def update(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        importance: float | None = None,
        tags: list[str] | None = None,
    ) -> bool:
        """Update a memory entry in place. Returns True if updated, False if id not found.

        Only fields passed as non-None are touched. ``last_used_at`` is NOT updated
        by this method — PATCH from reflection is not a "recall" event, so it
        shouldn't reset decay timers. ``updated_at`` is bumped on every call.

        Contract: never raises on missing id (returns False). This keeps callers
        like Reflection's ``_apply_llm_facts`` simple — invalid target_id falls
        back to CREATE without exception handling on the hot path.
        """
        ...


class MessagePort(Protocol):
    """Structural interface for message retrieval (isolates SQL from consumers)."""

    def get_messages(
        self, session_id: str, limit: int = 50, *, since: str | None = None
    ) -> list[dict]:
        """Return messages for a session as dicts with 'role' and 'content', chronological order.

        With *since*, only messages created after that ISO timestamp."""
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
