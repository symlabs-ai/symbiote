"""Multi-round context growth tests — full vs index vs semantic.

Simulates a 3-round conversation where each round involves:
- User sends a query
- LLM responds with tool calls
- Tool results are added to history
- Next round sees all previous messages

For index mode, each round also includes a get_tool_schema call+result
before the actual tool call, which adds extra messages to history.

Measures total context size (system prompt + message history) at each
round to detect context bloat.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext, ContextAssembler
from symbiote.core.identity import IdentityManager
from symbiote.core.models import Message
from symbiote.core.session import SessionManager
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.memory.working import WorkingMemory
from symbiote.runners.chat import ChatRunner

# ── Tool set (same as test_loading_modes.py) ─────────────────────────────

_TAG_DIST = {
    "Journals": 25, "Config": 22, "Sources": 21, "Newsletter": 18,
    "Plugins": 15, "Compose": 14, "Public": 14, "Capture": 12,
    "View": 12, "Analytics": 12, "Admin": 11, "Authentication": 7,
    "Items": 7, "Push": 7, "Clark": 8, "Search": 1,
}

_PARAMS = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "description": "Resource ID"},
        "status": {"type": "string", "description": "Filter by status"},
        "limit": {"type": "integer", "description": "Max results"},
        "offset": {"type": "integer", "description": "Pagination offset"},
    },
    "required": ["id"],
}


# ── Conversation scenario ────────────────────────────────────────────────

# 3 rounds of conversation with different intents
_ROUNDS = [
    {
        "user": "publique a matéria sobre o incêndio no jornal principal",
        "semantic_tags": ["Items", "Compose", "Journals"],
        # In index mode, LLM first fetches schema for the tool it wants
        "index_schema_fetch": "items_003",
        # Then calls the actual tool
        "tool_call": "items_003",
        "tool_params": {"id": 42, "status": "draft"},
        "tool_result": {"success": True, "item_id": 42, "status": "published"},
        "assistant_text": "Matéria publicada com sucesso no jornal principal.",
    },
    {
        "user": "agora configure a fonte RSS do G1 para captura automática",
        "semantic_tags": ["Config", "Sources"],
        "index_schema_fetch": "sources_005",
        "tool_call": "sources_005",
        "tool_params": {"id": 1, "status": "active"},
        "tool_result": {"success": True, "source": "G1 RSS", "status": "active"},
        "assistant_text": "Fonte RSS do G1 configurada para captura automática.",
    },
    {
        "user": "me mostre as estatísticas de acesso do mês passado",
        "semantic_tags": ["Analytics"],
        "index_schema_fetch": "analytics_002",
        "tool_call": "analytics_002",
        "tool_params": {"id": 0, "limit": 30},
        "tool_result": {"views": 12450, "unique_visitors": 8320, "period": "2026-02"},
        "assistant_text": "No mês passado tivemos 12.450 views e 8.320 visitantes únicos.",
    },
]


# ── Mock semantic LLM ────────────────────────────────────────────────────

_SEMANTIC_MAP = {r["user"]: r["semantic_tags"] for r in _ROUNDS}


class _MockSemanticLLM:
    def complete(self, messages, **kw):
        user_msg = messages[-1]["content"]
        tags = _SEMANTIC_MAP.get(user_msg, list(_TAG_DIST.keys()))
        return json.dumps(tags)


class _MockChatLLM:
    def complete(self, messages, **kw):
        return "ok"


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "multiround_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    return IdentityManager(storage=adapter).create(
        name="Clark", role="assistant", persona={"role": "newsroom assistant"},
    ).id


@pytest.fixture()
def session_id(adapter: SQLiteAdapter, symbiote_id: str) -> str:
    return SessionManager(storage=adapter).start(
        symbiote_id=symbiote_id, goal="multi-round test",
    ).id


@pytest.fixture()
def env(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def gw(adapter: SQLiteAdapter, env: EnvironmentManager) -> ToolGateway:
    gate = PolicyGate(env_manager=env, storage=adapter)
    gateway = ToolGateway(policy_gate=gate)
    for tag, count in _TAG_DIST.items():
        for i in range(count):
            gateway.register_descriptor(
                ToolDescriptor(
                    tool_id=f"{tag.lower()}_{i:03d}", name=f"{tag} Tool {i}",
                    description=f"{tag} operation #{i}: manage {tag.lower()}",
                    parameters=_PARAMS, tags=[tag], handler_type="http",
                ),
                lambda p: None,
            )
    return gateway


# ── Helpers ──────────────────────────────────────────────────────────────


def _build_system_prompt(context: AssembledContext) -> str:
    runner = ChatRunner(llm=_MockChatLLM(), tool_gateway=None)
    return runner._build_system(context)


def _simulate_round_messages(
    round_data: dict,
    mode: str,
    gw: ToolGateway,
) -> list[Message]:
    """Generate the messages that a round would add to WorkingMemory.

    For index mode: adds get_tool_schema call+result BEFORE the actual tool call.
    For full/semantic: just the tool call+result.
    """
    messages: list[Message] = []

    if mode == "index":
        # 1. Assistant calls get_tool_schema
        schema_call = json.dumps({
            "tool": "get_tool_schema",
            "params": {"tool_id": round_data["index_schema_fetch"]},
        })
        messages.append(Message(
            session_id="s", role="assistant",
            content=f"I need to check the parameters first.\n```tool_call\n{schema_call}\n```",
        ))
        # 2. Tool result for get_tool_schema
        desc = gw.get_descriptor(round_data["index_schema_fetch"])
        schema_result = {
            "tool_id": desc.tool_id,
            "name": desc.name,
            "description": desc.description,
            "parameters": desc.parameters,
        } if desc else {"error": "not found"}
        messages.append(Message(
            session_id="s", role="user",
            content=f"[Tool result: get_tool_schema]\n{json.dumps(schema_result)}",
        ))

    # 3. Assistant calls the actual tool
    tool_call = json.dumps({
        "tool": round_data["tool_call"],
        "params": round_data["tool_params"],
    })
    messages.append(Message(
        session_id="s", role="assistant",
        content=f"{round_data['assistant_text']}\n```tool_call\n{tool_call}\n```",
    ))

    # 4. Tool result
    messages.append(Message(
        session_id="s", role="user",
        content=f"[Tool result: {round_data['tool_call']}]\n{json.dumps(round_data['tool_result'])}",
    ))

    return messages


def _run_multiround(
    adapter, symbiote_id, session_id, env, gw, mode,
    semantic_llm=None,
) -> list[dict]:
    """Run 3 rounds and return per-round metrics."""
    identity = IdentityManager(storage=adapter)
    memory = MemoryStore(storage=adapter)
    knowledge = KnowledgeService(storage=adapter)
    wm = WorkingMemory(session_id=session_id, max_messages=50)

    env.configure(symbiote_id=symbiote_id, tool_loading=mode)

    metrics = []

    for round_idx, round_data in enumerate(_ROUNDS):
        # Add user message to working memory
        wm.update_message(Message(
            session_id=session_id, role="user", content=round_data["user"],
        ))

        # Build context (this is what the LLM would see)
        assembler = ContextAssembler(
            identity=identity, memory=memory, knowledge=knowledge,
            context_budget=200_000, tool_gateway=gw, environment=env,
            semantic_llm=semantic_llm,
        )
        ctx = assembler.build(
            session_id=session_id, symbiote_id=symbiote_id,
            user_input=round_data["user"],
        )

        # Measure system prompt
        system_prompt = _build_system_prompt(ctx)

        # Measure history (what's in working memory)
        history_chars = sum(len(m.content) for m in wm.recent_messages)

        # Total context = system + history + current user input
        total_chars = len(system_prompt) + history_chars + len(round_data["user"])
        total_tokens = total_chars // 4

        metrics.append({
            "round": round_idx + 1,
            "mode": mode,
            "tools_in_prompt": len(ctx.available_tools),
            "system_prompt_chars": len(system_prompt),
            "history_messages": len(wm.recent_messages),
            "history_chars": history_chars,
            "total_chars": total_chars,
            "total_tokens": total_tokens,
        })

        # Simulate what happens after the LLM responds:
        # add tool calls and results to history
        round_messages = _simulate_round_messages(round_data, mode, gw)
        for msg in round_messages:
            wm.update_message(msg)

    return metrics


# ══════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════


class TestMultiRoundContextGrowth:
    """Measure how context grows across 3 rounds for each mode."""

    def test_full_mode_system_prompt_constant_across_rounds(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        metrics = _run_multiround(adapter, symbiote_id, session_id, env, gw, "full")
        # System prompt should be identical every round (same tools)
        prompts = [m["system_prompt_chars"] for m in metrics]
        assert prompts[0] == prompts[1] == prompts[2]

    def test_index_mode_system_prompt_constant_across_rounds(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        metrics = _run_multiround(adapter, symbiote_id, session_id, env, gw, "index")
        # Index system prompt should also be constant (compact list doesn't change)
        prompts = [m["system_prompt_chars"] for m in metrics]
        assert prompts[0] == prompts[1] == prompts[2]

    def test_semantic_mode_system_prompt_varies_per_query(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        metrics = _run_multiround(
            adapter, symbiote_id, session_id, env, gw, "semantic",
            semantic_llm=_MockSemanticLLM(),
        )
        # Semantic resolves different tags per round, so prompt size varies
        prompts = [m["system_prompt_chars"] for m in metrics]
        # Round 1 (Items+Compose+Journals=46 tools) > Round 3 (Analytics=12 tools)
        assert prompts[0] > prompts[2]

    def test_index_mode_history_grows_faster_than_full(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        """Index adds get_tool_schema call+result to history each round."""
        full_metrics = _run_multiround(adapter, symbiote_id, session_id, env, gw, "full")
        index_metrics = _run_multiround(adapter, symbiote_id, session_id, env, gw, "index")

        # After 3 rounds, index has more messages in history
        # (2 extra per round: get_tool_schema call + result)
        full_msgs = full_metrics[2]["history_messages"]
        index_msgs = index_metrics[2]["history_messages"]
        assert index_msgs > full_msgs

    def test_index_history_growth_per_round(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        metrics = _run_multiround(adapter, symbiote_id, session_id, env, gw, "index")
        # Round 1: 1 user msg in history (round hasn't completed yet when measured)
        # After round 1: +4 messages (schema_call, schema_result, tool_call, tool_result)
        # Round 2: 1+4 + 1 user = 6 messages
        # After round 2: +4 more
        # Round 3: 1+4+1+4 + 1 user = 11 messages
        assert metrics[0]["history_messages"] == 1   # just the first user msg
        assert metrics[1]["history_messages"] == 6   # 1 user + 4 from r1 + 1 user
        assert metrics[2]["history_messages"] == 11  # + 4 from r2 + 1 user

    def test_full_history_growth_per_round(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        metrics = _run_multiround(adapter, symbiote_id, session_id, env, gw, "full")
        # Full mode: 2 messages per completed round (tool_call + tool_result)
        assert metrics[0]["history_messages"] == 1   # just user
        assert metrics[1]["history_messages"] == 4   # 1 user + 2 from r1 + 1 user
        assert metrics[2]["history_messages"] == 7   # + 2 from r2 + 1 user

    def test_semantic_history_growth_same_as_full(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        """Semantic doesn't add extra messages — history grows like full."""
        full_metrics = _run_multiround(adapter, symbiote_id, session_id, env, gw, "full")
        sem_metrics = _run_multiround(
            adapter, symbiote_id, session_id, env, gw, "semantic",
            semantic_llm=_MockSemanticLLM(),
        )
        for i in range(3):
            assert sem_metrics[i]["history_messages"] == full_metrics[i]["history_messages"]


