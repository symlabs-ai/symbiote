"""A tool mode per session (B-109).

The tool mode came only from the symbiote's configuration, so changing it
changed every conversation of that symbiote, the ones running at the same
time included. ``kernel.set_session_tool_mode`` gives one session a mode of
its own; ``None`` goes back to the symbiote's.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.core.session_scope import (
    SessionScope,
    bind_session_scope,
    reset_session_scope,
    session_tool_mode_for,
)
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


def _ask_once(_index: int, messages: list[dict]) -> str:
    """Asks for ``lookup`` until a tool result is in the conversation."""
    return "done" if "[Tool" in str(messages[-1]["content"]) else tool_call_block("lookup")


@pytest.fixture()
def shop(tmp_path: Path):
    llm = ScriptedLLM(_ask_once)
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="Shop", role="assistant")
    add_tool(kernel, sym.id, "lookup", lambda _p: "found")
    kernel.environment.configure(symbiote_id=sym.id, tool_mode="brief")
    yield kernel, llm, sym
    kernel.shutdown()


def _calls_of(llm: ScriptedLLM, run) -> int:
    before = len(llm.calls)
    run()
    return len(llm.calls) - before


# ── one session, its own mode ───────────────────────────────────────────


def test_a_session_runs_in_the_mode_it_chose(shop) -> None:
    kernel, llm, sym = shop
    quick = kernel.start_session(sym.id)
    usual = kernel.start_session(sym.id)

    updated = kernel.set_session_tool_mode(quick.id, "instant")

    assert updated.tool_mode == "instant"
    outcomes = {}
    calls = {
        name: _calls_of(llm, lambda s=s, n=name: outcomes.update({n: kernel.turn(s.id, "go")}))
        for name, s in (("quick", quick), ("usual", usual))
    }
    # instant: one call, the action, no reading; brief: the loop reads the result
    assert calls == {"quick": 1, "usual": 2}
    assert (outcomes["quick"].tool_mode, outcomes["usual"].tool_mode) == ("instant", "brief")
    assert kernel.environment.get_config(sym.id).tool_mode == "brief"


def test_none_goes_back_to_the_mode_of_the_symbiote(shop) -> None:
    kernel, llm, sym = shop
    session = kernel.start_session(sym.id)
    kernel.set_session_tool_mode(session.id, "instant")

    back = kernel.set_session_tool_mode(session.id, None)

    assert back.tool_mode is None
    assert _calls_of(llm, lambda: kernel.turn(session.id, "go")) == 2
    assert kernel.get_session(session.id).tool_mode is None


def test_a_new_session_starts_in_the_mode_of_the_symbiote(shop) -> None:
    kernel, _llm, sym = shop
    kernel.set_session_tool_mode(kernel.start_session(sym.id).id, "instant")

    assert kernel.start_session(sym.id).tool_mode is None


def test_the_trace_and_the_score_carry_the_mode_the_turn_ran_in(shop) -> None:
    kernel, _llm, sym = shop
    session = kernel.start_session(sym.id)
    kernel.set_session_tool_mode(session.id, "instant")

    kernel.turn(session.id, "go")
    kernel.close_session(session.id)

    trace = kernel.inspector.list_traces(session_id=session.id)[0]
    assert (trace["tool_mode"], trace["stop_reason"]) == ("instant", "end_turn")
    # instant scoring: one successful action is a full score
    assert kernel.inspector.list_scores(session_id=session.id)[0]["auto_score"] == 1.0


def test_auto_is_resolved_at_every_turn(shop) -> None:
    kernel, _llm, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, tool_mode="instant")
    with_tools = kernel.start_session(sym.id)
    without = kernel.start_session(sym.id, tools_allowed=[])
    for session in (with_tools, without):
        kernel.set_session_tool_mode(session.id, "auto")

    assert kernel.turn(with_tools.id, "go").tool_mode == "brief"
    # no tool left for this session: auto means instant
    assert kernel.turn(without.id, "go").tool_mode == "instant"


def test_two_sessions_of_the_same_symbiote_run_at_once_in_different_modes(shop) -> None:
    kernel, _llm, sym = shop
    quick = kernel.start_session(sym.id)
    usual = kernel.start_session(sym.id)
    kernel.set_session_tool_mode(quick.id, "instant")

    async def both():
        return await asyncio.gather(
            kernel.turn_async(quick.id, "go"), kernel.turn_async(usual.id, "go")
        )

    first, second = asyncio.run(both())

    assert (first.tool_mode, second.tool_mode) == ("instant", "brief")


def test_the_mode_is_only_for_the_symbiote_of_the_session() -> None:
    token = bind_session_scope(
        SessionScope(symbiote_id="owner", session_id="s1", tool_mode="long_run")
    )
    try:
        assert session_tool_mode_for("owner") == "long_run"
        # a colleague called through spawn works in its own mode
        assert session_tool_mode_for("colleague") is None
    finally:
        reset_session_scope(token)
    assert session_tool_mode_for("owner") is None


# ── refusals ────────────────────────────────────────────────────────────


def test_an_unknown_mode_or_session_is_refused(shop) -> None:
    kernel, _llm, sym = shop
    session = kernel.start_session(sym.id)

    with pytest.raises(ValidationError, match="tool_mode must be one of"):
        kernel.set_session_tool_mode(session.id, "turbo")
    with pytest.raises(EntityNotFoundError):
        kernel.set_session_tool_mode("nope", "brief")
    assert kernel.get_session(session.id).tool_mode is None


# ── reading it back, over HTTP and through the SDK ──────────────────────


def test_the_inspector_returns_the_mode(shop) -> None:
    kernel, _llm, sym = shop
    chosen = kernel.start_session(sym.id)
    plain = kernel.start_session(sym.id)
    kernel.set_session_tool_mode(chosen.id, "long_run")

    modes = {s["id"]: s["tool_mode"] for s in kernel.inspector.list_sessions(sym.id)}

    assert modes == {chosen.id: "long_run", plain.id: None}


def test_the_endpoint_sets_and_clears_the_mode(api_client) -> None:
    api = api_client(llm=ScriptedLLM("ok"))
    h = api.auth(api.new_key("tenant-a"))
    sym = api.client.post("/symbiotes", headers=h, json={"name": "a", "role": "r"}).json()
    sid = api.client.post("/sessions", headers=h, json={"symbiote_id": sym["id"]}).json()["id"]

    chosen = api.client.put(f"/sessions/{sid}/tool-mode", headers=h, json={"tool_mode": "instant"})
    read = api.client.get(f"/sessions/{sid}", headers=h)
    bad = api.client.put(f"/sessions/{sid}/tool-mode", headers=h, json={"tool_mode": "turbo"})
    cleared = api.client.put(f"/sessions/{sid}/tool-mode", headers=h, json={"tool_mode": None})

    assert (chosen.status_code, chosen.json()["tool_mode"]) == (200, "instant")
    assert read.json()["tool_mode"] == "instant"
    assert bad.status_code == 422
    assert (cleared.status_code, cleared.json()["tool_mode"]) == (200, None)


def test_the_sdk_sends_the_mode(monkeypatch) -> None:
    from symbiote.sdk import SymbioteClient

    sent: list[tuple[str, dict]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {}

    client = SymbioteClient(base_url="http://test", api_key="k")
    monkeypatch.setattr(
        client._client, "put", lambda path, json: sent.append((path, json)) or _Response()
    )

    client.set_session_tool_mode("s1", "brief")
    client.set_session_tool_mode("s1", None)

    assert sent == [
        ("/sessions/s1/tool-mode", {"tool_mode": "brief"}),
        ("/sessions/s1/tool-mode", {"tool_mode": None}),
    ]
