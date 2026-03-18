"""Tests for CLI `init` and `discover` commands."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from symbiote.cli.main import _read_symbiote_config, _write_symbiote_config, app

runner = CliRunner()

_FAKE_SYMBIOTE_ID = "abc123de-0000-0000-0000-000000000001"
_FAKE_SERVER = "http://localhost:8000"
_FAKE_API_KEY = "sk-symbiote_test"


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_urlopen_mock(response_body: dict):
    """Return a context-manager mock that yields a fake HTTP response."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(response_body).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _write_config(tmp_path: Path, symbiote_id: str = _FAKE_SYMBIOTE_ID) -> None:
    _write_symbiote_config(
        {
            "server": _FAKE_SERVER,
            "api_key": _FAKE_API_KEY,
            "id": symbiote_id,
            "name": "TestBot",
        },
        path=tmp_path,
    )


# ── _read_symbiote_config / _write_symbiote_config helpers ───────────────────


class TestConfigHelpers:
    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        cfg = {"server": "http://x:8000", "api_key": "k", "id": "abc", "name": "bot"}
        _write_symbiote_config(cfg, path=tmp_path)
        result = _read_symbiote_config(path=tmp_path)
        assert result["server"] == "http://x:8000"
        assert result["api_key"] == "k"
        assert result["id"] == "abc"
        assert result["name"] == "bot"

    def test_read_missing_config_exits(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["discover", str(tmp_path)],
            catch_exceptions=False,
            env={"HOME": str(tmp_path)},
        )
        # Should fail because no .symbiote/config
        assert result.exit_code != 0 or "init" in result.output.lower()


# ── init ─────────────────────────────────────────────────────────────────────


