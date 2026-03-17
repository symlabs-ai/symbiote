"""ForgeLLM adapter — delegates to forge_llm ChatAgent."""

from __future__ import annotations

import os

from symbiote.core.exceptions import LLMError

try:
    from forge_llm import ChatAgent, ChatMessage
except ImportError:  # pragma: no cover
    ChatAgent = None  # type: ignore[assignment, misc]
    ChatMessage = None  # type: ignore[assignment, misc]


class ForgeLLMAdapter:
    """LLMPort implementation backed by forge_llm."""

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if ChatAgent is None:
            raise LLMError(
                "forge_llm is not installed — run `pip install forge-llm`"
            )

        try:
            prefix = provider.upper()
            # Resolve API key: explicit > env var ({PROVIDER}_API_KEY)
            if api_key is None:
                api_key = os.environ.get(f"{prefix}_API_KEY")
            # Resolve base URL: explicit > env var ({PROVIDER}_BASE_URL)
            if base_url is None:
                base_url = os.environ.get(f"{prefix}_BASE_URL")

            kwargs: dict = {"provider": provider}
            if model is not None:
                kwargs["model"] = model
            if api_key is not None:
                kwargs["api_key"] = api_key
            if base_url is not None:
                kwargs["base_url"] = base_url
            self._agent = ChatAgent(**kwargs)
        except Exception as exc:
            raise LLMError(str(exc)) from exc

    def complete(self, messages: list[dict], config: dict | None = None) -> str:
        """Send messages to the LLM and return the text response.

        Args:
            messages: Chat messages as dicts with 'role' and 'content'.
            config: Optional generation settings (temperature, max_tokens, etc.)
                    from GenerationSettings.to_config_dict().
        """
        try:
            chat_messages = [
                ChatMessage(role=m["role"], content=m["content"]) for m in messages
            ]
            kwargs: dict = {"messages": chat_messages}
            if config:
                kwargs["config"] = config
            response = self._agent.chat(**kwargs)
            return response.content
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(str(exc)) from exc
