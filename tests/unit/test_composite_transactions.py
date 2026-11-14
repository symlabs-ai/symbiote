"""Composite operations are all-or-nothing.

Each test lets a composite operation start, makes one of its later writes
fail, and checks that the earlier writes of the same operation are gone.
Runs on whatever database the suite uses (SQLite or PostgreSQL).

The covered operations are listed in docs/HOST_INTEGRATION.md,
"Transactions".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.discovery.service import DiscoveryService
from symbiote.environment.descriptors import ToolDescriptor
from tests.support import ScriptedLLM, add_tool, tool_call_block


class InjectedError(RuntimeError):
    """The injected failure."""


class FailingStorage:
    """A storage adapter whose writes matching ``fail_on`` raise ``InjectedError``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.fail_on: re.Pattern[str] | None = None
        self.skip = 0  # matching writes to let through before failing

    def arm(self, pattern: str, *, skip: int = 0) -> None:
        self.fail_on = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        self.skip = skip

    def disarm(self) -> None:
        self.fail_on = None

    def execute(self, sql: str, params: tuple | None = None) -> Any:
        if self.fail_on is not None and self.fail_on.search(sql):
            if self.skip == 0:
                raise InjectedError(sql[:60])
            self.skip -= 1
        return self._inner.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture()
def storage(tmp_path: Path):
    adapter = SQLiteAdapter(tmp_path / "tx.db", check_same_thread=False)
    failing = FailingStorage(adapter)
    yield failing
    adapter.close()


@pytest.fixture()
def replies() -> list[str]:
    """Model replies for the next calls; "ok" once it runs out."""
    return []


@pytest.fixture()
def kernel(storage: FailingStorage, replies: list[str], tmp_path: Path):
    k = SymbioteKernel(
        KernelConfig(db_path=tmp_path / "unused.db", skills_root=tmp_path / "skills"),
        llm=ScriptedLLM(lambda _i, _m: replies.pop(0) if replies else "ok"),
        storage=storage,
    )
    yield k
    k.shutdown()


def _count(storage: FailingStorage, table: str, where: str = "1 = 1", params: tuple = ()) -> int:
    row = storage.fetch_one(f"SELECT COUNT(*) AS c FROM {table} WHERE {where}", params)  # noqa: S608
    return int(row["c"])


def _snapshot(storage: FailingStorage) -> dict[str, int]:
    return {table: _count(storage, table) for table in storage.table_names()}


def test_turn_result_trace_and_reply_are_stored_together(kernel, storage, replies) -> None:
    sym = kernel.create_symbiote(name="a", role="r")
    add_tool(kernel, sym.id, "echo", lambda p: p.get("text", ""))
    session = kernel.start_session(sym.id)

    # Control: a turn with a tool call stores its trace and its reply.
    replies += [tool_call_block("echo", text="hi"), "done"]
    kernel.message(session.id, "use the tool")
    assert _count(storage, "execution_traces") == 1
    assert _count(storage, "messages") == 2

    replies += [tool_call_block("echo", text="hi"), "done"]
    storage.arm(r"INSERT INTO messages", skip=1)  # the user message passes
    with pytest.raises(InjectedError):
        kernel.message(session.id, "use the tool again")
    storage.disarm()

    # The assistant message failed, so the trace written just before it in
    # the same transaction is gone too (and the user message was undone).
    assert _count(storage, "execution_traces") == 1
    assert _count(storage, "messages") == 2


def test_persona_change_and_its_audit_row(kernel, storage) -> None:
    sym = kernel.create_symbiote(name="a", role="r", persona={"tone": "old"})
    storage.arm(r"UPDATE symbiotes SET persona_json")
    with pytest.raises(InjectedError):
        kernel.update_symbiote(sym.id, persona={"tone": "new"})
    storage.disarm()
    assert _count(storage, "persona_audit") == 0
    assert kernel.get_symbiote(sym.id).persona_json == {"tone": "old"}

    kernel.update_symbiote(sym.id, persona={"tone": "new"})
    assert _count(storage, "persona_audit") == 1


