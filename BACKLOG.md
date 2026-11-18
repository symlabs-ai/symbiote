# Backlog — Symbiote

> Popule via `/backlog <descrição>` em maintenance mode.

## Ideias

| # | Descrição | Origem | Prioridade | Status |
|---|-----------|--------|------------|--------|
| B-36 | Tool Loop — custo tracking: tokens por chamada ao modelo, com symbiote, sessão e finalidade, tokens de cache e teto de gasto por turno | 2026-03-19, dev | alta | ✅ resolvido: medição e atribuição na v0.8.10; teto por turno (`max_turn_tokens`, `max_turn_llm_calls`, `stop_reason="budget"`) na v0.8.11 |
| B-41 | Kimi K2 context limit — plano pago resolveu o 500, mas context_length_exceeded persiste com 75+ tools no index; queries multi-step estouram. Bloqueador para tool loops complexos. Precisa de semantic loading ou redução de tags | 2026-03-20, harness | alta | monitoramento |
| B-42 | Tool descriptions genéricas — melhorar summaries no OpenAPI do YouNews (ex: "Item Action" → "Change item status") | 2026-03-20, harness | média | cross-repo:younews |
| B-44 | Validar supressão de narração intermediária no YouNews staging após deploy v0.17.31+ | 2026-03-20, harness | média | cross-repo:younews |
| B-45 | Test harness pytest — validar tests/e2e/test_kimi_tool_loop.py (requer YouNews + SymGateway rodando) | 2026-03-20, harness | baixa | |
| B-46 | **[BUG] 4 testes pré-existentes falhos em test_generation_settings + test_instant_mode**: assertions mockam `_run_instant(ctx, on_token)` sem o kwarg `llm_config=None` adicionado na v0.5.0. Fix trivial: atualizar 4 assert chamadas pra `_run_instant(ctx, on_token, llm_config=None)`. Não é regressão funcional, é só drift de teste. Ver F3 detalhamento abaixo. | 2026-05-27, sprint5-followup | baixa | ✅ resolvido 2026-05-28 |
| B-47 | **[BUG] `SQLiteAdapter` single-cursor thread safety**: conexão única compartilhada entre threads. Race em writes/reads concorrentes manifesta como `sqlite3.InterfaceError: bad parameter or other API misuse` (visto no teste H1 do Sprint 4.1). Mitigado em produção single-user (rotinas mid-session são sequenciais), mas qualquer nova feature paralela morre nele. Fix: lock em `SQLiteAdapter` ou connection pool. Ver F4 detalhamento. | 2026-05-27, sprint5-followup | média | ✅ resolvido 2026-05-28 (opção 1: lock) |
| B-79 | **Memória por usuário final (`subject_key`)**: a memória é particionada só por `symbiote_id`, então um symbiota que atende várias pessoas mistura o que aprende de cada uma. Adicionar uma chave opaca de usuário final à sessão e à memória: a sessão lê a memória compartilhada mais a do próprio usuário e grava só na do usuário. Bloqueia a publicação de symbiotas para terceiros no estúdio de symbiotas. Ver detalhamento em "Detalhamento de features". | 2026-11-16, estúdio de symbiotas | alta | ✅ resolvido 2026-11-16 (v0.8.9). Ver "Como ficou" no detalhamento |
| B-80 | **[BUG] `close_session` descarta a nota do host**: `_persist_score` recalcula `final_score` só com a nota automática e sobrescreve o que `report_feedback` gravou antes do encerramento | 2026-11-17, pedido do SymbiOS | alta | ✅ resolvido 2026-11-17 (v0.8.10): o `final_score` do encerramento combina a nota automática com a nota do host já dada; a sessão pontuada antes de encerrar também passa a contar uma vez para o score da versão do harness |
| B-81 | **Procedimentos em teste nunca são usados**: `listable_skills` só lista os ativos e `skill_view` recusa os em quarentena, então `skill_auto_promote_threshold` não tem como disparar. Opção por symbiote para listar os em teste, marcados, e deixar `skill_view` lê-los contando o uso | 2026-11-17, pedido do SymbiOS | alta | ✅ resolvido 2026-11-17 (v0.8.10): `EnvironmentConfig.skill_trial_listing` (padrão desligado). Ligado, as skills em teste aparecem marcadas `status="trial"`, `skill_view` as lê e cada leitura conta para a promoção |
| B-82 | `<symbiote-chat>`: no modo inline o histórico é marcado como carregado ainda com o `NoopAdapter`, e um adapter definido depois nunca o carrega; falta um `reload()` público. O arquivo do widget também não vai no pacote Python instalado | 2026-11-17, pedido do SymbiOS | média | ✅ resolvido 2026-11-17 (v0.8.10): o histórico é carregado uma vez, de um adapter de verdade, quando o painel está aberto; `reload()` público; `symbiote.ui.widget_path()` / `widget_version()` |
| B-83 | `MemoryStore.reactivate(memory_id)`: `deactivate` não tem volta e `update` recusa uma memória inativa | 2026-11-17, pedido do SymbiOS | baixa | ✅ resolvido 2026-11-17 (v0.8.10): `MemoryStore.reactivate`, com as regras de symbiote e de partição do `deactivate` |
| B-84 | Contexto público de spawn (`current_spawn()`): ganchos e tools disparam para o symbiote delegado sem dizer quem delegou; o host hoje lê `_spawn_depth`, que é privado | 2026-11-17, pedido do SymbiOS | média | ✅ resolvido 2026-11-17 (v0.8.10): `symbiote.environment.exec_context.current_spawn()` devolve o delegado, quem delegou, a raiz da cadeia e a profundidade |
| B-85 | Aviso e espera do fim do dream (`set_dream_callback`, `wait_dream`): o host hoje espera a thread em `kernel._dream_threads`, que é privado | 2026-11-17, pedido do SymbiOS | média | ✅ resolvido 2026-11-17 (v0.8.10): `kernel.set_dream_callback(fn)` e `kernel.wait_dream(symbiote_id, timeout)` |
| B-86 | Modelo do dream por symbiote: a reflexão já escolhe entre o modelo auxiliar e o principal; o dream usa sempre o auxiliar | 2026-11-17, pedido do SymbiOS | baixa | ✅ resolvido 2026-11-17 (v0.8.10): `EnvironmentConfig.dream_llm` (`aux` padrão, `main`) |
| B-87 | Resumo de sessão: `_generate_summary` emenda as mensagens num parágrafo. Modo por symbiote: o de hoje (padrão), resumo com o modelo auxiliar, ou nenhum | 2026-11-17, pedido do SymbiOS | baixa | ✅ resolvido 2026-11-17 (v0.8.10): `EnvironmentConfig.session_summary_mode` (`concat` padrão, `llm`, `off`) |
| B-88 | Descoberta de tools a partir de uma descrição OpenAPI já em mãos (um `dict`), além de `{url}/openapi.json` | 2026-11-17, pedido do SymbiOS | baixa | ✅ resolvido 2026-11-17 (v0.8.10): `kernel.discover_tools(symbiote_id, spec={...}, base_url=...)`. O montador de chamada HTTP não foi exposto: a execução segue pelo gateway de tools |
| B-89 | Conhecimento sob demanda por item: hoje `context_mode` vale para o symbiote inteiro. Marca por item: fora da busca automática, alcançável por `search_knowledge` | 2026-11-17, pedido do SymbiOS | baixa | ✅ resolvido 2026-11-17 (v0.8.10): `register_source(..., auto_recall=False)` e `knowledge.set_auto_recall` |
| B-90 | **Aprovação adiada**: o callback de aprovação só responde sim ou não e segura a thread do turno. Passa a poder responder "pendente": a engine grava o pedido, o turno termina, e `kernel.resolve_pending(...)` executa depois no contexto original e entrega o resultado no turno seguinte. Listar, expirar e sobreviver a reinício. Era o item "caixa de aprovação assíncrona" deixado fora do B-79 | 2026-11-17, pedido do SymbiOS | alta | ✅ resolvido 2026-11-17 (v0.8.10): `ApprovalDecision.defer`, tabela `pending_approvals`, `kernel.resolve_pending` / `expire_pending`, `inspector.list_pending`, entrega do resultado no turno seguinte. Ver `docs/HOST_INTEGRATION.md`, "Deferring the decision" |
| B-91 | Busca e crawl sem SymGateway e sem mecanismo fixo: os backends de `web_search` e de crawl só existem via gateway, com Brave preso no código. O host passa um provedor dele ou o nome de um provedor registrado | 2026-11-17, plano dos pedidos | alta | ✅ resolvido 2026-11-17 (v0.8.10): `search_backend` / `extract_backend` / `crawl_backend` aceitam objeto do host ou nome registrado (`register_search_provider`...); `brave_api` e `searxng` vão direto ao serviço. Sem motor padrão: `search_routing` sem provedor para `web_search` agora é erro |
| B-92 | `ForgeLLMAdapter` envia `project_slug: "symbiote"` em toda chamada, para qualquer provedor: detalhe do SymGateway num adapter genérico | 2026-11-17, plano dos pedidos | baixa | ✅ resolvido 2026-11-17 (v0.8.10): o slug só vai com `provider="symgateway"` (padrão mantido) e o host escolhe o dele com `project_slug=` |
| B-93 | Objectives no `SymGatewayLLMAdapter`: aceitar `objective` no lugar de `model`, listar os objectives da chave e devolver o que roteou. Só dentro do adapter | 2026-11-17, pedido do SymbiOS | baixa | ✅ resolvido 2026-11-17 (v0.8.10): `objective=` no lugar de `model=`, `list_objectives()`, `with_objective()`, `on_routed` / `last_routing`. Só no adapter |
| B-94 | `forge_llm` reduz o uso do provedor a três contadores (`TokenUsage`): os tokens de cache da Anthropic e da OpenAI não chegam ao `ForgeLLMAdapter`, e com a Anthropic o `prompt_tokens` fica sem a parte que veio do cache. A engine já lê e grava esses contadores quando chegam (`usage_from_provider`); falta o `forge_llm` repassar o `usage` do provedor, com e sem streaming | 2026-11-17, adendo ao pedido do SymbiOS | média | ✅ resolvido 2026-11-17 (v0.8.10): `forge_llm` v0.7.17 repassa o uso do provedor em `TokenUsage.raw` e o entrega no fim do stream com `include_usage`; a v0.7.18 (`b26b5e3`) acrescenta as notas de atualização. O Symbiote fixa a v0.7.18 e o `ForgeLLMAdapter` lê de lá |
| B-95 | **Sessão presa a uma revisão nomeada da configuração**: uma mudança na persona, nas tools ou na configuração vale na hora para todas as conversas, inclusive as que estão no meio; não há como testar antes, voltar atrás, auditar com que configuração uma resposta foi dada nem comparar duas configurações. `create_revision` tira um retrato imutável (persona, `EnvironmentConfig`, conhecimento) e `start_session(..., revision=)` prende a sessão a ele. Substitui o pedido de override por turno do SymbiOS, recusado | 2026-11-17, réplica do SymbiOS | alta | ✅ resolvido 2026-11-17 (v0.8.11). Ver "Revisions" em `docs/HOST_INTEGRATION.md` |
| B-96 | **Restringir as tools de uma sessão, só para menos**: o mesmo symbiote atende públicos diferentes (dono, clientes logados, visitantes), cada um com um conjunto de tools, e o host muda essas regras sem publicar outra revisão. `start_session(..., tools_allowed=[...])` e `set_session_tools` | 2026-11-18, pedido do SymbiOS | alta | ✅ resolvido 2026-11-18, na release seguinte à v0.8.11: a sessão vê e chama só a interseção com o que a configuração autoriza; vale para as meta-tools de modo (`environment.mode_tools`) e acompanha a delegação. Ver `docs/HOST_INTEGRATION.md`, "Narrowing a session's tools" |
| B-97 | **Delegação quando o colega não tem o mesmo rótulo**: a sessão presa a `L` delegava para a configuração atual do colega (o rascunho) quando ele não tinha a revisão `L`. Revisão padrão por symbiote (`set_default_revision`) como destino, e modo estrito por sessão (`delegation="strict"`) que recusa a delegação sem revisão | 2026-11-18, pedido do SymbiOS | alta | ✅ resolvido 2026-11-18, na release seguinte à v0.8.11. Ver `docs/HOST_INTEGRATION.md`, "Delegation between symbiotes published at different times" |
| B-98 | **Ler uma descrição OpenAPI sem gravar nem autorizar**: `discover_tools(spec=)` + `load_discovered_tools` servem a quem integra a própria API, não a quem gerencia integrações com APIs de terceiros (dono no workspace, autorização por pessoa, credencial por integração, nomes com prefixo). Funções públicas e puras `parse_openapi` e `http_config_for` | 2026-11-18, pedido do SymbiOS | média | ✅ resolvido 2026-11-18, na release seguinte à v0.8.11: `symbiote.discovery.parse_openapi`, `http_config_for`, `OpenAPIOperation`; o caminho interno usa as mesmas funções |
| B-99 | **[BUG] Ids de tool são um espaço só por kernel**: `ToolGateway.register_http_tool` substitui a tool de mesmo id, e `DiscoveredToolLoader` só protege as que não são HTTP. Duas descrições com uma operação `search`, carregadas para dois symbiotes, fazem a segunda carga trocar a URL da tool do primeiro (reproduzido em 2026-11-18: depois de carregar para `b`, o `search` de `a` chama o host de `b`). Dentro de um symbiote, a segunda descoberta substitui a primeira e zera a aprovação. Caminhos: recusar a troca de uma tool HTTP por outra de URL diferente, ou id por symbiote no registro | 2026-11-18, achado na avaliação do pedido do SymbiOS | média | |

