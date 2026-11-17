"""The kernel works without SymGateway.

SymGateway is one optional way to reach a model and a search engine. Nothing
in the kernel may import it or need its settings: this fails if someone ties
the two together.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from symbiote.core.models import MemoryEntry
from tests.support import ScriptedLLM, make_kernel

_GATEWAY_MODULES = (
    "symbiote.adapters.llm.symgateway",
    "symbiote.browser.search.providers.brave",
    "symbiote.browser.search.providers.firecrawl",
    "symbiote.browser.search.providers.symgateway_objective",
)
_SRC = str(Path(__file__).resolve().parents[2] / "src")


def test_importing_the_kernel_and_the_web_tools_loads_no_gateway_module() -> None:
    code = (
        "import sys\n"
        "import symbiote.core.kernel, symbiote.api.http, symbiote.browser, symbiote.sdk.client\n"
        f"loaded = [m for m in {_GATEWAY_MODULES!r} if m in sys.modules]\n"
        "print(loaded)\n"
    )
    result = subprocess.run(  # noqa: S603 — our own interpreter, fixed code
        [sys.executable, "-c", code],
        capture_output=True, text=True, check=True,
        env={"PYTHONPATH": _SRC, "PATH": "", "SYMBIOTE_DEV_MODE": "1"},
    )

    assert result.stdout.strip() == "[]"


@pytest.fixture()
def no_gateway(monkeypatch):
    """Every gateway module refuses to import and no gateway setting exists."""
    for name in _GATEWAY_MODULES:
        monkeypatch.setitem(sys.modules, name, None)  # ``import`` raises ImportError
    for name in ("SYMGATEWAY_BASE_URL", "SYMGATEWAY_API_KEY", "SYMGATEWAY_PROJECT_SLUG"):
        monkeypatch.delenv(name, raising=False)


def test_a_turn_a_close_and_a_dream_run_with_the_gateway_out_of_reach(
    tmp_path: Path, no_gateway
) -> None:
    with pytest.raises(ImportError):
        import symbiote.adapters.llm.symgateway  # noqa: F401

    main = ScriptedLLM("ok")
    aux = ScriptedLLM(lambda _i, _m: "Always check the deploy checklist.")
    kernel = make_kernel(tmp_path, main)
    try:
        kernel.set_evolver_llm(aux)
        sym = kernel.create_symbiote(name="S", role="assistant")
        kernel.environment.configure(
            symbiote_id=sym.id, dream_mode="full", session_summary_mode="llm"
        )
        for text in ("run the tests before deploy", "check migrations before deploy",
                     "announce the deploy in the channel"):
            kernel.memory.store(MemoryEntry(
                symbiote_id=sym.id, type="procedural", scope="global", source="reflection",
                content=text, tags=["deploy", "checklist"], importance=0.6,
            ))
        session = kernel.start_session(sym.id)

        outcome = kernel.turn(session.id, "Please remember that I prefer short answers.")
        closed = kernel.close_session(session.id)
        report = kernel.dream(sym.id)

        assert outcome.text == "ok"
        assert closed.status == "closed"
        assert report.total_llm_calls > 0
        usage = kernel.inspector.usage_by("purpose", sym.id)
        assert {"turn", "session_summary"} <= {row["purpose"] for row in usage}
    finally:
        kernel.shutdown()


def test_web_search_runs_with_the_gateway_out_of_reach(tmp_path: Path, no_gateway) -> None:
    import asyncio

    from symbiote.browser import register

    class Engine:
        name = "host-search"

        async def search(self, query: str, *, limit: int = 5):
            return [{"url": "https://example.com", "title": "T", "snippet": query}]

    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    try:
        register(kernel, search_backend=Engine())
        sym = kernel.create_symbiote(name="S", role="assistant")
        kernel.environment.configure(symbiote_id=sym.id, tools=["web_search"])

        result = asyncio.run(kernel.tool_gateway.execute_async(
            symbiote_id=sym.id, session_id=None, tool_id="web_search",
            params={"query": "symbiotes"}, timeout=10.0,
        ))

        assert result.success, result.error
        assert result.output["results"][0]["snippet"] == "symbiotes"
    finally:
        kernel.shutdown()
