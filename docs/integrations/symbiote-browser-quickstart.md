# symbiote.browser — Quickstart para clientes

> Guia prático de instalação, setup e uso do subpackage `symbiote.browser`, que adiciona **websearch** e **navegação em browser** ao Symbiote.

**Status:** Especificação da v1.0 (referência para implementação e early adopters)
**Pré-requisitos:** Symbiote ≥ 0.5.0, Python ≥ 3.12
**Plano arquitetural:** [symbiote-browser.md](./symbiote-browser.md)

---

## 1. O que você ganha

Ao ativar `symbiote.browser`, seu Symbiota passa a poder:

- **Pesquisar na web** (`web_search`, `web_extract`, `web_crawl`) via Tavily, Exa, Firecrawl ou Parallel
- **Navegar em sites reais** (`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_fill`, `browser_screenshot`, ...) via Chromium local, Browserbase ou Browser Use
- **Aplicar policy de domínios** (blocklist/allowlist) sem escrever código
- **Auditar tudo** — cada ação passa pelo `PolicyGate` do Symbiote e fica no `audit_log`

Tudo isso **sem mudar nada no seu código atual** do Symbiote. É opt-in: enquanto você não chama `register()`, o comportamento é exatamente o anterior.

## 2. Instalação

### 2.1 Escolha o que precisa

O Symbiote tem três extras opcionais para a feature de browser:

| Extra | Inclui | Tamanho aprox. | Quando usar |
|---|---|---|---|
| `[search]` | SDKs de Tavily/Exa/Firecrawl/Parallel | ~30 MB | Só web search, sem navegação real |
| `[browser]` | Playwright + Chromium runtime | ~5 MB python + ~170 MB Chromium | Quando precisa abrir páginas e interagir |
| `[stealth]` | `playwright-stealth` | ~1 MB | Anti-fingerprint pra sites com bot detection |

### 2.2 Instalando

```bash
# Apenas web search
pip install "symbiote[search]"

# Apenas navegação em browser
pip install "symbiote[browser]"
playwright install chromium      # baixa o runtime (uma vez)

# Tudo
pip install "symbiote[search,browser,stealth]"
playwright install chromium
```

### 2.3 Verificando

```bash
python -c "from symbiote.browser import register; print('OK')"
python -c "from playwright.sync_api import sync_playwright; print('Chromium runtime OK')"
```

## 3. Configuração do ambiente

### 3.1 Credenciais

**Você não precisa pedir credenciais ao usuário final.** Pedir ao DevOps:

```
/ask devops
> Preciso das credenciais do SymVault para symbiote-browser:
> - TAVILY_API_KEY (ou EXA_API_KEY / FIRECRAWL_API_KEY / PARALLEL_API_KEY)
> - BROWSERBASE_API_KEY + BROWSERBASE_PROJECT_ID (se for usar Browserbase)
> - BROWSER_USE_API_KEY (se for usar Browser Use)
```

DevOps entrega via SymVault. Em desenvolvimento local, coloque no `.env` do seu projeto:

```bash
# .env (NUNCA commitar)
TAVILY_API_KEY=sk-tavily-xxxxxxxxxxxxxxxx
BROWSERBASE_API_KEY=bb_xxxxxxxxxxxxxxxx
BROWSERBASE_PROJECT_ID=proj_xxxxxxxxxxxxxxxx
```

Em produção, as variáveis vêm do SymVault automaticamente.

### 3.2 Cache do Chromium (compartilhado)

Playwright guarda os binários em `~/.cache/ms-playwright/`. **Múltiplos projetos compartilham esse cache** — se você já tem outro projeto que usa Playwright, o `playwright install chromium` pode ser instantâneo.

Verifique:
```bash
ls ~/.cache/ms-playwright/
```

Se aparece `chromium-1217` ou versão similar, está pronto.

## 4. Quickstart — primeira pesquisa

5 linhas pra fazer um Symbiota pesquisar na web:

