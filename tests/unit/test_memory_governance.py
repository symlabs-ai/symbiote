"""Per-user governance of memory and learning (P4 of the SymGateway request).

1. Isolation: with one symbiote per user and ``skill_scope="per_symbiote"``,
   nothing one symbiote learned is read by another, including the batch
   processes (dream, background review, cross learning).
2. Retention and erasure: memories expire with ``memory_retention_days``;
   ``purge_symbiote`` removes everything about one symbiote and nothing else.
3. Trace: every turn records which memories, knowledge entries and skills
   entered its context.
4. Switches: with a mechanism turned off, nothing is written to its table.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from symbiote.core.capabilities import CapabilityError
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor
from symbiote.skills.scope import reset_active_symbiote, set_active_symbiote
from symbiote.skills.store import SkillReadOnlyError
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block

SKILL = """---
name: {name}
description: {description}
---

# {name}

{body}
"""

ALICE_SECRET = "the vault code is 7741"


def _count(kernel: SymbioteKernel, table: str, where: str = "1 = 1", params: tuple = ()) -> int:
    row = kernel._storage.fetch_one(
        f"SELECT COUNT(*) AS c FROM {table} WHERE {where}", params  # noqa: S608
    )
    return int(row["c"])


def _tables(kernel: SymbioteKernel) -> list[str]:
    rows = kernel._storage.fetch_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return [r["name"] for r in rows]


def _dump(kernel: SymbioteKernel) -> str:
    return "\n".join(
        json.dumps(row, default=str)
        for table in _tables(kernel)
        for row in kernel._storage.fetch_all(f"SELECT * FROM {table}")  # noqa: S608
    )


def _write_skill(kernel: SymbioteKernel, symbiote_id: str, name: str, body: str) -> None:
    store = kernel.skills_store_for(symbiote_id)
    assert store is not None
    store.create(name, SKILL.format(name=name, description=f"how to {name}", body=body))


# ── 1. isolation ──────────────────────────────────────────────────────────────


class TestIsolationBetweenSymbiotes:
    @pytest.fixture()
    def world(self, tmp_path: Path):  # noqa: ANN201
        llm = ScriptedLLM("ok")
        kernel = make_kernel(tmp_path, llm, skill_scope="per_symbiote")
        kernel.set_evolver_llm(llm)
        alice = kernel.create_symbiote(name="alice", role="assistant")
        bob = kernel.create_symbiote(name="bob", role="assistant")
        for sym in (alice, bob):
            kernel.environment.configure(
                symbiote_id=sym.id,
                tools=["search_memories"],
                skill_injection_enabled=True,
                skill_review_enabled=True,
                dream_mode="full",
                dream_min_sessions=1,
            )
        session = kernel.start_session(alice.id)
        kernel.capabilities.learn(alice.id, session.id, ALICE_SECRET, importance=1.0)
        _write_skill(kernel, alice.id, "open-vault", f"Remember: {ALICE_SECRET}.")
        yield kernel, llm, alice, bob
        kernel.shutdown()

    def test_recall_and_search(self, world) -> None:  # noqa: ANN001
        kernel, _llm, alice, bob = world
        assert kernel.memory.get_relevant("vault code", symbiote_id=alice.id)
        assert kernel.memory.get_relevant("vault code", symbiote_id=bob.id) == []
        assert kernel.memory.search("vault", symbiote_id=bob.id) == []

    def test_context_of_a_turn(self, world) -> None:  # noqa: ANN001
        kernel, llm, alice, bob = world
        kernel.message(kernel.start_session(alice.id).id, "what is the vault code?")
        assert ALICE_SECRET in llm.prompt_text(-1)  # the owner does get it
        assert "open-vault" in llm.prompt_text(-1)

        before = len(llm.calls)
        session = kernel.start_session(bob.id)
        kernel.message(session.id, "what is the vault code?")
        bob_prompts = "\n".join(llm.prompt_text(i) for i in range(before, len(llm.calls)))
        assert ALICE_SECRET not in bob_prompts
        assert "open-vault" not in bob_prompts
        record = kernel.inspector.list_turn_contexts(session_id=session.id)[0]
        assert record["memories"] == [] and record["skills"] == []

    def test_on_demand_search_tool(self, world) -> None:  # noqa: ANN001
        kernel, _llm, _alice, bob = world
        result = kernel.tool_gateway.execute(
            bob.id, None, "search_memories", {"query": "vault"}
        )
        assert result.success and result.output == []

    def test_skills(self, world) -> None:  # noqa: ANN001
        kernel, _llm, alice, bob = world
        assert [s.name for s in kernel.skills_loader_for(alice.id).list_skills()] == ["open-vault"]
        assert kernel.skills_loader_for(bob.id).list_skills() == []

    def test_batch_processes(self, world) -> None:  # noqa: ANN001
        """Close a session of the other symbiote (reflection, background
        review, dream) and run a dream cycle by hand: no prompt sent to a
        model carries anything of the first symbiote."""
        kernel, llm, _alice, bob = world
        before = len(llm.calls)
        session = kernel.start_session(bob.id)
        kernel.message(session.id, "what is the vault code?")
        kernel.close_session(session.id)
        kernel.dream(bob.id)
        for thread in kernel._background_threads():
            thread.join(timeout=10)

        assert len(llm.calls) > before
        bob_prompts = "\n".join(llm.prompt_text(i) for i in range(before, len(llm.calls)))
        assert ALICE_SECRET not in bob_prompts
        assert "open-vault" not in bob_prompts
        # ...and nothing of it was copied under the other symbiote.
        rows = kernel._storage.fetch_all(
            "SELECT content FROM memory_entries WHERE symbiote_id = ?", (bob.id,)
        )
        assert all("7741" not in r["content"] for r in rows)

    def test_cross_learning_can_be_turned_off(self, tmp_path: Path) -> None:
        def build(path: Path, **config: Any) -> list:
            kernel = make_kernel(path, **config)
            source = kernel.create_symbiote(name="source", role="assistant")
            target = kernel.create_symbiote(name="target", role="assistant")
            version = kernel.harness_versions.create_version(
                source.id, "tool_instructions", "instructions written from alice's sessions"
            )
            kernel._storage.execute(
                "UPDATE harness_versions SET avg_score = 0.9 "
                "WHERE symbiote_id = ? AND version = ?",
                (source.id, version),
            )
            found = kernel.cross_learning_candidates(target.id)
            kernel.shutdown()
            return found

        assert len(build(tmp_path / "on")) == 1  # the default reads across symbiotes
        assert build(tmp_path / "off", cross_symbiote_learning=False) == []


# ── 2. retention and erasure ──────────────────────────────────────────────────


class TestMemoryRetention:
    def _kernel_with_memories(self, tmp_path: Path, **config: Any):  # noqa: ANN202
        kernel = make_kernel(tmp_path, **config)
        sym = kernel.create_symbiote(name="a", role="assistant")
        session = kernel.start_session(sym.id)
        old = kernel.capabilities.learn(sym.id, session.id, "an old fact")
        new = kernel.capabilities.learn(sym.id, session.id, "a recent fact")
        created = (datetime.now(tz=UTC) - timedelta(days=40)).isoformat()
        kernel._storage.execute(
            "UPDATE memory_entries SET created_at = ? WHERE id = ?", (created, old.id)
        )
        return kernel, old, new

    def test_off_by_default(self, tmp_path: Path) -> None:
        kernel, _old, _new = self._kernel_with_memories(tmp_path)
        assert kernel.prune()["memory_entries"] == 0
        assert _count(kernel, "memory_entries") == 2
        kernel.shutdown()

    def test_memories_older_than_the_window_are_removed(self, tmp_path: Path) -> None:
        kernel, _old, new = self._kernel_with_memories(tmp_path, memory_retention_days=30)

        assert kernel.prune(dry_run=True)["memory_entries"] == 1
        assert _count(kernel, "memory_entries") == 2
        assert kernel.prune()["memory_entries"] == 1

        rows = kernel._storage.fetch_all("SELECT id FROM memory_entries")
        assert [r["id"] for r in rows] == [new.id]
        kernel.shutdown()

    def test_window_given_to_the_call(self, tmp_path: Path) -> None:
        kernel, _old, _new = self._kernel_with_memories(tmp_path)
        assert kernel.prune(memories_days=30)["memory_entries"] == 1
        kernel.shutdown()


class TestPurgeSymbiote:
    def _populate(self, kernel: SymbioteKernel, name: str):  # noqa: ANN202
        """A symbiote with at least one row in every table the kernel fills
        through its public surface."""
        sym = kernel.create_symbiote(name=name, role="assistant", persona={"tone": "dry"})
        kernel.update_symbiote(sym.id, persona={"tone": "warm"})  # persona_audit
        add_tool(kernel, sym.id, f"{name}_tool", lambda params: "ok")
        kernel.environment.configure(
            symbiote_id=sym.id, skill_review_enabled=True, reflection_mode="hybrid",
            dream_mode="full", dream_min_sessions=1,
        )
        kernel.knowledge.register_source(sym.id, f"{name}-doc", content=f"{name} knowledge")
        kernel.workspaces.create(sym.id, f"{name}-ws", str(kernel._config.db_path.parent))
        kernel.harness_versions.create_version(sym.id, "tool_instructions", f"{name} text")
        kernel.register_http_tool(
            sym.id,
            ToolDescriptor(tool_id=f"{name}_http", name="h", description="d"),
            HttpToolConfig(method="GET", url_template="https://example.com/x"),
        )
        session = kernel.start_session(sym.id)
        kernel.capabilities.learn(sym.id, session.id, f"I prefer {name} things")
        kernel.message(session.id, f"I always want {name} answers, remember that")
        kernel.report_feedback(session.id, 0.9)
        kernel.close_session(session.id)
        kernel.dream(sym.id)
        for thread in kernel._background_threads():
            thread.join(timeout=10)
        _write_skill(kernel, sym.id, f"{name}-skill", "steps")
        return sym

    @pytest.fixture()
    def world(self, tmp_path: Path):  # noqa: ANN201
        calls = {"n": 0}

        def script(_i: int, messages: list[dict]) -> str:
            calls["n"] += 1
            last = str(messages[-1].get("content"))
            if "answers, remember that" in last and "tool_result" not in last:
                name = last.split("want ")[1].split(" ")[0]
                return tool_call_block(f"{name}_tool")
            return "done"

        llm = ScriptedLLM(script)
        kernel = make_kernel(tmp_path, llm, skill_scope="per_symbiote")
        kernel.set_evolver_llm(llm)
        gone = self._populate(kernel, "gone")
        kept = self._populate(kernel, "kept")
        yield kernel, gone, kept, tmp_path
        kernel.shutdown()

    def test_removes_everything_of_one_symbiote_only(self, world) -> None:  # noqa: ANN001
        kernel, gone, kept, tmp_path = world
        kept_before = {
            t: _count(kernel, t) for t in _tables(kernel)
        }
        # The fixture really did fill the tables this test is about.
        for table in (
            "sessions", "messages", "memory_entries", "knowledge_entries",
            "execution_traces", "turn_contexts", "session_scores", "audit_log",
            "environment_configs", "harness_versions", "persona_audit",
            "http_tools", "workspaces", "dream_reports", "reflection_audit",
        ):
            assert _count(kernel, table) >= 2, table
        skills_dir = tmp_path / "skills" / gone.id
        assert skills_dir.is_dir()

        preview = kernel.purge_symbiote(gone.id, dry_run=True)
        assert preview["symbiotes"] == 1 and preview["skills"] == 1
        assert kernel.get_symbiote(gone.id) is not None  # dry run deletes nothing

        counts = kernel.purge_symbiote(gone.id)

        assert counts == preview
        assert counts["memory_entries"] >= 1 and counts["messages"] >= 2
        # No row anywhere still mentions the symbiote or its sessions.
        assert gone.id not in _dump(kernel)
        assert "gone" not in _dump(kernel)
        assert not skills_dir.exists()
        assert not kernel.tool_gateway.has_tool("gone_http")
        # The other symbiote lost nothing.
        assert kernel.get_symbiote(kept.id) is not None
        assert (tmp_path / "skills" / kept.id).is_dir()
        assert kernel.tool_gateway.has_tool("kept_http")
        for table, before in kept_before.items():
            assert _count(kernel, table) == before - counts.get(table, 0), table
        assert kernel.memory.get_relevant("kept things", symbiote_id=kept.id)

    def test_unknown_symbiote(self, world) -> None:  # noqa: ANN001
        kernel, *_ = world
        with pytest.raises(EntityNotFoundError):
            kernel.purge_symbiote("no-such-id")

    def test_twice_is_an_error_not_a_partial_delete(self, world) -> None:  # noqa: ANN001
        kernel, gone, *_ = world
        kernel.purge_symbiote(gone.id)
        with pytest.raises(EntityNotFoundError):
            kernel.purge_symbiote(gone.id)


# ── 3. what entered the context of each turn ──────────────────────────────────


class TestTurnContextTrace:
    def _kernel(self, tmp_path: Path, llm: Any = "ok", **config: Any):  # noqa: ANN202
        kernel = make_kernel(tmp_path, ScriptedLLM(llm), skill_scope="per_symbiote", **config)
        sym = kernel.create_symbiote(name="a", role="assistant")
        session = kernel.start_session(sym.id)
        memory = kernel.capabilities.learn(sym.id, session.id, "the user prefers tea", importance=0.9)
        other = kernel.capabilities.learn(sym.id, session.id, "the office is in Lisbon")
        doc = kernel.knowledge.register_source(sym.id, "tea-guide", content="how to brew tea")
        return kernel, sym, session, memory, other, doc

    def test_turn_without_tools_is_recorded(self, tmp_path: Path) -> None:
        kernel, sym, session, memory, other, doc = self._kernel(tmp_path)
        _write_skill(kernel, sym.id, "brew-tea", "boil water")
        kernel.environment.configure(symbiote_id=sym.id, skill_injection_enabled=True)

        kernel.message(session.id, "which tea should I brew?")

        assert _count(kernel, "execution_traces") == 0  # no tools ran
        records = kernel.inspector.list_turn_contexts(session_id=session.id)
        assert len(records) == 1
        record = records[0]
        assert record["memories"] == [{"id": memory.id, "source": "packed"}]
        assert other.id not in json.dumps(record)  # recalled nothing about Lisbon
        assert record["knowledge"] == [{"id": doc.id, "source": "packed"}]
        assert record["skills"] == ["brew-tea"]
        assert record["tokens_estimate"] > 0
        assert kernel.inspector.list_turn_contexts(symbiote_id=sym.id) == records
        kernel.shutdown()

    def test_one_record_per_turn_sync_and_async(self, tmp_path: Path) -> None:
        kernel, _sym, session, memory, _other, _doc = self._kernel(tmp_path)
        kernel.message(session.id, "tea?")
        asyncio.run(kernel.message_async(session.id, "which tea again?"))
        kernel.message(session.id, "something unrelated to anything stored")

        records = kernel.inspector.list_turn_contexts(session_id=session.id)
        assert len(records) == 3
        assert [bool(r["memories"]) for r in reversed(records)] == [True, True, False]
        assert records[1]["memories"] == [{"id": memory.id, "source": "packed"}]
        kernel.shutdown()

    def test_on_demand_results_are_recorded(self, tmp_path: Path) -> None:
        script = lambda i, _m: (  # noqa: E731
            "\n".join([
                tool_call_block("search_memories", query="tea"),
                tool_call_block("search_knowledge", query="tea"),
            ])
            if i == 0 else "done"
        )
        kernel, sym, session, memory, _other, doc = self._kernel(tmp_path, script)
        kernel.environment.configure(symbiote_id=sym.id, context_mode="on_demand")

        kernel.message(session.id, "which tea should I brew?")

        record = kernel.inspector.list_turn_contexts(session_id=session.id)[0]
        assert record["memories"] == [{"id": memory.id, "source": "on_demand"}]
        assert record["knowledge"] == [{"id": doc.id, "source": "on_demand"}]
        kernel.shutdown()

    def test_failed_turn_is_recorded_too(self, tmp_path: Path) -> None:
        def boom(_i: int, _m: list[dict]) -> str:
            raise RuntimeError("provider down")

        kernel, _sym, session, memory, _other, _doc = self._kernel(tmp_path, boom)
        with pytest.raises(Exception, match="provider down|Chat runner failed|chat"):
            kernel.message(session.id, "which tea should I brew?")

        records = kernel.inspector.list_turn_contexts(session_id=session.id)
        assert [r["memories"] for r in records] == [[{"id": memory.id, "source": "packed"}]]
        kernel.shutdown()

    def test_pruned_with_the_traces(self, tmp_path: Path) -> None:
        kernel, _sym, session, *_ = self._kernel(tmp_path)
        kernel.message(session.id, "tea?")
        kernel.close_session(session.id)
        ended = (datetime.now(tz=UTC) - timedelta(days=40)).isoformat()
        kernel._storage.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (ended, session.id))

        assert kernel.prune(traces_days=30)["turn_contexts"] == 1
        assert kernel.inspector.list_turn_contexts(session_id=session.id) == []
        kernel.shutdown()


# ── 4. switches ───────────────────────────────────────────────────────────────


class TestSwitches:
    def _closed_session(self, kernel: SymbioteKernel, symbiote_id: str) -> None:
        """A session whose close would normally learn: a stated preference
        and a tool loop that hits the circuit breaker (failure memory)."""
        session = kernel.start_session(symbiote_id)
        kernel.message(session.id, "I always prefer short answers, remember that")
        kernel.close_session(session.id)
        for thread in kernel._background_threads():
            thread.join(timeout=10)

    def _kernel(self, tmp_path: Path, **config: Any):  # noqa: ANN202
        def script(_i: int, messages: list[dict]) -> str:
            return tool_call_block("flaky")  # never stops calling a failing tool

        llm = ScriptedLLM(script)
        kernel = make_kernel(tmp_path, llm, skill_scope="per_symbiote", **config)
        kernel.set_evolver_llm(llm)
        sym = kernel.create_symbiote(name="a", role="assistant")

        def flaky(params: dict) -> str:
            raise RuntimeError("nope")

        add_tool(kernel, sym.id, "flaky", flaky)
        return kernel, llm, sym

    def test_baseline_learns(self, tmp_path: Path) -> None:
        """The same scenario with everything on does write: the tests below
        are not passing just because nothing would be written anyway."""
        kernel, _llm, sym = self._kernel(tmp_path)
        self._closed_session(kernel, sym.id)
        sources = {
            r["source"]
            for r in kernel._storage.fetch_all("SELECT source FROM memory_entries")
        }
        assert {"reflection", "system"} <= sources
        kernel.shutdown()

    def test_reflection_off(self, tmp_path: Path) -> None:
        kernel, llm, sym = self._kernel(tmp_path)
        kernel.environment.configure(symbiote_id=sym.id, reflection_mode="hybrid")
        kernel.environment.configure(symbiote_id=sym.id, reflection_mode="off")
        self._closed_session(kernel, sym.id)

        assert _count(kernel, "memory_entries", "source = 'reflection'") == 0
        assert _count(kernel, "reflection_audit") == 0
        # Reflection is one mechanism; the failure memory is another.
        assert _count(kernel, "memory_entries", "source = 'system'") == 1
        kernel.shutdown()

    def test_memory_off_writes_and_recalls_nothing(self, tmp_path: Path) -> None:
        kernel, llm, sym = self._kernel(tmp_path)
        session = kernel.start_session(sym.id)
        kernel.capabilities.learn(sym.id, session.id, "the user prefers short answers")
        kernel.environment.configure(
            symbiote_id=sym.id, memory_enabled=False, reflection_mode="hybrid",
            dream_mode="full", dream_min_sessions=1, tools=["flaky", "search_memories"],
        )
        before = _count(kernel, "memory_entries")

        self._closed_session(kernel, sym.id)
        kernel.dream(sym.id)

        assert _count(kernel, "memory_entries") == before  # nothing written
        assert _count(kernel, "reflection_audit") == 0
        assert _count(kernel, "dream_reports", "dry_run = 0") <= 1  # only the manual one
        with pytest.raises(CapabilityError, match="disabled"):
            kernel.capabilities.learn(sym.id, session.id, "another fact")
        # ...and the memory that already existed is not recalled.
        assert "prefers short answers" not in llm.prompt_text(0)
        assert kernel.memory.get_relevant("short answers", symbiote_id=sym.id) == []
        result = kernel.tool_gateway.execute(sym.id, None, "search_memories", {"query": "short"})
        assert result.output == []
        kernel.shutdown()

    def test_memory_switch_is_per_symbiote(self, tmp_path: Path) -> None:
        kernel, _llm, sym = self._kernel(tmp_path)
        other = kernel.create_symbiote(name="b", role="assistant")
        kernel.environment.configure(symbiote_id=sym.id, memory_enabled=False)
        session = kernel.start_session(other.id)
        kernel.capabilities.learn(other.id, session.id, "the user prefers tea")

        assert kernel.memory.get_relevant("tea", symbiote_id=other.id)
        kernel.shutdown()

    def test_background_review_off_by_default(self, tmp_path: Path) -> None:
        kernel, llm, sym = self._kernel(tmp_path)
        self._closed_session(kernel, sym.id)

        assert _count(kernel, "skill_review_audit") == 0
        assert kernel._background_threads() == []
        kernel.shutdown()

    def test_skill_writes_off(self, tmp_path: Path) -> None:
        skills_root = tmp_path / "skills"
        kernel, llm, sym = self._kernel(tmp_path, skill_writes=False)
        kernel.environment.configure(
            symbiote_id=sym.id, skill_review_enabled=True, dream_mode="full",
            dream_min_sessions=1, tools=["flaky", "skill_manage"],
        )
        self._closed_session(kernel, sym.id)
        kernel.dream(sym.id)

        store = kernel.skills_store_for(sym.id)
        with pytest.raises(SkillReadOnlyError):
            store.create("x", SKILL.format(name="x", description="d", body="b"))
        # The tool resolves the store of the symbiote whose turn is running.
        token = set_active_symbiote(sym.id)
        try:
            result = kernel.tool_gateway.execute(
                sym.id, None, "skill_manage",
                {"action": "create", "name": "x",
                 "content": SKILL.format(name="x", description="d", body="b")},
            )
        finally:
            reset_active_symbiote(token)
        assert "Skill writes are disabled" in json.dumps(result.model_dump(), default=str)
        assert _count(kernel, "skill_review_audit") == 0  # the review never ran
        # Nothing at all was created on disk.
        assert not skills_root.exists() or not any(skills_root.rglob("*"))
        kernel.shutdown()

    def test_read_only_library_is_still_readable(self, tmp_path: Path) -> None:
        writer = make_kernel(tmp_path, skill_scope="per_symbiote")
        sym = writer.create_symbiote(name="a", role="assistant")
        _write_skill(writer, sym.id, "brew-tea", "boil water")
        writer.shutdown()
        sidecar = tmp_path / "skills" / sym.id / "skills" / "brew-tea" / ".skill_meta.json"
        before = sidecar.read_text() if sidecar.exists() else None

        reader = make_kernel(tmp_path, skill_scope="per_symbiote", skill_writes=False)
        loader = reader.skills_loader_for(sym.id)
        assert loader.get_skill("brew-tea").content
        # Loading it did not count a use on disk.
        assert (sidecar.read_text() if sidecar.exists() else None) == before
        reader.shutdown()


# ── cache bound ───────────────────────────────────────────────────────────────


def test_review_engines_do_not_grow_with_the_number_of_symbiotes(tmp_path: Path) -> None:
    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm, skill_scope="per_symbiote", symbiote_cache_size=3)
    kernel.set_evolver_llm(llm)
    ids = []
    for i in range(10):
        sym = kernel.create_symbiote(name=f"user-{i}", role="assistant")
        kernel.environment.configure(symbiote_id=sym.id, skill_review_enabled=True)
        assert kernel._background_review_for(sym.id) is not None
        ids.append(sym.id)

    assert list(kernel._background_review_by_symbiote) == ids[-3:]
    assert len(kernel._skill_scope_mgr._cache) <= 3
    # An evicted symbiote gets a working engine again.
    assert kernel._background_review_for(ids[0]) is not None
    kernel.shutdown()
