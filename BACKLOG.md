# Backlog — Symbiote

> Popule via `/backlog <descrição>` em maintenance mode.

## Ideias

| # | Descrição | Origem | Prioridade | Status |
|---|-----------|--------|------------|--------|
| B-25 | Tool Loop — LLM não sabe parar | 2026-03-19, dev | alta | |
| B-26 | Tool Loop — context growth mid-loop | 2026-03-19, dev | alta | |
| B-27 | Tool Loop — streaming mid-loop | 2026-03-19, dev | média | |
| B-28 | Tool Loop — observability | 2026-03-19, dev | alta | |
| B-29 | Tool Loop — human-in-the-loop | 2026-03-19, dev | média | |
| B-30 | Tool Loop — working memory intermediária | 2026-03-19, dev | média | |
| B-31 | Tool Loop — circuit breaker | 2026-03-19, dev | alta | |
| B-32 | Tool Loop — max_iterations configurável | 2026-03-19, dev | baixa | |
| B-33 | Tool Loop — timeout | 2026-03-19, dev | alta | |
| B-34 | Tool Loop — index mode cache | 2026-03-19, dev | média | |
| B-35 | Tool Loop — teste multi-modelo | 2026-03-19, dev | média | |
| B-36 | Tool Loop — custo tracking | 2026-03-19, dev | alta | |
| B-37 | Tool Loop — concorrência | 2026-03-19, dev | média | |
| B-38 | Tool Loop — retry inteligente | 2026-03-19, dev | baixa | |
| B-39 | Tool Loop — context compaction (Ralph-inspired) | 2026-03-19, dev | alta | |
| B-40 | Tool Mode — refatorar tool_loop:bool para tool_mode:Literal["instant","brief","continuous"] | 2026-03-19, dev | alta | |
| B-41 | Kimi K2 context limit — investigar limite de request via Groq e adaptar context_budget/tool index | 2026-03-20, harness | alta | |
| B-42 | Tool descriptions genéricas — melhorar summaries no OpenAPI do YouNews (ex: "Item Action" → "Change item status") | 2026-03-20, harness | média | cross-repo:younews |
| B-43 | Discovery params incompletos — várias tools com "sem params" mesmo tendo request body; revisar extração | 2026-03-20, harness | média | |
| B-44 | Validar supressão de narração intermediária no YouNews staging após deploy v0.17.31+ | 2026-03-20, harness | média | cross-repo:younews |
| B-45 | Test harness pytest — validar tests/e2e/test_kimi_tool_loop.py (requer YouNews + SymGateway rodando) | 2026-03-20, harness | baixa | |

### Detalhamento dos itens do Tool Loop

**B-39 — Context compaction mid-loop (Ralph-inspired).** Insight extraído da implementação Ralph do Gemini CLI (Franziska Hinkelmann) e do Reddit breakdown: o Ralph original resolve context rot limpando a memória conversacional a cada iteração e usando os arquivos como source of truth. No nosso caso, wipe total não funciona — o LLM precisa saber quais tools já chamou para não repetir. Mas acumular messages raw é o problema que vimos: 10 iterações × 2 mensagens = 20 mensagens extras com JSONs de tool results que enchem o contexto. A solução é **compactação estruturada**: a cada N iterações (ou quando tokens ultrapassam um threshold), substituir as mensagens intermediárias por um bloco resumo tipo "Steps completed so far: 1) items_list → found 3 items, item 42 matches 'incêndio', 2) items_publish(42) → published successfully. Continue from here." Isso mantém o LLM informado do progresso sem context rot. Diferente do B-26 (que é sobre trim genérico), este item é especificamente sobre o padrão de compactação inspirado no fresh-context do Ralph, adaptado para tool execution onde estado vive nas respostas das tools e não em arquivos. Referências: Hinkelmann (Medium, Mar 2026), Reddit r/ClaudeAI Ralph breakdown, Simon Wang (ITNEXT, Jan 2026 — sobre riscos de context acumulado em loops autônomos).

**B-25 — LLM não sabe parar.** No teste real com Llama-3.3-70b, o agente publicou a matéria no step 2 mas continuou chamando `items_get` e `items_update` por mais 8 iterações até bater no max_iterations=10. O problema é que o LLM não reconhece que a tarefa foi concluída. Três abordagens complementares: (1) adicionar instrução explícita no prompt: "When the user's request is fully accomplished, respond with a confirmation and do NOT call more tools" — inspirado no Ralph original que usa "completion promise" como stop condition explícita (ref: Reddit breakdown, Hinkelmann/Gemini CLI que usa `--completion-promise TESTS_PASSED`); (2) detectar no ChatRunner quando o LLM chama a mesma tool com os mesmos params consecutivamente e forçar o break; (3) injetar na mensagem de tool result um lembrete: "Original task: {user_input}. Is this task now complete? If yes, respond to the user." A abordagem (1) é preventiva no prompt, (2) é safety net programático, (3) mantém o goal do usuário visível mesmo após muitas iterações (evita goal drift que Hinkelmann descreve como context rot).

