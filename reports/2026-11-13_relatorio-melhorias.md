# Relatório de melhorias — Symbiote 0.7.3

Data: 2026-11-13 · Commit analisado: `0737fcf` (main) · Escopo: repositório e instância local. Produção e staging ficaram de fora.

## Resumo

O Symbiote tem uma base bem construída: 1758 testes passam em 27 s, o ruff está limpo, o pipeline de staging é cuidadoso e a instância local respondeu rápido e sem perder dados sob carga. Os problemas estão em três lugares que a suíte não cobre:

1. **Isolamento.** A memória de um symbiote entra no prompt de outro, e 25 rotas da API HTTP leem ou alteram dados sem chave.
2. **O agente não se comporta como prometido em casos comuns.** A compactação apaga o resultado da tool que o modelo ainda não leu, o recall padrão quase nunca encontra memória, fechar sessão pela API pula todo o aprendizado, e hooks documentados nunca disparam.
3. **Aprendizado automático com guardas furadas.** A quarentena de skills, a aprovação de tools descobertas e o rollback do harness podem ser contornados ou erram.

Nenhum dos achados abaixo é pego pela suíte atual. A recomendação transversal é transformar cada reprodução em teste de regressão antes de corrigir.

## Como foi feito

- **Leitura e revisão** de todo o `src/` (26 mil linhas), dividida em quatro frentes: API HTTP, caminho de execução, memória/aprendizado e tools/CLI/docs. As três últimas foram feitas por subagentes revisores com instrução de reproduzir cada achado.
- **Testes ao vivo** contra a instância local em `http://127.0.0.1:8008` (porta confirmada pelo devops): matriz de autenticação, isolamento entre dois tenants, entradas inválidas, concorrência, latência e tamanho de payload.
- **Testes in-process** com LLM falso e banco temporário, para o que exige um modelo respondendo.
- **Gates de qualidade:** pytest com cobertura, ruff e mypy no `.venv` do projeto.

Legenda de evidência usada em cada item:

| Marca | Significado |
|---|---|
| **C** | Confirmado por mim nesta sessão: reproduzi ou medi. |
| **R** | Reproduzido por um subagente revisor, com script e saída. Não refiz. |
| **L** | Só leitura de código, sem reprodução. |

Gravidade: Crítico, Alto, Médio, Baixo. Esforço: P (horas), M (dias), G (semana ou mais).

Os scripts de reprodução estão em `/tmp/claude-1000/-home-palhano-dev-tools-symbiote/90063900-573e-49eb-8b1b-1f27b9a247f9/scratchpad/` (subpastas `live`, `inproc`, `agent-exec`, `agent-mem`, `agent-tools`). É um diretório temporário da sessão; se forem virar testes, precisam ser copiados antes.

## Estado medido

| Item | Resultado |
|---|---|
| pytest (suíte completa) | 1758 passaram, 24 pulados, 27,5 s |
| Cobertura | 85,48% (limite configurado: 85%) |
| ruff | limpo |
| mypy `strict` (configurado no `pyproject.toml`) | 453 erros em 62 arquivos; 305 são `type-arg` (`dict`/`list` sem parâmetro) |
| mypy sem `strict` | 55 erros em 18 arquivos |
| Menor cobertura | `browser/.../chromium_local.py` 28%, `cli/main.py` 48%, `sdk/client.py` 70%, `adapters/llm/forge.py` 70% |

Latência na instância local (40 chamadas sequenciais, p50):

| Rota | p50 |
|---|---|
| `GET /sessions/{id}` | 0,6 ms |
| `GET /symbiotes/{id}` (com auth) | 0,6 ms |
| `GET /api/dashboard` | 2,2 ms |
| `GET /health` | 2,4 ms |
| `POST /sessions/{id}/messages` | 6,4 ms |

Sob carga: 200 escritas com 20 threads deram 161 req/s, todas com 201 e nenhuma mensagem perdida.

## Os doze primeiros

| # | Item | Gravidade | Esforço | Evidência |
|---|---|---|---|---|
| 1 | S-1 Memória vaza entre symbiotes | Crítico | M | C |
| 2 | S-2 API: 25 rotas de dados sem chave | Crítico se exposta | M | C |
| 3 | E-1 Compactação apaga o resultado da tool | Alto | M | C |
| 4 | S-3 SSRF aceita IPv6 mapeado para IPv4 | Alto | P | C |
| 5 | M-1 Recall padrão quase nunca recupera memória | Alto | M | C |
| 6 | M-2 Fechar sessão pela API pula o aprendizado | Alto | P | C |
| 7 | E-5 Handler async devolve coroutine como sucesso | Alto | P/M | C |
| 8 | E-2 Hooks e `long_run` documentados não estão ligados | Alto | P/M | C |
| 9 | S-7 Parâmetros do LLM entram na URL sem encoding | Alto | P | C |
| 10 | S-4 Quarentena de skills contornável | Alto | M | R |
| 11 | S-5 Aprovação de tool sobrevive a re-discovery | Alto | M | R |
| 12 | E-3 Trace cruzado entre sessões falseia o score | Alto | M | R |

---

## S — Segurança e isolamento