### Detalhamento dos itens pendentes

**B-27 — Streaming mid-loop.** O `on_token` callback só emite tokens quando não há tool calls. Nas iterações intermediárias do loop, o usuário vê silêncio total até o loop terminar. Para UX aceitável, o host precisa receber tokens em todas as iterações, com indicação de progresso entre os passos.

**B-29 — Human-in-the-loop.** O host poderia classificar tools por nível de risco — tools de leitura rodam sem aprovação, tools de escrita pausam o loop e pedem confirmação. Implementação: callback `on_before_tool_call(tool_id, params) -> bool` e/ou `risk_level` no ToolDescriptor.

**B-30 — Working memory intermediária.** O ChatRunner salva apenas a resposta final na WorkingMemory. Passos intermediários são perdidos. Opções: salvar todos os passos (verbose) ou resumo estruturado (compacto).

**B-32 — max_iterations configurável.** Adicionar `max_tool_iterations: int = 10` no EnvironmentConfig por symbiote, com teto global como safety net.

**B-33 — Timeout.** Dois mecanismos: (1) timeout por tool call no ToolGateway.execute; (2) timeout total do loop no ChatRunner.

**B-34 — Index mode cache.** Cache de schemas já fetchados na sessão do loop para evitar calls repetidas a `get_tool_schema`.