**B-26 — Context growth mid-loop.** Cada iteração do loop adiciona 2 mensagens (assistant com tool_call + user com tool_result) à lista de messages passada ao LLM. Em 10 iterações com resultados JSON grandes (ex: lista de 50 itens), o contexto pode crescer de 3k tokens para 30k+ tokens, potencialmente estourando o limite do modelo. Hoje o ContextAssembler faz trim antes do loop começar, mas durante o loop não há nenhum controle. Precisa de um mecanismo de compactação mid-loop — por exemplo, sumarizar tool results antigos quando o total de tokens ultrapassa um threshold, ou truncar resultados JSON grandes antes de injetar na mensagem.

**B-27 — Streaming mid-loop.** O `on_token` callback só emite tokens na primeira chamada ao LLM. Nas iterações 2+ do loop, o texto intermediário do LLM é consumido internamente e descartado — o usuário vê silêncio total até o loop terminar. Para uma UX aceitável, o host precisa receber tokens em todas as iterações, com um separador que indique "estou executando a tool X..." entre os passos. Isso é especialmente crítico no endpoint SSE da API HTTP, onde o cliente pode achar que a conexão travou.

**B-28 — Observability.** Hoje o RunResult retorna os tool_results acumulados mas não tem trace do raciocínio intermediário — não sabemos quantas iterações rodaram, quanto tempo cada uma levou, quantos tokens foram consumidos por iteração, nem qual foi o texto intermediário do LLM em cada passo. Simon Wang (ITNEXT) argumenta que pedir ao LLM para explicar suas decisões gera "post-hoc rationalization, not reasoning trace" — concordo. Mas para tool execution, o trace das ações (qual tool, quais params, qual resultado) É factual e verificável. Não é interpretação — é fato. Precisamos de um LoopTrace que registre cada iteração: {iteration, tool_calls, tool_results, tokens_in, tokens_out, elapsed_ms}. Esse trace pode ser retornado no RunResult e/ou persistido via audit trail. Diferente do Ralph para código (onde o raciocínio é opaco), nosso loop de tools é transparente por natureza — a sequência de API calls É o raciocínio.

**B-29 — Human-in-the-loop.** Uma vez que o loop começa, ele roda autonomamente até o fim. Não tem como o host ou o usuário intervir mid-loop — aprovar uma ação destrutiva antes dela executar, corrigir um parâmetro errado, ou simplesmente cancelar. Simon Wang (ITNEXT) propõe três perguntas antes de ativar autonomia: (1) o resultado tem vida curta? (2) testes pegam erros arquiteturais? (3) alguém revisa antes de importar? Para nosso caso, traduz para: a tool tem side-effects irreversíveis? Em vez de on/off binário, o host poderia classificar tools por nível de risco — tools de leitura (items_list) rodam sem aprovação, tools de escrita (items_publish, newsletter_send) pausam o loop e pedem confirmação. Implementação: callback `on_before_tool_call(tool_id, params) -> bool` no ChatRunner, e/ou metadata de `risk_level` no ToolDescriptor que o loop consulta automaticamente.

**B-30 — Working memory intermediária.** Hoje o ChatRunner salva apenas a resposta final na WorkingMemory. Os passos intermediários (quais tools foram chamadas, em que ordem, com que resultados) são perdidos quando a sessão avança. Isso significa que numa conversa multi-turn, o symbiote não tem memória de como executou a tarefa anterior — se o usuário perguntar "qual matéria você publicou?", o LLM não tem essa informação no histórico. A decisão é: salvar todos os passos (verbose mas completo) ou salvar um resumo estruturado dos passos (compacto mas requer formatação).

**B-31 — Circuit breaker.** Se uma tool falha (ex: API fora do ar), o LLM recebe o erro com um hint genérico e tenta de novo — potencialmente a mesma tool com os mesmos params. O loop repete isso até max_iterations, gerando 10 chamadas idênticas que falham. Precisa de um circuit breaker: se a mesma tool_id falha N vezes consecutivas (ex: 3), injetar uma mensagem explícita "esta tool está indisponível, não tente novamente" e/ou forçar o break do loop. Complementar ao B-25 (detecção de repetição) mas focado especificamente em falhas.

**B-32 — max_iterations configurável.** Hoje `_MAX_TOOL_ITERATIONS = 10` é uma constante fixa no código. Para tarefas simples (publicar uma matéria = 2-3 passos), 10 é desperdício quando o LLM não para. Para tarefas complexas (migrar dados entre jornais = 15+ passos), 10 pode não ser suficiente. O host deveria poder configurar isso por symbiote via EnvironmentConfig, com um teto global como safety net. Implementação simples: adicionar `max_tool_iterations: int = 10` no EnvironmentConfig seguindo o mesmo padrão de tool_loop.