### S-1 · Memória não é isolada por symbiote — Crítico · M · C
- **O que acontece:** `MemoryStore.search` e `get_relevant` não filtram por `symbiote_id`. Guardei um segredo na memória do symbiote A; o symbiote B mandou uma mensagem que é substring dela ("token do cliente") e o segredo apareceu no prompt de B. Com a mensagem `%` vem tudo.
- **Variantes (R):** a tool `search_memories` executada por B devolve memórias de A. `search_knowledge` usa um `symbiote_id` vindo dos parâmetros do LLM. Em `reflection_mode=llm`, um `target_id` na saída do modelo de B sobrescreve memória de A.
- **Onde:** `src/symbiote/memory/store.py:57-139`, `:165`, `:178`; `src/symbiote/environment/tools.py:562-586`; `src/symbiote/core/reflection.py:418-420`.
- **Correção:** tornar `symbiote_id` obrigatório em `search`, `get_relevant`, `update` e `deactivate`, com `AND symbiote_id = ?`. Escapar `%` e `_` no `LIKE`. O gateway deve injetar o symbiote ativo nos handlers builtin, nunca lê-lo dos parâmetros.

### S-2 · API HTTP: rotas de dados sem autenticação — Crítico se exposta · M · C
- **O que acontece:** das 47 rotas, 27 não exigem chave. Tirando `/` e `/health`, 25 leem ou alteram dados. Na instância local, sem nenhuma chave, consegui:
  - ler as mensagens de uma sessão de outro tenant (`GET /api/sessions/{id}/messages`);
  - injetar mensagem com `role=system` numa sessão alheia;
  - fechar a sessão;
  - listar os symbiotes de todos os tenants (`GET /api/symbiotes`);
  - registrar uma HTTP tool com URL arbitrária e executá-la.
- **O que funciona:** nas rotas autenticadas o isolamento entre tenants segurou. Oito tentativas com chave válida de outro tenant deram 403.
- **Onde:** `src/symbiote/api/http.py:605-837` (sessões, memória, tools) e `:1266-1588` (Console `/api/*`). `_PUBLIC_PATHS` em `src/symbiote/api/middleware.py:20` está definido e nunca é usado.
- **Relacionado:**
  - symbiote com `owner_id` nulo passa em qualquer tenant (`if sym.owner_id and ...`, `http.py:549`, `:570`, `:596`, `:1178`) — L;
  - depois de `DELETE /symbiotes/{id}`, as mensagens continuam legíveis e é possível criar sessão nova no symbiote apagado — C.
- **Correção:** aplicar `require_auth` como dependência global do app, com allowlist explícita (`/health`, e `/` só em modo local). Checar dono em toda rota que recebe `session_id` ou `symbiote_id`. As rotas `/api/*` do Console devem exigir admin.

### S-3 · SSRF: `validate_url` aceita IPv6 mapeado para IPv4 — Alto · P · C
- **O que acontece:** `http://127.0.0.1/` é bloqueado, mas `http://[::ffff:127.0.0.1]/`, `http://[::ffff:169.254.169.254]/latest/meta-data/` e `http://[::]/` passam. Uma HTTP tool com URL controlada buscou um servidor em loopback por esse caminho. A revalidação de redirect tem o mesmo buraco (R).
- **Onde:** `src/symbiote/security/network.py:14-25`, `:61-72`.
- **Correção:** normalizar com `ip.ipv4_mapped or ip` e bloquear `not ip.is_global`, em vez de lista manual de redes.

### S-4 · Quarentena de skills contornável — Alto · M · R
- **O que acontece:**
  - `skill_view` conta o uso antes de checar o status. Três chamadas recusadas promovem a skill em quarentena para `active` e devolvem o conteúdo.
  - O review em background, alimentado pela transcrição, pode editar uma skill humana ativa sem passar por quarentena.
  - Em `per_symbiote`, `skills_extra_roots` é gravável por todos os symbiotes, apesar do docstring dizer o contrário.
- **Onde:** `src/symbiote/skills/tool.py:243-254`; `src/symbiote/skills/usage.py:211-228`; `src/symbiote/core/background_review.py:426-441`; `src/symbiote/skills/scope.py:129`.
- **Correção:** checar status antes de `mark_used`. Sob review em background, restringir `edit`/`patch` a skills `agent_created` ou rebaixar a skill alterada para quarentena. Tratar `extra_roots` como somente leitura.

### S-5 · Aprovação de tool descoberta sobrevive à troca de método e host — Alto · M · R
- **O que acontece:** um humano aprova `list_items = GET A/api/items`. A spec muda e o mesmo `operationId` vira `DELETE` em outro host. O registro continua `approved` (o upsert preserva o `status`; li o SQL em `repository.py`), o loader registra com `allow_internal=True` e manda os headers de autenticação do host ao novo destino. Um `operationId: bash` aprovado substitui a builtin `bash` no gateway inteiro.
- **Onde:** `src/symbiote/discovery/repository.py:28-38`; `src/symbiote/discovery/loader.py:95-104`; `src/symbiote/discovery/service.py:200-217`.
- **Correção:** voltar para `pending` quando método, URL, parâmetros ou risco mudarem. Só conceder `allow_internal` e `header_factory` a URLs da mesma origem do `base_url`. Recusar `tool_id` que colida com builtin. `tests/unit/test_discovery_service.py:131-144` afirma a preservação e precisa mudar.

### S-6 · Console: o escape não cobre atributos HTML — Alto com `SYMBIOTE_LOCAL_ADMIN=1` · P · C
- **O que acontece:** `esc()` usa `textContent`/`innerHTML`, que não escapa aspas. Em `title="${esc(m.content)}"`, um conteúdo com `" onmouseover="...` injeta um atributo. Confirmei no Chromium headless com a função e o template copiados do Console: o `<span>` saiu com o atributo `onmouseover`. Não exercitei o caminho completo pela página.
- **Por que importa:** o conteúdo de memória vem de mensagens de usuários (ver M-3) e, em modo local admin, a chave admin fica em `window.__SYMBIOTE_KEY__`.
- **Onde:** `src/symbiote/api/dashboard.html:595-600` (função), usos em `:834`, `:1752`, `:1767-1769`, `:1782`, `:1857`, `:2165`.
- **Correção:** escapar `"` e `'` em `esc()` e trocar `onclick` inline com dados por `addEventListener`.

