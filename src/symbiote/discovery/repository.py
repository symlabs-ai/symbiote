"""Repository for discovered_tools table."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from symbiote.core.ports import StoragePort
from symbiote.discovery.models import DiscoveredTool


class DiscoveredToolRepository:
    """CRUD operations for the discovered_tools table."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    def save(self, tool: DiscoveredTool) -> DiscoveredTool:
        """Insert or replace a discovered tool."""
        self._storage.execute(
            """
            INSERT INTO discovered_tools
                (id, symbiote_id, tool_id, name, description, handler_type,
                 method, url_template, parameters_json, tags_json,
                 status, source_path, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbiote_id, tool_id) DO UPDATE SET
                name           = excluded.name,
                description    = excluded.description,
                handler_type   = excluded.handler_type,
                method         = excluded.method,
                url_template   = excluded.url_template,
                parameters_json = excluded.parameters_json,
                tags_json      = excluded.tags_json,
                source_path    = excluded.source_path,
                discovered_at  = excluded.discovered_at
            """,
            (
                tool.id or str(uuid4()),
                tool.symbiote_id,
                tool.tool_id,
                tool.name,
                tool.description,
                tool.handler_type,
                tool.method,
                tool.url_template,
                json.dumps(tool.parameters),
                json.dumps(tool.tags),
                tool.status,
                tool.source_path,
                tool.discovered_at or datetime.now(tz=UTC).isoformat(),
            ),
        )
        return tool

    def list(
        self,
        symbiote_id: str,
        status: str | None = None,
    ) -> list[DiscoveredTool]:
        """Return discovered tools for a symbiote, optionally filtered by status."""
        if status:
            rows = self._storage.fetch_all(
                "SELECT * FROM discovered_tools WHERE symbiote_id = ? AND status = ? "
                "ORDER BY discovered_at DESC",
                (symbiote_id, status),
            )
        else:
            rows = self._storage.fetch_all(
                "SELECT * FROM discovered_tools WHERE symbiote_id = ? "
                "ORDER BY discovered_at DESC",
                (symbiote_id,),
            )
        return [self._row_to_model(r) for r in rows]

    def get(self, symbiote_id: str, tool_id: str) -> DiscoveredTool | None:
        row = self._storage.fetch_one(
            "SELECT * FROM discovered_tools WHERE symbiote_id = ? AND tool_id = ?",
            (symbiote_id, tool_id),
        )
        return self._row_to_model(row) if row else None

    def set_status(
        self,
        symbiote_id: str,
        tool_id: str,
        status: str,
    ) -> bool:
        """Update status of a discovered tool. Returns True if row existed."""
        approved_at = (
            datetime.now(tz=UTC).isoformat() if status == "approved" else None
        )
        cur = self._storage.execute(
            "UPDATE discovered_tools SET status = ?, approved_at = ? "
            "WHERE symbiote_id = ? AND tool_id = ?",
            (status, approved_at, symbiote_id, tool_id),
        )
        return cur.rowcount > 0  # type: ignore[union-attr]

    def delete(self, symbiote_id: str, tool_id: str) -> bool:
        cur = self._storage.execute(
            "DELETE FROM discovered_tools WHERE symbiote_id = ? AND tool_id = ?",
            (symbiote_id, tool_id),
        )
        return cur.rowcount > 0  # type: ignore[union-attr]

    # ── private ──────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_model(row: dict) -> DiscoveredTool:
        return DiscoveredTool(
            id=row["id"],
            symbiote_id=row["symbiote_id"],
            tool_id=row["tool_id"],
            name=row["name"],
            description=row.get("description", ""),
            handler_type=row.get("handler_type", "http"),
            method=row.get("method"),
            url_template=row.get("url_template"),
            parameters=json.loads(row.get("parameters_json") or "{}"),
            tags=json.loads(row.get("tags_json") or "[]"),
            status=row.get("status", "pending"),
            source_path=row.get("source_path"),
            discovered_at=row.get("discovered_at", ""),
            approved_at=row.get("approved_at"),
        )
