"""Domain models for the Discovery Service."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DiscoveredTool(BaseModel):
    """A tool found during environment discovery."""

    id: str
    symbiote_id: str
    tool_id: str
    name: str
    description: str = ""
    handler_type: Literal["http", "custom"] = "http"
    method: str | None = None
    url_template: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    status: Literal["pending", "approved", "disabled"] = "pending"
    source_path: str | None = None
    discovered_at: str = ""
    approved_at: str | None = None
