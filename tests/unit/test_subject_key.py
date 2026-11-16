"""Memory per end user (``subject_key``, B-79).

A symbiote that talks to several people keeps one memory partition per
person. A session with ``subject_key = S`` reads the shared memories plus the
ones of ``S`` and writes only to the partition of ``S``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SCHEMA_VERSION, SQLiteAdapter
from symbiote.core.background_review import BackgroundReviewEngine
from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.core.models import MemoryEntry
from symbiote.core.subjects import MAX_SUBJECT_KEY_CHARS
from symbiote.harness.evolver import HarnessEvolver
from symbiote.harness.tuner import ParameterTuner
from symbiote.harness.versions import HarnessVersionRepository
from symbiote.memory import store as store_mod
from tests.support import ScriptedLLM, make_kernel, tool_call_block

SHARED = "Acme office opens at nine"
OF_ANA = "Acme order of Ana is number 4471"
OF_BIA = "Acme order of Bia is number 9902"


def _memory(symbiote_id: str, content: str, **kw) -> MemoryEntry:
    kw.setdefault("type", "factual")
    kw.setdefault("importance", 0.9)
    return MemoryEntry(
        symbiote_id=symbiote_id, scope="global", source="user", content=content, **kw
    )


@pytest.fixture()
def shop(tmp_path: Path):
    """One symbiote with a shared memory and one memory for each of two
    end users, ``ana`` and ``bia``."""
    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="Shop", role="assistant")
    kernel._memory.store(_memory(sym.id, SHARED))
    kernel._memory.store(_memory(sym.id, OF_ANA, subject_key="ana"))
    kernel._memory.store(_memory(sym.id, OF_BIA, subject_key="bia"))
    yield kernel, llm, sym
    kernel.shutdown()


def _contents(entries) -> set[str]:
    return {e.content for e in entries}


# ── the key on the session ──────────────────────────────────────────────


def test_session_keeps_its_subject_key(shop) -> None:
    kernel, _llm, sym = shop

    session = kernel.start_session(sym.id, subject_key="ana")

    assert session.subject_key == "ana"
    assert kernel.get_session(session.id).subject_key == "ana"
    assert kernel.start_session(sym.id).subject_key is None


@pytest.mark.parametrize("bad", ["", "   ", "x" * (MAX_SUBJECT_KEY_CHARS + 1)])
def test_empty_or_too_long_subject_key_is_refused(shop, bad) -> None:
    kernel, _llm, sym = shop

    with pytest.raises(ValidationError):
        kernel.start_session(sym.id, subject_key=bad)


def test_subject_of_an_existing_session_cannot_change(shop) -> None:
    kernel, _llm, sym = shop
    first = kernel.get_or_create_session(sym.id, "chat-1", subject_key="ana")

    again = kernel.get_or_create_session(sym.id, "chat-1", subject_key="ana")
    unspecified = kernel.get_or_create_session(sym.id, "chat-1")

    assert again.id == first.id == unspecified.id
    assert unspecified.subject_key == "ana"
    with pytest.raises(ValidationError):
        kernel.get_or_create_session(sym.id, "chat-1", subject_key="bia")


def test_session_without_subject_cannot_be_claimed_later(shop) -> None:
    kernel, _llm, sym = shop
    kernel.get_or_create_session(sym.id, "chat-2")

    with pytest.raises(ValidationError):
        kernel.get_or_create_session(sym.id, "chat-2", subject_key="ana")


# ── read rule ───────────────────────────────────────────────────────────


def test_each_reader_gets_the_shared_memories_plus_its_own(shop) -> None:
    kernel, _llm, sym = shop
    memory = kernel._memory

    def read(**subject):
        return {
            "search": _contents(memory.search("Acme", symbiote_id=sym.id, **subject)),
            "relevant": _contents(memory.get_relevant("Acme", symbiote_id=sym.id, **subject)),
            "by_type": _contents(memory.get_by_type(sym.id, "factual", **subject)),
        }

    for view in read(subject_key="ana").values():
        assert view == {SHARED, OF_ANA}
    for view in read(subject_key="bia").values():
        assert view == {SHARED, OF_BIA}
    for view in read().values():
        assert view == {SHARED}


def test_own_partition_alone_with_include_shared_off(shop) -> None:
    kernel, _llm, sym = shop

    own = kernel._memory.get_by_type(
        sym.id, "factual", subject_key="ana", include_shared=False
    )

    assert _contents(own) == {OF_ANA}


def test_standing_memories_follow_the_partition(shop) -> None:
    kernel, _llm, sym = shop
    memory = kernel._memory
    memory.store(_memory(sym.id, "Everyone: answer in short topics", type="preference"))
    memory.store(
        _memory(sym.id, "Ana: never call before ten", type="constraint", subject_key="ana")
    )

    for_ana = memory.get_standing(sym.id, ("preference", "constraint"), subject_key="ana")
    for_bia = memory.get_standing(sym.id, ("preference", "constraint"), subject_key="bia")

    assert _contents(for_ana) == {
        "Everyone: answer in short topics", "Ana: never call before ten",
    }
    assert _contents(for_bia) == {"Everyone: answer in short topics"}


def test_prompt_of_one_end_user_never_carries_anothers_memory(shop) -> None:
    kernel, llm, sym = shop
    session = kernel.start_session(sym.id, subject_key="bia")

    kernel.message(session.id, "what about my Acme order?")

    prompt = llm.all_prompts_text()
    assert "9902" in prompt  # her own
    assert "opens at nine" in prompt  # the shared one
    assert "4471" not in prompt  # Ana's


def test_session_without_subject_reads_only_the_shared_memories(shop) -> None:
    kernel, llm, sym = shop
    session = kernel.start_session(sym.id)

    kernel.message(session.id, "what about the Acme order?")

    prompt = llm.all_prompts_text()
    assert "opens at nine" in prompt
    assert "4471" not in prompt
    assert "9902" not in prompt


def test_async_turn_follows_the_same_partition(shop) -> None:
    kernel, llm, sym = shop
    session = kernel.start_session(sym.id, subject_key="ana")

    asyncio.run(kernel.message_async(session.id, "what about my Acme order?"))

    prompt = llm.all_prompts_text()
    assert "4471" in prompt
    assert "9902" not in prompt


def test_other_partitions_do_not_crowd_out_the_recall(shop, monkeypatch) -> None:
    """The partition is applied before the candidate limit: thousands of
    visitors' memories must not push the shared ones out of the recall."""
    kernel, _llm, sym = shop
    monkeypatch.setattr(store_mod, "_RECALL_CANDIDATES", 3)
    for i in range(20):
        kernel._memory.store(
            _memory(sym.id, f"Acme note {i} of a visitor", importance=1.0, subject_key=f"v{i}")
        )

    recalled = kernel._memory.get_relevant("Acme office", symbiote_id=sym.id, subject_key="ana")

    assert SHARED in _contents(recalled)
    assert all(e.subject_key in (None, "ana") for e in recalled)


