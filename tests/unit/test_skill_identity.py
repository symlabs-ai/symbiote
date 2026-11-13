"""Regression: a skill's directory and its frontmatter name always agree, the
loader sees skill_manage writes at once, and skill limits are enforced
(report item M-8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.skills import usage
from symbiote.skills.loader import SkillsLoader
from symbiote.skills.store import SkillsStore, SkillValidationError
from tests.support import ScriptedLLM, make_kernel


def _md(name: str, body: str = "body") -> str:
    return f"---\nname: {name}\ndescription: d\n---\n{body}\n"


def _store(tmp_path: Path) -> SkillsStore:
    return SkillsStore(roots=[tmp_path / "skills"])


@pytest.mark.parametrize(
    "content",
    [
        "playbook without frontmatter",
        _md("other-name"),
        "---\ndescription: no name\n---\nbody",
    ],
)
def test_create_requires_matching_frontmatter(tmp_path: Path, content: str) -> None:
    with pytest.raises(SkillValidationError):
        _store(tmp_path).create("deploy", content)
    assert not (tmp_path / "skills" / "deploy").exists()


def test_edit_and_patch_cannot_rename_through_frontmatter(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create("deploy", _md("deploy"))
    with pytest.raises(SkillValidationError):
        store.edit("deploy", _md("shadow"))
    with pytest.raises(SkillValidationError):
        store.patch("deploy", "name: deploy", "name: shadow")
    store.patch("deploy", "body", "new body")  # body edits still work


def test_name_with_trailing_newline_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SkillValidationError):
        _store(tmp_path).create("abc\n", _md("abc"))


def test_skill_manage_create_is_visible_to_skill_view(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.environment.configure(symbiote_id=sym.id, tools=["skill_manage", "skill_view"])

    created = kernel.tool_gateway.execute(
        sym.id, None, "skill_manage", {"action": "create", "name": "notes", "content": _md("notes")}
    )
    viewed = kernel.tool_gateway.execute(sym.id, None, "skill_view", {"name": "notes"})

    assert json.loads(created.output)["success"] is True
    assert "not found" not in str(viewed.output).lower()
    kernel.shutdown()


def test_max_active_skills_blocks_promotion(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillsStore(roots=[root])
    for name in ("one", "two"):
        store.create(name, _md(name))
        usage.set_status(root / name, usage.STATUS_ACTIVE)
    store.create("candidate", _md("candidate"))
    usage.set_status(root / "candidate", usage.STATUS_QUARANTINE)
    loader = SkillsLoader(tmp_path, auto_promote_threshold=1)
    loader.set_max_active_skills(2)

    loader.get_skill("candidate")
    loader.get_skill("candidate")

    assert usage.read_meta(root / "candidate")["status"] == usage.STATUS_QUARANTINE


def test_store_counts_quarantine_from_disk(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillsStore(roots=[root])
    store.create("q1", _md("q1"))
    usage.set_status(root / "q1", usage.STATUS_QUARANTINE)
    store.create("a1", _md("a1"))
    usage.set_status(root / "a1", usage.STATUS_ACTIVE)

    assert store.count_by_status() == {"active": 1, "quarantine": 1}
