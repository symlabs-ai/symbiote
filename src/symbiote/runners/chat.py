"""ChatRunner — conversational runner that delegates to an LLM."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from symbiote.agent_protocols import (
    AgentProtocol,
    AgentProtocolError,
    AgentProtocolRegistry,
    PreparedTurn,
    ProtocolState,
    create_default_protocol_registry,
)
from symbiote.agent_protocols.base import (
    compact_tool_result,
    format_legacy_assistant,
    format_legacy_tool_results,
)
from symbiote.core.context import AssembledContext
from symbiote.core.exceptions import is_transient_llm_error
from symbiote.core.models import Message
from symbiote.core.ports import LLMPort
from symbiote.environment.descriptors import LLMResponse, ToolCall, ToolCallResult
from symbiote.environment.parser import parse_tool_calls
from symbiote.environment.runtime_context import inject_runtime_context
from symbiote.memory.working import WorkingMemory
from symbiote.runners.base import (
    AgentProtocolAudit,
    LoopStep,
    LoopTrace,
    RunResult,
)
from symbiote.runners.loop_control import LoopController
from symbiote.runners.response_validator import (
    fallback_text,
    has_raw_tool_fence,
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
# Loop context budget (tokens) used by Layer 3 autocompact. The kernel passes
# KernelConfig.loop_context_budget; this is the default for hosts that build
# a ChatRunner directly.
_DEFAULT_CONTEXT_BUDGET = 16000
# After autocompact, a fresh tool result is never cut below this many chars.
_MIN_RESULT_CHARS_AFTER_AUTOCOMPACT = 2000
_MAX_LLM_RETRIES = 3
# User-facing texts when the loop ends without a model answer (same language
# as response_validator's fallback).
_LOOP_TIMEOUT_TEXT = (
    "Não consegui concluir a tarefa dentro do tempo limite. "
    "Posso continuar de onde parei se você pedir."
)
_NO_FINAL_ANSWER_TEXT = (
    "Não consegui chegar a uma resposta final com as ferramentas disponíveis."
)
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


_COMPACTED_PREFIX = "[Context compacted — previous tool loop steps summarized]"
_AUTOCOMPACTED_PREFIX = "[Autocompact — context budget pressure, earlier loop steps summarized]"
_STEP_LINE = re.compile(r"^\d+\) (.+)$")


def _summary_steps(message: dict) -> list[str] | None:
    """Step descriptions held by a compaction summary message, else None."""
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, str):
        return None
    if not content.startswith((_COMPACTED_PREFIX, _AUTOCOMPACTED_PREFIX)):
        return None
    steps = []
    for line in content.splitlines():
        match = _STEP_LINE.match(line)
        if match:
            steps.append(match.group(1))
    return steps


def _on_loop_thread(
    loop: asyncio.AbstractEventLoop, callback: Callable[[str], None] | None
) -> Callable[[str], None] | None:
    """Wrap ``callback`` so calls from a worker thread run on ``loop``.

    ``call_soon_threadsafe`` keeps the order, and the callbacks queued by the
    worker run before the coroutine awaiting that worker resumes.
    """
    if callback is None:
        return None

    def forward(token: str) -> None:
        loop.call_soon_threadsafe(callback, token)

    return forward


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
        tool_result_max_chars: int = _MICROCOMPACT_MAX_CHARS,
        on_before_tool_call: Callable[[str, dict, str], bool] | None = None,
        on_after_tool_result: Callable[[list[ToolCall], list[ToolCallResult]], str | None] | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_stream: Callable[[str, int], None] | None = None,
        # Sprint 4 — optional. When provided AND a per-symbiote
        # skill_review_enabled=True, the runner fires the engine every N
        # tool iterations from inside the loop. Default None preserves
        # backward-compat with hosts (you_news, sym_talk_lt) that
        # instantiate ChatRunner directly.
        background_review: object | None = None,
        skill_nudge_interval: int = 10,
        agent_protocol: str | AgentProtocol | None = None,
        protocol_registry: AgentProtocolRegistry | None = None,
    ) -> None:
        self._llm = llm
        self._working_memory = working_memory
        self._tool_gateway = tool_gateway
        self._consolidator = consolidator
        self._native_tools = native_tools
        self._context_budget = context_budget
        self._tool_result_max_chars = max(1, int(tool_result_max_chars))
        self._on_before_tool_call = on_before_tool_call
        self._on_after_tool_result = on_after_tool_result
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._background_review = background_review
        self._skill_nudge_interval = max(1, int(skill_nudge_interval))
        self._iters_since_skill_review = 0
        self._protocol_registry = (
            protocol_registry or create_default_protocol_registry()
        )
        self._agent_protocol = self._protocol_registry.resolve(agent_protocol)

    def can_handle(self, intent: str) -> bool:
        return intent in _HANDLED_INTENTS

    @property
    def agent_protocol_id(self) -> str:
        """Return the exact protocol ID selected for subsequent turns."""
        return self._agent_protocol.protocol_id

    def set_approval_callback(
        self, callback: Callable[[str, dict, str], bool] | None
    ) -> None:
        """Gate for high-risk tools: ``callback(tool_id, params, risk_level)``
        returns True to allow. ``None`` removes the gate."""
        self._on_before_tool_call = callback

    def set_progress_callbacks(
        self,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_stream: Callable[[str, int], None] | None = None,
    ) -> None:
        """Install (or clear, with ``None``) the mid-loop callbacks.

        ``on_progress(event, iteration, max_iterations)`` receives
        ``iteration_start``, ``tool_start``, ``tool_end`` and ``iteration_end``;
        ``on_stream(text, iteration)`` receives the parsed text of every model
        reply, including the ones that only request tools.
        """
        self._on_progress = on_progress
        self._on_stream = on_stream

    def set_native_tools(self, enabled: bool) -> None:
        """Use the provider's structured tool calls instead of text parsing."""
        self._native_tools = enabled

    def set_agent_protocol(
        self,
        protocol: str | AgentProtocol | None,
    ) -> None:
        """Select an explicit protocol, with ``None`` restoring legacy.

        Prefer constructor injection. This mutates the shared runner and is
        intended for setup before serving traffic, not concurrent active runs.
        """
        self._agent_protocol = self._protocol_registry.resolve(protocol)

    def _effective_native_tools(self) -> bool:
        """Return whether the selected protocol requires native tool wiring."""
        protocol = getattr(self, "_agent_protocol", None)
        return (
            self._native_tools
            or (
                protocol is not None
                and protocol.capabilities.native_tools_required
            )
        )

    def _prepare_protocol_turn(
        self,
        context: AssembledContext,
        llm_config: dict | None,
    ) -> PreparedTurn:
        """Prepare one isolated protocol state for the whole runner call."""
        state = self._agent_protocol.create_state()
        try:
            return self._agent_protocol.prepare_turn(
                context,
                host_overrides=llm_config,
                native_tools=self._native_tools,
                state=state,
            )
        except AgentProtocolError:
            self._log_protocol_failure(
                code="prepare_failed",
                protocol_id=state.protocol_id,
            )
            raise

    def _validate_protocol_response(
        self,
        response: str | LLMResponse,
        calls: list[ToolCall],
        state: ProtocolState,
    ) -> None:
        """Validate a model response before any tool or callback side effect."""
        try:
            self._agent_protocol.validate_response(response, calls, state)
        except AgentProtocolError:
            self._log_protocol_failure(
                code="response_rejected",
                protocol_id=state.protocol_id,
            )
            raise

    @staticmethod
    def _protocol_audit(prepared: PreparedTurn) -> AgentProtocolAudit:
        """Project ephemeral protocol state into a safe audit envelope."""
        schema_strategy = prepared.state.metadata.get("schema_strategy")
        return AgentProtocolAudit(
            protocol_id=prepared.protocol_id,
            profile=prepared.state.profile,
            schema_strategy=(
                schema_strategy
                if isinstance(schema_strategy, str)
                else None
            ),
            native_tools=prepared.native_tools,
            exchange_count=prepared.state.exchange_count,
        )

    @classmethod
    def _complete_protocol_run(
        cls,
        result: RunResult,
        prepared: PreparedTurn,
    ) -> RunResult:
        """Attach and log only the canonical, non-sensitive audit envelope."""
        audit = cls._protocol_audit(prepared)
        result.agent_protocol = audit
        logger.info(
            "agent_protocol_completed protocol_id=%s profile=%s "
            "schema_strategy=%s native_tools=%s exchange_count=%d",
            audit.protocol_id,
            audit.profile,
            audit.schema_strategy,
            audit.native_tools,
            audit.exchange_count,
        )
        return result

    @staticmethod
    def _log_protocol_failure(*, code: str, protocol_id: str) -> None:
        """Log a fail-closed protocol error without response payloads."""
        logger.warning(
            "agent_protocol_failed code=%s protocol_id=%s",
            code,
            protocol_id,
        )

    def run(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
    ) -> RunResult:
        """Run the chat runner synchronously with tool-loop support.

        When ``context.tool_loop`` is True (default), the runner feeds tool
        results back to the LLM and re-invokes it until the LLM responds
        without tool calls or ``_MAX_TOOL_ITERATIONS`` is reached.

        When ``context.tool_mode == "instant"``, a streamlined fast-path is
        used: single LLM call, optional single tool execution, no loop
        controller, no compaction, no progress callbacks.

        ``llm_config`` is merged into the ``config`` kwarg passed to
        ``self._llm.stream(...)`` — useful for per-call overrides like
        ``{"mode": "high"}`` (effort escalation) without mutating session
        or runner state.
        """
        if context.tool_mode == "instant":
            return self._run_instant(context, on_token, llm_config=llm_config)
        return self._run_loop(context, on_token, llm_config=llm_config)

    def _run_instant(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
    ) -> RunResult:
        """Fast-path for instant mode: single LLM call + optional tool exec.

        Skips: LoopController, schema cache, compaction (all 3 layers),
        loop summary in working memory, on_progress callbacks.
        Keeps: LLM retry, tool execution, approval gate, response
        validation, on_stream, on_token.
        """
        prepared = self._prepare_protocol_turn(context, llm_config)
        protocol_state = prepared.state
        messages = self._build_messages(
            context,
            native_tools=prepared.native_tools,
        )
        kwargs = prepared.to_llm_kwargs()
        loop_start = time.monotonic()

        # Single LLM call with retry
        try:
            response, buffered_chunks = self._call_llm_with_retry(messages, kwargs, on_token)
        except Exception as exc:
            return self._complete_protocol_run(
                RunResult(
                    success=False,
                    error=str(exc),
                    runner_type=self.runner_type,
                ),
                prepared,
            )

        clean_text, tool_calls = self._parse_response(response)
        self._validate_protocol_response(
            response,
            tool_calls,
            protocol_state,
        )

        if not tool_calls:
            clean_text, buffered_chunks = self._validate_and_fix(
                clean_text,
                buffered_chunks,
                messages,
                kwargs,
                protocol_state=protocol_state,
            )

        # Emit via on_stream
        if self._on_stream is not None and clean_text:
            self._on_stream(clean_text, 1)

        all_tool_results: list[ToolCallResult] = []

        # Execute tool calls if any (at most 1 iteration worth)
        if tool_calls and self._tool_gateway is not None:
            # Approval gate (kept — a single tool call can still be high-risk)
            approved_calls, denial_results = self._check_approval(tool_calls)

            gateway_results: list[ToolCallResult] = []
            if approved_calls:
                exec_results = self._tool_gateway.execute_tool_calls(
                    symbiote_id=context.symbiote_id,
                    session_id=context.session_id,
                    calls=approved_calls,
                    timeout=context.tool_call_timeout,
                )
                exec_results = self._correlate_tool_results(
                    approved_calls,
                    exec_results,
                )
                gateway_results.extend(exec_results)

            results = self._merge_tool_results(
                tool_calls,
                denial_results,
                gateway_results,
            )
            all_tool_results.extend(results)

            # Hook: let the caller decide whether tool results end the loop.
            hook_text: str | None = None
            if self._on_after_tool_result is not None:
                hook_text = self._on_after_tool_result(tool_calls, results)

            if hook_text is not None:
                clean_text = hook_text
            elif all(tr.success for tr in all_tool_results):
                # Default: skip final LLM round-trip on success when no hook.
                if not clean_text.strip():
                    clean_text = "Pronto."
            else:
                # Feed results back for a final LLM response only on failure
                self._append_tool_exchange(
                    messages,
                    response,
                    clean_text,
                    tool_calls,
                    results,
                    protocol_state=protocol_state,
                )

                try:
                    final_response, buffered_chunks = self._call_llm_with_retry(messages, kwargs, on_token)
                except Exception:
                    pass  # keep the tool-call text as final
                else:
                    clean_text, final_calls = self._parse_response(final_response)
                    self._validate_protocol_response(
                        final_response,
                        final_calls,
                        protocol_state,
                    )

        # Release tokens — no suppression since there's no next iteration.
        # The buffered chunks are the raw stream of the last model call; when
        # that call carried a tool_call block (and no final call replaced
        # it), they differ from the clean text, so the clean text is sent.
        if on_token is not None:
            if buffered_chunks is not None and "".join(buffered_chunks).strip() == clean_text.strip():
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

        output: str | dict[str, Any] = clean_text
        if all_tool_results:
            output = {"text": clean_text, "tool_results": [r.model_dump() for r in all_tool_results]}

        return self._complete_protocol_run(
            RunResult(
                success=True,
                output=output,
                runner_type=self.runner_type,
                loop_trace=trace,
            ),
            prepared,
        )

    def _run_loop(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
    ) -> RunResult:
        """Full loop execution for brief/continuous modes."""
        prepared = self._prepare_protocol_turn(context, llm_config)
        protocol_state = prepared.state
        messages = self._build_messages(
            context,
            native_tools=prepared.native_tools,
        )
        kwargs = prepared.to_llm_kwargs()
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
        rounds = 0  # model rounds actually executed (LLM calls in the loop)

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
                # Totals are normally filled after the loop; fill them here
                # so a failed turn still records what it did.
                trace.total_iterations = rounds
                trace.total_tool_calls = len(trace.steps)
                trace.total_elapsed_ms = int((time.monotonic() - loop_start) * 1000)
                trace.stop_reason = trace.stop_reason or "error"
                return self._complete_protocol_run(
                    RunResult(
                        success=False,
                        error=str(exc),
                        runner_type=self.runner_type,
                        loop_trace=trace,
                    ),
                    prepared,
                )
            rounds += 1

            clean_text, tool_calls = self._parse_response(response)
            self._validate_protocol_response(
                response,
                tool_calls,
                protocol_state,
            )

            # ── Response validation ───────────────────────────────────────
            # Only validate when there are no tool calls (a tool-call
            # response is intentionally structured and not meant for the user).
            if not tool_calls:
                clean_text, buffered_chunks = self._validate_and_fix(
                    clean_text,
                    buffered_chunks,
                    messages,
                    kwargs,
                    protocol_state=protocol_state,
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

            # Approval gate: check high-risk tools before execution
            approved_calls, denial_results = self._check_approval(remaining_calls)

            if self._on_progress is not None:
                self._on_progress("tool_start", iteration + 1, max_iters)

            step_start = time.monotonic()
            gateway_results: list[ToolCallResult] = []
            if approved_calls:
                exec_results = self._tool_gateway.execute_tool_calls(
                    symbiote_id=context.symbiote_id,
                    session_id=context.session_id,
                    calls=approved_calls,
                    timeout=context.tool_call_timeout,
                )
                exec_results = self._correlate_tool_results(
                    approved_calls,
                    exec_results,
                )
                gateway_results.extend(exec_results)
            step_elapsed = int((time.monotonic() - step_start) * 1000)

            if self._on_progress is not None:
                self._on_progress("tool_end", iteration + 1, max_iters)

            remaining_results = self._merge_tool_results(
                remaining_calls,
                denial_results,
                gateway_results,
            )

            # Update schema cache with new results
            self._update_schema_cache(
                remaining_calls,
                remaining_results,
                schema_cache,
            )

            # Merge cached + gateway results in original order
            results = self._merge_tool_results(
                tool_calls,
                cached_results,
                remaining_results,
            )
            all_tool_results.extend(results)

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
            controller.end_round()

            # Hook: let the caller decide whether tool results end the loop.
            # Returns a string to use as final text, or None to continue.
            if self._on_after_tool_result is not None:
                hook_text = self._on_after_tool_result(tool_calls, results)
                if hook_text is not None:
                    trace.stop_reason = "on_after_tool_result"
                    final_text = hook_text
                    if self._on_progress is not None:
                        self._on_progress("iteration_end", iteration + 1, max_iters)
                    break

            # Skill review nudge — fire a daemon review thread every N tool
            # iterations when the host has wired one. Non-blocking; the
            # engine handles its own errors. Sprint 4.
            self._maybe_nudge_skill_review(context)

            # Check loop health — stop early on stagnation or circuit breaker
            should_stop, stop_reason = controller.should_stop()
            if should_stop:
                trace.stop_reason = stop_reason
                injection = controller.get_injection_message()
                if stop_reason == "max_iterations" and not context.tool_loop:
                    # Single-shot turn (tool_loop disabled): the host takes
                    # the tool results as they are; no follow-up model call.
                    injection = None
                if injection:
                    self._append_tool_exchange(
                        messages,
                        response,
                        clean_text,
                        tool_calls,
                        results,
                        protocol_state=protocol_state,
                    )
                    messages.append({"role": "user", "content": injection})
                    # Drop ``tools`` from kwargs for the injection call so
                    # the LLM is forced to emit assistant text rather than
                    # yet-another tool_call. Observed in sym_talk_lt
                    # 2026-05-27: stagnation fired correctly, but the
                    # injection call still had tools exposed and Sonnet
                    # emitted one more tool_call instead of text, leaving
                    # final_text empty and triggering a deterministic
                    # fallback bubble. With tools absent the model has no
                    # choice but to text-respond.
                    inj_kwargs = dict(kwargs)
                    inj_kwargs.pop("tools", None)
                    try:
                        inj_response, inj_chunks = self._call_llm_sync(messages, inj_kwargs, on_token)
                    except Exception:
                        pass  # keep the last final_text
                    else:
                        inj_text, inj_calls = self._parse_response(inj_response)
                        self._validate_protocol_response(
                            inj_response,
                            inj_calls,
                            protocol_state,
                        )
                        # A reply that still asks for tools ignored the stop
                        # instruction; its text is narration, not an answer.
                        final_text = inj_text if not inj_calls else ""
                        if on_token is not None:
                            if inj_chunks is not None:
                                for chunk in inj_chunks:
                                    on_token(chunk)
                            else:
                                on_token(inj_text)
                break

            # Feed results back for the next iteration
            self._append_tool_exchange(
                messages,
                response,
                clean_text,
                tool_calls,
                results,
                protocol_state=protocol_state,
            )

            # Layer 2: compact old loop messages to prevent context growth
            self._compact_loop_messages(messages, initial_msg_count)
            # Layer 3: aggressive autocompact if approaching context budget
            self._autocompact_if_needed(messages, initial_msg_count)

            if self._on_progress is not None:
                self._on_progress("iteration_end", iteration + 1, max_iters)

        # Finalize trace
        trace.total_iterations = rounds if context.tool_loop else 0
        trace.total_tool_calls = len(trace.steps)
        trace.total_elapsed_ms = int((time.monotonic() - loop_start) * 1000)
        if trace.stop_reason is None:
            trace.stop_reason = "end_turn" if trace.total_iterations > 0 else None
        final_text = self._ensure_final_text(
            final_text,
            trace.stop_reason,
            bool(all_tool_results) and context.tool_loop,
            on_token,
        )

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

        output: str | dict[str, Any] = final_text
        if all_tool_results:
            output = {"text": final_text, "tool_results": [r.model_dump() for r in all_tool_results]}

        if trace.steps:
            logger.info(
                "[tool-loop] completed: %d iterations, %d tool calls, %dms total",
                trace.total_iterations, trace.total_tool_calls, trace.total_elapsed_ms,
            )

        return self._complete_protocol_run(
            RunResult(
                success=True,
                output=output,
                runner_type=self.runner_type,
                loop_trace=trace if trace.steps else None,
            ),
            prepared,
        )

    async def run_async(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
    ) -> RunResult:
        """Async variant of run() with tool-loop support.

        The LLM adapter is synchronous; every model call runs in a worker
        thread (``asyncio.to_thread``) so other sessions and tasks on the
        event loop keep running meanwhile. ``on_token`` is still called on
        the event loop thread.
        """
        if context.tool_mode == "instant":
            # The instant path is synchronous end to end (model call, one
            # tool round, final call): run all of it in a worker thread and
            # hand tokens back to the loop thread in order.
            return await asyncio.to_thread(
                self._run_instant,
                context,
                _on_loop_thread(asyncio.get_running_loop(), on_token),
                llm_config=llm_config,
            )
        prepared = self._prepare_protocol_turn(context, llm_config)
        protocol_state = prepared.state
        messages = self._build_messages(
            context,
            native_tools=prepared.native_tools,
        )
        kwargs = prepared.to_llm_kwargs()
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
        rounds = 0  # model rounds actually executed (LLM calls in the loop)

        for iteration in range(max_iters):
            # Check loop timeout before each iteration
            if time.monotonic() - loop_start > loop_timeout:
                logger.info("[tool-loop] loop timeout exceeded (%.1fs)", loop_timeout)
                trace.stop_reason = "timeout"
                break

            if self._on_progress is not None:
                self._on_progress("iteration_start", iteration + 1, max_iters)

            try:
                response, buffered_chunks = await asyncio.to_thread(
                    self._call_llm_with_retry, messages, kwargs, on_token
                )
            except Exception as exc:
                # Totals are normally filled after the loop; fill them here
                # so a failed turn still records what it did.
                trace.total_iterations = rounds
                trace.total_tool_calls = len(trace.steps)
                trace.total_elapsed_ms = int((time.monotonic() - loop_start) * 1000)
                trace.stop_reason = trace.stop_reason or "error"
                return self._complete_protocol_run(
                    RunResult(
                        success=False,
                        error=str(exc),
                        runner_type=self.runner_type,
                        loop_trace=trace,
                    ),
                    prepared,
                )
            rounds += 1

            clean_text, tool_calls = self._parse_response(response)
            self._validate_protocol_response(
                response,
                tool_calls,
                protocol_state,
            )

            # ── Response validation ───────────────────────────────────────
            if not tool_calls:
                clean_text, buffered_chunks = await asyncio.to_thread(
                    self._validate_and_fix,
                    clean_text,
                    buffered_chunks,
                    messages,
                    kwargs,
                    protocol_state=protocol_state,
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

            # Approval gate: check high-risk tools before execution. The
            # callback is synchronous and may wait for a human, so it runs in
            # a worker thread instead of blocking this event loop.
            if self._on_before_tool_call is not None:
                approved_calls, denial_results = await asyncio.to_thread(
                    self._check_approval, remaining_calls
                )
            else:
                approved_calls, denial_results = self._check_approval(remaining_calls)

            if self._on_progress is not None:
                self._on_progress("tool_start", iteration + 1, max_iters)

            step_start = time.monotonic()
            gateway_results: list[ToolCallResult] = []
            if approved_calls:
                exec_results = await self._tool_gateway.execute_tool_calls_async(
                    symbiote_id=context.symbiote_id,
                    session_id=context.session_id,
                    calls=approved_calls,
                    timeout=context.tool_call_timeout,
                )
                exec_results = self._correlate_tool_results(
                    approved_calls,
                    exec_results,
                )
                gateway_results.extend(exec_results)
            step_elapsed = int((time.monotonic() - step_start) * 1000)

            if self._on_progress is not None:
                self._on_progress("tool_end", iteration + 1, max_iters)

            remaining_results = self._merge_tool_results(
                remaining_calls,
                denial_results,
                gateway_results,
            )

            # Update schema cache with new results
            self._update_schema_cache(
                remaining_calls,
                remaining_results,
                schema_cache,
            )

            # Merge cached + gateway results in original order
            results = self._merge_tool_results(
                tool_calls,
                cached_results,
                remaining_results,
            )
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
            controller.end_round()

            # Hook: let the caller decide whether tool results end the loop
            # (same contract as run()).
            if self._on_after_tool_result is not None:
                hook_text = self._on_after_tool_result(tool_calls, results)
                if hook_text is not None:
                    trace.stop_reason = "on_after_tool_result"
                    final_text = hook_text
                    if self._on_progress is not None:
                        self._on_progress("iteration_end", iteration + 1, max_iters)
                    break

            # Skill review nudge (same cadence as run()).
            self._maybe_nudge_skill_review(context)

            # Check loop health — stop early on stagnation or circuit breaker
            should_stop, stop_reason = controller.should_stop()
            if should_stop:
                trace.stop_reason = stop_reason
                injection = controller.get_injection_message()
                if stop_reason == "max_iterations" and not context.tool_loop:
                    # Single-shot turn (tool_loop disabled): the host takes
                    # the tool results as they are; no follow-up model call.
                    injection = None
                if injection:
                    self._append_tool_exchange(
                        messages,
                        response,
                        clean_text,
                        tool_calls,
                        results,
                        protocol_state=protocol_state,
                    )
                    messages.append({"role": "user", "content": injection})
                    # Drop ``tools`` from kwargs for the injection call so
                    # the LLM is forced to emit assistant text rather than
                    # yet-another tool_call. Observed in sym_talk_lt
                    # 2026-05-27: stagnation fired correctly, but the
                    # injection call still had tools exposed and Sonnet
                    # emitted one more tool_call instead of text, leaving
                    # final_text empty and triggering a deterministic
                    # fallback bubble. With tools absent the model has no
                    # choice but to text-respond.
                    inj_kwargs = dict(kwargs)
                    inj_kwargs.pop("tools", None)
                    try:
                        inj_response, inj_chunks = await asyncio.to_thread(
                            self._call_llm_sync, messages, inj_kwargs, on_token
                        )
                    except Exception:
                        pass  # keep the last final_text
                    else:
                        inj_text, inj_calls = self._parse_response(inj_response)
                        self._validate_protocol_response(
                            inj_response,
                            inj_calls,
                            protocol_state,
                        )
                        # A reply that still asks for tools ignored the stop
                        # instruction; its text is narration, not an answer.
                        final_text = inj_text if not inj_calls else ""
                        if on_token is not None:
                            if inj_chunks is not None:
                                for chunk in inj_chunks:
                                    on_token(chunk)
                            else:
                                on_token(inj_text)
                break

            self._append_tool_exchange(
                messages,
                response,
                clean_text,
                tool_calls,
                results,
                protocol_state=protocol_state,
            )

            # Layer 2: compact old loop messages to prevent context growth
            self._compact_loop_messages(messages, initial_msg_count)
            # Layer 3: aggressive autocompact if approaching context budget
            self._autocompact_if_needed(messages, initial_msg_count)

            if self._on_progress is not None:
                self._on_progress("iteration_end", iteration + 1, max_iters)

        trace.total_iterations = rounds if context.tool_loop else 0
        trace.total_tool_calls = len(trace.steps)
        trace.total_elapsed_ms = int((time.monotonic() - loop_start) * 1000)
        if trace.stop_reason is None:
            trace.stop_reason = "end_turn" if trace.total_iterations > 0 else None
        final_text = self._ensure_final_text(
            final_text,
            trace.stop_reason,
            bool(all_tool_results) and context.tool_loop,
            on_token,
        )

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

        output: str | dict[str, Any] = final_text
        if all_tool_results:
            output = {"text": final_text, "tool_results": [r.model_dump() for r in all_tool_results]}

        if trace.steps:
            logger.info(
                "[tool-loop] completed: %d iterations, %d tool calls, %dms total",
                trace.total_iterations, trace.total_tool_calls, trace.total_elapsed_ms,
            )

        return self._complete_protocol_run(
            RunResult(
                success=True,
                output=output,
                runner_type=self.runner_type,
                loop_trace=trace if trace.steps else None,
            ),
            prepared,
        )

    def _check_approval(
        self,
        calls: list[ToolCall],
    ) -> tuple[list[ToolCall], dict[int, ToolCallResult]]:
        """Check approval for tool calls based on risk_level.

        Returns a tuple of (approved_calls, denial_results_by_index).
        High-risk tools require approval via the on_before_tool_call callback.
        If the callback is None, all tools are auto-approved (backward compat).
        """
        approved: list[ToolCall] = []
        denied: dict[int, ToolCallResult] = {}

        for idx, call in enumerate(calls):
            if call.arguments_error is not None:
                # Arguments could not be decoded (truncated JSON, ...): never
                # run the tool with guessed/empty params; tell the model.
                denied[idx] = ToolCallResult(
                    call_id=call.call_id,
                    tool_id=call.tool_id,
                    success=False,
                    error=(
                        f"{call.arguments_error}. The tool was not executed; "
                        "call it again with complete JSON arguments."
                    ),
                )
                continue
            if self._on_before_tool_call is None or self._tool_gateway is None:
                approved.append(call)
                continue
            risk = self._tool_gateway.get_risk_level(call.tool_id)
            if risk == "high" and not self._on_before_tool_call(call.tool_id, call.params, risk):
                denied[idx] = ToolCallResult(
                    call_id=call.call_id,
                    tool_id=call.tool_id,
                    success=False,
                    error="Tool call denied by approval callback",
                    risk_level=risk,
                )
                continue
            approved.append(call)

        return approved, denied

    # ── Loop summary for working memory ─────────────────────────────────

    @staticmethod
    def _ensure_final_text(
        final_text: str,
        stop_reason: str | None,
        tools_ran: bool,
        on_token: Callable[[str], None] | None,
    ) -> str:
        """Never end a turn that ran tools with an empty or narration-only text.

        On timeout the last text is the narration of a tool-call turn ("let me
        check one more source..."), so it is replaced. Otherwise an empty text
        (the model kept asking for tools even when told to stop) gets a
        generic answer.
        """
        if stop_reason == "timeout":
            replacement = _LOOP_TIMEOUT_TEXT
        elif tools_ran and not final_text.strip():
            replacement = _NO_FINAL_ANSWER_TEXT
        else:
            return final_text
        if on_token is not None:
            on_token(replacement)
        return replacement

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
                        call_id=call.call_id,
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
        for call, result in zip(remaining_calls, gateway_results, strict=True):
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
        """Merge results in call order and preserve native correlation IDs."""
        expected_gateway_results = len(original_calls) - len(cached)
        if len(gateway_results) != expected_gateway_results:
            raise ValueError(
                "tool result cardinality mismatch while merging: "
                f"expected {expected_gateway_results} gateway results for "
                f"{len(original_calls)} calls, got {len(gateway_results)}"
            )
        merged: list[ToolCallResult] = []
        gw_iter = iter(gateway_results)
        for idx, call in enumerate(original_calls):
            result = cached[idx] if idx in cached else next(gw_iter)
            if (
                result.call_id is not None
                and result.call_id != call.call_id
            ):
                raise ValueError(
                    "tool result correlation mismatch while merging: "
                    f"call_id={call.call_id!r}, "
                    f"result.call_id={result.call_id!r}"
                )
            if result.call_id is None and call.call_id is not None:
                result = result.model_copy(update={"call_id": call.call_id})
            merged.append(result)
        return merged

    @staticmethod
    def _correlate_tool_results(
        calls: list[ToolCall],
        results: list[ToolCallResult],
    ) -> list[ToolCallResult]:
        """Attach each call's ID to gateway results without mutating them."""
        if len(calls) != len(results):
            raise ValueError(
                "tool result cardinality mismatch from gateway: "
                f"{len(calls)} calls but {len(results)} results"
            )
        correlated: list[ToolCallResult] = []
        for call, result in zip(calls, results, strict=True):
            if (
                result.call_id is not None
                and result.call_id != call.call_id
            ):
                raise ValueError(
                    "tool result correlation mismatch from gateway: "
                    f"call_id={call.call_id!r}, "
                    f"result.call_id={result.call_id!r}"
                )
            if result.call_id is None and call.call_id is not None:
                result = result.model_copy(update={"call_id": call.call_id})
            correlated.append(result)
        return correlated

    # ── internal ─────────────────────────────────────────────────────────

    def _build_llm_kwargs(
        self,
        context: AssembledContext,
        llm_config: dict | None = None,
    ) -> dict:
        """Build protocol-prepared kwargs for compatibility with callers.

        Runner execution prepares once and reuses the resulting state. This
        helper remains available to existing tests/hosts that inspect kwargs.
        """
        return self._prepare_protocol_turn(
            context,
            llm_config,
        ).to_llm_kwargs()

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
        """Return True if *exc* is a transient error worth retrying.

        Decided by ``LLMError.retryable`` (set by the adapter from the
        provider status) or, for other exceptions, by the HTTP status found
        in the exception chain or the exception type. Message text is never
        inspected: a 400 whose message quotes "timeout" or "503" is not
        retried, and a 503 whose request id contains "429" is.
        """
        return is_transient_llm_error(exc)

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
    def _parse_response(
        response: str | LLMResponse,
    ) -> tuple[str, list[ToolCall]]:
        """Extract clean text and tool calls from LLM response."""
        if isinstance(response, LLMResponse):
            calls: list[ToolCall] = []
            for native_call in response.tool_calls:
                call = native_call.to_tool_call()
                if call.call_id is None:
                    call = call.model_copy(
                        update={"call_id": f"call_{uuid4().hex}"}
                    )
                calls.append(call)
            return response.content or "", calls
        return parse_tool_calls(response or "")

    def _validate_and_fix(
        self,
        clean_text: str,
        buffered_chunks: list[str] | None,
        messages: list[dict],
        kwargs: dict,
        *,
        protocol_state: ProtocolState | None = None,
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

        original_text, original_chunks = clean_text, buffered_chunks
        retry_messages = list(messages)
        for _ in range(_MAX_VALIDATION_RETRIES):
            retry_messages.append({"role": "assistant", "content": clean_text})
            retry_messages.append({"role": "user", "content": reformulate_message()})
            try:
                retry_response, retry_chunks = self._call_llm_sync(retry_messages, kwargs, None)
            except Exception:
                break
            clean_text, retry_calls = self._parse_response(retry_response)
            if protocol_state is not None:
                self._validate_protocol_response(
                    retry_response,
                    retry_calls,
                    protocol_state,
                )
            if is_valid_response(clean_text):
                # Return single-chunk buffer matching the validated text
                return clean_text, [clean_text] if buffered_chunks is not None else None

        # Retries did not help. Keep the model's own answer unless there is
        # nothing to show or it is a raw tool_call block: a false positive of
        # the validator must not turn a real answer into an apology.
        if original_text.strip() and not has_raw_tool_fence(original_text):
            return original_text, original_chunks
        fb = fallback_text()
        return fb, [fb] if buffered_chunks is not None else None

    @staticmethod
    def _compact_loop_messages(
        messages: list[dict], initial_count: int
    ) -> None:
        """Replace old tool exchanges with a compact summary.

        Only compacts messages added during the tool loop (after
        ``initial_count``). Keeps the most recent complete exchange intact so
        native ``assistant`` + one-or-more ``tool`` messages never become
        orphaned. Legacy assistant/user pairs retain their previous shape.

        Modifies *messages* in-place.
        """
        loop_messages = messages[initial_count:]
        assistant_positions = [
            idx
            for idx, message in enumerate(loop_messages)
            if message.get("role") == "assistant"
        ]
        if len(assistant_positions) < _COMPACTION_THRESHOLD:
            return

        last_exchange_start = assistant_positions[-1]
        messages_to_compact = loop_messages[:last_exchange_start]
        last_exchange = loop_messages[last_exchange_start:]
        if not messages_to_compact:
            return

        steps: list[str] = []
        for message in messages_to_compact:
            role = message.get("role")
            content = message.get("content")
            result_text = content if isinstance(content, str) else ""

            carried = _summary_steps(message)
            if carried is not None:
                # A previous summary: keep its steps instead of losing them
                # (native) or parsing the whole summary as one step (text).
                for step in carried:
                    steps.append(f"{len(steps) + 1}) {step}")
                continue

            if role == "tool":
                tool_id = (
                    message.get("name")
                    or message.get("tool_call_id")
                    or "unknown"
                )
            elif role == "user" and "[Tool result: " in result_text:
                tool_id = result_text.split("[Tool result: ")[1].split("]")[0]
            elif role == "user" and "[Tool error: " in result_text:
                tool_id = result_text.split("[Tool error: ")[1].split("]")[0]
            else:
                continue

            # One line per step, so a later compaction can carry it as-is.
            result_preview = " ".join(result_text[:200].split())
            if len(result_text) > 200:
                result_preview += "... (truncated)"
            steps.append(
                f"{len(steps) + 1}) {tool_id} → {result_preview}"
            )

        summary = (
            f"{_COMPACTED_PREFIX}\n"
            "Steps completed so far:\n"
            + ("\n".join(steps) if steps else "Earlier tool exchanges compacted.")
            + "\n"
            "Continue from here."
        )

        del messages[initial_count:]
        messages.append({"role": "user", "content": summary})
        messages.extend(last_exchange)

    def _microcompact_tool_result(self, result_text: str) -> str:
        """Truncate a single tool result if it exceeds the size threshold.

        Layer 1 of the 3-layer compaction system.  Applied to each tool
        result *before* it is injected into the message list, preventing
        large JSON payloads from consuming the context window.  The
        threshold is per-runner (``tool_result_max_chars``, plumbed from
        ``KernelConfig``) so hosts with large tool payloads can raise it.
        """
        return compact_tool_result(
            result_text,
            self._tool_result_max_chars,
        )

    @staticmethod
    def _message_chars(message: dict) -> int:
        total = 0
        for key in ("content", "tool_calls", "reasoning_content", "reasoning_state"):
            value = message.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                total += len(value)
            else:
                total += len(json.dumps(value, default=str, ensure_ascii=False))
        return total

    @staticmethod
    def _summarize_step(message: dict) -> tuple[str, str] | None:
        """(tool_id, status) for a tool-result message, else None."""
        role = message.get("role")
        content = message.get("content")
        result_text = content if isinstance(content, str) else ""
        if role == "tool":
            tool_id = message.get("name") or message.get("tool_call_id") or "unknown"
            status = (
                "error"
                if '"success":false' in result_text.replace(" ", "").lower()
                else "ok"
            )
            return tool_id, status
        if role == "user" and "[Tool result: " in result_text:
            return result_text.split("[Tool result: ")[1].split("]")[0], "ok"
        if role == "user" and "[Tool error: " in result_text:
            return result_text.split("[Tool error: ")[1].split("]")[0], "error"
        return None

    def _autocompact_if_needed(
        self, messages: list[dict], initial_count: int
    ) -> bool:
        """Compact when total tokens approach the loop context budget.

        Layer 3 of the 3-layer compaction system. When the estimated size of
        all messages exceeds ``_AUTOCOMPACT_THRESHOLD`` of
        ``self._context_budget``:

        1. every loop message *before* the most recent exchange (the last
           assistant turn and the tool results that answer it) is replaced
           by a one-line-per-step summary. The latest exchange is never
           summarized: the model has not read those results yet, and hiding
           them makes it repeat tools with side effects;
        2. if that is still over budget, the tool results of the latest
           exchange are truncated (never removed) to fit, keeping their
           beginning.

        Returns True if anything was changed.
        """
        threshold_chars = int(self._context_budget * _AUTOCOMPACT_THRESHOLD) * _CHARS_PER_TOKEN
        if sum(self._message_chars(m) for m in messages) <= threshold_chars:
            return False

        loop_messages = messages[initial_count:]
        assistant_positions = [
            idx for idx, m in enumerate(loop_messages) if m.get("role") == "assistant"
        ]
        if not assistant_positions:
            return False
        last_start = assistant_positions[-1]
        older = loop_messages[:last_start]
        last_exchange = loop_messages[last_start:]
        changed = False

        if older:
            steps: list[str] = []
            for step in older:
                carried = _summary_steps(step)
                if carried is not None:
                    for carried_step in carried:
                        steps.append(f"{len(steps) + 1}) {carried_step}")
                    continue
                summary = self._summarize_step(step)
                if summary is not None:
                    tool_id, status = summary
                    steps.append(f"{len(steps) + 1}) {tool_id} → {status}")
            logger.info(
                "[autocompact] over budget; summarizing %d older loop messages",
                len(older),
            )
            summary_msg = {
                "role": "user",
                "content": (
                    f"{_AUTOCOMPACTED_PREFIX}\n"
                    "Steps completed:\n"
                    + ("\n".join(steps) if steps else "Tool exchanges compacted.")
                    + "\nThe most recent tool results follow in full. "
                    "Do not repeat completed steps."
                ),
            }
            messages[initial_count:] = [summary_msg, *last_exchange]
            changed = True

        over = sum(self._message_chars(m) for m in messages) - threshold_chars
        if over > 0:
            changed = self._truncate_latest_results(messages, over) or changed
        return changed

    def _truncate_latest_results(self, messages: list[dict], excess_chars: int) -> bool:
        """Shorten the newest tool results by about ``excess_chars`` in total."""
        result_idx = []
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "assistant":
                break
            if self._summarize_step(messages[idx]) is not None:
                result_idx.append(idx)
        if not result_idx:
            return False
        per_result_cut = excess_chars // len(result_idx) + 1
        changed = False
        for idx in result_idx:
            content = messages[idx].get("content")
            if not isinstance(content, str):
                continue
            keep = max(_MIN_RESULT_CHARS_AFTER_AUTOCOMPACT, len(content) - per_result_cut)
            if keep < len(content):
                messages[idx] = {
                    **messages[idx],
                    "content": content[:keep]
                    + f"\n... (truncated {len(content) - keep} chars to fit the context budget)",
                }
                changed = True
        return changed

    @staticmethod
    def _format_assistant_with_calls(text: str, calls: list[ToolCall]) -> str:
        """Format assistant message including tool_call blocks for the loop context."""
        return format_legacy_assistant(text, calls)

    def _append_tool_exchange(
        self,
        messages: list[dict[str, Any]],
        response: str | LLMResponse,
        text: str,
        calls: list[ToolCall],
        results: list[ToolCallResult],
        *,
        protocol_state: ProtocolState | None = None,
    ) -> None:
        """Append one exchange exclusively through the selected protocol."""
        state = protocol_state or self._agent_protocol.create_state()
        exchange = self._agent_protocol.serialize_exchange(
            response,
            text,
            calls,
            results,
            tool_result_max_chars=self._tool_result_max_chars,
            state=state,
        )
        messages.extend(exchange)

    def _maybe_nudge_skill_review(self, context: AssembledContext) -> None:
        """Fire skill review every ``skill_nudge_interval`` tool iterations.

        ``background_review`` may be passed either as an engine (object with a
        ``spawn(session_id, symbiote_id)`` method) or as a per-symbiote
        resolver ``Callable[[str], engine | None]``. The resolver form lets a
        shared ChatRunner check ``skill_review_enabled`` per symbiote without
        the runner needing to know about ``EnvironmentManager``.

        Counter is per-ChatRunner instance — same runner across sessions nudges
        at the configured cadence. Failure modes (resolver raises, engine None,
        spawn missing) are swallowed so nudging never breaks the turn.
        """
        if self._background_review is None:
            return
        self._iters_since_skill_review += 1
        if self._iters_since_skill_review < self._skill_nudge_interval:
            return
        self._iters_since_skill_review = 0

        try:
            engine = (
                self._background_review(context.symbiote_id)
                if callable(self._background_review)
                else self._background_review
            )
            if engine is None:
                return
            spawn = getattr(engine, "spawn", None)
            if spawn is None:
                return
            spawn(context.session_id, context.symbiote_id)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "skill review nudge failed: %s", exc,
            )

    def _format_tool_results(self, results: list[ToolCallResult]) -> str:
        """Format tool results as a user message for the next LLM turn.

        Applies Layer 1 microcompaction to each individual result.
        """
        return format_legacy_tool_results(
            results,
            max_chars=self._tool_result_max_chars,
        )

    def _build_messages(
        self,
        context: AssembledContext,
        *,
        native_tools: bool | None = None,
    ) -> list[dict]:
        messages: list[dict] = []

        # 1. System message
        messages.append({
            "role": "system",
            "content": self._build_system(
                context,
                native_tools=native_tools,
            ),
        })

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
                for turn in self._parse_conversation_history(history_text):
                    messages.append(turn)

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

    def _build_system(
        self,
        context: AssembledContext,
        *,
        native_tools: bool | None = None,
    ) -> str:
        parts: list[str] = []
        if native_tools is None:
            native_tools = self._effective_native_tools()

        # Persona
        if context.persona:
            parts.append("## Persona")
            parts.append(self._render_persona(context.persona))

        # Available tools (text-based instructions only when NOT using native tools)
        if context.available_tools and not native_tools:
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

        # Learned skills (opt-in via skill_injection_enabled). The assembler
        # has already budget-trimmed the summary; the runner only renders it.
        if context.skills_summary:
            parts.append("## Skills")
            parts.append(
                "Skills you have available. Read a skill's full instructions "
                "by name before applying it."
            )
            parts.append(context.skills_summary)

        if not parts:
            return "You are a helpful assistant."

        return "\n\n".join(parts)

    # Free-text persona fields rendered as prose (the system prompt itself).
    # ``description``/``system_prompt``/``instructions`` are the canonical
    # prompt fields the admin Console edits; ``behavior`` is the legacy name.
    _PERSONA_PROSE_KEYS = ("description", "system_prompt", "instructions", "behavior")

    @staticmethod
    def _render_persona(persona: dict) -> str:
        """Render persona dict as natural-language instructions.

        Recognised keys are rendered as structured text; the free-text prompt
        fields are rendered as prose; any remaining keys are appended as a
        compact JSON block so nothing is lost.
        """
        prose_keys = ChatRunner._PERSONA_PROSE_KEYS
        known_keys = {"role", "tone", "language", *prose_keys}
        lines: list[str] = []

        if role := persona.get("role"):
            lines.append(f"You are: {role}")
        if tone := persona.get("tone"):
            lines.append(f"Tone: {tone}")
        if lang := persona.get("language"):
            lines.append(f"Language: {lang}")
        # Render each free-text prompt field as prose (lossless: all present
        # ones are emitted, not just the first).
        for key in prose_keys:
            val = persona.get(key)
            if isinstance(val, str) and val:
                lines.append(f"\n{val}")

        # Render any extra keys the host added (custom fields), plus any
        # prose-key whose value isn't a plain string (kept in JSON, lossless).
        extra = {
            k: v
            for k, v in persona.items()
            if k not in known_keys or (k in prose_keys and not isinstance(v, str))
        }
        if extra:
            lines.append(json.dumps(extra, indent=2, default=str, ensure_ascii=False))

        return "\n".join(lines) if lines else json.dumps(persona, indent=2, default=str)
