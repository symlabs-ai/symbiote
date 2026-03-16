"""Tests for KernelConfig — T-01."""

from pathlib import Path

import pytest
import yaml

from symbiote.config.models import KernelConfig

# ── Defaults ──────────────────────────────────────────────────────────


class TestKernelConfigDefaults:
    """KernelConfig must work with zero configuration."""

    def test_default_db_path(self):
        cfg = KernelConfig()
        assert cfg.db_path == Path(".symbiote/symbiote.db")

    def test_default_context_budget(self):
        cfg = KernelConfig()
        assert cfg.context_budget == 4000

    def test_default_llm_provider(self):
        cfg = KernelConfig()
        assert cfg.llm_provider == "forge"

    def test_default_log_level(self):
        cfg = KernelConfig()
        assert cfg.log_level == "INFO"


# ── Type coercion / validation ────────────────────────────────────────


class TestKernelConfigValidation:
    """Fields must validate and coerce correctly."""

    def test_db_path_accepts_string(self):
        cfg = KernelConfig(db_path="/tmp/test.db")
        assert cfg.db_path == Path("/tmp/test.db")
        assert isinstance(cfg.db_path, Path)

    def test_context_budget_must_be_positive(self):
        with pytest.raises(ValueError):
            KernelConfig(context_budget=0)

    def test_context_budget_must_be_positive_negative(self):
        with pytest.raises(ValueError):
            KernelConfig(context_budget=-1)

    def test_log_level_uppercased(self):
        cfg = KernelConfig(log_level="debug")
        assert cfg.log_level == "DEBUG"

    def test_log_level_rejects_invalid(self):
        with pytest.raises(ValueError):
            KernelConfig(log_level="BANANA")

    def test_llm_provider_stripped(self):
        cfg = KernelConfig(llm_provider="  forge  ")
        assert cfg.llm_provider == "forge"

    def test_llm_provider_rejects_empty(self):
        with pytest.raises(ValueError):
            KernelConfig(llm_provider="")

    def test_llm_provider_rejects_whitespace_only(self):
        with pytest.raises(ValueError):
            KernelConfig(llm_provider="   ")


# ── YAML loading ──────────────────────────────────────────────────────


class TestKernelConfigFromYaml:
    """Config should be loadable from a YAML file."""

    def test_from_yaml_full(self, tmp_path):
        yaml_file = tmp_path / "config.yml"
        yaml_file.write_text(
            yaml.dump(
                {
                    "db_path": "/data/symbiote.db",
                    "context_budget": 8000,
                    "llm_provider": "openrouter",
                    "log_level": "WARNING",
                }
            )
        )
        cfg = KernelConfig.from_yaml(yaml_file)
        assert cfg.db_path == Path("/data/symbiote.db")
        assert cfg.context_budget == 8000
        assert cfg.llm_provider == "openrouter"
        assert cfg.log_level == "WARNING"

    def test_from_yaml_partial_uses_defaults(self, tmp_path):
        yaml_file = tmp_path / "config.yml"
        yaml_file.write_text(yaml.dump({"context_budget": 2000}))
        cfg = KernelConfig.from_yaml(yaml_file)
        assert cfg.context_budget == 2000
        assert cfg.db_path == Path(".symbiote/symbiote.db")  # default

    def test_from_yaml_empty_file_uses_defaults(self, tmp_path):
        yaml_file = tmp_path / "config.yml"
        yaml_file.write_text("")
        cfg = KernelConfig.from_yaml(yaml_file)
        assert cfg.db_path == Path(".symbiote/symbiote.db")

    def test_from_yaml_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            KernelConfig.from_yaml(tmp_path / "nonexistent.yml")
