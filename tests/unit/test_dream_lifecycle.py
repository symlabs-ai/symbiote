"""Regression: dream cycles respect each symbiote's config, never overlap,
ignore dry runs and do not regenerate the same generalization (report item
M-6)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from symbiote.core.models import MemoryEntry
from symbiote.dream.engine import DreamEngine
from tests.support import ScriptedLLM, make_kernel


def _reports(kernel, symbiote_id: str, dry_run: int | None = None) -> int:
    sql = "SELECT COUNT(*) c FROM dream_reports WHERE symbiote_id = ?"
    params: tuple = (symbiote_id,)
    if dry_run is not None:
        sql += " AND dry_run = ?"
        params += (dry_run,)
    return kernel._storage.fetch_one(sql, params)["c"]


def _wait_dreams(kernel) -> None:
    for thread in list(getattr(kernel, "_dream_threads", {}).values()):
        thread.join(timeout=10)


def _close_n(kernel, symbiote_id: str, n: int) -> None:
    for _ in range(n):
        session = kernel.start_session(symbiote_id)
        kernel.close_session(session.id)


@pytest.mark.parametrize("scope", ["global", "per_symbiote"])
def test_dreams_never_overlap_for_a_symbiote(tmp_path: Path, monkeypatch, scope: str) -> None:
    running = 0
    peak = 0
    lock = threading.Lock()
    real_dream = DreamEngine.dream

    def slow_dream(self, symbiote_id, dream_mode):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.3)
        try:
            return real_dream(self, symbiote_id, dream_mode)
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(DreamEngine, "dream", slow_dream)
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), skill_scope=scope)
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.environment.configure(symbiote_id=sym.id, dream_mode="light", dream_min_sessions=1)

    _close_n(kernel, sym.id, 4)
    _wait_dreams(kernel)

    assert peak == 1
    kernel.shutdown()


def test_each_symbiote_uses_its_own_min_sessions(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    a = kernel.create_symbiote(name="a", role="r")
    b = kernel.create_symbiote(name="b", role="r")
    kernel.environment.configure(symbiote_id=a.id, dream_mode="light", dream_min_sessions=50)
    kernel.environment.configure(symbiote_id=b.id, dream_mode="light", dream_min_sessions=2)

    _close_n(kernel, a.id, 1)  # A builds whatever engine first
    _close_n(kernel, b.id, 2)
    _wait_dreams(kernel)

    assert _reports(kernel, a.id) == 0
    assert _reports(kernel, b.id) == 1  # fires on the 2nd close, not the 3rd
    kernel.shutdown()


def test_dry_run_does_not_reset_the_session_count(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.environment.configure(symbiote_id=sym.id, dream_mode="light", dream_min_sessions=2)
    _close_n(kernel, sym.id, 1)

    kernel.dream(sym.id, dry_run=True)
    _close_n(kernel, sym.id, 1)
    _wait_dreams(kernel)

    assert _reports(kernel, sym.id, dry_run=0) == 1
    kernel.shutdown()


def test_generalization_is_not_repeated(tmp_path: Path) -> None:
    llm = ScriptedLLM("Always check the deploy checklist before shipping.")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.environment.configure(symbiote_id=sym.id, dream_mode="full")
    for text in ("run the tests before deploy", "check migrations before deploy",
                 "announce the deploy in the channel"):
        kernel._memory.store(MemoryEntry(symbiote_id=sym.id, type="procedural", scope="global",
                                         source="reflection", content=text,
                                         tags=["deploy", "checklist"], importance=0.6))

    kernel.dream(sym.id)
    kernel.dream(sym.id)

    rows = kernel._storage.fetch_all(
        "SELECT id FROM memory_entries WHERE symbiote_id = ? AND tags_json LIKE '%dream:generalized%'",
        (sym.id,),
    )
    assert len(rows) == 1
    kernel.shutdown()


def test_two_clusters_in_one_dream_are_both_generalized(tmp_path: Path) -> None:
    # Regression (found by mypy in TK-83): the LLM text overwrote the list of
    # existing generalizations, so the second cluster crashed on str.tags.
    llm = ScriptedLLM(lambda i, m: f"General rule number {i}.")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.environment.configure(symbiote_id=sym.id, dream_mode="full")
    groups = {
        ("deploy", "checklist"): ("run the tests before deploy", "check migrations before deploy",
                                  "announce the deploy in the channel"),
        ("billing", "invoice"): ("send invoices on the first day", "attach the invoice pdf",
                                 "mark the invoice as sent"),
    }
    for tags, texts in groups.items():
        for text in texts:
            kernel._memory.store(MemoryEntry(symbiote_id=sym.id, type="procedural", scope="global",
                                             source="reflection", content=text,
                                             tags=list(tags), importance=0.6))

    kernel.dream(sym.id)

    rows = kernel._storage.fetch_all(
        "SELECT content FROM memory_entries WHERE symbiote_id = ? AND tags_json LIKE '%dream:generalized%'",
        (sym.id,),
    )
    assert len(rows) == 2
    kernel.shutdown()


@pytest.mark.parametrize(
    ("persona", "rule"),
    [
        ({"language": "português do Brasil"}, "Write in português do Brasil."),
        (None, "Write in the language of the memories or conversation shown."),
    ],
)
def test_what_the_dream_writes_follows_the_symbiote_language(
    tmp_path: Path, persona: dict | None, rule: str
) -> None:
    llm = ScriptedLLM("Antes de reservar, compare opções e salve o comprovante.")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="r", persona=persona)
    kernel.environment.configure(symbiote_id=sym.id, dream_mode="full")
    for text in ("reservar hotel: comparar 3 opções", "comprar passagem: comparar 2 sites",
                 "alugar carro: comparar 3 locadoras"):
        kernel._memory.store(MemoryEntry(symbiote_id=sym.id, type="procedural", scope="global",
                                         source="reflection", content=text,
                                         tags=["viagem", "reserva"], importance=0.6))

    kernel.dream(sym.id)

    assert llm.calls, "the generalize phase asked nothing"
    assert rule in llm.prompt_text(0)
    kernel.shutdown()
