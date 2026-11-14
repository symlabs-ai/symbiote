"""LongRunRunner — runner for project-scale, multi-block tasks.

Implements a Planner -> Generator -> Evaluator architecture inspired by:
- Anthropic "Harness Design for Long-Running Application Development" (2026)
- Meta-Harness paper (Stanford/CMU, 2026)
- Ralph Loop research (fresh context philosophy)

Each phase is optional — the host activates what it needs:
- Without planner: host provides the spec directly via extra_context
- Without evaluator: generator self-evaluates (less robust, cheaper)
- With evaluator: GAN-inspired separation (who does != who judges)

The runner operates in blocks of work. Each block:
1. (Optional) Negotiates a sprint contract with the evaluator
2. Executes via the ChatRunner (brief-mode loop internally)
3. (Optional) Gets evaluated by a separate LLM call
4. Persists progress for handoff

Context strategy between blocks is configurable:
- compaction: keep accumulated context, compact aggressively (70%)
- reset: fresh context per block, re-inject plan + progress summary
- hybrid: compaction within blocks, reset between blocks (default)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING

from symbiote.agent_protocols import (
    AgentProtocol,
    AgentProtocolRegistry,
    create_default_protocol_registry,
)
from symbiote.core.context import AssembledContext
from symbiote.core.models import Message
from symbiote.core.ports import LLMPort
from symbiote.core.turn_control import TurnStop, check_turn
from symbiote.runners.base import (
    BlockResult,
    LongRunPlan,
    LoopStep,
    LoopTrace,
    RunResult,
)
from symbiote.runners.token_stream import TOKEN_STREAMING_MODES

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from symbiote.environment.tools import ToolGateway
    from symbiote.memory.consolidator import MemoryConsolidator
    from symbiote.memory.working import WorkingMemory

_HANDLED_INTENTS = frozenset({"chat", "ask", "question", "talk"})
# How much of a previous block's output the "compaction" strategy carries.
_CARRIED_OUTPUT_CHARS = 2000


def _response_text(response: object) -> str:
    """Text of an adapter response (``str`` or ``LLMResponse``)."""
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    return content if isinstance(content, str) else str(response)

_DEFAULT_PLANNER_PROMPT = """\
You are a project planner. Given the user's request, create a structured plan
by decomposing it into concrete blocks of work.

Rules:
- Each block should be independently completable and verifiable
- Order blocks by dependency (foundational first, dependent later)
- Each block should have: name, description, success_criteria
- Be ambitious but realistic about scope
- Do not include implementation details in descriptions — focus on deliverables
  and observable outcomes; implementation details cascade errors to the generator
- If a previous session handoff is provided, identify completed blocks and plan
  only the remaining work — do not re-plan what is already done
- Return ONLY a JSON array of blocks, no other text

Example output:
[
  {"name": "Data model", "description": "Define database schema and models", "success_criteria": "All tables created, relationships defined"},
  {"name": "API endpoints", "description": "Implement REST API", "success_criteria": "All CRUD endpoints working with tests"},
  {"name": "Frontend", "description": "Build user interface", "success_criteria": "All pages render, forms submit correctly"}
]"""

_DEFAULT_EVALUATOR_PROMPT = """\
You are a strict quality evaluator. Review the work completed in this block
and grade it against the success criteria.

Rules:
- Be skeptical — do not assume things work just because the generator says so
- Grade each criterion on a 0.0-1.0 scale
- Provide specific, actionable feedback for anything below 0.8
- If any criterion is below the threshold, the block FAILS
- It is unacceptable to mark a block as passed unless every success criterion
  is explicitly verified — do not assume things work because the generator claims so
- If you cannot verify a criterion, mark it as failed with a blocking_issue
- Return ONLY a JSON object with scores and feedback

