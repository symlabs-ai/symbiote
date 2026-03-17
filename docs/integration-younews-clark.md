# Integração YouNews → Symbiote: Clark

> Guia prático para migrar o Clark do modelo ad-hoc para o kernel Symbiote.

## O que é o Symbiote

Symbiote é um **kernel para criar e gerenciar entidades cognitivas persistentes** — chamadas Symbiotas. Pense nele como o runtime que dá vida a assistentes de IA que:

- **Lembram** — mantêm memória de longo prazo entre sessões, aprendem preferências, acumulam contexto
- **Agem** — executam ações no ambiente (APIs, banco, serviços) com autorização controlada e auditoria completa
- **Persistem** — cada Symbiota tem identidade, persona, e histórico que sobrevivem a reinicializações
- **Se integram** — embarcam em qualquer app Python como biblioteca, sem dependências pesadas

### Por que usar o Symbiote no Clark?

O Clark atual funciona, mas toda a lógica está concentrada em um único arquivo (`routes/clark.py`, 471 linhas) que mistura:

- Montagem manual de system prompt
- Regex ad-hoc para extrair ações (````action```) e sugestões (`:::suggestions:::`)
- Execução de ações via httpx hardcoded
- Persistência de histórico em tabela custom
- Lógica de compose mode acoplada

Com o Symbiote, o Clark ganha:

| Antes (ad-hoc) | Depois (Symbiote) |
|---|---|
| Prompt montado manualmente, 40+ linhas de string | Persona + contexto + tools montados pelo ContextAssembler com budget de tokens |
| Regex para extrair `action` blocks | Parser builtin que extrai `tool_call` blocks e executa automaticamente |
| httpx hardcoded para cada endpoint | Tools HTTP declarativas registradas com JSON Schema |
| Sem auditoria de ações | PolicyGate deny-by-default + audit log completo |
| Tabela `yn_clark_conversations` custom | Sessions do kernel com memória de longo prazo |
| Nenhuma memória entre conversas diferentes | Memory Stack de 4 camadas — Clark aprende sobre o usuário |
| Compose mode como if/else no prompt | Extra context injection — o host injeta o que quiser |

---

## Arquitetura da Integração

```
┌──────────────────────────────────────────────────┐
│  Frontend (clark.html — sem mudanças)            │
│  FAB → painel lateral → POST /clark/chat         │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│  routes/clark.py (simplificado)                  │
│  1. Autenticação (sem mudança)                   │
│  2. get_or_create_session (external_key)         │
│  3. kernel.message(session, msg, extra_context)  │
│  4. Extrair :::suggestions::: (lógica YouNews)   │
│  5. Retornar response                            │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│  SymbioteKernel (biblioteca embarcada)           │
│  - Persona do Clark                              │
│  - Context assembly (persona + tools + memórias) │
│  - LLM call                                      │
│  - Tool call parser → PolicyGate → execução      │
│  - Audit log                                     │
│  - Session + message persistence                 │
└──────────────────────────────────────────────────┘
```

---

## Passo a Passo

### 1. Instalar o Symbiote

```bash
# No pyproject.toml do YouNews, adicionar:
symbiote = { path = "../symbiote", develop = true }

# Ou via pip:
pip install -e /path/to/symbiote
```

### 2. Inicializar o Kernel (startup do app)

**Onde:** `younews/adapters/inbound/fastapi/app.py` → dentro do `lifespan()`

```python
from symbiote.core.kernel import SymbioteKernel
from symbiote.config.models import KernelConfig
from symbiote.adapters.llm.forge import ForgeLLMAdapter
from symbiote.environment.descriptors import ToolDescriptor, HttpToolConfig

async def lifespan(app):
    # ... setup existente ...

    # ── Symbiote: inicializar kernel do Clark ──
    kernel = SymbioteKernel(
        config=KernelConfig(db_path="data/clark.db"),
        llm=ForgeLLMAdapter(provider="anthropic"),
    )

    # Criar o Clark (idempotente — verificar se já existe)
    clark = kernel.create_symbiote(
        name="Clark",
        role="younews_assistant",
        persona={
            "name": "Clark",
            "tone": "conciso, útil e proativo",
            "language": "português brasileiro",
            "expertise": "jornalista digital, curadoria de conteúdo",
            "constraints": [
                "nunca deletar sem confirmação explícita",
                "nunca executar ações destrutivas em lote sem confirmação",
                "sempre usar ações para buscar dados reais",
                "responder em markdown quando útil",
            ],
        },
    )

    # Registrar tools (ações do YouNews)
    _register_clark_tools(kernel, clark.id, settings.API_PORT)

    # Disponibilizar no app state
    app.state.clark_kernel = kernel
    app.state.clark_id = clark.id

    yield

    kernel.shutdown()
```

### 3. Registrar as Tools do Clark

```python
def _register_clark_tools(kernel: SymbioteKernel, clark_id: str, api_port: int):
    """Registrar todas as ações do YouNews como tools declarativas."""
    base = f"http://127.0.0.1:{api_port}"

    tools = [
        # ── Itens ──
        ("yn_list_items", "List Items", "List user items by journal and status",
         {"type": "object", "properties": {
             "journal_id": {"type": "string"}, "status": {"type": "string"}, "limit": {"type": "integer"}
         }},
         "GET", f"{base}/items/?journal_id={{journal_id}}&status={{status}}&limit={{limit}}"),

        ("yn_publish_item", "Publish Item", "Publish an item from inbox",
         {"type": "object", "properties": {"item_id": {"type": "string"}}, "required": ["item_id"]},
         "POST", f"{base}/items/{{item_id}}/publish"),

        ("yn_archive_item", "Archive Item", "Archive an item",
         {"type": "object", "properties": {"item_id": {"type": "string"}}, "required": ["item_id"]},
         "POST", f"{base}/items/{{item_id}}/archive"),

        ("yn_delete_item", "Delete Item", "Delete an item (requires explicit user request)",
         {"type": "object", "properties": {"item_id": {"type": "string"}}, "required": ["item_id"]},
         "DELETE", f"{base}/items/{{item_id}}"),

        ("yn_highlight_item", "Highlight Item", "Toggle favorite on an item",
         {"type": "object", "properties": {"item_id": {"type": "string"}}, "required": ["item_id"]},
         "POST", f"{base}/items/{{item_id}}/highlight"),

        ("yn_capture_url", "Capture URL", "Capture a URL into a journal",
         {"type": "object", "properties": {"url": {"type": "string"}, "journal_id": {"type": "string"}}, "required": ["url", "journal_id"]},
         "POST", f"{base}/items/capture"),

        # ── Journals ──
        ("yn_list_journals", "List Journals", "List all user journals",
         {}, "GET", f"{base}/journals/"),

        ("yn_get_journal", "Get Journal", "Get journal details",
         {"type": "object", "properties": {"journal_id": {"type": "string"}}, "required": ["journal_id"]},
         "GET", f"{base}/journals/{{journal_id}}"),

        ("yn_journal_tags", "Journal Tags", "Get tags of a journal",
         {"type": "object", "properties": {"journal_id": {"type": "string"}}, "required": ["journal_id"]},
         "GET", f"{base}/journals/{{journal_id}}/tags"),

        # ── Busca ──
        ("yn_search", "Search", "Semantic + textual search across user items",
         {"type": "object", "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["q"]},
         "GET", f"{base}/search?q={{q}}&limit={{limit}}"),

        # ── Compose ──
        ("yn_search_items_for_source", "Search Items for Source", "Search items to use as article sources",
         {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
         "GET", f"{base}/compose/search-items?q={{q}}"),

        # ── Inbox ──
        ("yn_bulk_action", "Bulk Action", "Execute bulk action on inbox items",
         {"type": "object", "properties": {
             "item_ids": {"type": "array", "items": {"type": "string"}},
             "action": {"type": "string", "enum": ["publish", "archive"]},
         }, "required": ["item_ids", "action"]},
         "POST", f"{base}/inbox/bulk-action"),

        # ── Analytics ──
        ("yn_analytics_overview", "Analytics Overview", "Get site analytics summary",
         {"type": "object", "properties": {"days": {"type": "integer"}}},
         "GET", f"{base}/analytics/overview?days={{days}}"),

        ("yn_analytics_top_pages", "Top Pages", "Most visited pages",
         {"type": "object", "properties": {"days": {"type": "integer"}, "limit": {"type": "integer"}}},
         "GET", f"{base}/analytics/top-pages?days={{days}}&limit={{limit}}"),
    ]

    tool_ids = []
    for tool_id, name, desc, params, method, url in tools:
        kernel.tool_gateway.register_http_tool(
            ToolDescriptor(tool_id=tool_id, name=name, description=desc, parameters=params),
            HttpToolConfig(method=method, url_template=url),
        )
        tool_ids.append(tool_id)

    # Autorizar todas as tools para o Clark
    kernel.environment.configure(symbiote_id=clark_id, tools=tool_ids)
```

### 4. Simplificar o routes/clark.py

O `clark.py` atual tem 471 linhas. Com o Symbiote, fica assim:

```python
"""Clark AI assistant routes — powered by Symbiote kernel."""

import re
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from younews.adapters.inbound.fastapi.dependencies import get_uow
from younews.adapters.inbound.fastapi.middleware.auth import get_current_user_optional
from younews.adapters.outbound.database import DatabaseUnitOfWork
from younews.domain.entities.user import User

router = APIRouter(prefix="/clark", tags=["clark"])
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str
    url_key: str
    page_context: str = ""
    source_item_ids: list[str] | None = None
    current_content: str = ""


class ChatResponse(BaseModel):
    response: str
    suggestions: list[str] | None = None
    tool_results: list[dict] | None = None


# ── Helpers ──

def _extract_suggestions(text: str) -> list[str]:
    """Extract :::suggestions::: blocks (YouNews-specific format)."""
    match = re.search(r":::suggestions\s*\n(.*?)\n:::", text, re.DOTALL)
    if not match:
        return []
    lines = match.group(1).strip().splitlines()
    return [line.lstrip("- ").strip() for line in lines if line.strip()]


def _clean_suggestions(text: str) -> str:
    """Remove suggestion blocks from visible text."""
    return re.sub(r":::suggestions\s*\n.*?\n:::", "", text, flags=re.DOTALL).strip()


# ── Routes ──

@router.post("/chat", response_model=ChatResponse)
async def clark_chat(
    body: ChatRequest,
    request: Request,
    current_user: User | None = Depends(get_current_user_optional),
):
    """Chat with Clark AI assistant."""
    if not current_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    kernel = request.app.state.clark_kernel
    clark_id = request.app.state.clark_id

    # 1. Session por (user, page) — automatico
    session = kernel.get_or_create_session(
        symbiote_id=clark_id,
        external_key=f"{current_user.id}:{body.url_key}",
        goal=f"Assist on {body.url_key}",
    )

    # 2. Montar extra context (page + compose mode)
    extra_context = {
        "page_url": body.url_key,
        "page_content": body.page_context[:8000] if body.page_context else "",
    }
    if body.url_key.startswith("/compose"):
        extra_context["mode"] = "compose"
        if body.current_content:
            extra_context["draft_content"] = body.current_content[:4000]
        # Nota: fontes podem ser carregadas aqui e adicionadas ao extra_context

    # 3. Enviar para o kernel — ele cuida de tudo
    try:
        response = kernel.message(
            session.id,
            body.message,
            extra_context=extra_context,
        )
    except Exception as e:
        logger.error(f"Clark error: {e}", exc_info=True)
        response = "Desculpe, houve um erro ao processar sua mensagem."

    # 4. Processar response
    tool_results = None
    if isinstance(response, dict):
        text = response.get("text", "")
        tool_results = response.get("tool_results")
    else:
        text = response

    # 5. Extrair suggestions (formato YouNews)
    suggestions = _extract_suggestions(text)
    clean_text = _clean_suggestions(text)

    return ChatResponse(
        response=clean_text,
        suggestions=suggestions or None,
        tool_results=tool_results,
    )


@router.get("/history")
async def clark_history(
    url_key: str,
    request: Request,
    current_user: User | None = Depends(get_current_user_optional),
):
    """Load conversation history for a URL."""
    if not current_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    kernel = request.app.state.clark_kernel
    clark_id = request.app.state.clark_id

    session = kernel._sessions.find_by_external_key(
        f"{current_user.id}:{url_key}"
    )
    if session is None:
        return {"messages": [], "url_key": url_key}

    messages = kernel._sessions.get_messages(session.id)
    return {
        "messages": [
            {"role": m.role, "content": m.content}
            for m in reversed(messages)
        ],
        "url_key": url_key,
    }


@router.post("/clear")
async def clark_clear(
    request: Request,
    url_key: str = "",
    current_user: User | None = Depends(get_current_user_optional),
):
    """Clear conversation for a URL."""
    if not current_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    kernel = request.app.state.clark_kernel
    clark_id = request.app.state.clark_id

    session = kernel._sessions.find_by_external_key(
        f"{current_user.id}:{url_key}"
    )
    if session:
        kernel.close_session(session.id)

    return JSONResponse({"cleared": True, "url_key": url_key})
```

**De 471 linhas para ~130 linhas.** Sem:
- Montagem manual de prompt (kernel faz)
- Regex de `action` blocks (parser builtin)
- httpx hardcoded (HTTP tool handler)
- Tabela custom de conversas (sessions do kernel)

### 5. Frontend (clark.html)

**Nenhuma mudança necessária.** O contrato da API (`POST /clark/chat`) é o mesmo:
- Request: `message`, `url_key`, `page_context`, `source_item_ids`, `current_content`
- Response: `response` (text), `suggestions` (list), `tool_results` (novo campo, ignorado se não usado)

A única diferença é que `actions_executed` vira `tool_results` no response. Ajuste trivial no JS se quiser exibir resultados de tools.

### 6. Migração de Dados

A tabela `yn_clark_conversations` continua existindo — o Symbiote usa seu próprio SQLite (`data/clark.db`). As conversas antigas ficam na tabela old, novas vão para o kernel.

Opções:
- **A) Corte limpo** — novas conversas no Symbiote, antigas morrem naturalmente
- **B) Migração** — script que lê `yn_clark_conversations` e cria sessions/messages no kernel

Recomendo **A** — mais simples, sem risco.

---

## Formato tool_call vs action

O Clark atual usa:
```
\`\`\`action
{"method": "POST", "path": "/items/123/publish"}
\`\`\`
```

O Symbiote usa:
```
\`\`\`tool_call
{"tool": "yn_publish_item", "params": {"item_id": "123"}}
\`\`\`
```

A diferença é que no Symbiote o LLM chama tools **pelo nome** (semântico) em vez de construir chamadas HTTP (técnico). O kernel resolve a URL internamente.

A persona do Clark no Symbiote já recebe as tool descriptions no system prompt automaticamente — o LLM sabe quais tools existem e como chamá-las.

---

## Checklist de Migração

- [ ] Adicionar `symbiote` como dependência do YouNews
- [ ] Inicializar kernel no `lifespan()` do app
- [ ] Registrar tools com `_register_clark_tools()`
- [ ] Substituir `routes/clark.py` pela versão simplificada
- [ ] Ajustar `clark.html` para `tool_results` (opcional)
- [ ] Testar: chat simples, tool execution, compose mode, history, clear
- [ ] Remover `ClarkConversationModel` e migration (quando confortável)
- [ ] Remover dependência de `forge_llm_adapter` no Clark (kernel usa seu próprio)
