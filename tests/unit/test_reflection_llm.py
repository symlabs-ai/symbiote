"""Tests for ReflectionEngine LLM mode + defense-in-depth guard + audit.

Matrix from the LLM-reflection plan:
  - Anti-patterns: env-failure, negative tool claim, transient error -> dropped
  - Positives: style correction, workflow correction, decision-with-reasoning
  - PATCH path: existing memory updated, not duplicated (Sprint 2 first-class)
  - LLM failure -> falls back to keyword
  - Invalid JSON -> falls back to keyword (no persist of garbage)
  - Guard: blocklisted phrase as constraint -> dropped
  - Hybrid: persists keyword, logs LLM diff to reflection_audit
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.core.reflection import ReflectionEngine
from symbiote.memory.store import MemoryStore

# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "reflection_llm_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    return mgr.create(name="RefLLMBot", role="assistant").id


@pytest.fixture()
def session_id(adapter: SQLiteAdapter, symbiote_id: str) -> str:
    sid = str(uuid4())
    adapter.execute(
        "INSERT INTO sessions (id, symbiote_id, status, started_at) "
        "VALUES (?, ?, 'active', datetime('now'))",
        (sid, symbiote_id),
    )
    return sid


@pytest.fixture()
def store(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(storage=adapter)


def _insert_messages(adapter: SQLiteAdapter, session_id: str, lines: list[tuple[str, str]]) -> None:
    for role, content in lines:
        adapter.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (str(uuid4()), session_id, role, content),
        )


class _MockLLM:
    """Returns a canned JSON response."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict], config: dict | None = None,
                 tools: list[dict] | None = None) -> str:
        self.calls.append(messages)
        return self.response


class _FailingLLM:
    def complete(self, messages, config=None, tools=None):
        raise RuntimeError("model unavailable")


# ── anti-patterns (LLM output that SHOULD be dropped by guard) ────────────


class TestAntiPatterns:
    def test_command_not_found_as_constraint_is_dropped(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [
            ("user", "Tentei typora, deu erro"),
            ("assistant", "typora: command not found"),
        ])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "constraint",
             "content": "typora command not found, never run", "importance": 0.9}
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.persisted_count == 0

    def test_negative_tool_claim_as_constraint_is_dropped(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [("user", "browser não funcionou")])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "constraint",
             "content": "browser tool is broken, don't use", "importance": 0.9}
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.persisted_count == 0

    def test_anti_pattern_as_factual_passes_with_downgrade(
        self, store, adapter, symbiote_id, session_id
    ):
        """Same content as factual (not constraint) passes — but importance capped."""
        _insert_messages(adapter, session_id, [("user", "deu erro")])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "factual",
             "content": "command not found on this run", "importance": 0.95}
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        # passes guard (factual, not constraint), but importance capped at 0.7
        assert result.persisted_count == 1
        assert result.extracted_facts[0]["importance"] == 0.7


# ── positive signals (engineered prompt SHOULD capture) ───────────────────


class TestPositiveSignals:
    def test_style_correction_creates_preference(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [
            ("user", "Não me chame de senhor, fala normal"),
            ("assistant", "Ok"),
        ])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "preference",
             "content": "User prefers informal tone, no 'senhor'", "importance": 0.7}
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.persisted_count == 1
        # persisted to memory_entries
        rows = adapter.fetch_all(
            "SELECT type, content FROM memory_entries WHERE symbiote_id = ?",
            (symbiote_id,),
        )
        types = [r["type"] for r in rows]
        assert "preference" in types

    def test_decision_with_reasoning_creates_decision(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [
            ("user", "vou usar pytest porque integra melhor com coverage"),
        ])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "decision",
             "content": "Chose pytest over unittest for coverage integration",
             "importance": 0.6}
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.persisted_count == 1
        assert result.extracted_facts[0]["type"] == "decision"


# ── failure / fallback paths ──────────────────────────────────────────────


class TestFallback:
    def test_llm_failure_falls_back_to_keyword(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [
            ("user", "Always run linter before commit"),  # 'always' triggers keyword
        ])
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=_FailingLLM(), mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        # keyword fallback fires on 'always'
        assert result.persisted_count == 1
        assert result.llm_error is not None
        assert result.mode_used == "llm"

    def test_invalid_json_falls_back_to_keyword(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [
            ("user", "Always run linter before commit"),
        ])
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=_MockLLM("this is not json"),
            mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        # keyword fallback again
        assert result.persisted_count == 1
        assert result.llm_error is not None

    def test_no_llm_forces_keyword_mode(self, store, adapter, symbiote_id, session_id):
        _insert_messages(adapter, session_id, [
            ("user", "Always run linter"),
        ])
        # mode=llm but no llm -> falls back to keyword at __init__
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=None, mode="llm",
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.mode_used == "keyword"


# ── hybrid mode + audit table ─────────────────────────────────────────────


