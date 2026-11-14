"""EnvironmentManager — configure and query environment configs for symbiotes."""

from __future__ import annotations

import json
import threading
from typing import Any

from symbiote.core.exceptions import StorageIntegrityError, ValidationError
from symbiote.core.models import EnvironmentConfig
from symbiote.core.ports import StoragePort

# Serialises read-modify-write of environment configs in this process (one
# lock for every manager: the API builds a manager per request). Across
# processes the unique index on (symbiote_id, workspace_id) is the guard.
_CONFIG_LOCK = threading.RLock()


class EnvironmentManager:
    """Manages environment configuration CRUD with workspace-level overrides."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage
        # Back-reference set by SymbioteKernel after construction so configure()
        # can validate evolver_llm requirements without circular imports.
        self._kernel: Any = None

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
        memory_enabled: bool | None = None,
        # Skill self-improvement fields (Sprint 4)
        skill_review_enabled: bool | None = None,
        skill_injection_enabled: bool | None = None,
        skill_injection_mode: str | None = None,
        skill_review_strict: bool | None = None,
        skill_review_extra_criteria: str | None = None,
        skill_nudge_interval: int | None = None,
        max_active_skills: int | None = None,
        max_quarantine_skills: int | None = None,
        # Sprint 5 — lifecycle automation
        skill_auto_promote_threshold: int | None = None,
        skill_quarantine_timeout_days: int | None = None,
    ) -> EnvironmentConfig:
        """Create or update an environment config for a symbiote+workspace combo."""
        # Only the fields passed (not None) change; captured before any other
        # local exists. 0 / 0.0 / False are values, not "unset".
        updates = {
            k: v for k, v in locals().items()
            if k not in ("self", "symbiote_id", "workspace_id") and v is not None
        }
        # None means "unchanged", so an empty string is how a caller clears
        # the long-run prompts (an empty prompt has no other meaning; a stored
        # "" would switch the evaluator on with the default prompt).
        for key in ("planner_prompt", "evaluator_prompt"):
            if updates.get(key) == "":
                updates[key] = None
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

        # Host-policy ceiling on max_tool_iterations. The per-symbiote value is
        # configurable, but the embedding application owns the upper bound via
        # KernelConfig.max_tool_iterations_ceiling (default 50). Enforced here —
        # the layer that knows the host policy — rather than as a static Pydantic
        # bound. Fail-fast with a clear error instead of silently no-op'ing.
        if max_tool_iterations is not None:
            kernel = getattr(self, "_kernel", None)
            ceiling = getattr(
                getattr(kernel, "_config", None),
                "max_tool_iterations_ceiling",
                50,
            )
            if max_tool_iterations > ceiling:
                raise ValidationError(
                    f"max_tool_iterations={max_tool_iterations} exceeds the host "
                    f"ceiling ({ceiling}). Raise KernelConfig."
                    f"max_tool_iterations_ceiling to allow higher values. "
                    f"loop_timeout (≤3600s) remains the independent wall-clock guard."
                )

        # Auto-derive tool_loop from tool_mode when tool_mode is set explicitly
        if tool_mode is not None and tool_loop is None:
            updates["tool_loop"] = tool_mode != "instant"
        # Backward compat: if only tool_loop=False is set, derive tool_mode
        if tool_loop is False and tool_mode is None:
            updates["tool_mode"] = "instant"

        for attempt in range(2):
            with _CONFIG_LOCK:
                try:
                    # Read, merge and write as one unit.
                    with self._storage.transaction():
                        existing = self._fetch_exact(symbiote_id, workspace_id)
                        # A workspace config starts from the symbiote's own
                        # config, so setting one field there does not reset
                        # the others (tools!).
                        base = existing
                        if base is None and workspace_id is not None:
                            base = self._fetch_exact(symbiote_id, None)
                        data = (
                            base.model_dump(exclude={"id", "symbiote_id", "workspace_id"})
                            if base is not None
                            else {}
                        )
                        data.update(updates)
                        if existing is not None:
                            data["id"] = existing.id
                        cfg = EnvironmentConfig(
                            symbiote_id=symbiote_id, workspace_id=workspace_id, **data
                        )
                        self._write(cfg, insert=existing is None)
                    return cfg
                except StorageIntegrityError:
                    # Another process created this config first: merge onto it.
                    if existing is not None or attempt:
                        raise
        raise AssertionError("unreachable")

    def add_tools(
        self, symbiote_id: str, tool_ids: list[str], workspace_id: str | None = None
    ) -> list[str]:
        """Authorize *tool_ids* in one step (no lost update); returns the list."""
        with _CONFIG_LOCK:
            current = self.list_tools(symbiote_id, workspace_id)
            merged = current + [t for t in dict.fromkeys(tool_ids) if t not in current]
            self.configure(symbiote_id=symbiote_id, workspace_id=workspace_id, tools=merged)
            return merged

    def remove_tools(
        self, symbiote_id: str, tool_ids: list[str], workspace_id: str | None = None
    ) -> list[str]:
        """Revoke *tool_ids* in one step (no lost update); returns the list."""
        with _CONFIG_LOCK:
            drop = set(tool_ids)
            kept = [t for t in self.list_tools(symbiote_id, workspace_id) if t not in drop]
            self.configure(symbiote_id=symbiote_id, workspace_id=workspace_id, tools=kept)
            return kept

    # Model fields stored as ``<name>_json`` columns.
    _JSON_FIELDS = frozenset(
        {"tools", "services", "humans", "policies", "resources", "tool_tags", "evaluator_criteria"}
    )

    def _write(self, cfg: EnvironmentConfig, *, insert: bool) -> None:
        row: dict[str, object] = {}
        for name, value in cfg.model_dump().items():
            if name in self._JSON_FIELDS:
                row[f"{name}_json"] = None if value is None else json.dumps(value)
            elif isinstance(value, bool):
                row[name] = int(value)
            else:
                row[name] = value
        if insert:
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            self._storage.execute(
                f"INSERT INTO environment_configs ({cols}) VALUES ({marks})",  # noqa: S608
                tuple(row.values()),
            )
        else:
            row.pop("id")
            sets = ", ".join(f"{col} = ?" for col in row)
            self._storage.execute(
                f"UPDATE environment_configs SET {sets} WHERE id = ?",  # noqa: S608
                (*row.values(), cfg.id),
            )

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

    def get_skill_injection_enabled(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> bool:
        """Return skill_injection_enabled from config, or False if no config.

        When True, ContextAssembler injects active skills into the system
        prompt. Default False preserves the legacy no-injection behaviour.
        """
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return False
        return cfg.skill_injection_enabled

    def get_skill_injection_mode(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> str:
        """Return skill_injection_mode ('full' | 'index'), or 'full' default."""
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return "full"
        return cfg.skill_injection_mode

    def get_skill_review_criteria(
        self, symbiote_id: str, workspace_id: str | None = None
    ) -> dict:
        """Return host-tunable skill-review criteria for the evolver prompt.

        ``{'strict': bool, 'extra': str | None}``. Both no-op by default; the
        baseline review prompt already carries the core anti-patterns.
        """
        cfg = self.get_config(symbiote_id, workspace_id)
        if cfg is None:
            return {"strict": False, "extra": None}
        return {
            "strict": cfg.skill_review_strict,
            "extra": cfg.skill_review_extra_criteria,
        }

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

    def get_memory_enabled(self, symbiote_id: str) -> bool:
        """False only when the symbiote turned long-term memory off."""
        cfg = self.get_config(symbiote_id)
        return cfg.memory_enabled if cfg is not None else True

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
    def _float_or_default(row: dict, key: str, default: float) -> float:
        """``float(row[key])`` if non-None, else ``default`` (keeps ``0.0``)."""
        val = row.get(key)
        return float(val) if val is not None else default

    @staticmethod
    def _row_to_config(row: dict) -> EnvironmentConfig:
        _int = EnvironmentManager._int_or_default
        _float = EnvironmentManager._float_or_default
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
            memory_share=_float(row, "memory_share", 0.40),
            knowledge_share=_float(row, "knowledge_share", 0.25),
            max_tool_iterations=_int(row, "max_tool_iterations", 10),
            tool_call_timeout=_float(row, "tool_call_timeout", 30.0),
            loop_timeout=_float(row, "loop_timeout", 300.0),
            context_mode=row.get("context_mode") or "packed",
            planner_prompt=row.get("planner_prompt"),
            evaluator_prompt=row.get("evaluator_prompt"),
            evaluator_criteria=json.loads(row["evaluator_criteria_json"]) if row.get("evaluator_criteria_json") else None,
            context_strategy=row.get("context_strategy") or "hybrid",
            max_blocks=_int(row, "max_blocks", 20),
            dream_mode=row.get("dream_mode") or "off",
            dream_max_llm_calls=_int(row, "dream_max_llm_calls", 10),
            dream_min_sessions=_int(row, "dream_min_sessions", 5),
            reflection_mode=row.get("reflection_mode") or "keyword",
            reflection_max_tokens=_int(row, "reflection_max_tokens", 4000),
            memory_enabled=bool(_int(row, "memory_enabled", 1)),
            skill_review_enabled=bool(row.get("skill_review_enabled", 0)),
            skill_injection_enabled=bool(row.get("skill_injection_enabled", 0)),
            skill_injection_mode=row.get("skill_injection_mode") or "full",
            skill_review_strict=bool(row.get("skill_review_strict", 0)),
            skill_review_extra_criteria=row.get("skill_review_extra_criteria"),
            skill_nudge_interval=_int(row, "skill_nudge_interval", 10),
            max_active_skills=_int(row, "max_active_skills", 20),
            max_quarantine_skills=_int(row, "max_quarantine_skills", 10),
            skill_auto_promote_threshold=_int(row, "skill_auto_promote_threshold", 3),
            skill_quarantine_timeout_days=_int(row, "skill_quarantine_timeout_days", 14),
        )