### S-7 · Parâmetros do LLM entram no `url_template` sem URL-encoding — Alto · P · C
- **O que acontece:** numa tool `.../api/items/{id}`, `id="../../admin/users"` e `id="42?as_admin=1"` chegaram ao servidor como path e query novos. Valores com espaço ou acento quebram (`InvalidURL`, `UnicodeEncodeError`), inclusive no exemplo do README (`search?q={q}`).
- **Onde:** `src/symbiote/environment/tools.py:637-638`.
- **Correção:** `urllib.parse.quote(str(v), safe="")` em cada valor antes do `format`.

### S-8 · Redirect reenvia `Authorization` para outra origem — Médio · P · R
- **Onde:** `src/symbiote/environment/tools.py:734-741`.
- **Correção:** remover `Authorization`, `Cookie` e os headers do `header_factory` quando esquema, host ou porta mudarem.

### S-9 · Tools de filesystem e `bash` — Médio · P/M · C
- **O que acontece:** `allowed_root` vem dos próprios parâmetros da chamada, então sem ele qualquer path passa (`/etc/passwd` aceito). A checagem usa `startswith` e aceitou `ws-evil` com root `ws`. O ramo de symlink é código morto depois do `resolve()`. Em `bash`, `_cwd` e `timeout` são controlados pelo LLM e o timeout mata só o `sh`: o processo filho continua (R).
- **Onde:** `src/symbiote/environment/tools.py:761-818`.
- **Correção:** root fixado pelo host na configuração da tool, comparação com `Path.is_relative_to`, e `start_new_session=True` com `killpg` no timeout.

### S-10 · Sem limite de tamanho — Médio · P · C
- **O que acontece:** a instância local aceitou sem chave uma mensagem de 50 MB (0,46 s) e um nome de symbiote de 1 MB. A resposta de uma HTTP tool também é lida inteira, sem teto (R).
- **Correção:** limite de corpo no servidor, `max_length` nos modelos de request e leitura limitada na resposta de tools.

### S-11 · Itens menores
- Janela de DNS rebinding: valida resolvendo o DNS e o fetch resolve de novo (`security/network.py:57`) — L.
- `.symbiote/config` com a API key é gravado em modo 0644 (`cli/main.py:658-667`) — R.

---

## E — Caminho de execução

### E-1 · A compactação apaga o resultado da tool que o modelo ainda não viu — Alto · M · C
- **O que acontece:** o budget do runner é fixo em 16 mil tokens; `KernelConfig.context_budget` não chega ao `ChatRunner`. Quando system + histórico passa de 80% disso, o layer 3 troca todas as mensagens do loop, inclusive a recém-anexada, por `"1) tool → ok"`. Com uma persona de 60 mil caracteres, a tool rodou 6 vezes, o modelo nunca viu o resultado e respondeu que não conseguiu. Com persona de 2 mil, respondeu certo em 2 chamadas.
- **Por que importa:** tools com efeito colateral são repetidas. Basta um system prompt grande (muitos schemas, página em `extra_context`).
- **Onde:** `src/symbiote/runners/chat.py:58`, `:135`, `:1378-1460`; `src/symbiote/core/kernel.py:123-134`. Agravante (R): `ContextAssembler._total_tokens` ignora tools e `extra_context` (`core/context.py:597-612`).
- **Correção:** nunca compactar a última exchange. Medir o crescimento do loop contra um budget vindo da config ou do modelo.

### E-2 · Recursos documentados que não estão ligados — Alto · P/M · C e R
- **Hooks (C):** registrei um hook em `kernel.hooks`, a tool executou e o hook não recebeu nenhum evento. Não existe chamada a `before_tool`, `after_tool`, `before_turn` ou `after_turn` em `src/`. O README (linhas 167-179) e `docs/HOST_INTEGRATION.md` apresentam isso como ponto de auditoria.
- **`LongRunRunner` (C por grep, R em execução):** não é registrado em lugar nenhum. `tool_mode="long_run"` roda como chat comum, sem planner nem handoff. O runner em si tem defeitos latentes: o veredito do avaliador não altera `success`, e `run()` não aceita o `llm_config` que o kernel sempre passa.
- **`ProcessEngine` (R):** o índice do passo vive só em memória; depois de um restart, `advance()` regride. `work("research_task: ...")` sempre falha porque o runner usa o texto inteiro como nome do processo.
- **`SemanticRecallProvider` (C por grep):** existe, mas não é instanciado (ver M-1).
- **Correção:** despachar o `CompositeHook` no gateway e no runner, ou tirar da documentação. Registrar e rotear o `LongRunRunner`, ou marcá-lo como experimental.

### E-3 · Trace cruzado entre sessões falseia o score — Alto · M · R
- **O que acontece:** `_last_loop_trace` e `_last_trace` são um slot único por kernel, enquanto o lock é por sessão. Dois revisores reproduziram, de formas diferentes:
  - com 8 threads, de 160 traces esperados, 61 a 88 foram gravados com o marcador de outra sessão e 35 a 51 se perderam;
  - sem threads: a sessão A termina em estagnação, B manda uma mensagem, `close_session(A)` grava score 0,8 em vez de 0,2 e não gera a memória de falha.
