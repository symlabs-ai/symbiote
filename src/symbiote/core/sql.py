"""Helpers for the portable SQL the kernel sends through ``StoragePort``.

The kernel writes one SQL text for every adapter (see the "Portable SQL"
rules in ``StoragePort``). The constructs that differ between databases are
kept out of it and built here instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

LIKE_ESCAPE = "\\"
_ASCII_LOWER = {code: code + 32 for code in range(ord("A"), ord("Z") + 1)}


def ascii_lower(text: str) -> str:
    """Lower-case the ASCII letters only, as SQL ``LOWER`` does on the
    kernel's text columns in every supported database."""
    return text.translate(_ASCII_LOWER)


def like_contains(text: str) -> str:
    """Pattern for ``contains(column)`` matching ``text`` literally:
    ``%`` and ``_`` in ``text`` are not wildcards."""
    escaped = (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{ascii_lower(escaped)}%"


def contains(column: str) -> str:
    """SQL condition "``column`` contains the text", one ``?`` parameter built
    with ``like_contains``.

    Case-insensitive for ASCII letters and case-sensitive for the others, on
    every database: plain ``LIKE`` is case-insensitive on SQLite and
    case-sensitive on PostgreSQL, and its default escape character differs
    too, so neither is left to the database.
    """
    return f"LOWER({column}) LIKE ? ESCAPE '{LIKE_ESCAPE}'"


def differs(left: str, right: str) -> str:
    """SQL condition "``left`` and ``right`` are different", treating NULL as
    a value (two NULLs are equal). The portable spelling of SQLite's
    ``IS NOT`` / PostgreSQL's ``IS DISTINCT FROM``."""
    return (
        f"({left} <> {right} OR ({left} IS NULL AND {right} IS NOT NULL) "
        f"OR ({left} IS NOT NULL AND {right} IS NULL))"
    )


def days_ago(days: float, *, now: datetime | None = None) -> str:
    """ISO timestamp of ``days`` days ago, comparable with the timestamps the
    kernel stores (``datetime.now(UTC).isoformat()``)."""
    return ((now or datetime.now(tz=UTC)) - timedelta(days=days)).isoformat()


__all__ = ["LIKE_ESCAPE", "ascii_lower", "contains", "days_ago", "differs", "like_contains"]
