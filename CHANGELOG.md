# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/)

## [Unreleased]

Pedidos de hosts atendidos na engine (B-36, B-80 a B-93; vieram da equipe do SymbiOS e entraram no que serve a qualquer host), e a engine deixa de depender do SymGateway para buscar na web.

**Mudança de comportamento para hosts:** toda opção nova nasce com o comportamento anterior. O banco ganha as migrações 13 a 15 (PostgreSQL 7 a 9), aplicadas ao subir. O que um host pode notar:

- a nota dada com `report_feedback` antes do `close_session` passa a valer no `final_score` (antes o encerramento a sobrescrevia);
- `ToolCallResult` e os dicionários de `tool_results` de `message()` têm a chave nova `pending_approval_id` (`None`, salvo em chamada adiada);
- `prune()` devolve mais uma chave, `llm_usage`; as linhas de `inspector.list_turn_contexts` trazem `llm_calls` e os tokens do turno;
- `register(search_routing=...)` sem provedor para `web_search` agora dá `ValueError` (antes caía no Brave pelo SymGateway); nome de backend desconhecido dá `ValueError` em vez de `NotImplementedError`;
- `ForgeLLMAdapter` só envia `project_slug` quando o provider é `symgateway`;
- `kernel._llm` e `kernel._evolver_llm` (atributos privados) passam a ser um medidor em volta do adapter recebido; `.wrapped` devolve o adapter;
- **tokens de entrada com a Anthropic:** em todo registro de uso (`LLMResponse.usage` do `ForgeLLMAdapter`, `llm_usage`, `TurnOutcome.usage`), `prompt_tokens` passa a ser a entrada inteira, com a parte lida e a parte escrita no cache. Antes, pelo `ForgeLLMAdapter` com a Anthropic, essa parte ficava de fora. Para quem usa prompt cache o número de entrada sobe, e `total_tokens` junto. Quem calcula custo a partir de `prompt_tokens` precisa descontar o cache: a entrada cobrada pelo preço comum é `prompt_tokens - cache_read_tokens - cache_write_tokens`. Com a OpenAI nada muda, porque ela já contava o cache dentro da entrada;
- o `ForgeLLMAdapter` passa a pedir o uso em toda chamada com streaming (`include_usage`): com provedores no formato da OpenAI a requisição leva `stream_options.include_usage`.

### Novas funcionalidades

- feat(approval): aprovação adiada (B-90). O callback de aprovação pode responder `ApprovalDecision.defer(motivo)` além de `True` / `False`: a chamada não roda, o pedido fica em `pending_approvals`, o modelo é avisado de que está aguardando e o turno termina normalmente. `kernel.resolve_pending(id, approved, note=, resolved_by=, host_context=)` decide depois; a chamada aprovada roda com a sessão, o symbiote, o usuário final e os parâmetros originais, passa de novo pela política e pelo tool guard (`current_tool_context().approval_id` identifica o pedido) e a linha de auditoria da execução leva o id. Um pedido é resolvido uma única vez, expira (`KernelConfig.pending_approval_ttl_hours`, padrão 168, ou `defer(expires_in=)`) e sobrevive a reinício. O turno seguinte da conversa, ou do mesmo usuário final com o mesmo symbiote, recebe o resultado no contexto, uma vez. `TurnOutcome.pending_approvals`, `inspector.list_pending` / `get_pending`, `GET /symbiotes/{id}/approvals`, `POST /approvals/{id}/resolve` e o SDK (`list_approvals`, `resolve_approval`). `ApprovalDecision.deny(motivo)` entrega o motivo ao modelo.
- feat(usage): uso por chamada ao modelo (B-36). `current_llm_call()` (`symbiote.core.llm_usage`) diz ao adapter o symbiote, a sessão e a finalidade da chamada: `turn`, `subagent`, `reflection`, `session_summary`, `skill_review`, `dream:<fase>`, `harness`, `memory_consolidation`, `tool_resolution`, `long_run:plan` / `evaluate` / `report`. Cada chamada vira uma linha em `llm_usage` com provedor, modelo e tokens, sem texto da conversa e sem preço. Os tokens vêm do `usage` do `LLMResponse` ou de `report_usage()`, para o adapter que devolve texto puro ou faz streaming. `TurnOutcome.usage` e `turn_contexts` trazem o total do turno, incluindo o que ele delegou; `inspector.usage_totals` / `usage_by` / `list_llm_calls` leem por symbiote, sessão, usuário final, finalidade, modelo e dia; `kernel.set_usage_callback(fn)` entrega cada registro ao host; `prune(usage_days=)` e `KernelConfig.usage_retention_days` limitam a tabela. Funciona com chave direta de provedor: nada depende de gateway. Cada registro traz também os tokens de cache: `cache_read_tokens`, `cache_write_tokens`, `cache_write_5m_tokens`, `cache_write_1h_tokens` e, como informação, `reasoning_tokens`. A convenção é uma só para todo provedor: `prompt_tokens` é a entrada inteira, com o cache, e os campos de cache são partes dela; `usage_from_provider(raw)` converte o que o provedor devolveu (a Anthropic informa a entrada sem o cache, a OpenAI com). `report_usage()` aceita os mesmos campos e as somas do inspector os incluem.
- feat(browser): as tools de web deixam de ter mecanismo fixo (B-91). `search_backend`, `extract_backend` e `crawl_backend` aceitam um provedor do host ou o nome de um registrado (`register_search_provider`, `register_extract_provider`, `register_crawl_provider`). Entram `brave_api` (API do Brave com chave própria) e `searxng` (instância SearXNG), que falam direto com o serviço; `brave`, `symgateway_objective` e `firecrawl` continuam indo pelo SymGateway. `SearchOptions.provider_options` leva as opções de cada provedor.
- feat(skills): `EnvironmentConfig.skill_trial_listing` (B-81). Ligado, as skills em teste aparecem ao modelo marcadas `status="trial"`, `skill_view` as lê e cada leitura conta para `skill_auto_promote_threshold`.
- feat(spawn): `current_spawn()` em `symbiote.environment.exec_context` (B-84) diz a ganchos, adapter, tool guard e tools que o código roda para um symbiote delegado: o delegado e a subsessão, quem delegou, a raiz da cadeia e a profundidade.
- feat(dream): `kernel.set_dream_callback(fn)` e `kernel.wait_dream(symbiote_id, timeout)` (B-85); `EnvironmentConfig.dream_llm` (`aux` padrão, `main`) escolhe o modelo do dream (B-86).
- feat(session): `EnvironmentConfig.session_summary_mode` (B-87): `concat` (padrão, sem chamar modelo), `llm` (resumo escrito pelo modelo auxiliar, uma vez por encerramento) ou `off`.
- feat(discovery): `kernel.discover_tools(symbiote_id, spec={...}, base_url=...)` grava as tools de uma descrição OpenAPI que o host já tem em mãos (B-88).
- feat(knowledge): `register_source(..., auto_recall=False)` e `knowledge.set_auto_recall` deixam um item fora do conhecimento empacotado no prompt e alcançável por `search_knowledge` (B-89).
- feat(memory): `MemoryStore.reactivate(memory_id, ...)` desfaz o `deactivate` (B-83).
- feat(ui): `symbiote.ui.widget_path()` e `widget_version()` entregam o `<symbiote-chat>` de dentro do pacote instalado; `reload()` público no componente (B-82).
- feat(symgateway): `SymGatewayLLMAdapter` aceita `objective=` no lugar de `model=`, lista os Objectives da chave (`list_objectives`) e informa qual roteou cada chamada (`on_routed`, `last_routing`, `LLMResponse.metadata`). Fica só no adapter (B-93).

### Correções

- fix(score): `close_session` recalculava o `final_score` só com a nota automática e apagava a nota do host dada antes do encerramento; a sessão pontuada antes de encerrar também nunca contava para o score da versão do harness (B-80).
- fix(ui): no modo inline, o `<symbiote-chat>` marcava o histórico como carregado ainda com o `NoopAdapter`, e um adapter definido depois nunca o carregava (B-82).
- fix(llm): `ForgeLLMAdapter` enviava `project_slug="symbiote"` a qualquer provedor; agora só ao `symgateway`, e o host pode trocar ou tirar com `project_slug=` (B-92).
- fix(usage): os adapters `ForgeLLMAdapter` e `SymGatewayLLMAdapter` perdiam os tokens das respostas em texto puro; agora informam ao kernel.
- fix(llm): `forge-llm` atualizado para a v0.7.18 (`b26b5e3`). A v0.7.17 passou a repassar o uso do provedor como veio; a v0.7.18 só acrescenta as notas de atualização dela. Com isso o `ForgeLLMAdapter` preenche os tokens de cache da Anthropic e da OpenAI, com e sem streaming (ele passa a pedir o uso do stream), e a entrada da Anthropic deixa de vir sem a parte do cache (B-94).
- fix(symgateway): o `SymGatewayLLMAdapter` lia `usage.prompt_tokens` / `completion_tokens`, mas o gateway responde `input_tokens` / `output_tokens`; os tokens de entrada e de saída vinham vazios. Agora lê os campos do gateway, inclusive os de cache.

### Outros

- test: o kernel, a API HTTP, as tools de web e o SDK importam sem carregar nenhum módulo do SymGateway, e um turno, um encerramento, um dream e uma busca na web rodam com o gateway fora de alcance (`tests/unit/test_no_gateway_dependency.py`).
- docs: `docs/HOST_INTEGRATION.md` (aprovação adiada, uso por chamada, delegação, resumo, skills em teste, dream, conhecimento por item, SymGateway opcional), `docs/arch-02-execution.md`, `docs/arch-03-memory.md`, `docs/arch-04-dream-mode.md`, `docs/integrations/symbiote-browser-quickstart.md` e `symbiote-ui/README.md`.

## [v0.8.9] - 2026-11-16

Memória por usuário final: um symbiote que conversa com várias pessoas passa a guardar uma partição de memória por pessoa (B-79, pedido pelo estúdio de symbiotas).

**Mudança de comportamento para hosts:** nenhuma para quem não usa `subject_key`. As sessões e memórias existentes ficam na partição compartilhada, que é o comportamento anterior. O banco ganha a migração 12 no SQLite e a 6 no PostgreSQL, aplicadas na inicialização.

### Novas funcionalidades

- feat(memory): `kernel.start_session(..., subject_key="...")` e `get_or_create_session(..., subject_key="...")` dizem de qual usuário final é a sessão. Uma sessão com `subject_key` lê as memórias compartilhadas do symbiote mais as daquele usuário e grava só na partição dele; uma sessão sem a chave lê e grava as compartilhadas. A chave é um texto opaco de até 500 caracteres, definida ao abrir a sessão e imutável: pedir a mesma `external_key` com outra `subject_key` levanta `ValidationError`.
- feat(memory): as duas regras ficam no `MemoryStore`. `store()` tira a partição da sessão da entrada, então reflexão, memória de falha, handoff, consolidação e `learn` gravam no lugar certo sem passar a chave. `search`, `get_relevant`, `get_standing`, `get_by_type` e `get_by_category` aceitam `subject_key`; o filtro entra no SQL antes do limite de candidatas do recall. `update` e `deactivate` aceitam `subject_key` e tratam como inexistente a memória de outra partição.
- feat(memory): contexto do turno, `search_memories`, `teach`, `show`, o handoff de long-run e a reflexão usam a partição da sessão. A reflexão por LLM só oferece como alvo de PATCH as memórias da própria partição; um PATCH vindo da sessão de um usuário final não alcança uma memória compartilhada.
- feat(spawn): a sessão aberta por `spawn` herda a `subject_key` da sessão que delegou; o symbiote delegado lê e grava na partição da mesma pessoa.
- feat(config): `EnvironmentConfig.learn_from_subject_sessions` (padrão `False`). Desligado, as sessões de usuários finais têm score e memórias próprias, mas ficam fora do `ParameterTuner`, do evolver do harness e dos scores por versão, das fases `mine` e `evaluate` do dream e do review de skills. Ligado, contam como as demais.
- feat(dream): `reconcile` e `generalize` trabalham só na partição compartilhada, então nada é unido entre usuários finais; `prune` julga cada memória sozinha e cobre todas as partições.
- feat(inspector): `kernel.inspector.list_subjects(symbiote_id)` lista os usuários finais com contagem de sessões e de memórias; `list_memories` e `list_sessions` aceitam `subject_key` e devolvem a coluna.
- feat(kernel): `kernel.purge_subject(symbiote_id, subject_key, dry_run=False)` apaga, numa transação, as sessões, mensagens, traces, scores, linhas de auditoria e memórias de um usuário final naquele symbiote, sem tocar nos outros.
- feat(api): `POST /sessions` aceita `subject_key` e `SessionResponse` a devolve; `GET /memory/search` aceita `subject_key`; `PUT /symbiotes/{id}/config` aceita `learn_from_subject_sessions`. No SDK, `SymbioteClient.create_session(..., subject_key=...)` e `search_memory(..., subject_key=...)`. Na CLI, `symbiote memory search --subject`.

### Outros

- `ExportService.export_memory(symbiote_id, subject_key=None)` exporta as memórias compartilhadas ou, com a chave, as de um usuário final; as duas nunca se misturam num export. O `SemanticRecallProvider`, que não recebe a sessão, lê só as compartilhadas.
- docs: seção "End users: one memory partition per person" em `docs/HOST_INTEGRATION.md` e "Memoria por usuario final" em `docs/arch-03-memory.md`.

## [v0.8.8] - 2026-11-16

Os dois pontos que o protótipo do SymGateway deixou em aberto na v0.8.7: saber pelo próprio turno por que o loop de tools parou, e o tipo do que `message()` devolve.

### Novas funcionalidades

- feat(kernel): `kernel.turn()` e `kernel.turn_async()` recebem os mesmos argumentos de `message()` e devolvem um `TurnOutcome`: a resposta, os resultados das tools (`ToolCallResult`), o `stop_reason` (`end_turn`, `max_iterations`, `stagnation`, `circuit_breaker`, `timeout`, `on_after_tool_result` ou `block_failure`, listados em `symbiote.core.turn_result.STOP_REASONS`), `stopped_early`, iterações, chamadas de tool, tempo e modo. O host deixa de precisar ler `execution_traces` para isso.

### Correções

- fix(kernel): `message()` e `message_async()` passam a declarar `str | TurnReply`, o dicionário (`text`, `tool_results`) que já devolviam quando o turno chamava tools; antes estavam anotados como `str`. O comportamento não muda.
- fix(demo): o chat segue só o próprio turno (antes adotava o primeiro turno do Symbiota e podia misturar o texto e as ferramentas de outra aba); pedidos de aprovação contados por tool; o evento de reset deixa de ser tratado duas vezes; o README descreve o streaming ao vivo; `run_turn` usa `turn_async()`.

### Outros

- docs(approval): `risk_threshold` documentado na seção "Human-in-the-Loop" do guia, com os níveis, o alcance (kernel inteiro: chat, `instant` e blocos de `long_run`) e como variar por usuário com `pass_context=True`.

## [v0.8.7] - 2026-11-16

O agente pode trabalhar no Chrome que a pessoa já usa, com os logins dela, sem configuração de arquivo; o host escolhe a partir de que risco uma tool pede aprovação.

**Mudança de comportamento para hosts:** nenhuma por padrão. `set_approval_callback` continua perguntando só no risco alto; uma tool com `requires_approval=True` sem callback instalado é recusada em vez de rodar.

### Novas funcionalidades

- feat(browser): `register_user_browser(kernel, allowlist=[...], session="open")` registra tools que se conectam ao Chrome do dia a dia da pessoa (ela liga a depuração remota em `chrome://inspect/#remote-debugging` e permite a conexão no diálogo do Chrome). O agente trabalha numa aba própria, só nos sites da lista (inclusive em links clicados) e nunca digita senha ou código; clicar e preencher sempre pedem a aprovação de uma pessoa. `session="own"` usa um Chrome separado, com perfil próprio, que o kernel abre quando precisa. `register_browser(prefix=...)` permite mais de uma família de navegador no mesmo kernel.
- feat(approval): `ToolDescriptor.requires_approval` faz a tool sempre passar pelo callback de aprovação, qualquer que seja o risco, e ser recusada se não houver callback. `set_approval_callback(callback, risk_threshold="low"|"medium"|"high")` escolhe a partir de que risco perguntar, também dentro dos blocos do long-run.
- feat(kernel): `Kernel.on_shutdown(callback)` para extensões liberarem o que abriram (o navegador e suas abas) antes de o loop de tools parar.
- feat(demo): o Sol ganha as tools `meu_chrome_*` no Chrome aberto da pessoa, configuráveis na engrenagem ("Chrome do Sol": sessão e sites liberados), e explica como ligar a conexão quando ela ainda não existe. Nível de aprovação (perguntar no risco baixo, médio ou alto, nunca, ou permitir tudo, inclusive no Chrome) e máximo de turnos viram seletores junto da caixa de mensagem; as abas sobem para o topo e o "Ao vivo" vira um painel recolhível à direita do chat; destaque em azul claro para contraste, também no `<symbiote-chat>`.

### Correções

- fix(demo): a chave de aprovação da interface respondia 422 (a rota `/api/approvals/toggle` era capturada por `/api/approvals/{request_id}`); a página se recarrega sozinha depois de um reinício do servidor em vez de falhar com 403; o desligamento espera no máximo 3 segundos pelo stream de eventos.
- fix(demo): os passos do roteiro que dependem da API pessoal (passo 4) avisam quando ela não foi carregada; zerar o demo zera também o progresso do roteiro; a persona do Sol distingue as tools da API pessoal e quando usar `fs_read` em vez de `bash`.

## [v0.8.6] - 2026-11-16

O roteiro do demo, rodado de ponta a ponta contra um modelo real, mostrou seis comportamentos fora do esperado; quatro eram do kernel e estão corrigidos aqui. Entram também a busca web por um objective do SymGateway e links com miniatura no `<symbiote-chat>`.

**Mudanças de comportamento para hosts:** a resposta de um turno `long_run` deixa de ser o quadro `"Project completed: N/M blocks"` (que continua em `handoff_data["output_summary"]`) e passa a ser escrita pelo modelo, com uma chamada a mais por projeto; preferências e restrições entram em todo turno (até 5, `standing_memory_limit=0` desliga); uma chamada que falha pode ser repetida até a terceira falha.

### Novas funcionalidades

- feat(memory): memórias do tipo `preference` e `constraint` entram no contexto de todo turno, as mais importantes primeiro, até `KernelConfig.standing_memory_limit` (padrão 5; `0` volta ao recall só por relevância), independentemente das palavras da mensagem. Antes, "prefere respostas em tópicos curtos" só chegava ao modelo quando a pergunta falava de formato, e o modelo chegou a negar uma preferência que existia. Entrar num turno conta como uso para o dream. Um `MemoryPort` do host sem `get_standing()` mantém o comportamento anterior.
- feat(browser): `search_backend="symgateway_objective"` faz o `web_search` pedir ao SymGateway um propósito (`POST /v1/objectives/invoke` com objective e service, ex.: `web_seek` / `websearch`) em vez de um provedor. Aceita como saída o JSON do Brave, `{"results": [...]}` ou uma lista; uma recusa traz o motivo dado pelo gateway (ex.: a permissão que falta).
- feat(ui): o `<symbiote-chat>` transforma endereços da resposta em links (domínios sem `https://` também; nunca dentro de código; links `javascript:` são descartados), abertos fora do chat. Um host que fornece `linkPreview(url)` no adapter ganha cartões com miniatura, título e site para os seis primeiros links de cada resposta; sem ele nada muda.
- feat(demo): engrenagem com as configurações do LLM (servidor, modelos e chave, editáveis e com botão Testar que não salva nada); `web_search` pelo objective `web_seek` do gateway; links clicáveis com miniaturas buscadas pelo servidor do demo (só endereços públicos em cada salto, imagens raster de até 3 MB).

### Correções

- fix(long-run): o planejador passa a ser informado do `max_blocks`, e blocos planejados além dele são cortados antes do `on_plan` e guardados no handoff como `dropped_blocks`, nunca como pendentes. Plano, progresso, trace e handoff dão a mesma contagem (a resposta dizia "3/4 blocks" num projeto que rodou tudo o que podia). A resposta ao usuário é escrita por uma chamada a mais ao modelo a partir dos relatórios dos blocos, no idioma do pedido e na voz da persona; se essa chamada falha, vale o relatório do último bloco bem-sucedido.
- fix(long-run): o handoff (categoria `handoff`) não entra mais no recall de memórias de um turno e só é injetado na primeira mensagem de uma sessão quando o symbiote está em `long_run` com trabalho pendente. Um turno de chat recebia o registro JSON inteiro no prompt de sistema (cerca de 5 mil caracteres).
- fix(loop): duas chamadas idênticas seguidas só contam como estagnação quando a primeira deu certo. Repetir uma chamada que falhou (pedido do usuário ou erro passageiro) agora vai até o circuit breaker na terceira falha seguida, e a memória de falha nomeia a tool em vez de sugerir "verificar se a task já foi completada".
- fix(dream, skills): as fases generalize, mine e evaluate do dream escrevem no idioma da persona (ou do material mostrado), e o review de skills escreve a descrição e o corpo da skill no idioma da conversa, que é contra o que a descrição é comparada. Antes saíam em inglês numa conversa em português.
- fix(demo): o roteiro mostra o que promete. O Sol sabe que o que aprende é gravado ao encerrar a sessão; o passo 7 tenta o navegador antes de delegar e esbarra na política; as sementes de poda do dream não têm palavras em comum com os prompts do roteiro (com teste); a calculadora nomeia o parâmetro que espera.

## [v0.8.5] - 2026-11-16

### Outros

