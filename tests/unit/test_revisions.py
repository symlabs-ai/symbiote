"""Sessions pinned to a named revision of the configuration (B-95).

A change to a symbiote's persona, tools or config used to apply at once to
every conversation, including the ones in the middle. ``create_revision``
takes an immutable picture of the persona, the ``EnvironmentConfig`` and the
knowledge; a session started with ``revision=label`` reads that picture,
whatever is edited afterwards. A session without a revision reads the current
configuration, as always.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from symbiote.core.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    ValidationError,
)
from symbiote.core.llm_usage import report_usage
from symbiote.core.models import MemoryEntry
from symbiote.core.revisions import bind_revision, current_revision, reset_revision
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block

V1_KNOWLEDGE = "Acme refund policy: refunds are paid within five days."
V2_KNOWLEDGE = "Acme refund policy: refunds are paid within thirty days."


class Shop:
    """A symbiote published as ``v1`` and then edited."""

    def __init__(self, tmp_path: Path, script="ok") -> None:
        self.llm = ScriptedLLM(script)
        self.kernel = make_kernel(tmp_path, self.llm)
        k = self.kernel
        self.sym = k.create_symbiote(
            name="Shop", role="support agent", persona={"tone": "FORMAL-V1"}
        )
        add_tool(k, self.sym.id, "lookup", lambda _p: "found by lookup")
        k.tool_gateway.register_tool("refund", lambda _p: "refunded")
        k.environment.configure(symbiote_id=self.sym.id, max_tool_iterations=4)
        self.k1 = k.knowledge.register_source(self.sym.id, "refund policy", content=V1_KNOWLEDGE)
        self.v1 = k.create_revision(self.sym.id, "v1", note="first publication")

    def edit(self) -> None:
        """What the owner does after publishing: everything changes."""
        k = self.kernel
        k.update_symbiote(self.sym.id, role="sales agent", persona={"tone": "CASUAL-V2"})
        k.environment.configure(
            symbiote_id=self.sym.id, tools=["refund"], max_tool_iterations=9
        )
        k.knowledge.remove(self.k1.id)
        k.knowledge.register_source(self.sym.id, "refund policy", content=V2_KNOWLEDGE)

    def prompt(self, session_id: str, text: str = "what is the Acme refund policy?") -> str:
        self.kernel.message(session_id, text)
        return self.llm.prompt_text()


@pytest.fixture()
def shop(tmp_path: Path):
    s = Shop(tmp_path)
    yield s
    s.kernel.shutdown()


# ── what a pinned session reads ─────────────────────────────────────────


def test_a_pinned_session_keeps_reading_the_revision_after_every_edit(shop) -> None:
    k = shop.kernel
    pinned = k.start_session(shop.sym.id, revision="v1")
    shop.edit()
    current = k.start_session(shop.sym.id)

    old, new = shop.prompt(pinned.id), shop.prompt(current.id)

    assert "FORMAL-V1" in old and "CASUAL-V2" not in old
    assert "within five days" in old and "thirty days" not in old
    assert "CASUAL-V2" in new and "FORMAL-V1" not in new
    assert "within thirty days" in new and "five days" not in new
    assert pinned.revision == "v1"
    assert current.revision is None


def test_the_tool_policy_of_a_pinned_session_is_the_revisions(tmp_path: Path) -> None:
    shop = Shop(tmp_path, lambda _i, m: (
        "done" if "[Tool" in str(m[-1]["content"])
        else tool_call_block("lookup") + "\n" + tool_call_block("refund")
    ))
    try:
        k = shop.kernel
        pinned = k.start_session(shop.sym.id, revision="v1")
        shop.edit()  # the current config now allows refund and not lookup
        current = k.start_session(shop.sym.id)

        on_v1 = {r.tool_id: r for r in k.turn(pinned.id, "go").tool_results}
        on_current = {r.tool_id: r for r in k.turn(current.id, "go").tool_results}

        # v1 allowed lookup only; nothing reaches a pinned session from outside
        # the revision, in either direction.
        assert on_v1["lookup"].success is True
        assert on_v1["refund"].success is False and "blocked" in on_v1["refund"].error
        assert on_current["refund"].success is True
        assert on_current["lookup"].success is False
    finally:
        shop.kernel.shutdown()


def test_config_values_come_from_the_revision(tmp_path: Path) -> None:
    """Any config field, here the ceiling on model calls of a turn."""
    shop = Shop(tmp_path, lambda i, _m: tool_call_block("lookup", n=i))
    try:
        k = shop.kernel
        k.environment.configure(symbiote_id=shop.sym.id, max_turn_llm_calls=2)
        k.create_revision(shop.sym.id, "capped")
        k.environment.configure(symbiote_id=shop.sym.id, max_turn_llm_calls=0)
        pinned = k.start_session(shop.sym.id, revision="capped")

        outcome = k.turn(pinned.id, "go")

        assert outcome.stop_reason == "budget"
        assert outcome.usage.llm_calls == 2
    finally:
        shop.kernel.shutdown()


def test_search_knowledge_of_a_pinned_session_searches_the_revision(tmp_path: Path) -> None:
    shop = Shop(tmp_path, lambda _i, m: (
        "done" if "[Tool" in str(m[-1]["content"])
        else tool_call_block("search_knowledge", query="Acme refund policy")
    ))
    try:
        k = shop.kernel
        k.environment.configure(
            symbiote_id=shop.sym.id, tools=["search_knowledge"], context_mode="on_demand"
        )
        k.create_revision(shop.sym.id, "on-demand")
        pinned = k.start_session(shop.sym.id, revision="on-demand")
        shop.edit()
        k.environment.configure(symbiote_id=shop.sym.id, context_mode="on_demand")

        (result,) = k.turn(pinned.id, "refund policy?").tool_results

        assert [item["content"] for item in result.output] == [V1_KNOWLEDGE]
        (context,) = k.inspector.list_turn_contexts(session_id=pinned.id)
        assert context["knowledge"] == [{"id": shop.k1.id, "source": "on_demand"}]
    finally:
        shop.kernel.shutdown()


def test_knowledge_kept_for_search_only_stays_so_in_the_revision(shop) -> None:
    k = shop.kernel
    k.knowledge.register_source(
        shop.sym.id, "refund exceptions", content="Acme refund exceptions: opened software.",
        auto_recall=False,
    )
    k.create_revision(shop.sym.id, "v1b")

    prompt = shop.prompt(k.start_session(shop.sym.id, revision="v1b").id, "Acme refund exceptions?")

    assert "opened software" not in prompt
    assert k.get_revision(shop.sym.id, "v1b").knowledge_count == 2


def test_memories_are_not_part_of_a_revision(shop) -> None:
    """What the symbiote learns after the revision reaches the pinned sessions."""
    k = shop.kernel
    pinned = k.start_session(shop.sym.id, revision="v1")
    k.memory.store(MemoryEntry(
        symbiote_id=shop.sym.id, scope="global", source="user", type="factual",
        importance=0.9, content="Acme refund desk closes at six",
    ))

    assert "closes at six" in shop.prompt(pinned.id, "when does the Acme refund desk close?")


def test_a_symbiote_with_no_config_at_the_time_stays_so_for_the_revision(tmp_path: Path) -> None:
    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm)
    try:
        sym = kernel.create_symbiote(name="Bare", role="assistant")
        kernel.create_revision(sym.id, "bare")
        kernel.environment.configure(symbiote_id=sym.id, context_mode="on_demand")
        pinned = kernel.start_session(sym.id, revision="bare")

        kernel.message(pinned.id, "hello")

        assert kernel.get_revision(sym.id, "bare").config is None
        assert "## Context Access" not in llm.prompt_text()  # on_demand came later
        kernel.message(kernel.start_session(sym.id).id, "hello")
        assert "## Context Access" in llm.prompt_text()
    finally:
        kernel.shutdown()


def test_async_turns_read_the_revision_too(shop) -> None:
    k = shop.kernel
    pinned = k.start_session(shop.sym.id, revision="v1")
    shop.edit()

    asyncio.run(k.message_async(pinned.id, "what is the Acme refund policy?"))

    assert "FORMAL-V1" in shop.llm.prompt_text()
    assert current_revision() is None  # nothing stays bound after the turn


# ── the revision itself ─────────────────────────────────────────────────


def test_a_revision_is_a_picture_of_what_was_configured(shop) -> None:
    rev = shop.kernel.get_revision(shop.sym.id, "v1")

    assert (rev.label, rev.note) == ("v1", "first publication")
    assert (rev.name, rev.role, rev.persona) == ("Shop", "support agent", {"tone": "FORMAL-V1"})
    assert rev.config["tools"] == ["lookup"]
    assert rev.config["max_tool_iterations"] == 4
    assert rev.knowledge_count == 1
    assert rev.retired_at is None


def test_a_revision_never_changes_and_its_label_is_never_reused(shop) -> None:
    k = shop.kernel
    before = k.get_revision(shop.sym.id, "v1")
    shop.edit()

    with pytest.raises(DuplicateEntityError):
        k.create_revision(shop.sym.id, "v1")

    assert k.get_revision(shop.sym.id, "v1") == before
    assert [r.label for r in k.list_revisions(shop.sym.id)] == ["v1"]


@pytest.mark.parametrize("bad", ["", "   ", "x" * 101, None])
def test_a_label_must_be_a_short_text(shop, bad) -> None:
    with pytest.raises(ValidationError):
        shop.kernel.create_revision(shop.sym.id, bad)


def test_unknown_symbiote_and_unknown_label(shop) -> None:
    k = shop.kernel

    with pytest.raises(EntityNotFoundError):
        k.create_revision("no-such-symbiote", "v1")
    with pytest.raises(EntityNotFoundError):
        k.start_session(shop.sym.id, revision="v9")
    assert k.get_revision(shop.sym.id, "v9") is None
    # A label belongs to one symbiote.
    other = k.create_symbiote(name="Other", role="assistant")
    with pytest.raises(EntityNotFoundError):
        k.start_session(other.id, revision="v1")


def test_a_retired_revision_takes_no_new_session_and_keeps_the_old_ones(shop) -> None:
    k = shop.kernel
    pinned = k.start_session(shop.sym.id, revision="v1")
    shop.edit()

    retired = k.retire_revision(shop.sym.id, "v1")

    assert retired.retired_at is not None
    with pytest.raises(ValidationError, match="retired"):
        k.start_session(shop.sym.id, revision="v1")
    assert "FORMAL-V1" in shop.prompt(pinned.id)
    with pytest.raises(EntityNotFoundError):
        k.retire_revision(shop.sym.id, "v9")


# ── sessions and revisions ──────────────────────────────────────────────


def test_an_existing_session_keeps_its_revision_when_found_again(shop) -> None:
    k = shop.kernel
    first = k.get_or_create_session(shop.sym.id, "chat-1", revision="v1")
    shop.edit()
    k.create_revision(shop.sym.id, "v2")

    again = k.get_or_create_session(shop.sym.id, "chat-1", revision="v2")
    fresh = k.get_or_create_session(shop.sym.id, "chat-2", revision="v2")

    assert (first.id, again.revision) == (again.id, "v1")
    assert fresh.revision == "v2"
    assert "FORMAL-V1" in shop.prompt(again.id)
    assert "CASUAL-V2" in shop.prompt(fresh.id)


def test_a_session_can_be_moved_to_another_revision_or_to_none(shop) -> None:
    k = shop.kernel
    session = k.start_session(shop.sym.id, revision="v1")
    shop.edit()
    k.create_revision(shop.sym.id, "v2")
    k.update_symbiote(shop.sym.id, persona={"tone": "DRAFT-V3"})

    assert "FORMAL-V1" in shop.prompt(session.id)
    assert k.set_session_revision(session.id, "v2").revision == "v2"
    assert "CASUAL-V2" in shop.prompt(session.id)
    assert k.set_session_revision(session.id, None).revision is None
    assert "DRAFT-V3" in shop.prompt(session.id)

    with pytest.raises(EntityNotFoundError):
        k.set_session_revision(session.id, "v9")
    with pytest.raises(EntityNotFoundError):
        k.set_session_revision("no-such-session", "v1")


def test_what_happens_after_the_close_follows_the_current_configuration(shop) -> None:
    """A revision governs the turns; the summary, reflection and dream of a
    closed session are the symbiote's own business."""
    k = shop.kernel
    pinned = k.start_session(shop.sym.id, revision="v1")  # v1: default summary
    k.message(pinned.id, "what is the Acme refund policy?")
    k.environment.configure(symbiote_id=shop.sym.id, session_summary_mode="off")

    assert k.close_session(pinned.id).summary is None


