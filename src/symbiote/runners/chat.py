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
- CRITICAL — After completing a tool call and receiving its result, \
evaluate whether the user's FULL request has been satisfied. If the \
request involves multiple steps (e.g. "list X and then email Y"), \
continue executing the remaining steps. Only provide your final \
response when ALL parts of the request have been addressed.

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
        on_before_tool_call: Callable[[str, dict, str], bool] | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_stream: Callable[[str, int], None] | None = None,
    ) -> None:
        self._llm = llm
        self._working_memory = working_memory
        self._tool_gateway = tool_gateway
        self._consolidator = consolidator
        self._native_tools = native_tools
        self._context_budget = context_budget
        self._on_before_tool_call = on_before_tool_call
        self._on_progress = on_progress
        self._on_stream = on_stream

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

        When ``context.tool_mode == "instant"``, a streamlined fast-path is
        used: single LLM call, optional single tool execution, no loop
        controller, no compaction, no progress callbacks.
        """
        if context.tool_mode == "instant":
            return self._run_instant(context, on_token)
        return self._run_loop(context, on_token)

    def _run_instant(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Fast-path for instant mode: single LLM call + optional tool exec.

        Skips: LoopController, schema cache, compaction (all 3 layers),
        loop summary in working memory, on_progress callbacks.
        Keeps: LLM retry, tool execution, approval gate, response
        validation, on_stream, on_token.
        """
        messages = self._build_messages(context)
        kwargs = self._build_llm_kwargs(context)
        loop_start = time.monotonic()

        # Single LLM call with retry
        try:
            response, buffered_chunks = self._call_llm_with_retry(messages, kwargs, on_token)
        except Exception as exc:
            return RunResult(success=False, error=str(exc), runner_type=self.runner_type)

        clean_text, tool_calls = self._parse_response(response)

        if not tool_calls:
            clean_text, buffered_chunks = self._validate_and_fix(
                clean_text, buffered_chunks, messages, kwargs
            )

        # Emit via on_stream
        if self._on_stream is not None and clean_text:
            self._on_stream(clean_text, 1)

        all_tool_results: list[ToolCallResult] = []

        # Execute tool calls if any (at most 1 iteration worth)
        if tool_calls and self._tool_gateway is not None:
            # Approval gate (kept — a single tool call can still be high-risk)
            approved_calls, denial_results = self._check_approval(tool_calls)
            all_tool_results.extend(denial_results)

            gateway_results: list[ToolCallResult] = list(denial_results)
            if approved_calls:
                exec_results = self._tool_gateway.execute_tool_calls(
                    symbiote_id=context.symbiote_id,
                    session_id=context.session_id,
                    calls=approved_calls,
                    timeout=context.tool_call_timeout,
                )
                gateway_results.extend(exec_results)
                all_tool_results.extend(exec_results)

            # Feed results back for a final LLM response
            messages.append({"role": "assistant", "content": self._format_assistant_with_calls(clean_text, tool_calls)})
            results = self._merge_tool_results(tool_calls, {}, gateway_results)
            messages.append({"role": "user", "content": self._format_tool_results(results)})

            try:
                final_response, buffered_chunks = self._call_llm_with_retry(messages, kwargs, on_token)
                clean_text, _ = self._parse_response(final_response)
            except Exception:
                pass  # keep the tool-call text as final

        # Release tokens — no suppression since there's no next iteration
        if on_token is not None:
            if buffered_chunks is not None:
                for chunk in buffered_chunks:
                    on_token(chunk)
            else:
                on_token(clean_text)

        # Save to working memory (no loop summary — nothing to summarize)
        if self._working_memory is not None:
            self._working_memory.update_message(
                Message(session_id=context.session_id, role="assistant", content=clean_text)
            )
            if self._consolidator is not None:
                self._consolidator.consolidate_if_needed(self._working_memory, context.symbiote_id)

        # Build trace for scoring/persistence
        elapsed_ms = int((time.monotonic() - loop_start) * 1000)
        trace_steps = [
            LoopStep(
                iteration=1,
                tool_id=tc.tool_id,
                params=tc.params,
                success=tr.success,
                error=tr.error,
                elapsed_ms=tr.elapsed_ms if hasattr(tr, "elapsed_ms") else 0,
            )
            for tc, tr in zip(tool_calls, all_tool_results, strict=False)
        ] if tool_calls else []
        trace = LoopTrace(
            steps=trace_steps,
            total_iterations=1,
            total_tool_calls=len(all_tool_results),
            total_elapsed_ms=elapsed_ms,
            stop_reason="end_turn",
            tool_mode="instant",
        )

        output = clean_text
        if all_tool_results:
            output = {"text": clean_text, "tool_results": [r.model_dump() for r in all_tool_results]}

        return RunResult(success=True, output=output, runner_type=self.runner_type, loop_trace=trace)

    def _run_loop(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Full loop execution for brief/continuous modes."""
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
        schema_cache: dict[str, dict] = {}  # loop-local cache for index mode
        trace = LoopTrace(tool_mode=context.tool_mode)
        final_text = ""
        loop_start = time.monotonic()
        loop_timeout = context.loop_timeout
        last_iteration = 0

        for iteration in range(max_iters):
            last_iteration = iteration
            # Check loop timeout before each iteration
            if time.monotonic() - loop_start > loop_timeout:
                logger.info("[tool-loop] loop timeout exceeded (%.1fs)", loop_timeout)
                trace.stop_reason = "timeout"
                break

            if self._on_progress is not None:
                self._on_progress("iteration_start", iteration + 1, max_iters)

            try:
                response, buffered_chunks = self._call_llm_with_retry(messages, kwargs, on_token)
            except Exception as exc:
                return RunResult(success=False, error=str(exc), runner_type=self.runner_type, loop_trace=trace)

            clean_text, tool_calls = self._parse_response(response)

            # ── Response validation ───────────────────────────────────────
            # Only validate when there are no tool calls (a tool-call
            # response is intentionally structured and not meant for the user).
            if not tool_calls:
                clean_text, buffered_chunks = self._validate_and_fix(
                    clean_text, buffered_chunks, messages, kwargs
                )

            final_text = clean_text

            # Emit intermediate text via on_stream for ALL iterations
            if self._on_stream is not None and clean_text:
                self._on_stream(clean_text, iteration + 1)

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
                if self._on_progress is not None:
                    self._on_progress("iteration_end", iteration + 1, max_iters)
                break

            # Schema cache: intercept repeated get_tool_schema in index mode
            cached_results, remaining_calls = self._split_cached_schema_calls(
                tool_calls, schema_cache, context.tool_loading,
            )
            all_tool_results.extend(cached_results.values())

            # Approval gate: check high-risk tools before execution
            approved_calls, denial_results = self._check_approval(remaining_calls)
            all_tool_results.extend(denial_results)

            if self._on_progress is not None:
                self._on_progress("tool_start", iteration + 1, max_iters)

            step_start = time.monotonic()
            gateway_results: list[ToolCallResult] = list(denial_results)
            if approved_calls:
                exec_results = self._tool_gateway.execute_tool_calls(
                    symbiote_id=context.symbiote_id,
                    session_id=context.session_id,
                    calls=approved_calls,
                    timeout=context.tool_call_timeout,
                )
                gateway_results.extend(exec_results)
                all_tool_results.extend(exec_results)
            step_elapsed = int((time.monotonic() - step_start) * 1000)

            if self._on_progress is not None:
                self._on_progress("tool_end", iteration + 1, max_iters)

            # Update schema cache with new results
            self._update_schema_cache(remaining_calls, gateway_results, schema_cache)

            # Merge cached + gateway results in original order
            results = self._merge_tool_results(tool_calls, cached_results, gateway_results)

            # Record each tool call in trace and loop controller
            for tc, tr in zip(tool_calls, results, strict=False):
                trace.steps.append(LoopStep(
                    iteration=iteration + 1,
                    tool_id=tc.tool_id,
                    params=tc.params,
                    success=tr.success,
                    error=tr.error,
                    elapsed_ms=step_elapsed,
                ))
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

            # Feed results back for the next iteration
            messages.append({"role": "assistant", "content": self._format_assistant_with_calls(clean_text, tool_calls)})
            messages.append({"role": "user", "content": self._format_tool_results(results)})

            # Layer 2: compact old loop messages to prevent context growth
            self._compact_loop_messages(messages, initial_msg_count)
            # Layer 3: aggressive autocompact if approaching context budget
            self._autocompact_if_needed(messages, initial_msg_count)

            if self._on_progress is not None:
                self._on_progress("iteration_end", iteration + 1, max_iters)

        # Finalize trace
        trace.total_iterations = last_iteration + 1 if context.tool_loop else 0
        trace.total_tool_calls = len(trace.steps)
        trace.total_elapsed_ms = int((time.monotonic() - loop_start) * 1000)
        if trace.stop_reason is None:
            trace.stop_reason = "end_turn" if trace.total_iterations > 0 else None

        # Save final response (with loop summary) to working memory
        if self._working_memory is not None:
            if all_tool_results:
                summary = self._build_loop_summary(all_tool_results)
                memory_text = f"{summary}\n\n{final_text}"
            else:
                memory_text = final_text
            self._working_memory.update_message(
                Message(session_id=context.session_id, role="assistant", content=memory_text)
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

    async def run_async(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Async variant of run() with tool-loop support."""
        # Instant mode uses sync fast-path (no async tool execution needed
        # for a single call) — avoids duplicating the instant logic.
        if context.tool_mode == "instant":
            return self._run_instant(context, on_token)
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
        schema_cache: dict[str, dict] = {}  # loop-local cache for index mode
        trace = LoopTrace(tool_mode=context.tool_mode)
        final_text = ""
        loop_start = time.monotonic()
        loop_timeout = context.loop_timeout

        for iteration in range(max_iters):
            # Check loop timeout before each iteration
            if time.monotonic() - loop_start > loop_timeout:
                logger.info("[tool-loop] loop timeout exceeded (%.1fs)", loop_timeout)
                trace.stop_reason = "timeout"
                break

            if self._on_progress is not None:
                self._on_progress("iteration_start", iteration + 1, max_iters)

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

            # Emit intermediate text via on_stream for ALL iterations
            if self._on_stream is not None and clean_text:
                self._on_stream(clean_text, iteration + 1)

            # Release tokens to the caller after validation.
            if on_token is not None and not tool_calls:
                if buffered_chunks is not None:
                    for chunk in buffered_chunks:
                        on_token(chunk)
                else:
                    on_token(clean_text)

            if not tool_calls or self._tool_gateway is None:
                if self._on_progress is not None:
                    self._on_progress("iteration_end", iteration + 1, max_iters)
                break

            # Schema cache: intercept repeated get_tool_schema in index mode
            cached_results, remaining_calls = self._split_cached_schema_calls(
                tool_calls, schema_cache, context.tool_loading,
            )
            all_tool_results.extend(cached_results.values())

            # Approval gate: check high-risk tools before execution
            approved_calls, denial_results = self._check_approval(remaining_calls)
            all_tool_results.extend(denial_results)

            if self._on_progress is not None:
                self._on_progress("tool_start", iteration + 1, max_iters)

            step_start = time.monotonic()
            gateway_results: list[ToolCallResult] = list(denial_results)
            if approved_calls:
                exec_results = await self._tool_gateway.execute_tool_calls_async(
                    symbiote_id=context.symbiote_id,
                    session_id=context.session_id,
                    calls=approved_calls,
                    timeout=context.tool_call_timeout,
                )
                gateway_results.extend(exec_results)
                all_tool_results.extend(exec_results)
            step_elapsed = int((time.monotonic() - step_start) * 1000)

            if self._on_progress is not None:
                self._on_progress("tool_end", iteration + 1, max_iters)

            # Update schema cache with new results
            self._update_schema_cache(remaining_calls, gateway_results, schema_cache)

            # Merge cached + gateway results in original order
            results = self._merge_tool_results(tool_calls, cached_results, gateway_results)

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

            if self._on_progress is not None:
                self._on_progress("iteration_end", iteration + 1, max_iters)

        trace.total_iterations = iteration + 1 if context.tool_loop else 0
        trace.total_tool_calls = len(trace.steps)
        trace.total_elapsed_ms = int((time.monotonic() - loop_start) * 1000)
        if trace.stop_reason is None:
            trace.stop_reason = "end_turn" if trace.total_iterations > 0 else None

        if self._working_memory is not None:
            if all_tool_results:
                summary = self._build_loop_summary(all_tool_results)
                memory_text = f"{summary}\n\n{final_text}"
            else:
                memory_text = final_text
            self._working_memory.update_message(
                Message(session_id=context.session_id, role="assistant", content=memory_text)
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

    def _check_approval(
        self,
        calls: list[ToolCall],
    ) -> tuple[list[ToolCall], list[ToolCallResult]]:
        """Check approval for tool calls based on risk_level.

        Returns a tuple of (approved_calls, denial_results).
        High-risk tools require approval via the on_before_tool_call callback.
        If the callback is None, all tools are auto-approved (backward compat).
        """
        if self._on_before_tool_call is None or self._tool_gateway is None:
            return calls, []

        approved: list[ToolCall] = []
        denied: list[ToolCallResult] = []

        for call in calls:
            risk = self._tool_gateway.get_risk_level(call.tool_id)
            if risk == "high" and not self._on_before_tool_call(call.tool_id, call.params, risk):
                denied.append(ToolCallResult(
                    tool_id=call.tool_id,
                    success=False,
                    error="Tool call denied by approval callback",
                    risk_level=risk,
                ))
                continue
            approved.append(call)

        return approved, denied

    # ── Loop summary for working memory ─────────────────────────────────

    @staticmethod
    def _build_loop_summary(tool_results: list[ToolCallResult]) -> str:
        """Build a compact summary of tool loop execution for working memory."""
        if not tool_results:
            return ""
        lines = [f"[Loop summary: {len(tool_results)} tool calls]"]
        for i, r in enumerate(tool_results, 1):
            status = "ok" if r.success else f"error: {(r.error or '')[:50]}"
            lines.append(f"{i}) {r.tool_id} \u2192 {status}")
        return "\n".join(lines)

    # ── Tool mode resolution ───────────────────────────────────────────

    @staticmethod
    def _resolve_max_iters(context: AssembledContext) -> int:
        """Derive max loop iterations from tool_mode, with tool_loop backward compat."""
        if context.tool_mode == "instant":
            return 1
        if not context.tool_loop:
            return 1
        return context.max_tool_iterations

    # ── Index mode schema cache ────────────────────────────────────────

    @staticmethod
    def _split_cached_schema_calls(
        tool_calls: list[ToolCall],
        schema_cache: dict[str, dict],
        tool_loading: str,
    ) -> tuple[dict[int, ToolCallResult], list[ToolCall]]:
        """Intercept get_tool_schema calls that are already cached."""
        if tool_loading != "index":
            return {}, list(tool_calls)

        cached: dict[int, ToolCallResult] = {}
        remaining: list[ToolCall] = []
        for idx, call in enumerate(tool_calls):
            if call.tool_id == "get_tool_schema":
                lookup_id = call.params.get("tool_id", "")
                if lookup_id in schema_cache:
                    cached[idx] = ToolCallResult(
                        tool_id="get_tool_schema",
                        success=True,
                        output=schema_cache[lookup_id],
                    )
                    logger.debug("[schema-cache] hit for %s", lookup_id)
                    continue
            remaining.append(call)
        return cached, remaining

    @staticmethod
    def _update_schema_cache(
        remaining_calls: list[ToolCall],
        gateway_results: list[ToolCallResult],
        schema_cache: dict[str, dict],
    ) -> None:
        """Cache successful get_tool_schema results from the gateway."""
        for call, result in zip(remaining_calls, gateway_results, strict=False):
            if call.tool_id == "get_tool_schema" and result.success:
                lookup_id = call.params.get("tool_id", "")
                if lookup_id:
                    schema_cache[lookup_id] = result.output

    @staticmethod
    def _merge_tool_results(
        original_calls: list[ToolCall],
        cached: dict[int, ToolCallResult],
        gateway_results: list[ToolCallResult],
    ) -> list[ToolCallResult]:
        """Merge cached and gateway results in the original call order."""
        if not cached:
            return list(gateway_results)

        merged: list[ToolCallResult] = []
        gw_iter = iter(gateway_results)
        for idx in range(len(original_calls)):
            if idx in cached:
                merged.append(cached[idx])
            else:
                merged.append(next(gw_iter))
        return merged

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

        # On-demand context mode instruction
        if context.context_mode == "on_demand" and not context.relevant_memories and not context.relevant_knowledge:
            parts.append(
                "## Context Access\n"
                "You have access to search_memories and search_knowledge tools. "
                "Use them when you need historical context or domain knowledge."
            )

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
