"""Tests for MessageBus — B-12."""

from __future__ import annotations

import asyncio

import pytest

from symbiote.bus.events import InboundMessage, OutboundMessage
from symbiote.bus.message_bus import MessageBus


def _make_inbound(**kwargs) -> InboundMessage:
    defaults = {
        "channel": "test",
        "chat_id": "chat-1",
        "symbiote_id": "sym-1",
        "content": "Hello",
    }
    defaults.update(kwargs)
    return InboundMessage(**defaults)


# ── Event models ─────────────────────────────────────────────────────────


class TestEvents:
    def test_inbound_has_defaults(self) -> None:
        msg = _make_inbound()
        assert msg.id is not None
        assert msg.timestamp is not None
        assert msg.channel == "test"

    def test_outbound_has_defaults(self) -> None:
        msg = OutboundMessage(channel="test", chat_id="c1", content="Hi")
        assert msg.id is not None
        assert msg.metadata == {}

    def test_inbound_extra_context(self) -> None:
        msg = _make_inbound(extra_context={"page": "/about"})
        assert msg.extra_context["page"] == "/about"


# ── MessageBus core ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestMessageBusPublishReceive:
    async def test_publish_and_receive(self) -> None:
        bus = MessageBus()
        outbound = OutboundMessage(channel="test", chat_id="c1", content="Response")
        await bus.respond(outbound)

        received = await bus.receive(timeout=1.0)
        assert received is not None
        assert received.content == "Response"

    async def test_receive_timeout(self) -> None:
        bus = MessageBus()
        result = await bus.receive(timeout=0.1)
        assert result is None

    async def test_queue_sizes(self) -> None:
        bus = MessageBus()
        assert bus.inbound_size == 0
        assert bus.outbound_size == 0

        await bus.publish(_make_inbound())
        assert bus.inbound_size == 1

        await bus.respond(OutboundMessage(channel="t", chat_id="c", content="r"))
        assert bus.outbound_size == 1


@pytest.mark.asyncio
class TestMessageBusHandler:
    async def test_sync_handler_called(self) -> None:
        bus = MessageBus()
        received: list[InboundMessage] = []

        def handler(msg: InboundMessage) -> None:
            received.append(msg)

        bus.on_inbound(handler)
        await bus.start()

        await bus.publish(_make_inbound(content="Test"))
        await asyncio.sleep(0.2)  # let the loop process

        await bus.stop()
        assert len(received) == 1
        assert received[0].content == "Test"

    async def test_async_handler_called(self) -> None:
        bus = MessageBus()
        received: list[str] = []

        async def handler(msg: InboundMessage) -> None:
            received.append(msg.content)

        bus.on_inbound(handler)
        await bus.start()

        await bus.publish(_make_inbound(content="Async"))
        await asyncio.sleep(0.2)

        await bus.stop()
        assert received == ["Async"]

    async def test_handler_error_does_not_crash_bus(self) -> None:
        bus = MessageBus()

        def bad_handler(msg: InboundMessage) -> None:
            raise ValueError("boom")

        bus.on_inbound(bad_handler)
        await bus.start()

        await bus.publish(_make_inbound())
        await asyncio.sleep(0.2)

        assert bus.is_running
        await bus.stop()

    async def test_multiple_messages_processed(self) -> None:
        bus = MessageBus()
        received: list[str] = []

        def handler(msg: InboundMessage) -> None:
            received.append(msg.content)

        bus.on_inbound(handler)
        await bus.start()

        for i in range(5):
            await bus.publish(_make_inbound(content=f"msg-{i}"))

        await asyncio.sleep(0.5)
        await bus.stop()
        assert len(received) == 5


@pytest.mark.asyncio
class TestMessageBusLifecycle:
    async def test_start_stop(self) -> None:
        bus = MessageBus()
        assert not bus.is_running

        await bus.start()
        assert bus.is_running

        await bus.stop()
        assert not bus.is_running

    async def test_double_start_is_idempotent(self) -> None:
        bus = MessageBus()
        await bus.start()
        await bus.start()  # should not create second task
        assert bus.is_running
        await bus.stop()

    async def test_stop_without_start(self) -> None:
        bus = MessageBus()
        await bus.stop()  # should not raise
        assert not bus.is_running