- **Por que importa:** esse score alimenta o evolver, o tuner e o dream.
- **Onde:** `src/symbiote/core/capabilities.py:143-144`, `:182-183`; `src/symbiote/core/kernel.py:164-169`, `:478-488`, `:580`.
- **Correção:** `chat()` devolve o `RunResult` e o kernel guarda trace e handoff por `session_id`, ou relê `execution_traces` no close.

### E-4 · O timeout de tool não interrompe — Médio · P · C
- **O que acontece:** no caminho síncrono, o `with ThreadPoolExecutor` espera a thread ao sair. Medi: timeout de 0,2 s, tool de 2 s, retorno em 2,00 s com erro de timeout. O próprio `test_slow_tool_times_out` leva 2,00 s. No caminho async a chamada retorna no prazo, mas a thread segue e o efeito colateral acontece depois do "timed out" (R). No caminho paralelo o timeout é cumulativo (R).
- **Onde:** `src/symbiote/environment/policies.py:91-94`; `src/symbiote/environment/tools.py:404-429`.
- **Correção:** `pool.shutdown(wait=False)` com executor de vida longa, e documentar que a tool não é cancelada. Para `bash`, matar o grupo de processos.

### E-5 · Handler async devolve coroutine como sucesso — Alto · P/M · C
- **O que acontece:** com `tool_mode="instant"` e handler `async def`, `message_async` respondeu "Pronto." sem executar a tool, e o audit gravou `success`. No modo `brief` funcionou. Pelo caminho síncrono (`kernel.message`, CLI, `POST /chat`) nenhum handler async roda: o LLM recebe o `repr` do coroutine (R). Todos os handlers de `symbiote.browser` e do MCP são async, e os quickstarts do browser usam `kernel.message`.
- **Onde:** `src/symbiote/runners/chat.py:742-743`; `src/symbiote/environment/policies.py:95-97`; `src/symbiote/environment/tools.py:398`.
- **Correção:** no caminho sync, detectar awaitable e executar com `asyncio.run` na thread do pool, ou falhar com erro explícito. No instant dentro de `run_async`, usar `execute_tool_calls_async`.

### E-6 · Loop esgotado devolve sucesso com texto vazio — Médio · P/M · C
- **O que acontece:** com `max_tool_iterations=3` e um modelo que ainda pede tool na última rodada, a resposta final foi `''`, gravada como mensagem do assistant. O `LoopController` conta tool calls como iterações: 12 chamadas paralelas na primeira resposta estouram um limite de 10 (R).
- **Onde:** `src/symbiote/runners/loop_control.py:66`, `:83`; `src/symbiote/runners/chat.py:630-633`, `:693-715`.
- **Correção:** contar rodadas, e ao sair por limite ou timeout fazer uma chamada final sem tools, como já é feito na estagnação.

### E-7 · O parser de tool call lança exceção e deixa a mensagem do usuário sem resposta — Médio · P · C
- **O que acontece:** um bloco `tool_call` com lista em vez de objeto, ou `params: null`, levanta `AttributeError`/`ValidationError` que sai de `kernel.message`. A mensagem do usuário já foi persistida. No caminho nativo, argumentos truncados viram `{}` e a tool executa com parâmetros vazios (R).
- **Onde:** `src/symbiote/environment/parser.py:56-62`; `src/symbiote/adapters/llm/forge.py:312-329`.
- **Correção:** aceitar lista, capturar `ValidationError`, devolver argumento inválido como erro ao modelo e normalizar `content or ""`.

### E-8 · O validador rejeita respostas legítimas — Médio · P · C
- **O que acontece:** `is_valid_response` devolveu `False` para citações (`[1] Relatório`), link markdown no início da linha e checklist (`[x] feito`). No fluxo completo isso custa 2 chamadas extras de LLM e, se o modelo insistir, o usuário recebe "Desculpe, tive dificuldades técnicas" (R).
- **Onde:** `src/symbiote/runners/response_validator.py:13-47`; `src/symbiote/runners/chat.py:1254-1296`.
- **Correção:** só invalidar quando o trecho decodifica como JSON com chave `tool`/`params`, e devolver o texto original se os retries não melhorarem.

### E-9 · `message_async` bloqueia o event loop — Médio · P/M · R
- **O que acontece:** o LLM síncrono e o `time.sleep` do retry rodam na thread do loop. Quatro sessões com LLM de 0,5 s levaram 2,00 s em vez de 0,5 s.
- **Onde:** `src/symbiote/runners/chat.py:777`, `:922`, `:1233`.
- **Correção:** `await asyncio.to_thread(...)` ou uma porta `acomplete` com `asyncio.sleep`.

### E-10 · Classificação de erro retentável quebrada — Médio · P/M · R
- **O que acontece:** o adapter converte toda exceção em `LLMError(str(exc))`, então erros de conexão, 500 e 504 não são retentados. Um 400 determinístico com "timeout" na mensagem é retentado 3 vezes.
- **Onde:** `src/symbiote/runners/chat.py:1196-1211`; `src/symbiote/adapters/llm/forge.py:52`, `:105`, `:149`.
- **Correção:** `LLMError` carregar `status_code` e `retryable`.

### E-11 · Divergências entre os caminhos sync e async — Médio · G para unificar · R
- `on_after_tool_result` encerra o loop em `run()` e é ignorado em `run_async()`.
- O nudge de review de skills só existe no sync.
- `max_tool_iterations=0`: `run()` funciona, `run_async()` levanta `UnboundLocalError`.
- No instant com streaming, `on_token` recebe o bloco `tool_call` cru.
- **Onde:** `src/symbiote/runners/chat.py:404-409`, `:613-627`, `:876-896`, `:958`.
- **Correção:** um único corpo de loop parametrizado pelo executor de tools.

