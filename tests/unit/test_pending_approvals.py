"""Tool calls that wait for a person's approval (B-90).

The approval callback could only answer yes or no, holding the turn's thread
meanwhile. It can now answer ``ApprovalDecision.defer(...)``: the call is not
executed, the request is stored, the turn ends, and
``kernel.resolve_pending(id, approved)`` runs it later in the context it was
asked in. The conversation hears the outcome on its next turn.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from symbiote.core.approvals import DEFAULT_PENDING_MESSAGE
from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.exec_context import current_tool_context
from symbiote.environment.policies import ApprovalDecision, ToolDecision
from tests.support import ScriptedLLM, make_kernel, tool_call_block


class Shop:
    """A kernel with one symbiote and a high-risk ``cancel_order`` tool."""

    def __init__(self, tmp_path: Path, script=None, **config) -> None:
        self.tmp_path = tmp_path
        self.llm = ScriptedLLM(script or self.cancel_then_done)
        self.kernel = make_kernel(tmp_path, self.llm, **config)
        self.sym = self.kernel.create_symbiote(name="Shop", role="assistant")
        self.calls: list[dict] = []
        self.contexts: list = []
        self.register(self.kernel, self.sym.id)
        self.answer = ApprovalDecision.defer("needs the owner")
        self.asked: list[tuple] = []
        self.kernel.set_approval_callback(self.callback)

    @staticmethod
    def cancel_then_done(_i, messages):
        last = str(messages[-1]["content"])
        if "[Tool" in last:
            return "noted"
        return tool_call_block("cancel_order", order_id="4471", card="4111-1111-1111-1111")

    def register(self, kernel, symbiote_id: str) -> None:
        kernel.tool_gateway.register_descriptor(
            ToolDescriptor(
                tool_id="cancel_order",
                name="cancel_order",
                description="Cancel an order",
                parameters={"type": "object", "properties": {"order_id": {"type": "string"}}},
                risk_level="high",
                secret_params=["card"],
            ),
            self.handler,
        )
        kernel.tool_gateway.register_descriptor(
            ToolDescriptor(tool_id="lookup", name="lookup", description="Look an order up"),
            lambda _params: "order 4471 is open",
        )
        kernel.environment.configure(symbiote_id=symbiote_id, tools=["cancel_order", "lookup"])

    def handler(self, params: dict) -> dict:
        self.calls.append(dict(params))
        self.contexts.append(current_tool_context())
        return {"cancelled": params["order_id"]}

    def callback(self, tool_id: str, params: dict, risk: str):
        self.asked.append((tool_id, dict(params), risk))
        return self.answer

    def turn(self, session_id: str | None = None, text: str = "cancel my order", **start):
        session_id = session_id or self.kernel.start_session(self.sym.id, **start).id
        return session_id, self.kernel.turn(session_id, text)

    def pending(self, **filters) -> list[dict]:
        return self.kernel.inspector.list_pending(self.sym.id, **filters)


@pytest.fixture()
def shop(tmp_path: Path):
    s = Shop(tmp_path)
    yield s
    s.kernel.shutdown()


# ── the callback's three answers ────────────────────────────────────────


@pytest.mark.parametrize("answer", [True, ApprovalDecision.approve()])
def test_yes_runs_the_call_as_before(shop, answer) -> None:
    shop.answer = answer

    _sid, outcome = shop.turn()

    assert shop.calls == [{"order_id": "4471", "card": "4111-1111-1111-1111"}]
    assert outcome.tool_results[0].success is True
    assert outcome.pending_approvals == []
    assert shop.pending(status=None) == []


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (False, "Tool call denied by approval callback"),
        (ApprovalDecision.deny(), "Tool call denied by approval callback"),
        (ApprovalDecision.deny("orders are frozen"), "Tool call denied: orders are frozen"),
    ],
)
def test_no_refuses_the_call(shop, answer, error) -> None:
    shop.answer = answer

    _sid, outcome = shop.turn()

    assert shop.calls == []
    assert outcome.tool_results[0].error == error
    assert shop.pending(status=None) == []


def test_defer_records_the_request_and_ends_the_turn_normally(shop) -> None:
    session_id, outcome = shop.turn(subject_key="ana")

    assert shop.calls == []
    assert outcome.stop_reason == "end_turn"
    assert outcome.text == "noted"
    (result,) = outcome.tool_results
    assert result.success is False
    assert result.error == DEFAULT_PENDING_MESSAGE
    assert outcome.pending_approvals == [result.pending_approval_id]
    assert DEFAULT_PENDING_MESSAGE in shop.llm.prompt_text()  # the model was told

    (request,) = shop.pending()
    assert request["id"] == result.pending_approval_id
    assert request["status"] == "pending"
    assert (request["symbiote_id"], request["session_id"]) == (shop.sym.id, session_id)
    assert (request["root_symbiote_id"], request["root_session_id"]) == (shop.sym.id, session_id)
    assert request["subject_key"] == "ana"
    assert (request["tool_id"], request["risk_level"]) == ("cancel_order", "high")
    assert request["reason"] == "needs the owner"
    assert request["expires_at"] > request["created_at"]
    assert shop.kernel.inspector.get_pending(request["id"]) == request


def test_message_also_reports_the_pending_id(shop) -> None:
    session = shop.kernel.start_session(shop.sym.id)

    reply = shop.kernel.message(session.id, "cancel my order")

    assert reply["tool_results"][0]["pending_approval_id"] == shop.pending()[0]["id"]


def test_defer_can_say_what_the_model_is_told(shop) -> None:
    shop.answer = ApprovalDecision.defer(message="Sent to the owner; answer within a day.")

    _sid, outcome = shop.turn()

    assert outcome.tool_results[0].error == "Sent to the owner; answer within a day."


def test_calls_of_the_same_round_that_were_not_deferred_still_run(tmp_path: Path) -> None:
    def script(_i, messages):
        if "[Tool" in str(messages[-1]["content"]):
            return "noted"
        return (
            tool_call_block("lookup")
            + "\n"
            + tool_call_block("cancel_order", order_id="4471")
        )

    shop = Shop(tmp_path, script)
    try:
        _sid, outcome = shop.turn()

        by_tool = {r.tool_id: r for r in outcome.tool_results}
        assert by_tool["lookup"].success is True
        assert by_tool["lookup"].output == "order 4471 is open"
        assert by_tool["cancel_order"].pending_approval_id
        assert [a[0] for a in shop.asked] == ["cancel_order"]  # low risk is not asked
    finally:
        shop.kernel.shutdown()


def test_parameters_are_shown_without_secrets_and_erased_once_resolved(shop) -> None:
    shop.turn()
    (request,) = shop.pending()
    raw = shop.kernel._storage.fetch_one(
        "SELECT params_json FROM pending_approvals WHERE id = ?", (request["id"],)
    )

    assert request["params"]["order_id"] == "4471"
    assert "4111" not in str(request["params"])
    assert "4111-1111-1111-1111" in raw["params_json"]  # kept to run the call later

    shop.kernel.resolve_pending(request["id"], True)

    raw = shop.kernel._storage.fetch_one(
        "SELECT params_json FROM pending_approvals WHERE id = ?", (request["id"],)
    )
    assert raw["params_json"] is None
    assert shop.calls == [{"order_id": "4471", "card": "4111-1111-1111-1111"}]


# ── deciding later ──────────────────────────────────────────────────────


def test_approval_runs_the_call_in_its_original_context(shop) -> None:
    session_id, _ = shop.turn(subject_key="ana")
    (request,) = shop.pending()

    resolved = shop.kernel.resolve_pending(
        request["id"], True, note="ok, go ahead", resolved_by="owner-1",
        host_context={"token": "t-1"},
    )

    assert resolved.status == "executed"
    assert resolved.result == {"cancelled": "4471"}
    assert (resolved.note, resolved.resolved_by) == ("ok, go ahead", "owner-1")
    assert resolved.resolved_at is not None
    assert len(shop.calls) == 1
    (context,) = shop.contexts
    assert (context.symbiote_id, context.session_id) == (shop.sym.id, session_id)
    assert context.approval_id == request["id"]
    assert context.host["token"] == "t-1"
    assert shop.pending() == []
    assert [r["id"] for r in shop.pending(status="executed")] == [request["id"]]


def test_the_audit_row_of_the_execution_names_the_request(shop) -> None:
    session_id, _ = shop.turn()
    (request,) = shop.pending()
    shop.kernel.resolve_pending(request["id"], True)

    rows = shop.kernel._storage.fetch_all(
        "SELECT tool_id, action, approval_id FROM audit_log WHERE session_id = ?", (session_id,)
    )

    assert [(r["tool_id"], r["approval_id"]) for r in rows] == [("cancel_order", request["id"])]


def test_denial_runs_nothing(shop) -> None:
    shop.turn()
    (request,) = shop.pending()

    resolved = shop.kernel.resolve_pending(request["id"], False, note="not this month")

    assert resolved.status == "denied"
    assert resolved.note == "not this month"
    assert shop.calls == []


def test_a_request_is_resolved_only_once(shop) -> None:
    shop.turn()
    (request,) = shop.pending()
    shop.kernel.resolve_pending(request["id"], True)

    with pytest.raises(ValidationError, match="executed, not pending"):
        shop.kernel.resolve_pending(request["id"], True)
    with pytest.raises(ValidationError):
        shop.kernel.resolve_pending(request["id"], False)
    with pytest.raises(EntityNotFoundError):
        shop.kernel.resolve_pending("no-such-id", True)

    assert len(shop.calls) == 1


def test_two_people_deciding_at_once_run_the_call_once(shop) -> None:
    shop.turn()
    (request,) = shop.pending()
    outcomes: list[str] = []

    def decide() -> None:
        try:
            outcomes.append(shop.kernel.resolve_pending(request["id"], True).status)
        except ValidationError:
            outcomes.append("refused")

    threads = [threading.Thread(target=decide) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["executed", "refused", "refused", "refused"]
    assert len(shop.calls) == 1


def test_permissions_are_checked_again_when_the_call_finally_runs(shop) -> None:
    shop.turn()
    (request,) = shop.pending()
    shop.kernel.environment.configure(symbiote_id=shop.sym.id, tools=["lookup"])

    resolved = shop.kernel.resolve_pending(request["id"], True)

    assert resolved.status == "failed"
    assert "blocked" in resolved.error
    assert shop.calls == []


def test_the_tool_guard_sees_that_the_call_was_approved(shop) -> None:
    seen: list = []

    def guard(tool_id, _params, context):
        seen.append((tool_id, context.approval_id))
        if tool_id == "cancel_order" and context.approval_id is None:
            return ToolDecision.deny("visitors cannot cancel orders")
        return ToolDecision.allow()

    shop.kernel.set_tool_guard(guard)
    shop.answer = True  # asked only after the guard: the guard refuses first
    _sid, outcome = shop.turn()
    assert "visitors cannot cancel orders" in outcome.tool_results[0].error

    # A host that defers in the callback lets the guard through for approved calls.
    shop.kernel.set_tool_guard(None)
    shop.answer = ApprovalDecision.defer()
    shop.turn()
    (request,) = shop.pending()
    shop.kernel.set_tool_guard(guard)

    resolved = shop.kernel.resolve_pending(request["id"], True)

    assert resolved.status == "executed"
    assert seen[-1] == ("cancel_order", request["id"])


def test_a_tool_that_fails_when_finally_run_is_recorded_as_failed(shop) -> None:
    shop.turn()
    (request,) = shop.pending()

    def broken(_params: dict):
        raise RuntimeError("payment service down")

    shop.handler = broken
    shop.kernel.tool_gateway.register_descriptor(
        shop.kernel.tool_gateway.get_descriptor("cancel_order"), broken
    )

    resolved = shop.kernel.resolve_pending(request["id"], True)

    assert resolved.status == "failed"
    assert "payment service down" in resolved.error


# ── the conversation hears the outcome ──────────────────────────────────


def _context_block(shop) -> str:
    return shop.llm.calls[-1]["messages"][0]["content"]


def test_next_turn_of_the_session_is_told_once(shop) -> None:
    session_id, _ = shop.turn()
    (request,) = shop.pending()
    shop.kernel.resolve_pending(request["id"], True, note="done with care")
    shop.llm._fn = lambda _i, _m: "your order was cancelled"

    shop.kernel.message(session_id, "any news?")

    told = _context_block(shop)
    assert "Requests that waited for approval" in told
    assert "cancel_order" in told
    assert "APPROVED and executed" in told
    assert '"cancelled": "4471"' in told
    assert "done with care" in told
    assert "4111" not in told
    assert shop.kernel.inspector.get_pending(request["id"])["delivered_at"] is not None

    shop.kernel.message(session_id, "thanks")
    assert "Requests that waited for approval" not in _context_block(shop)


def test_a_denial_is_told_too(shop) -> None:
    session_id, _ = shop.turn()
    shop.kernel.resolve_pending(shop.pending()[0]["id"], False, note="not this month")
    shop.llm._fn = lambda _i, _m: "sorry"

    shop.kernel.message(session_id, "any news?")

    told = _context_block(shop)
    assert "DENIED" in told
    assert "not this month" in told


def test_an_end_user_is_told_in_a_later_session_and_nobody_else_is(shop) -> None:
    shop.turn(subject_key="ana")
    shop.kernel.resolve_pending(shop.pending()[0]["id"], True)
    shop.llm._fn = lambda _i, _m: "ok"

    bia = shop.kernel.start_session(shop.sym.id, subject_key="bia")
    shop.kernel.message(bia.id, "hello")
    assert "Requests that waited for approval" not in _context_block(shop)
    owner = shop.kernel.start_session(shop.sym.id)
    shop.kernel.message(owner.id, "hello")
    assert "Requests that waited for approval" not in _context_block(shop)

    ana_again = shop.kernel.start_session(shop.sym.id, subject_key="ana")
    shop.kernel.message(ana_again.id, "I am back")
    assert "APPROVED and executed" in _context_block(shop)


def test_a_request_still_waiting_is_not_announced(shop) -> None:
    session_id, _ = shop.turn()
    shop.llm._fn = lambda _i, _m: "still waiting"

    shop.kernel.message(session_id, "any news?")

    assert "Requests that waited for approval" not in _context_block(shop)


def test_outcome_is_told_again_if_the_turn_that_carried_it_failed(shop) -> None:
    session_id, _ = shop.turn()
    request_id = shop.pending()[0]["id"]
    shop.kernel.resolve_pending(request_id, False)

    def boom(_i, _m):
        raise ValueError("model down")

    shop.llm._fn = boom
    with pytest.raises(Exception):  # noqa: B017, PT011 — whatever the turn raises
        shop.kernel.message(session_id, "any news?")
    assert shop.kernel.inspector.get_pending(request_id)["delivered_at"] is None

    shop.llm._fn = lambda _i, _m: "sorry"
    shop.kernel.message(session_id, "any news?")
    assert "DENIED" in _context_block(shop)


# ── expiry, restart, deletion ───────────────────────────────────────────


def test_a_request_expires_and_then_cannot_be_approved(shop) -> None:
    shop.answer = ApprovalDecision.defer(expires_in=0.05)
    session_id, _ = shop.turn()
    request_id = shop.pending()[0]["id"]
    time.sleep(0.1)

    assert shop.pending() == []
    assert [r["id"] for r in shop.pending(status="expired")] == [request_id]
    with pytest.raises(ValidationError, match="expired"):
        shop.kernel.resolve_pending(request_id, True)
    assert shop.calls == []
    raw = shop.kernel._storage.fetch_one(
        "SELECT status, params_json FROM pending_approvals WHERE id = ?", (request_id,)
    )
    assert (raw["status"], raw["params_json"]) == ("expired", None)

    shop.llm._fn = lambda _i, _m: "it expired"
    shop.kernel.message(session_id, "any news?")
    assert "EXPIRED without an answer" in _context_block(shop)


def test_default_time_to_live_comes_from_the_kernel_config(tmp_path: Path) -> None:
    shop = Shop(tmp_path, pending_approval_ttl_hours=None)
    try:
        shop.turn()
        assert shop.pending()[0]["expires_at"] is None
        assert shop.kernel.expire_pending() == 0
    finally:
        shop.kernel.shutdown()


def test_requests_survive_a_restart(tmp_path: Path) -> None:
    first = Shop(tmp_path)
    session_id, _ = first.turn(subject_key="ana")
    request_id = first.pending()[0]["id"]
    first.kernel.shutdown()

    second = Shop.__new__(Shop)
    second.calls, second.contexts = [], []
    second.kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    try:
        second.register(second.kernel, first.sym.id)
        assert [r["id"] for r in second.kernel.inspector.list_pending(first.sym.id)] == [request_id]

        resolved = second.kernel.resolve_pending(request_id, True)

        assert resolved.status == "executed"
        assert second.calls == [{"order_id": "4471", "card": "4111-1111-1111-1111"}]
        assert second.contexts[0].session_id == session_id
    finally:
        second.kernel.shutdown()


def test_purging_an_end_user_or_a_symbiote_removes_their_requests(shop) -> None:
    shop.turn(subject_key="ana")
    shop.turn(subject_key="bia")
    shop.turn()

    shop.kernel.purge_subject(shop.sym.id, "ana")
    assert sorted(str(r["subject_key"]) for r in shop.pending()) == ["None", "bia"]

    shop.kernel.purge_symbiote(shop.sym.id)
    assert shop.kernel.inspector.list_pending(status=None) == []


# ── the other paths ─────────────────────────────────────────────────────


def test_message_async_defers_like_message(shop) -> None:
    session = shop.kernel.start_session(shop.sym.id)

    outcome = asyncio.run(shop.kernel.turn_async(session.id, "cancel my order"))

    assert shop.calls == []
    assert outcome.pending_approvals == [shop.pending()[0]["id"]]


def test_a_delegated_symbiote_defers_under_the_conversation_that_delegated(
    tmp_path: Path,
) -> None:
    def script(_i, messages):
        last = str(messages[-1]["content"])
        if "[Tool result: spawn]" in last or "[Tool error: spawn]" in last:
            return "the cancellation is waiting for approval"
        if "[Tool" in last:
            return "worker: waiting"
        if "SUBTASK" in last:
            return tool_call_block("cancel_order", order_id="4471")
        return tool_call_block("spawn", target_symbiote="Shop", task="SUBTASK cancel 4471")

    shop = Shop(tmp_path, script)
    try:
        boss = shop.kernel.create_symbiote(name="boss", role="front desk")
        shop.kernel.environment.configure(symbiote_id=boss.id, tools=["spawn"])
        session = shop.kernel.start_session(boss.id, subject_key="ana")

        shop.kernel.message(session.id, "cancel my order")

        (request,) = shop.pending()
        assert request["symbiote_id"] == shop.sym.id
        assert request["session_id"] != session.id
        assert (request["root_symbiote_id"], request["root_session_id"]) == (boss.id, session.id)
        assert request["subject_key"] == "ana"

        shop.kernel.resolve_pending(request["id"], True)
        shop.llm._fn = lambda _i, _m: "your order was cancelled"
        shop.kernel.message(session.id, "any news?")

        assert "APPROVED and executed" in _context_block(shop)
    finally:
        shop.kernel.shutdown()


def test_a_runner_with_nowhere_to_record_refuses_a_deferred_call() -> None:
    from symbiote.environment.descriptors import ToolCall
    from symbiote.runners.chat import ChatRunner

    class Gateway:
        def get_risk_level(self, _tool_id: str) -> str:
            return "high"

    runner = ChatRunner(
        ScriptedLLM("ok"), tool_gateway=Gateway(),
        on_before_tool_call=lambda *_a: ApprovalDecision.defer(),
    )

    approved, denied = runner._check_approval([ToolCall(tool_id="cancel_order", params={})])

    assert approved == []
    assert denied[0].success is False
    assert denied[0].pending_approval_id is None
    assert "nobody can give it now" in denied[0].error


def test_a_long_run_block_defers_too(tmp_path: Path) -> None:
    import json

    plan = json.dumps([{"name": "one", "description": "cancel it", "success_criteria": "done"}])

    def script(_i, messages):
        system = str(messages[0]["content"])
        if system.startswith("PLANNER"):
            return plan
        if system.startswith("You ran a multi-step project"):
            return "ANSWER"
        if "[Tool" in str(messages[-1]["content"]):
            return "block: waiting for approval"
        return tool_call_block("cancel_order", order_id="4471")

    shop = Shop(tmp_path, script)
    try:
        shop.kernel.environment.configure(
            symbiote_id=shop.sym.id, tool_mode="long_run", planner_prompt="PLANNER: decompose"
        )
        session = shop.kernel.start_session(shop.sym.id)

        shop.kernel.message(session.id, "cancel my order")

        assert shop.calls == []
        (request,) = shop.pending()
        assert (request["tool_id"], request["session_id"]) == ("cancel_order", session.id)
    finally:
        shop.kernel.shutdown()


# ── HTTP API ────────────────────────────────────────────────────────────


def test_api_lists_and_resolves_pending_approvals(api_client) -> None:
    api = api_client(ScriptedLLM(Shop.cancel_then_done))
    owner = api.auth(api.new_key("tenant-a"))
    stranger = api.auth(api.new_key("tenant-b"))
    sym_id = api.client.post(
        "/symbiotes", json={"name": "Shop", "role": "assistant"}, headers=owner
    ).json()["id"]
    shop = Shop.__new__(Shop)
    shop.calls, shop.contexts = [], []
    shop.register(api.kernel, sym_id)
    api.kernel.set_approval_callback(lambda *_a: ApprovalDecision.defer("needs the owner"))
    session_id = api.client.post(
        "/sessions", json={"symbiote_id": sym_id, "subject_key": "ana"}, headers=owner
    ).json()["id"]
    chat = api.client.post(
        f"/sessions/{session_id}/chat", json={"content": "cancel my order"}, headers=owner
    ).json()
    approval_id = chat["response"]["tool_results"][0]["pending_approval_id"]

    listed = api.client.get(f"/symbiotes/{sym_id}/approvals", headers=owner).json()
    assert [(r["id"], r["status"], r["subject_key"]) for r in listed] == [
        (approval_id, "pending", "ana")
    ]
    assert "4111" not in str(listed)
    assert api.client.get(
        f"/symbiotes/{sym_id}/approvals", params={"subject_key": "bia"}, headers=owner
    ).json() == []
    assert api.client.get(f"/symbiotes/{sym_id}/approvals", headers=stranger).status_code in (
        403, 404,
    )
    assert api.client.post(
        f"/approvals/{approval_id}/resolve", json={"approved": True}, headers=stranger
    ).status_code in (403, 404)
    assert shop.calls == []

    resolved = api.client.post(
        f"/approvals/{approval_id}/resolve",
        json={"approved": True, "note": "fine"},
        headers=owner,
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "executed"
    assert resolved.json()["result"] == {"cancelled": "4471"}
    assert resolved.json()["resolved_by"]
    assert len(shop.calls) == 1

    again = api.client.post(
        f"/approvals/{approval_id}/resolve", json={"approved": True}, headers=owner
    )
    assert again.status_code == 409
    assert api.client.post(
        "/approvals/no-such-id/resolve", json={"approved": True}, headers=owner
    ).status_code == 404
    everything = api.client.get(
        f"/symbiotes/{sym_id}/approvals", params={"status": "all"}, headers=owner
    ).json()
    assert [r["status"] for r in everything] == ["executed"]
