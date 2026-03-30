"""MessageBus — async inbound/outbound queues decoupling channels from kernel."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from symbiote.bus.events import InboundMessage, OutboundMessage, StreamDelta


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
        self._deltas: asyncio.Queue[StreamDelta] = asyncio.Queue(maxsize=maxsize * 10)
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

    async def receive_delta(self, timeout: float | None = None) -> StreamDelta | None:
        """Receive a streaming delta (from kernel to channel).

        Channels that support progressive rendering should consume deltas
        in a loop until ``delta.is_final`` is True.  Returns None on timeout.
        """
        try:
            if timeout is not None:
                return await asyncio.wait_for(
                    self._deltas.get(), timeout=timeout
                )
            return await self._deltas.get()
        except TimeoutError:
            return None

    # ── Kernel-facing API ─────────────────────────────────────────────────

    def on_inbound(self, handler: Callable[[InboundMessage], Any]) -> None:
        """Register the handler called for each inbound message."""
        self._handler = handler

    async def respond(self, message: OutboundMessage) -> None:
        """Publish an outbound message (from kernel to channels)."""
        await self._outbound.put(message)

    async def send_delta(self, delta: StreamDelta) -> None:
        """Publish a streaming delta (from kernel to channels).

        Non-blocking: if the delta queue is full, the oldest delta is
        discarded to prevent blocking the LLM streaming loop.
        """
        try:
            self._deltas.put_nowait(delta)
        except asyncio.QueueFull:
            # Drop oldest delta to make room — streaming UX prefers
            # losing a token over blocking the generation loop.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._deltas.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._deltas.put_nowait(delta)

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

    @property
    def delta_size(self) -> int:
        return self._deltas.qsize()

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
