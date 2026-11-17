# Plano de implementação — pedidos do SymbiOS à engine

Data: 2026-11-17 · Base: `main` em `268a3ce` (versão 0.8.9) · Origem: pedido da equipe do SymbiOS com 15 itens (`P-1` a `P-15`, na ordem em que vieram)

## 1. Objetivo

Atender, na engine, tudo o que serve a qualquer host do Symbiote. Fechar com um relatório para a equipe do SymbiOS que diga o que entrou, o que foi recusado, por quê, e como fazer do lado deles o que ficou de fora.

Dois critérios valem para todas as tarefas:

1. **Sem regra de produto.** A engine ganha o mecanismo; conceitos como "dono", "visitante", "rascunho" e "publicado" ficam no host.
2. **Sem depender do SymGateway.** Tudo tem que funcionar com chave direta de um provedor. O que for específico do gateway fica em adapter opcional.

## 2. O que entra, o que não entra

| Pedido | Decisão | Tarefas |
|---|---|---|
| P-1 Aprovação adiada | Entra | TK-30 a TK-35 |
| P-2 Contexto e tokens por chamada ao modelo | Entra, ampliando o B-36 | TK-20 a TK-23 |
| P-3 Versão do symbiota por turno | **Não entra** | relatório |
| P-4 Saber que está dentro de um spawn | Entra | TK-10 |
| P-5 Aviso de fim do sonho | Entra o aviso; o comentário do dono no sonho não | TK-11 |
| P-6 Widget no pacote e defeito do histórico | Entra | TK-03, TK-16 |
| P-7 Procedimentos em teste | Entra | TK-02 |
| P-8 Objectives do SymGateway | Entra só dentro do adapter do SymGateway | TK-42 |
| P-9 Resumo ao encerrar | Entra | TK-13 |
| P-10 Nota automática e do dono | Entra a nota do dono e o registro do turno sem ferramentas; a nota 0,8 não muda | TK-01, TK-22 |
| P-11 Reativar memória | Entra | TK-04 |
| P-12 Modelo do sonho por symbiota | Entra | TK-12 |
| P-13 Conhecimento sob demanda por item | Entra | TK-15 |
| P-14 Ler OpenAPI já baixado | Entra a conversão em ferramentas; o montador de chamada HTTP não | TK-14 |
| P-15 Textos do prompt por idioma | **Não entra agora** | relatório |

Fora do pedido, mas dentro do segundo critério: TK-40 e TK-41 tiram o acoplamento com o SymGateway que achei no código.

## 3. Decisões que são suas

Sigo com a opção recomendada em cada uma, salvo aviso seu.

| # | Decisão | Recomendo | Motivo |
|---|---|---|---|
| D-1 | Na aprovação adiada, os parâmetros da chamada ficam gravados como vieram ou passam pelo redator? | Gravar como vieram enquanto o pedido está pendente; apagar ao resolver ou expirar; as listagens mostram a versão redigida | Para executar depois, a engine precisa dos parâmetros reais. É a única exceção à regra de que nada vai para o banco sem redação, e só existe para quem usar o adiamento. |
| D-2 | Procedimentos em teste aparecem para o modelo por padrão? | Não. Opção por symbiota, desligada | Ligar por padrão faria procedimentos escritos pelo agente e ainda não revisados passarem a influenciar as respostas de todos os clientes. |
| D-3 | O resumo com modelo vira o padrão? | Não. Padrão continua o de hoje | Resumo com modelo é uma chamada a mais por sessão encerrada; cada cliente decide se paga. |
| D-4 | A engine calcula custo em dinheiro? | Não. Grava tokens, provedor e modelo; o host aplica o preço | Preço muda e varia por contrato. O "budget máximo por request" do B-36 fica fora deste plano e o B-36 segue aberto só nessa parte. |
| D-5 | Qual mecanismo de busca a engine usa? | Nenhum fixo. O host escolhe: passa um provedor dele ou o nome de um provedor registrado. Brave é só um dos registrados | Decidido pelo operador em 2026-11-17: Brave não pode ficar preso no código; pode ser DuckDuckGo ou qualquer outro. Os nomes que já existem continuam valendo para não quebrar quem usa. |
| D-6 | Uma release por fase? | Não. Uma release só, no fim, com tudo | Decidido pelo operador em 2026-11-17. |

