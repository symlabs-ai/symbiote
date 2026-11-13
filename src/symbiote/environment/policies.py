"""PolicyGate — enforce tool-access policies and audit all executions."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from symbiote.core.ports import StoragePort
from symbiote.environment.manager import EnvironmentManager

logger = logging.getLogger(__name__)


def run_in_daemon_thread(
    fn: Callable[[dict], Any], params: dict, *, name: str = "symbiote-tool"
) -> concurrent.futures.Future:
    """Start ``fn(params)`` in a daemon thread and return its future.

    Used to bound tool execution with a timeout. A Python thread cannot be
    killed: after a timeout the handler keeps running in the background and
    its side effects may still happen. The thread is a daemon, so a hung
    handler neither blocks the caller past its timeout nor keeps the process
    alive at exit. The caller's ``contextvars`` (host auth, current user,
    tool execution context) are copied into the thread.
    """
    future: concurrent.futures.Future = concurrent.futures.Future()
    ctx = contextvars.copy_context()

    def target() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(ctx.run(fn, params))
        except BaseException as exc:  # noqa: BLE001 — delivered to the caller
            future.set_exception(exc)

    threading.Thread(target=target, name=name, daemon=True).start()
    return future


def log_tool_timeout(tool_id: str, timeout: float) -> None:
    logger.warning(
        "Tool %s timed out after %ss; its handler keeps running in the "
        "background (threads cannot be cancelled)",
        tool_id,
        timeout,
    )


# ── Models ────────────────────────────────────────────────────────────────────


class PolicyResult(BaseModel):
    allowed: bool
    reason: str


class ToolResult(BaseModel):
    success: bool
    tool_id: str
    output: Any = None
    error: str | None = None


# ── PolicyGate ────────────────────────────────────────────────────────────────


# Meta-tools that a mode turns on by itself (see PolicyGate.check).
INDEX_META_TOOL = "get_tool_schema"
ON_DEMAND_META_TOOLS = ("search_memories", "search_knowledge")


class PolicyGate:
    """Checks tool authorization against EnvironmentManager configs and logs to audit_log."""

    def __init__(self, env_manager: EnvironmentManager, storage: StoragePort) -> None:
        self._env = env_manager
        self._storage = storage

    # ── public API ────────────────────────────────────────────────────────

    def check(
        self,
        symbiote_id: str,
        tool_id: str,
        workspace_id: str | None = None,
    ) -> PolicyResult:
        """Check if a tool is authorized for a symbiote (+ optional workspace).

        The meta-tools of a mode need no manual authorization while that mode
        is on: ``get_tool_schema`` with ``tool_loading="index"``, and
        ``search_memories`` / ``search_knowledge`` with
        ``context_mode="on_demand"`` (the prompt tells the model to use them).
        """
        if self._env.is_tool_enabled(symbiote_id, tool_id, workspace_id):
            return PolicyResult(allowed=True, reason=f"Tool '{tool_id}' is authorized")
        if tool_id == INDEX_META_TOOL and (
            self._env.get_tool_loading(symbiote_id, workspace_id) == "index"
        ):
            return PolicyResult(allowed=True, reason="index mode meta-tool")
        if tool_id in ON_DEMAND_META_TOOLS and (
            self._env.get_context_mode(symbiote_id, workspace_id) == "on_demand"
        ):
            return PolicyResult(allowed=True, reason="on_demand mode meta-tool")
        return PolicyResult(
            allowed=False,
            reason=f"Tool '{tool_id}' is not allowed (deny by default)",
        )

    def execute_with_policy(
        self,
        symbiote_id: str,
        session_id: str | None,
        tool_id: str,
        params: dict,
        action_fn: Callable,
        workspace_id: str | None = None,
        timeout: float = 30.0,
    ) -> ToolResult:
        """Check policy, execute if allowed, and always write to audit_log."""
        policy = self.check(symbiote_id, tool_id, workspace_id)

        if not policy.allowed:
            self._write_audit(
                symbiote_id=symbiote_id,
                session_id=session_id,
                tool_id=tool_id,
                action="blocked",
                params=params,
                result="blocked",
            )
            return ToolResult(
                success=False,
                tool_id=tool_id,
                error=f"Tool '{tool_id}' blocked: {policy.reason}",
            )

        try:
            # Propagate the caller's contextvars (e.g. host auth /
            # current-user state) into the worker thread so handlers and
            # header_factory observe them — a bare pool thread starts empty.
            future = run_in_daemon_thread(action_fn, params, name=f"tool-{tool_id}")
            output = future.result(timeout=timeout)
            self._write_audit(
                symbiote_id=symbiote_id,
                session_id=session_id,
                tool_id=tool_id,
                action="execute",
                params=params,
                result="success",
            )
            return ToolResult(success=True, tool_id=tool_id, output=output)
        except FuturesTimeoutError:
            log_tool_timeout(tool_id, timeout)
            self._write_audit(
                symbiote_id=symbiote_id,
                session_id=session_id,
                tool_id=tool_id,
                action="execute",
                params=params,
                result="error:TimeoutError",
            )
            return ToolResult(
                success=False,
                tool_id=tool_id,
                error=f"Tool execution timed out after {timeout}s",
            )
        except Exception as exc:
            self._write_audit(
                symbiote_id=symbiote_id,
                session_id=session_id,
                tool_id=tool_id,
                action="execute",
                params=params,
                result=f"error:{type(exc).__name__}",
            )
            return ToolResult(
                success=False,
                tool_id=tool_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def execute_with_policy_async(
        self,
        symbiote_id: str,
        session_id: str | None,
        tool_id: str,
        params: dict,
        action_fn: Callable,
        workspace_id: str | None = None,
        timeout: float = 30.0,
    ) -> ToolResult:
        """Async variant: check policy, execute (awaiting coroutines), and audit."""
        policy = self.check(symbiote_id, tool_id, workspace_id)

        if not policy.allowed:
            self._write_audit(
                symbiote_id=symbiote_id,
                session_id=session_id,
                tool_id=tool_id,
                action="blocked",
                params=params,
                result="blocked",
            )
            return ToolResult(
                success=False,
                tool_id=tool_id,
                error=f"Tool '{tool_id}' blocked: {policy.reason}",
            )

        try:
            if inspect.iscoroutinefunction(action_fn):
                coro = action_fn(params)
            else:
                # Daemon thread instead of asyncio.to_thread: a hung sync
                # handler must not hold the loop's default executor (which
                # asyncio.run() waits for on exit).
                coro = asyncio.wrap_future(
                    run_in_daemon_thread(action_fn, params, name=f"tool-{tool_id}")
                )
            output = await asyncio.wait_for(coro, timeout=timeout)
            if inspect.isawaitable(output):
                # A sync wrapper returning a coroutine (e.g. a lambda around
                # an async function).
                output = await asyncio.wait_for(output, timeout=timeout)
            self._write_audit(
                symbiote_id=symbiote_id,
                session_id=session_id,
                tool_id=tool_id,
                action="execute",
                params=params,
                result="success",
            )
            return ToolResult(success=True, tool_id=tool_id, output=output)
        except TimeoutError:
            log_tool_timeout(tool_id, timeout)
            self._write_audit(
                symbiote_id=symbiote_id,
                session_id=session_id,
                tool_id=tool_id,
                action="execute",
                params=params,
                result="error:TimeoutError",
            )
            return ToolResult(
                success=False,
                tool_id=tool_id,
                error=f"Tool execution timed out after {timeout}s",
            )
        except Exception as exc:
            self._write_audit(
                symbiote_id=symbiote_id,
                session_id=session_id,
                tool_id=tool_id,
                action="execute",
                params=params,
                result=f"error:{type(exc).__name__}",
            )
            return ToolResult(
                success=False,
                tool_id=tool_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    def get_audit_log(
        self, symbiote_id: str, limit: int = 50
    ) -> list[dict]:
        """Return audit log entries for a symbiote, most recent first."""
        return self._storage.fetch_all(
            "SELECT * FROM audit_log "
            "WHERE symbiote_id = ? "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (symbiote_id, limit),
        )

    # ── private helpers ───────────────────────────────────────────────────

    def _write_audit(
        self,
        symbiote_id: str,
        session_id: str | None,
        tool_id: str,
        action: str,
        params: dict,
        result: str,
    ) -> None:
        self._storage.execute(
            "INSERT INTO audit_log "
            "(id, symbiote_id, session_id, tool_id, action, params_json, result, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                symbiote_id,
                session_id,
                tool_id,
                action,
                json.dumps(params),
                result,
                datetime.now(tz=UTC).isoformat(),
            ),
        )
