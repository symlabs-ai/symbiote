"""ChatRunner — conversational runner that delegates to an LLM."""

from __future__ import annotations

import json

from symbiote.core.context import AssembledContext
from symbiote.core.models import Message
from symbiote.core.ports import LLMPort
from symbiote.memory.working import WorkingMemory
from symbiote.runners.base import RunResult

_HANDLED_INTENTS = frozenset({"chat", "ask", "question", "talk"})


class ChatRunner:
    """Runner that handles conversational intents via an LLM."""

    runner_type: str = "chat"

    def __init__(
        self,
        llm: LLMPort,
        working_memory: WorkingMemory | None = None,
    ) -> None:
        self._llm = llm
        self._working_memory = working_memory

    def can_handle(self, intent: str) -> bool:
        return intent in _HANDLED_INTENTS

    def run(self, context: AssembledContext) -> RunResult:
        messages = self._build_messages(context)
        try:
            response = self._llm.complete(messages)
        except Exception as exc:
            return RunResult(success=False, error=str(exc), runner_type=self.runner_type)

        if self._working_memory is not None:
            self._working_memory.update_message(
                Message(
                    session_id=context.session_id,
                    role="assistant",
                    content=response,
                )
            )

        return RunResult(success=True, output=response, runner_type=self.runner_type)

    # ── internal ─────────────────────────────────────────────────────────

    def _build_messages(self, context: AssembledContext) -> list[dict]:
        messages: list[dict] = []

        # 1. System message
        messages.append({"role": "system", "content": self._build_system(context)})

        # 2. Conversation history from working memory
        if self._working_memory is not None:
            for msg in self._working_memory.recent_messages:
                messages.append({"role": msg.role, "content": msg.content})

        # 3. Current user input
        messages.append({"role": "user", "content": context.user_input})

        return messages

    def _build_system(self, context: AssembledContext) -> str:
        parts: list[str] = []

        # Persona
        if context.persona:
            parts.append("## Persona")
            parts.append(json.dumps(context.persona, indent=2, default=str))

        # Relevant memories
        if context.relevant_memories:
            parts.append("## Relevant Memories")
            for mem in context.relevant_memories:
                parts.append(f"- {mem.get('content', '')}")

        # Relevant knowledge
        if context.relevant_knowledge:
            parts.append("## Relevant Knowledge")
            for k in context.relevant_knowledge:
                name = k.get("name", "")
                content = k.get("content", "")
                parts.append(f"### {name}\n{content}")

        if not parts:
            return "You are a helpful assistant."

        return "\n\n".join(parts)
