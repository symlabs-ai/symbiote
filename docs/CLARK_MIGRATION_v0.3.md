# Guia de Migração Clark → Symbiote v0.3.x

> Para: equipe YouNews (Clark integration)
> De: Symbiote kernel team
> Data: 2026-04-01
> Atualizado: 2026-04-01 (v0.3.3 — 4 modos de execução)
> Versão: v0.3.3 "Execution Modes"

---

## Contexto

O Symbiote v0.3.x traz 24+ features focadas em resiliência, auto-evolução e **4 modos de execução** para diferentes complexidades de tarefa. **Nenhuma é breaking change** — o Clark atualiza e continua funcionando identicamente. Todas as features novas são opt-in.

### Novidade v0.3.1-v0.3.3: Modos de Execução

O Symbiote agora tem 4 modos que o Clark pode usar conforme a tarefa:

| Modo | Para que serve | Exemplo no Clark |
|------|---------------|-----------------|
| **instant** | Perguntas simples, 0-1 tool call | "Quantas matérias publicamos hoje?" |
| **brief** | Tarefas compostas, 3-10 steps | "Publique a matéria + envie newsletter + poste nas redes" |
| **long_run** | Projetos grandes, plano + execução + avaliação | "Crie uma edição especial: pesquise 5 temas, redija, revise, publique" |
| **continuous** | Agente always-on (futuro) | Clark monitorando, sugerindo pautas, publicando proativamente |

O Clark já funciona em **brief** (default). A novidade é que tarefas simples podem usar **instant** (mais rápido, mais barato) e tarefas complexas podem usar **long_run** (com plano estruturado e avaliação de qualidade).

Este guia apresenta a migração em 5 níveis, do mais simples (zero código) ao mais avançado (harness auto-evolutivo). Cada nível é independente — você pode parar em qualquer um e colher os benefícios daquele nível.

---

## Nível 0 — Atualização Zero-Effort

**Esforço**: Atualizar a dependência do Symbiote para v0.3.0. Nenhuma mudança de código.

**Como fazer**:
```bash
# No ambiente do YouNews
pip install -e /path/to/symbiote  # ou atualizar o git ref
```

**O que muda automaticamente (sem código novo)**:

| Feature | O que faz | Impacto no Clark |
|---------|-----------|------------------|
| LLM retry com backoff | Retenta 3x em erros transientes (429, 503, timeout) com delays 1s/2s/4s | Clark parava de responder quando SymGateway retornava rate limit. Agora se recupera sozinho |
| Parallel tool execution | Quando o LLM emite 2+ tool calls no mesmo turno, executam em paralelo (até 4 workers) | Tasks que chamam múltiplas APIs ficam mais rápidas |
| Circuit breaker | Se uma tool falha 3x consecutivas, para de tentar e responde ao usuário | Quando a API do YouNews está fora do ar, Clark não gasta 10 iterações tentando. Para após 3 e avisa |
| Stagnation detection | Detecta quando o LLM repete a mesma tool com mesmos params | O problema que vimos com Llama-3.3-70b chamando items_get 8x — resolvido. Clark para e responde |
| 3-layer compaction | Trunca tool results grandes, resume iterações antigas, compacta quando o contexto enche | Listas com 50+ itens não estouram mais o contexto. O loop sobrevive com mais iterações |
| Index mode cache | Schemas já buscados via get_tool_schema não são re-buscados no mesmo loop | Clark usa index mode com ~75 tools. Antes: 6 iterações para 3 steps (3 schema + 3 tool). Agora: ~4 iterações |
| SessionScore automático | Todo close_session() computa um score 0.0-1.0 baseado em stop_reason + iterations | Dados de qualidade começam a acumular no SQLite. Útil para análise posterior |
| LoopTrace persistence | Todo loop é registrado com steps, timing, stop_reason | Observabilidade: "por que aquela sessão travou?" — agora tem resposta no banco |
| MemoryEntry de falha | Quando o loop falha, gera memória procedural automaticamente | Se items_publish falhou porque o item não estava em status "ready", o Clark memoriza e não repete o erro |

