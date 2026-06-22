"""S5 — skill review quality criteria (stop learning junk).

Covers:
  - default prompt carries the core anti-patterns (incl. persona re-doc)
  - build_skill_review_prompt(strict=True) adds the strict block
  - build_skill_review_prompt(extra_criteria=...) appends host criteria
  - build_skill_review_prompt() with no args == baseline (unchanged)
  - the engine resolves per-symbiote criteria into the prompt it sends
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core import _review_prompts as rp
from symbiote.core.background_review import BackgroundReviewEngine
from symbiote.core.identity import IdentityManager
from symbiote.environment.manager import EnvironmentManager
from symbiote.skills.loader import SkillsLoader
from symbiote.skills.store import SkillsStore

# ── prompt composition (pure) ─────────────────────────────────────────────


class TestPromptComposition:
    def test_default_prompt_has_core_anti_patterns(self):
        p = rp.SKILL_REVIEW_PROMPT
        # env-dependent failures, negative tool claims, one-off narratives
        assert "command not found" in p or "comando não encontrado" in p
        assert "broken" in p or "quebrado" in p
        assert "One-off task narratives" in p
        # new persona re-documentation rule
        assert "ALREADY has" in p

    def test_no_args_returns_baseline(self):
        assert rp.build_skill_review_prompt() == rp.SKILL_REVIEW_PROMPT

    def test_strict_adds_strict_block(self):
        p = rp.build_skill_review_prompt(strict=True)
        assert "STRICT MODE" in p
        assert "class-level" in p
        # placeholders preserved for render_prompt
        assert "{existing_skills}" in p
        assert "{messages}" in p

    def test_extra_criteria_appended(self):
        p = rp.build_skill_review_prompt(
            extra_criteria="Never capture anything about PII handling."
        )
        assert "ADDITIONAL HOST CRITERIA" in p
        assert "PII handling" in p
        assert "{messages}" in p

    def test_strict_and_extra_combined(self):
        p = rp.build_skill_review_prompt(
            strict=True, extra_criteria="Domain rule X."
        )
        assert "STRICT MODE" in p
        assert "Domain rule X." in p


# ── engine integration ────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    adp = SQLiteAdapter(db_path=tmp_path / "rc.db")
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter):
    return IdentityManager(storage=adapter).create(name="RCBot", role="assistant").id


@pytest.fixture()
def session_id(adapter, symbiote_id):
    sid = str(uuid4())
    adapter.execute(
        "INSERT INTO sessions (id, symbiote_id, status, started_at) "
        "VALUES (?, ?, 'active', datetime('now'))",
        (sid, symbiote_id),
    )
    adapter.execute(
        "INSERT INTO messages (id, session_id, role, content, created_at) "
        "VALUES (?, ?, 'user', 'oi', datetime('now'))",
        (str(uuid4()), sid),
    )
    return sid


class _CapturingLLM:
    """Captures the prompt it receives; returns empty op list."""

    def __init__(self):
        self.last_prompt = ""

    def complete(self, messages, config=None, tools=None):
        self.last_prompt = messages[0]["content"]
        return json.dumps([])


def _engine(adapter, environment, tmp_path, llm):
    return BackgroundReviewEngine(
        llm=llm,
        messages=MessageRepository(adapter),
        store=SkillsStore(roots=[tmp_path / "skills"]),
        loader=SkillsLoader(tmp_path),
        storage=adapter,
        environment=environment,
    )


class TestEngineAppliesCriteria:
    def test_strict_criteria_reaches_prompt(
        self, adapter, symbiote_id, session_id, tmp_path
    ):
        env = EnvironmentManager(storage=adapter)
        env.configure(symbiote_id, skill_review_strict=True)
        llm = _CapturingLLM()
        eng = _engine(adapter, env, tmp_path, llm)

        eng.run_sync(session_id, symbiote_id)

        assert "STRICT MODE" in llm.last_prompt

    def test_extra_criteria_reaches_prompt(
        self, adapter, symbiote_id, session_id, tmp_path
    ):
        env = EnvironmentManager(storage=adapter)
        env.configure(
            symbiote_id, skill_review_extra_criteria="Forbid codename skills."
        )
        llm = _CapturingLLM()
        eng = _engine(adapter, env, tmp_path, llm)

        eng.run_sync(session_id, symbiote_id)

        assert "Forbid codename skills." in llm.last_prompt

    def test_no_criteria_uses_baseline(
        self, adapter, symbiote_id, session_id, tmp_path
    ):
        env = EnvironmentManager(storage=adapter)
        env.configure(symbiote_id)  # nothing set
        llm = _CapturingLLM()
        eng = _engine(adapter, env, tmp_path, llm)

        eng.run_sync(session_id, symbiote_id)

        assert "STRICT MODE" not in llm.last_prompt
        assert "ADDITIONAL HOST CRITERIA" not in llm.last_prompt

    def test_criteria_isolated_per_symbiote(
        self, adapter, session_id, tmp_path
    ):
        env = EnvironmentManager(storage=adapter)
        ident = IdentityManager(storage=adapter)
        strict_sym = ident.create(name="Strict", role="a").id
        loose_sym = ident.create(name="Loose", role="a").id
        env.configure(strict_sym, skill_review_strict=True)
        # loose_sym has no config

        # session_id belongs to RCBot; make per-symbiote sessions
        def _sess(sym):
            sid = str(uuid4())
            adapter.execute(
                "INSERT INTO sessions (id, symbiote_id, status, started_at) "
                "VALUES (?, ?, 'active', datetime('now'))",
                (sid, sym),
            )
            adapter.execute(
                "INSERT INTO messages (id, session_id, role, content, created_at) "
                "VALUES (?, ?, 'user', 'oi', datetime('now'))",
                (str(uuid4()), sid),
            )
            return sid

        llm = _CapturingLLM()
        eng = _engine(adapter, env, tmp_path, llm)

        eng.run_sync(_sess(strict_sym), strict_sym)
        assert "STRICT MODE" in llm.last_prompt

        eng.run_sync(_sess(loose_sym), loose_sym)
        assert "STRICT MODE" not in llm.last_prompt
