"""HarnessEvolver — LLM-powered evolution of harness text components.

Uses a proposer LLM to analyze session traces (failures vs successes)
and propose improved versions of evolvable harness texts.  Guard rails
ensure proposals don't degrade quality.

Evolvable components:
  - tool_instructions: rules for how the LLM should use tools
  - injection_stagnation: message when loop stagnates
  - injection_circuit_breaker: message when circuit breaker triggers

The evolver is a batch job — not called per-request.  The host invokes
it periodically (e.g. weekly cron, CLI command, or after N sessions).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from symbiote.harness.versions import HarnessVersionRepository

if TYPE_CHECKING:
    from symbiote.core.ports import LLMPort, StoragePort

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

EVOLVABLE_COMPONENTS = frozenset({
    "tool_instructions",
    "injection_stagnation",
    "injection_circuit_breaker",
})

# Guard rail limits
MAX_LENGTH_MULTIPLIER = 2.0
MIN_SESSIONS_FOR_VERDICT = 50
ROLLBACK_THRESHOLD = 0.05  # rollback if new_avg < old_avg - this
MIN_EVOLUTION_INTERVAL_DAYS = 7

# Minimum failed/successful sessions to attempt evolution
MIN_FAILED_SESSIONS = 5
MIN_SUCCESSFUL_SESSIONS = 3

_PROPOSER_PROMPT = """\
You are an expert at writing instructions for AI agents that use tools.

## Current instructions
{current_text}

## Sessions that FAILED (low score)
{failed_summary}

## Sessions that SUCCEEDED (high score)
{success_summary}

## Task
Analyze the failure patterns. Propose an improved version of the instructions \
that addresses the observed problems without breaking what already works.

