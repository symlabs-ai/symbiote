"""E2E multi-model test matrix for tool loop validation — B-35.

Tests the tool loop across multiple LLM providers/models with 3 scenarios:
  1. Simple tool call (calculator)
  2. Multi-step task (search + get_item)
  3. Error recovery (tool fails first, succeeds on retry)

Collects metrics: iterations, tool_calls, success, stop_reason, elapsed_ms.

Usage:
    SYMBIOTE_E2E_LLM=1 pytest tests/e2e/test_multi_model.py -v -s
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.runners.chat import ChatRunner

_SKIP_REASON = (
    "Set SYMBIOTE_E2E_LLM=1 and ensure forge-llm is configured "
    "to run multi-model tests"
)

skip_unless_llm = pytest.mark.skipif(
    os.environ.get("SYMBIOTE_E2E_LLM") != "1",
    reason=_SKIP_REASON,
)

# ---------------------------------------------------------------------------
# Model matrix — each entry runs through every scenario
# ---------------------------------------------------------------------------

MODEL_MATRIX: list[dict[str, str]] = [
    {"provider": "symgateway", "model": "anthropic/claude-sonnet-4-20250514"},
    {"provider": "symgateway", "model": "openai/gpt-4.1-mini"},
    {"provider": "symgateway", "model": "moonshotai/kimi-k2-instruct"},
]

# ---------------------------------------------------------------------------
# Metrics dataclass
# ---------------------------------------------------------------------------


@dataclass
class ScenarioMetrics:
    model: str
    scenario: str
    iterations: int = 0
    tool_calls: int = 0
    success: bool = False
    stop_reason: str = ""
    elapsed_ms: int = 0
    wasted_iterations: int = 0  # iterations after task was logically complete


# Global collector — printed at session end
_collected_metrics: list[ScenarioMetrics] = []

# ---------------------------------------------------------------------------
# Tool definitions (deterministic handlers)
# ---------------------------------------------------------------------------

_CALCULATOR_DESC = ToolDescriptor(
    tool_id="calculator",
    name="Calculator",
    description="Add two numbers and return the sum.",
    parameters={
        "type": "object",
        "properties": {
            "a": {"type": "number", "description": "First number"},
            "b": {"type": "number", "description": "Second number"},
        },
        "required": ["a", "b"],
    },
)

_SEARCH_DESC = ToolDescriptor(
    tool_id="search_items",
    name="Search Items",
    description="Search for items by query. Returns a list of item summaries with IDs.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    },
)

_GET_ITEM_DESC = ToolDescriptor(
    tool_id="get_item_details",
    name="Get Item Details",
    description="Get full details of an item by its ID.",
    parameters={
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "The item ID to fetch"},
        },
        "required": ["item_id"],
    },
)

_FLAKY_DESC = ToolDescriptor(
    tool_id="flaky_service",
    name="Flaky Service",
    description="Retrieve status from an external service. May fail transiently.",
    parameters={
        "type": "object",
        "properties": {
            "service_name": {
                "type": "string",
                "description": "Name of the service to query",
            },
        },
        "required": ["service_name"],
    },
)


def _calculator_handler(params: dict) -> Any:
    return {"result": params["a"] + params["b"]}


def _search_handler(params: dict) -> Any:
    return {
        "results": [
            {"id": "item-101", "title": "Wireless Mouse", "score": 0.95},
            {"id": "item-202", "title": "Mechanical Keyboard", "score": 0.87},
        ]
    }


def _get_item_handler(params: dict) -> Any:
    items = {
        "item-101": {
            "id": "item-101",
            "title": "Wireless Mouse",
            "price": 29.99,
            "in_stock": True,
        },
        "item-202": {
            "id": "item-202",
            "title": "Mechanical Keyboard",
            "price": 89.99,
            "in_stock": False,
        },
    }
    item_id = params.get("item_id", "")
    if item_id in items:
        return items[item_id]
    return {"error": f"Item {item_id} not found"}


class _FlakyHandler:
    """Fails on the first call, succeeds on subsequent calls."""

    def __init__(self) -> None:
        self._call_count = 0

    def __call__(self, params: dict) -> Any:
        self._call_count += 1
        if self._call_count == 1:
            raise RuntimeError("Service temporarily unavailable — please retry")
        return {"status": "healthy", "service": params.get("service_name", "unknown")}


# ---------------------------------------------------------------------------
# Kernel factory
# ---------------------------------------------------------------------------


def _make_kernel(
    tmp_path: Path, provider: str, model: str
) -> SymbioteKernel:
    """Create a kernel with a real LLM and deterministic tools."""
    from symbiote.adapters.llm.forge import ForgeLLMAdapter

    llm = ForgeLLMAdapter(provider=provider, model=model)
    config = KernelConfig(db_path=tmp_path / "multi_model.db")
    kernel = SymbioteKernel(config=config, llm=llm)

    # Replace default ChatRunner with native_tools=True for proper tool calling
    kernel._runner_registry._runners = [
        r for r in kernel._runner_registry._runners if r.runner_type != "chat"
    ]
    kernel._runner_registry.register(
        ChatRunner(llm, tool_gateway=kernel._tool_gateway, native_tools=True)
    )

    return kernel


def _register_tools(
    kernel: SymbioteKernel,
    symbiote_id: str,
    descriptors: list[tuple[ToolDescriptor, Any]],
) -> None:
    """Register tool descriptors + handlers and authorize them."""
    tool_ids = []
    for desc, handler in descriptors:
        kernel.tool_gateway.register_descriptor(desc, handler=handler)
        tool_ids.append(desc.tool_id)
    kernel.environment.configure(symbiote_id=symbiote_id, tools=tool_ids, loop=True)


# ---------------------------------------------------------------------------
# Metrics extraction helper
# ---------------------------------------------------------------------------


def _extract_metrics(
    response: Any,
    model: str,
    scenario: str,
    elapsed_ms: int,
    *,
    expected_min_tool_calls: int = 1,
) -> ScenarioMetrics:
    """Build ScenarioMetrics from a kernel.message response."""
    m = ScenarioMetrics(model=model, scenario=scenario, elapsed_ms=elapsed_ms)

    if isinstance(response, dict):
        tool_results = response.get("tool_results", [])
        m.tool_calls = len(tool_results)
        m.success = any(tr.get("success") for tr in tool_results)
        text = response.get("text", "")
    else:
        text = str(response)
        m.tool_calls = 0
        m.success = len(text) > 0

    # Estimate iterations from tool call count (each iteration = 1 LLM call)
    # The tool loop does 1 iteration per LLM round; if no tools were called it's 1
    m.iterations = max(m.tool_calls, 1)

    # Wasted iterations: tool calls beyond the expected minimum
    if m.tool_calls > expected_min_tool_calls:
        m.wasted_iterations = m.tool_calls - expected_min_tool_calls

    m.stop_reason = "end_turn"
    return m


# ---------------------------------------------------------------------------
# Parametrized test class
# ---------------------------------------------------------------------------


@skip_unless_llm
class TestMultiModelToolLoop:
    """Run 3 tool-loop scenarios across every model in MODEL_MATRIX."""

    @pytest.mark.parametrize(
        "model_entry",
        MODEL_MATRIX,
        ids=[f"{m['provider']}/{m['model']}" for m in MODEL_MATRIX],
    )
    def test_simple_calc(self, tmp_path: Path, model_entry: dict) -> None:
        """Scenario 1: Simple tool call — 'What is 10 + 5?'"""
        provider, model = model_entry["provider"], model_entry["model"]
        kernel = _make_kernel(tmp_path, provider, model)
        try:
            sym = kernel.create_symbiote(name="CalcBot", role="assistant")
            _register_tools(kernel, sym.id, [(_CALCULATOR_DESC, _calculator_handler)])

            session = kernel.start_session(symbiote_id=sym.id, goal="simple calc")

            t0 = time.monotonic()
            response = kernel.message(
                session_id=session.id,
                content="What is 10 + 5? Use the calculator tool to compute it.",
            )
            elapsed = int((time.monotonic() - t0) * 1000)

            m = _extract_metrics(
                response, model, "simple_calc", elapsed, expected_min_tool_calls=1
            )
            _collected_metrics.append(m)

            # Assertions
            assert m.success, f"[{model}] Calculator tool was not called successfully"
            assert m.tool_calls >= 1, f"[{model}] Expected at least 1 tool call"

            # Verify the result contains the correct answer
            if isinstance(response, dict):
                tool_results = response.get("tool_results", [])
                outputs = [tr.get("output") for tr in tool_results if tr.get("success")]
                assert any(
                    "15" in str(o) for o in outputs
                ), f"[{model}] Expected 15 in tool outputs: {outputs}"

            kernel.close_session(session.id)
        finally:
            kernel.shutdown()

    @pytest.mark.parametrize(
        "model_entry",
        MODEL_MATRIX,
        ids=[f"{m['provider']}/{m['model']}" for m in MODEL_MATRIX],
    )
    def test_multi_step(self, tmp_path: Path, model_entry: dict) -> None:
        """Scenario 2: Multi-step — search then get details."""
        provider, model = model_entry["provider"], model_entry["model"]
        kernel = _make_kernel(tmp_path, provider, model)
        try:
            sym = kernel.create_symbiote(name="SearchBot", role="assistant")
            _register_tools(
                kernel,
                sym.id,
                [
                    (_SEARCH_DESC, _search_handler),
                    (_GET_ITEM_DESC, _get_item_handler),
                ],
            )

            session = kernel.start_session(symbiote_id=sym.id, goal="multi-step")

            t0 = time.monotonic()
            response = kernel.message(
                session_id=session.id,
                content=(
                    "Search for 'mouse', then get the full details of the first result. "
                    "Use search_items first, then get_item_details with the ID from the search."
                ),
            )
            elapsed = int((time.monotonic() - t0) * 1000)

            m = _extract_metrics(
                response, model, "multi_step", elapsed, expected_min_tool_calls=2
            )
            _collected_metrics.append(m)

            # Assertions
            assert m.tool_calls >= 2, (
                f"[{model}] Expected at least 2 tool calls (search + get), got {m.tool_calls}"
            )

            if isinstance(response, dict):
                tool_results = response.get("tool_results", [])
                tool_ids_called = [tr.get("tool_id") for tr in tool_results]
                assert "search_items" in tool_ids_called, (
                    f"[{model}] search_items not called: {tool_ids_called}"
                )
                assert "get_item_details" in tool_ids_called, (
                    f"[{model}] get_item_details not called: {tool_ids_called}"
                )

            kernel.close_session(session.id)
        finally:
            kernel.shutdown()

    @pytest.mark.parametrize(
        "model_entry",
        MODEL_MATRIX,
        ids=[f"{m['provider']}/{m['model']}" for m in MODEL_MATRIX],
    )
    def test_error_recovery(self, tmp_path: Path, model_entry: dict) -> None:
        """Scenario 3: Error recovery — tool fails first, LLM should retry."""
        provider, model = model_entry["provider"], model_entry["model"]
        kernel = _make_kernel(tmp_path, provider, model)
        try:
            sym = kernel.create_symbiote(name="RetryBot", role="assistant")
            flaky = _FlakyHandler()
            _register_tools(kernel, sym.id, [(_FLAKY_DESC, flaky)])

            session = kernel.start_session(symbiote_id=sym.id, goal="error recovery")

            t0 = time.monotonic()
            response = kernel.message(
                session_id=session.id,
                content=(
                    "Check the status of service 'payments' using the flaky_service tool. "
                    "If it fails, retry the same call."
                ),
            )
            elapsed = int((time.monotonic() - t0) * 1000)

            m = _extract_metrics(
                response, model, "error_recovery", elapsed, expected_min_tool_calls=2
            )
            _collected_metrics.append(m)

            # The tool should have been called at least twice (1 fail + 1 success)
            assert m.tool_calls >= 2, (
                f"[{model}] Expected at least 2 tool calls (fail + retry), got {m.tool_calls}"
            )

            if isinstance(response, dict):
                tool_results = response.get("tool_results", [])
                failures = [tr for tr in tool_results if not tr.get("success")]
                successes = [tr for tr in tool_results if tr.get("success")]
                assert len(failures) >= 1, (
                    f"[{model}] Expected at least 1 failure: {tool_results}"
                )
                assert len(successes) >= 1, (
                    f"[{model}] Expected at least 1 success after retry: {tool_results}"
                )

            kernel.close_session(session.id)
        finally:
            kernel.shutdown()


# ---------------------------------------------------------------------------
# Session-scoped summary table (printed with pytest -s)
# ---------------------------------------------------------------------------


def pytest_sessionfinish(session, exitstatus):
    """Print the metrics summary table at the end of the test run."""
    if not _collected_metrics:
        return

    # Header
    header = (
        f"{'Model':<45} | {'Scenario':<16} | {'Iters':>5} | {'Tools':>5} "
        f"| {'Success':>7} | {'Stop':<10} | {'Elapsed':>8} | {'Wasted':>6}"
    )
    sep = "-" * len(header)
    lines = [
        "",
        "=" * len(header),
        "MULTI-MODEL TEST MATRIX — RESULTS",
        "=" * len(header),
        header,
        sep,
    ]

    for m in _collected_metrics:
        model_short = m.model
        if len(model_short) > 44:
            model_short = "..." + model_short[-41:]
        lines.append(
            f"{model_short:<45} | {m.scenario:<16} | {m.iterations:>5} | "
            f"{m.tool_calls:>5} | {str(m.success):>7} | {m.stop_reason:<10} | "
            f"{m.elapsed_ms:>6}ms | {m.wasted_iterations:>6}"
        )

    lines.append(sep)
    lines.append("")

    print("\n".join(lines))
