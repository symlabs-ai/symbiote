"""Standalone test harness — FastAPI server with <symbiote-chat> + Kimi K2 tool loop.

Serves a web page with the symbiote-chat web component connected to a real
SymbioteKernel using Kimi K2 and discovered tools from symbiote.db.
Tools point to YouNews at localhost:8000.

Usage:
    python -m tests.e2e.test_harness.server

Requires:
    - YouNews running at localhost:8000
    - SYMGATEWAY_API_KEY and SYMGATEWAY_BASE_URL env vars
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root before any adapter reads env vars
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from symbiote.adapters.llm.forge import ForgeLLMAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.runners.chat import ChatRunner

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_SYMBIOTE_CHAT_JS = _PROJECT_ROOT / "symbiote-ui" / "dist" / "symbiote-chat.js"
_DB_PATH = _PROJECT_ROOT / ".symbiote" / "symbiote.db"

_YOUNEWS_BASE_URL = os.environ.get("YOUNEWS_BASE_URL", "http://127.0.0.1:8000")
_PROVIDER = os.environ.get("SYMBIOTE_LLM_PROVIDER", "symgateway")
_MODEL = os.environ.get("SYMBIOTE_LLM_MODEL", "moonshotai/kimi-k2-instruct")
_PORT = int(os.environ.get("HARNESS_PORT", "8058"))

_TOOL_TAGS = ["Items", "Inbox", "Journals", "Search", "Compose", "View", "Capture"]

_YOUNEWS_CONTEXT = """\
# YouNews — Contexto para Clark

- **Journal**: coleção temática de itens (links/artigos) com URL pública
- **Item**: link/artigo salvo com título, URL, descrição, status (inbox/published/archived), tags
- **Inbox**: itens recém-capturados aguardando triagem
- **Sources**: feeds RSS/Atom importando itens automaticamente
- **Compose**: editor de artigos originais usando itens salvos como referência
- **Semantic search**: busca por significado no texto dos itens
- **Analytics**: métricas de visitantes dos jornais públicos
- **Newsletter**: envio periódico por email para assinantes

Você é Clark, o assistente do YouNews. Responda sempre em português brasileiro.

## Regras de resposta
- NUNCA exponha IDs técnicos (UUIDs, journal_id, item_id) ao usuário
- Use apenas nomes legíveis (nome do jornal, título do item, URL)
- Responda de forma natural e concisa, como num bate-papo
- Não narre o que vai fazer ("Vou verificar...") — apenas faça e reporte o resultado
"""


class ChatRequest(BaseModel):
    message: str
    session_key: str = "test-harness"


def _format_sse(event: str, data: dict) -> str:
    return f"event:{event}\ndata:{json.dumps(data, ensure_ascii=False)}\n\n"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init LLM
    llm = ForgeLLMAdapter(provider=_PROVIDER, model=_MODEL)

    # Init kernel with existing DB (contains discovered tools)
    assert _DB_PATH.exists(), f"symbiote.db not found at {_DB_PATH}"
    config = KernelConfig(db_path=_DB_PATH, context_budget=16000)
    kernel = SymbioteKernel(config=config, llm=llm)

    # Find the Clark that actually has approved tools
    rows = kernel._storage.fetch_all(
        """SELECT s.id
           FROM symbiotes s
           JOIN discovered_tools dt ON dt.symbiote_id = s.id AND dt.status = 'approved'
           WHERE s.name = 'Clark' AND s.status = 'active'
           GROUP BY s.id
           ORDER BY COUNT(dt.id) DESC
           LIMIT 1""",
    )
    if rows:
        clark_id = rows[0]["id"]
    else:
        sym = kernel.create_symbiote(
            name="Clark",
            role="younews_assistant",
            persona={"tone": "friendly", "language": "pt-BR"},
        )
        clark_id = sym.id

    # Load discovered tools
    tool_ids = kernel.load_discovered_tools(clark_id, base_url=_YOUNEWS_BASE_URL)
    print(f"[harness] Loaded {len(tool_ids)} tools from symbiote.db")

    # Configure tool visibility
    kernel.configure_tool_visibility(clark_id, tags=_TOOL_TAGS, loading="index", loop=True)

    # Replace ChatRunner with text-based tool calling
    # (ForgeLLMAdapter doesn't forward native tools to the provider)
    kernel._runner_registry._runners = [
        r for r in kernel._runner_registry._runners if r.runner_type != "chat"
    ]
    kernel._runner_registry.register(
        ChatRunner(llm, tool_gateway=kernel._tool_gateway, native_tools=False)
    )

    # Register domain knowledge
    kernel._knowledge.register_source(
        symbiote_id=clark_id,
        name="younews_domain",
        content=_YOUNEWS_CONTEXT,
    )

    app.state.kernel = kernel
    app.state.clark_id = clark_id

    print(f"[harness] Clark ready (id={clark_id[:8]}...) with {len(tool_ids)} tools")
    print(f"[harness] Tools target: {_YOUNEWS_BASE_URL}")
    print(f"[harness] Server: http://localhost:{_PORT}")

    yield

    kernel.shutdown()


app = FastAPI(title="Symbiote Test Harness", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = _STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/static/symbiote-chat.js")
async def serve_chat_js():
    return FileResponse(
        _SYMBIOTE_CHAT_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    kernel: SymbioteKernel = app.state.kernel
    clark_id: str = app.state.clark_id

    session = kernel.get_or_create_session(
        symbiote_id=clark_id,
        external_key=f"harness:{body.session_key}",
        goal="test harness session",
    )

    queue: asyncio.Queue = asyncio.Queue()

    async def _run():
        try:
            def on_token(text: str):
                queue.put_nowait(("text_delta", {"text": text}))

            response = await kernel.message_async(
                session_id=session.id,
                content=body.message,
                on_token=on_token,
            )

            # Emit response_done
            if isinstance(response, dict):
                text = response.get("text", "")
                tool_results = response.get("tool_results", [])
            else:
                text = str(response)
                tool_results = []

            queue.put_nowait(("response_done", {
                "text": text,
                "tool_results": tool_results,
            }))
        except Exception as e:
            queue.put_nowait(("error", {"message": str(e)}))
        finally:
            queue.put_nowait(None)  # sentinel

    asyncio.create_task(_run())

    async def event_stream():
        while True:
            event = await queue.get()
            if event is None:
                break
            event_type, data = event
            yield _format_sse(event_type, data)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/history")
async def load_history(session_key: str = "test-harness"):
    kernel: SymbioteKernel = app.state.kernel
    clark_id: str = app.state.clark_id

    session = kernel.get_or_create_session(
        symbiote_id=clark_id,
        external_key=f"harness:{session_key}",
    )

    rows = kernel._storage.fetch_all(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY created_at",
        (session.id,),
    )

    messages = [{"role": r["role"], "content": r["content"]} for r in rows]
    return {"messages": messages}


@app.post("/clear")
async def clear_history(session_key: str = "test-harness"):
    kernel: SymbioteKernel = app.state.kernel
    clark_id: str = app.state.clark_id

    session = kernel.get_or_create_session(
        symbiote_id=clark_id,
        external_key=f"harness:{session_key}",
    )

    kernel._storage.execute(
        "DELETE FROM messages WHERE session_id = ?", (session.id,)
    )
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "tests.e2e.test_harness.server:app",
        host="0.0.0.0",
        port=_PORT,
        reload=False,
    )
