"""ParameterTuner — auto-calibrate harness parameters from session data.

Uses tiered activation to be safe with little data and more aggressive
with more data.  Works from day zero: with no sessions, defaults are
unchanged.  With 5 sessions, only obvious safe adjustments are made.

Tier 0 (0 sessions):   Defaults, no adjustments.
Tier 1 (5-19 sessions): Only adjustments that cannot degrade quality
                         (e.g. increasing a limit that's being hit 100%).
Tier 2 (20-49 sessions): Statistical adjustments with moderate confidence.
Tier 3 (50+ sessions):  Full tuning with rollback safety net.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from symbiote.core.ports import StoragePort

logger = logging.getLogger(__name__)

# ── Tier thresholds ──────────────────────────────────────────────────────────

TIER_1_MIN = 5
TIER_2_MIN = 20
TIER_3_MIN = 50

# ── Caps (absolute limits for safety) ────────────────────────────────────────

MAX_TOOL_ITERATIONS_CAP = 30
MIN_TOOL_ITERATIONS = 3
MIN_COMPACTION_THRESHOLD = 2
MAX_COMPACTION_THRESHOLD = 10


@dataclass
class TuningResult:
    """Result of a tuning run — what was analyzed and what changed."""

    symbiote_id: str
    session_count: int
    tier: int
    adjustments: dict[str, Any] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    applied: bool = False


class ParameterTuner:
    """Analyzes session scores and traces to recommend parameter adjustments.

    The tuner reads from ``session_scores`` and ``execution_traces`` tables
    (populated by Fase 1 foundations), computes statistics, and applies
    adjustments via ``EnvironmentManager.configure()``.

    Safe by design:
    - Each adjustment has a tier requirement (minimum sessions)
    - Each adjustment is capped (cannot exceed absolute limits)
    - Applied adjustments are logged for auditability
    - Rollback is possible via EnvironmentManager.configure() with original values
    """

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    def analyze(self, symbiote_id: str, days: int = 7) -> TuningResult:
        """Analyze recent sessions and compute recommended adjustments.

        Does NOT apply changes — call ``apply()`` with the result to persist.

        Instant-mode sessions are excluded from iteration/compaction rules
        (they always have 1 iteration — no signal).  They ARE included in
        memory/knowledge share analysis (Tier 3) since context quality
        matters even more in single-shot mode.
        """
        traces = self._get_recent_traces(symbiote_id, days)
        scores = self._get_recent_scores(symbiote_id, days)
        # Separate instant traces — they have no iteration signal
        loop_traces = [t for t in traces if t.get("tool_mode", "brief") != "instant"]
        count = len(loop_traces)
        tier = self._compute_tier(count)

        result = TuningResult(
            symbiote_id=symbiote_id,
            session_count=count,
            tier=tier,
        )

        if tier == 0:
            return result  # no data, no adjustments

        # Get current config values
        current = self._get_current_config(symbiote_id)

        # ── Tier 1+ rules (safe adjustments) ─────────────────────────
        # Use loop_traces (excluding instant) for iteration-based rules
        if tier >= 1:
            self._rule_max_iterations_too_low(result, loop_traces, current)
            self._rule_max_iterations_too_high(result, loop_traces, current)

        # ── Tier 2+ rules (statistical adjustments) ──────────────────
        if tier >= 2:
            self._rule_compaction_threshold(result, loop_traces, current)

        # ── Tier 3+ rules (fine tuning) ──────────────────────────────
        # memory/knowledge share uses ALL scores (instant included —
        # context quality matters even more in single-shot mode)
        if tier >= 3:
            self._rule_memory_knowledge_share(result, scores, traces, current)

        return result

    def apply(
        self, result: TuningResult, env_manager: object
    ) -> TuningResult:
        """Apply the adjustments from a TuningResult via EnvironmentManager.

        Returns the same result with ``applied=True`` if changes were made.
        """
        if not result.adjustments:
            return result

        # env_manager is EnvironmentManager but we avoid import for decoupling
        configure = getattr(env_manager, "configure", None)
        if configure is None:
            logger.warning("[tuner] env_manager has no configure method")
            return result

        # Filter to only params that configure() accepts
        import inspect
        valid_params = set(inspect.signature(configure).parameters) - {"self"}
        applicable = {k: v for k, v in result.adjustments.items() if k in valid_params}
        skipped = {k: v for k, v in result.adjustments.items() if k not in valid_params}

        for param, value in skipped.items():
            logger.warning(
                "[tuner] symbiote=%s skipped %s=%s (not a configure() param)",
                result.symbiote_id[:8], param, value,
            )

        if applicable:
            configure(symbiote_id=result.symbiote_id, **applicable)

        result.applied = bool(applicable)

        for param, value in applicable.items():
            logger.info(
                "[tuner] symbiote=%s tier=%d adjusted %s=%s reason=%s",
                result.symbiote_id[:8],
                result.tier,
                param,
                value,
                result.reasons.get(param, ""),
            )

        return result

    # ── Tuning rules ─────────────────────────────────────────────────────

    def _rule_max_iterations_too_low(
        self, result: TuningResult, traces: list[dict], current: dict
    ) -> None:
        """Tier 1: If too many sessions hit max_iterations, increase the limit.

        Safe because increasing the limit only allows more work, never less.
        Tier 1 requires >80% hitting the limit (very conservative).
        Tier 2+ requires >30%.
        """
        if not traces:
            return

        max_iter_count = sum(1 for t in traces if t["stop_reason"] == "max_iterations")
        ratio = max_iter_count / len(traces)

        threshold = 0.80 if result.tier == 1 else 0.30
        current_max = current.get("max_tool_iterations", 10)

        if ratio > threshold and current_max < MAX_TOOL_ITERATIONS_CAP:
            new_value = min(current_max + 5, MAX_TOOL_ITERATIONS_CAP)
            result.adjustments["max_tool_iterations"] = new_value
            result.reasons["max_tool_iterations"] = (
                f"{ratio:.0%} of sessions hit max_iterations ({current_max}); "
                f"increasing to {new_value}"
            )

    def _rule_max_iterations_too_high(
        self, result: TuningResult, traces: list[dict], current: dict
    ) -> None:
        """Tier 1: If successful sessions complete in very few iterations, lower the cap.

        Safe because we only lower to 2x the observed max, ensuring headroom.
        """
        if not traces:
            return

        successful = [t for t in traces if t["stop_reason"] == "end_turn"]
        if len(successful) < TIER_1_MIN:
            return

        max_observed = max(t["total_iterations"] for t in successful)
        current_max = current.get("max_tool_iterations", 10)
        suggested = max(max_observed * 2, MIN_TOOL_ITERATIONS)

        if suggested < current_max - 2:  # only if meaningful reduction
            result.adjustments["max_tool_iterations"] = suggested
            result.reasons["max_tool_iterations"] = (
                f"Successful sessions use at most {max_observed} iterations; "
                f"lowering from {current_max} to {suggested} (2x headroom)"
            )

    def _rule_compaction_threshold(
        self, result: TuningResult, traces: list[dict], current: dict
    ) -> None:
        """Tier 2: Adjust compaction threshold based on average iteration count.

        If avg iterations in successful sessions is below the compaction threshold,
        compaction never triggers — wasteful.  Lower the threshold.
        If most sessions trigger compaction, the threshold may be too low.
        """
        successful = [t for t in traces if t["stop_reason"] == "end_turn"]
        if len(successful) < TIER_2_MIN:
            return

        avg_iters = sum(t["total_iterations"] for t in successful) / len(successful)
        # Compaction threshold is in pairs, not iterations — 4 pairs = 8 messages
        # But we compare against iteration count for simplicity
        current_threshold = current.get("compaction_threshold", 4)

        if avg_iters < current_threshold and current_threshold > MIN_COMPACTION_THRESHOLD:
            new_threshold = max(int(avg_iters), MIN_COMPACTION_THRESHOLD)
            if new_threshold < current_threshold:
                result.adjustments["compaction_threshold"] = new_threshold
                result.reasons["compaction_threshold"] = (
                    f"Avg successful iterations ({avg_iters:.1f}) below compaction threshold "
                    f"({current_threshold}); lowering to {new_threshold}"
                )

    def _rule_memory_knowledge_share(
        self, result: TuningResult, scores: list[dict], traces: list[dict], current: dict
    ) -> None:
        """Tier 3: Adjust memory/knowledge share based on tool usage patterns.

        If most sessions use tools heavily (high tool_calls), the LLM benefits
        more from tool context than from memories — shift share toward tools
        by reducing memory/knowledge shares.

        Conservative: only adjusts if strong signal and within bounds.
        """
        if len(scores) < TIER_3_MIN:
            return

        # Compare scores of sessions with many tool calls vs few
        high_tool = [s for s in scores if s["total_tool_calls"] >= 3]
        low_tool = [s for s in scores if s["total_tool_calls"] == 0]

        if not high_tool or not low_tool:
            return

        avg_high = sum(s["final_score"] for s in high_tool) / len(high_tool)
        avg_low = sum(s["final_score"] for s in low_tool) / len(low_tool)

        current_mem = current.get("memory_share", 0.40)

        # If tool-heavy sessions score significantly lower, they might need
        # less memory/knowledge overhead to leave room for tool context
        if avg_high < avg_low - 0.1 and current_mem > 0.20:
            result.adjustments["memory_share"] = round(current_mem - 0.05, 2)
            result.reasons["memory_share"] = (
                f"Tool-heavy sessions avg score ({avg_high:.2f}) significantly lower than "
                f"no-tool sessions ({avg_low:.2f}); reducing memory_share by 0.05"
            )

    # ── Data access ──────────────────────────────────────────────────────

    def _get_recent_traces(self, symbiote_id: str, days: int) -> list[dict]:
        return self._storage.fetch_all(
            "SELECT * FROM execution_traces "
            "WHERE symbiote_id = ? AND created_at >= datetime('now', ?) "
            "ORDER BY created_at DESC",
            (symbiote_id, f"-{days} days"),
        )

    def _get_recent_scores(self, symbiote_id: str, days: int) -> list[dict]:
        return self._storage.fetch_all(
            "SELECT * FROM session_scores "
            "WHERE symbiote_id = ? AND computed_at >= datetime('now', ?) "
            "ORDER BY computed_at DESC",
            (symbiote_id, f"-{days} days"),
        )

    def _get_current_config(self, symbiote_id: str) -> dict:
        row = self._storage.fetch_one(
            "SELECT * FROM environment_configs "
            "WHERE symbiote_id = ? AND workspace_id IS NULL",
            (symbiote_id,),
        )
        if row is None:
            return {
                "max_tool_iterations": 10,
                "compaction_threshold": 4,
                "memory_share": 0.40,
                "knowledge_share": 0.25,
            }
        return dict(row)

    @staticmethod
    def _compute_tier(session_count: int) -> int:
        if session_count >= TIER_3_MIN:
            return 3
        if session_count >= TIER_2_MIN:
            return 2
        if session_count >= TIER_1_MIN:
            return 1
        return 0
