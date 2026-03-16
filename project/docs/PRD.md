# PRD — Product Requirements Document

> Projeto: Symbiote
> Autor: Symlabs
> Data: 2026-03-16
> Status: draft

---

## 1. Hipótese

### 1.1 Contexto

Os frameworks de agentes de IA atuais (LangChain, CrewAI, AutoGen, etc.) modelam a unidade de software como tarefa: um loop efêmero de `input → LLM → tools → output`. Quando se tenta construir assistentes que operam continuamente — copilotos de projeto, assistentes pessoais, agentes de domínio —, o modelo de tarefa se esgota. O resultado: agentes amnésicos, histórico bruto despejado no prompt, baixa auditabilidade e nenhuma continuidade real entre sessões.

### 1.2 Sinal de Mercado

O mercado está migrando de chatbots para agentes persistentes (Devin, Claude Code, Cursor), mas todos tratam persistência como feature lateral, não como arquitetura central. Nenhum framework open-source modela explicitamente identidade + memória em camadas + workspace + environment + processo + reflexão como componentes de primeira classe de um kernel. O gap é análogo ao salto de scripts CGI (stateless) para application servers (stateful) nos anos 2000.

### 1.3 Oportunidade

Construir o kernel — não o framework completo, não a plataforma — que resolve a camada mais fundamental: como instanciar, persistir e operar uma entidade cognitiva ao longo do tempo. Se o kernel estiver certo, sustenta múltiplos produtos por cima. Primeiro runtime Python open-source projetado para entidades persistentes, local-first, SQLite + filesystem, extensível via adapters.

### 1.4 Grau de Certeza

Médio-alto (60-70%). Problema validado internamente e pela comunidade. Risco principal: escopo — o conceito é amplo e pode virar plataforma antes de ser kernel.

---

## 2. Visão

### 2.1 Intenção Central

Oferecer um kernel Python para construir entidades cognitivas persistentes que lembram, operam sobre artefatos reais, aprendem com a relação e preservam contexto de forma seletiva.

### 2.2 Problema

Frameworks de agentes modelam "tarefa", não "entidade". O resultado: agentes amnésicos, contexto despejado sem curadoria, zero continuidade entre sessões, workspace tratado como afterthought e nenhuma separação entre memória e conhecimento.

### 2.3 Público-Alvo

- **Primário**: desenvolvedores Python que constroem assistentes e agentes persistentes e precisam de um runtime sólido como base.
- **Secundário**: equipes técnicas que embarcam IA em produtos internos (ERP, backoffice, plataformas SaaS).
- **Uso interno (Symlabs)**: base para os symbiotas do processo Fast Track e futuros produtos.

### 2.4 Diferencial Estratégico

1. Unidade de abstração é a entidade, não a tarefa.
2. Memória em camadas com curadoria — working, session, long-term relational, semantic recall.
3. Workspace e environment são componentes de primeira classe do kernel.
4. Local-first — SQLite + filesystem, sem dependência de vector DB.
5. Reflexão como capacidade obrigatória.

---

## 3. Modelo de Negócio

### 3.1 Monetização

Open-source puro. Valor gerado via adoção, ecossistema e uso como base tecnológica para produtos Symlabs.

### 3.2 Mercado

- **TAM**: desenvolvedores Python que constroem agentes/assistentes de IA (~2M globalmente, crescendo).
- **SAM**: desenvolvedores insatisfeitos com frameworks task-based que precisam de persistência real (~200K).
- **SOM**: early adopters técnicos que construiriam sobre o kernel nos primeiros 6 meses (~5K).

---

## 4. Métricas de Sucesso

| Métrica | Meta | Prazo |
|---------|------|-------|
| Cenário ponta-a-ponta funcional (criar simbióta → sessão → chat → work → fechar → reabrir com contexto) | 100% passando | MVP |
| Estado sobrevive a restart | 100% | MVP |
| Contexto montado dentro de orçamento configurável | >= 95% das interações | MVP |
| Cobertura de testes nos fluxos críticos | >= 85% | MVP |
| Runtime funciona sem vector DB externo | 100% | MVP |