D-1 a D-4 foram aceitas pelo operador em 2026-11-17 como recomendadas.

## 4. Regras de trabalho

1. Um commit por tarefa, no padrão do `git log`, citando o `B-nn`. Sem push e sem release: quem roda o `/push` é você.
2. Edição que preserva os bytes nos arquivos de fim de linha misto; conferir `git diff --stat` antes de cada commit.
3. Cada tarefa traz teste que falha antes e passa depois. Suíte em SQLite e em PostgreSQL (`scripts/test-postgres.sh`), `ruff` e `mypy` limpos.
4. Toda coluna nova entra nas duas migrações (SQLite a partir da 13, PostgreSQL a partir da 7) e passa no teste de paridade de schema.
5. **Compatibilidade:** toda opção nova nasce com o comportamento de hoje. Nenhum método novo é obrigatório nas portas (`LLMPort`, `MemoryPort`). Callback que hoje devolve `bool` continua valendo.
6. Nada fora do adapter do SymGateway e dos backends de busca nomeados pode importar ou exigir o gateway.
7. Linha de base a manter: 2697 testes em SQLite, 2625 em PostgreSQL.

## 5. Tarefas

### Fase 0 — Preparação

| TK | Tarefa |
|---|---|
| TK-00 | Registrar no `BACKLOG.md` os itens B-80 a B-93 (um por tarefa ou grupo, ver seção 6) e ampliar o B-36 com a atribuição por chamada. |

### Fase 1 — Defeitos que atingem todo cliente

| TK | Pedido | O que fazer | Onde | Pronto quando |
|---|---|---|---|---|
| TK-01 | P-10 | Ao encerrar, a nota final combina a nota automática com a nota do host que já existir, em vez de sobrescrevê-la. | `core/kernel.py` (`_persist_score`, `_write_score`) | `report_feedback` seguido de `close_session` mantém a nota do host na nota final; sessão reaberta e encerrada de novo também. |
| TK-02 | P-7 | Opção por symbiota para listar os procedimentos em teste, marcados como tal, e para `skill_view` lê-los contando o uso. Com ela ligada, `skill_auto_promote_threshold` passa a funcionar. Documentar que, desligada, a promoção só acontece pelo host. | `skills/loader.py`, `skills/tool.py`, `core/context.py`, `core/models.py`, migração | Com a opção ligada, o N-ésimo `skill_view` promove; desligada, tudo igual a hoje (D-2). Biblioteca cheia continua sem promover. |
| TK-03 | P-6 | No widget, carregar o histórico quando um adapter de verdade substitui o `NoopAdapter`, e expor `reload()`. Regerar o `dist`. | `symbiote-ui/src/symbiote-chat.js` | Teste em navegador (Playwright do `.venv`): modo inline, adapter definido depois de conectar, histórico aparece. |
| TK-04 | P-11 | `MemoryStore.reactivate(memory_id, ...)`, com as mesmas regras de partição do `deactivate`. Rota e comando onde já existe o de desativar. | `memory/store.py`, `core/ports.py` (doc), API, CLI | Memória desativada volta a aparecer em busca e contexto; reativar a de outro usuário final é recusado. |

### Fase 2 — Pontos de extensão públicos

