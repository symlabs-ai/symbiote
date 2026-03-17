"""GenerationSettings — configurable LLM generation parameters."""

from __future__ import annotations

from pydantic import BaseModel


class GenerationSettings(BaseModel):
    """Settings propagated to the LLM for each completion call.

    These override provider defaults when set (None means use provider default).
    """

    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    reasoning_effort: str | None = None  # e.g. "low", "medium", "high"

    def to_config_dict(self) -> dict:
        """Convert to a config dict for LLMPort.complete(config=...).

        Only includes non-None values so provider defaults are preserved.
        """
        return {k: v for k, v in self.model_dump().items() if v is not None}