---

## 5. User Stories + Acceptance Criteria

> Cada User Story tem ACs no formato Given/When/Then.

### US-01: Criar e persistir identidade de simbióta
**Como** desenvolvedor, **quero** criar um simbióta com identidade persistente (nome, papel, persona, constraints), **para** que ele mantenha sua identidade entre restarts do runtime.

**Acceptance Criteria:**
- **AC-01**: Given um kernel configurado, When eu chamo `create_symbiote(name, role, persona)`, Then um simbióta é criado com id único e persistido em SQLite.
- **AC-02**: Given um simbióta existente, When o runtime é reiniciado, Then a identidade (nome, role, persona, constraints) é recuperada intacta.
- **AC-03**: Given um simbióta existente, When eu atualizo a persona, Then a alteração é persistida e a versão anterior fica auditável.
- **AC-04**: Given um simbióta com persona definida, When o contexto é montado para uma interação, Then a persona é incluída no contexto.

### US-02: Gerenciar sessões com ciclo de vida completo
**Como** desenvolvedor, **quero** criar, retomar e encerrar sessões explícitas com objetivo, mensagens, decisões e artefatos, **para** manter continuidade de trabalho entre interações.

**Acceptance Criteria:**
- **AC-01**: Given um simbióta existente, When eu chamo `start_session(symbiote_id, goal)`, Then uma sessão é criada com status `active` e timestamp.
- **AC-02**: Given uma sessão ativa, When eu envio mensagens, Then elas são registradas com role, content e timestamp.
- **AC-03**: Given uma sessão ativa, When eu chamo `close_session(session_id)`, Then o sistema gera um summary da sessão e persiste decisões e artefatos vinculados.
- **AC-04**: Given uma sessão encerrada, When eu chamo `resume_session(session_id)`, Then a sessão é reaberta com contexto anterior recuperado.

### US-03: Operar sobre workspaces e workdir reais
**Como** desenvolvedor, **quero** associar workspaces persistentes a um simbióta e definir um workdir ativo, **para** que o simbióta opere sobre arquivos e artefatos reais no filesystem.

**Acceptance Criteria:**
- **AC-01**: Given um simbióta, When eu crio um workspace com `root_path` apontando para um diretório real, Then o workspace é persistido e associado ao simbióta.
- **AC-02**: Given uma sessão ativa com workspace, When o simbióta gera um artefato, Then o artefato é registrado (path, type, description) e existe no filesystem.
- **AC-03**: Given uma sessão encerrada e reaberta, When eu consulto o workdir, Then ele aponta para o mesmo diretório da sessão anterior.

### US-04: Configurar environment com tools e policies
**Como** desenvolvedor, **quero** definir o environment operacional de um simbióta (tools habilitadas, serviços, policies de autorização), **para** que ações respeitem limites configurados.

**Acceptance Criteria:**
- **AC-01**: Given um environment configurado com lista de tools habilitadas, When o simbióta tenta executar uma tool habilitada, Then a execução é permitida.
- **AC-02**: Given um environment com policy restritiva, When o simbióta tenta executar uma tool não autorizada, Then a execução é bloqueada e o bloqueio é registrado.
- **AC-03**: Given dois workspaces do mesmo simbióta, When eu configuro environments diferentes para cada um, Then cada workspace respeita suas próprias policies.

### US-05: Consultar knowledge separado de memória
**Como** desenvolvedor, **quero** registrar fontes de conhecimento (documentos, notas, bases) separadas da memória relacional, **para** que o simbióta diferencie "o que sei do domínio" de "o que lembro da relação".