def test_new_harness_version_keeps_the_old_one_active_on_failure(kernel, storage) -> None:
    sym = kernel.create_symbiote(name="a", role="r")
    versions = kernel.harness_versions
    assert versions.create_version(sym.id, "tool_instructions", "v1 text") == 1

    storage.arm(r"INSERT INTO harness_versions")
    with pytest.raises(InjectedError):
        versions.create_version(sym.id, "tool_instructions", "v2 text")
    storage.disarm()

    active = versions.get_active_version(sym.id, "tool_instructions")
    assert active is not None and active["version"] == 1 and active["content"] == "v1 text"


def test_harness_rollback_is_atomic(kernel, storage) -> None:
    sym = kernel.create_symbiote(name="a", role="r")
    versions = kernel.harness_versions
    versions.create_version(sym.id, "tool_instructions", "v1 text")
    versions.create_version(sym.id, "tool_instructions", "v2 text")

    storage.arm(r"UPDATE harness_versions SET is_active = 1")
    with pytest.raises(InjectedError):
        versions.rollback(sym.id, "tool_instructions")
    storage.disarm()
    assert versions.get_active_version(sym.id, "tool_instructions")["version"] == 2

    assert versions.rollback(sym.id, "tool_instructions") is True
    assert versions.get_active_version(sym.id, "tool_instructions")["version"] == 1


def test_session_score_and_harness_counters(kernel, storage) -> None:
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.harness_versions.create_version(sym.id, "tool_instructions", "v1 text")
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "hello")

    storage.arm(r"UPDATE harness_versions SET avg_score")
    kernel.close_session(session.id)  # the score step fails and is logged
    storage.disarm()

    assert _count(storage, "session_scores") == 0
    row = storage.fetch_one(
        "SELECT session_count FROM harness_versions WHERE symbiote_id = ? AND version = 1",
        (sym.id,),
    )
    assert row["session_count"] == 0


def _two_symbiotes_with_history(kernel) -> tuple[str, str]:
    ids = []
    for name in ("a", "b"):
        sym = kernel.create_symbiote(name=name, role="r")
        kernel.environment.configure(symbiote_id=sym.id, tools=[])
        session = kernel.start_session(sym.id)
        kernel.message(session.id, "I prefer short answers, always")
        kernel.close_session(session.id)
        ids.append(sym.id)
    return ids[0], ids[1]


def test_purge_is_all_or_nothing(kernel, storage) -> None:
    a, _b = _two_symbiotes_with_history(kernel)
    before = _snapshot(storage)
    assert before["messages"] >= 4 and before["memory_entries"] >= 2

    storage.arm(r"DELETE FROM sessions")  # one of the last deletes
    with pytest.raises(InjectedError):
        kernel.purge_symbiote(a)
    storage.disarm()
    assert _snapshot(storage) == before

    kernel.purge_symbiote(a)
    after = _snapshot(storage)
    assert after["symbiotes"] == before["symbiotes"] - 1
    assert _count(storage, "messages", "session_id IN (SELECT id FROM sessions WHERE symbiote_id = ?)", (a,)) == 0


def test_prune_is_all_or_nothing(kernel, storage) -> None:
    _two_symbiotes_with_history(kernel)
    before = _snapshot(storage)
    assert before["messages"] and before["turn_contexts"] and before["memory_entries"]

    storage.arm(r"DELETE FROM memory_entries")  # the last delete of a prune
    with pytest.raises(InjectedError):
        kernel.prune(messages_days=0, traces_days=0, memories_days=0)
    storage.disarm()
    assert _snapshot(storage) == before

    counts = kernel.prune(messages_days=0, traces_days=0, memories_days=0)
    assert counts["messages"] == before["messages"]
    assert _count(storage, "messages") == 0 and _count(storage, "memory_entries") == 0


def test_reflection_facts_and_watermark_move_together(kernel, storage) -> None:
    sym = kernel.create_symbiote(name="a", role="r")
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "I prefer short answers, always")

    storage.arm(r"UPDATE sessions SET reflected_until")
    kernel.close_session(session.id)  # the reflection step fails and is logged
    storage.disarm()
    learned = "source = 'reflection' AND symbiote_id = ?"
    assert _count(storage, "memory_entries", learned, (sym.id,)) == 0
    row = storage.fetch_one("SELECT reflected_until FROM sessions WHERE id = ?", (session.id,))
    assert row["reflected_until"] is None

    # Closing again learns the same messages once.
    kernel._sessions.resume(session.id)
    kernel.close_session(session.id)
    first = _count(storage, "memory_entries", learned, (sym.id,))
    assert first >= 1
    kernel._sessions.resume(session.id)
    kernel.close_session(session.id)
    assert _count(storage, "memory_entries", learned, (sym.id,)) == first


