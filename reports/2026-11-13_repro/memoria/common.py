import glob, os, shutil, logging
from pathlib import Path
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.adapters.llm.base import MockLLMAdapter
S = Path(__file__).parent

def fresh(name, llm=None, **cfg):
    d = S / "run" / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    k = SymbioteKernel(KernelConfig(db_path=d / "t.db", **cfg), llm=llm if llm is not None else MockLLMAdapter("ok"))
    return k, d