**Acceptance Criteria:**
- **AC-01**: Given um simbióta com fontes de knowledge registradas, When eu consulto knowledge por tema, Then o sistema retorna entradas relevantes sem persistir a consulta como memória relacional.
- **AC-02**: Given o banco de dados, When eu inspeciono as tabelas, Then knowledge e memory estão em estruturas lógicas separadas.
- **AC-03**: Given uma montagem de contexto, When knowledge e memória relacional são candidatas, Then ambas participam do ranking mas são identificáveis por origem.

### US-06: Persistir memória em quatro camadas
**Como** desenvolvedor, **quero** que o simbióta mantenha memória em quatro camadas (working, session, long-term relational, semantic recall), **para** que o sistema gerencie contexto com granularidade e relevância.

**Acceptance Criteria:**
- **AC-01**: Given uma sessão ativa, When mensagens são trocadas, Then a working memory é atualizada com últimas mensagens, objetivo atual e decisões recentes.
- **AC-02**: Given uma sessão encerrada, When o summary é gerado, Then a session memory é persistida com resumo, decisões, artefatos e próximos passos.
- **AC-03**: Given uma reflexão executada, When fatos duráveis são detectados (preferência, procedimento, restrição), Then eles são persistidos como long-term relational memory com type, scope, tags, importance e source.
- **AC-04**: Given o runtime sem vector DB externo, When memórias são buscadas por relevância, Then o sistema retorna resultados usando ranking por escopo, importância, recência e tags.
- **AC-05**: Given entradas de memória, When eu inspeciono os campos, Then cada entrada tem `type`, `scope`, `tags`, `importance`, `source`, `created_at` e `last_used_at`.

### US-07: Montar contexto de forma seletiva e orçamentada
**Como** desenvolvedor, **quero** que o sistema monte contexto seletivamente dentro de um orçamento configurável, **para** que o prompt nunca receba histórico bruto e respeite limites de tokens.

**Acceptance Criteria:**
- **AC-01**: Given um orçamento de contexto configurado (ex: 4000 tokens), When o contexto é montado, Then o resultado final não ultrapassa o orçamento.
- **AC-02**: Given memórias de diferentes camadas disponíveis, When o contexto é montado, Then working memory tem prioridade, seguida por relevância relacional, proximidade de sessão e semantic recall.
- **AC-03**: Given um contexto montado, When eu chamo uma função de inspeção, Then consigo ver exatamente quais memórias, knowledge e persona foram incluídas e quais foram descartadas.

### US-08: Executar ações via runners especializados
**Como** desenvolvedor, **quero** que o kernel selecione e execute runners adequados (chat, task, tool, process) conforme a intenção, **para** que diferentes tipos de ação tenham tratamento especializado.

**Acceptance Criteria:**
- **AC-01**: Given uma mensagem de chat, When o kernel processa, Then o ChatRunner é selecionado e produz uma resposta contextual.
- **AC-02**: Given uma intenção de trabalho sobre workspace, When o kernel processa, Then o TaskRunner ou ToolRunner é selecionado conforme a granularidade.
- **AC-03**: Given uma interface de Runner, When eu implemento um novo runner, Then ele pode ser registrado no kernel sem alterar o core.

### US-09: Executar tools com policy gate
**Como** desenvolvedor, **quero** que tools de filesystem, execução de comandos e busca operem sob policy gate, **para** que nenhuma ação sensível rode sem autorização e todas fiquem auditáveis.

**Acceptance Criteria:**
- **AC-01**: Given uma tool de escrita habilitada por policy, When o simbióta a executa, Then o artefato é criado e a execução é registrada no log de auditoria.
- **AC-02**: Given uma tool de execução de comandos desabilitada por policy, When o simbióta tenta executá-la, Then a execução é bloqueada com mensagem explicativa.
- **AC-03**: Given um log de auditoria, When eu inspeciono, Then cada execução de tool tem timestamp, tool_id, parâmetros e resultado (success/blocked).

