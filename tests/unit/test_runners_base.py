"""Tests for Runner base, RunnerRegistry, and EchoRunner."""

from __future__ import annotations

import pytest

from symbiote.core.context import AssembledContext
from symbiote.runners.base import EchoRunner, RunnerRegistry, RunResult

# ── RunResult defaults ────────────────────────────────────────────────────────


class TestRunResult:
    def test_defaults(self):
        r = RunResult(success=True)
        assert r.success is True
        assert r.output is None
        assert r.error is None
        assert r.runner_type == ""

    def test_with_values(self):
        r = RunResult(success=False, output={"k": "v"}, error="boom", runner_type="test")
        assert r.success is False
        assert r.output == {"k": "v"}
        assert r.error == "boom"
        assert r.runner_type == "test"


# ── RunnerRegistry ───────────────────────────────────────────────────────────


class TestRunnerRegistry:
    def test_register_appears_in_list(self):
        registry = RunnerRegistry()
        runner = EchoRunner()
        registry.register(runner)
        assert "echo" in registry.list_runners()

    def test_select_matching_intent(self):
        registry = RunnerRegistry()
        runner = EchoRunner()
        registry.register(runner)
        selected = registry.select("echo")
        assert selected is runner

    def test_select_no_match_returns_none(self):
        registry = RunnerRegistry()
        registry.register(EchoRunner())
        assert registry.select("unknown_intent") is None

    def test_empty_registry_select_returns_none(self):
        registry = RunnerRegistry()
        assert registry.select("echo") is None

    def test_empty_registry_list_runners(self):
        registry = RunnerRegistry()
        assert registry.list_runners() == []

    def test_multiple_runners_correct_selection(self):
        """Register multiple runners; select returns the one that matches."""

        class AlphaRunner:
            runner_type: str = "alpha"

            def can_handle(self, intent: str) -> bool:
                return intent == "alpha"

            def run(self, context: AssembledContext) -> RunResult:
                return RunResult(success=True, runner_type=self.runner_type)

        registry = RunnerRegistry()
        alpha = AlphaRunner()
        echo = EchoRunner()
        registry.register(alpha)
        registry.register(echo)

        assert registry.select("alpha") is alpha
        assert registry.select("echo") is echo
        assert set(registry.list_runners()) == {"alpha", "echo"}


# ── EchoRunner ───────────────────────────────────────────────────────────────


class TestEchoRunner:
    def test_can_handle_echo(self):
        runner = EchoRunner()
        assert runner.can_handle("echo") is True

    def test_cannot_handle_other(self):
        runner = EchoRunner()
        assert runner.can_handle("other") is False

    def test_run_returns_user_input(self):
        runner = EchoRunner()
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess1",
            user_input="hello world",
        )
        result = runner.run(ctx)
        assert result.success is True
        assert result.output == "hello world"
        assert result.runner_type == "echo"
        assert result.error is None