def test_editing_the_config_during_a_pinned_turn_edits_the_current_one(shop) -> None:
    k = shop.kernel
    token = bind_revision(k.get_revision(shop.sym.id, "v1"))
    try:
        assert k.environment.list_tools(shop.sym.id) == ["lookup"]
        k.environment.add_tools(shop.sym.id, ["refund"])
        assert k.environment.list_tools(shop.sym.id) == ["lookup"]  # the revision still
    finally:
        reset_revision(token)

    assert k.environment.list_tools(shop.sym.id) == ["lookup", "refund"]
    assert k.get_revision(shop.sym.id, "v1").config["tools"] == ["lookup"]


# ── delegation ──────────────────────────────────────────────────────────


def _team(tmp_path: Path):
    def script(_i, messages):
        last = str(messages[-1]["content"])
        if "[Tool result: spawn]" in last:
            return "done"
        if "SUBTASK" in last:
            return "worker answer"
        return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK")

    llm = ScriptedLLM(script)
    kernel = make_kernel(tmp_path, llm)
    boss = kernel.create_symbiote(name="boss", role="front desk", persona={"tone": "BOSS-V1"})
    worker = kernel.create_symbiote(name="worker", role="orders", persona={"tone": "WORKER-V1"})
    kernel.environment.configure(symbiote_id=boss.id, tools=["spawn"])
    return kernel, llm, boss, worker