### E-12 · Outros — Médio/Baixo · R
- Compactação em cascata perde o resumo anterior (`chat.py:1326-1362`).
- `SubagentManager._depth` é global: 5 spawns paralelos de profundidade 1 batem no limite de 3, e um spawn que falha deixa a sub-sessão aberta (`runners/subagent.py:96-248`).
- Retry do `MessageBus` reexecuta `kernel.message`: 4 cobranças e 4 mensagens `user` idênticas num teste (`bus/message_bus.py:173-206`). O bus não está ligado a nada em `src/`.

---

## M — Memória e aprendizado

### M-1 · O recall padrão quase nunca recupera memória — Alto · M · C
- **O que acontece:** `get_relevant` faz `content LIKE '%<mensagem inteira>%'`. Com a memória "User prefers the vim editor", a pergunta "Which editor do I prefer?" não trouxe nada; só a mensagem exata "vim" trouxe. O recall por palavras-chave com ranking existe em `memory/recall.py`, mas nunca é instanciado.
- **Onde:** `src/symbiote/memory/store.py:104-123`; `src/symbiote/memory/recall.py:56`.
- **Correção:** `get_relevant` delegar ao recall tokenizado, com filtro por symbiote. O tokenizer `[a-zA-Z0-9_]+` e as stop-words só em inglês precisam de ajuste para português.

### M-2 · Fechar sessão pela API pula todo o aprendizado — Alto · P · C
- **O que acontece:** `POST /sessions/{id}/close` chama `SessionManager.close` direto. Com as mesmas três mensagens, fechar pela rota gerou 0 memórias e 0 scores; `kernel.close_session` gerou 2 memórias e 1 score. O SDK usa essa rota, então symbiotes servidos por HTTP não refletem, não pontuam, não revisam skills e não sonham.
- **Onde:** `src/symbiote/api/http.py:685-701`.
- **Correção:** a rota chamar `kernel.close_session`, com checagem de dono.

### M-3 · Reflexão por palavra-chave: só inglês, grava a mensagem inteira e não deduplica — Alto · M · C e R
- **Só inglês (C):** as frases "Eu prefiro respostas curtas", "sempre uso Python 3.12" e "Decidi que o deploy será toda sexta" geraram 0 memórias; as equivalentes em inglês geraram 2. É o modo padrão.
- **O resto (R):** o match é por substring em mensagens de qualquer role, então uma resposta do assistant vira memória `constraint` com importância 0,8. Sessões por `external_key` são reabertas e cada close reprocessa as últimas 50 mensagens: 4 closes deram 10 memórias, 1 distinta, e 4 linhas de score. `kernel.get_session()` reabre uma sessão fechada como efeito de leitura.
- **Onde:** `src/symbiote/core/reflection.py:55-56`, `:153-171`, `:276-295`; `src/symbiote/core/kernel.py:1195-1216`; `src/symbiote/core/session.py:66-76`, `:103-110`.
- **Correção:** restringir a mensagens `user` com fronteira de palavra, incluir termos em português, marcar a última mensagem refletida por sessão e criar `UNIQUE(session_id)` em `session_scores`.

### M-4 · Saída malformada do LLM derruba `close_session` e deixa a sessão aberta — Médio · P · R
- **O que acontece:** em `reflection_mode=llm`, um item com `"importance": "high"` levanta `ValueError`. A sessão fica `active`, os fatos anteriores do lote já foram gravados e cada nova tentativa duplica.
- **Onde:** `src/symbiote/core/reflection.py:550`, `:586-596`; `src/symbiote/core/kernel.py:587`.
- **Correção:** validar cada fato e descartar o inválido; fechar a sessão num `finally`.

### M-5 · O dream desativa memórias distintas que só compartilham tags — Alto com dream ligado · M · R
- **O que acontece:** no modo `light`, quatro `constraint` diferentes com as mesmas tags viraram uma; três foram desativadas. `_PROTECTED_TYPES` só vale no Prune. O dream vem desligado por padrão.
- **Onde:** `src/symbiote/dream/phases.py:249-285`.
- **Correção:** no modo determinístico, reconciliar só quase-duplicatas e respeitar os tipos protegidos.

### M-6 · Ciclo de vida do dream — Médio · M · R
- Em `per_symbiote` o engine é recriado a cada close, então a guarda de concorrência nunca vê threads ativas: 3 dreams simultâneos num teste.
- Em `global` o engine é construído com a config do primeiro symbiote e ignora a dos demais.
- `dream(dry_run=True)` conta como dream e zera o contador de sessões.
- `GeneralizePhase` cria outra memória generalizada a cada ciclo.
- **Onde:** `src/symbiote/core/kernel.py:946-969`; `src/symbiote/dream/engine.py:65-67`, `:130-165`; `src/symbiote/dream/phases.py:327-393`.

### M-7 · Evolver e rollback do harness — Médio · M · R
- `rollback()` reativa `version < atual` e ignora `parent_version`: depois de reverter v2 e criar v3, reverter v3 reativa a v2 rejeitada.
- A primeira versão evoluída não tem baseline e nunca é revertida, mesmo com média 0,0 em 60 sessões.
- O `LEFT JOIN` com traces multiplica linhas: 1 falha e 1 sucesso satisfazem "5 falhas + 3 sucessos".
- `MIN_EVOLUTION_INTERVAL_DAYS` nunca é lido.
- **Onde:** `src/symbiote/harness/versions.py:152-157`; `src/symbiote/harness/evolver.py:41`, `:250-257`, `:338-360`.

