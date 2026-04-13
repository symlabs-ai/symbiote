"""DreamEngine — orchestrates background memory rumination phases."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from symbiote.dream.models import BudgetTracker, DreamContext, DreamReport
from symbiote.dream.phases import (
    EvaluatePhase,
    GeneralizePhase,
    MinePhase,
    PrunePhase,
    ReconcilePhase,
)

if TYPE_CHECKING:
    from symbiote.core.ports import LLMPort, StoragePort
    from symbiote.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_LIGHT_PHASES = [PrunePhase, ReconcilePhase]
_FULL_PHASES = [PrunePhase, ReconcilePhase, GeneralizePhase, MinePhase, EvaluatePhase]


class DreamEngine:
    """Background engine that ruminates over historical memory data."""

    def __init__(
        self,
        storage: StoragePort,
        memory: MemoryStore,
        llm: LLMPort | None = None,
        *,
        max_llm_calls: int = 10,
        min_sessions: int = 5,
        dry_run: bool = False,
    ) -> None:
        self._storage = storage
        self._memory = memory
        self._llm = llm
        self._max_llm_calls = max_llm_calls
        self._min_sessions = min_sessions
        self._dry_run = dry_run
        self._active_threads: dict[str, threading.Thread] = {}

    # ── public API ─────────────────────────────────────────────────────

    def should_dream(self, symbiote_id: str, dream_mode: str) -> bool:
        """Check whether a dream cycle should run for this symbiote."""
        if dream_mode == "off":
            return False

        # Already running?
        thread = self._active_threads.get(symbiote_id)
        if thread is not None and thread.is_alive():
            return False

        # Enough new sessions since last dream?
        last_dream_at = self._last_dream_at(symbiote_id)
        since = last_dream_at.isoformat() if last_dream_at else "1970-01-01"
        row = self._storage.fetch_one(
            "SELECT COUNT(*) AS cnt FROM sessions "
            "WHERE symbiote_id = ? AND status = 'closed' AND ended_at > ?",
            (symbiote_id, since),
        )
        count = int(row["cnt"]) if row else 0
        return count >= self._min_sessions

    def dream(self, symbiote_id: str, dream_mode: str) -> DreamReport:
        """Run a dream cycle synchronously. Returns the report."""
        report = DreamReport(
            symbiote_id=symbiote_id,
            dream_mode=dream_mode,
            dry_run=self._dry_run,
            max_llm_calls=self._max_llm_calls,
        )

        budget = BudgetTracker(self._max_llm_calls)
        last_dream_at = self._last_dream_at(symbiote_id)

        ctx = DreamContext(
            symbiote_id=symbiote_id,
            storage=self._storage,
            memory=self._memory,
            llm=self._llm,
            budget=budget,
            dry_run=self._dry_run,
            last_dream_at=last_dream_at,
        )

        phase_classes = _FULL_PHASES if dream_mode == "full" else _LIGHT_PHASES

        for cls in phase_classes:
            phase = cls()
            if phase.requires_llm and self._llm is None:
                continue
            if phase.requires_llm and budget.remaining == 0:
                continue

            try:
                result = phase.run(ctx)
                report.phases.append(result)
            except Exception as exc:
                logger.exception("dream phase %s failed", phase.name)
                from symbiote.dream.models import DreamPhaseResult
                report.phases.append(DreamPhaseResult(
                    phase=phase.name,
                    error=str(exc),
                ))

        report.completed_at = datetime.now(tz=UTC)
        report.total_llm_calls = budget.used

        self._persist_report(report)
        return report

    def dream_async(self, symbiote_id: str, dream_mode: str) -> None:
        """Spawn a background daemon thread for the dream cycle."""
        thread = threading.Thread(
            target=self._background_dream,
            args=(symbiote_id, dream_mode),
            daemon=True,
            name=f"dream-{symbiote_id[:8]}",
        )
        thread.start()
        self._active_threads[symbiote_id] = thread

    # ── private ────────────────────────────────────────────────────────

    def _background_dream(self, symbiote_id: str, dream_mode: str) -> None:
        try:
            self.dream(symbiote_id, dream_mode)
        except Exception:
            logger.exception("dream background cycle failed for %s", symbiote_id)
        finally:
            self._active_threads.pop(symbiote_id, None)

    def _last_dream_at(self, symbiote_id: str) -> datetime | None:
        row = self._storage.fetch_one(
            "SELECT completed_at FROM dream_reports "
            "WHERE symbiote_id = ? ORDER BY started_at DESC LIMIT 1",
            (symbiote_id,),
        )
        if row is None or row.get("completed_at") is None:
            return None
        val = row["completed_at"]
        if isinstance(val, str):
            return datetime.fromisoformat(val)
        return val

    def _persist_report(self, report: DreamReport) -> None:
        phases_json = json.dumps([p.model_dump(mode="json") for p in report.phases])
        self._storage.execute(
            "INSERT INTO dream_reports "
            "(id, symbiote_id, started_at, completed_at, dream_mode, "
            "dry_run, total_llm_calls, max_llm_calls, phases_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.id,
                report.symbiote_id,
                report.started_at.isoformat(),
                report.completed_at.isoformat() if report.completed_at else None,
                report.dream_mode,
                int(report.dry_run),
                report.total_llm_calls,
                report.max_llm_calls,
                phases_json,
            ),
        )
