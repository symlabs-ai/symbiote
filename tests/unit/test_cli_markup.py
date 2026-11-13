"""Regression: dynamic text printed by the CLI is never parsed as Rich
markup — a memory with ``[/tmp]`` or a message with ``[/INST]`` used to crash
the command (MarkupError), and ``learn`` lost its ``[type, importance]``
suffix (report item C-1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from symbiote.cli.main import app
from symbiote.core.models import MemoryEntry
from tests.support import ScriptedLLM, make_kernel


@pytest.fixture()
def seeded(tmp_path: Path):
    db = tmp_path / "cli.db"
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    sym = kernel.create_symbiote(name="bot [/x]", role="r")
    session = kernel.start_session(sym.id)
    kernel._sessions.add_message(session.id, "user", "prompt with [/INST] inside [bold]")
    kernel._sessions.add_message(session.id, "assistant", "answer [/tmp] done")
    kernel._memory.store(MemoryEntry(symbiote_id=sym.id, type="factual", scope="global",
                                     source="user", content="paths like [/tmp] matter",
                                     importance=0.7))
    kernel.shutdown()
    return db, sym.id, session.id


def _run(db: Path, *args: str):
    return CliRunner().invoke(app, ["--db-path", str(db), *args])


@pytest.mark.parametrize(
    "args",
    [
        ("memory", "search", "tmp", "--symbiote", "{sid}"),
        ("export", "session", "{session}"),
        ("reflect", "{session}"),
        ("session", "close", "{session}"),
        ("list",),
    ],
)
def test_commands_survive_markup_like_text(seeded, args) -> None:
    db, sid, session = seeded
    argv = [a.format(sid=sid, session=session) for a in args]

    result = _run(db, *argv)

    assert result.exit_code == 0, result.output
    assert result.exception is None


def test_export_shows_the_text_verbatim(seeded) -> None:
    db, _sid, session = seeded
    out = _run(db, "export", "session", session).output
    assert "[/INST]" in out and "[bold]" in out


def test_learn_shows_type_and_importance(seeded) -> None:
    db, _sid, session = seeded
    result = _run(db, "learn", session, "likes tea", "--type", "preference", "--importance", "0.9")
    assert result.exit_code == 0, result.output
    assert "[preference, importance=0.9]" in result.output
