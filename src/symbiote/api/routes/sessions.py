"""Sessions, messages, memory search and chat."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from symbiote.api.auth import APIKey
from symbiote.api.http import (
    _require_session_access,
    _require_symbiote_access,
    get_adapter,
    get_kernel,
    get_memory_store,
    get_session_manager,
)
from symbiote.api.middleware import (
    require_auth,
)
from symbiote.api.schemas import (
    ChatRequest,
    ChatResponse,
    CreateMessageRequest,
    CreateSessionRequest,
    MemoryEntryResponse,
    MessageResponse,
    SessionResponse,
)
from symbiote.core.exceptions import (
    EntityNotFoundError,
)
from symbiote.core.kernel import SymbioteKernel
from symbiote.core.ports import StoragePort
from symbiote.core.session import SessionManager
from symbiote.memory.store import MemoryStore

router = APIRouter()


# ── Session endpoints ────────────────────────────────────────────────────


@router.post("/sessions", status_code=201, response_model=SessionResponse)
def create_session(
    body: CreateSessionRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    sessions: Annotated[SessionManager, Depends(get_session_manager)],
) -> SessionResponse:
    _require_symbiote_access(adapter, body.symbiote_id, auth)
    if body.external_key:
        sess = sessions.get_or_create_by_external_key(
            symbiote_id=body.symbiote_id,
            external_key=body.external_key,
            goal=body.goal,
        )
    else:
        sess = sessions.start(symbiote_id=body.symbiote_id, goal=body.goal)
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
        external_key=sess.external_key,
        status=sess.status,
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    sessions: Annotated[SessionManager, Depends(get_session_manager)],
) -> SessionResponse:
    _require_session_access(adapter, session_id, auth)
    sess = sessions.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found") from None
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
        external_key=sess.external_key,
        status=sess.status,
        summary=sess.summary,
    )


@router.get("/sessions/by-key/{external_key}", response_model=SessionResponse)
def get_session_by_key(
    external_key: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    sessions: Annotated[SessionManager, Depends(get_session_manager)],
) -> SessionResponse:
    sess = sessions.find_by_external_key(external_key)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found") from None
    _require_session_access(adapter, sess.id, auth)
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
        external_key=sess.external_key,
        status=sess.status,
        summary=sess.summary,
    )


@router.post(
    "/sessions/{session_id}/messages",
    status_code=201,
    response_model=MessageResponse,
)
def add_message(
    session_id: str,
    body: CreateMessageRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    sessions: Annotated[SessionManager, Depends(get_session_manager)],
) -> MessageResponse:
    _require_session_access(adapter, session_id, auth)
    try:
        msg = sessions.add_message(session_id, role=body.role, content=body.content)
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found") from None
    return MessageResponse(
        id=msg.id,
        role=msg.role,
        content=msg.content,
        created_at=msg.created_at.isoformat(),
    )


@router.post("/sessions/{session_id}/close", response_model=SessionResponse)
def close_session(
    session_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> SessionResponse:
    """Close a session through the kernel: score, failure memory, reflection,
    handoff, skill review and dream run exactly as in kernel.close_session()."""
    _require_session_access(adapter, session_id, auth)
    try:
        sess = kernel.close_session(session_id)
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found") from None
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
        external_key=sess.external_key,
        status=sess.status,
        summary=sess.summary,
    )


# ── Memory endpoints ─────────────────────────────────────────────────────


@router.get("/memory/search", response_model=list[MemoryEntryResponse])
def search_memory(
    query: Annotated[str, Query()],
    symbiote_id: Annotated[str, Query()],
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    memory: Annotated[MemoryStore, Depends(get_memory_store)],
    scope: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> list[MemoryEntryResponse]:
    _require_symbiote_access(adapter, symbiote_id, auth)
    entries = memory.search(
        query=query, scope=scope, limit=limit, symbiote_id=symbiote_id
    )
    return [
        MemoryEntryResponse(
            id=e.id,
            symbiote_id=e.symbiote_id,
            session_id=e.session_id,
            type=e.type,
            scope=e.scope,
            content=e.content,
            tags=e.tags,
            importance=e.importance,
            source=e.source,
        )
        for e in entries
    ]


# ══════════════════════════════════════════════════════════════════════════


@router.post("/sessions/{session_id}/chat", response_model=ChatResponse)
def chat(
    session_id: str,
    body: ChatRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> ChatResponse:
    """Send a message and get an LLM response with tool execution.

    This is the main endpoint for conversational interaction with a Symbiota.
    The kernel assembles context, calls the LLM, executes any tool calls,
    and returns the response. Tenant isolation is enforced via session ownership.
    """
    _require_session_access(kernel._storage, session_id, auth)
    if kernel._llm is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "No LLM configured: set SYMBIOTE_LLM_PROVIDER (and "
                "SYMBIOTE_LLM_MODEL) and restart the server."
            ),
        )

    response = kernel.message(
        session_id=session_id,
        content=body.content,
        extra_context=body.extra_context,
        # Per-call LLM overrides (e.g. temperature), as the SDK sends them.
        llm_config=body.generation_settings,
    )
    return ChatResponse(response=response, session_id=session_id)
