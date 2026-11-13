import os, sys, tempfile, uuid, logging
from pathlib import Path
SCRATCH = Path(__file__).parent
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import ToolDescriptor

def mk_kernel(llm, **cfg):
    db = SCRATCH / f"db_{uuid.uuid4().hex[:8]}.sqlite"
    k = SymbioteKernel(KernelConfig(db_path=db, skills_root=SCRATCH / f"skills_{uuid.uuid4().hex[:6]}", **cfg), llm=llm)
    return k

def add_tool(k, sym_id, tool_id, handler, params=None, **kw):
    d = ToolDescriptor(tool_id=tool_id, name=tool_id, description=f"tool {tool_id}",
                       parameters=params or {"type": "object", "properties": {}}, handler_type="builtin", **kw)
    k.tool_gateway.register_descriptor(d, handler)

class FakeLLM:
    """Scripted LLM: fn(call_index, messages, config, tools) -> str"""
    def __init__(self, fn):
        self.fn = fn; self.calls = []
    def complete(self, messages, config=None, tools=None):
        import copy
        self.calls.append({"messages": copy.deepcopy(messages), "config": config, "tools": tools})
        return self.fn(len(self.calls) - 1, messages, config, tools)

def tc(tool, **params):
    import json
    return '```tool_call\n' + json.dumps({"tool": tool, "params": params}) + '\n```'
