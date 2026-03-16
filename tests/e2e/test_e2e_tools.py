"""E2E test: full tool lifecycle — register, describe, execute via chat, audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor


@pytest.fixture()
def kernel(tmp_path: Path) -> SymbioteKernel:
    # Mock LLM that returns a tool_call block
    llm = MockLLMAdapter(
        default_response=(
            "I'll calculate that for you.\n\n"
            "```tool_call\n"
            '{"tool": "calculator", "params": {"a": 10, "b": 5}}\n'
            "```\n\n"
            "Here's the result."
        )
    )
    config = KernelConfig(db_path=tmp_path / "e2e_tools.db")
    k = SymbioteKernel(config=config, llm=llm)
    yield k
    k.shutdown()


class TestToolLifecycle:
    def test_register_execute_audit(self, kernel: SymbioteKernel) -> None:
        # 1. Create symbiote
        sym = kernel.create_symbiote(name="Clark", role="assistant")

        # 2. Register a custom tool with descriptor
        desc = ToolDescriptor(
            tool_id="calculator",
            name="Calculator",
            description="Add two numbers",
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
        )
        kernel.tool_gateway.register_descriptor(
            desc,
            handler=lambda p: p["a"] + p["b"],
        )

        # 3. Authorize the tool
        kernel.environment.configure(
            symbiote_id=sym.id,
            tools=["calculator"],
        )

        # 4. Verify descriptor is available
        descriptors = kernel.tool_gateway.get_descriptors()
        calc_descs = [d for d in descriptors if d.tool_id == "calculator"]
        assert len(calc_descs) == 1
        assert calc_descs[0].name == "Calculator"

        # 5. Start session and send message
        session = kernel.start_session(sym.id, goal="Math help")
        response = kernel.message(session.id, "What is 10 + 5?")

        # 6. Response should contain tool results
        assert isinstance(response, dict)
        assert "calculate" in response["text"].lower() or "result" in response["text"].lower()
        assert len(response["tool_results"]) == 1
        assert response["tool_results"][0]["success"] is True
        assert response["tool_results"][0]["output"] == 15

        # 7. Check audit log
        log = kernel._policy_gate.get_audit_log(sym.id)
        assert len(log) >= 1
        assert log[0]["tool_id"] == "calculator"
        assert log[0]["action"] == "execute"
        assert log[0]["result"] == "success"

        # 8. Close session
        kernel.close_session(session.id)

    def test_unauthorized_tool_blocked_in_chat(self, kernel: SymbioteKernel) -> None:
        sym = kernel.create_symbiote(name="Limited", role="assistant")

        # Register tool but DON'T authorize it
        kernel.tool_gateway.register_tool("calculator", lambda p: p["a"] + p["b"])

        session = kernel.start_session(sym.id, goal="Test")
        response = kernel.message(session.id, "Calculate")

        # Tool call is parsed but blocked by policy
        assert isinstance(response, dict)
        assert len(response["tool_results"]) == 1
        assert response["tool_results"][0]["success"] is False
        assert "blocked" in response["tool_results"][0]["error"].lower() or "not allowed" in response["tool_results"][0]["error"].lower()

        kernel.close_session(session.id)

    def test_context_includes_tool_descriptors(self, kernel: SymbioteKernel) -> None:
        sym = kernel.create_symbiote(name="Aware", role="assistant")

        desc = ToolDescriptor(
            tool_id="search",
            name="Search",
            description="Search articles",
        )
        kernel.tool_gateway.register_descriptor(desc, lambda p: [])
        kernel.environment.configure(symbiote_id=sym.id, tools=["search"])

        session = kernel.start_session(sym.id, goal="Test")

        # Build context manually to verify tools are included
        context = kernel._context_assembler.build(
            session_id=session.id,
            symbiote_id=sym.id,
            user_input="find news",
        )
        assert len(context.available_tools) >= 1
        tool_ids = [t["tool_id"] for t in context.available_tools]
        assert "search" in tool_ids

        kernel.close_session(session.id)


class TestHttpToolRegistration:
    def test_register_http_tool_via_gateway(self, kernel: SymbioteKernel) -> None:
        kernel.create_symbiote(name="HTTPBot", role="assistant")

        desc = ToolDescriptor(
            tool_id="api_fetch",
            name="Fetch API",
            description="Fetch from external API",
        )
        config = HttpToolConfig(
            method="GET",
            url_template="http://localhost:9999/api/{resource}",
            timeout=5.0,
        )
        kernel.tool_gateway.register_http_tool(desc, config)

        # Verify registration
        assert kernel.tool_gateway.has_tool("api_fetch")
        d = kernel.tool_gateway.get_descriptor("api_fetch")
        assert d is not None
        assert d.handler_type == "http"

        hc = kernel.tool_gateway.get_http_config("api_fetch")
        assert hc is not None
        assert hc.method == "GET"
        assert hc.timeout == 5.0
