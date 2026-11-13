"""Regression: the CLI project config holding the API key is private and
round-trips values containing '%' (report item S-11)."""

from __future__ import annotations

import stat
from pathlib import Path

from symbiote.cli.main import _read_symbiote_config, _write_symbiote_config


def test_config_file_is_owner_only(tmp_path: Path) -> None:
    _write_symbiote_config({"api_key": "sk-symbiote_x", "server": "http://h"}, tmp_path)
    mode = stat.S_IMODE((tmp_path / ".symbiote" / "config").stat().st_mode)
    assert mode == 0o600


def test_existing_file_permissions_are_tightened(tmp_path: Path) -> None:
    cfg = tmp_path / ".symbiote" / "config"
    cfg.parent.mkdir()
    cfg.write_text("[symbiote]\n")
    cfg.chmod(0o644)
    _write_symbiote_config({"api_key": "k"}, tmp_path)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_percent_values_round_trip(tmp_path: Path) -> None:
    values = {"api_key": "sk-symbiote_a%b%%c", "name": "100% bot"}
    _write_symbiote_config(values, tmp_path)
    assert _read_symbiote_config(tmp_path) == values
