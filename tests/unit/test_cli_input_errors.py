"""Regression: bad CLI input ends with one error line and a non-zero exit
code, never a traceback; the default server is the documented port (report
items C-2, C-3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from symbiote.cli.main import app
from tests.support import ScriptedLLM, make_kernel


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "cli.db"
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=path)
    sym = kernel.create_symbiote(name="a", role="r")
    session = kernel.start_session(sym.id)
    kernel.shutdown()
    return path, sym.id, session.id


def _run(*args: str):
    # catch_exceptions=False: an uncaught exception fails the test with its
    # traceback instead of being folded into result.exception.
    return CliRunner().invoke(app, list(args), catch_exceptions=False)


@pytest.mark.parametrize(
    "argv",
    [
        ("create", "--name", "x", "--role", "r", "--persona-json", "{not json"),
        ("session", "start", "00000000-0000-0000-0000-000000000000"),
        ("learn", "{session}", "fact", "--importance", "5"),
        ("learn", "{session}", "fact", "--type", "gossip"),
        ("skills", "list", "--root", "/nonexistent/skills"),
    ],
)
def test_bad_input_is_one_line_and_exit_1(db, argv) -> None:
    path, sid, session = db
    args = [a.replace("{sid}", sid).replace("{session}", session) for a in argv]

    result = _run("--db-path", str(path), *args)

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    errors = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(errors) == 1, result.output
    assert errors[0].startswith(("Error", "No skills"))


def test_default_server_is_8008() -> None:
    import inspect

    import symbiote.cli.main as cli

    source = inspect.getsource(cli)
    assert "localhost:8000" not in source.replace("e.g. http://localhost:8000", "")
    assert "http://localhost:8008" in source
