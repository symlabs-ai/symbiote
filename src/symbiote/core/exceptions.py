"""Domain exceptions for Symbiote kernel."""


class SymbioteError(Exception):
    """Base exception for all Symbiote domain errors."""


class EntityNotFoundError(SymbioteError):
    """Raised when a requested entity does not exist."""

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} {entity_id!r} not found")


class ValidationError(SymbioteError, ValueError):
    """Raised when a domain validation rule is violated.

    Also a ``ValueError`` so callers that caught the plain ``ValueError``
    raised before this type was used keep working.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvariantViolationError(SymbioteError):
    """Raised when a domain invariant is broken."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class BusinessRuleViolationError(SymbioteError):
    """Raised when a business rule is violated."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DuplicateEntityError(SymbioteError):
    """Raised when attempting to create an entity that already exists."""

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} {entity_id!r} already exists")


class StorageIntegrityError(SymbioteError):
    """Raised by a storage adapter when a write violates a constraint
    (unique key, foreign key, NOT NULL).

    Every adapter translates its driver's error into this type, so the kernel
    and the hosts catch one exception whatever the database is.
    """


class SessionBusyError(SymbioteError):
    """Raised by ``kernel.message()`` when the session's lock could not be
    taken within ``KernelConfig.session_lock_wait``: another turn is running
    on that session. Nothing was stored; the turn can be retried."""

    def __init__(self, session_id: str, waited: float | None = None) -> None:
        self.session_id = session_id
        self.waited = waited
        suffix = f" after {waited:g}s" if waited is not None else ""
        super().__init__(f"session {session_id!r} is busy with another turn{suffix}")


class SessionLockLostError(SymbioteError):
    """Raised by ``kernel.message()`` when the session's lock could not be
    renewed while the turn ran. The turn stopped before its next step (no
    further model or tool call started) and stored no reply; what had already
    run (tool side effects, memories written by tools) is not undone."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(
            f"the lock of session {session_id!r} was lost during the turn; "
            "the turn was stopped"
        )


class LLMError(SymbioteError):
    """Raised when an LLM operation fails.

    ``status_code`` is the provider's HTTP status when known. ``retryable``
    says whether retrying the same request may succeed; ``None`` means the
    adapter could not tell (callers fall back to ``is_transient_llm_error``).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


# Statuses worth retrying: timeouts and server-side failures. 429 is left
# out on purpose: most provider SDKs already retry rate limits internally
# (forge_llm only forwards max_retries=0 to the symrouter provider), and a
# second retry layer multiplies the requests that caused the limit.
_TRANSIENT_STATUSES = frozenset({408, 425, 500, 502, 503, 504, 529})
_NOT_TRANSIENT_OS_ERRORS = (
    FileNotFoundError,
    FileExistsError,
    IsADirectoryError,
    NotADirectoryError,
    PermissionError,
)
_TRANSIENT_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "RemoteProtocolError",
        "RequestTimeoutError",
        "ServiceUnavailableError",
    }
)


def _exception_chain(exc: BaseException, limit: int = 6) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < limit:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def llm_error_status(exc: BaseException) -> int | None:
    """HTTP status carried by ``exc`` or any exception it wraps, if any."""
    for item in _exception_chain(exc):
        for candidate in (
            getattr(item, "status_code", None),
            getattr(item, "status", None),
            getattr(getattr(item, "response", None), "status_code", None),
        ):
            if isinstance(candidate, int) and 100 <= candidate <= 599:
                return candidate
    return None


def is_transient_llm_error(exc: BaseException) -> bool:
    """Whether retrying the request that raised ``exc`` may succeed.

    Decided by the HTTP status (anywhere in the exception chain) or by the
    exception type — never by words in the message, which may quote user
    input or request ids.
    """
    if isinstance(exc, LLMError) and exc.retryable is not None:
        return exc.retryable
    status = llm_error_status(exc)
    if status is not None:
        return status in _TRANSIENT_STATUSES or status >= 500
    for item in _exception_chain(exc):
        if isinstance(item, _NOT_TRANSIENT_OS_ERRORS):
            return False
        if isinstance(item, OSError):  # ConnectionError, TimeoutError, socket errors
            return True
        if type(item).__name__ in _TRANSIENT_NAMES:
            return True
    return False
