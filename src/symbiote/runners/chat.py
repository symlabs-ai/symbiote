"""ChatRunner — conversational runner that delegates to an LLM."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from symbiote.core.context import AssembledContext
from symbiote.core.models import Message
from symbiote.core.ports import LLMPort
from symbiote.environment.descriptors import ToolCallResult
from symbiote.environment.parser import parse_tool_calls
from symbiote.environment.runtime_context import inject_runtime_context
from symbiote.memory.working import WorkingMemory
from symbiote.runners.base import RunResult

if TYPE_CHECKING:
    from symbiote.environment.tools import ToolGateway
    from symbiote.memory.consolidator import MemoryConsolidator

_HANDLED_INTENTS = frozenset({"chat", "ask", "question", "talk"})

_TOOL_INSTRUCTIONS = """\
To use a tool, include a fenced code block with the language tag `tool_call` containing a JSON object:

```tool_call
{"tool": "<tool_id>", "params": {<parameters>}}
```

You may include multiple tool_call blocks in a single response. \
Tool results will be provided back to you."""


class ChatRunner:
    """Runner that handles conversational intents via an LLM."""

    runner_type: str = "chat"

    def __init__(
        self,
        llm: LLMPort,
        working_memory: WorkingMemory | None = None,
        tool_gateway: ToolGateway | None = None,
        consolidator: MemoryConsolidator | None = None,
    ) -> None:
        self._llm = llm
        self._working_memory = working_memory
        self._tool_gateway = tool_gateway
        self._consolidator = consolidator

    def can_handle(self, intent: str) -> bool:
        return intent in _HANDLED_INTENTS

    def run(self, context: AssembledContext) -> RunResult:
        messages = self._build_messages(context)
        try:
            response = self._llm.complete(messages)
        except Exception as exc:
            return RunResult(success=False, error=str(exc), runner_type=self.runner_type)

        # Parse and execute tool calls
        clean_text, tool_calls = parse_tool_calls(response)
        tool_results: list[ToolCallResult] = []

        if tool_calls and self._tool_gateway is not None:
            tool_results = self._tool_gateway.execute_tool_calls(
                symbiote_id=context.symbiote_id,
                session_id=context.session_id,
                calls=tool_calls,
            )

        if self._working_memory is not None:
            self._working_memory.update_message(
                Message(
                    session_id=context.session_id,
                    role="assistant",
                    content=clean_text,
                )
            )

            # Consolidate if working memory exceeds token threshold
            if self._consolidator is not None:
                self._consolidator.consolidate_if_needed(
                    self._working_memory, context.symbiote_id
                )

        output = clean_text
        if tool_results:
            output = {
                "text": clean_text,
                "tool_results": [r.model_dump() for r in tool_results],
            }

        return RunResult(success=True, output=output, runner_type=self.runner_type)

    # ── internal ─────────────────────────────────────────────────────────

    def _build_messages(self, context: AssembledContext) -> list[dict]:
        messages: list[dict] = []

        # 1. System message
        messages.append({"role": "system", "content": self._build_system(context)})

        # 2. Conversation history from working memory
        if self._working_memory is not None:
            for msg in self._working_memory.recent_messages:
                messages.append({"role": msg.role, "content": msg.content})

        # 3. Current user input (with ephemeral runtime context for the LLM)
        user_content = inject_runtime_context(
            context.user_input,
            session_id=context.session_id,
        )
        messages.append({"role": "user", "content": user_content})

        return messages

    def _build_system(self, context: AssembledContext) -> str:
        parts: list[str] = []

        # Persona
        if context.persona:
            parts.append("## Persona")
            parts.append(json.dumps(context.persona, indent=2, default=str))

        # Available tools
        if context.available_tools:
            parts.append("## Available Tools")
            parts.append(_TOOL_INSTRUCTIONS)
            for tool in context.available_tools:
                tool_id = tool.get("tool_id", "")
                name = tool.get("name", "")
                desc = tool.get("description", "")
                params = tool.get("parameters", {})
                parts.append(f"### {tool_id} — {name}")
                parts.append(desc)
                if params:
                    parts.append(f"Parameters: ```json\n{json.dumps(params, indent=2)}\n```")

        # Extra context (host-injected, e.g. page context)
        if context.extra_context:
            parts.append("## Context")
            for key, value in context.extra_context.items():
                parts.append(f"### {key}\n{value}")

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
