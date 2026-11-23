"""SymGateway adapter — LLM calls through the Symlabs gateway.

The gateway key replaces the provider key. The gateway's contract:

- ``GET /v1/models`` lists the models the key may use (``service_type`` ``llm``);
- ``GET /_api/objectives`` lists the Objectives the key may read; a call
  names either a ``model`` or an ``objective`` (the gateway then chooses the
  model), and the response says which Objective routed it;
- ``POST /_api/work-sessions`` opens a work session, and every inference call
  carries its uid in ``X-SymGateway-Session-Id``; the same uid is reused for
  every call of one conversation or task;
- ``POST /v1/chat/completions`` is OpenAI-compatible, whole or as an event
  stream (``stream: true``), and takes ``provider_options`` for the provider
  behind the gateway;
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

Built with ``streaming=True`` the adapter reads every reply as an event
stream, in ``complete()`` and in ``stream()``: a reply that takes minutes
keeps the connection alive while it is written, and ``on_activity`` runs for
every line received. Without the option nothing changes: one request, one
whole reply, and no ``stream()`` for the kernel to call.

With ``stream_reasoning=True`` too, ``stream()`` also yields the model's
reasoning as ``ReasoningDelta`` items, apart from the text, when the gateway
relays it in the stream (``delta.reasoning_content`` or ``delta.reasoning``).
Asking the provider for visible reasoning is a ``provider_options`` matter.

A reply that called tools may carry the gateway's opaque replay state
(``reasoning_state``: Anthropic's thinking blocks, OpenAI's encrypted
reasoning), in the terminal chunk of a stream or in the whole message. It
comes back as ``LLMResponse.reasoning_state`` and is sent unchanged on the
assistant message that called the tools in the next round, as the gateway
requires for a tool loop with reasoning (SymGateway 0.34.164 and later).
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

import httpx

from symbiote.adapters.llm.forge import _extract_tool_calls
from symbiote.core.exceptions import LLMError
from symbiote.core.llm_usage import report_usage, usage_from_provider
from symbiote.environment.descriptors import (
    LLMModelIdentity,
    LLMResponse,
    ReasoningDelta,
)

DEFAULT_BASE_URL = "https://symgateway.symlabs.ai"
SESSION_HEADER = "X-SymGateway-Session-Id"
PROJECT_HEADER = "X-Project-Slug"

# ChatConfig fields the kernel may send that map 1:1 to the OpenAI body.
_PASSTHROUGH = (
    "max_tokens", "temperature", "top_p", "stop", "tool_choice",
    "response_format", "parallel_tool_calls", "seed",
)
# Fields of a stream delta that carry reasoning, in order of preference: a
# delta with both (vLLM sends the same text in each) is read once.
_REASONING_FIELDS = ("reasoning_content", "reasoning")
# Fields of the canonical message dict the OpenAI body accepts.
_MESSAGE_FIELDS = ("role", "content", "name", "tool_calls", "tool_call_id")
# The gateway's opaque replay state of a reply that called tools.
_STATE = "reasoning_state"
# A rejected session uid (expired, closed, unknown) is replaced once.
_STALE_SESSION = frozenset({404, 409, 410})
_DEFAULT_KEY = "__default__"
_COMPLETIONS = "/v1/chat/completions"
# ``with_model(..., provider_options=)`` not given: the copy keeps the options.
_KEEP: Any = object()


class SymGatewayLLMAdapter:
    """LLMPort that routes calls through SymGateway with work sessions."""

    # Off unless asked; a subclass that skips __init__ sees it too.
    _stream_reasoning = False

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
        streaming: bool = False,
        on_activity: Callable[[], None] | None = None,
        provider_options: Mapping[str, Any] | None = None,
        stream_reasoning: bool = False,
    ) -> None:
        """``model`` names the model to call; ``objective`` names a gateway
        Objective instead, and the gateway chooses the model. One of the two
        is required; with both, the objective is used.

        ``on_routed(routing)`` is called after every call with what the
        gateway answered: ``{"model", "objective", "provider", "request_id",
        "work_session"}`` (``objective`` is the one that routed the call, or
        ``None``). ``last_routing`` keeps the latest one.

        ``streaming=True`` asks the gateway for every reply as an event
        stream (``stream: true``, with the usage in the last chunk) and gives
        the adapter a ``stream()``, so the kernel gets the text in the pieces
        the model wrote, for ``on_token``. ``complete()`` reads the stream
        too and returns the same value as without the option. ``timeout``
        then bounds the silence inside a reply, not the whole reply.
        ``on_activity()`` runs for every line of a stream as it arrives (a
        sign of life while a long reply is written; how often to show it is
        the host's matter). Off, the default, the adapter sends one request
        and reads one whole reply, as it always did. The routing of a
        streamed call is read from its chunks: ``objective``, ``provider``
        and ``request_id`` are ``None`` when no chunk carries the gateway's
        ``symgateway`` block.

        ``provider_options`` goes in the body of every call as
        ``provider_options``, for the provider behind the gateway (for
        example Anthropic's ``{"cache_control": {"type": "ephemeral"}}``).
        The ``provider_options`` of a call's own config is merged over it,
        key by key. Which models take which options is the host's decision
        (``models()`` says which provider serves each one); the attribute
        ``provider_options`` may be replaced after construction.

        ``stream_reasoning=True`` (needs ``streaming=True``): ``stream()``
        also yields the reasoning the gateway relays in the stream
        (``delta.reasoning_content``, or ``delta.reasoning``) as
        ``ReasoningDelta`` items, never mixed with the text; the kernel hands
        them to ``on_reasoning``. It changes neither the body of the call
        nor the reply: the reasoning is not kept in the text, the tool calls
        or ``reasoning_content``. Off, the default, the stream yields exactly
        what it did. Whether the model shows its reasoning at all is asked
        of the provider through ``provider_options`` (see "Visible reasoning"
        in ``docs/HOST_INTEGRATION.md``).
        """
        if not api_key:
            raise LLMError("SymGateway API key is required")
        if not model and not objective:
            raise LLMError(
                "SymGateway needs a model or an objective — pick one from "
                "list_models() or list_objectives()"
            )
        if stream_reasoning and not streaming:
            raise LLMError("stream_reasoning=True needs streaming=True")
        self.model = model
        self.objective = objective
        self.provider = "symgateway"
        self._on_routed = on_routed
        self._streaming = bool(streaming)
        self._on_activity = on_activity
        self._stream_reasoning = bool(stream_reasoning)
        self.provider_options: dict[str, Any] = dict(provider_options or {})
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

    def models(self) -> list[dict[str, Any]]:
        """What this key may use, as the gateway lists it (``GET /v1/models``):
        one dict per entry, of every service type, with the gateway's own
        fields (``id``, ``service_type``, ``provider``, ...)."""
        data = self._request("GET", "/v1/models").json()
        entries = data.get("data") if isinstance(data, dict) else None
        return [dict(m) for m in entries or [] if isinstance(m, dict)]

    def list_models(self) -> list[str]:
        """IDs of the LLM models this key may use."""
        return [
            m["id"] for m in self.models()
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

    def with_model(
        self, model: str, *, provider_options: Mapping[str, Any] | None = _KEEP
    ) -> SymGatewayLLMAdapter:
        """Same key, client and work sessions, another model — so a main and
        an auxiliary model working on one conversation share its session.

        The copy keeps this adapter's ``provider_options`` unless others are
        given: pass the other model's own, or ``None`` for none, when it is
        served by another provider (an auxiliary model must not inherit the
        main one's ``cache_control``)."""
        other = self._copy(provider_options)
        other.model = model
        other.objective = None
        return other

    def with_objective(
        self, objective: str, *, provider_options: Mapping[str, Any] | None = _KEEP
    ) -> SymGatewayLLMAdapter:
        """Same key, client and work sessions, routed by another Objective.
        ``provider_options`` as in ``with_model``."""
        other = self._copy(provider_options)
        other.objective = objective
        return other

    def _copy(self, provider_options: Mapping[str, Any] | None) -> SymGatewayLLMAdapter:
        other = copy.copy(self)
        kept = self.provider_options if provider_options is _KEEP else provider_options
        other.provider_options = dict(kept or {})
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
        body = self._body(messages, config, tools)
        if not self._streaming:
            return self._reply(*self._post(body))
        completion: tuple[dict[str, Any], str] | None = None
        for kind, value in self._events(body):
            if kind == "done":
                completion = value
        assert completion is not None  # a stream ends with its completion
        return self._reply(*completion)

    @property
    def stream(self) -> Callable[..., Iterator[str | ReasoningDelta | LLMResponse]]:
        """``stream(messages, config=None, tools=None)``: the reply as the
        model writes it, with the contract of ``ForgeLLMAdapter.stream()``.
        Yields the text in the pieces the gateway sends and, when the model
        called tools, one ``LLMResponse`` at the end (content, tool calls,
        usage). Plain text reports its usage with ``report_usage``. Built
        with ``stream_reasoning=True``, it also yields ``ReasoningDelta``
        items for the reasoning the gateway relays.

        Only an adapter built with ``streaming=True`` has it. The kernel
        streams through any adapter that has ``stream()``, so on one built
        without the option the attribute is absent (``hasattr`` is False)
        and the kernel keeps calling ``complete()``, as it always did.
        """
        if not self._streaming:
            raise AttributeError(
                "stream() needs SymGatewayLLMAdapter(..., streaming=True)"
            )
        return self._stream

    def _stream(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> Iterator[str | ReasoningDelta | LLMResponse]:
        completion: tuple[dict[str, Any], str] | None = None
        for kind, value in self._events(self._body(messages, config, tools)):
            if kind == "text":
                yield value
            elif kind == "reasoning":
                if self._stream_reasoning:
                    yield ReasoningDelta(text=value)
            else:
                completion = value
        assert completion is not None  # a stream ends with its completion
        reply = self._reply(*completion)
        if isinstance(reply, LLMResponse):
            yield reply

    # ── internals ──────────────────────────────────────────────────────

    def _body(
        self,
        messages: list[dict],
        config: dict | None,
        tools: list[dict] | None,
    ) -> dict[str, Any]:
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
            value = asked.get(name)
            if value is not None:
                body[name] = value
        if tools:
            body["tools"] = tools
        own = asked.get("provider_options")
        options = {**self.provider_options, **(own if isinstance(own, Mapping) else {})}
        if options:
            body["provider_options"] = options
        if self._streaming:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    def _post(self, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """One whole reply and the work session that carried the call."""
        key = self._session_key() if self._session_key else None
        uid = self.work_session(key)
        try:
            response = self._request(
                "POST", _COMPLETIONS, json=body, headers={SESSION_HEADER: uid}
            )
        except LLMError as exc:
            if exc.status_code not in _STALE_SESSION:
                raise
            self._forget_session(key)
            uid = self.work_session(key)
            response = self._request(
                "POST", _COMPLETIONS, json=body, headers={SESSION_HEADER: uid}
            )
        return response.json(), uid

    def _events(self, body: dict[str, Any]) -> Iterator[tuple[str, Any]]:
        """The reply as an event stream: ``("text", piece)`` and
        ``("reasoning", piece)`` as they arrive, then ``("done", (completion,
        work session uid))``."""
        key = self._session_key() if self._session_key else None
        for attempt in (1, 2):
            uid = self.work_session(key)
            try:
                with self._client.stream(
                    "POST",
                    self.base_url + _COMPLETIONS,
                    json=body,
                    headers={**self._headers, SESSION_HEADER: uid},
                ) as response:
                    status = response.status_code
                    if status >= 400:
                        response.read()
                        if attempt == 1 and status in _STALE_SESSION:
                            # A rejected work session is replaced once.
                            self._forget_session(key)
                            continue
                        raise _status_error(response, _COMPLETIONS)
                    for kind, value in _read_stream(
                        response.iter_lines(), self._on_activity
                    ):
                        yield (kind, (value, uid)) if kind == "done" else (kind, value)
                    return
            except httpx.TimeoutException as exc:
                raise LLMError(
                    f"SymGateway timeout on {_COMPLETIONS}", retryable=True
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"SymGateway unreachable: {exc}", retryable=True) from exc

    def _forget_session(self, key: str | None) -> None:
        with self._lock:
            self._sessions.pop(key or _DEFAULT_KEY, None)

    def _reply(self, data: dict[str, Any], uid: str) -> str | LLMResponse:
        routing = _routing(data, uid)
        self.last_routing = routing
        if self._on_routed is not None:
            self._on_routed(routing)
        return _parse_completion(data, uid)

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
            raise _status_error(response, path)
        return response


def _status_error(response: httpx.Response, path: str) -> LLMError:
    """The gateway refused the request; the body was already read."""
    status = response.status_code
    return LLMError(
        f"SymGateway {status} on {path}: {_error_text(response)}",
        status_code=status,
        retryable=status in (408, 429) or status >= 500,
    )


def _stream_failed(reason: object) -> LLMError:
    """The event stream is not a whole reply: asking again may get one."""
    return LLMError(f"SymGateway stream failed: {reason}", retryable=True)


def _read_stream(
    lines: Iterable[str], on_activity: Callable[[], None] | None = None
) -> Iterator[tuple[str, Any]]:
    """Read an OpenAI-style event stream while the model writes.

    Yields ``("text", piece)`` for every piece of text as it arrives,
    ``("reasoning", piece)`` for every piece of reasoning (``reasoning_content``
    or, without it, ``reasoning`` in the delta) and, once the stream ends,
    ``("done", completion)``: the whole reply in the shape of one that was not
    streamed, so ``_parse_completion`` reads both the same way. The reasoning
    is not part of the completion; the replay state (``delta.reasoning_state``,
    on the terminal chunk) is, in the message, as a whole reply carries it.
    ``on_activity`` runs for every line received.

    Only ``data:`` lines count; an empty one is a sign of life. ``[DONE]``
    ends the stream and what follows it is not read. Only the choice of
    index 0 is read. A tool call arrives in parts, placed by ``index``; with
    no index, a part with an ``id`` opens a call and one without continues
    the latest. A chunk that is an error or is not JSON, and a stream cut
    before ``finish_reason`` or ``[DONE]``, raise a retryable ``LLMError``.
    """
    text: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    # A file written in one call arrives in thousands of parts: joined once.
    arguments: dict[int, list[str]] = {}
    identifier: Any = None
    model: Any = None
    usage: Any = None
    gateway: dict[str, Any] = {}
    state: Any = None
    finish: str | None = None
    done = False
    for raw in lines:
        if on_activity is not None:
            on_activity()
        line = raw.strip()
        if not line.startswith("data:"):
            continue  # blank lines, comments and the other event fields
        payload = line[5:].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            done = True
            break
        try:
            chunk = json.loads(payload)
        except ValueError:
            raise _stream_failed("a chunk is not JSON") from None
        if not isinstance(chunk, dict):
            raise _stream_failed("a chunk is not an object")
        if chunk.get("error"):
            error = chunk["error"]
            reason = error.get("message") if isinstance(error, dict) else error
            raise _stream_failed(str(reason or "the gateway reported an error")[:300])
        identifier = chunk.get("id") or identifier
        model = chunk.get("model") or model
        usage = chunk.get("usage") or usage
        block = chunk.get("symgateway")
        if isinstance(block, dict):
            gateway.update({k: v for k, v in block.items() if v is not None})
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta")
            delta = delta if isinstance(delta, dict) else {}
            thought = next(
                (
                    delta[name] for name in _REASONING_FIELDS
                    if isinstance(delta.get(name), str) and delta[name]
                ),
                None,
            )
            if thought is not None:
                yield "reasoning", thought
            # The opaque replay state, on the terminal chunk of the choice.
            if delta.get(_STATE) is not None:
                state = delta[_STATE]
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                text.append(piece)
                yield "text", piece
            for part in delta.get("tool_calls") or []:
                if not isinstance(part, dict):
                    continue
                position = part.get("index")
                if not isinstance(position, int) or isinstance(position, bool):
                    position = len(calls) if part.get("id") or not calls else max(calls)
                call = calls.setdefault(
                    position,
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if part.get("id"):
                    call["id"] = part["id"]
                function = part.get("function")
                if isinstance(function, dict):
                    if function.get("name") and not call["function"]["name"]:
                        call["function"]["name"] = str(function["name"])
                    if isinstance(function.get("arguments"), str):
                        arguments.setdefault(position, []).append(function["arguments"])
            finish = choice.get("finish_reason") or finish
    if finish is None and not done:
        # The connection ended before the model said it was done.
        raise _stream_failed("the stream ended before the reply was complete")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
    if calls:
        for position, call in calls.items():
            call["function"]["arguments"] = "".join(arguments.get(position, []))
        message["tool_calls"] = [calls[position] for position in sorted(calls)]
    if state is not None:
        message[_STATE] = state
    completion: dict[str, Any] = {
        "id": identifier,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
        "usage": usage,
    }
    if gateway:
        # ``on_routed`` reads it. A gateway whose chunks do not carry the
        # block leaves the objective, provider and request id unknown.
        completion["symgateway"] = gateway
    yield "done", completion


def _wire_message(message: dict[str, Any]) -> dict[str, Any]:
    """Canonical message → OpenAI body.

    ``reasoning_content`` is dropped: it is display text, not state. The
    gateway's ``reasoning_state`` goes back unchanged, but only on an
    assistant message that called tools (a tool exchange flattened into
    text for a call without tools no longer is one).
    """
    out = {k: message[k] for k in _MESSAGE_FIELDS if message.get(k) is not None}
    if out.get("role") == "assistant" and "content" not in out:
        out["content"] = ""
    if out.get("role") == "assistant" and out.get("tool_calls") and message.get(_STATE) is not None:
        out[_STATE] = message[_STATE]
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
    # The gateway reports ``input_tokens`` / ``output_tokens`` (cache already
    # inside the input) with ``cached_input_tokens`` and ``cache_write_*``.
    usage = usage_from_provider(data.get("usage") or None)
    if not tool_calls:
        # Plain text carries no usage: hand it to the kernel directly.
        report_usage(usage=usage, provider="symgateway", model=data.get("model"))
        return message.get("content") or ""
    return LLMResponse(
        content=message.get("content") or "",
        tool_calls=tool_calls,
        reasoning_state=message.get(_STATE),
        finish_reason=choice.get("finish_reason"),
        model_identity=LLMModelIdentity(provider="symgateway", model=data.get("model")),
        usage=usage,
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
