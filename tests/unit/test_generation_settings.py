"""Tests for GenerationSettings — B-17."""

from __future__ import annotations

from symbiote.core.context import AssembledContext
from symbiote.core.generation import GenerationSettings


class TestGenerationSettings:
    def test_default_all_none(self) -> None:
        gs = GenerationSettings()
        assert gs.temperature is None
        assert gs.max_tokens is None
        assert gs.to_config_dict() == {}

    def test_to_config_dict_filters_none(self) -> None:
        gs = GenerationSettings(temperature=0.7, max_tokens=1000)
        config = gs.to_config_dict()
        assert config == {"temperature": 0.7, "max_tokens": 1000}
        assert "top_p" not in config
        assert "reasoning_effort" not in config

    def test_all_fields_set(self) -> None:
        gs = GenerationSettings(
            temperature=0.5, max_tokens=2000, top_p=0.9, reasoning_effort="high"
        )
        config = gs.to_config_dict()
        assert len(config) == 4
        assert config["reasoning_effort"] == "high"


class TestAssembledContextWithSettings:
    def test_context_accepts_generation_settings(self) -> None:
        gs = GenerationSettings(temperature=0.3)
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess-1",
            user_input="Hello",
            generation_settings=gs.to_config_dict(),
        )
        assert ctx.generation_settings == {"temperature": 0.3}

    def test_context_default_none(self) -> None:
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess-1", user_input="Hello"
        )
        assert ctx.generation_settings is None


class TestChatRunnerPassesConfig:
    def test_config_propagated_to_llm(self) -> None:
        from symbiote.runners.chat import ChatRunner

        calls: list[dict] = []

        class CaptureLLM:
            def complete(self, messages, config=None):
                calls.append({"messages": messages, "config": config})
                return "response"

        runner = ChatRunner(CaptureLLM())
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess-1",
            user_input="Hello",
            generation_settings={"temperature": 0.5, "max_tokens": 500},
        )
        runner.run(ctx)

        assert len(calls) == 1
        assert calls[0]["config"] == {"temperature": 0.5, "max_tokens": 500}

    def test_none_config_passes_none(self) -> None:
        from symbiote.runners.chat import ChatRunner

        calls: list[dict] = []

        class CaptureLLM:
            def complete(self, messages, config=None):
                calls.append({"config": config})
                return "response"

        runner = ChatRunner(CaptureLLM())
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess-1", user_input="Hello"
        )
        runner.run(ctx)

        assert calls[0]["config"] is None


class TestPromptCachingIntegration:
    """Tests for prompt_caching flag propagation from EnvironmentConfig to LLM."""

    def test_prompt_caching_in_environment_config(self) -> None:
        from symbiote.core.models import EnvironmentConfig

        cfg = EnvironmentConfig(symbiote_id="s1", prompt_caching=True)
        assert cfg.prompt_caching is True

    def test_prompt_caching_default_false(self) -> None:
        from symbiote.core.models import EnvironmentConfig

        cfg = EnvironmentConfig(symbiote_id="s1")
        assert cfg.prompt_caching is False

    def test_prompt_caching_propagated_to_llm_config(self) -> None:
        from symbiote.runners.chat import ChatRunner

        calls: list[dict] = []

        class CaptureLLM:
            def complete(self, messages, config=None):
                calls.append({"config": config})
                return "response"

        runner = ChatRunner(CaptureLLM())
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess-1",
            user_input="Hello",
            generation_settings={"prompt_caching": True},
        )
        runner.run(ctx)

        assert calls[0]["config"] == {"prompt_caching": True}

    def test_prompt_caching_not_set_when_disabled(self) -> None:
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess-1",
            user_input="Hello",
        )
        assert ctx.generation_settings is None