def test_search_memories_tool_uses_the_calling_sessions_partition(shop) -> None:
    kernel, _llm, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, tools=["search_memories"])
    session = kernel.start_session(sym.id, subject_key="ana")

    result = kernel.tool_gateway.execute(
        sym.id, session.id, "search_memories", {"query": "Acme", "limit": 10}
    )

    assert result.success
    found = {m["content"] for m in result.output}
    assert found == {SHARED, OF_ANA}


def test_search_memories_ignores_a_subject_sent_by_the_model(shop) -> None:
    kernel, _llm, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, tools=["search_memories"])
    session = kernel.start_session(sym.id, subject_key="ana")

    result = kernel.tool_gateway.execute(
        sym.id, session.id, "search_memories",
        {"query": "Acme", "limit": 10, "subject_key": "bia"},
    )

    assert OF_BIA not in {m["content"] for m in result.output}


# ── write rule ──────────────────────────────────────────────────────────


def test_what_an_end_user_teaches_belongs_to_that_end_user(shop) -> None:
    kernel, _llm, sym = shop
    session = kernel.start_session(sym.id, subject_key="ana")

    entry = kernel.capabilities.learn(sym.id, session.id, "Ana is allergic to peanuts")

    assert entry.subject_key == "ana"
    assert kernel._memory.get(entry.id).subject_key == "ana"
    assert kernel._memory.search("peanuts", symbiote_id=sym.id, subject_key="bia") == []
    assert kernel._memory.search("peanuts", symbiote_id=sym.id) == []


