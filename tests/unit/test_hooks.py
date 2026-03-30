"""Tests for CompositeHook — composable lifecycle hooks (B-50)."""

from __future__ import annotations

from typing import Any

import pytest

from symbiote.core.hooks import BaseHook, CompositeHook


class RecordingHook(BaseHook):
    """Hook that records all calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def before_tool(self, tool_id: str, params: dict[str, Any]) -> None:
        self.calls.append(("before_tool", {"tool_id": tool_id, "params": params}))

    async def after_tool(self, tool_id: str, params: dict[str, Any], result: Any) -> None:
        self.calls.append(("after_tool", {"tool_id": tool_id, "result": result}))

    async def before_turn(self, messages: list[dict]) -> None:
        self.calls.append(("before_turn", {"count": len(messages)}))

    async def after_turn(self, messages: list[dict], response: str) -> None:
        self.calls.append(("after_turn", {"response": response}))


class FailingHook(BaseHook):
    """Hook that always raises."""

    async def before_tool(self, tool_id: str, params: dict[str, Any]) -> None:
        raise RuntimeError("hook exploded")

    async def after_tool(self, tool_id: str, params: dict[str, Any], result: Any) -> None:
        raise RuntimeError("hook exploded")


@pytest.mark.asyncio
class TestCompositeHook:
    async def test_before_tool_dispatched(self) -> None:
        hook = RecordingHook()
        composite = CompositeHook()
        composite.add(hook)

        await composite.before_tool("fs_read", {"path": "/tmp"})

        assert len(hook.calls) == 1
        assert hook.calls[0][0] == "before_tool"
        assert hook.calls[0][1]["tool_id"] == "fs_read"

    async def test_after_tool_dispatched(self) -> None:
        hook = RecordingHook()
        composite = CompositeHook()
        composite.add(hook)

        await composite.after_tool("fs_read", {"path": "/tmp"}, {"data": "ok"})

        assert hook.calls[0][0] == "after_tool"
        assert hook.calls[0][1]["result"] == {"data": "ok"}

    async def test_before_turn_dispatched(self) -> None:
        hook = RecordingHook()
        composite = CompositeHook()
        composite.add(hook)

        await composite.before_turn([{"role": "user", "content": "hi"}])

        assert hook.calls[0][0] == "before_turn"
        assert hook.calls[0][1]["count"] == 1

    async def test_after_turn_dispatched(self) -> None:
        hook = RecordingHook()
        composite = CompositeHook()
        composite.add(hook)

        await composite.after_turn([], "response text")

        assert hook.calls[0][0] == "after_turn"
        assert hook.calls[0][1]["response"] == "response text"

    async def test_multiple_hooks_all_called(self) -> None:
        h1, h2 = RecordingHook(), RecordingHook()
        composite = CompositeHook()
        composite.add(h1)
        composite.add(h2)

        await composite.before_tool("test_tool", {})

        assert len(h1.calls) == 1
        assert len(h2.calls) == 1

    async def test_error_isolation(self) -> None:
        """Failing hook does not prevent other hooks from running."""
        failing = FailingHook()
        recording = RecordingHook()
        composite = CompositeHook()
        composite.add(failing)
        composite.add(recording)

        await composite.before_tool("test", {})

        # Recording hook still got called despite failing hook
        assert len(recording.calls) == 1

    async def test_remove_hook(self) -> None:
        hook = RecordingHook()
        composite = CompositeHook()
        composite.add(hook)
        composite.remove(hook)

        await composite.before_tool("test", {})

        assert len(hook.calls) == 0

    async def test_hooks_property(self) -> None:
        h1, h2 = RecordingHook(), RecordingHook()
        composite = CompositeHook()
        composite.add(h1)
        composite.add(h2)

        assert len(composite.hooks) == 2

    async def test_empty_composite_is_noop(self) -> None:
        composite = CompositeHook()
        # Should not raise
        await composite.before_tool("test", {})
        await composite.after_tool("test", {}, None)
        await composite.before_turn([])
        await composite.after_turn([], "")
