"""Who a model call is for, and what it cost in tokens.

Public contract (hosts may rely on it):

* ``current_llm_call()`` returns an ``LLMCallContext`` while the kernel is
  calling a model: the symbiote, the session (when there is one) and the
  ``purpose`` of the call. An LLM adapter reads it inside ``complete()`` /
  ``stream()`` to attribute the call; ``None`` when the adapter is called by
  something else.
* Purposes: ``turn`` (a conversation turn), ``subagent`` (a turn of a
  delegated symbiote, see ``current_spawn()``), ``reflection``,
  ``session_summary``, ``skill_review``, ``dream:<phase>``, ``harness``,
  ``memory_consolidation``, ``tool_resolution``, ``long_run:plan``,
  ``long_run:evaluate`` and ``long_run:report``. New purposes may be added.
* The kernel records one ``LLMCallRecord`` per call (table ``llm_usage``,
  ``kernel.inspector.usage(...)``, ``kernel.set_usage_callback(fn)``). The
  tokens are the ``usage`` of the ``LLMResponse`` the adapter returned. An
  adapter that returns plain text, or that streams, calls ``report_usage()``
  before it returns to say what the call cost; without either the call is
  recorded with no token counts.

Nothing here depends on a gateway: the counts come from the adapter, whatever
provider it talks to. The kernel stores tokens, provider and model, never a
price.

Token convention, the same for every provider: ``prompt_tokens`` is the whole
input, cache included; ``cache_read_tokens`` and ``cache_write_tokens`` are
parts of it. Providers disagree (Anthropic reports the input without the
cache, OpenAI with it): ``usage_from_provider(raw)`` turns what a provider
returned into that convention, so a host prices every call the same way.
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from symbiote.core.turn_record import current_turn_record
from symbiote.environment.descriptors import LLMResponse, LLMTokenUsage
from symbiote.environment.exec_context import current_spawn

_log = logging.getLogger(__name__)

PURPOSE_TURN = "turn"
PURPOSE_SUBAGENT = "subagent"
PURPOSE_OTHER = "other"


@dataclass(frozen=True)
class LLMCallContext:
    """Who the model call in progress is for."""

    purpose: str
    symbiote_id: str | None = None
    session_id: str | None = None
    # The session of the turn the host started: differs from ``session_id``
    # only for the calls of a delegated symbiote (``spawn``).
    root_session_id: str | None = None


@dataclass(frozen=True)
class LLMCallRecord:
    """One finished model call: who it was for and what it cost."""

    purpose: str
    symbiote_id: str | None
    session_id: str | None
    root_session_id: str | None
    provider: str | None
    model: str | None
    # ``prompt_tokens`` is the whole input, cache included; the cache counts
    # are parts of it. ``None``: the adapter did not report it.
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    elapsed_ms: int
    success: bool
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    # Already inside ``completion_tokens``; information only.
    reasoning_tokens: int | None = None


# ── the context of a call ───────────────────────────────────────────────

_explicit: contextvars.ContextVar[LLMCallContext | None] = contextvars.ContextVar(
    "symbiote_llm_call", default=None
)


def current_llm_call() -> LLMCallContext | None:
    """Who the model call in progress is for, or ``None`` outside one."""
    explicit = _explicit.get()
    record = current_turn_record()
    if explicit is None and record is None:
        return None
    spawn = current_spawn()
    symbiote_id = explicit.symbiote_id if explicit and explicit.symbiote_id else None
    session_id = explicit.session_id if explicit and explicit.session_id else None
    if record is not None:
        symbiote_id = symbiote_id or record.symbiote_id
        session_id = session_id or record.session_id
    root = explicit.root_session_id if explicit else None
    if root is None and spawn is not None and session_id == spawn.session_id:
        root = spawn.root_session_id
    if explicit is not None:
        purpose = explicit.purpose
    else:
        purpose = PURPOSE_SUBAGENT if spawn is not None else PURPOSE_TURN
    return LLMCallContext(
        purpose=purpose,
        symbiote_id=symbiote_id,
        session_id=session_id,
        root_session_id=root or session_id,
    )


@contextmanager
def llm_call(
    purpose: str, symbiote_id: str | None = None, session_id: str | None = None
) -> Iterator[None]:
    """Mark the model calls made inside the block.

    ``symbiote_id`` / ``session_id`` left out are taken from the enclosing
    block or from the turn in progress.
    """
    outer = _explicit.get()
    token = _explicit.set(
        LLMCallContext(
            purpose=purpose,
            symbiote_id=symbiote_id or (outer.symbiote_id if outer else None),
            session_id=session_id or (outer.session_id if outer else None),
            root_session_id=outer.root_session_id if outer else None,
        )
    )
    try:
        yield
    finally:
        _explicit.reset(token)


# ── what a call cost ────────────────────────────────────────────────────


# The counters of one call besides prompt / completion / total.
_EXTRA_COUNTERS = (
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "reasoning_tokens",
)


@dataclass
class _Reported:
    usage: LLMTokenUsage | None = None
    provider: str | None = None
    model: str | None = None


_in_flight: contextvars.ContextVar[_Reported | None] = contextvars.ContextVar(
    "symbiote_llm_call_in_flight", default=None
)


def report_usage(
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    *,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cache_write_5m_tokens: int | None = None,
    cache_write_1h_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    usage: LLMTokenUsage | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    """Called by an LLM adapter, inside ``complete()`` / ``stream()``, to say
    what the call cost when it does not return an ``LLMResponse`` with
    ``usage`` (plain text, or text streamed chunk by chunk).

    ``prompt_tokens`` is the whole input, cache included; the cache counts
    are parts of it (see ``LLMTokenUsage``). Pass the counts one by one, or
    ``usage=usage_from_provider(raw)`` to hand over what the provider
    returned already in that convention.

    Does nothing when the adapter is not being called by a kernel. ``usage``
    on the returned ``LLMResponse`` wins over what is reported here.
    """
    reported = _in_flight.get()
    if reported is None:
        return
    reported.usage = usage or LLMTokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_write_5m_tokens=cache_write_5m_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
        reasoning_tokens=reasoning_tokens,
    )
    reported.provider = provider or reported.provider
    reported.model = model or reported.model


def _field(source: Any, *path: str) -> Any:
    """``source[a][b]`` / ``source.a.b`` for dicts and objects; ``None`` when absent."""
    value = source
    for key in path:
        if value is None:
            return None
        value = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
    return value


def _count(source: Any, *paths: tuple[str, ...]) -> int | None:
    """The first of ``paths`` that holds a whole number."""
    for path in paths:
        value = _field(source, *path)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def usage_from_provider(raw: Any) -> LLMTokenUsage | None:
    """The usage a provider returned (a dict or an object), in the kernel's
    convention: ``prompt_tokens`` is the whole input, cache included.

    Reads each provider's own field names:

    * OpenAI Chat Completions / Responses: ``prompt_tokens`` (or
      ``input_tokens``) already includes the cache;
      ``prompt_tokens_details.cached_tokens`` and
      ``completion_tokens_details.reasoning_tokens``.
    * Anthropic: ``input_tokens`` does **not** include the cache;
      ``cache_read_input_tokens`` and ``cache_creation_input_tokens`` are
      added to it, and ``cache_creation.ephemeral_5m_input_tokens`` /
      ``ephemeral_1h_input_tokens`` split what was written.
    * Counters already in this convention (``cache_read_tokens``,
      ``cache_write_tokens``, ``cached_input_tokens``, ``cache_write_5m_tokens``,
      ``cache_write_1h_tokens``, ``reasoning_tokens``) are taken as they are.

    Returns ``None`` for ``None``. Counters the provider did not send stay
    ``None``.
    """
    if raw is None:
        return None
    if isinstance(raw, LLMTokenUsage):
        return raw
    prompt = _count(raw, ("prompt_tokens",), ("input_tokens",))
    completion = _count(raw, ("completion_tokens",), ("output_tokens",))
    total = _count(raw, ("total_tokens",))

    # Parts the provider counts inside the input it reported...
    read = _count(
        raw,
        ("cache_read_tokens",),
        ("cached_input_tokens",),
        ("prompt_tokens_details", "cached_tokens"),
        ("input_tokens_details", "cached_tokens"),
        ("cached_tokens",),
    )
    write = _count(raw, ("cache_write_tokens",))
    # ...and the ones it reports beside it (Anthropic).
    read_beside = _count(raw, ("cache_read_input_tokens",))
    write_beside = _count(raw, ("cache_creation_input_tokens",))
    beside = (read_beside or 0) + (write_beside or 0)
    if prompt is not None and beside:
        prompt += beside
        total = prompt + (completion or 0)
    if read is None:
        read = read_beside
    if write is None:
        write = write_beside
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)

    known = {
        "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens",
        "output_tokens", "cache_read_tokens", "cache_write_tokens",
        "cached_input_tokens", "cached_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "cache_write_5m_tokens",
        "cache_write_1h_tokens", "reasoning_tokens",
    }
    if isinstance(raw, dict):
        extra = {key: value for key, value in raw.items() if key not in known}
    elif hasattr(raw, "__dict__"):
        extra = {key: value for key, value in vars(raw).items() if key not in known}
    else:
        extra = {}
    return LLMTokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cache_read_tokens=read,
        cache_write_tokens=write,
        cache_write_5m_tokens=_count(
            raw, ("cache_write_5m_tokens",), ("cache_creation", "ephemeral_5m_input_tokens")
        ),
        cache_write_1h_tokens=_count(
            raw, ("cache_write_1h_tokens",), ("cache_creation", "ephemeral_1h_input_tokens")
        ),
        reasoning_tokens=_count(
            raw,
            ("reasoning_tokens",),
            ("completion_tokens_details", "reasoning_tokens"),
            ("output_tokens_details", "reasoning_tokens"),
        ),
        metadata=extra,
    )


class MeteredLLM:
    """An ``LLMPort`` that reports every call to ``sink``.

    Transparent: every attribute of the wrapped adapter is reachable, and it
    has ``stream`` only when the adapter has it. ``wrapped`` is the adapter.
    """

    def __init__(self, llm: Any, sink: Callable[[LLMCallRecord], None]) -> None:
        self._metered_llm = llm
        self._metered_sink = sink

    @property
    def wrapped(self) -> Any:
        return self._metered_llm

    def __getattr__(self, name: str) -> Any:
        if name == "stream":
            if not hasattr(self._metered_llm, "stream"):
                raise AttributeError(name)
            return self._metered_stream
        return getattr(self._metered_llm, name)

    def complete(self, messages: list[dict], *args: Any, **kwargs: Any) -> Any:
        reported = _Reported()
        token = _in_flight.set(reported)
        started = time.monotonic()
        try:
            response = self._metered_llm.complete(messages, *args, **kwargs)
        except BaseException:
            self._emit(reported, None, args, kwargs, started, success=False)
            raise
        finally:
            _in_flight.reset(token)
        self._emit(reported, response, args, kwargs, started, success=True)
        return response

    def _metered_stream(self, messages: list[dict], *args: Any, **kwargs: Any) -> Iterator[Any]:
        reported = _Reported()
        started = time.monotonic()
        response: Any = None
        success = False
        try:
            # The reporting slot is bound only while the adapter runs, never
            # across a ``yield``: the consumer owns the context in between.
            token = _in_flight.set(reported)
            try:
                items = iter(self._metered_llm.stream(messages, *args, **kwargs))
            finally:
                _in_flight.reset(token)
            while True:
                token = _in_flight.set(reported)
                try:
                    item = next(items)
                except StopIteration:
                    break
                finally:
                    _in_flight.reset(token)
                if isinstance(item, LLMResponse):
                    response = item
                yield item
            success = True
        finally:
            self._emit(reported, response, args, kwargs, started, success=success)

    def _emit(
        self,
        reported: _Reported,
        response: Any,
        args: tuple,
        kwargs: dict,
        started: float,
        *,
        success: bool,
    ) -> None:
        try:
            self._metered_sink(self._record(reported, response, args, kwargs, started, success))
        except Exception:
            _log.exception("could not record a model call")

    def _record(
        self,
        reported: _Reported,
        response: Any,
        args: tuple,
        kwargs: dict,
        started: float,
        success: bool,
    ) -> LLMCallRecord:
        usage = getattr(response, "usage", None) if isinstance(response, LLMResponse) else None
        identity = (
            getattr(response, "model_identity", None)
            if isinstance(response, LLMResponse)
            else None
        )
        config = kwargs.get("config") if "config" in kwargs else (args[0] if args else None)
        config_model = config.get("model") if isinstance(config, dict) else None
        llm = self._metered_llm
        model = (
            getattr(identity, "model", None)
            or reported.model
            or config_model
            or _text(getattr(llm, "model", None))
        )
        provider = (
            getattr(identity, "provider", None)
            or reported.provider
            or _text(getattr(llm, "provider", None))
        )
        if usage is None:
            usage = reported.usage or LLMTokenUsage()
        prompt, completion, total = (
            usage.prompt_tokens, usage.completion_tokens, usage.total_tokens,
        )
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        context = current_llm_call()
        return LLMCallRecord(
            purpose=context.purpose if context else PURPOSE_OTHER,
            symbiote_id=context.symbiote_id if context else None,
            session_id=context.session_id if context else None,
            root_session_id=context.root_session_id if context else None,
            provider=provider,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            success=success,
            **{name: getattr(usage, name) for name in _EXTRA_COUNTERS},
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def metered(llm: Any, sink: Callable[[LLMCallRecord], None]) -> Any:
    """``llm`` reporting to ``sink`` (``None`` stays ``None``; an adapter
    already metered is unwrapped first, so a call is recorded once)."""
    if llm is None:
        return None
    if isinstance(llm, MeteredLLM):
        llm = llm.wrapped
    return MeteredLLM(llm, sink)


__all__ = [
    "LLMCallContext",
    "LLMCallRecord",
    "MeteredLLM",
    "current_llm_call",
    "llm_call",
    "metered",
    "report_usage",
    "usage_from_provider",
]
