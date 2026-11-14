"""SymGateway adapter — LLM calls through the Symlabs gateway.

The gateway key replaces the provider key. The gateway's contract:

- ``GET /v1/models`` lists the models the key may use (``service_type`` ``llm``);
- ``POST /_api/work-sessions`` opens a work session, and every inference call
  carries its uid in ``X-SymGateway-Session-Id``; the same uid is reused for
  every call of one conversation or task;
- ``POST /v1/chat/completions`` is OpenAI-compatible;
- ``X-Project-Slug`` goes on every request only when the key is used inside a
  project.

The key needs ``models.read``, ``work_sessions.create`` and
``gateway.llm.invoke``.

The kernel already speaks the OpenAI wire format (``tool_calls`` with
``function.arguments`` as JSON text, ``role: tool`` results), so this adapter
talks to the gateway directly with httpx instead of going through forge_llm,
whose ``symgateway`` provider cannot send the per-request session header.

Which work session a call belongs to is decided by ``session_key``: a callable
the host passes that returns a stable key for the current conversation (for
example the Symbiote session id, read from a context variable). Calls with no
key share one default work session.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable
from typing import Any

import httpx

from symbiote.adapters.llm.forge import _extract_tool_calls
from symbiote.core.exceptions import LLMError
from symbiote.environment.descriptors import (
    LLMModelIdentity,
    LLMResponse,
    LLMTokenUsage,
)

DEFAULT_BASE_URL = "https://symgateway.symlabs.ai"
SESSION_HEADER = "X-SymGateway-Session-Id"
PROJECT_HEADER = "X-Project-Slug"

# ChatConfig fields the kernel may send that map 1:1 to the OpenAI body.
_PASSTHROUGH = (
    "max_tokens", "temperature", "top_p", "stop", "tool_choice",
    "response_format", "parallel_tool_calls", "seed",
)
# Fields of the canonical message dict the OpenAI body accepts.
_MESSAGE_FIELDS = ("role", "content", "name", "tool_calls", "tool_call_id")
# A rejected session uid (expired, closed, unknown) is replaced once.
_STALE_SESSION = frozenset({404, 409, 410})
_DEFAULT_KEY = "__default__"


class SymGatewayLLMAdapter:
    """LLMPort that routes calls through SymGateway with work sessions."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        project_slug: str | None = None,
        session_key: Callable[[], str | None] | None = None,
        session_title: str = "Symbiote",
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise LLMError("SymGateway API key is required")
        if not model:
            raise LLMError("SymGateway model is required — pick one from list_models()")
        self.model = model
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._session_key = session_key
        self._session_title = session_title
        headers = {"Authorization": f"Bearer {api_key}"}
        if project_slug:
            headers[PROJECT_HEADER] = project_slug
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = headers
        self._sessions: dict[str, str] = {}
        self._lock = threading.Lock()

    # ── gateway API ────────────────────────────────────────────────────

    def list_models(self) -> list[str]:
        """IDs of the LLM models this key may use."""
        data = self._request("GET", "/v1/models").json()
        return [
            m["id"] for m in data.get("data", [])
            if m.get("service_type", "llm") == "llm" and m.get("id")
        ]

    def create_work_session(self, title: str | None = None) -> str:
        """Open a work session and return its uid."""
        body = self._request(
            "POST", "/_api/work-sessions", json={"title": title or self._session_title}
        ).json()
        uid = body.get("uid")
        if not uid:
            raise LLMError("SymGateway did not return a work session uid")
        return str(uid)

    def work_session(self, key: str | None = None) -> str:
        """The work session uid for ``key``, opened on first use."""
        key = key or _DEFAULT_KEY
        with self._lock:
            uid = self._sessions.get(key)
            if uid is None:
                title = self._session_title if key == _DEFAULT_KEY else f"{self._session_title} · {key[:8]}"
                uid = self.create_work_session(title)
                self._sessions[key] = uid
            return uid

    def with_model(self, model: str) -> SymGatewayLLMAdapter:
        """Same key, client and work sessions, another model — so a main and
        an auxiliary model working on one conversation share its session."""
        other = copy.copy(self)
        other.model = model
        return other

    @property
    def work_sessions(self) -> dict[str, str]:
        """Conversation key → work session uid opened so far."""
        with self._lock:
            return dict(self._sessions)

    def close(self) -> None:
        self._client.close()

    # ── LLMPort ────────────────────────────────────────────────────────

    def complete(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> str | LLMResponse:
        body: dict[str, Any] = {
            "model": (config or {}).get("model") or self.model,
            "messages": [_wire_message(m) for m in messages],
        }
        for name in _PASSTHROUGH:
            value = (config or {}).get(name)
            if value is not None:
                body[name] = value
        if tools:
            body["tools"] = tools

        key = self._session_key() if self._session_key else None
        uid = self.work_session(key)
        try:
            response = self._request(
                "POST", "/v1/chat/completions", json=body, headers={SESSION_HEADER: uid}
            )
        except LLMError as exc:
            if exc.status_code not in _STALE_SESSION:
                raise
            with self._lock:
                self._sessions.pop(key or _DEFAULT_KEY, None)
            uid = self.work_session(key)
            response = self._request(
                "POST", "/v1/chat/completions", json=body, headers={SESSION_HEADER: uid}
            )
        return _parse_completion(response.json(), uid)

    # ── internals ──────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                self.base_url + path,
                json=json,
                headers={**self._headers, **(headers or {})},
            )
        except httpx.TimeoutException as exc:
            raise LLMError(f"SymGateway timeout on {path}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"SymGateway unreachable: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            status = response.status_code
            raise LLMError(
                f"SymGateway {status} on {path}: {_error_text(response)}",
                status_code=status,
                retryable=status in (408, 429) or status >= 500,
            )
        return response


def _wire_message(message: dict[str, Any]) -> dict[str, Any]:
    """Canonical message → OpenAI body; reasoning replay fields are dropped."""
    out = {k: message[k] for k in _MESSAGE_FIELDS if message.get(k) is not None}
    if out.get("role") == "assistant" and "content" not in out:
        out["content"] = ""
    if out.get("role") != "tool":
        out.pop("tool_call_id", None)
    return out


def _parse_completion(data: dict[str, Any], session_uid: str) -> str | LLMResponse:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"SymGateway returned no choices: {json.dumps(data)[:300]}")
    choice = choices[0]
    message = choice.get("message") or {}
    tool_calls = _extract_tool_calls(message)
    if not tool_calls:
        return message.get("content") or ""
    usage = data.get("usage") or {}
    return LLMResponse(
        content=message.get("content") or "",
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason"),
        model_identity=LLMModelIdentity(provider="symgateway", model=data.get("model")),
        usage=LLMTokenUsage(
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        ) if usage else None,
        metadata={"id": data.get("id"), "work_session": session_uid},
    )


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        err = body.get("error") or body.get("detail") or body.get("message") or body
        if isinstance(err, dict):
            text = str(err.get("message") or err)
            # The upstream provider's own error, when the gateway relays one:
            # {"details": {"provider_error": {"error": {"type": "not_found_error", ...}}}}
            upstream = ((err.get("details") or {}).get("provider_error") or {})
            upstream = upstream.get("error", upstream) if isinstance(upstream, dict) else {}
            kind = upstream.get("type") if isinstance(upstream, dict) else None
            if kind:
                text = f"{text} (provider: {kind})"
            return text[:300]
        return str(err)[:300]
    return str(body)[:300]
