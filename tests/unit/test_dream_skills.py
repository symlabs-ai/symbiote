"""Tests for DreamEngine.PrunePhase skill-lifecycle extension (Sprint 4).

Pins the safety invariant: human-created skills (no sidecar) and pinned
skills are NEVER touched by Dream, no matter how old they get. Only skills
the agent created in a background review go through active→stale→archived.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.dream.engine import DreamEngine
from symbiote.dream.phases import PrunePhase
from symbiote.memory.store import MemoryStore
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


def _make_skill(root: Path, name: str, *, meta: dict | None = None, parent_dirname: str = "skills") -> Path:
    """Create a skill at root/{parent_dirname}/{name}/SKILL.md.

    Default parent_dirname is 'skills' to match the SkillsLoader convention.
    """
    skill_dir = root / parent_dirname / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_FRONTMATTER.format(name=name, desc="t"))
    if meta is not None:
        usage.write_meta(skill_dir, meta)
    return skill_dir


def _days_ago(days: float) -> str:
    return (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "ds.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    return IdentityManager(storage=adapter).create(name="DSBot", role="assistant").id


@pytest.fixture()
def memory(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(storage=adapter)


@pytest.fixture()
def loader(tmp_path: Path) -> SkillsLoader:
    return SkillsLoader(tmp_path)


# ── safety: humans / pinned untouched ────────────────────────────────────


class TestSafety:
    def test_human_skill_without_sidecar_untouched_even_after_years(
        self, tmp_path, loader, adapter, symbiote_id, memory
    ):
        _make_skill(tmp_path, "human-old", parent_dirname="skills")
        loader.refresh()

        engine = DreamEngine(
            storage=adapter, memory=memory,
            min_sessions=1, skills_loader=loader,
        )
        engine.dream(symbiote_id, "light")

        # Human skill must still be at its original status (active by default).
        assert loader.get_skill("human-old").status == usage.STATUS_ACTIVE
        # No sidecar appeared at all (was never written).
        # NOTE: get_skill() bumps use_count so a sidecar IS created — but
        # it must still have agent_created=false.
        meta = usage.read_meta(tmp_path / "skills" / "human-old")
        if meta is not None:
            assert meta["agent_created"] is False

    def test_pinned_agent_skill_untouched(
        self, tmp_path, loader, adapter, symbiote_id, memory
    ):
        meta = usage.default_meta(agent_created=True, status=usage.STATUS_ACTIVE)
        meta["last_used_at"] = _days_ago(365)
        meta["pinned"] = True
        _make_skill(tmp_path, "pinned-skill", meta=meta)
        loader.refresh()

        engine = DreamEngine(
            storage=adapter, memory=memory,
            min_sessions=1, skills_loader=loader,
        )
        engine.dream(symbiote_id, "light")

        # Pinned -> never demoted.
        assert loader.get_skill("pinned-skill").status == usage.STATUS_ACTIVE


# ── lifecycle transitions ────────────────────────────────────────────────


class TestLifecycleTransitions:
    def test_active_to_stale_after_30_days(
        self, tmp_path, loader, adapter, symbiote_id, memory
    ):
        meta = usage.default_meta(agent_created=True, status=usage.STATUS_ACTIVE)
        meta["last_used_at"] = _days_ago(31)
        _make_skill(tmp_path, "old-skill", meta=meta)
        loader.refresh()

        engine = DreamEngine(
            storage=adapter, memory=memory,
            min_sessions=1, skills_loader=loader,
        )
        engine.dream(symbiote_id, "light")

        assert loader.get_skill("old-skill").status == usage.STATUS_STALE

    def test_stale_to_archived_after_90_days(
        self, tmp_path, loader, adapter, symbiote_id, memory
    ):
        meta = usage.default_meta(agent_created=True, status=usage.STATUS_STALE)
        meta["last_used_at"] = _days_ago(100)
        _make_skill(tmp_path, "very-old", meta=meta)
        loader.refresh()

        engine = DreamEngine(
            storage=adapter, memory=memory,
            min_sessions=1, skills_loader=loader,
        )
        engine.dream(symbiote_id, "light")

        # Archived skills are filtered out at discovery — invisible.
        assert loader.get_skill("very-old") is None
        # But sidecar still exists with status=archived on disk.
        meta_after = usage.read_meta(tmp_path / "skills" / "very-old")
        assert meta_after["status"] == usage.STATUS_ARCHIVED

    def test_recent_skill_stays_active(
        self, tmp_path, loader, adapter, symbiote_id, memory
    ):
        meta = usage.default_meta(agent_created=True, status=usage.STATUS_ACTIVE)
        meta["last_used_at"] = _days_ago(5)
        _make_skill(tmp_path, "fresh", meta=meta)
        loader.refresh()

        engine = DreamEngine(
            storage=adapter, memory=memory,
            min_sessions=1, skills_loader=loader,
        )
        engine.dream(symbiote_id, "light")

        assert loader.get_skill("fresh").status == usage.STATUS_ACTIVE


# ── dry_run ───────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_proposes_but_does_not_apply(
        self, tmp_path, loader, adapter, symbiote_id, memory
    ):
        meta = usage.default_meta(agent_created=True, status=usage.STATUS_ACTIVE)
        meta["last_used_at"] = _days_ago(60)
        _make_skill(tmp_path, "dry-target", meta=meta)
        loader.refresh()

        engine = DreamEngine(
            storage=adapter, memory=memory,
            min_sessions=1, skills_loader=loader,
            dry_run=True,
        )
        report = engine.dream(symbiote_id, "light")

        # Status unchanged on disk.
        assert loader.get_skill("dry-target").status == usage.STATUS_ACTIVE
        # Phase report records the proposal anyway.
        prune_phase = next(p for p in report.phases if p.phase == "prune")
        skill_details = [d for d in prune_phase.details if d.get("kind") == "skill"]
        assert len(skill_details) == 1
        assert skill_details[0]["name"] == "dry-target"
        assert skill_details[0]["to_status"] == usage.STATUS_STALE
