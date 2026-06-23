# Self-Improvement Rollout Plan (v0.6.2+)

Plano operacional para ativar gradualmente o loop de self-improvement (Sprints 1-5) em produção. Defaults do `v0.6.2` são todos OFF — esse documento descreve **como** ligar de forma incremental, **quem** decide cada gate, e **o que** medir antes de avançar.

**Status atual (2026-05-27)**: plano não iniciado. Toda decisão abaixo está aguardando o mantenedor.

---

## Princípio guia

> "Ative o caminho mais barato em UM symbiote interno antes de ligar globalmente. Audite por 1-2 semanas em cada estágio. Promova apenas com sinal positivo mensurável."

3 estágios, ordenados por: custo crescente, risco crescente, valor crescente.

---

## Escopo de skills no deploy da API (multi-tenant) — v0.7.0+

A API HTTP (`symbiote.api.http`) é um entrypoint **multi-tenant**: um kernel singleton serve vários symbiotes/tenants via `/symbiotes/{symbiote_id}`. Por isso o `_resolve_config()` da API lê o escopo de skills de ENV e **assume `per_symbiote` por default** (diferente do `KernelConfig`, que segue `global` para não quebrar CLI/embarcados):

| ENV | Valores | Default na API |
|-----|---------|----------------|
| `SYMBIOTE_SKILL_SCOPE` | `global` \| `per_symbiote` | `per_symbiote` |
| `SYMBIOTE_SKILLS_ROOT` | caminho | (default do KernelConfig) |
| `SYMBIOTE_SKILLS_PROTECTED_ROOTS` | lista (`,` ou `os.pathsep`) | vazio |

- **Por que `per_symbiote` por default:** em `global`, skills aprendidas viram um pool compartilhado → **vazamento cross-tenant** (contas/clientes diferentes veriam skills uns dos outros). `per_symbiote` isola fisicamente em `{skills_root}/<symbiote_id>/skills/...`.
- **MIGRAÇÃO (atenção):** se um deploy de API existente dependia de um pool GLOBAL de skills, ligar `per_symbiote` (default novo) torna essas skills **invisíveis** (modo M3 — arquivos antigos ficam no disco, só não são lidos). Para manter o comportamento antigo, set `SYMBIOTE_SKILL_SCOPE=global` explicitamente. Quando o scope não é setado por ENV, a API loga um WARNING no boot explicando isso.
- **Catálogo comum read-only:** `SYMBIOTE_SKILLS_PROTECTED_ROOTS` aponta raízes de skills "de fábrica" visíveis a todos os tenants mas não graváveis.

---

## Pré-requisitos (uma vez só)

Antes de qualquer estágio:

```python
# Setar o evolver LLM. Recomendação: Haiku 4.5 (barato + rápido).
# Sem isso, EnvironmentManager.configure() levanta ValueError quando o
# host tenta ligar reflection_mode='llm' ou skill_review_enabled=True.
from symbiote.adapters.llm.forge import ForgeLLMAdapter
kernel.set_evolver_llm(ForgeLLMAdapter(model="claude-haiku-4-5"))
```

Confirmar:

```python
assert kernel._evolver_llm is not None
```

---

## Estágio 1 — Reflection LLM em modo `hybrid` (1-2 semanas)

**O que faz**: roda extração via LLM em paralelo com a heurística keyword (legacy), mas **persiste apenas o que o keyword produz**. O resultado do LLM vai para `reflection_audit` para comparação offline.

**Por que começar aqui**: menor custo (~$0.01/sessão), zero risco de side-effect em produção (não muta `memory_entries`), permite auditoria sem comprometer estabilidade.

### Setup

```python
kernel.environment.configure(
    symbiote_id="<piloto>",
    reflection_mode="hybrid",
)
```

### O que medir (diariamente)

```bash
# Resumo dos últimos 7 dias
symbiote audit reflection --days 7

# Diff completo dos primeiros 5 rows (manualmente revisar)
symbiote audit reflection --days 7 --diff
```

Cobrir manualmente ~50 rows e responder por amostra:

