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
    ) -> None:
        self._identity = identity
        self._memory = memory
        self._knowledge = knowledge
        self._budget = context_budget
        self._tool_gateway = tool_gateway

    # ── public API ────────────────────────────────────────────────────────

    def build(
        self,
        session_id: str,
        symbiote_id: str,
        user_input: str,
        working_memory: WorkingMemory | None = None,
        extra_context: dict | None = None,
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

        # 5. Tool descriptors
        tool_dicts: list[dict] = []
        if self._tool_gateway is not None:
            tool_dicts = [
                {
                    "tool_id": d.tool_id,
                    "name": d.name,
                    "description": d.description,
                    "parameters": d.parameters,
                }
                for d in self._tool_gateway.get_descriptors()
            ]

        # 6. Trim to fit budget
        persona, wm_snapshot, memories_dicts, knowledge_dicts = self._trim_to_budget(
            persona, wm_snapshot, memories_dicts, knowledge_dicts, user_input
        )

        # 7. Compute total token estimate
        total = self._total_tokens(
            persona, wm_snapshot, memories_dicts, knowledge_dicts, user_input
        )

        return AssembledContext(
            symbiote_id=symbiote_id,
            session_id=session_id,
            persona=persona,
            working_memory_snapshot=wm_snapshot,
            relevant_memories=memories_dicts,
            relevant_knowledge=knowledge_dicts,
            available_tools=tool_dicts,
            extra_context=extra_context,
            user_input=user_input,
            total_tokens_estimate=total,
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
        mem_budget = int(available * _MEMORIES_SHARE / (_MEMORIES_SHARE + _KNOWLEDGE_SHARE))
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
