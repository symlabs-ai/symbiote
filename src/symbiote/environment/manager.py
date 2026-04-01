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
        tool_mode: str | None = None,
        tool_loop: bool | None = None,
        prompt_caching: bool | None = None,
        memory_share: float | None = None,
        knowledge_share: float | None = None,
        max_tool_iterations: int | None = None,
        tool_call_timeout: float | None = None,
        loop_timeout: float | None = None,
        context_mode: str | None = None,
    ) -> EnvironmentConfig:
        """Create or update an environment config for a symbiote+workspace combo."""
        existing = self._fetch_exact(symbiote_id, workspace_id)

        # Auto-derive tool_loop from tool_mode when tool_mode is set explicitly
        if tool_mode is not None and tool_loop is None:
            tool_loop = tool_mode != "instant"
        # Backward compat: if only tool_loop is set, derive tool_mode
        if tool_loop is not None and tool_mode is None:
            tool_mode = "instant" if not tool_loop else None  # None = keep existing

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
                tool_mode=tool_mode if tool_mode is not None else existing.tool_mode,
                tool_loop=tool_loop if tool_loop is not None else existing.tool_loop,
                prompt_caching=prompt_caching if prompt_caching is not None else existing.prompt_caching,
                memory_share=memory_share if memory_share is not None else existing.memory_share,
                knowledge_share=knowledge_share if knowledge_share is not None else existing.knowledge_share,
                max_tool_iterations=max_tool_iterations if max_tool_iterations is not None else existing.max_tool_iterations,
                tool_call_timeout=tool_call_timeout if tool_call_timeout is not None else existing.tool_call_timeout,
                loop_timeout=loop_timeout if loop_timeout is not None else existing.loop_timeout,
                context_mode=context_mode if context_mode is not None else existing.context_mode,
            )
            self._storage.execute(
                "UPDATE environment_configs SET "
                "tools_json = ?, services_json = ?, humans_json = ?, "
                "policies_json = ?, resources_json = ?, tool_tags_json = ?, "
                "tool_loading = ?, tool_loop = ?, prompt_caching = ?, "
                "memory_share = ?, knowledge_share = ?, max_tool_iterations = ?, "
                "tool_call_timeout = ?, loop_timeout = ?, tool_mode = ?, context_mode = ? "
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
                    int(cfg.prompt_caching),
                    cfg.memory_share,
                    cfg.knowledge_share,
                    cfg.max_tool_iterations,
                    cfg.tool_call_timeout,
                    cfg.loop_timeout,
                    cfg.tool_mode,
                    cfg.context_mode,
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
            tool_mode=tool_mode if tool_mode is not None else "brief",
            tool_loop=tool_loop if tool_loop is not None else True,
            prompt_caching=prompt_caching if prompt_caching is not None else False,
            memory_share=memory_share if memory_share is not None else 0.40,
            knowledge_share=knowledge_share if knowledge_share is not None else 0.25,
            max_tool_iterations=max_tool_iterations if max_tool_iterations is not None else 10,
            tool_call_timeout=tool_call_timeout if tool_call_timeout is not None else 30.0,
            loop_timeout=loop_timeout if loop_timeout is not None else 300.0,
            context_mode=context_mode or "packed",
        )
        self._storage.execute(
            "INSERT INTO environment_configs "
            "(id, symbiote_id, workspace_id, tools_json, services_json, "
            "humans_json, policies_json, resources_json, tool_tags_json, "
            "tool_loading, tool_loop, prompt_caching, memory_share, knowledge_share, "
            "max_tool_iterations, tool_call_timeout, loop_timeout, tool_mode, context_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                int(cfg.prompt_caching),
                cfg.memory_share,
                cfg.knowledge_share,
                cfg.max_tool_iterations,
                cfg.tool_call_timeout,
                cfg.loop_timeout,
                cfg.tool_mode,
                cfg.context_mode,
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

    def get_tool_mode(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> str:
        """Return tool_mode from config, or 'brief' if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return "brief"
        return cfg.tool_mode

    def get_tool_loop(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> bool:
        """Return tool_loop flag from config, or True if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return True
        return cfg.tool_loop

    def get_prompt_caching(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> bool:
        """Return prompt_caching flag from config, or False if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return False
        return cfg.prompt_caching

    def get_memory_share(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> float:
        """Return memory_share from config, or 0.40 if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return 0.40
        return cfg.memory_share

    def get_knowledge_share(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> float:
        """Return knowledge_share from config, or 0.25 if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return 0.25
        return cfg.knowledge_share

    def get_max_tool_iterations(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> int:
        """Return max_tool_iterations from config, or 10 if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return 10
        return cfg.max_tool_iterations

    def get_tool_call_timeout(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> float:
        """Return tool_call_timeout from config, or 30.0 if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return 30.0
        return cfg.tool_call_timeout

    def get_loop_timeout(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> float:
        """Return loop_timeout from config, or 300.0 if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return 300.0
        return cfg.loop_timeout

    def get_context_mode(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> str:
        """Return context_mode from config, or 'packed' if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return "packed"
        return cfg.context_mode

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
            tool_mode=row.get("tool_mode") or "brief",
            tool_loop=bool(row.get("tool_loop", 1)),
            prompt_caching=bool(row.get("prompt_caching", 0)),
            memory_share=float(row.get("memory_share", 0.40) or 0.40),
            knowledge_share=float(row.get("knowledge_share", 0.25) or 0.25),
            max_tool_iterations=int(row.get("max_tool_iterations", 10) or 10),
            tool_call_timeout=float(row.get("tool_call_timeout", 30.0) or 30.0),
            loop_timeout=float(row.get("loop_timeout", 300.0) or 300.0),
            context_mode=row.get("context_mode") or "packed",
        )