def test_what_the_owner_teaches_is_shared(shop) -> None:
    kernel, _llm, sym = shop
    session = kernel.start_session(sym.id)

    entry = kernel.capabilities.learn(sym.id, session.id, "Returns are accepted for 30 days")

    assert entry.subject_key is None
    assert kernel._memory.search("Returns", symbiote_id=sym.id, subject_key="bia")


def test_reflection_of_an_end_users_session_stays_in_the_partition(shop) -> None:
    kernel, _llm, sym = shop
    session = kernel.start_session(sym.id, subject_key="ana")
    kernel.message(session.id, "I prefer to be contacted by email, never by phone")

    kernel.close_session(session.id)

    learned = [
        m for m in kernel.inspector.list_memories(sym.id)
        if "contacted by email" in m["content"]
    ]
    assert learned
    assert {m["subject_key"] for m in learned} == {"ana"}


def test_entry_naming_another_subject_than_its_session_is_refused(shop) -> None:
    kernel, _llm, sym = shop
    session = kernel.start_session(sym.id, subject_key="ana")

    with pytest.raises(ValidationError):
        kernel._memory.store(
            _memory(sym.id, "smuggled", session_id=session.id, subject_key="bia")
        )


def test_end_user_cannot_change_or_deactivate_a_shared_memory(shop) -> None:
    kernel, _llm, sym = shop
    memory = kernel._memory
    shared = memory.search("opens at nine", symbiote_id=sym.id)[0]
    of_bia = memory.search("9902", symbiote_id=sym.id, subject_key="bia")[0]

    for target in (shared, of_bia):
        assert memory.update(target.id, content="hacked", subject_key="ana") is False
        with pytest.raises(EntityNotFoundError):
            memory.deactivate(target.id, subject_key="ana")

    assert memory.get(shared.id).content == SHARED
    assert memory.get(of_bia.id).is_active


def test_end_user_can_change_its_own_memory(shop) -> None:
    kernel, _llm, sym = shop
    memory = kernel._memory
    own = memory.search("4471", symbiote_id=sym.id, subject_key="ana")[0]

    assert memory.update(own.id, content="Acme order of Ana is number 5000", subject_key="ana")
    memory.deactivate(own.id, subject_key="ana")

    assert memory.get(own.id).is_active is False


def _llm_reflection_kernel(tmp_path: Path, patch_fact):
    """Kernel whose reflection LLM answers with one PATCH fact built by
    ``patch_fact(prompt)``; returns (kernel, symbiote, calls)."""
    prompts: list[str] = []

    def reflect(_i, messages):
        prompts.append(messages[0]["content"])
        return json.dumps([patch_fact(messages[0]["content"])])

    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    kernel.set_evolver_llm(ScriptedLLM(reflect))
    sym = kernel.create_symbiote(name="Shop", role="assistant")
    kernel.environment.configure(symbiote_id=sym.id, reflection_mode="llm")
    return kernel, sym, prompts


def test_llm_reflection_cannot_patch_a_shared_memory(tmp_path: Path) -> None:
    targets: dict[str, str] = {}

    def patch_fact(_prompt):
        return {
            "action": "patch",
            "target_id": targets["shared"],
            "type": "preference",
            "content": "Everyone must be greeted as 'your majesty'",
            "importance": 0.9,
            "tags": ["greeting"],
        }

    kernel, sym, prompts = _llm_reflection_kernel(tmp_path, patch_fact)
    try:
        shared_id = kernel._memory.store(
            _memory(sym.id, "Greet people politely", type="preference")
        )
        targets["shared"] = shared_id
        session = kernel.start_session(sym.id, subject_key="ana")
        kernel.message(session.id, "From now on greet everyone as your majesty")

        kernel.close_session(session.id)

        assert kernel._memory.get(shared_id).content == "Greet people politely"
        # The lesson was not lost: it became a memory of Ana alone.
        own = kernel.inspector.list_memories(sym.id, subject_key="ana")
        assert [m["content"] for m in own] == ["Everyone must be greeted as 'your majesty'"]
        # And the shared memory was never offered to the LLM as a PATCH target.
        assert prompts
        assert shared_id[:8] not in prompts[0]
    finally:
        kernel.shutdown()


