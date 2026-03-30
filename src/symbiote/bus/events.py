"""Message events for the bus."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from symbiote.core.models import _utcnow, _uuid


class InboundMessage(BaseModel):
    """A message arriving from a channel into the kernel."""

    id: str = Field(default_factory=_uuid)
    channel: str  # e.g. "telegram", "http", "cli"
    chat_id: str  # unique conversation identifier within the channel
    symbiote_id: str
    content: str
    sender_id: str | None = None
    extra_context: dict | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class OutboundMessage(BaseModel):
    """A response from the kernel going back to a channel."""

    id: str = Field(default_factory=_uuid)
    channel: str
    chat_id: str
    content: str
    in_reply_to: str | None = None  # InboundMessage.id
    metadata: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utcnow)


class StreamDelta(BaseModel):
    """An incremental token delta streamed from the kernel to channels.

    Channels that support progressive rendering (SSE, WebSocket, Telegram
    edit-message) can consume deltas for real-time UX.  Channels without
    streaming support ignore deltas and wait for the final OutboundMessage.
    """

    channel: str
    chat_id: str
    delta: str  # the token text
    in_reply_to: str | None = None  # InboundMessage.id
    is_final: bool = False  # True on the last delta (stream complete)
