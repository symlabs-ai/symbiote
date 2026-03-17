"""Tests for LLM adapters — MockLLMAdapter and ForgeLLMAdapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.adapters.llm.forge import ForgeLLMAdapter
from symbiote.core.exceptions import LLMError
from symbiote.core.ports import LLMPort

# ---------------------------------------------------------------------------
# MockLLMAdapter
# ---------------------------------------------------------------------------


class TestMockLLMAdapter:
    """Unit tests for MockLLMAdapter."""

    def test_returns_default_response(self) -> None:
        adapter = MockLLMAdapter()
        result = adapter.complete([{"role": "user", "content": "Hi"}])
        assert result == "Mock response"

    def test_returns_custom_default_response(self) -> None:
        adapter = MockLLMAdapter(default_response="Custom reply")
        result = adapter.complete([{"role": "user", "content": "Hi"}])
        assert result == "Custom reply"

    def test_records_calls(self) -> None:
        adapter = MockLLMAdapter()
        msgs = [{"role": "user", "content": "Hello"}]
        cfg = {"temperature": 0.5}
        adapter.complete(msgs, config=cfg)

        assert len(adapter.calls) == 1
        assert adapter.calls[0]["messages"] == msgs
        assert adapter.calls[0]["config"] == cfg

    def test_records_multiple_calls(self) -> None:
        adapter = MockLLMAdapter()
        adapter.complete([{"role": "user", "content": "A"}])
        adapter.complete([{"role": "user", "content": "B"}])
        assert len(adapter.calls) == 2

    def test_cycles_through_custom_responses(self) -> None:
        adapter = MockLLMAdapter(responses=["first", "second", "third"])
        assert adapter.complete([]) == "first"
        assert adapter.complete([]) == "second"
        assert adapter.complete([]) == "third"
        # cycles back
        assert adapter.complete([]) == "first"

    def test_satisfies_llm_port_protocol(self) -> None:
        adapter = MockLLMAdapter()
        assert isinstance(adapter, LLMPort)

    def test_config_defaults_to_none(self) -> None:
        adapter = MockLLMAdapter()
        adapter.complete([{"role": "user", "content": "Hi"}])
        assert adapter.calls[0]["config"] is None


# ---------------------------------------------------------------------------
# ForgeLLMAdapter
# ---------------------------------------------------------------------------


class TestForgeLLMAdapter:
    """Unit tests for ForgeLLMAdapter."""

    @patch("symbiote.adapters.llm.forge.ChatAgent")
    def test_construction_succeeds(self, mock_agent_cls: MagicMock) -> None:
        adapter = ForgeLLMAdapter(
            provider="anthropic", model="claude-sonnet-4-20250514", api_key="sk-test"
        )
        assert adapter is not None

    @patch("symbiote.adapters.llm.forge.ChatAgent")
    def test_satisfies_llm_port_protocol(self, mock_agent_cls: MagicMock) -> None:
        adapter = ForgeLLMAdapter(api_key="sk-test")
        assert isinstance(adapter, LLMPort)

    @patch("symbiote.adapters.llm.forge.ChatAgent")
    def test_complete_delegates_to_forge_llm(self, mock_agent_cls: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.content = "LLM says hi"
        mock_agent = mock_agent_cls.return_value
        mock_agent.chat.return_value = mock_response

        adapter = ForgeLLMAdapter(api_key="sk-test")
        result = adapter.complete([{"role": "user", "content": "Hi"}])

        assert result == "LLM says hi"
        mock_agent.chat.assert_called_once()

    @patch("symbiote.adapters.llm.forge.ChatAgent")
    def test_complete_wraps_exception_in_llm_error(
        self, mock_agent_cls: MagicMock
    ) -> None:
        mock_agent = mock_agent_cls.return_value
        mock_agent.chat.side_effect = RuntimeError("API failure")

        adapter = ForgeLLMAdapter(api_key="sk-test")
        with pytest.raises(LLMError, match="API failure"):
            adapter.complete([{"role": "user", "content": "Hi"}])

    @patch(
        "symbiote.adapters.llm.forge.ChatAgent",
        side_effect=Exception("missing key"),
    )
    def test_construction_without_api_key_raises_llm_error(
        self, mock_agent_cls: MagicMock
    ) -> None:
        with pytest.raises(LLMError, match="missing key"):
            ForgeLLMAdapter(provider="anthropic")

    def test_llm_error_is_symbiote_error(self) -> None:
        from symbiote.core.exceptions import SymbioteError

        err = LLMError("boom")
        assert isinstance(err, SymbioteError)
