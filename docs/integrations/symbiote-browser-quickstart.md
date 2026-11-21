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
register(kernel, search_backend="brave_api")   # precisa de BRAVE_API_KEY; veja "Mecanismo de busca" abaixo

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
    search_backend="brave_api",  # nome registrado, objeto do host, ou None
    search_options={
        "max_results_default": 5,
        "compress_results": True,    # passa resultados pelo compressor LLM
        "provider_options": {        # o que cada provedor precisa, por nome
            "brave_api": {"api_key": "BSA..."},
        },
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

### Mecanismo de busca

Nenhum mecanismo de busca vem fixo: o host escolhe. `search_backend`, `extract_backend` e `crawl_backend` aceitam o nome de um provedor registrado ou um objeto do próprio host.

| Nome | Ferramenta | Como chega ao serviço | O que precisa |
|---|---|---|---|
| `brave_api` | `web_search` | direto na API do Brave | `provider_options["brave_api"]["api_key"]` ou `BRAVE_API_KEY` |
| `searxng` | `web_search` | direto numa instância SearXNG | `provider_options["searxng"]["base_url"]` ou `SEARXNG_URL` (saída JSON habilitada) |
| `brave` | `web_search` | pelo SymGateway | `SYMGATEWAY_BASE_URL` e `SYMGATEWAY_API_KEY` |
| `symgateway_objective` | `web_search` | pelo SymGateway | os dois acima e `search_options["symgateway_objective"]` |
| `duckduckgo`, `yahoo` | `web_search` | lendo a página de resultados do buscador | escolha explícita; ver "Busca e leitura sem serviço pago" |
| `http` | `web_extract` | um GET direto na página | nada |
| `forge_scraper` | `web_extract` | local | extra `[extract]` |
| `firecrawl` | `web_extract`, `web_crawl` | pelo SymGateway | `SYMGATEWAY_BASE_URL` e `SYMGATEWAY_API_KEY` |

Para usar outro mecanismo (DuckDuckGo, Google, um índice interno), escreva um provedor com o método do protocolo em `symbiote.browser.search.providers.base` e passe o objeto, ou registre um nome:

```python
from symbiote.browser import register, register_search_provider

class MinhaBusca:
    name = "minha-busca"

    async def search(self, query: str, *, limit: int = 5) -> list[dict]:
        hits = await meu_indice.buscar(query, limit)
        return [{"url": h.url, "title": h.titulo, "snippet": h.resumo} for h in hits]

register(kernel, search_backend=MinhaBusca())                 # o objeto

register_search_provider("minha-busca", lambda options: MinhaBusca())
register(kernel, search_backend="minha-busca")                # ou o nome
```

A fábrica recebe o `SearchOptions` dado a `register`; `options.options_for("minha-busca")` devolve o que veio em `provider_options` para aquele nome. `register_extract_provider` e `register_crawl_provider` fazem o mesmo para `web_extract` (`async extract(url)`) e `web_crawl` (`async crawl(domain, *, instruction, max_pages)`).

### Busca e leitura sem serviço pago

Nada desta seção liga sozinho: sem `search_backend` não há `web_search`, sem `extract_backend` não há `web_extract`, e nenhum buscador é escolhido pelo host.

**Ler uma página: `extract_backend="http"`.** Um GET na página, sem serviço no meio. O pedido prefere `text/markdown`; HTML vira markdown leve (títulos, listas, links), sem navegação, rodapé, formulário e script; texto e JSON voltam como vieram; outros tipos (PDF, imagem) dão erro, e numa lista o próximo provedor tenta. Nada é executado: página montada por JavaScript rende pouco ou nenhum texto.

```python
register(kernel, extract_backend="http")
register(kernel, extract_backend=["http", "firecrawl"])   # com reserva
```

Página longa volta com o começo e o fim e uma linha dizendo quanto ficou de fora (`max_content_chars`, padrão 40 000; a resposta traz `truncated` e `total_chars`). O corpo é lido até `max_page_bytes` (padrão 2 MB).

**Quem traz a página é do host: `search_options["fetcher"]`.** O extrator `http` e os leitores de buscador nunca abrem conexão: pedem a página a um objeto com um método só.

```python
from symbiote.browser import FetchedPage, register

class MeuCanal:
    async def fetch(self, url, *, headers=None):
        resposta = await meu_canal.get(url, headers=headers)   # um proxy, um app em outra máquina
        return FetchedPage(
            status=resposta.status, headers=resposta.headers, body=resposta.corpo, url=resposta.url_final
        )

register(kernel, extract_backend="http", search_options={"fetcher": MeuCanal()})
```

- `url` na resposta é o endereço de onde ela veio, depois dos redirecionamentos.
- **Quem traz a página segue os redirecionamentos e confere cada salto.** Só ele sabe para onde um nome resolve na rede de onde o pedido sai. Os clientes do pacote conferem com `validate_url` (só http e https, nenhum endereço interno).
- Seja quem for, o pacote limita o tempo (`timeout_seconds`) e o tamanho, e o `web_extract` confere o endereço final contra a `policy`.
- Sem `fetcher`, vale o cliente do pacote (`"http"`, httpx comum). `"chrome"` é o que imita o Chrome (abaixo). `register_fetcher(nome, fabrica)` registra outro nome.

**Vários provedores de busca em ordem.** `search_backend` aceita uma lista: o provedor que falha é pulado e responde o primeiro que trouxer resultados.

```python
register(kernel, search_backend=["brave_api", "searxng"])
```

**Os desfechos de uma busca não se confundem.** A resposta do `web_search` traz `outcome`:

| Desfecho | O que o modelo recebe |
|---|---|
| Resultados | `outcome: "results"`, a lista e, numa lista de provedores, `source` (quem respondeu) e `attempts` (o que os anteriores fizeram) |
| Nenhum resultado | `outcome: "no_results"`, lista vazia e uma nota: a busca rodou e não achou nada |
| A busca não rodou | erro com o que cada provedor fez, por exemplo `duckduckgo: refused (HTTP 202); yahoo: results unrelated to the query` |

"Veio cheio e sem relação com a consulta" é uma recusa, não um sucesso: é como alguns buscadores recusam sem dizer. Um provedor avisa disso com `SearchUnrelatedError` (de `symbiote.browser.search.outcomes`, junto de `SearchRefusedError` e de `query_coverage`, a medida usada: a fração das palavras da consulta, com quatro letras ou mais e sem acento, que aparece no título, no resumo ou no endereço dos cinco primeiros).

**Buscar lendo a página de um buscador: `duckduckgo` e `yahoo`.** Leia antes de escolher:

- Ler a página de resultados assim contraria os termos dos buscadores. Por isso é sempre escolha explícita do host, que responde por ela.
- É melhor esforço. O buscador recusa o que não parece uma pessoa num navegador: pelo endereço de onde o pedido sai (datacenter é recusado cedo; uma conexão residencial, no ritmo de uma pessoa, não) e pelo jeito de abrir a conexão. Só os cabeçalhos de navegador não bastam em toda rede.
- Os seletores são os das páginas de 2026-11-21 e quebram quando o buscador muda o HTML. O leitor separa "página que não consegui ler" (erro) de "a busca não achou nada" (lista vazia), e o uso recomendado é em lista.

```python
register(
    kernel,
    search_backend=["duckduckgo", "yahoo"],
    search_options={
        "fetcher": "chrome",                       # ou o objeto do host
        "provider_options": {"duckduckgo": {"region": "br-pt", "accept_language": "pt-BR,pt;q=0.9"}},
    },
)
```

`fetcher="chrome"` (extra `[web]`, `pip install "symbiote[web]"`, biblioteca `curl_cffi`) abre a conexão como o Chrome abre, no TLS e no HTTP/2. `min_relevance` (padrão 0,5; por provedor em `provider_options`) é o piso da conferência de relação; `0` desliga. Uma consulta cujas palavras não aparecem em resultado nenhum também cai nesse desfecho: em 2026-11-21 o Yahoo respondia a uma consulta sem correspondência com resultados aproximados, e só o DuckDuckGo dizia "nenhum resultado". O Bing não tem leitor: na medição do SymbiOS de 2026-11-21, metade das respostas dele era sobre outro assunto.

Conferência ao vivo em 2026-11-21, de uma conexão residencial, um pedido a cada 2,5 s: com `"chrome"`, o DuckDuckGo respondeu 11 de 12 consultas (um HTTP 202) e o Yahoo 12 de 12; com o cliente comum, 8 de 8 nos dois. Em maio de 2026 e na medição do SymbiOS, de outras redes, o cliente comum recebeu 202 do DuckDuckGo em todas.

**Cache.** `search_options["cache_ttl_seconds"]` guarda em memória as respostas de `web_search` e `web_extract` por esse tempo; a mesma consulta ou o mesmo endereço não sai de novo. Desligado por padrão.

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
