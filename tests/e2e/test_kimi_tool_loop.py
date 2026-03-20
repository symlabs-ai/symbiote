"""E2E tests for Kimi K2 tool loop with real YouNews tools.

Tests the full tool loop: Kimi K2 → tool calls → YouNews API (localhost:8000) → response.
Requires YouNews running at localhost:8000 and discovered tools in symbiote.db.

Usage:
    SYMBIOTE_E2E_LLM=1 pytest tests/e2e/test_kimi_tool_loop.py -v
"""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path

import pytest

from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.runners.chat import ChatRunner

_SKIP_LLM = "Set SYMBIOTE_E2E_LLM=1 to run LLM integration tests"
_SKIP_YOUNEWS = "YouNews not running at localhost:8000"

_YOUNEWS_HOST = "127.0.0.1"
_YOUNEWS_PORT = 8000
_YOUNEWS_BASE_URL = f"http://{_YOUNEWS_HOST}:{_YOUNEWS_PORT}"

_TOOL_TAGS = ["Items", "Inbox", "Search", "Journals"]

skip_unless_llm = pytest.mark.skipif(
    os.environ.get("SYMBIOTE_E2E_LLM") != "1",
    reason=_SKIP_LLM,
)


def _younews_reachable() -> bool:
    """Check if YouNews is listening on localhost:8000."""
    try:
        with socket.create_connection((_YOUNEWS_HOST, _YOUNEWS_PORT), timeout=2):
            return True
    except OSError:
        return False


skip_unless_younews = pytest.mark.skipif(
    not _younews_reachable(),
    reason=_SKIP_YOUNEWS,
)


def _make_kernel(tmp_path: Path) -> SymbioteKernel:
    """Create a kernel with Kimi K2 and tools from symbiote.db."""
    from symbiote.adapters.llm.forge import ForgeLLMAdapter

    # Copy production DB for isolation (keeps discovered tools)
    src_db = Path(__file__).resolve().parents[2] / ".symbiote" / "symbiote.db"
    if not src_db.exists():
        src_db = Path(__file__).resolve().parents[2] / "symbiote.db"
    assert src_db.exists(), f"symbiote.db not found at {src_db}"

    dst_db = tmp_path / "test_harness.db"
    shutil.copy2(src_db, dst_db)

    provider = os.environ.get("SYMBIOTE_LLM_PROVIDER", "symgateway")
    model = os.environ.get("SYMBIOTE_LLM_MODEL", "moonshotai/kimi-k2-instruct")

    llm = ForgeLLMAdapter(provider=provider, model=model)
    config = KernelConfig(db_path=dst_db, context_budget=16000)
    kernel = SymbioteKernel(config=config, llm=llm)

    # Replace default ChatRunner with native_tools=True
    kernel._runner_registry._runners = [
        r for r in kernel._runner_registry._runners if r.runner_type != "chat"
    ]
    kernel._runner_registry.register(
        ChatRunner(llm, tool_gateway=kernel._tool_gateway, native_tools=True)
    )

    return kernel


def _setup_clark(kernel: SymbioteKernel) -> str:
    """Find Clark or create a test symbiote, load tools, return symbiote_id."""
    clark = kernel.find_symbiote_by_name("Clark")
    if clark:
        clark_id = clark.id
    else:
        sym = kernel.create_symbiote(
            name="Clark",
            role="younews_assistant",
            persona={"tone": "friendly", "language": "pt-BR"},
        )
        clark_id = sym.id

    # Load discovered tools from DB
    tool_ids = kernel.load_discovered_tools(clark_id, base_url=_YOUNEWS_BASE_URL)
    assert len(tool_ids) > 0, "No discovered tools found in symbiote.db"

    # Configure visibility with tags
    kernel.configure_tool_visibility(clark_id, tags=_TOOL_TAGS, loop=True)
    return clark_id


@skip_unless_llm
@skip_unless_younews
class TestKimiToolLoop:
    """Kimi K2 tool loop with real YouNews tools at localhost:8000."""

    def test_tool_loop_list_items(self, tmp_path: Path) -> None:
        """Kimi should call a tool to list inbox items."""
        kernel = _make_kernel(tmp_path)
        try:
            clark_id = _setup_clark(kernel)
            session = kernel.start_session(symbiote_id=clark_id, goal="test tool loop")

            response = kernel.message(
                session_id=session.id,
                content="Liste os itens do meu inbox. Responda em português.",
            )

            # Expect a dict with tool_results
            assert isinstance(response, dict), f"Expected dict, got {type(response)}"
            text = response.get("text", "")
            tool_results = response.get("tool_results", [])

            assert len(text) > 0, "Empty response text"
            assert len(tool_results) > 0, (
                f"No tool_results — Kimi didn't call any tool. Response: {text[:200]}"
            )

            # At least one tool succeeded
            successes = [tr for tr in tool_results if tr.get("success")]
            assert len(successes) > 0, f"All tools failed: {tool_results}"

            kernel.close_session(session.id)
        finally:
            kernel.shutdown()

    def test_tool_loop_multi_step(self, tmp_path: Path) -> None:
        """Kimi should chain multiple tool calls (list → detail)."""
        kernel = _make_kernel(tmp_path)
        try:
            clark_id = _setup_clark(kernel)
            session = kernel.start_session(symbiote_id=clark_id, goal="multi-step test")

            response = kernel.message(
                session_id=session.id,
                content=(
                    "Quais são meus jornais? "
                    "Depois de listar, me diga quantos itens tem o primeiro jornal."
                ),
            )

            assert isinstance(response, dict), f"Expected dict, got {type(response)}"
            tool_results = response.get("tool_results", [])
            assert len(tool_results) >= 1, "Expected at least 1 tool call for multi-step"

            kernel.close_session(session.id)
        finally:
            kernel.shutdown()
