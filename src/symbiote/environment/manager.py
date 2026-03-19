"""EnvironmentManager — configure and query environment configs for symbiotes."""

from __future__ import annotations

import json

from symbiote.core.models import EnvironmentConfig
from symbiote.core.ports import StoragePort


class EnvironmentManager:
    """Manages environment configuration CRUD with workspace-level overrides."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    # ── public API ─────────────────────────────────────────────────────

    def configure(
        self,
        symbiote_id: str,
        workspace_id: str | None = None,
        tools: list[str] | None = None,
        services: list[str] | None = None,
        humans: list[str] | None = None,
        policies: dict | None = None,
        resources: dict | None = None,
        tool_tags: list[str] | None = None,
        tool_loading: str | None = None,
        tool_loop: bool | None = None,
    ) -> EnvironmentConfig:
        """Create or update an environment config for a symbiote+workspace combo."""
        existing = self._fetch_exact(symbiote_id, workspace_id)

        if existing is not None:
            # Update in place
            cfg = EnvironmentConfig(
                id=existing.id,
                symbiote_id=symbiote_id,
                workspace_id=workspace_id,
                tools=tools if tools is not None else existing.tools,
                services=services if services is not None else existing.services,
                humans=humans if humans is not None else existing.humans,
                policies=policies if policies is not None else existing.policies,
                resources=resources if resources is not None else existing.resources,
                tool_tags=tool_tags if tool_tags is not None else existing.tool_tags,
                tool_loading=tool_loading if tool_loading is not None else existing.tool_loading,
                tool_loop=tool_loop if tool_loop is not None else existing.tool_loop,
            )
            self._storage.execute(
                "UPDATE environment_configs SET "
                "tools_json = ?, services_json = ?, humans_json = ?, "
                "policies_json = ?, resources_json = ?, tool_tags_json = ?, "
                "tool_loading = ?, tool_loop = ? "
                "WHERE id = ?",
                (
                    json.dumps(cfg.tools),
                    json.dumps(cfg.services),
                    json.dumps(cfg.humans),
                    json.dumps(cfg.policies),
                    json.dumps(cfg.resources),
                    json.dumps(cfg.tool_tags),
                    cfg.tool_loading,
                    int(cfg.tool_loop),
                    cfg.id,
                ),
            )
            return cfg

        # Create new
        cfg = EnvironmentConfig(
            symbiote_id=symbiote_id,
            workspace_id=workspace_id,
            tools=tools or [],
            services=services or [],
            humans=humans or [],
            policies=policies or {},
            resources=resources or {},
            tool_tags=tool_tags or [],
            tool_loading=tool_loading or "full",
            tool_loop=tool_loop if tool_loop is not None else True,
        )
        self._storage.execute(
            "INSERT INTO environment_configs "
            "(id, symbiote_id, workspace_id, tools_json, services_json, "
            "humans_json, policies_json, resources_json, tool_tags_json, "
            "tool_loading, tool_loop) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cfg.id,
                cfg.symbiote_id,
                cfg.workspace_id,
                json.dumps(cfg.tools),
                json.dumps(cfg.services),
                json.dumps(cfg.humans),
                json.dumps(cfg.policies),
                json.dumps(cfg.resources),
                json.dumps(cfg.tool_tags),
                cfg.tool_loading,
                int(cfg.tool_loop),
            ),
        )
        return cfg

    def get_config(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> EnvironmentConfig | None:
        """Fetch config. If workspace_id given, try workspace-specific first, fall back to symbiote-level."""
        if workspace_id is not None:
            cfg = self._fetch_exact(symbiote_id, workspace_id)
            if cfg is not None:
                return cfg
        # Fall back to symbiote-level (workspace_id IS NULL)
        return self._fetch_exact(symbiote_id, None)

    def list_tools(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> list[str]:
        """Return tools list from config, or empty list if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return []
        return cfg.tools

    def is_tool_enabled(
        self, symbiote_id: str, tool_id: str, workspace_id: str | None = None
    ) -> bool:
        """Check if a tool is in the tools list."""
        return tool_id in self.list_tools(symbiote_id, workspace_id)

    def get_tool_tags(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> list[str]:
        """Return tool_tags from config, or empty list if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return []
        return cfg.tool_tags

    def get_tool_loading(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> str:
        """Return tool_loading mode from config, or 'full' if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return "full"
        return cfg.tool_loading

    def get_tool_loop(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> bool:
        """Return tool_loop flag from config, or True if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return True
        return cfg.tool_loop

    # ── private helpers ────────────────────────────────────────────────

    def _fetch_exact(
        self, symbiote_id: str, workspace_id: str | None
    ) -> EnvironmentConfig | None:
        """Fetch config matching exact symbiote_id + workspace_id (including NULL)."""
        if workspace_id is None:
            row = self._storage.fetch_one(
                "SELECT * FROM environment_configs "
                "WHERE symbiote_id = ? AND workspace_id IS NULL",
                (symbiote_id,),
            )
        else:
            row = self._storage.fetch_one(
                "SELECT * FROM environment_configs "
                "WHERE symbiote_id = ? AND workspace_id = ?",
                (symbiote_id, workspace_id),
            )
        if row is None:
            return None
        return self._row_to_config(row)

    @staticmethod
    def _row_to_config(row: dict) -> EnvironmentConfig:
        return EnvironmentConfig(
            id=row["id"],
            symbiote_id=row["symbiote_id"],
            workspace_id=row["workspace_id"],
            tools=json.loads(row["tools_json"]),
            services=json.loads(row["services_json"]),
            humans=json.loads(row["humans_json"]),
            policies=json.loads(row["policies_json"]),
            resources=json.loads(row["resources_json"]),
            tool_tags=json.loads(row.get("tool_tags_json") or "[]"),
            tool_loading=row.get("tool_loading") or "full",
            tool_loop=bool(row.get("tool_loop", 1)),
        )