**B-35 — Teste multi-modelo.** Matriz de testes {modelo × modo × query} com métricas de iterações, taxa de sucesso e custo.

**B-36 — Custo tracking.** Medir tokens in/out por iteração, custo acumulado por request, budget máximo por request.

**B-40 — Tool Mode: instant, brief, continuous.** Refatorar `tool_loop: bool` para `tool_mode: Literal["instant", "brief", "continuous"]`. Detalhes no `/kb/ralph-loop-analysis.md`.

### Detalhamento dos itens Meta-Harness (B-60 a B-68)

Documentação completa em `~/dev/kb/engenharia/meta_harness.md`, seção 4 (Backlog de Implementação).

## Implementadas

| # | Descrição | Implementada em | Versão |
|---|-----------|-----------------|--------|
| B-33 | Timeout — per-tool (30s) + loop total (300s) configuráveis per symbiote | 2026-04-01 | 0.2.27 |
| B-29 | Human-in-the-loop — risk_level + approval callback | 2026-04-01 | 0.2.27 |
| B-34 | Index mode cache — loop-local schema cache para index mode | 2026-04-01 | 0.2.27 |
| B-35 | Multi-model test matrix — E2E infra com 3 cenários × 3 modelos | 2026-04-01 | 0.2.27 |
| B-40 | Tool Mode — instant/brief/continuous replaces binary tool_loop | 2026-04-01 | 0.2.27 |
| B-27 | Streaming mid-loop — on_progress + on_stream callbacks | 2026-04-01 | 0.2.27 |
| B-30 | Working memory intermediária — loop summary in WorkingMemory | 2026-04-01 | 0.2.27 |
| B-68 | Memory/Knowledge on-demand — search_memories/search_knowledge tools | 2026-04-01 | 0.2.27 |
| B-69 | Streaming na API HTTP: `POST /sessions/{id}/chat` só responde no fim do turno. O kernel já tem `message_async(on_token=...)`; falta expor por SSE (ou um endpoint `/chat/stream`) e documentar no SDK. Fora do plano de melhorias de 2026-11 (D-17) | 2026-11-13, relatório de melhorias | média | ✅ resolvido 2026-11-14: `POST /sessions/{id}/chat/stream` (SSE `token`/`done`/`error`) e `SymbioteClient.chat_stream()` |
| B-70 | Container roda como root: o `Dockerfile` não define `USER`. Trocar por um usuário sem privilégios exige ajustar as permissões do volume de staging (`staging-symbiote`) antes; por isso ficou fora do plano de melhorias de 2026-11 (D-16) | 2026-11-13, relatório de melhorias | média | |
| B-71 | mypy `strict`: o gate do CI roda sem `strict` (limpo desde 2026-11-13). Com `--strict` restam 416 erros em 57 arquivos: 339 `type-arg` (mecânicos: `dict` → `dict[str, Any]`), 33 `no-untyped-def`, 32 `no-any-return`, 11 `unused-ignore`, 1 `union-attr`. Ao zerar, voltar `strict = true` no `pyproject.toml` (D-18) | 2026-11-13, relatório de melhorias | baixa | |
| B-72 | Unificar os loops sync/async do `ChatRunner` (Q-3, E-11): `_run_instant` (172 linhas), `_run_loop` (297) e `run_async` (315) repetem o mesmo loop. Proposta: o corpo do loop vira uma máquina de estados sem I/O (gerador que emite pedidos de chamada ao LLM e de execução de tools), conduzida por um driver síncrono e outro assíncrono; o instant vira o caso de uma iteração. Rede de segurança: `tests/unit/test_sync_async_parity.py` (TK-32) deve passar sem alteração. Ficou fora do plano de 2026-11 por passar de 2x o tamanho estimado da TK-86 | 2026-11-13, relatório de melhorias | média | ✅ resolvido 2026-11-13: `_run_loop` e `run_async` viraram um corpo só (`ChatRunner._loop_steps`, gerador sem I/O) com dois drivers (`_drive`, `_drive_async`); `test_sync_async_parity.py` passa sem alteração. O `_run_instant` continua separado: já tinha um corpo só (o async o roda numa thread) e regras próprias (sem controller, sem compactação, resposta padrão sem segunda chamada) |
| B-73 | Streaming token a token: `on_token` só recebe o texto final, depois de validado e de uma vez (`ChatRunner._call_llm_sync` guarda o stream num buffer), e nada nos turnos com tools. Com `set_native_tools(True)` e tools no prompt o `ForgeLLMAdapter` nem usa stream. O demo mostra o texto bruto pela aba Ao vivo | 2026-11-13, demo | média | ✅ resolvido 2026-11-14: `kernel.set_token_streaming("live")` (`runners/token_stream.py`); com tools nativas, `ForgeLLMAdapter(stream_tool_calls=True)`. O `SymGatewayLLMAdapter` continua sem stream |
| B-74 | `LongRunRunner._execute_block` cria o `ChatRunner` de cada bloco sem o `agent_protocol` nem o `background_review` do kernel: um host com protocolo nativo (ex.: poolside) roda os blocos no protocolo legado | 2026-11-13, demo | média || ✅ resolvido 2026-11-14: `LongRunRunner(agent_protocol=, protocol_registry=, background_review=)`, passados pelo kernel e repassados ao runner de cada bloco; `kernel.set_agent_protocol()` vale para os dois runners |
| B-75 | O gate de aprovação roda antes da checagem de política: o usuário é consultado sobre uma tool de risco alto que a política vai bloquear em seguida | 2026-11-13, demo | baixa | ✅ resolvido 2026-11-13 (ordem: política, veto do host, aprovação) |
| B-76 | O prompt mostra todas as tools registradas (filtradas só por tag), inclusive as que o symbiote não pode executar; builtins sem tag (`fs_*`, `search_*`, `spawn`) somem se o host usar tags. Falta um modo "só as autorizadas" no `EnvironmentConfig` | 2026-11-13, demo | média || ✅ resolvido 2026-11-14: `EnvironmentConfig.tool_visibility="authorized"` lista só as tools autorizadas e mantém as autorizadas sem tag; o padrão `"tagged"` não muda |
| B-77 | `docs/arch-02-execution.md` descreve heurística por palavra-chave para `tool_mode="auto"` ("pesquise" → long_run), que não existe: o código só olha `planner_prompt` e se há tools autorizadas | 2026-11-13, demo | baixa | ✅ resolvido 2026-11-14: doc e docstring de `_resolve_auto_mode` descrevem as regras reais |
| B-78 | O Console admin (`api/routes/console.py`) e a CLI repetem em SQL cru as leituras que agora existem em `kernel.inspector`; migrar para o inspector | 2026-11-13, demo | baixa | ✅ resolvido 2026-11-14: leituras migradas, inspector ganhou filtros; `/trace`, `/memory`, `/reflections` e `/activity` do Console ficam em SQL (o dashboard lê o JSON cru) |
| H-11 | BenchmarkRunner — task grading (tool_called, param_match, custom) | 2026-04-01 | 0.2.27 |
| H-12 | StructuralEvolver — pluggable strategy registry | 2026-04-01 | 0.2.27 |
| H-13 | CrossSymbioteLearner — tool overlap + version transfer | 2026-04-01 | 0.2.27 |
| B-67 | HarnessEvolver — LLM proposer evolui tool_instructions com guard rails + auto-rollback | 2026-04-01 | 0.2.26 |
| B-32 | max_tool_iterations configurável — per symbiote via EnvironmentConfig, cap 50 | 2026-04-01 | 0.2.25 |
| B-64 | harness_versions — versionamento de textos evolvable por symbiote com rollback | 2026-04-01 | 0.2.25 |
| B-65 | ParameterTuner — auto-calibração tiered (Tier 0-3) com safety caps e logging | 2026-04-01 | 0.2.25 |
| B-60 | SessionScore — auto_score from LoopTrace + user feedback composition | 2026-04-01 | 0.2.24 |
| B-61 | FeedbackPort — protocol para host reportar qualidade de sessão | 2026-04-01 | 0.2.24 |
| B-62 | MemoryEntry de falha — fato procedural determinístico quando loop falha | 2026-04-01 | 0.2.24 |
| B-63 | Context splits configuráveis — memory_share/knowledge_share per symbiote | 2026-04-01 | 0.2.24 |
| B-66 | LoopTrace persistence — execution_traces table no SQLite | 2026-04-01 | 0.2.24 |
| B-55 | Parallel tool execution — asyncio.gather (async) + ThreadPoolExecutor (sync) com max_workers=4 | 2026-03-31 | 0.2.22 |
| B-56 | LLM retry with exponential backoff — 3 retries, 1s/2s/4s, only transient errors | 2026-03-31 | 0.2.22 |
| B-57 | Diminishing returns detection + circuit breaker — LoopController com duplicate/failure detection. Cobre B-25 (LLM não sabe parar), B-28 (observability/LoopTrace), B-31 (circuit breaker) | 2026-03-31 | 0.2.22 |
| B-58 | 3-layer compaction — microcompact + loop compact + autocompact. Cobre B-26 (context growth), B-39 (Ralph-inspired compaction) | 2026-03-31 | 0.2.22 |
| B-46 | Prompt Cache Integration — forge_llm prompt_caching=True via EnvironmentConfig | 2026-03-30 | 0.2.20 |
| B-47 | Message Retry + Backoff — exponential backoff no MessageBus handler e respond() | 2026-03-30 | 0.2.20 |
| B-48 | Per-Session Locks — SessionLock sync/async no kernel.message() | 2026-03-30 | 0.2.20 |
| B-49 | Hardened allow_internal — exclude from serialization + audit log | 2026-03-30 | 0.2.20 |
| B-50 | CompositeHook — lifecycle hooks composáveis com error isolation | 2026-03-30 | 0.2.20 |
| B-51 | Delta Streaming — StreamDelta no MessageBus para canais real-time | 2026-03-30 | 0.2.20 |
| B-52 | SessionRecallPort — protocol para busca host-provided em sessões passadas | 2026-03-30 | 0.2.21 |
| B-53 | MemoryCategory — auto-classificação de memórias (ephemeral, declarative, procedural, meta) | 2026-03-30 | 0.2.21 |
| B-54 | Context Compaction — compactação mid-loop de mensagens do tool loop (Ralph-inspired) | 2026-03-30 | 0.2.21 |
| 23 | Deploy Hosted — porta 8008, symbiote.service, nginx + SSL em symbiote.symlabs.ai, CI/CD Gitea Actions | 2026-03-18 | 0.2.4 |
| 7 | MCP Integration — McpToolProvider bridges forge_llm ToolRegistry → ToolGateway; kernel.load_mcp_tools(registry, symbiote_id) | 2026-03-18 | 0.2.4 |
| 24 | DiscoveredToolLoader — bridge entre discovered_tools aprovadas e ToolGateway: kernel.load_discovered_tools() carrega tools com status=approved, registra como HttpTool e autoriza via EnvironmentManager | 2026-03-18 | 0.2.4 |
| 19 | API Key Auth — Bearer token, SHA-256, tenant isolation, admin/user roles | 2026-03-17 | 0.1.8 |
| 20 | Chat Endpoint — POST /sessions/{id}/chat com kernel.message() | 2026-03-17 | 0.1.8 |
| 21 | Multi-tenant Isolation — owner_id enforcement em endpoints | 2026-03-17 | 0.1.8 |
| 22 | Python SDK — SymbioteClient thin HTTP client com httpx | 2026-03-17 | 0.1.8 |
| 14 | SSRF Protection — validação de IP + redirect guard em HTTP tools | 2026-03-17 | 0.1.7 |
| 15 | Untrusted Content Banner — anti-prompt-injection em respostas HTTP | 2026-03-17 | 0.1.7 |
| 18 | Tool Call Pair Consistency — trim alinhado a turn boundaries | 2026-03-17 | 0.1.7 |
| 17 | GenerationSettings — pass-through de temperature/max_tokens/reasoning_effort | 2026-03-17 | 0.1.7 |
| 16 | Async Memory Consolidation — trim imediato + LLM em background thread | 2026-03-17 | 0.1.7 |
| 8 | Tool Error Hints — hint de retry automático em tool results com erro | 2026-03-17 | 0.1.5 |
| 9 | Runtime Context Strip — metadata efêmera no prompt sem poluir histórico | 2026-03-17 | 0.1.5 |
| 3 | MessageRepository port para isolar SQL do ReflectionEngine | 2026-03-17 | 0.1.5 |
| 10 | Memory Consolidation — sumarização automática via LLM quando tokens excedem threshold | 2026-03-17 | 0.1.5 |
| 11 | Subagent Spawning — delegação de tarefas entre Symbiotas com tool set restrito | 2026-03-17 | 0.1.5 |
| 12 | MessageBus — fila async inbound/outbound para desacoplar channels do kernel | 2026-03-17 | 0.1.5 |
| 13 | Progressive Skills — skills como .md no workspace, carregamento lazy por demanda | 2026-03-17 | 0.1.5 |
| 4 | Semantic recall provider (keyword-based MVP implementation) | 2026-03-17 | 0.1.5 |
| 6 | ProcessEngine cache invalidation para multi-worker | 2026-03-17 | 0.1.5 |
| 2 | Interactive chat mode na CLI (loop input/output) | 2026-03-17 | 0.1.5 |
| 5 | Integração com LLM real testada ponta-a-ponta | 2026-03-17 | 0.1.5 |
| 1 | Docker container de referência para modo serviço | 2026-03-17 | 0.1.5 |

