# Plano de implementação — melhorias do Symbiote

Data: 2026-11-13 · Base: `main` em `0737fcf` (versão 0.7.3) · Origem: `reports/2026-11-13_relatorio-melhorias.md`

Este plano é para o agente que vai executar as correções. Ele assume que você não tem nenhum contexto além deste arquivo, do relatório e do repositório.

## 1. Objetivo e como usar este plano

Implementar todos os itens do relatório de melhorias, em seis fases, com um teste de regressão para cada defeito.

Leia nesta ordem antes de tocar em código:

1. Este plano, inteiro.
2. `reports/2026-11-13_relatorio-melhorias.md` — descreve cada defeito (o que acontece, onde, evidência). O plano não repete essas descrições; cada tarefa cita o ID do relatório (`S-1`, `E-4`, ...).
3. `reports/2026-11-13_repro/README.md` — como rodar os scripts que reproduzem os defeitos.
4. `README.md`, `INFRA.md` e `docs/HOST_INTEGRATION.md` do repositório.

**Sobre os IDs.** `TK-nn` são as tarefas deste plano. Os demais (`S-1`, `E-4`, `M-2`, `T-1`, `A-1`, `C-1`, `D`, `Q-1`, `P-1`) são itens do relatório; em particular, `T-1` a `T-7` são os itens de tools do relatório, não tarefas.

Execute as fases na ordem. Dentro de uma fase, siga a ordem das tarefas, salvo quando a coluna "Depende" permitir outra. Atualize a coluna **Status** da tabela da seção 8 a cada tarefa (`pending`, `in_progress`, `done`, `skipped`) e registre desvios na seção 11.

## 2. Regras de trabalho

Estas regras valem para todas as tarefas.

1. **Nunca faça push para `main`.** Push em `main` dispara testes, build e deploy em staging (`.gitea/workflows/`). Trabalhe em branches locais e não faça push de nenhuma branch sem o operador pedir.
2. **Não toque em staging nem em produção.** Nada de SSH, deploy, `/deploy`, nem requisições a `symbiote.symlabs.ai`. Dúvida de infraestrutura vai para o operador.
3. **Não faça release.** Não altere a versão em `pyproject.toml`, não crie tag e não rode `/push`. O operador faz isso.
4. **Não mexa na instância local do operador** em `127.0.0.1:8008` nem no diretório `.symbiote/` da raiz. Para testar a API use o `TestClient` do FastAPI com banco em `tmp_path`. Não abra portas fixas.
5. **Sem rede e sem LLM real nos testes.** Use LLM falso (seção 4). Para HTTP tools, suba `http.server` em `127.0.0.1` porta `0` dentro do próprio teste.
6. **Não sobrescreva o `.coverage` versionado** antes da tarefa TK-80. Rode cobertura com `COVERAGE_FILE` apontando para fora do repo.
7. **Não peça credenciais.** Se uma tarefa parecer exigir uma, pare (seção 7).
8. **Um commit por tarefa**, no padrão do `git log` (`fix(memory): ...`, `feat(api): ...`), citando o ID do relatório na mensagem. Não inclua nos commits de tarefa os arquivos de `reports/` criados nesta análise; eles estão fora do git e o operador decide o que fazer com eles.
9. **Mudança incompatível só se estiver na seção 6.** Se uma correção exigir quebrar uma API pública que não está lá, pare.

## 3. Ambiente e comandos

O `.venv` na raiz já existe (Python 3.12, `pip install -e ".[dev,llm]"`). Se precisar recriar:

```bash
uv venv --python 3.12 .venv && VIRTUAL_ENV=$PWD/.venv uv pip install -e ".[dev,llm]"
```

Não use `setup_env.sh`: é um template de outro projeto (a TK-77 trata disso).

Comandos de verificação, sempre a partir da raiz:

```bash
# Suíte completa (linha de base: 1758 passed, 24 skipped, ~27 s)
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider

# Cobertura (linha de base: 85,48%)
COVERAGE_FILE=/tmp/symbiote.cov .venv/bin/python -m pytest -q -p no:cacheprovider \
  --cov=src/symbiote --cov-report=term:skip-covered

# Lint (linha de base: limpo)
.venv/bin/ruff check src tests

# Tipos (linha de base: 453 erros com strict; só entra como gate na TK-83)
.venv/bin/mypy src/symbiote

# Smoke da CLI (gate E2E do processo do repo)
bash tests/smoke/smoke_cli.sh
```

Dois detalhes do ambiente de teste que explicam por que a suíte atual não pega os defeitos:

- `tests/conftest.py` define `SYMBIOTE_DEV_MODE=1`, que faz `require_auth` devolver um admin falso. Testes de autenticação precisam de `monkeypatch.delenv("SYMBIOTE_DEV_MODE", raising=False)` (veja `tests/e2e/test_api_chat.py:23`).
- `symbiote.api.http` guarda estado em variáveis de módulo (`_adapter`, `_kernel`, `_tool_gateway`, `_key_manager`, `_local_admin_key`), e `symbiote.api.middleware` guarda `_key_manager`. Testes de API precisam zerá-las entre casos.

## 4. Método por tarefa

Para cada tarefa:

1. **Reproduza.** Rode o script indicado em "Repro" a partir de um diretório temporário (instruções no README dos scripts) e confirme que o defeito aparece no código atual.
2. **Escreva o teste que falha.** No arquivo indicado em "Teste primeiro". Rode e confirme que falha pelo motivo certo, não por erro de import ou de fixture.
3. **Corrija** com a menor mudança que resolve o defeito. Leia o código ao redor antes: as linhas citadas são da base `0737fcf` e podem ter se deslocado por tarefas anteriores.
4. **Verifique:** teste novo verde, suíte completa verde, `ruff` limpo.
5. **Atualize** o `CHANGELOG.md` na seção `[Unreleased]` (uma linha por tarefa, no estilo das entradas existentes; mudança incompatível ganha um sub-item `**MIGRAÇÃO:**`) e a documentação afetada.
6. **Commit** e atualização do Status na seção 8.

Itens marcados **R** no relatório foram reproduzidos por um subagente revisor e não foram refeitos na análise. Se a reprodução do passo 1 falhar no código atual, não invente a correção: marque a tarefa como `skipped (não reproduz)`, anote na seção 11 e siga.

**Definição de pronto de uma tarefa:** teste de regressão novo falhava antes e passa depois; suíte completa verde; `ruff` limpo; cobertura dos arquivos alterados em 85% ou mais (`min_coverage` em `process/fast_track/state/ft_state.yml`); `CHANGELOG.md` atualizado.

**Tamanhos** (legenda do repo): XS (< 30 min), S (30 min a 2 h), M (2 h a 4 h), L (mais de 4 h). **Prioridade:** P0 (isolamento e resultado errado silencioso), P1 (confiabilidade), P2 (o resto).

**Arquivos com muitas tarefas.** Não paralelize tarefas que tocam o mesmo arquivo:

- `src/symbiote/runners/chat.py`: TK-20, TK-21, TK-23, TK-25, TK-30, TK-31, TK-32, TK-33, TK-86.
- `src/symbiote/api/http.py`: TK-11, TK-12, TK-13, TK-41, TK-60, TK-61, TK-68, TK-86.
- `src/symbiote/environment/tools.py`: TK-10, TK-13, TK-15, TK-16, TK-18, TK-21, TK-22, TK-63, TK-85.
- `src/symbiote/core/kernel.py`: TK-20, TK-26, TK-27, TK-28, TK-35, TK-43, TK-46, TK-50, TK-65.

## 5. Decisões já tomadas

Vários itens do relatório admitem mais de uma correção. As escolhas abaixo são o padrão deste plano. O operador pode mudar qualquer uma antes de você começar a tarefa correspondente; sem instrução contrária, siga o padrão.

| # | Questão | Padrão deste plano | Tarefa |
|---|---|---|---|
| D-1 | Hooks documentados que nunca disparam: implementar ou tirar da doc | Implementar o disparo | TK-27 |
| D-2 | `LongRunRunner` não registrado: ligar ou marcar experimental | Ligar e corrigir os defeitos latentes | TK-28 |
| D-3 | Rotas públicas da API | `/health`, `/` (HTML do Console), `/docs`, `/openapi.json`, `/redoc`. Todo o resto exige chave | TK-11 |
| D-4 | Rotas `/api/*` do Console | Exigem chave admin | TK-11 |
| D-5 | Symbiote com `owner_id` nulo na API | Só admin acessa | TK-11 |
| D-6 | Assinatura de `MemoryStore.search` e `get_relevant` | `symbiote_id` vira argumento nomeado obrigatório; chamar sem ele falha | TK-10 |
| D-7 | Raiz das tools de filesystem | Novo `KernelConfig.fs_allowed_root`; quando definido, vale para todas as chamadas e o parâmetro `allowed_root` só pode restringir | TK-18 |
| D-8 | Valores do LLM no `url_template` | Sempre codificados com `quote(safe="")`. Exceção: template que começa pelo placeholder (`{base_url}...`), cujo primeiro placeholder não é codificado | TK-15 |
| D-9 | `load_discovered_tools` e `load_mcp_tools` | Passam a acrescentar às tools autorizadas, sem substituir | TK-65 |
| D-10 | Budget de compactação do loop | Novo `KernelConfig.loop_context_budget`, padrão 16000 (valor atual), repassado ao `ChatRunner` | TK-20 |
| D-11 | Score de sessão | Continua vindo do último trace da sessão, mas lido da tabela `execution_traces` no fechamento | TK-26 |
| D-12 | Retenção de mensagens e traces | Mecanismo novo, desligado por padrão | TK-73 |
| D-13 | `PRAGMA synchronous` | `NORMAL` | TK-74 |
| D-14 | Configuração do ruff | `ruff.toml` continua sendo a fonte única; o bloco `[tool.ruff]` morto sai do `pyproject.toml` | TK-81 |
| D-15 | Arquivos soltos na raiz (`clark-*.txt`, `harness_plan.md`, `env/`) | Não mover. Só tirar do git `symbiote.db`, `.coverage` e `.context/` | TK-80 |
| D-16 | `USER` não-root no `Dockerfile` | Não alterar: muda permissões do volume de staging. Registrar no `BACKLOG.md` | TK-80 |
| D-17 | Streaming na API HTTP | Recurso novo, fora deste plano. Registrar no `BACKLOG.md` | TK-68 |
| D-18 | mypy | Gate sem `strict` primeiro; `strict` só se couber | TK-83 |

