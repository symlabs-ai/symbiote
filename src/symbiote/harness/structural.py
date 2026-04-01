"""StructuralEvolver — registry-based structural evolution.

Enables code-level harness changes beyond text evolution.  Pluggable
strategy functions analyze session data and propose structural changes
(parameter adjustments, strategy swaps, pipeline modifications).

Only "parameter" changes are auto-applicable for now; other types are
recorded for manual review.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from symbiote.core.ports import StoragePort

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class StructuralProposal:
    """A proposed structural change to the harness."""

    id: str
    component: str  # e.g. "context_assembler", "chat_runner"
    change_type: str  # "parameter", "strategy", "pipeline"
    description: str
    current_value: Any
    proposed_value: Any
    confidence: float  # 0.0-1.0, how confident the proposer is
    evidence: str  # why this change is proposed


# ── Strategy type alias ───────────────────────────────────────────────────────

EvolutionStrategy = Callable[[StoragePort, str], list[StructuralProposal]]


# ── Evolver ───────────────────────────────────────────────────────────────────


class StructuralEvolver:
    """Registry-based structural evolution.

    Strategies register themselves and are called during evolution cycles.
    Each strategy analyzes data and may propose changes.
    """

    def __init__(self) -> None:
        self._strategies: list[EvolutionStrategy] = []

    def register_strategy(
        self, fn: EvolutionStrategy
    ) -> None:
        """Register an evolution strategy.

        Strategy signature: (storage, symbiote_id) -> list[StructuralProposal]
        """
        self._strategies.append(fn)

    def propose(
        self, storage: StoragePort, symbiote_id: str
    ) -> list[StructuralProposal]:
        """Run all registered strategies and collect proposals."""
        proposals: list[StructuralProposal] = []
        for strategy in self._strategies:
            try:
                proposals.extend(strategy(storage, symbiote_id))
            except Exception as exc:
                logger.warning("[structural] strategy %s failed: %s", strategy.__name__, exc)
        return proposals

    def apply(self, proposal: StructuralProposal, env_manager: object) -> bool:
        """Apply a structural proposal via EnvironmentManager.

        Only "parameter" type is auto-applicable for now.
        """
        if proposal.change_type != "parameter":
            return False
        configure = getattr(env_manager, "configure", None)
        if configure is None:
            return False
        try:
            configure(
                symbiote_id=proposal.component,
                **{proposal.description: proposal.proposed_value},
            )
            return True
        except Exception as exc:
            logger.warning(
                "[structural] apply failed for %s: %s", proposal.id, exc
            )
            return False


# ── Built-in strategy: context_mode recommendation ────────────────────────────


def strategy_context_mode(
    storage: StoragePort, symbiote_id: str
) -> list[StructuralProposal]:
    """Recommend switching to on_demand if memories are rarely useful.

    Placeholder — real implementation needs memory usage tracking.
    """
    return []