---

## Detalhamento de bugs

### B-46 — Drift de testes em test_instant_mode + test_generation_settings

**Sintoma**: 4 testes falham consistentemente no `pytest tests/unit/`:

- `tests/unit/test_generation_settings.py::TestChatRunnerPassesConfig::test_none_config_passes_none`
- `tests/unit/test_instant_mode.py::TestInstantFastPath::test_instant_delegates_to_run_instant`
- `tests/unit/test_instant_mode.py::TestInstantFastPath::test_brief_uses_run_loop`
- `tests/unit/test_instant_mode.py::TestInstantFastPath::test_instant_async_delegates`

**Root cause confirmado**: assertions mockam o call de `_run_instant(ctx, on_token)` sem incluir o kwarg `llm_config=None` que foi adicionado em `v0.5.0` (feature "Per-call llm_config propaga kernel.message → ChatRunner.run → adapter config"). O `ChatRunner._run_instant` agora **sempre** recebe `llm_config=<value or None>` mas os testes não atualizaram.

**Diagnóstico** (exemplo de uma das falhas):

```
Expected: _run_instant(<AssembledContext...>, None)
Actual:   _run_instant(<AssembledContext...>, None, llm_config=None)
```

**Fix sugerido**: atualizar os 4 testes para incluir `llm_config=None` em `assert_called_with(...)`:

```diff
- mock_run_instant.assert_called_with(ctx, None)
+ mock_run_instant.assert_called_with(ctx, None, llm_config=None)
```

**Impacto**: zero em produção (só testes). Pré-existente desde v0.5.0. Sobreviveu a 5 sprints sem ser notado porque não é regressão de comportamento.

**Estimativa**: 4 edits + 1 rerun. <15min.

---

### B-47 — SQLiteAdapter single-cursor thread safety

**Sintoma**: `sqlite3.InterfaceError: bad parameter or other API misuse` quando ≥2 threads chamam `_storage.fetch_one` / `_storage.execute` simultaneamente.

**Manifestação histórica**:

- Apareceu no teste `TestKernelLazyBuildLock::test_concurrent_calls_build_engine_once` (Sprint 4.1, commit `a0c8c40`). O teste foi reescrito pra contornar (mock no `_environment.get_config`) — não corrigiu o adapter.
- Teste `TestSpawnDeduplication::test_concurrent_spawn_for_same_session_returns_single_thread` (mesma origem) precisou de `check_same_thread=False` na fixture pra rodar — mas isso desabilita a guarda, não corrige a race no cursor compartilhado.

**Root cause**: `src/symbiote/adapters/storage/sqlite.py` mantém um único `sqlite3.Connection` em `self._conn`. Métodos `execute` e `fetch_*` fazem `self._conn.execute(sql, params)` direto, sem lock. CPython sqlite3 não é thread-safe pra cursor compartilhado, mesmo com `check_same_thread=False`.