### M-8 · Store e loader de skills discordam sobre a identidade da skill — Médio · P/M · R
- Uma skill em quarentena com `name:` igual ao de uma skill curada a esconde do prompt.
- Skill criada sem frontmatter fica em disco, invisível ao loader, sem contar no limite de quarentena.
- `skill_manage` não chama `refresh()`: a skill criada só aparece depois de um review ou restart.
- `max_active_skills` é armazenado e nunca aplicado (L).
- **Onde:** `src/symbiote/skills/store.py:231-265`; `src/symbiote/skills/loader.py:222`, `:247-252`; `src/symbiote/skills/tool.py:138-202`.

### M-9 · `shutdown()` não espera as threads de background — Médio · P · R
- **O que acontece:** num processo curto (padrão da CLI), o review final de skills morre antes de o LLM responder. Num processo longo, a thread grava depois do shutdown e o audit falha com "Cannot operate on a closed database".
- **Onde:** `src/symbiote/core/kernel.py:1268-1270`.
- **Correção:** registrar as threads e fazer `join` com timeout antes de fechar o storage.

### M-10 · Corrida em `get_or_create_session` por `external_key` — Médio · P · C
- **O que acontece:** com 8 threads e a mesma chave nova, 18 de 20 rodadas criaram mais de uma sessão (até 8). Pela API HTTP, com 40 requisições simultâneas, não reproduzi: saiu uma sessão só.
- **Onde:** `src/symbiote/core/session.py:54-81`; índice não único em `src/symbiote/adapters/storage/sqlite.py:33`.
- **Correção:** índice `UNIQUE(symbiote_id, external_key)` com `INSERT ... ON CONFLICT`.

---

## T — Tools e integrações

### T-1 · Tools registradas pela API e pela CLI não chegam ao chat — Alto · M · C e R
- **API (C):** `POST /symbiotes/{id}/tools` grava num `ToolGateway` próprio da API, só em memória. O `/chat` usa o gateway do kernel, que não enxerga a tool.
- **CLI (R):** `symbiote tools add` registra no gateway do processo, que termina em seguida. O id fica autorizado no banco, mas `tools exec` responde "Tool not registered".
- **Onde:** `src/symbiote/api/http.py:457`, `:499-507`; `src/symbiote/cli/main.py:521-528`.
- **Correção:** persistir descriptor e `HttpToolConfig` numa tabela e recarregar na subida do kernel; a API usar `kernel.tool_gateway`.

### T-2 · Discovery gera tools sem query params e descarta rotas — Alto · M · R
- **O que acontece:** parâmetros `in: query` aparecem no schema mostrado ao LLM, mas não entram na URL: `search_items(q='red shoes')` chegou ao servidor como `GET /api/search`. No scanner de código, `/items`, `/items/{id}` e `/items/{id}/{rev}` viram o mesmo `get_items` e só o primeiro sobrevive, sem erro.
- **Onde:** `src/symbiote/discovery/service.py:85-91`, `:145`, `:217`, `:386-391`; `src/symbiote/discovery/loader.py:152-157`.
- **Correção:** guardar o `in` de cada parâmetro e montar a query com `urlencode`; incluir os path params no `tool_id` e registrar colisões.

### T-3 · Os modos `index` e `on_demand` mandam o LLM chamar tools bloqueadas — Médio · P/M · R
- **O que acontece:** o prompt instrui a usar `get_tool_schema` ou `search_memories`, mas nada as autoriza; o PolicyGate bloqueia. Depois de autorizada, `get_tool_schema` devolve o schema de qualquer tool registrada.
- **Onde:** `src/symbiote/core/context.py:521-542`; `src/symbiote/environment/tools.py:476-544`.
- **Correção:** autorizar as meta-tools implicitamente quando o modo está ativo, e filtrar pelo que o symbiote pode usar.

### T-4 · `EnvironmentManager.configure` perde atualizações e lê `0.0` como padrão — Médio · M · R
- **O que acontece:** 16 autorizações simultâneas deixaram 3 linhas de config e 3 de 16 tools. `configure(memory_share=0.0)` grava 0.0, mas a leitura devolve 0.40 (`float(x or 0.40)`).
- **Onde:** `src/symbiote/environment/manager.py:125-134`, `:238-334`, `:589-590`.
- **Correção:** índice único com upsert numa transação, API atômica `add_tools`/`remove_tools`, e tratar zero como valor válido.

### T-5 · `load_discovered_tools` e `load_mcp_tools` substituem as tools autorizadas — Médio · P · R
- **O que acontece:** `['search_articles','search_memories']` vira `['get_x']`. A documentação diz que os ids são "automatically authorized".
- **Onde:** `src/symbiote/core/kernel.py:1055`, `:1079`.

### T-6 · O browser instala um handler de SIGTERM que engole o sinal — Alto para hosts em container · P · R
- **O que acontece:** basta chamar `register(kernel, browser_backend="chromium")`, mesmo sem Playwright. O processo continua vivo depois do SIGTERM e só sai com SIGKILL.
- **Onde:** `src/symbiote/browser/browser/supervisor.py:37-39`, `:59-61`.
- **Correção:** encadear o handler anterior depois do cleanup, ou usar só `atexit`.

### T-7 · Browser: itens menores — Baixo/Médio · R
- A `policy` só chega a `browser_navigate`; `web_extract` ignora a blocklist. `ads.com.` com ponto final fura a blocklist.
- Sem os extras, `register()` não falha: registra 9 tools e o erro só aparece na chamada.
- A idempotência é por `id(kernel)`: um kernel novo no mesmo endereço fica sem tools.

---

## A — Robustez da API HTTP