**Benefício**: Resiliência imediata. O Clark fica mais robusto sem uma linha de código nova. Os problemas mais reportados (loop infinito, API fora do ar, context overflow) são resolvidos.

**Risco**: Zero. Todos os defaults são os mesmos valores de antes da atualização.

---

## Nível 1 — Configuração Básica

**Esforço**: ~10 linhas no clark.py (ou equivalente).

**Pré-requisito**: Nível 0 aplicado.

**O que configurar**:

### 1.1 — Timeout customizado

O Clark chama APIs do YouNews que podem demorar. Configurar um timeout evita que uma API lenta trave o loop inteiro.

```python
# No setup do Clark, após kernel init
kernel.environment.configure(
    symbiote_id=clark_id,
    tool_call_timeout=15.0,   # cada tool call tem no máximo 15s (default: 30s)
    loop_timeout=120.0,       # o loop inteiro tem no máximo 2min (default: 5min)
)
```

**Benefício**: Se a API de items_list demorar mais que 15s (ex: banco lento), o Clark recebe um timeout error em vez de travar indefinidamente. O LLM vê o erro e pode tentar outra abordagem.

### 1.2 — max_iterations ajustado

O Clark faz tarefas simples (publicar matéria = 2-3 steps). O default de 10 iterações é muito para ele.

```python
kernel.environment.configure(
    symbiote_id=clark_id,
    max_tool_iterations=6,    # suficiente para 3 steps + margem (default: 10)
)
```

**Benefício**: Se o LLM entrar em loop (Llama-3.3 fazia isso), para em 6 em vez de 10. Menos custo, resposta mais rápida.

### 1.3 — Feedback básico

Quando o Clark consegue publicar com sucesso, reportar isso como feedback positivo.

```python
# No handler de resposta do Clark, quando o usuário confirma publicação
kernel.report_feedback(session_id, score=1.0, source="publish_success")

# Quando o usuário repete a pergunta (sinal de insatisfação)
kernel.report_feedback(session_id, score=0.2, source="user_retry")
```

**Benefício**: O SessionScore composto (60% auto + 40% user) fica mais preciso. Isso alimenta o ParameterTuner e o HarnessEvolver nos níveis seguintes. Quanto mais cedo começar a coletar feedback, mais dados terá quando ativar a auto-evolução.

**Risco**: Nenhum. O feedback só enriquece o score — não muda comportamento.

---

## Nível 2 — UX e Segurança

**Esforço**: ~30 linhas no clark.py + ajustes no frontend.

**Pré-requisito**: Nível 1 aplicado.

### 2.1 — Streaming mid-loop

Hoje o usuário vê silêncio total enquanto o Clark executa tools. Com streaming, o frontend pode mostrar progresso.

```python
# No ChatRunner init (ou via kernel config)
def on_progress(event: str, iteration: int, total: int):
    """Envia progresso via SSE para o frontend."""
    if event == "tool_start":
        send_sse(f"Executando ação {iteration}/{total}...")
    elif event == "tool_end":
        send_sse(f"Ação {iteration} concluída.")

def on_stream(text: str, iteration: int):
    """Texto intermediário do LLM (opcional — para debug)."""
    logger.debug(f"[iter {iteration}] {text[:100]}")

# Passar callbacks ao criar o ChatRunner
runner = ChatRunner(
    llm=llm,
    tool_gateway=gateway,
    on_progress=on_progress,
    on_stream=on_stream,
)
```

**Benefício**: O usuário do YouNews vê "Buscando matérias...", "Publicando item 42..." em vez de silêncio de 10-30 segundos. Reduz a percepção de que o Clark travou.

**Nota**: `on_token` (resposta final) continua funcionando exatamente como antes. Os callbacks novos são aditivos.

### 2.2 — Human-in-the-loop para ações destrutivas

O Clark pode publicar matérias e enviar newsletters. Essas ações são irreversíveis. Com risk_level, o host pode exigir confirmação.

