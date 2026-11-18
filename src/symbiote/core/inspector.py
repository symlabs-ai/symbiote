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

from symbiote.core.approvals import PENDING, PendingApprovalStore
from symbiote.core.ports import StoragePort
from symbiote.core.session_scope import DELEGATION_OPEN, tools_from_json
from symbiote.core.subjects import ANY_SUBJECT, AnySubject


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


def _where(sql: str, *, since: str | None = None, **equals: str | None) -> tuple[str, list[Any]]:
    """Append ``WHERE`` with ``column = ?`` for each non-None filter in
    ``equals`` (column names come from this module, never from callers) and
    ``created_at >= ?`` for ``since``."""
    conditions = [f"{column} = ?" for column, value in equals.items() if value is not None]
    params: list[Any] = [value for value in equals.values() if value is not None]
    if since is not None:
        conditions.append("created_at >= ?")
        params.append(since)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    return sql, params


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
        subject_key: str | None | AnySubject = ANY_SUBJECT,
        revision: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Sessions, newest first, with the symbiote's name and the score (if any).

        ``subject_key`` keeps the sessions of one end user; ``None`` keeps
        the ones without an end user. By default, all of them. ``revision``
        keeps the sessions pinned to that revision; each row says which one
        it is pinned to (``None``: the current configuration), the tools it
        is narrowed to (``tools_allowed``; ``None``: no restriction) and its
        ``delegation`` mode.
        """
        sql = (
            "SELECT s.id, s.symbiote_id, sym.name AS symbiote_name, s.goal, s.status, "
            "s.external_key, s.subject_key, s.revision, s.tools_allowed, s.delegation, "
            "s.started_at, s.ended_at, s.summary, "
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
        if subject_key is None:
            conditions.append("s.subject_key IS NULL")
        elif not isinstance(subject_key, AnySubject):
            conditions.append("s.subject_key = ?")
            params.append(subject_key)
        if revision is not None:
            conditions.append("s.revision = ?")
            params.append(revision)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY s.started_at DESC, s.rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._storage.fetch_all(sql, tuple(params))
        for row in rows:
            row["tools_allowed"] = tools_from_json(row.get("tools_allowed"))
            row["delegation"] = row.get("delegation") or DELEGATION_OPEN
        return rows

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
        subject_key: str | None | AnySubject = ANY_SUBJECT,
        limit: int = 200,
    ) -> list[dict]:
        """A symbiote's memories, newest first (active only by default).

        Each row carries its ``subject_key`` (``None``: shared by every
        session). ``subject_key="..."`` keeps the memories of that end user
        alone; ``subject_key=None`` keeps the shared ones. By default, every
        partition.
        """
        sql = (
            "SELECT id, session_id, subject_key, type, category, scope, content, "
            "tags_json, importance, "
            "source, confidence, created_at, last_used_at, updated_at, is_active "
            "FROM memory_entries WHERE symbiote_id = ?"
        )
        params: list[Any] = [symbiote_id]
        if subject_key is None:
            sql += " AND subject_key IS NULL"
        elif not isinstance(subject_key, AnySubject):
            sql += " AND subject_key = ?"
            params.append(subject_key)
        if not include_inactive:
            sql += " AND is_active = 1"
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._storage.fetch_all(sql, tuple(params))
        return [_decode(r, "tags_json") for r in rows]

    def list_subjects(self, symbiote_id: str, *, limit: int = 200) -> list[dict]:
        """The end users of a symbiote, most recently seen first.

        One row per ``subject_key`` that has a session or a memory:
        ``sessions`` (how many), ``last_session_at`` (start of the newest,
        None when only memories are left) and ``memories`` (active ones).
        """
        sessions = self._storage.fetch_all(
            "SELECT subject_key, COUNT(*) AS sessions, MAX(started_at) AS last_session_at "
            "FROM sessions WHERE symbiote_id = ? AND subject_key IS NOT NULL "
            "GROUP BY subject_key",
            (symbiote_id,),
        )
        memories = self._storage.fetch_all(
            "SELECT subject_key, COUNT(*) AS memories FROM memory_entries "
            "WHERE symbiote_id = ? AND subject_key IS NOT NULL AND is_active = 1 "
            "GROUP BY subject_key",
            (symbiote_id,),
        )
        subjects: dict[str, dict] = {}
        for row in sessions:
            subjects[row["subject_key"]] = {
                "subject_key": row["subject_key"],
                "sessions": int(row["sessions"]),
                "last_session_at": row["last_session_at"],
                "memories": 0,
            }
        for row in memories:
            entry = subjects.setdefault(
                row["subject_key"],
                {
                    "subject_key": row["subject_key"],
                    "sessions": 0,
                    "last_session_at": None,
                    "memories": 0,
                },
            )
            entry["memories"] = int(row["memories"])
        ordered = sorted(
            subjects.values(),
            key=lambda s: (s["last_session_at"] or "", s["subject_key"]),
            reverse=True,
        )
        return ordered[:limit]

    # ── scores & traces ────────────────────────────────────────────────

    def list_scores(
        self,
        symbiote_id: str | None = None,
        *,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Session scores of a symbiote and/or a session, newest first."""
        if symbiote_id is None and session_id is None:
            raise ValueError("list_scores needs symbiote_id or session_id")
        sql = (
            "SELECT id, session_id, symbiote_id, auto_score, user_score, final_score, "
            "stop_reason, total_iterations, total_tool_calls, computed_at "
            "FROM session_scores"
        )
        sql, params = _where(sql, symbiote_id=symbiote_id, session_id=session_id)
        sql += " ORDER BY computed_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        return self._storage.fetch_all(sql, tuple(params))

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

    def list_turn_contexts(
        self,
        *,
        symbiote_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """What entered the context of each turn, newest first.

        One row per turn (with or without tools). ``memories`` and
        ``knowledge`` are lists of ``{"id", "source"}``, ``source`` being
        ``"packed"`` (in the system prompt) or ``"on_demand"`` (returned by
        ``search_memories`` / ``search_knowledge`` during the turn);
        ``skills`` lists the names in ``<available-skills>``. It records what
        the model was given, not what it used. ``llm_calls`` and the token
        columns are what the model calls of the turn cost, the ones of a
        symbiote it delegated to included.
        """
        if symbiote_id is None and session_id is None:
            raise ValueError("list_turn_contexts needs symbiote_id or session_id")
        column, value = ("session_id", session_id) if session_id else ("symbiote_id", symbiote_id)
        rows = self._storage.fetch_all(
            "SELECT id, session_id, symbiote_id, memories_json, knowledge_json, "
            "skills_json, tokens_estimate, created_at, "
            "llm_calls, prompt_tokens, completion_tokens, total_tokens, "
            "cache_read_tokens, cache_write_tokens, revision "
            f"FROM turn_contexts WHERE {column} = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (value, limit),
        )
        return [
            _decode(r, "memories_json", "knowledge_json", "skills_json") for r in rows
        ]

    # ── learning ───────────────────────────────────────────────────────

    def list_reflections(
        self,
        symbiote_id: str | None = None,
        *,
        session_id: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Reflection audit rows (written by the ``llm``/``hybrid`` modes), newest first.

        Filters combine with AND; with none, rows of every symbiote are listed.
        ``since`` is an ISO timestamp (e.g. ``symbiote.core.sql.days_ago(7)``).
        """
        sql, params = _where(
            "SELECT id, session_id, mode, keyword_facts_json, llm_facts_json, llm_error, "
            "created_at FROM reflection_audit",
            symbiote_id=symbiote_id, session_id=session_id, since=since,
        )
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._storage.fetch_all(sql, tuple(params))
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

    def list_skill_reviews(
        self,
        symbiote_id: str | None = None,
        *,
        session_id: str | None = None,
        trigger: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Background skill-review runs with the operations they applied, newest first.

        Filters combine with AND; with none, runs of every symbiote are listed.
        ``since`` is an ISO timestamp (e.g. ``symbiote.core.sql.days_ago(7)``).
        """
        sql, params = _where(
            "SELECT id, session_id, trigger, applied, skipped, ok, error, ops_json, "
            "created_at FROM skill_review_audit",
            symbiote_id=symbiote_id, session_id=session_id, trigger=trigger, since=since,
        )
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._storage.fetch_all(sql, tuple(params))
        return [_decode(r, "ops_json") for r in rows]

    # ── revisions of the configuration ─────────────────────────────────

    def list_revisions(self, symbiote_id: str) -> list[dict]:
        """The revisions of a symbiote, newest first, each with how many
        sessions are pinned to it (``sessions``) and how many of those are
        still open (``active_sessions``). ``is_default`` marks the
        symbiote's default revision. The picture itself (persona, config,
        knowledge) is ``kernel.get_revision(symbiote_id, label)``."""
        rows = self._storage.fetch_all(
            "SELECT r.id, r.label, r.note, r.created_at, r.retired_at, r.is_default, "
            "(SELECT COUNT(*) FROM revision_knowledge k WHERE k.revision_id = r.id) "
            "AS knowledge_count, "
            "(SELECT COUNT(*) FROM sessions s WHERE s.symbiote_id = r.symbiote_id "
            "AND s.revision = r.label) AS sessions, "
            "(SELECT COUNT(*) FROM sessions s WHERE s.symbiote_id = r.symbiote_id "
            "AND s.revision = r.label AND s.status = 'active') AS active_sessions "
            "FROM symbiote_revisions r WHERE r.symbiote_id = ? "
            "ORDER BY r.created_at DESC, r.rowid DESC",
            (symbiote_id,),
        )
        for row in rows:
            row["is_default"] = bool(row.get("is_default") or 0)
        return rows

    # ── approvals that wait for a person ───────────────────────────────

    def list_pending(
        self,
        symbiote_id: str | None = None,
        *,
        status: str | None = PENDING,
        session_id: str | None = None,
        subject_key: str | None | AnySubject = ANY_SUBJECT,
        limit: int = 100,
    ) -> list[dict]:
        """Tool calls deferred by the approval callback, newest first.

        By default the ones still waiting; ``status`` picks another state
        (``approved``, ``denied``, ``expired``, ``executed``, ``failed``) and
        ``status=None`` every state. A request past its time is listed as
        ``expired``. ``params`` are the call's parameters with the known
        secrets removed; ``result`` / ``error`` are the outcome of an approved
        call. Decide one with ``kernel.resolve_pending(id, approved)``.
        """
        found = PendingApprovalStore(self._storage).find(
            symbiote_id,
            status=status,
            session_id=session_id,
            subject_key=subject_key,
            limit=limit,
        )
        return [approval.model_dump() for approval in found]

    def get_pending(self, approval_id: str) -> dict | None:
        """One deferred tool call by id (``None`` when unknown)."""
        found = PendingApprovalStore(self._storage).get(approval_id)
        return found.model_dump() if found is not None else None

    # ── model usage ────────────────────────────────────────────────────

    # What ``usage_by`` can group on. ``subject`` is the end user of the
    # conversation the call was made for; ``root_session`` is that
    # conversation (the session of the turn the host started, which differs
    # from ``session`` for the calls of a delegated symbiote).
    USAGE_GROUPS: dict[str, str] = {
        "purpose": "u.purpose",
        "symbiote": "u.symbiote_id",
        "session": "u.session_id",
        "root_session": "u.root_session_id",
        "provider": "u.provider",
        "model": "u.model",
        "day": "SUBSTR(u.created_at, 1, 10)",
        "subject": "s.subject_key",
        # The revision the call ran on; for a call made outside a turn
        # (reflection, summary), the one its conversation is pinned to.
        "revision": "COALESCE(u.revision, s.revision)",
    }
    _USAGE_SUMS = (
        "COUNT(*) AS llm_calls, "
        "COALESCE(SUM(CASE WHEN u.success = 0 THEN 1 ELSE 0 END), 0) AS failed_calls, "
        "COALESCE(SUM(u.prompt_tokens), 0) AS prompt_tokens, "
        "COALESCE(SUM(u.completion_tokens), 0) AS completion_tokens, "
        "COALESCE(SUM(u.total_tokens), 0) AS total_tokens, "
        "COALESCE(SUM(u.cache_read_tokens), 0) AS cache_read_tokens, "
        "COALESCE(SUM(u.cache_write_tokens), 0) AS cache_write_tokens, "
        "COALESCE(SUM(u.cache_write_5m_tokens), 0) AS cache_write_5m_tokens, "
        "COALESCE(SUM(u.cache_write_1h_tokens), 0) AS cache_write_1h_tokens, "
        "COALESCE(SUM(u.reasoning_tokens), 0) AS reasoning_tokens"
    )
    _USAGE_FROM = (
        " FROM llm_usage u LEFT JOIN sessions s "
        "ON s.id = COALESCE(u.root_session_id, u.session_id)"
    )

    @staticmethod
    def _usage_filter(
        symbiote_id: str | None,
        session_id: str | None,
        root_session_id: str | None,
        subject_key: str | None | AnySubject,
        purpose: str | None,
        since: str | None,
        until: str | None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("u.symbiote_id", symbiote_id),
            ("u.session_id", session_id),
            ("u.root_session_id", root_session_id),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(value)
        if subject_key is None:
            conditions.append("s.subject_key IS NULL")
        elif not isinstance(subject_key, AnySubject):
            conditions.append("s.subject_key = ?")
            params.append(subject_key)
        if purpose is not None:
            # ``dream`` also selects ``dream:prune``, ``dream:generalize``...
            conditions.append("(u.purpose = ? OR u.purpose LIKE ?)")
            params.extend([purpose, f"{purpose}:%"])
        if since is not None:
            conditions.append("u.created_at >= ?")
            params.append(since)
        if until is not None:
            conditions.append("u.created_at < ?")
            params.append(until)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return where, params

    def usage_totals(
        self,
        symbiote_id: str | None = None,
        *,
        session_id: str | None = None,
        root_session_id: str | None = None,
        subject_key: str | None | AnySubject = ANY_SUBJECT,
        purpose: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict:
        """Model calls and tokens that match the filters, summed.

        ``llm_calls`` counts every call and ``failed_calls`` the ones that
        raised; the token sums cover the calls whose adapter reported them.
        ``prompt_tokens`` is the whole input, cache included:
        ``cache_read_tokens`` and ``cache_write_tokens`` are parts of it, and
        ``cache_write_5m_tokens`` / ``cache_write_1h_tokens`` split the
        written part by how long the provider keeps it. ``reasoning_tokens``
        is already inside ``completion_tokens``. The kernel stores no price:
        the uncached input is ``prompt - cache_read - cache_write``.
        ``purpose`` selects one purpose or a family (``"dream"`` covers every
        ``dream:<phase>``). ``since`` / ``until`` are ISO-8601 instants,
        ``until`` excluded. ``subject_key`` keeps the calls made for one end
        user (``None``: for no end user).
        """
        where, params = self._usage_filter(
            symbiote_id, session_id, root_session_id, subject_key, purpose, since, until
        )
        row = self._storage.fetch_one(
            f"SELECT {self._USAGE_SUMS}{self._USAGE_FROM}{where}",  # noqa: S608
            tuple(params),
        )
        return {key: int(value or 0) for key, value in dict(row or {}).items()}

    def usage_by(
        self,
        group: str,
        symbiote_id: str | None = None,
        *,
        session_id: str | None = None,
        root_session_id: str | None = None,
        subject_key: str | None | AnySubject = ANY_SUBJECT,
        purpose: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """The sums of ``usage_totals``, one row per value of ``group`` (a
        key of ``USAGE_GROUPS``), largest ``total_tokens`` first. Each row
        carries the group's value under the key ``group``."""
        column = self.USAGE_GROUPS.get(group)
        if column is None:
            raise ValueError(
                f"unknown usage group {group!r}: expected one of {sorted(self.USAGE_GROUPS)}"
            )
        where, params = self._usage_filter(
            symbiote_id, session_id, root_session_id, subject_key, purpose, since, until
        )
        rows = self._storage.fetch_all(
            f"SELECT {column} AS grp, {self._USAGE_SUMS}{self._USAGE_FROM}{where} "  # noqa: S608
            f"GROUP BY {column} ORDER BY total_tokens DESC, llm_calls DESC, grp LIMIT ?",
            (*params, limit),
        )
        out = []
        for row in rows:
            data = dict(row)
            value = data.pop("grp")
            out.append({group: value, **{k: int(v or 0) for k, v in data.items()}})
        return out

    def list_llm_calls(
        self,
        symbiote_id: str | None = None,
        *,
        session_id: str | None = None,
        root_session_id: str | None = None,
        subject_key: str | None | AnySubject = ANY_SUBJECT,
        purpose: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """The recorded model calls that match the filters, newest first."""
        where, params = self._usage_filter(
            symbiote_id, session_id, root_session_id, subject_key, purpose, since, until
        )
        rows = self._storage.fetch_all(
            "SELECT u.id, u.symbiote_id, u.session_id, u.root_session_id, u.purpose, "
            "u.provider, u.model, u.prompt_tokens, u.completion_tokens, u.total_tokens, "
            "u.cache_read_tokens, u.cache_write_tokens, u.cache_write_5m_tokens, "
            "u.cache_write_1h_tokens, u.reasoning_tokens, u.revision, "
            f"u.elapsed_ms, u.success, u.created_at{self._USAGE_FROM}{where} "  # noqa: S608
            "ORDER BY u.created_at DESC, u.rowid DESC LIMIT ?",
            (*params, limit),
        )
        return [dict(r) for r in rows]

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
