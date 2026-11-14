"""Host governance: injectable storage, caller context and the host's veto.

Covers what an embedding host with its own IAM relies on:

1. ``SymbioteKernel(config, storage=...)`` uses the host's storage adapter.
2. ``host_context`` given to ``message()`` / ``message_async()`` reaches tool
   handlers, the approval callback, the tool guard and the LLM adapter, on
   every execution path, and never crosses between concurrent turns.
3. ``set_tool_guard`` can refuse a call before it runs, with the reason
   returned to the model and recorded.
4. The order is policy, host guard, approval (B-75).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import ToolCall, ToolDescriptor
from symbiote.environment.exec_context import (
    ToolExecContext,
    current_host_context,
    current_tool_context,
)
from symbiote.environment.policies import ToolDecision
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block

SECRET = "sk-host-credential-7f3a"


def _once(*blocks: str) -> ScriptedLLM:
    """LLM that emits ``blocks`` on its first call and plain text afterwards."""
    return ScriptedLLM(lambda i, _m: "\n".join(blocks) if i == 0 else "done")


def _register(
    kernel: SymbioteKernel,
    symbiote_id: str,
    tool_id: str,
    handler: Any,
    *,
    risk_level: str = "low",
    pass_context: bool = False,
    authorize: bool = True,
) -> None:
    kernel.tool_gateway.register_descriptor(
        ToolDescriptor(
            tool_id=tool_id,
            name=tool_id,
            description=f"test tool {tool_id}",
            parameters={"type": "object", "properties": {}},
            risk_level=risk_level,
        ),
        handler,
        pass_context=pass_context,
    )
    if authorize:
        kernel.environment.add_tools(symbiote_id, [tool_id])


def _text(reply: Any) -> str:
    """Reply text: ``message()`` returns a dict when the turn ran tools."""
    return reply["text"] if isinstance(reply, dict) else reply


def _dump_database(storage: Any) -> str:
    """Every row of every table as one string."""
    return "\n".join(
        json.dumps(row, default=str)
        for table in storage.table_names()
        for row in storage.fetch_all(f"SELECT * FROM {table}")  # noqa: S608
    )


# ── 1. storage injection ──────────────────────────────────────────────────────


class TestStorageInjection:
    def test_injected_storage_is_used_and_left_open(self, tmp_path: Path) -> None:
        storage = SQLiteAdapter(tmp_path / "host.db")
        unused = tmp_path / "unused.db"
        kernel = SymbioteKernel(KernelConfig(db_path=unused), storage=storage)

        sym = kernel.create_symbiote(name="a", role="assistant")

        assert not unused.exists()
        assert storage.fetch_one("SELECT id FROM symbiotes WHERE id = ?", (sym.id,))
        kernel.shutdown()
        # The host owns an injected adapter: shutdown leaves it open.
        assert storage.fetch_one("SELECT COUNT(*) AS c FROM symbiotes")["c"] == 1
        storage.close()

    def test_default_storage_is_closed_on_shutdown(self, tmp_path: Path) -> None:
        kernel = make_kernel(tmp_path)
        kernel.shutdown()
        with pytest.raises(Exception, match="(?i)closed"):
            kernel._storage.fetch_one("SELECT 1 AS one")

    def test_two_kernels_on_one_storage_see_the_same_session(
        self, tmp_path: Path
    ) -> None:
        storage = SQLiteAdapter(tmp_path / "shared.db")
        config = KernelConfig(db_path=tmp_path / "unused.db", skills_root=tmp_path / "s")
        first = SymbioteKernel(config, llm=ScriptedLLM("hello from first"), storage=storage)
        second = SymbioteKernel(config, llm=ScriptedLLM("hello from second"), storage=storage)

        sym = first.create_symbiote(name="a", role="assistant")
        session = first.start_session(sym.id)
        first.message(session.id, "one")
        second.message(session.id, "two")

        for kernel in (first, second):
            contents = [m["content"] for m in kernel.inspector.list_messages(session.id)]
            assert sorted(contents) == sorted(
                ["one", "hello from first", "two", "hello from second"]
            )
        first.shutdown()
        second.shutdown()
        storage.close()


# ── 2. caller context ─────────────────────────────────────────────────────────


class TestHostContext:
    def test_handler_receives_context_when_asked(self, tmp_path: Path) -> None:
        kernel = make_kernel(tmp_path, _once(tool_call_block("whoami")))
        sym = kernel.create_symbiote(name="a", role="assistant")
        seen: list[ToolExecContext] = []

        def whoami(params: dict, ctx: ToolExecContext) -> str:
            seen.append(ctx)
            return "ok"

        _register(kernel, sym.id, "whoami", whoami, pass_context=True)
        session = kernel.start_session(sym.id)
        kernel.message(session.id, "who", host_context={"user": "ana", "token": SECRET})

        assert len(seen) == 1
        assert seen[0].symbiote_id == sym.id
        assert seen[0].session_id == session.id
        assert seen[0].host == {"user": "ana", "token": SECRET}
        with pytest.raises(TypeError):
            seen[0].host["user"] = "eve"  # type: ignore[index]
        kernel.shutdown()

    def test_old_handler_signature_reads_the_context(self, tmp_path: Path) -> None:
        kernel = make_kernel(tmp_path, _once(tool_call_block("legacy")))
        sym = kernel.create_symbiote(name="a", role="assistant")
        seen: list[Any] = []

        def legacy(params: dict) -> str:
            seen.append(current_tool_context())
            return "ok"

        add_tool(kernel, sym.id, "legacy", legacy)
        session = kernel.start_session(sym.id)
        kernel.message(session.id, "go", host_context={"user": "ana"})

        assert seen[0].host == {"user": "ana"}
        assert seen[0].session_id == session.id
        kernel.shutdown()

    def test_no_host_context_gives_an_empty_mapping(self, tmp_path: Path) -> None:
        kernel = make_kernel(tmp_path, _once(tool_call_block("whoami")))
        sym = kernel.create_symbiote(name="a", role="assistant")
        seen: list[ToolExecContext] = []
        _register(
            kernel, sym.id, "whoami",
            lambda params, ctx: seen.append(ctx) or "ok", pass_context=True,
        )
        kernel.message(kernel.start_session(sym.id).id, "who")

        assert dict(seen[0].host) == {}
        kernel.shutdown()

    def test_llm_adapter_sees_it_and_it_is_gone_after_the_turn(
        self, tmp_path: Path
    ) -> None:
        seen: list[dict] = []

        def script(_i: int, _m: list[dict]) -> str:
            seen.append(dict(current_host_context()))
            return "hi"

        kernel = make_kernel(tmp_path, ScriptedLLM(script))
        sym = kernel.create_symbiote(name="a", role="assistant")
        kernel.message(kernel.start_session(sym.id).id, "x", host_context={"user": "ana"})

        assert seen == [{"user": "ana"}]
        assert dict(current_host_context()) == {}
        kernel.shutdown()

    def test_never_in_the_prompt_nor_in_the_database(self, tmp_path: Path) -> None:
        llm = _once(tool_call_block("whoami"))
        kernel = make_kernel(tmp_path, llm)
        sym = kernel.create_symbiote(name="a", role="assistant")
        add_tool(kernel, sym.id, "whoami", lambda params: "ok")
        session = kernel.start_session(sym.id)
        kernel.message(session.id, "who", host_context={"token": SECRET})
        kernel.close_session(session.id)

        assert SECRET not in llm.all_prompts_text()
        assert SECRET not in _dump_database(kernel._storage)
        kernel.shutdown()

    def test_approval_callback_receives_context(self, tmp_path: Path) -> None:
        kernel = make_kernel(tmp_path, _once(tool_call_block("wipe")))
        sym = kernel.create_symbiote(name="a", role="assistant")
        _register(kernel, sym.id, "wipe", lambda params: "ok", risk_level="high")
        seen: list[tuple[Any, Any]] = []

        def gate(tool_id: str, params: dict, risk: str, ctx: ToolExecContext) -> bool:
            seen.append((ctx, current_tool_context()))
            return True

        kernel.set_approval_callback(gate, pass_context=True)
        session = kernel.start_session(sym.id)
        kernel.message(session.id, "wipe", host_context={"user": "ana"})

        ctx, ambient = seen[0]
        assert ctx == ambient
        assert (ctx.symbiote_id, ctx.session_id) == (sym.id, session.id)
        assert ctx.host == {"user": "ana"}
        kernel.shutdown()


    def test_nested_turn_inherits_the_context(self, tmp_path: Path) -> None:
        """A turn started from a handler (a sub-agent) without ``host_context``
        keeps the context of the turn that started it."""
        seen: list[dict] = []

        def script(i: int, _m: list[dict]) -> str:
            seen.append(dict(current_host_context()))
            return tool_call_block("delegate") if i == 0 else "done"

        kernel = make_kernel(tmp_path, ScriptedLLM(script))
        sym = kernel.create_symbiote(name="a", role="assistant")
        child = kernel.start_session(sym.id)
        add_tool(kernel, sym.id, "delegate", lambda params: kernel.message(child.id, "sub"))
        kernel.message(kernel.start_session(sym.id).id, "go", host_context={"user": "ana"})

        assert seen == [{"user": "ana"}] * 3  # parent, child, parent again
        kernel.shutdown()


class _PerUserLLM:
    """Emits two tool calls on each user's first model call, then plain text.

    Keyed by the turn's host context, so it is safe under concurrency.
    """

    def __init__(self, *tools: str) -> None:
        self._tools = tools
        self._seen: Counter[str] = Counter()
        self._lock = threading.Lock()

    def complete(self, messages: list[dict], config: Any = None, tools: Any = None) -> str:
        user = current_host_context()["user"]
        with self._lock:
            self._seen[user] += 1
            first = self._seen[user] == 1
        if first:
            return "\n".join(tool_call_block(t, n=i) for i, t in enumerate(self._tools))
        return f"done {user}"


class TestHostContextIsolation:
    USERS = 12

    def _setup(self, tmp_path: Path, tools: tuple[str, ...]):  # noqa: ANN202
        kernel = make_kernel(tmp_path, _PerUserLLM(*tools))
        sym = kernel.create_symbiote(name="shared", role="assistant")
        sessions = {
            f"user-{i}": kernel.start_session(sym.id).id for i in range(self.USERS)
        }
        return kernel, sym, sessions

    def test_concurrent_sync_turns_never_cross(self, tmp_path: Path) -> None:
        kernel, sym, sessions = self._setup(tmp_path, ("probe", "probe"))
        seen: list[tuple[str | None, str, str]] = []
        barrier = threading.Barrier(self.USERS)

        def probe(params: dict, ctx: ToolExecContext) -> str:
            time.sleep(0.02)  # overlap the handlers of different turns
            seen.append((ctx.session_id, ctx.host["user"], threading.current_thread().name))
            return ctx.host["user"]

        _register(kernel, sym.id, "probe", probe, pass_context=True)
        replies: dict[str, str] = {}

        def turn(user: str) -> None:
            barrier.wait()
            replies[user] = _text(
                kernel.message(sessions[user], "go", host_context={"user": user})
            )

        threads = [threading.Thread(target=turn, args=(u,)) for u in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert replies == {u: f"done {u}" for u in sessions}
        assert len(seen) == 2 * self.USERS
        assert all(sessions[user] == session_id for session_id, user, _ in seen)
        # The two calls of a turn run in worker threads, not the caller's.
        assert all(name.startswith("tool-") for _, _, name in seen)
        kernel.shutdown()

    def test_concurrent_async_turns_never_cross(self, tmp_path: Path) -> None:
        kernel, sym, sessions = self._setup(tmp_path, ("probe_async", "probe_sync"))
        seen: list[tuple[str | None, str]] = []

        async def probe_async(params: dict, ctx: ToolExecContext) -> str:
            await asyncio.sleep(0.02)
            seen.append((ctx.session_id, ctx.host["user"]))
            return "ok"

        def probe_sync(params: dict) -> str:
            time.sleep(0.02)
            ctx = current_tool_context()
            assert ctx is not None
            seen.append((ctx.session_id, ctx.host["user"]))
            return "ok"

        _register(kernel, sym.id, "probe_async", probe_async, pass_context=True)
        add_tool(kernel, sym.id, "probe_sync", probe_sync)

        async def run() -> list[str]:
            return await asyncio.gather(
                *(
                    kernel.message_async(sid, "go", host_context={"user": user})
                    for user, sid in sessions.items()
                )
            )

        replies = [_text(r) for r in asyncio.run(run())]

        assert replies == [f"done {u}" for u in sessions]
        assert len(seen) == 2 * self.USERS
        assert all(sessions[user] == session_id for session_id, user in seen)
        kernel.shutdown()


# ── 3. host veto ──────────────────────────────────────────────────────────────


class TestToolGuard:
    def _kernel(self, tmp_path: Path, *blocks: str):  # noqa: ANN202
        llm = _once(*(blocks or (tool_call_block("rotate_key", provider="x"),)))
        kernel = make_kernel(tmp_path, llm)
        sym = kernel.create_symbiote(name="a", role="assistant")
        ran: list[dict] = []
        add_tool(kernel, sym.id, "rotate_key", lambda params: ran.append(params) or "rotated")
        return kernel, llm, sym, ran

    def test_denied_call_does_not_run_and_reason_is_recorded(
        self, tmp_path: Path
    ) -> None:
        kernel, llm, sym, ran = self._kernel(tmp_path)
        asked: list[tuple[str, dict, ToolExecContext]] = []

        def guard(tool_id: str, params: dict, ctx: ToolExecContext) -> ToolDecision:
            asked.append((tool_id, params, ctx))
            return ToolDecision.deny("role viewer cannot rotate keys")

        kernel.set_tool_guard(guard)
        session = kernel.start_session(sym.id)
        kernel.message(session.id, "rotate", host_context={"user": "ana"})

        assert ran == []
        assert len(asked) == 1
        assert asked[0][0] == "rotate_key"
        assert asked[0][2].session_id == session.id
        assert asked[0][2].host == {"user": "ana"}
        # Returned to the model...
        assert "denied by host: role viewer cannot rotate keys" in llm.prompt_text(1)
        # ...written to the audit log...
        audit = kernel.inspector.audit_log(sym.id)
        assert [(a["action"], a["result"]) for a in audit] == [
            ("denied", "denied:role viewer cannot rotate keys")
        ]
        # ...and to the execution trace.
        steps = json.loads(
            kernel._storage.fetch_one(
                "SELECT steps_json FROM execution_traces WHERE session_id = ?",
                (session.id,),
            )["steps_json"]
        )
        assert steps[0]["success"] is False
        assert "role viewer cannot rotate keys" in steps[0]["error"]
        kernel.shutdown()

    def test_allowed_call_runs(self, tmp_path: Path) -> None:
        kernel, _llm, sym, ran = self._kernel(tmp_path)
        kernel.set_tool_guard(lambda tool_id, params, ctx: ToolDecision.allow())
        kernel.message(kernel.start_session(sym.id).id, "rotate")

        assert ran == [{"provider": "x"}]
        kernel.shutdown()

    @pytest.mark.parametrize("bad", ["raises", "returns_none", "returns_text"])
    def test_guard_failure_denies(self, tmp_path: Path, bad: str) -> None:
        kernel, llm, sym, ran = self._kernel(tmp_path)

        def guard(tool_id: str, params: dict, ctx: ToolExecContext) -> Any:
            if bad == "raises":
                raise RuntimeError("iam down")
            return None if bad == "returns_none" else "yes"

        kernel.set_tool_guard(guard)
        kernel.message(kernel.start_session(sym.id).id, "rotate")

        assert ran == []
        assert "denied by host" in llm.prompt_text(1)
        kernel.shutdown()

    def test_policy_block_is_not_shown_to_the_guard(self, tmp_path: Path) -> None:
        kernel, llm, sym, _ran = self._kernel(tmp_path, tool_call_block("unlisted"))
        _register(kernel, sym.id, "unlisted", lambda params: "x", authorize=False)
        asked: list[str] = []
        kernel.set_tool_guard(
            lambda tool_id, params, ctx: asked.append(tool_id) or ToolDecision.allow()
        )
        kernel.message(kernel.start_session(sym.id).id, "go")

        assert asked == []
        assert "blocked" in llm.prompt_text(1)
        kernel.shutdown()

    def test_parallel_calls_are_decided_one_by_one(self, tmp_path: Path) -> None:
        kernel, llm, sym, ran = self._kernel(
            tmp_path,
            tool_call_block("rotate_key", provider="ok"),
            tool_call_block("rotate_key", provider="forbidden"),
        )
        kernel.set_tool_guard(
            lambda tool_id, params, ctx: ToolDecision(
                allowed=params["provider"] == "ok", reason="not this provider"
            )
        )
        kernel.message(kernel.start_session(sym.id).id, "rotate both")

        assert ran == [{"provider": "ok"}]
        assert "not this provider" in llm.prompt_text(1)
        kernel.shutdown()

    @pytest.mark.parametrize("kind", ["async_guard", "sync_guard"])
    def test_async_path(self, tmp_path: Path, kind: str) -> None:
        kernel, llm, sym, ran = self._kernel(
            tmp_path,
            tool_call_block("rotate_key", provider="ok"),
            tool_call_block("rotate_key", provider="forbidden"),
        )
        asked: list[str] = []

        def decide(params: dict, ctx: ToolExecContext) -> ToolDecision:
            asked.append(ctx.host["user"])
            return ToolDecision(allowed=params["provider"] == "ok", reason="nope")

        async def async_guard(tool_id: str, params: dict, ctx: ToolExecContext) -> ToolDecision:
            await asyncio.sleep(0)
            return decide(params, ctx)

        def sync_guard(tool_id: str, params: dict, ctx: ToolExecContext) -> ToolDecision:
            return decide(params, ctx)

        kernel.set_tool_guard(async_guard if kind == "async_guard" else sync_guard)
        session = kernel.start_session(sym.id)
        asyncio.run(kernel.message_async(session.id, "go", host_context={"user": "ana"}))

        assert ran == [{"provider": "ok"}]
        assert asked == ["ana", "ana"]
        assert "denied by host: nope" in llm.prompt_text(1)
        kernel.shutdown()

    def test_direct_gateway_execution_is_guarded_too(self, tmp_path: Path) -> None:
        kernel, _llm, sym, ran = self._kernel(tmp_path)
        kernel.set_tool_guard(lambda tool_id, params, ctx: ToolDecision.deny("no"))

        result = kernel.tool_gateway.execute(sym.id, None, "rotate_key", {})

        assert result.success is False
        assert result.error == "Tool 'rotate_key' denied by host: no"
        assert ran == []
        kernel.shutdown()

    def test_marker_cannot_come_from_model_output(self) -> None:
        call = ToolCall.model_validate(
            {"tool_id": "rotate_key", "params": {}, "_guard_checked": True}
        )
        assert call._guard_checked is False


# ── 4. order: policy, host guard, approval ────────────────────────────────────


class TestGateOrder:
    def _kernel(self, tmp_path: Path, *, authorize: bool = True):  # noqa: ANN202
        llm = _once(tool_call_block("wipe"))
        kernel = make_kernel(tmp_path, llm)
        sym = kernel.create_symbiote(name="a", role="assistant")
        events: list[str] = []
        _register(
            kernel, sym.id, "wipe", lambda params: events.append("run") or "wiped",
            risk_level="high", authorize=authorize,
        )

        def approve(tool_id: str, params: dict, risk: str) -> bool:
            events.append("approval")
            return True

        kernel.set_approval_callback(approve)
        return kernel, llm, sym, events

    def test_guard_runs_once_then_approval_then_handler(self, tmp_path: Path) -> None:
        kernel, _llm, sym, events = self._kernel(tmp_path)
        kernel.set_tool_guard(
            lambda tool_id, params, ctx: events.append("guard") or ToolDecision.allow()
        )
        kernel.message(kernel.start_session(sym.id).id, "wipe")

        assert events == ["guard", "approval", "run"]
        kernel.shutdown()

    def test_nobody_is_asked_to_approve_what_the_guard_denies(
        self, tmp_path: Path
    ) -> None:
        kernel, llm, sym, events = self._kernel(tmp_path)
        kernel.set_tool_guard(
            lambda tool_id, params, ctx: events.append("guard") or ToolDecision.deny("no")
        )
        kernel.message(kernel.start_session(sym.id).id, "wipe")

        assert events == ["guard"]
        assert "denied by host: no" in llm.prompt_text(1)
        kernel.shutdown()

    def test_nobody_is_asked_to_approve_what_policy_blocks(self, tmp_path: Path) -> None:
        """B-75: the approval gate used to run before the policy check."""
        kernel, llm, sym, events = self._kernel(tmp_path, authorize=False)
        kernel.message(kernel.start_session(sym.id).id, "wipe")

        assert events == []
        assert "Tool 'wipe' blocked" in llm.prompt_text(1)
        audit = kernel.inspector.audit_log(sym.id)
        assert [a["action"] for a in audit] == ["blocked"]
        kernel.shutdown()

    def test_same_order_on_the_async_path(self, tmp_path: Path) -> None:
        kernel, _llm, sym, events = self._kernel(tmp_path)
        kernel.set_tool_guard(
            lambda tool_id, params, ctx: events.append("guard") or ToolDecision.allow()
        )
        asyncio.run(kernel.message_async(kernel.start_session(sym.id).id, "wipe"))

        assert events == ["guard", "approval", "run"]
        kernel.shutdown()
