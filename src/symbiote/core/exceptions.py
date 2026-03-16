"""Domain exceptions for Symbiote kernel."""


class SymbioteError(Exception):
    """Base exception for all Symbiote domain errors."""


class EntityNotFoundError(SymbioteError):
    """Raised when a requested entity does not exist."""

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} {entity_id!r} not found")


class ValidationError(SymbioteError):
    """Raised when a domain validation rule is violated."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