**Impacto atual em produção**:

- Single-user (caso comum): rotinas de `kernel.close_session` rodam sequencial → não atinge a race.
- Background threads que **escrevem** no DB: `MemoryConsolidator`, `BackgroundReviewEngine._write_audit`, `DreamEngine` async. Cada uma roda sozinha (1 thread por callsite), mas se 2 disparam simultâneo (ex: `close_session` chama spawn_final e `_maybe_dream` quase ao mesmo tempo), pode dar conflict.

**Fix sugerido**: 2 opções, qualquer uma viável.

1. **Lock no adapter** (mais simples):

   ```python
   class SQLiteAdapter:
       def __init__(self, ...):
           ...
           self._lock = threading.Lock()

       def execute(self, sql, params=None):
           with self._lock:
               cur = self._conn.execute(sql, params or ())
               self._conn.commit()
               return cur

       def fetch_one(self, sql, params=None):
           with self._lock:
               cur = self._conn.execute(sql, params or ())
               return dict(cur.fetchone()) if cur.fetchone() else None
       # ... idem fetch_all
   ```

   Custo: serializa todas as operações de SQLite (single-user OK, multi-tenant degrada).

2. **Connection pool** (mais escalável):

   ```python
   from queue import Queue
   class SQLiteAdapter:
       def __init__(self, ..., pool_size=4):
           self._pool = Queue()
           for _ in range(pool_size):
               self._pool.put(sqlite3.connect(...))

       def execute(self, sql, params=None):
           conn = self._pool.get()
           try:
               cur = conn.execute(sql, params or ())
               conn.commit()
               return cur
           finally:
               self._pool.put(conn)
   ```

   Custo: mais código, mas escala.

