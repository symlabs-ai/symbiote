"""symbiote.browser probes — extras extract/browser/stealth NOT installed. No network:
the SymGateway URL is a dummy that is never contacted (only handlers that fail before I/O are invoked)."""
import asyncio
import gc
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

os.environ["SYMGATEWAY_BASE_URL"] = "http://127.0.0.1:9/v1"   # never contacted
os.environ["SYMGATEWAY_API_KEY"] = "dummy"

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

HERE = Path(sys.argv[1])
for f in HERE.glob("b*.db*"):
    f.unlink()


def mk(n):
    return SymbioteKernel(config=KernelConfig(db_path=HERE / f"b{n}.db"), llm=MockLLMAdapter())


def web_tools(k):
    return sorted(t for t in k.tool_gateway.list_tools() if t.startswith(("web_", "browser_")))


print("== 1. README register(...) verbatim, extras missing ==")
import importlib.util
print("  playwright installed:", importlib.util.find_spec("playwright") is not None,
      "| forge_scraper installed:", importlib.util.find_spec("forge_scraper") is not None)
k = mk(1)
prev_term = signal.getsignal(signal.SIGTERM)
register(k, search_backend="brave", extract_backend=["forge_scraper", "firecrawl"], browser_backend="chromium",
         policy={"blocklist": ["*.ads.com", "doubleclick.net", "93.184.216.34"]})
t = web_tools(k)
print(f"  register() raised nothing; {len(t)} tools registered:", t)
print("  SIGTERM handler before:", prev_term, "| after:", signal.getsignal(signal.SIGTERM))
sym = k.create_symbiote(name="W", role="assistant")
k.environment.configure(symbiote_id=sym.id, tools=t)
gw = k.tool_gateway


async def calls():
    r = await gw.execute_async(sym.id, None, "browser_snapshot", {})
    print("  browser_snapshot ->", r.success, (r.error or "")[:110])
    r = await gw.execute_async(sym.id, None, "browser_navigate", {"url": "http://93.184.216.34/"})
    print("  browser_navigate(blocklisted IP) ->", r.success, (r.error or "")[:80])
    r = await gw.execute_async(sym.id, None, "web_extract", {"url": "http://93.184.216.34/"})
    print("  web_extract(same blocklisted IP) ->", r.success, (r.error or "")[:110])


asyncio.run(calls())

print("\n== 2. sync kernel.message() with an async (browser/web/MCP-style) tool ==")
k._llm._responses = ['```tool_call\n{"tool": "browser_snapshot", "params": {}}\n```', "The page says hello."]
s = k.start_session(sym.id)
out = k.message(s.id, "what is on the page?")
fed = k._llm.calls[1]["messages"][-1]["content"]
print("  tool result fed back to the LLM:", fed[:160].replace("\n", " | "))
audit = k._storage.fetch_all("SELECT tool_id, action, result FROM audit_log WHERE tool_id='browser_snapshot' ORDER BY created_at DESC LIMIT 1")
print("  audit row:", [dict(a) for a in audit], "| final answer:", repr(out)[:60])

print("\n== 3. web_crawl with the documented 'domain' input ==")
from symbiote.browser.search.tools import build_handlers


class FakeCrawl:
    async def crawl(self, domain, *, instruction, max_pages=10):
        return [{"url": domain}]


h = build_handlers(crawl=FakeCrawl())["web_crawl"]
for d in ["93.184.216.34", "http://93.184.216.34/"]:
    try:
        print(f"  web_crawl(domain={d!r}) ->", asyncio.run(h({"domain": d, "instruction": "x"})))
    except Exception as e:  # noqa: BLE001
        print(f"  web_crawl(domain={d!r}) -> {type(e).__name__}: {e}")

print("\n== 4. WebsitePolicy ==")
from symbiote.browser.config import PolicyConfig
from symbiote.browser.hooks.website_policy import WebsitePolicy

p = WebsitePolicy(PolicyConfig(blocklist=["ads.com", "*.tracker.io"]))
for u in ["http://ads.com/", "http://x.ads.com/", "http://ads.com./", "http://ADS.COM/", "http://x.tracker.io./", "http://user@ads.com:80/"]:
    try:
        p.check(u)
        print(f"  {u:28} allowed")
    except Exception as e:  # noqa: BLE001
        print(f"  {u:28} blocked")

print("\n== 5. register() idempotency keyed on id(kernel) ==")
hits = 0
for i in range(20):
    ka = mk(100 + i)
    register(ka, browser_backend="chromium")
    ida = id(ka)
    ka.shutdown()
    del ka
    gc.collect()
    kb = mk(200 + i)
    same = id(kb) == ida
    register(kb, browser_backend="chromium")
    n = len(web_tools(kb))
    if same:
        hits += 1
        if hits == 1:
            print(f"  iteration {i}: new kernel reused id {ida:#x}; register() returned normally; browser tools on new kernel: {n}")
    kb.shutdown()
    del kb
print(f"  id reuse happened in {hits}/20 iterations")

print("\n== 6. SIGTERM after register(browser_backend='chromium') ==")
code = textwrap.dedent(f"""
    import os, signal, sys, time
    from pathlib import Path
    from symbiote.adapters.llm.base import MockLLMAdapter
    from symbiote.browser import register
    from symbiote.config.models import KernelConfig
    from symbiote.core.kernel import SymbioteKernel
    k = SymbioteKernel(config=KernelConfig(db_path=Path({str(HERE)!r}) / "bsig.db"), llm=MockLLMAdapter())
    if sys.argv[1] == "with":
        register(k, browser_backend="chromium")
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(0.5)
    print("still alive 0.5s after SIGTERM")
""")
for mode in ("without", "with"):
    r = subprocess.run([sys.executable, "-c", code, mode], capture_output=True, text=True, cwd=str(HERE), timeout=60)
    print(f"  {mode:7} register: returncode={r.returncode} stdout={r.stdout.strip()!r}")
k.shutdown()
