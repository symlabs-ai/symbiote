"""ChatRunner — conversational runner that delegates to an LLM."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from symbiote.core.context import AssembledContext
from symbiote.core.models import Message
from symbiote.core.ports import LLMPort
from symbiote.environment.descriptors import LLMResponse, ToolCall, ToolCallResult
from symbiote.environment.parser import parse_tool_calls
from symbiote.environment.runtime_context import inject_runtime_context
from symbiote.memory.working import WorkingMemory
from symbiote.runners.base import RunResult
from symbiote.runners.response_validator import (
    fallback_text,
    is_valid_response,
    reformulate_message,
)

if TYPE_CHECKING:
    from symbiote.environment.tools import ToolGateway
    from symbiote.memory.consolidator import MemoryConsolidator

_HANDLED_INTENTS = frozenset({"chat", "ask", "question", "talk"})
_MAX_TOOL_ITERATIONS = 10
_MAX_VALIDATION_RETRIES = 2

_TOOL_INSTRUCTIONS = """\
You are an autonomous agent that EXECUTES actions via tools. Rules:
- Do not narrate or explain what you will do. Just call the tool.
- Never ask the user to do something manually when a tool exists.
- Never invent or assume values (like IDs). If you need data, call a tool to get it first.
- After including a tool_call block, STOP your response immediately. \
Do not guess the result. Do not call another tool that depends on it. \
Wait for the actual result, which will be provided in the next message.
- You may include multiple tool_call blocks ONLY if they are fully independent \
(neither depends on the other's result).

To call a tool:

```tool_call
{"tool": "<tool_id>", "params": {<parameters>}}
```"""

_INDEX_INSTRUCTIONS = """\
The tool list below shows only names and descriptions — parameters are NOT shown.
You MUST call `get_tool_schema` to fetch the full parameter schema \
BEFORE calling any other tool. Calling a tool with invented parameters will fail."""


class ChatRunner:
    """Runner that handles conversational intents via an LLM."""

    runner_type: str = "chat"

    def __init__(
        self,
        llm: LLMPort,
        working_memory: WorkingMemory | None = None,
        tool_gateway: ToolGateway | None = None,
        consolidator: MemoryConsolidator | None = None,
        *,
        native_tools: bool = False,
    ) -> None:
        self._llm = llm
        self._working_memory = working_memory
        self._tool_gateway = tool_gateway
        self._consolidator = consolidator
        self._native_tools = native_tools

    def can_handle(self, intent: str) -> bool:
        return intent in _HANDLED_INTENTS

    def run(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Run the chat runner synchronously with tool-loop support.

        When ``context.tool_loop`` is True (default), the runner feeds tool
        results back to the LLM and re-invokes it until the LLM responds
        without tool calls or ``_MAX_TOOL_ITERATIONS`` is reached.
        """
        messages = self._build_messages(context)
        kwargs = self._build_llm_kwargs(context)
        max_iters = _MAX_TOOL_ITERATIONS if context.tool_loop else 1

        all_tool_results: list[ToolCallResult] = []
        final_text = ""

        for _ in range(max_iters):
            try:
                response, buffered_chunks = self._call_llm_sync(messages, kwargs, on_token)
            except Exception as exc:
                return RunResult(success=False, error=str(exc), runner_type=self.runner_type)

            clean_text, tool_calls = self._parse_response(response)

            # ── Response validation ───────────────────────────────────────
            # Only validate when there are no tool calls (a tool-call
            # response is intentionally structured and not meant for the user).
            if not tool_calls:
                clean_text, buffered_chunks = self._validate_and_fix(
                    clean_text, buffered_chunks, messages, kwargs
                )

            final_text = clean_text

            # Release tokens to the caller after validation.
            # Streaming: flush the buffered chunks.
            # Non-streaming: emit the full text once (original behaviour).
            if on_token is not None:
                if buffered_chunks is not None:
                    for chunk in buffered_chunks:
                        on_token(chunk)
                else:
                    on_token(clean_text)

            if not tool_calls or self._tool_gateway is None:
                break

            results = self._tool_gateway.execute_tool_calls(
                symbiote_id=context.symbiote_id,
                session_id=context.session_id,
                calls=tool_calls,
            )
            all_tool_results.extend(results)

            # Feed results back for the next iteration
            messages.append({"role": "assistant", "content": self._format_assistant_with_calls(clean_text, tool_calls)})
            messages.append({"role": "user", "content": self._format_tool_results(results)})

        # Save only the final response to working memory
        if self._working_memory is not None:
            self._working_memory.update_message(
                Message(session_id=context.session_id, role="assistant", content=final_text)
            )
            if self._consolidator is not None:
                self._consolidator.consolidate_if_needed(self._working_memory, context.symbiote_id)

        output = final_text
        if all_tool_results:
            output = {"text": final_text, "tool_results": [r.model_dump() for r in all_tool_results]}

        return RunResult(success=True, output=output, runner_type=self.runner_type)

    async def run_async(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Async variant of run() with tool-loop support."""
        messages = self._build_messages(context)
        kwargs = self._build_llm_kwargs(context)
        max_iters = _MAX_TOOL_ITERATIONS if context.tool_loop else 1

        all_tool_results: list[ToolCallResult] = []
        final_text = ""

        for _ in range(max_iters):
            try:
                response, buffered_chunks = self._call_llm_sync(messages, kwargs, on_token)
            except Exception as exc:
                return RunResult(success=False, error=str(exc), runner_type=self.runner_type)

            clean_text, tool_calls = self._parse_response(response)

            # ── Response validation ───────────────────────────────────────
            if not tool_calls:
                clean_text, buffered_chunks = self._validate_and_fix(
                    clean_text, buffered_chunks, messages, kwargs
                )

            final_text = clean_text

            # Release tokens to the caller after validation.
            if on_token is not None:
                if buffered_chunks is not None:
                    for chunk in buffered_chunks:
                        on_token(chunk)
                else:
                    on_token(clean_text)

            if not tool_calls or self._tool_gateway is None:
                break

            results = await self._tool_gateway.execute_tool_calls_async(
                symbiote_id=context.symbiote_id,
                session_id=context.session_id,
                calls=tool_calls,
            )
            all_tool_results.extend(results)

            messages.append({"role": "assistant", "content": self._format_assistant_with_calls(clean_text, tool_calls)})
            messages.append({"role": "user", "content": self._format_tool_results(results)})

        if self._working_memory is not None:
            self._working_memory.update_message(
                Message(session_id=context.session_id, role="assistant", content=final_text)
            )
            if self._consolidator is not None:
                self._consolidator.consolidate_if_needed(self._working_memory, context.symbiote_id)

        output = final_text
        if all_tool_results:
            output = {"text": final_text, "tool_results": [r.model_dump() for r in all_tool_results]}

        return RunResult(success=True, output=output, runner_type=self.runner_type)

    # ── internal ─────────────────────────────────────────────────────────

    def _build_llm_kwargs(self, context: AssembledContext) -> dict:
        """Build kwargs dict for the LLM call (config + native tools)."""
        native_tool_defs: list[dict] | None = None
        if self._native_tools and context.available_tools:
            from symbiote.environment.descriptors import ToolDescriptor

            native_tool_defs = [
                ToolDescriptor(**t).to_openai_schema() for t in context.available_tools
            ]
        kwargs: dict = {"config": context.generation_settings}
        if native_tool_defs is not None:
            kwargs["tools"] = native_tool_defs
        return kwargs

    def _call_llm_sync(
        self, messages: list[dict], kwargs: dict,
        on_token: Callable[[str], None] | None,
    ) -> tuple[str | LLMResponse, list[str] | None]:
        """Call the LLM synchronously, with optional streaming.

        Returns a tuple of ``(response, buffered_chunks)`` where
        *buffered_chunks* holds the raw token sequence collected during
        streaming (``None`` when not streaming).  Tokens are **never**
        forwarded to *on_token* here — the caller must do so after the
        response has been validated.
        """
        if on_token is not None and hasattr(self._llm, "stream"):
            chunks: list[str] = []
            response: str | LLMResponse = ""
            for item in self._llm.stream(messages, **kwargs):
                if isinstance(item, LLMResponse):
                    response = item
                else:
                    chunks.append(item)
            if not isinstance(response, LLMResponse):
                response = "".join(chunks)
            return response, chunks
        response = self._llm.complete(messages, **kwargs)
        return response, None

    @staticmethod
    def _parse_response(response: str | LLMResponse) -> tuple[str, list]:
        """Extract clean text and tool calls from LLM response."""
        if isinstance(response, LLMResponse):
            return response.content, [tc.to_tool_call() for tc in response.tool_calls]
        return parse_tool_calls(response)

    def _validate_and_fix(
        self,
        clean_text: str,
        buffered_chunks: list[str] | None,
        messages: list[dict],
        kwargs: dict,
    ) -> tuple[str, list[str] | None]:
        """Validate *clean_text* and retry up to ``_MAX_VALIDATION_RETRIES`` times.

        If the LLM returns tool-call syntax leaked as plain text (or an empty
        response), append a reformulation instruction and call the LLM again.
        After all retries are exhausted the generic fallback string is used.

        Returns the validated ``(clean_text, buffered_chunks)`` pair — the
        caller is responsible for flushing *buffered_chunks* to *on_token*.
        """
        if is_valid_response(clean_text):
            return clean_text, buffered_chunks

        retry_messages = list(messages)
        for _ in range(_MAX_VALIDATION_RETRIES):
            retry_messages.append({"role": "assistant", "content": clean_text})
            retry_messages.append({"role": "user", "content": reformulate_message()})
            try:
                retry_response, retry_chunks = self._call_llm_sync(retry_messages, kwargs, None)
            except Exception:
                break
            clean_text, _ = self._parse_response(retry_response)
            if is_valid_response(clean_text):
                # Return single-chunk buffer matching the validated text
                return clean_text, [clean_text] if buffered_chunks is not None else None

        # All retries failed — use fallback
        fb = fallback_text()
        return fb, [fb] if buffered_chunks is not None else None

    @staticmethod
    def _format_assistant_with_calls(text: str, calls: list[ToolCall]) -> str:
        """Format assistant message including tool_call blocks for the loop context."""
        parts = []
        if text:
            parts.append(text)
        for call in calls:
            block = json.dumps({"tool": call.tool_id, "params": call.params}, ensure_ascii=False)
            parts.append(f"```tool_call\n{block}\n```")
        return "\n".join(parts)

    @staticmethod
    def _format_tool_results(results: list[ToolCallResult]) -> str:
        """Format tool results as a user message for the next LLM turn."""
        parts = []
        for r in results:
            if r.success:
                parts.append(f"[Tool result: {r.tool_id}]\n{json.dumps(r.output, default=str, ensure_ascii=False)}")
            else:
                parts.append(f"[Tool error: {r.tool_id}]\n{r.error}")
        return "\n\n".join(parts)

    def _build_messages(self, context: AssembledContext) -> list[dict]:
        messages: list[dict] = []

        # 1. System message
        messages.append({"role": "system", "content": self._build_system(context)})

        # 2. Conversation history from working memory
        if self._working_memory is not None:
            for msg in self._working_memory.recent_messages:
                messages.append({"role": msg.role, "content": msg.content})

        # 3. Conversation history from extra_context (when working_memory is not used)
        #    The host app (e.g. YouNews) can inject "conversation_history" as a
        #    newline-separated list of "Role: content" lines. We promote these to
        #    real user/assistant message pairs so the LLM sees proper multi-turn.
        if self._working_memory is None and context.extra_context:
            history_text = context.extra_context.get("conversation_history")
            if history_text:
                for msg in self._parse_conversation_history(history_text):
                    messages.append(msg)

        # 4. Current user input (with ephemeral runtime context for the LLM)
        user_content = inject_runtime_context(
            context.user_input,
            session_id=context.session_id,
        )
        messages.append({"role": "user", "content": user_content})

        return messages

    @staticmethod
    def _parse_conversation_history(text: str) -> list[dict]:
        """Parse "Role: content" lines into LLM message dicts.

        Expected format (produced by YouNews clark.py):
            Usuário: message text
            Clark: response text

        Consecutive lines with the same role are merged.
        """
        messages: list[dict] = []
        role_map = {
            "usuário": "user", "usuario": "user", "user": "user",
            "clark": "assistant", "assistant": "assistant",
        }
        current_role = None
        current_lines: list[str] = []

        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue

            # Try to parse "Role: content"
            parsed = False
            if ": " in stripped:
                prefix, rest = stripped.split(": ", 1)
                mapped = role_map.get(prefix.lower())
                if mapped:
                    if current_role and current_lines:
                        messages.append({"role": current_role, "content": "\n".join(current_lines)})
                    current_role = mapped
                    current_lines = [rest]
                    parsed = True

            if not parsed and current_role:
                current_lines.append(stripped)

        if current_role and current_lines:
            messages.append({"role": current_role, "content": "\n".join(current_lines)})

        return messages

    def _build_system(self, context: AssembledContext) -> str:
        parts: list[str] = []

        # Persona
        if context.persona:
            parts.append("## Persona")
            parts.append(self._render_persona(context.persona))

        # Available tools (text-based instructions only when NOT using native tools)
        if context.available_tools and not self._native_tools:
            if context.tool_loading == "index":
                parts.append("## Available Tools (Index)")
                parts.append(_TOOL_INSTRUCTIONS)
                parts.append(_INDEX_INSTRUCTIONS)
                for tool in context.available_tools:
                    tool_id = tool.get("tool_id", "")
                    name = tool.get("name", "")
                    desc = tool.get("description", "")
                    params = tool.get("parameters")
                    if params:
                        # get_tool_schema itself — show full params
                        parts.append(f"### {tool_id} — {name}")
                        parts.append(desc)
                        parts.append(f"Parameters: ```json\n{json.dumps(params, indent=2)}\n```")
                    else:
                        # Index entry — compact, no params
                        parts.append(f"- **{tool_id}** — {name}: {desc}")
            else:
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
        # conversation_history is excluded here — it's promoted to real
        # messages in _build_messages() for proper multi-turn LLM context.
        if context.extra_context:
            ctx_items = {
                k: v for k, v in context.extra_context.items()
                if k != "conversation_history"
            }
            if ctx_items:
                parts.append("## Context")
                for key, value in ctx_items.items():
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

    @staticmethod
    def _render_persona(persona: dict) -> str:
        """Render persona dict as natural-language instructions.

        Recognised keys are rendered as structured text; any remaining
        keys are appended as a compact JSON block so nothing is lost.
        """
        known_keys = {"role", "tone", "language", "behavior"}
        lines: list[str] = []

        if role := persona.get("role"):
            lines.append(f"You are: {role}")
        if tone := persona.get("tone"):
            lines.append(f"Tone: {tone}")
        if lang := persona.get("language"):
            lines.append(f"Language: {lang}")
        if behavior := persona.get("behavior"):
            lines.append(f"\n{behavior}")

        # Render any extra keys the host added (custom fields)
        extra = {k: v for k, v in persona.items() if k not in known_keys}
        if extra:
            lines.append(json.dumps(extra, indent=2, default=str, ensure_ascii=False))

        return "\n".join(lines) if lines else json.dumps(persona, indent=2, default=str)
