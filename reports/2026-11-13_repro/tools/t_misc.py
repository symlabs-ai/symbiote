"""Misc probes: tool-call parser, bash orphan, configure lost update, CLI config writer, from_yaml."""
import os
import sys
import threading
import time
from pathlib import Path

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.parser import parse_tool_calls

HERE = Path(sys.argv[1])
for f in HERE.glob("m.db*"):
    f.unlink()

print("== 1. parse_tool_calls on malformed-but-valid-JSON blocks ==")
for txt in ['```tool_call\n[{"tool": "a", "params": {}}]\n```', '```tool_call\n{"tool": "a", "params": "x"}\n```',
            '```tool_call\n{"tool": "a", "params": null}\n```', '```tool_call\n{"tool": 5}\n```', '```tool_call\n"just a string"\n```']:
    try:
        print(f"  {txt.splitlines()[1]:38} ->", parse_tool_calls(txt)[1])
    except Exception as e:  # noqa: BLE001
        print(f"  {txt.splitlines()[1]:38} -> RAISED {type(e).__name__}: {str(e).splitlines()[0][:60]}")

kernel = SymbioteKernel(config=KernelConfig(db_path=HERE / "m.db"), llm=MockLLMAdapter())
gw = kernel.tool_gateway
a = kernel.create_symbiote(name="A", role="assistant")
kernel.environment.configure(symbiote_id=a.id, tools=["bash"])

print("\n== 2. kernel.message() when the LLM emits such a block ==")
for resp in ['```tool_call\n[{"tool": "bash", "params": {}}]\n```', '```tool_call\n{"tool": "bash", "params": "ls"}\n```']:
    kernel._llm._responses = [resp, "done"]
    kernel._llm._response_index = 0
    s = kernel.start_session(a.id)
    try:
        print("  ->", repr(kernel.message(s.id, "go"))[:90])
    except Exception as e:  # noqa: BLE001
        print(f"  -> RAISED {type(e).__name__}: {str(e).splitlines()[0][:80]}")

print("\n== 3. bash timeout leaves the child running ==")
marker = HERE / "orphan.txt"
marker.unlink(missing_ok=True)
cmd = f"{sys.executable} -c \"import time; time.sleep(2); open(r'{marker}', 'w').write('child survived')\"; echo end"
t0 = time.perf_counter()
r = gw.execute(a.id, None, "bash", {"command": cmd, "timeout": 1})
print(f"  returned in {time.perf_counter() - t0:.2f}s:", r.output)
time.sleep(2.0)
print("  2s later, file written by the 'timed out' command:", marker.read_text() if marker.exists() else None)

print("\n== 4. configure(): concurrent read-modify-write on the tools list ==")
b = kernel.create_symbiote(name="B", role="assistant")
env = kernel.environment
N = 16
barrier = threading.Barrier(N)


def add(i):
    barrier.wait()
    env.configure(symbiote_id=b.id, tools=env.list_tools(b.id) + [f"tool_{i}"])   # same pattern as cli tools add / POST /symbiotes/{id}/tools


ths = [threading.Thread(target=add, args=(i,)) for i in range(N)]
[t.start() for t in ths]
[t.join() for t in ths]
rows = kernel._storage.fetch_all("SELECT id, tools_json FROM environment_configs WHERE symbiote_id=?", (b.id,))
print(f"  {N} concurrent authorisations -> rows for this symbiote: {len(rows)}; tools visible via list_tools: {len(env.list_tools(b.id))}/{N}")

print("\n== 5. CLI .symbiote/config writer ==")
from symbiote.cli.main import _read_symbiote_config, _write_symbiote_config

cfgdir = HERE / "proj"
cfgdir.mkdir(exist_ok=True)
old = os.umask(0o022)
_write_symbiote_config({"server": "http://x", "api_key": "sk-symbiote_SECRET", "id": "abc", "name": "bot"}, path=cfgdir)
os.umask(old)
print("  mode of .symbiote/config holding the API key:", oct((cfgdir / ".symbiote/config").stat().st_mode & 0o777))
for name in ["100% bot", "sk-symbiote_a%b"]:
    try:
        _write_symbiote_config({"server": "http://x", "api_key": name, "id": "abc", "name": name}, path=cfgdir)
        print(f"  write value {name!r} -> ok; read back:", _read_symbiote_config(cfgdir).get("name"))
    except Exception as e:  # noqa: BLE001
        print(f"  write value {name!r} -> RAISED {type(e).__name__}: {str(e)[:70]}")

print("\n== 6. KernelConfig.from_yaml ==")
y = HERE / "cfg.yaml"
for content in ["contex_budget: 9000\nlog_level: debug\n", "- a\n- b\n", "context_budget: 0\n"]:
    y.write_text(content)
    try:
        c = KernelConfig.from_yaml(y)
        print(f"  {content!r} -> context_budget={c.context_budget} log_level={c.log_level}")
    except Exception as e:  # noqa: BLE001
        print(f"  {content!r} -> {type(e).__name__}: {str(e).splitlines()[0][:70]}")
kernel.shutdown()
