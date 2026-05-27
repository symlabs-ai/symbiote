"""Tests for SkillsLoader lifecycle integration (Sprint 3 additions).

Pins the backward-compat invariant: skills WITHOUT a .skill_meta.json sidecar
must keep appearing in <available-skills>, so every human-curated skill in
the repo today continues to work without migration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.skills import usage
from symbiote.skills.loader import SkillsLoader

_FRONTMATTER = """\
---
name: {name}
description: {desc}
---
# {name}

Body.
"""


def _make_skill(root: Path, name: str, *, desc: str = "Test", meta: dict | None = None) -> Path:
    """Create a workspace-style skill at root/skills/{name}/SKILL.md."""
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_FRONTMATTER.format(name=name, desc=desc))
    if meta is not None:
        usage.write_meta(skill_dir, meta)
    return skill_dir


# ── backward-compat ───────────────────────────────────────────────────────


class TestBackwardCompat:
    def test_skill_without_sidecar_is_listable(self, tmp_path):
        """The core backward-compat guard: existing skills appear as-is."""
        _make_skill(tmp_path, "human-skill", desc="Created by a human")
        loader = SkillsLoader(tmp_path)
        listable = loader.listable_skills()
        assert any(s.name == "human-skill" for s in listable)
        summary = loader.build_summary()
        assert "human-skill" in summary

    def test_skill_without_sidecar_is_not_marked_agent_created(self, tmp_path):
        _make_skill(tmp_path, "human-skill")
        loader = SkillsLoader(tmp_path)
        skill = loader.get_skill("human-skill")
        assert skill.agent_created is False
        assert skill.status == usage.STATUS_ACTIVE


# ── quarantine filtering ──────────────────────────────────────────────────


class TestQuarantineFiltering:
    def test_quarantine_excluded_from_build_summary(self, tmp_path):
        _make_skill(
            tmp_path, "fresh-from-agent",
            meta=usage.default_meta(agent_created=True),  # quarantine
        )
        loader = SkillsLoader(tmp_path)
        summary = loader.build_summary()
        assert "fresh-from-agent" not in summary
        # But discoverable by name (loader.get_skill still works).
        assert loader.get_skill("fresh-from-agent") is not None

    def test_quarantine_not_in_listable_but_in_list_skills(self, tmp_path):
        _make_skill(
            tmp_path, "quarantine-skill",
            meta=usage.default_meta(agent_created=True),
        )
        loader = SkillsLoader(tmp_path)
        all_names = {s.name for s in loader.list_skills()}
        listable_names = {s.name for s in loader.listable_skills()}
        assert "quarantine-skill" in all_names
        assert "quarantine-skill" not in listable_names

    def test_archived_filtered_at_discovery(self, tmp_path):
        meta = usage.default_meta(agent_created=True)
        meta["status"] = usage.STATUS_ARCHIVED
        _make_skill(tmp_path, "old-skill", meta=meta)
        loader = SkillsLoader(tmp_path)
        # Archived isn't even in list_skills — completely invisible.
        assert all(s.name != "old-skill" for s in loader.list_skills())


# ── promotion / refresh ───────────────────────────────────────────────────


class TestPromotion:
    def test_promote_makes_quarantine_appear(self, tmp_path):
        skill_dir = _make_skill(
            tmp_path, "new-skill",
            meta=usage.default_meta(agent_created=True),
        )
        loader = SkillsLoader(tmp_path)
        assert "new-skill" not in loader.build_summary()

        usage.set_status(skill_dir, usage.STATUS_ACTIVE)
        loader.refresh()
        assert "new-skill" in loader.build_summary()


# ── use_count telemetry ───────────────────────────────────────────────────


class TestUseCountTelemetry:
    def test_get_skill_bumps_use_count(self, tmp_path):
        skill_dir = _make_skill(
            tmp_path, "tracked",
            meta=usage.default_meta(agent_created=True, status=usage.STATUS_ACTIVE),
        )
        loader = SkillsLoader(tmp_path)
        loader.get_skill("tracked")
        loader.get_skill("tracked")
        meta = usage.read_meta(skill_dir)
        assert meta["use_count"] == 2
        assert meta["last_used_at"] is not None
