#!/usr/bin/env python3
"""Compare LLM responses across the 3 tool loading modes (full, index, semantic).

Sends the same query to a real LLM via SymGateway and prints each response
so we can evaluate agentic behavior (does the LLM ACT or just EXPLAIN?).

Usage:
    python scripts/compare_modes_llm.py

Requires .env with SYMGATEWAY_API_KEY and SYMGATEWAY_BASE_URL.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Load .env
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from symbiote.adapters.llm.forge import ForgeLLMAdapter  # noqa: E402
from symbiote.adapters.storage.sqlite import SQLiteAdapter  # noqa: E402
from symbiote.core.context import ContextAssembler  # noqa: E402
from symbiote.core.identity import IdentityManager  # noqa: E402
from symbiote.core.session import SessionManager  # noqa: E402
from symbiote.environment.descriptors import ToolDescriptor  # noqa: E402
from symbiote.environment.manager import EnvironmentManager  # noqa: E402
from symbiote.environment.policies import PolicyGate  # noqa: E402
from symbiote.environment.tools import ToolGateway  # noqa: E402
from symbiote.knowledge.service import KnowledgeService  # noqa: E402
from symbiote.memory.store import MemoryStore  # noqa: E402
from symbiote.runners.chat import ChatRunner  # noqa: E402

# ── YouNews-like tool set ─────────────────────────────────────────────

TOOLS = [
    ToolDescriptor(
        tool_id="items_list", name="Listar Itens",
        description="Lista todos os itens de notícia com filtros",
        parameters={
            "type": "object",
            "properties": {
                "journal_id": {"type": "integer", "description": "ID do jornal"},
                "status": {"type": "string", "enum": ["draft", "published", "archived"], "description": "Filtro de status"},
                "limit": {"type": "integer", "description": "Máximo de resultados"},
            },
        },
        tags=["Items"], handler_type="http",
    ),
    ToolDescriptor(
        tool_id="items_get", name="Obter Item",
        description="Retorna detalhes completos de um item de notícia",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "ID do item"},
            },
            "required": ["item_id"],
        },
        tags=["Items"], handler_type="http",
    ),
    ToolDescriptor(
        tool_id="items_publish", name="Publicar Item",
        description="Publica um item de notícia no jornal",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "ID do item a publicar"},
            },
            "required": ["item_id"],
        },
        tags=["Items"], handler_type="http",
    ),
    ToolDescriptor(
        tool_id="items_update", name="Atualizar Item",
        description="Atualiza campos de um item de notícia existente",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "ID do item"},
                "title": {"type": "string", "description": "Novo título"},
                "body": {"type": "string", "description": "Novo corpo da matéria"},
                "status": {"type": "string", "description": "Novo status"},
            },
            "required": ["item_id"],
        },
        tags=["Items"], handler_type="http",
    ),
    ToolDescriptor(
        tool_id="compose_draft", name="Criar Rascunho",
        description="Cria um novo rascunho de matéria",
        parameters={
            "type": "object",
            "properties": {
                "journal_id": {"type": "integer", "description": "Jornal de destino"},
                "title": {"type": "string", "description": "Título da matéria"},
                "body": {"type": "string", "description": "Corpo da matéria"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags da matéria"},
            },
            "required": ["journal_id", "title"],
        },
        tags=["Compose"], handler_type="http",
    ),
    ToolDescriptor(
        tool_id="compose_suggest_title", name="Sugerir Título",
        description="Sugere títulos para uma matéria com base no conteúdo",
        parameters={
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "Corpo da matéria"},
                "count": {"type": "integer", "description": "Quantas sugestões"},
            },
            "required": ["body"],
        },
        tags=["Compose"], handler_type="http",
    ),
]

QUERY = "publique a matéria sobre o incêndio no jornal principal"


class ThinkingStripLLM:
    """Wrapper that strips <think>...</think> blocks and EOS tokens from LLM output."""

    def __init__(self, inner):
        self._inner = inner

    def complete(self, messages, **kwargs):
        import re
        # Inject /no_think for Qwen3 models
        if messages and messages[0]["role"] == "system":
            messages = list(messages)
            messages[0] = {**messages[0], "content": "/no_think\n" + messages[0]["content"]}
        resp = self._inner.complete(messages, **kwargs)
        if resp:
            resp = re.sub(r'<think>.*?</think>', '', resp, flags=re.DOTALL).strip()
            resp = resp.replace('<|im_end|>', '').replace('<|endoftext|>', '').strip()
        return resp

PERSONA = {
    "role": "Agente autônomo de redação do YouNews",
    "tone": "profissional e direto",
    "language": "pt-BR",
}

MODELS = [
    "grok-4-1-fast-reasoning",
    "moonshotai/kimi-k2-instruct",
    "gpt-5.4",
]
MODEL = MODELS[0]  # default for backward compat


def setup_infra(tmp_dir: Path):
    """Create adapter, identity, session, gateway, env."""
    db = tmp_dir / "compare.db"
    adapter = SQLiteAdapter(db_path=db)
    adapter.init_schema()

    identity = IdentityManager(storage=adapter)
    memory = MemoryStore(storage=adapter)
    knowledge = KnowledgeService(storage=adapter)
    env = EnvironmentManager(storage=adapter)
    gate = PolicyGate(env_manager=env, storage=adapter)
    gw = ToolGateway(policy_gate=gate)

    # Realistic mock handlers
    def items_list_handler(p):
        return [
            {"id": 42, "title": "Incêndio na zona sul deixa 3 desabrigados", "status": "draft", "journal_id": 1},
            {"id": 43, "title": "Chuvas previstas para o fim de semana", "status": "draft", "journal_id": 1},
            {"id": 44, "title": "Novo parque inaugurado no centro", "status": "published", "journal_id": 1},
        ]

    def items_get_handler(p):
        return {"id": p.get("item_id", 42), "title": "Incêndio na zona sul deixa 3 desabrigados", "status": "draft", "body": "Um incêndio..."}

    def items_publish_handler(p):
        return {"id": p.get("item_id", 42), "status": "published", "published_at": "2026-03-19T12:00:00"}

    handlers = {
        "items_list": items_list_handler,
        "items_get": items_get_handler,
        "items_publish": items_publish_handler,
        "items_update": lambda p: {"updated": True},
        "compose_draft": lambda p: {"id": 99, "status": "draft"},
        "compose_suggest_title": lambda p: ["Título 1", "Título 2"],
    }

    for tool in TOOLS:
        gw.register_descriptor(tool, handlers.get(tool.tool_id, lambda p: {"mock": True}))

    sym = identity.create(name="Clark", role="assistant", persona=PERSONA)
    session = SessionManager(storage=adapter).start(symbiote_id=sym.id, goal="compare")

    return adapter, identity, memory, knowledge, env, gw, sym, session


def run_mode(mode: str, identity, memory, knowledge, env, gw, sym, session, llm, semantic_llm=None, use_loop=False):
    """Build prompt for a mode and send to real LLM. Return response text."""
    tool_ids = [t.tool_id for t in TOOLS]
    env.configure(symbiote_id=sym.id, tools=tool_ids, tool_loading=mode, tool_tags=["Items", "Compose"], tool_loop=use_loop)

    assembler = ContextAssembler(
        identity=identity, memory=memory, knowledge=knowledge,
        context_budget=100_000, tool_gateway=gw, environment=env,
        semantic_llm=semantic_llm,
    )
    ctx = assembler.build(
        session_id=session.id, symbiote_id=sym.id, user_input=QUERY,
    )

    if use_loop:
        # Use full ChatRunner with tool loop
        runner = ChatRunner(llm=llm, tool_gateway=gw)
        t0 = time.time()
        result = runner.run(ctx)
        elapsed = time.time() - t0
        system_prompt = runner._build_system(ctx)

        if isinstance(result.output, dict):
            response = result.output["text"]
            tool_results = result.output.get("tool_results", [])
            response += "\n\n--- Tool Results ---"
            for tr in tool_results:
                status = "OK" if tr["success"] else "FAIL"
                response += f"\n  {tr['tool_id']}: {status} → {tr.get('output') or tr.get('error')}"
        else:
            response = str(result.output)

        return system_prompt, response, elapsed, len(ctx.available_tools)

    # Single-shot: call LLM directly
    runner = ChatRunner(llm=llm, tool_gateway=None)
    system_prompt = runner._build_system(ctx)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": QUERY},
    ]

    t0 = time.time()
    response = llm.complete(messages)
    elapsed = time.time() - t0

    return system_prompt, response, elapsed, len(ctx.available_tools)


def main():
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="symbiote_compare_"))
    adapter, identity, memory, knowledge, env, gw, sym, session = setup_infra(tmp)

    provider = os.environ.get("SYMBIOTE_LLM_PROVIDER", "symgateway")

    class SemanticLLM:
        def complete(self, messages, **kw):
            return json.dumps(["Items", "Compose"])

    sep = "=" * 80
    models = sys.argv[1:] if len(sys.argv) > 1 else MODELS

    print(f"\n{sep}")
    print("TOOL LOOP MULTI-MODEL TEST — semantic + brief mode")
    print(f"Query: \"{QUERY}\"")
    print(f"Models: {', '.join(models)}")
    print(sep)

    for model_id in models:
        print(f"\n{'━' * 80}")
        print(f"  MODEL: {model_id}")
        print(f"{'━' * 80}")

        try:
            # Detect local models
            mlx_models = {"qwen3-14b-mlx": "mlx-community/Qwen3-14B-4bit", "moonlight-mlx": "mlx-community/Moonlight-16B-A3B-Instruct-4-bit", "gemma3-12b-mlx": "mlx-community/gemma-3-12b-it-4bit", "phi4-mlx": "mlx-community/phi-4-4bit", "qwen3-8b-mlx": "mlx-community/Qwen3-8B-4bit", "qwen3-8b-8bit-mlx": "lmstudio-community/Qwen3-8B-MLX-8bit"}
            if model_id in mlx_models:
                model_provider = "openai"
                actual_model = mlx_models[model_id]
                os.environ["OPENAI_BASE_URL"] = "http://mlx.minimac.local:11434/v1"
                os.environ["OPENAI_API_KEY"] = "not-needed"
                raw_llm = ForgeLLMAdapter(provider=model_provider, model=actual_model, api_key="not-needed", base_url="http://mlx.minimac.local:11434/v1")
                llm = ThinkingStripLLM(raw_llm)
            elif ":" in model_id or model_id in ("phi4-mini", "phi4-mini:latest"):
                model_provider = "ollama"
                llm = ForgeLLMAdapter(provider=model_provider, model=model_id)
            else:
                model_provider = provider
                llm = ForgeLLMAdapter(provider=model_provider, model=model_id)
        except Exception as exc:
            print(f"  ERROR creating LLM: {exc}")
            continue

        try:
            prompt, response, elapsed, n_tools = run_mode(
                "semantic", identity, memory, knowledge, env, gw, sym, session, llm,
                semantic_llm=SemanticLLM(), use_loop=True,
            )

            # Count useful vs wasted iterations
            if isinstance(response, str) and "--- Tool Results ---" in response:
                total_calls = response.count(": OK") + response.count(": FAIL")
            else:
                total_calls = 0

            print(f"\n  Time: {elapsed:.1f}s | Tool calls: {total_calls}")
            print(f"  {'─' * 76}")
            print(response)

        except Exception as exc:
            print(f"  ERROR: {exc}")

    print(f"\n{sep}")
    adapter.close()


if __name__ == "__main__":
    main()