def _worker_prompt(llm) -> str:
    (index,) = [
        i for i, call in enumerate(llm.calls)
        if "SUBTASK" in str(call["messages"][-1].get("content"))
        and "[Tool" not in str(call["messages"][-1].get("content"))
    ]
    return llm.prompt_text(index)


def test_a_pinned_session_delegates_to_the_same_label_of_the_other_symbiote(
    tmp_path: Path,
) -> None:
    kernel, llm, boss, worker = _team(tmp_path)
    try:
        kernel.create_revision(boss.id, "release-1")
        kernel.create_revision(worker.id, "release-1")
        kernel.update_symbiote(worker.id, persona={"tone": "WORKER-DRAFT"})
        session = kernel.start_session(boss.id, revision="release-1")

        kernel.message(session.id, "go")

        assert "WORKER-V1" in _worker_prompt(llm)
        (sub,) = kernel.inspector.list_sessions(worker.id)
        assert sub["revision"] == "release-1"
    finally:
        kernel.shutdown()


def test_without_a_matching_label_the_delegate_reads_its_current_configuration(
    tmp_path: Path,
) -> None:
    kernel, llm, boss, worker = _team(tmp_path)
    try:
        kernel.create_revision(boss.id, "release-1")
        kernel.update_symbiote(boss.id, persona={"tone": "BOSS-DRAFT"})
        kernel.update_symbiote(worker.id, persona={"tone": "WORKER-DRAFT"})
        session = kernel.start_session(boss.id, revision="release-1")

        kernel.message(session.id, "go")

        # The boss's revision governs the boss alone.
        assert "WORKER-DRAFT" in _worker_prompt(llm)
        assert "BOSS-V1" in llm.prompt_text(0)
        (sub,) = kernel.inspector.list_sessions(worker.id)
        assert sub["revision"] is None
    finally:
        kernel.shutdown()


