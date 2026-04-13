"""Dream Mode phases — each phase is a self-contained unit of background work."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Protocol

from symbiote.core.models import MemoryEntry
from symbiote.dream.models import DreamContext, DreamPhaseResult

logger = logging.getLogger(__name__)


# ── Phase protocol ─────────────────────────────────────────────────────────��


class DreamPhase(Protocol):
    name: str
    requires_llm: bool

    def run(self, ctx: DreamContext) -> DreamPhaseResult: ...


# ── Helpers ─────────────────────────────���───────────────────────────────────

_PROTECTED_TYPES = frozenset({"constraint", "handoff"})


def _llm_complete(llm, prompt: str) -> str:
    """Call LLM and extract plain text from the response."""
    result = llm.complete(messages=[{"role": "user", "content": prompt}])
    if isinstance(result, str):
        return result
    return result.content  # LLMResponse


def _days_since(dt: datetime) -> float:
    """Days elapsed since *dt* (UTC-aware)."""
    now = datetime.now(tz=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (now - dt).total_seconds() / 86400


def _token_set(text: str) -> set[str]:
    """Whitespace-split tokens, lowercased."""
    return set(text.lower().split())


def _overlap_ratio(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Phase 1: Prune ─────────────────────────────────────────────────────────


class PrunePhase:
    """Deactivate stale, low-importance memories based on decay score."""

    name = "prune"
    requires_llm = False

    def __init__(self, decay_threshold: float = 30.0) -> None:
        self._threshold = decay_threshold

    def run(self, ctx: DreamContext) -> DreamPhaseResult:
        start = datetime.now(tz=UTC)
        rows = ctx.storage.fetch_all(
            "SELECT * FROM memory_entries WHERE is_active = 1 AND symbiote_id = ?",
            (ctx.symbiote_id,),
        )

        proposed: list[dict] = []
        applied = 0

        for row in rows:
            entry = ctx.memory._row_to_entry(row)
            if entry.type in _PROTECTED_TYPES:
                continue
            days = _days_since(entry.last_used_at)
            decay = days * (1.0 - entry.importance)
            if decay <= self._threshold:
                continue

            detail = {
                "id": entry.id,
                "type": entry.type,
                "importance": entry.importance,
                "days_since_used": round(days, 1),
                "decay": round(decay, 1),
                "content_preview": entry.content[:80],
            }
            proposed.append(detail)

            if not ctx.dry_run:
                ctx.memory.deactivate(entry.id)
                applied += 1

        return DreamPhaseResult(
            phase=self.name,
            started_at=start,
            completed_at=datetime.now(tz=UTC),
            actions_proposed=len(proposed),
            actions_applied=applied,
            details=proposed,
        )


# ── Phase 2: Reconcile ─────────────────────────────────────────────────────


class ReconcilePhase:
    """Detect and resolve conflicting memories with overlapping tags."""

    name = "reconcile"
    requires_llm = False

    def __init__(
        self,
        tag_overlap_threshold: float = 0.6,
        content_similarity_threshold: float = 0.3,
    ) -> None:
        self._tag_thresh = tag_overlap_threshold
        self._content_thresh = content_similarity_threshold

    def run(self, ctx: DreamContext) -> DreamPhaseResult:
        start = datetime.now(tz=UTC)
        rows = ctx.storage.fetch_all(
            "SELECT * FROM memory_entries WHERE is_active = 1 AND symbiote_id = ?",
            (ctx.symbiote_id,),
        )

        entries = [ctx.memory._row_to_entry(r) for r in rows]
        # Only consider entries with 2+ tags for clustering
        tagged = [e for e in entries if len(e.tags) >= 2]

        conflicts: list[dict] = []
        deactivated: set[str] = set()
        applied = 0

        for i, a in enumerate(tagged):
            if a.id in deactivated:
                continue
            for b in tagged[i + 1 :]:
                if b.id in deactivated:
                    continue
                tag_ov = _overlap_ratio(set(a.tags), set(b.tags))
                if tag_ov < self._tag_thresh:
                    continue
                content_sim = _overlap_ratio(_token_set(a.content), _token_set(b.content))
                if content_sim >= self._content_thresh:
                    continue  # similar content = not a conflict

                # Conflict found — keep the higher-importance entry
                loser, winner = (a, b) if a.importance < b.importance else (b, a)
                detail = {
                    "winner_id": winner.id,
                    "loser_id": loser.id,
                    "tag_overlap": round(tag_ov, 2),
                    "content_similarity": round(content_sim, 2),
                    "winner_importance": winner.importance,
                    "loser_importance": loser.importance,
                }
                conflicts.append(detail)

                if not ctx.dry_run:
                    ctx.memory.deactivate(loser.id)
                    deactivated.add(loser.id)
                    # Tag the winner
                    new_tags = list(winner.tags) + ["dream:reconciled"]
                    ctx.storage.execute(
                        "UPDATE memory_entries SET tags_json = ? WHERE id = ?",
                        (json.dumps(new_tags), winner.id),
                    )
                    applied += 1
                else:
                    deactivated.add(loser.id)

        return DreamPhaseResult(
            phase=self.name,
            started_at=start,
            completed_at=datetime.now(tz=UTC),
            actions_proposed=len(conflicts),
            actions_applied=applied,
            details=conflicts,
        )


# ── Phase 3: Generalize ────────────────────────────────────────────────────


_GENERALIZE_PROMPT = """\
You are analyzing a cluster of procedural memories from an AI agent. \
These memories describe similar step-by-step procedures.