## 6. Mudanças incompatíveis previstas

O Symbiote é embutido em outros produtos (o changelog cita YouNews, SymTalk e ChopChop). Estas mudanças alteram comportamento visível para hosts. Cada uma precisa de nota `**MIGRAÇÃO:**` no `CHANGELOG.md` e de atualização em `docs/HOST_INTEGRATION.md`.

| Tarefa | O que muda para o host |
|---|---|
| TK-10 | `MemoryStore.search()` e `get_relevant()` exigem `symbiote_id`. Quem implementa `MemoryPort` precisa acompanhar a assinatura |
| TK-11 | Rotas de sessão, memória, tools e `/api/*` passam a exigir chave. Symbiotes sem dono ficam visíveis só para admin |
| TK-15 | Valores com `/`, `?` ou `&` em placeholders de path deixam de alterar a URL |
| TK-18 | Com `fs_allowed_root` definido, paths fora dele são recusados |
| TK-23 | Loop que estoura o limite devolve texto de resposta em vez de string vazia |
| TK-41 | `POST /sessions/{id}/close` passa a rodar reflexão, score, review de skills e dream; fica mais lento |
| TK-42 | A reflexão por palavra-chave deixa de gravar mensagens do assistant e passa a reconhecer termos em português |
| TK-44 | `(symbiote_id, external_key)` passa a ser único para sessões |
| TK-65 | `load_*_tools` deixam de apagar autorizações anteriores |

## 7. Quando parar e perguntar ao operador

Pare, descreva o que encontrou e espere instrução quando:

- a suíte estiver vermelha por motivo não relacionado à sua tarefa;
- uma correção exigir mudança incompatível fora da seção 6, ou contrariar uma decisão da seção 5;
- uma tarefa passar de duas vezes o tamanho estimado;
- for preciso credencial, rede, LLM real, porta nova, ou qualquer coisa em staging, produção ou no repositório do devops;
- o teste de regressão não puder ser escrito sem acesso que você não tem.

Não pare por dúvida de estilo ou de nome: escolha o que combina com o código vizinho e siga.

## 8. Tarefas

### Visão geral

| ID | Tarefa | Relatório | Prior. | Tam. | Depende | Status |
|---|---|---|---|---|---|---|
| **Fase 0 — Preparação** | | | | | | |
| TK-00 | Branch e linha de base | — | P0 | XS | — | done |
| TK-01 | Suporte de teste compartilhado | Q-2 | P0 | S | TK-00 | done |
| **Fase 1 — Isolamento e exposição** | | | | | | |
| TK-10 | Isolar memória por symbiote | S-1 | P0 | M | TK-01 | done |
| TK-11 | Autenticação global e checagem de dono na API | S-2 | P0 | L | TK-01 | done |
| TK-12 | Entrada inválida devolve 4xx | A-1 | P1 | S | TK-11 | done |
| TK-13 | Limites de tamanho | S-10 | P1 | S | TK-11 | done |
| TK-14 | SSRF: IPv6 mapeado e endereços não globais | S-3 | P0 | S | TK-01 | done |
| TK-15 | URL-encoding dos parâmetros de tool | S-7 | P0 | S | TK-01 | done |
| TK-16 | Redirect sem credenciais entre origens | S-8 | P1 | S | TK-14 | done |
| TK-17 | Escape de atributos no Console | S-6 | P0 | S | — | done |
| TK-18 | Tools de filesystem e `bash` | S-9 | P1 | M | TK-01 | done |
| TK-19 | Permissão do arquivo de config da CLI | S-11 | P2 | XS | — | done |
| **Fase 2 — Caminho de execução** | | | | | | |
| TK-20 | Compactação preserva a última troca | E-1 | P0 | M | TK-01 | done |
| TK-21 | Handlers async em todos os caminhos | E-5 | P0 | M | TK-01 | done |
| TK-22 | Timeout de tool que devolve o controle | E-4 | P1 | M | TK-21 | done |
| TK-23 | Loop esgotado devolve resposta | E-6 | P1 | M | TK-20 | done |
| TK-24 | Parser de tool call que não levanta | E-7 | P1 | S | TK-01 | done |
| TK-25 | Validador sem falsos positivos | E-8 | P1 | S | TK-01 | done |
| TK-26 | Trace e handoff por sessão | E-3 | P0 | M | TK-01 | done |
| TK-27 | Disparo dos hooks | E-2 | P1 | M | TK-21 | done |
| TK-28 | Ligar e corrigir o `LongRunRunner` | E-2 | P1 | L | TK-26 | done |
| TK-29 | `ProcessEngine` e `ProcessRunner` | E-2 | P2 | S | TK-01 | done |
| TK-30 | LLM fora do event loop em `message_async` | E-9 | P1 | S | TK-23 | done |
| TK-31 | Classificação de erro retentável | E-10 | P1 | S | TK-30 | done |
| TK-32 | Divergências pontuais sync/async | E-11 | P1 | M | TK-31 | done |
| TK-33 | Compactação em cascata mantém o resumo | E-12 | P2 | S | TK-20 | done |
| TK-34 | Profundidade de subagente por cadeia | E-12 | P2 | S | TK-01 | done |
| TK-35 | Mensagem órfã e retry do bus | E-12 | P2 | M | TK-01 | done |
| **Fase 3 — Memória e aprendizado** | | | | | | |
| TK-40 | Recall por termos | M-1 | P0 | M | TK-10 | done |
| TK-41 | Fechamento de sessão pela API usa o kernel | M-2 | P0 | S | TK-11 | done |
| TK-42 | Reflexão por palavra-chave e fechamento idempotente | M-3 | P0 | M | TK-10 | done |
| TK-43 | Fato malformado não derruba o fechamento | M-4 | P1 | S | TK-42 | done |
| TK-44 | Sessão única por `external_key` | M-10 | P1 | S | TK-01 | done |
| TK-45 | Reconcile do dream sem perda de memória | M-5 | P1 | M | TK-10 | done |
| TK-46 | Ciclo de vida do dream | M-6 | P1 | M | TK-45 | done |
| TK-47 | Evolver e rollback do harness | M-7 | P1 | M | TK-26 | done |
| TK-48 | Identidade de skill entre store e loader | M-8 | P1 | M | TK-01 | done |
| TK-49 | Quarentena de skills | S-4 | P0 | M | TK-48 | done |
| TK-50 | `shutdown()` espera as threads | M-9 | P1 | S | TK-46 | done |
| TK-51 | Reaprovação de tool descoberta | S-5 | P0 | M | TK-01 | done |
| **Fase 4 — Tools, integrações e API** | | | | | | |
| TK-60 | Inicialização única da API | A-2 | P1 | M | TK-11 | done |
| TK-61 | Tools persistidas e um só gateway | T-1 | P1 | L | TK-60 | done |
| TK-62 | Discovery com query params e sem colisão | T-2 | P1 | M | TK-51 | done |
| TK-63 | Meta-tools dos modos `index` e `on_demand` | T-3 | P1 | S | TK-10 | done |
| TK-64 | `configure` atômico e zero como valor válido | T-4 | P1 | M | TK-01 | done |
| TK-65 | `load_*_tools` acrescentam | T-5 | P1 | XS | TK-64 | done |
| TK-66 | Handler de SIGTERM do browser | T-6 | P1 | S | — | done |
| TK-67 | Itens menores do browser | T-7 | P2 | S | TK-66 | done |
| TK-68 | Itens menores da API | A-3 | P2 | S | TK-60 | done |
| **Fase 5 — CLI, desempenho e documentação** | | | | | | |
| TK-70 | Escape de markup na CLI | C-1 | P1 | S | — | done |
| TK-71 | Erros de entrada da CLI | C-2, C-3 | P2 | S | TK-70 | done |
| TK-72 | Índices | P-1 | P1 | S | TK-44 | done |
| TK-73 | Retenção | P-2 | P2 | M | TK-72 | done |
| TK-74 | `synchronous=NORMAL` | P-3 | P2 | XS | — | done |
| TK-75 | Correções de documentação | D | P1 | M | fases 1 a 4 | done |
| TK-76 | Teste que mantém a documentação honesta | D | P2 | S | TK-75 | done |
| TK-77 | `INFRA.md` e `setup_env.sh` | D | P2 | XS | — | done |
| **Fase 6 — Qualidade e fechamento** | | | | | | |
| TK-80 | Higiene do repositório | Q-4 | P2 | XS | — | done |
| TK-81 | Configuração única do ruff | Q-1 | P2 | S | — | done |
| TK-82 | Gate de cobertura no CI | Q-1 | P1 | S | fases 1 a 5 | done |
| TK-83 | Gate de tipos no CI | Q-1 | P2 | L | TK-82 | done |
| TK-84 | Versão de schema | Q-5 | P2 | M | TK-72 | done |
| TK-85 | Conexão fixada no IP validado | S-11 | P2 | M | TK-14 | done |
| TK-86 | Refatorações estruturais | Q-3 | P2 | L | todas | done |
| TK-90 | Fechamento | — | P0 | S | todas | done |

Caminhos de "Repro" são relativos a `reports/2026-11-13_repro/`.

---

### Fase 0 — Preparação

#### TK-00 · Branch e linha de base
- Crie a branch `melhorias/2026-11-fase-1` a partir de `main`. A cada fase concluída, crie a próxima (`...-fase-2`) a partir da anterior.
- Rode os comandos da seção 3 e anote os números na seção 11. Se a suíte não der 1758 passed, pare.

