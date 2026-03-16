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
