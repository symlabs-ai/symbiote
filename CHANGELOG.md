# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/)

## [Unreleased]

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
