"""Tool calls that wait for a person's approval.

When the approval callback answers ``ApprovalDecision.defer(...)`` the call is
not executed: it is stored here as a pending approval, the model is told that
it is waiting, and the turn ends. Later the host calls
``kernel.resolve_pending(id, approved)``; an approved call runs then, in the
context it was asked in (same symbiote, same session, same end user), and the
outcome is given to the model at the start of the next turn of that
conversation or of that end user.

States: ``pending`` → ``approved`` (being executed) → ``executed`` | ``failed``;
``pending`` → ``denied``; ``pending`` → ``expired``.

The parameters of the call are kept as they came for as long as the request
is pending, because running the call later needs them; they are erased when
the request is resolved or expires. What is shown (``PendingApproval.params``)
is a copy with the secrets the redactor knows removed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.core.ports import StoragePort
from symbiote.core.subjects import ANY_SUBJECT, AnySubject

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
EXECUTED = "executed"
FAILED = "failed"
STATUSES = (PENDING, APPROVED, DENIED, EXPIRED, EXECUTED, FAILED)
# States that are an outcome the conversation should hear about.
_OUTCOMES = (DENIED, EXPIRED, EXECUTED, FAILED)

# What the model is told when a call is deferred (``ApprovalDecision.message``
# replaces it).
DEFAULT_PENDING_MESSAGE = (
    "This action needs a person's approval and has NOT been executed. The "
    "request was recorded and will be decided later. Do not call the tool "
    "again for it; tell the user that the request is waiting for approval."
)
# A stored tool result longer than this is cut (it is shown to a person and
# given to the model in a later turn).
_MAX_RESULT_CHARS = 20_000

_COLUMNS = (
    "id, symbiote_id, session_id, root_session_id, root_symbiote_id, subject_key, "
    "tool_id, params_preview_json, risk_level, reason, status, created_at, expires_at, "
    "resolved_at, resolved_by, note, result_json, error, delivered_at"
)


class PendingApproval(BaseModel):
    """A tool call that waits, or waited, for a person's approval."""

    id: str
    symbiote_id: str
    session_id: str | None = None
    # The conversation the host started (differs from ``session_id`` /
    # ``symbiote_id`` when a delegated symbiote asked).
    root_session_id: str | None = None
    root_symbiote_id: str | None = None
    subject_key: str | None = None
    tool_id: str
    # The call's parameters with the known secrets removed.
    params: dict[str, Any] | None = None
    risk_level: str | None = None
    reason: str | None = None
    status: str
    created_at: str
    expires_at: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None
    note: str | None = None
    # The tool's output once executed (``error`` when it failed).
    result: Any = None
    error: str | None = None
    # When the outcome was given to the model.
    delivered_at: str | None = None


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _loads(raw: Any) -> Any:
    if raw in (None, ""):
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return raw


