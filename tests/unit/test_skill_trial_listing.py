"""Skills on trial listed to the model by option (B-81).

A skill the agent writes starts in quarantine. ``listable_skills`` shows only
active skills and ``skill_view`` refuses the others, so nothing in a turn ever
used a skill on trial and ``skill_auto_promote_threshold`` could not fire.
With ``EnvironmentConfig.skill_trial_listing`` the symbiote lists them, marked
as on trial, and reading one with ``skill_view`` counts as a use.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.skills import usage
from tests.support import ScriptedLLM, make_kernel, tool_call_block

_MD = """\
---
name: {name}
description: {desc}
---
# {name}

BODY of {name}.
"""


def _write_skill(root: Path, name: str, status: str) -> Path:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_MD.format(name=name, desc=f"about {name}"))
    usage.write_meta(skill_dir, usage.default_meta(agent_created=True, status=status))
    return skill_dir


@pytest.fixture(params=["global", "per_symbiote"])
def shop(tmp_path: Path, request):
    """A symbiote with one active skill and one on trial, in both skill scopes."""
    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm, skill_scope=request.param)
    sym = kernel.create_symbiote(name="Shop", role="assistant")
    # global: {skills_root}/<name>; per_symbiote: {skills_root}/<id>/skills/<name>
    root = tmp_path if request.param == "global" else tmp_path / "skills" / sym.id
    _write_skill(root, "refunds", usage.STATUS_ACTIVE)
    trial_dir = _write_skill(root, "returns", usage.STATUS_QUARANTINE)
    kernel.skills_loader_for(sym.id).refresh()
    kernel.environment.configure(
        symbiote_id=sym.id,
        tools=["skill_view"],
        skill_injection_enabled=True,
        skill_injection_mode="index",
    )
    yield kernel, llm, sym, trial_dir
    kernel.shutdown()


def _view(kernel, llm, sym, name: str) -> dict:
    """One turn in which the model calls ``skill_view(name)``; its result."""
    start = len(llm.calls)
    llm._fn = lambda i, _m: tool_call_block("skill_view", name=name) if i == start else "done"
    session = kernel.start_session(sym.id)
    outcome = kernel.turn(session.id, "go")
    (result,) = [r for r in outcome.tool_results if r.tool_id == "skill_view"]
    return result.output if isinstance(result.output, dict) else json.loads(result.output)


def test_trial_skills_stay_hidden_by_default(shop) -> None:
    kernel, llm, sym, trial_dir = shop

    answer = _view(kernel, llm, sym, "returns")

    prompt = llm.prompt_text(0)
    assert 'name="refunds"' in prompt
    assert "returns" not in prompt
    assert answer["success"] is False
    assert answer["kind"] == "not_active"
    assert usage.read_meta(trial_dir)["use_count"] == 0


def test_trial_skills_are_listed_and_marked_when_the_symbiote_opts_in(shop) -> None:
    kernel, llm, sym, _trial_dir = shop
    kernel.environment.configure(symbiote_id=sym.id, skill_trial_listing=True)

    session = kernel.start_session(sym.id)
    kernel.message(session.id, "hi")

    prompt = llm.prompt_text(0)
    assert '<skill name="refunds">' in prompt
    assert '<skill name="returns" status="trial">' in prompt
    assert "not reviewed yet" in prompt


def test_reading_a_trial_skill_counts_and_promotes_at_the_threshold(shop) -> None:
    kernel, llm, sym, trial_dir = shop
    kernel.environment.configure(
        symbiote_id=sym.id, skill_trial_listing=True, skill_auto_promote_threshold=2
    )

    first = _view(kernel, llm, sym, "returns")
    assert first["success"] is True
    assert first["status"] == "trial"
    assert "BODY of returns" in first["content"]
    assert usage.read_meta(trial_dir)["status"] == usage.STATUS_QUARANTINE

    second = _view(kernel, llm, sym, "returns")
    assert second["status"] == "active"
    assert usage.read_meta(trial_dir)["status"] == usage.STATUS_ACTIVE
    # From now on it is an ordinary active skill.
    third = _view(kernel, llm, sym, "returns")
    assert third["success"] is True
    assert "status" not in third


def test_a_full_library_counts_the_use_but_does_not_promote(shop) -> None:
    kernel, llm, sym, trial_dir = shop
    kernel.environment.configure(
        symbiote_id=sym.id,
        skill_trial_listing=True,
        skill_auto_promote_threshold=1,
        max_active_skills=1,
    )

    answer = _view(kernel, llm, sym, "returns")

    assert answer["status"] == "trial"
    meta = usage.read_meta(trial_dir)
    assert meta["status"] == usage.STATUS_QUARANTINE
    assert meta["use_count"] == 1


def test_threshold_zero_never_promotes(shop) -> None:
    kernel, llm, sym, trial_dir = shop
    kernel.environment.configure(
        symbiote_id=sym.id, skill_trial_listing=True, skill_auto_promote_threshold=0
    )

    for _ in range(3):
        assert _view(kernel, llm, sym, "returns")["status"] == "trial"

    assert usage.read_meta(trial_dir)["status"] == usage.STATUS_QUARANTINE


def test_option_is_stored_per_symbiote_and_exposed_by_the_api(api_client) -> None:
    api = api_client(ScriptedLLM("ok"))
    headers = api.auth(api.admin_key)
    sym_id = api.client.post(
        "/symbiotes", json={"name": "S", "role": "assistant"}, headers=headers
    ).json()["id"]

    before = api.client.get(f"/symbiotes/{sym_id}/config", headers=headers).json()
    after = api.client.put(
        f"/symbiotes/{sym_id}/config", json={"skill_trial_listing": True}, headers=headers
    ).json()

    assert before["skill_trial_listing"] is False
    assert after["skill_trial_listing"] is True
