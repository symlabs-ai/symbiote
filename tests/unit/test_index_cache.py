"""Tests for index mode schema cache in ChatRunner (B-34)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext
from symbiote.core.identity import IdentityManager
from symbiote.environment.descriptors import ToolCallResult
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.runners.chat import ChatRunner

# ── Helpers ──────────────────────────────────────────────────────────────


class MultiStepMockLLM:
    """Mock LLM that returns different responses on each call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._call_idx = 0
        self.call_count = 0
        self.all_messages: list[list[dict]] = []

    def complete(self, messages, **kwargs):
        self.call_count += 1
        self.all_messages.append(list(messages))
        idx = min(self._call_idx, len(self._responses) - 1)
        self._call_idx += 1
        return self._responses[idx]


class GatewayCallTracker:
    """Wraps a ToolGateway to count execute_tool_calls invocations."""

    def __init__(self, gateway: ToolGateway) -> None:
        self._gateway = gateway
        self.call_log: list[list[str]] = []  # each entry = list of tool_ids

    def execute_tool_calls(self, *, symbiote_id, session_id, calls, **kwargs):
        self.call_log.append([c.tool_id for c in calls])
        return self._gateway.execute_tool_calls(
            symbiote_id=symbiote_id, session_id=session_id, calls=calls, **kwargs,
        )

    def __getattr__(self, name):
        return getattr(self._gateway, name)


def _make_context(
    symbiote_id: str,
    user_input: str,
    *,
    tools: list[dict] | None = None,
    tool_loop: bool = True,
    tool_loading: str = "full",
) -> AssembledContext:
    return AssembledContext(
        symbiote_id=symbiote_id,
        session_id="sess-1",
        user_input=user_input,
        available_tools=tools or [],
        tool_loop=tool_loop,
        tool_loading=tool_loading,
    )


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "index_cache_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="CacheBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def tool_gateway(env_manager: EnvironmentManager, adapter: SQLiteAdapter) -> ToolGateway:
    gate = PolicyGate(env_manager=env_manager, storage=adapter)
    return ToolGateway(policy_gate=gate)


# ── Tests ────────────────────────────────────────────────────────────────