### US-10: Executar processos declarativos com steps
**Como** desenvolvedor, **quero** definir e executar processos declarativos (chat, research, artifact generation, review, workspace task), **para** que fluxos de trabalho complexos tenham steps, checkpoints e reflexão.

**Acceptance Criteria:**
- **AC-01**: Given uma intenção mapeada para um processo, When o kernel seleciona o processo, Then o ProcessRunner executa os steps na ordem definida.
- **AC-02**: Given um processo em execução, When um step é concluído, Then o checkpoint é registrado e o output do step fica disponível para o próximo.
- **AC-03**: Given um processo concluído, When a reflexão final roda, Then aprendizados podem ser persistidos como memória.

### US-11: Usar as seis capacidades do simbióta
**Como** desenvolvedor, **quero** que o simbióta exponha explicitamente Learn, Teach, Chat, Work, Show e Reflect como operações, **para** que cada tipo de interação tenha comportamento verificável.

**Acceptance Criteria:**
- **AC-01**: Given uma interação de chat, When o simbióta responde, Then a capacidade Chat é exercida com contexto seletivo e memória recuperada.
- **AC-02**: Given uma solicitação de trabalho, When o simbióta age sobre o workspace, Then a capacidade Work é exercida e artefatos são produzidos.
- **AC-03**: Given o fechamento de sessão, When Reflect é executado, Then o simbióta gera summary, extrai fatos duráveis e descarta ruído.
- **AC-04**: Given fatos duráveis detectados pelo Reflect, When Learn é executado, Then entradas estruturadas de memória são persistidas com type, tags e importance.
- **AC-05**: Given uma solicitação de explicação, When Teach é exercido, Then a resposta é estruturada e usa knowledge + memória relacional como contexto.
- **AC-06**: Given uma solicitação de exibição, When Show é exercido, Then o resultado é formatado (Markdown, diff, relatório) e entregue de forma legível.

### US-12: Consolidar memória via reflexão
**Como** desenvolvedor, **quero** que o sistema execute reflexão ao encerrar sessões e após tarefas relevantes, **para** que memórias úteis sejam consolidadas e ruído descartado.

**Acceptance Criteria:**
- **AC-01**: Given uma sessão encerrada, When a reflexão roda, Then um summary é gerado.
- **AC-02**: Given a reflexão executada, When preferências/procedimentos/restrições são detectados, Then candidatos a memória são gerados e persistidos.
- **AC-03**: Given uma interação descartável (ruído, brainstorming cru), When a reflexão roda, Then nenhuma memória de longo prazo é gerada.

### US-13: Exportar estado em formato legível
**Como** operador, **quero** exportar sessões, memórias e decisões em Markdown legível, **para** auditar o estado do simbióta sem consultar internals.

**Acceptance Criteria:**
- **AC-01**: Given uma sessão encerrada, When eu solicito export, Then recebo um arquivo Markdown com summary, decisões e artefatos.
- **AC-02**: Given memórias de longo prazo, When eu solicito export, Then recebo um Markdown com entradas organizadas por tipo e escopo.
- **AC-03**: Given um log de decisões, When eu solicito export, Then recebo um Markdown com cada decisão, contexto e timestamp.

### US-14: Usar o simbióta via biblioteca, CLI e API HTTP
**Como** desenvolvedor, **quero** interagir com o simbióta via biblioteca Python embutida, CLI local e serviço HTTP, **para** usar o kernel no modo mais adequado ao meu caso.

**Acceptance Criteria:**
- **AC-01**: Given o pacote instalado via pip, When eu importo `from symbiote.core.kernel import SymbioteKernel` e crio um simbióta, Then consigo abrir sessão e trocar mensagens via código.
- **AC-02**: Given a CLI instalada, When eu rodo `symbiote session start --goal "..."`, Then uma sessão é criada e posso interagir via terminal.
- **AC-03**: Given o serviço HTTP rodando, When eu faço `POST /sessions` e `POST /sessions/{id}/messages`, Then recebo respostas contextuais via API.