def test_secret_scrub_of_a_session_is_atomic(kernel, storage) -> None:
    sym = kernel.create_symbiote(name="a", role="r")
    secret = "sk-live-0123456789abcdef"
    descriptor = ToolDescriptor(
        tool_id="register",
        name="register",
        description="register a credential",
        parameters={"type": "object", "properties": {"key": {"type": "string"}}},
        secret_params=["key"],
    )
    kernel.tool_gateway.register_descriptor(descriptor, lambda p: "stored")
    kernel.environment.configure(symbiote_id=sym.id, tools=["register"])
    session = kernel.start_session(sym.id)
    kernel._sessions.add_message(session.id, "user", f"my key is {secret}")
    kernel._sessions.add_message(session.id, "assistant", f"got {secret}")
    kernel.memory.store(_memory(sym.id, session.id, f"the key is {secret}"))

    # The scrub rewrites messages first and memory_entries last.
    storage.arm(r"UPDATE memory_entries SET content = REPLACE")
    with pytest.raises(InjectedError):
        kernel._learn_secret_params(session.id, ["key"], {"key": secret})
    storage.disarm()
    assert _count(storage, "messages", "content LIKE ?", (f"%{secret}%",)) == 2

    kernel._storage.scrub_session(session.id, [secret], "[REDACTED]")
    assert _count(storage, "messages", "content LIKE ?", (f"%{secret}%",)) == 0
    assert _count(storage, "memory_entries", "content LIKE ?", (f"%{secret}%",)) == 0


def _memory(symbiote_id: str, session_id: str, content: str):
    from symbiote.core.models import MemoryEntry

    return MemoryEntry(
        symbiote_id=symbiote_id, session_id=session_id, type="factual",
        scope="session", content=content, source="user",
    )


def test_a_discovery_scan_is_stored_whole(kernel, storage, tmp_path: Path) -> None:
    sym = kernel.create_symbiote(name="a", role="r")
    source = tmp_path / "app"
    source.mkdir()
    (source / "routes.py").write_text(
        '@router.get("/api/one")\ndef one(): pass\n\n'
        '@router.get("/api/two")\ndef two(): pass\n\n'
        '@router.get("/api/three")\ndef three(): pass\n'
    )
    service = DiscoveryService(DiscoveredToolRepository(storage))

    storage.arm(r"INSERT INTO discovered_tools", skip=2)  # the third tool fails
    with pytest.raises(InjectedError):
        service.discover(sym.id, str(source))
    storage.disarm()
    assert _count(storage, "discovered_tools") == 0

    assert len(service.discover(sym.id, str(source)).discovered) == 3
    assert _count(storage, "discovered_tools") == 3


def test_environment_configure_reads_and_writes_in_one_transaction(kernel, storage) -> None:
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.environment.configure(symbiote_id=sym.id, tools=["a"])
    storage.arm(r"UPDATE environment_configs")
    with pytest.raises(InjectedError):
        kernel.environment.add_tools(sym.id, ["b"])
    storage.disarm()
    assert kernel.environment.list_tools(sym.id) == ["a"]
    assert kernel.environment.add_tools(sym.id, ["b"]) == ["a", "b"]


def test_recall_touches_are_one_commit(kernel, storage) -> None:
    sym = kernel.create_symbiote(name="a", role="r")
    for i in range(3):
        kernel.memory.store(_memory(sym.id, None, f"gateway restart procedure {i}"))  # type: ignore[arg-type]
    before = storage.fetch_all("SELECT id, last_used_at FROM memory_entries ORDER BY id")

    storage.arm(r"UPDATE memory_entries SET last_used_at", skip=2)
    with pytest.raises(InjectedError):
        kernel.memory.get_relevant("gateway restart", limit=3, symbiote_id=sym.id)
    storage.disarm()
    assert storage.fetch_all("SELECT id, last_used_at FROM memory_entries ORDER BY id") == before
