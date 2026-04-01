"""ChatRunner — conversational runner that delegates to an LLM."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from symbiote.core.context import AssembledContext
from symbiote.core.models import Message
from symbiote.core.ports import LLMPort
from symbiote.environment.descriptors import LLMResponse, ToolCall, ToolCallResult
from symbiote.environment.parser import parse_tool_calls
from symbiote.environment.runtime_context import inject_runtime_context
from symbiote.memory.working import WorkingMemory
from symbiote.runners.base import LoopStep, LoopTrace, RunResult
from symbiote.runners.loop_control import LoopController
from symbiote.runners.response_validator import (
    fallback_text,
    is_valid_response,
    reformulate_message,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from symbiote.environment.tools import ToolGateway
    from symbiote.memory.consolidator import MemoryConsolidator

_HANDLED_INTENTS = frozenset({"chat", "ask", "question", "talk"})
_MAX_TOOL_ITERATIONS = 10
_MAX_VALIDATION_RETRIES = 2
_COMPACTION_THRESHOLD = 4  # compact after this many loop-added message pairs
_CHARS_PER_TOKEN = 4  # rough estimate for token counting
_MICROCOMPACT_MAX_CHARS = 2000  # truncate individual tool results beyond this
_AUTOCOMPACT_THRESHOLD = 0.80  # trigger autocompact at 80% of context budget
_DEFAULT_CONTEXT_BUDGET = 16000  # tokens; overridden by context.total_tokens_estimate
_MAX_LLM_RETRIES = 3
_LLM_RETRY_BASE = 1  # seconds
_LLM_RETRY_MULTIPLIER = 2

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
- CRITICAL — Match intent to action. If the user asks to FIND or LOCATE \
something, use a search/list tool — NEVER use a create/capture tool. \
Creating something is not the same as finding it. If no search tool \
exists for that use case, say so honestly.
- CRITICAL — After receiving a tool result, JUDGE whether it actually \
satisfies what the user asked for. Compare URLs, titles, and key fields \
against the original request. If the result does not match, DO NOT \
present it as a success. Instead, tell the user honestly that you tried \
but could not fulfill the request. Never fabricate or assume a result \
is correct — if it doesn't match, it doesn't match.

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
        context_budget: int = _DEFAULT_CONTEXT_BUDGET,
    ) -> None:
        self._llm = llm
        self._working_memory = working_memory
        self._tool_gateway = tool_gateway
        self._consolidator = consolidator
        self._native_tools = native_tools
        self._context_budget = context_budget

    def can_handle(self, intent: str) -> bool:
        return intent in _HANDLED_INTENTS

    def run(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Run the chat runner synchronously with tool-loop support.

        When ``context.tool_mode`` is ``"brief"`` or ``"continuous"`` the
        runner feeds tool results back to the LLM and re-invokes until the
        LLM responds without tool calls or max iterations is reached.
        ``"instant"`` mode limits to a single shot (no loop).
        """
        messages = self._build_messages(context)
        kwargs = self._build_llm_kwargs(context)
        max_iters = self._resolve_max_iters(context)
        initial_msg_count = len(messages)
        controller = LoopController(
            max_iterations=max_iters,
            stagnation_msg=context.injection_stagnation_override,
            circuit_breaker_msg=context.injection_circuit_breaker_override,
        )

        all_tool_results: list[ToolCallResult] = []
        final_text = ""

        for _ in range(max_iters):
            try:
                response, buffered_chunks = self._call_llm_with_retry(messages, kwargs, on_token)
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
            # When tool calls are present, suppress intermediate narration
            # (e.g. "Vou verificar...") — only the final response should
            # reach the user for a natural conversation experience.
            if on_token is not None and not tool_calls:
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

            # Record each tool call in the loop controller
            for tc, tr in zip(tool_calls, results, strict=False):
                controller.record(tc.tool_id, tc.params, tr.success)

            # Check loop health — stop early on stagnation or circuit breaker
            should_stop, stop_reason = controller.should_stop()
            if should_stop:
                injection = controller.get_injection_message()
                if injection:
                    messages.append({"role": "assistant", "content": self._format_assistant_with_calls(clean_text, tool_calls)})
                    messages.append({"role": "user", "content": self._format_tool_results(results)})
                    messages.append({"role": "user", "content": injection})
                    try:
                        inj_response, inj_chunks = self._call_llm_sync(messages, kwargs, on_token)
                        inj_text, _ = self._parse_response(inj_response)
                        final_text = inj_text
                        if on_token is not None:
                            if inj_chunks is not None:
                                for chunk in inj_chunks:
                                    on_token(chunk)
                            else:
                                on_token(inj_text)
                    except Exception:
                        pass  # keep the last final_text
                break

            # Feed results back for the next iteration
            messages.append({"role": "assistant", "content": self._format_assistant_with_calls(clean_text, tool_calls)})
            messages.append({"role": "user", "content": self._format_tool_results(results)})

            # Layer 2: compact old loop messages to prevent context growth
            self._compact_loop_messages(messages, initial_msg_count)
            # Layer 3: aggressive autocompact if approaching context budget
            self._autocompact_if_needed(messages, initial_msg_count)

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
        max_iters = self._resolve_max_iters(context)
        initial_msg_count = len(messages)
        controller = LoopController(
            max_iterations=max_iters,
            stagnation_msg=context.injection_stagnation_override,
            circuit_breaker_msg=context.injection_circuit_breaker_override,
        )

        all_tool_results: list[ToolCallResult] = []
        trace = LoopTrace()
        final_text = ""
        loop_start = time.monotonic()

        for iteration in range(max_iters):
            try:
                response, buffered_chunks = self._call_llm_with_retry(messages, kwargs, on_token)
            except Exception as exc:
                return RunResult(success=False, error=str(exc), runner_type=self.runner_type, loop_trace=trace)

            clean_text, tool_calls = self._parse_response(response)

            # ── Response validation ───────────────────────────────────────
            if not tool_calls:
                clean_text, buffered_chunks = self._validate_and_fix(
                    clean_text, buffered_chunks, messages, kwargs
                )

            final_text = clean_text

            # Release tokens to the caller after validation.
            if on_token is not None and not tool_calls:
                if buffered_chunks is not None:
                    for chunk in buffered_chunks:
                        on_token(chunk)
                else:
                    on_token(clean_text)

            if not tool_calls or self._tool_gateway is None:
                break

            step_start = time.monotonic()
            results = await self._tool_gateway.execute_tool_calls_async(
                symbiote_id=context.symbiote_id,
                session_id=context.session_id,
                calls=tool_calls,
            )
            step_elapsed = int((time.monotonic() - step_start) * 1000)
            all_tool_results.extend(results)

            # Record trace for each tool call
            for tc, tr in zip(tool_calls, results, strict=False):
                step = LoopStep(
                    iteration=iteration + 1,
                    tool_id=tc.tool_id,
                    params=tc.params,
                    success=tr.success,
                    error=tr.error,
                    elapsed_ms=step_elapsed,
                )
                trace.steps.append(step)
                logger.info(
                    "[tool-loop] iter=%d tool=%s success=%s elapsed=%dms",
                    iteration + 1, tc.tool_id, tr.success, step_elapsed,
                )

                # Record in loop controller
                controller.record(tc.tool_id, tc.params, tr.success)

            # Check loop health — stop early on stagnation or circuit breaker
            should_stop, stop_reason = controller.should_stop()
            if should_stop:
                trace.stop_reason = stop_reason
                injection = controller.get_injection_message()
                if injection:
                    messages.append({"role": "assistant", "content": self._format_assistant_with_calls(clean_text, tool_calls)})
                    messages.append({"role": "user", "content": self._format_tool_results(results)})
                    messages.append({"role": "user", "content": injection})
                    try:
                        inj_response, inj_chunks = self._call_llm_sync(messages, kwargs, on_token)
                        inj_text, _ = self._parse_response(inj_response)
                        final_text = inj_text
                        if on_token is not None:
                            if inj_chunks is not None:
                                for chunk in inj_chunks:
                                    on_token(chunk)
                            else:
                                on_token(inj_text)
                    except Exception:
                        pass  # keep the last final_text
                break

            messages.append({"role": "assistant", "content": self._format_assistant_with_calls(clean_text, tool_calls)})
            messages.append({"role": "user", "content": self._format_tool_results(results)})

            # Layer 2: compact old loop messages to prevent context growth
            self._compact_loop_messages(messages, initial_msg_count)
            # Layer 3: aggressive autocompact if approaching context budget
            self._autocompact_if_needed(messages, initial_msg_count)

        trace.total_iterations = iteration + 1 if max_iters > 1 else 0
        trace.total_tool_calls = len(trace.steps)
        trace.total_elapsed_ms = int((time.monotonic() - loop_start) * 1000)
        if trace.stop_reason is None:
            trace.stop_reason = "end_turn" if trace.total_iterations > 0 else None

        if self._working_memory is not None:
            self._working_memory.update_message(
                Message(session_id=context.session_id, role="assistant", content=final_text)
            )
            if self._consolidator is not None:
                self._consolidator.consolidate_if_needed(self._working_memory, context.symbiote_id)

        output = final_text
        if all_tool_results:
            output = {"text": final_text, "tool_results": [r.model_dump() for r in all_tool_results]}

        if trace.steps:
            logger.info(
                "[tool-loop] completed: %d iterations, %d tool calls, %dms total",
                trace.total_iterations, trace.total_tool_calls, trace.total_elapsed_ms,
            )

        return RunResult(
            success=True, output=output, runner_type=self.runner_type,
            loop_trace=trace if trace.steps else None,
        )

    # ── internal ─────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_max_iters(context: AssembledContext) -> int:
        """Derive max loop iterations from tool_mode, with tool_loop backward compat.

        - ``instant`` mode always yields 1 (no loop).
        - ``brief`` / ``continuous`` yield ``max_tool_iterations``.
        - Backward compat: if ``tool_mode`` is the default "brief" but
          ``tool_loop`` is explicitly False, treat as instant (1 iteration).
        """
        if context.tool_mode == "instant":
            return 1
        # Backward compat: tool_loop=False overrides default brief mode
        if not context.tool_loop:
            return 1
        return context.max_tool_iterations

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
    def _is_retryable_error(exc: Exception) -> bool:
        """Return True if *exc* is a transient error worth retrying."""
        if isinstance(exc, ValueError | TypeError | KeyError):
            return False
        if isinstance(exc, ConnectionError | TimeoutError | OSError):
            return True
        msg = str(exc).lower()
        return any(kw in msg for kw in ("rate limit", "429", "503", "502", "timeout"))

    def _call_llm_with_retry(
        self,
        messages: list[dict],
        kwargs: dict,
        on_token: Callable[[str], None] | None,
    ) -> tuple[str | LLMResponse, list[str] | None]:
        """Wrap ``_call_llm_sync`` with exponential-backoff retry logic."""
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_LLM_RETRIES + 1):
            try:
                return self._call_llm_sync(messages, kwargs, on_token)
            except Exception as exc:
                if not self._is_retryable_error(exc) or attempt == _MAX_LLM_RETRIES:
                    raise
                last_exc = exc
                delay = _LLM_RETRY_BASE * (_LLM_RETRY_MULTIPLIER ** (attempt - 1))
                logger.warning(
                    "[llm-retry] attempt %d/%d after %s: %s",
                    attempt, _MAX_LLM_RETRIES, type(exc).__name__, exc,
                )
                time.sleep(delay)
        # Should never reach here, but satisfy type checker
        raise last_exc  # type: ignore[misc]

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
    def _compact_loop_messages(
        messages: list[dict], initial_count: int
    ) -> None:
        """Replace old tool-loop message pairs with a compact summary.

        Only compacts messages added during the tool loop (after
        ``initial_count``).  Keeps the most recent pair intact so the LLM
        sees the latest tool result.  Older pairs are replaced by a single
        summary message.

        Modifies *messages* in-place.
        """
        loop_messages = messages[initial_count:]
        # Each iteration adds 2 messages (assistant + user/tool_result)
        # Keep the last pair, compact the rest
        if len(loop_messages) < _COMPACTION_THRESHOLD * 2:
            return  # not enough to compact

        pairs_to_compact = loop_messages[:-2]  # all except last pair
        if not pairs_to_compact:
            return

        # Build compact summary from the pairs
        steps: list[str] = []
        for step_num, i in enumerate(range(0, len(pairs_to_compact), 2), 1):
            tool_result_msg = pairs_to_compact[i + 1]["content"] if i + 1 < len(pairs_to_compact) else ""

            # Extract tool_id from tool_result format "[Tool result: xxx]"
            tool_id = "unknown"
            if "[Tool result: " in tool_result_msg:
                tool_id = tool_result_msg.split("[Tool result: ")[1].split("]")[0]
            elif "[Tool error: " in tool_result_msg:
                tool_id = tool_result_msg.split("[Tool error: ")[1].split("]")[0]

            # Truncate large results
            result_preview = tool_result_msg[:200]
            if len(tool_result_msg) > 200:
                result_preview += "... (truncated)"

            steps.append(f"{step_num}) {tool_id} → {result_preview}")

        summary = (
            "[Context compacted — previous tool loop steps summarized]\n"
            "Steps completed so far:\n" + "\n".join(steps) + "\n"
            "Continue from here."
        )

        # Replace compacted pairs with single summary message
        last_pair = messages[-2:]
        del messages[initial_count:]
        messages.append({"role": "user", "content": summary})
        messages.extend(last_pair)

    @staticmethod
    def _microcompact_tool_result(result_text: str) -> str:
        """Truncate a single tool result if it exceeds the size threshold.

        Layer 1 of the 3-layer compaction system.  Applied to each tool
        result *before* it is injected into the message list, preventing
        large JSON payloads from consuming the context window.
        """
        if len(result_text) <= _MICROCOMPACT_MAX_CHARS:
            return result_text
        # Keep first portion + tail marker
        truncated = result_text[:_MICROCOMPACT_MAX_CHARS]
        remaining = len(result_text) - _MICROCOMPACT_MAX_CHARS
        return f"{truncated}\n... ({remaining} chars truncated)"

    def _autocompact_if_needed(
        self, messages: list[dict], initial_count: int
    ) -> bool:
        """Aggressively compact when total tokens approach context budget.

        Layer 3 of the 3-layer compaction system.  Estimates the total
        token count of all messages and, when it exceeds
        ``_AUTOCOMPACT_THRESHOLD`` of the budget, replaces ALL loop
        messages (not just old ones) with a single summary.

        Returns True if compaction was performed.
        """
        total_chars = sum(len(m.get("content", "")) for m in messages)
        estimated_tokens = total_chars // _CHARS_PER_TOKEN
        threshold = int(self._context_budget * _AUTOCOMPACT_THRESHOLD)

        if estimated_tokens <= threshold:
            return False

        loop_messages = messages[initial_count:]
        if len(loop_messages) < 2:
            return False

        logger.info(
            "[autocompact] tokens ~%d exceed threshold %d; compacting %d loop messages",
            estimated_tokens, threshold, len(loop_messages),
        )

        # Build aggressive summary — keep only tool_id and success/error
        steps: list[str] = []
        for step_num, i in enumerate(range(0, len(loop_messages), 2), 1):
            tool_result_msg = loop_messages[i + 1]["content"] if i + 1 < len(loop_messages) else ""

            tool_id = "unknown"
            status = "ok"
            if "[Tool result: " in tool_result_msg:
                tool_id = tool_result_msg.split("[Tool result: ")[1].split("]")[0]
            elif "[Tool error: " in tool_result_msg:
                tool_id = tool_result_msg.split("[Tool error: ")[1].split("]")[0]
                status = "error"

            # Ultra-compact: just tool_id and status, no content
            steps.append(f"{step_num}) {tool_id} → {status}")

        summary = (
            "[Autocompact — context budget pressure, all loop steps summarized]\n"
            "Steps completed:\n" + "\n".join(steps) + "\n"
            "Continue from here. Do not repeat completed steps."
        )

        del messages[initial_count:]
        messages.append({"role": "user", "content": summary})
        return True

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

    @classmethod
    def _format_tool_results(cls, results: list[ToolCallResult]) -> str:
        """Format tool results as a user message for the next LLM turn.

        Applies Layer 1 microcompaction to each individual result.
        """
        parts = []
        for r in results:
            if r.success:
                raw = f"[Tool result: {r.tool_id}]\n{json.dumps(r.output, default=str, ensure_ascii=False)}"
            else:
                raw = f"[Tool error: {r.tool_id}]\n{r.error}"
            parts.append(cls._microcompact_tool_result(raw))
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
            effective_tool_instructions = context.tool_instructions_override or _TOOL_INSTRUCTIONS
            if context.tool_loading == "index":
                parts.append("## Available Tools (Index)")
                parts.append(effective_tool_instructions)
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
                parts.append(effective_tool_instructions)
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
