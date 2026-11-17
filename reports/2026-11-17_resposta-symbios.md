# Resposta da equipe do Symbiote ao pedido do SymbiOS

Data: 2026-11-17 · Base do pedido: Symbiote v0.8.9 (`268a3ce`) · Entrega: na release seguinte à v0.8.9

Dos 16 pedidos (os 15 originais e o adendo dos tokens de cache), 14 entraram na engine, inteiros ou em parte, e 2 ficaram de fora. O critério foi o mesmo para todos: a engine tem vários hosts em uso, então entra o mecanismo que serve a qualquer um deles, e a regra de um produto fica no host. Para cada item que ficou de fora há abaixo o motivo e o caminho para fazer do lado de vocês com a API pública.

A referência completa de cada recurso está em `docs/HOST_INTEGRATION.md`. O que muda de comportamento ao atualizar está na seção 4.

## 1. Resumo

| # | Pedido | Resultado |
|---|---|---|
| 1 | Aprovação adiada | Entrou |
| 2 | Contexto e tokens por chamada ao modelo | Entrou. O "budget máximo por request" do B-36 continua aberto |
| 3 | Versão do symbiota por turno | **Não entrou** |
| 4 | Saber que o código roda dentro de um spawn | Entrou |
| 5 | Aviso de fim do sonho | Entrou o aviso e a espera. O sonho ler o comentário do dono não entrou |
| 6 | Widget no pacote Python e defeito do histórico | Entrou |
| 7 | Procedimentos em teste listados e legíveis | Entrou, como opção por symbiota |
| 8 | Objectives no adapter do SymGateway | Entrou, só dentro do adapter |
| 9 | Resumo ao encerrar | Entrou |
| 10 | Nota automática e avaliação do dono | Entrou a nota do dono. A nota 0,8 do turno sem ferramentas não mudou |
| 11 | Reativar uma memória | Entrou |
| 12 | Modelo do sonho por symbiota | Entrou |
| 13 | Conhecimento "só quando procura", item a item | Entrou |
| 14 | Ler uma descrição OpenAPI já baixada | Entrou a conversão em ferramentas. A função que monta a chamada HTTP não entrou |
| 15 | Textos do prompt por idioma | **Não entrou agora** |
| 16 | Tokens de cache em cada chamada registrada | Entrou, na engine e nos dois adapters que vêm com ela |

## 2. O que entrou, e o que vocês podem apagar

### Pedido 1 — Aprovação adiada

O callback de aprovação continua aceitando `True` e `False` e ganhou uma terceira resposta:

```python
from symbiote.environment.policies import ApprovalDecision

def aprovar(tool_id, params, risco, ctx):
    if ctx.host.get("dono"):
        return True
    return ApprovalDecision.defer("pedido de visitante", expires_in=48 * 3600)

kernel.set_approval_callback(aprovar, pass_context=True)
```

Com `defer`, a chamada não roda. A engine grava o pedido, o modelo recebe um resultado dizendo que a ação aguarda uma pessoa (o texto é configurável com `defer(message=...)`) e o turno termina normalmente.

```python
outcome = kernel.turn(session_id, texto)
outcome.pending_approvals                       # ids criados neste turno
kernel.inspector.list_pending(symbiote_id)      # os que aguardam
kernel.inspector.list_pending(symbiote_id, status=None, subject_key="visitante-42")

resolvido = kernel.resolve_pending(id, True, note="pode cancelar", resolved_by="dono-1",
                                   host_context={...})
resolvido.status    # "executed" (resolvido.result) ou "failed" (resolvido.error)
kernel.resolve_pending(outro_id, False, note="este mês não")     # "denied"
```

O que foi atendido do pedido de vocês:

- **Registro do pedido:** id, symbiota, sessão, `subject_key`, ferramenta, parâmetros e risco. Os parâmetros que a listagem mostra já vêm sem os segredos conhecidos.
- **Execução no contexto original:** mesma sessão, mesmo symbiota, mesmo `subject_key`, mesmos parâmetros.
- **Auditoria ligada ao pedido:** a linha de `audit_log` da execução leva o id do pedido, e o tool guard e o handler o enxergam em `current_tool_context().approval_id`.
- **Listar, expirar e sobreviver a reinício:** o prazo padrão é de 168 horas (`KernelConfig.pending_approval_ttl_hours`) ou o de `defer(expires_in=...)`.