### A-1 · Entrada inválida vira erro 500 — Médio · P · C
- **O que acontece:** sete casos na instância local devolveram 500 em vez de 4xx:
  - mensagem com `role` inválido;
  - sessão para symbiote inexistente (`IntegrityError` de chave estrangeira);
  - config com `max_tool_iterations` negativo ou acima do teto;
  - `tool_mode` inválido;
  - `memory_share=5`;
  - chave de API com `role` inválido.
- **Causa:** só `SymbioteError` tem handler. `pydantic.ValidationError` dos modelos de domínio, `ValueError` e `sqlite3.IntegrityError` não. Depois do 500 o uvicorn fecha a conexão.
- **Onde:** `src/symbiote/api/http.py:102-114`.
- **Correção:** handlers para essas três exceções e tipos `Literal` nos modelos de request.

### A-2 · Estado e efeitos colaterais no módulo — Médio · M · C e L
- **503 no primeiro acesso (C):** num processo novo, toda rota autenticada responde `503 Authentication not configured` até alguma rota inicializar o adapter.
- **Dois adapters, dois key managers (L):** `get_adapter()` e `get_kernel()` abrem conexões separadas para o mesmo arquivo.
- **Logging no import (L):** `logging.basicConfig(level=DEBUG)` e `httpx`/`openai` em DEBUG ao importar `symbiote.api.http`.
- **Onde:** `src/symbiote/api/http.py:64-68`, `:439-472`.
- **Correção:** inicializar tudo num `lifespan` do FastAPI, com um único kernel como dono do storage.

### A-3 · Itens menores
- Chat sem LLM configurado devolve `400 [chat] No runner available for intent 'chat'`, sem dizer o que configurar — C.
- `/health` executa `git rev-parse` a cada chamada (2,4 ms contra 0,6 ms das outras leituras) — C. Basta resolver o commit uma vez.
- Nome de symbiote vazio é aceito — C.
- A API não tem streaming, embora o kernel suporte `on_token` — L.

---

## C — CLI

### C-1 · Markup do Rich derruba comandos — Médio · P · R
- **O que acontece:** conteúdo do usuário vai para `console.print` com markup ligado. Uma memória com `[/tmp]` derruba `memory search`; uma mensagem com `[/INST]` derruba `export session`, `reflect` e `session close` (a sessão é fechada e o comando sai com erro).
- **Onde:** `src/symbiote/cli/main.py:196-200`, `:327`, `:436`, `:467`, `:485`.
- **Correção:** `rich.markup.escape()` em todo texto dinâmico.

### C-2 · Erros de entrada viram traceback — Baixo · P · R
- `--persona-json` inválido, symbiote inexistente em `session start`, `--importance 5` e `skills list --root /inexistente`.

### C-3 · Cobertura de 48% em `cli/main.py` — Baixo · M · C

---

## D — Documentação

Divergências entre o que está escrito e o que acontece ao executar. As marcadas com C eu conferi; as demais são R.

| Onde | O que diz | O que acontece |
|---|---|---|
| `INFRA.md:7` | Local em `localhost:8003`, unit `symbios.service` | A porta do Symbiote é 8008 e a unit não existe (resposta do devops) — C |
| `QUICKSTART.md:177`, `:323`; `docs/HOST_INTEGRATION.md:72`, `:121`, `:611` | `kernel.tool_gateway.register(...)` | O método não existe. Os reais são `register_http_tool` e `register_descriptor` — C |
| `QUICKSTART.md:206`; `HOST_INTEGRATION.md:106` | `from forge_llm.application.tools import McpToolset` | `ImportError` — C |
| `QUICKSTART.md:1` | "Symbiote v0.2.27" | O pacote é 0.7.3 — C |
| `README.md:88-90` | `curl` sem `Authorization`, porta 8000 | 401. O QUICKSTART usa 8008 com header — C |
| `README.md:167-179` | Hooks de auditoria | Nunca disparam (E-2) — C |
| `README.md:100-108` | Registra a tool `search` e segue | A tool nunca é autorizada; `q` com espaço falha (S-7) |
| `README.md:227-242` | `register(...)` habilita "10 new tools" | Registra 9; sem `crawl_backend` não há `web_crawl` |
| `README.md:238` | Policy aplicada a todas as tools web | Só `browser_navigate` (T-7) |
| `QUICKSTART.md:244-251` | `context_mode="on_demand"` basta | As tools de busca ficam bloqueadas (T-3) |
| `QUICKSTART.md:125` | Lista de builtins | Faltam `bash`, `spawn`, `skill_manage`, `skill_view` |
| `HOST_INTEGRATION.md:724` | `symbiote show <SYMBIOTE_ID>` | `show` recebe `SESSION_ID QUERY` |
| `src/symbiote/cli/main.py:673`, `:685`, `:765` | Servidor padrão `localhost:8000` | O servidor sobe em 8008 — C |
| `setup_env.sh` | Setup do ambiente | É um template de outro projeto: instala forgebase e forgellmclient — C |

**Correção:** além de corrigir os trechos, rodar os blocos de código dos docs no CI.

---

## Q — Qualidade, CI e higiene

### Q-1 · Os gates configurados não rodam no CI — Médio · P · C
- O `Dockerfile:39` roda `pytest -q` sem `--cov`; o `fail_under = 85` nunca é aplicado. A cobertura real está em 85,48%, margem de meio ponto.
- O mypy `strict` do `pyproject.toml` não roda. Um caminho gradual: ligar no CI sem `strict` (55 erros), depois resolver os 305 `type-arg`.
- O `ruff.toml` da raiz tem precedência sobre o `[tool.ruff]` do `pyproject.toml`, que fica sem efeito. Ele é um template de outro projeto (py311, referências a `forgebase`).

