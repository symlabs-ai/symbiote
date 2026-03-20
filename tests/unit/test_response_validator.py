"""Tests for the deterministic response validator."""

from __future__ import annotations

import pytest

from symbiote.runners.response_validator import (
    fallback_text,
    is_valid_response,
    reformulate_message,
)


class TestIsValidResponse:
    def test_empty_string_is_invalid(self) -> None:
        assert is_valid_response("") is False

    def test_whitespace_only_is_invalid(self) -> None:
        assert is_valid_response("   \n\t  ") is False

    def test_plain_text_is_valid(self) -> None:
        assert is_valid_response("Olá! Como posso ajudar você hoje?") is True

    def test_multiline_plain_text_is_valid(self) -> None:
        text = "Aqui está sua resposta.\n\nVocê pode fazer mais perguntas."
        assert is_valid_response(text) is True

    # ── bare JSON detection ───────────────────────────────────────────────

    def test_bare_json_object_on_line_is_invalid(self) -> None:
        # This would survive parse_tool_calls if the JSON is malformed
        text = '{"invalid": true, "no_tool_key": "here"}'
        assert is_valid_response(text) is False

    def test_bare_json_after_newline_is_invalid(self) -> None:
        text = "Aqui vou chamar a ferramenta:\n[\"item1\", \"item2\"]"
        assert is_valid_response(text) is False

    def test_bare_json_array_on_line_is_invalid(self) -> None:
        text = '[{"id": 1}, {"id": 2}]'
        assert is_valid_response(text) is False

    def test_inline_json_not_at_line_start_is_valid(self) -> None:
        # JSON embedded mid-sentence (not starting a line) is fine
        text = 'Use the format like {"key": "value"} for your data.'
        assert is_valid_response(text) is True

    # ── text-serialised tool call detection ──────────────────────────────

    def test_inline_text_tool_call_is_invalid(self) -> None:
        # parse_tool_calls does NOT extract inline tool refs — they leak to clean_text
        text = 'Vou usar {"tool": "yn_list_journals", "params": {}} para listar os journals.'
        assert is_valid_response(text) is False

    def test_text_tool_call_with_spaces_is_invalid(self) -> None:
        text = 'Aqui está: { "tool" : "some_tool", "params": {} }'
        assert is_valid_response(text) is False

    # ── fenced tool_call block detection ─────────────────────────────────

    def test_malformed_fenced_tool_call_block_is_invalid(self) -> None:
        # parse_tool_calls strips valid fenced blocks; malformed ones survive
        text = "Vou chamar:\n```tool_call\n{bad json here}\n```"
        assert is_valid_response(text) is False

    def test_fenced_tool_call_uppercase_is_invalid(self) -> None:
        text = "```TOOL_CALL\n{\"tool\": \"x\", \"params\": {}}\n```"
        assert is_valid_response(text) is False

    def test_code_block_not_tool_call_is_valid(self) -> None:
        text = "Aqui está um exemplo:\n```python\nprint('hello')\n```"
        assert is_valid_response(text) is True


class TestValidatorHelpers:
    def test_reformulate_message_is_non_empty_string(self) -> None:
        msg = reformulate_message()
        assert isinstance(msg, str)
        assert len(msg) > 10

    def test_fallback_text_is_non_empty_string(self) -> None:
        fb = fallback_text()
        assert isinstance(fb, str)
        assert len(fb) > 10


