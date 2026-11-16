"""ApprovalBroker — the human-in-the-loop gate, answered from the browser.

The kernel calls ``callback(tool_id, params, risk)`` for tools selected by the
host's approval threshold and
waits for a bool. Here the call publishes an ``approval_request`` event and
blocks its (worker) thread until the page answers through ``decide`` or the
timeout expires, which counts as a denial.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from demo.events import TURN, EventBus


class ApprovalBroker:
    def __init__(self, bus: EventBus, timeout: float = 180.0) -> None:
        self._bus = bus
        self._timeout = timeout
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def callback(self, tool_id: str, params: dict, risk: str) -> bool:
        request_id = uuid.uuid4().hex[:12]
        done = threading.Event()
        turn = TURN.get()
        entry: dict[str, Any] = {
            "id": request_id,
            "tool_id": tool_id,
            "params": params,
            "risk": risk,
            "session_id": turn.session_id if turn else None,
            "event": done,
            "approved": False,
            # A page that reloads mid-request counts down from here, not from 180 again.
            "ts": time.time(),
            "timeout_s": self._timeout,
        }
        with self._lock:
            self._pending[request_id] = entry
        self._bus.publish(
            "approval_request", id=request_id, tool_id=tool_id, params=params, risk=risk,
            timeout_s=self._timeout,
        )
        answered = done.wait(self._timeout)
        with self._lock:
            self._pending.pop(request_id, None)
        approved = bool(entry["approved"]) if answered else False
        self._bus.publish(
            "approval_result", id=request_id, tool_id=tool_id, approved=approved,
            timed_out=not answered,
        )
        return approved

    def decide(self, request_id: str, approved: bool) -> bool:
        with self._lock:
            entry = self._pending.get(request_id)
        if entry is None:
            return False
        entry["approved"] = approved
        entry["event"].set()
        return True

    def pending(self) -> list[dict]:
        with self._lock:
            return [
                {k: v for k, v in e.items() if k != "event"} for e in self._pending.values()
            ]

    def deny_all(self) -> None:
        """Release every waiting turn (used on shutdown/reset)."""
        with self._lock:
            entries = list(self._pending.values())
        for entry in entries:
            entry["approved"] = False
            entry["event"].set()
