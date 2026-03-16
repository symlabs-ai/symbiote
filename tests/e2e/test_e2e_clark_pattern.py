"""E2E test: Clark integration pattern — external key + extra context."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel


@pytest.fixture()
def kernel(tmp_path: Path) -> SymbioteKernel:
    llm = MockLLMAdapter(default_response="Here's what I found on this page.")
    config = KernelConfig(db_path=tmp_path / "e2e_clark.db")
    k = SymbioteKernel(config=config, llm=llm)
    yield k
    k.shutdown()


class TestClarkPattern:
    def test_external_key_session_reuse(self, kernel: SymbioteKernel) -> None:
        """Simulate Clark: same (user, page) maps to same session."""
        clark = kernel.create_symbiote(
            name="Clark",
            role="assistant",
            persona={"tone": "helpful"},
        )

        # First message on /articles/123
        s1 = kernel.get_or_create_session(
            symbiote_id=clark.id,
            external_key="user42:/articles/123",
            goal="Article help",
        )
        kernel.message(s1.id, "What is this article about?")

        # Second message on same page — should reuse session
        s2 = kernel.get_or_create_session(
            symbiote_id=clark.id,
            external_key="user42:/articles/123",
        )
        assert s2.id == s1.id

        # Different page — new session
        s3 = kernel.get_or_create_session(
            symbiote_id=clark.id,
            external_key="user42:/compose/new",
        )
        assert s3.id != s1.id

    def test_extra_context_page_injection(self, kernel: SymbioteKernel) -> None:
        """Simulate Clark: page context injected into LLM context."""
        clark = kernel.create_symbiote(
            name="Clark",
            role="assistant",
            persona={"tone": "concise"},
        )
        session = kernel.start_session(clark.id, goal="Page help")

        response = kernel.message(
            session.id,
            "Summarize this page",
            extra_context={
                "page_url": "/articles/ai-trends-2026",
                "page_content": "AI is transforming every industry...",
                "page_type": "article",
            },
        )
        # Response should succeed (mock LLM)
        assert response is not None

    def test_full_clark_flow(self, kernel: SymbioteKernel) -> None:
        """Full Clark flow: create bot, register tools, session by key, message with context."""
        clark = kernel.create_symbiote(
            name="Clark",
            role="younews_assistant",
            persona={
                "tone": "helpful and concise",
                "constraints": ["never share internal data"],
            },
        )

        # Register a tool
        from symbiote.environment.descriptors import ToolDescriptor

        kernel.tool_gateway.register_descriptor(
            ToolDescriptor(
                tool_id="yn_search",
                name="Search",
                description="Search articles",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            ),
            handler=lambda p: [{"title": "AI Trends", "id": "123"}],
        )
        kernel.environment.configure(
            symbiote_id=clark.id,
            tools=["yn_search"],
        )

        # Get or create session by external key
        session = kernel.get_or_create_session(
            symbiote_id=clark.id,
            external_key="user99:/dashboard",
            goal="Dashboard help",
        )

        # Send message with page context
        response = kernel.message(
            session.id,
            "What articles are trending?",
            extra_context={
                "page_url": "/dashboard",
                "visible_items": "Top stories: AI, Climate, Economy",
            },
        )
        assert response is not None

        # Session persists
        found = kernel._sessions.find_by_external_key("user99:/dashboard")
        assert found is not None
        assert found.id == session.id

        kernel.close_session(session.id)