Decisão pendente do mantenedor. Pro caso single-user local-first, opção 1 é suficiente.

**Estimativa**: opção 1 = ~30 LoC + teste de stress concorrente. <2h.

## Detalhamento de features

### B-79 — Memória por usuário final (`subject_key`)

**Para quem é.** Hosts em que um mesmo symbiota conversa com várias pessoas. O caso que motiva o pedido é o estúdio de symbiotas, um produto novo que fica em outro repositório e usa o Symbiote como engine. Nele o dono cria um symbiota e o publica para terceiros, por API key ou por um widget de chat. Cada terceiro precisa ter a própria memória com aquele symbiota, sem ver a dos outros.

**Como é hoje.**

- A tabela `memory_entries` tem `symbiote_id` e `session_id`, e nenhuma coluna que diga de qual pessoa é a memória (`src/symbiote/adapters/storage/sqlite.py:50`).
- `MemoryEntry.scope` aceita o valor `"user"`, mas é só um rótulo: não há campo com a identidade do usuário, e o recall não filtra por ele (`src/symbiote/core/models.py:106`).
- Todas as leituras filtram só por symbiota: `get_relevant`, `get_standing`, `search`, `get_by_type` e `get_by_category` em `src/symbiote/memory/store.py`.
- Tudo o que grava usa `scope="global"`: a reflexão (`src/symbiote/core/reflection.py:358`, `:634`, `:716`), a memória de falha (`src/symbiote/core/kernel.py:2450`), o handoff do long-run (`src/symbiote/core/kernel.py:1741`) e o `teach` (`src/symbiote/core/capabilities.py:93`).
- `Session.external_key` identifica a conversa para o host reencontrá-la. O kernel não o usa para separar memória.

O efeito: o que o visitante A conta ao symbiota entra no recall do visitante B, e qualquer visitante consegue gravar uma "regra" que todos os outros passam a receber no prompt.

**O que se pede.**

1. **Uma chave de usuário final, `subject_key`.** É um texto opaco que o host informa ao criar a sessão e que identifica a pessoa do outro lado. O kernel não interpreta o valor; só compara por igualdade. `NULL` significa "sem usuário final", que é o comportamento de hoje. O nome é uma proposta; o time pode escolher outro.
   - `Session.subject_key`, definido em `start_session(..., subject_key=None)` e em `get_or_create_session`, e imutável depois.
   - `MemoryEntry.subject_key`, com a coluna `memory_entries.subject_key TEXT NULL` e um índice por `(symbiote_id, subject_key)`. A migração vale para SQLite e Postgres.
   - Mesmo limite de tamanho do `external_key` (`MAX_KEY_CHARS`); texto vazio é recusado.

2. **Regra de leitura.** Uma sessão com `subject_key = S` enxerga as memórias compartilhadas (`subject_key IS NULL`) e as de `S`. Uma sessão sem `subject_key` enxerga só as compartilhadas. O filtro precisa estar no SQL: o recall carrega um número fixo de candidatas ordenadas por importância (`_RECALL_CANDIDATES`), e filtrar depois deixaria as memórias de milhares de visitantes expulsarem as compartilhadas dessa lista.

3. **Regra de escrita.** Toda memória gravada em nome de uma sessão com `subject_key = S` recebe `subject_key = S`. Uma sessão com usuário final nunca cria, altera nem desativa uma memória compartilhada ou de outro usuário. Isso inclui o PATCH que a reflexão por LLM faz em memórias existentes: o alvo precisa ser da mesma partição (`_resolve_target_id` e `_apply_llm_facts` em `src/symbiote/core/reflection.py`).

4. **Onde a chave precisa chegar.**
   - `ContextAssembler`: recall e memórias permanentes (`src/symbiote/core/context.py:216` e `_standing_memories`).
   - A tool builtin `search_memories` (`src/symbiote/environment/tools.py:1028`): a chave vem da sessão que está chamando, nunca de um parâmetro que o modelo preenche.
   - `spawn`: a sessão do symbiota delegado herda o `subject_key` da sessão que delegou. Sem isso, a delegação perde a memória do visitante ou grava na partição errada.
   - Reflexão, consolidador (`src/symbiote/memory/consolidator.py:153`), memória de falha, handoff e as capabilities `teach`, `show` e `reflect`.
   - Dream: nenhuma fase junta, generaliza ou reconcilia memórias de partições diferentes. O mínimo aceitável é o dream trabalhar só na partição compartilhada.
   - API HTTP e SDK: `CreateSessionRequest.subject_key` e o equivalente no `SymbioteClient`.

