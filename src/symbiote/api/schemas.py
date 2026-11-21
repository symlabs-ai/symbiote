"""Request / response models of the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from symbiote.api.limits import (
    MAX_KEY_CHARS,
    MAX_MESSAGE_CHARS,
    MAX_NAME_CHARS,
    MAX_TEXT_CHARS,
)

# ── Request / Response schemas ────────────────────────────────────────────


def _not_blank(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("must not be empty")
    return value


class CreateSymbioteRequest(BaseModel):
    name: str = Field(max_length=MAX_NAME_CHARS)
    role: str = Field(max_length=MAX_NAME_CHARS)
    persona_json: dict | None = None

    _name_not_blank = field_validator("name")(_not_blank)


class UpdateSymbioteRequest(BaseModel):
    name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    role: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    persona_json: dict | None = None

    _name_not_blank = field_validator("name")(_not_blank)


class SymbioteResponse(BaseModel):
    id: str
    name: str
    role: str
    status: str
    persona_json: dict | None = None


class CreateSessionRequest(BaseModel):
    symbiote_id: str = Field(max_length=MAX_KEY_CHARS)
    goal: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    external_key: str | None = Field(default=None, max_length=MAX_KEY_CHARS)
    # The end user this session belongs to: the session reads the symbiote's
    # shared memories plus this user's own, and what it learns is stored for
    # this user alone. An opaque value chosen by the host; cannot be changed.
    subject_key: str | None = Field(default=None, min_length=1, max_length=MAX_KEY_CHARS)
    # Label of the revision of the symbiote's configuration the session is
    # pinned to. Omitted: the session reads the current configuration.
    revision: str | None = Field(default=None, min_length=1, max_length=100)
    # The tools the session narrows itself to: it sees and may call only the
    # ones in this list that the configuration authorizes. Omitted or null:
    # no restriction; an empty list: no tool.
    tools_allowed: list[str] | None = Field(default=None, max_length=1000)
    # "strict": a symbiote this session delegates to only answers from a
    # revision. Omitted: "open".
    delegation: Literal["open", "strict"] | None = None


class CreateRevisionRequest(BaseModel):
    """Take a picture of the symbiote's configuration under a label."""

    label: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)


class SetSessionRevisionRequest(BaseModel):
    """Pin a session to another revision, or to none (null)."""

    revision: str | None = Field(default=None, min_length=1, max_length=100)


class SetSessionToolsRequest(BaseModel):
    """Narrow a session to a list of tools, or lift the restriction (null)."""

    tools_allowed: list[str] | None = Field(default=None, max_length=1000)


class SetSessionDelegationRequest(BaseModel):
    """Set a session's delegation mode."""

    delegation: Literal["open", "strict"] = "open"


class SetDefaultRevisionRequest(BaseModel):
    """Make a revision the symbiote's default, or leave it with none (null)."""

    label: str | None = Field(default=None, min_length=1, max_length=100)


class SessionResponse(BaseModel):
    id: str
    symbiote_id: str
    goal: str | None = None
    external_key: str | None = None
    subject_key: str | None = None
    revision: str | None = None
    tools_allowed: list[str] | None = None
    delegation: str = "open"
    status: str
    summary: str | None = None
    # False: closed with ``learn=false``; the symbiote learns nothing from it.
    learn: bool = True