```python
# Ao registrar/descobrir tools, marcar as destrutivas como high-risk
from symbiote.environment.descriptors import ToolDescriptor

# Se usando discovered tools, setar risk_level após o load
for tool_id in ["items_publish", "newsletter_send", "items_delete"]:
    desc = kernel.tool_gateway.get_descriptor(tool_id)
    if desc:
        desc.risk_level = "high"

# Callback de aprovação
def approval_callback(tool_id: str, params: dict, risk_level: str) -> bool:
    """Pede confirmação ao usuário para ações de alto risco."""
    if risk_level != "high":
        return True  # auto-approve low/medium

    # Enviar pergunta ao frontend via SSE/WebSocket
    confirmation = ask_user_confirmation(
        f"O Clark quer executar '{tool_id}'. Confirmar?"
    )
    return confirmation  # True = prosseguir, False = negar

# Passar ao ChatRunner
runner = ChatRunner(
    llm=llm,
    tool_gateway=gateway,
    on_before_tool_call=approval_callback,
)
```

**Benefício**: O Clark não publica/envia sem confirmação explícita do usuário. Se o LLM alucinar e tentar publicar uma matéria errada, o callback bloqueia. O LLM recebe o erro "Tool call denied by approval callback" e pode reformular.

**Risco**: Adiciona latência (espera a confirmação do usuário). Para workflows automatizados (publicação em batch), não usar. Para uso interativo, é safety net essencial.

### 2.3 — Execution mode por contexto

O Clark pode usar diferentes modos conforme a complexidade da tarefa.

```python
# Default: brief para tarefas compostas (publicar + newsletter)
kernel.environment.configure(
    symbiote_id=clark_id,
    tool_mode="brief",
)

# Para um symbiote de FAQ que só responde perguntas:
kernel.environment.configure(
    symbiote_id=faq_bot_id,
    tool_mode="instant",  # fast-path: 1 LLM call, mais rápido e barato
)

# Para edições especiais (pesquisa + redação + revisão + publicação):
kernel.environment.configure(
    symbiote_id=clark_editorial_id,
    tool_mode="long_run",
    planner_prompt=(
        "Você é um editor-chefe. Decomponha a pauta em blocos de trabalho: "
        "pesquisa, redação, revisão, publicação. "
        "Retorne um JSON array com name, description, success_criteria."
    ),
    evaluator_prompt=(
        "Você é um revisor editorial exigente. Avalie: "
        "precisão factual, qualidade da escrita, SEO, e completude."
    ),
    evaluator_criteria=[
        {"name": "precisao", "weight": 1.0, "threshold": 0.8,
         "description": "Fatos verificáveis e fontes citadas"},
        {"name": "qualidade", "weight": 0.8, "threshold": 0.7,
         "description": "Escrita clara, sem erros, tom adequado"},
        {"name": "completude", "weight": 0.6, "threshold": 0.6,
         "description": "Todos os aspectos da pauta cobertos"},
    ],
    context_strategy="hybrid",
    max_blocks=8,
)
```

**Benefício**: Tarefas simples (instant) são rápidas e baratas. Tarefas compostas (brief) funcionam como antes. Projetos editoriais (long_run) ganham planejamento estruturado e revisão automática de qualidade — o Clark decompõe a pauta, executa bloco a bloco, e um "revisor" avalia cada bloco antes de prosseguir.

**Sobre o long_run**: O planner e o evaluator são **opcionais**. Se não configurar `planner_prompt`, o Clark pula o planejamento. Se não configurar `evaluator_prompt`, pula a avaliação. Comece simples e adicione conforme necessidade.

---

### 2.4 — Roteamento inteligente de modos

O Symbiote nao decide qual modo usar — o host decide. Hoje o Clark trata toda mensagem da mesma forma (brief). Isso significa que "quantas materias temos?" gasta o mesmo setup de loop que "publique, envie newsletter e poste nas redes". O roteamento de modos resolve isso.

**Abordagem 1: Regras simples (recomendado para comecar)**

