"""ChatRunner — conversational runner that delegates to an LLM."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Generator, Mapping
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
    flatten_tool_exchanges,
    format_legacy_assistant,
    format_legacy_tool_results,
)
from symbiote.core.approvals import DEFAULT_PENDING_MESSAGE
from symbiote.core.context import AssembledContext
from symbiote.core.exceptions import ValidationError, is_transient_llm_error
from symbiote.core.models import Message
from symbiote.core.origin import label_origin, normalize_origin
from symbiote.core.ports import LLMPort
from symbiote.core.session_scope import tool_in_session
from symbiote.core.turn_control import TurnStop, check_turn
from symbiote.core.turn_record import turn_budget_exceeded
from symbiote.environment.descriptors import (
    LLMResponse,
    ReasoningDelta,
    ToolCall,
    ToolCallResult,
)
from symbiote.environment.exec_context import tool_exec_context
from symbiote.environment.parser import parse_tool_calls
from symbiote.environment.policies import (
    ApprovalDecision,
    as_approval_decision,
    error_with_hint,
)
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
from symbiote.runners.token_stream import TOKEN_STREAMING_MODES, TokenSink

logger = logging.getLogger(__name__)

# Records a call the approval callback deferred and returns the id of the
# pending approval: (tool_id, params, risk_level, decision, symbiote_id,
# session_id) -> approval id.
DeferralHandler = Callable[[str, dict, str, ApprovalDecision, str, str], str]

if TYPE_CHECKING:
    from symbiote.environment.tools import ToolGateway
    from symbiote.memory.consolidator import MemoryConsolidator

_HANDLED_INTENTS = frozenset({"chat", "ask", "question", "talk"})
_MAX_TOOL_ITERATIONS = 10
_MAX_VALIDATION_RETRIES = 2
# Layer 2: compact after this many loop rounds (KernelConfig.loop_compaction_rounds).
_COMPACTION_THRESHOLD = 4
_CHARS_PER_TOKEN = 4  # rough estimate for token counting
_MICROCOMPACT_MAX_CHARS = 3000  # truncate individual tool results beyond this
_AUTOCOMPACT_THRESHOLD = 0.80  # trigger autocompact at 80% of context budget
# Loop context budget (tokens) used by Layer 3 autocompact. The kernel passes
# KernelConfig.loop_context_budget; this is the default for hosts that build
# a ChatRunner directly.
_DEFAULT_CONTEXT_BUDGET = 16000
_RUNTIME_CONTEXT_MODES = frozenset({"full", "timestamp", "off"})
_HISTORY_ROLES = frozenset({"user", "assistant"})
# After autocompact, a fresh tool result is never cut below this many chars.
_MIN_RESULT_CHARS_AFTER_AUTOCOMPACT = 2000
_MAX_LLM_RETRIES = 3

# What an instant turn with ``instant_read_results`` tells the model after its
# actions ran. A host replaces it per symbiote with the harness text
# ``injection_instant_results`` (``kernel.harness_versions.create_version``).
INSTANT_RESULTS_MSG = (
    "The actions above have run. Read their results and answer the user now. "
    "Do not call any more tools."
)
# User-facing texts when the loop ends without a model answer (same language
# as response_validator's fallback).
_LOOP_TIMEOUT_TEXT = (
    "Não consegui concluir a tarefa dentro do tempo limite. "
    "Posso continuar de onde parei se você pedir."
)
_BUDGET_TEXT = (
    "Parei antes de concluir porque esta resposta atingiu o limite de uso "
    "configurado. Posso continuar se você pedir."
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


class _Blocking:
    """Loop request: call ``fn(*args, **kwargs)``, which may block (a model
    call, the approval callback)."""

    __slots__ = ("args", "fn", "kwargs")

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self.fn = fn
        self.args = args
        self.kwargs = kwargs


class _ToolCalls:
    """Loop request: execute these (approved) tool calls through the gateway."""

    __slots__ = ("calls", "context")

    def __init__(self, calls: list[ToolCall], context: AssembledContext) -> None:
        self.calls = calls
        self.context = context


_LoopRequest = _Blocking | _ToolCalls


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
        on_before_tool_call: Callable[[str, dict, str], bool | ApprovalDecision] | None = None,
        # Lowest risk that goes to ``on_before_tool_call`` ("low", "medium"
        # or "high"); tools with ``requires_approval`` are always asked.
        approval_risk_threshold: str = "high",
        # Records a call the approval callback deferred and returns the id of
        # the pending approval (see ``set_deferral_handler``).
        on_defer_tool_call: DeferralHandler | None = None,
        on_after_tool_result: Callable[[list[ToolCall], list[ToolCallResult]], str | None] | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_stream: Callable[[str, int], None] | None = None,
        # The model's reasoning as it is streamed (see set_progress_callbacks).
        on_reasoning: Callable[[str, int], None] | None = None,
        # Sprint 4 — optional. When provided AND a per-symbiote
        # skill_review_enabled=True, the runner fires the engine every N
        # tool iterations from inside the loop. Default None preserves
        # backward-compat with hosts (you_news, sym_talk_lt) that
        # instantiate ChatRunner directly.
        background_review: object | None = None,
        skill_nudge_interval: int = 10,
        agent_protocol: str | AgentProtocol | None = None,
        protocol_registry: AgentProtocolRegistry | None = None,
        token_streaming: str = "final",
        # What is prepended to the user message sent to the model: "full"
        # (timestamp + session id, the historical behaviour), "timestamp"
        # (no session id) or "off" (the message goes as the user wrote it).
        runtime_context: str = "full",
        # Layer 2: after this many tool rounds, older exchanges are replaced
        # by a one-line-per-step summary every round. None or 0 turns it off,
        # leaving only the budget compaction (Layer 3).
        loop_compaction_rounds: int | None = _COMPACTION_THRESHOLD,
        # Whether the time a person takes to answer the approval callback
        # counts toward ``loop_timeout``. False: the loop's limit measures
        # the work only (model calls and tools), not the wait.
        loop_timeout_counts_approval: bool = True,
    ) -> None:
        if runtime_context not in _RUNTIME_CONTEXT_MODES:
            raise ValidationError(
                f"runtime_context must be one of {sorted(_RUNTIME_CONTEXT_MODES)}, "
                f"got {runtime_context!r}"
            )
        if loop_compaction_rounds is not None and loop_compaction_rounds < 0:
            raise ValidationError(
                "loop_compaction_rounds must be None or >= 0, "
                f"got {loop_compaction_rounds!r}"
            )
        self._runtime_context = runtime_context
        self._loop_compaction_rounds = loop_compaction_rounds or None
        self._loop_timeout_counts_approval = bool(loop_timeout_counts_approval)
        self._llm = llm
        self._working_memory = working_memory
        self._tool_gateway = tool_gateway
        self._consolidator = consolidator
        self._native_tools = native_tools
        self._context_budget = context_budget
        self._tool_result_max_chars = max(1, int(tool_result_max_chars))
        self._on_before_tool_call = on_before_tool_call
        self._approval_risk_threshold = "high"
        self.set_approval_callback(on_before_tool_call, risk_threshold=approval_risk_threshold)
        self._on_defer_tool_call = on_defer_tool_call
        self._on_after_tool_result = on_after_tool_result
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_reasoning = on_reasoning
        self._background_review = background_review
        self._skill_nudge_interval = max(1, int(skill_nudge_interval))
        self._iters_since_skill_review = 0
        self._protocol_registry = (
            protocol_registry or create_default_protocol_registry()
        )
        self._agent_protocol = self._protocol_registry.resolve(agent_protocol)
        self._token_streaming = "final"
        self.set_token_streaming(token_streaming)

    def can_handle(self, intent: str) -> bool:
        return intent in _HANDLED_INTENTS

    @property
    def agent_protocol_id(self) -> str:
        """Return the exact protocol ID selected for subsequent turns."""
        return self._agent_protocol.protocol_id

    def set_approval_callback(
        self,
        callback: Callable[[str, dict, str], bool | ApprovalDecision] | None,
        *,
        risk_threshold: str = "high",
    ) -> None:
        """Gate tools at or above ``risk_threshold``; required tools always ask.

        The callback answers ``True`` / ``False`` or an ``ApprovalDecision``
        (approve, deny with a reason, or defer: see ``set_deferral_handler``).
        """
        if risk_threshold not in ("low", "medium", "high"):
            raise ValueError(f"Unknown approval risk threshold: {risk_threshold}")
        self._on_before_tool_call = callback
        self._approval_risk_threshold = risk_threshold

    def set_deferral_handler(self, handler: DeferralHandler | None) -> None:
        """Install what records a call the approval callback deferred.

        ``handler(tool_id, params, risk_level, decision, symbiote_id,
        session_id)`` stores the request and returns its id. Without one, a
        deferred call is refused like a denied one: the runner alone has
        nowhere to keep it.
        """
        self._on_defer_tool_call = handler

    def set_progress_callbacks(
        self,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_stream: Callable[[str, int], None] | None = None,
        on_reasoning: Callable[[str, int], None] | None = None,
    ) -> None:
        """Install (or clear, with ``None``) the mid-loop callbacks.

        ``on_progress(event, iteration, max_iterations)`` receives
        ``iteration_start``, ``tool_start``, ``tool_end`` and ``iteration_end``;
        ``on_stream(text, iteration)`` receives the parsed text of every model
        reply, including the ones that only request tools.

        ``on_reasoning(text, iteration)`` receives the model's reasoning as it
        is streamed, piece by piece, during every model call of the turn:
        ``iteration`` is the number of the call, the one ``on_progress``
        gives its ``iteration_start`` (a call after the last round, such as
        the answer forced by a step limit or instant mode's reading of the
        results, gets the next number; a retried call keeps its own). It
        needs an adapter that streams reasoning (``stream_reasoning=True``)
        and makes every call go through ``stream()`` even without
        ``on_token``. The reasoning never enters the turn's text, the
        history or the memory.
        """
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_reasoning = on_reasoning

    def set_native_tools(self, enabled: bool) -> None:
        """Use the provider's structured tool calls instead of text parsing."""
        self._native_tools = enabled

    def set_token_streaming(self, mode: str) -> None:
        """How text reaches ``on_token``: ``"final"`` or ``"live"``.

        See ``symbiote.runners.token_stream``.
        """
        if mode not in TOKEN_STREAMING_MODES:
            raise ValueError(
                f"token_streaming must be one of {TOKEN_STREAMING_MODES}, got {mode!r}"
            )
        self._token_streaming = mode

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
        controller, no compaction, no progress callbacks. The model is called
        again only when an action failed or, with
        ``context.instant_read_results``, to read the results and answer.

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
        done: list[tuple[ToolCall, ToolCallResult]] = []
        started = time.monotonic()
        try:
            return self._instant(context, on_token, llm_config, done)
        except TurnStop as stop:
            stop.trace = LoopTrace(
                steps=[
                    LoopStep(
                        iteration=1,
                        tool_id=call.tool_id,
                        params=call.params,
                        success=result.success,
                        error=result.error,
                    )
                    for call, result in done
                ],
                total_iterations=1,
                total_tool_calls=len(done),
                total_elapsed_ms=int((time.monotonic() - started) * 1000),
                stop_reason=stop.reason,
                tool_mode="instant",
            )
            raise

    def _instant(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None,
        llm_config: dict | None,
        done: list[tuple[ToolCall, ToolCallResult]],
    ) -> RunResult:
        """The instant path itself; ``done`` receives the tool calls executed."""
        prepared = self._prepare_protocol_turn(context, llm_config)
        protocol_state = prepared.state
        messages = self._build_messages(
            context,
            native_tools=prepared.native_tools,
        )
        kwargs = prepared.to_llm_kwargs()
        loop_start = time.monotonic()
        sink = TokenSink(on_token, self._token_streaming)

        # Single LLM call with retry
        try:
            response, buffered_chunks = self._call_llm_with_retry(
                messages, kwargs, on_token, sink, reasoning=self._reasoning_for(1)
            )
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
            approved_calls, denial_results = self._check_approval(tool_calls, context)

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
            done.extend(zip(tool_calls, results, strict=False))

            # Hook: let the caller decide whether tool results end the loop.
            hook_text: str | None = None
            if self._on_after_tool_result is not None:
                hook_text = self._on_after_tool_result(tool_calls, results)

            if hook_text is not None:
                clean_text = hook_text
            elif all(tr.success for tr in all_tool_results) and context.instant_read_results:
                # The actions ran: one more call, without tools, so the model
                # reads what they returned and answers. Without it the turn
                # ends with the text written before the results existed.
                self._append_tool_exchange(
                    messages,
                    response,
                    clean_text,
                    tool_calls,
                    results,
                    protocol_state=protocol_state,
                )
                messages.append({
                    "role": "user",
                    "content": (
                        context.injection_instant_results_override or INSTANT_RESULTS_MSG
                    ),
                })
                # No ``tools`` in this call, and so no structured tool
                # exchange either (see the step-limit call of the loop).
                read_kwargs = dict(kwargs)
                read_kwargs.pop("tools", None)
                try:
                    read_response, read_chunks = self._call_llm_with_retry(
                        flatten_tool_exchanges(messages), read_kwargs, on_token, sink,
                        reasoning=self._reasoning_for(2),
                    )
                except Exception:
                    pass  # keep the text of the first call
                else:
                    read_text, read_calls = self._parse_response(read_response)
                    self._validate_protocol_response(
                        read_response,
                        read_calls,
                        protocol_state,
                    )
                    # A reply that still asks for tools ignored the
                    # instruction; its text is narration, not an answer.
                    if read_text.strip() and not read_calls:
                        clean_text, buffered_chunks = read_text, read_chunks
                if not clean_text.strip():
                    clean_text = "Pronto."
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
                    final_response, buffered_chunks = self._call_llm_with_retry(
                        messages, kwargs, on_token, sink,
                        reasoning=self._reasoning_for(2),
                    )
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
        sink.release(clean_text, buffered_chunks, exact=True)

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
        return self._drive(self._loop_steps(context, on_token, llm_config))

    def _loop_steps(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
    ) -> Generator[_LoopRequest, Any, RunResult]:
        """The tool loop of brief/continuous modes, without I/O.

        A generator: wherever the loop needs something slow (a model call,
        the approval callback, the execution of tool calls) it yields a
        request and receives the result, or the exception, back. ``_drive``
        serves the requests on the calling thread and ``_drive_async`` on
        the event loop, so ``run()`` and ``run_async()`` share this one body
        and cannot drift apart. Everything else (callbacks, ``on_token``,
        bookkeeping) runs on the thread that drives the generator.

        When the turn has to stop (``TurnStop``: cancelled, or its session
        lock was lost), the trace of what was done so far goes up with it.
        """
        trace = LoopTrace(tool_mode=context.tool_mode)
        started = time.monotonic()
        try:
            return (yield from self._loop_body(context, on_token, llm_config, trace))
        except TurnStop as stop:
            trace.total_tool_calls = len(trace.steps)
            trace.total_elapsed_ms = int((time.monotonic() - started) * 1000)
            trace.stop_reason = stop.reason
            stop.trace = trace
            raise

    def _loop_body(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None,
        llm_config: dict | None,
        trace: LoopTrace,
    ) -> Generator[_LoopRequest, Any, RunResult]:
        """The loop itself; see ``_loop_steps``."""
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
        final_text = ""
        loop_start = time.monotonic()
        loop_timeout = context.loop_timeout
        # Seconds spent in the approval gate, taken off the loop's clock
        # when ``loop_timeout_counts_approval`` is off. The trace's
        # ``total_elapsed_ms`` stays wall-clock time either way.
        approval_wait = 0.0
        rounds = 0  # model rounds actually executed (LLM calls in the loop)
        sink = TokenSink(on_token, self._token_streaming)

        for iteration in range(max_iters):
            # Check loop timeout before each iteration
            if time.monotonic() - loop_start - approval_wait > loop_timeout:
                logger.info("[tool-loop] loop timeout exceeded (%.1fs)", loop_timeout)
                trace.stop_reason = "timeout"
                break
            # ...and the spending ceiling: no further model call once the
            # turn reached it (the first round always runs).
            if turn_budget_exceeded(context.max_turn_tokens, context.max_turn_llm_calls):
                logger.info("[tool-loop] turn budget reached")
                trace.stop_reason = "budget"
                break

            if self._on_progress is not None:
                self._on_progress("iteration_start", iteration + 1, max_iters)

            try:
                response, buffered_chunks = yield _Blocking(
                    self._call_llm_with_retry, messages, kwargs, on_token, sink,
                    reasoning=self._reasoning_for(iteration + 1),
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
            trace.total_iterations = rounds  # kept current for a stopped turn

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
                clean_text, buffered_chunks = yield _Blocking(
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

            # Release tokens to the caller after validation. In "final" mode
            # the narration of a tool-call round (e.g. "Vou verificar...")
            # never reaches the user; in "live" mode it already streamed.
            if tool_calls:
                sink.end_round()
            else:
                sink.release(clean_text, buffered_chunks)

            if not tool_calls or self._tool_gateway is None:
                if self._on_progress is not None:
                    self._on_progress("iteration_end", iteration + 1, max_iters)
                break

            # Schema cache: intercept repeated get_tool_schema in index mode
            cached_results, remaining_calls = self._split_cached_schema_calls(
                tool_calls, schema_cache, context.tool_loading,
            )

            # Approval gate: check high-risk tools before execution. The
            # callback is synchronous and may wait for a human, so it is a
            # blocking request (a worker thread under the async driver).
            if self._on_before_tool_call is not None:
                asked = (
                    None if self._loop_timeout_counts_approval else time.monotonic()
                )
                approved_calls, denial_results = yield _Blocking(
                    self._check_approval, remaining_calls, context
                )
                if asked is not None:
                    approval_wait += time.monotonic() - asked
            else:
                approved_calls, denial_results = self._check_approval(
                    remaining_calls, context
                )

            if self._on_progress is not None:
                self._on_progress("tool_start", iteration + 1, max_iters)

            step_start = time.monotonic()
            gateway_results: list[ToolCallResult] = []
            if approved_calls:
                exec_results = yield _ToolCalls(approved_calls, context)
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
                logger.info(
                    "[tool-loop] iter=%d tool=%s success=%s elapsed=%dms",
                    iteration + 1, tc.tool_id, tr.success, step_elapsed,
                )
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
                    #
                    # A request without ``tools`` must not carry structured
                    # tool calls either: several providers refuse a history
                    # with tool-call / tool-result blocks when no tool is
                    # declared. The exchanges go to this call as plain text.
                    inj_kwargs = dict(kwargs)
                    inj_kwargs.pop("tools", None)
                    inj_messages = flatten_tool_exchanges(messages)
                    try:
                        inj_response, inj_chunks = yield _Blocking(
                            self._call_llm_sync, inj_messages, inj_kwargs, on_token,
                            live=sink.begin_round(),
                            reasoning=self._reasoning_for(iteration + 2),
                        )
                    except Exception:
                        sink.end_round()  # keep the last final_text
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
                        if inj_calls:
                            sink.end_round()
                        else:
                            sink.release(inj_text, inj_chunks)
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
            self._compact_loop_messages(
                messages, initial_msg_count, rounds=self._loop_compaction_rounds
            )
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
            sink.write_new if on_token is not None else None,
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
        return await self._drive_async(self._loop_steps(context, on_token, llm_config))

    # ── drivers of ``_loop_steps`` ─────────────────────────────────────

    def _drive(self, steps: Generator[_LoopRequest, Any, RunResult]) -> RunResult:
        """Serve the loop's requests on the calling thread."""
        try:
            request = next(steps)
            while True:
                try:
                    value = self._serve(request)
                except BaseException as exc:  # noqa: BLE001 - handed to the loop
                    request = steps.throw(exc)
                else:
                    request = steps.send(value)
        except StopIteration as done:
            result: RunResult = done.value
            return result

    async def _drive_async(
        self, steps: Generator[_LoopRequest, Any, RunResult]
    ) -> RunResult:
        """Serve the loop's requests without blocking the event loop."""
        try:
            request = next(steps)
            while True:
                try:
                    value = await self._serve_async(request)
                except BaseException as exc:  # noqa: BLE001 - handed to the loop
                    request = steps.throw(exc)
                else:
                    request = steps.send(value)
        except StopIteration as done:
            result: RunResult = done.value
            return result

    def _serve(self, request: _LoopRequest) -> Any:
        if isinstance(request, _ToolCalls):
            assert self._tool_gateway is not None
            return self._tool_gateway.execute_tool_calls(
                symbiote_id=request.context.symbiote_id,
                session_id=request.context.session_id,
                calls=request.calls,
                timeout=request.context.tool_call_timeout,
            )
        return request.fn(*request.args, **request.kwargs)

    async def _serve_async(self, request: _LoopRequest) -> Any:
        if isinstance(request, _ToolCalls):
            assert self._tool_gateway is not None
            return await self._tool_gateway.execute_tool_calls_async(
                symbiote_id=request.context.symbiote_id,
                session_id=request.context.session_id,
                calls=request.calls,
                timeout=request.context.tool_call_timeout,
            )
        # The LLM adapter and the approval callback are synchronous: a
        # worker thread keeps other sessions on this event loop running.
        return await asyncio.to_thread(request.fn, *request.args, **request.kwargs)

    def _needs_person(self, tool_id: str) -> bool:
        """Whether the tool's descriptor sets ``requires_approval``."""
        requires = getattr(self._tool_gateway, "requires_approval", None)
        # ``is True``: a gateway without the method (or a test double that
        # answers anything) keeps the old behaviour.
        return callable(requires) and requires(tool_id) is True

    def _risk_asks(self, risk: str) -> bool:
        """Whether a tool of this risk reaches the approval threshold."""
        order = {"low": 0, "medium": 1, "high": 2}
        return order.get(risk, 0) >= order[self._approval_risk_threshold]

    def approval_applies(self, tool_id: str) -> bool:
        """Whether a call of ``tool_id`` made in a turn goes to a person
        before it runs: the tool requires approval, or an approval callback
        is installed and the tool's risk reaches its threshold."""
        if self._tool_gateway is None:
            return False
        if self._needs_person(tool_id):
            return True
        if self._on_before_tool_call is None:
            return False
        return self._risk_asks(self._tool_gateway.get_risk_level(tool_id))

    def ask_approval(self, tool_id: str, params: dict) -> ApprovalDecision:
        """What the installed approval callback answers for one call
        (approve when none is installed). The caller binds the call's
        ``ToolExecContext``; the callback may block."""
        callback = self._on_before_tool_call
        if callback is None or self._tool_gateway is None:
            return ApprovalDecision.approve()
        risk = self._tool_gateway.get_risk_level(tool_id)
        return as_approval_decision(callback(tool_id, params, risk))

    def _check_approval(
        self,
        calls: list[ToolCall],
        context: AssembledContext | None = None,
    ) -> tuple[list[ToolCall], dict[int, ToolCallResult]]:
        """Check approval for tool calls at the configured risk threshold.

        Returns a tuple of (approved_calls, denial_results_by_index).
        Selected risks require approval via the on_before_tool_call callback.
        If the callback is None, all tools are auto-approved (backward compat),
        except those whose descriptor sets ``requires_approval``: they are
        refused, and with a callback they are always asked.

        With ``context`` (every call made by the loops), a high-risk call is
        first checked against policy and the host's tool guard
        (``ToolGateway.authorize``): nobody is asked to approve a call that
        would be refused anyway. The callback then runs inside the call's
        ``ToolExecContext``, so ``current_tool_context()`` works in it.
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
            if self._tool_gateway is None:
                approved.append(call)
                continue
            needs_person = self._needs_person(call.tool_id)
            if self._on_before_tool_call is None:
                if needs_person:
                    # Nobody can say yes: refuse rather than run unchecked.
                    denied[idx] = ToolCallResult(
                        call_id=call.call_id,
                        tool_id=call.tool_id,
                        success=False,
                        error=(
                            "This tool runs only with a person's approval, and no "
                            "approval callback is installed: it was not executed."
                        ),
                        risk_level=self._tool_gateway.get_risk_level(call.tool_id),
                    )
                else:
                    approved.append(call)
                continue
            risk = self._tool_gateway.get_risk_level(call.tool_id)
            if not self._risk_asks(risk) and not needs_person:
                approved.append(call)
                continue
            authorize = getattr(self._tool_gateway, "authorize", None)
            if context is not None and authorize is not None:
                refusal = authorize(context.symbiote_id, context.session_id, call)
                if refusal is not None:
                    denied[idx] = ToolCallResult(
                        call_id=call.call_id,
                        tool_id=call.tool_id,
                        success=False,
                        error=error_with_hint(refusal.error or "Tool call refused"),
                        risk_level=risk,
                    )
                    continue
            if context is not None:
                with tool_exec_context(context.symbiote_id, context.session_id):
                    answer = self._on_before_tool_call(call.tool_id, call.params, risk)
            else:
                answer = self._on_before_tool_call(call.tool_id, call.params, risk)
            decision = as_approval_decision(answer)
            if decision.outcome == "defer":
                denied[idx] = self._deferred_result(call, risk, decision, context)
                continue
            if decision.outcome != "approve":
                denied[idx] = ToolCallResult(
                    call_id=call.call_id,
                    tool_id=call.tool_id,
                    success=False,
                    error=(
                        f"Tool call denied: {decision.reason}"
                        if decision.reason
                        else "Tool call denied by approval callback"
                    ),
                    risk_level=risk,
                )
                continue
            approved.append(call)

        return approved, denied

    def _deferred_result(
        self,
        call: ToolCall,
        risk: str,
        decision: ApprovalDecision,
        context: AssembledContext | None,
    ) -> ToolCallResult:
        """The result of a call that waits for a person: not executed, the
        request recorded, the model told that it is pending."""
        handler = self._on_defer_tool_call
        approval_id: str | None = None
        error: str | None = None
        if handler is None or context is None:
            error = (
                "This tool needs a person's approval and nobody can give it "
                "now: it was not executed."
            )
        else:
            try:
                approval_id = handler(
                    call.tool_id, call.params, risk, decision,
                    context.symbiote_id, context.session_id,
                )
            except Exception:
                logger.exception("could not record the deferred call to %s", call.tool_id)
                error = (
                    "This tool needs a person's approval and the request could "
                    "not be recorded: it was not executed."
                )
        return ToolCallResult(
            call_id=call.call_id,
            tool_id=call.tool_id,
            success=False,
            error=error or decision.message or DEFAULT_PENDING_MESSAGE,
            risk_level=risk,
            pending_approval_id=approval_id,
        )

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
        elif stop_reason == "budget":
            replacement = _BUDGET_TEXT
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

    def _reasoning_for(self, call: int) -> Callable[[str], None] | None:
        """What hands the reasoning of model call ``call`` to the host."""
        on_reasoning = self._on_reasoning
        if on_reasoning is None:
            return None
        return lambda text: on_reasoning(text, call)

    def _call_llm_sync(
        self, messages: list[dict], kwargs: dict,
        on_token: Callable[[str], None] | None,
        *,
        live: Callable[[str], None] | None = None,
        reasoning: Callable[[str], None] | None = None,
    ) -> tuple[str | LLMResponse, list[str] | None]:
        """Call the LLM synchronously, with optional streaming.

        Returns a tuple of ``(response, buffered_chunks)`` where
        *buffered_chunks* holds the raw token sequence collected during
        streaming (``None`` when not streaming, or when only the reasoning
        is).  Tokens are **never** forwarded to *on_token* here — the caller
        must do so after the response has been validated. ``live`` (the
        live-mode filter of a ``TokenSink``) receives each chunk as it
        arrives. ``reasoning`` receives the text of every ``ReasoningDelta``
        the adapter yields; with it the call is streamed even without
        ``on_token``. Reasoning never joins the response or the chunks.
        """
        check_turn()  # no model call starts once the turn has to stop
        if (on_token is not None or reasoning is not None) and hasattr(self._llm, "stream"):
            chunks: list[str] = []
            response: str | LLMResponse = ""
            for item in self._llm.stream(messages, **kwargs):
                if isinstance(item, LLMResponse):
                    response = item
                elif isinstance(item, ReasoningDelta):
                    if reasoning is not None and item.text:
                        reasoning(item.text)
                else:
                    chunks.append(item)
                    if live is not None:
                        live(item)
            if not isinstance(response, LLMResponse):
                response = "".join(chunks)
            return response, chunks if on_token is not None else None
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
        sink: TokenSink | None = None,
        *,
        reasoning: Callable[[str], None] | None = None,
    ) -> tuple[str | LLMResponse, list[str] | None]:
        """Wrap ``_call_llm_sync`` with exponential-backoff retry logic.

        With a live ``sink`` each attempt is a round of its own: text a
        failed attempt already streamed stays, and the retry starts a new
        paragraph. ``reasoning`` goes to every attempt (see
        ``_call_llm_sync``).
        """
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_LLM_RETRIES + 1):
            live = sink.begin_round() if sink is not None else None
            try:
                return self._call_llm_sync(
                    messages, kwargs, on_token, live=live, reasoning=reasoning
                )
            except Exception as exc:
                if sink is not None:
                    sink.end_round()
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
        for attempt in range(1, _MAX_VALIDATION_RETRIES + 1):
            retry_messages = list(messages)
            # Empty assistant text is not a valid provider content block. A
            # reformulation needs only the non-empty user instruction; keep a
            # non-empty rejected answer when it can help the model fix itself.
            if clean_text.strip():
                retry_messages.append({"role": "assistant", "content": clean_text})
            retry_messages.append({"role": "user", "content": reformulate_message()})
            try:
                retry_response, retry_chunks = self._call_llm_sync(retry_messages, kwargs, None)
            except Exception as exc:
                # A kernel-wrapped adapter also records this failed call in
                # llm_usage; the warning keeps standalone runners observable.
                logger.warning(
                    "response reformulation attempt %d/%d failed: %s",
                    attempt,
                    _MAX_VALIDATION_RETRIES,
                    exc,
                    exc_info=True,
                )
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
        messages: list[dict],
        initial_count: int,
        *,
        rounds: int | None = _COMPACTION_THRESHOLD,
    ) -> None:
        """Replace old tool exchanges with a compact summary.

        Only compacts messages added during the tool loop (after
        ``initial_count``), once the loop has ``rounds`` assistant turns;
        ``None`` or ``0`` never compacts. Keeps the most recent complete
        exchange intact so native ``assistant`` + one-or-more ``tool``
        messages never become orphaned. Legacy assistant/user pairs retain
        their previous shape.

        Modifies *messages* in-place.
        """
        if not rounds:
            return
        loop_messages = messages[initial_count:]
        assistant_positions = [
            idx
            for idx, message in enumerate(loop_messages)
            if message.get("role") == "assistant"
        ]
        if len(assistant_positions) < rounds:
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
                logger.warning(
                    "tool result cut to fit the loop: %d of %d chars kept "
                    "(KernelConfig.loop_context_budget=%d tokens). Raise "
                    "loop_context_budget so the model reads the whole result.",
                    keep,
                    len(content),
                    self._context_budget,
                )
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
                content = msg.content
                if msg.role == "user":
                    content = label_origin(content, getattr(msg, "origin", None))
                messages.append({"role": msg.role, "content": content})

        # 3. Conversation history from extra_context (when working_memory is not used)
        #    The host passes "conversation_history" either as a list of
        #    {"role", "content"} turns (taken as they are) or, the older form,
        #    as "Role: content" lines. Both become real user/assistant
        #    messages so the LLM sees proper multi-turn.
        if self._working_memory is None and context.extra_context:
            history = context.extra_context.get("conversation_history")
            if history:
                messages.extend(self._history_messages(history))

        # 4. Current input (with ephemeral runtime context for the LLM). When
        #    the person did not write it, one line says so: it sits in the
        #    user position only because providers alternate user and assistant.
        user_content = label_origin(context.user_input, context.input_origin)
        if self._runtime_context != "off":
            user_content = inject_runtime_context(
                user_content,
                session_id=(
                    context.session_id if self._runtime_context == "full" else None
                ),
            )
        messages.append({"role": "user", "content": user_content})

        return messages

    @classmethod
    def _history_messages(cls, history: object) -> list[dict]:
        """Turn ``extra_context["conversation_history"]`` into messages.

        A list of ``{"role": "user" | "assistant", "content": str}`` is taken
        as it is: nothing inside a content is read as a role label, and blank
        lines and indentation are kept. Turns with an empty content are left
        out (providers refuse empty messages). A user turn may carry
        ``"origin"`` (what ``Message.origin`` holds): the model is then told
        that the user did not write it, as it was in the turn it started.

        A string is parsed as "Label: content" lines. That form cannot tell a
        label from a line of a message that merely starts like one ("User: "
        pasted by the user opens a new turn), so hosts that relay text they
        did not write should send the list.
        """
        if isinstance(history, str):
            return cls._parse_conversation_history(history)
        if not isinstance(history, (list, tuple)):
            raise ValidationError(
                "conversation_history must be a list of {'role', 'content'} "
                f"turns or a string, got {type(history).__name__}"
            )
        messages: list[dict] = []
        for index, turn in enumerate(history):
            if not isinstance(turn, Mapping):
                raise ValidationError(
                    f"conversation_history[{index}] must be a mapping with "
                    f"'role' and 'content', got {type(turn).__name__}"
                )
            role, content = turn.get("role"), turn.get("content")
            if role not in _HISTORY_ROLES:
                raise ValidationError(
                    f"conversation_history[{index}].role must be 'user' or "
                    f"'assistant', got {role!r}"
                )
            if not isinstance(content, str):
                raise ValidationError(
                    f"conversation_history[{index}].content must be a string, "
                    f"got {type(content).__name__}"
                )
            if not content.strip():
                continue
            if role == "user" and turn.get("origin") is not None:
                try:
                    origin = normalize_origin(turn.get("origin"))
                except ValidationError as exc:
                    raise ValidationError(
                        f"conversation_history[{index}].origin: {exc}"
                    ) from None
                content = label_origin(content, origin)
            messages.append({"role": role, "content": content})
        return messages

    @staticmethod
    def _parse_conversation_history(text: str) -> list[dict]:
        """Parse "Role: content" lines into LLM message dicts.

        Expected format (produced by YouNews clark.py):
            Usuário: message text
            Clark: response text

        Lines without a recognized label belong to the turn above them, with
        their indentation and the blank lines between them. Any line that
        starts with a recognized label opens a new turn, whoever wrote it:
        see ``_history_messages`` for the form without that ambiguity.
        """
        messages: list[dict] = []
        role_map = {
            "usuário": "user", "usuario": "user", "user": "user",
            "clark": "assistant", "assistant": "assistant",
        }
        current_role = None
        current_lines: list[str] = []

        def flush() -> None:
            # Blank lines around a turn only separate it from its neighbours.
            content = "\n".join(current_lines).strip("\n")
            if current_role and content:
                messages.append({"role": current_role, "content": content})

        for line in text.split("\n"):
            line = line.rstrip()
            stripped = line.lstrip()

            # Try to parse "Role: content"
            mapped = None
            if ": " in stripped:
                prefix, rest = stripped.split(": ", 1)
                mapped = role_map.get(prefix.lower())
            if mapped:
                flush()
                current_role = mapped
                current_lines = [rest]
            elif current_role:
                current_lines.append(line)

        flush()
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
            # Name only the tools this session kept (``tools_allowed``).
            kept = [
                (tool_id, what)
                for tool_id, what in (
                    ("search_memories", "historical context"),
                    ("search_knowledge", "domain knowledge"),
                )
                if tool_in_session(context.symbiote_id, tool_id)
            ]
            if len(kept) == 2:
                parts.append(
                    "## Context Access\n"
                    "You have access to search_memories and search_knowledge tools. "
                    "Use them when you need historical context or domain knowledge."
                )
            elif kept:
                tool_id, what = kept[0]
                parts.append(
                    "## Context Access\n"
                    f"You have access to the {tool_id} tool. "
                    f"Use it when you need {what}."
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
