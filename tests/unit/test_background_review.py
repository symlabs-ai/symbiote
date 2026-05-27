"""Tests for BackgroundReviewEngine — Sprint 4 fork engine.

Covers:
- Provenance: skills created under spawn() are tagged agent_created=true,
  status=quarantine; skills created outside spawn() are not.
- max_active_skills cap enforced (create refused, patch/write_file allowed).
- LLM failure / invalid JSON: result.ok=False but no exception bubbles.
- Refresh: loader.refresh() invoked after writes so subsequent queries see
  the new skill.
- run_sync covers the same paths without threading flakiness.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.background_review import BackgroundReviewEngine
from symbiote.core.identity import IdentityManager
from symbiote.skills import usage
from symbiote.skills.loader import SkillsLoader
from symbiote.skills.store import SkillsStore

_VALID_SKILL_MD = """\
---
name: {name}
description: {desc}
---
# {name}

{body}
"""


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "br.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    return IdentityManager(storage=adapter).create(name="BRBot", role="assistant").id


@pytest.fixture()
def session_id(adapter: SQLiteAdapter, symbiote_id: str) -> str:
    sid = str(uuid4())
    adapter.execute(
        "INSERT INTO sessions (id, symbiote_id, status, started_at) "
        "VALUES (?, ?, 'active', datetime('now'))",
        (sid, symbiote_id),
    )
    return sid


def _insert_messages(adapter, session_id, lines):
    for role, content in lines:
        adapter.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (str(uuid4()), session_id, role, content),
        )


@pytest.fixture()
def store(tmp_path: Path):
    # Layout matches kernel default: tmp_path/skills/{skill_name}/SKILL.md
    agent_root = tmp_path / "skills"
    return SkillsStore(roots=[agent_root])


@pytest.fixture()
def loader(tmp_path: Path):
    # Loader expects {root}/skills/{name}/SKILL.md, so passing tmp_path
    # scans tmp_path/skills/ — where the store writes.
    return SkillsLoader(tmp_path)


class _StaticLLM:
    """Returns a canned response each call."""

    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def complete(self, messages, config=None, tools=None):
        self.calls += 1
        return self.response


# ── create + provenance ───────────────────────────────────────────────────


class TestCreateProvenance:
    def test_create_skill_under_background_review_marks_agent_created(
        self, adapter, symbiote_id, session_id, store, loader, tmp_path
    ):
        _insert_messages(adapter, session_id, [
            ("user", "preciso de um workflow pra NFe"),
            ("assistant", "OK"),
        ])
        skill_content = _VALID_SKILL_MD.format(
            name="nfe-workflow",
            desc="Extract NFe data",
            body="Step 1...",
        )
        llm = _StaticLLM(json.dumps([{
            "action": "create",
            "name": "nfe-workflow",
            "content": skill_content,
            "reasoning": "non-trivial fix",
        }]))
        engine = BackgroundReviewEngine(
            llm=llm,
            messages=MessageRepository(adapter),
            store=store,
            loader=loader,
        )

        result = engine.run_sync(session_id, symbiote_id)
        assert result["ok"] is True
        assert result["applied"] == 1

        # Sidecar must tag agent_created=true, status=quarantine.
        meta = usage.read_meta(tmp_path / "skills" / "nfe-workflow")
        assert meta is not None
        assert meta["agent_created"] is True
        assert meta["status"] == usage.STATUS_QUARANTINE

    def test_loader_refresh_called_so_new_skill_visible(
        self, adapter, symbiote_id, session_id, store, loader
    ):
        _insert_messages(adapter, session_id, [("user", "x")])
        llm = _StaticLLM(json.dumps([{
            "action": "create", "name": "fresh-skill",
            "content": _VALID_SKILL_MD.format(name="fresh-skill", desc="d", body="b"),
            "reasoning": "x",
        }]))
        engine = BackgroundReviewEngine(
            llm=llm, messages=MessageRepository(adapter),
            store=store, loader=loader,
        )
        engine.run_sync(session_id, symbiote_id)
        # After run_sync, refresh() must have been called — loader sees skill.
        assert loader.get_skill("fresh-skill") is not None


# ── max_active_skills cap ─────────────────────────────────────────────────


class TestQuarantineCap:
    def test_create_refused_when_quarantine_cap_reached(
        self, adapter, symbiote_id, session_id, store, loader, tmp_path
    ):
        """Sprint 4.1 hardening: create checks the quarantine bucket, not active.

        Library with 1 active + max quarantine should ACCEPT no more creates
        until promotion or archive. Active count is irrelevant here.
        """
        from symbiote.core.provenance import (
            BACKGROUND_REVIEW,
            reset_current_write_origin,
            set_current_write_origin,
        )

        # 1 active skill (foreground create -> not quarantine).
        store.create(
            "active-skill",
            _VALID_SKILL_MD.format(name="active-skill", desc="d", body="b"),
        )
        # Fill quarantine bucket by simulating background creates.
        tok = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            store.create(
                "quarantine-skill",
                _VALID_SKILL_MD.format(name="quarantine-skill", desc="d", body="b"),
            )
        finally:
            reset_current_write_origin(tok)
        loader.refresh()

        _insert_messages(adapter, session_id, [("user", "x")])
        llm = _StaticLLM(json.dumps([{
            "action": "create", "name": "would-be-new",
            "content": _VALID_SKILL_MD.format(name="would-be-new", desc="d", body="b"),
            "reasoning": "x",
        }]))
        # max_quarantine=1 — already filled by the seeded quarantine-skill.
        engine = BackgroundReviewEngine(
            llm=llm, messages=MessageRepository(adapter),
            store=store, loader=loader,
            max_active_skills=100,  # high — proves active count is irrelevant
            max_quarantine_skills=1,
        )
        result = engine.run_sync(session_id, symbiote_id)
        assert result["applied"] == 0
        assert result["skipped"] == 1
        assert "max_quarantine_skills cap" in result["ops"][0]["error"]
        assert not (tmp_path / "skills" / "would-be-new").exists()

    def test_full_active_does_not_block_create(
        self, adapter, symbiote_id, session_id, store, loader, tmp_path
    ):
        """Sprint 4.1 hardening: active library full but quarantine empty -> CREATE allowed.

        This is the deadlock the old single-cap behavior caused: it counted
        quarantine toward max_active_skills, so a library of 20 promoted
        active skills would refuse any new background-review create until
        someone archived an active one. Wrong layer to enforce.
        """
        for i in range(3):
            store.create(
                f"active-{i}",
                _VALID_SKILL_MD.format(name=f"active-{i}", desc="d", body="b"),
            )
        loader.refresh()

        _insert_messages(adapter, session_id, [("user", "x")])
        llm = _StaticLLM(json.dumps([{
            "action": "create", "name": "new-quarantine",
            "content": _VALID_SKILL_MD.format(name="new-quarantine", desc="d", body="b"),
            "reasoning": "x",
        }]))
        engine = BackgroundReviewEngine(
            llm=llm, messages=MessageRepository(adapter),
            store=store, loader=loader,
            max_active_skills=3,   # FULL
            max_quarantine_skills=5,  # empty — CREATE goes here
        )
        result = engine.run_sync(session_id, symbiote_id)
        assert result["applied"] == 1
        assert result["skipped"] == 0
        assert (tmp_path / "skills" / "new-quarantine").exists()


# ── failure modes ─────────────────────────────────────────────────────────


class TestFailureModes:
    def test_llm_raises_no_exception_bubbles(
        self, adapter, symbiote_id, session_id, store, loader
    ):
        class _FailingLLM:
            def complete(self, messages, config=None, tools=None):
                raise RuntimeError("boom")
        _insert_messages(adapter, session_id, [("user", "x")])
        engine = BackgroundReviewEngine(
            llm=_FailingLLM(), messages=MessageRepository(adapter),
            store=store, loader=loader,
        )
        result = engine.run_sync(session_id, symbiote_id)
        assert result["ok"] is False
        assert result["error"] is not None
        assert result["applied"] == 0

    def test_invalid_json_returns_error(
        self, adapter, symbiote_id, session_id, store, loader
    ):
        _insert_messages(adapter, session_id, [("user", "x")])
        engine = BackgroundReviewEngine(
            llm=_StaticLLM("not json at all"),
            messages=MessageRepository(adapter),
            store=store, loader=loader,
        )
        result = engine.run_sync(session_id, symbiote_id)
        assert result["ok"] is False
        assert "JSON" in result["error"]

    def test_empty_array_is_ok(
        self, adapter, symbiote_id, session_id, store, loader
    ):
        """'Nothing to save' is a valid outcome."""
        _insert_messages(adapter, session_id, [("user", "x")])
        engine = BackgroundReviewEngine(
            llm=_StaticLLM("[]"),
            messages=MessageRepository(adapter),
            store=store, loader=loader,
        )
        result = engine.run_sync(session_id, symbiote_id)
        assert result["ok"] is True
        assert result["applied"] == 0
        assert result["skipped"] == 0

    def test_delete_action_refused(
        self, adapter, symbiote_id, session_id, store, loader
    ):
        """Deletes are too destructive for autonomous review."""
        store.create(
            "to-keep",
            _VALID_SKILL_MD.format(name="to-keep", desc="d", body="b"),
        )
        _insert_messages(adapter, session_id, [("user", "delete that")])
        llm = _StaticLLM(json.dumps([{
            "action": "delete", "name": "to-keep",
        }]))
        engine = BackgroundReviewEngine(
            llm=llm, messages=MessageRepository(adapter),
            store=store, loader=loader,
        )
        result = engine.run_sync(session_id, symbiote_id)
        assert result["applied"] == 0
        assert result["skipped"] == 1
        # Skill still on disk
        assert store.find_skill_dir("to-keep") is not None


# ── patch + write_file paths ──────────────────────────────────────────────


class TestPatchAndWriteFile:
    def test_patch_existing_skill(
        self, adapter, symbiote_id, session_id, store, loader, tmp_path
    ):
        store.create(
            "to-patch",
            _VALID_SKILL_MD.format(name="to-patch", desc="d", body="original line"),
        )
        loader.refresh()

        _insert_messages(adapter, session_id, [("user", "refine that")])
        llm = _StaticLLM(json.dumps([{
            "action": "patch", "name": "to-patch",
            "old_string": "original line", "new_string": "refined line",
            "reasoning": "fix",
        }]))
        engine = BackgroundReviewEngine(
            llm=llm, messages=MessageRepository(adapter),
            store=store, loader=loader,
        )
        result = engine.run_sync(session_id, symbiote_id)
        assert result["applied"] == 1
        text = (tmp_path / "skills" / "to-patch" / "SKILL.md").read_text()
        assert "refined line" in text
        assert "original line" not in text

    def test_write_file_adds_reference(
        self, adapter, symbiote_id, session_id, store, loader, tmp_path
    ):
        store.create(
            "with-refs",
            _VALID_SKILL_MD.format(name="with-refs", desc="d", body="b"),
        )
        loader.refresh()
        _insert_messages(adapter, session_id, [("user", "add a note")])
        llm = _StaticLLM(json.dumps([{
            "action": "write_file", "name": "with-refs",
            "file_path": "references/note.md",
            "file_content": "Some note content",
        }]))
        engine = BackgroundReviewEngine(
            llm=llm, messages=MessageRepository(adapter),
            store=store, loader=loader,
        )
        result = engine.run_sync(session_id, symbiote_id)
        assert result["applied"] == 1
        ref = tmp_path / "skills" / "with-refs" / "references" / "note.md"
        assert ref.read_text() == "Some note content"
