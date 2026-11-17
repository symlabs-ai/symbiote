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
from symbiote.environment.descriptors import LLMResponse
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
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    elapsed_ms: int
    success: bool


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


@dataclass
class _Reported:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    provider: str | None = None
    model: str | None = None
    given: bool = False


_in_flight: contextvars.ContextVar[_Reported | None] = contextvars.ContextVar(
    "symbiote_llm_call_in_flight", default=None
)


def report_usage(
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    """Called by an LLM adapter, inside ``complete()`` / ``stream()``, to say
    what the call cost when it does not return an ``LLMResponse`` with
    ``usage`` (plain text, or text streamed chunk by chunk).

    Does nothing when the adapter is not being called by a kernel. ``usage``
    on the returned ``LLMResponse`` wins over what is reported here.
    """
    reported = _in_flight.get()
    if reported is None:
        return
    reported.prompt_tokens = prompt_tokens
    reported.completion_tokens = completion_tokens
    reported.total_tokens = total_tokens
    reported.provider = provider or reported.provider
    reported.model = model or reported.model
    reported.given = True


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
        if usage is not None:
            counts = (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
        elif reported.given:
            counts = (reported.prompt_tokens, reported.completion_tokens, reported.total_tokens)
        else:
            counts = (None, None, None)
        prompt, completion, total = counts
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
]