Synthesize them into ONE higher-level procedural memory that captures \
the general pattern. Be concise — one or two sentences.

Memories:
{memories}

Generalized procedure (one concise sentence):"""


class GeneralizePhase:
    """Cluster similar procedural memories and create higher-level abstractions."""

    name = "generalize"
    requires_llm = True

    def run(self, ctx: DreamContext) -> DreamPhaseResult:
        start = datetime.now(tz=UTC)
        if ctx.llm is None:
            return DreamPhaseResult(
                phase=self.name, started_at=start, completed_at=datetime.now(tz=UTC),
                error="no_llm",
            )

        entries = ctx.memory.get_by_type(ctx.symbiote_id, "procedural", limit=100)
        tagged = [e for e in entries if len(e.tags) >= 1]

        # Cluster by tag overlap (2+ common tags)
        clusters: list[list[MemoryEntry]] = []
        used: set[str] = set()
        for i, a in enumerate(tagged):
            if a.id in used:
                continue
            cluster = [a]
            used.add(a.id)
            for b in tagged[i + 1 :]:
                if b.id in used:
                    continue
                if len(set(a.tags) & set(b.tags)) >= 2:
                    cluster.append(b)
                    used.add(b.id)
            if len(cluster) >= 3:
                clusters.append(cluster)

        details: list[dict] = []
        applied = 0

        for cluster in clusters:
            if not ctx.budget.consume(1):
                break

            memories_text = "\n".join(f"- {e.content}" for e in cluster)
            prompt = _GENERALIZE_PROMPT.format(memories=memories_text)

            try:
                generalized = _llm_complete(ctx.llm, prompt).strip()
            except Exception as exc:
                logger.warning("dream generalize LLM error: %s", exc)
                details.append({"cluster_size": len(cluster), "error": str(exc)})
                continue

            max_imp = max(e.importance for e in cluster)
            new_importance = min(max_imp + 0.05, 1.0)

            detail = {
                "cluster_size": len(cluster),
                "generalized": generalized,
                "importance": new_importance,
                "source_ids": [e.id for e in cluster],
            }
            details.append(detail)

            if not ctx.dry_run:
                # Merge tags from all cluster members
                all_tags = set()
                for e in cluster:
                    all_tags.update(e.tags)
                all_tags.add("dream:generalized")

                new_entry = MemoryEntry(
                    symbiote_id=ctx.symbiote_id,
                    type="procedural",
                    scope="global",
                    content=generalized,
                    tags=sorted(all_tags),
                    importance=new_importance,
                    source="inference",
                    confidence=0.7,
                )
                ctx.memory.store(new_entry)
                applied += 1

        return DreamPhaseResult(
            phase=self.name,
            started_at=start,
            completed_at=datetime.now(tz=UTC),
            actions_proposed=len(details),
            actions_applied=applied,
            llm_calls_used=ctx.budget.used,  # total used so far
            details=details,
        )


# ── Phase 4: Mine ──────────────────────────────────────────────────────────

_MINE_PROMPT = """\
You are analyzing execution traces from an AI agent. \
These traces show tool calls that failed or caused the agent to stagnate.

Identify the top failure patterns and suggest concrete procedural rules \
to avoid them in the future.

Failure data:
{failures}

Return a JSON array of objects with:
- "content": the procedural rule (one sentence)
- "importance": float 0.0–1.0