---

## 6. Requisitos Não-Funcionais

| Requisito | Descrição | Prioridade |
|-----------|-----------|------------|
| Portabilidade | Rodar em macOS/Linux/Windows com Python 3.12+ e SQLite | P0 |
| Deploy simples | `pip install` + configuração local; Docker opcional | P0 |
| Baixo acoplamento | Sem dependência estrutural de LangChain ou equivalentes | P0 |
| Observabilidade | Logs estruturados (structlog), IDs de sessão/processo, trilha de execução | P1 |
| Determinismo relativo | Decisões operacionais críticas guiadas por regras, não só por texto livre do modelo | P1 |
| Segurança básica | Toda tool sensível passa por policy gate | P0 |
| Configurabilidade | Budget de contexto, providers, tools, paths, policies e processos configuráveis | P1 |

---

## 7. Restrições Técnicas + Decision Log

### 7.1 Restrições

- Python 3.12+
- SQLite como persistência principal (MVP)
- Filesystem para workspaces e artefatos
- Sem dependência obrigatória de banco vetorial externo
- Sem dependência de frameworks de agentes (LangChain, CrewAI, etc.)

### 7.2 Decision Log

| # | Decisão | Contexto | Alternativas Consideradas | Data |
|---|---------|----------|---------------------------|------|
| 1 | SQLite como fonte de verdade do MVP | Simplicidade operacional, portabilidade, zero config | Postgres (pesado para MVP), Redis (sem persistência durável), YAML files (sem query) | 2026-03-16 |
| 2 | Filesystem para workspaces e artefatos | Artefatos precisam ser acessíveis diretamente; DB para metadados | Tudo no DB (binários em blob), Object storage (infra externa) | 2026-03-16 |
| 3 | Markdown como camada de export, não de storage | Auditabilidade humana sem depender de tooling | Markdown como storage principal (sem query estruturada), JSON exports (menos legível) | 2026-03-16 |
| 4 | Semantic recall como interface sem vector DB | MVP funcional sem infra externa; interface pronta para plugar provider | Exigir ChromaDB/Pinecone (dependência pesada no MVP), Ignorar semantic recall (perde extensibilidade) | 2026-03-16 |
| 5 | Memory e Knowledge como domínios separados | Evitar confusão conceitual entre "o que sei" e "o que lembro" | Tabela única com flag (perde semântica), Apenas memory (knowledge vira memória) | 2026-03-16 |
| 6 | Entidade persistente como unidade de abstração | Diferencial central do produto; resolve o gap identificado | Task-based como os demais frameworks (sem diferencial) | 2026-03-16 |
| 7 | Reflect como capacidade obrigatória | Garantir consolidação de memória e descarte de ruído em toda sessão | Reflect opcional (memória inflada), Sem reflect (amnésia) | 2026-03-16 |
| 8 | Modo biblioteca + modo serviço | Atender tanto embedded quanto standalone; Python API + FastAPI | Apenas biblioteca (sem HTTP), Apenas serviço (sem embed) | 2026-03-16 |

---

## 8. Riscos e Mitigações

| Risco | Impacto | Probabilidade | Mitigação |
|-------|---------|---------------|-----------|
| Escopo excessivo — conceito amplo vira plataforma antes de ser kernel | Alto | Médio | Manter MVP no kernel; não construir UI/ecossistema |
| Memória mal calibrada — persistir demais, recuperar demais ou recuperar errado | Alto | Médio | Política de escrita clara, context budget e auditoria |
| Processo excessivamente "mágico" — tudo depende do modelo decidir | Médio | Médio | Processos declarativos simples e regras explícitas |
| Acoplamento forte com provider de LLM | Médio | Baixo | LLMAdapter fino e genérico |
| Confusão entre knowledge e memory | Médio | Médio | Serviços separados e tipos explícitos |