5. **Aprendizado a partir de sessões de terceiros.** Além da memória, o encerramento de sessão alimenta coisas que valem para o symbiota inteiro: o score (`session_scores`), que o tuner, o evolver do harness e o dream leem, e o review de skills. Decidido em 2026-11-16: isso é configurável por uma flag no `EnvironmentConfig`, por symbiota, que diz se as sessões com `subject_key` entram nesses cálculos. Com a flag desligada, a sessão ainda tem score gravado, mas o tuner, o evolver, as fases `mine` e `evaluate` do dream e o review de skills a ignoram. Com a flag ligada, elas contam como as sessões do dono.

6. **Inspeção e exclusão.**
   - `kernel.inspector`: listar memórias filtrando por `subject_key` e listar os usuários finais de um symbiota com a contagem de memórias e sessões.
   - Uma operação que apaga tudo de um usuário final num symbiota (memórias, sessões e mensagens), para o dono atender um pedido de exclusão de dados.

7. **Compatibilidade.** Sem `subject_key`, nada muda: as linhas existentes ficam com `NULL` e todo host atual continua como está. O parâmetro novo no `MemoryPort` é opcional e por nome. Um host que traz o próprio adapter de memória sem suporte à chave recebe um erro claro ao abrir uma sessão com `subject_key`, em vez de a partição ser ignorada em silêncio.

**Critérios de aceite.**

- Dois usuários finais do mesmo symbiota: o que um ensina não aparece no contexto, no `search_memories` nem na reflexão do outro.
- Os dois recebem as memórias compartilhadas que o dono ensinou numa sessão sem `subject_key`.
- Uma sessão com `subject_key` não consegue alterar nem desativar uma memória compartilhada, inclusive pelo PATCH da reflexão por LLM.
- Com mais memórias de usuários finais do que `_RECALL_CANDIDATES`, as memórias compartilhadas continuam entrando no recall.
- Numa delegação por `spawn`, o symbiota delegado lê e grava na partição do mesmo usuário final.
- Um ciclo de dream não desativa, junta nem generaliza memórias de partições diferentes.
- A exclusão de um usuário final remove as memórias, sessões e mensagens dele e deixa as dos outros intactas.
- Um banco criado antes da migração abre e se comporta como antes, em SQLite e em Postgres.
- Os caminhos síncrono e assíncrono se comportam igual (`tests/unit/test_sync_async_parity.py`).
- `docs/arch-03-memory.md` e `docs/HOST_INTEGRATION.md` descrevem a chave e as duas regras.

**Fora do escopo.** Ficam para tickets próprios: API keys restritas a um symbiota publicado, o subconjunto de tools que um symbiota publicado pode usar, limite de uso por chave e a caixa de aprovação assíncrona do dono.

**Decisões em aberto.**

- O valor padrão da flag do item 5. A proposta é desligado: conversas de terceiros não mudam o harness nem geram skills, a menos que o dono ligue.
- Se o dono, numa sessão sem `subject_key`, deve ter um modo de recall que enxergue as partições dos usuários finais. A proposta é não: ele consulta pelo inspector.

**Como ficou (2026-11-16).** Implementado conforme os itens 1 a 7, com estes pontos decididos na implementação:

- As duas decisões em aberto seguiram a proposta: `learn_from_subject_sessions` nasce desligado, e a sessão sem `subject_key` lê só a partição compartilhada.
- A regra de escrita está em `MemoryStore.store()`, que tira a partição da sessão da entrada. Nenhum escritor (reflexão, memória de falha, handoff, consolidador, `learn`) passa a chave, e um escritor novo já nasce dentro da regra.
- Dream: `reconcile` e `generalize` trabalham só na partição compartilhada. `prune` cobre todas as partições, porque julga cada memória sozinha e sem ele a memória dos usuários finais só cresceria.
- Item 7: o kernel sempre usa o próprio `MemoryStore`, então não há host com adapter de memória sem suporte passando por ele. Os componentes que aceitam um `MemoryPort` de fora (`ContextAssembler`, `ReflectionEngine`) só passam `subject_key` a um port que tenha `subject_for_session`; um port antigo é chamado como antes e lê a memória sem partição. O erro explícito pedido no item 7 não foi implementado, por não haver caminho no kernel que chegue a ele.
- Além do pedido: o handoff de long-run só é retomado pela mesma pessoa; `ExportService.export_memory` e o `SemanticRecallProvider` leem só a partição compartilhada por padrão; `kernel.purge_subject` também apaga traces, scores e linhas de auditoria das sessões da pessoa.
- Não coberto por `purge_subject`: skills escritas a partir das sessões da pessoa e scores já somados às versões do harness. Os dois só existem com `learn_from_subject_sessions` ligado.
- Testes: `tests/unit/test_subject_key.py` (um por critério de aceite) e `test_subject_key_migration_upgrades_existing_rows` em `tests/unit/test_postgres_adapter.py`.