```python
# No handler de mensagens do Clark, ANTES de chamar kernel.message()
def choose_mode(user_input: str, has_tools: bool) -> str:
    """Decide o modo de execução baseado na mensagem."""
    # Sem tools = sempre instant (Q&A puro)
    if not has_tools:
        return "instant"

    # Heurísticas de complexidade
    input_lower = user_input.lower()

    # Palavras que indicam projeto complexo (long_run)
    long_run_signals = ["crie uma edição", "pesquise e redija", "monte um especial",
                        "faça uma cobertura completa", "produza um relatório"]
    if any(signal in input_lower for signal in long_run_signals):
        return "long_run"

    # Palavras que indicam multi-step (brief)
    brief_signals = ["publique", "envie", "agende", "mova", "atualize",
                     "crie", "delete", "e também", "depois"]
    if any(signal in input_lower for signal in brief_signals):
        return "brief"

    # Default: perguntas e consultas = instant
    return "instant"

# Uso:
mode = choose_mode(user_input, has_tools=True)
kernel.environment.configure(symbiote_id=clark_id, tool_mode=mode)
response = kernel.message(session_id, user_input)
```

**Abordagem 2: LLM classifica (mais preciso, custo extra)**

```python
# Usar um modelo barato para classificar a complexidade
CLASSIFIER_PROMPT = """Classifique a mensagem do usuário em um dos modos:
- instant: pergunta simples, consulta, informação (0-1 ações)
- brief: tarefa composta, 2-5 ações sequenciais
- long_run: projeto complexo que precisa de planejamento

Responda APENAS com: instant, brief, ou long_run

Mensagem: {user_input}"""

def classify_mode(user_input: str) -> str:
    response = cheap_llm.complete([
        {"role": "user", "content": CLASSIFIER_PROMPT.format(user_input=user_input)}
    ])
    mode = response.strip().lower()
    if mode in ("instant", "brief", "long_run"):
        return mode
    return "brief"  # fallback seguro
```

**Abordagem 3: Baseada no histórico (mais sofisticada)**

```python
# Usar o SessionRecallPort para ver como mensagens similares foram tratadas
# Se mensagens parecidas tiveram score alto em instant, usar instant
# Se tiveram score baixo (precisavam de mais steps), usar brief
```

**Impacto esperado no Clark:**

| Sem roteamento (hoje) | Com roteamento |
|---|---|
| "Quantas matérias?" → brief (2+ LLM calls, setup de loop) | → instant (1 LLM call, fast-path) |
| "Publique a matéria" → brief (ok, é o modo certo) | → brief (sem mudança) |
| "Monte edição especial de fim de ano" → brief (tenta fazer tudo de uma vez, pode falhar) | → long_run (planeja, executa em blocos, avalia) |

**Recomendação**: Começar com Abordagem 1 (regras). É simples, zero custo extra, e já captura 80% dos casos. Evoluir para Abordagem 2 se as regras não forem suficientes.

---

## Nível 3 — Auto-Calibração

**Esforço**: ~20 linhas + cron job semanal.

**Pré-requisito**: Nível 1 aplicado + pelo menos 50 sessões com scores acumulados.

### 3.1 — Parameter Tuner

O ParameterTuner analisa os scores e traces acumulados e ajusta parâmetros automaticamente.

```python
from symbiote.harness.tuner import ParameterTuner

# Rodar semanalmente (cron, celery, ou manualmente)
tuner = ParameterTuner(storage=kernel._storage)
result = tuner.analyze(clark_id)

print(f"Tier: {result.tier} ({result.session_count} sessões)")
print(f"Ajustes recomendados: {result.adjustments}")
print(f"Razões: {result.reasons}")

# Aplicar automaticamente
if result.adjustments:
    tuner.apply(result, kernel.environment)
    print("Ajustes aplicados!")
```

**Como funciona o tiering**:

| Tier | Sessões | O que ajusta | Exemplo |
|------|---------|-------------|---------|
| 0 | 0-4 | Nada | Sem dados |
| 1 | 5-19 | Só ajustes seguros (aumentar limites quando 80%+ das sessões batem neles) | "100% das sessões bateram max_iterations=6 → aumentar para 11" |
| 2 | 20-49 | Ajustes estatísticos (compaction threshold baseado no avg de iterações) | "Avg 2 iterações, compaction_threshold=4 nunca trigga → baixar para 2" |
| 3 | 50+ | Fine tuning (memory/knowledge shares) | "Sessões com muitas tools têm score menor → reduzir memory_share 5%" |

