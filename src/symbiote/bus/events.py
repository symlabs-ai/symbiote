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