```python
from symbiote.core.kernel import SymbioteKernel
from symbiote.config.models import KernelConfig
from symbiote.browser import register

kernel = SymbioteKernel(KernelConfig(db_path="data/db.sqlite"), llm=your_llm_adapter)
register(kernel, search_backend="tavily")

bot = kernel.create_symbiote(name="Researcher", role="web researcher")
session = kernel.start_session(bot.id, goal="research")
print(kernel.message(session.id, "What's the current Python version?"))
kernel.close_session(session.id)
```

O Symbiota vai:
1. Decidir que precisa pesquisar
2. Chamar `web_search` com a query apropriada
3. Receber resultados compressados (compressor LLM reduz tokens)
4. Te dar a resposta sintetizada

## 5. Quickstart — primeira navegação

Pra abrir um site real, clicar e extrair informação:

```python
from symbiote.core.kernel import SymbioteKernel
from symbiote.config.models import KernelConfig
from symbiote.browser import register

kernel = SymbioteKernel(KernelConfig(db_path="data/db.sqlite"), llm=your_llm_adapter)
register(
    kernel,
    browser_backend="chromium",      # local headless
    browser_options={"headed": True, "slow_mo": 500},  # pra ver acontecendo
)

bot = kernel.create_symbiote(name="Navigator", role="web navigator")
session = kernel.start_session(bot.id, goal="navigate")
print(kernel.message(
    session.id,
    "Open en.wikipedia.org/wiki/Python_(programming_language) "
    "and tell me the year of first release"
))
kernel.close_session(session.id)
```

Com `headed=True` o Chromium abre na sua tela e você vê cada ação. `slow_mo=500` adiciona 500ms entre ações pra dar tempo de acompanhar.

## 6. Configuração completa de `register()`

```python
register(
    kernel,

    # ── Search ─────────────────────────────────────────────
    search_backend="tavily",   # "tavily" | "exa" | "firecrawl" | "parallel" | None
    search_options={
        "max_results_default": 5,
        "compress_results": True,    # passa resultados pelo compressor LLM
    },

    # ── Browser ────────────────────────────────────────────
    browser_backend="chromium",  # "chromium" | "browserbase" | "browser_use" | None
    browser_options={
        "headed": False,           # True pra debug visual
        "slow_mo": 0,              # ms entre ações (debug)
        "viewport": (1280, 800),
        "timeout_ms": 30000,
        "isolated_session_per_task": True,
    },

    # ── Policy ─────────────────────────────────────────────
    policy={
        "blocklist": ["facebook.com", "*.ads.com", "*.tracking.com"],
        "allowlist": None,         # se setado, **só** esses domínios passam
        "ttl_seconds": 300,        # cache da policy
    },

    # ── Stealth (extra opcional) ───────────────────────────
    stealth=False,                  # True requer pip install "symbiote[stealth]"
)
```

Qualquer parâmetro pode ser omitido — defaults sensatos. `search_backend=None` ou `browser_backend=None` desativa aquela família.

## 7. Tools disponíveis (referência rápida)

### Web search

| Tool | Função |
|---|---|
| `web_search` | Busca textual; retorna lista de resultados |
| `web_extract` | Extrai conteúdo limpo de URLs específicas |
| `web_crawl` | Crawl direcionado por instrução natural |

### Browser

| Tool | Função |
|---|---|
| `browser_navigate` | Abre URL em sessão isolada |
| `browser_snapshot` | Captura página como accessibility tree (texto) |
| `browser_click` | Clica elemento via ref selector (`@e1`, `@e2`, ...) |
| `browser_fill` | Preenche input |
| `browser_select` | Seleciona option em `<select>` |
| `browser_extract` | Extrai informação task-aware via LLM |
| `browser_screenshot` | PNG da viewport ou da página inteira |
| `browser_wait_for` | Espera elemento ou texto aparecer |
| `browser_close` | Encerra sessão |

Detalhes (parâmetros, schemas, exemplos) em `docs/integrations/symbiote-browser.md` §4.

## 8. Como testar

