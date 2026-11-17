"""The summary of a closed session, by the symbiote's summary mode (B-87).

The default joins messages into a paragraph. ``session_summary_mode="llm"``
has the auxiliary model write a real summary; ``"off"`` stores none.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import ScriptedLLM, make_kernel

QUESTION = "Please cancel order 4471, it arrived broken."
ANSWER = "Order 4471 is cancelled and the refund was requested."
WRITTEN = "The customer asked to cancel a broken order; it was cancelled and refunded."


def _is_summary_call(messages: list[dict]) -> bool:
    return "Summarize the conversation below" in str(messages[-1]["content"])


@pytest.fixture()
def shop(tmp_path: Path):
    main = ScriptedLLM(ANSWER)
    aux = ScriptedLLM(lambda _i, m: WRITTEN if _is_summary_call(m) else "[]")
    kernel = make_kernel(tmp_path, main)
    kernel.set_evolver_llm(aux)
    sym = kernel.create_symbiote(name="Shop", role="assistant")
    yield kernel, main, aux, sym
    kernel.shutdown()


def _closed(kernel, symbiote_id: str, **start):
    session = kernel.start_session(symbiote_id, **start)
    kernel.message(session.id, QUESTION)
    return kernel.close_session(session.id)


def test_default_summary_is_the_concatenation_and_calls_no_model(shop) -> None:
    kernel, main, aux, sym = shop

    closed = _closed(kernel, sym.id)

    assert closed.summary == f"{QUESTION}\n{ANSWER}"
    assert aux.calls == []
    assert len(main.calls) == 1


def test_llm_mode_stores_a_summary_written_by_the_auxiliary_model(shop) -> None:
    kernel, main, aux, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, session_summary_mode="llm")

    closed = _closed(kernel, sym.id)

    assert closed.summary == WRITTEN
    assert kernel.get_session(closed.id).summary == WRITTEN
    summary_calls = [c for c in aux.calls if _is_summary_call(c["messages"])]
    assert len(summary_calls) == 1  # written once, shared with the reflection
    prompt = summary_calls[0]["messages"][-1]["content"]
    assert QUESTION in prompt
    assert ANSWER in prompt
    assert len(main.calls) == 1  # the turn only


def test_llm_mode_without_an_auxiliary_model_uses_the_main_one(tmp_path: Path) -> None:
    main = ScriptedLLM(lambda _i, m: WRITTEN if _is_summary_call(m) else ANSWER)
    kernel = make_kernel(tmp_path, main)
    try:
        sym = kernel.create_symbiote(name="Shop", role="assistant")
        kernel.environment.configure(symbiote_id=sym.id, session_summary_mode="llm")

        assert _closed(kernel, sym.id).summary == WRITTEN
    finally:
        kernel.shutdown()


@pytest.mark.parametrize("failure", ["raises", "empty"])
def test_llm_mode_falls_back_to_the_concatenation(shop, failure) -> None:
    kernel, _main, aux, sym = shop

    def broken(_i, messages):
        if not _is_summary_call(messages):
            return "[]"
        if failure == "raises":
            raise RuntimeError("model down")
        return "   "

    aux._fn = broken
    kernel.environment.configure(symbiote_id=sym.id, session_summary_mode="llm")

    closed = _closed(kernel, sym.id)

    assert QUESTION in closed.summary
    assert ANSWER in closed.summary
    assert closed.status == "closed"


def test_off_mode_stores_no_summary_and_calls_no_model(shop) -> None:
    kernel, main, aux, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, session_summary_mode="off")

    closed = _closed(kernel, sym.id)

    assert closed.summary is None
    assert kernel.get_session(closed.id).summary is None
    assert aux.calls == []
    assert len(main.calls) == 1


def test_reflection_result_carries_the_same_summary(shop) -> None:
    kernel, _main, _aux, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, session_summary_mode="llm")
    session = kernel.start_session(sym.id)
    kernel.message(session.id, QUESTION)

    result = kernel._reflection_for(sym.id).reflect_session(session.id, sym.id)

    assert result.summary == WRITTEN


def test_summary_of_an_end_users_session_reads_only_that_session(shop) -> None:
    kernel, _main, aux, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, session_summary_mode="llm")
    other = kernel.start_session(sym.id, subject_key="bia")
    kernel.message(other.id, "My secret address is 12 Elm Street, do not share it.")

    _closed(kernel, sym.id, subject_key="ana")

    (call,) = [c for c in aux.calls if _is_summary_call(c["messages"])]
    assert "Elm Street" not in call["messages"][-1]["content"]


def test_mode_is_exposed_by_the_config_api(api_client) -> None:
    api = api_client(ScriptedLLM("ok"))
    headers = api.auth(api.admin_key)
    sym_id = api.client.post(
        "/symbiotes", json={"name": "S", "role": "assistant"}, headers=headers
    ).json()["id"]

    before = api.client.get(f"/symbiotes/{sym_id}/config", headers=headers).json()
    after = api.client.put(
        f"/symbiotes/{sym_id}/config", json={"session_summary_mode": "off"}, headers=headers
    ).json()

    assert before["session_summary_mode"] == "concat"
    assert after["session_summary_mode"] == "off"