### Q-2 · A suíte não pega nenhum dos achados deste relatório — Alto · M · C
- 1758 testes passam com todos os defeitos acima presentes. Dois testes afirmam o comportamento defeituoso:
  - `test_slow_tool_times_out` leva os 2 s da tool (E-4);
  - `tests/unit/test_discovery_service.py:131-144` exige que o status aprovado seja preservado (S-5).
- **Correção:** converter as reproduções em testes de regressão, começando por isolamento entre symbiotes e autenticação por rota.

### Q-3 · Arquivos grandes com caminhos duplicados — Médio · G · C
- `runners/chat.py` tem 1759 linhas com sync e async duplicados (origem de E-11). `api/http.py` tem 1610 e `core/kernel.py` 1270.
- O kernel expõe setters que alteram atributos privados do runner (`kernel.py:249-319`).

### Q-4 · Higiene do repositório — Baixo · P · C
- `symbiote.db` e `.context/` estão versionados apesar de constarem no `.gitignore`. `.coverage` também está versionado e nem consta lá.
- Na raiz: `clark-*.txt` (72 KB), `harness_plan.md` e `env/git-dev.zip`.
- O `Dockerfile` não define `USER`: o container roda como root.

### Q-5 · Migrações sem versão de schema — Baixo · M · R
- São cerca de 35 `ALTER TABLE` com `try/except` a cada boot. Funciona: 22 dos 24 schemas históricos convergem. Só os dois do primeiro dia falham, por um `CREATE INDEX` fora do `try`.

---

## P — Desempenho

### P-1 · Tabelas quentes sem índice — Médio · P · R
- **O que acontece:** não há índice em `messages(session_id)`, `memory_entries(symbiote_id, ...)` nem `sessions(symbiote_id)`. Com 400 mil mensagens de outras sessões, `kernel.message()` foi de 0,3 ms para 70,9 ms, dentro do lock único do adapter.
- **Onde:** `src/symbiote/adapters/storage/sqlite.py:21-57`.
- **Correção:** índices em `messages(session_id, created_at)`, `memory_entries(symbiote_id, is_active, type)`, `sessions(symbiote_id, status, ended_at)` e `execution_traces(session_id)`.

### P-2 · Sem retenção — Médio · M · R
- `messages` e `execution_traces` nunca são podadas pelo kernel. O único `DELETE` de retenção é um comando manual da CLI.

### P-3 · Escrita com commit por statement — Baixo · P · C
- Uma escrita custa 6,4 ms contra 0,6 ms de uma leitura. `PRAGMA synchronous=NORMAL` com WAL reduz isso sem risco de corrupção; o custo é poder perder as últimas transações numa queda de energia.

---

## Sequência sugerida

**Onda 1 — isolamento e exposição.** S-1, S-2, S-3, S-7, S-6, A-1. São correções pequenas ou médias e fecham o que permite um tenant ou um anônimo ler dados de outro.

**Onda 2 — o agente funciona como prometido.** E-1, E-5, E-4, E-6, E-7, E-8, M-1, M-2, M-3, E-2. Aqui estão os casos em que uma conversa comum dá resultado errado sem erro visível.

**Onda 3 — aprendizado confiável.** E-3, M-4 a M-9, S-4, S-5. Faz sentido depois da onda 2: enquanto o score vier errado (E-3), evolver e tuner otimizam contra ruído.

**Onda 4 — integrações, documentação e CI.** T-1 a T-7, D, Q, P, C.

Em todas as ondas: escrever o teste que falha antes da correção (Q-2).

## O que está bom e deve ser preservado

- **Pipeline de staging:** commit exato, dependências com lock, verificação de proveniência do `forge-llm`, backup do SQLite e rollback por health.
- **Isolamento nas rotas autenticadas:** oito tentativas entre tenants, oito 403.
- **PolicyGate deny-by-default com audit** em todos os caminhos de execução (`environment/policies.py:73-88`).
- **Tokens só chegam ao usuário depois de validados:** o stream é bufferizado, então retry não duplica saída (`runners/chat.py:1169-1193`).
- **Protocolos de agente versionados e fail-closed**, com estado isolado por execução (`agent_protocols/`).
- **Escrita atômica e validação de nome de skills:** path traversal bloqueado (`skills/store.py:93-155`).
- **Reflexão LLM com fallback** para palavra-chave e audit do erro (`core/reflection.py:193-215`).
- **Validação fail-fast em `configure`**, com mensagem acionável.
- **Subpacote `browser` opt-in:** `import symbiote` não carrega dependências extras.
- **Robustez sob carga na instância local:** 200 escritas concorrentes sem erro nem perda.

## Limites desta análise

- **Sem LLM real.** Tudo que depende de modelo usou um LLM falso. O comportamento de providers reais (rejeição de histórico, 429, truncamento) não foi exercitado.
- **Extras não instalados.** `extract`, `browser` e `stealth` ficaram de fora; as tools de browser foram revisadas sem Chromium rodando.
- **Itens R não foram refeitos por mim.** Conferi por conta própria os de maior gravidade de cada revisor (S-1, S-3, S-7, E-1, E-2, E-5 a E-8, M-1, M-10) e todos se confirmaram.
- **S-6 foi confirmado só no mecanismo,** não pela página completa do Console.
- **O estágio `quality` do Docker não foi construído;** os gates rodaram no `.venv` local.
- **24 testes pulados** (e2e que exigem serviços externos) não foram executados.
- **Staging e produção** não foram tocados.