class TestInit:
    def test_init_creates_new_symbiote(self, tmp_path: Path) -> None:
        mock_resp = _make_urlopen_mock({"id": _FAKE_SYMBIOTE_ID, "name": "TestBot"})

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("symbiote.cli.main._write_symbiote_config") as mock_write,
        ):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--server", _FAKE_SERVER,
                    "--api-key", _FAKE_API_KEY,
                    "--name", "TestBot",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "Connected" in result.output
        mock_write.assert_called_once()
        cfg = mock_write.call_args[0][0]
        assert cfg["id"] == _FAKE_SYMBIOTE_ID
        assert cfg["server"] == _FAKE_SERVER

    def test_init_links_existing_symbiote(self, tmp_path: Path) -> None:
        mock_resp = _make_urlopen_mock({"id": _FAKE_SYMBIOTE_ID, "name": "ExistingBot"})

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("symbiote.cli.main._write_symbiote_config") as mock_write,
        ):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--server", _FAKE_SERVER,
                    "--api-key", _FAKE_API_KEY,
                    "--id", _FAKE_SYMBIOTE_ID,
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        cfg = mock_write.call_args[0][0]
        assert cfg["id"] == _FAKE_SYMBIOTE_ID
        assert cfg["name"] == "ExistingBot"

    def test_init_http_error_exits_nonzero(self, tmp_path: Path) -> None:
        err = urllib.error.HTTPError(
            url=_FAKE_SERVER,
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"Unauthorized"),
        )

        with patch("urllib.request.urlopen", side_effect=err):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--server", _FAKE_SERVER,
                    "--api-key", "bad-key",
                    "--name", "Bot",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code != 0

    def test_init_connection_error_exits_nonzero(self) -> None:
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--server", "http://nowhere:9999",
                    "--api-key", _FAKE_API_KEY,
                    "--name", "Bot",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code != 0

    def test_init_writes_config_file(self, tmp_path: Path) -> None:
        mock_resp = _make_urlopen_mock({"id": _FAKE_SYMBIOTE_ID, "name": "TestBot"})

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch(
                "symbiote.cli.main._write_symbiote_config",
                side_effect=lambda cfg, path=Path("."): _write_symbiote_config(cfg, tmp_path),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--server", _FAKE_SERVER,
                    "--api-key", _FAKE_API_KEY,
                    "--name", "TestBot",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        written = _read_symbiote_config(path=tmp_path)
        assert written["id"] == _FAKE_SYMBIOTE_ID


# ── discover ─────────────────────────────────────────────────────────────────


class TestDiscover:
    def test_discover_prints_tools(self, tmp_path: Path) -> None:
        _write_config(tmp_path)
        response_data = {
            "discovered": 2,
            "errors": [],
            "tools": [
                {
                    "tool_id": "get_api_search",
                    "method": "GET",
                    "url_template": "/api/search",
                    "source_path": "/repo/routes.py",
                    "status": "pending",
                },
                {
                    "tool_id": "post_api_publish",
                    "method": "POST",
                    "url_template": "/api/publish",
                    "source_path": "/repo/routes.py",
                    "status": "pending",
                },
            ],
        }
        mock_resp = _make_urlopen_mock(response_data)

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch(
                "symbiote.cli.main._read_symbiote_config",
                return_value=_read_symbiote_config(path=tmp_path),
            ),
        ):
            result = runner.invoke(
                app,
                ["discover", str(tmp_path)],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "2 tool(s) discovered" in result.output
        assert "get_api_search" in result.output
        assert "post_api_publish" in result.output

    def test_discover_zero_tools(self, tmp_path: Path) -> None:
        _write_config(tmp_path)
        response_data = {"discovered": 0, "errors": [], "tools": []}
        mock_resp = _make_urlopen_mock(response_data)

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch(
                "symbiote.cli.main._read_symbiote_config",
                return_value=_read_symbiote_config(path=tmp_path),
            ),
        ):
            result = runner.invoke(
                app,
                ["discover", str(tmp_path)],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "0 tool(s) discovered" in result.output

    def test_discover_shows_warnings_on_errors(self, tmp_path: Path) -> None:
        _write_config(tmp_path)
        response_data = {
            "discovered": 0,
            "errors": ["openapi:/repo/bad.yaml: parse error"],
            "tools": [],
        }
        mock_resp = _make_urlopen_mock(response_data)

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch(
                "symbiote.cli.main._read_symbiote_config",
                return_value=_read_symbiote_config(path=tmp_path),
            ),
        ):
            result = runner.invoke(
                app,
                ["discover", "."],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Warnings" in result.output or "scan error" in result.output

    def test_discover_http_error_exits_nonzero(self, tmp_path: Path) -> None:
        _write_config(tmp_path)
        err = urllib.error.HTTPError(
            url=_FAKE_SERVER,
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"not found"),
        )

        with (
            patch("urllib.request.urlopen", side_effect=err),
            patch(
                "symbiote.cli.main._read_symbiote_config",
                return_value=_read_symbiote_config(path=tmp_path),
            ),
        ):
            result = runner.invoke(
                app,
                ["discover", "."],
                catch_exceptions=False,
            )

        assert result.exit_code != 0

    def test_discover_no_config_exits(self, tmp_path: Path) -> None:
        # No .symbiote/config — _read_symbiote_config should raise Exit(1)
        with patch(
            "symbiote.cli.main._read_symbiote_config",
            side_effect=SystemExit(1),
        ):
            result = runner.invoke(app, ["discover", "."])

        assert result.exit_code != 0

    def test_discover_sends_correct_symbiote_id(self, tmp_path: Path) -> None:
        _write_config(tmp_path, symbiote_id=_FAKE_SYMBIOTE_ID)
        response_data = {"discovered": 0, "errors": [], "tools": []}
        mock_resp = _make_urlopen_mock(response_data)

        captured_url = []

        def fake_urlopen(req, timeout=10):
            captured_url.append(req.full_url)
            return mock_resp

        with (
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
            patch(
                "symbiote.cli.main._read_symbiote_config",
                return_value=_read_symbiote_config(path=tmp_path),
            ),
        ):
            result = runner.invoke(
                app,
                ["discover", "."],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert _FAKE_SYMBIOTE_ID in captured_url[0]
        assert "/discover" in captured_url[0]
