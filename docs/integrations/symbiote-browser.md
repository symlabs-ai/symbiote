# symbiote.browser — Plano de integração

> Subpackage `symbiote.browser` que adiciona capacidades de **websearch** e **navegação em browsers** ao Symbiote, vivendo dentro do próprio repo, com deps em extras opcionais e ativação opt-in pelo host.

**Status:** Proposta — não iniciado
**Owner:** TBD
**Tracking:** TBD
**Última atualização:** 2026-05-26

---

## 1. Objetivo

Permitir que qualquer host do Symbiote (incluindo SymbiOS) ganhe capacidades de:

1. **Web search** com múltiplos backends (Exa / Tavily / Firecrawl / Parallel / xAI)
2. **Web extract / crawl** com compressão LLM dos resultados
3. **Navegação em browser** (snapshot via accessibility tree, click, fill, navigate)
4. **Backends de browser intercambiáveis**: Chromium local (headless), Browserbase, Browser Use
5. **Stealth opcional** (anti-bot fingerprinting) e supervisão de processo

Tudo isso entregue como **tools registradas no `ToolGateway`** do Symbiote — não como modificação do kernel.

## 2. Princípio arquitetural

**Subpackage no monorepo do Symbiote. Opt-in. Aditivo. Zero modificação do kernel.**

| Decisão | Por quê |
|---|---|
| Vive em `src/symbiote/browser/` (irmão de `dream/`, `harness/`, `discovery/`, `mcp/`) | Só Symbiote consome — não justifica repo/lib externa |
| Web search via **SymGateway proxy** (não SDK direto) | Credenciais centralizadas, billing centralizado, mesmo padrão dos outros projetos Symlabs. **Zero SDK extra** — só `httpx`. |
| Stack pesada apenas em extras (`[browser]`, `[stealth]`) | `pip install symbiote` permanece slim; `pip install "symbiote[browser]"` puxa Playwright. Search não precisa de extra. |
| **Nada de `symbiote.browser` é importado por default** | `import symbiote` não ativa Playwright nem tenta abrir Chromium |
| API de ativação: `from symbiote.browser import register; register(kernel, ...)` | Host opta em uma linha; sem essa linha, comportamento é exatamente o anterior |
| Zero alteração em `core/`, `runners/`, `environment/`, `security/` | Garante backward-compat total com clientes atuais (mesma regra da v0.5.0) |
| Versão segue o Symbiote (sem tag própria) | Patch por default — minor só com sua autorização |

## 3. Arquitetura

```
┌─────────────────────────────────────────────────────────────────────┐
│  Host (SymbiOS, app embedado, CLI, etc.)                            │
└─────────────────┬───────────────────────────────────────────────────┘
                  │
                  │  from symbiote.core.kernel import SymbioteKernel
                  │  from symbiote.browser import register
                  │
                  │  kernel = SymbioteKernel(config, llm)
                  │  register(kernel, policy=...)   # opt-in, uma linha
                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  src/symbiote/                                                      │
│  ├─ core/, runners/, environment/, security/   ◄── NÃO modificado  │
│  │   ├─ ToolGateway, PolicyGate, CompositeHook                      │
│  │   ├─ ChatRunner (loop + 3-layer compaction)                      │
│  │   └─ security.network.validate_url (SSRF)                        │
│  ├─ dream/, harness/, discovery/, mcp/   ◄── irmãos existentes     │
│  │                                                                  │
│  └─ browser/   ◄── NOVO subpackage (opt-in, deps em extras)        │
│      ├─ __init__.py            # exporta register()                 │
│      ├─ register.py            # API pública                        │
│      ├─ config.py              # SearchConfig, BrowserConfig...     │
│      ├─ search/                                                     │
│      │   ├─ tools.py           # web_search, web_extract, ...       │
│      │   ├─ compressor.py      # compressão LLM de resultados       │
│      │   └─ providers/  (exa, tavily, firecrawl, parallel)          │
│      ├─ browser/                                                    │
│      │   ├─ tools.py           # browser_navigate, snapshot, ...    │
│      │   ├─ supervisor.py      # lifecycle, atexit, cleanup         │
│      │   ├─ snapshot.py        # aria tree → texto                  │
│      │   ├─ stealth.py         # opcional, behind [stealth] extra   │
│      │   └─ providers/  (chromium_local, browserbase, browser_use,  │
│      │                  cdp)                                        │
│      └─ hooks/                                                      │
│          ├─ website_policy.py  # blocklist por domínio + TTL        │
│          └─ redirect_safety.py # SSRF em redirects                  │
└─────────────────────────────────────────────────────────────────────┘
```

