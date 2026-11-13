"""KernelInspector — read-only views over what the kernel has stored.

Hosts that show the kernel's state (an admin page, a demo, a debugging
endpoint) used to read the SQLite tables directly. This class gathers those
reads behind one object, ``kernel.inspector``, so the table layout stays an
implementation detail. Nothing here writes, and every method returns plain
dicts (JSON columns already decoded) ready to serialize.
"""

from __future__ import annotations

import json
from typing import Any

from symbiote.core.ports import StoragePort


def _decode(row: dict, *columns: str) -> dict:
    """Decode the ``*_json`` columns of ``row`` into ``*`` keys (``steps_json`` → ``steps``)."""
    out = dict(row)
    for column in columns:
        raw = out.pop(column, None)
        key = column.removesuffix("_json")
        if raw in (None, ""):
            out[key] = None
            continue
        try:
            out[key] = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            out[key] = raw
    return out


class KernelInspector:
    """Read-only queries over sessions, messages, memory, scores, traces,
    reflections, dream reports, skill reviews and the tool audit log."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    # ── sessions & messages ────────────────────────────────────────────

    def list_sessions(
        self,
        symbiote_id: str | None = None,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Sessions, newest first, with the symbiote's name and the score (if any)."""
        sql = (
            "SELECT s.id, s.symbiote_id, sym.name AS symbiote_name, s.goal, s.status, "
            "s.external_key, s.started_at, s.ended_at, s.summary, "
            "sc.auto_score, sc.user_score, sc.final_score, sc.stop_reason "
            "FROM sessions s JOIN symbiotes sym ON sym.id = s.symbiote_id "
            "LEFT JOIN session_scores sc ON sc.session_id = s.id"
        )
        conditions: list[str] = []
        params: list[Any] = []
        if symbiote_id is not None:
            conditions.append("s.symbiote_id = ?")
            params.append(symbiote_id)
        if status is not None:
            conditions.append("s.status = ?")
            params.append(status)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY s.started_at DESC, s.rowid DESC LIMIT ?"
        params.append(limit)
        return self._storage.fetch_all(sql, tuple(params))

    def list_messages(self, session_id: str, *, limit: int = 200) -> list[dict]:
        """The last ``limit`` messages of a session, in chronological order."""
        rows = self._storage.fetch_all(
            "SELECT id, role, content, created_at FROM messages WHERE session_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (session_id, limit),
        )
        return list(reversed(rows))

    # ── memory ─────────────────────────────────────────────────────────

    def list_memories(
        self,
        symbiote_id: str,
        *,
        include_inactive: bool = False,
        category: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """A symbiote's memories, newest first (active only by default)."""
        sql = (
            "SELECT id, session_id, type, category, scope, content, tags_json, importance, "
            "source, confidence, created_at, last_used_at, updated_at, is_active "
            "FROM memory_entries WHERE symbiote_id = ?"
        )
        params: list[Any] = [symbiote_id]
        if not include_inactive:
            sql += " AND is_active = 1"
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._storage.fetch_all(sql, tuple(params))
        return [_decode(r, "tags_json") for r in rows]

    # ── scores & traces ────────────────────────────────────────────────

    def list_scores(self, symbiote_id: str, *, limit: int = 50) -> list[dict]:
        """Session scores of a symbiote, newest first."""
        return self._storage.fetch_all(
            "SELECT session_id, auto_score, user_score, final_score, stop_reason, "
            "total_iterations, total_tool_calls, computed_at "
            "FROM session_scores WHERE symbiote_id = ? "
            "ORDER BY computed_at DESC, rowid DESC LIMIT ?",
            (symbiote_id, limit),
        )

    def list_traces(
        self,
        *,
        symbiote_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Execution traces (one per turn that ran tools), newest first, with steps."""
        if symbiote_id is None and session_id is None:
            raise ValueError("list_traces needs symbiote_id or session_id")
        column, value = ("session_id", session_id) if session_id else ("symbiote_id", symbiote_id)
        rows = self._storage.fetch_all(
            "SELECT id, session_id, symbiote_id, total_iterations, total_tool_calls, "
            "total_elapsed_ms, stop_reason, tool_mode, steps_json, created_at "
            f"FROM execution_traces WHERE {column} = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (value, limit),
        )
        return [_decode(r, "steps_json") for r in rows]

    # ── learning ───────────────────────────────────────────────────────

    def list_reflections(self, symbiote_id: str, *, limit: int = 50) -> list[dict]:
        """Reflection audit rows (written by the ``llm``/``hybrid`` modes), newest first."""
        rows = self._storage.fetch_all(
            "SELECT id, session_id, mode, keyword_facts_json, llm_facts_json, llm_error, "
            "created_at FROM reflection_audit WHERE symbiote_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (symbiote_id, limit),
        )
        return [_decode(r, "keyword_facts_json", "llm_facts_json") for r in rows]

    def list_dream_reports(self, symbiote_id: str, *, limit: int = 20) -> list[dict]:
        """Dream reports with their phases, newest first."""
        rows = self._storage.fetch_all(
            "SELECT id, started_at, completed_at, dream_mode, dry_run, total_llm_calls, "
            "max_llm_calls, phases_json FROM dream_reports WHERE symbiote_id = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (symbiote_id, limit),
        )
        return [_decode(r, "phases_json") for r in rows]

    def list_skill_reviews(self, symbiote_id: str, *, limit: int = 50) -> list[dict]:
        """Background skill-review runs with the operations they applied, newest first."""
        rows = self._storage.fetch_all(
            "SELECT id, session_id, trigger, applied, skipped, ok, error, ops_json, "
            "created_at FROM skill_review_audit WHERE symbiote_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (symbiote_id, limit),
        )
        return [_decode(r, "ops_json") for r in rows]

    # ── tools ──────────────────────────────────────────────────────────

    def audit_log(
        self,
        symbiote_id: str,
        *,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Tool executions and policy blocks, newest first."""
        sql = (
            "SELECT id, session_id, tool_id, action, params_json, result, created_at "
            "FROM audit_log WHERE symbiote_id = ?"
        )
        params: list[Any] = [symbiote_id]
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._storage.fetch_all(sql, tuple(params))
        return [_decode(r, "params_json") for r in rows]
