"""IdentityManager — create, read, update symbiote identities."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.models import Symbiote
from symbiote.core.ports import StoragePort


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _uuid() -> str:
    return str(uuid4())


class IdentityManager:
    """Manages symbiote identity CRUD and persona audit trail."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    # ── public API ─────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        role: str,
        persona: dict | None = None,
        owner_id: str | None = None,
    ) -> Symbiote:
        """Create and persist a new symbiote, returning the domain model."""
        persona = persona or {}
        now = _utcnow()
        sym = Symbiote(
            name=name,
            role=role,
            owner_id=owner_id,
            persona_json=persona,
            created_at=now,
            updated_at=now,
        )
        self._storage.execute(
            "INSERT INTO symbiotes "
            "(id, name, role, owner_id, persona_json, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sym.id,
                sym.name,
                sym.role,
                sym.owner_id,
                json.dumps(sym.persona_json),
                sym.status,
                sym.created_at.isoformat(),
                sym.updated_at.isoformat(),
            ),
        )
        return sym

    def get(self, symbiote_id: str) -> Symbiote | None:
        """Fetch a symbiote by ID, returning None if not found."""
        row = self._storage.fetch_one(
            "SELECT * FROM symbiotes WHERE id = ?", (symbiote_id,)
        )
        if row is None:
            return None
        return self._row_to_symbiote(row)

    def update_persona(self, symbiote_id: str, persona: dict) -> Symbiote:
        """Update persona, write audit trail, return updated model.

        Raises EntityNotFoundError if the symbiote does not exist.
        """
        existing = self.get(symbiote_id)
        if existing is None:
            raise EntityNotFoundError("Symbiote", symbiote_id)

        old_persona = existing.persona_json
        now = _utcnow()

        # Write audit record.
        self._storage.execute(
            "INSERT INTO persona_audit "
            "(id, symbiote_id, old_persona_json, new_persona_json, changed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                _uuid(),
                symbiote_id,
                json.dumps(old_persona),
                json.dumps(persona),
                now.isoformat(),
            ),
        )

        # Update symbiote row.
        self._storage.execute(
            "UPDATE symbiotes SET persona_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(persona), now.isoformat(), symbiote_id),
        )

        return self.get(symbiote_id)  # type: ignore[return-value]

    def update(
        self,
        symbiote_id: str,
        name: str | None = None,
        role: str | None = None,
        persona: dict | None = None,
    ) -> Symbiote:
        """Update name/role and optionally persona. Raises EntityNotFoundError if not found."""
        existing = self.get(symbiote_id)
        if existing is None:
            raise EntityNotFoundError("Symbiote", symbiote_id)
        new_name = name if name is not None else existing.name
        new_role = role if role is not None else existing.role
        now = _utcnow()
        self._storage.execute(
            "UPDATE symbiotes SET name=?, role=?, updated_at=? WHERE id=?",
            (new_name, new_role, now.isoformat(), symbiote_id),
        )
        if persona is not None:
            return self.update_persona(symbiote_id, persona)
        return self.get(symbiote_id)  # type: ignore[return-value]

    def delete(self, symbiote_id: str) -> None:
        """Soft-delete a symbiote (status='deleted'). Raises EntityNotFoundError if not found."""
        existing = self.get(symbiote_id)
        if existing is None:
            raise EntityNotFoundError("Symbiote", symbiote_id)
        now = _utcnow()
        self._storage.execute(
            "UPDATE symbiotes SET status='deleted', updated_at=? WHERE id=?",
            (now.isoformat(), symbiote_id),
        )

    def list_all(self) -> list[Symbiote]:
        """Return all non-deleted symbiotes."""
        rows = self._storage.fetch_all(
            "SELECT * FROM symbiotes WHERE status != 'deleted'"
        )
        return [self._row_to_symbiote(r) for r in rows]

    # ── private helpers ────────────────────────────────────────────────

    @staticmethod
    def _row_to_symbiote(row: dict) -> Symbiote:
        """Convert a DB row dict into a Symbiote domain model."""
        persona = row.get("persona_json")
        if isinstance(persona, str):
            persona = json.loads(persona)
        return Symbiote(
            id=row["id"],
            name=row["name"],
            role=row.get("role", ""),
            owner_id=row.get("owner_id"),
            persona_json=persona or {},
            status=row.get("status", "active"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