---

## 9. Fora de Escopo (v1)

- Multi-tenant completo
- ACL corporativa avançada
- Marketplace de simbiótas
- Interface visual rica (UI web completa)
- Voz e multimodalidade avançada
- Treinamento/fine-tuning do modelo
- Orquestração distribuída de múltiplos simbiótas
- Dependência obrigatória de banco vetorial externo
- Colaboração multiusuário concorrente
- Engine de agendamento complexo
- Sincronização em nuvem como dependência central

---

## 10. Value Tracks & Support Tracks

> Fluxos de valor mensuráveis que o usuário executa repetidamente via simbióta.
> Mapeados para ForgeBase Pulse via `forgepulse.value_tracks.yml`.

### Value Tracks

| Track ID | Descrição | Done = | KPIs |
|----------|-----------|--------|------|
| learn | Persistir conhecimento útil da relação e trabalho | Memória útil persistida (preferência, fato, procedimento, restrição) | memory_precision, learn_yield_per_session |
| teach | Explicar, orientar e devolver conhecimento estruturado | Resposta estruturada entregue com contexto relevante | teach_relevance_score, response_completeness |
| chat | Interação contextual contínua com memória seletiva | Resposta contextual entregue com contexto dentro do orçamento | context_budget_compliance, recall_hit_rate, response_latency_p95 |
| work | Agir sobre workspace real, produzir artefatos | Tarefa executada, artefatos produzidos e rastreados, tools sob policy | artifact_production_rate, policy_compliance_rate, task_completion_rate |
| show | Exibir resultados estruturados (diffs, relatórios, exports) | Resultado formatado e entregue em formato legível | export_format_validity, show_completeness |
| reflect | Consolidar aprendizados e decidir o que persistir | Summary gerado, memórias candidatas extraídas, ruído descartado | reflection_yield, summary_quality, memory_discard_ratio |

### Support Tracks

| Track ID | Sustenta | Descrição | KPIs |
|----------|----------|-----------|------|
| persistence_integrity | learn, reflect, chat | SQLite + filesystem como fonte de verdade; restart não corrompe estado | state_recovery_rate, data_integrity_score |
| context_assembly | chat, teach, work | Pipeline de recuperação e curadoria de contexto com orçamento configurável | context_within_budget_rate, relevant_memory_hit_rate |
| policy_enforcement | work | Policy gate para tools; auditabilidade de ações | policy_violation_rate, audit_completeness |
| session_lifecycle | todos | Criação, retomada e encerramento de sessões com continuidade entre restarts | session_recovery_rate, summary_generation_rate |

### Mapeamento US → Track

| User Story | Value Track | Subtrack (opcional) |
|------------|-------------|---------------------|
| US-01 | learn, chat, teach, work, show, reflect | persistence_integrity |
| US-02 | chat, work, reflect | session_lifecycle |
| US-03 | work | persistence_integrity |
| US-04 | work | policy_enforcement |
| US-05 | teach, chat | context_assembly |
| US-06 | learn, reflect, chat | persistence_integrity |
| US-07 | chat, teach, work | context_assembly |
| US-08 | chat, work | — |
| US-09 | work | policy_enforcement |
| US-10 | work, reflect | — |
| US-11 | learn, teach, chat, work, show, reflect | — |
| US-12 | reflect, learn | persistence_integrity |
| US-13 | show | — |
| US-14 | chat, work, show | session_lifecycle |

### Contrato de Observabilidade

- **Métricas por execução**: count, duration, success, error
- **Eventos mínimos**: start, finish, error
- **Edges observáveis** (quando usados): LLM / HTTP / DB
- **Disciplina de tags**: proibido alta cardinalidade (ex: user_id como tag)
- **Implementação**: toda execução passa por `forge_base.pulse.UseCaseRunner` — nunca chamar `use_case.execute()` direto nos entrypoints
