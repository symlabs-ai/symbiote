"""Tests for LLM retry with exponential backoff in ChatRunner."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from symbiote.environment.descriptors import LLMResponse
from symbiote.runners.chat import _MAX_LLM_RETRIES, ChatRunner

# ── helpers ──────────────────────────────────────────────────────────────


class MockLLM:
    """Minimal LLM that returns pre-programmed responses or raises."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.call_count = 0

    def complete(self, messages, config=None, tools=None):
        self.call_count += 1
        response = self._responses.pop(0) if self._responses else "fallback"
        if isinstance(response, Exception):
            raise response
        return response


def _make_runner(llm: MockLLM) -> ChatRunner:
    return ChatRunner(llm=llm)


# ── tests ────────────────────────────────────────────────────────────────


class TestIsRetryableError:
    """Unit tests for the _is_retryable_error static method."""

    def test_connection_error_is_retryable(self):
        assert ChatRunner._is_retryable_error(ConnectionError("conn refused"))

    def test_timeout_error_is_retryable(self):
        assert ChatRunner._is_retryable_error(TimeoutError("timed out"))

    def test_os_error_is_retryable(self):
        assert ChatRunner._is_retryable_error(OSError("network down"))

    def test_rate_limit_string_is_retryable(self):
        assert ChatRunner._is_retryable_error(Exception("rate limit exceeded"))

    def test_429_string_is_retryable(self):
        assert ChatRunner._is_retryable_error(Exception("HTTP 429 Too Many Requests"))

    def test_503_string_is_retryable(self):
        assert ChatRunner._is_retryable_error(Exception("503 Service Unavailable"))

    def test_502_string_is_retryable(self):
        assert ChatRunner._is_retryable_error(Exception("502 Bad Gateway"))

    def test_timeout_string_is_retryable(self):
        assert ChatRunner._is_retryable_error(Exception("request timeout"))

    def test_value_error_not_retryable(self):
        assert not ChatRunner._is_retryable_error(ValueError("bad value"))

    def test_type_error_not_retryable(self):
        assert not ChatRunner._is_retryable_error(TypeError("wrong type"))

    def test_key_error_not_retryable(self):
        assert not ChatRunner._is_retryable_error(KeyError("missing"))

    def test_generic_exception_not_retryable(self):
        assert not ChatRunner._is_retryable_error(Exception("something else"))


class TestCallLLMWithRetry:
    """Tests for _call_llm_with_retry."""

    @patch("symbiote.runners.chat.time.sleep")
    def test_success_first_attempt(self, mock_sleep):
        llm = MockLLM(["hello"])
        runner = _make_runner(llm)

        result, chunks = runner._call_llm_with_retry(
            [{"role": "user", "content": "hi"}], {"config": None}, None,
        )

        assert result == "hello"
        assert chunks is None
        assert llm.call_count == 1
        mock_sleep.assert_not_called()

    @patch("symbiote.runners.chat.time.sleep")
    def test_retry_on_transient_then_success(self, mock_sleep):
        llm = MockLLM([ConnectionError("refused"), "ok"])
        runner = _make_runner(llm)

        result, chunks = runner._call_llm_with_retry(
            [{"role": "user", "content": "hi"}], {"config": None}, None,
        )

        assert result == "ok"
        assert llm.call_count == 2
        mock_sleep.assert_called_once_with(1)  # first backoff = 1s

    @patch("symbiote.runners.chat.time.sleep")
    def test_max_retries_exhausted_raises(self, mock_sleep):
        errors = [ConnectionError(f"fail {i}") for i in range(_MAX_LLM_RETRIES)]
        llm = MockLLM(errors)
        runner = _make_runner(llm)

        with pytest.raises(ConnectionError, match="fail 2"):
            runner._call_llm_with_retry(
                [{"role": "user", "content": "hi"}], {"config": None}, None,
            )

        assert llm.call_count == _MAX_LLM_RETRIES

    @patch("symbiote.runners.chat.time.sleep")
    def test_non_retryable_error_raises_immediately(self, mock_sleep):
        llm = MockLLM([ValueError("bad")])
        runner = _make_runner(llm)

        with pytest.raises(ValueError, match="bad"):
            runner._call_llm_with_retry(
                [{"role": "user", "content": "hi"}], {"config": None}, None,
            )

        assert llm.call_count == 1
        mock_sleep.assert_not_called()

    @patch("symbiote.runners.chat.time.sleep")
    def test_backoff_timing(self, mock_sleep):
        """Verify exponential backoff delays: 1s, 2s, then raise on 3rd."""
        errors = [TimeoutError("t/o")] * _MAX_LLM_RETRIES
        llm = MockLLM(errors)
        runner = _make_runner(llm)

        with pytest.raises(TimeoutError):
            runner._call_llm_with_retry(
                [{"role": "user", "content": "hi"}], {"config": None}, None,
            )

        # sleep called for attempts 1 and 2 (attempt 3 raises directly)
        assert mock_sleep.call_count == _MAX_LLM_RETRIES - 1
        mock_sleep.assert_any_call(1)   # 1 * 2^0
        mock_sleep.assert_any_call(2)   # 1 * 2^1

    @patch("symbiote.runners.chat.time.sleep")
    def test_retry_with_llm_response_type(self, mock_sleep):
        """Retry works when the LLM returns an LLMResponse object."""
        llm_response = LLMResponse(content="done", tool_calls=[])
        llm = MockLLM([ConnectionError("err"), llm_response])
        runner = _make_runner(llm)

        result, chunks = runner._call_llm_with_retry(
            [{"role": "user", "content": "hi"}], {"config": None}, None,
        )

        assert isinstance(result, LLMResponse)
        assert result.content == "done"
        assert llm.call_count == 2

    @patch("symbiote.runners.chat.time.sleep")
    def test_retry_with_string_return(self, mock_sleep):
        """Retry works when the LLM returns a plain string."""
        llm = MockLLM([OSError("net"), "plain string"])
        runner = _make_runner(llm)

        result, chunks = runner._call_llm_with_retry(
            [{"role": "user", "content": "hi"}], {"config": None}, None,
        )

        assert result == "plain string"
        assert isinstance(result, str)

    @patch("symbiote.runners.chat.time.sleep")
    def test_rate_limit_exception_is_retried(self, mock_sleep):
        """Exceptions with 'rate limit' in message are retried."""
        llm = MockLLM([Exception("rate limit exceeded"), "ok"])
        runner = _make_runner(llm)

        result, _ = runner._call_llm_with_retry(
            [{"role": "user", "content": "hi"}], {"config": None}, None,
        )

        assert result == "ok"
        assert llm.call_count == 2