- test(postgres): a migração 5 (`tool_visibility`) é testada sobre um banco que já estava na versão 4 com uma config gravada: a config antiga passa a ler `"tagged"` e aceita `"authorized"`. O teste da coluna no SQLite ganhou a marca `sqlite_only`, porque usa `PRAGMA`.

## [v0.8.4] - 2026-11-14

Os blocos do long-run passam a rodar no protocolo do kernel e com o review de skills, e o host pode listar no prompt só as tools que o symbiote pode executar.

### Novas funcionalidades

- feat(config): `EnvironmentConfig.tool_visibility` decide quais tools o prompt lista (B-76). `"tagged"` (padrão, comportamento anterior) lista toda tool cujas tags batem com `tool_tags`, autorizada ou não. `"authorized"` lista só as que a política deixa o symbiote executar; `tool_tags` continua restringindo as tools com tag, e uma tool autorizada sem tag (`fs_*`, `search_*`, `spawn`) continua listada mesmo com `tool_tags` definido. O `get_tool_schema` segue a mesma regra. Disponível em `environment.configure(...)` e no `PUT /symbiotes/{id}/config`; nova coluna `tool_visibility` em `environment_configs` (migração 11 no SQLite, 5 no PostgreSQL).

### Correções

- fix(long-run): os blocos de `tool_mode="long_run"` rodavam sempre no protocolo legado e sem o review de skills em background (B-74). O `LongRunRunner` passa a receber do kernel o `agent_protocol` (com tools nativas obrigatórias no `poolside_laguna_v1`) e o resolvedor do `background_review`, e os repassa ao runner de cada bloco; `kernel.set_agent_protocol()` passa a valer também para o long-run. A contagem de iterações até o próximo review continua de um bloco para o outro, então um intervalo maior que um bloco ainda dispara.

## [v0.8.3] - 2026-11-14

Streaming token a token e endpoint SSE de chat, conferência de contagens no long-run e o retorno do SymGateway sobre a 0.8.2: histórico estruturado, resposta final sem tools aceita por qualquer provedor e limites de resultado visíveis ao host.

**Mudança de padrão:** `tool_result_max_chars` passa de 2.000 para 3.000 caracteres.

### Novas funcionalidades

- feat(kernel): streaming token a token (B-73). `kernel.set_token_streaming("live")` faz o `on_token` receber o texto enquanto o modelo escreve, em todas as rodadas do loop de tools, e não mais só a resposta final validada de uma vez. A narração antes de uma tool call aparece; a marcação da chamada (bloco `tool_call`, JSON `{"tool": ...}` solto) é retida e descartada. As rodadas são separadas por uma linha em branco, e o valor devolvido por `message_async` continua sendo a resposta oficial (quando a validação troca uma resposta, a nova vem como parágrafo novo). O padrão continua `"final"`, sem mudança para hosts existentes. `ForgeLLMAdapter(..., stream_tool_calls=True)` transmite também com tools nativas, montando as tool calls a partir do stream (desligado por padrão: o Groq não monta tool calls em stream e o stream não traz o estado de raciocínio que o `poolside_laguna_v1` reenvia). Ver `docs/HOST_INTEGRATION.md`, "Token Streaming".
- feat(api): `POST /sessions/{id}/chat/stream` responde com Server-Sent Events (B-69): `token` com cada pedaço do texto, `done` com o mesmo corpo do `POST /chat` e `error` com o status que o `POST /chat` daria (409 sessão ocupada ou turno cancelado, 500 sem expor a mensagem interna). Acesso e LLM são checados antes do stream, com erros HTTP normais; se o cliente desconecta no meio, o turno é cancelado. O servidor usa `live` por padrão (`SYMBIOTE_TOKEN_STREAMING=final` para só a resposta validada). O SDK ganhou `SymbioteClient.chat_stream()` e `SymbioteStreamError`.
- feat(demo): o chat do demo mostra o texto enquanto o modelo escreve (modo `live`, `stream_tool_calls` com OpenAI e Anthropic); a resposta final substitui o texto no fim do turno.
- feat(chat): histórico estruturado. `extra_context["conversation_history"]` aceita uma lista de `{"role": "user" | "assistant", "content": str}`, entregue ao modelo como está: nada dentro de um `content` é lido como rótulo de papel, e linhas em branco e indentação são mantidas. Turno com `content` vazio fica de fora; lista malformada levanta `ValidationError` apontando o turno. A forma em texto (`"Rótulo: conteúdo"` por linha) continua aceita.
- feat(config): `KernelConfig.runtime_context` (`"full"`, padrão; `"timestamp"`; `"off"`) controla o bloco que o kernel acrescenta à mensagem do usuário enviada ao modelo. `"timestamp"` tira o id da sessão, `"off"` manda a mensagem como o usuário escreveu. O bloco continua não sendo persistido.

### Melhorias

- fix(long-run): o kernel confere as contagens que o bloco afirma. O avaliador devolve `count_claims` (cada número que o texto do bloco dá sobre as próprias ações, com a tool correspondente) e o runtime compara com as chamadas bem-sucedidas daquela tool: se não bate, o bloco falha com nota no máximo 0.5, qualquer que seja a nota do avaliador, e o retry recebe a ordem de corrigir o relato sem repetir nem desfazer chamadas. Num teste real o avaliador escreveu "afirma 24 tarefas, as chamadas mostram 28" e aprovou com 0.95. Uma contagem para uma tool que não rodou não é conferida (pode ser nome errado do avaliador); `null` (nenhuma chamada fez aquilo) com número maior que zero falha. O pedido vai na mensagem de avaliação, então vale também com `evaluator_prompt` do host. No retry, a evidência avaliada passa a incluir as chamadas da tentativa rejeitada, e o resumo do retry passa a contar o bloco inteiro.
- refactor(console, cli): o Console admin e a CLI (`symbiote audit reflection`, `audit skill-review`) leem pelo `kernel.inspector` em vez de SQL próprio (B-78), com as mesmas respostas. `KernelInspector.list_scores` aceita `session_id` (e `symbiote_id` ficou opcional); `list_reflections` e `list_skill_reviews` aceitam `session_id` e `since`, e o segundo também `trigger`. Os endpoints do Console que devolvem o JSON cru gravado (`/trace`, `/memory`, `/reflections`, `/activity`) continuam em SQL, porque o dashboard lê esse texto.
- feat(config): o padrão de `tool_result_max_chars` sobe de 2.000 para 3.000 caracteres por resultado de tool (em `KernelConfig` e no `ChatRunner` instanciado direto). Hosts que não configuram o limite passam a enviar até 1.000 caracteres a mais por resultado ao modelo; quem quer o comportamento anterior define `tool_result_max_chars=2000`. `loop_context_budget` continua em 16.000 tokens.

### Correções

- docs: `docs/arch-02-execution.md` descrevia uma heurística por palavra-chave para `tool_mode="auto"` ("pesquise" → long_run) que não existe (B-77). O modo é decidido assim: `long_run` se há `planner_prompt`, `instant` se nenhuma tool está autorizada, `brief` nos demais casos; o texto do usuário não entra.
- fix(chat): quando o modelo ainda pede tools depois de mandado parar (estagnação, circuit breaker), o texto dessa resposta não vai mais para o `on_token`; a resposta padrão que substitui o turno vai.
- fix(chat): a resposta final forçada (limite de iterações, estagnação, circuit breaker) é pedida sem a lista de tools, mas levava o histórico com chamadas de tool estruturadas; provedores como Anthropic e Bedrock recusam essa combinação. As trocas de tool vão para essa chamada como texto (`[Called tool: nome]` / `[Tool result: nome]`), sem `tool_calls` nem mensagens `tool`; texto, argumentos, resultados e raciocínio continuam lá.
- fix(chat): a forma em texto de `conversation_history` perdia a indentação e as linhas em branco de dentro de cada mensagem (código colado chegava achatado ao modelo). Continua valendo o limite que só a forma estruturada resolve: qualquer linha que comece com um rótulo reconhecido abre um turno novo, mesmo dentro de um texto colado pelo usuário.
- fix(long-run): os blocos de `tool_mode="long_run"` ignoravam `tool_result_max_chars` do `KernelConfig` e cortavam cada resultado de tool em 2.000 caracteres. Passam a seguir a configuração do kernel (e `runtime_context`).
- fix(chat): resultado de tool cortado deixou de ser silencioso. O host recebe o resultado inteiro em `tool_results`, então nada indicava que o modelo tinha lido menos. O primeiro corte de cada tool gera um WARNING com a tool, os tamanhos e o nome da configuração (`tool_result_max_chars`), nunca o conteúdo; os seguintes saem em DEBUG. O corte do autocompact (`loop_context_budget`) também é registrado.

### Outros

- ci: o estágio `quality` da imagem (o gate do deploy de staging) copia `QUICKSTART.md` e `docs/HOST_INTEGRATION.md`; sem eles o teste que confere os exemplos dos guias quebrava na coleta dentro do container.
- docs(host): `HOST_INTEGRATION.md` descreve a forma estruturada do histórico e o limite da forma em texto, o bloco acrescentado à mensagem do usuário, como dimensionar `tool_result_max_chars` junto com `loop_context_budget` (o padrão, agora 3.000 caracteres, ainda corta respostas de API grandes; `loop_context_budget >= tool_result_max_chars * 0,625` é o mínimo) e a chamada final sem tools.

## [v0.8.2] - 2026-11-13

O kernel passa a rodar em PostgreSQL com várias réplicas do host: SQL portátil e `PostgresAdapter`, transação por operação composta, trava de sessão injetável com validade e renovação, biblioteca de skills compartilhada e cancelamento de turno. Sem configurar nada disso, o comportamento com SQLite num processo só continua o mesmo.

**Mudança incompatível:** quem escreveu um adaptador de armazenamento próprio contra o `StoragePort` da 0.8.1 precisa ajustá-lo (nota **MIGRAÇÃO** abaixo). Bancos SQLite existentes recebem quatro migrações aditivas no `init_schema()`.

### Novas funcionalidades

- feat(storage): `PostgresAdapter` (`symbiote.adapters.storage.postgres`, extra `symbiote[postgres]`, psycopg 3 com pool de conexões) para hosts com várias réplicas no mesmo banco: `SymbioteKernel(config, llm, storage=PostgresAdapter(dsn, schema="symbiote"))`. O esquema é criado por migrações versionadas (`postgres_migrations.py`, tabela `schema_migrations`), aplicadas numa transação sob advisory lock, então réplicas que sobem juntas migram uma de cada vez. As colunas de texto usam a collation `C`: comparação, ordenação e busca por substring dão o mesmo resultado que no SQLite. A suíte inteira roda nos dois bancos (`scripts/test-postgres.sh` sobe um `postgres:15-alpine` descartável; `SYMBIOTE_TEST_POSTGRES_DSN` aponta para um servidor existente). Ver `docs/HOST_INTEGRATION.md`, "PostgreSQL".
- feat(storage): `StoragePort` deixa de ser dialeto SQLite. O kernel manda o mesmo SQL para qualquer adaptador, com as regras escritas no docstring do port (placeholders `?`, só construções padrão, datas calculadas em Python, `rowid` como ordem de inserção, `COALESCE` ao ordenar coluna que pode ser NULL). Entram no contrato: `transaction()` (as instruções do bloco são confirmadas juntas ou desfeitas juntas), `execute()` devolvendo `rowcount`, e `StorageIntegrityError` para violação de restrição em qualquer banco. `symbiote.core.sql` tem os trechos que diferem entre bancos (`contains`/`like_contains`, `differs`, `days_ago`). As tabelas `api_keys` e `http_tools` passam a ser criadas pelo adaptador (migração 7 no SQLite), não mais pelos módulos que as usam.
  - **MIGRAÇÃO:** quem escreveu um adaptador de armazenamento próprio contra o `StoragePort` da 0.8.1 precisa acrescentar `transaction()` (context manager que confirma ou desfaz as instruções do bloco) e fazer `execute()` devolver um objeto com `rowcount`. `SQLiteAdapter` já atende. Bancos SQLite existentes ganham quatro migrações aditivas (tabelas `api_keys`/`http_tools` criadas pelo adaptador, `session_locks`, `skill_files`/`skill_scopes` e a coluna `cancel_requested` de `session_locks`), aplicadas no `init_schema()`.
