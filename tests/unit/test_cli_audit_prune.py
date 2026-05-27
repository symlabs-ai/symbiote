"""Tests for ``symbiote audit prune`` (retention CLI, F5)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.cli.main import app

runner = CliRunner()


def _insert_reflection(adapter: SQLiteAdapter, *, age_sql: str) -> None:
    adapter.execute(
        "INSERT INTO reflection_audit "
        "(id, session_id, symbiote_id, mode, keyword_facts_json, "
        "llm_facts_json, llm_error, created_at) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, {age_sql})",
        (str(uuid4()), "sess", "sym", "kw", "[]", "[]", None),
    )


def _insert_skill_review(adapter: SQLiteAdapter, *, age_sql: str) -> None:
    adapter.execute(
        "INSERT INTO skill_review_audit "
        "(id, session_id, symbiote_id, trigger, applied, skipped, "
        "ok, error, ops_json, created_at) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {age_sql})",
        (str(uuid4()), "sess", "sym", "nudge", 0, 0, 1, None, "[]"),
    )


def _seed_audit_rows(adapter: SQLiteAdapter, *, recent: int = 0, old: int = 0) -> None:
    """Insert N recent + N old rows into BOTH audit tables.

    'recent' rows have created_at = now.
    'old'    rows have created_at = 200 days ago (well past any reasonable cutoff).
    """
    for _ in range(recent):
        _insert_reflection(adapter, age_sql="datetime('now')")
        _insert_skill_review(adapter, age_sql="datetime('now')")
    for _ in range(old):
        _insert_reflection(adapter, age_sql="datetime('now', '-200 days')")
        _insert_skill_review(adapter, age_sql="datetime('now', '-200 days')")


@pytest.fixture()
def db_with_rows(tmp_path):
    db = tmp_path / "prune.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    _seed_audit_rows(adp, recent=2, old=3)
    adp.close()
    yield db


# ── CLI invocations ──────────────────────────────────────────────────────


def test_dry_run_counts_without_deleting(db_with_rows):
    res = runner.invoke(app, [
        "--db-path", str(db_with_rows),
        "audit", "prune", "--days", "90", "--dry-run",
    ])
    assert res.exit_code == 0, res.output
    assert "would delete 3" in res.output
    # Confirm nothing was actually deleted.
    adp = SQLiteAdapter(db_path=db_with_rows)
    n_reflection = adp.fetch_one("SELECT COUNT(*) AS c FROM reflection_audit")["c"]
    n_skill = adp.fetch_one("SELECT COUNT(*) AS c FROM skill_review_audit")["c"]
    adp.close()
    assert n_reflection == 5  # 2 recent + 3 old
    assert n_skill == 5


def test_prune_deletes_old_rows_only(db_with_rows):
    res = runner.invoke(app, [
        "--db-path", str(db_with_rows),
        "audit", "prune", "--days", "90",
    ])
    assert res.exit_code == 0, res.output
    assert "deleted 3" in res.output
    # Recent rows preserved.
    adp = SQLiteAdapter(db_path=db_with_rows)
    n_reflection = adp.fetch_one("SELECT COUNT(*) AS c FROM reflection_audit")["c"]
    n_skill = adp.fetch_one("SELECT COUNT(*) AS c FROM skill_review_audit")["c"]
    adp.close()
    assert n_reflection == 2
    assert n_skill == 2


def test_prune_table_filter_reflection_only(db_with_rows):
    res = runner.invoke(app, [
        "--db-path", str(db_with_rows),
        "audit", "prune", "--days", "90", "--table", "reflection",
    ])
    assert res.exit_code == 0, res.output
    adp = SQLiteAdapter(db_path=db_with_rows)
    n_reflection = adp.fetch_one("SELECT COUNT(*) AS c FROM reflection_audit")["c"]
    n_skill = adp.fetch_one("SELECT COUNT(*) AS c FROM skill_review_audit")["c"]
    adp.close()
    # Reflection pruned, skill_review untouched.
    assert n_reflection == 2
    assert n_skill == 5


def test_prune_table_filter_skill_review_only(db_with_rows):
    res = runner.invoke(app, [
        "--db-path", str(db_with_rows),
        "audit", "prune", "--days", "90", "--table", "skill_review",
    ])
    assert res.exit_code == 0, res.output
    adp = SQLiteAdapter(db_path=db_with_rows)
    n_reflection = adp.fetch_one("SELECT COUNT(*) AS c FROM reflection_audit")["c"]
    n_skill = adp.fetch_one("SELECT COUNT(*) AS c FROM skill_review_audit")["c"]
    adp.close()
    assert n_reflection == 5
    assert n_skill == 2


def test_prune_invalid_table_exits_with_error(db_with_rows):
    res = runner.invoke(app, [
        "--db-path", str(db_with_rows),
        "audit", "prune", "--table", "bogus",
    ])
    # Typer's BadParameter exits with code 2 (Click convention for usage
    # errors). The earlier hand-rolled typer.Exit(1) was replaced in P1.
    assert res.exit_code == 2
    out = res.output + (res.stderr or "")
    assert "--table" in out
    assert "bogus" in out


def test_prune_empty_db_no_op(tmp_path):
    """No rows at all → both tables show 0/0, no errors."""
    db = tmp_path / "empty.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    adp.close()
    res = runner.invoke(app, [
        "--db-path", str(db),
        "audit", "prune", "--days", "30",
    ])
    assert res.exit_code == 0, res.output
    assert "nothing to do" in res.output or "deleted 0" in res.output


def test_prune_short_window_keeps_recent_only(db_with_rows):
    """--days 1 keeps only rows from today; 3 old (200d) gone."""
    res = runner.invoke(app, [
        "--db-path", str(db_with_rows),
        "audit", "prune", "--days", "1",
    ])
    assert res.exit_code == 0, res.output
    adp = SQLiteAdapter(db_path=db_with_rows)
    n_reflection = adp.fetch_one("SELECT COUNT(*) AS c FROM reflection_audit")["c"]
    n_skill = adp.fetch_one("SELECT COUNT(*) AS c FROM skill_review_audit")["c"]
    adp.close()
    # Old rows (200d) deleted, recent (today) kept.
    assert n_reflection == 2
    assert n_skill == 2


# ── Sprint 5 follow-up review fixes ───────────────────────────────────────


def test_prune_negative_days_rejected(db_with_rows):
    """P2: Typer min=0 rejects negative --days before reaching SQL."""
    res = runner.invoke(app, [
        "--db-path", str(db_with_rows),
        "audit", "prune", "--days", "-5",
    ])
    assert res.exit_code != 0
    out = res.output + (res.stderr or "")
    # Typer surfaces the min violation in its own format — check for the flag.
    assert "--days" in out or "Usage" in out


def test_prune_dry_run_emits_explicit_disclaimer(db_with_rows):
    """P3: Even in dry-run with rows that WOULD be deleted, the user sees
    an explicit 'no rows were deleted' line, not just the table."""
    res = runner.invoke(app, [
        "--db-path", str(db_with_rows),
        "audit", "prune", "--days", "90", "--dry-run",
    ])
    assert res.exit_code == 0, res.output
    assert "Dry-run" in res.output
    assert "no rows were deleted" in res.output


def test_prune_live_with_nothing_old_shows_positive_message(tmp_path):
    """P3: Live run with zero deletable rows emits 'Nothing to prune'
    instead of silence — useful for cron log readers."""
    db = tmp_path / "fresh.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    # Seed only recent rows (none past the cutoff).
    _seed_audit_rows(adp, recent=2)
    adp.close()
    res = runner.invoke(app, [
        "--db-path", str(db),
        "audit", "prune", "--days", "90",
    ])
    assert res.exit_code == 0, res.output
    assert "Nothing to prune" in res.output