**Garantia de "não importa por default"**: `src/symbiote/__init__.py` não faz `from .browser import …`. Cliente que faz `import symbiote` continua sem tocar em Playwright.

## 4. Tools expostas

### 4.1 Web search

| Tool ID | Descrição | Parâmetros principais | Fase |
|---|---|---|---|
| `web_search` | Busca textual via Brave (SymGateway proxy) | `query`, `limit` (1-20) | **1 — implementado** |
| `web_extract` | Extrai conteúdo de URLs específicas | `urls[]`, `format` (markdown/text) | 4 (planejado) |
| `web_crawl` | Crawl com instrução natural | `domain`, `instruction`, `max_pages` | 4 (planejado) |

**Como funciona o `web_search` hoje:** `register(kernel, search_backend="brave")` ativa a tool. O handler faz `POST {SYMGATEWAY_BASE_URL_sem_v1}/proxy/brave/web-search` com o `SYMGATEWAY_API_KEY` do host. SymGateway repassa pra Brave Search API e devolve o JSON; nós normalizamos pra uma lista compacta de `{url, title, snippet}`.

Vantagens do roteamento via SymGateway:
- **Sem nova credencial** — usa o mesmo bearer token do LLM
- **Sem SDK extra** — só `httpx`, já em `dependencies`
- **Billing centralizado** — Brave cobra $0.003/query, gateway repassa
- **Auditoria centralizada** — gateway logs incluem `request_id`, `cost_usd`, `elapsed_ms`

### 4.2 Browser automation

| Tool ID | Descrição | Parâmetros |
|---|---|---|
| `browser_navigate` | Abre URL em sessão isolada | `url`, `task_id?` |
| `browser_snapshot` | Accessibility tree da página (texto) | `task_id` |
| `browser_click` | Clica elemento por ref selector | `ref` (@e1, @e2…), `task_id` |
| `browser_fill` | Preenche input | `ref`, `value`, `task_id` |
| `browser_select` | Seleciona option | `ref`, `value`, `task_id` |
| `browser_extract` | Extrai conteúdo task-aware via LLM | `task_id`, `goal` |
| `browser_screenshot` | PNG da viewport | `task_id`, `full_page?` |
| `browser_wait_for` | Espera por texto/elemento | `task_id`, `target`, `timeout` |
| `browser_close` | Encerra sessão | `task_id` |

`task_id` mapeia 1:1 com sessão do Symbiote por padrão (usa `external_key`).

### 4.3 Tool descriptors

Cada tool é declarada como `ToolDescriptor` com JSON Schema completo. Exemplo:

```python
ToolDescriptor(
    tool_id="browser_navigate",
    name="Browser Navigate",
    description="Open a URL in an isolated browser session and return initial snapshot.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "task_id": {"type": "string"},
        },
        "required": ["url"],
    },
    tags=["browser", "navigation"],
)
```

## 5. Hooks de segurança

### 5.1 `WebsitePolicyHook`

Blocklist/allowlist por domínio com cache TTL.

```python
class WebsitePolicyHook(BaseHook):
    def __init__(self, blocklist: list[str], allowlist: list[str] | None = None, ttl: int = 300):
        ...

    async def before_tool(self, tool_id: str, params: dict) -> None:
        if tool_id.startswith(("browser_", "web_")):
            url = params.get("url") or params.get("urls", [None])[0]
            if url and not self._allowed(url):
                raise PermissionError(f"Domain blocked by policy: {url}")
```

