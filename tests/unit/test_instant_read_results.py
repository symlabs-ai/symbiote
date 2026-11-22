"""Instant mode that reads the results of its actions before answering (B-108).

In instant mode the turn makes one model call and runs the actions it asks
for. When they all succeed the answer is the text of that call, written
before any result existed: right for a command, wrong for a query. With
``instant_read_results`` the model is called once more, without tools, to
read the results and answer; the turn still ends as ``end_turn``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import _MIGRATIONS, SCHEMA_VERSION, SQLiteAdapter
from symbiote.runners.chat import INSTANT_RESULTS_MSG
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block

NARRATION = "Good morning! Let me pull your numbers for the week."
ASK = f"{NARRATION}\n{tool_call_block('sales')}"
ANSWER = "You sold 57 units this week."


def _script(index: int, _messages: list[dict]) -> str:
    return ASK if index == 0 else ANSWER


class Desk:
    """A symbiote in instant mode with one tool, ``sales``."""

    def __init__(self, tmp_path: Path, script=_script, *, handler=None, **configure) -> None:
        self.llm = ScriptedLLM(script)
        self.kernel = make_kernel(tmp_path, self.llm)
        self.sym = self.kernel.create_symbiote(name="Desk", role="assistant")
        self.ran = 0

        def sales(_params: dict) -> str:
            self.ran += 1
            return "sales_week: 57 units"

        add_tool(self.kernel, self.sym.id, "sales", handler or sales)
        self.kernel.environment.configure(
            symbiote_id=self.sym.id, tool_mode="instant", **configure
        )
        self.session = self.kernel.start_session(self.sym.id)


@pytest.fixture()
def desk(tmp_path: Path):
    made: list[Desk] = []

    def make(script=_script, **kw) -> Desk:
        made.append(Desk(tmp_path / f"d{len(made)}", script, **kw))
        return made[-1]

    yield make
    for d in made:
        d.kernel.shutdown()


# ── off, the default: as before ─────────────────────────────────────────


def test_by_default_the_turn_ends_with_the_text_written_before_the_results(desk) -> None:
    d = desk()

    outcome = d.kernel.turn(d.session.id, "how was my week?")

    assert outcome.text == NARRATION
    assert (d.ran, len(d.llm.calls)) == (1, 1)
    assert (outcome.stop_reason, outcome.tool_mode) == ("end_turn", "instant")
    assert d.kernel.environment.get_config(d.sym.id).instant_read_results is False


# ── on: one more call, without tools, that reads the results ────────────


def test_with_the_option_the_model_reads_the_results_and_answers(desk) -> None:
    d = desk(instant_read_results=True)

    outcome = d.kernel.turn(d.session.id, "how was my week?")

    assert outcome.text == ANSWER
    assert (d.ran, len(d.llm.calls)) == (1, 2)
    read = d.llm.calls[1]
    assert read["tools"] is None
    assert "sales_week: 57 units" in d.llm.prompt_text(1)
    assert read["messages"][-1] == {"role": "user", "content": INSTANT_RESULTS_MSG}
    assert [r.tool_id for r in outcome.tool_results] == ["sales"]


def test_the_turn_still_ends_as_end_turn_and_scores_like_a_good_instant_turn(desk) -> None:
    d = desk(instant_read_results=True)

    outcome = d.kernel.turn(d.session.id, "how was my week?")
    d.kernel.close_session(d.session.id)

    assert (outcome.stop_reason, outcome.tool_mode, outcome.iterations) == ("end_turn", "instant", 1)
    trace = d.kernel.inspector.list_traces(session_id=d.session.id)[0]
    assert (trace["stop_reason"], trace["tool_mode"]) == ("end_turn", "instant")
    score = d.kernel.inspector.list_scores(session_id=d.session.id)[0]
    assert score["auto_score"] == 1.0
    failures = d.kernel._storage.fetch_all(
        "SELECT id FROM memory_entries WHERE session_id = ? AND type = 'procedural'",
        (d.session.id,),
    )
    assert failures == []


def test_the_answer_is_what_is_stored_as_the_reply(desk) -> None:
    d = desk(instant_read_results=True)

    d.kernel.turn(d.session.id, "how was my week?")

    messages = d.kernel.inspector.list_messages(d.session.id)
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "how was my week?"),
        ("assistant", ANSWER),
    ]


def test_the_host_replaces_the_instruction(desk) -> None:
    d = desk(instant_read_results=True)
    d.kernel.harness_versions.create_version(
        d.sym.id, "injection_instant_results", "Leia os resultados e responda ao usuário."
    )

    d.kernel.turn(d.session.id, "como foi minha semana?")

    assert d.llm.calls[1]["messages"][-1]["content"] == "Leia os resultados e responda ao usuário."


def test_a_turn_without_actions_makes_no_second_call(desk) -> None:
    d = desk(lambda _i, _m: "Hello there.", instant_read_results=True)

    outcome = d.kernel.turn(d.session.id, "hi")

    assert (outcome.text, len(d.llm.calls)) == ("Hello there.", 1)


def test_when_the_reading_call_fails_the_first_text_stays(desk) -> None:
    def script(index: int, _messages: list[dict]) -> str:
        if index == 0:
            return ASK
        raise ValueError("model unavailable")

    d = desk(script, instant_read_results=True)

    outcome = d.kernel.turn(d.session.id, "how was my week?")

    assert (outcome.text, outcome.stop_reason) == (NARRATION, "end_turn")


def test_a_reply_that_asks_for_more_tools_is_not_taken_for_the_answer(desk) -> None:
    d = desk(lambda _i, _m: ASK, instant_read_results=True)

    outcome = d.kernel.turn(d.session.id, "how was my week?")

    assert outcome.text == NARRATION
    assert (d.ran, len(d.llm.calls)) == (1, 2)


def test_a_failed_action_goes_back_to_the_model_as_before(desk) -> None:
    def broken(_params: dict) -> str:
        raise RuntimeError("database offline")

    for option in (False, True):
        d = desk(handler=broken, instant_read_results=option)

        outcome = d.kernel.turn(d.session.id, "how was my week?")

        assert outcome.text == ANSWER
        assert len(d.llm.calls) == 2
        assert d.llm.calls[1]["messages"][-1]["content"] != INSTANT_RESULTS_MSG


def test_the_hosts_hook_still_decides_the_text(desk) -> None:
    d = desk(instant_read_results=True)
    runner = d.kernel._runner_registry.get("chat")
    runner._on_after_tool_result = lambda _calls, _results: "Done by the host."

    outcome = d.kernel.turn(d.session.id, "how was my week?")

    assert (outcome.text, len(d.llm.calls)) == ("Done by the host.", 1)


def test_the_async_turn_reads_the_results_too(desk) -> None:
    d = desk(instant_read_results=True)
    tokens: list[str] = []

    outcome = asyncio.run(
        d.kernel.turn_async(d.session.id, "how was my week?", on_token=tokens.append)
    )

    assert outcome.text == ANSWER
    assert "".join(tokens) == ANSWER


def test_the_option_does_nothing_outside_instant_mode(desk) -> None:
    d = desk(lambda i, _m: tool_call_block("sales") if i == 0 else ANSWER, instant_read_results=True)
    d.kernel.environment.configure(symbiote_id=d.sym.id, tool_mode="brief")

    outcome = d.kernel.turn(d.session.id, "how was my week?")

    assert (outcome.text, outcome.tool_mode) == (ANSWER, "brief")
    assert all(INSTANT_RESULTS_MSG not in d.llm.prompt_text(i) for i in range(len(d.llm.calls)))


# ── storage and HTTP ────────────────────────────────────────────────────


@pytest.mark.sqlite_only
def test_a_database_without_the_columns_gets_them(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(symbiote_id=sym.id, tool_mode="instant")
    session = kernel.start_session(sym.id)
    kernel.shutdown()

    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE environment_configs DROP COLUMN instant_read_results")
    conn.execute("ALTER TABLE sessions DROP COLUMN tool_mode")
    conn.execute(f"PRAGMA user_version = {_MIGRATIONS.index('_migrate_turn_modes')}")
    conn.commit()
    conn.close()

    adapter = SQLiteAdapter(db)
    adapter.init_schema()
    config = adapter.fetch_one(
        "SELECT instant_read_results FROM environment_configs WHERE symbiote_id = ?", (sym.id,)
    )
    row = adapter.fetch_one("SELECT tool_mode FROM sessions WHERE id = ?", (session.id,))
    assert (config, row) == ({"instant_read_results": 0}, {"tool_mode": None})
    assert adapter._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    adapter.close()


def test_the_config_endpoint_takes_and_returns_the_option(api_client) -> None:
    api = api_client(llm=ScriptedLLM("ok"))
    h = api.auth(api.new_key("tenant-a", role="admin"))
    sym = api.client.post("/symbiotes", headers=h, json={"name": "a", "role": "r"}).json()

    before = api.client.get(f"/symbiotes/{sym['id']}/config", headers=h).json()
    put = api.client.put(
        f"/symbiotes/{sym['id']}/config",
        headers=h,
        json={"tool_mode": "instant", "instant_read_results": True},
    )
    after = api.client.get(f"/symbiotes/{sym['id']}/config", headers=h).json()

    assert put.status_code == 200, put.text
    assert (before["instant_read_results"], after["instant_read_results"]) == (False, True)