#### TK-01 · Suporte de teste compartilhado (Q-2)
- **Por quê:** cinco arquivos de teste definem cada um seu LLM falso, e nenhum teste de API roda sem `SYMBIOTE_DEV_MODE`.
- **Crie** `tests/support/__init__.py` com:
  - `ScriptedLLM(fn)`: implementa `complete(messages, config=None, tools=None)` chamando `fn(indice, messages)` e guarda as chamadas. Base: `FakeLLM` em `execucao/common.py`.
  - `tool_call_block(tool, **params)`: gera o bloco ```` ```tool_call ````.
  - `make_kernel(tmp_path, llm=None, **cfg)`: `SymbioteKernel` com banco e `skills_root` dentro de `tmp_path`.
  - `add_tool(kernel, symbiote_id, tool_id, handler, **configure_kwargs)`: registra com `tool_gateway.register_descriptor(descriptor, handler)` e autoriza com `environment.configure`.
  - fixture `api_client(tmp_path, monkeypatch)`: remove `SYMBIOTE_DEV_MODE`, aponta `SYMBIOTE_DB_PATH` para `tmp_path`, zera as variáveis de módulo citadas na seção 3 e devolve `(TestClient, admin_key)`. Aceita um LLM opcional para injetar no kernel.
- **Pronto quando:** um teste mínimo usa cada helper e a suíte segue verde. Não migre os testes antigos.

---

### Fase 1 — Isolamento e exposição

#### TK-10 · Isolar memória por symbiote (S-1)
- **Repro:** `verify_mem.py`, `memoria/r1_isolation.py`, `memoria/r9_misc.py` (bloco A), `tools/t_gateway.py`.
- **Teste primeiro:** `tests/unit/test_memory_isolation.py`:
  - dois symbiotes; segredo na memória de A; B manda um trecho do segredo e depois `%`; o prompt de B (capturado pelo `ScriptedLLM`) não contém o segredo;
  - `search_memories` executada por B não devolve memória de A;
  - `search_knowledge` chamada por B com `symbiote_id` de A nos parâmetros devolve só conhecimento de B;
  - `update` e `deactivate` com id de memória de outro symbiote não alteram nada;
  - reflexão LLM de B com `target_id` de memória de A não a sobrescreve.
- **Implementação:**
  - `src/symbiote/memory/store.py`: `search(query, *, symbiote_id, ...)` e `get_relevant(intent, *, symbiote_id, ...)` com `AND symbiote_id = ?`. `get`, `update` e `deactivate` aceitam `symbiote_id` e filtram por ele. Escape `%`, `_` e `\` no `LIKE` com `ESCAPE '\'`. O `last_used_at` só é atualizado para memórias do próprio symbiote.
  - `src/symbiote/core/ports.py:45-57`: atualize `MemoryPort`.
  - Chamadores: `core/context.py:188`, `core/capabilities.py:94` e `:233`, `environment/tools.py:569`, `api/http.py:714`, `cli/main.py:457`, `core/reflection.py:418-420`.
  - Handlers builtin (`environment/tools.py:562-586`): o `ToolGateway` passa a definir um `contextvars.ContextVar` com `symbiote_id` e `session_id` em `execute`, `execute_async` e nas variantes em lote, antes de chamar o `PolicyGate` (que já copia o contexto para a thread: `environment/policies.py:88-91`; confirme o caminho async). Os handlers leem dali e ignoram `symbiote_id` vindo dos parâmetros.
- **Pronto quando:** os cinco casos passam e nenhum caminho de leitura ou escrita de memória aceita operar sem symbiote.

#### TK-11 · Autenticação global e checagem de dono na API (S-2)
- **Repro:** `api/live1.py` (precisa de servidor; use como roteiro), `api/probe_inicial.py`.
- **Teste primeiro:** `tests/unit/test_api_authorization.py`, com a fixture `api_client`:
  - **teste de varredura:** percorre `app.routes`, e toda rota fora da allowlist de D-3 responde 401 sem chave. É este teste que impede uma rota nova de nascer sem autenticação;
  - com chave do tenant B, toda rota que recebe `session_id` ou `symbiote_id` de A responde 403 (inclua sessões, mensagens, close, tools, exec, `/memory/search`);
  - rotas `/api/*` respondem 403 para chave `user` e 200 para admin;
  - symbiote com `owner_id` nulo: 403 para `user`, 200 para admin;
  - symbiote apagado: `POST /sessions` responde 404 e as mensagens das sessões dele não são mais servidas.
- **Implementação:**
  - `src/symbiote/api/http.py`: dependência global no `FastAPI(...)` que valida a chave para todo path fora de `_PUBLIC_PATHS` (`api/middleware.py:20`, hoje sem uso; acrescente `/`).
  - Dois helpers, `_require_symbiote_access(symbiote_id, auth)` e `_require_session_access(session_id, auth)`, com a regra: admin passa; dono passa; `owner_id` nulo só admin. Use-os em todas as rotas. O padrão de consulta já existe em `http.py:1170-1179`.
  - `/memory/search` passa a exigir `symbiote_id` na query (depende da TK-10).
  - `/api/*` usam `require_admin`. O `api()` do Console já envia a chave (`api/dashboard.html:631-644`); garanta que um 401 vira mensagem pedindo a chave, não tela vazia.
  - `POST /sessions` valida que o symbiote existe, está ativo e pertence ao chamador.
- **Cuidado:** os testes existentes rodam com `SYMBIOTE_DEV_MODE=1`, que devolve admin; não devem quebrar. O SDK (`sdk/client.py:40`) já envia a chave em tudo.
- **Pronto quando:** a varredura passa e o roteiro de `api/live1.py`, refeito com `TestClient`, só obtém 401 ou 403 onde hoje obtém dados.

#### TK-12 · Entrada inválida devolve 4xx (A-1)
- **Repro:** bloco "erros e entradas ruins" de `api/live1.py`.
- **Teste primeiro:** `tests/unit/test_api_input_errors.py`: os sete casos do relatório devolvem 422 (ou 404 para symbiote inexistente), nunca 500.
- **Implementação:**
  - Tipos `Literal` e limites nos modelos de request (`role`, `tool_mode`, `tool_loading`, `context_mode`, faixas numéricas).
  - `ValueError` levantado por `EnvironmentManager.configure` (`environment/manager.py:118`) e por `APIKeyManager.create_key` (`api/auth.py:75`) passa a ser `symbiote.core.exceptions.ValidationError`, que já tem handler.
  - Handlers para `pydantic.ValidationError` (422) e `sqlite3.IntegrityError` (409). Não crie handler genérico para `ValueError`: esconderia bugs.

#### TK-13 · Limites de tamanho (S-10)
- **Repro:** bloco "corpo grande sem limite" de `api/live2.py`; `tools/t_http.py` (resposta de 52 MB).
- **Teste primeiro:** `tests/unit/test_size_limits.py`: mensagem acima do limite devolve 413 ou 422; nome de symbiote com 1 MB é recusado; resposta de HTTP tool acima do teto vira erro de tool, sem ler tudo.
- **Implementação:** `max_length` nos campos de texto dos requests; limite de corpo por middleware lendo `Content-Length`; leitura limitada em `environment/tools.py:742`. Valores sugeridos: 1 MB por mensagem, 200 caracteres para nomes, 5 MB por resposta de tool. Deixe-os em constantes nomeadas.

#### TK-14 · SSRF: IPv6 mapeado e endereços não globais (S-3)
- **Repro:** `verify_tools.py`, `tools/t_http.py`.
- **Teste primeiro:** acrescente a `tests/unit/` os casos `[::ffff:127.0.0.1]`, `[::ffff:169.254.169.254]`, `[::ffff:10.0.0.5]`, `[::]` e um redirect 302 para `[::ffff:127.0.0.1]`; todos bloqueados. Mantenha os casos que já passam.
- **Implementação:** `src/symbiote/security/network.py:61-72`: `ip = ip.ipv4_mapped or ip` e bloqueie `not ip.is_global`. A lista manual `_BLOCKED_NETWORKS` pode ficar como segunda barreira.

#### TK-15 · URL-encoding dos parâmetros de tool (S-7)
- **Repro:** `verify_tools.py`, `tools/t_http.py`.
- **Teste primeiro:** servidor local em porta `0`; `id="../../admin/users"` e `id="42?as_admin=1"` chegam como um único segmento codificado; `q="python dataclasses"` e `q="preço"` funcionam; `archived=False` e `offset=0` continuam na URL.
- **Implementação:** `src/symbiote/environment/tools.py:610-638`: `urllib.parse.quote(str(v), safe="")` em cada valor, com a exceção de D-8. Corrija na mesma função o descarte de valores falsy válidos (`tools.py:620`).

#### TK-16 · Redirect sem credenciais entre origens (S-8)
- **Repro:** `tools/t_http.py`.
- **Teste primeiro:** dois servidores locais; A redireciona para B; B não recebe `Authorization`.
- **Implementação:** `environment/tools.py:734-741`: no `redirect_request`, se esquema, host ou porta mudarem, remova `Authorization`, `Cookie` e todo header vindo do `header_factory`.

#### TK-17 · Escape de atributos no Console (S-6)
- **Repro:** `api/console_escape.html` no Chromium headless.
- **Teste primeiro:** `tests/unit/test_dashboard_escape.py`, estático: falha se `dashboard.html` tiver `onclick="` contendo `${`, e verifica que a função `esc` trata `"` e `'`. Se houver Chromium disponível, acrescente o teste de DOM marcado como `integration`.
- **Implementação:** `src/symbiote/api/dashboard.html:595-600`: `esc()` passa a escapar `&`, `<`, `>`, `"` e `'` por substituição de string. Troque o `onclick` inline da linha 834 por `data-*` mais `addEventListener`.