### 5.2 `RedirectSafetyHook`

Re-valida cada redirect contra `symbiote.security.network.validate_url`. Cobre o gap conhecido: SSRF via redirect 3xx.

### 5.3 Composição com PolicyGate

PolicyGate continua autorizando o `tool_id` por symbiote (allowlist nível tool); hooks fazem a verificação URL-level. Os dois caminhos rodam em série — gate primeiro, hook depois — e ambos logam no `audit_log`.

## 6. Estrutura no repo

### 6.1 Layout dos arquivos (no repo do Symbiote)

```
symbiote/
├── pyproject.toml                # extras [browser], [search], [stealth] adicionados
├── src/symbiote/
│   ├── core/                     # ◄── intocado
│   ├── runners/                  # ◄── intocado
│   ├── environment/              # ◄── intocado
│   ├── security/                 # ◄── intocado
│   ├── ...                       # outros subpacotes existentes intocados
│   └── browser/                  # ◄── NOVO
│       ├── __init__.py           # re-exporta register
│       ├── register.py           # register(kernel, ...)
│       ├── config.py             # SearchConfig, BrowserConfig, PolicyConfig
│       ├── search/
│       │   ├── __init__.py
│       │   ├── tools.py          # web_search, web_extract, web_crawl
│       │   ├── compressor.py     # compressão LLM de resultados
│       │   └── providers/
│       │       ├── base.py       # SearchProvider protocol
│       │       ├── exa.py
│       │       ├── tavily.py
│       │       ├── firecrawl.py
│       │       └── parallel.py
│       ├── browser/
│       │   ├── __init__.py
│       │   ├── tools.py          # browser_* handlers
│       │   ├── supervisor.py     # lifecycle, atexit, cleanup
│       │   ├── snapshot.py       # aria tree → texto
│       │   ├── stealth.py        # opcional, [stealth] extra
│       │   └── providers/
│       │       ├── base.py       # BrowserProvider protocol
│       │       ├── chromium_local.py
│       │       ├── browserbase.py
│       │       ├── browser_use.py
│       │       └── cdp.py
│       └── hooks/
│           ├── __init__.py
│           ├── website_policy.py
│           └── redirect_safety.py
└── tests/
    ├── unit/browser/             # mock providers
    ├── integration/browser/      # exige Chromium local
    └── smoke/browser/            # exige creds reais
```

### 6.2 Mudança no `pyproject.toml` (aditiva)

Adicionar aos extras existentes:

```toml
[project.optional-dependencies]
# ... extras existentes (dev, llm) ...

browser = [
    "playwright>=1.40",
]
stealth = [
    "playwright-stealth",
]
```

**Nenhum extra `[search]`.** Web search via SymGateway só precisa de `httpx`, que já está em `dependencies`. Se algum cliente quiser provider direto (sem SymGateway), pode plugar via `extra_providers=` em uma versão futura.

Instalação:
- `pip install -e .` → search via SymGateway funciona, browser inativo
- `pip install -e ".[browser]"` → libera Playwright + Chromium
- `pip install -e ".[browser,stealth]"` → tudo

### 6.3 Imports preguiçosos (lazy)

Em `src/symbiote/browser/register.py`, os imports de Playwright/providers ficam **dentro** das funções que precisam deles. Razão: se o usuário roda `pip install -e ".[search]"` (sem `[browser]`) e chama `register(..., browser_backend=None)`, nada de Playwright deve ser importado. Erros de import faltante só aparecem quando o backend é efetivamente solicitado.

## 7. API de integração

Uma função, uma linha no host:

```python
from symbiote.core.kernel import SymbioteKernel
from symbiote.browser import register

kernel = SymbioteKernel(config, llm)

register(
    kernel,
    search_backend="tavily",     # ou "exa" | "firecrawl" | "parallel" | None
    browser_backend="chromium",  # ou "browserbase" | "browser_use" | None
    policy={
        "blocklist": ["facebook.com", "*.tracking.com"],
        "allowlist_only": False,
    },
    stealth=False,
)
```