| TK | Pedido | O que fazer | Onde | Pronto quando |
|---|---|---|---|---|
| TK-10 | P-4 | `current_spawn()` público: devolve a sessão e o symbiota de quem pediu, a sessão e o symbiota do colega e a profundidade, ou `None` fora de um spawn. | `runners/subagent.py` | Ganchos e ferramentas chamados dentro de um spawn enxergam os dados; fora dele, `None`. `_spawn_depth` continua existindo. |
| TK-11 | P-5 | `kernel.set_dream_callback(fn)`, chamado ao fim de todo ciclo com o symbiota, o relatório e o erro, se houve; e `kernel.wait_dream(symbiote_id, timeout)`. | `core/kernel.py` | Callback dispara no sonho automático e no pedido; exceção no callback não derruba nada; `wait_dream` devolve se terminou. |
| TK-12 | P-12 | Opção por symbiota para o sonho usar o modelo principal em vez do auxiliar. | `core/models.py`, `core/kernel.py` (`_build_dream_engine`), migração | Com a opção, as fases do sonho chamam o modelo principal; padrão igual a hoje. |
| TK-13 | P-9 | Modo do resumo de sessão por symbiota: o de hoje (padrão), com o modelo auxiliar, ou nenhum. Reaproveitar o prompt de resumo de `core/_review_prompts.py`. Falha do modelo cai no modo de hoje. | `core/reflection.py`, `core/session.py`, `core/models.py`, migração | Os três modos testados; sessão de usuário final resumida só com o que é dela. |
| TK-14 | P-14 | Função pública que converte uma descrição OpenAPI já em mãos (um `dict`) em ferramentas descobertas, pelo mesmo caminho de `discover_tools`. | `discovery/service.py`, `core/kernel.py` | Mesma descrição dá as mesmas ferramentas por URL e por `dict`; a execução segue pelo gateway de ferramentas. |
| TK-15 | P-13 | Marca por item de conhecimento: fora da busca automática, mas alcançável por `search_knowledge`. | `core/models.py`, `knowledge/service.py`, `core/context.py`, migração | Item marcado não entra no contexto montado e aparece em `search_knowledge`. |
| TK-16 | P-6 | Levar `symbiote-chat.js` no pacote Python, com `symbiote.ui.widget_path()` e a versão do widget. Teste que acusa diferença entre o arquivo do pacote e o `dist`. | `src/symbiote/ui/`, `pyproject.toml`, `symbiote-ui/build.sh` | O wheel instalado devolve um caminho que existe; a imagem Docker de qualidade passa. |

### Fase 3 — Uso por chamada ao modelo (B-36 ampliado)

| TK | Pedido | O que fazer | Onde | Pronto quando |
|---|---|---|---|---|
| TK-20 | P-2 | Contexto público `current_llm_call()` com symbiota, sessão (quando houver) e finalidade: `turn`, `subagent`, `reflection`, `skill_review`, `dream:<fase>`, `harness`, `memory_consolidation`, `long_run:<etapa>`, `tool_resolution`. Definido em todos os pontos que chamam o modelo. Por contexto, não por assinatura: adapter de host antigo segue funcionando. | os 11 pontos de `llm.complete`, mais os caminhos de streaming, em `runners/`, `core/`, `dream/`, `harness/`, `memory/`, `environment/` | Um adapter de teste lê o contexto em cada finalidade, inclusive nas linhas de execução de fundo. |
| TK-21 | P-2 | Tabela `llm_usage`: uma linha por chamada, com symbiota, sessão, finalidade, provedor, modelo e tokens. Os tokens vêm do `usage` que o adapter devolve; adapter que devolve só texto gera linha sem tokens. Callback opcional `kernel.set_usage_callback(fn)` para o host que tem contabilidade própria. | `core/` (novo módulo), migração, `core/kernel.py` | Funciona igual com `ForgeLLMAdapter` de chave direta e com o do SymGateway; nenhum conteúdo de conversa na tabela. |
| TK-22 | P-2, P-10 | Totais de tokens e de chamadas no registro do turno e no `TurnOutcome`. Turno sem ferramentas também grava registro. | `runners/base.py`, `runners/chat.py`, `core/kernel.py`, `core/turn_result.py` | Turno sem ferramentas aparece no registro com os tokens; a nota automática dele continua 0,8. |
| TK-23 | P-2 | Leitura: `inspector.usage(...)` com filtros por symbiota, período, finalidade, sessão e usuário final, e soma agrupada. Rota no console. | `core/inspector.py`, `api/routes/console.py` | Somas batem com as linhas; `purge_subject` mantém a contagem e não deixa nada que identifique a pessoa além do id opaco da sessão. |

