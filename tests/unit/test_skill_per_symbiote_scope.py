"""Per-symbiote skill scope (Option A) — binary isolation tests.

Covers:
  - default scope "global" unchanged (single store/loader; properties work)
  - per_symbiote: each symbiote has its own root; u1 cannot see/edit/use u2
    in load, injection, skill_view and skill_manage
  - name collision between users allowed
  - shared read-only catalogue visible to all but not writable
  - skills_loader_for / skills_store_for resolve per-user; single properties
    return None in per_symbiote mode
  - review engine scoped per-symbiote (writes land in the right root)
  - GC (dream) scoped per-symbiote
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.skills import scope as skill_scope
from symbiote.skills import usage


class StubLLM:
    def complete(self, messages, **kw):
        return "ok"

    def stream(self, messages, **kw):
        yield "ok"


def _kernel(tmp_path: Path, *, scope: str, shared: Path | None = None) -> SymbioteKernel:
    db = tmp_path / "k.db"
    kw = {"db_path": db, "skills_root": tmp_path / "skills", "skill_scope": scope}
    if shared is not None:
        kw["skills_protected_roots"] = [shared]
    return SymbioteKernel(KernelConfig(**kw), llm=StubLLM())


def _write_skill(store, name, desc="d", body="BODY"):
    return store.create(name=name, content=f"---\nname: {name}\ndescription: {desc}\n---\n{body}")


# ── default global unchanged ──────────────────────────────────────────────


class TestGlobalDefault:
    def test_default_scope_is_global_single_store(self, tmp_path):
        k = _kernel(tmp_path, scope="global")
        assert k.skills_store is not None
        assert k.skills_loader is not None
        # _for resolves to the same single instance regardless of id
        assert k.skills_store_for("any") is k.skills_store
        assert k.skills_loader_for("x") is k.skills_loader
        k.shutdown()


# ── per-symbiote isolation ────────────────────────────────────────────────


class TestPerSymbioteIsolation:
    def test_single_properties_none_in_per_symbiote(self, tmp_path):
        k = _kernel(tmp_path, scope="per_symbiote")
        assert k.skills_store is None
        assert k.skills_loader is None
        k.shutdown()

    def test_each_symbiote_has_own_store_and_isolated(self, tmp_path):
        k = _kernel(tmp_path, scope="per_symbiote")
        u1 = k.create_symbiote(name="U1", role="a").id
        u2 = k.create_symbiote(name="U2", role="a").id

        _write_skill(k.skills_store_for(u1), "u1-only", desc="secret1")
        k.skills_loader_for(u1).refresh()  # host refreshes after a store write

        # u1 sees it; u2 does NOT
        u1_names = {s.name for s in k.skills_loader_for(u1).list_skills()}
        u2_names = {s.name for s in k.skills_loader_for(u2).list_skills()}
        assert "u1-only" in u1_names
        assert "u1-only" not in u2_names
        k.shutdown()

    def test_name_collision_between_users_allowed(self, tmp_path):
        k = _kernel(tmp_path, scope="per_symbiote")
        u1 = k.create_symbiote(name="U1", role="a").id
        u2 = k.create_symbiote(name="U2", role="a").id

        r1 = _write_skill(k.skills_store_for(u1), "git-helper", body="U1 BODY")
        r2 = _write_skill(k.skills_store_for(u2), "git-helper", body="U2 BODY")
        assert r1.success and r2.success
        k.skills_loader_for(u1).refresh()
        k.skills_loader_for(u2).refresh()

        b1 = k.skills_loader_for(u1).get_skill("git-helper").content
        b2 = k.skills_loader_for(u2).get_skill("git-helper").content
        assert "U1 BODY" in b1
        assert "U2 BODY" in b2

    def test_injection_scoped_to_active_symbiote(self, tmp_path):
        k = _kernel(tmp_path, scope="per_symbiote")
        u1 = k.create_symbiote(name="U1", role="a").id
        u2 = k.create_symbiote(name="U2", role="a").id
        _write_skill(k.skills_store_for(u1), "u1-skill", desc="for u1")
        k.skills_loader_for(u1).refresh()
        k.environment.configure(u1, skill_injection_enabled=True)
        k.environment.configure(u2, skill_injection_enabled=True)

        ctx_u1 = k._context_assembler.build(session_id="s1", symbiote_id=u1, user_input="hi")
        ctx_u2 = k._context_assembler.build(session_id="s2", symbiote_id=u2, user_input="hi")

        assert "u1-skill" in (ctx_u1.skills_summary or "")
        assert {s.name for s in ctx_u1.injected_skills} == {"u1-skill"}
        # u2 sees nothing of u1
        assert ctx_u2.skills_summary is None
        assert ctx_u2.injected_skills == []
        k.shutdown()

    def test_skill_view_scoped_by_active_contextvar(self, tmp_path):
        k = _kernel(tmp_path, scope="per_symbiote")
        u1 = k.create_symbiote(name="U1", role="a").id
        u2 = k.create_symbiote(name="U2", role="a").id
        _write_skill(k.skills_store_for(u1), "u1-skill", body="U1 SECRET")
        k.skills_loader_for(u1).refresh()
        k.environment.configure(u1, tools=["skill_view"])
        k.environment.configure(u2, tools=["skill_view"])

        # active = u1 → sees it
        tok = skill_scope.set_active_symbiote(u1)
        try:
            out = json.loads(
                k.tool_gateway.execute(u1, None, "skill_view", {"name": "u1-skill"}).output
            )
        finally:
            skill_scope.reset_active_symbiote(tok)
        assert out["success"] is True and "U1 SECRET" in out["content"]

        # active = u2 → cannot see u1's skill
        tok = skill_scope.set_active_symbiote(u2)
        try:
            out2 = json.loads(
                k.tool_gateway.execute(u2, None, "skill_view", {"name": "u1-skill"}).output
            )
        finally:
            skill_scope.reset_active_symbiote(tok)
        assert out2["success"] is False
        k.shutdown()

    def test_skill_manage_writes_to_active_symbiote_root(self, tmp_path):
        k = _kernel(tmp_path, scope="per_symbiote")
        u1 = k.create_symbiote(name="U1", role="a").id
        u2 = k.create_symbiote(name="U2", role="a").id
        k.environment.configure(u1, tools=["skill_manage"])

        tok = skill_scope.set_active_symbiote(u1)
        try:
            out = json.loads(
                k.tool_gateway.execute(
                    u1, None, "skill_manage",
                    {"action": "create", "name": "made-by-u1",
                     "content": "---\nname: made-by-u1\ndescription: x\n---\nbody"},
                ).output
            )
        finally:
            skill_scope.reset_active_symbiote(tok)
        assert out["success"] is True

        # landed in u1's root only
        assert (tmp_path / "skills" / u1 / "skills" / "made-by-u1" / "SKILL.md").is_file()
        assert not (tmp_path / "skills" / u2 / "skills" / "made-by-u1").exists()
        k.shutdown()


# ── shared read-only catalogue ────────────────────────────────────────────


class TestSharedCatalogue:
    def test_shared_catalogue_visible_to_all_not_writable(self, tmp_path):
        shared = tmp_path / "shared" / "skills"
        # seed a factory skill in the shared catalogue
        fd = shared / "factory-skill"
        fd.mkdir(parents=True)
        (fd / "SKILL.md").write_text("---\nname: factory-skill\ndescription: common\n---\nSHARED BODY")
        usage.write_meta(fd, usage.default_meta(agent_created=False, status=usage.STATUS_ACTIVE))

        k = _kernel(tmp_path, scope="per_symbiote", shared=shared)
        u1 = k.create_symbiote(name="U1", role="a").id
        u2 = k.create_symbiote(name="U2", role="a").id

        # both users see it
        assert "factory-skill" in {s.name for s in k.skills_loader_for(u1).list_skills()}
        assert "factory-skill" in {s.name for s in k.skills_loader_for(u2).list_skills()}

        # but cannot write it: the shared catalogue is not in the store's
        # writable roots, so an edit attempt is refused (NotFound or Protected
        # — both mean "the user cannot mutate the shared catalogue").
        from symbiote.skills.store import SkillNotFoundError, SkillProtectedError
        with pytest.raises((SkillProtectedError, SkillNotFoundError)):
            k.skills_store_for(u1).edit(
                "factory-skill",
                "---\nname: factory-skill\ndescription: hacked\n---\nx",
            )
        # and a same-named create lands in the USER's root, never the catalogue
        k.skills_store_for(u1).create(
            "factory-skill", "---\nname: factory-skill\ndescription: mine\n---\nMINE"
        )
        assert (tmp_path / "skills" / u1 / "skills" / "factory-skill" / "SKILL.md").is_file()
        # shared catalogue file untouched
        assert "SHARED BODY" in (shared / "factory-skill" / "SKILL.md").read_text()
        k.shutdown()


# ── review engine scoped ──────────────────────────────────────────────────


class TestReviewScoped:
    def test_review_engine_per_symbiote_distinct(self, tmp_path):
        k = _kernel(tmp_path, scope="per_symbiote")
        k.set_evolver_llm(StubLLM())
        u1 = k.create_symbiote(name="U1", role="a").id
        u2 = k.create_symbiote(name="U2", role="a").id
        k.environment.configure(u1, skill_review_enabled=True)
        k.environment.configure(u2, skill_review_enabled=True)

        e1 = k._background_review_for(u1)
        e2 = k._background_review_for(u2)
        assert e1 is not None and e2 is not None
        assert e1 is not e2  # distinct engines, distinct stores
        k.shutdown()


class TestGcScoped:
    def test_dream_engine_uses_symbiote_loader(self, tmp_path):
        k = _kernel(tmp_path, scope="per_symbiote")
        u1 = k.create_symbiote(name="U1", role="a").id
        u2 = k.create_symbiote(name="U2", role="a").id
        # the GC loader resolver is per-symbiote and returns distinct loaders
        l1 = k._skills_loader_for(u1)
        l2 = k._skills_loader_for(u2)
        assert l1 is not None and l2 is not None
        assert l1 is not l2
        k.shutdown()
