"""Mock LLM adapter for testing without API keys."""

from __future__ import annotations


class MockLLMAdapter:
    """In-memory mock that satisfies LLMPort — useful for tests."""

    def __init__(
        self,
        default_response: str = "Mock response",
        responses: list[str] | None = None,
    ) -> None:
        self.default_response = default_response
        self._responses = responses
        self._response_index = 0
        self.calls: list[dict] = []

    def complete(self, messages: list[dict], config: dict | None = None, tools: list[dict] | None = None) -> str:
        """Return a canned response and record the call."""
        self.calls.append({"messages": messages, "config": config, "tools": tools})

        if self._responses:
            response = self._responses[self._response_index % len(self._responses)]
            self._response_index += 1
            return response

        return self.default_response