class PendingApprovalStore:
    """Reads and writes ``pending_approvals``.

    ``preview(tool_id, params)`` returns the parameters as they may be shown
    and ``redact(value)`` removes known secrets from any JSON-like value;
    both come from the kernel's redactor.
    """

    def __init__(
        self,
        storage: StoragePort,
        *,
        preview: Callable[[str, dict], dict] | None = None,
        redact: Callable[[Any], Any] | None = None,
    ) -> None:
        self._storage = storage
        self._preview = preview or (lambda _tool_id, params: params)
        self._redact = redact or (lambda value: value)

    # ── writing ────────────────────────────────────────────────────────

    def create(
        self,
        *,
        symbiote_id: str,
        session_id: str | None,
        root_session_id: str | None,
        root_symbiote_id: str | None,
        subject_key: str | None,
        tool_id: str,
        params: dict,
        risk_level: str | None,
        reason: str | None,
        ttl_seconds: float | None,
    ) -> PendingApproval:
        approval_id = str(uuid4())
        created = datetime.now(tz=UTC)
        expires = (
            (created + timedelta(seconds=ttl_seconds)).isoformat()
            if ttl_seconds is not None
            else None
        )
        self._storage.execute(
            "INSERT INTO pending_approvals "
            "(id, symbiote_id, session_id, root_session_id, root_symbiote_id, subject_key, "
            "tool_id, params_json, params_preview_json, risk_level, reason, status, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                approval_id,
                symbiote_id,
                session_id,
                root_session_id,
                root_symbiote_id,
                subject_key,
                tool_id,
                json.dumps(params, default=str),
                json.dumps(self._redact(self._preview(tool_id, params)), default=str),
                risk_level,
                self._redact(reason) if reason else None,
                PENDING,
                created.isoformat(),
                expires,
            ),
        )
        found = self.get(approval_id)
        assert found is not None
        return found

    def expire_due(self) -> int:
        """Mark as expired every pending request past its time; returns how many."""
        now = _now()
        result = self._storage.execute(
            "UPDATE pending_approvals SET status = ?, resolved_at = ?, params_json = NULL "
            "WHERE status = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            (EXPIRED, now, PENDING, now),
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def claim(
        self, approval_id: str, approved: bool, *, resolved_by: str | None, note: str | None
    ) -> PendingApproval:
        """Take a pending request for resolution: ``approved`` or ``denied``.

        Atomic: of two callers resolving the same request, one gets it and
        the other gets ``ValidationError``. Raises ``EntityNotFoundError``
        for an unknown id.
        """
        status = APPROVED if approved else DENIED
        # A denied request needs its parameters no more.
        keep = "params_json" if approved else "NULL"
        result = self._storage.execute(
            f"UPDATE pending_approvals SET status = ?, resolved_at = ?, resolved_by = ?, "  # noqa: S608
            f"note = ?, params_json = {keep} WHERE id = ? AND status = ?",
            (
                status,
                _now(),
                resolved_by,
                self._redact(note) if note else None,
                approval_id,
                PENDING,
            ),
        )
        current = self.get(approval_id)
        if current is None:
            raise EntityNotFoundError("PendingApproval", approval_id)
        if not int(getattr(result, "rowcount", 0) or 0):
            raise ValidationError(
                f"Pending approval {approval_id} is {current.status}, not pending: "
                "it was already resolved or it expired"
            )
        return current

    def raw_params(self, approval_id: str) -> dict:
        """The parameters of the call as they came (only while they are kept)."""
        row = self._storage.fetch_one(
            "SELECT params_json FROM pending_approvals WHERE id = ?", (approval_id,)
        )
        params = _loads(row["params_json"]) if row else None
        return params if isinstance(params, dict) else {}

    def finish(
        self, approval_id: str, *, executed: bool, result: Any = None, error: str | None = None
    ) -> PendingApproval:
        """Record the outcome of an approved call and erase its parameters."""
        stored = json.dumps(self._redact(result), default=str) if result is not None else None
        if stored is not None and len(stored) > _MAX_RESULT_CHARS:
            stored = json.dumps(stored[:_MAX_RESULT_CHARS] + "… [truncated]")
        self._storage.execute(
            "UPDATE pending_approvals SET status = ?, result_json = ?, error = ?, "
            "params_json = NULL WHERE id = ?",
            (
                EXECUTED if executed else FAILED,
                stored,
                self._redact(error) if error else None,
                approval_id,
            ),
        )
        found = self.get(approval_id)
        assert found is not None
        return found

    def mark_delivered(self, approval_ids: list[str]) -> None:
        now = _now()
        for approval_id in approval_ids:
            self._storage.execute(
                "UPDATE pending_approvals SET delivered_at = ? "
                "WHERE id = ? AND delivered_at IS NULL",
                (now, approval_id),
            )

    # ── reading ────────────────────────────────────────────────────────

    def get(self, approval_id: str) -> PendingApproval | None:
        row = self._storage.fetch_one(
            f"SELECT {_COLUMNS} FROM pending_approvals WHERE id = ?",  # noqa: S608
            (approval_id,),
        )
        return self._to_model(row) if row else None

    def find(
        self,
        symbiote_id: str | None = None,
        *,
        status: str | None = PENDING,
        session_id: str | None = None,
        subject_key: str | None | AnySubject = ANY_SUBJECT,
        limit: int = 100,
    ) -> list[PendingApproval]:
        """Requests, newest first. ``status=None`` lists every state;
        ``subject_key`` filters one end user (``None``: no end user)."""
        conditions: list[str] = []
        params: list[Any] = []
        for column, value in (("symbiote_id", symbiote_id), ("session_id", session_id)):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(value)
        if subject_key is None:
            conditions.append("subject_key IS NULL")
        elif not isinstance(subject_key, AnySubject):
            conditions.append("subject_key = ?")
            params.append(subject_key)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self._storage.fetch_all(
            f"SELECT {_COLUMNS} FROM pending_approvals{where} "  # noqa: S608
            "ORDER BY created_at DESC, rowid DESC",
            tuple(params),
        )
        # The state a request is really in is decided after reading: one
        # past its time is expired even before anything wrote that down.
        found = [self._to_model(r) for r in rows]
        if status is not None:
            found = [a for a in found if a.status == status]
        return found[:limit]

    def undelivered(
        self, *, symbiote_id: str, session_id: str, subject_key: str | None
    ) -> list[PendingApproval]:
        """Outcomes this conversation has not heard yet: the requests made
        in it (or under it, by a delegated symbiote) and, for an end user,
        the ones that person made to this symbiote in any session."""
        sql = (
            f"SELECT {_COLUMNS} FROM pending_approvals "  # noqa: S608
            "WHERE delivered_at IS NULL AND COALESCE(root_symbiote_id, symbiote_id) = ? "
            "AND (COALESCE(root_session_id, session_id) = ?"
        )
        params: list[Any] = [symbiote_id, session_id]
        if subject_key is not None:
            sql += " OR subject_key = ?"
            params.append(subject_key)
        sql += ") ORDER BY created_at, rowid"
        rows = self._storage.fetch_all(sql, tuple(params))
        return [a for a in (self._to_model(r) for r in rows) if a.status in _OUTCOMES]

    @staticmethod
    def _to_model(row: dict) -> PendingApproval:
        data = dict(row)
        params = _loads(data.pop("params_preview_json", None))
        data["params"] = params if isinstance(params, dict) else None
        data["result"] = _loads(data.pop("result_json", None))
        if (
            data["status"] == PENDING
            and data.get("expires_at")
            and data["expires_at"] <= _now()
        ):
            data["status"] = EXPIRED
        return PendingApproval(**data)


def render_outcomes(approvals: list[PendingApproval], *, max_result_chars: int = 3000) -> str:
    """The text that tells the model what became of earlier requests."""
    lines = [
        "These actions were asked earlier and needed a person's approval. "
        "This is what was decided since; tell the user."
    ]
    for approval in approvals:
        asked = f"{approval.tool_id}({json.dumps(approval.params or {}, default=str)})"
        note = f" Note from the person: {approval.note}" if approval.note else ""
        if approval.status == EXECUTED:
            result = json.dumps(approval.result, default=str)
            if len(result) > max_result_chars:
                result = result[:max_result_chars] + "… [truncated]"
            line = f"- {asked}: APPROVED and executed. Result: {result}.{note}"
        elif approval.status == FAILED:
            line = (
                f"- {asked}: APPROVED, but the execution failed: "
                f"{approval.error or 'unknown error'}.{note}"
            )
        elif approval.status == DENIED:
            line = f"- {asked}: DENIED, it was not executed.{note}"
        else:
            line = f"- {asked}: EXPIRED without an answer, it was not executed."
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "APPROVED",
    "DEFAULT_PENDING_MESSAGE",
    "DENIED",
    "EXECUTED",
    "EXPIRED",
    "FAILED",
    "PENDING",
    "STATUSES",
    "PendingApproval",
    "PendingApprovalStore",
    "render_outcomes",
]
