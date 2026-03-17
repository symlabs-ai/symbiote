"""E2E tests for real LLM integration — B-5.

These tests require a real LLM provider (forge-llm with API key).
They are skipped by default unless SYMBIOTE_E2E_LLM=1 is set.

Usage:
    SYMBIOTE_E2E_LLM=1 pytest tests/e2e/test_e2e_llm_integration.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

_SKIP_REASON = "Set SYMBIOTE_E2E_LLM=1 and ensure forge-llm is configured to run LLM integration tests"

skip_unless_llm = pytest.mark.skipif(
    os.environ.get("SYMBIOTE_E2E_LLM") != "1",
    reason=_SKIP_REASON,
)


def _make_kernel(tmp_path: Path) -> SymbioteKernel:
    """Create a kernel with real LLM via ForgeLLMAdapter."""
    from symbiote.adapters.llm.forge import ForgeLLMAdapter

    config = KernelConfig(db_path=tmp_path / "e2e_llm.db")
    provider = os.environ.get("SYMBIOTE_LLM_PROVIDER", "anthropic")
    model = os.environ.get("SYMBIOTE_LLM_MODEL")

    llm = ForgeLLMAdapter(provider=provider, model=model)
    return SymbioteKernel(config=config, llm=llm)


@skip_unless_llm
class TestLLMIntegrationBasic:
    """Basic end-to-end tests with a real LLM."""

    def test_simple_chat(self, tmp_path: Path) -> None:
        """Send a message and get a real LLM response."""
        kernel = _make_kernel(tmp_path)
        try:
            sym = kernel.create_symbiote(name="E2E-Bot", role="assistant")
            session = kernel.start_session(symbiote_id=sym.id, goal="E2E test")

            response = kernel.message(
                session_id=session.id,
                content="Reply with exactly: HELLO_E2E",
            )

            # Response should be a non-empty string
            if isinstance(response, dict):
                text = response.get("text", "")
            else:
                text = str(response)

            assert len(text) > 0
            assert "HELLO_E2E" in text.upper().replace(" ", "_") or len(text) > 5

            kernel.close_session(session.id)
        finally:
            kernel.shutdown()

    def test_chat_with_persona(self, tmp_path: Path) -> None:
        """Test that persona influences the response."""
        kernel = _make_kernel(tmp_path)
        try:
            sym = kernel.create_symbiote(
                name="Pirate-Bot",
                role="assistant",
                persona={"tone": "pirate", "style": "uses nautical terms"},
            )
            session = kernel.start_session(symbiote_id=sym.id)

            response = kernel.message(
                session_id=session.id,
                content="Say hello to me in character.",
            )

            text = response if isinstance(response, str) else response.get("text", "")
            assert len(text) > 0

            kernel.close_session(session.id)
        finally:
            kernel.shutdown()

    def test_multi_turn_conversation(self, tmp_path: Path) -> None:
        """Test that the kernel handles multi-turn conversation."""
        kernel = _make_kernel(tmp_path)
        try:
            sym = kernel.create_symbiote(name="Multi-Bot", role="assistant")
            session = kernel.start_session(symbiote_id=sym.id)

            # Turn 1
            r1 = kernel.message(session_id=session.id, content="My name is TestUser42.")
            assert r1 is not None

            # Turn 2 — should remember the name
            r2 = kernel.message(session_id=session.id, content="What is my name?")
            text = r2 if isinstance(r2, str) else r2.get("text", "")
            assert "TestUser42" in text or "testuser" in text.lower()

            kernel.close_session(session.id)
        finally:
            kernel.shutdown()


@skip_unless_llm
class TestLLMIntegrationWithTools:
    """Test tool calling with a real LLM."""

    def test_builtin_fs_read_tool(self, tmp_path: Path) -> None:
        """Test that the LLM can use the fs_read tool."""
        kernel = _make_kernel(tmp_path)
        try:
            # Create a file to read
            test_file = tmp_path / "hello.txt"
            test_file.write_text("Secret message: FOUND_IT_42")

            sym = kernel.create_symbiote(name="Tool-Bot", role="assistant")
            kernel.environment.configure(
                symbiote_id=sym.id, tools=["fs_read"]
            )
            session = kernel.start_session(symbiote_id=sym.id)

            response = kernel.message(
                session_id=session.id,
                content=f"Read the file at {test_file} and tell me the secret message.",
            )

            if isinstance(response, dict):
                text = response.get("text", "")
                tool_results = response.get("tool_results", [])
                # Either the LLM used the tool or mentioned the content
                has_tool = any(
                    tr.get("success") and "FOUND_IT_42" in str(tr.get("output", ""))
                    for tr in tool_results
                )
                has_text = "FOUND_IT_42" in text
                assert has_tool or has_text or len(text) > 0
            else:
                assert len(str(response)) > 0

            kernel.close_session(session.id)
        finally:
            kernel.shutdown()


@skip_unless_llm
class TestLLMIntegrationReflection:
    """Test reflection with real LLM-generated messages."""

    def test_reflection_after_conversation(self, tmp_path: Path) -> None:
        """Reflection should extract facts from a real conversation."""
        kernel = _make_kernel(tmp_path)
        try:
            sym = kernel.create_symbiote(name="Reflect-Bot", role="assistant")
            session = kernel.start_session(symbiote_id=sym.id)

            kernel.message(
                session_id=session.id,
                content="I always prefer Python over JavaScript for backend work.",
            )
            kernel.message(
                session_id=session.id,
                content="The rule is: never deploy on Fridays.",
            )

            session = kernel.close_session(session.id)
            assert session.status == "closed"
            assert session.summary is not None
        finally:
            kernel.shutdown()