@pytest.mark.asyncio
class TestMessageBusIntegration:
    async def test_full_flow_inbound_to_outbound(self) -> None:
        """Simulate a full channel→kernel→channel flow."""
        bus = MessageBus()

        async def kernel_handler(msg: InboundMessage) -> None:
            response = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Echo: {msg.content}",
                in_reply_to=msg.id,
            )
            await bus.respond(response)

        bus.on_inbound(kernel_handler)
        await bus.start()

        # Channel publishes
        inbound = _make_inbound(content="Hello kernel")
        await bus.publish(inbound)

        # Channel receives
        outbound = await bus.receive(timeout=2.0)
        await bus.stop()

        assert outbound is not None
        assert outbound.content == "Echo: Hello kernel"
        assert outbound.in_reply_to == inbound.id


@pytest.mark.asyncio
class TestMessageBusRetry:
    """Tests for retry with exponential backoff."""

    async def test_handler_retried_on_failure(self) -> None:
        """Handler is retried up to max_retries times before dropping."""
        bus = MessageBus(max_retries=2, base_delay=0.01)
        call_count = 0

        def failing_then_ok(msg: InboundMessage) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("transient error")

        bus.on_inbound(failing_then_ok)
        await bus.start()
        await bus.publish(_make_inbound(content="retry-me"))
        await asyncio.sleep(0.5)
        await bus.stop()

        assert call_count == 3  # 1 initial + 2 retries

    async def test_handler_dropped_after_max_retries(self) -> None:
        """Message is dropped after all retries exhausted."""
        bus = MessageBus(max_retries=2, base_delay=0.01)
        call_count = 0

        def always_fail(msg: InboundMessage) -> None:
            nonlocal call_count
            call_count += 1
            raise ValueError("permanent error")

        bus.on_inbound(always_fail)
        await bus.start()
        await bus.publish(_make_inbound(content="fail"))
        await asyncio.sleep(0.5)
        await bus.stop()

        assert call_count == 3  # 1 initial + 2 retries
        assert bus.is_running is False  # stopped gracefully

    async def test_respond_retries_on_queue_full(self) -> None:
        """respond() retries with backoff when outbound queue is full."""
        bus = MessageBus(maxsize=1, max_retries=2, base_delay=0.01)

        # Fill the queue
        await bus.respond(OutboundMessage(channel="t", chat_id="c", content="first"))
        assert bus.outbound_size == 1

        # Start a consumer that drains after a short delay
        async def drain():
            await asyncio.sleep(0.02)
            await bus.receive(timeout=1.0)

        asyncio.create_task(drain())

        # This should retry and succeed after drain
        msg = OutboundMessage(channel="t", chat_id="c", content="second")
        await bus.respond(msg)
        # If we get here without exception, retry worked

    async def test_respond_raises_after_max_retries(self) -> None:
        """respond() raises QueueFull after all retries exhausted."""
        bus = MessageBus(maxsize=1, max_retries=1, base_delay=0.01)

        # Fill the queue
        await bus.respond(OutboundMessage(channel="t", chat_id="c", content="first"))

        with pytest.raises(asyncio.QueueFull):
            await bus.respond(
                OutboundMessage(channel="t", chat_id="c", content="overflow")
            )

    async def test_bus_continues_after_handler_exhausts_retries(self) -> None:
        """Bus keeps processing subsequent messages after one is dropped."""
        bus = MessageBus(max_retries=1, base_delay=0.01)
        results: list[str] = []
        call_count = 0

        def sometimes_fail(msg: InboundMessage) -> None:
            nonlocal call_count
            call_count += 1
            if msg.content == "fail":
                raise ValueError("boom")
            results.append(msg.content)

        bus.on_inbound(sometimes_fail)
        await bus.start()

        await bus.publish(_make_inbound(content="fail"))
        await asyncio.sleep(0.3)
        await bus.publish(_make_inbound(content="ok"))
        await asyncio.sleep(0.3)
        await bus.stop()

        assert "ok" in results
        assert bus.is_running is False
