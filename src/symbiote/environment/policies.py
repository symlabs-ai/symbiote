"""PolicyGate — enforce tool-access policies and audit all executions."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from symbiote.core.ports import StoragePort
from symbiote.environment.manager import EnvironmentManager

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
        """Check if a tool is authorized for a symbiote (+ optional workspace)."""
        if self._env.is_tool_enabled(symbiote_id, tool_id, workspace_id):
            return PolicyResult(allowed=True, reason=f"Tool '{tool_id}' is authorized")
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
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(action_fn, params)
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
                coro = asyncio.to_thread(action_fn, params)
            output = await asyncio.wait_for(coro, timeout=timeout)
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