Três pontos em que a forma final difere da sugestão:

- **O resultado chega ao modelo pelo contexto, não como mensagem gravada na sessão.** No turno seguinte da mesma conversa, ou do mesmo visitante com o mesmo symbiota em qualquer sessão, o modelo recebe um bloco dizendo o que aconteceu com cada pedido (executado e o resultado, falhou, negado com a nota, ou expirado), uma única vez. Isso cobre o visitante que volta numa sessão nova, o que uma mensagem na sessão antiga não cobria.
- **O `defer` é do callback de aprovação, não do tool guard.** O guard roda antes do callback. Se o guard de vocês nega a ferramenta para visitantes, o callback nunca é consultado. O guard precisa deixar passar a chamada que já tem aprovação, por exemplo `if ctx.approval_id: return ToolDecision.allow()`, e o adiamento fica no callback.
- **O callback só é consultado a partir do risco configurado.** O padrão é risco alto. Para adiar ferramentas de risco menor, usem `set_approval_callback(..., risk_threshold="low")` e respondam `True` para o resto.

O `host_context` do turno que pediu não é guardado, porque a engine nunca o persiste. Passem em `resolve_pending` o que a execução precisar.

Dá para apagar: a tabela de pedidos de vocês e a execução direta por `tool_gateway.execute` planejada para a entrega 7.

### Pedido 2 — Para quem é cada chamada ao modelo, e quanto custou

```python
from symbiote.core.llm_usage import current_llm_call, report_usage

chamada = current_llm_call()     # dentro de complete() / stream() do adapter
chamada.symbiote_id, chamada.session_id, chamada.root_session_id, chamada.purpose
```

`purpose` é `turn`, `subagent`, `reflection`, `session_summary`, `skill_review`, `dream:<fase>`, `harness`, `memory_consolidation`, `tool_resolution`, `long_run:plan`, `long_run:evaluate` ou `long_run:report`. Vale também para as chamadas do modelo auxiliar feitas fora de um turno, que eram as que ficavam sem dono.

A engine grava uma linha por chamada na tabela `llm_usage`, com provedor, modelo e tokens. Funciona com chave direta da Anthropic ou da OpenAI: os tokens vêm do adapter, não de gateway. A engine guarda tokens e não preço; o preço é de vocês.

```python
outcome.usage                                    # total do turno, incluindo o que ele delegou
ins = kernel.inspector
ins.usage_by("purpose", symbiote_id)             # por finalidade
ins.usage_by("subject", symbiote_id)             # por visitante
ins.usage_totals(root_session_id=session_id)     # uma conversa inteira, com as delegações
ins.usage_totals(symbiote_id, purpose="dream", since="2026-11-01T00:00:00+00:00")
kernel.set_usage_callback(lambda registro: ...)  # cada chamada, na hora
```

Um ponto de atenção para o adapter de vocês: a engine lê os tokens do `usage` do `LLMResponse`. Se o adapter devolve texto puro, ou faz streaming, ele precisa chamar `report_usage(prompt_tokens, completion_tokens, provider=..., model=...)` antes de retornar. Sem isso a chamada é registrada sem tokens.

Para separar "API key" de "widget", a engine não conhece esses conceitos. Usem o `subject_key`, que já distingue os visitantes, ou guardem do lado de vocês a origem de cada sessão e somem por `session` ou `root_session`.

Ficou de fora o "budget máximo por request" do B-36. Continua no backlog.

Dá para apagar: a medição de tokens dentro do adapter de vocês, se quiserem usar a da engine.

### Pedido 16 — Tokens de cache

Entrou na mesma migração da tabela `llm_usage`, como vocês sugeriram. A convenção é a que vocês propuseram: `prompt_tokens` é sempre a entrada inteira, com o cache, e os campos de cache são partes dela.

| Campo | O que é |
|---|---|
| `prompt_tokens` | a entrada inteira, com o cache |
| `cache_read_tokens` | a parte lida do cache |
| `cache_write_tokens` | a parte escrita no cache |
| `cache_write_5m_tokens`, `cache_write_1h_tokens` | a escrita separada por duração, quando o provedor informa |
| `reasoning_tokens` | só informação; já está dentro de `completion_tokens` |

