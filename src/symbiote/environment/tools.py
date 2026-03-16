"""ToolGateway — registry of tool implementations with policy-gated execution."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from symbiote.environment.policies import PolicyGate, ToolResult


class ToolGateway:
    """Manages tool registration and delegates execution through PolicyGate."""

    def __init__(self, policy_gate: PolicyGate) -> None:
        self._gate = policy_gate
        self._registry: dict[str, Callable[[dict], Any]] = {}
        self._register_builtins()

    # ── public API ────────────────────────────────────────────────────────

    def register_tool(self, tool_id: str, handler: Callable[[dict], Any]) -> None:
        """Register a tool handler."""
        self._registry[tool_id] = handler

    def execute(
        self,
        symbiote_id: str,
        session_id: str | None,
        tool_id: str,
        params: dict,
        workspace_id: str | None = None,
    ) -> ToolResult:
        """Execute a tool through the policy gate.

        Returns ToolResult with success=False and error="Tool not registered"
        if the tool_id has no registered handler.
        """
        if tool_id not in self._registry:
            return ToolResult(
                success=False,
                tool_id=tool_id,
                error="Tool not registered",
            )

        return self._gate.execute_with_policy(
            symbiote_id=symbiote_id,
            session_id=session_id,
            tool_id=tool_id,
            params=params,
            action_fn=self._registry[tool_id],
            workspace_id=workspace_id,
        )

    def list_tools(self) -> list[str]:
        """Return registered tool IDs."""
        return list(self._registry.keys())

    def has_tool(self, tool_id: str) -> bool:
        """Check if a tool is registered."""
        return tool_id in self._registry

    # ── built-in tools ────────────────────────────────────────────────────

    def _register_builtins(self) -> None:
        self.register_tool("fs_read", _fs_read)
        self.register_tool("fs_write", _fs_write)
        self.register_tool("fs_list", _fs_list)


# ── built-in handler implementations ─────────────────────────────────────


def _validate_path(params: dict) -> Path:
    """Resolve and validate path against allowed_root if provided."""
    p = Path(params["path"]).resolve()
    allowed_root = params.get("allowed_root")
    if allowed_root:
        root = Path(allowed_root).resolve()
        if not str(p).startswith(str(root)):
            raise PermissionError(f"Path {p} is outside allowed root {root}")
    if p.is_symlink():
        real = p.resolve()
        if allowed_root and not str(real).startswith(str(Path(allowed_root).resolve())):
            raise PermissionError(f"Symlink {p} escapes allowed root")
    return p


def _fs_read(params: dict) -> str:
    """Read file content. Params: {"path": str, "allowed_root": str (optional)}."""
    p = _validate_path(params)
    return p.read_text(encoding="utf-8")


def _fs_write(params: dict) -> str:
    """Write content to file. Params: {"path": str, "content": str, "allowed_root": str (optional)}."""
    p = _validate_path(params)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(params["content"], encoding="utf-8")
    return "ok"


def _fs_list(params: dict) -> list[str]:
    """List filenames in directory. Params: {"path": str, "allowed_root": str (optional)}."""
    p = _validate_path(params)
    return os.listdir(str(p))
