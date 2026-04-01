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

import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from symbiote.core.context import AssembledContext
from symbiote.core.models import Message
from symbiote.core.ports import LLMPort
from symbiote.runners.base import (
    BlockResult,
    LongRunPlan,
    LoopStep,
    LoopTrace,
    RunResult,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from symbiote.environment.tools import ToolGateway
    from symbiote.memory.consolidator import MemoryConsolidator
    from symbiote.memory.working import WorkingMemory

_HANDLED_INTENTS = frozenset({"chat", "ask", "question", "talk"})

_DEFAULT_PLANNER_PROMPT = """\
You are a project planner. Given the user's request, create a structured plan
by decomposing it into concrete blocks of work.

Rules:
- Each block should be independently completable and verifiable
- Order blocks by dependency (foundational first, dependent later)
- Each block should have: name, description, success_criteria
- Be ambitious but realistic about scope
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
    ) -> None:
        self._llm = llm
        self._working_memory = working_memory
        self._tool_gateway = tool_gateway
        self._consolidator = consolidator
        self._native_tools = native_tools
        self._context_budget = context_budget
        self._evaluator_llm = evaluator_llm or llm
        self._on_progress = on_progress
        self._on_block_complete = on_block_complete

    def can_handle(self, intent: str) -> bool:
        return intent in _HANDLED_INTENTS

    def run(
        self,
        context: AssembledContext,
        on_token: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Execute a long-run project: plan -> generate blocks -> evaluate."""
        loop_start = time.monotonic()
        trace = LoopTrace(tool_mode="long_run")

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

        if self._on_progress is not None:
            self._on_progress("plan_complete", 0, plan.total_blocks)

        # ── Phase 2: Execute blocks ─────────────────────────────────────
        block_results: list[BlockResult] = []
        max_blocks = min(context.max_blocks, plan.total_blocks)
        accumulated_messages: list[dict] = []

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
                context, block_prompt, block_idx, block_name,
                accumulated_messages, on_token,
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
                        block_prompt, eval_result
                    )
                    block_result = self._execute_block(
                        context, retry_prompt, block_idx, block_name,
                        accumulated_messages, on_token,
                    )
                    block_result.elapsed_ms += int((time.monotonic() - block_start) * 1000)

                    # Re-evaluate after retry
                    eval_result = self._evaluate_block(
                        context, plan, block_def, block_result
                    )
                    block_result.evaluator_score = eval_result.get("overall_score")
                    block_result.evaluator_feedback = eval_result.get("feedback")

            block_results.append(block_result)

            if self._on_block_complete is not None:
                self._on_block_complete(block_result)

            if self._on_progress is not None:
                self._on_progress("block_end", block_idx + 1, max_blocks)

            # Context strategy between blocks
            if context.context_strategy in ("reset", "hybrid"):
                accumulated_messages.clear()

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

        return RunResult(
            success=completed > 0,
            output=output,
            runner_type=self.runner_type,
            loop_trace=trace,
            plan=plan,
            block_results=block_results,
        )

    # ── Planner ──────────────────────────────────────────────────────────

    def _run_planner(self, context: AssembledContext) -> LongRunPlan:
        """Phase 1: Expand user prompt into a structured work plan."""
        planner_prompt = context.planner_prompt or _DEFAULT_PLANNER_PROMPT

        messages = [
            {"role": "system", "content": planner_prompt},
            {"role": "user", "content": context.user_input},
        ]

        try:
            response = self._llm.complete(messages, config=context.generation_settings)
            raw = response if isinstance(response, str) else str(response)
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
        accumulated_messages: list[dict],
        on_token: Callable[[str], None] | None = None,
    ) -> BlockResult:
        """Execute a single block of work using the LLM with tool loop."""
        from symbiote.runners.chat import ChatRunner

        # Create a brief-mode ChatRunner for this block
        runner = ChatRunner(
            llm=self._llm,
            working_memory=None,  # managed at long-run level
            tool_gateway=self._tool_gateway,
            consolidator=self._consolidator,
            native_tools=self._native_tools,
            context_budget=self._context_budget,
        )

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

        result = runner.run(block_context, on_token=on_token)

        tool_calls = 0
        iterations = 0
        if result.loop_trace is not None:
            tool_calls = result.loop_trace.total_tool_calls
            iterations = result.loop_trace.total_iterations

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
            progress = []
            for b in completed_blocks:
                status = "DONE" if b.success else "FAILED"
                progress.append(f"- {b.block_name}: {status}")
                if b.evaluator_feedback:
                    progress.append(f"  Feedback: {b.evaluator_feedback}")
            parts.append("\n## Progress So Far\n" + "\n".join(progress))

        parts.append(
            "\n## Task\nExecute ONLY the current block. "
            "Use the available tools to complete the work described above. "
            "When done, summarize what you accomplished."
        )

        return "\n".join(parts)

    def _build_retry_prompt(
        self, original_prompt: str, eval_result: dict
    ) -> str:
        """Build a retry prompt incorporating evaluator feedback."""
        feedback = eval_result.get("feedback", "Quality below threshold")
        blocking = eval_result.get("blocking_issues", [])
        issues = "\n".join(f"- {issue}" for issue in blocking) if blocking else feedback

        return (
            f"{original_prompt}\n\n"
            f"## IMPORTANT: Previous attempt was rejected by QA\n"
            f"Issues to fix:\n{issues}\n\n"
            f"Address ALL issues above before completing this block."
        )

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
                f"Evaluate this work against the criteria above."
            )},
        ]

        try:
            response = self._evaluator_llm.complete(
                eval_messages, config=context.generation_settings
            )
            raw = response if isinstance(response, str) else str(response)
            return self._parse_eval_json(raw)
        except Exception as exc:
            logger.warning("[long-run] evaluator failed: %s", exc)
            return {"passed": True, "overall_score": 0.5, "feedback": f"Evaluator error: {exc}"}

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