Os campos estão no `LLMTokenUsage`, no `report_usage()`, no `LLMCallRecord` (e portanto no `set_usage_callback`), na tabela `llm_usage` e nas somas de `inspector.usage_totals` e `usage_by`. `TurnOutcome.usage` traz `cache_read_tokens` e `cache_write_tokens` do turno. A entrada cobrada pelo preço comum é `prompt_tokens - cache_read_tokens - cache_write_tokens`. A engine continua guardando só tokens.

Para o host não precisar saber que a Anthropic informa diferente da OpenAI, há um conversor público:

```python
from symbiote.core.llm_usage import report_usage, usage_from_provider

report_usage(usage=usage_from_provider(resposta.usage), provider="anthropic", model=modelo)
```

`usage_from_provider` lê os nomes de cada provedor: soma `cache_read_input_tokens` e `cache_creation_input_tokens` à entrada da Anthropic, lê `prompt_tokens_details.cached_tokens` da OpenAI e aceita os campos do SymGateway como vêm.

Os dois adapters que vêm com a engine preenchem os campos:

- **`ForgeLLMAdapter`.** O `forge_llm` reduzia o uso do provedor a três contadores, então o cache não chegava e a entrada da Anthropic vinha sem a parte do cache. Corrigimos no `forge_llm` (v0.7.17) e a engine já usa essa versão. Vale para Anthropic e OpenAI, em respostas de texto, com ferramentas e em streaming.
- **`SymGatewayLLMAdapter`.** Ele procurava `prompt_tokens` e `completion_tokens`, e o gateway responde `input_tokens` e `output_tokens`, então entrada e saída vinham vazias. Corrigimos, e ele preenche também os campos de cache.

Um adapter próprio, como o de vocês para as chaves diretas, preenche tudo com `usage_from_provider`. No streaming da OpenAI é preciso pedir o uso ao provedor (`stream_options.include_usage`), como vocês já apontaram.

### Pedido 4 — Saber que o código roda dentro de um spawn

```python
from symbiote.environment.exec_context import current_spawn

spawn = current_spawn()     # None fora de uma delegação
spawn.symbiote_id, spawn.session_id                 # o colega e a subsessão dele
spawn.parent_symbiote_id, spawn.parent_session_id   # quem chamou spawn
spawn.root_symbiote_id, spawn.root_session_id       # o turno que o host iniciou
spawn.depth
```

Vale no adapter do modelo, nos ganchos, no tool guard e nos handlers.

Dá para apagar: a leitura de `symbiote.runners.subagent._spawn_depth`.

### Pedido 5 — Fim do sonho

```python
kernel.set_dream_callback(lambda symbiote_id, report, error: ...)
kernel.wait_dream(symbiote_id, timeout=30)    # True quando não há ciclo automático rodando
```

O callback roda ao fim de todo ciclo, automático ou pedido por `kernel.dream()`, com o `DreamReport`, ou com o erro quando o ciclo falhou. `wait_dream` espera também o callback.

Dá para apagar: a espera em `kernel._dream_threads`. O passo próprio de vocês vai dentro do callback.

### Pedido 6 — Widget

```python
from symbiote.ui import widget_path, widget_version
```

O arquivo vai dentro do pacote instalado, e `widget_version()` muda sempre que o arquivo muda.

O defeito do histórico que vocês acharam procede e foi corrigido: o histórico agora é carregado uma vez, de um adapter de verdade, quando o painel está aberto. Um adapter definido depois de conectar carrega o histórico sozinho. Há também um `reload()` público.

Dá para apagar: a cópia da v0.8.9 do arquivo e o zeramento de `_historyLoaded`.

### Pedido 7 — Procedimentos em teste

O diagnóstico de vocês estava certo: sem caminho para um procedimento em teste ser lido num turno, `skill_auto_promote_threshold` nunca disparava. Agora é uma opção por symbiota, desligada por padrão para não mudar o comportamento dos outros hosts:

```python
kernel.environment.configure(symbiote_id=sid, skill_trial_listing=True)
```

Ligada, os procedimentos em teste aparecem ao modelo marcados `status="trial"`, o `skill_view` os lê e cada leitura conta para a promoção.

Dá para apagar: a listagem no contexto extra e a habilidade `skill_trial_view`.

### Pedido 8 — Objectives do SymGateway

