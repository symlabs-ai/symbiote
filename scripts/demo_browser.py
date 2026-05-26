#!/usr/bin/env python3
"""Visual demo of symbiote.browser — Chromium opens on screen.

Two modes:
    scripted (default): direct tool calls, no LLM, deterministic
    agentic           : Symbiota driving via LLM (uses .env SymGateway config)

Usage:
    .venv/bin/python scripts/demo_browser.py
    .venv/bin/python scripts/demo_browser.py --mode scripted --headed
    .venv/bin/python scripts/demo_browser.py --mode agentic --goal "Find ..."
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure src/ on path when running from repo root
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from symbiote.adapters.llm.base import MockLLMAdapter  # noqa: E402
from symbiote.browser import register  # noqa: E402
from symbiote.browser.config import BrowserOptions  # noqa: E402
from symbiote.config.models import KernelConfig  # noqa: E402
from symbiote.core.kernel import SymbioteKernel  # noqa: E402

DEFAULT_URL = "https://en.wikipedia.org/wiki/Python_(programming_language)"
BROWSER_TOOLS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_fill",
    "browser_close",
]


def _build_kernel(db_path: str, llm) -> SymbioteKernel:
    return SymbioteKernel(KernelConfig(db_path=db_path), llm=llm)


async def run_scripted(headed: bool, slow_mo: int, url: str) -> None:
    """Direct tool calls, no LLM. Proves the plumbing end-to-end."""
    print(f"[scripted] Headed={headed} slow_mo={slow_mo}ms target={url}")

    kernel = _build_kernel(db_path=":memory:", llm=MockLLMAdapter())
    register(
        kernel,
        browser_backend="chromium",
        browser_options=BrowserOptions(
            headed=headed,
            slow_mo=slow_mo,
            timeout_ms=20_000,
        ),
    )

    bot = kernel.create_symbiote(name="Demo", role="visual demo")
    kernel.environment.configure(symbiote_id=bot.id, tools=BROWSER_TOOLS)
    task_id = "demo-1"

    print("\n[scripted] 1/4 navigate")
    r = await kernel._tool_gateway.execute_async(  # noqa: SLF001
        symbiote_id=bot.id,
        session_id=None,
        tool_id="browser_navigate",
        params={"url": url, "task_id": task_id},
        timeout=30.0,
    )
    print("        →", r.output if r.success else f"FAILED: {r.error}")

    print("\n[scripted] 2/4 snapshot")
    r = await kernel._tool_gateway.execute_async(  # noqa: SLF001
        symbiote_id=bot.id,
        session_id=None,
        tool_id="browser_snapshot",
        params={"task_id": task_id},
        timeout=30.0,
    )
    if not r.success:
        print("        FAILED:", r.error)
        return
    text = r.output["text"]
    refs = r.output["refs"]
    print(f"        → text length: {len(text)} chars, {len(refs)} interactive refs")
    print("        first 8 refs:", refs[:8])
    print("        ---- snapshot preview (first 30 lines) ----")
    for ln in text.splitlines()[:30]:
        print("        " + ln)

    # Try clicking a few candidate refs — Wikipedia's @e1 is an invisible "Jump
    # to content" anchor. Walk forward to find a visible, clickable element.
    if refs:
        for candidate in refs[:6]:
            print(f"\n[scripted] 3/4 click {candidate}")
            r = await kernel._tool_gateway.execute_async(  # noqa: SLF001
                symbiote_id=bot.id,
                session_id=None,
                tool_id="browser_click",
                params={"ref": candidate, "task_id": task_id},
                timeout=8.0,
            )
            if r.success:
                print("        →", r.output)
                if headed:
                    await asyncio.sleep(1.5)
                break
            print(f"        skipped ({(r.error or '')[:60]}…)")
        else:
            print("        (no clickable ref found in first 6 — skipping)")

    print("\n[scripted] 4/4 close")
    r = await kernel._tool_gateway.execute_async(  # noqa: SLF001
        symbiote_id=bot.id,
        session_id=None,
        tool_id="browser_close",
        params={"task_id": task_id},
        timeout=10.0,
    )
    print("        →", r.output if r.success else f"FAILED: {r.error}")

    # Final cleanup of the provider (closes Playwright)

    # Walk supervisor registry to close cleanly
    from symbiote.browser.browser.supervisor import _providers

    for prov in list(_providers):
        if hasattr(prov, "close_all_async"):
            await prov.close_all_async()

    print("\n[scripted] done")


def run_agentic(headed: bool, slow_mo: int, goal: str) -> None:
    """Symbiota driving the browser via an LLM. Uses .env SymGateway config."""
    print(
        "[agentic] Mode not wired yet — needs LLM adapter integration. "
        "Run scripted first to validate the plumbing; agentic comes next."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="symbiote.browser visual demo")
    ap.add_argument("--mode", choices=["scripted", "agentic"], default="scripted")
    ap.add_argument("--headed", action="store_true", help="Show the Chromium window")
    ap.add_argument(
        "--slow-mo",
        type=int,
        default=400,
        help="Delay between actions (ms). 0 = no delay.",
    )
    ap.add_argument("--url", default=DEFAULT_URL, help="Starting URL for scripted mode")
    ap.add_argument(
        "--goal",
        default="Find what year Python was first released",
        help="Goal for agentic mode",
    )
    args = ap.parse_args()

    if args.mode == "scripted":
        asyncio.run(run_scripted(args.headed, args.slow_mo, args.url))
    else:
        run_agentic(args.headed, args.slow_mo, args.goal)


if __name__ == "__main__":
    main()