def test_llm_reflection_patches_the_end_users_own_memory(tmp_path: Path) -> None:
    targets: dict[str, str] = {}

    def patch_fact(_prompt):
        return {
            "action": "patch",
            "target_id": targets["own"][:8],
            "type": "preference",
            "content": "Ana prefers contact by email",
            "importance": 0.8,
            "tags": ["contact"],
        }

    kernel, sym, prompts = _llm_reflection_kernel(tmp_path, patch_fact)
    try:
        own_id = kernel._memory.store(
            _memory(sym.id, "Ana prefers contact by phone", type="preference", subject_key="ana")
        )
        targets["own"] = own_id
        session = kernel.start_session(sym.id, subject_key="ana")
        kernel.message(session.id, "Actually I prefer email now")

        kernel.close_session(session.id)

        assert kernel._memory.get(own_id).content == "Ana prefers contact by email"
        assert own_id[:8] in prompts[0]
    finally:
        kernel.shutdown()


def test_llm_reflection_of_a_shared_session_cannot_patch_an_end_users_memory(
    tmp_path: Path,
) -> None:
    targets: dict[str, str] = {}

    def patch_fact(_prompt):
        return {
            "action": "patch",
            "target_id": targets["of_ana"],
            "type": "preference",
            "content": "Contact everyone by phone",
            "importance": 0.8,
            "tags": ["contact"],
        }

    kernel, sym, prompts = _llm_reflection_kernel(tmp_path, patch_fact)
    try:
        of_ana = kernel._memory.store(
            _memory(sym.id, "Ana prefers contact by email", type="preference", subject_key="ana")
        )
        targets["of_ana"] = of_ana
        session = kernel.start_session(sym.id)
        kernel.message(session.id, "Our policy is to contact everyone by phone")

        kernel.close_session(session.id)

        assert kernel._memory.get(of_ana).content == "Ana prefers contact by email"
        shared = kernel.inspector.list_memories(sym.id, subject_key=None)
        assert [m["content"] for m in shared] == ["Contact everyone by phone"]
        assert of_ana[:8] not in prompts[0]
    finally:
        kernel.shutdown()


def test_handoff_is_resumed_only_by_the_same_end_user(shop) -> None:
    kernel, _llm, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, tool_mode="long_run")
    handoff = {"pending_blocks": ["step 2"], "project": "Ana's move"}
    kernel._memory.store(
        _memory(
            sym.id, json.dumps(handoff), type="session_summary", category="handoff",
            subject_key="ana",
        )
    )
    of_ana = kernel.start_session(sym.id, subject_key="ana")
    of_bia = kernel.start_session(sym.id, subject_key="bia")
    shared = kernel.start_session(sym.id)

    assert kernel._inject_handoff_if_resuming(of_ana.id, sym.id, None) == {
        "previous_handoff": handoff
    }
    assert kernel._inject_handoff_if_resuming(of_bia.id, sym.id, None) is None
    assert kernel._inject_handoff_if_resuming(shared.id, sym.id, None) is None


# ── delegation ──────────────────────────────────────────────────────────


def test_spawned_symbiote_works_for_the_same_end_user(tmp_path: Path) -> None:
    def script(_i, messages):
        last = messages[-1]["content"]
        if "[Tool result: spawn]" in last or "[Tool error: spawn]" in last:
            return "done"
        if "SUBTASK" in last:
            return "worker answer"
        return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK order status")

    llm = ScriptedLLM(script)
    kernel = make_kernel(tmp_path, llm)
    try:
        boss = kernel.create_symbiote(name="boss", role="front desk")
        worker = kernel.create_symbiote(name="worker", role="orders")
        kernel.environment.configure(symbiote_id=boss.id, tools=["spawn"])
        kernel._memory.store(_memory(worker.id, "SUBTASK note: Ana ordered a lamp", subject_key="ana"))
        kernel._memory.store(_memory(worker.id, "SUBTASK note: Bia ordered a chair", subject_key="bia"))
        session = kernel.start_session(boss.id, subject_key="ana")

        kernel.message(session.id, "where is my order?")

        delegated = kernel.inspector.list_sessions(worker.id)
        assert [s["subject_key"] for s in delegated] == ["ana"]
        worker_prompts = "\n".join(
            llm.prompt_text(i)
            for i, call in enumerate(llm.calls)
            if "SUBTASK order status" in str(call["messages"][-1].get("content"))
        )
        assert "ordered a lamp" in worker_prompts
        assert "ordered a chair" not in worker_prompts
    finally:
        kernel.shutdown()