`None` em qualquer backend desativa aquela família — útil pra carregar só search ou só browser.

`register()` faz internamente:
1. Lê credenciais do env (sem nunca pedir ao usuário — vai pra SymVault em prod)
2. Instancia providers selecionados
3. Registra cada `ToolDescriptor` no `kernel.tool_gateway`
4. Adiciona `WebsitePolicyHook` e `RedirectSafetyHook` em `kernel.hooks`
5. Garante cleanup do browser via `atexit`

## 8. Plano de fases

### Fase 0 — Discovery ✅
- [x] Confirmar requisitos com SymbiOS (search via SymGateway/Brave)
- [x] Decidido: Brave via SymGateway proxy (já configurado, sem novas creds)
- [x] Branch `feature/browser` criado
- [x] Adicionados extras `[browser]`, `[stealth]` ao `pyproject.toml`

### Fase 1 — Web search MVP via Brave/SymGateway ✅
- [x] `SearchProvider` async protocol
- [x] `BraveViaSymGateway` provider — chama `/proxy/brave/web-search`
- [x] `SearchOptions.resolved_gateway_url()` / `resolved_api_key()` lendo `.env`
- [x] `web_search` tool com descriptor + handler async
- [x] Wiring em `register(kernel, search_backend="brave")`
- [x] 7 unit tests com httpx mockado + 1 smoke real contra SymGateway
- [x] **Gate:** `kernel.tool_gateway.execute_async("web_search", ...)` retorna resultados normalizados (verificado: query "symlabs" devolve 3 hits)
- [ ] `WebsitePolicyHook` com blocklist (movido pra Fase 5 — hardening)
- [ ] Compressor LLM opcional (movido pra Fase 5 — hoje os snippets do Brave já são curtos)

### Fase 2 — Browser local (Chromium) ✅
- [ ] `BrowserProvider` protocol
- [ ] `chromium_local.py` via Playwright
- [ ] `supervisor.py` — processo isolado por `task_id`, cleanup em `atexit`/SIGTERM
- [ ] `snapshot.py` — accessibility tree → texto com refs (@e1, @e2…)
- [ ] Tools: `navigate`, `snapshot`, `click`, `fill`, `close`
- [ ] `RedirectSafetyHook` integrado
- [ ] Testes integration com sites estáticos controlados
- [ ] **Gate:** Symbiota consegue navegar, ler, e clicar numa página real

### Fase 3 — Providers de cloud (3-5 dias)
- [ ] `browserbase.py` + `browser_use.py`
- [ ] Auto-detect de backend baseado em credenciais disponíveis
- [ ] Documentar config matrix
- [ ] **Gate:** Mesma tool funciona local ou cloud sem mudar código do host

### Fase 4 — Web extract/crawl (Firecrawl via SymGateway)
- [ ] `/ask devops` para seed do Firecrawl no SymGateway (mesmo padrão do `seed_brave_search.py`)
- [ ] `FirecrawlViaSymGateway` provider chamando `/proxy/firecrawl/...`
- [ ] `web_extract` e `web_crawl` tools com descriptors
- [ ] **Gate:** Symbiota consegue extrair markdown limpo de URLs específicas

### Fase 5 — Hardening (3-5 dias)
- [ ] `browser_screenshot`, `browser_wait_for`, `browser_extract`
- [ ] Stealth opcional (`[stealth]` extra)
- [ ] xAI search (`x_search`) — se houver demanda
- [ ] Coverage ≥85% (alinhado com Symbiote core)
- [ ] **Gate:** Pronto para deploy em staging via systemd

### Fase 6 — Release v0.6.0
- [ ] Documentação completa (README, exemplos no SymbiOS)
- [ ] CHANGELOG
- [ ] Bump pyproject 0.5.0 → 0.6.0
- [ ] Tag + push + `/deploy`

### Fase 7 — DuckDuckGo HTML ❌ (investigada e revertida)

