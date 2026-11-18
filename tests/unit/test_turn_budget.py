"""Spending ceiling of a turn (B-36, closing part).

``max_tool_iterations`` limits the rounds of a turn, not what they cost.
``max_turn_llm_calls`` and ``max_turn_tokens`` stop a turn that reached its
ceiling: no further model call starts and the turn ends with
``stop_reason="budget"``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from symbiote.core.llm_usage import report_usage
from symbiote.core.turn_result import STOP_REASONS
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


class Metered(ScriptedLLM):
    """Reports 100 tokens per call."""

    def complete(self, messages, config=None, tools=None):
        report_usage(80, 20)
        return super().complete(messages, config, tools)


def _always_calls_lookup(i, _messages):
    # A different call each round, or the loop would stop for stagnation.
    return tool_call_block("lookup", n=i)


@pytest.fixture()
def shop(tmp_path: Path):
    llm = Metered(_always_calls_lookup)
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="Shop", role="assistant")
    add_tool(kernel, sym.id, "lookup", lambda _p: "found", max_tool_iterations=10)
    yield kernel, llm, sym
    kernel.shutdown()


def test_no_ceiling_by_default(shop) -> None:
    kernel, llm, sym = shop
    cfg = kernel.environment.get_config(sym.id)

    outcome = kernel.turn(kernel.start_session(sym.id).id, "go")

    assert (cfg.max_turn_tokens, cfg.max_turn_llm_calls) == (0, 0)
    assert outcome.stop_reason != "budget"
    assert len(llm.calls) > 3


def test_a_ceiling_on_model_calls_stops_the_turn(shop) -> None:
    kernel, llm, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, max_turn_llm_calls=3)

    outcome = kernel.turn(kernel.start_session(sym.id).id, "go")

    assert outcome.stop_reason == "budget"
    assert outcome.stopped_early is True
    assert len(llm.calls) == 3  # no extra call to write an answer
    assert outcome.usage.llm_calls == 3
    assert outcome.tool_calls == 3  # what the model asked for still ran
    assert "limite de uso" in outcome.text
    assert "budget" in STOP_REASONS


def test_a_ceiling_on_tokens_stops_the_turn(shop) -> None:
    kernel, llm, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, max_turn_tokens=250)

    outcome = kernel.turn(kernel.start_session(sym.id).id, "go")

    # 100 tokens a call: the third call starts under the ceiling and passes it.
    assert outcome.stop_reason == "budget"
    assert len(llm.calls) == 3
    assert outcome.usage.total_tokens == 300


def test_a_turn_under_the_ceiling_is_untouched(shop) -> None:
    kernel, llm, sym = shop
    llm._fn = lambda i, _m: tool_call_block("lookup") if i == 0 else "done"
    kernel.environment.configure(
        symbiote_id=sym.id, max_turn_llm_calls=5, max_turn_tokens=10_000
    )

    outcome = kernel.turn(kernel.start_session(sym.id).id, "go")

    assert (outcome.stop_reason, outcome.text) == ("end_turn", "done")


def test_the_first_call_always_runs(shop) -> None:
    kernel, llm, sym = shop
    llm._fn = lambda _i, _m: "hello"
    kernel.environment.configure(symbiote_id=sym.id, max_turn_tokens=1)

    outcome = kernel.turn(kernel.start_session(sym.id).id, "go")

    assert (outcome.text, outcome.stop_reason) == ("hello", "end_turn")


def test_tokens_ceiling_needs_an_adapter_that_reports_usage(tmp_path: Path) -> None:
    """With no usage reported there is nothing to count: the ceiling on
    calls is the one that always works."""
    kernel = make_kernel(tmp_path, ScriptedLLM(_always_calls_lookup))
    try:
        sym = kernel.create_symbiote(name="S", role="assistant")
        add_tool(kernel, sym.id, "lookup", lambda _p: "found", max_tool_iterations=4)
        kernel.environment.configure(symbiote_id=sym.id, max_turn_tokens=1)

        outcome = kernel.turn(kernel.start_session(sym.id).id, "go")
        assert outcome.stop_reason != "budget"

        kernel.environment.configure(symbiote_id=sym.id, max_turn_llm_calls=2)
        outcome = kernel.turn(kernel.start_session(sym.id).id, "go")
        assert outcome.stop_reason == "budget"
    finally:
        kernel.shutdown()


def test_what_a_delegated_symbiote_spends_counts_for_the_turn(tmp_path: Path) -> None:
    def script(i, messages):
        last = str(messages[-1]["content"])
        if "SUBTASK" in last or "[Tool result: lookup]" in last:
            return tool_call_block("lookup", n=i)  # the worker never finishes by itself
        return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK")

    llm = Metered(script)
    kernel = make_kernel(tmp_path, llm)
    try:
        boss = kernel.create_symbiote(name="boss", role="front desk")
        worker = kernel.create_symbiote(name="worker", role="orders")
        kernel.environment.configure(
            symbiote_id=boss.id, tools=["spawn"], max_turn_llm_calls=4
        )
        add_tool(kernel, worker.id, "lookup", lambda _p: "found", max_tool_iterations=3)

        outcome = kernel.turn(kernel.start_session(boss.id).id, "go")

        # The boss made one call; the worker, which has no ceiling of its own,
        # made four (three rounds and the answer after max_iterations). Back in
        # the boss's loop the turn is past its ceiling of 4: no second call.
        assert outcome.stop_reason == "budget"
        assert outcome.usage.llm_calls == 5
        assert kernel.inspector.usage_totals(boss.id)["llm_calls"] == 1
    finally:
        kernel.shutdown()


def test_the_ceiling_is_per_turn_not_per_session(shop) -> None:
    kernel, llm, sym = shop
    llm._fn = lambda _i, _m: "hello"
    kernel.environment.configure(symbiote_id=sym.id, max_turn_llm_calls=1)
    session = kernel.start_session(sym.id)

    first = kernel.turn(session.id, "one")
    second = kernel.turn(session.id, "two")

    assert (first.stop_reason, second.stop_reason) == ("end_turn", "end_turn")


def test_async_turn_stops_at_the_ceiling_too(shop) -> None:
    kernel, llm, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, max_turn_llm_calls=2)
    session = kernel.start_session(sym.id)

    outcome = asyncio.run(kernel.turn_async(session.id, "go"))

    assert outcome.stop_reason == "budget"
    assert len(llm.calls) == 2


def test_a_long_run_project_stops_starting_blocks_at_the_ceiling(tmp_path: Path) -> None:
    plan = json.dumps([
        {"name": name, "description": "d", "success_criteria": "c"} for name in "ABC"
    ])

    def script(_i, messages):
        system = str(messages[0]["content"])
        if system.startswith("PLANNER"):
            return plan
        if system.startswith("You ran a multi-step project"):
            return "ANSWER"
        return "block done"

    llm = Metered(script)
    kernel = make_kernel(tmp_path, llm)
    try:
        sym = kernel.create_symbiote(name="a", role="assistant")
        kernel.environment.configure(
            symbiote_id=sym.id, tool_mode="long_run", planner_prompt="PLANNER: plan",
            max_turn_llm_calls=3,  # the plan and two blocks
        )
        session = kernel.start_session(sym.id)

        outcome = kernel.turn(session.id, "build it")

        assert outcome.stop_reason == "budget"
        assert len(llm.calls) == 3  # no third block, no call to write the reply
        assert kernel._handoffs[session.id]["blocks_completed"] == 2
    finally:
        kernel.shutdown()


def test_ceiling_is_stored_and_exposed_by_the_config_api(api_client) -> None:
    api = api_client(ScriptedLLM("ok"))
    headers = api.auth(api.admin_key)
    sym_id = api.client.post(
        "/symbiotes", json={"name": "S", "role": "assistant"}, headers=headers
    ).json()["id"]

    before = api.client.get(f"/symbiotes/{sym_id}/config", headers=headers).json()
    after = api.client.put(
        f"/symbiotes/{sym_id}/config",
        json={"max_turn_tokens": 5000, "max_turn_llm_calls": 8},
        headers=headers,
    ).json()
    cleared = api.client.put(
        f"/symbiotes/{sym_id}/config", json={"max_turn_tokens": 0}, headers=headers
    ).json()

    assert (before["max_turn_tokens"], before["max_turn_llm_calls"]) == (0, 0)
    assert (after["max_turn_tokens"], after["max_turn_llm_calls"]) == (5000, 8)
    assert (cleared["max_turn_tokens"], cleared["max_turn_llm_calls"]) == (0, 8)