JSON array:"""


class MinePhase:
    """Pattern-mine execution traces for recurring failures."""

    name = "mine"
    requires_llm = True

    def run(self, ctx: DreamContext) -> DreamPhaseResult:
        start = datetime.now(tz=UTC)
        if ctx.llm is None:
            return DreamPhaseResult(
                phase=self.name, started_at=start, completed_at=datetime.now(tz=UTC),
                error="no_llm",
            )

        since = ctx.last_dream_at.isoformat() if ctx.last_dream_at else "1970-01-01"
        rows = ctx.storage.fetch_all(
            "SELECT * FROM execution_traces "
            "WHERE symbiote_id = ? AND created_at > ? AND stop_reason != 'end_turn' "
            "ORDER BY created_at DESC LIMIT 50",
            (ctx.symbiote_id, since),
        )

        if not rows:
            return DreamPhaseResult(
                phase=self.name, started_at=start, completed_at=datetime.now(tz=UTC),
            )

        # Aggregate failure info
        failure_summary: list[str] = []
        for row in rows:
            steps = json.loads(row.get("steps_json") or "[]")
            failed = [s for s in steps if not s.get("success", False)]
            if failed:
                tools = ", ".join(s.get("tool_id", "?") for s in failed)
                errors = "; ".join(s.get("error", "?") for s in failed if s.get("error"))
                failure_summary.append(
                    f"stop={row.get('stop_reason')}, failed_tools=[{tools}], errors=[{errors}]"
                )

        if not failure_summary:
            return DreamPhaseResult(
                phase=self.name, started_at=start, completed_at=datetime.now(tz=UTC),
            )

        if not ctx.budget.consume(1):
            return DreamPhaseResult(
                phase=self.name, started_at=start, completed_at=datetime.now(tz=UTC),
                error="budget_exhausted",
            )

        prompt = _MINE_PROMPT.format(failures="\n".join(failure_summary[:20]))
        details: list[dict] = []
        applied = 0

        try:
            text = _llm_complete(ctx.llm, prompt).strip()
            # Parse JSON — strip markdown fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            facts = json.loads(text)
        except Exception as exc:
            logger.warning("dream mine LLM error: %s", exc)
            return DreamPhaseResult(
                phase=self.name, started_at=start, completed_at=datetime.now(tz=UTC),
                llm_calls_used=1, error=str(exc),
            )

        for fact in facts[:5]:
            content = fact.get("content", "")
            importance = min(float(fact.get("importance", 0.6)), 1.0)
            details.append({"content": content, "importance": importance})

            if not ctx.dry_run and content:
                entry = MemoryEntry(
                    symbiote_id=ctx.symbiote_id,
                    type="procedural",
                    scope="global",
                    content=content,
                    tags=["dream:failure_pattern"],
                    importance=importance,
                    source="inference",
                    confidence=0.6,
                )
                ctx.memory.store(entry)
                applied += 1

        return DreamPhaseResult(
            phase=self.name,
            started_at=start,
            completed_at=datetime.now(tz=UTC),
            actions_proposed=len(details),
            actions_applied=applied,
            llm_calls_used=1,
            details=details,
        )


# ── Phase 5: Evaluate ───────────────────────────────��──────────────────────

_EVALUATE_PROMPT = """\
You are reviewing a past conversation from an AI agent that received \
a low quality score ({score:.2f}/1.0). Identify what went wrong and \
suggest a concrete procedural rule to improve future performance.

Conversation:
{conversation}

Write ONE concise insight (one sentence):"""


class EvaluatePhase:
    """Self-review low-scoring sessions for improvement insights."""

    name = "evaluate"
    requires_llm = True

    def run(self, ctx: DreamContext) -> DreamPhaseResult:
        start = datetime.now(tz=UTC)
        if ctx.llm is None:
            return DreamPhaseResult(
                phase=self.name, started_at=start, completed_at=datetime.now(tz=UTC),
                error="no_llm",
            )

        since = ctx.last_dream_at.isoformat() if ctx.last_dream_at else "1970-01-01"
        scores = ctx.storage.fetch_all(
            "SELECT * FROM session_scores "
            "WHERE symbiote_id = ? AND final_score < 0.5 AND computed_at > ? "
            "ORDER BY final_score ASC LIMIT 10",
            (ctx.symbiote_id, since),
        )

        if not scores:
            return DreamPhaseResult(
                phase=self.name, started_at=start, completed_at=datetime.now(tz=UTC),
            )

        details: list[dict] = []
        applied = 0
        llm_calls = 0

        for score_row in scores:
            if not ctx.budget.consume(1):
                break

            llm_calls += 1
            session_id = score_row["session_id"]
            final_score = float(score_row.get("final_score", 0))

            messages = ctx.storage.fetch_all(
                "SELECT role, content FROM messages "
                "WHERE session_id = ? ORDER BY created_at LIMIT 20",
                (session_id,),
            )
            if not messages:
                continue

            convo = "\n".join(f"[{m['role']}] {m['content'][:200]}" for m in messages)
            prompt = _EVALUATE_PROMPT.format(score=final_score, conversation=convo)

            try:
                insight = _llm_complete(ctx.llm, prompt).strip()
            except Exception as exc:
                logger.warning("dream evaluate LLM error: %s", exc)
                details.append({"session_id": session_id, "error": str(exc)})
                continue

            detail = {
                "session_id": session_id,
                "final_score": final_score,
                "insight": insight,
            }
            details.append(detail)

            if not ctx.dry_run and insight:
                entry = MemoryEntry(
                    symbiote_id=ctx.symbiote_id,
                    type="reflection",
                    scope="global",
                    content=insight,
                    tags=["dream:self_review"],
                    importance=0.7,
                    source="inference",
                    confidence=0.6,
                )
                ctx.memory.store(entry)
                applied += 1

        return DreamPhaseResult(
            phase=self.name,
            started_at=start,
            completed_at=datetime.now(tz=UTC),
            actions_proposed=len(details),
            actions_applied=applied,
            llm_calls_used=llm_calls,
            details=details,
        )