**Benefício**: O Clark se calibra sozinho. Se o padrão de uso mudar (mais matérias complexas, mais APIs lentas), os parâmetros se adaptam automaticamente.

**Risco**: Baixo. Cada regra tem caps absolutos (max_iterations nunca > 30), e só aplica com confiança estatística. Se um ajuste piorar os scores, o tuner corrige na próxima rodada.

### 3.2 — Context mode on-demand (opcional)

Se o Clark acumular muitas memórias e o pre-packed estiver poluindo o contexto com memórias irrelevantes:

```python
kernel.environment.configure(
    symbiote_id=clark_id,
    context_mode="on_demand",
)

# As tools search_memories e search_knowledge já estão registradas.
# Autorizar para o Clark:
current_tools = kernel.environment.list_tools(clark_id)
kernel.environment.configure(
    symbiote_id=clark_id,
    tools=current_tools + ["search_memories", "search_knowledge"],
)
```

**Benefício**: O Clark busca memórias só quando precisa, formulando a query certa. Em vez de receber 5 memórias pré-selecionadas pelo `LIKE %query%` (que pode ser irrelevante), ele busca "qual foi a última matéria sobre incêndio?" e recebe exatamente o que precisa.

**Trade-off**: Gasta 1-2 iterações extras (o LLM precisa chamar a tool antes de responder). Para conversas rápidas, `packed` continua sendo melhor. Avaliar com dados antes de ativar.

---

## Nível 4 — Harness Evolution

**Esforço**: ~40 linhas + cron semanal + LLM adicional (Haiku recomendado).

**Pré-requisito**: Nível 3 aplicado + pelo menos 200 sessões com scores + feedback do Nível 1.3.

### 4.1 — Prompt Evolution

O HarnessEvolver usa um LLM para analisar sessões que falharam e propor versões melhores das instruções de tools.

```python
# Injetar um LLM proposer (idealmente diferente do LLM principal)
from symbiote.adapters.llm.forge import ForgeLLMAdapter

haiku = ForgeLLMAdapter(provider="anthropic", model="claude-haiku-4-5-20251001")
kernel.set_evolver_llm(haiku)

# Rodar evolução (semanalmente)
from symbiote.runners.chat import _TOOL_INSTRUCTIONS

result = kernel.evolve_harness(
    symbiote_id=clark_id,
    component="tool_instructions",
    default_text=_TOOL_INSTRUCTIONS,
    days=7,  # analisar últimos 7 dias
)

if result.success:
    print(f"Nova versão {result.new_version} criada!")
    print(f"Baseada em {result.reason}")
else:
    print(f"Não evoluiu: {result.reason}")

# Checar rollback após 50+ sessões com a versão nova
rolled_back = kernel.check_harness_rollback(clark_id, "tool_instructions")
if rolled_back:
    print("Versão nova teve score pior — revertida automaticamente")
```

**O que o evolver descobre**: Padrões específicos do domínio do Clark. Exemplos hipotéticos de instruções que poderia gerar:

- "Antes de chamar items_publish, sempre verificar o status do item via items_get. Só publicar se status=ready."
- "Ao buscar itens, usar filtro de data para limitar resultados. Nunca chamar items_list sem filtro."
- "Se o usuário pedir para publicar 'a matéria sobre X', primeiro buscar com items_search para encontrar o ID correto."

Essas instruções são específicas do Clark/YouNews — o harness default genérico nunca as teria.

**Guard rails**:
- A versão nova não pode ter mais que 2x o tamanho da anterior
- Linhas marcadas "CRITICAL" são preservadas
- Se o proposer retorna JSON/código (lixo), é descartado
- Precisa de 50 sessões com a versão nova antes de aceitar/rejeitar
- Rollback automático se o score cair

**Benefício**: O Clark melhora suas próprias instruções baseado em falhas reais. Sem intervenção humana.

**Custo**: 1 chamada de Haiku por semana (~10k tokens input). Negligível.

### 4.2 — Benchmark Suite (para validação)

