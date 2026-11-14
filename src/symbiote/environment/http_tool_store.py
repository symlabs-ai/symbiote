"""Persistence for HTTP tools registered at runtime (HTTP API, CLI).

Tools registered in code by a host live only in its process; tools added
through the API or the CLI must survive a restart and be visible to every
entry point of the same database, so they are stored here and loaded into
the kernel's ToolGateway at startup.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from symbiote.core.ports import StoragePort
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor


class HttpToolRepository:
    """Table ``http_tools`` is created by the storage adapter's schema."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    def owner(self, tool_id: str) -> str | None:
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM http_tools WHERE tool_id = ?", (tool_id,)
        )
        return row["symbiote_id"] if row else None

    def save(
        self, symbiote_id: str, descriptor: ToolDescriptor, config: HttpToolConfig
    ) -> None:
        # header_factory is code and allow_internal is never persisted.
        config_json = config.model_dump(mode="json", exclude={"header_factory"})
        self._storage.execute(
            "INSERT INTO http_tools (tool_id, symbiote_id, descriptor_json, config_json, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(tool_id) DO UPDATE SET "
            "descriptor_json = excluded.descriptor_json, config_json = excluded.config_json",
            (
                descriptor.tool_id,
                symbiote_id,
                json.dumps(descriptor.model_dump(mode="json")),
                json.dumps(config_json),
                datetime.now(tz=UTC).isoformat(),
            ),
        )

    def delete(self, tool_id: str) -> None:
        self._storage.execute("DELETE FROM http_tools WHERE tool_id = ?", (tool_id,))

    def list_all(self) -> list[tuple[str, ToolDescriptor, HttpToolConfig]]:
        rows = self._storage.fetch_all(
            "SELECT symbiote_id, descriptor_json, config_json FROM http_tools ORDER BY created_at"
        )
        return [
            (
                r["symbiote_id"],
                ToolDescriptor(**json.loads(r["descriptor_json"])),
                HttpToolConfig(**json.loads(r["config_json"])),
            )
            for r in rows
        ]
