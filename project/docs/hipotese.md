# Hipótese

> Projeto: Symbiote
> Data: 2026-03-16
> Status: draft

---

## 1. Contexto

Os frameworks de agentes de IA atuais (LangChain, CrewAI, AutoGen, etc.) modelam a unidade de software como **tarefa**: um loop efêmero de `input → LLM → tools → output`. Isso resolve bem chamada de modelo, chamada de ferramentas e encadeamento de etapas. Porém, quando se tenta construir assistentes que operam continuamente — copilotos de projeto, assistentes pessoais, agentes de domínio embutidos em sistemas —, o modelo de tarefa se esgota rapidamente. O resultado são agentes amnésicos, histórico bruto despejado no prompt, baixa auditabilidade e nenhuma continuidade real entre sessões.

A Symlabs opera com symbiotas (ft_manager, ft_coach, forge_coder, ft_gatekeeper) no processo Fast Track e já vivencia diariamente a limitação: cada sessão começa do zero, memória é file-based e ad-hoc, não há separação entre conhecimento e memória relacional, e o contexto é montado manualmente.

## 2. Sinal de Mercado

- **Tendência clara**: o mercado está migrando de "chatbots" para "agentes persistentes" (Devin, Claude Code, Cursor, Windsurf), mas todos ainda tratam persistência como feature lateral, não como arquitetura central.
- **Dor recorrente em produção**: empresas que tentam levar agentes de protótipo para uso contínuo relatam amnésia, prompts inflados, falta de auditabilidade e dificuldade de debugging.
- **Analogia**: a transição é similar ao salto de scripts CGI (stateless) para application servers (stateful) nos anos 2000. Quem ofereceu o runtime stateful certo (Django, Rails) capturou o ecossistema.
- **Gap identificado**: nenhum framework open-source modela explicitamente identidade + memória em camadas + workspace + environment + processo + reflexão como componentes de primeira classe de um kernel.

## 3. Oportunidade

Construir o **kernel** — não o framework completo, não a plataforma — que resolve a camada mais fundamental: como instanciar, persistir e operar uma entidade cognitiva ao longo do tempo. Se o kernel estiver certo, ele sustenta múltiplos produtos por cima (assistentes pessoais, copilotos, agentes de domínio, runtimes embarcados). Se estiver errado, nada que se construa em cima terá solidez.

O Symbiote seria o primeiro runtime Python open-source projetado desde o início para entidades persistentes, não para tarefas efêmeras. Local-first, SQLite + filesystem, sem dependência de infraestrutura pesada, extensível via adapters.

## 4. Grau de Certeza

**Médio-alto (60-70%)**

- **Alta certeza** de que o problema existe (vivenciado internamente e reportado amplamente pela comunidade).
- **Alta certeza** de que a abstração correta é "entidade persistente" e não "tarefa".
- **Certeza média** sobre a adoção: o mercado de frameworks de agentes é ruidoso e fragmentado; capturar atenção exige execução e demonstração clara de valor.
- **Risco principal**: escopo — o conceito é amplo e pode facilmente virar plataforma antes de ser kernel.

---

## 5. Visão Inicial

### 5.1 Intenção Central

Oferecer um kernel Python para construir entidades cognitivas persistentes que lembram, operam sobre artefatos reais, aprendem com a relação e preservam contexto de forma seletiva.

### 5.2 Problema

Frameworks de agentes modelam "tarefa", não "entidade". O resultado: agentes amnésicos, contexto despejado sem curadoria, zero continuidade entre sessões, workspace tratado como afterthought e nenhuma separação entre memória e conhecimento.

### 5.3 Público-Alvo

- **Primário**: desenvolvedores Python que constroem assistentes e agentes persistentes e precisam de um runtime sólido como base (em vez de reinventar persistência, memória e contexto a cada projeto).
- **Secundário**: equipes técnicas que embarcam IA em produtos internos (ERP, backoffice, plataformas SaaS) e precisam de kernel embarcável sem dependência de frameworks pesados.
- **Uso interno (Symlabs)**: base para os symbiotas do processo Fast Track e futuros produtos da empresa.

### 5.4 Diferencial Estratégico

1. **Unidade de abstração é a entidade, não a tarefa** — modelar simbióta (identidade + memória + workspace + environment + processo + reflexão) como primitiva de primeira classe.
2. **Memória em camadas com curadoria** — working, session, long-term relational e semantic recall como camadas separadas; contexto é orçamentado e seletivo, nunca despejado.
3. **Workspace e environment são componentes do kernel** — não afterthoughts; o simbióta opera sobre artefatos reais em diretórios reais com ferramentas governadas por policy.
4. **Local-first e deploy simples** — SQLite + filesystem, sem dependência de vector DB, funciona como biblioteca Python ou serviço HTTP.
5. **Reflexão como capacidade obrigatória** — o simbióta consolida aprendizado e decide o que persistir ao final de cada sessão/tarefa.

---

## 6. Value Tracks (candidatos)

> Fluxos de negócio que o cliente executaria repetidamente. Serão formalizados no PRD (seção 10).

| Track (candidato) | Done = | KPIs (rascunho) |
|-------------------|--------|------------------|
| learn | Memória útil persistida a partir da interação (preferência, fato, procedimento, restrição) | memory_precision, learn_yield_per_session |
| teach | Explicação/orientação/revisão estruturada entregue ao usuário com contexto relevante | teach_relevance_score, response_completeness |
| chat | Resposta contextual entregue com memória recuperada e contexto seletivo (não despejado) | context_budget_compliance, recall_hit_rate, response_latency_p95 |
| work | Tarefa executada sobre workspace real, artefatos produzidos e rastreados, tools governadas por policy | artifact_production_rate, policy_compliance_rate, task_completion_rate |
| show | Resultado exibido ao usuário (diff, relatório, resumo, export) de forma estruturada e legível | export_format_validity, show_completeness |
| reflect | Sessão/tarefa consolidada: summary gerado, memórias candidatas extraídas, ruído descartado | reflection_yield, summary_quality, memory_discard_ratio |

### Support Tracks (quando aplicável)

| Track (candidato) | Sustenta | Descrição |
|-------------------|----------|-----------|
| persistence_integrity | learn, reflect, chat | SQLite + filesystem como fonte de verdade; restart não corrompe estado |
| context_assembly | chat, teach, work | Pipeline de recuperação e curadoria de contexto com orçamento configurável |
| policy_enforcement | work | Policy gate para tools; auditabilidade de ações |
| session_lifecycle | todos | Criação, retomada e encerramento de sessões com continuidade entre restarts |
