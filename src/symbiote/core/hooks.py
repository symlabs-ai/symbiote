"""CompositeHook — composable lifecycle hooks for the agent loop.

Provides pre/post hooks for tool execution and chat turns.
Each hook runs in isolation — one hook's failure does not prevent others
from executing.

Usage::

    hooks = CompositeHook()
    hooks.add(AuditHook())
    hooks.add(MetricsHook())

    # In the runner:
    await hooks.before_tool(tool_id, params)
    result = gateway.execute(tool_id, params)
    await hooks.after_tool(tool_id, params, result)

    await hooks.before_turn(messages)
    response = llm.complete(messages)
    await hooks.after_turn(messages, response)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Hook(Protocol):
    """Protocol for lifecycle hooks.

    All methods are optional — implement only the ones you need.
    """

    async def before_tool(self, tool_id: str, params: dict[str, Any]) -> None: ...
    async def after_tool(
        self, tool_id: str, params: dict[str, Any], result: Any
    ) -> None: ...
    async def before_turn(self, messages: list[dict]) -> None: ...
    async def after_turn(self, messages: list[dict], response: str) -> None: ...


class BaseHook:
    """Base class for hooks — all methods are no-ops by default."""

    async def before_tool(self, tool_id: str, params: dict[str, Any]) -> None:
        pass

    async def after_tool(
        self, tool_id: str, params: dict[str, Any], result: Any
    ) -> None:
        pass

    async def before_turn(self, messages: list[dict]) -> None:
        pass

    async def after_turn(self, messages: list[dict], response: str) -> None:
        pass


class CompositeHook:
    """Composes multiple hooks and dispatches lifecycle events to all of them.

    Error isolation: each hook runs independently. If one raises, the error
    is logged but other hooks still execute.
    """

    def __init__(self) -> None:
        self._hooks: list[BaseHook] = []

    def add(self, hook: BaseHook) -> None:
        """Register a hook."""
        self._hooks.append(hook)

    def remove(self, hook: BaseHook) -> None:
        """Unregister a hook."""
        self._hooks.remove(hook)

    @property
    def hooks(self) -> list[BaseHook]:
        return list(self._hooks)

    async def before_tool(self, tool_id: str, params: dict[str, Any]) -> None:
        await self._dispatch("before_tool", tool_id=tool_id, params=params)

    async def after_tool(
        self, tool_id: str, params: dict[str, Any], result: Any
    ) -> None:
        await self._dispatch("after_tool", tool_id=tool_id, params=params, result=result)

    async def before_turn(self, messages: list[dict]) -> None:
        await self._dispatch("before_turn", messages=messages)

    async def after_turn(self, messages: list[dict], response: str) -> None:
        await self._dispatch("after_turn", messages=messages, response=response)

    async def _dispatch(self, method: str, **kwargs: Any) -> None:
        """Call *method* on every registered hook, isolating errors."""
        for hook in self._hooks:
            fn = getattr(hook, method, None)
            if fn is None:
                continue
            try:
                result = fn(**kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "Hook %s.%s() failed", type(hook).__name__, method,
                )