Rules:
- Keep the same general structure and format
- Lines marked CRITICAL must be preserved exactly
- Be concise — do not add unnecessary verbosity
- Return ONLY the improved text, no explanation or markdown wrapper
"""


@dataclass
class EvolutionResult:
    """Result of an evolution attempt."""

    symbiote_id: str
    component: str
    success: bool
    new_version: int | None = None
    reason: str = ""
    guard_rail_failed: str | None = None


@dataclass
class RollbackCheck:
    """Result of a rollback check."""

    should_rollback: bool
    reason: str = ""
    old_avg: float = 0.0
    new_avg: float = 0.0
    session_count: int = 0


class HarnessEvolver:
    """Proposes and validates improved harness texts using an LLM.

    The evolver reads from session_scores and execution_traces, asks
    a proposer LLM for improvements, validates with guard rails, and
    persists via HarnessVersionRepository.
    """

    def __init__(
        self,
        storage: StoragePort,
        versions: HarnessVersionRepository,
        proposer_llm: LLMPort | None = None,
    ) -> None:
        self._storage = storage
        self._versions = versions
        self._proposer_llm = proposer_llm

    def set_proposer_llm(self, llm: LLMPort) -> None:
        """Inject or replace the proposer LLM."""
        self._proposer_llm = llm

    # ── Main API ─────────────────────────────────────────────────────────

    def evolve(
        self,
        symbiote_id: str,
        component: str,
        default_text: str,
        *,
        days: int = 7,
    ) -> EvolutionResult:
        """Attempt to evolve a single component for a symbiote.

        Args:
            symbiote_id: Target symbiote.
            component: One of EVOLVABLE_COMPONENTS.
            default_text: The hardcoded default text (used when no version exists).
            days: Look-back window for session data.

        Returns:
            EvolutionResult with success status and details.
        """
        if component not in EVOLVABLE_COMPONENTS:
            return EvolutionResult(
                symbiote_id=symbiote_id, component=component,
                success=False, reason=f"Component '{component}' is not evolvable",
            )

        if self._proposer_llm is None:
            return EvolutionResult(
                symbiote_id=symbiote_id, component=component,
                success=False, reason="No proposer LLM configured",
            )

        # Get current text (version or default)
        current_text = self._versions.get_active(symbiote_id, component) or default_text

        # Collect session data
        failed = self._get_failed_sessions(symbiote_id, days)
        successful = self._get_successful_sessions(symbiote_id, days)

        if len(failed) < MIN_FAILED_SESSIONS:
            return EvolutionResult(
                symbiote_id=symbiote_id, component=component,
                success=False,
                reason=f"Not enough failed sessions ({len(failed)}/{MIN_FAILED_SESSIONS})",
            )

        if len(successful) < MIN_SUCCESSFUL_SESSIONS:
            return EvolutionResult(
                symbiote_id=symbiote_id, component=component,
                success=False,
                reason=f"Not enough successful sessions ({len(successful)}/{MIN_SUCCESSFUL_SESSIONS})",
            )

        # Build prompt and call proposer
        prompt = _PROPOSER_PROMPT.format(
            current_text=current_text,
            failed_summary=self._summarize_sessions(failed),
            success_summary=self._summarize_sessions(successful),
        )

        try:
            proposal = self._proposer_llm.complete(
                [{"role": "user", "content": prompt}]
            )
            if not isinstance(proposal, str):
                proposal = getattr(proposal, "content", str(proposal))
        except Exception as exc:
            return EvolutionResult(
                symbiote_id=symbiote_id, component=component,
                success=False, reason=f"Proposer LLM failed: {exc}",
            )

        # Strip markdown wrappers if present
        proposal = self._strip_markdown(proposal)

        # Validate with guard rails
        guard_fail = self._check_guard_rails(current_text, proposal)
        if guard_fail:
            logger.warning(
                "[evolver] guard rail failed for %s/%s: %s",
                symbiote_id[:8], component, guard_fail,
            )
            return EvolutionResult(
                symbiote_id=symbiote_id, component=component,
                success=False, reason="Guard rail failed",
                guard_rail_failed=guard_fail,
            )

        # Get current version number for parent tracking
        current_version_row = self._versions.get_active_version(symbiote_id, component)
        parent_version = current_version_row["version"] if current_version_row else None

        # Persist new version
        new_version = self._versions.create_version(
            symbiote_id=symbiote_id,
            component=component,
            content=proposal,
            parent_version=parent_version,
        )

        logger.info(
            "[evolver] evolved %s/%s to version %d (from %d failed + %d successful sessions)",
            symbiote_id[:8], component, new_version, len(failed), len(successful),
        )

        return EvolutionResult(
            symbiote_id=symbiote_id, component=component,
            success=True, new_version=new_version,
            reason=f"Evolved from {len(failed)} failed + {len(successful)} successful sessions",
        )

    def check_rollback(
        self,
        symbiote_id: str,
        component: str,
    ) -> RollbackCheck:
        """Check if the active version should be rolled back.

        Compares avg_score of the active version against its parent.
        Only triggers if enough sessions have been observed.
        """
        current = self._versions.get_active_version(symbiote_id, component)
        if current is None:
            return RollbackCheck(should_rollback=False, reason="No active version")

        if current["session_count"] < MIN_SESSIONS_FOR_VERDICT:
            return RollbackCheck(
                should_rollback=False,
                reason=f"Not enough sessions ({current['session_count']}/{MIN_SESSIONS_FOR_VERDICT})",
                new_avg=current["avg_score"],
                session_count=current["session_count"],
            )

        parent_version = current.get("parent_version")
        if parent_version is None:
            return RollbackCheck(
                should_rollback=False,
                reason="No parent version to compare against",
                new_avg=current["avg_score"],
                session_count=current["session_count"],
            )

        # Find parent's avg_score
        parent_row = self._storage.fetch_one(
            "SELECT avg_score FROM harness_versions "
            "WHERE symbiote_id = ? AND component = ? AND version = ?",
            (symbiote_id, component, parent_version),
        )
        if parent_row is None:
            return RollbackCheck(
                should_rollback=False, reason="Parent version not found",
            )

        old_avg = parent_row["avg_score"]
        new_avg = current["avg_score"]

        if new_avg < old_avg - ROLLBACK_THRESHOLD:
            return RollbackCheck(
                should_rollback=True,
                reason=f"Score dropped: {new_avg:.3f} < {old_avg:.3f} - {ROLLBACK_THRESHOLD}",
                old_avg=old_avg, new_avg=new_avg,
                session_count=current["session_count"],
            )

        return RollbackCheck(
            should_rollback=False,
            reason=f"Score acceptable: {new_avg:.3f} vs {old_avg:.3f}",
            old_avg=old_avg, new_avg=new_avg,
            session_count=current["session_count"],
        )

    def auto_rollback_if_needed(
        self, symbiote_id: str, component: str
    ) -> bool:
        """Check and perform rollback if needed. Returns True if rolled back."""
        check = self.check_rollback(symbiote_id, component)
        if check.should_rollback:
            ok = self._versions.rollback(symbiote_id, component)
            if ok:
                logger.info(
                    "[evolver] rolled back %s/%s: %s",
                    symbiote_id[:8], component, check.reason,
                )
            return ok
        return False

    # ── Guard rails ──────────────────────────────────────────────────────

    def _check_guard_rails(self, current: str, proposal: str) -> str | None:
        """Validate a proposal against guard rails. Returns failure reason or None."""
        # 1. Max length
        max_len = int(len(current) * MAX_LENGTH_MULTIPLIER)
        if len(proposal) > max_len:
            return f"Too long: {len(proposal)} chars > {max_len} max (2x current)"

        # 2. Empty or too short
        if len(proposal.strip()) < 20:
            return f"Too short: {len(proposal.strip())} chars"

        # 3. CRITICAL lines preserved
        critical_lines = [
            line.strip() for line in current.split("\n")
            if "CRITICAL" in line
        ]
        for line in critical_lines:
            if line not in proposal:
                return f"CRITICAL line missing: {line[:80]}..."

        # 4. Not JSON or code
        stripped = proposal.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return "Proposal appears to be JSON, not instructions"
        if stripped.startswith("```"):
            return "Proposal appears to be a code block"
        if stripped.startswith("def ") or stripped.startswith("class "):
            return "Proposal appears to be Python code"

        return None

    # ── Data collection ──────────────────────────────────────────────────

    def _get_failed_sessions(self, symbiote_id: str, days: int) -> list[dict]:
        return self._storage.fetch_all(
            "SELECT ss.*, et.stop_reason as trace_stop, et.steps_json, "
            "et.total_iterations as trace_iters "
            "FROM session_scores ss "
            "LEFT JOIN execution_traces et ON ss.session_id = et.session_id "
            "WHERE ss.symbiote_id = ? AND ss.final_score < 0.5 "
            "AND ss.computed_at >= datetime('now', ?) "
            "ORDER BY ss.final_score ASC LIMIT 20",
            (symbiote_id, f"-{days} days"),
        )

    def _get_successful_sessions(self, symbiote_id: str, days: int) -> list[dict]:
        return self._storage.fetch_all(
            "SELECT ss.*, et.stop_reason as trace_stop, et.steps_json, "
            "et.total_iterations as trace_iters "
            "FROM session_scores ss "
            "LEFT JOIN execution_traces et ON ss.session_id = et.session_id "
            "WHERE ss.symbiote_id = ? AND ss.final_score >= 0.8 "
            "AND ss.computed_at >= datetime('now', ?) "
            "ORDER BY ss.final_score DESC LIMIT 10",
            (symbiote_id, f"-{days} days"),
        )

    @staticmethod
    def _summarize_sessions(sessions: list[dict]) -> str:
        """Build a compact text summary of sessions for the proposer prompt."""
        if not sessions:
            return "(none)"

        lines = []
        for i, s in enumerate(sessions[:10], 1):
            score = s.get("final_score", "?")
            stop = s.get("trace_stop") or s.get("stop_reason") or "unknown"
            iters = s.get("trace_iters") or s.get("total_iterations") or "?"
            lines.append(f"{i}. score={score}, stop={stop}, iterations={iters}")

            # Include step tool_ids if available
            steps_json = s.get("steps_json")
            if steps_json:
                import json
                try:
                    steps = json.loads(steps_json)
                    tool_ids = [st.get("tool_id", "?") for st in steps[:5]]
                    if tool_ids:
                        lines.append(f"   tools: {', '.join(tool_ids)}")
                except (json.JSONDecodeError, TypeError):
                    pass

        return "\n".join(lines)

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Remove markdown code fences if the proposer wrapped its response."""
        stripped = text.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            # Remove first and last lines (the fences)
            lines = stripped.split("\n")
            if len(lines) >= 3:
                return "\n".join(lines[1:-1]).strip()
        return stripped