| Métrica | Threshold pra promover |
|---|---|
| % de rows onde LLM produziu ≥1 fact que keyword perdeu | ≥40% |
| % de rows onde LLM produziu fact espurioso (não-útil ou wrong) | ≤15% |
| Custo médio por close_session (somar nas linhas do audit) | ≤$0.02 |
| Erros LLM (`llm_error` não-null) | ≤5% das rows |

### Critério de stop (qualquer um)

- Custo médio > $0.05/sessão
- Taxa de erro > 20%
- ≥1 incidente de output do LLM contendo PII/secret (audit row precisa ser apagada manualmente E o caso vira issue)

### Critério de promoção

- Todos os 4 thresholds acima dentro do limite por ≥7 dias consecutivos
- Revisão manual de 50 amostras confirma que LLM capturou ≥40% mais signal útil que keyword
- Decisão registrada no `BACKLOG.md` ou similar

---

## Estágio 2 — Reflection LLM como persistência real (`llm`)

**O que faz**: troca persistência de keyword para LLM. PATCH path (`MemoryPort.update`) começa a refinar memórias existentes em vez de criar duplicatas.

**Pré-requisito**: estágio 1 concluído com sinal positivo.

### Setup

```python
kernel.environment.configure(
    symbiote_id="<piloto>",
    reflection_mode="llm",  # antes era 'hybrid'
)
```

### O que mudar de comportamento

- `memory_entries.source = "reflection"` rows agora vêm do LLM, não do keyword
- `reflection_audit` continua sendo escrito (modo `llm` audita igual)
- PATCH path: `memory_entries.updated_at` deixa de ser sempre NULL

### O que medir (2 semanas)

```bash
# Quantas PATCHs vs CREATEs por dia (querry direta no DB)
sqlite3 .symbiote/symbiote.db "
  SELECT date(updated_at) as d, count(*) FROM memory_entries
  WHERE updated_at IS NOT NULL AND updated_at > date('now', '-14 days')
  GROUP BY d
"
```

| Métrica | Threshold pra promover |
|---|---|
| Ratio PATCH / (PATCH + CREATE) | ≥20% (sinal que dedup funciona) |
| Crescimento de `memory_entries` ativos | <50% do baseline com keyword |
| Custo médio por close_session | mesma faixa do hybrid |
| Reclamações de "agente esqueceu X" | 0 |

### Critério de stop

- Ratio PATCH/total < 5% por 7 dias (LLM não está usando hierarquia)
- Crescimento de memórias > baseline keyword (auto-dedup falhou)

### Critério de promoção pra Estágio 3

- 2 semanas estáveis em modo `llm`
- Bug rate normal (não pior que pré-Sprint 1)
- Mantenedor autoriza ativação do skill review

---

## Estágio 3 — Skill review autônomo

**O que faz**: liga o loop completo. Background daemon thread analisa cada sessão, decide criar/patchar skills via `SkillsStore`. Skills nascem em `quarantine`; auto-promovem para `active` após N usos (default 3); quarantine não-usada vai pra `archived` após 14d.

**Pré-requisito**: estágio 2 concluído. `_evolver_llm` setado (já é desde o pré-requisito global).

### Setup

```python
kernel.environment.configure(
    symbiote_id="<piloto>",
    skill_review_enabled=True,
    # Defaults conservadores (todos opcionais)
    skill_nudge_interval=10,           # nudge mid-session a cada 10 tool iters
    max_active_skills=20,              # cap do que aparece em <available-skills>
    max_quarantine_skills=10,          # cap do backlog
    skill_auto_promote_threshold=3,    # N loads pra auto-promover
    skill_quarantine_timeout_days=14,  # auto-archive
)
```

### Setup `KernelConfig` (uma vez, no boot)

```python
config = KernelConfig(
    db_path=Path(".symbiote/symbiote.db"),
    # Default: {db_path.parent}/skills/. Override se quiser caminho explícito:
    # skills_root=Path("/srv/symbiote/agent-skills"),
)
```

### O que medir (4 semanas — pode ir mais devagar)

```bash
# Inventário diário
symbiote skills list --all                # active + quarantine + archived
symbiote audit skill-review --days 7      # taxa de ops aplicadas/skipped
symbiote audit skill-review --days 7 --ops  # JSON das ops (revisar amostra)
```