# ── dream ───────────────────────────────────────────────────────────────


def _duplicate(symbiote_id: str, importance: float, **kw) -> MemoryEntry:
    return _memory(
        symbiote_id,
        "Prefers birthday reminders one week in advance",
        type="preference",
        tags=["birthdays", "reminders"],
        importance=importance,
        **kw,
    )


def test_dream_does_not_reconcile_across_partitions(shop) -> None:
    kernel, _llm, sym = shop
    memory = kernel._memory
    shared_id = memory.store(_duplicate(sym.id, 0.6))
    ana_id = memory.store(_duplicate(sym.id, 0.4, subject_key="ana"))
    bia_id = memory.store(_duplicate(sym.id, 0.5, subject_key="bia"))

    kernel.dream(sym.id)

    for memory_id in (shared_id, ana_id, bia_id):
        assert memory.get(memory_id).is_active


def test_dream_still_reconciles_inside_the_shared_partition(shop) -> None:
    kernel, _llm, sym = shop
    memory = kernel._memory
    winner = memory.store(_duplicate(sym.id, 0.6))
    loser = memory.store(_duplicate(sym.id, 0.4))

    kernel.dream(sym.id)

    assert memory.get(winner).is_active
    assert memory.get(loser).is_active is False


# ── learning from end users' sessions ───────────────────────────────────


def _closed_session(kernel, symbiote_id: str, **kw) -> str:
    session = kernel.start_session(symbiote_id, **kw)
    kernel.message(session.id, "hello there, how are you")
    kernel.close_session(session.id)
    return session.id


def test_end_user_sessions_are_scored_but_left_out_of_the_harness(shop) -> None:
    kernel, _llm, sym = shop
    owner = _closed_session(kernel, sym.id)
    visitor = _closed_session(kernel, sym.id, subject_key="ana")
    tuner = ParameterTuner(kernel._storage)
    evolver = HarnessEvolver(kernel._storage, HarnessVersionRepository(kernel._storage))

    scored = {s["session_id"] for s in kernel.inspector.list_scores(sym.id)}
    tuned = {s["session_id"] for s in tuner._get_recent_scores(sym.id, 7)}
    evolved = {s["session_id"] for s in evolver._get_successful_sessions(sym.id, 7)}

    assert scored == {owner, visitor}
    assert tuned == {owner}
    assert evolved == {owner}
    assert kernel._session_feeds_learning(visitor, sym.id) is False


def test_end_user_sessions_count_when_the_flag_is_on(shop) -> None:
    kernel, _llm, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, learn_from_subject_sessions=True)
    owner = _closed_session(kernel, sym.id)
    visitor = _closed_session(kernel, sym.id, subject_key="ana")
    tuner = ParameterTuner(kernel._storage)
    evolver = HarnessEvolver(kernel._storage, HarnessVersionRepository(kernel._storage))

    assert {s["session_id"] for s in tuner._get_recent_scores(sym.id, 7)} == {owner, visitor}
    assert {
        s["session_id"] for s in evolver._get_successful_sessions(sym.id, 7)
    } == {owner, visitor}
    assert kernel._session_feeds_learning(visitor, sym.id) is True


def test_flag_is_off_by_default_and_persists(shop) -> None:
    kernel, _llm, sym = shop

    kernel.environment.configure(symbiote_id=sym.id, tools=[])
    assert kernel.environment.get_config(sym.id).learn_from_subject_sessions is False

    kernel.environment.configure(symbiote_id=sym.id, learn_from_subject_sessions=True)
    assert kernel.environment.get_config(sym.id).learn_from_subject_sessions is True


def _review_engine(kernel, reviewer: ScriptedLLM) -> BackgroundReviewEngine:
    return BackgroundReviewEngine(
        llm=reviewer,
        messages=kernel._message_repo,
        store=kernel._skills_store,
        loader=kernel._skills_loader,
        storage=kernel._storage,
        environment=kernel._environment,
    )