Há três modos de teste, do mais simples ao mais completo:

### 8.1 Modo scripted (sem LLM)

Útil pra verificar que o plumbing funciona, sem custo de LLM:

```python
from symbiote.core.kernel import SymbioteKernel
from symbiote.config.models import KernelConfig
from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.browser import register

kernel = SymbioteKernel(
    KernelConfig(db_path="data/test.sqlite"),
    llm=MockLLMAdapter(),
)
register(kernel, browser_backend="chromium", browser_options={"headed": True, "slow_mo": 500})

bot = kernel.create_symbiote(name="t", role="tester")

# Invoca tools diretamente, sem passar pelo loop de LLM
result = kernel.tool_gateway.execute(bot.id, "browser_navigate", {
    "url": "https://example.com",
    "task_id": "t1",
})
print(result.output)

snap = kernel.tool_gateway.execute(bot.id, "browser_snapshot", {"task_id": "t1"})
print(snap.output)

kernel.tool_gateway.execute(bot.id, "browser_close", {"task_id": "t1"})
```

### 8.2 Modo agentic (com LLM)

Symbiota decide as ações:

```python
register(kernel, browser_backend="chromium", browser_options={"headed": True, "slow_mo": 500})
bot = kernel.create_symbiote(name="r", role="researcher")
session = kernel.start_session(bot.id, goal="navigation test")
print(kernel.message(
    session.id,
    "Go to example.com and tell me what's in the heading"
))
kernel.close_session(session.id)
```

### 8.3 Modo headed pra demo visual

Os flags `headed=True` e `slow_mo=500` (ou `1000`) são feitos pra isso. Em produção, use `headed=False` (default).

```python
browser_options={"headed": True, "slow_mo": 1000}
```

### 8.4 Suite de testes automatizados

Se você é dev do Symbiote, a suite cobre 5 camadas:

```bash
# Camada 1+2 — backward-compat + unit (rápido, sem rede)
pytest tests/unit/browser/

# Camada 3 — integration (precisa Chromium local)
pytest tests/integration/browser/

# Camada 4 — smoke (precisa creds reais; gated por env vars)
pytest tests/smoke/browser/ -m smoke
```

Detalhes em `docs/integrations/symbiote-browser.md` §14.

## 9. Considerações de produção

### 9.1 Headless sempre

Em servers sem display, `headed=True` quebra. Default já é `False`. Confirme antes do deploy.

### 9.2 Timeouts e cleanup

- `browser_options["timeout_ms"]` (default 30000): cada operação tem limite
- Cada `task_id` mantém uma sessão Chromium isolada até `browser_close` ou `atexit`
- `supervisor.py` garante kill de processos zumbis em SIGTERM/SIGINT

### 9.3 Custo

- **Chromium local:** zero custo, paga só CPU/RAM
- **Browserbase:** cobra por sessão; veja docs do provider
- **Browser Use:** idem
- **Search providers:** todos têm tier free + cobram por request acima

Recomendação: começar com Chromium local + Tavily; subir pra Browserbase só quando precisar de proxies residenciais ou stealth pesado.

### 9.4 Policy de domínios

Sempre defina `blocklist` em produção, mesmo que mínimo:

```python
policy={
    "blocklist": ["*.tracking.com", "*.ads.com", "doubleclick.net"],
}
```

Pra ambientes regulados, prefira `allowlist`:

```python
policy={
    "allowlist": ["wikipedia.org", "*.gov.br", "github.com"],
}
```

### 9.5 SSRF

`symbiote.security.network.validate_url` é aplicado automaticamente pelo `RedirectSafetyHook` em qualquer redirect. Cobre cloud metadata (169.254.169.254), localhost, ranges privados. **Não desative** salvo necessidade técnica documentada.

### 9.6 Logs e audit

Toda tool call vira entrada no `audit_log` do Symbiote (`storage.audit_log` table). Inspecione:

```sql
SELECT tool_id, params, success, timestamp
FROM audit_log
WHERE tool_id LIKE 'browser_%' OR tool_id LIKE 'web_%'
ORDER BY timestamp DESC
LIMIT 50;
```