# ── on record ───────────────────────────────────────────────────────────


def test_the_revision_is_on_the_session_the_turn_and_the_usage(tmp_path: Path) -> None:
    class Metered(ScriptedLLM):
        def complete(self, messages, config=None, tools=None):
            report_usage(80, 20)
            return super().complete(messages, config, tools)

    shop = Shop(tmp_path)
    shop.kernel.shutdown()
    kernel = make_kernel(tmp_path, Metered("ok"))
    try:
        sid = shop.sym.id
        pinned = kernel.start_session(sid, revision="v1")
        current = kernel.start_session(sid)
        kernel.message(pinned.id, "one")
        kernel.message(pinned.id, "two")
        kernel.message(current.id, "three")
        ins = kernel.inspector

        assert [s["id"] for s in ins.list_sessions(sid, revision="v1")] == [pinned.id]
        assert {s["id"]: s["revision"] for s in ins.list_sessions(sid)} == {
            pinned.id: "v1", current.id: None,
        }
        assert {c["revision"] for c in ins.list_turn_contexts(session_id=pinned.id)} == {"v1"}
        assert {c["revision"] for c in ins.list_turn_contexts(session_id=current.id)} == {None}
        assert {c["revision"] for c in ins.list_llm_calls(session_id=pinned.id)} == {"v1"}
        by_revision = {r["revision"]: r for r in ins.usage_by("revision", sid)}
        assert (by_revision["v1"]["llm_calls"], by_revision["v1"]["total_tokens"]) == (2, 200)
        assert by_revision[None]["llm_calls"] == 1
        (listed,) = ins.list_revisions(sid)
        assert (listed["label"], listed["sessions"], listed["active_sessions"]) == ("v1", 1, 1)
        assert listed["knowledge_count"] == 1
    finally:
        kernel.shutdown()