Definir tasks representativos do Clark para medir evolução:

```python
from symbiote.harness.benchmark import BenchmarkRunner, BenchmarkTask

tasks = [
    BenchmarkTask(
        id="publish_simple",
        description="Publique a matéria sobre incêndio na BR-101",
        expected_tools=["items_search", "items_publish"],
        grading="tool_called",
    ),
    BenchmarkTask(
        id="search_by_date",
        description="Busque matérias publicadas ontem",
        expected_tools=["items_list"],
        expected_params={"date_from": "2026-03-31"},
        grading="param_match",
    ),
    BenchmarkTask(
        id="newsletter_draft",
        description="Monte um rascunho de newsletter com as top 3 matérias do dia",
        expected_tools=["items_list", "newsletter_create"],
        grading="tool_called",
        timeout=60.0,
    ),
]

runner = BenchmarkRunner(kernel)
suite = runner.run_suite(clark_id, tasks, suite_name="clark_core")

print(f"Passed: {suite.passed}/{suite.total_tasks}")
print(f"Avg score: {suite.avg_score:.2f}")
for r in suite.results:
    status = "PASS" if r.passed else "FAIL"
    print(f"  [{status}] {r.task_id}: score={r.score:.2f}, iters={r.iterations}")
```

**Benefício**: Medição objetiva de qualidade. Rodar antes e depois de uma evolução para validar que a mudança melhorou. Detecta regressões automaticamente.

---

## Nível 5 — Cross-Symbiote (quando tiver múltiplos symbiotes)

**Esforço**: ~15 linhas.

**Pré-requisito**: 2+ symbiotes com tools similares (ex: Clark + outro bot que também usa items_list/items_publish).

```python
from symbiote.harness.cross_learning import CrossSymbioteLearner

learner = CrossSymbioteLearner(
    storage=kernel._storage,
    versions=kernel.harness_versions,
)

# Encontrar aprendizados transferíveis para um novo symbiote
candidates = learner.find_candidates(
    target_symbiote_id=new_bot_id,
    min_overlap=0.5,  # pelo menos 50% das tools em comum
)

for c in candidates:
    print(f"De {c.source_symbiote[:8]}: {c.component} (score={c.source_avg_score:.2f}, overlap={c.tool_overlap:.0%})")

# Transferir o melhor
if candidates:
    best = candidates[0]
    new_version = learner.transfer(best)
    print(f"Transferido! Versão {new_version} criada no symbiote alvo")
```

**Benefício**: Se o Clark já evoluiu boas instruções, um novo symbiote que use tools parecidas começa com essas instruções em vez de começar do zero. Bootstrapping acelerado.

---

## Resumo de Impacto por Nível

| Nível | Esforço | Tempo | Benefício principal |
|-------|---------|-------|---------------------|
| **0** | Zero | 5 min | Resiliência (retry, circuit breaker, stagnation, compaction) |
| **1** | ~10 linhas | 30 min | Configuração otimizada + feedback começando a acumular |
| **2** | ~30 linhas | 2h | UX (streaming) + segurança (approval) + execution modes (instant/brief/long_run) |
| **3** | ~20 linhas + cron | 1h | Auto-calibração de parâmetros baseada em dados reais |
| **4** | ~40 linhas + cron + Haiku | 2h | Instruções de tools evoluem sozinhas. Benchmark para medir |
| **5** | ~15 linhas | 30 min | Transferência de aprendizados entre symbiotes |

## Recomendação

Começar pelo **Nível 0 + Nível 1** imediatamente. São zero risco e dão a base de dados para os níveis seguintes. O feedback do Nível 1.3 é especialmente importante — quanto mais cedo começar a coletar, mais rápido terá dados suficientes para os Níveis 3 e 4.

O **Nível 2** depende de prioridade de UX. Se os usuários reclamam de silêncio durante o loop, priorizar. Se não, pode esperar.

Os **Níveis 3 e 4** ativam quando tiver ~50-200 sessões com scores. Com o volume do YouNews, isso pode ser questão de dias.

O **Nível 5** só quando tiver um segundo symbiote em produção.
