"""SymGateway adapter — LLM calls through the Symlabs gateway.

The gateway key replaces the provider key. The gateway's contract:

- ``GET /v1/models`` lists the models the key may use (``service_type`` ``llm``);
- ``GET /_api/objectives`` lists the Objectives the key may read; a call
  names either a ``model`` or an ``objective`` (the gateway then chooses the
  model), and the response says which Objective routed it;
- ``POST /_api/work-sessions`` opens a work session, and every inference call
  carries its uid in ``X-SymGateway-Session-Id``; the same uid is reused for
  every call of one conversation or task;
- ``POST /v1/chat/completions`` is OpenAI-compatible;
- ``X-Project-Slug`` goes on every request only when the key is used inside a
  project.

The key needs ``models.read``, ``work_sessions.create`` and
``gateway.llm.invoke`` (and ``objectives.read`` for ``list_objectives``).

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
from symbiote.core.llm_usage import report_usage
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
        model: str | None = None,
        *,
        objective: str | None = None,
        base_url: str | None = None,
        project_slug: str | None = None,
        session_key: Callable[[], str | None] | None = None,
        session_title: str = "Symbiote",
        timeout: float = 120.0,
        client: httpx.Client | None = None,
        on_routed: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """``model`` names the model to call; ``objective`` names a gateway
        Objective instead, and the gateway chooses the model. One of the two
        is required; with both, the objective is used.

        ``on_routed(routing)`` is called after every call with what the
        gateway answered: ``{"model", "objective", "provider", "request_id",
        "work_session"}`` (``objective`` is the one that routed the call, or
        ``None``). ``last_routing`` keeps the latest one.
        """
        if not api_key:
            raise LLMError("SymGateway API key is required")
        if not model and not objective:
            raise LLMError(
                "SymGateway needs a model or an objective — pick one from "
                "list_models() or list_objectives()"
            )
        self.model = model
        self.objective = objective
        self.provider = "symgateway"
        self._on_routed = on_routed
        self.last_routing: dict[str, Any] | None = None
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

    def list_objectives(self) -> list[str]:
        """Names of the LLM Objectives this key may route by: enabled and
        with routing active."""
        data = self._request(
            "GET", "/_api/objectives?service_type=llm&enabled=true"
        ).json()
        items = data if isinstance(data, list) else data.get("data", [])
        return [
            o["name"] for o in items
            if isinstance(o, dict) and o.get("name")
            and o.get("service_type", "llm") == "llm"
            and o.get("enabled", True)
            and o.get("routing_status", "active") == "active"
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
        other.objective = None
        return other

    def with_objective(self, objective: str) -> SymGatewayLLMAdapter:
        """Same key, client and work sessions, routed by another Objective."""
        other = copy.copy(self)
        other.objective = objective
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
        # A call names an objective or a model. What the call's own config
        # asks for wins over what the adapter was built with.
        asked = config or {}
        body: dict[str, Any] = {"messages": [_wire_message(m) for m in messages]}
        if asked.get("objective"):
            body["objective"] = asked["objective"]
        elif asked.get("model"):
            body["model"] = asked["model"]
        elif self.objective:
            body["objective"] = self.objective
        else:
            body["model"] = self.model
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
        data = response.json()
        routing = _routing(data, uid)
        self.last_routing = routing
        if self._on_routed is not None:
            self._on_routed(routing)
        return _parse_completion(data, uid)

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


def _routing(data: dict[str, Any], session_uid: str) -> dict[str, Any]:
    """What the gateway says about how it served a call."""
    meta = data.get("symgateway") or {}
    return {
        "model": data.get("model"),
        "objective": meta.get("objective"),
        "provider": meta.get("provider"),
        "request_id": meta.get("request_id"),
        "work_session": session_uid,
    }


def _parse_completion(data: dict[str, Any], session_uid: str) -> str | LLMResponse:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"SymGateway returned no choices: {json.dumps(data)[:300]}")
    choice = choices[0]
    message = choice.get("message") or {}
    tool_calls = _extract_tool_calls(message)
    usage = data.get("usage") or {}
    if not tool_calls:
        # Plain text carries no usage: hand it to the kernel directly.
        report_usage(
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("total_tokens"),
            provider="symgateway",
            model=data.get("model"),
        )
        return message.get("content") or ""
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
        metadata={
            "id": data.get("id"),
            "work_session": session_uid,
            "objective": (data.get("symgateway") or {}).get("objective"),
        },
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
