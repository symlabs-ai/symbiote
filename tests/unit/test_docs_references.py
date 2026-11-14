"""The guides only reference things that exist: every ``symbiote`` /
``forge_llm`` import in their Python blocks resolves, and every method used on
``kernel``, ``kernel.tool_gateway`` and ``kernel.environment`` is real. The
blocks are not executed (report item D)."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from tests.support import ScriptedLLM, make_kernel

ROOT = Path(__file__).resolve().parents[2]
DOCS = ["README.md", "QUICKSTART.md", "docs/HOST_INTEGRATION.md"]
_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)
_FROM = re.compile(r"^\s*from\s+((?:symbiote|forge_llm)[\w.]*)\s+import\s+\(?([^)\n]+)", re.M)
_IMPORT = re.compile(r"^\s*import\s+((?:symbiote|forge_llm)[\w.]*)", re.M)
_KERNEL_ATTR = re.compile(r"\bkernel\.(\w+)(?:\.(\w+))?")


def _blocks() -> list[tuple[str, str]]:
    out = []
    for doc in DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        out.extend((doc, block) for block in _BLOCK.findall(text))
    return out


def _imports() -> list[tuple[str, str, str]]:
    found = set()
    for doc, block in _blocks():
        for module, names in _FROM.findall(block):
            for name in names.split(","):
                name = name.split(" as ")[0].strip().strip("()")
                if name and name != "*":
                    found.add((doc, module, name))
        for module in _IMPORT.findall(block):
            found.add((doc, module, ""))
    return sorted(found)


def _optional_dependency_missing(module: str) -> bool:
    top = {"forge_llm": "forge_llm"}.get(module.split(".")[0])
    return top is not None and importlib.util.find_spec(top) is None


@pytest.mark.parametrize(("doc", "module", "name"), _imports())
def test_doc_imports_resolve(doc: str, module: str, name: str) -> None:
    if _optional_dependency_missing(module):
        pytest.skip(f"{module} not installed")
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        # A module of ours that needs an optional extra (``symbiote[postgres]``).
        if "symbiote[" in str(exc):
            pytest.skip(str(exc))
        raise
    if name and not hasattr(mod, name):
        importlib.import_module(f"{module}.{name}")  # a submodule is fine too


def _kernel_references() -> list[tuple[str, str, str | None]]:
    refs = set()
    for doc, block in _blocks():
        for attr, sub in _KERNEL_ATTR.findall(block):
            refs.add((doc, attr, sub if attr in ("tool_gateway", "environment") and sub else None))
    return sorted(refs, key=lambda r: (r[0], r[1], r[2] or ""))


@pytest.fixture(scope="module")
def kernel(tmp_path_factory):
    k = make_kernel(tmp_path_factory.mktemp("docs"), ScriptedLLM("ok"))
    yield k
    k.shutdown()


@pytest.mark.parametrize(("doc", "attr", "sub"), _kernel_references())
def test_doc_kernel_references_exist(kernel, doc: str, attr: str, sub: str | None) -> None:
    assert hasattr(kernel, attr), f"{doc}: kernel.{attr} does not exist"
    if sub:
        target = getattr(kernel, attr)
        assert hasattr(target, sub), f"{doc}: kernel.{attr}.{sub} does not exist"
