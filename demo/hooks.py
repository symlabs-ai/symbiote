"""DemoHook — turns the kernel's lifecycle hooks into page events.

``before_tool`` / ``after_tool`` fire around every tool execution (including
calls the policy blocks); ``on_context`` fires once per chat turn with the
assembled context. Nothing here changes what the kernel does.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from demo.events import TURN, EventBus
from symbiote.core.hooks import BaseHook

_PREVIEW_CHARS = 1500


def _preview(value: Any) -> str:
    if isinstance(value, dict) and "png_base64" in value:
        value = {k: v for k, v in value.items() if k != "png_base64"} | {"png_base64": "…"}
    if isinstance(value, dict | list):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = "" if value is None else str(value)
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"


class DemoHook(BaseHook):
    def __init__(self, bus: EventBus, kernel: Any, shots_dir: Path) -> None:
        self._bus = bus
        self._kernel = kernel
        self._shots_dir = shots_dir
        self._started: dict[tuple, float] = {}
        self._lock = threading.Lock()
        # Last assembled context per session, for the Context tab.
        self.last_context: dict[str, dict] = {}

    @staticmethod
    def _key(tool_id: str, params: dict) -> tuple:
        turn = TURN.get()
        return (turn.turn_id if turn else None, tool_id, json.dumps(params, sort_keys=True, default=str))

    async def before_tool(self, tool_id: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._started[self._key(tool_id, params)] = time.monotonic()
        descriptor = self._kernel.tool_gateway.get_descriptor(tool_id)
        self._bus.publish(
            "tool_start",
            tool_id=tool_id,
            name=descriptor.name if descriptor else tool_id,
            params=params,
            risk=self._kernel.tool_gateway.get_risk_level(tool_id),
            handler_type=descriptor.handler_type if descriptor else None,
        )

    async def after_tool(self, tool_id: str, params: dict[str, Any], result: Any) -> None:
        with self._lock:
            started = self._started.pop(self._key(tool_id, params), None)
        elapsed = int((time.monotonic() - started) * 1000) if started else None
        success = bool(getattr(result, "success", False))
        error = getattr(result, "error", None)
        output = getattr(result, "output", None)
        shot_url = None
        if tool_id == "browser_screenshot" and success and isinstance(output, dict):
            shot_url = self._save_screenshot(output.get("png_base64"))
        self._bus.publish(
            "tool_end",
            tool_id=tool_id,
            success=success,
            blocked=bool(error and "blocked:" in error and "not allowed" in error),
            error=error,
            output=_preview(output),
            elapsed_ms=elapsed,
            screenshot=shot_url,
        )

    def on_context(self, context: Any) -> None:
        inspection = self._kernel.context_assembler.inspect(context)
        extra = dict(context.extra_context or {})
        history = extra.pop("conversation_history", None)
        summary = {
            "symbiote_id": context.symbiote_id,
            "persona": context.persona,
            "tool_mode": context.tool_mode,
            "tool_loading": context.tool_loading,
            "context_mode": context.context_mode,
            "max_tool_iterations": context.max_tool_iterations,
            "memories": context.relevant_memories,
            "knowledge": [
                {"name": k.get("name"), "content": (k.get("content") or "")[:600]}
                for k in context.relevant_knowledge
            ],
            "tools": [
                {"tool_id": t.get("tool_id"), "name": t.get("name"), "full": bool(t.get("parameters"))}
                for t in context.available_tools
            ],
            "authorized": self._kernel.environment.list_tools(context.symbiote_id),
            "skills": [s.model_dump() if hasattr(s, "model_dump") else s for s in context.injected_skills],
            "extra_context": {k: str(v)[:800] for k, v in extra.items()},
            "history": history or "",
            "tokens": inspection.total_tokens_estimate,
            "budget": inspection.budget,
            "within_budget": inspection.within_budget,
        }
        session_id = context.session_id
        self.last_context[session_id] = summary
        self._bus.publish(
            "context",
            session_id=session_id,
            tool_mode=context.tool_mode,
            tool_loading=context.tool_loading,
            context_mode=context.context_mode,
            memories=len(context.relevant_memories),
            knowledge=len(context.relevant_knowledge),
            tools=len(context.available_tools),
            tokens=inspection.total_tokens_estimate,
            budget=inspection.budget,
        )

    def _save_screenshot(self, png_base64: str | None) -> str | None:
        if not png_base64:
            return None
        self._shots_dir.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time())}-{uuid.uuid4().hex[:6]}.png"
        (self._shots_dir / name).write_bytes(base64.b64decode(png_base64))
        return f"/shots/{name}"