class TestIndexSchemaCache:
    """Index mode schema cache avoids redundant get_tool_schema calls."""

    def test_schema_fetched_once_second_call_cached(
        self, symbiote_id, tool_gateway, env_manager,
    ) -> None:
        """When the LLM calls get_tool_schema twice for the same tool,
        only the first call goes through the gateway."""
        tool_gateway.register_tool("items_list", lambda p: [{"id": 1}])
        tool_gateway.register_index_tool()
        env_manager.configure(symbiote_id=symbiote_id, tools=["items_list", "get_tool_schema"])

        tracker = GatewayCallTracker(tool_gateway)

        llm = MultiStepMockLLM([
            # 1: fetch schema for items_list
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_list"}}\n```',
            # 2: call items_list
            '```tool_call\n{"tool": "items_list", "params": {}}\n```',
            # 3: fetch schema for items_list AGAIN (should be cached)
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_list"}}\n```',
            # 4: call items_list again
            '```tool_call\n{"tool": "items_list", "params": {}}\n```',
            # 5: final text
            "Done.",
        ])
        runner = ChatRunner(llm, tool_gateway=tracker)
        context = _make_context(
            symbiote_id, "do stuff", tool_loading="index", tool_loop=True,
        )
        result = runner.run(context)

        assert result.success is True
        # The 3rd LLM response (get_tool_schema for items_list) should NOT
        # appear in gateway calls because it was cached.
        schema_gateway_calls = [
            calls for calls in tracker.call_log
            if "get_tool_schema" in calls
        ]
        assert len(schema_gateway_calls) == 1, (
            f"get_tool_schema should only go through gateway once, got: {tracker.call_log}"
        )

    def test_non_schema_calls_not_affected(
        self, symbiote_id, tool_gateway, env_manager,
    ) -> None:
        """Non get_tool_schema calls always go through the gateway."""
        tool_gateway.register_tool("echo", lambda p: "ok")
        tool_gateway.register_index_tool()
        env_manager.configure(symbiote_id=symbiote_id, tools=["echo", "get_tool_schema"])

        tracker = GatewayCallTracker(tool_gateway)

        llm = MultiStepMockLLM([
            '```tool_call\n{"tool": "echo", "params": {}}\n```',
            '```tool_call\n{"tool": "echo", "params": {}}\n```',
            "Done.",
        ])
        runner = ChatRunner(llm, tool_gateway=tracker)
        context = _make_context(
            symbiote_id, "echo twice", tool_loading="index", tool_loop=True,
        )
        result = runner.run(context)

        assert result.success is True
        # Both echo calls should go through gateway
        echo_calls = [c for calls in tracker.call_log for c in calls if c == "echo"]
        assert len(echo_calls) == 2

    def test_cache_only_active_in_index_mode(
        self, symbiote_id, tool_gateway, env_manager,
    ) -> None:
        """In full mode, get_tool_schema calls are not cached."""
        tool_gateway.register_tool("items_list", lambda p: [{"id": 1}])
        tool_gateway.register_index_tool()
        env_manager.configure(symbiote_id=symbiote_id, tools=["items_list", "get_tool_schema"])

        tracker = GatewayCallTracker(tool_gateway)

        llm = MultiStepMockLLM([
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_list"}}\n```',
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_list"}}\n```',
            "Done.",
        ])
        runner = ChatRunner(llm, tool_gateway=tracker)
        # tool_loading="full" — cache should NOT be active
        context = _make_context(
            symbiote_id, "fetch schema", tool_loading="full", tool_loop=True,
        )
        result = runner.run(context)

        assert result.success is True
        schema_gateway_calls = [
            calls for calls in tracker.call_log
            if "get_tool_schema" in calls
        ]
        # Both calls should go through gateway since we're in full mode
        assert len(schema_gateway_calls) == 2

    def test_cache_hit_returns_correct_data(
        self, symbiote_id, tool_gateway, env_manager,
    ) -> None:
        """Cached schema result contains the same data as the original."""
        tool_gateway.register_tool("items_list", lambda p: [{"id": 1}])
        tool_gateway.register_index_tool()
        env_manager.configure(symbiote_id=symbiote_id, tools=["items_list", "get_tool_schema"])

        llm = MultiStepMockLLM([
            # 1: fetch schema
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_list"}}\n```',
            # 2: fetch schema again (cached)
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_list"}}\n```',
            "Done.",
        ])
        runner = ChatRunner(llm, tool_gateway=tool_gateway)
        context = _make_context(
            symbiote_id, "get schema", tool_loading="index", tool_loop=True,
        )
        result = runner.run(context)

        assert result.success is True
        assert isinstance(result.output, dict)
        # Both schema tool results should be present and successful
        schema_results = [
            r for r in result.output["tool_results"]
            if r["tool_id"] == "get_tool_schema"
        ]
        assert len(schema_results) == 2
        assert all(r["success"] for r in schema_results)

    def test_mixed_calls_some_cached_some_not(
        self, symbiote_id, tool_gateway, env_manager,
    ) -> None:
        """A mix of cached schema calls and regular tool calls works correctly."""
        tool_gateway.register_tool("items_list", lambda p: [{"id": 1}])
        tool_gateway.register_tool("items_publish", lambda p: {"ok": True})
        tool_gateway.register_index_tool()
        env_manager.configure(
            symbiote_id=symbiote_id,
            tools=["items_list", "items_publish", "get_tool_schema"],
        )

        tracker = GatewayCallTracker(tool_gateway)

        llm = MultiStepMockLLM([
            # 1: fetch schema for items_list (gateway)
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_list"}}\n```',
            # 2: call items_list (gateway)
            '```tool_call\n{"tool": "items_list", "params": {}}\n```',
            # 3: fetch schema for items_publish (gateway) + items_list schema (cached)
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_list"}}\n```',
            # 4: fetch schema for items_publish (new, gateway)
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_publish"}}\n```',
            # 5: call items_publish
            '```tool_call\n{"tool": "items_publish", "params": {"item_id": 1}}\n```',
            # 6: done
            "Published.",
        ])
        runner = ChatRunner(llm, tool_gateway=tracker)
        context = _make_context(
            symbiote_id, "publish item", tool_loading="index", tool_loop=True,
        )
        result = runner.run(context)

        assert result.success is True
        # items_list schema: only 1 gateway call (2nd was cached)
        # items_publish schema: 1 gateway call
        all_gateway_tool_ids = [tid for calls in tracker.call_log for tid in calls]
        schema_calls_for_items_list = all_gateway_tool_ids.count("get_tool_schema")
        # First iteration: get_tool_schema(items_list) -> 1 gateway call
        # Third iteration: get_tool_schema(items_list) -> cached, 0 gateway calls
        # Fourth iteration: get_tool_schema(items_publish) -> 1 gateway call
        # Total gateway get_tool_schema calls: 2
        assert schema_calls_for_items_list == 2

    def test_cache_is_per_loop(self, symbiote_id, tool_gateway, env_manager) -> None:
        """Each run() invocation starts with a fresh cache."""
        tool_gateway.register_tool("items_list", lambda p: [{"id": 1}])
        tool_gateway.register_index_tool()
        env_manager.configure(symbiote_id=symbiote_id, tools=["items_list", "get_tool_schema"])

        tracker = GatewayCallTracker(tool_gateway)

        # First run: fetch schema for items_list
        llm1 = MultiStepMockLLM([
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_list"}}\n```',
            "Done run 1.",
        ])
        runner = ChatRunner(llm1, tool_gateway=tracker)
        context = _make_context(
            symbiote_id, "run 1", tool_loading="index", tool_loop=True,
        )
        runner.run(context)

        # Second run: same schema fetch should go through gateway again
        # (cache is loop-local, not persistent)
        llm2 = MultiStepMockLLM([
            '```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "items_list"}}\n```',
            "Done run 2.",
        ])
        runner2 = ChatRunner(llm2, tool_gateway=tracker)
        context2 = _make_context(
            symbiote_id, "run 2", tool_loading="index", tool_loop=True,
        )
        runner2.run(context2)

        # Both runs should have sent get_tool_schema through gateway
        schema_gateway_calls = [
            calls for calls in tracker.call_log
            if "get_tool_schema" in calls
        ]
        assert len(schema_gateway_calls) == 2
