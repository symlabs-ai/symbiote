"""Symbiote CRUD, tool tags and per-symbiote config."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from symbiote.api.auth import APIKey
from symbiote.api.http import (
    _require_symbiote,
    get_env_manager,
    get_identity_manager,
)
from symbiote.api.middleware import (
    require_auth,
)
from symbiote.api.schemas import (
    ConfigRequest,
    ConfigResponse,
    CreateSymbioteRequest,
    SymbioteResponse,
    ToolTagsRequest,
    UpdateSymbioteRequest,
)
from symbiote.core.identity import IdentityManager
from symbiote.environment.manager import EnvironmentManager

router = APIRouter()


# ── Symbiote endpoints ───────────────────────────────────────────────────


@router.post("/symbiotes", status_code=201, response_model=SymbioteResponse)
def create_symbiote(
    body: CreateSymbioteRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
) -> SymbioteResponse:
    sym = identity.create(
        name=body.name,
        role=body.role,
        persona=body.persona_json,
        owner_id=auth.tenant_id,
    )
    return SymbioteResponse(
        id=sym.id,
        name=sym.name,
        role=sym.role,
        status=sym.status,
        persona_json=sym.persona_json,
    )


@router.get("/symbiotes/{symbiote_id}", response_model=SymbioteResponse)
def get_symbiote(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
) -> SymbioteResponse:
    sym = _require_symbiote(identity, symbiote_id, auth)
    return SymbioteResponse(
        id=sym.id,
        name=sym.name,
        role=sym.role,
        status=sym.status,
        persona_json=sym.persona_json,
    )


@router.put("/symbiotes/{symbiote_id}", response_model=SymbioteResponse)
def update_symbiote(
    symbiote_id: str,
    body: UpdateSymbioteRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
) -> SymbioteResponse:
    sym = _require_symbiote(identity, symbiote_id, auth)
    sym = identity.update(
        symbiote_id,
        name=body.name,
        role=body.role,
        persona=body.persona_json,
    )
    return SymbioteResponse(
        id=sym.id,
        name=sym.name,
        role=sym.role,
        status=sym.status,
        persona_json=sym.persona_json,
    )


@router.delete("/symbiotes/{symbiote_id}")
def delete_symbiote(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
) -> Response:
    _require_symbiote(identity, symbiote_id, auth)
    identity.delete(symbiote_id)
    return Response(status_code=204)


# ── Tool Tags endpoints ──────────────────────────────────────────────────


@router.put("/symbiotes/{symbiote_id}/tool-tags", status_code=200)
def set_tool_tags(
    symbiote_id: str,
    body: ToolTagsRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
    env: Annotated[EnvironmentManager, Depends(get_env_manager)],
) -> dict:
    """Set tool visibility tags for a symbiote."""
    _require_symbiote(identity, symbiote_id, auth)

    kwargs: dict[str, Any] = {"tool_tags": body.tags, "tool_loading": body.loading, "tool_loop": body.loop}
    if body.tool_mode is not None:
        kwargs["tool_mode"] = body.tool_mode
    env.configure(symbiote_id=symbiote_id, **kwargs)
    mode = env.get_tool_mode(symbiote_id)
    return {"tags": body.tags, "loading": body.loading, "loop": body.loop, "tool_mode": mode}


@router.get("/symbiotes/{symbiote_id}/tool-tags")
def get_tool_tags(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
    env: Annotated[EnvironmentManager, Depends(get_env_manager)],
) -> dict:
    """Get tool visibility tags for a symbiote."""
    _require_symbiote(identity, symbiote_id, auth)

    tags = env.get_tool_tags(symbiote_id)
    loading = env.get_tool_loading(symbiote_id)
    loop = env.get_tool_loop(symbiote_id)
    mode = env.get_tool_mode(symbiote_id)
    return {"tags": tags, "loading": loading, "loop": loop, "tool_mode": mode}


# ── Symbiote Config endpoints ────────────────────────────────────────────


@router.put("/symbiotes/{symbiote_id}/config", response_model=ConfigResponse)
def set_config(
    symbiote_id: str,
    body: ConfigRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
    env: Annotated[EnvironmentManager, Depends(get_env_manager)],
) -> ConfigResponse:
    """Set environment configuration for a symbiote.

    All fields are optional — only provided fields are updated.
    Supports tool_mode (instant/brief/long_run/continuous), timeouts,
    memory/knowledge shares, context mode, and long-run config
    (planner_prompt, evaluator_prompt, evaluator_criteria, etc.).
    """
    _require_symbiote(identity, symbiote_id, auth)

    # Build kwargs from non-None fields
    kwargs: dict[str, Any] = {}
    for field_name in (
        "tools", "tool_mode", "tool_loading", "tool_tags", "tool_visibility",
        "max_tool_iterations",
        "tool_call_timeout", "loop_timeout", "memory_share", "knowledge_share",
        "context_mode", "prompt_caching", "learn_from_subject_sessions",
    ):
        val = getattr(body, field_name, None)
        if val is not None:
            kwargs[field_name] = val

    if kwargs:
        env.configure(symbiote_id=symbiote_id, **kwargs)

    # Long-run fields are stored directly on the config model
    # (they pass through configure via the model fields)
    lr_fields = {}
    for field_name in ("planner_prompt", "evaluator_prompt", "evaluator_criteria",
                       "context_strategy", "max_blocks"):
        val = getattr(body, field_name, None)
        if val is not None:
            lr_fields[field_name] = val
    if lr_fields:
        env.configure(symbiote_id=symbiote_id, **lr_fields)

    return _build_config_response(env, symbiote_id)


@router.get("/symbiotes/{symbiote_id}/config", response_model=ConfigResponse)
def get_config(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
    env: Annotated[EnvironmentManager, Depends(get_env_manager)],
) -> ConfigResponse:
    """Get full environment configuration for a symbiote."""
    _require_symbiote(identity, symbiote_id, auth)

    return _build_config_response(env, symbiote_id)


def _build_config_response(env: EnvironmentManager, symbiote_id: str) -> ConfigResponse:
    """Build a ConfigResponse from the current EnvironmentConfig."""
    cfg = env.get_config(symbiote_id)
    if cfg is None:
        return ConfigResponse()

    lr_cfg = env.get_long_run_config(symbiote_id)
    return ConfigResponse(
        tools=cfg.tools,
        tool_mode=cfg.tool_mode,
        tool_loading=cfg.tool_loading,
        tool_tags=cfg.tool_tags,
        tool_visibility=cfg.tool_visibility,
        tool_loop=cfg.tool_loop,
        max_tool_iterations=cfg.max_tool_iterations,
        tool_call_timeout=cfg.tool_call_timeout,
        loop_timeout=cfg.loop_timeout,
        memory_share=cfg.memory_share,
        knowledge_share=cfg.knowledge_share,
        context_mode=cfg.context_mode,
        prompt_caching=cfg.prompt_caching,
        learn_from_subject_sessions=cfg.learn_from_subject_sessions,
        planner_prompt=lr_cfg.get("planner_prompt"),
        evaluator_prompt=lr_cfg.get("evaluator_prompt"),
        evaluator_criteria=lr_cfg.get("evaluator_criteria"),
        context_strategy=lr_cfg.get("context_strategy", "hybrid"),
        max_blocks=lr_cfg.get("max_blocks", 20),
    )