## 10. Troubleshooting

### `ImportError: pip install "symbiote[browser]"`

`browser_backend="chromium"` mas Playwright não está instalado.
**Fix:** `pip install "symbiote[browser]" && playwright install chromium`

### `Executable doesn't exist at ~/.cache/ms-playwright/...`

Playwright instalado mas Chromium não baixado.
**Fix:** `playwright install chromium`

### Chromium abre, mas trava em `about:blank` ou erro de display

Você está em server sem display gráfico mas com `headed=True`.
**Fix:** `browser_options={"headed": False}` ou rodar dentro de `xvfb-run`.

### `SSRFError: URL ... resolves to blocked IP`

Você tentou navegar pra IP privado. Hooks bloquearam.
**Fix em dev/test:** `policy={"allow_internal": True}` (só pra dev — nunca em prod).
**Fix em prod:** revise se a URL realmente deveria ser pública.

### Provider de search retorna 401/403

Credencial faltando ou inválida.
**Fix:** verifique `.env` ou `/ask devops` pra atualizar SymVault.

### Snapshot retorna texto enorme, estoura contexto

Site complexo. Soluções:
1. Use `browser_extract(goal="...")` em vez de `browser_snapshot` — compressor LLM filtra
2. Ative compressão automática: `search_options={"compress_results": True}`
3. Use Long-Run mode (`mode="long_run"` na sessão) — planner/evaluator lidam com chunks grandes

### Tools não aparecem na tool list do Symbiota

Você chamou `register()` mas o `tools_allowlist` do environment está restrito.
**Fix:**
```python
kernel.environment.configure(
    symbiote_id=bot.id,
    tools_allowlist=["web_search", "browser_navigate", "browser_snapshot", "browser_click"],
)
```

## 11. FAQ

**P: Posso usar isso com SymbiOS hoje?**
R: Quando a feature estiver implementada e released. O plano (`symbiote-browser.md`) tem o roadmap; este doc é a referência da v1.0 final.

**P: Vai quebrar meu código atual do Symbiote?**
R: Não. O subpackage é opt-in: sem chamar `register()`, comportamento é exatamente o anterior. `import symbiote` não importa Playwright nem providers de search.

**P: Funciona em Termux/Android?**
R: Search providers (`[search]`) sim. Browser local (`[browser]`) não — Chromium não roda em Termux. Use Browserbase ou Browser Use cloud.

**P: Posso plugar meu próprio provider?**
R: Sim. Implemente o protocol `SearchProvider` ou `BrowserProvider` e registre via parâmetro `extra_providers=` em `register()` (planejado pra v1.1).

**P: Suporta Firefox/WebKit?**
R: Não na v1. Chromium-only. Outros browsers entram em versão futura se houver demanda.

**P: Como funciona com Long-Run mode do Symbiote?**
R: Browser tools rodam como qualquer outra tool dentro do planner/evaluator. Recomendado pra pesquisas que percorrem dezenas de fontes. Veja `docs/arch-02-execution.md`.

**P: Dream Mode usa minhas pesquisas?**
R: Sim — resultados de search e snapshots viram memórias (declarative). Dream Mode consolida em ruminação noturna (prune/reconcile/generalize/mine). Veja `docs/arch-04-dream-mode.md`.

**P: MCP — posso expor essas tools pra outros agents?**
R: Sim. O `symbiote.mcp.provider` expõe automaticamente todas tools registradas no `ToolGateway`, incluindo as do browser.

---

## Próximos passos

1. Instale o(s) extra(s) que você precisa
2. `/ask devops` pra credenciais
3. Adapte um dos quickstarts (§4 ou §5) ao seu caso
4. Comece pelo modo **scripted + headed** pra ver acontecendo
5. Promova pra **agentic + headless** quando estiver confiante

Problemas, dúvidas ou requests: abra issue no repo do Symbiote com tag `area:browser`.