### Fase 4 — Aprovação adiada

| TK | Pedido | O que fazer | Onde | Pronto quando |
|---|---|---|---|---|
| TK-30 | P-1 | `ApprovalDecision` com `approve()`, `deny(reason)` e `defer(reason, message, expires_in)`. O callback de aprovação pode devolver isso ou o `bool` de hoje. | `environment/policies.py`, `runners/chat.py` (`_check_approval`), `runners/long_run.py` | `bool` e `ApprovalDecision` dão o mesmo resultado nos casos de hoje. |
| TK-31 | P-1 | Tabela `pending_approvals`: symbiota, sessão, usuário final, ferramenta, parâmetros, risco, motivo, estado (`pending`, `approved`, `denied`, `expired`, `executed`, `failed`), prazos, quem resolveu, nota e resultado. | migrações, novo módulo em `core/` | Pedido sobrevive a reinício do kernel; D-1 aplicada. |
| TK-32 | P-1 | No turno: a chamada adiada não roda, o modelo recebe uma mensagem configurável dizendo que ficou pendente, o turno termina normalmente e o `TurnOutcome` traz os pedidos criados. Vale para o caminho síncrono, o assíncrono, o `long_run` e dentro de spawn. | `runners/chat.py`, `core/turn_result.py` | Chamadas da mesma rodada que não foram adiadas rodam normalmente. |
| TK-33 | P-1 | `kernel.resolve_pending(id, approved, note, resolved_by)`: confere de novo a política e o veto do host, roda a ferramenta no contexto original (mesma sessão, mesmo usuário final), liga a linha de auditoria ao pedido e grava o resultado. Resolver duas vezes é erro; duas resoluções simultâneas, só uma vence. | `core/kernel.py`, `environment/tools.py`, `environment/policies.py` | Aprovado executa uma vez; negado não executa; ferramenta removida ou política mudada no intervalo resulta em `failed` com o motivo. |
| TK-34 | P-1 | Entrega do resultado: o próximo turno do mesmo usuário final (ou da mesma sessão, quando não há usuário final) recebe no contexto os pedidos resolvidos ainda não entregues, e eles são marcados como entregues. | `core/context.py` | O modelo vê o resultado na conversa seguinte, na mesma sessão ou em outra; outro usuário final nunca vê. |
| TK-35 | P-1 | `inspector.list_pending(...)`, expiração por prazo (ao listar, ao resolver e ao subir o kernel), `purge_subject` e exclusão do symbiota levam os pedidos junto. Rotas e SDK para listar e resolver. | `core/inspector.py`, `core/kernel.py`, API, SDK | Pedido vencido vira `expired` e não executa. |

### Fase 5 — Independência do SymGateway

