"""ForgeLLM adapter — delegates to forge_llm ChatAgent."""

from __future__ import annotations

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
    ) -> None:
        if ChatAgent is None:
            raise LLMError(
                "forge_llm is not installed — run `pip install forge-llm`"
            )

        try:
            kwargs: dict = {"provider": provider}
            if model is not None:
                kwargs["model"] = model
            if api_key is not None:
                kwargs["api_key"] = api_key
            self._agent = ChatAgent(**kwargs)
        except Exception as exc:
            raise LLMError(str(exc)) from exc

    def complete(self, messages: list[dict], config: dict | None = None) -> str:
        """Send messages to the LLM and return the text response."""
        try:
            chat_messages = [
                ChatMessage(role=m["role"], content=m["content"]) for m in messages
            ]
            response = self._agent.chat(messages=chat_messages)
            return response.message
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(str(exc)) from exc
