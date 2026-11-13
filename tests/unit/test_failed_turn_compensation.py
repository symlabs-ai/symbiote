"""Regression: a failed turn leaves no orphan user message, and the bus
reports a handler that keeps failing (report item E-12)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from symbiote.bus.events import InboundMessage
from symbiote.bus.message_bus import MessageBus
from symbiote.core.capabilities import CapabilityError
from tests.support import ScriptedLLM, make_kernel


def _failing(i: int, messages: list[dict]) -> str:
    raise RuntimeError("400 invalid_request: context_length_exceeded")


@pytest.mark.parametrize("entry", ["sync", "async"])
def test_failed_turn_removes_its_user_message(tmp_path: Path, entry: str) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM(_failing))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)

    for _ in range(3):
        with pytest.raises(CapabilityError):
            if entry == "sync":
                kernel.message(session.id, "charge 100")
            else:
                asyncio.run(kernel.message_async(session.id, "charge 100"))

    assert kernel._sessions.get_messages(session.id) == []
    kernel.shutdown()


def test_successful_turn_keeps_both_messages(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)

    kernel.message(session.id, "hi")

    assert [m.role for m in reversed(kernel._sessions.get_messages(session.id))] == [
        "user",
        "assistant",
    ]
    kernel.shutdown()


def test_bus_reports_exhausted_handler(caplog) -> None:
    async def scenario():
        bus = MessageBus(max_retries=2, base_delay=0.01)

        async def handler(msg):
            raise RuntimeError("boom")

        bus.on_inbound(handler)
        await bus.start()
        msg = InboundMessage(channel="test", chat_id="c1", symbiote_id="s", content="hi")
        await bus.publish(msg)
        out = await bus.receive(timeout=2.0)
        await bus.stop()
        return msg, out

    with caplog.at_level(logging.WARNING, logger="symbiote.bus.message_bus"):
        msg, out = asyncio.run(scenario())

    assert out is not None
    assert out.in_reply_to == msg.id
    assert out.chat_id == "c1"
    assert out.metadata.get("error") is True
    assert any(r.exc_info for r in caplog.records)
