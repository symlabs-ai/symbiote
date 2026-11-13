"""Regression: the browser supervisor's SIGTERM handler cleans up and then
lets the process terminate, and keeps calling a handler the host installed
before it (report item T-6)."""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap

_PRELUDE = textwrap.dedent(
    """
    import os, signal, sys, time
    from symbiote.browser.browser import supervisor

    class Provider:
        name = "fake"
        def close_all(self):
            print("cleanup", flush=True)
    """
)


def _run(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _PRELUDE + textwrap.dedent(body)],
        capture_output=True, text=True, timeout=20,
    )


def test_sigterm_still_terminates_the_process() -> None:
    proc = _run(
        """
        supervisor.register_provider(Provider())
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
        print("still alive", flush=True)
        """
    )
    assert "cleanup" in proc.stdout
    assert "still alive" not in proc.stdout
    assert proc.returncode == -signal.SIGTERM


def test_host_handler_installed_before_is_still_called() -> None:
    proc = _run(
        """
        def host(signum, frame):
            print("host handler", flush=True)
            sys.exit(3)
        signal.signal(signal.SIGTERM, host)
        supervisor.register_provider(Provider())
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
        print("still alive", flush=True)
        """
    )
    assert proc.stdout.splitlines()[:2] == ["cleanup", "host handler"]
    assert proc.returncode == 3


def test_ignored_sigterm_stays_ignored() -> None:
    proc = _run(
        """
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        supervisor.register_provider(Provider())
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.2)
        print("still alive", flush=True)
        """
    )
    assert "still alive" in proc.stdout
    assert proc.returncode == 0
