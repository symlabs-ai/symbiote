"""Sessions, messages, memory search and chat."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError as PydanticValidationError

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
    ResolveApprovalRequest,
    SessionResponse,
)
from symbiote.core.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    SessionBusyError,
    SessionLockLostError,
    StorageIntegrityError,
    SymbioteError,
    TurnCancelledError,
    ValidationError,
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
            subject_key=body.subject_key,
        )
    else:
        sess = sessions.start(
            symbiote_id=body.symbiote_id,
            goal=body.goal,
            subject_key=body.subject_key,
        )
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
        external_key=sess.external_key,
        subject_key=sess.subject_key,
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
        subject_key=sess.subject_key,
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
        subject_key=sess.subject_key,
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
        subject_key=sess.subject_key,
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
    subject_key: Annotated[str | None, Query(min_length=1)] = None,
) -> list[MemoryEntryResponse]:
    """Search a symbiote's memories: the shared ones, plus the ones of the
    end user ``subject_key`` when given."""
    _require_symbiote_access(adapter, symbiote_id, auth)
    entries = memory.search(
        query=query,
        scope=scope,
        limit=limit,
        symbiote_id=symbiote_id,
        subject_key=subject_key,
    )
    return [
        MemoryEntryResponse(
            id=e.id,
            symbiote_id=e.symbiote_id,
            session_id=e.session_id,
            subject_key=e.subject_key,
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
    _require_llm(kernel)

    response = kernel.message(
        session_id=session_id,
        content=body.content,
        extra_context=body.extra_context,
        # Per-call LLM overrides (e.g. temperature), as the SDK sends them.
        llm_config=body.generation_settings,
    )
    return ChatResponse(
        response=response if isinstance(response, str) else dict(response),
        session_id=session_id,
    )


# ── Approvals that wait for a person ─────────────────────────────────────


@router.get("/symbiotes/{symbiote_id}/approvals")
def list_approvals(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
    status: str | None = Query(default="pending"),
    subject_key: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Tool calls of a symbiote deferred by the approval callback: the ones
    still waiting, or the ones in another ``status`` (``all`` for every state)."""
    _require_symbiote_access(kernel._storage, symbiote_id, auth)
    filters: dict[str, Any] = {"status": None if status == "all" else status, "limit": limit}
    if subject_key is not None:
        filters["subject_key"] = subject_key
    return kernel.inspector.list_pending(symbiote_id, **filters)


@router.post("/approvals/{approval_id}/resolve")
def resolve_approval(
    approval_id: str,
    body: ResolveApprovalRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> dict:
    """Approve (the call runs now) or deny a tool call that waited for
    approval; returns the request in its new state."""
    found = kernel.inspector.get_pending(approval_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Pending approval not found")
    _require_symbiote_access(kernel._storage, found["symbiote_id"], auth)
    try:
        resolved = kernel.resolve_pending(
            approval_id, body.approved, note=body.note, resolved_by=auth.id
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return resolved.model_dump()


def _require_llm(kernel: SymbioteKernel) -> None:
    if kernel._llm is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "No LLM configured: set SYMBIOTE_LLM_PROVIDER (and "
                "SYMBIOTE_LLM_MODEL) and restart the server."
            ),
        )


@router.post(
    "/sessions/{session_id}/chat/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat_stream(
    session_id: str,
    body: ChatRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> StreamingResponse:
    """``chat`` as Server-Sent Events: the text as it is produced.

    Events (``data`` is JSON):

    - ``token`` ``{"text": ...}``: a piece of the answer. How much arrives
      before the turn ends depends on ``SYMBIOTE_TOKEN_STREAMING``: ``live``
      (the server default) streams every model round as it is written,
      including the narration before a tool call; ``final`` sends only the
      validated answer, at the end.
    - ``done`` ``{"response": ..., "session_id": ...}``: the same body as
      ``POST /chat``. ``response`` is the authoritative answer; clients that
      rendered the tokens should replace them with it.
    - ``error`` ``{"status": ..., "detail": ...}``: the turn failed; ``status``
      is what ``POST /chat`` would have answered (409 session busy, ...).

    Access and LLM checks answer with plain HTTP errors before the stream
    starts. If the client disconnects, the turn is cancelled.
    """
    _require_session_access(kernel._storage, session_id, auth)
    _require_llm(kernel)
    return StreamingResponse(
        _chat_events(kernel, session_id, body),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_STREAM_DONE = object()


async def _chat_events(
    kernel: SymbioteKernel, session_id: str, body: ChatRequest
) -> AsyncIterator[str]:
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def turn() -> Any:
        try:
            return await kernel.message_async(
                session_id=session_id,
                content=body.content,
                extra_context=body.extra_context,
                on_token=queue.put_nowait,  # called on this event loop
                llm_config=body.generation_settings,
            )
        finally:
            queue.put_nowait(_STREAM_DONE)

    task = asyncio.create_task(turn())
    finished = False
    try:
        while (item := await queue.get()) is not _STREAM_DONE:
            yield _sse("token", {"text": item})
        try:
            response = await task
        except Exception as exc:
            status, detail = _error_status(exc)
            yield _sse("error", {"status": status, "detail": detail})
        else:
            yield _sse("done", {"response": response, "session_id": session_id})
        finished = True
    finally:
        if not finished:  # the client went away mid-turn
            kernel.cancel_turn(session_id)
            task.add_done_callback(_ignore_result)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _ignore_result(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()  # retrieved: no "exception was never retrieved"


# Same statuses as the app's exception handlers for POST /chat.
_ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (EntityNotFoundError, 404),
    (ValidationError, 422),
    (DuplicateEntityError, 409),
    (SessionBusyError, 409),
    (SessionLockLostError, 409),
    (TurnCancelledError, 409),
    (StorageIntegrityError, 409),
    (SymbioteError, 400),
    (PydanticValidationError, 422),
)


def _error_status(exc: Exception) -> tuple[int, str]:
    for kind, status in _ERROR_STATUS:
        if isinstance(exc, kind):
            detail = "Conflict with existing data" if kind is StorageIntegrityError else str(exc)
            return status, detail
    logging.getLogger(__name__).exception("chat stream failed", exc_info=exc)
    return 500, "Internal Server Error"
