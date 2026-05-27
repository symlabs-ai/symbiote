"""Sprint 5 tests — lifecycle automation + skill_review_audit.

Pins three features:
- Auto-promote quarantine → active after N successful loads.
- Auto-archive quarantine that aged past timeout without any use.
- skill_review_audit row written for every BackgroundReviewEngine run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.background_review import BackgroundReviewEngine
from symbiote.core.identity import IdentityManager
from symbiote.dream.engine import DreamEngine
from symbiote.dream.phases import PrunePhase
from symbiote.memory.store import MemoryStore
from symbiote.skills import usage
from symbiote.skills.loader import SkillsLoader
from symbiote.skills.store import SkillsStore

_FRONTMATTER = """\
---
name: {name}
description: {desc}
---
# {name}

Body.
"""


def _make_skill(root: Path, name: str, *, meta: dict | None = None) -> Path:
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_FRONTMATTER.format(name=name, desc="t"))
    if meta is not None:
        usage.write_meta(d, meta)
    return d


def _days_ago(n: float) -> str:
    return (datetime.now(tz=UTC) - timedelta(days=n)).isoformat()


# ── S5.1 — auto-promote ─────────────────────────────────────────────────


class TestAutoPromote:
    def test_quarantine_promoted_after_threshold_loads(self, tmp_path):
        meta = usage.default_meta(agent_created=True)
        # quarantine by default
        skill_dir = _make_skill(tmp_path, "fresh-skill", meta=meta)

        loader = SkillsLoader(tmp_path, auto_promote_threshold=3)
        # Loads 1 and 2: still quarantine (don't call get_skill again to
        # check, that would trigger load #3).
        loader.get_skill("fresh-skill")
        loader.get_skill("fresh-skill")
        assert usage.read_meta(skill_dir)["status"] == usage.STATUS_QUARANTINE
        assert usage.read_meta(skill_dir)["use_count"] == 2

        # Load 3 trips the threshold → promotion.
        skill = loader.get_skill("fresh-skill")
        assert skill.status == usage.STATUS_ACTIVE
        assert usage.read_meta(skill_dir)["status"] == usage.STATUS_ACTIVE
        assert usage.read_meta(skill_dir)["use_count"] == 3

    def test_threshold_zero_keeps_manual(self, tmp_path):
        meta = usage.default_meta(agent_created=True)
        _make_skill(tmp_path, "stays-quarantine", meta=meta)

        loader = SkillsLoader(tmp_path, auto_promote_threshold=0)
        for _ in range(10):
            loader.get_skill("stays-quarantine")
        assert loader.get_skill("stays-quarantine").status == usage.STATUS_QUARANTINE

    def test_human_skills_never_auto_promoted(self, tmp_path):
        """Skills without sidecar (humans) start as 'active' and aren't agent_created.
        mark_used MUST NOT toggle status on them."""
        _make_skill(tmp_path, "human-skill")  # no meta
        loader = SkillsLoader(tmp_path, auto_promote_threshold=1)
        for _ in range(5):
            loader.get_skill("human-skill")
        meta = usage.read_meta(tmp_path / "skills" / "human-skill")
        # Sidecar gets created by mark_used, but agent_created=false stays.
        assert meta["agent_created"] is False
        assert meta["status"] == usage.STATUS_ACTIVE

    def test_active_skill_not_re_promoted(self, tmp_path):
        """No-op when status is already active — defensive."""
        meta = usage.default_meta(agent_created=True, status=usage.STATUS_ACTIVE)
        _make_skill(tmp_path, "active-skill", meta=meta)
        loader = SkillsLoader(tmp_path, auto_promote_threshold=1)
        loader.get_skill("active-skill")
        meta_after = usage.read_meta(tmp_path / "skills" / "active-skill")
        assert meta_after["status"] == usage.STATUS_ACTIVE
        assert meta_after["use_count"] == 1

    def test_concurrent_mark_used_increments_correctly(self, tmp_path):
        """H5.1 hardening: per-skill_dir lock guards read-modify-write.

        Without the lock, 10 threads doing 10 mark_used each would race
        and undercount (each thread reads N, both write N+1 → loses
        increments). With the lock, the total is exactly 100.
        """
        import threading

        meta = usage.default_meta(agent_created=True, status=usage.STATUS_ACTIVE)
        skill_dir = _make_skill(tmp_path, "race-skill", meta=meta)

        errors: list[Exception] = []

        def worker():
            try:
                for _ in range(10):
                    usage.mark_used(skill_dir)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == []
        # 10 threads × 10 increments — must be exactly 100, not less.
        assert usage.read_meta(skill_dir)["use_count"] == 100


# ── S5.2 — auto-archive quarantine ──────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path):
    adp = SQLiteAdapter(db_path=tmp_path / "s5.db", check_same_thread=False)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter):
    return IdentityManager(storage=adapter).create(name="s5bot", role="assistant").id


class TestQuarantineAutoArchive:
    def test_quarantine_archived_after_timeout(self, tmp_path, adapter, symbiote_id):
        meta = usage.default_meta(agent_created=True)
        meta["created_at"] = _days_ago(20)  # past default 14d
        _make_skill(tmp_path, "ancient-quarantine", meta=meta)

        loader = SkillsLoader(tmp_path)
        engine = DreamEngine(
            storage=adapter, memory=MemoryStore(adapter),
            min_sessions=1, skills_loader=loader,
            skill_quarantine_timeout_days=14,
        )
        engine.dream(symbiote_id, "light")

        assert usage.read_meta(tmp_path / "skills" / "ancient-quarantine")["status"] == usage.STATUS_ARCHIVED

    def test_recent_quarantine_untouched(self, tmp_path, adapter, symbiote_id):
        meta = usage.default_meta(agent_created=True)
        meta["created_at"] = _days_ago(3)  # well under threshold
        _make_skill(tmp_path, "fresh-quarantine", meta=meta)

        loader = SkillsLoader(tmp_path)
        engine = DreamEngine(
            storage=adapter, memory=MemoryStore(adapter),
            min_sessions=1, skills_loader=loader,
            skill_quarantine_timeout_days=14,
        )
        engine.dream(symbiote_id, "light")

        assert usage.read_meta(tmp_path / "skills" / "fresh-quarantine")["status"] == usage.STATUS_QUARANTINE

    def test_pinned_quarantine_protected(self, tmp_path, adapter, symbiote_id):
        meta = usage.default_meta(agent_created=True)
        meta["created_at"] = _days_ago(30)
        meta["pinned"] = True
        _make_skill(tmp_path, "pinned-quarantine", meta=meta)

        loader = SkillsLoader(tmp_path)
        engine = DreamEngine(
            storage=adapter, memory=MemoryStore(adapter),
            min_sessions=1, skills_loader=loader,
            skill_quarantine_timeout_days=14,
        )
        engine.dream(symbiote_id, "light")

        assert usage.read_meta(tmp_path / "skills" / "pinned-quarantine")["status"] == usage.STATUS_QUARANTINE

    def test_timeout_zero_disables_auto_archive(self, tmp_path, adapter, symbiote_id):
        meta = usage.default_meta(agent_created=True)
        meta["created_at"] = _days_ago(365)
        _make_skill(tmp_path, "would-archive", meta=meta)

        loader = SkillsLoader(tmp_path)
        # quarantine_timeout_days=0 → disabled
        phase = PrunePhase(quarantine_timeout_days=0)
        from symbiote.dream.models import BudgetTracker, DreamContext
        ctx = DreamContext(
            symbiote_id=symbiote_id, storage=adapter,
            memory=MemoryStore(adapter), llm=None,
            budget=BudgetTracker(1), dry_run=False,
            skills_loader=loader,
        )
        phase.run(ctx)
        assert usage.read_meta(tmp_path / "skills" / "would-archive")["status"] == usage.STATUS_QUARANTINE


# ── S5.3 — skill_review_audit ────────────────────────────────────────────


class _NoopLLM:
    def complete(self, messages, config=None, tools=None):
        return "[]"


class TestSkillReviewAudit:
    def test_audit_row_written_per_run(self, tmp_path, adapter, symbiote_id):
        sid = str(uuid4())
        adapter.execute(
            "INSERT INTO sessions (id, symbiote_id, status, started_at) "
            "VALUES (?, ?, 'active', datetime('now'))",
            (sid, symbiote_id),
        )
        adapter.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (str(uuid4()), sid, "user", "anything"),
        )

        store = SkillsStore(roots=[tmp_path / "skills"])
        loader = SkillsLoader(tmp_path)
        engine = BackgroundReviewEngine(
            llm=_NoopLLM(),
            messages=MessageRepository(adapter),
            store=store, loader=loader, storage=adapter,
        )
        engine.run_sync(sid, symbiote_id)

        rows = adapter.fetch_all(
            "SELECT trigger, applied, skipped, ok, ops_json "
            "FROM skill_review_audit WHERE session_id = ?",
            (sid,),
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["trigger"] == "sync"
        assert row["applied"] == 0
        assert row["skipped"] == 0
        assert row["ok"] == 1
        assert json.loads(row["ops_json"]) == []

    def test_audit_records_applied_ops(self, tmp_path, adapter, symbiote_id):
        sid = str(uuid4())
        adapter.execute(
            "INSERT INTO sessions (id, symbiote_id, status, started_at) "
            "VALUES (?, ?, 'active', datetime('now'))",
            (sid, symbiote_id),
        )
        adapter.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (str(uuid4()), sid, "user", "anything"),
        )

        skill_md = _FRONTMATTER.format(name="new-skill", desc="d")

        class _CreateLLM:
            def complete(self, messages, config=None, tools=None):
                return json.dumps([{
                    "action": "create", "name": "new-skill",
                    "content": skill_md, "reasoning": "x",
                }])

        store = SkillsStore(roots=[tmp_path / "skills"])
        loader = SkillsLoader(tmp_path)
        engine = BackgroundReviewEngine(
            llm=_CreateLLM(),
            messages=MessageRepository(adapter),
            store=store, loader=loader, storage=adapter,
        )
        engine.run_sync(sid, symbiote_id)

        row = adapter.fetch_one(
            "SELECT applied, ops_json FROM skill_review_audit WHERE session_id = ?",
            (sid,),
        )
        assert row["applied"] == 1
        ops = json.loads(row["ops_json"])
        assert len(ops) == 1
        assert ops[0]["action"] == "create"
        assert ops[0]["name"] == "new-skill"
        assert ops[0]["ok"] is True

    def test_audit_records_llm_failure(self, tmp_path, adapter, symbiote_id):
        sid = str(uuid4())
        adapter.execute(
            "INSERT INTO sessions (id, symbiote_id, status, started_at) "
            "VALUES (?, ?, 'active', datetime('now'))",
            (sid, symbiote_id),
        )
        adapter.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (str(uuid4()), sid, "user", "anything"),
        )

        class _FailingLLM:
            def complete(self, messages, config=None, tools=None):
                raise RuntimeError("model dead")

        store = SkillsStore(roots=[tmp_path / "skills"])
        loader = SkillsLoader(tmp_path)
        engine = BackgroundReviewEngine(
            llm=_FailingLLM(),
            messages=MessageRepository(adapter),
            store=store, loader=loader, storage=adapter,
        )
        engine.run_sync(sid, symbiote_id)

        row = adapter.fetch_one(
            "SELECT ok, error FROM skill_review_audit WHERE session_id = ?",
            (sid,),
        )
        assert row["ok"] == 0
        assert row["error"] is not None
        assert "model dead" in row["error"]

    def test_no_audit_without_storage(self, tmp_path, adapter, symbiote_id):
        """Engine without storage skips audit silently (used in unit tests)."""
        sid = str(uuid4())
        adapter.execute(
            "INSERT INTO sessions (id, symbiote_id, status, started_at) "
            "VALUES (?, ?, 'active', datetime('now'))",
            (sid, symbiote_id),
        )
        adapter.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (str(uuid4()), sid, "user", "anything"),
        )
        store = SkillsStore(roots=[tmp_path / "skills"])
        loader = SkillsLoader(tmp_path)
        engine = BackgroundReviewEngine(
            llm=_NoopLLM(),
            messages=MessageRepository(adapter),
            store=store, loader=loader,
            # storage=None — audit disabled
        )
        engine.run_sync(sid, symbiote_id)

        rows = adapter.fetch_all(
            "SELECT * FROM skill_review_audit WHERE session_id = ?", (sid,),
        )
        assert rows == []
