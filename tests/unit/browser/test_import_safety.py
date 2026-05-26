"""Backward-compat guard: `import symbiote` must NOT pull in browser deps.

These tests are the line in the sand for "the new subpackage is opt-in."
If `import symbiote` ever starts importing playwright or search SDKs at the
top level, this suite fails. Don't relax these tests without a design review.
"""

from __future__ import annotations

import subprocess
import sys
import time


def _import_in_fresh_interpreter(target_module: str, probes: list[str]) -> dict[str, bool]:
    """Spawn a clean Python and report whether each probe module ended up in sys.modules."""
    probe_list = ",".join(repr(p) for p in probes)
    script = (
        "import sys, importlib;"
        f"importlib.import_module({target_module!r});"
        f"probes=[{probe_list}];"
        "print({p: (p in sys.modules) for p in probes})"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return eval(result.stdout.strip())  # safe: subprocess we control


def test_import_symbiote_does_not_load_playwright():
    """`import symbiote` must not trigger import of playwright."""
    loaded = _import_in_fresh_interpreter(
        "symbiote",
        ["playwright", "playwright.sync_api", "playwright.async_api"],
    )
    assert loaded == {
        "playwright": False,
        "playwright.sync_api": False,
        "playwright.async_api": False,
    }, f"Symbiote core leaks playwright imports: {loaded}"


def test_import_symbiote_does_not_load_search_sdks():
    """`import symbiote` must not trigger import of any search provider SDK."""
    loaded = _import_in_fresh_interpreter(
        "symbiote",
        ["tavily", "firecrawl", "exa_py"],
    )
    assert not any(loaded.values()), f"Symbiote core leaks search-SDK imports: {loaded}"


def test_import_symbiote_browser_does_not_load_playwright():
    """Even `import symbiote.browser` must stay lazy — Playwright only on register()."""
    loaded = _import_in_fresh_interpreter(
        "symbiote.browser",
        ["playwright", "playwright.sync_api"],
    )
    assert not any(loaded.values()), (
        f"symbiote.browser eagerly imports playwright at module load: {loaded}"
    )


def test_import_symbiote_stays_fast():
    """Smoke gate: importing symbiote must complete under 2s on dev machines.

    Browser/search SDKs are heavy. If they leak into core, import time spikes.
    The 2s budget is generous; CI baseline should be ~0.5s.
    """
    start = time.perf_counter()
    subprocess.run(
        [sys.executable, "-c", "import symbiote"],
        capture_output=True,
        text=True,
        check=True,
    )
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"import symbiote took {elapsed:.2f}s — too slow"