```python
llm = SymGatewayLLMAdapter(api_key, objective="chat_fast", on_routed=registrar)
llm.list_objectives()
llm.last_routing      # {"model", "objective", "provider", "request_id", "work_session"}
```

Fica só dentro do adapter do SymGateway. A engine não depende do gateway e não conhece objectives.

### Pedidos 9, 11, 12, 13 e 14

| Pedido | Como usar |
|---|---|
| 9. Resumo | `configure(session_summary_mode="llm")` para o resumo escrito pelo modelo auxiliar, ou `"off"` para não gerar. O padrão continua o de antes |
| 11. Reativar memória | `kernel.memory.reactivate(memory_id, symbiote_id=...)` |
| 12. Modelo do sonho | `configure(dream_llm="main")` |
| 13. Conhecimento por item | `kernel.knowledge.register_source(..., auto_recall=False)` ou `kernel.knowledge.set_auto_recall(id, False)`. O item sai do contexto automático e continua em `search_knowledge` |
| 14. OpenAPI em mãos | `kernel.discover_tools(symbiote_id, spec=descricao, base_url=...)`, e depois os mesmos passos de aprovar e carregar |

### Pedido 10 — Nota

A nota dada com `report_feedback` antes de `close_session` agora entra na nota final. Era um defeito da engine.

Dá para apagar: a reaplicação da avaliação depois de encerrar.

## 3. O que não entrou, por quê, e como fazer do lado de vocês

### Pedido 3 — Versão do symbiota por turno

**Por que não.** "Rascunho" e "publicado" são conceitos do SymbiOS. O `TurnOverrides` proposto deixaria qualquer host trocar persona, configuração e ferramentas a cada turno por fora da política de ferramentas do symbiota, que é a garantia que os outros hosts usam.

**Como fazer.** Dois symbiotas: um de rascunho, que o dono edita, e um publicado, que atende os visitantes. Publicar é copiar de um para o outro. Testamos esta cópia só com API pública:

```python
rascunho = kernel.get_symbiote(rascunho_id)
kernel.update_symbiote(publicado_id, role=rascunho.role, persona=rascunho.persona_json)

cfg = kernel.environment.get_config(rascunho_id).model_dump(
    exclude={"id", "symbiote_id", "workspace_id"})
kernel.environment.configure(
    symbiote_id=publicado_id, **{k: v for k, v in cfg.items() if v is not None})

for item in kernel.knowledge.list_by_symbiote(publicado_id):
    kernel.knowledge.remove(item.id)
for item in kernel.knowledge.list_by_symbiote(rascunho_id):
    kernel.knowledge.register_source(
        publicado_id, item.name, source_path=item.source_path, content=item.content,
        entry_type=item.type, tags=item.tags, auto_recall=item.auto_recall)
```

O que mais considerar:

- **Memória.** A dos visitantes já fica no symbiota publicado, por `subject_key`. A memória compartilhada que o dono ensinou no rascunho precisa ser copiada ao publicar: `inspector.list_memories(rascunho_id, subject_key=None)` e `kernel.memory.store(...)` no publicado.
- **Procedimentos.** No escopo por symbiota, cada um tem a própria biblioteca: copiem com `kernel.skills_loader_for(rascunho_id)` e `kernel.skills_store_for(publicado_id)`.
- **Ferramentas.** A autorização é por symbiota (`configure(tools=[...])`), então o publicado pode ter uma lista diferente. Uma ferramenta HTTP registrada com `register_http_tool` pertence a um symbiota só: registrem uma vez e autorizem nos dois. Se o dono puder editar a definição da ferramenta, o publicado enxerga a edição na hora; para versionar a ferramenta, usem ids diferentes.
- **Prompt.** Um campo que vale `None` no rascunho não limpa o do publicado; para limpar `planner_prompt` e `evaluator_prompt`, passem texto vazio.

Isso remove a dependência de `kernel._identity.get`, que é o risco que vocês apontaram.

Se outro host pedir a mesma coisa, o desenho que serviria a todos é a sessão ficar presa a uma revisão nomeada da configuração do symbiota. Não está planejado.

### Pedido 5 (parte) — O sonho considerar o comentário do dono

**Por que não.** O comentário é um dado do produto, e o sonho não tem como saber o peso dele.