#### TK-18 · Tools de filesystem e `bash` (S-9)
- **Repro:** `api/probe_inicial.py` (itens 11 e 12), `tools/t_misc.py`.
- **Teste primeiro:** com `fs_allowed_root` definido, `fs_read` fora da raiz falha; `ws-evil` com raiz `ws` falha; symlink que sai da raiz falha; `allowed_root` nos parâmetros não amplia. Em `bash`: processo filho morre no timeout; `_cwd` fora da raiz é recusado.
- **Implementação:** novo campo `KernelConfig.fs_allowed_root: Path | None = None` (D-7), repassado ao registro dos builtins. `_validate_path` (`environment/tools.py:761-773`) usa `Path.resolve()` e `is_relative_to`. `_bash` (`tools.py:796-818`) usa `start_new_session=True` e `os.killpg` no timeout, e limita `timeout` a um teto. Sem `fs_allowed_root`, o comportamento atual se mantém e um aviso é logado uma vez.

#### TK-19 · Permissão do arquivo de config da CLI (S-11)
- `src/symbiote/cli/main.py:658-667`: grave `.symbiote/config` com modo `0600` e trate `%` no valor (use `RawConfigParser` ou escape). Teste: modo do arquivo e ida e volta de uma chave com `%`.

---

### Fase 2 — Caminho de execução

#### TK-20 · Compactação preserva a última troca (E-1)
- **Repro:** `verify_exec.py` (item 5), `execucao/t04_autocompact.py`, `execucao/t04b_autocompact_big_result.py`, `execucao/t18_budget.py`.
- **Teste primeiro:** `tests/unit/test_autocompact_regression.py`: persona de 60 mil caracteres, tool que devolve um marcador; o modelo vê o marcador na segunda chamada, a tool executa uma vez e a resposta contém o marcador. Segundo caso: resultado de tool grande com `tool_result_max_chars` alto.
- **Implementação:**
  - `src/symbiote/runners/chat.py:1378-1460`: o layer 3 nunca remove a última troca (a mensagem do assistant com a chamada e os resultados que a seguem; o layer 2 já delimita isso em `chat.py:1320-1322`). Se não houver passo anterior para compactar, não faça nada. Se a última troca sozinha estourar, trunque o resultado em vez de removê-lo.
  - Novo `KernelConfig.loop_context_budget` (D-10), repassado em `core/kernel.py:123-134`. Corrija o comentário de `chat.py:58`.
  - `core/context.py:597-612`: `_total_tokens` passa a contar schemas de tools e `extra_context`.

#### TK-21 · Handlers async em todos os caminhos (E-5)
- **Repro:** `verify_exec.py` (item 4), `execucao/t20_instant_async_tool.py`, `tools/t_gateway.py`, `tools/t_browser.py`.
- **Teste primeiro:** `tests/unit/test_async_handlers.py`: handler `async def` executado por `kernel.message` (sync), por `message_async` com `tool_mode="brief"` e com `tool_mode="instant"`; nos três o resultado real chega ao modelo e nenhum `repr` de coroutine aparece. Dois handlers async seguidos pelo caminho sync compartilham estado (simule um objeto criado na primeira chamada e usado na segunda).
- **Implementação:**
  - O `ToolGateway` passa a ter um event loop próprio numa thread daemon, criado sob demanda. No caminho sync, se o handler for coroutine function ou devolver awaitable, submeta com `asyncio.run_coroutine_threadsafe` e espere com o timeout da tool. Um loop por chamada (`asyncio.run`) não serve: as sessões do Playwright ficam presas ao loop que as criou.
  - `chat.py:742-743`: o instant dentro de `run_async` usa `execute_tool_calls_async`.
  - Encerre o loop em `kernel.shutdown()`.
  - Documente em `docs/HOST_INTEGRATION.md` que sessões de browser não devem ser compartilhadas entre `message()` e `message_async()`.

#### TK-22 · Timeout de tool que devolve o controle (E-4)
- **Repro:** `api/probe_inicial.py` (item 10), `tools/t_gateway.py`.
- **Teste primeiro:** timeout de 0,2 s com tool de 2 s retorna em menos de 1 s, nos caminhos sync, async e paralelo. No paralelo, três chamadas de 0,9 s com timeout de 1,5 s terminam todas com sucesso. Ajuste `tests/unit/test_timeout.py::TestToolTimeoutSync::test_slow_tool_times_out` para afirmar o tempo decorrido.
- **Implementação:** `environment/policies.py:91-94`: executor de vida longa no `PolicyGate`, sem `with`; no timeout devolva o erro e logue um aviso com o `tool_id` (a thread não pode ser morta; diga isso no docstring). `environment/tools.py:404-429`: o prazo de cada chamada paralela conta da submissão, não em cascata. Feche o executor em `kernel.shutdown()`.

#### TK-23 · Loop esgotado devolve resposta (E-6)
- **Repro:** `verify_exec.py` (item 6), `execucao/t09_max_iters.py`, `execucao/t19_hooks.py` (o `KeyError`).
- **Teste primeiro:** com `max_tool_iterations=3` e modelo que sempre pede tool, a resposta final não é vazia; 12 chamadas paralelas numa rodada com limite 10 não estouram o limite; no timeout, `total_iterations` bate com o número de chamadas ao LLM; override de mensagem de injeção contendo `{tool}` não levanta.
- **Implementação:** `runners/loop_control.py:66` e `:83`: conte rodadas, não tool calls. `chat.py:630-633` e `:693-715` (e os equivalentes async): ao sair por `max_iterations`, faça uma chamada final sem tools, como a estagnação já faz em `chat.py:644-654`; no timeout, devolva um texto fixo não vazio. `loop_control.py:128`: formatação tolerante a chaves desconhecidas.

#### TK-24 · Parser de tool call que não levanta (E-7)
- **Repro:** `verify_exec.py` (item 2), `execucao/t11_parser.py`, `execucao/t21_parser_fence.py`, `execucao/t17_forge.py`.
- **Teste primeiro:** bloco com lista de chamadas é aceito como várias chamadas; `params` nulo ou string não levanta; parâmetro contendo uma cerca ``` é extraído inteiro; argumentos nativos com JSON truncado não executam a tool; `content=None` do adapter não quebra.
- **Implementação:** `src/symbiote/environment/parser.py:11-14` e `:56-62`: extraia o JSON com `json.JSONDecoder().raw_decode` a partir da cerca de abertura, aceite lista, descarte entradas inválidas sem levantar. `src/symbiote/adapters/llm/forge.py:312-329`: argumento inválido vira resultado de erro para o modelo, não `{}`. `forge.py:101`: `content or ""`.

#### TK-25 · Validador sem falsos positivos (E-8)
- **Repro:** `verify_exec.py` (item 1), `execucao/t10_validator.py`.
- **Teste primeiro:** citações `[1]`, link markdown no início da linha, checklist `[x]` e bloco ```` ```json ```` pedido pelo usuário são válidos; um bloco `tool_call` vazado continua inválido. No fluxo completo, uma resposta com citações custa uma chamada de LLM e chega intacta.
- **Implementação:** `runners/response_validator.py:13-47`: só invalide quando o trecho decodifica como JSON com chave `tool` ou `params`, ou quando sobrou cerca `tool_call`. `chat.py:1254-1296`: se os retries não melhorarem, devolva o texto original em vez do pedido de desculpas.

#### TK-26 · Trace e handoff por sessão (E-3)
- **Repro:** `execucao/t02_trace_race.py`, `execucao/t03_trace_seq.py`, `memoria/r3_trace.py`.
- **Teste primeiro:** `tests/unit/test_trace_per_session.py`:
  - sequencial: A termina em estagnação, B manda mensagem, `close_session(A)` grava o score de estagnação e gera a memória de falha;
  - concorrente: 8 threads, uma sessão cada, metade com tool; todo trace em `execution_traces` pertence à sessão que o gerou e nenhum se perde;
  - um run que falha ainda persiste o trace.
- **Implementação:** `core/capabilities.py:143-144` e `:182-183`: acrescente um método interno que devolve o `RunResult` completo e faça o kernel usá-lo; mantenha `chat()` e as propriedades `last_*` para compatibilidade. `core/kernel.py:164-169`, `:478-488`, `:555-559`: remova os slots `_last_trace` e `_last_handoff`; guarde o handoff por `session_id` e, no fechamento, leia o último trace da sessão em `execution_traces` (D-11). Persista o trace antes de propagar `CapabilityError`.

#### TK-27 · Disparo dos hooks (E-2, D-1)
- **Repro:** `verify_exec.py` (item 3), `execucao/t19_hooks.py`.
- **Teste primeiro:** um hook registrado em `kernel.hooks` recebe `before_tool` e `after_tool` em `message()` e em `message_async()`, e os eventos de turno uma vez por mensagem. Um hook que levanta não derruba o turno.
- **Implementação:** leia `src/symbiote/core/hooks.py` para as assinaturas. Dispare os eventos de tool no `ToolGateway` e os de turno em `kernel.message*`. Os métodos do hook são `async`: no caminho sync use o loop da TK-21.

#### TK-28 · Ligar e corrigir o `LongRunRunner` (E-2, D-2)
- **Repro:** `execucao/t14_longrun.py`.
- **Teste primeiro:** `tool_mode="long_run"` com `planner_prompt` chama o planner e produz handoff; avaliador que reprova resulta em `success=False`; planner funciona com adapter que devolve `LLMResponse`; `llm_config` é aceito.
- **Implementação:** registre o runner em `core/kernel.py:119-138` e roteie por `tool_mode`. Em `src/symbiote/runners/long_run.py` (linhas citadas no relatório: `:129`, `:159`, `:210-230`, `:242`, `:296`, `:338`, `:520`):
  - o veredito do avaliador passa a valer;
  - extraia o texto de `LLMResponse`;
  - aceite `llm_config`;
  - corrija `_preflight_tools`;
  - use `accumulated_messages` ou remova `context_strategy`;
  - indexe os blocos pendentes por identidade, não por contagem.

#### TK-29 · `ProcessEngine` e `ProcessRunner` (E-2)
- **Repro:** `execucao/t13_process.py`.
- **Teste primeiro:** depois de recriar o `ProcessEngine` sobre o mesmo banco, `advance()` continua do passo persistido; `work("research_task: tema")` encontra o processo.
- **Implementação:** `process/engine.py:150`: derive o índice de `current_step`. `runners/process.py:24-25`: use o intent já extraído.