class TestMultiRoundTotalContext:
    """Compare total context (system + history) across rounds."""

    def test_index_total_smaller_than_full_at_round_1(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        full = _run_multiround(adapter, symbiote_id, session_id, env, gw, "full")
        index = _run_multiround(adapter, symbiote_id, session_id, env, gw, "index")
        # At round 1, index wins big (tiny system prompt, minimal history)
        assert index[0]["total_tokens"] < full[0]["total_tokens"]

    def test_index_total_still_smaller_than_full_at_round_3(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        full = _run_multiround(adapter, symbiote_id, session_id, env, gw, "full")
        index = _run_multiround(adapter, symbiote_id, session_id, env, gw, "index")
        # Even after 3 rounds with accumulated schemas, index should still be smaller
        # because the system prompt savings (~22k tokens) vastly outweigh
        # the history growth (~6 extra messages ~1k tokens)
        assert index[2]["total_tokens"] < full[2]["total_tokens"]

    def test_semantic_total_smallest_at_round_3(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        full = _run_multiround(adapter, symbiote_id, session_id, env, gw, "full")
        index = _run_multiround(adapter, symbiote_id, session_id, env, gw, "index")
        sem = _run_multiround(
            adapter, symbiote_id, session_id, env, gw, "semantic",
            semantic_llm=_MockSemanticLLM(),
        )
        # Semantic: small system prompt + same history as full = smallest total
        assert sem[2]["total_tokens"] < index[2]["total_tokens"]
        assert sem[2]["total_tokens"] < full[2]["total_tokens"]

    def test_index_history_overhead_quantified(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        """Quantify exactly how much extra history index mode accumulates."""
        full = _run_multiround(adapter, symbiote_id, session_id, env, gw, "full")
        index = _run_multiround(adapter, symbiote_id, session_id, env, gw, "index")

        # Extra history chars from get_tool_schema calls
        history_overhead = index[2]["history_chars"] - full[2]["history_chars"]
        system_savings = full[2]["system_prompt_chars"] - index[2]["system_prompt_chars"]

        # The savings from compact system prompt should far exceed the history overhead
        assert system_savings > history_overhead * 3, (
            f"System savings ({system_savings}) should be >3x history overhead "
            f"({history_overhead}) for index mode to be worthwhile"
        )


class TestMultiRoundReport:
    """Generate a human-readable comparison report."""

    def test_print_comparison_report(
        self, adapter, symbiote_id, session_id, env, gw,
    ) -> None:
        full = _run_multiround(adapter, symbiote_id, session_id, env, gw, "full")
        index = _run_multiround(adapter, symbiote_id, session_id, env, gw, "index")
        sem = _run_multiround(
            adapter, symbiote_id, session_id, env, gw, "semantic",
            semantic_llm=_MockSemanticLLM(),
        )

        print("\n")
        print("=" * 95)
        print("MULTI-ROUND CONTEXT GROWTH COMPARISON (3 rounds, 209 tools)")
        print("=" * 95)
        header = (
            f"{'Round':<7s} {'Mode':<10s} {'Tools':<7s} "
            f"{'SysPrompt':<12s} {'History':<10s} {'Msgs':<6s} "
            f"{'Total':<12s} {'~Tokens':<10s}"
        )
        print(header)
        print("-" * 95)

        for metrics_list in (full, index, sem):
            for m in metrics_list:
                print(
                    f"R{m['round']:<6d} {m['mode']:<10s} {m['tools_in_prompt']:<7d} "
                    f"{m['system_prompt_chars']:>10,d}  {m['history_chars']:>8,d}  "
                    f"{m['history_messages']:<6d}"
                    f"{m['total_chars']:>10,d}  {m['total_tokens']:>8,d}"
                )
            print()

        # Summary
        print("SUMMARY — Round 3 (worst case):")
        print(f"  Full:     {full[2]['total_tokens']:>6,d} tokens  ({full[2]['history_messages']} msgs in history)")
        print(f"  Index:    {index[2]['total_tokens']:>6,d} tokens  ({index[2]['history_messages']} msgs in history)")
        print(f"  Semantic: {sem[2]['total_tokens']:>6,d} tokens  ({sem[2]['history_messages']} msgs in history)")
        print()

        idx_overhead = index[2]["history_chars"] - full[2]["history_chars"]
        sys_savings = full[2]["system_prompt_chars"] - index[2]["system_prompt_chars"]
        print(f"  Index history overhead vs full: +{idx_overhead:,d} chars ({idx_overhead//4:,d} tokens)")
        print(f"  Index system prompt savings:    -{sys_savings:,d} chars ({sys_savings//4:,d} tokens)")
        print(f"  Net savings:                    -{sys_savings - idx_overhead:,d} chars ({(sys_savings - idx_overhead)//4:,d} tokens)")
        print("=" * 95)

        # This test always passes — it's for the report output
        assert True
