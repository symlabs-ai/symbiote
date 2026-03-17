"""MessageBus — async inbound/outbound queues decoupling channels from kernel."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from symbiote.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """Async message bus with inbound and outbound queues.

    Channels publish to inbound; the kernel consumes inbound and publishes
    outbound; channels consume outbound to deliver responses.

    Usage::

        bus = MessageBus()

        # Register handler (kernel.process)
        bus.on_inbound(handler_fn)

        # Channel publishes
        await bus.publish(inbound_msg)

        # Channel consumes
        outbound = await bus.receive()
    """

    def __init__(self, maxsize: int = 100) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=maxsize)
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=maxsize)
        self._handler: Callable[[InboundMessage], Any] | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    # ── Channel-facing API ────────────────────────────────────────────────

    async def publish(self, message: InboundMessage) -> None:
        """Publish an inbound message (from channel to kernel)."""
        await self._inbound.put(message)

    async def receive(self, timeout: float | None = None) -> OutboundMessage | None:
        """Receive an outbound message (from kernel to channel).

        Returns None if timeout expires.
        """
        try:
            if timeout is not None:
                return await asyncio.wait_for(
                    self._outbound.get(), timeout=timeout
                )
            return await self._outbound.get()
        except TimeoutError:
            return None

    # ── Kernel-facing API ─────────────────────────────────────────────────

    def on_inbound(self, handler: Callable[[InboundMessage], Any]) -> None:
        """Register the handler called for each inbound message."""
        self._handler = handler

    async def respond(self, message: OutboundMessage) -> None:
        """Publish an outbound message (from kernel to channels)."""
        await self._outbound.put(message)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the inbound processing loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._process_loop())

    async def stop(self) -> None:
        """Stop the processing loop gracefully."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def inbound_size(self) -> int:
        return self._inbound.qsize()

    @property
    def outbound_size(self) -> int:
        return self._outbound.qsize()

    # ── Internal ──────────────────────────────────────────────────────────

    async def _process_loop(self) -> None:
        """Consume inbound messages and dispatch to handler."""
        while self._running:
            try:
                msg = await asyncio.wait_for(self._inbound.get(), timeout=1.0)
            except TimeoutError:
                continue

            if self._handler is not None:
                try:
                    result = self._handler(msg)
                    # Support both sync and async handlers
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    # Handler errors should not crash the bus
                    pass
