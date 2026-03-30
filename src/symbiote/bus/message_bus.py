"""MessageBus — async inbound/outbound queues decoupling channels from kernel."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from symbiote.bus.events import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 1.0  # seconds


class MessageBus:
    """Async message bus with inbound and outbound queues.

    Channels publish to inbound; the kernel consumes inbound and publishes
    outbound; channels consume outbound to deliver responses.

    Handler failures are retried with exponential backoff (1s, 2s, 4s)
    before the message is dropped.

    Usage::

        bus = MessageBus()

        # Register handler (kernel.process)
        bus.on_inbound(handler_fn)

        # Channel publishes
        await bus.publish(inbound_msg)

        # Channel consumes
        outbound = await bus.receive()
    """

    def __init__(
        self,
        maxsize: int = 100,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_delay: float = _DEFAULT_BASE_DELAY,
    ) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=maxsize)
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=maxsize)
        self._handler: Callable[[InboundMessage], Any] | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._max_retries = max_retries
        self._base_delay = base_delay

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
        """Publish an outbound message (from kernel to channels).

        Retries with exponential backoff if the outbound queue is full.
        """
        for attempt in range(self._max_retries + 1):
            try:
                self._outbound.put_nowait(message)
                return
            except asyncio.QueueFull:
                if attempt >= self._max_retries:
                    logger.error(
                        "outbound queue full after %d retries, dropping message %s",
                        self._max_retries, message.id,
                    )
                    raise
                delay = self._base_delay * (2 ** attempt)
                logger.warning(
                    "outbound queue full, retry %d/%d in %.1fs",
                    attempt + 1, self._max_retries, delay,
                )
                await asyncio.sleep(delay)

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
        """Consume inbound messages and dispatch to handler.

        Handler failures are retried with exponential backoff.
        """
        while self._running:
            try:
                msg = await asyncio.wait_for(self._inbound.get(), timeout=1.0)
            except TimeoutError:
                continue

            if self._handler is not None:
                await self._dispatch_with_retry(msg)

    async def _dispatch_with_retry(self, msg: InboundMessage) -> None:
        """Dispatch a message to the handler with exponential backoff on failure."""
        for attempt in range(self._max_retries + 1):
            try:
                result = self._handler(msg)
                if asyncio.iscoroutine(result):
                    await result
                return
            except Exception:
                if attempt >= self._max_retries:
                    logger.error(
                        "handler failed after %d retries for message %s, dropping",
                        self._max_retries + 1, msg.id,
                    )
                    return
                delay = self._base_delay * (2 ** attempt)
                logger.warning(
                    "handler error on message %s, retry %d/%d in %.1fs",
                    msg.id, attempt + 1, self._max_retries, delay,
                )
                await asyncio.sleep(delay)