#### TK-30 · LLM fora do event loop em `message_async` (E-9)
- **Repro:** `execucao/t06_async_blocks.py`.
- **Teste primeiro:** quatro sessões em `asyncio.gather` com LLM de 0,3 s terminam em menos de 0,8 s, e uma task de heartbeat não fica mais de 0,1 s sem rodar.
- **Implementação:** `chat.py:777` e `:922`: `await asyncio.to_thread(self._call_llm_with_retry, ...)`. Verifique onde `on_token` é chamado: se passar a rodar na thread de trabalho, encaminhe com `loop.call_soon_threadsafe`.

#### TK-31 · Classificação de erro retentável (E-10)
- **Repro:** `execucao/t16_retry.py`.
- **Teste primeiro:** erro de conexão, 500, 503 e 504 vindos do adapter são retentados; um 400 cuja mensagem contém "timeout" ou "503" não é.
- **Implementação:** `LLMError` ganha `status_code` e `retryable`, preenchidos em `adapters/llm/forge.py:105` e `:149` a partir da exceção original. `chat.py:1196-1211` decide por esses campos. Confirme no forge_llm se `max_retries=0` (`forge.py:52`) desliga o retry de 429 e, se desligar, trate 429 aqui.

#### TK-32 · Divergências pontuais sync/async (E-11)
- **Repro:** `execucao/t07_sync_async_div.py`, `execucao/t07b_nudge.py`, `execucao/t08_instant_leak.py`.
- **Teste primeiro:** um teste parametrizado roda o mesmo cenário por `run()` e `run_async()` e compara texto, `stop_reason` e número de chamadas, para: `on_after_tool_result`, nudge de review de skills, `max_tool_iterations=0`, e o que `on_token` recebe no modo instant.
- **Implementação:** correções pontuais em `chat.py:404-409`, `:613-627`, `:876-896`, `:958`. A unificação dos dois caminhos fica para a TK-86; o teste parametrizado desta tarefa é a rede de segurança dela.

#### TK-33 · Compactação em cascata mantém o resumo (E-12)
- **Repro:** `execucao/t05_compaction.py`.
- **Teste primeiro:** loop de 8 passos; na última chamada o modelo ainda vê referência aos passos 0 a 2, nos modos texto e nativo.
- **Implementação:** `chat.py:1326-1362`: reconheça a mensagem de resumo anterior por um marcador e leve suas linhas para o novo resumo.

#### TK-34 · Profundidade de subagente por cadeia (E-12)
- **Repro:** `execucao/t15_subagent.py`.
- **Teste primeiro:** cinco `spawn` paralelos de profundidade 1 funcionam; aninhamento real acima do limite é recusado; spawn que falha fecha a sub-sessão.
- **Implementação:** `runners/subagent.py:96-248`: profundidade em `ContextVar`; `close_session` em `finally`; aplique ou remova `_BLOCKED_TOOLS` (`subagent.py:74`).

#### TK-35 · Mensagem órfã e retry do bus (E-12)
- **Repro:** `execucao/t12_bus.py`.
- **Teste primeiro:** `kernel.message` que falha não deixa mensagem `user` sem resposta na sessão; o bus loga a exceção e publica um outbound de erro quando esgota os retries.
- **Implementação:** `core/kernel.py:462` e `:536`: remova a mensagem `user` recém-inserida se o run falhar (compensação, depois propague). `bus/message_bus.py:173-206`: `exc_info=True` e outbound de erro.

---

### Fase 3 — Memória e aprendizado

#### TK-40 · Recall por termos (M-1)
- **Repro:** `verify_mem.py`, `memoria/r1b.py`.
- **Teste primeiro:** com a memória "User prefers the vim editor", a pergunta "Which editor do I prefer?" a traz para o prompt. Equivalente em português, com acento: memória "Usuário prefere o editor vim", pergunta "Qual é o meu editor preferido?".
- **Implementação:** `memory/store.py:104-123`: `get_relevant` delega ao recall tokenizado de `memory/recall.py`, filtrando por symbiote. Ajuste o tokenizer para letras acentuadas e acrescente stop-words em português.

#### TK-41 · Fechamento de sessão pela API usa o kernel (M-2)
- **Repro:** `api/close_path.py`.
- **Teste primeiro:** depois de três mensagens e `POST /sessions/{id}/close`, existem linhas em `session_scores` e memórias de reflexão, iguais às de `kernel.close_session`.
- **Implementação:** `api/http.py:685-701` chama `kernel.close_session`, com a checagem de dono da TK-11.

#### TK-42 · Reflexão por palavra-chave e fechamento idempotente (M-3)
- **Repro:** `api/close_path.py` (frases em português), `memoria/r2_reflection.py`.
- **Teste primeiro:**
  - frases em português com "prefiro", "sempre", "nunca" e "decidi" geram memórias;
  - uma resposta do assistant contendo "always" não gera;
  - fechar quatro vezes uma sessão reaberta por `external_key` não duplica memórias e deixa uma linha em `session_scores`;
  - `kernel.get_session()` não reabre uma sessão fechada.
- **Implementação:** `core/reflection.py:55-56`, `:153-171`, `:276-295`: só mensagens `user`, fronteira de palavra, termos em português, limite de tamanho, e uma marca por sessão da última mensagem refletida. `core/kernel.py:1195-1216`: `UNIQUE(session_id)` em `session_scores` com upsert. `core/session.py:103-110`: leitura sem efeito colateral para `get_session`; a reabertura fica só em `get_or_create_by_external_key`.

#### TK-43 · Fato malformado não derruba o fechamento (M-4)
- **Repro:** `memoria/r2a.py`.
- **Teste primeiro:** em `reflection_mode=llm`, um lote com um item de `importance` inválida grava os itens válidos, registra o erro no audit e fecha a sessão.
- **Implementação:** `core/reflection.py:550` e `:586-596`: valide e limite cada fato, descartando o inválido. `core/kernel.py:571-607`: os passos 1 a 6 de `close_session` ficam em `try`, com o fechamento num `finally` e o erro logado.

#### TK-44 · Sessão única por `external_key` (M-10)
- **Repro:** `verify_mem.py`, `memoria/r9_misc.py` (bloco B).
- **Teste primeiro:** 8 threads com a mesma chave nova criam uma sessão, em 20 rodadas.
- **Implementação:** `core/session.py:54-81`: lock em processo por `(symbiote_id, external_key)` em volta do buscar-ou-criar. `adapters/storage/sqlite.py:33`: índice `UNIQUE(symbiote_id, external_key) WHERE external_key IS NOT NULL`. Bancos existentes podem ter duplicatas: se a criação do índice falhar, logue um aviso com a contagem e siga só com o lock. Não apague nem altere sessões na migração.

#### TK-45 · Reconcile do dream sem perda de memória (M-5)
- **Repro:** `memoria/r4_dream.py` (bloco A).
- **Teste primeiro:** quatro `constraint` distintas com as mesmas tags continuam ativas depois de `kernel.dream()` no modo `light`; duas quase-duplicatas são reconciliadas.
- **Implementação:** `dream/phases.py:249-285`: no modo determinístico, só reconcilie pares com similaridade alta; respeite `_PROTECTED_TYPES` (`phases.py:30`); pule memórias já desativadas no laço; não reaplique a tag `dream:reconciled`.

#### TK-46 · Ciclo de vida do dream (M-6)
- **Repro:** `memoria/r4_dream.py` (blocos B, C, E), `memoria/r4d.py`.
- **Teste primeiro:** em `per_symbiote`, quatro closes com LLM lento disparam no máximo um dream por symbiote ao mesmo tempo; em `global`, cada symbiote usa o próprio `dream_min_sessions`; `dream(dry_run=True)` não altera `should_dream`; dois ciclos seguidos não criam duas memórias generalizadas do mesmo cluster.
- **Implementação:** `core/kernel.py:946-969`: registro de threads de dream por symbiote no kernel, com lock em volta de checar e disparar; configuração passada por chamada. `dream/engine.py:65-67`: `_last_dream_at` ignora `dry_run`. `dream/phases.py:327-393`: pule clusters que já têm `dream:generalized`. Mova `_maybe_dream` para depois de `_sessions.close` (`kernel.py:600-607`).

#### TK-47 · Evolver e rollback do harness (M-7)
- **Repro:** `memoria/r6_harness.py`.
- **Teste primeiro:** os cinco cenários (a) a (e) do relatório, cada um como caso.
- **Implementação:** `harness/versions.py:152-157`: rollback para `parent_version`, numa transação. `harness/evolver.py:250-257`, `:338-360`: baseline do texto padrão como versão pontuada; `COUNT(DISTINCT session_id)`; aplique `MIN_EVOLUTION_INTERVAL_DAYS` (`evolver.py:41`) e um mínimo de sessões no pai. `harness/tuner.py:187-210`: `too_high` não sobrescreve `too_low`.

#### TK-48 · Identidade de skill entre store e loader (M-8)
- **Repro:** `memoria/r5_skills.py` (blocos B e D), `memoria/r5b.py`.
- **Teste primeiro:** create com `name:` diferente do diretório é recusado; create sem frontmatter é recusado; depois de `skill_manage` create, `skill_view` encontra a skill; `max_active_skills` barra a promoção acima do limite.
- **Implementação:** `skills/store.py:231-265`: exija frontmatter com `name` igual ao diretório. `skills/tool.py:138-202`: `loader.refresh()` após escrita. Conte a quarentena pelo disco. Aplique `max_active_skills` na promoção. Corrija o regex de nome que aceita newline final.

#### TK-49 · Quarentena de skills (S-4)
- **Repro:** `memoria/r5_skills.py` (blocos A e C), `memoria/r9_misc.py` (bloco C).
- **Teste primeiro:** três `skill_view` recusados não promovem a skill; o review em background não altera uma skill humana ativa (ou a rebaixa para quarentena); em `per_symbiote`, A não consegue alterar skill em `skills_extra_roots`.
- **Implementação:** `skills/tool.py:243-254`: cheque o status antes de `mark_used`; só carregamentos bem-sucedidos contam. `core/background_review.py:426-441`: em background, `edit` e `patch` só em skills `agent_created`. `skills/scope.py:129`: `extra_roots` somente leitura.

