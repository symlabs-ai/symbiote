"""The default revision of a symbiote and the strict delegation (B-97).

A session pinned to ``L`` delegated to the revision ``L`` of the other
symbiote and, when there was none, to whatever was being edited there: the
person on the other side saw a draft through the colleague. A symbiote may
now have a default revision, where such a delegation lands, and a session
may refuse to delegate to anything that is not a revision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from tests.support import ScriptedLLM, make_kernel, tool_call_block


class Team:
    """``boss`` delegates to ``worker``; both were edited after ``V1``."""

    def __init__(self, tmp_path: Path) -> None:
        def script(_i, messages):
            last = str(messages[-1]["content"])
            if "[Tool" in last:
                return "done"
            if "SUBTASK" in last:
                return "worker answer"
            return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK")

        self.llm = ScriptedLLM(script)
        self.kernel = make_kernel(tmp_path, self.llm)
        k = self.kernel
        self.boss = k.create_symbiote(name="boss", role="front desk", persona={"tone": "BOSS-V1"})
        self.worker = k.create_symbiote(
            name="worker", role="orders", persona={"tone": "WORKER-V1"}
        )
        k.environment.configure(symbiote_id=self.boss.id, tools=["spawn"])

    def publish(self, symbiote, label: str, then: str) -> None:
        """Take the picture under ``label`` and go on editing (``then``)."""
        self.kernel.create_revision(symbiote.id, label)
        self.kernel.update_symbiote(symbiote.id, persona={"tone": then})

    def worker_prompt(self) -> str | None:
        found = [
            i for i, call in enumerate(self.llm.calls)
            if "SUBTASK" in str(call["messages"][-1].get("content"))
            and "[Tool" not in str(call["messages"][-1].get("content"))
        ]
        return self.llm.prompt_text(found[-1]) if found else None

    def spawn_result(self, session_id: str):
        (result,) = self.kernel.turn(session_id, "go").tool_results
        return result

    def sub_sessions(self) -> list[dict]:
        return self.kernel.inspector.list_sessions(self.worker.id)


@pytest.fixture()
def team(tmp_path: Path):
    t = Team(tmp_path)
    yield t
    t.kernel.shutdown()


# ── where a delegation lands ────────────────────────────────────────────


def test_without_the_same_label_the_delegate_answers_from_its_default(team) -> None:
    k = team.kernel
    team.publish(team.boss, "boss-v3", "BOSS-DRAFT")
    team.publish(team.worker, "worker-v7", "WORKER-DRAFT")
    k.set_default_revision(team.worker.id, "worker-v7")
    session = k.start_session(team.boss.id, revision="boss-v3")

    k.message(session.id, "go")

    prompt = team.worker_prompt()
    assert "WORKER-V1" in prompt and "WORKER-DRAFT" not in prompt
    (sub,) = team.sub_sessions()
    assert sub["revision"] == "worker-v7"  # the audit says which one answered


def test_the_same_label_comes_before_the_default(team) -> None:
    k = team.kernel
    team.publish(team.boss, "release-1", "BOSS-DRAFT")
    team.publish(team.worker, "release-1", "WORKER-V2")
    team.publish(team.worker, "worker-v7", "WORKER-DRAFT")
    k.set_default_revision(team.worker.id, "worker-v7")
    session = k.start_session(team.boss.id, revision="release-1")

    k.message(session.id, "go")

    assert "WORKER-V1" in team.worker_prompt()
    assert team.sub_sessions()[0]["revision"] == "release-1"


def test_a_retired_same_label_gives_way_to_the_default(team) -> None:
    k = team.kernel
    team.publish(team.boss, "release-1", "BOSS-DRAFT")
    team.publish(team.worker, "release-1", "WORKER-V2")
    team.publish(team.worker, "worker-v7", "WORKER-DRAFT")
    k.retire_revision(team.worker.id, "release-1")
    k.set_default_revision(team.worker.id, "worker-v7")
    session = k.start_session(team.boss.id, revision="release-1")

    k.message(session.id, "go")

    assert "WORKER-V2" in team.worker_prompt()
    assert team.sub_sessions()[0]["revision"] == "worker-v7"


def test_a_label_retired_by_another_replica_stops_taking_delegations(tmp_path: Path) -> None:
    team = Team(tmp_path)
    try:
        k = team.kernel
        team.publish(team.boss, "release-1", "BOSS-DRAFT")
        team.publish(team.worker, "release-1", "WORKER-DRAFT")
        session = k.start_session(team.boss.id, revision="release-1")
        k.message(session.id, "go")  # this replica has read the worker's release-1

        other = make_kernel(tmp_path, ScriptedLLM("ok"))
        other.retire_revision(team.worker.id, "release-1")
        other.shutdown()
        k.message(session.id, "go")

        newest, oldest = team.sub_sessions()
        assert (oldest["revision"], newest["revision"]) == ("release-1", None)
    finally:
        team.kernel.shutdown()


def test_with_no_default_an_open_session_still_reaches_the_current_configuration(team) -> None:
    k = team.kernel
    team.publish(team.boss, "boss-v3", "BOSS-DRAFT")
    team.publish(team.worker, "worker-v7", "WORKER-DRAFT")
    session = k.start_session(team.boss.id, revision="boss-v3")

    k.message(session.id, "go")

    assert "WORKER-DRAFT" in team.worker_prompt()
    assert team.sub_sessions()[0]["revision"] is None


def test_a_session_that_is_not_pinned_delegates_to_the_current_configuration(team) -> None:
    # The owner trying the draft: the default changes nothing for them.
    k = team.kernel
    team.publish(team.worker, "worker-v7", "WORKER-DRAFT")
    k.set_default_revision(team.worker.id, "worker-v7")

    k.message(k.start_session(team.boss.id).id, "go")

    assert "WORKER-DRAFT" in team.worker_prompt()
    assert team.sub_sessions()[0]["revision"] is None


def test_the_default_does_not_change_what_a_new_session_reads(team) -> None:
    k = team.kernel
    team.publish(team.worker, "worker-v7", "WORKER-DRAFT")
    k.set_default_revision(team.worker.id, "worker-v7")

    session = k.start_session(team.worker.id)
    k.message(session.id, "hello")

    assert session.revision is None
    assert "WORKER-DRAFT" in team.llm.prompt_text()


# ── strict delegation ───────────────────────────────────────────────────


def test_strict_refuses_when_the_delegate_has_neither_label_nor_default(team) -> None:
    k = team.kernel
    team.publish(team.boss, "boss-v3", "BOSS-DRAFT")
    team.publish(team.worker, "worker-v7", "WORKER-DRAFT")  # published, but not the default
    session = k.start_session(team.boss.id, revision="boss-v3", delegation="strict")

    result = team.spawn_result(session.id)

    assert result.success is False
    assert "delegation to 'worker' refused" in result.error
    assert "no revision 'boss-v3' and no default revision" in result.error
    assert team.worker_prompt() is None  # the worker never ran
    assert team.sub_sessions() == []
    # The model was told why, and the refusal is in the audit log.
    assert "delegation to 'worker' refused" in team.llm.prompt_text()
    (row,) = k._storage.fetch_all(
        "SELECT action, result FROM audit_log WHERE session_id = ? AND tool_id = 'spawn'",
        (session.id,),
    )
    assert (row["action"], row["result"]) == ("execute", "error:DelegationRefusedError")


def test_strict_lands_on_the_default_like_an_open_session(team) -> None:
    k = team.kernel
    team.publish(team.boss, "boss-v3", "BOSS-DRAFT")
    team.publish(team.worker, "worker-v7", "WORKER-DRAFT")
    k.set_default_revision(team.worker.id, "worker-v7")
    session = k.start_session(team.boss.id, revision="boss-v3", delegation="strict")

    assert team.spawn_result(session.id).success is True

    (sub,) = team.sub_sessions()
    assert (sub["revision"], sub["delegation"]) == ("worker-v7", "strict")  # the chain stays strict
    assert "WORKER-V1" in team.worker_prompt()


def test_strict_on_a_session_that_is_not_pinned(team) -> None:
    k = team.kernel
    session = k.start_session(team.boss.id, delegation="strict")

    refused = team.spawn_result(session.id)
    assert refused.success is False and "it has no default revision" in refused.error

    team.publish(team.worker, "worker-v7", "WORKER-DRAFT")
    k.set_default_revision(team.worker.id, "worker-v7")
    assert team.spawn_result(session.id).success is True
    assert team.sub_sessions()[0]["revision"] == "worker-v7"


def test_the_same_symbiote_is_strict_for_one_audience_and_open_for_another(team) -> None:
    k = team.kernel
    team.publish(team.boss, "boss-v3", "BOSS-DRAFT")
    public = k.start_session(team.boss.id, revision="boss-v3", delegation="strict")
    owner = k.start_session(team.boss.id)

    assert team.spawn_result(public.id).success is False
    assert team.spawn_result(owner.id).success is True

    k.set_session_delegation(public.id, "open")
    assert team.spawn_result(public.id).success is True
    assert k.get_session(public.id).delegation == "open"


# ── moving the default ──────────────────────────────────────────────────


def test_the_default_is_one_per_symbiote_and_can_be_moved_or_removed(team) -> None:
    k, worker = team.kernel, team.worker
    assert k.get_default_revision(worker.id) is None
    team.publish(worker, "v1", "D1")
    team.publish(worker, "v2", "D2")

    assert k.set_default_revision(worker.id, "v1").is_default is True
    assert k.set_default_revision(worker.id, "v2").label == "v2"

    assert k.get_default_revision(worker.id).label == "v2"
    assert k.get_revision(worker.id, "v1").is_default is False
    assert {r["label"]: r["is_default"] for r in k.inspector.list_revisions(worker.id)} == {
        "v1": False, "v2": True,
    }
    assert {r.label: r.is_default for r in k.list_revisions(worker.id)} == {
        "v1": False, "v2": True,
    }

    assert k.set_default_revision(worker.id, None) is None
    assert k.get_default_revision(worker.id) is None
    assert k.get_default_revision(team.boss.id) is None  # each symbiote has its own


def test_the_default_must_be_a_revision_that_takes_sessions(team) -> None:
    k, worker = team.kernel, team.worker
    team.publish(worker, "v1", "D1")
    team.publish(worker, "v2", "D2")
    k.retire_revision(worker.id, "v1")

    with pytest.raises(ValidationError):
        k.set_default_revision(worker.id, "v1")
    with pytest.raises(EntityNotFoundError):
        k.set_default_revision(worker.id, "v9")
    with pytest.raises(EntityNotFoundError):
        k.set_default_revision("no-such-symbiote", "v1")
    assert k.get_default_revision(worker.id) is None


def test_the_default_revision_cannot_be_retired_before_the_default_moves(team) -> None:
    k, worker = team.kernel, team.worker
    team.publish(worker, "v1", "D1")
    team.publish(worker, "v2", "D2")
    k.set_default_revision(worker.id, "v1")

    with pytest.raises(ValidationError, match="default"):
        k.retire_revision(worker.id, "v1")
    assert k.get_default_revision(worker.id).label == "v1"

    k.set_default_revision(worker.id, "v2")
    assert k.retire_revision(worker.id, "v1").retired_at is not None


def test_the_default_survives_a_restart_and_goes_with_the_symbiote(tmp_path: Path) -> None:
    team = Team(tmp_path)
    team.publish(team.worker, "v1", "D1")
    team.kernel.set_default_revision(team.worker.id, "v1")
    worker_id = team.worker.id
    team.kernel.shutdown()

    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    try:
        assert kernel.get_default_revision(worker_id).label == "v1"
        kernel.purge_symbiote(worker_id)
        assert kernel.get_default_revision(worker_id) is None
    finally:
        kernel.shutdown()


# ── HTTP API ────────────────────────────────────────────────────────────


def test_api_moves_the_default_revision(api_client) -> None:
    api = api_client(ScriptedLLM("ok"))
    owner = api.auth(api.new_key("tenant-a"))
    stranger = api.auth(api.new_key("tenant-b"))
    sym_id = api.client.post(
        "/symbiotes", json={"name": "Shop", "role": "assistant"}, headers=owner
    ).json()["id"]
    api.client.post(f"/symbiotes/{sym_id}/revisions", json={"label": "v1"}, headers=owner)

    made = api.client.put(
        f"/symbiotes/{sym_id}/default-revision", json={"label": "v1"}, headers=owner
    )
    assert made.status_code == 200
    assert (made.json()["label"], made.json()["is_default"]) == ("v1", True)
    listed = api.client.get(f"/symbiotes/{sym_id}/revisions", headers=owner).json()
    assert [(r["label"], r["is_default"]) for r in listed] == [("v1", True)]
    assert api.client.post(
        f"/symbiotes/{sym_id}/revisions/v1/retire", headers=owner
    ).status_code in (400, 422)

    assert api.client.put(
        f"/symbiotes/{sym_id}/default-revision", json={"label": "v9"}, headers=owner
    ).status_code == 404
    assert api.client.put(
        f"/symbiotes/{sym_id}/default-revision", json={"label": "v1"}, headers=stranger
    ).status_code in (403, 404)
    cleared = api.client.put(
        f"/symbiotes/{sym_id}/default-revision", json={"label": None}, headers=owner
    )
    assert cleared.status_code == 200 and cleared.json() is None
