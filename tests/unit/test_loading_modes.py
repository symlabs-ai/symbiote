"""Comparative tests for tool loading modes — full vs index vs semantic.

Uses a realistic tool set modeled on the YouNews OpenAPI (241 operations,
18 tags) to measure prompt size, tool visibility, and filtering accuracy
across the three modes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext, ContextAssembler
from symbiote.core.identity import IdentityManager
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.runners.chat import ChatRunner

# ── Realistic tool set (YouNews-like distribution) ───────────────────────

# tag → (count, description_template)
_TAG_DISTRIBUTION: dict[str, tuple[int, str]] = {
    "Journals": (25, "Journal operation #{i}: manage journals"),
    "Config": (22, "Config operation #{i}: system configuration"),
    "Sources": (21, "Source operation #{i}: manage news sources"),
    "Newsletter": (18, "Newsletter operation #{i}: email newsletters"),
    "Plugins": (15, "Plugin operation #{i}: manage plugins"),
    "Compose": (14, "Compose operation #{i}: content composition"),
    "Public": (14, "Public operation #{i}: public API access"),
    "Capture": (12, "Capture operation #{i}: content capture"),
    "View": (12, "View operation #{i}: content views"),
    "Analytics": (12, "Analytics operation #{i}: analytics data"),
    "Admin": (11, "Admin operation #{i}: administration"),
    "Authentication": (7, "Auth operation #{i}: authentication"),
    "Items": (7, "Item operation #{i}: manage news items"),
    "Push": (7, "Push operation #{i}: push notifications"),
    "Clark": (8, "Clark operation #{i}: Clark assistant actions"),
    "Search": (1, "Search items across all journals"),
}

# Realistic parameter schema for tools
_PARAMS_TEMPLATE = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "description": "Resource ID"},
        "status": {"type": "string", "description": "Filter by status"},
        "limit": {"type": "integer", "description": "Max results"},
        "offset": {"type": "integer", "description": "Pagination offset"},
    },
    "required": ["id"],
}


def _make_tools() -> list[ToolDescriptor]:
    """Generate a YouNews-like tool set with realistic tag distribution."""
    tools: list[ToolDescriptor] = []
    for tag, (count, desc_tpl) in _TAG_DISTRIBUTION.items():
        for i in range(count):
            tools.append(ToolDescriptor(
                tool_id=f"{tag.lower()}_{i:03d}",
                name=f"{tag} Tool {i}",
                description=desc_tpl.format(i=i),
                parameters=_PARAMS_TEMPLATE,
                tags=[tag],
                handler_type="http",
            ))
    return tools


# ── Mock LLM for semantic mode ──────────────────────────────────────────

# Predefined semantic resolution results per query
_SEMANTIC_RESPONSES: dict[str, list[str]] = {
    "publique a matéria sobre o incêndio": ["Items", "Compose", "Journals"],
    "configure as fontes de notícia do RSS": ["Config", "Sources"],
    "mostre as estatísticas de acesso do mês": ["Analytics"],
    "crie um rascunho para o editorial de amanhã": ["Compose"],
    "altere as permissões do usuário admin": ["Admin"],
    "envie a newsletter de hoje": ["Newsletter"],
    "busque matérias sobre eleições": ["Search", "Items"],
    "configure o plugin de redes sociais": ["Plugins", "Config"],
    "veja as notificações push pendentes": ["Push"],
    "como faço para capturar uma notícia?": ["Capture", "Clark"],
}


class _MockSemanticLLM:
    """Mock LLM that returns predefined tag resolutions."""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_messages: list[dict] = []

    def complete(self, messages: list[dict], **kwargs) -> str:
        self.call_count += 1
        self.last_messages = messages
        user_msg = messages[-1]["content"]
        tags = _SEMANTIC_RESPONSES.get(user_msg, ["Items", "Clark"])
        return json.dumps(tags)


class _MockLLMPort:
    """Minimal mock LLM for ChatRunner (just returns text)."""

    def complete(self, messages: list[dict], **kwargs) -> str:
        return "I'll help you with that."


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "loading_modes_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="Clark", role="assistant", persona={"role": "newsroom assistant"})
    return sym.id


@pytest.fixture()
def session_id(adapter: SQLiteAdapter, symbiote_id: str) -> str:
    from symbiote.core.session import SessionManager
    mgr = SessionManager(storage=adapter)
    return mgr.start(symbiote_id=symbiote_id, goal="test").id


@pytest.fixture()
def identity(adapter: SQLiteAdapter) -> IdentityManager:
    return IdentityManager(storage=adapter)


@pytest.fixture()
def memory(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(storage=adapter)


@pytest.fixture()
def knowledge(adapter: SQLiteAdapter) -> KnowledgeService:
    return KnowledgeService(storage=adapter)


@pytest.fixture()
def env(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def gw(adapter: SQLiteAdapter, env: EnvironmentManager) -> ToolGateway:
    gate = PolicyGate(env_manager=env, storage=adapter)
    gateway = ToolGateway(policy_gate=gate)
    for tool in _make_tools():
        gateway.register_descriptor(tool, lambda p: None)
    return gateway


@pytest.fixture()
def all_tags() -> list[str]:
    return sorted(_TAG_DISTRIBUTION.keys())


def _build_context(
    identity, memory, knowledge, gw, env, symbiote_id, session_id,
    user_input, semantic_llm=None,
) -> AssembledContext:
    """Helper: build context with the current env config."""
    assembler = ContextAssembler(
        identity=identity, memory=memory, knowledge=knowledge,
        context_budget=100_000, tool_gateway=gw, environment=env,
        semantic_llm=semantic_llm,
    )
    return assembler.build(
        session_id=session_id, symbiote_id=symbiote_id,
        user_input=user_input,
    )


def _estimate_prompt_tokens(context: AssembledContext) -> int:
    """Estimate tokens for the tool section of the prompt."""
    total_chars = 0
    for tool in context.available_tools:
        total_chars += len(json.dumps(tool, default=str))
    return total_chars // 4  # rough chars-to-tokens heuristic


def _build_system_prompt(context: AssembledContext) -> str:
    """Build the actual system prompt using ChatRunner logic."""
    runner = ChatRunner(llm=_MockLLMPort(), tool_gateway=None)
    return runner._build_system(context)


# ══════════════════════════════════════════════════════════════════════════
# 1. TOOL COUNT COMPARISON
# ══════════════════════════════════════════════════════════════════════════


class TestToolCount:
    """Compare how many tools each mode puts in the context."""

    def test_full_mode_includes_all_tools(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(symbiote_id=symbiote_id, tool_loading="full")
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "publique a matéria",
        )
        total_registered = len(_make_tools()) + 3  # +3 builtins (fs_read, fs_write, fs_list)
        assert len(ctx.available_tools) == total_registered

    def test_full_mode_with_tags_reduces_count(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(
            symbiote_id=symbiote_id,
            tool_tags=["Items", "Compose", "Clark"],
            tool_loading="full",
        )
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "publique a matéria",
        )
        expected = 7 + 14 + 8  # Items + Compose + Clark
        assert len(ctx.available_tools) == expected

    def test_index_mode_same_count_plus_meta_tool(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(symbiote_id=symbiote_id, tool_loading="index")
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "publique a matéria",
        )
        # All tools + builtins + get_tool_schema meta-tool
        total_registered = len(_make_tools()) + 3 + 1
        assert len(ctx.available_tools) == total_registered

    def test_semantic_mode_reduces_to_relevant_tags(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(symbiote_id=symbiote_id, tool_loading="semantic")
        semantic_llm = _MockSemanticLLM()
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "publique a matéria sobre o incêndio",
            semantic_llm=semantic_llm,
        )
        # LLM resolved to ["Items", "Compose", "Journals"] → 7+14+25 = 46
        tool_ids = {t["tool_id"] for t in ctx.available_tools}
        assert all(
            tid.startswith(("items_", "compose_", "journals_"))
            for tid in tool_ids
        )
        assert len(ctx.available_tools) == 7 + 14 + 25


# ══════════════════════════════════════════════════════════════════════════
# 2. PROMPT SIZE COMPARISON
# ══════════════════════════════════════════════════════════════════════════


class TestPromptSize:
    """Compare token estimates for the tool section across modes."""

    def test_index_mode_drastically_smaller_than_full(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        # Full mode
        env.configure(symbiote_id=symbiote_id, tool_loading="full")
        ctx_full = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "hello",
        )

        # Index mode
        env.configure(symbiote_id=symbiote_id, tool_loading="index")
        ctx_index = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "hello",
        )

        full_tokens = _estimate_prompt_tokens(ctx_full)
        index_tokens = _estimate_prompt_tokens(ctx_index)

        # Index should be at least 3x smaller (no parameters in entries)
        assert index_tokens < full_tokens / 3
        assert full_tokens > 0
        assert index_tokens > 0

    def test_semantic_mode_smaller_than_full_for_focused_query(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        # Full mode — all tools
        env.configure(symbiote_id=symbiote_id, tool_loading="full")
        ctx_full = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "altere as permissões do usuário admin",
        )

        # Semantic mode — LLM resolves to ["Admin"] only
        env.configure(symbiote_id=symbiote_id, tool_loading="semantic")
        ctx_semantic = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "altere as permissões do usuário admin",
            semantic_llm=_MockSemanticLLM(),
        )

        full_tokens = _estimate_prompt_tokens(ctx_full)
        semantic_tokens = _estimate_prompt_tokens(ctx_semantic)

        # Admin = 11 tools vs all 206+ tools
        assert semantic_tokens < full_tokens / 5

    def test_system_prompt_size_comparison(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        """Compare actual system prompt character count across all three modes."""
        query = "envie a newsletter de hoje"

        # Full
        env.configure(symbiote_id=symbiote_id, tool_loading="full")
        ctx_full = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id, query,
        )
        prompt_full = _build_system_prompt(ctx_full)

        # Index
        env.configure(symbiote_id=symbiote_id, tool_loading="index")
        ctx_index = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id, query,
        )
        prompt_index = _build_system_prompt(ctx_index)

        # Semantic → ["Newsletter"]
        env.configure(symbiote_id=symbiote_id, tool_loading="semantic")
        ctx_semantic = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id, query,
            semantic_llm=_MockSemanticLLM(),
        )
        prompt_semantic = _build_system_prompt(ctx_semantic)

        # Index should be the smallest system prompt
        assert len(prompt_index) < len(prompt_full)
        # Semantic (18 newsletter tools) should be much smaller than full (206+)
        assert len(prompt_semantic) < len(prompt_full) / 5


# ══════════════════════════════════════════════════════════════════════════
# 3. TOOL CONTENT COMPARISON — what's in available_tools
# ══════════════════════════════════════════════════════════════════════════


class TestToolContent:
    """Verify structural differences in available_tools across modes."""

    def test_full_mode_includes_parameters(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(
            symbiote_id=symbiote_id,
            tool_tags=["Items"],
            tool_loading="full",
        )
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "hello",
        )
        for tool in ctx.available_tools:
            assert "parameters" in tool
            assert "properties" in tool["parameters"]

    def test_index_mode_omits_parameters_except_meta_tool(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(
            symbiote_id=symbiote_id,
            tool_tags=["Items"],
            tool_loading="index",
        )
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "hello",
        )
        for tool in ctx.available_tools:
            if tool["tool_id"] == "get_tool_schema":
                assert "parameters" in tool
                assert tool["parameters"]["required"] == ["tool_id"]
            else:
                assert "parameters" not in tool

    def test_index_mode_get_tool_schema_is_first(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(symbiote_id=symbiote_id, tool_loading="index")
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "hello",
        )
        assert ctx.available_tools[0]["tool_id"] == "get_tool_schema"

    def test_semantic_mode_includes_full_parameters(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(symbiote_id=symbiote_id, tool_loading="semantic")
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "altere as permissões do usuário admin",
            semantic_llm=_MockSemanticLLM(),
        )
        # Semantic returns full schemas, not index
        for tool in ctx.available_tools:
            assert "parameters" in tool

    def test_tool_loading_field_propagated(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        for mode in ("full", "index", "semantic"):
            env.configure(symbiote_id=symbiote_id, tool_loading=mode)
            ctx = _build_context(
                identity, memory, knowledge, gw, env, symbiote_id, session_id,
                "hello",
                semantic_llm=_MockSemanticLLM() if mode == "semantic" else None,
            )
            assert ctx.tool_loading == mode


# ══════════════════════════════════════════════════════════════════════════
# 4. SEMANTIC ACCURACY — does the resolver pick the right tags?
# ══════════════════════════════════════════════════════════════════════════


class TestSemanticAccuracy:
    """Verify semantic mode routes user queries to correct tool groups."""

    @pytest.mark.parametrize("query,expected_prefixes", [
        ("publique a matéria sobre o incêndio", {"items_", "compose_", "journals_"}),
        ("configure as fontes de notícia do RSS", {"config_", "sources_"}),
        ("mostre as estatísticas de acesso do mês", {"analytics_"}),
        ("crie um rascunho para o editorial de amanhã", {"compose_"}),
        ("altere as permissões do usuário admin", {"admin_"}),
        ("envie a newsletter de hoje", {"newsletter_"}),
        ("busque matérias sobre eleições", {"search_", "items_"}),
        ("configure o plugin de redes sociais", {"plugins_", "config_"}),
        ("veja as notificações push pendentes", {"push_"}),
        ("como faço para capturar uma notícia?", {"capture_", "clark_"}),
    ])
    def test_semantic_routes_to_correct_tags(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
        query: str, expected_prefixes: set[str],
    ) -> None:
        env.configure(symbiote_id=symbiote_id, tool_loading="semantic")
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            query, semantic_llm=_MockSemanticLLM(),
        )
        tool_ids = {t["tool_id"] for t in ctx.available_tools}
        actual_prefixes = {tid.rsplit("_", 1)[0] + "_" for tid in tool_ids}
        assert actual_prefixes == expected_prefixes

    def test_semantic_excludes_irrelevant_tags(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(symbiote_id=symbiote_id, tool_loading="semantic")
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "altere as permissões do usuário admin",
            semantic_llm=_MockSemanticLLM(),
        )
        tool_ids = {t["tool_id"] for t in ctx.available_tools}
        # Should NOT include unrelated tags
        assert not any(tid.startswith("newsletter_") for tid in tool_ids)
        assert not any(tid.startswith("compose_") for tid in tool_ids)
        assert not any(tid.startswith("journals_") for tid in tool_ids)
        assert not any(tid.startswith("push_") for tid in tool_ids)


# ══════════════════════════════════════════════════════════════════════════
# 5. SEMANTIC WITH TAG RESTRICTION — tool_tags + semantic combined
# ══════════════════════════════════════════════════════════════════════════


class TestSemanticWithTagRestriction:
    """Semantic mode should respect tool_tags as an upper bound."""

    def test_semantic_intersects_with_configured_tags(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        # Admin has access to Items, Compose, Admin, Clark only
        env.configure(
            symbiote_id=symbiote_id,
            tool_tags=["Items", "Compose", "Admin", "Clark"],
            tool_loading="semantic",
        )
        # Query would resolve to ["Items", "Compose", "Journals"]
        # But Journals is not in tool_tags, so it's excluded from candidates
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "publique a matéria sobre o incêndio",
            semantic_llm=_MockSemanticLLM(),
        )
        tool_ids = {t["tool_id"] for t in ctx.available_tools}
        # Journals should be excluded (not in tool_tags)
        assert not any(tid.startswith("journals_") for tid in tool_ids)
        # Items and Compose should be present
        assert any(tid.startswith("items_") for tid in tool_ids)
        assert any(tid.startswith("compose_") for tid in tool_ids)


# ══════════════════════════════════════════════════════════════════════════
# 6. FALLBACK BEHAVIOR
# ══════════════════════════════════════════════════════════════════════════


class _FailingLLM:
    def complete(self, messages, **kwargs):
        raise RuntimeError("LLM unavailable")


class TestFallback:
    """Verify graceful degradation when semantic LLM fails or is missing."""

    def test_semantic_without_llm_falls_back_to_all(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(symbiote_id=symbiote_id, tool_loading="semantic")
        # No semantic_llm passed
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "hello",
        )
        total_registered = len(_make_tools()) + 3  # +3 builtins
        assert len(ctx.available_tools) == total_registered

    def test_semantic_with_failing_llm_falls_back(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(symbiote_id=symbiote_id, tool_loading="semantic")
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "hello",
            semantic_llm=_FailingLLM(),
        )
        # Should fall back gracefully — tools still present
        assert len(ctx.available_tools) > 0

    def test_index_mode_meta_tool_handles_unknown_tool(
        self, gw, env, symbiote_id,
    ) -> None:
        gw.register_index_tool()
        env.configure(symbiote_id=symbiote_id, tools=["get_tool_schema"])
        result = gw.execute(
            symbiote_id=symbiote_id, session_id=None,
            tool_id="get_tool_schema",
            params={"tool_id": "nonexistent_tool"},
        )
        assert result.success is True
        assert "error" in result.output


# ══════════════════════════════════════════════════════════════════════════
# 7. SYSTEM PROMPT RENDERING
# ══════════════════════════════════════════════════════════════════════════


class TestSystemPromptRendering:
    """Verify the ChatRunner renders each mode differently."""

    def test_full_mode_renders_with_parameters(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(
            symbiote_id=symbiote_id,
            tool_tags=["Search"],
            tool_loading="full",
        )
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "hello",
        )
        prompt = _build_system_prompt(ctx)
        assert "## Available Tools" in prompt
        assert "### search_000" in prompt
        assert "Parameters: ```json" in prompt

    def test_index_mode_renders_compact_list(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(
            symbiote_id=symbiote_id,
            tool_tags=["Search"],
            tool_loading="index",
        )
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "hello",
        )
        prompt = _build_system_prompt(ctx)
        assert "## Available Tools (Index)" in prompt
        assert "get_tool_schema" in prompt
        # Index entries use compact format
        assert "- **search_000**" in prompt
        # get_tool_schema has full params
        assert "### get_tool_schema" in prompt

    def test_semantic_mode_renders_same_as_full(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        env.configure(
            symbiote_id=symbiote_id,
            tool_tags=["Search"],
            tool_loading="semantic",
        )
        # Mock resolves "hello" to default ["Items", "Clark"]
        # but tool_tags restricts to ["Search"] only
        # The semantic resolver sees only ["Search"] as candidates
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "busque matérias sobre eleições",
            semantic_llm=_MockSemanticLLM(),
        )
        prompt = _build_system_prompt(ctx)
        # Semantic renders like full — with parameters
        assert "## Available Tools" in prompt
        assert "Parameters: ```json" in prompt

    def test_agentic_instructions_present_in_all_modes(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        """All modes must include agentic behavior instructions."""
        for mode in ("full", "index", "semantic"):
            env.configure(
                symbiote_id=symbiote_id,
                tool_tags=["Search"],
                tool_loading=mode,
            )
            kwargs = {}
            if mode == "semantic":
                kwargs["semantic_llm"] = _MockSemanticLLM()
            ctx = _build_context(
                identity, memory, knowledge, gw, env, symbiote_id, session_id,
                "buscar matérias", **kwargs,
            )
            prompt = _build_system_prompt(ctx)
            assert "autonomous agent" in prompt, f"mode={mode} missing agentic instructions"
            assert "EXECUTES" in prompt, f"mode={mode} missing EXECUTE instruction"
            assert "invent or assume" in prompt, f"mode={mode} missing no-invent warning"

    def test_persona_rendered_as_text_not_json(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
    ) -> None:
        """Persona should be rendered as natural language, not raw JSON."""
        ctx = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id,
            "hello",
        )
        prompt = _build_system_prompt(ctx)
        # Should NOT contain JSON-style persona dump
        assert '"role": "newsroom assistant"' not in prompt
        # Should contain natural-language rendering
        assert "You are: newsroom assistant" in prompt


# ══════════════════════════════════════════════════════════════════════════
# 8. INDEX META-TOOL ROUND-TRIP
# ══════════════════════════════════════════════════════════════════════════


class TestIndexMetaToolRoundTrip:
    """Verify the full workflow: index prompt → get_tool_schema → execute."""

    def test_meta_tool_returns_full_schema_for_any_registered_tool(
        self, gw, env, symbiote_id,
    ) -> None:
        gw.register_index_tool()
        env.configure(symbiote_id=symbiote_id, tools=["get_tool_schema"])

        # Pick a tool from the registered set
        result = gw.execute(
            symbiote_id=symbiote_id, session_id=None,
            tool_id="get_tool_schema",
            params={"tool_id": "items_000"},
        )
        assert result.success is True
        schema = result.output
        assert schema["tool_id"] == "items_000"
        assert schema["name"] == "Items Tool 0"
        assert "properties" in schema["parameters"]
        assert "id" in schema["parameters"]["properties"]

    def test_meta_tool_works_for_tools_across_all_tags(
        self, gw, env, symbiote_id,
    ) -> None:
        gw.register_index_tool()
        env.configure(symbiote_id=symbiote_id, tools=["get_tool_schema"])

        for tag in ("items", "admin", "journals", "newsletter", "compose"):
            result = gw.execute(
                symbiote_id=symbiote_id, session_id=None,
                tool_id="get_tool_schema",
                params={"tool_id": f"{tag}_000"},
            )
            assert result.success is True
            assert result.output["tool_id"] == f"{tag}_000"


# ══════════════════════════════════════════════════════════════════════════
# 9. SUMMARY COMPARISON — side-by-side metrics
# ══════════════════════════════════════════════════════════════════════════


class TestSummaryComparison:
    """Side-by-side comparison of all three modes for reporting."""

    @pytest.mark.parametrize("query", [
        "publique a matéria sobre o incêndio",
        "altere as permissões do usuário admin",
        "envie a newsletter de hoje",
        "configure o plugin de redes sociais",
    ])
    def test_semantic_always_fewer_tools_than_full(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
        query: str,
    ) -> None:
        # Full
        env.configure(symbiote_id=symbiote_id, tool_loading="full")
        ctx_full = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id, query,
        )

        # Semantic
        env.configure(symbiote_id=symbiote_id, tool_loading="semantic")
        ctx_semantic = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id, query,
            semantic_llm=_MockSemanticLLM(),
        )

        assert len(ctx_semantic.available_tools) < len(ctx_full.available_tools)

    @pytest.mark.parametrize("query", [
        "publique a matéria sobre o incêndio",
        "altere as permissões do usuário admin",
        "envie a newsletter de hoje",
    ])
    def test_index_smaller_prompt_than_full_same_tool_count(
        self, identity, memory, knowledge, gw, env, symbiote_id, session_id,
        query: str,
    ) -> None:
        env.configure(symbiote_id=symbiote_id, tool_loading="full")
        ctx_full = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id, query,
        )

        env.configure(symbiote_id=symbiote_id, tool_loading="index")
        ctx_index = _build_context(
            identity, memory, knowledge, gw, env, symbiote_id, session_id, query,
        )

        prompt_full = _build_system_prompt(ctx_full)
        prompt_index = _build_system_prompt(ctx_index)

        # Index has 1 extra tool (get_tool_schema) but much smaller prompt
        assert len(ctx_index.available_tools) == len(ctx_full.available_tools) + 1
        assert len(prompt_index) < len(prompt_full)