Example output:
{
  "passed": false,
  "overall_score": 0.65,
  "criteria_scores": {"completeness": 0.8, "correctness": 0.5},
  "feedback": "The API endpoints are defined but error handling is missing. Add try/catch for database operations.",
  "blocking_issues": ["No error handling in POST endpoints"]
}"""


class LongRunRunner:
    """Runner for project-scale tasks using Planner/Generator/Evaluator."""

    runner_type: str = "long_run"

    def __init__(
        self,
        llm: LLMPort,
        working_memory: WorkingMemory | None = None,
        tool_gateway: ToolGateway | None = None,
        consolidator: MemoryConsolidator | None = None,
        native_tools: bool = False,
        context_budget: int = 16000,
        evaluator_llm: LLMPort | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_block_complete: Callable[[BlockResult], None] | None = None,
        on_plan: Callable[[LongRunPlan], None] | None = None,
        on_before_tool_call: Callable[[str, dict, str], bool] | None = None,
        *,
        # Handed to the ChatRunner of each block (see ChatRunner).
        tool_result_max_chars: int | None = None,
        runtime_context: str = "full",
        agent_protocol: str | AgentProtocol | None = None,
        protocol_registry: AgentProtocolRegistry | None = None,
        background_review: object | None = None,
        skill_nudge_interval: int = 10,
    ) -> None:
        self._tool_result_max_chars = tool_result_max_chars
        self._runtime_context = runtime_context
        # Blocks run in the kernel's protocol and nudge its skill review
        # like a plain chat turn does (B-74).
        self._protocol_registry = (
            protocol_registry or create_default_protocol_registry()
        )
        self._agent_protocol = self._protocol_registry.resolve(agent_protocol)
        self._background_review = background_review
        self._skill_nudge_interval = skill_nudge_interval
        # Tool iterations since the last skill review, carried across blocks
        # (each block gets a fresh ChatRunner).
        self._iters_since_skill_review = 0
        self._llm = llm
        self._working_memory = working_memory
        self._tool_gateway = tool_gateway
        self._consolidator = consolidator
        self._native_tools = native_tools
        self._context_budget = context_budget
        self._evaluator_llm = evaluator_llm or llm
        self._on_progress = on_progress
        self._on_block_complete = on_block_complete
        self._on_plan = on_plan
        self._on_before_tool_call = on_before_tool_call
        self._token_streaming = "final"

    def set_native_tools(self, enabled: bool) -> None:
        """Use the provider's structured tool calls instead of text parsing."""
        self._native_tools = enabled

    @property
    def agent_protocol_id(self) -> str:
        """The protocol the blocks run in."""
        return self._agent_protocol.protocol_id

    def set_agent_protocol(self, protocol: str | AgentProtocol | None) -> None:
        """Protocol of the block runners; ``None`` restores the legacy default."""
        self._agent_protocol = self._protocol_registry.resolve(protocol)

    def set_token_streaming(self, mode: str) -> None:
        """How block text reaches ``on_token`` (see ``ChatRunner``)."""
        if mode not in TOKEN_STREAMING_MODES:
            raise ValueError(
                f"token_streaming must be one of {TOKEN_STREAMING_MODES}, got {mode!r}"
            )
        self._token_streaming = mode

    def set_approval_callback(
        self, callback: Callable[[str, dict, str], bool] | None
    ) -> None:
        """Gate for high-risk tools inside every block (see ``ChatRunner``)."""
        self._on_before_tool_call = callback

    def set_progress_callbacks(
        self,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_block_complete: Callable[[BlockResult], None] | None = None,
        on_plan: Callable[[LongRunPlan], None] | None = None,
    ) -> None:
        """Install (or clear, with ``None``) the run's callbacks.

        ``on_progress(event, index, total)`` receives ``plan_complete``,
        ``block_start`` and ``block_end``; ``on_plan(plan)`` receives the
        planner's blocks before the first one runs; ``on_block_complete``
        receives each ``BlockResult`` after evaluation.
        """
        self._on_progress = on_progress
        self._on_block_complete = on_block_complete
        self._on_plan = on_plan

    def can_handle(self, intent: str) -> bool:
        return intent in _HANDLED_INTENTS

    async def run_async(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
    ) -> RunResult:
        """Run the (synchronous) long-run in a worker thread.

        ``on_token`` is called from that thread.
        """
        return await asyncio.to_thread(
            self.run, context, on_token=on_token, llm_config=llm_config
        )

    def run(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
    ) -> RunResult:
        """Execute a long-run project: plan -> generate blocks -> evaluate.

        ``llm_config`` (per-call overrides, e.g. effort) is merged over
        ``context.generation_settings`` for the planner and evaluator calls
        and forwarded to every block.

        A turn that has to stop (``TurnStop``: cancelled, or its session
        lock was lost) leaves with the trace of the blocks done so far.
        """
        trace = LoopTrace(tool_mode="long_run")
        started = time.monotonic()
        try:
            return self._run_project(context, on_token, llm_config, trace)
        except TurnStop as stop:
            trace.total_iterations = len({step.iteration for step in trace.steps})
            trace.total_tool_calls = len(trace.steps)
            trace.total_elapsed_ms = int((time.monotonic() - started) * 1000)
            trace.stop_reason = stop.reason
            stop.trace = trace
            raise

    def _run_project(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None,
        llm_config: dict | None,
        trace: LoopTrace,
    ) -> RunResult:
        """The project itself; see ``run``."""
        loop_start = time.monotonic()
        self._llm_config = llm_config
        self._call_config = {**(context.generation_settings or {}), **(llm_config or {})} or None

        # ── Phase 1: Planning ───────────────────────────────────────────
        plan = self._run_planner(context)
        if not plan.blocks:
            return RunResult(
                success=False,
                error="Planner produced no blocks of work",
                runner_type=self.runner_type,
                plan=plan,
            )

        logger.info(
            "[long-run] plan: %d blocks from prompt '%s'",
            plan.total_blocks, context.user_input[:80],
        )

        if self._on_plan is not None:
            try:
                self._on_plan(plan)
            except Exception:
                logger.exception("[long-run] on_plan callback failed")

        if self._on_progress is not None:
            self._on_progress("plan_complete", 0, plan.total_blocks)

        # ── Phase 2: Execute blocks ─────────────────────────────────────
        block_results: list[BlockResult] = []
        max_blocks = min(context.max_blocks, plan.total_blocks)

        # B3: Pre-flight — verify tools with health_check before spending tokens
        preflight_error = self._preflight_tools(context)
        if preflight_error:
            return RunResult(
                success=False,
                error=f"Pre-flight check failed: {preflight_error}",
                runner_type=self.runner_type,
            )

        for block_idx in range(max_blocks):
            block_def = plan.blocks[block_idx]
            block_name = block_def.get("name", f"Block {block_idx + 1}")

            if self._on_progress is not None:
                self._on_progress("block_start", block_idx + 1, max_blocks)

            logger.info("[long-run] block %d/%d: %s", block_idx + 1, max_blocks, block_name)

            # Build block prompt with context of what's been done
            block_prompt = self._build_block_prompt(
                context, plan, block_def, block_results
            )

            # Execute the block using a brief-mode ChatRunner
            block_start = time.monotonic()
            block_result = self._execute_block(
                context, block_prompt, block_idx, block_name, on_token,
            )
            block_result.elapsed_ms = int((time.monotonic() - block_start) * 1000)

            # Record trace steps
            for _step in range(block_result.tool_calls):
                trace.steps.append(LoopStep(
                    iteration=block_idx + 1,
                    tool_id=f"block:{block_name}",
                    params={"block_index": block_idx},
                    success=block_result.success,
                    elapsed_ms=block_result.elapsed_ms,
                ))

            # ── Phase 3: Evaluate block ─────────────────────────────────
            if context.evaluator_prompt is not None and block_result.success:
                eval_result = self._evaluate_block(
                    context, plan, block_def, block_result
                )
                block_result.evaluator_score = eval_result.get("overall_score")
                block_result.evaluator_feedback = eval_result.get("feedback")

                if not eval_result.get("passed", True):
                    logger.info(
                        "[long-run] block %d FAILED evaluation: %s",
                        block_idx + 1, eval_result.get("feedback", "")[:100],
                    )
                    # Retry once with evaluator feedback
                    retry_prompt = self._build_retry_prompt(
                        block_prompt, eval_result, block_result
                    )
                    rejected = block_result
                    block_result = self._execute_block(
                        context, retry_prompt, block_idx, block_name, on_token,
                    )
                    block_result.elapsed_ms += int((time.monotonic() - block_start) * 1000)
                    # The rejected attempt's calls took effect too: the block's
                    # evidence is both attempts, or a retry that only fixes the
                    # report would be judged as having done nothing.
                    block_result.tool_steps = rejected.tool_steps + block_result.tool_steps
                    block_result.tool_calls += rejected.tool_calls
                    block_result.iterations += rejected.iterations

                    # Re-evaluate after retry
                    eval_result = self._evaluate_block(
                        context, plan, block_def, block_result
                    )
                    block_result.evaluator_score = eval_result.get("overall_score")
                    block_result.evaluator_feedback = eval_result.get("feedback")

                if not eval_result.get("passed", True):
                    # Rejected even after the retry: the evaluator's verdict
                    # wins over the generator's own claim of success.
                    block_result.success = False

            block_results.append(block_result)

            if self._on_block_complete is not None:
                self._on_block_complete(block_result)

            if self._on_progress is not None:
                self._on_progress("block_end", block_idx + 1, max_blocks)

            # Stop early if block failed and no evaluator to retry
            if not block_result.success and context.evaluator_prompt is None:
                logger.info("[long-run] block %d failed, stopping", block_idx + 1)
                break

        # ── Finalize ────────────────────────────────────────────────────
        total_elapsed = int((time.monotonic() - loop_start) * 1000)
        completed = sum(1 for b in block_results if b.success)

        trace.total_iterations = len(block_results)
        trace.total_tool_calls = sum(b.tool_calls for b in block_results)
        trace.total_elapsed_ms = total_elapsed
        trace.stop_reason = (
            "end_turn" if completed == max_blocks
            else "block_failure"
        )

        # Build final output summary
        output = self._build_output_summary(plan, block_results)

        # Save to working memory
        if self._working_memory is not None:
            summary = f"[Long-run: {completed}/{max_blocks} blocks completed]\n{output}"
            self._working_memory.update_message(
                Message(session_id=context.session_id, role="assistant", content=summary)
            )
            if self._consolidator is not None:
                self._consolidator.consolidate_if_needed(
                    self._working_memory, context.symbiote_id
                )

        logger.info(
            "[long-run] completed: %d/%d blocks, %dms total",
            completed, max_blocks, total_elapsed,
        )

        # Build handoff artifact for session resume (L-04)
        handoff_data = {
            "user_input": context.user_input,
            "blocks_completed": completed,
            "blocks_total": max_blocks,
            "plan_blocks": plan.blocks,
            "block_results": [
                {
                    "name": b.block_name,
                    "success": b.success,
                    "output": b.output,
                    "evaluator_score": b.evaluator_score,
                    "evaluator_feedback": b.evaluator_feedback,
                }
                for b in block_results
            ],
            # Blocks not completed successfully, in plan order (failures may
            # be anywhere, not only at the tail).
            "pending_blocks": [
                block
                for idx, block in enumerate(plan.blocks)
                if idx not in {b.block_index for b in block_results if b.success}
            ],
            "output_summary": output,
        }

        return RunResult(
            success=completed > 0,
            error=None if completed > 0 else output,
            output=output,
            runner_type=self.runner_type,
            loop_trace=trace,
            plan=plan,
            block_results=block_results,
            handoff_data=handoff_data,
        )

    # ── Planner ──────────────────────────────────────────────────────────

    def _run_planner(self, context: AssembledContext) -> LongRunPlan:
        """Phase 1: Expand user prompt into a structured work plan."""
        planner_prompt = context.planner_prompt or _DEFAULT_PLANNER_PROMPT

        # B2: If resuming from a previous session, orient the planner with handoff data
        ph = (context.extra_context or {}).get("previous_handoff")
        # Only a handoff with work left orients the plan; with nothing pending
        # "plan ONLY the pending blocks" yields an empty plan.
        if isinstance(ph, dict) and ph.get("pending_blocks"):
            done = [b.get("name") for b in ph.get("block_results", []) if b.get("success")]
            pending = [b.get("name") for b in ph.get("pending_blocks", [])]
            planner_prompt = (
                planner_prompt
                + "\n\n## Previous Session Handoff\n"
                "Read this before creating your plan:\n"
                f"- Original task: {ph.get('user_input', '')}\n"
                f"- Completed blocks: {done}\n"
                f"- Pending blocks: {pending}\n"
                "Plan ONLY the pending blocks. Do not re-plan completed work.\n"
            )

        messages = [
            {"role": "system", "content": planner_prompt},
            {"role": "user", "content": context.user_input},
        ]

        check_turn()  # no model call starts once the turn has to stop
        try:
            response = self._llm.complete(messages, config=self._config_for(context))
            raw = _response_text(response)
        except Exception as exc:
            logger.warning("[long-run] planner failed: %s", exc)
            return LongRunPlan(raw_spec=str(exc))

        # Parse JSON blocks from response
        blocks = self._parse_plan_json(raw)
        return LongRunPlan(
            blocks=blocks,
            total_blocks=len(blocks),
            raw_spec=raw,
        )

    @staticmethod
    def _parse_plan_json(raw: str) -> list[dict]:
        """Extract JSON block array from planner response."""
        # Try direct parse
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from markdown fences
        import re
        json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass

        # Try line-by-line for JSON array
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("["):
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    continue

        logger.warning("[long-run] planner output not parseable as JSON blocks")
        return []

    # ── Generator (block execution) ──────────────────────────────────────

    def _execute_block(
        self,
        context: AssembledContext,
        block_prompt: str,
        block_index: int,
        block_name: str,
        on_token: Callable[[str], None] | None = None,
    ) -> BlockResult:
        """Execute a single block of work using the LLM with tool loop."""
        from symbiote.runners.chat import ChatRunner

        # Create a brief-mode ChatRunner for this block
        limits: dict = {}
        if self._tool_result_max_chars is not None:
            limits["tool_result_max_chars"] = self._tool_result_max_chars
        runner = ChatRunner(
            llm=self._llm,
            working_memory=None,  # managed at long-run level
            tool_gateway=self._tool_gateway,
            consolidator=self._consolidator,
            native_tools=self._native_tools,
            context_budget=self._context_budget,
            runtime_context=self._runtime_context,
            **limits,
            # High-risk tools inside a block go through the same human gate
            # as a plain chat turn.
            on_before_tool_call=self._on_before_tool_call,
            token_streaming=self._token_streaming,
            agent_protocol=self._agent_protocol,
            protocol_registry=self._protocol_registry,
            background_review=self._background_review,
            skill_nudge_interval=self._skill_nudge_interval,
        )
        runner._iters_since_skill_review = self._iters_since_skill_review

        # Build a brief context for the block
        block_context = AssembledContext(
            symbiote_id=context.symbiote_id,
            session_id=context.session_id,
            persona=context.persona,
            working_memory_snapshot=context.working_memory_snapshot,
            relevant_memories=context.relevant_memories,
            relevant_knowledge=context.relevant_knowledge,
            available_tools=context.available_tools,
            tool_loading=context.tool_loading,
            tool_mode="brief",  # blocks execute as brief
            tool_loop=True,
            max_tool_iterations=context.max_tool_iterations,
            tool_call_timeout=context.tool_call_timeout,
            loop_timeout=context.loop_timeout,
            tool_instructions_override=context.tool_instructions_override,
            injection_stagnation_override=context.injection_stagnation_override,
            injection_circuit_breaker_override=context.injection_circuit_breaker_override,
            context_mode=context.context_mode,
            extra_context=context.extra_context,
            user_input=block_prompt,
            generation_settings=context.generation_settings,
        )

        try:
            result = runner.run(
                block_context,
                on_token=on_token,
                llm_config=getattr(self, "_llm_config", None),
            )
        finally:
            self._iters_since_skill_review = runner._iters_since_skill_review

        tool_calls = 0
        iterations = 0
        steps: list[LoopStep] = []
        if result.loop_trace is not None:
            tool_calls = result.loop_trace.total_tool_calls
            iterations = result.loop_trace.total_iterations
            steps = list(result.loop_trace.steps)

        # Extract text output
        if isinstance(result.output, dict):
            text = result.output.get("text", str(result.output))
        else:
            text = str(result.output) if result.output else ""

        return BlockResult(
            block_index=block_index,
            block_name=block_name,
            success=result.success,
            output=text,
            error=result.error,
            iterations=iterations,
            tool_calls=tool_calls,
            tool_steps=steps,
        )

    def _build_block_prompt(
        self,
        context: AssembledContext,
        plan: LongRunPlan,
        block_def: dict,
        completed_blocks: list[BlockResult],
    ) -> str:
        """Build the prompt for a block, including plan context and progress."""
        parts = [
            f"## Project Plan\n{json.dumps(plan.blocks, indent=2)}",
            f"\n## Current Block: {block_def.get('name', 'Unnamed')}",
            f"Description: {block_def.get('description', '')}",
            f"Success criteria: {block_def.get('success_criteria', '')}",
        ]

        if completed_blocks:
            # context_strategy: "compaction" carries each previous block's
            # output forward; "reset" / "hybrid" start each block fresh with
            # only the status and evaluator feedback of earlier blocks.
            carry_output = context.context_strategy == "compaction"
            progress = []
            for b in completed_blocks:
                status = "DONE" if b.success else "FAILED"
                progress.append(f"- {b.block_name}: {status}")
                if b.evaluator_feedback:
                    progress.append(f"  Feedback: {b.evaluator_feedback}")
                if carry_output and b.output:
                    progress.append(f"  Output: {b.output[:_CARRIED_OUTPUT_CHARS]}")
            parts.append("\n## Progress So Far\n" + "\n".join(progress))

        parts.append(
            "\n## Constraints\n"
            "Work ONLY on the current block. Do not implement anything outside its scope.\n"
            "Do not report this block as complete until all success_criteria are explicitly satisfied.\n"
            "If you are uncertain whether a criterion is met, it is not met.\n"
            "\n## Task\nExecute the current block using the available tools. "
            "When done, summarize what you accomplished."
        )

        return "\n".join(parts)

    def _build_retry_prompt(
        self,
        original_prompt: str,
        eval_result: dict,
        previous: BlockResult | None = None,
    ) -> str:
        """Build a retry prompt incorporating evaluator feedback.

        The tool calls of the rejected attempt already ran (tasks created,
        files written, messages sent), so they are listed for the retry to
        build on instead of repeating them.
        """
        feedback = eval_result.get("feedback", "Quality below threshold")
        blocking = eval_result.get("blocking_issues", [])
        issues = "\n".join(f"- {issue}" for issue in blocking) if blocking else feedback
        done = ""
        if previous is not None and previous.tool_steps:
            done = (
                "\n## Tool calls from the rejected attempt (they already took effect)\n"
                f"{_steps_digest(previous.tool_steps)}\n"
                "Do not repeat them: their effects are in place. Check the current "
                "state with read-only tools if needed and only add what is missing.\n"
                "Your final summary reports the whole block, these calls included.\n"
            )

        return (
            f"{original_prompt}\n\n"
            f"## IMPORTANT: Previous attempt was rejected by QA\n"
            f"Issues to fix:\n{issues}\n"
            f"{done}\n"
            f"Address ALL issues above before completing this block."
        )

    # ── Pre-flight ───────────────────────────────────────────────────────

    def _preflight_tools(self, context: AssembledContext) -> str | None:
        """B3: Check tool availability before starting blocks.

        For every tool offered to the model (``context.available_tools``),
        the handler registered in the gateway is checked; handlers exposing a
        ``health_check()`` callable are called. Tools without one are skipped
        (non-breaking).

        Returns an error message if any check fails, otherwise None.
        """
        if self._tool_gateway is None or not context.available_tools:
            return None
        registry = getattr(self._tool_gateway, "_registry", {})
        for tool in context.available_tools:
            tool_id = tool.get("tool_id") or tool.get("name")
            handler = registry.get(tool_id)
            check = getattr(handler, "health_check", None)
            if callable(check):
                try:
                    check()
                except Exception as exc:
                    return f"Tool '{tool_id}' unavailable: {exc}"
        return None

    def _config_for(self, context: AssembledContext) -> dict | None:
        return getattr(self, "_call_config", None) or context.generation_settings

    # ── Evaluator ────────────────────────────────────────────────────────

    def _evaluate_block(
        self,
        context: AssembledContext,
        plan: LongRunPlan,
        block_def: dict,
        block_result: BlockResult,
    ) -> dict:
        """Phase 3: Evaluate a completed block with separate LLM."""
        evaluator_prompt = context.evaluator_prompt or _DEFAULT_EVALUATOR_PROMPT

        # Build evaluation context
        criteria = context.evaluator_criteria or []
        criteria_text = ""
        if criteria:
            criteria_text = "\n## Evaluation Criteria\n"
            for c in criteria:
                criteria_text += f"- **{c.get('name', '')}** (weight: {c.get('weight', 1.0)}, threshold: {c.get('threshold', 0.7)}): {c.get('description', '')}\n"

        eval_messages = [
            {"role": "system", "content": evaluator_prompt},
            {"role": "user", "content": (
                f"## Block: {block_def.get('name', '')}\n"
                f"Description: {block_def.get('description', '')}\n"
                f"Success criteria: {block_def.get('success_criteria', '')}\n"
                f"{criteria_text}\n"
                f"## Generator Output\n{block_result.output}\n\n"
                f"{_evidence_section(block_result)}"
                f"Evaluate this work against the criteria above."
            )},
        ]

        check_turn()
        try:
            response = self._evaluator_llm.complete(
                eval_messages, config=self._config_for(context)
            )
            result = self._parse_eval_json(_response_text(response))
        except Exception as exc:
            logger.warning("[long-run] evaluator failed: %s", exc)
            return {"passed": True, "overall_score": 0.5, "feedback": f"Evaluator error: {exc}"}
        return _enforce_counts(result, block_result.tool_steps)

    @staticmethod
    def _parse_eval_json(raw: str) -> dict:
        """Extract evaluation JSON from evaluator response."""
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        import re
        json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Fallback: assume passed
        return {"passed": True, "overall_score": 0.5, "feedback": raw[:500]}

    # ── Output ───────────────────────────────────────────────────────────

    @staticmethod
    def _build_output_summary(
        plan: LongRunPlan, block_results: list[BlockResult]
    ) -> str:
        """Build a human-readable summary of the long-run execution."""
        completed = sum(1 for b in block_results if b.success)
        total = plan.total_blocks
        lines = [f"Project completed: {completed}/{total} blocks"]

        for b in block_results:
            status = "DONE" if b.success else "FAILED"
            score = f" (score: {b.evaluator_score:.2f})" if b.evaluator_score is not None else ""
            lines.append(f"  [{status}] {b.block_name}{score}")
            if b.evaluator_feedback and not b.success:
                lines.append(f"    Feedback: {b.evaluator_feedback}")

        return "\n".join(lines)