#### TK-50 · `shutdown()` espera as threads (M-9)
- **Repro:** `memoria/r7_shutdown.py` (usa `r7_child.py`).
- **Teste primeiro:** com review de 0,5 s em andamento, `shutdown()` espera, o audit é gravado e não há erro de banco fechado. Com `shutdown(timeout=0.1)`, retorna no prazo.
- **Implementação:** `core/kernel.py:1268-1270`: o kernel registra as threads que dispara (review, dream, consolidator) e faz `join` com timeout antes de fechar o storage, o executor da TK-22 e o loop da TK-21.

#### TK-51 · Reaprovação de tool descoberta (S-5)
- **Repro:** `tools/t_discovery.py`.
- **Teste primeiro:** tool aprovada cujo método, URL, parâmetros ou risco mudam numa nova descoberta volta para `pending`; tool inalterada continua aprovada; `operationId` igual ao de uma builtin é recusado; o loader só envia `header_factory` e `allow_internal` para a origem do `base_url`.
- **Implementação:** `discovery/repository.py:28-38`: no upsert, compare os campos e zere `status` e `approved_at` quando mudarem. `discovery/loader.py:95-104`: compare origem. `discovery/service.py:115-152`: valide a URL de descoberta e trate spec malformada sem propagar exceção. Atualize `tests/unit/test_discovery_service.py:131-144`, que hoje exige a preservação.

---

### Fase 4 — Tools, integrações e API

#### TK-60 · Inicialização única da API (A-2)
- **Repro:** `api/probe_inicial.py` (o 503 do item 1).
- **Teste primeiro:** num app recém-criado, a primeira requisição autenticada responde 200 ou 401, nunca 503; importar `symbiote.api.http` não altera o nível do logger raiz.
- **Implementação:** `api/http.py:64-68` e `:439-472`: um `lifespan` cria o kernel, e as dependências (`get_adapter`, `get_identity_manager`, ...) derivam de `kernel._storage`. Um único `APIKeyManager`. A configuração de logging sai do nível de módulo e vai para o `lifespan`, sem mexer no logger raiz quando o app é embutido. Mantenha os nomes das dependências: os testes usam `dependency_overrides`.

#### TK-61 · Tools persistidas e um só gateway (T-1)
- **Repro:** `api/probe_inicial.py` (item 9), `tools/t_docs.py` (CLI).
- **Teste primeiro:** tool registrada por `POST /symbiotes/{id}/tools` é usada pelo `/chat` (com `ScriptedLLM`); sobrevive à recriação do kernel sobre o mesmo banco; `symbiote tools add` seguido de `tools exec` em outro processo funciona; registrar um `tool_id` de builtin ou de outro dono devolve 409.
- **Implementação:** tabela nova com descriptor e `HttpToolConfig` por tool, com dono; carga na criação do kernel. A API usa `kernel.tool_gateway` e some o `_tool_gateway` de `http.py:499-507`. A CLI (`cli/main.py:521-528`) grava na mesma tabela. `allow_internal` nunca é aceito pela API. O registro é global por kernel: recuse colisão de `tool_id` entre donos.

#### TK-62 · Discovery com query params e sem colisão (T-2)
- **Repro:** `tools/t_discovery.py`.
- **Teste primeiro:** tool gerada de uma spec com parâmetros `in: query` envia a query; as rotas `/items`, `/items/{id}` e `/items/{id}/{rev}` geram três tools; `{file_path:path}` funciona.
- **Implementação:** `discovery/service.py:85-91`, `:145`, `:217`, `:386-391` e `discovery/loader.py:152-157`: guarde o `in` de cada parâmetro; monte a query com `urlencode` dos parâmetros não consumidos pelo path; inclua os path params no `tool_id`; registre colisões em `result.errors`.

#### TK-63 · Meta-tools dos modos `index` e `on_demand` (T-3)
- **Repro:** `tools/t_index.py`.
- **Teste primeiro:** em `loading="index"`, `get_tool_schema` funciona sem autorização manual e só devolve schema de tool autorizada para o symbiote; em `context_mode="on_demand"`, `search_memories` e `search_knowledge` funcionam.
- **Implementação:** `environment/tools.py:476-544`, `core/context.py:521-542`: o `PolicyGate` trata essas meta-tools como autorizadas quando o modo correspondente está ativo; `get_tool_schema` filtra pela lista autorizada e pelas tags.

#### TK-64 · `configure` atômico e zero como valor válido (T-4)
- **Repro:** `tools/t_misc.py`.
- **Teste primeiro:** 16 threads autorizando uma tool cada resultam em 16 tools e uma linha de config; `configure(memory_share=0.0)` é lido de volta como 0.0; `configure(workspace_id=..., prompt_caching=True)` não zera as tools.
- **Implementação:** `environment/manager.py:125-134`, `:238-334`, `:337-346`, `:589-590`: upsert numa transação com índice único em `(symbiote_id, COALESCE(workspace_id, ''))`; métodos `add_tools` e `remove_tools` atômicos; leitura que distingue zero de ausente. Bancos existentes podem ter linhas duplicadas: consolide na migração mantendo a mais recente e unindo as listas de tools.

#### TK-65 · `load_*_tools` acrescentam (T-5, D-9)
- `core/kernel.py:1055` e `:1079`: use `add_tools` da TK-64. Teste: autorizações anteriores sobrevivem a `load_discovered_tools` e `load_mcp_tools`.

#### TK-66 · Handler de SIGTERM do browser (T-6)
- **Repro:** `tools/t_browser.py`.
- **Teste primeiro:** em subprocesso, depois de `register(kernel, browser_backend="chromium")`, um SIGTERM encerra o processo; um handler que o host tinha instalado antes continua sendo chamado.
- **Implementação:** `browser/browser/supervisor.py:37-39` e `:59-61`: guarde o handler anterior e chame-o depois do cleanup; se era o padrão, restaure `SIG_DFL` e reenvie o sinal.

#### TK-67 · Itens menores do browser (T-7)
- **Repro:** `tools/t_browser.py`.
- A `policy` passa a valer para `web_extract` e `web_crawl` (`browser/register.py:85-93`); normalize ponto final em domínios (`browser/hooks/website_policy.py:113`); troque a idempotência por `id(kernel)` por um atributo no próprio kernel (`register.py:31`, `:65`, `:104`); `register()` falha cedo quando o extra do backend pedido não está instalado; `web_crawl` aceita domínio sem esquema (`browser/search/tools.py:165`). Um teste por item.

#### TK-68 · Itens menores da API (A-3)
- Chat sem LLM configurado responde 503 dizendo para definir `SYMBIOTE_LLM_PROVIDER`.
- `/health` resolve o commit uma vez, na subida.
- Nome de symbiote vazio é recusado.
- `generation_settings` é repassado ao kernel ou removido do SDK (`api/http.py:1181-1185`).
- Registre streaming no `BACKLOG.md` (D-17).

---

### Fase 5 — CLI, desempenho e documentação

#### TK-70 · Escape de markup na CLI (C-1)
- **Repro:** `tools/t_docs.py`, `tools/t_misc.py`.
- **Teste primeiro:** com `CliRunner`: memória contendo `[/tmp]` e mensagem contendo `[/INST]`; `memory search`, `export session`, `reflect` e `session close` saem com código 0; `learn` mostra `[preference, importance=0.9]`.
- **Implementação:** `rich.markup.escape()` em todo texto dinâmico de `cli/main.py` (pontos citados no relatório: `:196-200`, `:327`, `:436`, `:467`, `:485`; procure os demais).

#### TK-71 · Erros de entrada da CLI (C-2, C-3)
- **Teste primeiro:** `--persona-json` inválido, symbiote inexistente em `session start`, `--importance 5`, `--type` inválido e `skills list --root /inexistente` saem com código diferente de 0 e uma linha de erro, sem traceback.
- **Implementação:** capture `ValueError`, `pydantic.ValidationError`, `sqlite3.IntegrityError` e `FileNotFoundError` nos comandos. Unifique o servidor padrão da CLI em `http://localhost:8008` (`cli/main.py:673`, `:685`, `:765`). Aproveite para elevar a cobertura de `cli/main.py`, hoje em 48%.

#### TK-72 · Índices (P-1)
- **Repro:** `memoria/r8_index.py`.
- **Teste primeiro:** `EXPLAIN QUERY PLAN` das consultas quentes usa índice (`messages` por sessão, `memory_entries` por symbiote, `sessions` por symbiote, `execution_traces` por sessão).
- **Implementação:** `adapters/storage/sqlite.py`: `CREATE INDEX IF NOT EXISTS` em `messages(session_id, created_at)`, `memory_entries(symbiote_id, is_active, type)`, `sessions(symbiote_id, status, ended_at)` e `execution_traces(session_id)`.

#### TK-73 · Retenção (P-2, D-12)
- **Teste primeiro:** com a retenção ligada, traces e mensagens de sessões fechadas além do prazo são removidos; com ela desligada, nada é removido; sessões ativas nunca são tocadas.
- **Implementação:** campos em `KernelConfig` (padrão `None`, desligado) e um método `kernel.prune()`. Reaproveite o prune de audit da CLI (`cli/main.py:1210`). Não agende nada automaticamente.

#### TK-74 · `synchronous=NORMAL` (P-3, D-13)
- `adapters/storage/sqlite.py:194`: acrescente `PRAGMA synchronous=NORMAL` junto do WAL. Teste: o pragma está ativo numa conexão nova.

#### TK-75 · Correções de documentação (D)
- **Repro:** `tools/t_docs.py`.
- Corrija cada linha da tabela "D — Documentação" do relatório. Itens que as fases anteriores tornaram verdadeiros (hooks, `on_demand`, policy do browser) só precisam de conferência.
- Atualize `docs/HOST_INTEGRATION.md` e `QUICKSTART.md` para as mudanças da seção 6, e a versão no título do `QUICKSTART.md`.
- Atualize `project/docs/SPEC.md` nas seções afetadas (o cabeçalho ainda diz 0.2.27).

