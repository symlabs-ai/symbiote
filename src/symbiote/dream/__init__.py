"""Dream Mode — background memory rumination engine."""

from symbiote.dream.models import (
    BudgetTracker,
    DreamContext,
    DreamPhaseResult,
    DreamReport,
)

__all__ = [
    "BudgetTracker",
    "DreamContext",
    "DreamEngine",
    "DreamPhaseResult",
    "DreamReport",
]


def __getattr__(name: str):  # noqa: N807
    if name == "DreamEngine":
        from symbiote.dream.engine import DreamEngine
        return DreamEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
