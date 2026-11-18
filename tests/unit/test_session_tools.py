"""A session narrowed to a list of tools (B-96).

The same symbiote serves different audiences, each allowed a different set
of tools. ``start_session(..., tools_allowed=[...])`` narrows one session: it
sees and may call only the tools in the list that the configuration it reads
authorizes. The list only narrows, covers the tools a mode adds by itself,
and follows the delegation.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.core.session_scope import current_session_scope, session_tools_for
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.policies import ApprovalDecision
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


def _ask(*tools: str):
    """A model that asks for ``tools`` once and then answers."""
    block = "\n".join(tool_call_block(t) for t in tools)
    return lambda _i, m: "done" if "[Tool" in str(m[-1]["content"]) else block


class Shop:
    """A symbiote authorized to ``lookup`` and ``refund``; ``ghost`` is
    registered but not authorized."""

    def __init__(self, tmp_path: Path, script=None, **configure) -> None:
        self.llm = ScriptedLLM(script or _ask("lookup", "refund", "ghost"))
        self.kernel = make_kernel(tmp_path, self.llm)
        k = self.kernel
        self.sym = k.create_symbiote(name="Shop", role="support agent")
        self.ran: list[str] = []
        add_tool(k, self.sym.id, "lookup", lambda _p: self.ran.append("lookup") or "found")
        add_tool(k, self.sym.id, "refund", lambda _p: self.ran.append("refund") or "refunded")
        k.tool_gateway.register_tool("ghost", lambda _p: self.ran.append("ghost") or "boo")
        k.environment.configure(symbiote_id=self.sym.id, max_tool_iterations=4, **configure)

    def results(self, session_id: str, text: str = "go") -> dict:
        return {r.tool_id: r for r in self.kernel.turn(session_id, text).tool_results}

    def listed(self, index: int = 0) -> set[str]:
        """The tools the prompt of one call lists."""
        prompt = self.llm.prompt_text(index)
        return {
            t for t in ("lookup", "refund", "ghost", "spawn", "get_tool_schema",
                        "search_memories", "search_knowledge")
            if f"### {t} " in prompt or f"**{t}**" in prompt
        }


@pytest.fixture()
def shop(tmp_path: Path):
    s = Shop(tmp_path)
    yield s
    s.kernel.shutdown()


# ── what a narrowed session sees and may call ───────────────────────────


def test_a_narrowed_session_sees_and_calls_only_its_tools(shop) -> None:
    session = shop.kernel.start_session(shop.sym.id, tools_allowed=["lookup"])

    results = shop.results(session.id)

    assert results["lookup"].success is True
    assert results["refund"].success is False
    assert "not available in this conversation" in results["refund"].error
    assert shop.ran == ["lookup"]
    assert shop.listed() == {"lookup"}
    assert session.tools_allowed == ["lookup"]


def test_a_session_without_a_list_is_as_before(shop) -> None:
    session = shop.kernel.start_session(shop.sym.id)

    results = shop.results(session.id)

    assert results["lookup"].success and results["refund"].success
    assert results["ghost"].success is False  # the configuration never authorized it
    assert {"lookup", "refund"} <= shop.listed()
    assert session.tools_allowed is None


def test_the_list_only_narrows(shop) -> None:
    # ``ghost`` is in the session's list but not in the configuration's.
    session = shop.kernel.start_session(shop.sym.id, tools_allowed=["lookup", "ghost"])

    results = shop.results(session.id)

    assert results["lookup"].success is True
    assert results["ghost"].success is False and "deny by default" in results["ghost"].error
    assert "ghost" not in shop.ran


def test_an_empty_list_is_no_tool_at_all(shop) -> None:
    session = shop.kernel.start_session(shop.sym.id, tools_allowed=[])

    shop.kernel.message(session.id, "go")

    assert shop.ran == []
    assert shop.listed() == set()


def test_the_listing_is_narrowed_under_the_authorized_visibility_too(tmp_path: Path) -> None:
    shop = Shop(tmp_path, tool_visibility="authorized")
    try:
        narrowed = shop.kernel.start_session(shop.sym.id, tools_allowed=["refund"])
        shop.kernel.message(narrowed.id, "go")
        assert shop.listed() == {"refund"}
    finally:
        shop.kernel.shutdown()


def test_the_list_is_read_with_a_revision_as_with_the_current_configuration(shop) -> None:
    k = shop.kernel
    k.create_revision(shop.sym.id, "v1")  # lookup and refund
    k.environment.configure(symbiote_id=shop.sym.id, tools=["refund"])  # the draft drops lookup
    session = k.start_session(shop.sym.id, revision="v1", tools_allowed=["lookup", "ghost"])

    results = shop.results(session.id)

    # The revision still authorizes lookup; the session leaves refund out.
    assert results["lookup"].success is True
    assert results["refund"].success is False
    assert results["ghost"].success is False


def test_async_turns_are_narrowed_too(shop) -> None:
    session = shop.kernel.start_session(shop.sym.id, tools_allowed=["refund"])

    asyncio.run(shop.kernel.message_async(session.id, "go"))

    assert shop.ran == ["refund"]
    assert shop.listed() == {"refund"}


def test_a_refused_call_is_in_the_audit_log(shop) -> None:
    session = shop.kernel.start_session(shop.sym.id, tools_allowed=["lookup"])
    shop.results(session.id)

    rows = shop.kernel._storage.fetch_all(
        "SELECT tool_id, action FROM audit_log WHERE session_id = ? ORDER BY tool_id",
        (session.id,),
    )

    assert [(r["tool_id"], r["action"]) for r in rows] == [
        ("ghost", "blocked"), ("lookup", "execute"), ("refund", "blocked"),
    ]


def test_the_scope_ends_with_the_turn(shop) -> None:
    seen = []
    add_tool(
        shop.kernel, shop.sym.id, "peek",
        lambda _p: seen.append(session_tools_for(shop.sym.id)) or "ok",
    )
    shop.llm._fn = _ask("peek")
    session = shop.kernel.start_session(shop.sym.id, tools_allowed=["peek"])

    shop.kernel.message(session.id, "go")

    assert seen == [frozenset({"peek"})]
    assert current_session_scope() is None


# ── the tools a mode adds by itself ─────────────────────────────────────


def test_mode_tools_says_what_each_mode_adds(shop) -> None:
    env, sid = shop.kernel.environment, shop.sym.id
    assert env.mode_tools(sid) == []
    env.configure(symbiote_id=sid, context_mode="on_demand")
    assert env.mode_tools(sid) == ["search_memories", "search_knowledge"]
    env.configure(symbiote_id=sid, tool_loading="index")
    assert env.mode_tools(sid) == ["get_tool_schema", "search_memories", "search_knowledge"]


def test_on_demand_tools_are_narrowed_and_the_prompt_stops_naming_them(tmp_path: Path) -> None:
    shop = Shop(tmp_path, _ask("search_knowledge", "search_memories"), context_mode="on_demand")
    try:
        k = shop.kernel
        k.knowledge.register_source(shop.sym.id, "policy", content="refunds take five days")
        without = k.start_session(shop.sym.id, tools_allowed=["lookup"])
        one = k.start_session(shop.sym.id, tools_allowed=["lookup", "search_knowledge"])
        every = k.start_session(shop.sym.id)

        cut = shop.results(without.id, "policy?")
        assert cut["search_knowledge"].success is False
        assert cut["search_memories"].success is False
        assert shop.listed(0) == {"lookup"}
        assert "Context Access" not in shop.llm.prompt_text(0)

        first = len(shop.llm.calls)
        kept = shop.results(one.id, "policy?")
        assert kept["search_knowledge"].success is True
        assert kept["search_memories"].success is False
        assert shop.listed(first) == {"lookup", "search_knowledge"}
        prompt = shop.llm.prompt_text(first)
        assert "You have access to the search_knowledge tool" in prompt
        assert "search_memories" not in prompt

        first = len(shop.llm.calls)
        assert all(r.success for r in shop.results(every.id, "policy?").values())
        assert "You have access to search_memories and search_knowledge tools" in (
            shop.llm.prompt_text(first)
        )
    finally:
        shop.kernel.shutdown()


def test_without_get_tool_schema_an_index_session_is_shown_full_schemas(tmp_path: Path) -> None:
    shop = Shop(tmp_path, _ask("get_tool_schema"), tool_loading="index")
    try:
        k = shop.kernel
        without = k.start_session(shop.sym.id, tools_allowed=["lookup"])
        with_it = k.start_session(shop.sym.id, tools_allowed=["lookup", "get_tool_schema"])

        cut = shop.results(without.id)
        assert cut["get_tool_schema"].success is False
        prompt = shop.llm.prompt_text(0)
        assert "## Available Tools (Index)" not in prompt
        assert "### lookup " in prompt and "get_tool_schema" not in shop.listed(0)

        first = len(shop.llm.calls)
        k.message(with_it.id, "go")
        prompt = shop.llm.prompt_text(first)
        assert "## Available Tools (Index)" in prompt
        assert shop.listed(first) == {"lookup", "get_tool_schema"}
    finally:
        shop.kernel.shutdown()


# ── changing and reading the list ───────────────────────────────────────


def test_the_list_can_be_changed_and_lifted(shop) -> None:
    k = shop.kernel
    session = k.start_session(shop.sym.id, tools_allowed=["lookup"])

    assert k.set_session_tools(session.id, ["refund"]).tools_allowed == ["refund"]
    assert shop.results(session.id)["refund"].success is True
    assert shop.ran == ["refund"]

    assert k.set_session_tools(session.id, None).tools_allowed is None
    results = shop.results(session.id)
    assert results["lookup"].success and results["refund"].success


@pytest.mark.parametrize("bad", ["lookup", 7, [""], ["lookup", 3], {"lookup": True}])
def test_the_list_must_be_one_of_tool_ids(shop, bad) -> None:
    with pytest.raises(ValidationError):
        shop.kernel.start_session(shop.sym.id, tools_allowed=bad)
    session = shop.kernel.start_session(shop.sym.id)
    with pytest.raises(ValidationError):
        shop.kernel.set_session_tools(session.id, bad)


def test_repeats_are_dropped_and_an_unknown_session_is_reported(shop) -> None:
    session = shop.kernel.start_session(shop.sym.id, tools_allowed=["lookup", " lookup ", "refund"])
    assert session.tools_allowed == ["lookup", "refund"]
    with pytest.raises(EntityNotFoundError):
        shop.kernel.set_session_tools("no-such-session", ["lookup"])
    with pytest.raises(EntityNotFoundError):
        shop.kernel.set_session_delegation("no-such-session", "strict")
    with pytest.raises(ValidationError):
        shop.kernel.start_session(shop.sym.id, delegation="sometimes")


def test_a_session_found_again_keeps_its_list(shop) -> None:
    k = shop.kernel
    first = k.get_or_create_session(shop.sym.id, "visitor-1", tools_allowed=["lookup"])
    again = k.get_or_create_session(shop.sym.id, "visitor-1", tools_allowed=["refund"])
    plain = k.get_or_create_session(shop.sym.id, "visitor-1")

    assert again.id == plain.id == first.id
    assert again.tools_allowed == plain.tools_allowed == ["lookup"]


def test_the_list_is_on_the_session_for_whoever_audits(shop) -> None:
    k = shop.kernel
    narrowed = k.start_session(shop.sym.id, tools_allowed=["lookup"], delegation="strict")
    k.start_session(shop.sym.id)

    rows = {r["id"]: r for r in k.inspector.list_sessions(shop.sym.id)}

    assert rows[narrowed.id]["tools_allowed"] == ["lookup"]
    assert rows[narrowed.id]["delegation"] == "strict"
    others = [r for sid, r in rows.items() if sid != narrowed.id]
    assert [(r["tools_allowed"], r["delegation"]) for r in others] == [(None, "open")]
    assert k.get_session(narrowed.id).tools_allowed == ["lookup"]


def test_the_list_survives_a_restart(tmp_path: Path) -> None:
    shop = Shop(tmp_path)
    session = shop.kernel.start_session(shop.sym.id, tools_allowed=["lookup"], delegation="strict")
    shop.kernel.shutdown()

    again = Shop.__new__(Shop)
    again.llm = ScriptedLLM(_ask("lookup", "refund"))
    again.kernel = make_kernel(tmp_path, again.llm)
    again.ran = []
    try:
        for tool in ("lookup", "refund"):
            again.kernel.tool_gateway.register_tool(
                tool, lambda _p, tool=tool: again.ran.append(tool) or "ok"
            )
        reread = again.kernel.get_session(session.id)
        assert (reread.tools_allowed, reread.delegation) == (["lookup"], "strict")
        again.kernel.message(session.id, "go")
        assert again.ran == ["lookup"]
    finally:
        again.kernel.shutdown()


@pytest.mark.sqlite_only
def test_a_database_from_before_opens_with_no_session_narrowed(tmp_path: Path) -> None:
    shop = Shop(tmp_path)
    session = shop.kernel.start_session(shop.sym.id)
    shop.kernel.shutdown()
    conn = sqlite3.connect(tmp_path / "symbiote.db")
    conn.executescript(
        "ALTER TABLE sessions DROP COLUMN tools_allowed;"
        "ALTER TABLE sessions DROP COLUMN delegation;"
        "ALTER TABLE symbiote_revisions DROP COLUMN is_default;"
        "PRAGMA user_version = 17;"  # the migration before the session scope
    )
    conn.commit()
    conn.close()

    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    try:
        reread = kernel.get_session(session.id)
        assert (reread.tools_allowed, reread.delegation) == (None, "open")
        assert kernel.message(session.id, "hello") == "ok"
    finally:
        kernel.shutdown()


# ── delegation ──────────────────────────────────────────────────────────


def _team(tmp_path: Path, worker_asks=("lookup", "refund")):
    """``boss`` delegates to ``worker``, which then asks for its tools."""
    ran: list[str] = []
    worker_block = "\n".join(tool_call_block(t) for t in worker_asks)

    def script(_i, messages):
        last = str(messages[-1]["content"])
        if "[Tool result: spawn]" in last or "[Tool" in last:
            return "done"
        if "SUBTASK" in last:
            return worker_block
        return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK")

    llm = ScriptedLLM(script)
    kernel = make_kernel(tmp_path, llm)
    boss = kernel.create_symbiote(name="boss", role="front desk")
    worker = kernel.create_symbiote(name="worker", role="orders")
    kernel.environment.configure(symbiote_id=boss.id, tools=["spawn"])
    add_tool(kernel, worker.id, "lookup", lambda _p: ran.append("lookup") or "found")
    add_tool(kernel, worker.id, "refund", lambda _p: ran.append("refund") or "refunded")
    return kernel, llm, boss, worker, ran


def test_the_list_follows_the_delegation(tmp_path: Path) -> None:
    kernel, _llm, boss, worker, ran = _team(tmp_path)
    try:
        session = kernel.start_session(boss.id, tools_allowed=["spawn", "lookup"])

        kernel.message(session.id, "go")

        # The worker is authorized to refund, but this conversation is not.
        assert ran == ["lookup"]
        (sub,) = kernel.inspector.list_sessions(worker.id)
        assert sub["tools_allowed"] == ["lookup", "spawn"]
        blocked = kernel._storage.fetch_all(
            "SELECT tool_id FROM audit_log WHERE session_id = ? AND action = 'blocked'",
            (sub["id"],),
        )
        assert [r["tool_id"] for r in blocked] == ["refund"]
    finally:
        kernel.shutdown()


def test_spawn_itself_must_be_in_the_list(tmp_path: Path) -> None:
    kernel, _llm, boss, worker, ran = _team(tmp_path)
    try:
        session = kernel.start_session(boss.id, tools_allowed=["lookup"])

        (result,) = kernel.turn(session.id, "go").tool_results

        assert result.tool_id == "spawn" and result.success is False
        assert ran == []
        assert kernel.inspector.list_sessions(worker.id) == []
    finally:
        kernel.shutdown()


def test_a_delegation_from_a_session_without_a_list_is_as_before(tmp_path: Path) -> None:
    kernel, _llm, boss, worker, ran = _team(tmp_path)
    try:
        kernel.message(kernel.start_session(boss.id).id, "go")

        assert ran == ["lookup", "refund"]
        (sub,) = kernel.inspector.list_sessions(worker.id)
        assert (sub["tools_allowed"], sub["delegation"]) == (None, "open")
    finally:
        kernel.shutdown()


# ── a call that waited for a person ─────────────────────────────────────


def test_a_deferred_call_runs_under_the_list_the_session_has_by_then(tmp_path: Path) -> None:
    ran: list[str] = []
    llm = ScriptedLLM(_ask("cancel_order"))
    kernel = make_kernel(tmp_path, llm)
    try:
        sym = kernel.create_symbiote(name="Shop", role="assistant")
        kernel.tool_gateway.register_descriptor(
            ToolDescriptor(
                tool_id="cancel_order", name="cancel_order", description="Cancel an order",
                risk_level="high",
            ),
            lambda _p: ran.append("cancel_order") or "cancelled",
        )
        kernel.environment.configure(symbiote_id=sym.id, tools=["cancel_order"])
        kernel.set_approval_callback(lambda *_a: ApprovalDecision.defer("needs the owner"))
        kept = kernel.start_session(sym.id, tools_allowed=["cancel_order"])
        narrowed = kernel.start_session(sym.id, tools_allowed=["cancel_order"])
        kernel.turn(kept.id, "cancel")
        kernel.turn(narrowed.id, "cancel")
        requests = {r["session_id"]: r["id"] for r in kernel.inspector.list_pending(sym.id)}
        kernel.set_session_tools(narrowed.id, [])

        ok = kernel.resolve_pending(requests[kept.id], True)
        refused = kernel.resolve_pending(requests[narrowed.id], True)

        assert ok.status == "executed"
        assert refused.status == "failed" and "blocked" in refused.error
        assert ran == ["cancel_order"]
        assert current_session_scope() is None
    finally:
        kernel.shutdown()


# ── HTTP API ────────────────────────────────────────────────────────────


def test_api_narrows_a_session(api_client) -> None:
    api = api_client(ScriptedLLM("ok"))
    owner = api.auth(api.new_key("tenant-a"))
    stranger = api.auth(api.new_key("tenant-b"))
    sym_id = api.client.post(
        "/symbiotes", json={"name": "Shop", "role": "assistant"}, headers=owner
    ).json()["id"]

    created = api.client.post(
        "/sessions",
        json={"symbiote_id": sym_id, "tools_allowed": ["lookup"], "delegation": "strict"},
        headers=owner,
    )
    assert created.status_code == 201
    session = created.json()
    assert (session["tools_allowed"], session["delegation"]) == (["lookup"], "strict")
    plain = api.client.post("/sessions", json={"symbiote_id": sym_id}, headers=owner).json()
    assert (plain["tools_allowed"], plain["delegation"]) == (None, "open")

    changed = api.client.put(
        f"/sessions/{session['id']}/tools", json={"tools_allowed": []}, headers=owner
    )
    assert changed.json()["tools_allowed"] == []
    lifted = api.client.put(
        f"/sessions/{session['id']}/tools", json={"tools_allowed": None}, headers=owner
    )
    assert lifted.json()["tools_allowed"] is None
    opened = api.client.put(
        f"/sessions/{session['id']}/delegation", json={"delegation": "open"}, headers=owner
    )
    assert opened.json()["delegation"] == "open"
    assert api.client.get(f"/sessions/{session['id']}", headers=owner).json()["delegation"] == "open"

    assert api.client.put(
        f"/sessions/{session['id']}/tools", json={"tools_allowed": ["x"]}, headers=stranger
    ).status_code in (403, 404)
    assert api.client.put(
        f"/sessions/{session['id']}/delegation", json={"delegation": "never"}, headers=owner
    ).status_code == 422
    assert api.client.post(
        "/sessions", json={"symbiote_id": sym_id, "tools_allowed": [""]}, headers=owner
    ).status_code in (400, 422)