#### TK-76 · Teste que mantém a documentação honesta (D)
- `tests/unit/test_docs_references.py`: extrai dos blocos Python de `README.md`, `QUICKSTART.md` e `docs/HOST_INTEGRATION.md` os imports e os acessos a métodos de `kernel`, `tool_gateway` e `environment`, e falha se o nome não existir. Não precisa executar os blocos.

#### TK-77 · `INFRA.md` e `setup_env.sh` (D)
- `INFRA.md:7`: o ambiente local é `http://127.0.0.1:8008`, execução manual com `uvicorn symbiote.api.http:app --host 127.0.0.1 --port 8008`; não existe unit systemd. Não altere mais nada no `INFRA.md`.
- `setup_env.sh`: substitua pelo comando da seção 3 e uma linha de instrução. Remova as referências a forgebase.

---

### Fase 6 — Qualidade e fechamento

#### TK-80 · Higiene do repositório (Q-4, D-15, D-16)
- `git rm --cached symbiote.db .coverage` e `git rm -r --cached .context`; acrescente `.coverage` ao `.gitignore`. Não apague os arquivos do disco e não mova os arquivos soltos da raiz.
- Registre no `BACKLOG.md`: container rodando como root (D-16) e streaming na API (D-17).

#### TK-81 · Configuração única do ruff (Q-1, D-14)
- `ruff.toml`: `target-version = "py312"` e remova a linha `src/forgebase/**`. Apague o bloco `[tool.ruff]` e `[tool.ruff.lint]` do `pyproject.toml`. Corrija o que o ruff passar a apontar. O `Dockerfile` e o `.pre-commit-config.yaml` já usam `ruff.toml`.

#### TK-82 · Gate de cobertura no CI (Q-1)
- `Dockerfile:39`: `python -m pytest -q --cov=src/symbiote --cov-fail-under=85` (o `pytest-cov` já está em `requirements/ci-linux.lock`).
- Valide com `docker build --target quality .` local. A cobertura no container pode diferir da local por causa dos extras instalados; se ficar abaixo de 85, acrescente testes, não baixe o limite.

#### TK-83 · Gate de tipos no CI (Q-1, D-18)
- Etapa 1: corrija os 55 erros que aparecem sem `strict`, ajuste `[tool.mypy]` para o conjunto de flags que passa e acrescente `mypy src/symbiote` ao estágio `quality` do `Dockerfile`.
- Etapa 2, se couber no tamanho: os 305 `type-arg` são mecânicos (`dict` vira `dict[str, Any]`). Ao zerar o restante, volte para `strict = true`.
- Se a etapa 2 não couber, registre no `BACKLOG.md` com a contagem restante.

#### TK-84 · Versão de schema (Q-5)
- **Repro:** `memoria/mig_test.py`.
- **Teste primeiro:** um banco criado com o schema atual e um criado do zero convergem; rodar `init_schema` duas vezes é idempotente; `PRAGMA user_version` reflete a última migração.
- **Implementação:** `adapters/storage/sqlite.py:203-246`: migrações numeradas controladas por `PRAGMA user_version`. A lista atual de `ALTER TABLE` vira a migração 1, mantendo a tolerância a coluna já existente. Os índices e tabelas novos deste plano entram como migrações seguintes. Mova o `CREATE INDEX` de `sqlite.py:33` para depois das migrações de coluna.

#### TK-85 · Conexão fixada no IP validado (S-11)
- **Teste primeiro:** um resolvedor falso que devolve IP público na validação e `127.0.0.1` na conexão não alcança o loopback.
- **Implementação:** em `environment/tools.py:673-757`, conecte ao IP que `validate_url` aprovou, mantendo `Host` e SNI do hostname original. Se isso exigir trocar `urllib` por `httpx` com transporte próprio e passar do tamanho, pare e registre no `BACKLOG.md` com o que descobriu.

#### TK-86 · Refatorações estruturais (Q-3)
Só comece com todas as outras tarefas concluídas e a suíte verde. Cada item é um commit próprio, sem mudança de comportamento, validado pela suíte inteira.
- **`runners/chat.py`:** um único corpo de loop parametrizado pelo executor de tools; o instant vira o caso de uma iteração. O teste parametrizado da TK-32 precisa passar sem alteração.
- **`api/http.py`:** separe em routers por área (symbiotes, sessões, tools, discovery, admin, Console), mantendo os paths.
- **`core/kernel.py:249-319`:** os setters deixam de escrever em atributos privados do runner; o `ChatRunner` ganha métodos públicos equivalentes.

#### TK-90 · Fechamento
1. Rode a suíte completa, cobertura, `ruff`, `mypy` e `bash tests/smoke/smoke_cli.sh`.
2. Rode os scripts de `reports/2026-11-13_repro/` que cobrem itens P0 e confirme que o defeito não aparece mais.
3. Confira o `CHANGELOG.md`: uma entrada por tarefa em `[Unreleased]` e as notas de migração da seção 6.
4. Confira que todo item `skipped` e todo desvio está na seção 11, e que o que ficou para depois está no `BACKLOG.md`.
5. Entregue ao operador um resumo: tarefas feitas, puladas e por quê, números antes e depois (testes, cobertura, erros de mypy), mudanças incompatíveis e o que ele precisa decidir (push, release, D-15, D-16). Não faça push.

## 9. Gates entre fases

Antes de abrir a branch da fase seguinte:

- todas as tarefas da fase estão `done` ou `skipped` com justificativa;
- suíte completa verde, `ruff` limpo, cobertura em 85% ou mais;
- o `CHANGELOG.md` cobre a fase;
- ao fim das fases 1 e 2, pare e apresente um resumo ao operador antes de seguir. São as fases com mudança incompatível mais sensível (TK-10, TK-11) e com maior alteração no loop.

## 10. Cobertura do relatório

Todo item do relatório tem tarefa. Use esta tabela para conferir.

| Relatório | Tarefa | Relatório | Tarefa | Relatório | Tarefa |
|---|---|---|---|---|---|
| S-1 | TK-10 | E-1 | TK-20 | M-1 | TK-40 |
| S-2 | TK-11 | E-2 | TK-27, TK-28, TK-29, TK-40 | M-2 | TK-41 |
| S-3 | TK-14 | E-3 | TK-26 | M-3 | TK-42 |
| S-4 | TK-49 | E-4 | TK-22, TK-18 | M-4 | TK-43 |
| S-5 | TK-51 | E-5 | TK-21 | M-5 | TK-45 |
| S-6 | TK-17 | E-6 | TK-23 | M-6 | TK-46 |
| S-7 | TK-15 | E-7 | TK-24 | M-7 | TK-47 |
| S-8 | TK-16 | E-8 | TK-25 | M-8 | TK-48 |
| S-9 | TK-18 | E-9 | TK-30 | M-9 | TK-50 |
| S-10 | TK-13 | E-10 | TK-31 | M-10 | TK-44 |
| S-11 | TK-19, TK-85 | E-11 | TK-32, TK-86 | T-1 | TK-61 |
| A-1 | TK-12 | E-12 | TK-33, TK-34, TK-35 | T-2 | TK-62 |
| A-2 | TK-60 | C-1 | TK-70 | T-3 | TK-63 |
| A-3 | TK-68 | C-2, C-3 | TK-71 | T-4 | TK-64 |
| D | TK-75, TK-76, TK-77 | P-1 | TK-72 | T-5 | TK-65 |
| Q-1 | TK-81, TK-82, TK-83 | P-2 | TK-73 | T-6 | TK-66 |
| Q-2 | TK-01 e o método da seção 4 | P-3 | TK-74 | T-7 | TK-67 |
| Q-3 | TK-86 | Q-4 | TK-80 | Q-5 | TK-84 |

## 11. Notas de execução

Preencha durante a execução: linha de base medida na TK-00, tarefas puladas e motivo, desvios das decisões da seção 5, e qualquer defeito novo encontrado no caminho (registre e siga; não amplie o escopo sem falar com o operador).

