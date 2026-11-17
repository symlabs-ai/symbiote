"""Pydantic model for knowledge entries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class KnowledgeEntry(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    symbiote_id: str
    name: str
    source_path: str | None = None
    content: str | None = None
    type: Literal["document", "note", "reference", "repository"] = "document"
    tags: list[str] = Field(default_factory=list)
    # False: the entry is left out of the knowledge packed into the prompt
    # and is found only when the model looks for it (``search_knowledge``).
    auto_recall: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