- feat(storage): transação por operação composta. O kernel envolve em `storage.transaction()` cada operação que faz mais de uma escrita: fim do turno (trace + mensagem do assistente), persona + auditoria, `environment.configure`, criação e rollback de versão de harness, score da sessão + contadores, reflexão (memórias + marca d'água), toques de `last_used_at` do recall, reconciliação do dream, um scan do `discover_tools`, a limpeza de segredo de uma sessão, `prune` e `purge_symbiote`. O turno inteiro não é uma transação (ele espera o modelo e as tools). A lista do que entra e do que fica de fora está em `docs/HOST_INTEGRATION.md`, "Transactions"; `tests/unit/test_composite_transactions.py` interrompe cada operação no meio e confere que nada dela ficou gravado, nos dois bancos.
- feat(kernel): trava de sessão injetável. `SymbioteKernel(..., session_lock=...)` recebe um `SessionLockPort` (`acquire(session_id, wait=, ttl=) -> token | None`, `renew`, `release`; `acquire_async`/`release_async` opcionais). O padrão, `InProcessSessionLock`, mantém o comportamento de sempre (uma trava por sessão dentro do processo, espera sem limite). `StorageSessionLock(storage)` guarda a trava numa linha da tabela nova `session_locks` e vale entre réplicas que dividem o banco. `KernelConfig.session_lock_wait` limita a espera (`SessionBusyError`, nada é gravado), `session_lock_ttl` dá validade à trava e `session_lock_renew_interval` define de quanto em quanto tempo ela é renovada enquanto o turno roda (padrão: um terço da validade). Se a renovação falha, o turno para antes do próximo passo, sem nova chamada ao modelo nem a tool, não grava resposta e `message()` levanta `SessionLockLostError`; o que já estava rodando termina. A liberação é garantida também quando o turno falha. A API responde 409 nos dois erros. Ver `docs/HOST_INTEGRATION.md`, "Session lock".
- feat(kernel): cancelamento de turno. `kernel.cancel_turn(session_id)` pede que o turno em andamento pare: a partir dali nenhuma chamada nova ao modelo nem a tool começa, e a que está rodando termina. `message()` / `message_async()` levantam `TurnCancelledError`; a mensagem do usuário fica na sessão e o trace do que o turno fez é gravado com `stop_reason="cancelled"` (mesmo sem nenhuma tool), o que distingue o turno cancelado do concluído. Não é gravada resposta do assistente nem memória de falha. Vale para `brief`, `continuous`, `instant` e `long_run`, síncrono e assíncrono. Com `StorageSessionLock` o pedido feito em qualquer réplica chega à que roda o turno em até `KernelConfig.turn_cancel_poll_interval` (1 s); a API responde 409. Ver `docs/HOST_INTEGRATION.md`, "Cancelling a turn".
- feat(skills): biblioteca de skills compartilhada entre réplicas. `SymbioteKernel(..., skills_storage=...)` recebe um `SkillsStoragePort` (`version`, `list_files`, `read_files`, `write_files`, `delete_scope`); `StorageSkillsLibrary(storage)` o implementa no banco do kernel (tabelas novas `skill_files` e `skill_scopes`). Com ele a biblioteca passa a ser a fonte e o `skills_root` de cada réplica uma cópia local: toda mudança feita pelo kernel é enviada na hora, e cada turno começa conferindo a versão do seu escopo (uma consulta) e atualizando a cópia se mudou. Uma skill escrita numa réplica aparece no turno seguinte da outra, e um turno já montado não muda. Validação, roots protegidos, quarentena e isolamento por symbiote continuam iguais; `purge_symbiote` apaga o escopo do symbiote e `kernel.sweep_skill_cache()` remove a cópia local nas outras réplicas. Sem `skills_storage` nada muda: a biblioteca é o diretório local. Contadores de uso sobem junto com a próxima mudança (ou em 30 s, ou no shutdown); só arquivos de texto são compartilhados. Ver `docs/HOST_INTEGRATION.md`, "Skills shared between replicas".

### Melhorias

- refactor(chat): o loop de tools síncrono e o assíncrono do `ChatRunner` passam a ser um corpo só (B-72). `_loop_steps` é um gerador sem I/O que pede o que é lento (chamada ao modelo, callback de aprovação, execução de tools); `_drive` atende os pedidos na thread que chamou e `_drive_async` no event loop. Comportamento igual; `tests/unit/test_sync_async_parity.py` passa sem alteração. O caminho `instant` continua separado.

### Correções

- fix(storage): diferenças entre bancos que mudavam resultado. A busca por substring (`MemoryStore.search`, recall semântico, `KnowledgeService.query`) passa por `LOWER(coluna) LIKE ? ESCAPE '\'`: sem diferença entre maiúsculas e minúsculas só para letras ASCII, como já era no SQLite, e com `%`, `_` e `\` sempre literais (no `KnowledgeService.query` e no filtro por tags eles eram curingas). Os cortes por data de `symbiote audit ...`, do evolver e do tuner comparavam o `created_at` em ISO com `datetime('now', ...)`, de formato diferente, e erravam no dia do corte; agora o corte é calculado em Python. A consulta de sessões do evolver dependia de o SQLite escolher a linha do `MAX()` num `GROUP BY`; virou subconsulta explícita. Consultas de mensagens e memórias ganharam desempate por `rowid`, e a ordenação por `last_used_at` não depende mais de onde o banco põe NULL.

### Outros

- test: a suíte inteira roda também em PostgreSQL (`scripts/test-postgres.sh`, marcador `sqlite_only` para o que é específico de SQLite) e o gate do CI continua verde sem o driver instalado.

## [v0.8.1] - 2026-11-13

Observabilidade e governança para hosts (inspector, callbacks de progresso, contexto do chamador, veto do host, armazenamento injetável, redação de segredos, chaves de aprendizado), o adaptador do SymGateway, o demo interativo e as correções encontradas ao percorrer o demo com um LLM real.

**Mudança incompatível:** a entrada marcada com `!` (ordem da aprovação de tools) altera comportamento visível ao host e traz uma nota **MIGRAÇÃO**.

### Novas funcionalidades

- feat(kernel): redação de segredos no que o kernel persiste. `ToolDescriptor.secret_params` marca os parâmetros que carregam segredos: o valor vira `[REDACTED]` no `audit_log` e no trace, e passa a ser removido de tudo o que for gravado depois e do que a sessão já tinha gravado (a mensagem do usuário é salva antes da chamada que revela o segredo). `kernel.set_redactor(fn)` instala uma função do host `fn(texto) -> texto`, aplicada a todo texto derivado de conversa antes de gravar; se ela falhar, o texto é trocado por um marcador em vez de ser gravado sem checagem. `kernel.redactor.add_secret(valor)` registra um literal direto. Cobre `messages`, resumo de sessão, `memory_entries`, `audit_log`, `execution_traces`, `reflection_audit`, `skill_review_audit`, `dream_reports`, `decisions`, `process_instances`, `artifacts`, `harness_versions` e os arquivos de skill. Identificadores, timestamps e configuração do host (tools HTTP, persona, knowledge, `external_key`) não são reescritos, e logs não são cobertos. O modelo continua recebendo o valor real no turno que o usa.
- feat(kernel): chaves para desligar cada mecanismo de aprendizado, nenhuma ligada por padrão. Por symbiote (`environment.configure`): `reflection_mode="off"` e `memory_enabled=False` (nada é escrito em `memory_entries` por reflexão, memória de falha, handoff, dream ou `learn`, e nada é lembrado no contexto nem pelo `search_memories`). No kernel (`KernelConfig`): `skill_writes=False` deixa a biblioteca de skills somente leitura (tool `skill_manage`, review em segundo plano, ciclo de vida do dream e contador de uso), e `cross_symbiote_learning=False` faz `cross_learning_candidates()` não ler nada de outro symbiote.
- feat(kernel): retenção de memórias. `KernelConfig.memory_retention_days` e `kernel.prune(memories_days=...)` (CLI: `symbiote prune --memories-days N`) removem as memórias criadas há mais de N dias. `prune()` passa a devolver também `turn_contexts` e `memory_entries`.
- feat(kernel): `kernel.purge_symbiote(symbiote_id, dry_run=False)` apaga um symbiote e tudo o que o kernel guardou dele (sessões, mensagens, memórias, knowledge, traces, scores, auditoria, versões de harness, configuração, tools HTTP e, com `skill_scope="per_symbiote"`, o diretório de skills), sem tocar nos outros.
- feat(kernel): todo turno registra o que entrou no contexto. A tabela `turn_contexts` ganha uma linha por turno, com ou sem tools e mesmo quando o turno falha, com os IDs das memórias e do knowledge (`packed` no prompt ou `on_demand` via `search_memories`/`search_knowledge`) e os nomes das skills injetadas. Leitura por `kernel.inspector.list_turn_contexts(session_id=...)`. `AssembledContext` ganha `memory_ids` e `knowledge_ids`.
- feat(kernel): `KernelConfig.symbiote_cache_size` (padrão 256) limita os objetos por symbiote mantidos em memória. Os engines do review em segundo plano, antes guardados para sempre, passam a ser descartados por menos uso recente; o cache de skills usa o mesmo limite.
- feat(kernel): armazenamento injetável. `SymbioteKernel(config, llm, storage=...)` recebe o adaptador de armazenamento do host; sem o argumento, o kernel continua abrindo o SQLite de `config.db_path`. O adaptador injetado pertence ao host: o kernel chama `init_schema()` uma vez (agora parte do `StoragePort`) e não o fecha no `shutdown()`. O SQL enviado ainda é dialeto SQLite com marcadores `?`; a neutralização do dialeto e o adaptador PostgreSQL vêm depois.
- feat(kernel): contexto do chamador como contrato público. `kernel.message(..., host_context={...})` e `message_async` ligam um dicionário opaco do host a um único turno. Ele não entra no prompt, não é persistido nem logado (ao contrário do `extra_context`, que é renderizado no prompt). Handlers registrados com `pass_context=True` (`register_descriptor` / `register_tool`) são chamados como `handler(params, context)`, com `context` sendo o `ToolExecContext(symbiote_id, session_id, host)`; a assinatura `handler(params)` continua valendo, e `current_tool_context()` devolve o mesmo objeto. `kernel.set_approval_callback(cb, pass_context=True)` passa o contexto como quarto argumento. `current_host_context()` funciona em qualquer ponto do turno, inclusive no adaptador de LLM. A propagação para threads e tasks é garantida e testada sob concorrência (`symbiote.environment.exec_context`).
- feat(tools): veto do host. `kernel.set_tool_guard(guard)` instala `guard(tool_id, params, context)`, que devolve `ToolDecision.allow()` ou `ToolDecision.deny(motivo)` para toda chamada que a política permite, em qualquer nível de risco, antes da aprovação e do handler. A chamada negada não executa: o motivo volta ao modelo como resultado da tool e fica no `audit_log` (`action='denied'`) e no trace. Falha fechada: guard que lança exceção ou devolve outra coisa nega a chamada. O guard roda uma única vez por chamada e pode ser `async`. Vale também para `tool_gateway.execute()` chamado direto.
- feat(kernel): `kernel.set_progress_callbacks(on_progress=, on_stream=, on_plan=, on_block_complete=)` instala os callbacks de progresso nos runners internos do kernel (chat e long-run). Antes eles só existiam no construtor do `ChatRunner`, e o host precisava trocar o runner do kernel por outro, perdendo o review de skills e o protocolo configurado. `ChatRunner` e `LongRunRunner` ganham `set_progress_callbacks()`; o long-run ganha `on_plan(plan)`, chamado com o plano antes do primeiro bloco.
- feat(hooks): `BaseHook.on_context(context)`, síncrono, dispara a cada turno de chat com o `AssembledContext` logo depois de montado e antes da chamada ao modelo. Hooks existentes não precisam implementá-lo.
- feat(kernel): `kernel.inspector` (`KernelInspector`, somente leitura) lista sessões (com score), mensagens, memórias, scores, traces (com os passos), reflexões, relatórios de dream, reviews de skills e o audit log de tools, com as colunas JSON já decodificadas. `kernel.memory`, `kernel.knowledge`, `kernel.workspaces` e `kernel.context_assembler` passam a ser propriedades públicas.
- feat(ui): `<symbiote-chat mode="inline">` desenha um painel fixo que ocupa o contêiner, sem botão flutuante, overlay nem botão de fechar.
- feat(kernel): acessos públicos que antes exigiam atributos privados: `kernel.update_symbiote(id, name=, role=, persona=)` (com auditoria da persona) e `kernel.list_symbiotes()`; `kernel.discover_tools(symbiote_id, source_path, url=)` e `kernel.discovered_tools` (status pending/approved/disabled); `kernel.tune_parameters(symbiote_id, days=, apply=)`, `kernel.cross_learning_candidates(symbiote_id)` e `kernel.harness_default_text(component)`. `evolve_harness` aceita `default_text` omitido e usa o texto embutido do componente.
- feat(llm): `SymGatewayLLMAdapter` (`symbiote.adapters.llm.symgateway`) chama LLMs pelo SymGateway com a chave do gateway no lugar da do provider. `list_models()` lê `GET /v1/models` (só `service_type` `llm`); cada conversa ganha uma sessão de trabalho (`POST /_api/work-sessions`) cujo uid vai em `X-SymGateway-Session-Id` em todas as chamadas dela. O host diz a qual conversa a chamada pertence com `session_key=` (um callable; sem chave, as chamadas dividem uma sessão padrão); `with_model()` dá um segundo modelo que divide as mesmas sessões. `X-Project-Slug` só vai quando `project_slug=` é informado. Uma sessão recusada (404/409/410) é reaberta uma vez. Fala direto com `/v1/chat/completions` via httpx, porque o provider `symgateway` do forge_llm não envia o header de sessão.
- feat(demo): `demo/` — chat com um assistente pessoal e dez abas que mostram o kernel por dentro (turno ao vivo, contexto, memória, aprendizado, tools, execução, skills, harness, web, sistema), com roteiro de apresentação. Usa um LLM real (Anthropic, OpenAI ou SymGateway; a chave vai para `demo/demo.env`), roda só em 127.0.0.1 e serve também de exemplo de host. Ver `demo/README.md`.
- feat(demo): memórias semeadas para que o passo do dream mostre poda, reconciliação e generalização num demo recém-criado (o dream fica desligado até esse passo); pedidos de aprovação com contagem regressiva, aviso no título da aba, aviso quando expiram e prazo real depois de recarregar a página; modelo do SymGateway escolhido numa lista; "Zerar demo" sem recarga em loop; encerrar a sessão sempre abre uma nova.

### Correções

- fix(tools)!: a aprovação de tools de risco alto passa a rodar depois da política e do veto do host (B-75). Antes a pessoa era consultada sobre uma chamada que a política bloquearia em seguida.
  - **MIGRAÇÃO:** o callback de aprovação deixa de ser chamado para tools que o symbiote não está autorizado a usar; a chamada é recusada antes, com o mesmo erro `Tool '<id>' blocked: ...` e a mesma linha `blocked` no `audit_log`. Como a recusa acontece antes do gateway, os hooks `before_tool`/`after_tool` não disparam para essa chamada. Quem registrava tools de risco alto sem autorizá-las (`environment.add_tools`) e dependia do callback ser chamado precisa autorizá-las.
- fix(chat): no `message_async`, o callback de aprovação de tools de alto risco roda numa thread em vez de no event loop. Um callback que espera a resposta de uma pessoa travava o servidor inteiro, inclusive a requisição que traria a resposta.
- fix(long-run): o callback de aprovação passa a valer dentro dos blocos de `tool_mode="long_run"`; antes os blocos rodavam tools de alto risco sem consultar o host. `kernel.set_approval_callback` configura os dois runners.
- fix(knowledge): `KnowledgeService.query` passa a encontrar entradas por termos em comum (sem stop-words, acentos e sufixos, como o recall de memória), depois das que contêm o texto literal. Antes era só `LIKE '%<mensagem inteira>%'`, então no modo `packed` uma pergunta normal quase nunca trazia o knowledge para o prompt.
- fix(memory): a tool `search_memories` (modo `on_demand`) passa a trazer, depois dos resultados por substring, as memórias com termos em comum com a busca (o mesmo ranking do recall). O modelo busca por palavras-chave ("prioridade financeira ano meta"), que nunca são substring de uma memória, e a busca voltava vazia.
- fix(long-run): o avaliador recebe a lista das tool calls que o bloco executou (tool, parâmetros, ok/falhou), registrada pelo runtime. Antes ele via só o texto final do gerador, não conseguia verificar trabalho feito por tools e reprovava o bloco; o retrabalho repetia os efeitos (num teste real, 35 tarefas criadas viraram 65). O prompt de retrabalho agora lista as chamadas da tentativa rejeitada e pede para não repeti-las. `BlockResult.tool_steps` guarda essas chamadas.
- fix(long-run): a evidência do avaliador começa com o total de chamadas por tool (`create_task: 74 calls, 74 ok`), contado sobre todas as chamadas e não só as 60 listadas, e pede que ele confira os números que o gerador afirma sobre as próprias ações. Com só a lista, o avaliador não contava: aprovou um bloco que dizia ter criado 87 tarefas quando eram 74.
- fix(skills): o review de skills recebe, além das mensagens, as tool calls da sessão (dos traces persistidos). Com só as respostas finais ("Pronto! Salvei o resumo") não havia procedimento para virar skill, e o review devolvia `[]`.
- fix(long-run): um handoff sem blocos pendentes (projeto terminado) não é mais injetado na sessão seguinte, e o planejador só recebe "planeje apenas os pendentes" quando há pendentes. Antes, repetir um pedido já concluído gerava um plano vazio e o turno falhava com "Planner produced no blocks of work".
- fix(environment): `configure(planner_prompt="")` e `configure(evaluator_prompt="")` limpam o prompt. Antes não havia como limpar (`None` significa "não mudar"), e um `""` gravado ligava o avaliador do long-run com o prompt padrão.
- fix(ui): no streaming do `<symbiote-chat>`, uma resposta que chega só no `response_done` (sem `text_delta` antes) ou um evento `error` deixavam a bolha vazia e ela era removida. Agora o texto final fica na tela e prevalece sobre os deltas.

### Outros

- chore(types): `forge_scraper` entra no override de dependências opcionais sem tipos do mypy; com o extra `[extract]` instalado, o mypy falhava com `import-untyped`.

## [v0.8.0] - 2026-11-13

Rodada de correções do relatório de melhorias de 2026-11-13 (`reports/2026-11-13_relatorio-melhorias.md`): isolamento de memória por symbiote, autenticação obrigatória na API, SSRF e DNS pinning, loop de tools, migrações versionadas do SQLite e mypy no CI. Testes: 1758 → 2234; cobertura 85,5% → 90,1%.

**Mudanças incompatíveis:** as 16 entradas marcadas com `!` alteram comportamento visível ao host; cada uma traz uma nota **MIGRAÇÃO**, e o roteiro de atualização está em `docs/HOST_INTEGRATION.md` (seção "Upgrading").

### Novas funcionalidades

- feat(kernel): retenção opcional de mensagens e traces (P-2, D-12). `KernelConfig.message_retention_days` e `trace_retention_days` (padrão `None`, desligado) e `kernel.prune(dry_run=...)` removem as mensagens e os traces de sessões fechadas há mais de N dias; sessões ativas nunca são tocadas, e memórias, resumos e scores ficam. Nada roda sozinho: o novo `symbiote prune --messages-days N --traces-days N [--dry-run]` é para um cron.

### Melhorias

- perf(storage): índices para as consultas que rodam a cada turno e a cada fechamento de sessão (P-1): `messages(session_id, created_at)`, `memory_entries(symbiote_id, is_active, type)`, `sessions(symbiote_id, status, ended_at)` e `execution_traces(session_id)`. Antes essas consultas varriam a tabela inteira, e o tempo de `kernel.message()` crescia com o total de mensagens de todas as sessões. Os índices são criados ao abrir o banco.
- perf(storage): conexões SQLite com `PRAGMA synchronous=NORMAL` junto do WAL (P-3, D-13). Com WAL, `NORMAL` não perde dados num crash da aplicação; só uma queda do sistema operacional ou de energia pode perder os últimos commits. Em troca, cada commit deixa de esperar um fsync.
- refactor(kernel): `set_approval_callback`, `set_native_tools` e `set_agent_protocol` do kernel deixam de escrever em atributos privados dos runners. `ChatRunner` e `LongRunRunner` ganham `set_approval_callback()` / `set_native_tools()` públicos. Sem mudança de comportamento (Q-3).
- refactor(api): `api/http.py` (1768 linhas) foi dividido em `api/schemas.py` (modelos) e `api/routes/` (`symbiotes`, `sessions`, `tools`, `discovery`, `admin`, `console`, `harness`), um `APIRouter` por área (Q-3). O `http.py` mantém o app, o lifespan, os handlers de exceção, as dependências, a autorização e o estado de módulo (os testes continuam fazendo monkeypatch em `symbiote.api.http._kernel` etc.), e `symbiote.api.http:app` segue sendo o ponto de entrada. A OpenAPI gerada é idêntica à de antes, byte a byte. A varredura de autorização dos testes passou a percorrer também os routers incluídos.

### Correções

- fix(memory)!: memória isolada por symbiote (S-1). `MemoryStore.search` e `get_relevant` passam a filtrar por `symbiote_id`; `%` e `_` deixam de ser curingas. As tools `search_memories` e `search_knowledge` usam o symbiote do contexto de execução do `ToolGateway` e ignoram `symbiote_id` vindo do LLM. A reflexão LLM não aplica PATCH em memória de outro symbiote.
  - **MIGRAÇÃO:** `MemoryStore.search()` e `get_relevant()` exigem o argumento nomeado `symbiote_id` (e quem implementa `MemoryPort` deve aceitá-lo); `get`, `update` e `deactivate` aceitam `symbiote_id` opcional. `GET /memory/search` exige o query param `symbiote_id`, `SymbioteClient.search_memory` o argumento `symbiote_id` e `symbiote memory search` a opção `--symbiote`.
- fix(api)!: autenticação obrigatória em toda a API e checagem de dono por rota (S-2). Uma dependência global (`enforce_auth`) exige chave fora da allowlist (`/health`, `/`, `/docs`, `/openapi.json`, `/redoc`), de modo que uma rota nova não nasce aberta. Rotas de sessão, mensagem, fechamento, memória e tools passam a conferir o dono; as rotas `/api/*` do Console exigem chave admin. `POST /sessions` para symbiote inexistente devolve 404 em vez de 500. O Console envia a chave em todas as chamadas e avisa quando ela falta. `kernel.create_symbiote` aceita `owner_id`.
  - **MIGRAÇÃO:** clientes que chamavam sem chave `POST /sessions`, `GET /sessions/{id}`, `GET /sessions/by-key/{key}`, `POST /sessions/{id}/messages`, `POST /sessions/{id}/close`, `GET /memory/search`, as rotas `/symbiotes/{id}/tools*` ou `/api/*` passam a receber 401. Symbiotes sem `owner_id` só são acessíveis com chave admin, e as sessões de symbiotes apagados respondem 404.
- fix(api): entrada inválida devolve 4xx em vez de 500 (A-1). `role` de mensagem e de chave de API viram `Literal`; erros de validação dos modelos de domínio (`EnvironmentConfig`, `Message`) devolvem 422; `sqlite3.IntegrityError` devolve 409; o teto de `max_tool_iterations` e o papel inválido de chave levantam `symbiote.core.exceptions.ValidationError`, que agora também é `ValueError`.
- fix(api): limites de tamanho (S-10). Corpo de requisição limitado a 5 MB por um middleware ASGI (vale para `Content-Length` e corpo chunked; responde 413); mensagens a 1 milhão de caracteres; nomes, papéis e ids a 200; resposta de HTTP tool a 5 MB, acima disso vira erro da tool em vez de ser lida inteira. Os valores ficam em `symbiote.api.limits` e `environment.tools.MAX_HTTP_TOOL_RESPONSE_BYTES`. Respostas de HTTP tool que não são UTF-8 deixam de levantar `UnicodeDecodeError`.
- fix(security): a validação de SSRF bloqueia IPv6 mapeado para IPv4 (`::ffff:127.0.0.1`, `::ffff:169.254.169.254`), o prefixo NAT64 e qualquer endereço que não seja global e unicast (`::`, multicast, reservados, faixas de benchmark), inclusive em destinos de redirect (S-3).
- fix(tools): os valores dos placeholders de `url_template` passam a ser codificados (`quote(safe="")`), então um valor vindo do LLM não acrescenta segmentos de path, parâmetros de query nem fragmento, e valores com espaço ou acento funcionam. Placeholders opcionais com valor `False` ou `0` deixam de ser removidos (S-7).
  - **MIGRAÇÃO:** valores com `/`, `?` ou `&` em placeholders deixam de alterar a URL. Exceção: um placeholder no início do template (`{base_url}/...` ou `{url}`) é inserido sem codificação.
- fix(tools): num redirect para outra origem (esquema, host ou porta), a HTTP tool deixa de reenviar `Authorization`, `Cookie`, `Proxy-Authorization` e os headers definidos em `headers` e `header_factory` (S-8).
- fix(console): `esc()` passa a escapar aspas simples e duplas, então um valor vindo de memória, mensagem ou nome de symbiote não injeta atributos HTML (como `onmouseover`) no Console. Os botões que recebiam dados em `onclick` inline usam atributos `data-*` com um handler delegado (S-6).
- fix(tools): novo `KernelConfig.fs_allowed_root` confina `fs_read`, `fs_write` e `fs_list` (o caminho é resolvido com symlinks e comparado por `Path.is_relative_to`; `allowed_root` nos parâmetros só restringe) e define o diretório inicial do `bash`, cujo `_cwd` não pode sair da raiz. O timeout do `bash` mata o grupo de processos inteiro e é limitado a 300 s. A checagem por prefixo de string, que aceitava `ws-evil` com raiz `ws`, foi substituída (S-9).
  - **MIGRAÇÃO:** com `fs_allowed_root` definido, caminhos fora dele são recusados e caminhos relativos passam a ser relativos à raiz. Sem ele, o comportamento anterior se mantém (com aviso no log).
- fix(cli): `.symbiote/config`, que guarda a API key, passa a ser gravado com modo `0600` (e um arquivo antigo é restringido na próxima escrita); valores com `%` deixam de quebrar `symbiote init` (S-11).
- fix(chat): a compactação por orçamento (camada 3) deixa de resumir a troca mais recente do loop, cujo resultado o modelo ainda não leu; resume só os passos anteriores e, se a última troca sozinha não couber, trunca os resultados em vez de removê-los. Antes, um system prompt grande fazia o modelo nunca ver o resultado e repetir tools com efeito colateral (E-1). Novo `KernelConfig.loop_context_budget` (padrão 16000 tokens, o valor que já era fixo) chega ao `ChatRunner`; o kernel avisa no log quando `tool_result_max_chars` passa do que esse orçamento comporta. `total_tokens_estimate` passa a contar os schemas de tools e o `extra_context`, então `inspect().within_budget` pode ser `False` onde antes era `True`.
- fix(tools): handlers `async def` passam a rodar também pelo caminho síncrono (`kernel.message`, CLI, `POST /sessions/{id}/chat`) e pelo modo `instant` de `message_async`; antes a tool não executava, o audit registrava sucesso e o modelo recebia o `repr` de uma coroutine (E-5). O `ToolGateway` mantém um event loop próprio numa thread daemon, criado no primeiro uso e encerrado por `kernel.shutdown()`; os `contextvars` de quem chama são copiados para o handler.
- fix(tools): o timeout de tool devolve o controle no prazo em todos os caminhos. O `PolicyGate` saía do `with ThreadPoolExecutor` esperando a thread terminar (timeout de 0,2 s com tool de 2 s retornava em 2 s); o caminho async segurava o executor padrão do loop; e no caminho paralelo cada chamada esperava `timeout` em sequência. Agora o handler roda numa thread daemon, as chamadas paralelas compartilham um prazo contado da submissão (no máximo 4 simultâneas) e o timeout é logado. A thread não pode ser interrompida: depois do timeout o handler continua em segundo plano (E-4).
- fix(chat)!: um loop que esgota iterações ou tempo deixa de devolver sucesso com texto vazio ou com a narração de um turno de tool (E-6). Ao atingir `max_tool_iterations`, o modelo recebe uma instrução de parar e uma chamada final sem tools (como já acontecia na estagnação); no timeout, o turno termina com um texto fixo; se o modelo insistir em pedir tools, sai um texto genérico. O `LoopController` passa a contar rodadas do modelo (`end_round()`), não tool calls: 12 chamadas paralelas numa rodada valem uma iteração. `total_iterations` no trace conta as chamadas ao LLM realmente feitas. Mensagens de injeção vindas de `harness_versions` com `{tool}`, JSON ou chaves soltas não levantam mais `KeyError`. Com `tool_loop` desligado o turno continua de chamada única.
  - **MIGRAÇÃO:** o turno que atinge o limite de iterações custa uma chamada de LLM a mais. `LoopController.record()` não avança mais a contagem; quem usa a classe diretamente deve chamar `end_round()` por rodada.
- fix(chat): tool calls malformados não derrubam mais o turno nem executam tools com argumentos inventados (E-7). O parser de texto decodifica o JSON a partir da cerca de abertura (uma cerca ``` dentro de um valor não corta mais o bloco), aceita uma lista de chamadas num bloco e descarta entradas inválidas (`params` nulo, string, lista, `tool` não-string) em vez de levantar `AttributeError`/`ValidationError` com a mensagem do usuário já gravada. No caminho nativo, argumentos que não decodificam como objeto (JSON truncado por `finish_reason=length`) não executam a tool: o modelo recebe o erro. `content=None` do provedor vira texto vazio.
- fix(chat): o validador de resposta deixa de rejeitar citações (`[1]`), links markdown, checklists e JSON pedido pelo usuário (E-8). Uma resposta só é inválida se estiver vazia, se sobrar uma cerca `tool_call` ou se, fora de outros blocos de código, contiver um objeto JSON com chave `tool` ou `params`. Se as reformulações não resolverem, o texto original do modelo é devolvido em vez do pedido de desculpas fixo (que fica só para resposta vazia ou bloco `tool_call` cru).
- fix(kernel): trace e handoff passam a ser da sessão que os produziu (E-3). O kernel usa o `RunResult` da própria chamada (novos `CapabilitySurface.run_chat`/`run_chat_async`), persiste o trace mesmo quando o run falha e, no `close_session`, lê o último trace da sessão em `execution_traces`; o handoff de long-run fica num dicionário por sessão. Antes, sessões simultâneas gravavam traces umas das outras e uma mensagem de outra sessão antes do fechamento trocava o score e suprimia a memória de falha. Os atributos privados `_last_trace`/`_last_handoff` do kernel foram removidos; `capabilities.last_loop_trace`/`last_handoff_data` continuam, marcados como obsoletos.
- fix(hooks): os hooks registrados em `kernel.hooks` passam a disparar (E-2). Antes nenhum ponto do código chamava `before_tool`, `after_tool`, `before_turn` ou `after_turn`, embora o README e o `HOST_INTEGRATION.md` os apresentassem como ponto de auditoria. Os eventos de tool disparam no `ToolGateway` (todos os caminhos de execução) e os de turno em `kernel.message()`/`message_async()`; um hook que levanta é logado e não interrompe o turno.
- fix(long-run): `tool_mode="long_run"` passa a usar de fato o `LongRunRunner`, que o kernel registra e a `CapabilitySurface` escolhe por modo (inclusive em `message_async`); antes o modo rodava como chat comum, sem planner nem handoff (E-2). Correções no runner: um bloco reprovado pelo avaliador (mesmo após o retry) conta como falha; planner e avaliador aceitam respostas `LLMResponse`; `run()` aceita o `llm_config` que o kernel sempre passa; o preflight consulta o `health_check` dos handlers registrados no gateway (antes lia um atributo inexistente); `pending_blocks` do handoff lista os blocos não concluídos, em qualquer posição; `context_strategy="compaction"` leva a saída dos blocos anteriores ao próximo. `RunnerRegistry.get(runner_type)` é novo.
- fix(process): `ProcessEngine.advance()` passa a derivar o passo atual de `current_step` persistido; depois de um restart ou de `invalidate_cache()` o processo regredia ao primeiro passo e registrava passos duplicados. O `ProcessRunner` aceita `"<processo>: detalhes"`, a forma que `work()` usa para escolhê-lo; antes falhava sempre (E-2).
- fix(chat): `message_async` deixa de executar a chamada síncrona ao LLM (e o `time.sleep` do retry) na thread do event loop (E-9). As chamadas ao modelo do loop async, da reformulação e da resposta final rodam em `asyncio.to_thread`; o modo `instant` inteiro roda numa thread, com `on_token` devolvido à thread do loop em ordem. Quatro sessões com LLM de 0,3 s levavam 1,2 s e congelavam o loop; agora levam ~0,3 s.
- fix(llm): a decisão de retentar uma chamada ao LLM passa a usar o status HTTP e o tipo do erro, não palavras da mensagem (E-10). O `ForgeLLMAdapter` embrulhava tudo em `LLMError(str(exc))`, então erros de conexão, 500 e 504 nunca eram retentados, enquanto um 400 cuja mensagem citava "timeout" ou "503" era retentado três vezes. `LLMError` ganhou `status_code` e `retryable`, preenchidos pelo adapter a partir da exceção original; `is_transient_llm_error()` retenta 408/425/5xx e erros de conexão/timeout, e não retenta 429 (os SDKs já retentam rate limit) nem erros de arquivo.
  - **MIGRAÇÃO:** adapters de host que levantam exceções genéricas com "503" só na mensagem deixam de ser retentados; levante `LLMError(..., retryable=True)` ou uma exceção com `status_code`.
- fix(chat): `run_async()` passa a respeitar `on_after_tool_result` (antes ignorado, gerando uma chamada de LLM a mais e outro texto) e a disparar o nudge de review de skills, como `run()` (E-11). No modo `instant`, `on_token` deixa de receber o bloco `tool_call` cru quando o texto final é outro. Um teste parametrizado roda os mesmos cenários pelos dois caminhos.
- fix(chat): uma segunda compactação do loop deixa de perder o resumo da primeira (E-12). No modo nativo o resumo anterior era descartado inteiro; no modo texto virava um único passo truncado. As duas camadas reconhecem resumos anteriores e carregam seus passos; cada passo do resumo ocupa uma linha.
- fix(subagent): a profundidade de delegação passa a ser contada por cadeia (`ContextVar`), não num contador único do kernel: cinco `spawn` paralelos de profundidade 1, ou sessões diferentes delegando ao mesmo tempo, não batem mais no limite de 3. Um `spawn` cujo sub-run falha fecha a sub-sessão em vez de deixá-la ativa. A constante `_BLOCKED_TOOLS`, que nunca foi aplicada, foi removida junto com a promessa de "restricted tool sets" do docstring (E-12).
- fix(kernel): um turno que falha remove a mensagem do usuário que tinha gravado, então um retry do cliente ou do `MessageBus` não deixa mensagens `user` repetidas sem resposta (E-12). O `MessageBus` loga a exceção do handler (`exc_info`) a cada tentativa e, ao esgotar os retries, publica um `OutboundMessage` de erro para o canal (`metadata.error = True`) em vez de descartar em silêncio. `SessionManager.delete_message()` é novo.
  - **Observação:** o retry do bus ainda reexecuta tools com efeito colateral; idempotência fica para o host.
- fix(memory): o recall padrão passa a buscar memórias por termos, não pela mensagem inteira como substring (M-1). Antes "Which editor do I prefer?" não encontrava "User prefers the vim editor". `get_relevant` pontua as memórias do symbiote pela sobreposição de termos (com importância e recência), ignorando caixa, acentos, stop-words em inglês e português e sufixos comuns; memórias da sessão atual vêm primeiro. O tokenizer de `memory/recall.py` ganhou suporte a acentos e português.
- fix(api)!: `POST /sessions/{id}/close` passa a chamar `kernel.close_session` (score, memória de falha, reflexão, handoff, review de skills e dream). Antes chamava só `SessionManager.close`, então symbiotes servidos por HTTP (inclusive pelo SDK) nunca aprendiam com as sessões (M-2).
  - **MIGRAÇÃO:** a rota fica mais lenta, na medida do que a reflexão e o review configurados custam.
- fix(reflection)!: a reflexão por palavra-chave (modo padrão) passa a aprender só com mensagens do usuário, por palavra inteira, em inglês e português ("prefiro", "sempre", "nunca", "decidi", "regra"...), e ignora mensagens longas (M-3). Antes, só termos em inglês por substring, em qualquer papel: uma resposta do assistente com "always" virava memória `constraint`. Uma marca d'água por sessão (`sessions.reflected_until`) evita refletir de novo as mesmas mensagens quando uma sessão reaberta é fechada outra vez; `session_scores` fica com uma linha por sessão (atualizada) e o score entra uma vez só na média do harness. `kernel.get_session()` e `GET /sessions/{id}` deixam de reabrir sessões fechadas (`SessionManager.get()` é novo).
  - **MIGRAÇÃO:** coluna nova `sessions.reflected_until`, criada no boot. Mensagens do assistente deixam de gerar memórias por palavra-chave.
- fix(reflection): na reflexão por LLM, um fato com `importance` inválida ("high", `null`, lista) é descartado e valores fora de [0, 1] são limitados, em vez de levantar exceção e deixar a sessão aberta com fatos parciais gravados; o número de fatos descartados vai para `reflection_audit.llm_error` (M-4). Cada passo de `close_session` (score, memória de falha, reflexão, handoff, review, dream) roda isolado: uma falha é logada e a sessão é fechada mesmo assim.
- fix(session): `get_or_create_session` com a mesma `external_key` nova, chamado ao mesmo tempo por várias threads, cria uma única sessão (antes 8 threads chegavam a criar 8). Um lock por chave, compartilhado no processo, serializa o busca-ou-cria, e o índice único novo `(symbiote_id, external_key)` cobre outros processos. Bancos que já têm duplicatas sobem normalmente: o índice não é criado e um aviso informa quantas chaves estão duplicadas; nada é apagado (M-10).
  - **MIGRAÇÃO:** `(symbiote_id, external_key)` passa a ser único para sessões (índice parcial). Um banco que já tenha chaves duplicadas sobe normalmente, com um aviso no log, mas sem o índice até as duplicatas serem resolvidas. Inserir direto no banco uma segunda sessão com a mesma chave falha com `IntegrityError`.
- fix(dream)!: a fase determinística `Reconcile` passa a unir só quase-duplicatas (mesmo tipo, tags parecidas, similaridade de palavras ≥ 0,8) e nunca desativa tipos protegidos (`constraint`, `handoff`). Antes, memórias com as mesmas tags e conteúdo *diferente* eram tratadas como contradição: quatro regras distintas da equipe viravam uma, e a memória vencedora ganhava `dream:reconciled` de novo a cada ciclo (M-5).
  - **MIGRAÇÃO:** `ReconcilePhase(content_similarity_threshold=...)` virou `duplicate_similarity_threshold` (semântica invertida: acima dele é duplicata). Contradições ficam para o modo `full`, com LLM.
- fix(dream): o kernel passa a construir o `DreamEngine` a cada ciclo com a configuração atual do symbiote (antes, no escopo `global`, um singleton guardava a config do primeiro symbiote) e a controlar as threads de dream por symbiote, de modo que dois ciclos do mesmo symbiote nunca rodam juntos (no escopo `per_symbiote` a guarda nunca funcionava). `dream(dry_run=True)` deixa de contar como último dream; a fase `Generalize` não gera de novo a mesma generalização a cada ciclo; o dream é avaliado depois de fechar a sessão, então dispara no N-ésimo fechamento, não no N+1 (M-6).
- fix(harness)!: o rollback do harness volta para a versão de onde a atual foi derivada (`parent_version`), não para "a de número anterior": depois de reverter v2 para v1 e evoluir v3 a partir de v1, reverter v3 não reativa mais a v2 rejeitada. Reverter uma versão derivada do texto padrão volta ao padrão. As sessões rodadas com o texto padrão passam a ser pontuadas numa linha de baseline (versão 0), então a primeira versão evoluída também pode ser comparada e revertida. O evolver conta sessões, não traces (antes 1 falha + 1 sucesso passavam pelas guardas "5 + 3"), e respeita `MIN_EVOLUTION_INTERVAL_DAYS` e um mínimo de sessões pontuadas na versão ativa antes de evoluir de novo. Versões criadas sem `parent_version` passam a ter como pai a versão ativa. O `ParameterTuner` deixa de reduzir `max_tool_iterations` quando a maioria das sessões bate no limite (M-7).
  - **MIGRAÇÃO:** reverter a única versão de um componente agora devolve sucesso (volta ao padrão) em vez de `False`/404; `list_versions` não lista a linha de baseline.
- fix(skills)!: o nome no frontmatter do `SKILL.md` passa a ser obrigatório e igual ao nome do diretório em `create`, `edit` e `patch` (M-8). Antes, uma skill em quarentena criada com `name: deploy` em outro diretório escondia a skill curada `deploy`, e uma skill sem frontmatter ficava no disco invisível ao loader e aos limites. O nome de skill não aceita mais `\n` no fim. Toda escrita do `SkillsStore` chama `on_change`, que o kernel liga a `SkillsLoader.refresh`, então uma skill criada por `skill_manage` aparece no `skill_view` na hora. O review em background conta ativas/quarentena pelo disco (`SkillsStore.count_by_status()`), e `max_active_skills` passa a valer: uma skill em quarentena não é promovida enquanto o limite de ativas está cheio.
  - **MIGRAÇÃO:** `create`/`edit`/`patch` de `SKILL.md` sem frontmatter ou com `name` diferente do diretório levantam `SkillValidationError`.
- fix(skills): três caminhos que contornavam a quarentena de skills foram fechados (S-4). O `skill_view` confere o status antes de carregar a skill, então visualizações recusadas não contam mais uso (antes três recusas promoviam a skill em quarentena e entregavam o conteúdo). O review em background só altera (`edit`, `patch`, `write_file`) skills criadas pelo agente; uma skill humana ou curada não é mais reescrita a partir de uma transcrição. No escopo `per_symbiote`, `skills_extra_roots` é só leitura: um symbiote não altera mais uma skill que outro lê. `SkillsLoader.peek()` é novo.
  - **Observação:** com isso a autopromoção só acontece por carregamentos de skills já ativas; skills em quarentena passam a ser promovidas manualmente.
- fix(kernel): `shutdown()` passa a esperar as threads de review de skills e de dream (até `timeout`, padrão 10 s) antes de fechar o storage (M-9). Antes, num processo curto como a CLI, o review final morria antes da resposta do LLM; num processo longo, a thread gravava a skill depois do shutdown e o audit falhava com "Cannot operate on a closed database". Threads que passam do prazo são listadas num aviso.
- fix(discovery)!: uma tool descoberta já aprovada volta para `pending` quando uma nova descoberta muda seu método, URL, parâmetros ou risco; antes, um `GET` aprovado podia virar `DELETE` em outro host e seguir aprovado (S-5). O loader só concede `allow_internal` e o `header_factory` (credenciais do host) a URLs da mesma origem do `base_url`, e não deixa um `operationId` substituir uma tool builtin como `bash`. A descoberta por URL aceita só `http(s)` e uma spec malformada vira erro no resultado em vez de exceção.
  - **MIGRAÇÃO:** tools aprovadas cuja definição mudou precisam de nova aprovação; tools de outra origem que o `base_url` deixam de receber os headers do host.
- fix(api): a API passa a ter um único ponto de inicialização (A-2). Um `lifespan` cria o kernel na subida e o fecha na saída; `get_adapter()` devolve o storage do próprio kernel (uma conexão SQLite e um `APIKeyManager` por processo, em vez de dois). A primeira requisição de um processo novo responde 401 para chave ausente em vez de 503, porque a autenticação inicializa a API sob demanda. A configuração de logging (DEBUG no logger raiz e o buffer do Console) saiu do import e roda só quando a app é servida, então um host que importa `symbiote.api` mantém o próprio logging. Uma falha ao criar o adapter de LLM é logada e a API sobe sem chat, em vez de derrubar todas as rotas.
- fix(tools)!: tools HTTP registradas pela API (`POST /symbiotes/{id}/tools`) ou pela CLI (`symbiote tools add`) passam a ser persistidas (tabela `http_tools`) e registradas no gateway do kernel, o mesmo que o chat usa (T-1). Antes a API as colocava num gateway paralelo que o chat nunca consultava ("tool not registered"), e a CLI as perdia ao fim do processo. Registrar um `tool_id` de builtin (ex.: `bash`) ou de outro symbiote responde 409. Remover uma tool só desautoriza builtins e tools de outros symbiotes para quem pediu; antes, `DELETE /symbiotes/{id}/tools/bash` tirava o `bash` de todos.
  - **MIGRAÇÃO:** tools registradas pela API antes desta versão viviam só em memória; registre-as de novo. Os `headers` informados no registro ficam gravados no banco em texto puro.
- fix(discovery)!: tools descobertas enviam seus parâmetros `in: query`; antes a query era descartada e um `GET /search?q=...` chegava sem `q` (T-2). Os parâmetros de query entram na `url_template` (`?q={q}&limit={limit}`), os opcionais ausentes são omitidos e, num `POST`, ficam fora do body. Rotas sem `operationId` incluem os path params no `tool_id` (`/items/{id}` vira `get_items_by_id`), então `/items`, `/items/{id}` e `/items/{id}/{rev}` geram três tools em vez de uma. Conversores como `{file_path:path}` (Starlette) e `<int:user_id>` (Flask) viram placeholders simples. Duas operações com o mesmo `tool_id` geram um aviso em `result.errors` em vez de uma ser descartada em silêncio. Parâmetros `in: header`/`cookie` saem do schema.
  - **MIGRAÇÃO:** tools sem `operationId` em rotas com path params ganham um `tool_id` novo e precisam ser aprovadas de novo; as antigas ficam no banco até serem desativadas.
- fix(tools)!: as meta-tools dos modos de carregamento funcionam sem autorização manual (T-3). Com `tool_loading="index"`, o `get_tool_schema` é liberado pelo próprio modo, como o prompt já pedia ao modelo; antes era bloqueado e o modo `index` não funcionava. Ele também passa a descrever só tools que o symbiote chamador pode usar e que estão nas suas `tool_tags`; antes devolvia o schema de qualquer tool registrada no gateway. Com `context_mode="on_demand"`, `search_memories` e `search_knowledge` são liberadas pelo modo e oferecidas ao modelo mesmo quando as `tool_tags` as esconderiam.
  - **MIGRAÇÃO:** quem usa `get_tool_schema` para ler o schema de tools não autorizadas precisa autorizá-las.
- fix(environment)!: `configure()` deixa de perder atualizações concorrentes e de duplicar configs (T-4). A leitura-modificação-escrita roda sob um lock, e um índice único em `(symbiote_id, workspace_id)` impede linhas duplicadas entre processos. Na migração, as duplicatas existentes são consolidadas: fica a linha mais recente, com a união das tools de todas. Novos `add_tools()`/`remove_tools()` autorizam e revogam sem corrida; o kernel já os usa. `memory_share=0.0`, `knowledge_share=0.0` e os demais zeros voltam como zero, em vez do valor padrão. Uma config de workspace nova parte da config do symbiote: antes, `configure(workspace_id=..., prompt_caching=True)` criava a config do workspace sem nenhuma tool.
  - **MIGRAÇÃO:** configs de workspace criadas a partir de agora herdam os campos da config do symbiote que não forem informados.
- fix(kernel)!: `load_discovered_tools()` e `load_mcp_tools()` acrescentam as tools carregadas às já autorizadas do symbiote; antes substituíam a lista inteira e um symbiote perdia `fs_read`, `skill_view` etc. ao carregar tools descobertas (T-5, D-9).
  - **MIGRAÇÃO:** quem dependia da substituição deve revogar as tools antigas com `environment.remove_tools()`.
- fix(browser): o handler de SIGTERM do supervisor do browser fecha as sessões e depois deixa o sinal seguir seu curso (T-6). Antes ele substituía o handler do host e engolia o SIGTERM: o processo não terminava (systemd precisava de SIGKILL) e o handler que o host tinha instalado nunca era chamado. Agora o handler anterior é chamado depois do cleanup; sem handler, o padrão é restaurado e o sinal reenviado. Se o host ignorava SIGTERM, nada é instalado e o cleanup fica só no `atexit`.
- fix(browser)!: itens menores do browser e da busca (T-7). A `policy` de domínios passa a valer também para `web_extract` e `web_crawl`; antes só o `browser_navigate` a respeitava. Um ponto final no host (`evil.com.`) ou no padrão não contorna mais a blocklist. `register()` marca o próprio kernel em vez de guardar `id(kernel)`, que podia ser reutilizado por um kernel novo e fazê-lo pular o registro. `register()` falha na hora, com o `pip install` certo, quando o extra do backend pedido (`browser` para `chromium`, `extract` para `forge_scraper`) não está instalado; antes falhava só na primeira chamada da tool. `web_crawl` aceita domínio sem esquema (`docs.example.org`), como o próprio schema da tool pede.
  - **MIGRAÇÃO:** `register(browser_backend="chromium")` sem `playwright` instalado e `register(extract_backend="forge_scraper")` sem `forge_scraper` agora levantam `ImportError`.
- fix(api): itens menores da API (A-3). Chat num servidor sem LLM responde 503 dizendo para definir `SYMBIOTE_LLM_PROVIDER`, em vez de um 400 genérico ("No runner available"). O `/health` resolve versão e commit uma vez, na subida, em vez de rodar `git rev-parse` a cada chamada. Nome de symbiote vazio ou só com espaços é recusado (422) na criação e na edição. O `generation_settings` do `POST /sessions/{id}/chat`, que o SDK já enviava, passa a chegar à chamada do LLM; antes era ignorado. Streaming na API foi registrado no `BACKLOG.md` (B-69).
- fix(cli): texto dinâmico impresso pela CLI (nomes, mensagens, memórias, erros, caminhos) deixa de ser interpretado como markup do Rich (C-1). Uma memória com `[/tmp]` ou uma mensagem com `[/INST]` derrubava `memory search`, `export session`, `reflect`, `session close` e `list` com `MarkupError`; texto como `[bold]` sumia ou mudava a formatação. O `learn` volta a mostrar o sufixo `[tipo, importance=...]`, que o Rich engolia como tag.
- fix(cli)!: entradas inválidas terminam com uma linha `Error: ...` e código 1, sem traceback (C-2, C-3). Isso vale para `--persona-json` inválido, `--importance 5`, `--type` desconhecido, `skills list --root` inexistente e qualquer `ValueError`, erro de validação, `IntegrityError` ou `FileNotFoundError` vindo de um comando. `kernel.start_session()` com um symbiote inexistente levanta `EntityNotFoundError` em vez de `sqlite3.IntegrityError` (FOREIGN KEY). O servidor padrão dos comandos remotos (`init`, `discover` e afins) passa a ser `http://localhost:8008`, a porta documentada; antes era 8000.
  - **MIGRAÇÃO:** quem roda o servidor local em 8000 precisa passar `--server` ou gravá-lo com `symbiote init`.
- fix(dream): o `GeneralizePhase` voltou a generalizar mais de um cluster por ciclo. A guarda da TK-46 contra generalizações repetidas sobrescrevia a lista das existentes com o texto do LLM, e a partir do segundo cluster a fase falhava (`str` não tem `tags`) e ficava registrada como erro no relatório do dream. Achado pelo mypy na TK-83.
- fix(storage): migrações numeradas com `PRAGMA user_version` (Q-5). O `init_schema` aplica em ordem as migrações ainda não aplicadas: colunas, tabelas posteriores, índice único de `external_key`, consolidação das configs e índices quentes. Todas continuam idempotentes, então bancos anteriores ao versionamento (`user_version` 0) rodam todas. Bancos criados pelas primeiras versões, sem `sessions.external_key`, voltam a subir; antes o índice criado no script base quebrava o boot com `no such column`. Conferido contra as 24 versões históricas do schema no `main`.
- fix(security): a tool HTTP conecta no IP que a checagem de SSRF aprovou, em vez de resolver o nome de novo na conexão. Isso fecha o DNS rebinding: uma resposta de DNS que muda entre a validação e a conexão não alcança mais o loopback nem a rede interna (S-11). `Host`, SNI e a verificação do certificado continuam usando o hostname. Cada redirect também é validado e fixado. Com proxy configurado no ambiente, o proxy resolve o nome e o caminho anterior se mantém. Novo `security.network.validated_addresses(url)`. Continua sendo `urllib`, sem dependência nova.

### Outros

- test(support): helpers compartilhados em `tests/support` (`ScriptedLLM`, `make_kernel`, `add_tool`, `tool_call_block`) e fixture `api_client` com autenticação real (sem `SYMBIOTE_DEV_MODE`), base dos testes de regressão do relatório de melhorias (Q-2).
- docs: README, QUICKSTART, HOST_INTEGRATION e SPEC corrigidos contra o código (D). `tool_gateway.register(...)`, que não existe, virou `register_http_tool` / `register_descriptor`. A autorização passa a usar `environment.add_tools`. O import do MCP passa a ser `from forge_llm.mcp import McpToolset`. O `curl` do README usa a porta 8008 e o header `Authorization`. A lista de builtins está completa, e o QUICKSTART descreve as tools web que `register()` cria de fato e explica que os modos `index`/`on_demand` liberam as próprias meta-tools. `symbiote show` recebe `SESSION_ID QUERY`. O título do QUICKSTART não fixa mais versão, e o SPEC sai de 0.2.27. O HOST_INTEGRATION ganha a seção "Upgrading" com todas as mudanças incompatíveis desta rodada.
- test(docs): `tests/unit/test_docs_references.py` confere que os blocos Python do README, do QUICKSTART e do HOST_INTEGRATION só usam imports de `symbiote`/`forge_llm` que existem e métodos reais de `kernel`, `kernel.tool_gateway` e `kernel.environment`, sem executar os blocos (D). Contra os docs anteriores, o teste aponta o `tool_gateway.register` e o import do `McpToolset`.
- docs(infra): o `INFRA.md` registra o ambiente local real: execução manual com `uvicorn symbiote.api.http:app --host 127.0.0.1 --port 8008`, sem unit systemd (antes citava `symbios.service` na porta 8003). O `setup_env.sh`, que era um template de outro projeto (instalava forgebase e forgellmclient), passa a criar o `.venv` com `uv` e instalar `-e ".[dev,llm]"`.
- chore(repo): `symbiote.db`, `.coverage` e `.context/` saem do controle de versão (já constavam no `.gitignore`, exceto `.coverage`, que foi acrescentado). Os arquivos continuam no disco (Q-4, D-15). O container rodando como root ficou registrado no `BACKLOG.md` (B-70, D-16).
  - **MIGRAÇÃO:** num clone que faça pull desta mudança, o git apaga do disco esses três caminhos, por deixarem de ser versionados. Faça backup antes, se algum deles tiver conteúdo local que importe.
- chore(lint): `ruff.toml` passa a ser a única configuração do ruff, com `target-version = "py312"` e sem a regra de `src/forgebase/**`, que vinha de outro projeto. O bloco `[tool.ruff]` do `pyproject.toml`, que o ruff ignorava por causa do `ruff.toml`, foi removido (Q-1, D-14). Nada novo foi apontado.
- ci: o estágio `quality` do `Dockerfile` roda `pytest --cov=src/symbiote --cov-fail-under=85`, e o build falha abaixo de 85% de cobertura; antes o `fail_under` do `pyproject.toml` nunca era aplicado (Q-1). Localmente o mesmo comando passa com 89,99%.
- ci(types): o mypy entra como gate no estágio `quality` do `Dockerfile`, por enquanto sem `strict` (Q-1, D-18). Os 75 erros que ele apontava foram corrigidos. Entre as correções: o protocolo `BrowserProvider`/`BrowserSession` passa a ser assíncrono, como as implementações já eram; `MessagePort.get_messages` ganha o `since` que o repositório já aceitava; e `RegisterToolRequest.http_method` vira `Literal`, então um método inválido responde 422. Bibliotecas sem stubs (`yaml`, `forge_llm`, `playwright`, `tomli`) ficam em `ignore_missing_imports`. O `strict` (416 erros, a maioria `type-arg`) ficou registrado no `BACKLOG.md` (B-71).
- chore(backlog): a unificação dos loops sync/async do `ChatRunner` ficou registrada como B-72, com a proposta de desenho e o teste de paridade como rede de segurança. Não coube nesta rodada (Q-3).

## [v0.7.3] - 2026-07-28

- fix(deps): fixa ForgeScraper `v0.12.4` pelo commit imutável `632e4ce`, eliminando a dependência flutuante do extra `[extract]`.
- test(release): adiciona contratos para manter a versão esperada e todas as dependências VCS fixadas por SHA completo, preservando ForgeLLM `v0.7.16` em `af51be9`.

## [v0.7.2] - 2026-07-28

- feat(agent-protocols): adiciona camada versionada de protocolos por modelo, com `legacy_text_v1` e `poolside_laguna_v1`, validação estrita de configuração e integração ao runner sem quebrar o contrato legado.
- feat(llm): preserva o histórico canônico de reasoning e tool calls entre iterações, inclusive na ponte ForgeLLM, evitando perda de contexto em loops agênticos.
- ci(deps): fixa ForgeLLM `v0.7.16` pelo commit imutável `af51be9`, adiciona locks Linux/Python 3.12 e executa testes e lint no mesmo base image fixado por digest.
- ops(staging): deploy do commit exato com porta apenas em loopback, runtime sem bind mount/reload, health e provenance obrigatórios, backup online consistente do SQLite e rollback automático de imagem e código.

## [v0.7.1] - 2026-06-23

- fix(api): blindagem multi-tenant do escopo de skills no entrypoint HTTP. O `_resolve_config()` da API agora lê de ENV: `SYMBIOTE_SKILL_SCOPE` (`global` | `per_symbiote`), `SYMBIOTE_SKILLS_ROOT` e `SYMBIOTE_SKILLS_PROTECTED_ROOTS` (lista por `,` ou `os.pathsep`), e plumba para o `KernelConfig`. **A API agora assume `per_symbiote` por DEFAULT** (o `KernelConfig` em si segue `global` — CLI/embarcados intactos), porque a API é multi-tenant (um kernel servindo vários tenants via `/symbiotes/{id}`) e o pool global vazaria skills entre contas. Quando o scope não vem por ENV, a API loga um WARNING no boot explicando o default e como reverter.
  - **MIGRAÇÃO:** deploys de API que dependiam de um pool GLOBAL de skills passam a não enxergá-las sob o novo default (modo M3 — arquivos antigos ficam no disco, só não são lidos). Set `SYMBIOTE_SKILL_SCOPE=global` para manter o comportamento anterior. Ver `docs/ROLLOUT.md` (seção "Escopo de skills no deploy da API").
  - Não altera comportamento de CLI nem de hosts embarcados (ChopChop) — só o entrypoint da API HTTP.

## [v0.7.0] - 2026-06-23

- feat(skills): **isolamento de skills por symbiote** (Opção A) — novo `KernelConfig.skill_scope = "global" | "per_symbiote"` (default `"global"` = comportamento atual, retrocompatível; CLI e hosts single-symbiote não mudam). Em `"per_symbiote"`, cada symbiote tem seu root read-write próprio em `{skills_root}/<symbiote_id>/skills/...` MAIS um catálogo "de fábrica" read-only compartilhado (via `skills_protected_roots`, pode começar vazio). Nenhum symbiote vê/edita/usa skill de outro — isolamento **físico** (não por filtragem). Resolve o vazamento cross-tenant no modelo 1-kernel-N-symbiotes (ChopChop/Vaigo).
  - **Migração:** modo escopado NÃO lê nem migra o pool global antigo (`{skills_root}/skills/...`) — começa limpo por usuário (decisão M3); arquivos antigos ficam intactos no disco, só não são usados.
  - **Wiring:** novo `SkillScopeManager` (factory + cache LRU de `(SkillsLoader, SkillsStore)` por symbiote, lazy). `ContextAssembler` resolve o loader do symbiote do turno (injeção escopada). `skill_manage`/`skill_view` resolvem store/loader do symbiote ATIVO via ContextVar `active_symbiote` (setado pelo kernel em `message`/`message_async`; o path paralelo da gateway já faz `copy_context()`). `BackgroundReviewEngine` e o GC do Dream passam a ser por-symbiote. Colisão de nomes entre usuários é permitida (cada um no seu diretório).
  - **API nova:** `kernel.skills_loader_for(symbiote_id)` / `kernel.skills_store_for(symbiote_id)` (para a aba de Skills por usuário). Em modo escopado, as properties single `kernel.skills_loader`/`skills_store` retornam `None` (use os `_for`); em modo global, `_for` retorna a instância única (id ignorado), então o host pode usar um único call path. Também: `skills.tool.register_resolved`/`register_view_resolved`/`make_handler(store_provider=)`/`make_view_handler(loader_provider=)`, `skills.scope.{active_symbiote,set/get/reset_active_symbiote,SkillScopeManager}`, `ContextAssembler.set_skills_loader_resolver`. Coluna nova nenhuma (é config de kernel, não per-symbiote no DB). Nada na API existente quebra.
  - **Versão minor (0.7.0)** pela natureza estrutural (novo eixo de escopo + nova topologia de diretórios + properties single passam a `None` em modo escopado), apesar de ser opt-in.

## [v0.6.16] - 2026-06-22

- feat(skills): **progressive disclosure** na injeção de skills (S4) — novo modo `skill_injection_mode = "full" | "index"` no `environment.configure` (default `"full"` = comportamento da 0.6.15, retrocompatível). Espelha o vocabulário de `tool_loading=full|index`. Em `"index"`, o `<available-skills>` (que já era só `nome: descrição` por linha — corpos NUNCA foram injetados) ganha uma instrução explícita pra carregar o corpo sob demanda. Nova tool **`skill_view(name)`** (read-only, `risk_level="low"`, tags `["skills"]`) devolve o corpo do SKILL.md por nome, **só para skills ativas** (quarentena/arquivada são recusadas), e bumpa telemetria de uso como um recall real. Registrada mas não auto-autorizada — host liga com `tools=[..., "skill_view"]`. `AssembledContext.injected_skills` reflete o índice (nomes + tokens por linha).
- feat(skills): **critérios de review configuráveis** (S5) — o prompt default do evolver ganhou a anti-pattern de "não re-documentar capacidade que a persona JÁ tem" (resolve o caso "qual meu nome?" → aprendeu skill de recomendação). Dois novos campos per-symbiote em `environment.configure`: `skill_review_strict: bool` (prepende bloco STRICT MODE — barra alta, nomes class-level, PATCH>CREATE, preferir `[]`) e `skill_review_extra_criteria: str | None` (regras de domínio do host, anexadas verbatim). Ambos no-op por default. `build_skill_review_prompt(strict=, extra_criteria=)` compõe; `BackgroundReviewEngine` resolve os critérios por-symbiote a cada run via `EnvironmentManager.get_skill_review_criteria()`.
- **API nova**: `EnvironmentManager.get_skill_injection_mode()` / `get_skill_review_criteria()`; `skills.tool.register_view()` / `make_view_handler()` / `SKILL_VIEW_TOOL_ID`; `_review_prompts.build_skill_review_prompt()`. Colunas idempotentes `environment_configs.skill_injection_mode` / `skill_review_strict` / `skill_review_extra_criteria` (default = comportamento atual). Nada na API existente quebra.

## [v0.6.15] - 2026-06-22

- feat(skills): injeção nativa de skills aprendidas no system prompt do `ChatRunner` — **opt-in, retrocompatível**. Antes o kernel aprendia skills mas o chat embarcado nunca as usava: `_build_system` não tinha `<available-skills>`, o `ChatRunner` era criado sem `SkillsLoader`, o `AssembledContext` não tinha campo de skills e `build_summary()` nunca era chamado no caminho de mensagem. Agora, quando ligado, as skills **ativas** (status=`active`; quarantine/archived nunca entram) viram um bloco `<available-skills>` no system prompt.
  - **Opt-in por-symbiote** via `EnvironmentConfig.skill_injection_enabled` (default `False`) — consistente com `skill_review_enabled`/`context_mode`/etc. Ligar num symbiote não afeta outro. Liga-se com `kernel.environment.configure(symbiote_id, skill_injection_enabled=True)`. Nova coluna `environment_configs.skill_injection_enabled` (migração idempotente `ADD COLUMN ... DEFAULT 0`; linhas pré-migração lêem `False`).
  - **Observabilidade pro host** ("custo de usar"): `AssembledContext.skills_summary: str | None` (o bloco renderizado) e `AssembledContext.injected_skills: list[InjectedSkill]` com `name`/`description`/`tokens` (estimativa chars//4 por skill). Vazios quando desligado.
  - **Budget-aware**: as skills são adicionadas enquanto cabem em `context_budget`; o que não cabe é descartado, e `total_tokens_estimate` inclui o bloco. Summary renderizado e `injected_skills` nunca divergem.
  - **API pública nova**: `kernel.skills_loader` / `kernel.skills_store` (properties, antes só `_skills_loader`/`_skills_store`), `EnvironmentManager.get_skill_injection_enabled(...)`, `ContextAssembler(skills_loader=...)` + `ContextAssembler.set_skills_loader(...)`. Nada na API existente quebra; default preserva 100% o comportamento anterior.

## [v0.6.14] - 2026-06-11

- feat(chat): teto de truncamento de tool result (Layer 1 microcompaction) agora é configurável — `KernelConfig.tool_result_max_chars` (default 2000, comportamento inalterado) plumba até `ChatRunner(tool_result_max_chars=...)`. Antes era constante de módulo: payloads JSON maiores (ex.: árvore de tarefas completa) eram cortados em 2000 chars e o LLM tratava o rabo perdido como inexistente — respondia "não encontrei" para dados que a tool DEVOLVEU (bug observado no ChopChop/Vaigo). `_microcompact_tool_result`/`_format_tool_results` viraram métodos de instância.

## [v0.6.13] - 2026-06-09

- fix(runtime): `build_runtime_block` agora carimba a mensagem com a hora **local** do host + offset UTC explícito (ex.: `2026-06-09 14:50:28 -03:00`) em vez de `datetime.now(tz=UTC)` rotulado `UTC`. O default antigo fazia o LLM tratar o relógio UTC como hora de parede local e julgar errado se um horário já tinha passado (ex.: "hoje às 16h" lido como passado contra 16:57 UTC num host UTC−3). Timestamps tz-aware explícitos são renderizados no próprio fuso; naive vira local. Sem mudança no formato/delimitadores do bloco — `strip_runtime_context` intacto.

## [v0.6.12] - 2026-06-05

- feat(kernel): `SymbioteKernel.set_native_tools(bool)` — setter suportado para ligar native function-calling no runner interno (antes só dava pra setar `runner._native_tools` na marra). Levanta `RuntimeError` se o kernel não tem LLM.
- docs(host): seção proeminente **"Conversation Memory (multi-turn)"** no HOST_INTEGRATION.md — deixa explícito que o kernel PERSISTE mensagens mas NÃO injeta o histórico no prompt sozinho; chat embarcado precisa passar `extra_context["conversation_history"]` a cada turno (erro de integração nº1). Documenta o formato exato do parser (rótulos `usuário/user`, `assistant/clark`; `get_messages` vem DESC, reverter pra cronológico) e o falso-positivo de "mensagens acumulam no DB = memória".
- docs(host): checklist **"Common embedded-host pitfalls"** no topo do HOST_INTEGRATION.md — memória de conversa, gate de aprovação off-by-default, visibilidade de tools por TAG (não pela lista autorizada), e native_tools off-by-default. Todos silenciosos no happy-path.

## [v0.6.11] - 2026-06-05

- fix(security): `SymbioteKernel.set_approval_callback(cb)` — caminho suportado para plugar o gate de aprovação no runner interno usado por `message()`/`message_async()`. Antes, hosts embarcados não tinham como ativar o gate (o `ChatRunner` interno era construído sem `on_before_tool_call`), então o controle de risco ficava **silenciosamente desligado** em modo embarcado. O gate dispara só para `risk_level == "high"`.
- fix(env): handlers de tool e `header_factory` agora enxergam os `contextvars` do chamador. `PolicyGate.execute_with_policy` (single-call) e `ToolGateway.execute_tool_calls` (paralelo) despacham via `contextvars.copy_context()`, então estado por-requisição do host (ex.: usuário atual/auth) sobrevive nas threads do pool. Antes, só o caminho async propagava.
- docs: HOST_INTEGRATION.md corrigido — a seção de aprovação não induz mais a achar que `ChatRunner(on_before_tool_call=...)` avulso afeta `message()`; nova seção sobre identidade em handlers multiusuário via contextvar.

## [v0.6.10] - 2026-06-03

- feat(discovery): `risk_level` é cidadão de primeira classe no discovery via OpenAPI. `DiscoveredTool` ganha campo `risk_level` (`low|medium|high`, default `medium`); `DiscoveryService` lê a extensão `x-risk-level` por operação e, na ausência, aplica heurística por método HTTP (GET/HEAD→low, POST/PUT/PATCH→medium, DELETE→high); `DiscoveredToolLoader` propaga o valor ao `ToolDescriptor(risk_level=...)`. Host não precisa mais manter mapa de risco paralelo.
- feat(discovery): coluna `risk_level` em `discovered_tools` (migração idempotente) e campo `risk_level` exposto em `DiscoveredToolResponse` (HTTP API).
- docs: convenção `x-risk-level` documentada no HOST_INTEGRATION.md; CLARK_MIGRATION_v0.3.md atualizado para preferir anotação no host em vez de override pós-load.

## [v0.6.9] - 2026-06-02

- fix: `symbiote.__version__` deriva de `importlib.metadata.version("symbiote")` em vez de hardcode (estava defasado em `0.6.3`); nunca mais desatualiza a cada release

## [v0.6.8] - 2026-06-01

- feat(spawn): `SubagentManager.spawn` aceita `extra_context` (dict|None) e o repassa a `kernel.message`, renderizado no bloco `## Context` da sub-sessão delegada
- canal de injeção do host (fora do `SPAWN_DESCRIPTOR.parameters` — a LLM não autora); retrocompatível (omitir = `None`); validação de tipo com erro estruturado
- docs: seção sobre injeção de contexto em sub-sessões no HOST_INTEGRATION.md

## [v0.6.7] - 2026-06-01

- feat(console): aba **Memory** — inspeção de memória, reflexões (fatos aprendidos) e atividade (tools executadas) por símbiota
- feat(console): aba **Harness** — ver histórico de versões dos textos auto-evoluídos, editar (nova versão ativa) e rollback, com `avg_score`/contagem de sessões
- feat(api): endpoints de leitura `/api/symbiotes/{id}/memory|reflections|activity` (sem auth) e de harness `GET/POST /symbiotes/{id}/harness[/{component}][/rollback]` (mutação via `require_admin`)
- docs: QUICKSTART cita as novas abas do Console

## [v0.6.6] - 2026-06-01

- feat(console): `SYMBIOTE_DB_PATH` permite o server abrir o SQLite de qualquer instância (inclusive embarcada) sem depender do cwd
- feat(console): `SYMBIOTE_LOCAL_ADMIN=1` auto-provisiona key admin e injeta no Console, destravando a edição contra bancos sem API keys (resolve o chicken-and-egg do `require_admin`)
- feat(console): editor e wizard com campos estruturados (system prompt + tone/language + JSON avançado) no lugar de JSON cru
- fix(console): fim do erro silencioso no editor — falha de carregamento aparece no modal em vez de deixar campos vazios
- feat(persona): `_render_persona` renderiza `description`/`system_prompt`/`instructions`/`behavior` como prosa (lossless)
- docs: `SYMBIOTE_DB_PATH` e `SYMBIOTE_LOCAL_ADMIN` no QUICKSTART; seção de inspeção de Symbiote embarcado no HOST_INTEGRATION

## [v0.6.5] - 2026-05-29

### Novas funcionalidades

- **Teto de `max_tool_iterations` controlável pelo host**: o valor por-symbiota já era
  configurável; agora o teto também. Novo `KernelConfig.max_tool_iterations_ceiling`
  (default `50`, faixa 1–10000, carregável via YAML) deixa o app embutido decidir o limite.
  `EnvironmentManager.configure(max_tool_iterations=N)` valida contra esse teto e **falha
  alto** (`ValueError` claro) quando excede — antes era um `Field(le=50)` estático que
  levantava `ValidationError` (e era engolido por wrappers de host, deixando o DB no valor
  antigo). O `le=` do modelo `EnvironmentConfig` virou apenas backstop absoluto (10000);
  `loop_timeout` (≤3600s) segue como co-guard de wall-clock independente.

  *Compat*: clientes que não setam o ceiling têm comportamento idêntico (teto 50). Único
  impacto observável: quem capturava `pydantic.ValidationError` ao exceder 50 agora recebe
  `ValueError`.

### Correções

- **storage**: `SQLiteAdapter.close()` agora adquire o lock do adapter. Completa o fix de
  thread safety do v0.6.4 (B-47): com `check_same_thread=False`, fechar a conexão enquanto
  uma thread de background (consolidation/review/dream) está em `execute` é use-after-free
  nativo (segfault, não capturável em Python). Com o lock, `close()` espera o statement em
  voo terminar; um statement tardio passa a levantar `ProgrammingError` capturável.
  Reproduzível na suíte unit completa após o B-47 expor o race; 4 runs full verdes pós-fix.

## [v0.6.4] - 2026-05-29

### Correções

- **storage**: `SQLiteAdapter` agora serializa `execute`/`fetch_one`/`fetch_all` com um `threading.Lock` por adapter. Elimina `sqlite3.InterfaceError: bad parameter or other API misuse` quando background threads (consolidation, skill/reflection review, dream) escreviam enquanto a main thread lia. `check_same_thread` default `True`→`False` (necessário para a conexão única ser usada por outras threads sob o lock; produção já passava `False` explicitamente, zero mudança de comportamento). Novo teste de stress concorrente (12 threads × 25 insert+select). (B-47)

### Testes

- **chat runner**: atualizadas 4 assertions defasadas em `test_instant_mode` e `test_generation_settings` que mockavam a assinatura de `_run_instant`/`_run_loop` anterior ao kwarg `llm_config` (v0.5.0). Drift de teste, não regressão funcional. (B-46)

## [v0.6.3] - 2026-05-27

### Correções

- **fix(runners): `LoopController` detecta ping-pong (A,B,A,B,A) — corta loops alternados que escapavam do check consecutivo** — observado em sym_talk_lt 2026-05-27 ("qual a versão estável do Python?"): LLM alternou `web_search "Python latest"` ↔ `web_extract python.org/downloads` por **6 iterações cada** (12 tool calls, 17 totais), batendo o dedup cache em cada chamada mas nunca DUAS consecutivas idênticas — então o stagnation check anterior (`A,A`) jamais disparava. ### Fix em `should_stop()`: novo branch ping-pong com janela deslizante de 5 calls. Se `len(distinct (tool_id, params)) <= 2` na janela → reusa `stop_reason="stagnation"` (e a `injection_message` correspondente — kernel feedback scoring + host recovery prompts já tratam idêntico). Constantes `_PING_PONG_WINDOW=5` e `_PING_PONG_MAX_UNIQUE=2` documentadas in-line. Preserva o teste pré-existente `test_non_consecutive_duplicates_no_stagnation` (3 calls com 2 keys continuam OK — só dispara na 5ª).
- **fix(runners): `ChatRunner` drop `tools` do kwargs na injection final pós-stagnation** — observado em sym_talk_lt 2026-05-27 11:49 ("O Corinthians joga hoje?"): controller fired stagnation, injection msg "Respond to the user" enviada, mas Sonnet em sessão pt-BR ignorou e emitiu **mais um tool_call** em vez de texto (porque `kwargs["tools"]` ainda estava populado com 62 ferramentas). `final_text=""` → host caiu no fallback determinístico ("Empty response after N tool calls"). ### Fix em `_run_loop`/`_run_async_loop`: copiar `kwargs` e dropar `"tools"` antes do `_call_llm_sync` do injection. Sem tools no schema, o LLM é forçado a text-respond. Cobre `ForgeAdapter` e `SymTalkAgentAdapter` (ambos tratam ausência de `tools` como "sem ferramentas disponíveis"). Comentário in-line referencia o incidente como pin de regressão.

### Testes

- 3 novos em `test_loop_control.py::TestDuplicateDetection`: `test_ping_pong_fires_at_window_size` (A,B,A,B,A → stagnation), `test_ping_pong_three_unique_no_stop` (A,B,C,A,B → 3 unique → continua), `test_ping_pong_below_window_no_stop` (4 calls A,B,A,B → ainda abaixo da janela).
- 1 novo em `test_loop_control.py::TestChatRunnerIntegration`: `test_injection_call_omits_tools_kwarg` — `_CaptureLLM` mock grava `tools` de cada call; pin que a última chamada (injection, identificável pela mensagem "repeating the same action") tem `tools_present is False`. Bloqueia regressão silenciosa.
- Suite full: 1530 passed, 4 falhas pré-existentes em `test_generation_settings.py` / `test_instant_mode.py` (mesmas que já estavam na v0.6.2 da main, zero relacionadas com este release).

## [v0.6.2] - 2026-05-27

> **Escopo agregado**: este release consolida os PRs #1 (Sprints 1-4 + 4.1 + 4.2) e #2 (Sprint 5 + 5.1) — o **loop completo de self-improvement** (LLM reflection, skill autonomy, lifecycle automation, audit). Defaults preservados: `reflection_mode='keyword'` e `skill_review_enabled=False` mantêm comportamento idêntico ao v0.6.1; clientes embedados (`you_news`, `sym_talk_lt`) atualizam sem mudar código.
>
> Bumpado como **patch** apesar do escopo MINOR — decisão do mantenedor priorizando estabilidade do canal de versionamento. CHANGELOG abaixo documenta o escopo real para auditoria.

### Aprendizado — Sprint 5.1 (hardening do review)

Quatro fixes do review do PR #2. Defaults preservados; nenhum impacto observável quando `skill_review_enabled=False`.

- **H5.1 — Lock em `usage.mark_used`**: read-modify-write do sidecar `.skill_meta.json` agora protegido por `threading.Lock` per-skill_dir (cache global `_path_locks` indexado por `str(path.resolve())`). Elimina race que perdia incrementos de `use_count` em loads concorrentes — o auto-promote do Sprint 5 dependia desse contador estar correto. Stress test pin: 10 threads × 10 increments → `use_count == 100` exato.
- **H5.2 — Helper `_int_or_default` em `_row_to_config`**: extraído como `@staticmethod`. Os 2 novos fields do Sprint 5 (`skill_auto_promote_threshold`, `skill_quarantine_timeout_days`) usam o helper porque `0` é sentinela válido ("desativado"). Fields antigos (`max_*`, `dream_*`, etc.) ficam com o padrão `int(row.get(k, d) or d)` porque têm `Field(ge=1)` no Pydantic e `0` não é válido pra eles — comment no código aponta a regra de migração se algum desses ganhar `0` semântico no futuro.
- **H5.3 — 4 testes de round-trip** em `test_environment.py::TestSkillLifecycleRoundTrip`: round-trip via INSERT, `0` preservado (não coercido pra default), round-trip via UPDATE, e omitir fields num update preserva valores existentes.
- **H5.4 — Doc cleanup `_setup_skills_wiring`**: corrigida (era `skills/agent`, agora `skills/`); adicionada nota sobre wiring all-or-nothing (Sprint 4+ features dependem de `_skills_loader` não-None; guards garantem no-op quando wiring desabilitado).

### Aprendizado — Sprint 5 (lifecycle automation + audit)

Três pendências do PR #1 entregues: as feature loops do plano original que dependiam só de telemetria (e não de "esperar volume real"). Defaults conservadores — auto-promote ativo (threshold 3) e auto-archive de quarantine ativo (14 dias), mas a feature inteira só roda quando `skill_review_enabled=True` (default `False`).

- **S5.1 — Auto-promote `quarantine → active` após N usos** — `EnvironmentConfig.skill_auto_promote_threshold` (default `3`, `0` desativa). `SkillsLoader.__init__` aceita `auto_promote_threshold` (default `0`); kernel sincroniza com `cfg.skill_auto_promote_threshold` via `set_auto_promote_threshold` em cada `_background_review_for`. `usage.mark_used` ganha kwarg `auto_promote_threshold`; quando `agent_created=true` AND `status=quarantine` AND `use_count >= threshold`, flipa pra `active` e retorna `True` (loader atualiza `Skill.status` em memória). Skills humanas (`agent_created=false`) e ativas nunca são tocadas. 4 testes.
- **S5.2 — Auto-archive quarantine antiga no `DreamEngine.PrunePhase`** — `EnvironmentConfig.skill_quarantine_timeout_days` (default `14`, `0` desativa). Quarantine sem `last_used_at` (definição) → idade medida via `created_at`. Quando `days > timeout` AND não pinada AND `agent_created=true` → status flipa pra `archived` (sumindo do loader). Counterpart do `max_quarantine_skills` cap: cap protege contra explosão de novas, archive libera espaço. `PrunePhase.__init__` aceita `quarantine_timeout_days`; `DreamEngine` propaga via `skill_quarantine_timeout_days`; kernel propaga em ambos os caminhos (`_get_or_create_dream_engine` e `kernel.dream`). 4 testes (timeout normal, recent untouched, pinned protegida, `0` desativa).
- **S5.3 — Tabela `skill_review_audit` + CLI** — schema: `id, session_id, symbiote_id, trigger, applied, skipped, ok, error, ops_json, created_at`. Migration idempotente. `BackgroundReviewEngine.__init__` aceita `storage` opcional; `spawn`/`spawn_final`/`run_sync` ganham param `trigger` (`nudge|final|sync`); cada `_run` escreve uma linha no `finally` (best-effort, log on failure). Kernel passa `storage=self._storage` ao construir o engine. CLI `symbiote audit skill-review --days N --symbiote ID --session ID --trigger T --limit N --ops` espelha o `audit reflection`. 4 testes (no-op run, applied ops, LLM failure capturada, no-audit sem storage).

### Aprendizado — Sprint 1 (LLM Reflection + Consolidator refactor)

Primeira fase do plano de self-improvement (referência: `docs/RESEARCH-hermes-self-improvement.md`, processo de discussão em conversa). Defaults preservam comportamento atual: clientes embedados (`you_news`, `sym_talk_lt`) não veem diferença até trocar a flag explicitamente.

- **Novo `core/_review_prompts.py`** — `REFLECTION_PROMPT` engineered com blocos modulares (`_SIGNALS_BLOCK`, `_ANTI_PATTERNS_BLOCK`, `_HIERARCHY_BLOCK`, `_OUTPUT_SCHEMA`). Adaptado dos prompts do Hermes (`~/dev/research/hermes-agent/agent/background_review.py`) com schema de saída próprio (`action: create|patch`, `target_id`, `tags`, `reasoning`). Reusável pelo futuro Background Skill Review (Item 1 do plano).
- **`ReflectionEngine` ganha modos** — `keyword` (default, retrocompatível) | `llm` (via `_evolver_llm`) | `llm_main` (via main `_llm`, opt-in custo) | `hybrid` (roda os dois, persiste keyword, loga diff). Path LLM tem defense-in-depth (`_BLOCK_PATTERNS`): `constraint` com phrases tipo "command not found" / "broken tool" / "rm -rf" é descartado; importance > 0.9 fora de `constraint` é capped em 0.7. Fallback gracioso para keyword em qualquer falha LLM (parse error, timeout, JSON inválido).
- **`MemoryConsolidator` refatorado para single-responsibility** — agora produz UM `MemoryEntry(type=session_summary)` por overflow, não N facts tipados. Elimina a redundância com Reflection (ambos extraíam o mesmo via LLM). Compressão narrativa (≤300 palavras), truncada em `_MAX_SUMMARY_CHARS=2400`. Fallback naive em LLM failure ainda persiste 1 entry. Prompt anterior (`_CONSOLIDATION_PROMPT`) substituído por `_COMPRESSION_PROMPT`.
- **`EnvironmentConfig` ganha `reflection_mode` e `reflection_max_tokens`** — colunas novas em `environment_configs` via migration idempotente (`ALTER TABLE ... ADD COLUMN`). Defaults `"keyword"` e `4000`.
- **Guard de custo no `EnvironmentManager.configure`** — `reflection_mode in {"llm","hybrid"}` exige `kernel.set_evolver_llm(...)` setado, senão `ValueError` na configuração (não silencioso). `"llm_main"` é variante opt-in explícita ao modelo principal. Evita consumo surpresa do LLM caro em produção.
- **Tabela `reflection_audit`** — id, session_id, symbiote_id, mode, keyword_facts_json, llm_facts_json, llm_error, created_at. Permite auditar diferença keyword↔LLM antes de promover modo `"llm"` como default.
- **CLI `symbiote audit reflection`** — dump dos últimos N dias (default 7), filtros `--symbiote / --session`, `--diff` mostra payload comparado.
- **Tests** — `test_reflection_llm.py` (10 cenários: anti-patterns, positives, fallback, hybrid+audit), `test_backward_compat_reflection.py` (kernel sem flags = 0 LLM calls extras em `close_session`; `ValueError` em mode `llm/hybrid` sem evolver), `test_memory_consolidator.py` atualizado para o novo contrato (1 summary, não N facts).

### Aprendizado — Sprint 2 (PATCH first-class)

- **`MemoryPort.update(memory_id, *, content, importance, tags)`** — novo método no Protocol. Contrato: retorna `bool` (True se atualizou, False se id não existe ou já está inativo). Nunca levanta — caminho de PATCH em reflection cai gracioso para CREATE quando target_id é inválido. Adiciona `updated_at` em `MemoryEntry` (distinto de `last_used_at`: PATCH é refinamento, não recall, e não reseta decay).
- **Migration `memory_entries.updated_at`** — coluna nullable idempotente.
- **`ReflectionEngine._apply_llm_facts` agora honra PATCH** — resolve `target_id` (UUID completo ou prefixo de 8 chars vindo do `existing_memories`), chama `update()`, persiste no lugar. Fallback para CREATE quando: target_id inválido, memória inativa, `update()` retorna False, ou adapter sem `update`. Nenhum caminho perde a lição.
- **`_format_existing_memories` expandido** — inclui `preference`, `constraint`, `procedural`, `decision`, `factual` (antes só preference+constraint). Ordena por importance desc + last_used_at desc. Renderiza IDs encurtados (8 chars) pra economia de tokens — `_resolve_target_id` aceita ambos no input do LLM.
- **Tests** — 6 cenários de `MemoryStore.update` (content-only, all fields, nonexistent, inactive, no-op, preserva last_used_at), 5 cenários de PATCH path em reflection (atualiza no lugar sem duplicar, prefixo curto, target inválido → CREATE, sem target_id → CREATE, target inativo → CREATE), 1 cenário de `existing_memories` cobrindo os 5 tipos.

### Aprendizado — Sprint 3 (SkillsStore + skill_manage tool, manual)

Item 1 do plano (Hermes-style skill manager). Peças completas para criação/edição autônoma de skills, mas dispara apenas via humano (CLI). Trigger automático em background fica para Sprint 4. Defaults preservam comportamento atual.

- **Novo `core/provenance.py`** — `ContextVar write_origin` com `FOREGROUND` / `BACKGROUND_REVIEW`. Espelha `tools/skill_provenance.py` do Hermes. `is_background_review()` é a query rápida usada por `SkillsStore.create` para decidir se marca a skill como `agent_created`.
- **Novo `skills/usage.py`** — sidecar `{skill_dir}/.skill_meta.json` com `{agent_created, status, pinned, created_at, last_used_at, use_count, patch_count}`. **Regra de backward-compat invariante**: ausência de sidecar = `status="active"`, `agent_created=false` — preserva todas as skills humanas (`process/skills/feature.md` etc.) sem migração. Lifecycle: `quarantine → active → stale → archived`. `pinned` protege de delete e de futura archive do curator.
- **Novo `skills/store.py`** — `SkillsStore` com 6 ações (`create`, `edit`, `patch`, `delete`, `write_file`, `remove_file`). Validações: nome regex `^[a-z0-9][a-z0-9._-]*$`, max 64 chars, max 100k chars conteúdo, max 1 MiB arquivo suporte, allowlist subdirs (`references/templates/scripts/assets`), atomic write via `tempfile + os.replace`, path traversal bloqueado. `protected_roots` (e.g. `process/skills/`) bloqueia edit/patch/delete mas permite read. `delete` refuses pinned. Erros tipados (`SkillValidationError / NotFound / Exists / Protected`).
- **`skills/loader.py` lifecycle-aware** — `build_summary()` agora filtra `status="quarantine"` e `status="archived"` (quarantine sumida do `<available-skills>` até promoção, archived invisível em qualquer query). `listable_skills()` retorna o subset que vai pro LLM; `list_skills()` mantém comportamento antigo (incluindo quarantine pra CLI/inspeção). `get_skill()` bumpa `use_count` na sidecar — telemetria pra futura SkillCuratorPhase. Novo `refresh()` re-discovery após writes do `SkillsStore`. `discover()` agora público (alias de `_discover`).
- **Novo `skills/tool.py`** — wrapper `skill_manage` para `ToolGateway` com schema OpenAI-style. **Não auto-autorizada**: host opta-in via `kernel.environment.configure(tools=[..., 'skill_manage'])`. Erros do `SkillsStore` viram JSON tipado (`kind: validation|not_found|exists|protected|error|internal`) para audit log.
- **CLI `symbiote skills`** — `list` (com `--all` mostra quarantine/archived; tabela com status, autor, use_count, patch_count, pinned), `promote <name>` (quarantine→active), `pin/unpin`. Defaults busca `.symbiote/skills/agent/` + `skills/` se existirem.
- **Tests** — 27 cenários em `test_skills_store.py` (validação, criação foreground/background, edit/patch/write_file/remove_file, delete + protection, atomic write), 4 em `test_provenance.py` (default, set/reset, falsy normalization, thread isolation), 8 em `test_skills_loader_lifecycle.py` (backward-compat sem sidecar, quarantine filter, archived discovery, promote+refresh, use_count telemetry). `test_skills_loader.py` (17 testes existentes) continua verde.

### Aprendizado — Sprint 4 (loop autônomo)

Fecha o circuito do plano de self-improvement: skill review autônomo em background (durante e após sessão) + lifecycle automático no DreamEngine. **Defaults preservam comportamento atual** (`skill_review_enabled=False`).

- **Novo `SKILL_REVIEW_PROMPT`** em `core/_review_prompts.py` — engineered com signals + anti-patterns + hierarquia (PATCH skill carregada > WRITE_FILE em umbrella existente > CREATE classe). Schema JSON com 3 ações suportadas (create/patch/write_file); delete é bloqueado por design. Adaptado de `~/dev/research/hermes-agent/agent/background_review.py:45-148`.
- **Novo `core/background_review.py`** — `BackgroundReviewEngine` com `spawn(session_id, symbiote_id)` (daemon thread non-blocking) e `run_sync` (testes / CLI). Design: **single LLM call** (não tool loop reentrant) + aplicação sequencial via `SkillsStore`. Provenance: todo write roda sob `set_current_write_origin(BACKGROUND_REVIEW)` → skills nascem como `agent_created=true, status=quarantine`. Respeita `max_active_skills` (refuses `create` ao bater cap, mas aceita patch/write_file). Refresh `SkillsLoader` após writes. Erros (LLM raise, JSON inválido, write conflict) viram log; nunca propagam.
- **`EnvironmentConfig`** ganha 3 fields: `skill_review_enabled=False`, `skill_nudge_interval=10`, `max_active_skills=20`. Colunas idempotentes via ALTER TABLE.
- **`EnvironmentManager.configure` guard** — `skill_review_enabled=True` exige `kernel.set_evolver_llm(...)` setado, senão `ValueError` na configuração (mesma defesa que `reflection_mode='llm'` herdou no Sprint 1). Evita Opus rodando N vezes por sessão como surpresa de fatura.
- **`KernelConfig.skills_root` opcional** (+ `skills_extra_roots`, `skills_protected_roots`). Default: `{db_path.parent}/skills/` — layout flat onde sidecar `.skill_meta.json` distingue agent-created de humano. Kernel auto-wire em `__init__`: cria `SkillsStore` + `SkillsLoader` + registra `skill_manage` no `ToolGateway` (não autorizada por default).
- **Trigger em `ChatRunner`** — kwarg-only `background_review: Callable[[str], engine | None] | None` (resolver per-symbiote para um runner singleton servir vários symbiotes). Contador `_iters_since_skill_review` dispara `engine.spawn(session_id, symbiote_id)` a cada `skill_nudge_interval` tool iterations. Falhas swallowed; preserva clientes externos (you_news, sym_talk_lt) que instanciam `ChatRunner` direto sem o kwarg.
- **Trigger em `kernel.close_session`** — após reflection, chama `_background_review_for(symbiote_id)`; se retornar engine, `spawn_final(...)` em daemon (não bloqueia `close_session`). `_background_review_for` retorna `None` quando flag off, sem `_evolver_llm`, ou sem `SkillsStore`.
- **`DreamEngine.PrunePhase` estendido para skills** — novo passo após o memory prune. Para cada skill com `agent_created=true and not pinned`: `active → stale` após 30d sem `last_used_at` growth; `stale → archived` após 90d total. **Skills sem sidecar (humanas) e skills pinadas nunca são tocadas.** `dry_run` propõe sem aplicar. `DreamContext.skills_loader` é opcional — kernels sem skills root configurado continuam funcionando sem mudança.
- **Tests (+15)** — `test_background_review.py` (provenance: quarantine + agent_created=true; loader refresh; max_active_skills cap; LLM raise / JSON inválido / array vazio; delete recusado; patch + write_file). `test_dream_skills.py` (humanas sem sidecar untouched; pinadas untouched mesmo após 365d; active→stale 30d; stale→archived 90d; recente fica active; dry_run propõe sem aplicar).

### Aprendizado — Sprint 4.1 (hardening: thread safety + concurrency caps)

Quatro fixes [Major] identificados no code review do Sprint 4. Endereçam race conditions latentes que só se manifestariam com `skill_review_enabled=True` em produção — defaults continuam preservados.

- **H1 — Lock no `kernel._background_review_for`**: lazy construction do `BackgroundReviewEngine` era unsafe (duas threads podiam ver `None` e construir engines rivais). Hot path preserva fast-return sem lock; cold path serializa via `threading.Lock`. Teste de regressão `TestKernelLazyBuildLock.test_concurrent_calls_build_engine_once` (8 threads via `Barrier`).
- **H2 — Cap de threads concorrentes no `BackgroundReviewEngine`**: `spawn()` deduplica por `session_id`. Em sessão tool-heavy com `skill_nudge_interval=10` e 100 iters: antes spawneava 10 daemon threads concorrentes; agora retorna a thread em flight quando ainda viva. Slot liberado no `finally` do `_run` (com check de identidade — só limpa se a thread atual é a dona). Testes: 3 cenários (dedup mesma sessão, sessões independentes, slot liberado pós-completion).
- **H3 — `SkillsLoader.refresh()` thread-safe via atomic ref swap**: `_skills.clear() + _discover()` deixava reader concorrente cair em `RuntimeError: dictionary changed size during iteration`. Agora `_discover` constrói dict fresh e atribui via single rebind (atômico sob GIL). Helper `_discover_root_into(root, target)` toma o dict alvo como parâmetro. `add_root` também usa o pattern. Teste stress: 3 readers + 1 refresher em loop por 10s, zero erros.
- **H4 — Cap separado para quarantine**: antes `max_active_skills` contava `active + quarantine` no mesmo bucket, podendo bloquear creates quando o backlog de quarantine não promovido enchia. Agora `EnvironmentConfig` tem dois caps independentes: `max_active_skills` (default 20, bound do que vai pro `<available-skills>`) e `max_quarantine_skills` (default 10, bound do backlog de skills criadas pelo background review aguardando promoção). `BackgroundReviewEngine._count_skills_by_status` retorna a tupla; create checa só o quarantine cap. Migration idempotente `ALTER TABLE environment_configs ADD COLUMN max_quarantine_skills`. Teste do deadlock: 3 active skills + cap_active=3 + cap_quarantine=5 → create vai pra quarantine sem bloquear.
- **Limpeza incidental**: `BackgroundReviewEngine._run` perdeu o param `return_result` (sempre retornava o mesmo valor em ambos ramos) — apontado no review como código morto.

### Aprendizado — Sprint 4.2 (minor cleanup do review)

Cinco fixes [Minor] do code review. Cosméticos individualmente, mas eliminam classes de bug e tornam o código mais auditável.

- **M1 — `render_prompt` substitui `str.format()`**: novo helper em `core/_review_prompts.py` que usa `str.replace` em placeholders nomeados. Elimina a armadilha "escape `{` literal duas vezes ou explode com `KeyError` silencioso" — bug que pegou no Sprint 1. Schemas dos 3 prompts (REFLECTION/SKILL/COMPRESSION) limpos de `{{ }}` escapes; callers em `reflection.py`, `consolidator.py`, `background_review.py` migrados. Testes: 4 cenários (literal braces, unknown placeholders, real-prompt renders).
- **M2 — CLI `symbiote skills` ganha `--layout`** `auto | direct | nested`. Helper `_require_skills_roots` substitui erro genérico por mensagem acionável. Antes a auto-detecção falhava silenciosamente em diretórios vazios.
- **M3 — Import lazy movido pra topo em `dream/phases.py`**: `from symbiote.skills import usage` agora top-level (sem ciclo verificado). Alias renomeado de `_skill_usage` para `skill_usage`.
- **M4 — Cap em `_format_existing_skills`**: hard limit `_MAX_EXISTING_SKILLS_LISTED = 30` no listing pro LLM. Active priorizadas antes de quarantine; trailing `"+ N more not shown"` sinaliza truncamento (visível no audit).
- **M5 — `_setup_skills_wiring` re-raise quando `skills_root` foi explícito**: host setou `KernelConfig(skills_root=...)` → falha propaga. Default derivado → log + feature disabled. Stress test do lock (sprint4_hardening) refinado pra isolar do SQLite single-cursor concern.

### Não incluído

- Promoção de `reflection_mode` default para `"llm"` em symbiotes novos — fica para depois de 1-2 semanas de auditoria em modo `"hybrid"` em ambiente real.
- Promoção automática `quarantine → active` (após N usos bem-sucedidos) — hoje promoção é manual via `symbiote skills promote <name>` (CLI Sprint 3).
- `DreamEngine.ReconcilePhase` para skills (detectar overlap de `when_to_use`) — fica para quando houver volume suficiente para validar critério.
- Auditoria do background review (tabela `skill_review_audit` similar ao `reflection_audit`) — pendente; hoje a única observabilidade é o sidecar `.skill_meta.json` e os logs.
- Auto-archive de quarantine antiga (counterpart do `max_quarantine_skills` cap) — fica para um sprint dedicado ao curator.

## [v0.6.1] - 2026-05-27

### Correções

- **fix(browser): `ForgeScraperProvider._normalize()` propaga `content_quality` e `quality_reason` do forge_scraper >=0.11.0** — release anterior (v0.6.0) descartava esses campos no normalize, mesmo após forge_scraper 0.11.0 começar a expô-los. Downstream (sym_talk_lt / Jitto) não conseguia detectar páginas JS-rendered onde `get_content()` retorna só os 102 chars do `<meta description>` (ex.: ge.globo.com agendas, lance.com.br, dashboards) — caía em stagnation do tool loop. Agora os dois campos passam via `getattr(content_info, ..., None)`, defaults a `None` quando o forge_scraper instalado é mais antigo (backward-compat). `ExtractWithFallback` também ganha as chaves no shape final (com `None`) quando todos os providers falham — consistência defensiva para consumidores que sempre fazem `.get("content_quality")`. Bug raiz e ESC documentados em [forge_scraper#tickets/js-rendered-content-empty-extraction.md](https://github.com/symlabs-ai/forge_scraper). 3 testes em `tests/unit/browser/test_forge_scraper_provider.py` cobrem: shape novo, fallback para `None` em forge_scraper <0.11.0, e novo `test_extract_propagates_quality_signal` validando o caso "low + likely_js_rendered".

## [v0.6.0] - 2026-05-26

> **Release report user-facing:** [`docs/releases/v0.6.0.md`](docs/releases/v0.6.0.md) — quickstart, exemplos, cost cheat-sheet, limitações conhecidas, guia de migração.

### Novas funcionalidades

- **Subpackage `symbiote.browser`** — websearch, extração de conteúdo e navegação em browsers como tools opt-in registradas no `ToolGateway`. Ativação via uma linha (`from symbiote.browser import register; register(kernel, ...)`). Zero modificação no kernel; imports lazy garantem que `import symbiote` continua leve (<2s, sem Playwright/forge_scraper/SDKs em `sys.modules`). Tools registradas: `web_search` (Brave via SymGateway proxy), `web_extract` (forge_scraper primário + Firecrawl fallback configurável em chain), `web_crawl` (Firecrawl via SymGateway), `browser_navigate` / `_snapshot` / `_click` / `_fill` / `_screenshot` / `_wait_for` / `_close` (Chromium local via Playwright async).
- **`WebsitePolicy`** — blocklist/allowlist por domínio com wildcards (`*.ads.com`) e TTL cache. SSRF guard (`validate_url`) roda antes de qualquer I/O em navigate/extract/crawl. Toda chamada continua passando pelo PolicyGate + `audit_log` existentes — incluindo `request_id`, `cost_usd`, `elapsed_ms` do SymGateway.
- **Brave Search via SymGateway** — sem nova credencial (reusa `SYMGATEWAY_API_KEY`), sem SDK extra. Custo $0.003/query, billing centralizado. Endpoint `POST {gateway}/proxy/brave/web-search`.
- **`web_extract` com chain de fallback** — `forge_scraper` (platform-aware: YouTube transcript, Reddit posts, Twitter, Instagram, genérico via trafilatura) como primário, Firecrawl via SymGateway como fallback. Provider é selecionável via `extract_backend="forge_scraper"` ou `extract_backend=["forge_scraper", "firecrawl"]`. Campo `extracted_by` no resultado indica qual provider venceu.
- **Demo visual** — `scripts/demo_browser.py --headed --slow-mo 1500` abre Chromium na tela e executa navigate → snapshot → click → close pra validação manual.

### Extras opcionais (pyproject)

- `[browser]` — Playwright + Chromium runtime (~5 MB python + ~170 MB Chromium via `playwright install chromium`)
- `[extract]` — forge_scraper com sub-extras `[article,transcript,sofascore]` (sofascore é workaround pra bug de packaging upstream: `__init__.py` eager-import)
- `[stealth]` — espaço reservado para `playwright-stealth` (não implementado v0.6.0)

### Investigações

- **DuckDuckGo HTML como alternativa free ao Brave** — adicionada na Fase 7, investigada com binário do claw-code real, comprovada inviável (DDG bloqueia scraping HTTP com anomaly page e Playwright real com `static-pages/418.html` mesmo com UA Chrome + stealth init script). Revertido no commit `9ebd8a8`. Matriz de evidências preservada em `docs/integrations/symbiote-browser.md` Fase 7 pra não repetir o experimento.

### Documentação

- `docs/integrations/symbiote-browser.md` — plano arquitetural completo
- `docs/integrations/symbiote-browser-quickstart.md` — guia user-facing detalhado
- `docs/releases/v0.6.0.md` — release report para clientes (este release)

### Correções

- `__version__` realinhado com `pyproject.toml` (0.4.1 → 0.6.0 — drift desde a v0.4.1 corrigido)

### Backward compatibility

- **Zero breaking changes.** Hosts da v0.5.x atualizam sem mudar código. Tools só aparecem após `register()`; sem ela o kernel se comporta exatamente como antes. Verificado por suite dedicada (`tests/unit/browser/test_import_safety.py`, `test_register_noop.py`).

### Testes

- 50 unit + 6 integration/smoke no submódulo `symbiote.browser`, coverage ≥85%
- Suite full: 1446 passed (+40 vs v0.5.0). Mesmas 8 falhas pré-existentes em `e2e/test_acceptance`, `e2e/test_api_chat`, `unit/test_generation_settings`, `unit/test_instant_mode` que vinham da v0.5.0 — não tocadas neste release.

## [v0.5.0] - 2026-05-25

### Novas funcionalidades

- **Per-call `llm_config` propaga `kernel.message()` → `ChatRunner.run()` → adapter `config`** — novo kwarg opcional em `SymbioteKernel.message()` (e `message_async`) que carrega overrides de LLM ("essa chamada específica deve usar mode X / temperature Y") sem mutar a sessão nem o adapter global. Propagação cobre toda a cadeia: `kernel._message_inner` → `Capabilities.chat` → `ChatRunner.run` → `_build_llm_kwargs(context, llm_config=...)` que faz merge whitelist sobre `context.generation_settings` (copy-on-write — assembled context fica imutável). O dict de merge é o mesmo `config` que o adapter recebe via `self._llm.stream(messages, config=..., tools=...)`. Backward-compat: chamadas sem `llm_config` (None ou omitido) caem no caminho anterior bit-a-bit. ### Novo `effort` em `SubagentManager.spawn` — `SPAWN_DESCRIPTOR.parameters.properties.effort` aceita `"normal"` | `"high"` opcional. Quando setado, o spawn carrega `effort` no params → `SubagentManager.spawn` converte pra `llm_config={"mode": effort}` → `kernel.message(..., llm_config=...)`. Permite que sub-sessões individuais subam pra Opus-grade enquanto outras seguem em Sonnet, **sem mexer no adapter registrado no boot**. Effort inválido (`"ultra"` etc) rejeitado em `SubagentManager.spawn` antes de criar sessão — retorna `SpawnResult(success=False, error=...)` com mensagem clara. ### Testes — 4 novos em `test_subagent.py::TestSpawnEffort` cobrindo: descriptor lista effort como enum opcional; spawn com `effort="high"` forward llm_config; spawn sem effort omite llm_config (backward-compat); spawn com effort inválido rejeita estruturado. 4 novos em `test_chat_runner.py::TestLLMConfigOverride`: merge no stream config; override vence context settings; ausência preserva context; merge não muta context. 68/68 testes passam.

## [v0.4.1] - 2026-04-12

- fix: ParameterTuner.apply() filters unsupported params before configure()

---

## [v0.4.0] - 2026-04-12

### Novas funcionalidades

- **Dream Mode**: Motor de ruminacao em background com 5 fases (prune, reconcile, generalize, mine, evaluate). Toggleavel por symbiote via EnvironmentConfig (off/light/full). Budget-controlled LLM calls via BudgetTracker, dry-run mode, triggered automaticamente apos close_session(). Tabela dream_reports para persistencia de resultados.
- **on_after_tool_result hook**: Callback no ChatRunner que permite ao caller decidir se tool results encerram o loop — substitui heuristica hardcoded de fire-and-forget. Essencial para o OS Agent do SymTalk.
- **4-Mode Execution**: Taxonomia completa de execucao (instant/brief/long_run/continuous) implementada. Instant mode para single-shot, brief mode com loop controller, long-run com planner/evaluator por blocos, continuous mode para agentes persistentes de longa duracao.
- **Long-Run Handoff**: RunResult.handoff_data com blocks_completed, pending_blocks, output_summary. Session orientation on resume (S-01/S-02) — previous handoff injetado na primeira mensagem de sessao retomada.
- **Memory On-Demand**: context_mode="on_demand" expoe search_memories e search_knowledge como tools ao inves de pre-packed context.
- **Imperative Prompts**: Constraints section no generator e regras de verificacao explicitas no evaluator para long-run mode.
- **Preflight Tool Check**: _preflight_tools() aborta long-run se health_check() falha antes do loop.
- **API Config Endpoint**: PUT/GET /symbiotes/{id}/config para tool_mode, long-run config, timeouts.
- **Bash Builtin Tool**: Descriptor + handler nativo para execucao de comandos shell.

### Melhorias

- **ChatRunner**: Suporte a on_before_tool_call, on_after_tool_result, on_progress, on_stream callbacks. Schema cache para index mode. 3-layer compaction (microcompact + loop compact + autocompact).
- **ContextAssembler**: Resolucao de tool_mode="auto" via heuristica. Harness version overrides. Memory/knowledge shares configuraveis.
- **EnvironmentConfig**: 3 novos campos dream (dream_mode, dream_max_llm_calls, dream_min_sessions). PUT/DELETE /symbiotes/{id} endpoints. Tools allowlist.
- **Scoring**: Mode-aware auto_score (instant/brief/long_run/continuous calibration separada).

### Correcoes

- execution_traces schema inclui tool_mode column (fresh DB nao crasha mais)
- APIKey.is_admin property (cross-tenant auth checks nao dao AttributeError)
- OpenAI SDK retries desabilitados em 429 (previne burst amplification)
- __version__ alinhado com pyproject.toml
- Licenca corrigida de MIT para AGPL-3.0

### Documentacao

- 4 diagramas arquiteturais (Mermaid + PDF): estrutural, modos de execucao, memoria/aprendizado, dream mode
- Clark migration guide para v0.3.0
- Documentacao da taxonomia de 4 modos de execucao

### Outros

- 1346 tests (up from 1184), incluindo 35 novos testes para Dream Mode
- Modulo dream/ com engine.py, phases.py, models.py
- EnvironmentManager com getters para dream config

---

## [v0.3.0] - 2026-04-01

### Novas funcionalidades

- **Self-Evolving Harness**: Complete Meta-Harness system inspired by Stanford/CMU paper. SessionScore auto-computes quality from LoopTrace (stop_reason + iterations + failure rate). FeedbackPort lets hosts report user satisfaction. ParameterTuner auto-calibrates harness parameters with tiered activation (Tier 0-3). HarnessEvolver uses a proposer LLM to evolve prompt texts with guard rails and auto-rollback. Three evolvable components: tool_instructions, injection_stagnation, injection_circuit_breaker.
- **Harness Versioning**: harness_versions table tracks text variants per symbiote with score tracking, rollback chain, and version history. ChatRunner resolves active versions via ContextAssembler.
- **Agent Loop Resilience**: Parallel tool execution (asyncio.gather + ThreadPoolExecutor), LLM retry with exponential backoff (3 retries, 1s/2s/4s), diminishing returns detection (stagnation + circuit breaker via LoopController), 3-layer compaction (microcompact + loop compact + autocompact).
- **Timeout System**: Per-tool timeout (default 30s) and loop timeout (default 300s), both configurable per symbiote via EnvironmentConfig.
- **Human-in-the-Loop**: risk_level (low/medium/high) on ToolDescriptor, on_before_tool_call approval callback on ChatRunner. High-risk tools require explicit approval when callback is set.
- **Tool Mode**: tool_mode (instant/brief/continuous) replaces binary tool_loop. Instant = single-shot, brief = configurable loop (default), continuous = placeholder for future autonomous agents.
- **Streaming Mid-Loop**: on_progress(event, iteration, total) and on_stream(text, iteration) callbacks for real-time loop visibility. on_token behavior unchanged (final response only).
- **Working Memory Summary**: Loop execution summary prepended to WorkingMemory after tool calls, enabling multi-turn awareness of previous tool steps.
- **Memory On-Demand**: context_mode (packed/on_demand) per symbiote. search_memories and search_knowledge as builtin tools. On-demand mode skips pre-packed context injection.
- **Index Mode Cache**: Loop-local schema cache avoids redundant get_tool_schema calls in index mode, reducing iterations by ~50%.
- **Benchmark Suite**: BenchmarkRunner with task grading (tool_called, param_match, custom). Automated evaluation of symbiote performance.
- **Structural Evolution**: StructuralEvolver with pluggable strategy registry for code-level harness changes.
- **Cross-Symbiote Learning**: CrossSymbioteLearner detects tool overlap between symbiotes and transfers harness versions.
- **Multi-Model Test Matrix**: E2E test infrastructure with 3 scenarios across multiple models, collecting iteration/success/elapsed metrics.
- **MemoryEntry de Falha**: Deterministic procedural memory generated when tool loop fails (circuit_breaker, stagnation, max_iterations). Zero LLM cost.
- **Configurable Context Splits**: memory_share and knowledge_share per symbiote via EnvironmentConfig (defaults 0.40/0.25).
- **LoopTrace Persistence**: execution_traces table stores full trace (steps, stop_reason, timing) for observability and harness evolution.

### Melhorias

- **ContextAssembler**: Resolves evolvable text overrides from harness_versions, configurable memory/knowledge splits, context_mode support, timeout/tool_mode propagation
- **ChatRunner**: Accepts on_progress, on_stream, on_before_tool_call callbacks; uses _resolve_max_iters for tool_mode; integrates schema cache, approval gate, and timeout
- **LoopController**: Accepts custom stagnation and circuit_breaker messages for prompt evolution
- **EnvironmentConfig**: 16 configurable fields including tool_mode, context_mode, timeouts, splits, max_tool_iterations
- **ToolGateway**: register_memory_tools(), get_risk_level(), timeout support in execute/execute_async

### Correções

- run_async() now uses _call_llm_with_retry (was bypassing retry logic)
- E2E tool_results assertions relaxed for loop-aware behavior

### Documentação

- Complete QUICKSTART.md rewrite for v0.3.0
- New docs/HARNESS_EVOLUTION.md developer guide
- New docs/HOST_INTEGRATION.md for host developers
- Updated docs/README.md with architecture, API reference, config reference
- Updated SPEC.md with Execution Layer + Harness Layer in architecture diagram

### Outros

- 1184 tests (up from ~900), including 130+ new tests for harness features
- Multi-model E2E test infrastructure (skipable via SYMBIOTE_E2E_LLM=1)
- harness/ package: versions.py, tuner.py, evolver.py, benchmark.py, structural.py, cross_learning.py

---

## [v0.2.27] - 2026-04-01

### Added — Final Horizon Sprint

- [B-33] Per-tool timeout (30s default) + loop timeout (300s default) configurable per symbiote (`environment/tools.py`, `runners/chat.py`)
- [B-29] Human-in-the-loop — `risk_level` on ToolDescriptor + `on_before_tool_call` approval callback on ChatRunner (`environment/descriptors.py`, `runners/chat.py`)
- [B-34] Index mode schema cache — loop-local cache avoids redundant get_tool_schema calls (`runners/chat.py`)
- [B-35] Multi-model test matrix — E2E infrastructure with 3 scenarios across 3 models (`tests/e2e/test_multi_model.py`)
- [B-40] Tool Mode — `tool_mode: Literal["instant", "brief", "continuous"]` replaces binary `tool_loop` (`core/models.py`, `runners/chat.py`)
- [B-27] Streaming mid-loop — `on_progress(event, iter, total)` + `on_stream(text, iter)` callbacks (`runners/chat.py`)
- [B-30] Working memory intermediária — loop summary prepended to WorkingMemory assistant message (`runners/chat.py`)
- [B-68] Memory/Knowledge on-demand — `context_mode: packed|on_demand`, `search_memories`/`search_knowledge` builtin tools (`environment/tools.py`, `core/context.py`)
- [H-11] BenchmarkRunner — task grading (tool_called, param_match, custom) (`harness/benchmark.py`)
- [H-12] StructuralEvolver — pluggable strategy registry with proposal/apply (`harness/structural.py`)
- [H-13] CrossSymbioteLearner — tool overlap detection + harness version transfer (`harness/cross_learning.py`)

## [v0.2.26] - 2026-04-01

### Added — Prompt Evolution (Meta-Harness Fase 3)

- [B-67] HarnessEvolver — LLM proposer analyzes session traces (failed vs successful) and proposes improved harness texts; guard rails (max 2x length, CRITICAL preservation, format check); auto-rollback if score drops after 50 sessions (`harness/evolver.py`)
- [B-67] Evolvable text bridge — `AssembledContext` gains `tool_instructions_override`, `injection_stagnation_override`, `injection_circuit_breaker_override`; `ContextAssembler` resolves from `harness_versions`; `ChatRunner` and `LoopController` use overrides with fallback to defaults (`core/context.py`, `runners/chat.py`, `runners/loop_control.py`)
- [B-67] `kernel.set_evolver_llm(llm)` — host injects separate proposer LLM (option 3: accepts both, default to main LLM); `kernel.evolve_harness()` and `kernel.check_harness_rollback()` for batch invocation (`core/kernel.py`)

### Changed

- `LoopController` accepts `stagnation_msg` and `circuit_breaker_msg` parameters for customizable injection messages
- `_persist_score()` now tracks score per active harness version via `update_score()` for evolution rollback decisions
- 3 evolvable components defined: `tool_instructions`, `injection_stagnation`, `injection_circuit_breaker`

## [v0.2.25] - 2026-04-01

### Added — Harness Evolution (Meta-Harness Fase 2)

- [B-32/B-65] max_tool_iterations configurable — per symbiote via EnvironmentConfig (default 10, cap 50); propagates through ContextAssembler → AssembledContext → ChatRunner (`core/models.py`, `core/context.py`, `runners/chat.py`)
- [B-64] harness_versions table + HarnessVersionRepository — version evolvable texts per symbiote with score tracking and rollback (`harness/versions.py`, `adapters/storage/sqlite.py`)
- [B-65] ParameterTuner — tiered auto-calibration: Tier 0 (0 sessions, no change), Tier 1 (5+, safe only), Tier 2 (20+, statistical), Tier 3 (50+, fine tuning). Rules: max_iterations adjustment, compaction threshold, memory share. Safety caps + logging (`harness/tuner.py`)

### Changed

- `ChatRunner.run()`/`run_async()` now read `context.max_tool_iterations` instead of `_MAX_TOOL_ITERATIONS` constant
- `EnvironmentManager.configure()` accepts `max_tool_iterations` parameter

## [v0.2.24] - 2026-04-01

### Added — Harness Foundations (Meta-Harness Fase 1)

- [B-60] SessionScore — `compute_auto_score(trace)` computes 0.0-1.0 score from LoopTrace (stop_reason + iterations + failure rate); persisted in `session_scores` table on `close_session()` (`core/scoring.py`)
- [B-61] FeedbackPort — protocol for host to report session quality; `kernel.report_feedback(session_id, score, source)` updates `final_score = auto * 0.6 + user * 0.4` (`core/ports.py`, `core/kernel.py`)
- [B-62] MemoryEntry de falha — deterministic procedural memory generated when loop fails (circuit_breaker, stagnation, max_iterations); zero LLM cost, tagged `[harness_failure]` (`core/kernel.py`)
- [B-63] Context splits configuráveis — `memory_share` and `knowledge_share` per symbiote via EnvironmentConfig; defaults 0.40/0.25 preserved (`environment/manager.py`, `core/context.py`)
- [B-66] LoopTrace persistence — `execution_traces` table stores full trace (steps, stop_reason, timing); `CapabilitySurface` captures `last_loop_trace` from RunResult (`adapters/storage/sqlite.py`, `core/capabilities.py`)

### Changed

- `CapabilitySurface.chat()` and `chat_async()` now capture `loop_trace` from RunResult
- `kernel._message_inner()` persists trace to `execution_traces` after each chat call
- `kernel.close_session()` computes SessionScore + generates failure MemoryEntry before reflection
- `ContextAssembler._trim_to_budget()` uses per-symbiote memory/knowledge shares

## [v0.2.23] - 2026-04-01

- docs: clean up BACKLOG — remove 8 implemented items (B-25/26/28/31/37/38/39/43), add 9 Meta-Harness items (B-60 to B-68)
- docs: add Meta-Harness analysis to kb/

## [v0.2.22] - 2026-03-31

### Added — Agent Loop Resilience Sprint

- [B-55] Parallel tool execution — `ToolGateway.execute_tool_calls()` uses `ThreadPoolExecutor(max_workers=4)` for sync, `asyncio.gather()` for async; one failing tool does not block others (`environment/tools.py`)
- [B-56] LLM retry with exponential backoff — `ChatRunner._call_llm_with_retry()` retries transient errors (ConnectionError, TimeoutError, rate limits, 5xx) up to 3 times with 1s/2s/4s delays (`runners/chat.py`)
- [B-57] Diminishing returns detection + circuit breaker — `LoopController` monitors duplicate calls (same tool+params 2x), circuit breaker (same tool fails 3x), and injects stop message for clean LLM exit (`runners/loop_control.py`)
- [B-58] 3-layer compaction — Layer 1: microcompact (truncate tool results >2000 chars); Layer 2: loop compaction (summarize old pairs after 4 iterations); Layer 3: autocompact (aggressive compact when tokens >80% of context budget) (`runners/chat.py`)

### Changed

- [B-57] `LoopTrace` gains `stop_reason` field (end_turn, max_iterations, stagnation, circuit_breaker) (`runners/base.py`)
- [B-58] `ChatRunner` gains `context_budget` parameter (default 16000 tokens) for autocompact threshold
- [B-58] `_format_tool_results()` now applies microcompact to each individual result before injection

## [v0.2.21] - 2026-03-30

- feat: Hermes adaptations — SessionRecallPort, MemoryCategory, context compaction
- docs: comprehensive update — host integration guide, architecture, changelogs

## [v0.2.20] - 2026-03-30

### Added — Nanobot Report Adaptations

- [B-46] Prompt cache integration — `EnvironmentConfig.prompt_caching` flag propagates `prompt_caching=True` to forge_llm, enabling Anthropic cache breakpoints (~90% token saving)
- [B-47] Message retry with exponential backoff — `MessageBus` retries handler failures up to `max_retries` (default 3) with backoff (1s, 2s, 4s); `respond()` retries on QueueFull
- [B-48] Per-session locks — `SessionLock` provides sync/async per-session locking in `kernel.message()`; different sessions run in parallel, same session serializes
- [B-50] CompositeHook — composable lifecycle hooks (`before_tool`, `after_tool`, `before_turn`, `after_turn`) with error isolation per-hook
- [B-51] Delta streaming — `StreamDelta` event + `send_delta()`/`receive_delta()` in MessageBus for progressive token delivery to channels

### Added — Hermes Report Adaptations

- [B-52] SessionRecallPort — protocol for host-provided session search; kernel defines contract, host implements (FTS5, embeddings, etc.)
- [B-53] MemoryCategory — auto-classification of memories (ephemeral, declarative, procedural, meta) with `MEMORY_TYPE_CATEGORY` mapping; `get_by_category()` query method
- [B-54] Context compaction mid-loop — replaces old tool-loop message pairs with compact summary after 4+ iterations; prevents context growth during multi-step execution

### Changed

- [B-49] `HttpToolConfig.allow_internal` excluded from serialization (`model_dump()`) — can only be set programmatically in code, never from config/API/DB. Added audit log warning when SSRF bypass is active
- [B-53] ReflectionEngine now stores extracted facts with their actual type (preference, constraint, procedural) instead of generic "reflection"

## [0.2.5] — 2026-03-19

### Added — Tool Loop (agentic multi-step execution)

- `ChatRunner` tool loop — when `tool_loop=True` (default), the runner iterates: LLM → parse tool calls → execute → feed results back → LLM, until the model responds without tool calls or hits `max_iterations` (default 10). Previously the LLM was blind after the first tool call (single-shot).
- `_format_tool_results()` — formats tool execution results as structured messages injected back into the conversation for the next LLM turn
- `_format_assistant_with_calls()` — preserves the assistant's tool call text in conversation history so the LLM sees the full chain of reasoning
- Working memory only stores the **final** response, not intermediate tool-calling turns
- `RunResult.output` returns `{"text": ..., "tool_results": [...]}` when tools were executed, preserving full audit trail
- `run_async()` — async variant with identical loop semantics, uses `execute_tool_calls_async()`

### Added — Semantic Tool Loading

- `ContextAssembler` now supports three tool loading modes via `EnvironmentConfig.tool_loading`:
  - **full** — complete tool schemas in system prompt (existing behavior)
  - **index** — compact one-line-per-tool catalog with a `get_tool_schema` meta-tool for lazy schema fetching
  - **semantic** — LLM-powered pre-filter resolves relevant tool tags before context assembly, minimizing prompt size
- `ToolTagResolver` (`environment/resolver.py`) — uses a cheap/fast LLM to select relevant tool tags from the user query, reducing the tool set sent to the main LLM
- `EnvironmentConfig.tool_loading: Literal["full", "index", "semantic"]` — persisted per-symbiote
- `EnvironmentConfig.tool_loop: bool` — toggle agentic loop on/off per-symbiote
- `EnvironmentConfig.tool_tags: list[str]` — filter tools by tag for scoped visibility
- `EnvironmentManager.get_tool_loading()`, `get_tool_loop()`, `get_tool_tags()` — accessors with SQLite persistence
- `PUT/GET /symbiotes/{id}/tool-tags` — REST endpoints for tool loading configuration
- `kernel.configure_tool_visibility()` — unified API for setting tags, loading mode, and loop toggle

### Added — ToolGateway enhancements

- `ToolGateway.execute_tool_calls()` — batch execution accepting `list[ToolCall]`, returns `list[ToolCallResult]`
- `ToolGateway.execute_tool_calls_async()` — async batch variant
- `ToolGateway.get_tool_schema(tool_id)` — returns full schema dict for a single tool (used by index mode's meta-tool)
- `ToolGateway.list_tags()` — returns deduplicated set of all registered tool tags
- `ToolGateway.get_descriptors_by_tags(tags)` — filter registered tools by tag list
- `ToolCallResult` model — structured result with `tool_id`, `success`, `output`, `error`

### Added — Discovery enhancements

- `DiscoveredTool.handler_type` field — distinguishes HTTP vs CLI vs custom discovered tools
- `DiscoveryService` FastAPI strategy now extracts response models and query parameters
- `DiscoveredToolRepository` upsert preserves `handler_type` across re-scans

### Changed

- `ChatRunner.run()` refactored from single-shot to iterative loop (backward compatible: `tool_loop=False` restores single-shot)
- `ChatRunner._build_system()` now includes brief tool-loop instructions when `tool_loop=True`
- `AssembledContext` gains `tool_loading`, `tool_loop`, `available_tools` fields
- SQLite schema: `env_configs` table gains `tool_loading`, `tool_loop`, `tool_tags` columns (idempotent ALTER TABLE)

### Tests

- 794 tests passing (+170 new)
- `test_chat_runner_tools.py` — tool loop iterations, max_iterations guard, async loop, tool results accumulation
- `test_context.py` — all three loading modes (full/index/semantic), token budget with tools
- `test_tool_gateway.py` — batch execution, tag filtering, schema retrieval, async execution
- `test_environment.py` — tool_loading/tool_loop/tool_tags persistence round-trip
- `test_loading_modes.py` — 241 realistic tools (YouNews-like), semantic filtering with mock LLM
- `test_resolver.py` — ToolTagResolver unit tests

## [0.2.4] — 2026-03-18

### Added — B-23: Deploy Hosted (DevOps)

- Porta 8008 alocada no port-registry
- `symbiote.service` systemd unit rodando em produção
- Nginx + SSL via certbot em `symbiote.symlabs.ai`
- CI/CD via `.gitea/workflows/staging-deploy.yml` — push na main → pull + restart automático
- Deploy prod via `promote.sh`

### Added — B-7: MCP Integration

- `symbiote.mcp.provider.McpToolProvider` — bridges a live `forge_llm.application.tools.ToolRegistry` (produced by `McpToolset`) into Symbiote's `ToolGateway`; each MCP tool is registered as an async custom handler that delegates to `McpTool.execute_async()`
- `SymbioteKernel.load_mcp_tools(registry, symbiote_id)` — convenience method: loads all tools from a forge_llm ToolRegistry and auto-authorizes them via `EnvironmentManager.configure()`
- Tool names are sanitized (hyphens and spaces → underscores) to produce valid tool_ids
- MCP errors (`result.is_error`) surface as `RuntimeError` so PolicyGate captures them as failed tool results
- Supports stdio and HTTP transports via forge_llm's `McpToolset.from_stdio()` / `McpToolset.from_http()` / `McpToolset.from_servers()`

### Added — B-24: DiscoveredToolLoader

- `symbiote.discovery.loader.DiscoveredToolLoader` — reads approved discovered tools from SQLite and registers them as HTTP tools in `ToolGateway` with `allow_internal=True`; resolves `{base_url}` placeholder and skips CLI tools (`handler_type=custom`) or tools without `method`/`url_template`
- `SymbioteKernel.load_discovered_tools(symbiote_id, base_url)` — single call to load and auto-authorize discovered tools via `EnvironmentManager.configure()`, closing the loop: `discover → approve → kernel uses`

### Fixed

- SQLite `check_same_thread=False` in `SymbioteKernel.__init__` (prevented thread errors in asyncio context)
- Dev mode auth bypass (`SYMBIOTE_DEV_MODE=1`) now unconditional — checked before `key_manager` initialization to prevent 401 on second request

## [0.2.3] — 2026-03-18

### Added — Discovery Service (sprint-discovery-service)

- `symbiote.discovery.models.DiscoveredTool` — Pydantic model for tools found by scanning a repository (status: pending/approved/disabled)
- `symbiote.discovery.repository.DiscoveredToolRepository` — SQLite-backed CRUD for discovered tools; upsert preserves approval status across re-scans
- `symbiote.discovery.service.DiscoveryService.discover()` — scans a repository using 4 strategies: OpenAPI/Swagger specs, FastAPI decorators, Flask decorators, pyproject.toml scripts; deduplicates by tool_id
- `SQLiteAdapter` schema: `discovered_tools` table with unique constraint on `(symbiote_id, tool_id)` and index on `(symbiote_id, status)`
- REST API: `POST /symbiotes/{id}/discover`, `GET /symbiotes/{id}/discovered-tools?status=`, `PATCH /symbiotes/{id}/discovered-tools/{tool_id}`, `DELETE /symbiotes/{id}/discovered-tools/{tool_id}`
- CLI `symbiote init` — creates or links a symbiote on a remote server, writes `.symbiote/config` with server URL, API key, symbiote ID and name
- CLI `symbiote discover [path]` — scans a local repository and registers tools; displays Rich table of discovered tools with method, endpoint and source file
- Dashboard "Discovered Tools" section — lists all discovered tools across symbiotes with method, endpoint, status badge and approve/disable toggle; two new stat cards (Tools total, Pending); Quick Reference updated with discovery endpoints
- `GET /api/dashboard` now returns `discovered_tools` list and `stats.discovered_tools` / `stats.pending_tools`

## [0.2.2] — 2026-03-18

### Added — Internal Tools & Async Streaming (YouNews feedback)

- `HttpToolConfig.allow_internal` — opt-in flag to bypass SSRF validation for tools that intentionally call loopback/private-network endpoints (e.g. same-host services); default `False` preserves existing protection
- `kernel.message_async(session_id, content, on_token=...)` — async entry point for chat; eliminates manual `ContextVar`/`emit_event` workarounds in SSE integrations
- `CapabilitySurface.chat_async()` — async variant of `chat()` that propagates `on_token` down to the runner
- `on_token` callback in `ChatRunner.run()` and `run_async()`: called per-token when LLM exposes `stream()`, or once with full response as fallback

## [0.2.1] — 2026-03-18

### Added — Dynamic Auth Headers & Async Tool Handlers (YouNews feedback)

- `HttpToolConfig.header_factory` — callable invoked per-request to supply dynamic headers (e.g. user-scoped auth tokens); eliminates `threading.local` workarounds in host integrations
- `PolicyGate.execute_with_policy_async()` — async policy execution: awaits coroutine handlers, wraps sync handlers via `asyncio.to_thread`
- `ToolGateway.execute_async()` / `execute_tool_calls_async()` — async execution path for tool calls
- `ChatRunner.run_async()` — async runner variant that uses `execute_tool_calls_async()`, resolving single-worker event-loop deadlocks when tools call the same uvicorn process

## [0.2.0] — 2026-03-17

### Added — Native Function Calling

- `LLMResponse` model — structured return type for LLM adapters with optional `tool_calls` field
- `NativeToolCall` model — represents a provider-native tool call with `call_id`, `tool_id`, `params`
- `ToolDescriptor.to_openai_schema()` — converts tool descriptors to OpenAI function calling format
- `LLMPort.complete()` now accepts optional `tools` parameter for native tool definitions
- `ChatRunner(native_tools=True)` — opt-in flag to use native function calling instead of text-based parsing
- When `native_tools=True`, tool definitions are passed to the LLM via the `tools` parameter and text-based `tool_call` instructions are omitted from the system prompt
- Full backward compatibility: adapters returning `str` continue to work via text-based parsing

### Changed

- `LLMPort.complete()` signature expanded: `tools: list[dict] | None = None` parameter added
- `ChatRunner` detects `LLMResponse` vs `str` return type automatically

## [0.1.8] — 2026-03-17

### Added — Hosted Service (API + SDK)

- [B-19] API Key Authentication — Bearer token auth with SHA-256 hashed keys, tenant isolation, admin/user roles (`api/auth.py`, `api/middleware.py`)
- [B-20] Chat Endpoint — `POST /sessions/{id}/chat` calls `kernel.message()` with LLM + tools via HTTP API (`api/http.py`)
- [B-21] Multi-tenant Isolation — `owner_id` set on symbiote creation, tenant check on chat and get endpoints
- [B-22] Python SDK — `SymbioteClient` thin HTTP client with httpx, context manager, full API coverage (`sdk/client.py`)
- Admin endpoints: `POST/GET/DELETE /admin/api-keys` for key lifecycle management
- Dev mode: `SYMBIOTE_DEV_MODE=1` env var for local development without auth

### Changed

- All mutation endpoints now require `Authorization: Bearer sk-symbiote_...` header
- Symbiote creation via API sets `owner_id` from authenticated tenant
- `GET /symbiotes/{id}` enforces tenant ownership check
- `POST /sessions/{id}/chat` verifies session belongs to authenticated tenant

## [0.1.7] — 2026-03-17

### Added — Security & Quality (nanobot report)

- [B-14] SSRF Protection — validate URLs against private/internal IP ranges before HTTP requests, redirect validation (`security/network.py`)
- [B-15] Untrusted Content Banner — wrap external HTTP responses with `[External content]` banner to mitigate prompt injection (`environment/tools.py`)
- [B-17] GenerationSettings — configurable temperature/max_tokens/top_p/reasoning_effort with pass-through to LLM (`core/generation.py`)

### Changed

- [B-16] Memory Consolidation — async mode with background thread, sync fallback for SQLite, `_persist_facts()` extracted (`memory/consolidator.py`)
- [B-18] WorkingMemory trim — aligns to user turn boundaries, prevents orphaned assistant messages (`memory/working.py`)
- [B-14] HTTP tool handler uses custom redirect handler that re-validates each redirect URL
- [B-15] HTTP responses (string, dict, list) all wrapped with untrusted content banner

## [0.1.6] — 2026-03-17

### Fixed

- `ForgeLLMAdapter` — use `response.content` instead of `response.message` (forge-llm 0.7.8 API change)
- `ForgeLLMAdapter` — auto-resolve `{PROVIDER}_API_KEY` and `{PROVIDER}_BASE_URL` from env vars when not passed explicitly
- E2E tests default provider changed from `anthropic` to `symgateway`

## [0.1.5] — 2026-03-17

### Added — Nanobot-inspired Architecture

- [B-8] Tool Error Hints — auto-inject retry hints on failed tool calls (`environment/tools.py`)
- [B-9] Runtime Context Strip — ephemeral metadata in LLM prompts without polluting session history (`environment/runtime_context.py`)
- [B-10] Memory Consolidation — LLM-based summarization of working memory when tokens exceed threshold (`memory/consolidator.py`)
- [B-11] Subagent Spawning — inter-Symbiota task delegation with recursion guard and isolated sessions (`runners/subagent.py`)
- [B-12] MessageBus — async inbound/outbound queues for channel decoupling (`bus/`)
- [B-13] Progressive Skills — lazy-loaded .md skills with XML summary for system prompts (`skills/loader.py`)

### Added — Original Backlog

- [B-3] MessageRepository port — isolate SQL from ReflectionEngine via MessagePort protocol (`adapters/storage/message_repository.py`)
- [B-4] Semantic Recall Provider — keyword-based memory scoring with tokenization and stop words (`memory/recall.py`)
- [B-6] ProcessEngine Cache Invalidation — TTL-based cache with `invalidate_cache()` for multi-worker support (`process/engine.py`)
- [B-2] Interactive CLI Chat — REPL loop with `/quit`, `/reflect` commands (`cli/main.py interactive`)
- [B-5] LLM E2E Integration Tests — 5 skipable tests for real LLM validation (`tests/e2e/test_e2e_llm_integration.py`)
- [B-1] Docker Container — multi-stage Dockerfile with health check endpoint, volume persistence

### Changed

- `ReflectionEngine` now depends on `MessagePort` instead of `StoragePort` (B-3)
- `ChatRunner` now injects runtime context and supports optional `MemoryConsolidator` (B-9, B-10)
- `ProcessEngine` constructor accepts `cache_ttl` parameter (B-6)
- `ToolGateway.execute_tool_calls()` appends retry hint to error messages (B-8)
- `SymbioteKernel` now creates `MessageRepository`, `SubagentManager`, registers spawn tool (B-3, B-11)
- HTTP API: added `GET /health` endpoint (B-1)

## [0.1.4] — 2026-03-16

### Added

- symbiote-ui reusable chat Web Component
- Deployment architectures section to QUICKSTART (embedded vs HTTP)

## [MVP] — 2026-03-16

### Added

- [US-01] Identity & Persona — create, persist, update symbiote identity with audit trail
- [US-02] Session Lifecycle — start, resume, close sessions with messages, decisions, summary
- [US-03] Workspace & Workdir — persistent workspaces with artifact tracking on real filesystem
- [US-04] Environment — configurable tools, services, policies per symbiote/workspace
- [US-05] Knowledge Layer — knowledge sources separate from relational memory
- [US-06] Memory Stack — 4 layers: working, session, long-term relational, semantic recall interface
- [US-07] Context Assembly — selective pipeline with configurable token budget
- [US-08] Runners — ChatRunner, ProcessRunner with registry and intent selection
- [US-09] Tools & Policy Gate — fs_read/fs_write/fs_list with deny-by-default authorization and audit log
- [US-10] Process Engine — declarative processes with 5 default definitions and step-by-step execution
- [US-11] 6 Capabilities — Learn, Teach, Chat, Work, Show, Reflect as explicit operations
- [US-12] Reflection Engine — keyword heuristic fact extraction, noise detection, summary generation
- [US-13] Export Service — sessions, memories, decisions as Markdown
- [US-14] Three interfaces — Python library, CLI (Typer+Rich), HTTP API (FastAPI)
- MockLLMAdapter for testing without API key
- ForgeLLMAdapter for Anthropic/OpenAI/OpenRouter via ForgeLLM
- Domain exception hierarchy (EntityNotFoundError, ValidationError, CapabilityError, LLMError)
- 393 tests, ~96% coverage
