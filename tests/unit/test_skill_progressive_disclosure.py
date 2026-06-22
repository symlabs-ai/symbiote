"""S4 — progressive disclosure: index injection mode + skill_view tool.

Covers:
  - index mode injects the index + a skill_view hint (no full body in system)
  - full mode unchanged (no hint)
  - skill_view loads an active skill's body by name
  - skill_view refuses quarantine/archived and missing skills
  - injected_skills reflects the index (names + per-line tokens)
  - budget-aware in index mode
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import ContextAssembler
from symbiote.core.identity import IdentityManager
from symbiote.environment.manager import EnvironmentManager
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.skills import tool as skill_tool
from symbiote.skills import usage
from symbiote.skills.loader import SkillsLoader

_MD = """\
---
name: {name}
description: {desc}
---
# {name}

FULL BODY of {name} — long instructions that must NOT appear in the system.
"""


def _make_skill(root: Path, name: str, *, desc="does a thing", status=usage.STATUS_ACTIVE, agent=False) -> Path:
    sd = root / "skills" / name
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(_MD.format(name=name, desc=desc))
    usage.write_meta(sd, usage.default_meta(agent_created=agent, status=status))
    return sd


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    adp = SQLiteAdapter(db_path=tmp_path / "pd.db")
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def identity(adapter):
    return IdentityManager(storage=adapter)


@pytest.fixture()
def environment(adapter):
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def symbiote_id(identity):
    return identity.create(name="PDBot", role="assistant").id


@pytest.fixture()
def loader(tmp_path):
    return SkillsLoader(tmp_path)


def _assembler(identity, adapter, environment, loader, budget=4000):
    return ContextAssembler(
        identity=identity,
        memory=MemoryStore(storage=adapter),
        knowledge=KnowledgeService(storage=adapter),
        context_budget=budget,
        environment=environment,
        skills_loader=loader,
    )


def _build(asm, sid):
    return asm.build(session_id="s", symbiote_id=sid, user_input="hi")


# ── index vs full ─────────────────────────────────────────────────────────


class TestInjectionModes:
    def test_index_mode_injects_index_and_hint_not_body(
        self, identity, adapter, environment, loader, tmp_path, symbiote_id
    ):
        _make_skill(tmp_path, "git-helper", desc="git workflows")
        loader.refresh()
        environment.configure(
            symbiote_id, skill_injection_enabled=True, skill_injection_mode="index"
        )
        ctx = _build(_assembler(identity, adapter, environment, loader), symbiote_id)

        assert ctx.skills_summary is not None
        assert "git-helper" in ctx.skills_summary
        assert "git workflows" in ctx.skills_summary
        # the full body is never in the system prompt summary
        assert "FULL BODY" not in ctx.skills_summary
        # index mode adds the load-on-demand hint
        assert "skill_view" in ctx.skills_summary

    def test_full_mode_has_no_hint(
        self, identity, adapter, environment, loader, tmp_path, symbiote_id
    ):
        _make_skill(tmp_path, "git-helper")
        loader.refresh()
        environment.configure(
            symbiote_id, skill_injection_enabled=True, skill_injection_mode="full"
        )
        ctx = _build(_assembler(identity, adapter, environment, loader), symbiote_id)

        assert ctx.skills_summary is not None
        assert "skill_view" not in ctx.skills_summary

    def test_default_mode_is_full(
        self, identity, adapter, environment, loader, tmp_path, symbiote_id
    ):
        _make_skill(tmp_path, "git-helper")
        loader.refresh()
        # only enable, do not pass mode
        environment.configure(symbiote_id, skill_injection_enabled=True)
        assert environment.get_skill_injection_mode(symbiote_id) == "full"
        ctx = _build(_assembler(identity, adapter, environment, loader), symbiote_id)
        assert "skill_view" not in (ctx.skills_summary or "")

    def test_injected_skills_reflects_index(
        self, identity, adapter, environment, loader, tmp_path, symbiote_id
    ):
        _make_skill(tmp_path, "a", desc="alpha")
        _make_skill(tmp_path, "b", desc="beta")
        loader.refresh()
        environment.configure(
            symbiote_id, skill_injection_enabled=True, skill_injection_mode="index"
        )
        ctx = _build(_assembler(identity, adapter, environment, loader), symbiote_id)

        assert {s.name for s in ctx.injected_skills} == {"a", "b"}
        assert all(s.tokens > 0 for s in ctx.injected_skills)

    def test_index_mode_budget_aware(
        self, identity, adapter, environment, loader, tmp_path, symbiote_id
    ):
        for i in range(10):
            _make_skill(tmp_path, f"skill-{i}", desc="x" * 200)
        loader.refresh()
        environment.configure(
            symbiote_id, skill_injection_enabled=True, skill_injection_mode="index"
        )
        ctx = _build(
            _assembler(identity, adapter, environment, loader, budget=140), symbiote_id
        )
        assert 0 < len(ctx.injected_skills) < 10
        assert ctx.total_tokens_estimate <= 140


# ── skill_view tool ───────────────────────────────────────────────────────


class TestSkillViewTool:
    def test_view_loads_active_body(self, loader, tmp_path):
        _make_skill(tmp_path, "deploy", desc="deploys")
        loader.refresh()
        handler = skill_tool.make_view_handler(loader)

        out = json.loads(handler({"name": "deploy"}))

        assert out["success"] is True
        assert out["name"] == "deploy"
        assert "FULL BODY" in out["content"]
        assert out["description"] == "deploys"

    def test_view_refuses_quarantine(self, loader, tmp_path):
        _make_skill(
            tmp_path, "q-skill", status=usage.STATUS_QUARANTINE, agent=True
        )
        loader.refresh()
        handler = skill_tool.make_view_handler(loader)

        out = json.loads(handler({"name": "q-skill"}))

        assert out["success"] is False
        assert out["kind"] == "not_active"

    def test_view_missing_skill(self, loader, tmp_path):
        loader.refresh()
        handler = skill_tool.make_view_handler(loader)
        out = json.loads(handler({"name": "nope"}))
        assert out["success"] is False
        assert out["kind"] == "not_found"

    def test_view_requires_name(self, loader, tmp_path):
        handler = skill_tool.make_view_handler(loader)
        out = json.loads(handler({}))
        assert out["success"] is False


# ── registration wiring ───────────────────────────────────────────────────


class TestRegistration:
    def test_kernel_registers_skill_view_low_risk(self, tmp_path):
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        class StubLLM:
            def complete(self, messages, **kw):
                return "ok"

            def stream(self, messages, **kw):
                yield "ok"

        db = tmp_path / "k.db"
        k = SymbioteKernel(
            KernelConfig(db_path=db, skills_root=db.parent / "skills"),
            llm=StubLLM(),
        )
        desc = k.tool_gateway.get_descriptor("skill_view")
        assert desc is not None
        assert desc.risk_level == "low"
        k.shutdown()
