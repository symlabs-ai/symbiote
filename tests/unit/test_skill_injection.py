"""Tests for skill injection into the assembled context / system prompt.

Covers requirement matrix:
  (a) default off  -> nothing injected (no summary, empty injected_skills)
  (b) on           -> <available-skills> in system + injected_skills populated
  (c) quarantine   -> NOT injected (only active skills reach the prompt)
  (d) per-symbiote -> opt-in for one symbiote never leaks to another
Plus: budget-awareness and the ChatRunner rendering the summary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext, ContextAssembler
from symbiote.core.identity import IdentityManager
from symbiote.environment.manager import EnvironmentManager
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.skills import usage
from symbiote.skills.loader import SkillsLoader

_FRONTMATTER = """\
---
name: {name}
description: {desc}
---
# {name}

Full body instructions for {name}.
"""


def _make_skill(
    root: Path,
    name: str,
    *,
    desc: str = "does a thing",
    status: str = usage.STATUS_ACTIVE,
    agent_created: bool = False,
) -> Path:
    """Create root/skills/{name}/SKILL.md with a status sidecar."""
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_FRONTMATTER.format(name=name, desc=desc))
    usage.write_meta(
        skill_dir, usage.default_meta(agent_created=agent_created, status=status)
    )
    return skill_dir


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    adp = SQLiteAdapter(db_path=tmp_path / "si.db")
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def identity(adapter: SQLiteAdapter) -> IdentityManager:
    return IdentityManager(storage=adapter)


@pytest.fixture()
def environment(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def memory(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(storage=adapter)


@pytest.fixture()
def knowledge(adapter: SQLiteAdapter) -> KnowledgeService:
    return KnowledgeService(storage=adapter)


@pytest.fixture()
def symbiote_id(identity: IdentityManager) -> str:
    return identity.create(name="SkillBot", role="assistant").id


@pytest.fixture()
def loader(tmp_path: Path) -> SkillsLoader:
    return SkillsLoader(tmp_path)


def _assembler(
    identity: IdentityManager,
    memory: MemoryStore,
    knowledge: KnowledgeService,
    environment: EnvironmentManager,
    loader: SkillsLoader | None,
    *,
    budget: int = 4000,
) -> ContextAssembler:
    return ContextAssembler(
        identity=identity,
        memory=memory,
        knowledge=knowledge,
        context_budget=budget,
        environment=environment,
        skills_loader=loader,
    )


def _build(
    assembler: ContextAssembler, symbiote_id: str
) -> AssembledContext:
    return assembler.build(
        session_id="sess-1", symbiote_id=symbiote_id, user_input="hello"
    )


# ── (a) default off ──────────────────────────────────────────────────────────


class TestDefaultOff:
    def test_no_opt_in_means_no_injection(
        self, identity, memory, knowledge, environment, loader, tmp_path, symbiote_id
    ) -> None:
        _make_skill(tmp_path, "git-helper")
        loader.refresh()
        asm = _assembler(identity, memory, knowledge, environment, loader)

        ctx = _build(asm, symbiote_id)

        assert ctx.skills_summary is None
        assert ctx.injected_skills == []

    def test_no_loader_means_no_injection_even_if_enabled(
        self, identity, memory, knowledge, environment, symbiote_id
    ) -> None:
        environment.configure(symbiote_id, skill_injection_enabled=True)
        asm = _assembler(identity, memory, knowledge, environment, loader=None)

        ctx = _build(asm, symbiote_id)

        assert ctx.skills_summary is None
        assert ctx.injected_skills == []


# ── (b) on ───────────────────────────────────────────────────────────────────


class TestEnabled:
    def test_active_skills_injected_with_breakdown(
        self, identity, memory, knowledge, environment, loader, tmp_path, symbiote_id
    ) -> None:
        _make_skill(tmp_path, "git-helper", desc="git workflows")
        _make_skill(tmp_path, "csv-parser", desc="parse csv files")
        loader.refresh()
        environment.configure(symbiote_id, skill_injection_enabled=True)
        asm = _assembler(identity, memory, knowledge, environment, loader)

        ctx = _build(asm, symbiote_id)

        assert ctx.skills_summary is not None
        assert "<available-skills>" in ctx.skills_summary
        assert "git-helper" in ctx.skills_summary
        assert "csv-parser" in ctx.skills_summary
        names = {s.name for s in ctx.injected_skills}
        assert names == {"git-helper", "csv-parser"}
        # tokens estimated per skill, and folded into the total
        assert all(s.tokens > 0 for s in ctx.injected_skills)
        assert ctx.total_tokens_estimate > 0

    def test_summary_rendered_into_system_prompt(
        self, identity, memory, knowledge, environment, loader, tmp_path, symbiote_id
    ) -> None:
        from symbiote.runners.chat import ChatRunner

        _make_skill(tmp_path, "deploy-helper", desc="deploys things")
        loader.refresh()
        environment.configure(symbiote_id, skill_injection_enabled=True)
        asm = _assembler(identity, memory, knowledge, environment, loader)
        ctx = _build(asm, symbiote_id)

        runner = ChatRunner(llm=object())  # llm unused by _build_system
        system = runner._build_system(ctx)

        assert "## Skills" in system
        assert "<available-skills>" in system
        assert "deploy-helper" in system


# ── (c) quarantine excluded ──────────────────────────────────────────────────


class TestQuarantineExcluded:
    def test_quarantine_skill_not_injected(
        self, identity, memory, knowledge, environment, loader, tmp_path, symbiote_id
    ) -> None:
        _make_skill(tmp_path, "active-skill", status=usage.STATUS_ACTIVE)
        _make_skill(
            tmp_path,
            "quarantined-skill",
            status=usage.STATUS_QUARANTINE,
            agent_created=True,
        )
        loader.refresh()
        environment.configure(symbiote_id, skill_injection_enabled=True)
        asm = _assembler(identity, memory, knowledge, environment, loader)

        ctx = _build(asm, symbiote_id)

        names = {s.name for s in ctx.injected_skills}
        assert names == {"active-skill"}
        assert "quarantined-skill" not in (ctx.skills_summary or "")


# ── (d) per-symbiote isolation ───────────────────────────────────────────────


class TestPerSymbioteIsolation:
    def test_opt_in_does_not_leak_to_other_symbiote(
        self, identity, memory, knowledge, environment, loader, tmp_path
    ) -> None:
        sym_on = identity.create(name="OptIn", role="assistant").id
        sym_off = identity.create(name="OptOut", role="assistant").id
        _make_skill(tmp_path, "shared-skill")
        loader.refresh()
        # Only sym_on opts in.
        environment.configure(sym_on, skill_injection_enabled=True)
        asm = _assembler(identity, memory, knowledge, environment, loader)

        ctx_on = _build(asm, sym_on)
        ctx_off = _build(asm, sym_off)

        assert {s.name for s in ctx_on.injected_skills} == {"shared-skill"}
        assert ctx_off.skills_summary is None
        assert ctx_off.injected_skills == []


# ── budget awareness ─────────────────────────────────────────────────────────


class TestBudgetAware:
    def test_skills_dropped_when_budget_too_small(
        self, identity, memory, knowledge, environment, loader, tmp_path, symbiote_id
    ) -> None:
        for i in range(8):
            _make_skill(
                tmp_path, f"skill-{i}", desc="x" * 200  # ~50 tokens of desc each
            )
        loader.refresh()
        environment.configure(symbiote_id, skill_injection_enabled=True)
        # Tiny budget: only a couple of skills can fit.
        asm = _assembler(identity, memory, knowledge, environment, loader, budget=120)

        ctx = _build(asm, symbiote_id)

        # Some injected, but not all, and we stayed within budget.
        assert 0 < len(ctx.injected_skills) < 8
        assert ctx.total_tokens_estimate <= 120

    def test_no_room_injects_nothing(
        self, identity, memory, knowledge, environment, loader, tmp_path, symbiote_id
    ) -> None:
        _make_skill(tmp_path, "skill-a", desc="y" * 400)
        loader.refresh()
        environment.configure(symbiote_id, skill_injection_enabled=True)
        # Budget already consumed by persona/user_input; ~10 tokens left.
        asm = _assembler(identity, memory, knowledge, environment, loader, budget=10)

        ctx = _build(asm, symbiote_id)

        assert ctx.skills_summary is None
        assert ctx.injected_skills == []