**Como fazer.** Rodem o passo de vocês no `set_dream_callback`. Se o comentário for uma instrução que deve valer daqui para frente, gravem como memória compartilhada (`kernel.memory.store`, tipo `preference` ou `constraint`): aí ele entra em todo turno e o sonho o enxerga como qualquer outra memória.

### Pedido 10 (parte) — Nota de turno sem ferramentas

**Por que não.** A nota automática mede o laço de ferramentas. Sem ferramentas não há o que medir, e 0,8 é o valor neutro.

**Como fazer.** O turno sem ferramentas já fica registrado em `inspector.list_turn_contexts`, agora com os tokens. Para distinguir a qualidade, usem `report_feedback`, que passou a ser preservado.

### Pedido 14 (parte) — Função para montar a chamada HTTP

**Por que não.** É parte interna do gateway de ferramentas, com as regras de codificação, de origem e de credenciais.

**Como fazer.** Convertam a descrição com `discover_tools(spec=...)`, aprovem com `kernel.discovered_tools.set_status(...)`, carreguem com `kernel.load_discovered_tools(...)` e executem com `kernel.tool_gateway.execute(...)`.

### Pedido 7 (parte) — Arquivamento por prazo só no sonho

**Por que não.** A manutenção da biblioteca é uma fase do sonho.

**Como fazer.** `dream_mode="light"` roda só as fases sem modelo, incluindo essa, e não custa nenhuma chamada. Ou chamem `kernel.dream(symbiote_id)` num agendamento.

### Pedido 15 — Textos do prompt por idioma

**Por que não agora.** Muda todo prompt de todos os hosts, e não temos evidência de que cabeçalho em inglês piora a resposta.

**O que já dá para fazer:**

- **Instruções de ferramentas.** São um texto versionado por symbiota. `kernel.harness_versions.create_version(symbiote_id, "tool_instructions", texto)` troca o texto ativo daquele symbiota. O mesmo vale para `injection_stagnation` e `injection_circuit_breaker`.
- **Descrições das ferramentas de vocês.** Já são de vocês.
- **O que o sonho escreve.** Segue o campo `language` da persona.
- **Reflexão e resumo.** Escrevem no idioma da conversa.

Continuam em inglês os cabeçalhos de seção (`## Persona`, `## Relevant Memories`) e as descrições das ferramentas prontas. Se vocês medirem um caso em que isso piora a resposta, reabrimos com esse caso.

## 4. O que muda ao atualizar

Toda opção nova nasce com o comportamento anterior. O banco ganha as migrações 13 a 15 (PostgreSQL 7 a 9), aplicadas ao subir. O que vocês podem notar:

- **`kernel._llm` e `kernel._evolver_llm`.** Passam a ser um medidor em volta do adapter recebido. `.wrapped` devolve o adapter, e os atributos dele continuam acessíveis pelo medidor. Se algum código de vocês compara a identidade desse objeto, ajustem.
- **Resultados de ferramenta.** `ToolCallResult` e os dicionários de `tool_results` têm a chave nova `pending_approval_id`.
- **Nota final.** A avaliação dada antes de encerrar passa a valer; a reaplicação de vocês vira redundante, não errada.
- **`prune()`.** Devolve mais uma chave, `llm_usage`.
- **Busca na web.** O mecanismo deixou de ser fixo. Os nomes que existiam (`brave`, `symgateway_objective`, `firecrawl`) continuam valendo; agora dá para passar um provedor próprio ou usar `brave_api` e `searxng`, que não passam pelo SymGateway.
- **Persona.** A forma como a engine lê a persona não mudou, então o embrulho atual de vocês continua funcionando até migrarem.

## 5. O que achamos no caminho

Corrigimos três defeitos que vocês vinham contornando e um que ninguém tinha relatado:

- a nota do dono perdida ao encerrar (pedido 10);
- a promoção automática de procedimentos sem caminho para disparar (pedido 7);
- o histórico do widget com adapter definido depois (pedido 6);
- a sessão avaliada antes de encerrar nunca contava para a nota da versão do harness.

Além disso, os adapters `ForgeLLMAdapter` e `SymGatewayLLMAdapter` perdiam os tokens das respostas em texto puro, e agora informam; e o `SymGatewayLLMAdapter` lia nomes de campo que o gateway não usa, então os tokens de entrada e de saída das chamadas pelo gateway vinham vazios.
