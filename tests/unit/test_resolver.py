"""Tests for ToolTagResolver — semantic pre-filter."""

from __future__ import annotations

import json

import pytest

from symbiote.environment.resolver import ToolTagResolver


class _MockLLM:
    """Mock LLM that returns a canned JSON response."""

    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, messages: list[dict], **kwargs) -> str:
        return self._response


class _FailingLLM:
    """Mock LLM that always raises."""

    def complete(self, messages: list[dict], **kwargs) -> str:
        raise RuntimeError("LLM unavailable")


class TestToolTagResolver:
    def test_resolve_returns_relevant_tags(self) -> None:
        llm = _MockLLM(json.dumps(["Items", "Compose"]))
        resolver = ToolTagResolver(llm)
        result = resolver.resolve("list my items", ["Items", "Compose", "Admin", "Config"])
        assert result == ["Items", "Compose"]

    def test_resolve_filters_invalid_tags(self) -> None:
        """LLM returns a tag not in available_tags — should be filtered out."""
        llm = _MockLLM(json.dumps(["Items", "Nonexistent"]))
        resolver = ToolTagResolver(llm)
        result = resolver.resolve("list items", ["Items", "Admin"])
        assert result == ["Items"]

    def test_resolve_empty_available_tags(self) -> None:
        llm = _MockLLM(json.dumps([]))
        resolver = ToolTagResolver(llm)
        assert resolver.resolve("anything", []) == []

    def test_resolve_fallback_on_error(self) -> None:
        """When LLM fails, return all available tags."""
        resolver = ToolTagResolver(_FailingLLM())
        result = resolver.resolve("query", ["Items", "Admin"])
        assert set(result) == {"Items", "Admin"}

    def test_resolve_fallback_on_bad_json(self) -> None:
        """When LLM returns invalid JSON, fall back to all tags."""
        llm = _MockLLM("not json at all")
        resolver = ToolTagResolver(llm)
        result = resolver.resolve("query", ["Items", "Admin"])
        assert set(result) == {"Items", "Admin"}

    def test_resolve_fallback_on_non_list(self) -> None:
        """When LLM returns JSON but not a list, fall back."""
        llm = _MockLLM(json.dumps({"tags": ["Items"]}))
        resolver = ToolTagResolver(llm)
        result = resolver.resolve("query", ["Items", "Admin"])
        assert set(result) == {"Items", "Admin"}
