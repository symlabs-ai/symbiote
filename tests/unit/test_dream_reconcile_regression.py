"""Regression: the deterministic dream keeps distinct memories that share
tags and never deactivates protected types (report item M-5)."""

from __future__ import annotations

from pathlib import Path

from symbiote.core.models import MemoryEntry
from tests.support import ScriptedLLM, make_kernel

RULES = [
    "Never deploy to production on Fridays without written approval",
    "Always run the linter before committing Python code",
    "Database passwords must rotate every ninety days",
    "Customer emails are answered within one business day",
]


def _active(kernel, symbiote_id: str) -> list[str]:
    rows = kernel._storage.fetch_all(
        "SELECT content FROM memory_entries WHERE symbiote_id = ? AND is_active = 1",
        (symbiote_id,),
    )
    return sorted(r["content"] for r in rows)


def test_distinct_constraints_with_same_tags_survive(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="r")
    for rule in RULES:
        kernel._memory.store(
            MemoryEntry(symbiote_id=sym.id, type="constraint", scope="global", source="reflection",
                        content=rule, tags=["constraint", "team-policy"], importance=0.8)
        )

    kernel.dream(sym.id)

    assert _active(kernel, sym.id) == sorted(RULES)
    kernel.shutdown()


def test_protected_duplicates_are_not_deactivated(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="r")
    for importance in (0.6, 0.9):
        kernel._memory.store(
            MemoryEntry(symbiote_id=sym.id, type="constraint", scope="global", source="reflection",
                        content="never deploy on fridays", tags=["deploy", "policy"],
                        importance=importance)
        )

    kernel.dream(sym.id)

    assert len(_active(kernel, sym.id)) == 2
    kernel.shutdown()


def test_reconciled_tag_is_added_once(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="r")
    for importance in (0.6, 0.9):
        kernel._memory.store(
            MemoryEntry(symbiote_id=sym.id, type="preference", scope="global", source="reflection",
                        content="user likes dark mode in editors", tags=["ui", "editor"],
                        importance=importance)
        )
    kernel._memory.store(
        MemoryEntry(symbiote_id=sym.id, type="preference", scope="global", source="reflection",
                    content="user likes dark mode in the editors", tags=["ui", "editor"],
                    importance=0.5)
    )

    kernel.dream(sym.id)
    kernel.dream(sym.id)

    row = kernel._storage.fetch_one(
        "SELECT tags_json FROM memory_entries WHERE symbiote_id = ? AND is_active = 1",
        (sym.id,),
    )
    assert row["tags_json"].count("dream:reconciled") == 1
    assert len(_active(kernel, sym.id)) == 1
    kernel.shutdown()