def test_revisions_survive_a_restart_and_go_with_the_symbiote(tmp_path: Path) -> None:
    shop = Shop(tmp_path)
    pinned = shop.kernel.start_session(shop.sym.id, revision="v1")
    shop.edit()
    shop.kernel.shutdown()

    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm)
    try:
        kernel.message(pinned.id, "what is the Acme refund policy?")
        assert "FORMAL-V1" in llm.prompt_text() and "within five days" in llm.prompt_text()

        kernel.purge_symbiote(shop.sym.id)
        for table in ("symbiote_revisions", "revision_knowledge"):
            assert kernel._storage.fetch_all(f"SELECT id FROM {table}") == []  # noqa: S608
        assert kernel.get_revision(shop.sym.id, "v1") is None
    finally:
        kernel.shutdown()


@pytest.mark.sqlite_only
def test_a_database_from_before_revisions_opens_and_behaves_as_before(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    sym = kernel.create_symbiote(name="Old", role="assistant", persona={"tone": "OLD"})
    session_id = kernel.start_session(sym.id).id
    kernel.shutdown()
    conn = sqlite3.connect(db)
    conn.executescript(
        "DROP INDEX idx_revisions_symbiote_label; DROP INDEX idx_revision_knowledge_revision;"
        "DROP TABLE symbiote_revisions; DROP TABLE revision_knowledge;"
        "ALTER TABLE sessions DROP COLUMN revision;"
        "ALTER TABLE turn_contexts DROP COLUMN revision;"
        "ALTER TABLE llm_usage DROP COLUMN revision;"
        "PRAGMA user_version = 16;"  # the migration before revisions
    )
    conn.close()

    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm, db_path=db)
    try:
        kernel.message(session_id, "hello")
        assert "OLD" in llm.prompt_text()
        assert kernel.get_session(session_id).revision is None
        assert kernel.create_revision(sym.id, "v1").label == "v1"
    finally:
        kernel.shutdown()


# ── HTTP API ────────────────────────────────────────────────────────────


def test_api_creates_lists_and_pins_revisions(api_client) -> None:
    llm = ScriptedLLM("ok")
    api = api_client(llm)
    owner, stranger = api.auth(api.new_key("tenant-a")), api.auth(api.new_key("tenant-b"))
    sym_id = api.client.post(
        "/symbiotes", json={"name": "Shop", "role": "assistant"}, headers=owner
    ).json()["id"]
    api.kernel.update_symbiote(sym_id, persona={"tone": "FORMAL-V1"})

    created = api.client.post(
        f"/symbiotes/{sym_id}/revisions", json={"label": "v1", "note": "first"}, headers=owner
    )
    assert created.status_code == 201
    assert (created.json()["label"], created.json()["persona"]) == ("v1", {"tone": "FORMAL-V1"})
    assert api.client.post(
        f"/symbiotes/{sym_id}/revisions", json={"label": "v1"}, headers=owner
    ).status_code == 409
    assert api.client.post(
        f"/symbiotes/{sym_id}/revisions", json={"label": "x"}, headers=stranger
    ).status_code in (403, 404)

    api.kernel.update_symbiote(sym_id, persona={"tone": "CASUAL-V2"})
    session = api.client.post(
        "/sessions", json={"symbiote_id": sym_id, "revision": "v1"}, headers=owner
    ).json()
    assert session["revision"] == "v1"
    assert api.client.post(
        "/sessions", json={"symbiote_id": sym_id, "revision": "v9"}, headers=owner
    ).status_code == 404
    api.client.post(f"/sessions/{session['id']}/chat", json={"content": "hello"}, headers=owner)
    assert "FORMAL-V1" in llm.prompt_text()

    listed = api.client.get(f"/symbiotes/{sym_id}/revisions", headers=owner).json()
    assert [(r["label"], r["sessions"]) for r in listed] == [("v1", 1)]
    assert api.client.get(
        f"/symbiotes/{sym_id}/revisions/v1", headers=owner
    ).json()["note"] == "first"
    assert api.client.get(f"/symbiotes/{sym_id}/revisions/v9", headers=owner).status_code == 404

    moved = api.client.put(
        f"/sessions/{session['id']}/revision", json={"revision": None}, headers=owner
    )
    assert moved.json()["revision"] is None
    retired = api.client.post(f"/symbiotes/{sym_id}/revisions/v1/retire", headers=owner)
    assert retired.json()["retired_at"] is not None
    assert api.client.post(
        "/sessions", json={"symbiote_id": sym_id, "revision": "v1"}, headers=owner
    ).status_code in (400, 409, 422)
