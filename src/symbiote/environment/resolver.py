"""ToolTagResolver — semantic pre-filter using a cheap LLM."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from symbiote.core.ports import LLMPort

_log = logging.getLogger(__name__)

_RESOLVE_PROMPT = """\
You are a tool-routing assistant. Given a user message and a list of tool \
category tags, return ONLY the tags that are relevant to the user's request.

Available tags: {tags}

Rules:
- Return a JSON array of tag strings, e.g. ["Items", "Compose"]
- Only include tags directly relevant to the user message
- If unsure, include the tag (prefer false positives over false negatives)
- Return ONLY the JSON array, no explanation"""


class ToolTagResolver:
    """Resolve relevant tool tags from user input using a cheap LLM.

    Falls back to returning all tags if the LLM call fails.
    """

    def __init__(self, llm: LLMPort) -> None:
        self._llm = llm

    def resolve(self, user_input: str, available_tags: list[str]) -> list[str]:
        """Return the subset of *available_tags* relevant to *user_input*."""
        if not available_tags:
            return []

        system_msg = _RESOLVE_PROMPT.format(tags=json.dumps(available_tags))
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_input},
        ]

        try:
            response = self._llm.complete(messages)
            # Handle LLMResponse or plain str
            text = response.content if hasattr(response, "content") else str(response)
            parsed = json.loads(text.strip())
            if isinstance(parsed, list):
                # Only return tags that actually exist in available_tags
                valid = set(available_tags)
                return [t for t in parsed if t in valid]
        except Exception as exc:
            _log.warning("ToolTagResolver failed, falling back to all tags: %s", exc)

        return list(available_tags)