**Conclusão (2026-05-26): inviável.** Tentou-se adicionar `DuckDuckGoHtmlProvider` como alternativa free ao Brave, replicando a abordagem do `claw-code` (scraping de `html.duckduckgo.com/html/`). A implementação rodou e os unit tests passaram com HTML mockado. Smoke test inicial passou por **flakiness** — a próxima request retornou `HTTP 202 + página de anomaly` da DDG.

Diagnóstico (evidências capturadas):

| Vetor | Resultado |
|---|---|
| `httpx` GET com nosso User-Agent | 202 + anomaly page |
| `httpx` GET com UA exato do `claw-rust-tools/0.1` | 202 + anomaly page |
| `httpx` GET com UA Firefox real | 202 + anomaly page |
| **Binário `claw-code` real (Rust + reqwest)** | retorna apenas 2 links pra `duckduckgo.com/html/` (lixo) |
| Playwright headless Chromium | redireciona pra `static-pages/418.html` |
| Playwright + UA Chrome real + stealth init script + viewport real | redireciona pra `static-pages/418.html` |

DDG aplica detecção multi-camada (TLS fingerprint, IP reputation, headless flags, ordem de headers). **Bypass robusto exigiria** `playwright-stealth` + headed mode (quebra em servidor sem display) + proxies residenciais — caminho de "bot evasion" que não cabe numa infra séria.

**Lições registradas para evitar repetir:**
- claw-code resolve o caso single-user dele aceitando flakiness como custo
- "Free" via scraping é ilusório a médio prazo — sempre vira manutenção sem fim
- Antes de adicionar provider gratuito, validar com 5+ queries reais em sequência
- Caminho honesto pra "free fallback": **Searxng self-hosted** (DevOps roda instância, query via HTTP estável) ou apenas Brave free tier ($5/mês = 20k queries)

Revertido no commit subsequente. `SearchBackend = Literal["brave"]` permanece com apenas um provider; estrutura do `SearchProvider` protocol fica viva para futuras adições (Searxng/Firecrawl/Exa via SymGateway).

## 9. Riscos e mitigações

| Risco | Probabilidade | Impacto | Mitigação |
|---|---|---|---|
| Dependência de Playwright pesada em servers sem display | Alta | Médio | Extra `[browser]` opcional; **imports lazy** dentro do `register.py` para `import symbiote` nunca tocar em Playwright |
| Cliente atual fazendo `import symbiote.*` ganhar import lento | Baixa | Médio | `src/symbiote/__init__.py` **não importa** `browser`; CI roda smoke test medindo import time do core |
| Browser cloud providers (Browserbase) instáveis ou caros | Média | Médio | Local Chromium como default; cloud é opt-in |
| Compaction do ChatRunner não dá conta de snapshots grandes | Média | Alto | Compressor LLM no resultado da tool antes de devolver pro loop; testes de stress na Fase 2 |
| SSRF via JS redirect dentro do browser | Baixa | Alto | Reusar `validate_url` em qualquer `navigate` interno; documentar limitação de DNS rebinding |
| Schema drift entre providers de search | Alta | Baixo | `SearchProvider` protocol normaliza shape; testes per-provider |
| Credenciais vazadas em logs | Baixa | Alto | Nunca logar params crus; `audit_log` mascara campos sensíveis (padrão Symbiote já existe) |
| Tools de browser quebram quando site muda | Alta | Baixo | Snapshot via aria tree é mais estável que CSS selectors; documentar como debugar |
| Acoplamento implícito ao core ao longo do tempo | Média | Médio | `symbiote.browser` só importa do core via interfaces públicas (`ToolGateway.register`, `BaseHook`, `validate_url`); revisão arquitetural obrigatória se quiser tocar em internals |

## 10. Não-objetivos (explícitos)

Coisas que **não** vão entrar nesse package nessa primeira versão:

- Modificações no kernel do Symbiote
- Substituir o ChatRunner por um agent loop customizado
- Clarify-flow interativo (vive no host, não na tool)
- UI/TUI específica de browser (cliente do SymbiOS resolve)
- Suporte a Firefox/WebKit — só Chromium na v1
- File upload via browser — fase futura
- Captcha solving — fora de escopo por política

