"""ContextAssembler — build ranked, budget-aware context for LLM calls."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.identity import IdentityManager
from symbiote.core.ports import KnowledgePort, MemoryPort
from symbiote.memory.working import WorkingMemory

if TYPE_CHECKING:
    from symbiote.core.ports import LLMPort
    from symbiote.environment.manager import EnvironmentManager
    from symbiote.environment.tools import ToolGateway

# ── Models ────────────────────────────────────────────────────────────────────


class AssembledContext(BaseModel):
    symbiote_id: str
    session_id: str
    persona: dict | None = None
    working_memory_snapshot: dict | None = None
    relevant_memories: list[dict] = Field(default_factory=list)
    relevant_knowledge: list[dict] = Field(default_factory=list)
    available_tools: list[dict] = Field(default_factory=list)
    tool_loading: str = "full"
    tool_mode: str = "brief"
    tool_loop: bool = True  # deprecated — derived from tool_mode
    max_tool_iterations: int = 10
    tool_call_timeout: float = 30.0
    loop_timeout: float = 300.0
    # Evolvable text overrides (resolved by ContextAssembler from harness_versions)
    tool_instructions_override: str | None = None
    injection_stagnation_override: str | None = None
    injection_circuit_breaker_override: str | None = None
    context_mode: str = "packed"
    # Long-run mode fields
    planner_prompt: str | None = None
    evaluator_prompt: str | None = None
    evaluator_criteria: list[dict] | None = None
    context_strategy: str = "hybrid"
    max_blocks: int = 20
    extra_context: dict | None = None
    generation_settings: dict | None = None  # from GenerationSettings.to_config_dict()
    user_input: str
    total_tokens_estimate: int = 0


class ContextInspection(BaseModel):
    included_memories: int
    included_knowledge: int
    total_tokens_estimate: int
    budget: int
    within_budget: bool


# ── Budget allocation (fraction of total budget) ─────────────────────────────

_MEMORIES_SHARE = 0.40
_KNOWLEDGE_SHARE = 0.25


# ── Assembler ────────────────────────────────────────────────────────────────


class ContextAssembler:
    """Builds a ranked, budget-aware context payload for LLM calls."""

    def __init__(
        self,
        identity: IdentityManager,
        memory: MemoryPort,
        knowledge: KnowledgePort,
        context_budget: int = 4000,
        tool_gateway: ToolGateway | None = None,
        environment: EnvironmentManager | None = None,
        semantic_llm: LLMPort | None = None,
        harness_versions: object | None = None,
    ) -> None:
        self._identity = identity
        self._memory = memory
        self._knowledge = knowledge
        self._budget = context_budget
        self._tool_gateway = tool_gateway
        self._environment = environment
        self._semantic_llm = semantic_llm
        self._harness_versions = harness_versions

    # ── public API ────────────────────────────────────────────────────────

    def build(
        self,
        session_id: str,
        symbiote_id: str,
        user_input: str,
        working_memory: WorkingMemory | None = None,
        extra_context: dict | None = None,
        tool_tags: list[str] | None = None,
    ) -> AssembledContext:
        """Assemble context within token budget.

        Pipeline:
        1. Load persona from IdentityManager
        2. Get working memory snapshot (if provided)
        3. Get relevant memories from MemoryStore
        4. Get relevant knowledge from KnowledgeService
        5. Rank and trim to fit budget
        6. Return AssembledContext
        """
        # 1. Persona
        symbiote = self._identity.get(symbiote_id)
        if symbiote is None:
            raise EntityNotFoundError("Symbiote", symbiote_id)
        persona: dict | None = symbiote.persona_json

        # 2. Working memory
        wm_snapshot: dict | None = None
        if working_memory is not None:
            wm_snapshot = working_memory.snapshot()

        # 3. Relevant memories
        raw_memories = self._memory.get_relevant(user_input, session_id)
        memories_dicts = [
            {
                "content": m.content,
                "type": m.type,
                "importance": m.importance,
            }
            for m in raw_memories
        ]
        # Sort by importance descending for trimming
        memories_dicts.sort(key=lambda d: d["importance"], reverse=True)

        # 4. Relevant knowledge
        raw_knowledge = self._knowledge.query(symbiote_id, user_input)
        knowledge_dicts = [
            {"name": k.name, "content": k.content or ""}
            for k in raw_knowledge
        ]

        # 5. Tool descriptors — mode-aware loading
        #    Resolve tags: explicit param > EnvironmentConfig > None (all)
        effective_tags = tool_tags
        if not effective_tags and self._environment is not None:
            effective_tags = self._environment.get_tool_tags(symbiote_id) or None

        # Resolve loading mode, tool loop, prompt caching, and context splits
        loading_mode = "full"
        tool_mode = "brief"
        loop_enabled = True
        prompt_caching = False
        memory_share = _MEMORIES_SHARE
        knowledge_share = _KNOWLEDGE_SHARE
        max_tool_iterations = 10
        tool_call_timeout = 30.0
        loop_timeout = 300.0
        context_mode = "packed"
        planner_prompt = None
        evaluator_prompt = None
        evaluator_criteria = None
        context_strategy = "hybrid"
        max_blocks = 20
        if self._environment is not None:
            loading_mode = self._environment.get_tool_loading(symbiote_id)
            tool_mode = self._environment.get_tool_mode(symbiote_id)
            if tool_mode == "auto":
                tool_mode = self._resolve_auto_mode(
                    symbiote_id, user_input,
                )
            loop_enabled = tool_mode != "instant"
            prompt_caching = self._environment.get_prompt_caching(symbiote_id)
            memory_share = self._environment.get_memory_share(symbiote_id)
            knowledge_share = self._environment.get_knowledge_share(symbiote_id)
            max_tool_iterations = self._environment.get_max_tool_iterations(symbiote_id)
            tool_call_timeout = self._environment.get_tool_call_timeout(symbiote_id)
            loop_timeout = self._environment.get_loop_timeout(symbiote_id)
            context_mode = self._environment.get_context_mode(symbiote_id)
            # Long-run fields (via getter — only resolve for long_run mode)
            if tool_mode == "long_run":
                _get = getattr(self._environment, "get_long_run_config", None)
                if _get is not None:
                    try:
                        lr_cfg = _get(symbiote_id)
                        if isinstance(lr_cfg, dict):
                            planner_prompt = lr_cfg.get("planner_prompt")
                            evaluator_prompt = lr_cfg.get("evaluator_prompt")
                            evaluator_criteria = lr_cfg.get("evaluator_criteria")
                            context_strategy = lr_cfg.get("context_strategy", "hybrid")
                            max_blocks = lr_cfg.get("max_blocks", 20)
                    except Exception:
                        pass  # keep defaults

        # On-demand mode: skip memory/knowledge injection (available as tools)
        if context_mode == "on_demand":
            memories_dicts = []
            knowledge_dicts = []

        # Instant mode: precision > recall — cap memory share and prioritize
        # procedural memories (how-to) over declarative (facts/history)
        if tool_mode == "instant":
            memory_share = min(memory_share, 0.25)
            memories_dicts.sort(
                key=lambda d: (
                    0 if d.get("type") == "procedural" else 1,
                    -(d.get("importance", 0)),
                ),
            )

        tool_dicts: list[dict] = []
        if self._tool_gateway is not None:
            tool_dicts = self._build_tool_dicts(
                symbiote_id, user_input, effective_tags, loading_mode
            )

        # 6. Trim to fit budget (using configurable shares)
        persona, wm_snapshot, memories_dicts, knowledge_dicts = self._trim_to_budget(
            persona, wm_snapshot, memories_dicts, knowledge_dicts, user_input,
            memory_share=memory_share, knowledge_share=knowledge_share,
        )

        # 7. Compute total token estimate
        total = self._total_tokens(
            persona, wm_snapshot, memories_dicts, knowledge_dicts, user_input
        )

        # Build generation_settings with prompt_caching if enabled
        gen_settings: dict | None = None
        if prompt_caching:
            gen_settings = {"prompt_caching": True}

        # 8. Resolve evolvable text overrides from harness_versions
        #    Mode-specific lookup: tries e.g. "tool_instructions:instant" first,
        #    falls back to generic "tool_instructions" if not found.
        tool_instr_override = None
        stag_override = None
        cb_override = None
        if self._harness_versions is not None:
            get_active = getattr(self._harness_versions, "get_active", None)
            if get_active is not None:
                tool_instr_override = get_active(symbiote_id, "tool_instructions", tool_mode)
                stag_override = get_active(symbiote_id, "injection_stagnation", tool_mode)
                cb_override = get_active(symbiote_id, "injection_circuit_breaker", tool_mode)

        return AssembledContext(
            symbiote_id=symbiote_id,
            session_id=session_id,
            persona=persona,
            working_memory_snapshot=wm_snapshot,
            relevant_memories=memories_dicts,
            relevant_knowledge=knowledge_dicts,
            available_tools=tool_dicts,
            tool_loading=loading_mode,
            tool_mode=tool_mode,
            tool_loop=loop_enabled,
            max_tool_iterations=max_tool_iterations,
            tool_call_timeout=tool_call_timeout,
            loop_timeout=loop_timeout,
            tool_instructions_override=tool_instr_override,
            injection_stagnation_override=stag_override,
            injection_circuit_breaker_override=cb_override,
            context_mode=context_mode,
            planner_prompt=planner_prompt,
            evaluator_prompt=evaluator_prompt,
            evaluator_criteria=evaluator_criteria,
            context_strategy=context_strategy,
            max_blocks=max_blocks,
            extra_context=extra_context,
            user_input=user_input,
            total_tokens_estimate=total,
            generation_settings=gen_settings,
        )

    def inspect(self, context: AssembledContext) -> ContextInspection:
        """Return a breakdown of what the assembled context contains."""
        return ContextInspection(
            included_memories=len(context.relevant_memories),
            included_knowledge=len(context.relevant_knowledge),
            total_tokens_estimate=context.total_tokens_estimate,
            budget=self._budget,
            within_budget=context.total_tokens_estimate <= self._budget,
        )

    # ── internal helpers ──────────────────────────────────────────────────

    def _resolve_auto_mode(
        self,
        symbiote_id: str,
        user_input: str,
    ) -> str:
        """Resolve ``auto`` tool_mode to a concrete mode based on heuristics.

        Rules (evaluated in order):
        1. If long-run config exists (planner_prompt set) → ``long_run``
        2. If no tools are enabled for the symbiote → ``instant``
        3. If user input is short (≤80 chars, single line) → ``brief``
        4. Default → ``brief``
        """
        # Check if long-run planner is configured
        if self._environment is not None:
            _get = getattr(self._environment, "get_long_run_config", None)
            if _get is not None:
                try:
                    lr_cfg = _get(symbiote_id)
                    if isinstance(lr_cfg, dict) and lr_cfg.get("planner_prompt"):
                        return "long_run"
                except Exception:
                    pass

            # No tools enabled → instant (no loop needed)
            tools = self._environment.list_tools(symbiote_id)
            if not tools:
                return "instant"

        # Default
        return "brief"

    def _build_tool_dicts(
        self,
        symbiote_id: str,
        user_input: str,
        effective_tags: list[str] | None,
        loading_mode: str,
    ) -> list[dict]:
        """Build tool dicts for the assembled context based on the loading mode.

        - ``full``: full schema (tool_id, name, description, parameters)
        - ``index``: compact catalog (tool_id, name, description only) +
          ensures ``get_tool_schema`` meta-tool is registered
        - ``semantic``: use cheap LLM to pre-filter tags, then full schema
        """
        assert self._tool_gateway is not None

        resolved_tags = effective_tags

        if loading_mode == "semantic":
            resolved_tags = self._resolve_semantic_tags(
                user_input, effective_tags
            )

        descriptors = self._tool_gateway.get_descriptors(tags=resolved_tags)

        if loading_mode == "index":
            # Register meta-tool so the LLM can fetch full schemas on demand
            self._tool_gateway.register_index_tool()
            # Include get_tool_schema descriptor in the list (with full params)
            index_desc = self._tool_gateway.get_descriptor("get_tool_schema")

            tool_dicts = [
                {
                    "tool_id": d.tool_id,
                    "name": d.name,
                    "description": d.description,
                }
                for d in descriptors
                if d.tool_id != "get_tool_schema"
            ]
            # Add get_tool_schema with full parameters so the LLM knows how to call it
            if index_desc is not None:
                tool_dicts.insert(0, {
                    "tool_id": index_desc.tool_id,
                    "name": index_desc.name,
                    "description": index_desc.description,
                    "parameters": index_desc.parameters,
                })
            return tool_dicts

        # full or semantic: return full schemas
        return [
            {
                "tool_id": d.tool_id,
                "name": d.name,
                "description": d.description,
                "parameters": d.parameters,
            }
            for d in descriptors
        ]

    def _resolve_semantic_tags(
        self,
        user_input: str,
        effective_tags: list[str] | None,
    ) -> list[str] | None:
        """Use cheap LLM to select relevant tags. Falls back to effective_tags."""
        import logging

        if self._semantic_llm is None:
            logging.getLogger(__name__).warning(
                "semantic tool_loading configured but no semantic_llm set; "
                "falling back to full mode"
            )
            return effective_tags

        assert self._tool_gateway is not None
        from symbiote.environment.resolver import ToolTagResolver

        all_tags = self._tool_gateway.get_available_tags()
        # Intersect with configured tags if set
        if effective_tags:
            candidate_tags = [t for t in all_tags if t in set(effective_tags)]
        else:
            candidate_tags = all_tags

        if not candidate_tags:
            return effective_tags

        resolver = ToolTagResolver(self._semantic_llm)
        return resolver.resolve(user_input, candidate_tags) or effective_tags

    def _estimate_tokens(self, text: str) -> int:
        """Rough chars-to-tokens heuristic: len(text) // 4."""
        return len(text) // 4

    def _dict_tokens(self, d: dict | None) -> int:
        if d is None:
            return 0
        return self._estimate_tokens(json.dumps(d, default=str))

    def _total_tokens(
        self,
        persona: dict | None,
        wm_snapshot: dict | None,
        memories: list[dict],
        knowledge: list[dict],
        user_input: str,
    ) -> int:
        total = self._dict_tokens(persona)
        total += self._dict_tokens(wm_snapshot)
        for m in memories:
            total += self._estimate_tokens(json.dumps(m, default=str))
        for k in knowledge:
            total += self._estimate_tokens(json.dumps(k, default=str))
        total += self._estimate_tokens(user_input)
        return total

    def _trim_to_budget(
        self,
        persona: dict | None,
        wm_snapshot: dict | None,
        memories: list[dict],
        knowledge: list[dict],
        user_input: str,
        *,
        memory_share: float = _MEMORIES_SHARE,
        knowledge_share: float = _KNOWLEDGE_SHARE,
    ) -> tuple[dict | None, dict | None, list[dict], list[dict]]:
        """Trim memories and knowledge (least important first) to fit budget."""
        total = self._total_tokens(persona, wm_snapshot, memories, knowledge, user_input)
        if total <= self._budget:
            return persona, wm_snapshot, memories, knowledge

        # Fixed cost: persona + working memory + user_input
        fixed = self._dict_tokens(persona) + self._dict_tokens(wm_snapshot) + self._estimate_tokens(user_input)

        available = self._budget - fixed
        if available <= 0:
            # Not even fixed content fits; return with empty dynamic sections
            return persona, wm_snapshot, [], []

        # Split available between memories and knowledge proportionally
        total_share = memory_share + knowledge_share
        if total_share <= 0:
            total_share = 0.65  # fallback
        mem_budget = int(available * memory_share / total_share)
        know_budget = available - mem_budget

        # Trim memories (already sorted by importance desc)
        trimmed_memories: list[dict] = []
        mem_used = 0
        for m in memories:
            cost = self._estimate_tokens(json.dumps(m, default=str))
            if mem_used + cost <= mem_budget:
                trimmed_memories.append(m)
                mem_used += cost
            # else skip (least important since sorted desc)

        # Trim knowledge
        trimmed_knowledge: list[dict] = []
        know_used = 0
        for k in knowledge:
            cost = self._estimate_tokens(json.dumps(k, default=str))
            if know_used + cost <= know_budget:
                trimmed_knowledge.append(k)
                know_used += cost

        return persona, wm_snapshot, trimmed_memories, trimmed_knowledge
