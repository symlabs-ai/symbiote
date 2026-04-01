"""CrossSymbioteLearner — transfer harness improvements across symbiotes.

When one symbiote discovers an effective harness version (e.g. better
tool_instructions), symbiotes with similar tool configurations can
benefit from the same improvement.  Similarity is measured via Jaccard
overlap of their tool sets.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from symbiote.core.ports import StoragePort
from symbiote.harness.versions import HarnessVersionRepository

logger = logging.getLogger(__name__)


@dataclass
class LearningTransfer:
    """A harness improvement that can be transferred between symbiotes."""

    source_symbiote: str
    target_symbiote: str
    component: str  # harness_versions component
    source_version: int
    content: str
    source_avg_score: float
    tool_overlap: float  # 0.0-1.0, how similar the tool sets are


class CrossSymbioteLearner:
    """Identifies and transfers harness improvements across symbiotes."""

    def __init__(
        self, storage: StoragePort, versions: HarnessVersionRepository
    ) -> None:
        self._storage = storage
        self._versions = versions

    def find_candidates(
        self, target_symbiote_id: str, min_overlap: float = 0.5
    ) -> list[LearningTransfer]:
        """Find harness versions from other symbiotes that could benefit the target.

        Criteria:
        1. Source symbiote has a custom harness version with good score (>= 0.7)
        2. Source and target share >= min_overlap of their tool sets
        3. Target doesn't already have a custom version for that component
        """
        # Get all active harness versions from other symbiotes
        rows = self._storage.fetch_all(
            "SELECT * FROM harness_versions "
            "WHERE symbiote_id != ? AND is_active = 1 AND avg_score >= 0.7 "
            "ORDER BY avg_score DESC",
            (target_symbiote_id,),
        )

        # Get components where target already has a custom version
        target_components = {
            r["component"]
            for r in self._storage.fetch_all(
                "SELECT DISTINCT component FROM harness_versions "
                "WHERE symbiote_id = ? AND is_active = 1",
                (target_symbiote_id,),
            )
        }

        candidates: list[LearningTransfer] = []
        # Cache tool overlap per source symbiote
        overlap_cache: dict[str, float] = {}

        for row in rows:
            source_id = row["symbiote_id"]
            component = row["component"]

            # Skip if target already has this component
            if component in target_components:
                continue

            # Compute tool overlap (cached)
            if source_id not in overlap_cache:
                overlap_cache[source_id] = self.compute_tool_overlap(
                    source_id, target_symbiote_id
                )

            overlap = overlap_cache[source_id]
            if overlap < min_overlap:
                continue

            candidates.append(
                LearningTransfer(
                    source_symbiote=source_id,
                    target_symbiote=target_symbiote_id,
                    component=component,
                    source_version=row["version"],
                    content=row["content"],
                    source_avg_score=row["avg_score"],
                    tool_overlap=overlap,
                )
            )

        return candidates

    def compute_tool_overlap(self, symbiote_a: str, symbiote_b: str) -> float:
        """Jaccard similarity of tool sets between two symbiotes."""
        tools_a = self._get_tools(symbiote_a)
        tools_b = self._get_tools(symbiote_b)

        if not tools_a and not tools_b:
            return 1.0  # Both have no tools — considered identical
        if not tools_a or not tools_b:
            return 0.0

        intersection = tools_a & tools_b
        union = tools_a | tools_b
        return len(intersection) / len(union)

    def transfer(self, transfer: LearningTransfer) -> int:
        """Apply a learning transfer — create new version on target symbiote.

        Returns the new version number.
        """
        new_version = self._versions.create_version(
            symbiote_id=transfer.target_symbiote,
            component=transfer.component,
            content=transfer.content,
            parent_version=None,  # No parent — transferred from another symbiote
        )

        logger.info(
            "[cross-learning] transferred %s v%d from %s to %s (overlap=%.2f, score=%.2f) → v%d",
            transfer.component,
            transfer.source_version,
            transfer.source_symbiote[:8],
            transfer.target_symbiote[:8],
            transfer.tool_overlap,
            transfer.source_avg_score,
            new_version,
        )

        return new_version

    # ── Internal ──────────────────────────────────────────────────────────

    def _get_tools(self, symbiote_id: str) -> set[str]:
        """Get the tool set for a symbiote from environment_configs."""
        row = self._storage.fetch_one(
            "SELECT tools_json FROM environment_configs "
            "WHERE symbiote_id = ? AND workspace_id IS NULL",
            (symbiote_id,),
        )
        if row is None or not row["tools_json"]:
            return set()
        try:
            tools = json.loads(row["tools_json"])
            return set(tools) if isinstance(tools, list) else set()
        except (json.JSONDecodeError, TypeError):
            return set()
