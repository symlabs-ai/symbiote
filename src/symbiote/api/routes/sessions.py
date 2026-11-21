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
    CreateRevisionRequest,
    CreateSessionRequest,
    MemoryEntryResponse,
    MessageResponse,
    ResolveApprovalRequest,
    SessionResponse,
    SetDefaultRevisionRequest,
    SetSessionDelegationRequest,
    SetSessionRevisionRequest,
    SetSessionToolsRequest,
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


def _session_response(sess: Any, *, summary: bool = True) -> SessionResponse:
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
        external_key=sess.external_key,
        subject_key=sess.subject_key,
        revision=sess.revision,
        tools_allowed=sess.tools_allowed,
        delegation=sess.delegation,
        status=sess.status,
        summary=sess.summary if summary else None,
        learn=sess.learn,
    )


@router.post("/sessions", status_code=201, response_model=SessionResponse)
def create_session(
    body: CreateSessionRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> SessionResponse:
    _require_symbiote_access(adapter, body.symbiote_id, auth)
    if body.external_key:
        sess = kernel.get_or_create_session(
            symbiote_id=body.symbiote_id,
            external_key=body.external_key,
            goal=body.goal,
            subject_key=body.subject_key,
            revision=body.revision,
            tools_allowed=body.tools_allowed,
            delegation=body.delegation,
        )
    else:
        sess = kernel.start_session(
            symbiote_id=body.symbiote_id,
            goal=body.goal,
            subject_key=body.subject_key,
            revision=body.revision,
            tools_allowed=body.tools_allowed,
            delegation=body.delegation,
        )
    return _session_response(sess, summary=False)


@router.put("/sessions/{session_id}/revision", response_model=SessionResponse)
def set_session_revision(
    session_id: str,
    body: SetSessionRevisionRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> SessionResponse:
    """Pin a session to another revision, or to none (``null``: the current
    configuration). Takes effect on its next turn."""
    _require_session_access(kernel._storage, session_id, auth)
    sess = kernel.set_session_revision(session_id, body.revision)
    return _session_response(sess)


@router.put("/sessions/{session_id}/tools", response_model=SessionResponse)
def set_session_tools(
    session_id: str,
    body: SetSessionToolsRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> SessionResponse:
    """Narrow a session to a list of tools, or lift the restriction
    (``null``). It only narrows what the configuration authorizes. Takes
    effect on its next turn."""
    _require_session_access(kernel._storage, session_id, auth)
    return _session_response(kernel.set_session_tools(session_id, body.tools_allowed))


@router.put("/sessions/{session_id}/delegation", response_model=SessionResponse)
def set_session_delegation(
    session_id: str,
    body: SetSessionDelegationRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> SessionResponse:
    """Set a session's delegation mode (``"open"`` or ``"strict"``). Takes
    effect on its next turn."""
    _require_session_access(kernel._storage, session_id, auth)
    return _session_response(kernel.set_session_delegation(session_id, body.delegation))


# ── Revisions of a symbiote's configuration ──────────────────────────────


@router.post("/symbiotes/{symbiote_id}/revisions", status_code=201)
def create_revision(
    symbiote_id: str,
    body: CreateRevisionRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> dict:
    """Take an immutable picture of the symbiote's persona, config and
    knowledge under ``label``. A label is never overwritten (409)."""
    _require_symbiote_access(kernel._storage, symbiote_id, auth)
    return kernel.create_revision(symbiote_id, body.label, note=body.note).model_dump()


@router.get("/symbiotes/{symbiote_id}/revisions")
def list_revisions(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> list[dict]:
    """The revisions of a symbiote, newest first, with the sessions pinned
    to each."""
    _require_symbiote_access(kernel._storage, symbiote_id, auth)
    return kernel.inspector.list_revisions(symbiote_id)


@router.get("/symbiotes/{symbiote_id}/revisions/{label}")
def get_revision(
    symbiote_id: str,
    label: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> dict:
    """One revision: the persona and the config as they were."""
    _require_symbiote_access(kernel._storage, symbiote_id, auth)
    revision = kernel.get_revision(symbiote_id, label)
    if revision is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    return revision.model_dump()


@router.put("/symbiotes/{symbiote_id}/default-revision")
def set_default_revision(
    symbiote_id: str,
    body: SetDefaultRevisionRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> dict | None:
    """Make a revision the symbiote's default (where a delegation lands when
    the caller's label has no match), or leave it with none (``null``)."""
    _require_symbiote_access(kernel._storage, symbiote_id, auth)
    revision = kernel.set_default_revision(symbiote_id, body.label)
    return revision.model_dump() if revision is not None else None


@router.post("/symbiotes/{symbiote_id}/revisions/{label}/retire")
def retire_revision(
    symbiote_id: str,
    label: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> dict:
    """Close a revision to new sessions; the pinned ones go on."""
    _require_symbiote_access(kernel._storage, symbiote_id, auth)
    return kernel.retire_revision(symbiote_id, label).model_dump()


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
    return _session_response(sess)


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
    return _session_response(sess)


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
        origin=msg.origin,
    )


@router.post("/sessions/{session_id}/close", response_model=SessionResponse)
def close_session(
    session_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
    learn: Annotated[bool, Query()] = True,
) -> SessionResponse:
    """Close a session through the kernel: score, failure memory, reflection,
    handoff, skill review and dream run exactly as in kernel.close_session().
    ``?learn=false`` closes it without any of them and marks the session."""
    _require_session_access(adapter, session_id, auth)
    try:
        sess = kernel.close_session(session_id, learn=learn)
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found") from None
    return _session_response(sess)


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
        origin=body.origin,
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
                origin=body.origin,
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
