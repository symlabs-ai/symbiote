"""Tests for SkillsStore — agent-managed skill CRUD with provenance + lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.core.provenance import (
    BACKGROUND_REVIEW,
    reset_current_write_origin,
    set_current_write_origin,
)
from symbiote.skills import usage
from symbiote.skills.store import (
    MAX_NAME_LENGTH,
    SkillExistsError,
    SkillNotFoundError,
    SkillProtectedError,
    SkillsStore,
    SkillValidationError,
)

# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def agent_root(tmp_path: Path) -> Path:
    p = tmp_path / "agent"
    p.mkdir()
    return p


@pytest.fixture()
def store(agent_root: Path) -> SkillsStore:
    return SkillsStore(roots=[agent_root])


_VALID_SKILL_MD = """\
---
name: my-skill
description: Does something useful
---
# My Skill

Body of the skill.
"""


# ── validation ────────────────────────────────────────────────────────────


class TestValidation:
    def test_create_requires_name(self, store):
        with pytest.raises(SkillValidationError, match="Skill name is required"):
            store.create("", _VALID_SKILL_MD)

    def test_create_rejects_invalid_chars(self, store):
        for bad in ("My-Skill", "skill name", "skill/sub", "-leading", "skill!"):
            with pytest.raises(SkillValidationError, match="Invalid skill name"):
                store.create(bad, _VALID_SKILL_MD)

    def test_create_rejects_name_too_long(self, store):
        name = "a" * (MAX_NAME_LENGTH + 1)
        with pytest.raises(SkillValidationError, match="exceeds"):
            store.create(name, _VALID_SKILL_MD)

    def test_create_requires_content(self, store):
        with pytest.raises(SkillValidationError, match="content is required"):
            store.create("my-skill", "")

    def test_write_file_path_traversal_blocked(self, store):
        store.create("my-skill", _VALID_SKILL_MD)
        for evil in ("../etc/passwd", "references/../../../etc", "/abs/path"):
            with pytest.raises(SkillValidationError):
                store.write_file("my-skill", evil, "x")

    def test_write_file_rejects_disallowed_subdir(self, store):
        store.create("my-skill", _VALID_SKILL_MD)
        with pytest.raises(SkillValidationError, match="must start with"):
            store.write_file("my-skill", "secrets/api-key.txt", "x")


# ── create + sidecar ──────────────────────────────────────────────────────


class TestCreate:
    def test_create_writes_skill_md(self, store, agent_root):
        result = store.create("my-skill", _VALID_SKILL_MD)
        assert result.success
        assert result.action == "create"
        skill_file = agent_root / "my-skill" / "SKILL.md"
        assert skill_file.is_file()
        assert "Body of the skill" in skill_file.read_text()

    def test_create_writes_sidecar(self, store, agent_root):
        store.create("my-skill", _VALID_SKILL_MD)
        meta = usage.read_meta(agent_root / "my-skill")
        assert meta is not None
        # Foreground call -> agent_created False, status active.
        assert meta["agent_created"] is False
        assert meta["status"] == usage.STATUS_ACTIVE

    def test_create_in_background_context_marks_agent_created(self, store, agent_root):
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            store.create("agent-skill", _VALID_SKILL_MD)
        finally:
            reset_current_write_origin(token)
        meta = usage.read_meta(agent_root / "agent-skill")
        assert meta["agent_created"] is True
        # Quarantine — not yet visible to LLM via build_summary.
        assert meta["status"] == usage.STATUS_QUARANTINE

    def test_create_existing_name_raises(self, store):
        store.create("my-skill", _VALID_SKILL_MD)
        with pytest.raises(SkillExistsError):
            store.create("my-skill", _VALID_SKILL_MD)


# ── edit / patch / write_file / remove_file ───────────────────────────────


class TestEditPatchFiles:
    def test_edit_replaces_skill_md(self, store, agent_root):
        store.create("my-skill", _VALID_SKILL_MD)
        new_content = _VALID_SKILL_MD.replace("Does something useful", "Does X better")
        store.edit("my-skill", new_content)
        assert "Does X better" in (agent_root / "my-skill" / "SKILL.md").read_text()

    def test_edit_bumps_patch_count(self, store, agent_root):
        store.create("my-skill", _VALID_SKILL_MD)
        store.edit("my-skill", _VALID_SKILL_MD + "\nMore.\n")
        meta = usage.read_meta(agent_root / "my-skill")
        assert meta["patch_count"] == 1

    def test_patch_skill_md_default(self, store, agent_root):
        store.create("my-skill", _VALID_SKILL_MD)
        store.patch("my-skill", "Body of the skill", "Patched body")
        text = (agent_root / "my-skill" / "SKILL.md").read_text()
        assert "Patched body" in text
        assert "Body of the skill" not in text

    def test_patch_refuses_ambiguous(self, store):
        store.create("my-skill", _VALID_SKILL_MD)
        store.write_file("my-skill", "references/notes.md", "repeat repeat repeat")
        with pytest.raises(SkillValidationError, match="appears 3 times"):
            store.patch(
                "my-skill", "repeat", "REPEAT",
                file_path="references/notes.md",
            )

    def test_patch_with_replace_all(self, store, agent_root):
        store.create("my-skill", _VALID_SKILL_MD)
        store.write_file("my-skill", "references/notes.md", "x x x")
        store.patch(
            "my-skill", "x", "Y",
            file_path="references/notes.md", replace_all=True,
        )
        assert (agent_root / "my-skill" / "references" / "notes.md").read_text() == "Y Y Y"

    def test_patch_old_string_not_found(self, store):
        store.create("my-skill", _VALID_SKILL_MD)
        with pytest.raises(SkillValidationError, match="not found"):
            store.patch("my-skill", "nonexistent text", "x")

    def test_write_file_creates_subdir(self, store, agent_root):
        store.create("my-skill", _VALID_SKILL_MD)
        store.write_file("my-skill", "references/api.md", "API docs")
        assert (agent_root / "my-skill" / "references" / "api.md").is_file()

    def test_write_file_requires_existing_skill(self, store):
        with pytest.raises(SkillNotFoundError):
            store.write_file("ghost", "references/x.md", "x")

    def test_remove_file_deletes_and_cleans_empty_subdir(self, store, agent_root):
        store.create("my-skill", _VALID_SKILL_MD)
        store.write_file("my-skill", "references/api.md", "x")
        store.remove_file("my-skill", "references/api.md")
        assert not (agent_root / "my-skill" / "references" / "api.md").exists()
        assert not (agent_root / "my-skill" / "references").exists()


# ── delete + protection ───────────────────────────────────────────────────


class TestDeleteAndProtection:
    def test_delete_removes_dir(self, store, agent_root):
        store.create("my-skill", _VALID_SKILL_MD)
        store.delete("my-skill")
        assert not (agent_root / "my-skill").exists()

    def test_delete_refuses_pinned(self, store, agent_root):
        store.create("my-skill", _VALID_SKILL_MD)
        usage.set_pinned(agent_root / "my-skill", True)
        with pytest.raises(SkillProtectedError, match="pinned"):
            store.delete("my-skill")

    def test_protected_root_blocks_writes(self, tmp_path):
        protected = tmp_path / "process_skills"
        protected.mkdir()
        (protected / "human-skill").mkdir()
        (protected / "human-skill" / "SKILL.md").write_text(_VALID_SKILL_MD)

        agent = tmp_path / "agent"
        agent.mkdir()
        store = SkillsStore(roots=[agent, protected], protected_roots=[protected])

        # Find works (read OK), but edit/patch/delete refuse.
        assert store.find_skill_dir("human-skill") is not None
        with pytest.raises(SkillProtectedError):
            store.edit("human-skill", _VALID_SKILL_MD)
        with pytest.raises(SkillProtectedError):
            store.delete("human-skill")
        with pytest.raises(SkillProtectedError):
            store.patch("human-skill", "Body", "X")


# ── atomic write (no torn files on failure) ───────────────────────────────


class TestAtomicWrite:
    def test_no_temp_files_left_after_create(self, store, agent_root):
        store.create("my-skill", _VALID_SKILL_MD)
        # Walk the tree: no .tmp- prefixed files should remain.
        leftovers = list((agent_root / "my-skill").rglob(".tmp-*"))
        assert leftovers == []