def test_skill_review_skips_end_user_sessions_unless_the_flag_is_on(shop) -> None:
    kernel, _llm, sym = shop
    reviewer = ScriptedLLM("[]")
    engine = _review_engine(kernel, reviewer)
    visitor = kernel.start_session(sym.id, subject_key="ana")
    kernel.message(visitor.id, "please summarize the month for me")
    owner = kernel.start_session(sym.id)
    kernel.message(owner.id, "please summarize the month for me")

    skipped = engine.run_sync(visitor.id, sym.id)
    assert skipped.get("excluded") is True
    assert reviewer.calls == []

    engine.run_sync(owner.id, sym.id)
    assert len(reviewer.calls) == 1

    kernel.environment.configure(symbiote_id=sym.id, learn_from_subject_sessions=True)
    counted = engine.run_sync(visitor.id, sym.id)
    assert "excluded" not in counted
    assert len(reviewer.calls) == 2


# ── inspection and erasure ──────────────────────────────────────────────


def test_inspector_lists_memories_by_partition(shop) -> None:
    kernel, _llm, sym = shop
    inspector = kernel.inspector

    assert {m["content"] for m in inspector.list_memories(sym.id)} == {SHARED, OF_ANA, OF_BIA}
    assert [m["content"] for m in inspector.list_memories(sym.id, subject_key="ana")] == [OF_ANA]
    assert [m["content"] for m in inspector.list_memories(sym.id, subject_key=None)] == [SHARED]
    assert {m["subject_key"] for m in inspector.list_memories(sym.id)} == {None, "ana", "bia"}


def test_inspector_lists_the_end_users_of_a_symbiote(shop) -> None:
    kernel, _llm, sym = shop
    kernel.start_session(sym.id, subject_key="ana")
    kernel.start_session(sym.id, subject_key="ana")
    kernel.start_session(sym.id)

    subjects = {s["subject_key"]: s for s in kernel.inspector.list_subjects(sym.id)}

    assert set(subjects) == {"ana", "bia"}
    assert (subjects["ana"]["sessions"], subjects["ana"]["memories"]) == (2, 1)
    assert (subjects["bia"]["sessions"], subjects["bia"]["memories"]) == (0, 1)
    assert subjects["bia"]["last_session_at"] is None
    sessions = kernel.inspector.list_sessions(sym.id, subject_key="ana")
    assert len(sessions) == 2
    assert len(kernel.inspector.list_sessions(sym.id, subject_key=None)) == 1


def test_purge_subject_erases_one_end_user_and_nobody_else(shop) -> None:
    kernel, _llm, sym = shop
    of_ana = _closed_session(kernel, sym.id, subject_key="ana")
    of_bia = _closed_session(kernel, sym.id, subject_key="bia")
    owner = _closed_session(kernel, sym.id)

    preview = kernel.purge_subject(sym.id, "ana", dry_run=True)
    assert preview["sessions"] == 1
    assert preview["messages"] == 2
    assert preview["memory_entries"] >= 1
    assert kernel.get_session(of_ana) is not None  # dry run deletes nothing

    counts = kernel.purge_subject(sym.id, "ana")

    assert counts == preview
    assert kernel.get_session(of_ana) is None
    assert kernel.inspector.list_messages(of_ana) == []
    assert kernel.inspector.list_memories(sym.id, subject_key="ana", include_inactive=True) == []
    assert "ana" not in {s["subject_key"] for s in kernel.inspector.list_subjects(sym.id)}
    # Bia, the owner's session and the shared memory are intact.
    assert kernel.get_session(of_bia) is not None
    assert kernel.get_session(owner) is not None
    assert len(kernel.inspector.list_messages(of_bia)) == 2
    assert [m["content"] for m in kernel.inspector.list_memories(sym.id, subject_key="bia")] == [OF_BIA]
    assert SHARED in {m["content"] for m in kernel.inspector.list_memories(sym.id, subject_key=None)}


def test_purge_subject_of_an_unknown_end_user_deletes_nothing(shop) -> None:
    kernel, _llm, sym = shop

    counts = kernel.purge_subject(sym.id, "nobody")

    assert set(counts.values()) == {0}
    with pytest.raises(EntityNotFoundError):
        kernel.purge_subject("no-such-symbiote", "ana")
    with pytest.raises(ValidationError):
        kernel.purge_subject(sym.id, "")


# ── compatibility ───────────────────────────────────────────────────────