class CreateMessageRequest(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(max_length=MAX_MESSAGE_CHARS)


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    created_at: str
    # Who wrote an input that the person did not write; None: the person.
    origin: str | None = None


class MemoryEntryResponse(BaseModel):
    id: str
    symbiote_id: str
    session_id: str | None = None
    subject_key: str | None = None  # None: shared by every session
    type: str
    scope: str
    content: str
    tags: list[str] = []
    importance: float
    source: str


class RegisterToolRequest(BaseModel):
    tool_id: str = Field(max_length=MAX_NAME_CHARS)
    name: str = Field(max_length=MAX_NAME_CHARS)
    description: str = Field(max_length=MAX_TEXT_CHARS)
    parameters: dict = {}
    http_method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url_template: str
    headers: dict[str, str] = {}
    timeout: float = 30.0
    body_template: dict | None = None


class ToolDescriptorResponse(BaseModel):
    tool_id: str
    name: str
    description: str
    parameters: dict = {}
    handler_type: str


class ChatRequest(BaseModel):
    content: str = Field(max_length=MAX_MESSAGE_CHARS)
    extra_context: dict | None = None
    generation_settings: dict | None = None
    # Who wrote ``content`` when the person did not ("wake", "webhook"): a
    # label of letters, digits, "_", ".", ":" or "-".
    origin: str | None = Field(
        default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
    )


class ChatResponse(BaseModel):
    response: str | dict
    session_id: str


class ResolveApprovalRequest(BaseModel):
    """Decide a tool call that waited for approval."""

    approved: bool
    note: str | None = None


class ToolExecRequest(BaseModel):
    params: dict[str, Any] = {}


class ToolExecResponse(BaseModel):
    tool_id: str
    success: bool
    output: Any = None
    error: str | None = None


class ToolTagsRequest(BaseModel):
    tags: list[str]
    loading: str = "full"  # "full" | "index" | "semantic"
    loop: bool = True  # deprecated — use tool_mode in ConfigRequest
    tool_mode: str | None = None  # "instant" | "brief" | "long_run" | "continuous"


class ConfigRequest(BaseModel):
    """Full environment configuration for a symbiote."""

    tools: list[str] | None = None  # explicit tool allowlist (PolicyGate)
    tool_mode: str | None = None  # "instant" | "brief" | "long_run" | "continuous"
    tool_loading: str | None = None  # "full" | "index" | "semantic"
    tool_tags: list[str] | None = None
    tool_visibility: str | None = None  # "tagged" | "authorized"
    max_tool_iterations: int | None = None
    tool_call_timeout: float | None = None
    loop_timeout: float | None = None
    max_turn_tokens: int | None = None  # spending ceiling of a turn; 0 = none
    max_turn_llm_calls: int | None = None
    memory_share: float | None = None
    knowledge_share: float | None = None
    context_mode: str | None = None  # "packed" | "on_demand"
    prompt_caching: bool | None = None
    # Whether sessions of end users (subject_key) feed harness, dream and skills
    learn_from_subject_sessions: bool | None = None
    skill_trial_listing: bool | None = None  # trial skills listed to the model
    dream_llm: str | None = None  # "aux" | "main"
    session_summary_mode: str | None = None  # "concat" | "llm" | "off"
    # Long-run mode
    planner_prompt: str | None = None
    evaluator_prompt: str | None = None
    evaluator_criteria: list[dict] | None = None
    context_strategy: str | None = None  # "compaction" | "reset" | "hybrid"
    max_blocks: int | None = None


class ConfigResponse(BaseModel):
    """Current environment configuration."""

    tools: list[str] = []
    tool_mode: str = "brief"
    tool_loading: str = "full"
    tool_tags: list[str] = []
    tool_visibility: str = "tagged"
    tool_loop: bool = True
    max_tool_iterations: int = 10
    tool_call_timeout: float = 30.0
    loop_timeout: float = 300.0
    max_turn_tokens: int = 0
    max_turn_llm_calls: int = 0
    memory_share: float = 0.40
    knowledge_share: float = 0.25
    context_mode: str = "packed"
    prompt_caching: bool = False
    learn_from_subject_sessions: bool = False
    skill_trial_listing: bool = False
    dream_llm: str = "aux"
    session_summary_mode: str = "concat"
    planner_prompt: str | None = None
    evaluator_prompt: str | None = None
    evaluator_criteria: list[dict] | None = None
    context_strategy: str = "hybrid"
    max_blocks: int = 20


class DiscoverRequest(BaseModel):
    source_path: str
    url: str | None = None  # live server URL to fetch /openapi.json from


class DiscoveredToolResponse(BaseModel):
    id: str
    tool_id: str
    name: str
    description: str
    handler_type: str
    method: str | None = None
    url_template: str | None = None
    parameters: dict = {}
    tags: list[str] = []
    risk_level: str = "medium"
    status: str
    source_path: str | None = None
    discovered_at: str
    approved_at: str | None = None


class DiscoverResponse(BaseModel):
    discovered: int
    tools: list[DiscoveredToolResponse]
    errors: list[str] = []


class ClassifyToolsRequest(BaseModel):
    approve_tags: list[str]
    disable_rest: bool = False


class ClassifyToolsResponse(BaseModel):
    approved: int
    disabled: int
    unchanged: int


class ResetToolsResponse(BaseModel):
    reset: int


class UpdateDiscoveredToolRequest(BaseModel):
    status: str  # "approved" | "disabled" | "pending"