**B-33 — Timeout.** Se um handler de tool faz uma HTTP call para uma API externa que demora 30 segundos, o loop inteiro trava esperando. Multiplicado por 10 iterações, são 5 minutos de bloqueio. Não tem timeout por tool call (o gateway executa sem limite) nem timeout total do loop (o ChatRunner.run() bloqueia até completar). Precisa de dois mecanismos: (1) timeout por tool call no ToolGateway.execute, com ToolCallResult de erro se exceder; (2) timeout total do loop no ChatRunner, abortando com o melhor resultado disponível até aquele ponto.

**B-34 — Index mode cache.** No modo index, o LLM precisa chamar `get_tool_schema` antes de cada tool real para saber os parâmetros. Com o loop ativo, isso significa que uma tarefa de 3 passos gasta 6 iterações (3× schema + 3× tool real). Mas se o LLM já buscou o schema de `items_publish` na iteração 2, não precisa buscar de novo na iteração 4. Uma cache de schemas já fetchados na sessão do loop reduziria iterações pela metade. Implementação: interceptar calls a `get_tool_schema`, armazenar o resultado num dict local ao loop, e retornar da cache sem contar como iteração.

**B-35 — Teste multi-modelo.** Todos os testes reais foram feitos com Llama-3.3-70b via Groq. Modelos diferentes têm comportamentos muito diferentes no loop: Claude tende a ser mais disciplinado sobre parar quando a tarefa completa; GPT-4o segue instruções de formato melhor; modelos menores (8B) provavelmente ignoram as instruções de stop e inventam parâmetros. Precisamos de uma matriz de testes: {modelo × modo × query} com métricas de {iterações até completar, iterações desperdiçadas, taxa de tool calls com params corretos, custo total}. Isso informa quais modelos são viáveis para agentes e qual é o threshold mínimo de qualidade.

**B-36 — Custo tracking.** No teste real, o loop rodou 10 iterações × prompt de ~3.5k chars cada = ~35k chars de input apenas. Somando os tool results que crescem a cada iteração, o custo real foi provavelmente 50k+ tokens para uma tarefa que precisava de 3 chamadas. Em produção com múltiplos usuários, isso é custo descontrolado. Precisamos medir: tokens de input e output por iteração, custo acumulado por request, e expor isso no RunResult e/ou via métricas (Prometheus/logs). O host precisa poder setar um budget máximo por request e o loop abortar se ultrapassar.

**B-37 — Concorrência.** Se dois requests HTTP chegam simultaneamente para o mesmo symbiote, ambos lêem o mesmo EnvironmentConfig, ambos instanciam um ChatRunner, ambos executam tools no mesmo ToolGateway. Os handlers de tool são funções Python que podem ou não ser thread-safe. O ToolGateway usa dicts compartilhados para descriptors e handlers. O WorkingMemory é por-sessão, então provavelmente ok, mas o gateway é compartilhado. Precisa de validação: stress test com requests concorrentes, verificar se handlers HTTP (urllib/httpx) são safe, e avaliar se o gateway precisa de locking.

**B-38 — Retry inteligente.** Quando uma tool falha hoje, o resultado inclui um hint genérico: "Analyze the error above and try a different approach." Isso é pouco informativo — o LLM não sabe se o erro foi um parâmetro errado, uma permissão negada, ou a API fora do ar. Um retry inteligente injetaria contexto específico: se o erro é "parameter X required", incluir o schema da tool para o LLM ver os params corretos; se é "not found", sugerir buscar o ID antes; se é "permission denied", avisar que a tool não está autorizada e não tentar de novo. Isso reduziria iterações desperdiçadas e melhoraria a taxa de sucesso do loop.

**B-40 — Tool Mode: instant, brief, continuous.** Refatorar `tool_loop: bool` para `tool_mode: Literal["instant", "brief", "continuous"]`. Hoje temos dois estados (True/False) que mapeiam para `instant` (single-shot, sem loop) e `brief` (loop curto de 2-10 iterações para tool execution). O terceiro modo, `continuous`, é para o futuro: loops autônomos de longa duração para coding agents, data processing ou content generation — rodando por minutos/horas com fresh context per iteration, Plan.md como source of truth, completion promise, full observability e human checkpoints. A mudança na config layer é pequena (bool → Literal, SQLite TEXT, migration idempotente), mas abre a porta para o ContinuousRunner como runner separado. O Brief Mode (ChatRunner atual) e o Continuous Mode (futuro) têm mecânicas fundamentalmente diferentes: brief acumula contexto e compacta; continuous faz wipe e re-lê estado de arquivos. Detalhes completos no `/kb/ralph-loop-analysis.md`, seção "Nota para futuros Coding Agents".

## Implementadas

| # | Descrição | Implementada em | Versão |
|---|-----------|-----------------|--------|
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