| TK | Pedido | O que fazer | Onde | Pronto quando |
|---|---|---|---|---|
| TK-40 | — | A busca deixa de ter mecanismo fixo (D-5). `search_backend`, `extract_backend` e `crawl_backend` aceitam um objeto do host que implemente o protocolo de provedor, ou o nome de um provedor registrado por `register_search_provider(nome, fábrica)` (idem para crawl e extração). Os que já existem viram entradas desse registro. Entram dois provedores diretos como exemplo, sem gateway: a API do Brave com chave própria e um SearXNG por endereço. | `browser/register.py`, `browser/config.py`, `browser/search/providers/` | `web_search` e crawl funcionam sem nenhuma variável do SymGateway; um provedor escrito pelo host funciona sem tocar na engine. |
| TK-41 | — | `ForgeLLMAdapter` deixa de enviar `project_slug: "symbiote"` em toda chamada; vira parâmetro opcional. Antes de mudar, conferir se o staging depende dele e, se depender, passá-lo pela configuração do servidor. | `adapters/llm/forge.py`, `api/` | Chamada com chave direta não leva o campo; staging continua roteando. |
| TK-42 | P-8 | No `SymGatewayLLMAdapter`: aceitar `objective` no lugar de `model`, listar os objectives da chave e devolver o objective roteado na identidade do modelo. Ler o contrato em `~/dev/projects/sym_gateway` antes. | `adapters/llm/symgateway.py` | Nada muda em `LLMPort` nem no kernel. |
| TK-43 | — | Teste que roda um turno, uma reflexão e um sonho com o módulo do SymGateway bloqueado para importação. Uma linha em `docs/HOST_INTEGRATION.md` dizendo que o gateway é opcional. | `tests/unit/` | O teste falha se alguém amarrar o núcleo ao gateway. |

### Fase 6 — Fechamento

| TK | Tarefa |
|---|---|
| TK-50 | Documentação: `docs/HOST_INTEGRATION.md`, `docs/arch-02-execution.md`, `docs/arch-03-memory.md`, `docs/arch-04-dream-mode.md`, `symbiote-ui/README.md`, `project/docs/SPEC.md`, `CHANGELOG.md`; fechar os itens no `BACKLOG.md`. |
| TK-51 | Gate de qualidade em Docker (`docker build --target quality .`) e suíte nos dois bancos. |
| TK-52 | Relatório para a equipe do SymbiOS (seção 7). |

## 6. Itens de backlog

| Item | Assunto | Tarefas |
|---|---|---|
| B-36 (ampliado) | Uso por chamada ao modelo | TK-20 a TK-23 |
| B-80 | Nota do host perdida ao encerrar | TK-01 |
| B-81 | Procedimentos em teste visíveis por opção | TK-02 |
| B-82 | Widget: histórico com adapter tardio; widget no pacote | TK-03, TK-16 |
| B-83 | Reativar memória | TK-04 |
| B-84 | Contexto de spawn público | TK-10 |
| B-85 | Aviso e espera do fim do sonho | TK-11 |
| B-86 | Modelo do sonho por symbiota | TK-12 |
| B-87 | Modo do resumo de sessão | TK-13 |
| B-88 | OpenAPI a partir de descrição em mãos | TK-14 |
| B-89 | Conhecimento sob demanda por item | TK-15 |
| B-90 | Aprovação adiada | TK-30 a TK-35 |
| B-91 | Busca e crawl sem SymGateway | TK-40 |
| B-92 | `project_slug` fixo no adapter Forge | TK-41 |
| B-93 | Objectives no adapter do SymGateway | TK-42 |

## 7. Relatório final para o SymbiOS

Escrito na TK-52, em `reports/`, depois de tudo implementado, para citar a API como ela ficou. Conteúdo:

1. **O que entrou**, por pedido: a API final, a versão em que saiu e o que eles podem apagar do lado deles (a tabela própria de pedidos, a leitura de `_spawn_depth` e de `_dream_threads`, a cópia do widget, o `skill_trial_view`, a reaplicação da nota).
2. **O que foi recusado**, com o motivo e o caminho do lado deles. Cada orientação será conferida contra a API pública antes de ir para o relatório:

