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
class TestStreamDelta:
    """Tests for delta streaming through the message bus."""

    async def test_send_and_receive_delta(self) -> None:
        from symbiote.bus.events import StreamDelta

        bus = MessageBus()
        delta = StreamDelta(channel="ws", chat_id="c1", delta="Hello")
        await bus.send_delta(delta)

        received = await bus.receive_delta(timeout=1.0)
        assert received is not None
        assert received.delta == "Hello"
        assert received.channel == "ws"

    async def test_receive_delta_timeout(self) -> None:
        bus = MessageBus()
        result = await bus.receive_delta(timeout=0.05)
        assert result is None

    async def test_delta_is_final_flag(self) -> None:
        from symbiote.bus.events import StreamDelta

        bus = MessageBus()
        await bus.send_delta(StreamDelta(channel="ws", chat_id="c1", delta="end", is_final=True))

        received = await bus.receive_delta(timeout=1.0)
        assert received is not None
        assert received.is_final is True

    async def test_delta_queue_overflow_drops_oldest(self) -> None:
        """When delta queue is full, oldest delta is dropped."""
        from symbiote.bus.events import StreamDelta

        bus = MessageBus(maxsize=1)  # delta queue = maxsize * 10 = 10

        # Fill the delta queue
        for i in range(10):
            await bus.send_delta(StreamDelta(channel="ws", chat_id="c1", delta=f"tok-{i}"))

        # Send one more — should succeed by dropping oldest
        await bus.send_delta(StreamDelta(channel="ws", chat_id="c1", delta="overflow"))

        # First token should be dropped — we get tok-1 or later
        first = await bus.receive_delta(timeout=0.1)
        assert first is not None
        assert first.delta != "tok-0"  # oldest was dropped

    async def test_delta_size_property(self) -> None:
        from symbiote.bus.events import StreamDelta

        bus = MessageBus()
        assert bus.delta_size == 0

        await bus.send_delta(StreamDelta(channel="ws", chat_id="c1", delta="a"))
        assert bus.delta_size == 1

    async def test_stream_delta_full_flow(self) -> None:
        """Simulate kernel streaming tokens through the bus to a channel."""
        from symbiote.bus.events import StreamDelta

        bus = MessageBus()
        tokens = ["Hello", " ", "world", "!"]

        # Kernel sends deltas
        for i, tok in enumerate(tokens):
            await bus.send_delta(StreamDelta(
                channel="sse",
                chat_id="c1",
                delta=tok,
                is_final=(i == len(tokens) - 1),
            ))

        # Channel consumes
        received_tokens: list[str] = []
        while True:
            d = await bus.receive_delta(timeout=0.1)
            if d is None:
                break
            received_tokens.append(d.delta)
            if d.is_final:
                break

        assert "".join(received_tokens) == "Hello world!"