@pytest.mark.sqlite_only
def test_database_from_before_subjects_opens_and_behaves_as_before(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    sym = kernel.create_symbiote(name="Old", role="assistant")
    kernel._memory.store(_memory(sym.id, SHARED))
    session_id = kernel.start_session(sym.id).id
    kernel.shutdown()
    # Take the database back to the schema of the previous release.
    conn = sqlite3.connect(db)
    conn.executescript(
        "DROP INDEX idx_memory_symbiote_subject;"
        "DROP INDEX idx_sessions_symbiote_subject;"
        "ALTER TABLE memory_entries DROP COLUMN subject_key;"
        "ALTER TABLE sessions DROP COLUMN subject_key;"
        "ALTER TABLE environment_configs DROP COLUMN learn_from_subject_sessions;"
        f"PRAGMA user_version = {SCHEMA_VERSION - 1};"
    )
    conn.close()

    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm, db_path=db)
    try:
        assert kernel.get_session(session_id).subject_key is None
        kernel.message(session_id, "when does the Acme office open?")
        assert "opens at nine" in llm.all_prompts_text()
        assert [m["subject_key"] for m in kernel.inspector.list_memories(sym.id)] == [None]
        adapter = SQLiteAdapter(db_path=db)
        assert adapter._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        adapter.close()
    finally:
        kernel.shutdown()


# ── HTTP API ────────────────────────────────────────────────────────────


def test_api_creates_sessions_for_end_users(api_client) -> None:
    api = api_client(ScriptedLLM("ok"))
    headers = api.auth(api.admin_key)
    sym = api.client.post(
        "/symbiotes", json={"name": "Shop", "role": "assistant"}, headers=headers
    ).json()
    api.kernel._memory.store(_memory(sym["id"], SHARED))
    api.kernel._memory.store(_memory(sym["id"], OF_ANA, subject_key="ana"))

    created = api.client.post(
        "/sessions",
        json={"symbiote_id": sym["id"], "external_key": "chat-1", "subject_key": "ana"},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["subject_key"] == "ana"
    fetched = api.client.get(f"/sessions/{created.json()['id']}", headers=headers)
    assert fetched.json()["subject_key"] == "ana"

    clash = api.client.post(
        "/sessions",
        json={"symbiote_id": sym["id"], "external_key": "chat-1", "subject_key": "bia"},
        headers=headers,
    )
    assert clash.status_code == 422
    empty = api.client.post(
        "/sessions", json={"symbiote_id": sym["id"], "subject_key": ""}, headers=headers
    )
    assert empty.status_code == 422

    shared_only = api.client.get(
        "/memory/search", params={"query": "Acme", "symbiote_id": sym["id"]}, headers=headers
    )
    assert {m["content"] for m in shared_only.json()} == {SHARED}
    with_ana = api.client.get(
        "/memory/search",
        params={"query": "Acme", "symbiote_id": sym["id"], "subject_key": "ana"},
        headers=headers,
    )
    assert {(m["content"], m["subject_key"]) for m in with_ana.json()} == {
        (SHARED, None), (OF_ANA, "ana"),
    }


def test_api_config_carries_the_learning_flag(api_client) -> None:
    api = api_client(ScriptedLLM("ok"))
    headers = api.auth(api.admin_key)
    sym = api.client.post(
        "/symbiotes", json={"name": "Shop", "role": "assistant"}, headers=headers
    ).json()

    before = api.client.get(f"/symbiotes/{sym['id']}/config", headers=headers).json()
    after = api.client.put(
        f"/symbiotes/{sym['id']}/config",
        json={"learn_from_subject_sessions": True},
        headers=headers,
    )

    assert before["learn_from_subject_sessions"] is False
    assert after.status_code == 200
    assert after.json()["learn_from_subject_sessions"] is True


# ── other readers ───────────────────────────────────────────────────────


def test_memory_export_never_mixes_partitions(shop) -> None:
    kernel, _llm, sym = shop

    shared = kernel._export.export_memory(sym.id)
    of_ana = kernel._export.export_memory(sym.id, subject_key="ana")

    assert "opens at nine" in shared
    assert "4471" not in shared and "9902" not in shared
    assert "4471" in of_ana
    assert "opens at nine" not in of_ana and "9902" not in of_ana


def test_unwired_recall_provider_reads_only_shared_memories(shop) -> None:
    from symbiote.memory.recall import SemanticRecallProvider

    kernel, _llm, _sym = shop

    recalled = SemanticRecallProvider(kernel._storage).recall("Acme order office")

    assert _contents(recalled) == {SHARED}