# ── Evidence of executed tool calls ──────────────────────────────────────────

_DIGEST_MAX_STEPS = 60
_DIGEST_MAX_PARAMS = 160


def _steps_digest(steps: list[LoopStep]) -> str:
    """One line per executed tool call: status, tool, compact params."""
    lines = []
    for step in steps[:_DIGEST_MAX_STEPS]:
        params = json.dumps(step.params, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(params) > _DIGEST_MAX_PARAMS:
            params = params[: _DIGEST_MAX_PARAMS - 1] + "…"
        status = "ok" if step.success else f"FAILED: {(step.error or '')[:120]}"
        lines.append(f"- [{status}] {step.tool_id} {params}")
    if len(steps) > _DIGEST_MAX_STEPS:
        lines.append(f"- … and {len(steps) - _DIGEST_MAX_STEPS} more")
    return "\n".join(lines)


def _steps_totals(steps: list[LoopStep]) -> str:
    """Calls per tool, over every step (the digest is capped).

    A model reading a long list does not count it: an evaluator handed 74
    ``create_task`` lines approved a block whose output claimed 87.
    """
    totals: dict[str, list[int]] = {}
    for step in steps:
        calls_ok = totals.setdefault(step.tool_id, [0, 0])
        calls_ok[0] += 1
        calls_ok[1] += 1 if step.success else 0
    return "; ".join(
        f"{tool}: {calls} calls, {ok} ok" for tool, (calls, ok) in totals.items()
    )


def _evidence_section(block_result: BlockResult) -> str:
    """Tool calls recorded by the runtime, for the evaluator to verify claims.

    Without it the evaluator sees only the generator's text, cannot verify
    work done through tools, and rejects it — and the retry repeats the
    side effects.
    """
    if not block_result.tool_steps:
        return (
            "## Tool calls executed in this block (recorded by the runtime)\n"
            "None. Any claim of actions taken through tools is unsupported.\n\n"
        )
    return (
        "## Tool calls executed in this block (recorded by the runtime, "
        "not claimed by the generator)\n"
        f"Totals: {_steps_totals(block_result.tool_steps)}\n"
        f"{_steps_digest(block_result.tool_steps)}\n"
        "These calls really ran; [ok] means the tool reported success. "
        "Use them as evidence when checking the criteria.\n"
        "Check every number the generator states about its own actions "
        "(items created, sent, deleted, updated) against the totals above. "
        "A count that does not match is a correctness error: lower that score "
        "and give the real number in the feedback.\n"
        f"{_COUNT_CLAIMS_REQUEST}\n\n"
    )


# The evaluator reads the counts out of free text; the runtime checks them.
_COUNT_CLAIMS_REQUEST = (
    'Also add to your JSON a "count_claims" list, one entry per number the '
    "output states about actions of this block where each item is one tool "
    'call: {"tool": "<tool name exactly as in the totals, or null if no call '
    'above did that action>", "claimed": <the number stated>}. Skip numbers '
    "that include work from before this block and items a single call "
    "handled in bulk. Use [] when the output states no such number."
)
# Score ceiling of a block whose report contradicts the recorded calls.
_COUNT_MISMATCH_MAX_SCORE = 0.5


def _count_mismatches(claims: object, steps: list[LoopStep]) -> list[str]:
    """Counts the evaluator read from the output that the trace contradicts.

    Each claim is checked against the successful calls of its tool. A claim
    naming a tool that never ran is not checked (the evaluator may have
    misspelled it); ``null`` means no call did the action, so any count
    above zero is wrong.
    """
    if not isinstance(claims, list):
        return []
    called = {step.tool_id for step in steps}
    ok = Counter(step.tool_id for step in steps if step.success)
    issues = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claimed, tool = claim.get("claimed"), claim.get("tool")
        if isinstance(claimed, bool) or not isinstance(claimed, int | float):
            continue
        if claimed != int(claimed) or claimed < 0:
            continue
        if tool is None:
            if claimed > 0:
                issues.append(
                    f"The output reports {int(claimed)} of an action no tool call "
                    "recorded in this block did. Report only what the calls did."
                )
            continue
        if not isinstance(tool, str) or tool not in called:
            continue
        if int(claimed) != ok[tool]:
            issues.append(
                f"The output reports {int(claimed)} for {tool}, but the runtime "
                f"recorded {ok[tool]} successful {tool} calls in this block. Fix the "
                "number in the summary; the calls already took effect, do not repeat "
                "or undo them to match the text."
            )
    return issues


def _enforce_counts(result: dict, steps: list[LoopStep]) -> dict:
    """Fail a block whose report contradicts the calls the runtime recorded.

    In a real run the evaluator wrote "claims 24 tasks, the calls show 28"
    in its feedback and still passed the block with 0.95.
    """
    if not isinstance(result, dict):
        return result
    mismatches = _count_mismatches(result.get("count_claims"), steps)
    if not mismatches:
        return result
    score = result.get("overall_score")
    blocking = result.get("blocking_issues")
    return {
        **result,
        "passed": False,
        "overall_score": (
            min(score, _COUNT_MISMATCH_MAX_SCORE)
            if isinstance(score, int | float) and not isinstance(score, bool)
            else _COUNT_MISMATCH_MAX_SCORE
        ),
        "blocking_issues": [*(blocking if isinstance(blocking, list) else []), *mismatches],
        "feedback": " ".join(filter(None, [str(result.get("feedback") or ""), *mismatches])),
        "count_mismatches": mismatches,
    }

