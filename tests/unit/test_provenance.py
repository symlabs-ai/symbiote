"""Tests for core/provenance.py — ContextVar write-origin tagging."""

from __future__ import annotations

import threading

from symbiote.core.provenance import (
    BACKGROUND_REVIEW,
    FOREGROUND,
    get_current_write_origin,
    is_background_review,
    reset_current_write_origin,
    set_current_write_origin,
)


def test_default_is_foreground():
    assert get_current_write_origin() == FOREGROUND
    assert is_background_review() is False


def test_set_and_reset_roundtrip():
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        assert get_current_write_origin() == BACKGROUND_REVIEW
        assert is_background_review() is True
    finally:
        reset_current_write_origin(token)
    assert get_current_write_origin() == FOREGROUND


def test_falsy_origin_normalizes_to_foreground():
    token = set_current_write_origin("")  # type: ignore[arg-type]
    try:
        assert get_current_write_origin() == FOREGROUND
    finally:
        reset_current_write_origin(token)


def test_threads_dont_leak_origin():
    """ContextVar must isolate state across threads in both directions.

    Unlike asyncio Tasks, ``threading.Thread`` does NOT inherit the parent's
    Context — each thread starts with the ContextVar default. This test pins
    the two invariants we rely on:

    1. A thread's ``.set()`` does not affect the parent thread (the case that
       actually matters for the Sprint 4 background-review fork — we don't
       want the daemon's BACKGROUND_REVIEW origin leaking into the user-
       facing turn that spawned it).
    2. The thread starts at the default, not at whatever the parent had set.
    """
    main_observed: list[str] = []
    thread_observed: list[str] = []

    def worker():
        thread_observed.append(get_current_write_origin())
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            thread_observed.append(get_current_write_origin())
        finally:
            reset_current_write_origin(token)

    token = set_current_write_origin("custom-origin")
    try:
        main_observed.append(get_current_write_origin())
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        main_observed.append(get_current_write_origin())
    finally:
        reset_current_write_origin(token)

    # Thread starts at the ContextVar default (FOREGROUND), not "custom-origin".
    assert thread_observed[0] == FOREGROUND
    # Inside the thread, .set() works.
    assert thread_observed[1] == BACKGROUND_REVIEW
    # The thread's set/reset does NOT leak back to the parent.
    assert main_observed == ["custom-origin", "custom-origin"]
