"""EventBus — what the browser sees happening inside the kernel.

Events come from several threads (the server's event loop, tool worker
threads, ``asyncio.to_thread`` LLM calls, background reflection/dream
threads). ``publish`` is safe from any of them: the fan-out to subscribers
always runs on the server loop. A ring buffer keeps recent events so a page
that reconnects can catch up.

``TURN`` tags every event published while a chat turn runs, including from
worker threads (``asyncio.to_thread`` and the tool gateway copy contextvars).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import itertools
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Turn:
    turn_id: str
    session_id: str
    symbiote_id: str


TURN: contextvars.ContextVar[Turn | None] = contextvars.ContextVar("demo_turn", default=None)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    return str(value)


class EventBus:
    def __init__(self, history: int = 800) -> None:
        self._history: deque[dict] = deque(maxlen=history)
        self._subscribers: set[asyncio.Queue[dict]] = set()
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._loop_thread = threading.get_ident()

    def publish(self, type_: str, **data: Any) -> dict:
        turn = TURN.get()
        event = {
            "type": type_,
            "ts": time.time(),
            "turn_id": turn.turn_id if turn else None,
            "session_id": data.pop("session_id", None) or (turn.session_id if turn else None),
            **_jsonable(data),
        }
        with self._lock:
            event["seq"] = next(self._seq)
            self._history.append(event)
        loop = self._loop
        if loop is None or loop.is_closed():
            return event
        if threading.get_ident() == self._loop_thread:
            self._fan_out(event)
        else:
            loop.call_soon_threadsafe(self._fan_out, event)
        return event

    def _fan_out(self, event: dict) -> None:
        for queue in list(self._subscribers):
            # A stalled page loses events; the server never blocks on it.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    def recent(self, since: int = 0, limit: int = 300) -> list[dict]:
        with self._lock:
            items = [e for e in self._history if e["seq"] > since]
        return items[-limit:]

    async def subscribe(self, since: int = 0) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        try:
            last = since
            for event in self.recent(since):
                last = event["seq"]
                yield event
            while True:
                event = await queue.get()
                if event["seq"] > last:
                    last = event["seq"]
                    yield event
        finally:
            self._subscribers.discard(queue)