| Data | Tarefa | Nota |
|---|---|---|
| 2026-11-13 | TK-00 | Linha de base conferida: 1758 passed, 24 skipped, ruff limpo, cobertura 85,48%. Mypy não medido de novo. |
| 2026-11-13 | TK-00 | O repositório mistura terminações de linha (167 arquivos CRLF, 23 mistos no índice). As edições preservam a terminação original de cada linha; arquivos novos usam LF. |
| 2026-11-13 | TK-10 | Desvio da regra de pronto: get, update e deactivate recebem symbiote_id opcional (não obrigatório), porque a seção 6 só lista search e get_relevant como incompatíveis. A reflexão confere o dono pelo objeto devolvido por get(), o que funciona com MemoryPort de host. |
| 2026-11-13 | TK-10 | Consequência não listada na seção 6: a rota GET /memory/search, SymbioteClient.search_memory e a CLI memory search passam a exigir o symbiote. Registrado como MIGRAÇÃO no CHANGELOG. |
| 2026-11-13 | TK-11 | kernel.create_symbiote ganhou o parâmetro opcional owner_id (aditivo), necessário para testes e hosts criarem symbiotes com dono. O Console passou a chamar todas as rotas /api/* com a chave e mostra aviso quando ela falta; verificado no Chromium headless contra uma API temporária. |
| 2026-11-13 | TK-11 | Num processo novo, a primeira requisição sem chave ainda devolve 503 (gerenciador de chaves não inicializado), não 401. É o item A-2, tratado na TK-60. |
| 2026-11-13 | TK-12 | symbiote.core.exceptions.ValidationError passou a herdar também de ValueError, para não quebrar quem capturava ValueError do teto de max_tool_iterations e do papel de chave. |
| 2026-11-13 | TK-14 | O primeiro rascunho do teste de redirect substituiu socket.getaddrinfo global e fez uma tentativa de conexão TCP a 93.184.216.34 (30 s de espera). Corrigido antes do commit: o teste troca apenas o módulo socket usado por validate_url. |
| 2026-11-13 | TK-17 | Além do onclick da linha 834, outros quatro botões interpolavam dados em onclick (editar, fechar sessão, aprovar tool, revogar chave). Os cinco passaram a usar data-* com um handler delegado. |
| 2026-11-13 | Gate fase 1 | 1895 passed, 24 skipped; ruff limpo; cobertura total 87% (era 85,48%); smoke da CLI passa. cli/main.py segue em 48% (pré-existente, TK-71) e core/exceptions.py em 78% (só a herança mudou). |
| 2026-11-13 | TK-20 | Três testes antigos afirmavam o defeito (autocompact resumindo também a última troca) e foram reescritos para o novo contrato. O teste e2e de orçamento passou a conferir só as seções que o assembler corta: a estimativa total agora inclui as 9 tools builtin (~1,4 mil tokens), que aparecem no prompt mesmo sem autorização. |
| 2026-11-13 | TK-20 | Um único resultado maior que o orçamento do loop ainda é cortado (e a cauda se perde). A reprodução t04b só passa com loop_context_budget maior; o kernel agora avisa no log quando tool_result_max_chars excede o que o orçamento comporta. |
| 2026-11-13 | TK-21 | Desvio de implementação: o modo instant de message_async continua chamando _run_instant síncrono; ele funciona com handlers async porque o caminho síncrono do gateway agora os executa no loop próprio. Não troquei por execute_tool_calls_async. |
| 2026-11-13 | TK-22 | Desvio de implementação: em vez de um executor de vida longa, cada handler roda numa thread daemon. Um executor compartilhado ficaria sem workers depois de alguns handlers travados, e threads não-daemon seguram o encerramento do processo. |
| 2026-11-13 | TK-22 | test_sprint4_hardening::TestLoaderAtomicRefresh leva ~10 s nesta máquina; o mesmo tempo aparece no código da main (load average ~5). Não é regressão desta branch. |
| 2026-11-13 | TK-23 | Com tool_loop desligado (turno de chamada única) não há chamada final nem texto de fallback; o caso não estava no plano e apareceu num teste antigo. Uma resposta final que ainda pede tools é tratada como narração e vira o fallback. |
| 2026-11-13 | TK-23 | Incidente: um --amend foi feito com um teste e2e falhando, porque o código de saída do pytest se perdeu num pipe. Corrigido no mesmo commit antes de seguir; desde então os commits passam por um script que exige suíte e ruff verdes. |
| 2026-11-13 | TK-25 | Se as reformulações falham, o texto original volta ao usuário, exceto quando está vazio ou ainda tem um bloco tool_call cru (aí segue o pedido de desculpas fixo). |
| 2026-11-13 | TK-26 | Testes antigos de harness e handoff escreviam nos slots privados removidos; passaram a usar _persist_trace e o dicionário _handoffs. As reproduções t02 (8 threads) e t03 (sequencial) do relatório agora dão zero traces trocados ou perdidos. |
| 2026-11-13 | TK-28 | Com tool_mode=long_run, um planner que não devolve blocos faz o turno falhar com CapabilityError (antes o modo rodava como chat comum sem avisar). O long-run em message_async roda numa thread; on_token é chamado dela. |
| 2026-11-13 | TK-31 | Desvio do plano: 429 não é retentado nesta camada. Conferi no forge_llm 0.7.16 que só o provedor symrouter repassa max_retries=0; os demais SDKs já retentam 429, e retentar aqui multiplicaria as chamadas. Adapters de host que sinalizam erro só pelo texto da mensagem deixam de ser retentados (MIGRAÇÃO no CHANGELOG). |
| 2026-11-13 | TK-33 | Não incluí os params das chamadas no resumo (sugestão do relatório, fora da tarefa do plano). |
| 2026-11-13 | TK-34 | _BLOCKED_TOOLS foi removido em vez de aplicado: aplicá-lo bloquearia spawn em subagentes e tornaria o limite de profundidade inútil. |
| 2026-11-13 | TK-35 | O retry do bus ainda reexecuta tools com efeito colateral; idempotência fica com o host. Não implementei despacho por chat_id em tasks (fora da tarefa do plano). |
| 2026-11-13 | Gate fase 2 | 2013 passed, 24 skipped; ruff limpo; cobertura total 88%; smoke da CLI passa. adapters/llm/forge.py em 77% (era 70%; o que falta é código anterior). Reproduções t02-t21 conferidas contra o código novo. |
| 2026-11-13 | TK-41 | Achado no caminho: os testes de API antigos substituíam get_adapter mas não get_kernel, e a rota reutilizava um kernel singleton deixado por outro teste. O conftest agora usa um banco temporário como SYMBIOTE_DB_PATH padrão e restaura os singletons da API a cada teste. O banco local em .symbiote/ não foi tocado (conferido pela data de modificação). |
| 2026-11-13 | TK-42 | A marca d'água por sessão ficou numa coluna nova (sessions.reflected_until). O score de uma sessão reaberta é atualizado e entra uma vez só na média do harness. |
| 2026-11-13 | TK-45 | O desenho do dream (docs/arch-04) foi atualizado: o modo determinístico une só quase-duplicatas; contradições ficam para o modo full. |
| 2026-11-13 | TK-47 | Rollback da única versão agora volta ao texto padrão (antes falhava/404). A linha de baseline (versão 0) fica fora de list_versions. |
| 2026-11-13 | TK-49 | Com o skill_view não contando mais uso de skills em quarentena, a autopromoção na prática só acontece para skills já ativas; promover da quarentena passa a ser manual. |
| 2026-11-13 | TK-51 | Sem base_url o loader mantém o comportamento anterior (a aprovação humana é a única barreira); com base_url, só a mesma origem recebe credenciais e allow_internal. |
| 2026-11-13 | Gate fase 3 | 2080 passed, 24 skipped; ruff limpo; smoke da CLI passa; cobertura total em 88%. |
| 2026-11-13 | TK-61 | Os headers informados ao registrar uma tool pela API ficam gravados em texto puro na tabela http_tools; o header_factory (código) nunca é persistido. O conftest passou a dar um banco padrão novo a cada teste. |
| 2026-11-13 | TK-62 | Os tool_ids gerados para rotas sem operationId e com path params mudaram (get_items_by_id); as tools antigas ficam no banco como estavam, e as novas entram como pending. |
| 2026-11-13 | TK-63 | O contexto continua oferecendo ao modelo todas as tools registradas que casam com as tool_tags, autorizadas ou não; só a execução é barrada. Não mexi nisso (fora do item T-3); fica como observação. |
| 2026-11-13 | TK-64 | Uma config de workspace nova passa a herdar da config do symbiote os campos não informados (antes partia dos defaults e ficava sem tools). O lock é global ao processo; entre processos quem segura é o índice único. |
| 2026-11-13 | TK-67 | Os testes de integração do browser que falham antes de abrir o Chromium (policy, SSRF) e o da cadeia de extract, que usa providers falsos, desligam a checagem de extra para continuar rodando sem playwright/forge_scraper. |
| 2026-11-13 | TK-68 | generation_settings é repassado ao LLM como veio do cliente, sem lista de chaves permitidas: um tenant pode, por exemplo, trocar o modelo por chamada. Se isso for um problema de custo, restringir as chaves é uma decisão do operador. |
| 2026-11-13 | Gate fase 4 | 2121 passed, 24 skipped; ruff limpo; smoke da CLI passa; cobertura total 89%. browser/browser/supervisor.py aparece em 59% porque os testes de SIGTERM rodam em subprocesso. |
| 2026-11-13 | TK-71 | session find sem resultado continua saindo com código 0 e a mensagem 'No session found' (é uma busca); não mudei. |
| 2026-11-13 | TK-75 | O título do QUICKSTART deixou de fixar versão (fica sempre desatualizado a cada release); o SPEC aponta 0.7.3 + [Unreleased]. |
| 2026-11-13 | TK-77 | setup_env.sh não foi executado (recriaria o .venv em uso); só bash -n. |
| 2026-11-13 | Gate fase 5 | 2228 passed, 24 skipped; ruff limpo; smoke da CLI passa; cobertura total 90%; cli/main.py de 52% para 72%. |
| 2026-11-13 | TK-82 | docker build --target quality NÃO foi rodado: precisa de rede (apt-get, pip e clone do forge-llm no GitHub), o que a seção 7 manda perguntar. O comando equivalente passa localmente (89,99%). Validar no CI ou com docker build local com autorização do operador. |
| 2026-11-13 | TK-83 | Etapa 1 feita: mypy sem strict limpo (era 75 erros) e no Dockerfile. Etapa 2 não coube: com --strict restam 416 erros (339 type-arg), registrado como B-71. O mypy local é 2.4.1; o lock de CI fixa 2.3.0, então vale confirmar o resultado no CI. Um dos erros era um bug real (GeneralizePhase, commit próprio). |
| 2026-11-13 | TK-86 | Feito: setters do kernel por métodos públicos e divisão do api/http.py em schemas + 7 routers (OpenAPI idêntica). Não feito: unificação dos loops sync/async do ChatRunner (~780 linhas duplicadas), que passa de 2x o tamanho estimado; registrada como B-72 com a proposta (máquina de estados sem I/O, dois drivers). Decisão do operador. |
| 2026-11-13 | TK-90 | Fechamento: 2234 passed, 24 skipped; cobertura 90,08%; ruff e mypy (sem strict) limpos; smoke da CLI passa. Repros P0 rodados numa cópia: S-1, S-2, S-3, S-4, S-5, S-7, E-1, E-3, E-5 a E-8, M-3, M-4, M-10 não se reproduzem mais. probe_inicial.py, close_path.py e t_discovery.py param no meio porque assumiam o comportamento antigo (rotas abertas; tool alterada continuar aprovada); M-2 fica coberto pelo teste test_api_close_runs_learning. live1/live2 não rodaram (exigem a instância local em 8008, que roda o código antigo e não foi tocada). |
