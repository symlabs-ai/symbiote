"""Regression: retries are decided by the error's status/type, not by
substrings of its message (report item E-10)."""

from __future__ import annotations

import types

import pytest

from symbiote.adapters.llm.forge import ForgeLLMAdapter
from symbiote.core.exceptions import LLMError
from symbiote.runners.chat import ChatRunner


class APIStatusError(Exception):
    """Shape of the OpenAI/Anthropic SDK status errors."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class APIConnectionError(Exception):
    pass


def _through_adapter(exc: Exception) -> LLMError:
    class Agent:
        def chat(self, **kwargs):
            raise exc

    adapter = ForgeLLMAdapter.__new__(ForgeLLMAdapter)
    adapter._agent = Agent()
    with pytest.raises(LLMError) as info:
        adapter.complete([{"role": "user", "content": "hi"}])
    return info.value


retryable = ChatRunner._is_retryable_error


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("Connection refused"),
        APIConnectionError("Connection error."),
        TimeoutError("read timed out"),
        APIStatusError("Error code: 500 - Internal Server Error", 500),
        APIStatusError("Error code: 503", 503),
        APIStatusError("Error code: 504 - Gateway Time-out", 504),
    ],
)
def test_transient_errors_are_retried_after_adapter_wrapping(exc) -> None:
    wrapped = _through_adapter(exc)
    assert retryable(wrapped) is True


@pytest.mark.parametrize(
    "exc",
    [
        APIStatusError("prompt has 150370 tokens, limit 128000 (timeout)", 400),
        APIStatusError("unknown parameter: timeout; upstream said 503", 400),
        APIStatusError("invalid api key", 401),
        APIStatusError("Error code: 429 - rate limited", 429),  # SDKs retry these
        ValueError("bad config"),
        FileNotFoundError("missing"),
    ],
)
def test_permanent_errors_are_not_retried(exc) -> None:
    wrapped = _through_adapter(exc)
    assert retryable(wrapped) is False


def test_status_is_kept_on_llm_error() -> None:
    wrapped = _through_adapter(APIStatusError("Error code: 503", 503))
    assert wrapped.status_code == 503


def test_request_id_containing_429_does_not_matter() -> None:
    wrapped = _through_adapter(APIStatusError("503 upstream, request id req_429abc", 503))
    assert retryable(wrapped) is True
    wrapped = _through_adapter(APIStatusError("bad request, request id req_503x", 400))
    assert retryable(wrapped) is False


def test_runner_retries_transient_and_not_permanent(monkeypatch) -> None:
    import symbiote.runners.chat as chat_mod

    monkeypatch.setattr(chat_mod.time, "sleep", lambda s: None)

    class Flaky:
        def __init__(self, exc) -> None:
            self.exc, self.calls = exc, 0

        def complete(self, messages, config=None, tools=None):
            self.calls += 1
            if self.calls == 1:
                raise self.exc
            return "ok"

    transient = Flaky(LLMError("boom", status_code=503, retryable=True))
    runner = ChatRunner(transient)
    assert runner._call_llm_with_retry([], {}, None)[0] == "ok"
    assert transient.calls == 2

    permanent = Flaky(LLMError("bad", status_code=400, retryable=False))
    runner = ChatRunner(permanent)
    with pytest.raises(LLMError):
        runner._call_llm_with_retry([], {}, None)
    assert permanent.calls == 1
    _ = types  # keep import for readability of the stub shapes