| Métrica | Threshold pra continuar |
|---|---|
| # skills criadas / semana | ≥1 (sinal de vida) |
| # skills promovidas auto / criadas | ≥30% (cap subiu de 1 use → 3 active) |
| # skills archived auto / criadas | ≤40% (taxa razoável de skill ruim) |
| `applied / (applied + skipped)` no audit | ≥60% |
| Custo extra mensal (skill review) | ≤ $3 single-user |
| Reclamações de "agente fez algo estranho" | 0 |

### Critério de stop

- # skills criadas = 0 por 7 dias (review desligado de fato; investigar)
- # archived > # active (library entulhando lixo)
- ≥1 incidente onde skill agent-created causou bug em produção (kill switch: `skill_review_enabled=False`)

### Auditoria manual (semanal nas primeiras 4 semanas)

Inspecionar **toda** skill agent-created promovida pra `active`:

```bash
symbiote skills list  # só active — todas agent-created vão aparecer
# Pra cada, ler:
cat .symbiote/skills/<name>/SKILL.md
cat .symbiote/skills/<name>/.skill_meta.json
```

Se alguma for "ruim" (instrução errada, alucinação, etc):

```bash
symbiote skills pin <name> --unpin  # libera pra archive futuro
# OU edição manual: cat > .symbiote/skills/<name>/.skill_meta.json
#   com "status": "archived"
```

E reportar ao mantenedor para entender que tipo de sessão produziu a skill ruim — pode indicar prompt drift, anti-pattern do `_review_prompts.py` não filtrou, etc.

---

## Decisão de globalização (após Estágios 1-3 estáveis)

Quando o symbiote piloto está há ≥30 dias estável em Estágio 3 sem incidente, decisão do mantenedor:

| Opção | Quando faz sentido |
|---|---|
| **Manter opt-in per-symbiote** | Symbiote é local-first; cada usuário decide |
| **Mudar defaults globais** (`reflection_mode='llm'`, `skill_review_enabled=True`) | Múltiplos symbiotes ativos, todos beneficiam, custo aceitável agregado |
| **Habilitar por tier** (e.g. só premium) | Multi-tenant comercial — custo precisa ser repassado |

Mudar default **exige bump MINOR** (`v0.7.0`) com nota clara no CHANGELOG.

---

## Escolha do symbiote piloto

**TODO mantenedor**: decidir qual symbiote interno é piloto.

Critérios sugeridos:
- Symbiote com uso regular (≥5 sessões/dia) — produz sinal estatístico mais rápido
- Symbiote SEM dados sensíveis (audit/CLI mostra conteúdo de mensagens em audit logs)
- Symbiote cujo dono pode revisar skills manualmente sem cargo cognitivo (você mesmo, provavelmente)

Candidatos óbvios: o agente que você usa pra desenvolver (CLI `symbiote chat`), ou um symbiote dedicado pra dogfooding (`name="dogfood"`).

---

## Kill switch (operação de emergência)

Se algo explode em qualquer estágio:

```python
# Per-symbiote
kernel.environment.configure(symbiote_id="<sym>", reflection_mode="keyword",
                              skill_review_enabled=False)

# Ou em SQL direto (se kernel down)
sqlite3 .symbiote/symbiote.db "
  UPDATE environment_configs
  SET reflection_mode='keyword', skill_review_enabled=0
"
```

Defaults legados voltam imediatamente no próximo turn. Audit logs ficam para investigação.

---

## Roadmap pós-Estágio 3

Quando piloto comprovou estabilidade, próximos investimentos naturais:

1. **`DreamEngine.ReconcilePhase` para skills** — detecta overlap de `when_to_use` entre agent-created. Precisa de volume real (≥30 skills agent-created) pra calibrar threshold. Adiado no Sprint 5.
2. **Tool loop reentrant no background review** — vale só se qualidade do single-call plateaur (≤60% de signal capturado). Adiado no Sprint 5.
3. **Embedding-based skill discovery** — `_format_existing_skills` lista até 30 entries hoje; embedding semântico vira gargalo quando library passar de ~50. Adiado no Sprint 5.
4. **Promoção automática `reflection_mode='llm'` por default** — só com piloto comprovando 30 dias estáveis.

---

🤖 Documento gerado durante o release v0.6.2. Atualize ao tomar cada decisão.
