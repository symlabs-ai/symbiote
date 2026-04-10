"""Tests for the bash builtin tool — descriptor and handler."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import _BUILTIN_DESCRIPTORS, ToolGateway, _bash

# ── Descriptor ────────────────────────────────────────────────────────────────


class TestBashDescriptor:
    def test_registered_in_builtins(self):
        assert "bash" in _BUILTIN_DESCRIPTORS

    def test_risk_level_high(self):
        assert _BUILTIN_DESCRIPTORS["bash"].risk_level == "high"

    def test_handler_type_builtin(self):
        assert _BUILTIN_DESCRIPTORS["bash"].handler_type == "builtin"

    def test_tags(self):
        tags = _BUILTIN_DESCRIPTORS["bash"].tags
        assert "shell" in tags
        assert "system" in tags

    def test_gateway_registers_bash(self):
        gate = MagicMock(spec=PolicyGate)
        gw = ToolGateway(policy_gate=gate)
        assert gw.has_tool("bash")
        assert gw.get_risk_level("bash") == "high"


# ── Handler ───────────────────────────────────────────────────────────────────


class TestBashHandler:
    def test_echo(self):
        result = _bash({"command": "echo hello"})
        assert result["stdout"].strip() == "hello"
        assert "return_code_interpretation" not in result

    def test_exit_nonzero(self):
        result = _bash({"command": "exit 42"})
        assert result["return_code_interpretation"] == "exit_code:42"

    def test_stderr_captured(self):
        result = _bash({"command": "echo oops >&2"})
        assert "oops" in result["stderr"]

    def test_timeout(self):
        result = _bash({"command": "sleep 10", "timeout": 1})
        assert result["interrupted"] is True
        assert result["return_code_interpretation"] == "timeout"
        assert "timeout" in result["stderr"].lower()

    def test_empty_command_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            _bash({"command": ""})

    def test_whitespace_command_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            _bash({"command": "   "})

    def test_cwd_respected(self, tmp_path: Path):
        result = _bash({"command": "pwd", "_cwd": str(tmp_path)})
        assert result["stdout"].strip() == str(tmp_path.resolve())

    def test_default_timeout_on_bad_value(self):
        # Should not raise — falls back to 30s default
        result = _bash({"command": "echo ok", "timeout": -5})
        assert result["stdout"].strip() == "ok"

    def test_empty_output(self):
        result = _bash({"command": "true"})
        # Must always have at least stdout key
        assert "stdout" in result