| Recusado | Motivo | Caminho a orientar |
|---|---|---|
| P-3, troca de persona, configuração e ferramentas por turno | "Rascunho" e "publicado" são conceitos do produto, e a troca por turno passa por fora da política de ferramentas | Dois symbiotas, um de rascunho e um publicado; publicar é copiar persona, configuração, conhecimento e procedimentos do primeiro para o segundo. Tira a dependência de `kernel._identity`. |
| P-5, o sonho ler o comentário do dono | O comentário é dado do produto | Rodar o passo deles no callback de fim de sonho (TK-11). |
| P-10, nota de turno sem ferramentas | Sem ferramentas não há sinal objetivo para medir; 0,8 é o valor neutro | Usar a nota do host, que passa a ser preservada (TK-01). |
| P-14, montador de chamada HTTP | É parte interna do gateway de ferramentas | Registrar a ferramenta e executar pelo gateway (TK-14). |
| P-15, textos do prompt por idioma | Mexe em todo prompt de todos os clientes, sem evidência de ganho | Fixar o idioma na persona; as descrições das ferramentas deles já são deles. Reabrimos com um caso medido. |
| P-7, arquivamento por prazo fora do sonho | O sonho é o lugar da manutenção | Agendar `kernel.dream()`. |

3. **O que achamos no caminho**: os defeitos corrigidos que eles vinham contornando e o que muda de comportamento para eles em cada versão.

## 8. Ordem e dependências

Fase 1 não depende de nada e pode sair primeiro. A Fase 4 usa o `current_spawn()` da TK-10 e o contexto de chamada da TK-20 para registrar de onde veio o pedido. A TK-42 é a última: é a única que só serve a quem usa o gateway.

## 9. Notas de execução

Executado em 2026-11-17, em 17 commits locais sobre `268a3ce` (sem push e sem release). Todas as tarefas TK-00 a TK-52 foram feitas. Suíte: 2812 testes em SQLite; PostgreSQL e gate de qualidade em Docker registrados no relatório de entrega.

Desvios do que estava escrito acima:

- **TK-04.** `reactivate` ficou só no `MemoryStore`: não existe rota nem comando de desativar para acompanhar.
- **TK-22.** Os totais de tokens por turno foram para `turn_contexts`, que já tinha uma linha por turno com ou sem ferramentas, e não para `execution_traces`. A nota automática do turno sem ferramentas não mudou.
- **TK-34.** O resultado de um pedido resolvido chega ao modelo por um bloco de contexto no turno seguinte, não por mensagem gravada na sessão: assim o visitante que volta em outra sessão também é avisado.
- **TK-40.** Sem mecanismo padrão: `search_routing` sem provedor para `web_search` passou a ser erro (antes caía no Brave pelo SymGateway).
- **TK-41.** O `project_slug` continua `"symbiote"` por padrão quando o provider é `symgateway`, para não mudar o roteamento de quem já usa; deixou de ser enviado aos outros provedores e ganhou parâmetro.
- **Medição (TK-21).** `kernel._llm` e `kernel._evolver_llm` passaram a ser um medidor em volta do adapter; `.wrapped` devolve o adapter.

Feito além do plano, por ter aparecido no caminho:

- a sessão avaliada pelo host antes de encerrar nunca contava para o score da versão do harness (corrigido junto com o B-80);
- `ForgeLLMAdapter` e `SymGatewayLLMAdapter` perdiam os tokens das respostas em texto puro;
- duas colunas de texto da migração 7 do PostgreSQL estavam sem `COLLATE "C"` (corrigido antes de qualquer release).

Adendo de 2026-11-17 (pedido 16, tokens de cache), avaliado como genérico e feito na mesma migração do `llm_usage`: `cache_read_tokens`, `cache_write_tokens`, `cache_write_5m_tokens`, `cache_write_1h_tokens` e `reasoning_tokens` no registro, na tabela, no `report_usage()` e nas somas do inspector; convenção única (`prompt_tokens` é a entrada inteira, com o cache) e `usage_from_provider()` para converter o que cada provedor devolve. O `SymGatewayLLMAdapter` preenche tudo, e nele foi corrigida a leitura de `input_tokens` / `output_tokens`. O `ForgeLLMAdapter` só preenche o que o `forge_llm` repassa, e hoje ele não repassa o cache: virou o B-94, de outro repositório.

Em aberto: o "budget máximo por request" do B-36 e o B-94. O relatório para o SymbiOS está em `reports/2026-11-17_resposta-symbios.md`.
