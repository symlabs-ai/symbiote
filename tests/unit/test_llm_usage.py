"""Every model call says who it is for and is recorded with its tokens
(B-36, extended).

The kernel's models are metered: each call is stored in ``llm_usage`` with
the symbiote, the session and the purpose, and the tokens the adapter
reported. Nothing here needs a gateway: the counts come from the adapter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from symbiote.adapters.llm.symgateway import SymGatewayLLMAdapter
from symbiote.core.llm_usage import (
    LLMCallContext,
    LLMCallRecord,
    MeteredLLM,
    current_llm_call,
    report_usage,
)
from symbiote.core.models import MemoryEntry
from symbiote.environment.descriptors import (
    LLMModelIdentity,
    LLMResponse,
    LLMTokenUsage,
)
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


class CountingLLM(ScriptedLLM):
    """Scripted model that answers with token counts and notes, per call,
    what ``current_llm_call()`` said."""

    def __init__(self, fn="ok", *, prompt_tokens: int = 10, completion_tokens: int = 5,
                 provider: str = "acme", model: str = "acme-1") -> None:
        super().__init__(fn)
        self.contexts: list[LLMCallContext | None] = []
        self._usage = (prompt_tokens, completion_tokens)
        self._identity = LLMModelIdentity(provider=provider, model=model)

    def complete(self, messages, config=None, tools=None) -> Any:
        self.contexts.append(current_llm_call())
        text = super().complete(messages, config, tools)
        prompt, completion = self._usage
        # Plain text (so text tool calls are parsed) with the tokens reported.
        report_usage(
            prompt, completion,
            provider=self._identity.provider, model=self._identity.model,
        )
        return text

    def purposes(self) -> list[str]:
        return [c.purpose if c else "none" for c in self.contexts]


def _rows(kernel, **filters) -> list[dict]:
    return list(reversed(kernel.inspector.list_llm_calls(**filters)))


@pytest.fixture()
def shop(tmp_path: Path):
    main = CountingLLM("ok")
    aux = CountingLLM("[]", prompt_tokens=100, completion_tokens=1, model="acme-mini")
    kernel = make_kernel(tmp_path, main)
    kernel.set_evolver_llm(aux)
    sym = kernel.create_symbiote(name="Shop", role="assistant")
    yield kernel, main, aux, sym
    kernel.shutdown()


# ── who the call is for ─────────────────────────────────────────────────


def test_a_turn_is_recorded_with_symbiote_session_purpose_and_tokens(shop) -> None:
    kernel, main, _aux, sym = shop
    session = kernel.start_session(sym.id)

    kernel.message(session.id, "hello")

    assert main.contexts == [
        LLMCallContext(
            purpose="turn", symbiote_id=sym.id, session_id=session.id,
            root_session_id=session.id,
        )
    ]
    (row,) = _rows(kernel, symbiote_id=sym.id)
    assert (row["purpose"], row["session_id"], row["root_session_id"]) == (
        "turn", session.id, session.id,
    )
    assert (row["provider"], row["model"]) == ("acme", "acme-1")
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (10, 5, 15)
    assert row["success"] == 1
    assert current_llm_call() is None


def test_calls_of_a_delegated_symbiote_are_subagent_calls_of_the_root_session(
    tmp_path: Path,
) -> None:
    def script(_i, messages):
        last = str(messages[-1]["content"])
        if "[Tool result: spawn]" in last:
            return "done"
        if "SUBTASK" in last:
            return "worker answer"
        return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK")

    llm = CountingLLM(script)
    kernel = make_kernel(tmp_path, llm)
    try:
        boss = kernel.create_symbiote(name="boss", role="front desk")
        worker = kernel.create_symbiote(name="worker", role="orders")
        kernel.environment.configure(symbiote_id=boss.id, tools=["spawn"])
        session = kernel.start_session(boss.id)

        outcome = kernel.turn(session.id, "go")

        assert llm.purposes() == ["turn", "subagent", "turn"]
        (sub,) = _rows(kernel, symbiote_id=worker.id)
        assert sub["purpose"] == "subagent"
        assert sub["session_id"] != session.id
        assert sub["root_session_id"] == session.id
        # The turn's totals include what it delegated.
        assert outcome.usage.llm_calls == 3
        assert outcome.usage.total_tokens == 45
        whole = kernel.inspector.usage_totals(root_session_id=session.id)
        assert (whole["llm_calls"], whole["total_tokens"]) == (3, 45)
        assert kernel.inspector.usage_totals(session_id=session.id)["llm_calls"] == 2
    finally:
        kernel.shutdown()


def test_reflection_and_summary_at_close_are_attributed(shop) -> None:
    kernel, main, aux, sym = shop
    kernel.environment.configure(
        symbiote_id=sym.id, reflection_mode="llm", session_summary_mode="llm"
    )
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "Please remember that I prefer short answers.")

    kernel.close_session(session.id)

    assert sorted(aux.purposes()) == ["reflection", "session_summary"]
    assert {c.symbiote_id for c in aux.contexts} == {sym.id}
    assert {c.session_id for c in aux.contexts} == {session.id}
    by_purpose = {
        r["purpose"]: r for r in kernel.inspector.usage_by("purpose", sym.id)
    }
    assert set(by_purpose) == {"turn", "reflection", "session_summary"}
    assert by_purpose["reflection"]["total_tokens"] == 101
    assert main.purposes() == ["turn"]


def test_skill_review_runs_in_its_own_thread_and_is_still_attributed(shop) -> None:
    kernel, _main, aux, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, skill_review_enabled=True)
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "we fixed the cache by clearing it")
    kernel.close_session(session.id)
    kernel.shutdown()  # waits for the review thread

    (context,) = [c for c in aux.contexts if c and c.purpose == "skill_review"]
    assert (context.symbiote_id, context.session_id) == (sym.id, session.id)


def test_dream_calls_carry_the_phase(shop) -> None:
    kernel, _main, aux, sym = shop
    kernel.environment.configure(symbiote_id=sym.id, dream_mode="full")
    for text in ("run the tests before deploy", "check migrations before deploy",
                 "announce the deploy in the channel"):
        kernel.memory.store(MemoryEntry(
            symbiote_id=sym.id, type="procedural", scope="global", source="reflection",
            content=text, tags=["deploy", "checklist"], importance=0.6,
        ))
    aux._fn = lambda _i, _m: "Always check the deploy checklist."

    kernel.dream(sym.id)

    assert aux.purposes()
    assert all(p.startswith("dream:") for p in aux.purposes())
    assert {c.symbiote_id for c in aux.contexts} == {sym.id}
    assert {c.session_id for c in aux.contexts} == {None}
    family = kernel.inspector.usage_totals(sym.id, purpose="dream")
    assert family["llm_calls"] == len(aux.calls)


def test_long_run_planner_evaluator_and_report_have_their_own_purposes(tmp_path: Path) -> None:
    plan = json.dumps([{"name": "one", "description": "do it", "success_criteria": "done"}])

    def script(_i, messages):
        system = str(messages[0]["content"])
        if system.startswith("PLANNER"):
            return plan
        if system.startswith("EVAL"):
            return json.dumps(
                {"passed": True, "overall_score": 1.0, "feedback": "ok", "blocking_issues": []}
            )
        if system.startswith("You ran a multi-step project"):
            return "ANSWER"
        return "block done"

    llm = CountingLLM(script)
    kernel = make_kernel(tmp_path, llm)
    try:
        sym = kernel.create_symbiote(name="a", role="assistant")
        kernel.environment.configure(
            symbiote_id=sym.id, tool_mode="long_run",
            planner_prompt="PLANNER: decompose", evaluator_prompt="EVAL",
        )
        session = kernel.start_session(sym.id)

        kernel.message(session.id, "build it")

        purposes = llm.purposes()
        assert purposes[0] == "long_run:plan"
        assert "long_run:evaluate" in purposes
        assert purposes[-1] == "long_run:report"
        assert "turn" in purposes  # the block itself
        assert {c.session_id for c in llm.contexts} == {session.id}
        assert kernel.inspector.usage_totals(sym.id, purpose="long_run")["llm_calls"] >= 3
    finally:
        kernel.shutdown()


def test_semantic_tool_resolution_is_attributed_to_the_turn(shop) -> None:
    kernel, _main, _aux, sym = shop
    resolver = CountingLLM('["orders"]', model="acme-tags")
    kernel.set_semantic_llm(resolver)
    add_tool(kernel, sym.id, "lookup", lambda _p: "found", tool_loading="semantic")
    session = kernel.start_session(sym.id)

    kernel.message(session.id, "where is my order?")

    if resolver.contexts:  # only called when there are tags to choose from
        assert resolver.purposes() == ["tool_resolution"]
        assert resolver.contexts[0].session_id == session.id


# ── what the call cost ──────────────────────────────────────────────────


def test_usage_on_the_llm_response_is_recorded(tmp_path: Path) -> None:
    class Native(ScriptedLLM):
        def complete(self, messages, config=None, tools=None):
            return LLMResponse(
                content=super().complete(messages, config, tools),
                usage=LLMTokenUsage(prompt_tokens=8, completion_tokens=4),
                model_identity=LLMModelIdentity(provider="acme", model="acme-native"),
            )

    kernel = make_kernel(tmp_path, Native("ok"))
    try:
        sym = kernel.create_symbiote(name="a", role="r")
        outcome = kernel.turn(kernel.start_session(sym.id).id, "hello")

        (row,) = _rows(kernel, symbiote_id=sym.id)
        assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (8, 4, 12)
        assert (row["provider"], row["model"]) == ("acme", "acme-native")
        assert outcome.usage.total_tokens == 12
    finally:
        kernel.shutdown()


def test_plain_text_adapter_is_recorded_without_token_counts(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    try:
        sym = kernel.create_symbiote(name="a", role="r")
        outcome = kernel.turn(kernel.start_session(sym.id).id, "hello")

        (row,) = _rows(kernel, symbiote_id=sym.id)
        assert row["purpose"] == "turn"
        assert row["prompt_tokens"] is None
        assert row["total_tokens"] is None
        assert outcome.usage.llm_calls == 1
        assert outcome.usage.total_tokens == 0
    finally:
        kernel.shutdown()


def test_plain_text_adapter_can_report_its_tokens(tmp_path: Path) -> None:
    class Reporting(ScriptedLLM):
        model = "house-model"
        provider = "house"

        def complete(self, messages, config=None, tools=None):
            report_usage(7, 3)
            return super().complete(messages, config, tools)

    kernel = make_kernel(tmp_path, Reporting("ok"))
    try:
        sym = kernel.create_symbiote(name="a", role="r")
        kernel.message(kernel.start_session(sym.id).id, "hello")

        (row,) = _rows(kernel, symbiote_id=sym.id)
        assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (7, 3, 10)
        assert (row["provider"], row["model"]) == ("house", "house-model")
    finally:
        kernel.shutdown()


def test_report_usage_outside_a_kernel_call_does_nothing() -> None:
    report_usage(1, 2, 3)  # no error, nothing to attach it to


def test_a_failed_call_is_recorded_as_failed(tmp_path: Path) -> None:
    def boom(_i, _m):
        raise ValueError("bad request")

    kernel = make_kernel(tmp_path, ScriptedLLM(boom))
    try:
        sym = kernel.create_symbiote(name="a", role="r")
        with pytest.raises(Exception):  # noqa: B017, PT011 — whatever the turn raises
            kernel.message(kernel.start_session(sym.id).id, "hello")

        rows = _rows(kernel, symbiote_id=sym.id)
        assert rows
        assert {r["success"] for r in rows} == {0}
        assert kernel.inspector.usage_totals(sym.id)["failed_calls"] == len(rows)
    finally:
        kernel.shutdown()


def test_streamed_answer_is_recorded_once_with_what_the_adapter_reported() -> None:
    class Streaming:
        model = "stream-1"

        def complete(self, messages, config=None, tools=None):
            return "unused"

        def stream(self, messages, config=None, tools=None):
            yield "he"
            yield "llo"
            report_usage(4, 2, 6, provider="acme")

    records: list[LLMCallRecord] = []
    llm = MeteredLLM(Streaming(), records.append)

    assert hasattr(llm, "stream")
    assert list(llm.stream([{"role": "user", "content": "hi"}])) == ["he", "llo"]

    (record,) = records
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (4, 2, 6)
    assert (record.provider, record.model, record.success) == ("acme", "stream-1", True)
    assert record.purpose == "other"  # not called by a kernel turn


def test_an_adapter_without_stream_still_has_none() -> None:
    llm = MeteredLLM(ScriptedLLM("ok"), lambda _r: None)

    assert not hasattr(llm, "stream")
    assert llm.wrapped.calls == []
    assert llm.calls == []  # attributes of the adapter are reachable


def test_a_sink_that_fails_does_not_break_the_call() -> None:
    def sink(_record) -> None:
        raise RuntimeError("storage down")

    assert MeteredLLM(ScriptedLLM("ok"), sink).complete([{"role": "user", "content": "x"}]) == "ok"


def test_symgateway_adapter_reports_tokens_for_plain_text_answers() -> None:
    def gateway(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_api/work-sessions":
            return httpx.Response(201, json={"uid": "ws-1"})
        return httpx.Response(200, json={
            "model": "gpt-x",
            "choices": [{"message": {"role": "assistant", "content": "olá"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })

    adapter = SymGatewayLLMAdapter(
        "sk-test", "gpt-x", client=httpx.Client(transport=httpx.MockTransport(gateway))
    )
    records: list[LLMCallRecord] = []

    assert MeteredLLM(adapter, records.append).complete([{"role": "user", "content": "oi"}]) == "olá"

    (record,) = records
    assert (record.provider, record.model) == ("symgateway", "gpt-x")
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (3, 2, 5)


# ── the turn's totals ───────────────────────────────────────────────────


def test_turn_outcome_and_turn_context_carry_the_totals(shop) -> None:
    kernel, main, _aux, sym = shop
    main._fn = lambda i, _m: tool_call_block("lookup") if i == 0 else "done"
    add_tool(kernel, sym.id, "lookup", lambda _p: "found")
    session = kernel.start_session(sym.id)

    outcome = kernel.turn(session.id, "where is it?")

    assert outcome.usage.model_dump() == {
        "llm_calls": 2, "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
    }
    (context,) = kernel.inspector.list_turn_contexts(session_id=session.id)
    assert (context["llm_calls"], context["total_tokens"]) == (2, 30)


def test_a_turn_without_tools_is_on_record_with_its_tokens(shop) -> None:
    kernel, _main, _aux, sym = shop
    session = kernel.start_session(sym.id)

    outcome = kernel.turn(session.id, "hello")

    assert outcome.tool_calls == 0
    assert outcome.usage.total_tokens == 15
    (context,) = kernel.inspector.list_turn_contexts(session_id=session.id)
    assert (context["llm_calls"], context["prompt_tokens"], context["completion_tokens"]) == (
        1, 10, 5,
    )


def test_turn_async_reports_the_same_totals(shop) -> None:
    import asyncio

    kernel, _main, _aux, sym = shop
    session = kernel.start_session(sym.id)

    outcome = asyncio.run(kernel.turn_async(session.id, "hello"))

    assert outcome.usage.llm_calls == 1
    assert outcome.usage.total_tokens == 15


# ── reading it back ─────────────────────────────────────────────────────


def test_usage_by_end_user_session_model_and_day(shop) -> None:
    kernel, _main, _aux, sym = shop
    ana = kernel.start_session(sym.id, subject_key="ana")
    owner = kernel.start_session(sym.id)
    kernel.message(ana.id, "hello")
    kernel.message(ana.id, "again")
    kernel.message(owner.id, "hello")

    by_subject = {r["subject"]: r for r in kernel.inspector.usage_by("subject", sym.id)}
    assert by_subject["ana"]["llm_calls"] == 2
    assert by_subject[None]["llm_calls"] == 1
    assert kernel.inspector.usage_totals(sym.id, subject_key="ana")["total_tokens"] == 30
    assert kernel.inspector.usage_totals(sym.id, subject_key=None)["total_tokens"] == 15
    by_session = kernel.inspector.usage_by("session", sym.id)
    assert [(r["session"], r["llm_calls"]) for r in by_session] == [(ana.id, 2), (owner.id, 1)]
    (by_model,) = kernel.inspector.usage_by("model", sym.id)
    assert (by_model["model"], by_model["total_tokens"]) == ("acme-1", 45)
    (by_day,) = kernel.inspector.usage_by("day", sym.id)
    assert len(by_day["day"]) == 10
    assert kernel.inspector.usage_totals(sym.id, since="2999-01-01")["llm_calls"] == 0
    assert kernel.inspector.usage_totals(sym.id, until="2999-01-01")["llm_calls"] == 3
    with pytest.raises(ValueError, match="unknown usage group"):
        kernel.inspector.usage_by("content", sym.id)


def test_usage_callback_gets_every_record_and_cannot_break_a_turn(shop) -> None:
    kernel, _main, _aux, sym = shop
    seen: list[LLMCallRecord] = []

    def callback(record: LLMCallRecord) -> None:
        seen.append(record)
        raise RuntimeError("host bug")

    kernel.set_usage_callback(callback)
    session = kernel.start_session(sym.id)

    assert kernel.message(session.id, "hello") == "ok"

    (record,) = seen
    assert (record.purpose, record.symbiote_id, record.session_id) == ("turn", sym.id, session.id)
    assert record.total_tokens == 15
    kernel.set_usage_callback(None)
    kernel.message(session.id, "again")
    assert len(seen) == 1


def test_the_record_holds_no_conversation_text(shop) -> None:
    kernel, _main, _aux, sym = shop
    kernel.message(kernel.start_session(sym.id).id, "my card number is 4111 1111")

    (row,) = kernel._storage.fetch_all("SELECT * FROM llm_usage")
    assert set(row) == {
        "id", "symbiote_id", "session_id", "root_session_id", "purpose", "provider",
        "model", "prompt_tokens", "completion_tokens", "total_tokens", "elapsed_ms",
        "success", "created_at",
    }
    assert "4111" not in json.dumps(row)


# ── deleting ────────────────────────────────────────────────────────────


def test_purging_a_symbiote_removes_its_usage_and_purging_an_end_user_keeps_the_counts(
    shop,
) -> None:
    kernel, _main, _aux, sym = shop
    other = kernel.create_symbiote(name="Other", role="assistant")
    kernel.message(kernel.start_session(sym.id, subject_key="ana").id, "hello")
    kernel.message(kernel.start_session(other.id).id, "hello")

    kernel.purge_subject(sym.id, "ana")
    kept = kernel.inspector.usage_totals(sym.id)
    assert kept["llm_calls"] == 1  # the count stays; the session it names is gone
    assert kernel.inspector.usage_by("subject", sym.id)[0]["subject"] is None

    kernel.purge_symbiote(sym.id)
    assert kernel.inspector.usage_totals(sym.id)["llm_calls"] == 0
    assert kernel.inspector.usage_totals(other.id)["llm_calls"] == 1


def test_prune_removes_old_usage_only_when_asked(shop) -> None:
    kernel, _main, _aux, sym = shop
    kernel.message(kernel.start_session(sym.id).id, "hello")
    kernel._storage.execute("UPDATE llm_usage SET created_at = '2020-01-01T00:00:00+00:00'")

    assert kernel.prune()["llm_usage"] == 0
    assert kernel.prune(usage_days=30, dry_run=True)["llm_usage"] == 1
    assert kernel.inspector.usage_totals(sym.id)["llm_calls"] == 1
    assert kernel.prune(usage_days=30)["llm_usage"] == 1
    assert kernel.inspector.usage_totals(sym.id)["llm_calls"] == 0
