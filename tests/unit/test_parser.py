"""Tests for tool call parser."""

from __future__ import annotations

from symbiote.environment.parser import parse_tool_calls


class TestParseToolCalls:
    def test_no_tool_calls(self) -> None:
        text = "Hello, this is a normal response."
        clean, calls = parse_tool_calls(text)
        assert clean == text
        assert calls == []

    def test_single_tool_call(self) -> None:
        text = (
            "Let me publish that for you.\n\n"
            "```tool_call\n"
            '{"tool": "yn_publish", "params": {"id": "123"}}\n'
            "```\n\n"
            "Done!"
        )
        clean, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].tool_id == "yn_publish"
        assert calls[0].params == {"id": "123"}
        assert "```tool_call" not in clean
        assert "Done!" in clean
        assert "publish that" in clean

    def test_multiple_tool_calls(self) -> None:
        text = (
            "I'll do both.\n\n"
            "```tool_call\n"
            '{"tool": "search", "params": {"q": "news"}}\n'
            "```\n\n"
            "```tool_call\n"
            '{"tool": "publish", "params": {"id": "456"}}\n'
            "```\n"
        )
        clean, calls = parse_tool_calls(text)
        assert len(calls) == 2
        assert calls[0].tool_id == "search"
        assert calls[1].tool_id == "publish"

    def test_invalid_json_skipped(self) -> None:
        text = (
            "Trying.\n\n"
            "```tool_call\n"
            "not json at all\n"
            "```\n"
        )
        clean, calls = parse_tool_calls(text)
        assert calls == []

    def test_missing_tool_field_skipped(self) -> None:
        text = (
            "```tool_call\n"
            '{"params": {"x": 1}}\n'
            "```\n"
        )
        clean, calls = parse_tool_calls(text)
        assert calls == []

    def test_no_params_defaults_to_empty(self) -> None:
        text = (
            "```tool_call\n"
            '{"tool": "list_items"}\n'
            "```\n"
        )
        clean, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].params == {}

    def test_clean_text_strips_blocks(self) -> None:
        text = "Before\n\n```tool_call\n{\"tool\": \"x\"}\n```\n\nAfter"
        clean, _ = parse_tool_calls(text)
        assert "Before" in clean
        assert "After" in clean
        assert "tool_call" not in clean

    def test_closing_backticks_on_same_line(self) -> None:
        """LLM sometimes puts closing ``` right after JSON without newline."""
        text = (
            "```tool_call\n"
            '{"tool": "yn_capture_url", "params": {"url": "https://example.com"}}```'
        )
        clean, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].tool_id == "yn_capture_url"


class TestBareJsonFallback:
    """Fallback parsing for tool calls without ```tool_call fencing."""

    def test_bare_json_tool_call(self) -> None:
        text = (
            "Vou capturar o link.\n\n"
            '{"tool": "yn_capture_url", "params": {"url": "https://youtube.com/abc", "journal_id": "e138"}}\n'
        )
        clean, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].tool_id == "yn_capture_url"
        assert calls[0].params["url"] == "https://youtube.com/abc"
        assert '{"tool"' not in clean

    def test_bare_json_multiple_calls(self) -> None:
        text = (
            '{"tool": "yn_list_journals", "params": {}}\n'
            '{"tool": "yn_capture_url", "params": {"url": "https://x.com"}}\n'
        )
        clean, calls = parse_tool_calls(text)
        assert len(calls) == 2
        assert calls[0].tool_id == "yn_list_journals"
        assert calls[1].tool_id == "yn_capture_url"

    def test_fenced_takes_priority_over_bare(self) -> None:
        """If fenced format is present, bare JSON is not parsed."""
        text = (
            "```tool_call\n"
            '{"tool": "yn_search", "params": {"q": "news"}}\n'
            "```\n"
        )
        clean, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].tool_id == "yn_search"

    def test_bare_json_not_a_tool_call(self) -> None:
        """Regular JSON that doesn't match tool call format is left alone."""
        text = 'The result is {"items": [1, 2, 3]}'
        clean, calls = parse_tool_calls(text)
        assert calls == []
        assert clean == text

    def test_bare_json_with_surrounding_text(self) -> None:
        text = (
            "Vou tentar novamente.\n"
            '{"tool": "yn_capture_url", "params": {"url": "https://example.com", "journal_id": "abc"}}\n'
            "Pronto!"
        )
        clean, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert "Vou tentar" in clean
        assert '{"tool"' not in clean
