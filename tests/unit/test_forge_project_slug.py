"""``ForgeLLMAdapter`` speaks of the gateway only to the gateway (B-92).

It used to send ``project_slug: "symbiote"`` on every call, to any provider.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from symbiote.adapters.llm.forge import ForgeLLMAdapter


def _extra(mock_agent_cls: MagicMock) -> dict | None:
    return mock_agent_cls.call_args.kwargs.get("extra")


@pytest.mark.parametrize("provider", ["anthropic", "openai", "groq", "ollama"])
@patch("symbiote.adapters.llm.forge.ChatAgent")
def test_a_direct_provider_gets_no_gateway_field(mock_agent_cls: MagicMock, provider) -> None:
    ForgeLLMAdapter(provider=provider, api_key="k")

    assert "extra" not in mock_agent_cls.call_args.kwargs


@patch("symbiote.adapters.llm.forge.ChatAgent")
def test_a_direct_provider_ignores_a_project_slug(mock_agent_cls: MagicMock) -> None:
    ForgeLLMAdapter(provider="anthropic", api_key="k", project_slug="acme")

    assert "extra" not in mock_agent_cls.call_args.kwargs


@patch("symbiote.adapters.llm.forge.ChatAgent")
def test_the_gateway_keeps_its_historical_slug_by_default(mock_agent_cls: MagicMock) -> None:
    ForgeLLMAdapter(provider="symgateway", api_key="k")

    assert _extra(mock_agent_cls) == {"project_slug": "symbiote"}


@patch("symbiote.adapters.llm.forge.ChatAgent")
def test_the_host_names_its_own_project_or_none(mock_agent_cls: MagicMock) -> None:
    ForgeLLMAdapter(provider="symgateway", api_key="k", project_slug="acme")
    assert _extra(mock_agent_cls) == {"project_slug": "acme"}

    ForgeLLMAdapter(provider="symgateway", api_key="k", project_slug=None)
    assert "extra" not in mock_agent_cls.call_args.kwargs


@patch("symbiote.adapters.llm.forge.ChatAgent")
def test_the_adapter_says_which_provider_and_model_it_is(mock_agent_cls: MagicMock) -> None:
    adapter = ForgeLLMAdapter(provider="anthropic", model="claude-x", api_key="k")

    assert (adapter.provider, adapter.model) == ("anthropic", "claude-x")