## 11. Métricas de sucesso

- **Funcional:** SymbiOS executa "pesquise X e me conte" em <30s end-to-end
- **Funcional:** SymbiOS executa "abra Y, faça login, extraia tabela Z" sem código no host além do `register()`
- **Qualidade:** Cobertura ≥85%, mypy strict passa, ruff zero warnings
- **Compat:** Clientes atuais do `symbiote` core funcionam sem alteração (CI roda suite do Symbiote contra `symbiote-browser` instalado)
- **Operacional:** Deploy em staging via Gitea Actions; `/health` retorna 200

## 12. Decisões pendentes

| # | Decisão | Quem decide | Quando | Status |
|---|---|---|---|---|
| D1 | ~~Repo separado vs subpackage~~ | Você | — | **Resolvida**: subpackage `src/symbiote/browser/` no próprio repo |
| D2 | ~~Provider default de search~~ | Você | — | **Resolvida**: Brave via SymGateway (mais barato, sem creds novas, alinhado com arquitetura Symlabs) |
| D3 | Provider default de browser (Chromium local sempre? Ou auto-detect cloud?) | Você | Fase 2 | Aberta |
| D4 | Inclui xAI search na v1 ou v2? | Demanda do SymbiOS | Fase 4 | Aberta |
| D5 | Bump de versão do Symbiote ao mergear | Você (regra global: padrão é patch) | Antes do merge | Aberta — feature nova sugere minor, mas precisa do seu OK |

## 13. Próximos passos

1. Você revisa este doc e cravamos D2–D5
2. `/ask devops` para alocação de credenciais no SymVault
3. Abrir branch `feature/browser` no repo do Symbiote com skeleton da Fase 0
4. Abrir tracking issue agregadora

---

## Apêndice A — Mapa de reuso (o que vem do Symbiote)

| Capacidade Symbiote | Onde no kernel | Como `symbiote.browser` consome |
|---|---|---|
| Registro de tool declarativa | `environment/tools.py::ToolGateway` | `register()` chama `kernel.tool_gateway.register(...)` |
| HTTP tools com `url_template` | `environment/tools.py::register_http_tool` | Search providers HTTP usam direto |
| SSRF | `security/network.py::validate_url` | `RedirectSafetyHook` chama em cada redirect |
| Policy/audit | `environment/policies.py::PolicyGate` | Tool calls passam automaticamente |
| Hooks | `core/hooks.py::BaseHook` | `WebsitePolicyHook` e `RedirectSafetyHook` herdam |
| Tool loop + compaction | `runners/chat.py::ChatRunner` | Sem alteração — browser é só mais uma tool |
| Long-Run mode | `runners/long_run.py` | Pesquisa multi-fonte pode rodar em long-run |
| Subagent spawn | `runners/subagent.py::SubagentManager` | Pesquisa paralela com `effort="high"` |
| MessageBus | `bus/message_bus.py` | Streaming de progresso de browser pra UI |
| Reflection / Dream Mode | `core/reflection.py`, `dream/engine.py` | Pesquisas viram memória declarativa; Dream consolida |
| MCP provider | `mcp/provider.py` | Tools de browser ficam expostas via MCP automaticamente |

## Apêndice B — Snippet de uso completo

```python
from symbiote.core.kernel import SymbioteKernel
from symbiote.config.models import KernelConfig
from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register

kernel = SymbioteKernel(
    config=KernelConfig(db_path="data/symbiote.db"),
    llm=MockLLMAdapter(),
)

register(
    kernel,
    search_backend="tavily",
    browser_backend="chromium",
    policy={"blocklist": ["*.ads.com"]},
)

bot = kernel.create_symbiote(name="Researcher", role="web researcher")
kernel.environment.configure(
    symbiote_id=bot.id,
    tools_allowlist=["web_search", "web_extract", "browser_navigate", "browser_snapshot"],
)

session = kernel.start_session(bot.id, goal="Research X")
print(kernel.message(session.id, "Find the latest paper on retrieval-augmented generation"))
kernel.close_session(session.id)
```