class TestHybridAndAudit:
    def test_hybrid_persists_keyword_logs_llm_diff(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [
            ("user", "Always use ruff. Also: I prefer tabs over spaces."),
        ])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "preference",
             "content": "User prefers tabs", "importance": 0.7}
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="hybrid", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        # only keyword facts persisted in hybrid mode
        assert result.mode_used == "hybrid"
        # audit row written
        rows = adapter.fetch_all(
            "SELECT mode, keyword_facts_json, llm_facts_json FROM reflection_audit WHERE session_id = ?",
            (session_id,),
        )
        assert len(rows) == 1
        assert rows[0]["mode"] == "hybrid"
        assert "tabs" in rows[0]["llm_facts_json"]

    def test_llm_mode_writes_audit_too(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [("user", "I prefer Python")])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "preference",
             "content": "User prefers Python", "importance": 0.6}
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        engine.reflect_session(session_id, symbiote_id)
        rows = adapter.fetch_all(
            "SELECT mode FROM reflection_audit WHERE session_id = ?",
            (session_id,),
        )
        assert len(rows) == 1
        assert rows[0]["mode"] == "llm"


# ── tags normalization (Sprint 1.1 hardening) ─────────────────────────────


class TestTagsNormalization:
    def test_missing_tags_fallback_to_type(
        self, store, adapter, symbiote_id, session_id
    ):
        """LLM ignoring the tags requirement -> auto-fill [type], persist anyway."""
        _insert_messages(adapter, session_id, [("user", "I prefer dark mode")])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "preference",
             "content": "Dark mode preferred", "importance": 0.6}
            # no tags field
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.persisted_count == 1
        # Auto-filled with [type]
        assert result.extracted_facts[0]["tags"] == ["preference"]

    def test_empty_tags_list_fallback_to_type(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [("user", "I prefer Python")])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "preference",
             "content": "Python preferred", "importance": 0.6,
             "tags": []}
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.extracted_facts[0]["tags"] == ["preference"]

    def test_tags_deduplicated_and_lowercased(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [("user", "I prefer Python")])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "preference",
             "content": "Python preferred", "importance": 0.6,
             "tags": ["Python", "python", "  PYTHON  ", "stack"]}
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.extracted_facts[0]["tags"] == ["python", "stack"]

    def test_non_string_tags_filtered(
        self, store, adapter, symbiote_id, session_id
    ):
        _insert_messages(adapter, session_id, [("user", "I prefer Python")])
        llm = _MockLLM(json.dumps([
            {"action": "create", "type": "preference",
             "content": "Python preferred", "importance": 0.6,
             "tags": ["python", 42, None, {"x": 1}, "stack"]}
        ]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.extracted_facts[0]["tags"] == ["python", "stack"]


# ── compression prompt moved to _review_prompts (Sprint 1.1) ──────────────


class TestCompressionPromptRelocation:
    def test_compression_prompt_importable_from_review_prompts(self):
        from symbiote.core._review_prompts import COMPRESSION_PROMPT
        assert "{messages}" in COMPRESSION_PROMPT
        assert "Summary:" in COMPRESSION_PROMPT

    def test_consolidator_uses_central_prompt(self):
        """Catch the case where someone re-introduces a local copy in consolidator.py."""
        import symbiote.memory.consolidator as consolidator
        assert not hasattr(consolidator, "_COMPRESSION_PROMPT"), (
            "consolidator.py still defines _COMPRESSION_PROMPT locally — "
            "should import from core._review_prompts"
        )


# ── PATCH path (Sprint 2: first-class via MemoryPort.update) ──────────────


class TestPatchPath:
    def _seed_preference(self, store, adapter, symbiote_id, session_id, content):
        """Insert a preference memory and return its id."""
        from symbiote.core.models import MemoryEntry
        entry = MemoryEntry(
            symbiote_id=symbiote_id, session_id=session_id,
            type="preference", scope="global",
            content=content, tags=["tone"], importance=0.6,
            source="user",
        )
        return store.store(entry)

    def test_patch_updates_existing_no_duplicate(
        self, store, adapter, symbiote_id, session_id
    ):
        existing_id = self._seed_preference(
            store, adapter, symbiote_id, session_id,
            "User prefers informal tone",
        )
        # Insert a message that prompts the LLM to refine the existing entry
        _insert_messages(adapter, session_id, [
            ("user", "também: nunca me chame de Dr."),
        ])
        llm = _MockLLM(json.dumps([{
            "action": "patch",
            "target_id": existing_id,
            "type": "preference",
            "content": "User prefers informal tone; never use 'Dr.'",
            "importance": 0.7,
            "tags": ["tone", "address"],
        }]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.persisted_count == 1

        # Exactly ONE preference row for this symbiote — patched, not duplicated.
        rows = adapter.fetch_all(
            "SELECT id, content, importance FROM memory_entries "
            "WHERE symbiote_id = ? AND type = 'preference' AND is_active = 1",
            (symbiote_id,),
        )
        assert len(rows) == 1
        assert rows[0]["id"] == existing_id
        assert "Dr." in rows[0]["content"]
        assert rows[0]["importance"] == 0.7

    def test_patch_with_short_prefix_target_id(
        self, store, adapter, symbiote_id, session_id
    ):
        """LLM commonly returns the 8-char short id from the existing_memories block."""
        full_id = self._seed_preference(
            store, adapter, symbiote_id, session_id,
            "User prefers Python",
        )
        short = full_id[:8]
        _insert_messages(adapter, session_id, [("user", "ainda prefiro Python sim")])
        llm = _MockLLM(json.dumps([{
            "action": "patch", "target_id": short,
            "type": "preference",
            "content": "User prefers Python (confirmed)",
            "importance": 0.65,
            "tags": ["stack"],
        }]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        engine.reflect_session(session_id, symbiote_id)
        rows = adapter.fetch_all(
            "SELECT content FROM memory_entries WHERE id = ?", (full_id,),
        )
        assert "confirmed" in rows[0]["content"]

    def test_patch_with_invalid_target_falls_back_to_create(
        self, store, adapter, symbiote_id, session_id
    ):
        """target_id that doesn't resolve -> CREATE (don't lose the lesson)."""
        _insert_messages(adapter, session_id, [("user", "I prefer dark mode")])
        llm = _MockLLM(json.dumps([{
            "action": "patch", "target_id": "ghost-id-does-not-exist",
            "type": "preference",
            "content": "User prefers dark mode",
            "importance": 0.6,
            "tags": ["ui"],
        }]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.persisted_count == 1
        # New row created
        rows = adapter.fetch_all(
            "SELECT content FROM memory_entries WHERE symbiote_id = ? "
            "AND type = 'preference' AND is_active = 1",
            (symbiote_id,),
        )
        assert len(rows) == 1
        assert "dark mode" in rows[0]["content"]

    def test_patch_without_target_id_falls_back_to_create(
        self, store, adapter, symbiote_id, session_id
    ):
        """action=patch but target_id missing/empty -> CREATE."""
        _insert_messages(adapter, session_id, [("user", "I prefer vim")])
        llm = _MockLLM(json.dumps([{
            "action": "patch",  # but no target_id
            "type": "preference",
            "content": "User prefers vim",
            "importance": 0.6,
            "tags": ["editor"],
        }]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.persisted_count == 1

    def test_patch_against_inactive_memory_falls_back_to_create(
        self, store, adapter, symbiote_id, session_id
    ):
        """Soft-deleted target -> update returns False -> CREATE fallback."""
        old_id = self._seed_preference(
            store, adapter, symbiote_id, session_id, "Old preference"
        )
        store.deactivate(old_id)

        _insert_messages(adapter, session_id, [("user", "novo preferência")])
        llm = _MockLLM(json.dumps([{
            "action": "patch", "target_id": old_id,
            "type": "preference",
            "content": "User prefers something new",
            "importance": 0.6,
            "tags": ["taste"],
        }]))
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=llm, mode="llm", storage=adapter,
        )
        result = engine.reflect_session(session_id, symbiote_id)
        assert result.persisted_count == 1
        # Active count == 1 (the new one); old one stays inactive
        active = adapter.fetch_all(
            "SELECT id FROM memory_entries WHERE symbiote_id = ? "
            "AND type = 'preference' AND is_active = 1",
            (symbiote_id,),
        )
        assert len(active) == 1
        assert active[0]["id"] != old_id


# ── existing_memories context block ───────────────────────────────────────


class TestExistingMemoriesContext:
    def test_existing_memories_includes_all_patch_types(
        self, store, adapter, symbiote_id, session_id
    ):
        """The block fed to the LLM must include preference/constraint/procedural/decision/factual."""
        from symbiote.core.models import MemoryEntry
        for t in ("preference", "constraint", "procedural", "decision", "factual"):
            store.store(MemoryEntry(
                symbiote_id=symbiote_id, type=t, scope="global",
                content=f"sample {t}", tags=[t], importance=0.5,
                source="user",
            ))

        captured: dict[str, str] = {}

        class _CaptureLLM:
            def complete(self, messages, config=None, tools=None):
                captured["prompt"] = messages[0]["content"]
                return "[]"

        _insert_messages(adapter, session_id, [("user", "anything")])
        engine = ReflectionEngine(
            store, MessageRepository(adapter),
            llm=_CaptureLLM(), mode="llm", storage=adapter,
        )
        engine.reflect_session(session_id, symbiote_id)

        prompt = captured["prompt"]
        for t in ("preference", "constraint", "procedural", "decision", "factual"):
            assert f":: {t} ::" in prompt, f"existing_memories missing type {t}"