class TestChatRunnerValidation:
    """Integration tests: ChatRunner should retry invalid responses."""

    def _make_context(self, user_input: str = "Hello") -> object:
        from symbiote.core.context import AssembledContext
        return AssembledContext(
            symbiote_id="sym-1",
            session_id="sess-1",
            user_input=user_input,
        )

    def test_invalid_response_triggers_retry_and_succeeds(self) -> None:
        """If first response has inline tool call text, second should be returned."""
        from symbiote.runners.chat import ChatRunner

        call_count = 0

        class RetryLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # First call: leaked inline tool call (not extracted by parser)
                    return 'Vou usar {"tool": "yn_list_journals", "params": {}} para isso.'
                # Second call (reformulation): valid response
                return "Aqui estão seus journals."

        runner = ChatRunner(llm=RetryLLM())
        ctx = self._make_context()
        result = runner.run(ctx)

        assert result.success is True
        assert result.output == "Aqui estão seus journals."
        assert call_count == 2

    def test_valid_response_no_retry(self) -> None:
        """A valid response should not trigger any retry."""
        from symbiote.runners.chat import ChatRunner

        call_count = 0

        class CountingLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                return "Resposta válida."

        runner = ChatRunner(llm=CountingLLM())
        ctx = self._make_context()
        result = runner.run(ctx)

        assert result.success is True
        assert call_count == 1

    def test_all_retries_exhausted_returns_fallback(self) -> None:
        """If all retries return invalid text, fallback is used."""
        from symbiote.runners.chat import ChatRunner
        from symbiote.runners.response_validator import fallback_text

        class AlwaysInvalidLLM:
            def complete(self, messages, config=None, tools=None):
                # Malformed fenced block — survives parse_tool_calls, validator catches it
                return "```tool_call\n{bad json}\n```"

        runner = ChatRunner(llm=AlwaysInvalidLLM())
        ctx = self._make_context()
        result = runner.run(ctx)

        assert result.success is True
        assert result.output == fallback_text()

    def test_empty_response_triggers_retry(self) -> None:
        """An empty response triggers a retry."""
        from symbiote.runners.chat import ChatRunner

        call_count = 0

        class EmptyThenValidLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return ""
                return "Desculpe, vamos tentar novamente."

        runner = ChatRunner(llm=EmptyThenValidLLM())
        ctx = self._make_context()
        result = runner.run(ctx)

        assert result.success is True
        assert result.output == "Desculpe, vamos tentar novamente."
        assert call_count == 2

    def test_streaming_tokens_buffered_and_released_after_validation(self) -> None:
        """Streaming tokens must only reach on_token after validation passes."""
        from symbiote.runners.chat import ChatRunner

        class StreamingLLM:
            def complete(self, messages, config=None, tools=None):
                return "Resposta válida em streaming."

            def stream(self, messages, config=None, tools=None):
                yield from ["Resposta", " válida", " em", " streaming."]

        runner = ChatRunner(llm=StreamingLLM())
        ctx = self._make_context()

        received: list[str] = []
        result = runner.run(ctx, on_token=received.append)

        assert result.success is True
        assert result.output == "Resposta válida em streaming."
        # Tokens should have been released after validation
        assert received == ["Resposta", " válida", " em", " streaming."]

    def test_streaming_invalid_then_valid_retries_and_releases_correct_token(self) -> None:
        """When streaming yields invalid text, retry is issued and valid text is released."""
        from symbiote.runners.chat import ChatRunner

        call_count = 0

        class RetryStreamingLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return "```tool_call\n{bad}\n```"
                return "Resposta válida após retry."

            def stream(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    yield "```tool_call\n{bad}\n```"
                else:
                    yield "Resposta válida após retry."

        runner = ChatRunner(llm=RetryStreamingLLM())
        ctx = self._make_context()

        received: list[str] = []
        result = runner.run(ctx, on_token=received.append)

        assert result.success is True
        assert result.output == "Resposta válida após retry."
        # Only the valid response token should be emitted
        assert "Resposta válida após retry." in received

    @pytest.mark.asyncio
    async def test_run_async_validates_response(self) -> None:
        """run_async should also validate and retry."""
        from symbiote.runners.chat import ChatRunner

        call_count = 0

        class AsyncRetryLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return "```tool_call\n{bad json}\n```"
                return "Resposta correta assíncrona."

        runner = ChatRunner(llm=AsyncRetryLLM())
        ctx = self._make_context()
        result = await runner.run_async(ctx)

        assert result.success is True
        assert result.output == "Resposta correta assíncrona."
        assert call_count == 2
