"""One tool call as a session, outside a turn: ``kernel.run_tool``.

What a host does on a conversation's behalf between turns (asking whether a
job the symbiote started has finished) runs with the rules of that
conversation: its revision, the tools it is narrowed to, policy, the tool
guard, the hooks and the audit log. No model is called and nothing is added
to the conversation. A call that a turn would take to a person is not run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.hooks import BaseHook
from symbiote.core.session_scope import current_session_scope
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.exec_context import current_host_context, current_tool_context
from symbiote.environment.policies import ToolDecision
from symbiote.skills.scope import get_active_symbiote
from tests.support import ScriptedLLM, add_tool, make_kernel


class Jobs:
    """A symbiote authorized to ask for a job's ``status`` and to ``cancel``
    it; ``ghost`` is registered but not authorized."""

    def __init__(self, tmp_path: Path, llm: Any = "unset") -> None:
        self.llm = ScriptedLLM("ok") if llm == "unset" else llm
        self.kernel = make_kernel(tmp_path, self.llm)
        k = self.kernel
        self.sym = k.create_symbiote(name="Jobs", role="operator")
        self.seen: list[dict] = []
        add_tool(k, self.sym.id, "status", self._status)
        add_tool(k, self.sym.id, "cancel", lambda _p: "cancelled")
        k.tool_gateway.register_tool("ghost", lambda _p: "boo")
        self.session = k.start_session(self.sym.id)

    def _status(self, params: dict) -> dict:
        ctx = current_tool_context()
        scope = current_session_scope()
        self.seen.append(
            {
                "params": params,
                "symbiote": ctx.symbiote_id if ctx else None,
                "session": ctx.session_id if ctx else None,
                "host": dict(current_host_context()),
                "scope_session": scope.session_id if scope else None,
                "active_symbiote": get_active_symbiote(),
            }
        )
        return {"status": "running", "job": params.get("job")}

    def audit(self, session_id: str | None = None) -> list[tuple[str, str, str]]:
        rows = self.kernel._storage.fetch_all(
            "SELECT tool_id, action, result FROM audit_log WHERE session_id = ? "
            "ORDER BY rowid",
            (session_id or self.session.id,),
        )
        return [(r["tool_id"], r["action"], r["result"]) for r in rows]


@pytest.fixture()
def jobs(tmp_path: Path):
    j = Jobs(tmp_path)
    yield j
    j.kernel.shutdown()


# ── it runs as the session ───────────────────────────────────────────────


def test_the_call_runs_as_the_session(jobs) -> None:
    k = jobs.kernel

    result = k.run_tool(
        jobs.session.id, "status", {"job": "exec-1"}, host_context={"user": "ana"}
    )

    assert result.success is True
    assert result.output == {"status": "running", "job": "exec-1"}
    assert jobs.seen == [
        {
            "params": {"job": "exec-1"},
            "symbiote": jobs.sym.id,
            "session": jobs.session.id,
            "host": {"user": "ana"},
            "scope_session": jobs.session.id,
            "active_symbiote": jobs.sym.id,
        }
    ]


def test_nothing_of_the_session_stays_bound_afterwards(jobs) -> None:
    jobs.kernel.run_tool(jobs.session.id, "status", host_context={"user": "ana"})

    assert current_session_scope() is None
    assert dict(current_host_context()) == {}
    assert get_active_symbiote() is None


def test_no_model_is_called_and_the_conversation_is_untouched(jobs) -> None:
    k = jobs.kernel

    k.run_tool(jobs.session.id, "status", {"job": "exec-1"})

    assert jobs.llm.calls == []
    assert k.inspector.list_messages(jobs.session.id) == []


def test_it_works_without_a_model(tmp_path: Path) -> None:
    j = Jobs(tmp_path, llm=None)

    assert j.kernel.run_tool(j.session.id, "status").success is True
    j.kernel.shutdown()


def test_the_call_is_audited_like_any_other(jobs) -> None:
    jobs.kernel.run_tool(jobs.session.id, "status", {"job": "exec-1"})

    assert [(tool, action) for tool, action, _ in jobs.audit()] == [("status", "execute")]


def test_the_async_variant_awaits_a_coroutine_handler(jobs) -> None:
    k = jobs.kernel

    async def status_async(params: dict) -> dict:
        await asyncio.sleep(0)
        return {"status": "succeeded", "session": current_tool_context().session_id}

    add_tool(k, jobs.sym.id, "status_async", status_async)

    result = asyncio.run(k.run_tool_async(jobs.session.id, "status_async"))

    assert result.success is True
    assert result.output == {"status": "succeeded", "session": jobs.session.id}


def test_the_tool_hooks_fire(jobs) -> None:
    fired: list[str] = []

    class Spy(BaseHook):
        async def before_tool(self, tool_id: str, params: dict) -> None:
            fired.append(f"before:{tool_id}")

        async def after_tool(self, tool_id: str, params: dict, result: Any) -> None:
            fired.append(f"after:{tool_id}")

    jobs.kernel.hooks.add(Spy())

    jobs.kernel.run_tool(jobs.session.id, "status")

    assert fired == ["before:status", "after:status"]


# ── the rules of the session apply ───────────────────────────────────────


def test_a_tool_the_symbiote_is_not_authorized_to_is_refused(jobs) -> None:
    result = jobs.kernel.run_tool(jobs.session.id, "ghost")

    assert result.success is False
    assert "blocked" in result.error
    assert [(tool, action) for tool, action, _ in jobs.audit()] == [("ghost", "blocked")]


def test_a_tool_outside_the_session_s_list_is_refused(jobs) -> None:
    k = jobs.kernel
    narrowed = k.start_session(jobs.sym.id, tools_allowed=["cancel"])

    result = k.run_tool(narrowed.id, "status")

    assert result.success is False
    assert "not available in this conversation" in result.error
    assert jobs.seen == []
    assert k.run_tool(narrowed.id, "cancel").output == "cancelled"


def test_the_session_s_revision_decides_what_is_authorized(jobs) -> None:
    k = jobs.kernel
    k.create_revision(jobs.sym.id, "v1")  # status and cancel
    pinned = k.start_session(jobs.sym.id, revision="v1")
    k.environment.configure(symbiote_id=jobs.sym.id, tools=["cancel"])

    assert k.run_tool(pinned.id, "status").success is True  # as published
    assert k.run_tool(jobs.session.id, "status").success is False  # as edited


def test_the_tool_guard_is_asked(jobs) -> None:
    k = jobs.kernel
    asked: list[tuple[str, str | None]] = []

    def guard(tool_id: str, params: dict, ctx) -> ToolDecision:
        asked.append((tool_id, ctx.session_id))
        return ToolDecision.deny("the workspace is paused")

    k.set_tool_guard(guard)

    result = k.run_tool(jobs.session.id, "status")

    assert asked == [("status", jobs.session.id)]
    assert result.success is False
    assert "the workspace is paused" in result.error
    assert jobs.seen == []


def test_an_unknown_tool_comes_back_as_a_failed_result(jobs) -> None:
    result = jobs.kernel.run_tool(jobs.session.id, "nope")

    assert (result.success, result.error) == (False, "Tool not registered")


def test_a_failing_handler_comes_back_as_a_failed_result(jobs) -> None:
    def broken(_params: dict) -> None:
        raise RuntimeError("upstream is down")

    add_tool(jobs.kernel, jobs.sym.id, "broken", broken)

    result = jobs.kernel.run_tool(jobs.session.id, "broken")

    assert result.success is False
    assert "upstream is down" in result.error


def test_an_unknown_session_raises(jobs) -> None:
    with pytest.raises(EntityNotFoundError):
        jobs.kernel.run_tool("no-such-session", "status")
    with pytest.raises(EntityNotFoundError):
        asyncio.run(jobs.kernel.run_tool_async("no-such-session", "status"))


# ── what a turn would take to a person is not run ────────────────────────


def _risky(kernel, symbiote_id: str, tool_id: str, ran: list[str], **descriptor) -> None:
    kernel.tool_gateway.register_descriptor(
        ToolDescriptor(
            tool_id=tool_id, name=tool_id, description="test tool", **descriptor
        ),
        lambda _p: ran.append(tool_id) or "done",
    )
    tools = [*kernel.environment.list_tools(symbiote_id), tool_id]
    kernel.environment.configure(symbiote_id=symbiote_id, tools=tools)


def test_a_tool_that_requires_approval_is_not_run(jobs) -> None:
    k, ran = jobs.kernel, []
    _risky(k, jobs.sym.id, "wire", ran, requires_approval=True)

    result = k.run_tool(jobs.session.id, "wire", {"amount": 10})

    assert result.success is False
    assert "person approves" in result.error
    assert ran == []
    assert jobs.audit() == [("wire", "blocked", "blocked:needs approval")]


def test_nor_is_it_run_when_the_kernel_has_no_model(tmp_path: Path) -> None:
    j, ran = Jobs(tmp_path, llm=None), []
    _risky(j.kernel, j.sym.id, "wire", ran, requires_approval=True)

    assert j.kernel.run_tool(j.session.id, "wire").success is False
    assert ran == []
    j.kernel.shutdown()


def test_a_risk_the_approval_callback_asks_about_is_not_run(jobs) -> None:
    k, ran, asked = jobs.kernel, [], []
    _risky(k, jobs.sym.id, "delete_all", ran, risk_level="high")
    _risky(k, jobs.sym.id, "rename", ran, risk_level="medium")
    k.set_approval_callback(lambda *a: asked.append(a) or True)

    refused = k.run_tool(jobs.session.id, "delete_all")
    allowed = k.run_tool(jobs.session.id, "rename")

    assert refused.success is False
    assert allowed.success is True
    assert ran == ["rename"]
    assert asked == []  # nobody is asked outside a turn


def test_the_threshold_of_the_callback_is_the_one_that_counts(jobs) -> None:
    k, ran = jobs.kernel, []
    _risky(k, jobs.sym.id, "rename", ran, risk_level="medium")
    k.set_approval_callback(lambda *a: True, risk_threshold="medium")

    assert k.run_tool(jobs.session.id, "rename").success is False
    assert ran == []


def test_without_a_callback_a_high_risk_tool_runs_as_it_does_in_a_turn(jobs) -> None:
    k, ran = jobs.kernel, []
    _risky(k, jobs.sym.id, "delete_all", ran, risk_level="high")

    assert k.run_tool(jobs.session.id, "delete_all").success is True
    assert ran == ["delete_all"]


# ── next to a turn ───────────────────────────────────────────────────────


def test_it_does_not_wait_for_the_turn_running_on_the_session(tmp_path: Path) -> None:
    """A handler of the turn calls ``run_tool`` on its own session: with the
    session lock taken by that turn, a call that waited for it would hang."""
    polled: list[Any] = []

    def script(_i, messages):
        if "[Tool" in str(messages[-1]["content"]):
            return "done"
        return '```tool_call\n{"tool": "start", "params": {}}\n```'

    j = Jobs(tmp_path, llm=ScriptedLLM(script))
    k = j.kernel

    def start(_params: dict) -> str:
        polled.append(k.run_tool(j.session.id, "status", {"job": "x"}).output)
        return "started"

    add_tool(k, j.sym.id, "start", start)

    k.message(j.session.id, "start the job")

    assert polled == [{"status": "running", "job": "x"}]
    k.shutdown()
