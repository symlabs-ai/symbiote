"""EnvironmentManager — configure and query environment configs for symbiotes."""

from __future__ import annotations

import json

from symbiote.core.models import EnvironmentConfig
from symbiote.core.ports import StoragePort


class EnvironmentManager:
    """Manages environment configuration CRUD with workspace-level overrides."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage
        # Back-reference set by SymbioteKernel after construction so configure()
        # can validate evolver_llm requirements without circular imports.
        self._kernel = None  # type: ignore[assignment]

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
        # Long-run mode fields
        planner_prompt: str | None = None,
        evaluator_prompt: str | None = None,
        evaluator_criteria: list[dict] | None = None,
        context_strategy: str | None = None,
        max_blocks: int | None = None,
        # Dream mode fields
        dream_mode: str | None = None,
        dream_max_llm_calls: int | None = None,
        dream_min_sessions: int | None = None,
        # Reflection mode fields
        reflection_mode: str | None = None,
        reflection_max_tokens: int | None = None,
        # Skill self-improvement fields (Sprint 4)
        skill_review_enabled: bool | None = None,
        skill_nudge_interval: int | None = None,
        max_active_skills: int | None = None,
        max_quarantine_skills: int | None = None,
        # Sprint 5 — lifecycle automation
        skill_auto_promote_threshold: int | None = None,
        skill_quarantine_timeout_days: int | None = None,
    ) -> EnvironmentConfig:
        """Create or update an environment config for a symbiote+workspace combo."""
        # Guard: reflection_mode='llm' requires evolver_llm; 'llm_main' requires main llm.
        # This is the requirement-vs-fallback policy: opt-in to LLM reflection must be
        # explicit about which model pays the bill. Fails at configure() rather than
        # silently consuming the main (potentially expensive) model on every close_session.
        if reflection_mode in {"llm", "hybrid"}:
            kernel = getattr(self, "_kernel", None)
            evolver = getattr(kernel, "_evolver_llm", None) if kernel else None
            if evolver is None:
                raise ValueError(
                    f"reflection_mode={reflection_mode!r} requires kernel.set_evolver_llm(...) "
                    f"first. This protects against runaway cost from running LLM reflection "
                    f"on every close_session with the main (potentially expensive) model. "
                    f"Either set an evolver LLM (e.g. claude-haiku-4-5), or use "
                    f"reflection_mode='llm_main' to opt into the main LLM explicitly."
                )
        if reflection_mode == "llm_main":
            kernel = getattr(self, "_kernel", None)
            main_llm = getattr(kernel, "_llm", None) if kernel else None
            if main_llm is None:
                raise ValueError(
                    "reflection_mode='llm_main' requires a main LLM to be configured on the kernel."
                )

        # Same cost guard for the background skill review fork: turning it on
        # without an evolver LLM would silently use the (expensive) main model
        # for every nudge interval. Fail fast at configure().
        if skill_review_enabled:
            kernel = getattr(self, "_kernel", None)
            evolver = getattr(kernel, "_evolver_llm", None) if kernel else None
            if evolver is None:
                raise ValueError(
                    "skill_review_enabled=True requires kernel.set_evolver_llm(...) first. "
                    "Background skill review runs N times per session — using the main LLM "
                    "would explode cost. Set a cheap aux model (e.g. claude-haiku-4-5) first."
                )

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
                planner_prompt=planner_prompt if planner_prompt is not None else existing.planner_prompt,
                evaluator_prompt=evaluator_prompt if evaluator_prompt is not None else existing.evaluator_prompt,
                evaluator_criteria=evaluator_criteria if evaluator_criteria is not None else existing.evaluator_criteria,
                context_strategy=context_strategy if context_strategy is not None else existing.context_strategy,
                max_blocks=max_blocks if max_blocks is not None else existing.max_blocks,
                dream_mode=dream_mode if dream_mode is not None else existing.dream_mode,
                dream_max_llm_calls=dream_max_llm_calls if dream_max_llm_calls is not None else existing.dream_max_llm_calls,
                dream_min_sessions=dream_min_sessions if dream_min_sessions is not None else existing.dream_min_sessions,
                reflection_mode=reflection_mode if reflection_mode is not None else existing.reflection_mode,
                reflection_max_tokens=reflection_max_tokens if reflection_max_tokens is not None else existing.reflection_max_tokens,
                skill_review_enabled=skill_review_enabled if skill_review_enabled is not None else existing.skill_review_enabled,
                skill_nudge_interval=skill_nudge_interval if skill_nudge_interval is not None else existing.skill_nudge_interval,
                max_active_skills=max_active_skills if max_active_skills is not None else existing.max_active_skills,
                max_quarantine_skills=max_quarantine_skills if max_quarantine_skills is not None else existing.max_quarantine_skills,
                skill_auto_promote_threshold=skill_auto_promote_threshold if skill_auto_promote_threshold is not None else existing.skill_auto_promote_threshold,
                skill_quarantine_timeout_days=skill_quarantine_timeout_days if skill_quarantine_timeout_days is not None else existing.skill_quarantine_timeout_days,
            )
            self._storage.execute(
                "UPDATE environment_configs SET "
                "tools_json = ?, services_json = ?, humans_json = ?, "
                "policies_json = ?, resources_json = ?, tool_tags_json = ?, "
                "tool_loading = ?, tool_loop = ?, prompt_caching = ?, "
                "memory_share = ?, knowledge_share = ?, max_tool_iterations = ?, "
                "tool_call_timeout = ?, loop_timeout = ?, tool_mode = ?, context_mode = ?, "
                "planner_prompt = ?, evaluator_prompt = ?, evaluator_criteria_json = ?, "
                "context_strategy = ?, max_blocks = ?, "
                "dream_mode = ?, dream_max_llm_calls = ?, dream_min_sessions = ?, "
                "reflection_mode = ?, reflection_max_tokens = ?, "
                "skill_review_enabled = ?, skill_nudge_interval = ?, max_active_skills = ?, "
                "max_quarantine_skills = ?, "
                "skill_auto_promote_threshold = ?, skill_quarantine_timeout_days = ? "
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
                    cfg.planner_prompt,
                    cfg.evaluator_prompt,
                    json.dumps(cfg.evaluator_criteria) if cfg.evaluator_criteria else None,
                    cfg.context_strategy,
                    cfg.max_blocks,
                    cfg.dream_mode,
                    cfg.dream_max_llm_calls,
                    cfg.dream_min_sessions,
                    cfg.reflection_mode,
                    cfg.reflection_max_tokens,
                    int(cfg.skill_review_enabled),
                    cfg.skill_nudge_interval,
                    cfg.max_active_skills,
                    cfg.max_quarantine_skills,
                    cfg.skill_auto_promote_threshold,
                    cfg.skill_quarantine_timeout_days,
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
            planner_prompt=planner_prompt,
            evaluator_prompt=evaluator_prompt,
            evaluator_criteria=evaluator_criteria,
            context_strategy=context_strategy or "hybrid",
            max_blocks=max_blocks if max_blocks is not None else 20,
            dream_mode=dream_mode or "off",
            dream_max_llm_calls=dream_max_llm_calls if dream_max_llm_calls is not None else 10,
            dream_min_sessions=dream_min_sessions if dream_min_sessions is not None else 5,
            reflection_mode=reflection_mode or "keyword",
            reflection_max_tokens=reflection_max_tokens if reflection_max_tokens is not None else 4000,
            skill_review_enabled=skill_review_enabled if skill_review_enabled is not None else False,
            skill_nudge_interval=skill_nudge_interval if skill_nudge_interval is not None else 10,
            max_active_skills=max_active_skills if max_active_skills is not None else 20,
            max_quarantine_skills=max_quarantine_skills if max_quarantine_skills is not None else 10,
            skill_auto_promote_threshold=skill_auto_promote_threshold if skill_auto_promote_threshold is not None else 3,
            skill_quarantine_timeout_days=skill_quarantine_timeout_days if skill_quarantine_timeout_days is not None else 14,
        )
        self._storage.execute(
            "INSERT INTO environment_configs "
            "(id, symbiote_id, workspace_id, tools_json, services_json, "
            "humans_json, policies_json, resources_json, tool_tags_json, "
            "tool_loading, tool_loop, prompt_caching, memory_share, knowledge_share, "
            "max_tool_iterations, tool_call_timeout, loop_timeout, tool_mode, context_mode, "
            "planner_prompt, evaluator_prompt, evaluator_criteria_json, context_strategy, max_blocks, "
            "dream_mode, dream_max_llm_calls, dream_min_sessions, "
            "reflection_mode, reflection_max_tokens, "
            "skill_review_enabled, skill_nudge_interval, max_active_skills, "
            "max_quarantine_skills, "
            "skill_auto_promote_threshold, skill_quarantine_timeout_days) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                cfg.planner_prompt,
                cfg.evaluator_prompt,
                json.dumps(cfg.evaluator_criteria) if cfg.evaluator_criteria else None,
                cfg.context_strategy,
                cfg.max_blocks,
                cfg.dream_mode,
                cfg.dream_max_llm_calls,
                cfg.dream_min_sessions,
                cfg.reflection_mode,
                cfg.reflection_max_tokens,
                int(cfg.skill_review_enabled),
                cfg.skill_nudge_interval,
                cfg.max_active_skills,
                cfg.max_quarantine_skills,
                cfg.skill_auto_promote_threshold,
                cfg.skill_quarantine_timeout_days,
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

    def get_long_run_config(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> dict:
        """Return long-run specific config fields as a dict."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return {}
        return {
            "planner_prompt": cfg.planner_prompt,
            "evaluator_prompt": cfg.evaluator_prompt,
            "evaluator_criteria": cfg.evaluator_criteria,
            "context_strategy": cfg.context_strategy,
            "max_blocks": cfg.max_blocks,
        }

    def get_dream_mode(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> str:
        """Return dream_mode from config, or 'off' if no config."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return "off"
        return cfg.dream_mode

    def get_dream_config(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> dict:
        """Return all dream-related config fields as a dict."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return {"dream_mode": "off", "dream_max_llm_calls": 10, "dream_min_sessions": 5}
        return {
            "dream_mode": cfg.dream_mode,
            "dream_max_llm_calls": cfg.dream_max_llm_calls,
            "dream_min_sessions": cfg.dream_min_sessions,
        }

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
    def _int_or_default(row: dict, key: str, default: int) -> int:
        """Return ``int(row[key])`` if non-None, else ``default``.

        Distinguishes ``None`` (missing column / pre-migration row) from
        ``0`` (the user opted out). The older pattern ``int(row.get(k, d) or d)``
        treats ``0`` as falsy and forces it back to ``d`` — buggy for any
        field where ``0`` carries semantic meaning (e.g. Sprint 5's
        ``skill_auto_promote_threshold=0`` ≡ disabled).
        """
        val = row.get(key)
        return int(val) if val is not None else default

    @staticmethod
    def _row_to_config(row: dict) -> EnvironmentConfig:
        _int = EnvironmentManager._int_or_default
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
            planner_prompt=row.get("planner_prompt"),
            evaluator_prompt=row.get("evaluator_prompt"),
            evaluator_criteria=json.loads(row["evaluator_criteria_json"]) if row.get("evaluator_criteria_json") else None,
            context_strategy=row.get("context_strategy") or "hybrid",
            max_blocks=int(row.get("max_blocks", 20) or 20),
            dream_mode=row.get("dream_mode") or "off",
            dream_max_llm_calls=int(row.get("dream_max_llm_calls", 10) or 10),
            dream_min_sessions=int(row.get("dream_min_sessions", 5) or 5),
            reflection_mode=row.get("reflection_mode") or "keyword",
            reflection_max_tokens=int(row.get("reflection_max_tokens", 4000) or 4000),
            skill_review_enabled=bool(row.get("skill_review_enabled", 0)),
            skill_nudge_interval=int(row.get("skill_nudge_interval", 10) or 10),
            max_active_skills=int(row.get("max_active_skills", 20) or 20),
            max_quarantine_skills=int(row.get("max_quarantine_skills", 10) or 10),
            # NOTE: these two use ``_int`` (which preserves ``0``) because
            # ``0`` is the documented "disable" sentinel — unlike the
            # ``int(row.get(k, d) or d)`` pattern above, which forces 0 → default.
            # The other numeric fields above have ``ge=1`` Pydantic validators
            # so the old pattern is safe for them; migrate them too if any ever
            # gains a meaningful ``0``.
            skill_auto_promote_threshold=_int(row, "skill_auto_promote_threshold", 3),
            skill_quarantine_timeout_days=_int(row, "skill_quarantine_timeout_days", 14),
        )
