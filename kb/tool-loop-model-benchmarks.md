# Tool Loop — Benchmark de Modelos

Registro de testes de performance dos modelos no tool loop (brief mode) do Symbiote.
Atualizar a cada nova rodada de testes.

---

## Metodologia

- **Modo:** semantic (tool_loading) + brief (tool_loop)
- **Query:** "publique a matéria sobre o incêndio no jornal principal"
- **Tools disponíveis:** 6 (items_list, items_get, items_publish, items_update, compose_draft, compose_suggest_title)
- **Tarefa ideal:** 2 tool calls (items_list → items_publish), 3 iterações LLM (call 1, call 2, resposta final)
- **Max iterations:** 10
- **Gateway:** SymGateway (symgateway.symlabs.ai)
- **Handlers:** Mock com dados realistas (3 items, 1 matching "incêndio")

### Métricas

- **Tempo total:** wall clock do run() completo
- **Tool calls:** número de tools executadas
- **Iterações LLM:** chamadas ao modelo (tool calls + resposta final)
- **Desperdiçadas:** iterações além do necessário para completar a tarefa
- **Parou sozinho:** LLM respondeu sem tool_call após completar, sem bater no max
- **Resposta:** texto final retornado ao usuário
- **Sequência:** ordem das tool calls executadas

---

## 2026-03-19 — Rodada 1 (Llama 3.3 70B)

| Modelo | Tempo | Tool calls | Desp. | Parou? | Sequência |
|--------|-------|------------|-------|--------|-----------|
| llama-3.3-70b-versatile | 17.0s | 10 | 8 | Não | items_list → items_publish → items_get × 3 → items_update × 2 → items_get → items_publish → items_get |

**Observações:**
- Completou a tarefa no step 2 (items_list → items_publish) mas não parou
- Continuou chamando items_get e items_update repetidamente sem propósito
- Bateu no max_iterations=10
- Problema agravado porque o mock retorna sempre `status: "draft"` mesmo após publish, confundindo o modelo
- O mesmo modelo sem loop (instant mode) inventou sintaxe inexistente tentando encadear calls inline

## 2026-03-19 — Rodada 2 (Grok 4.1, Kimi K2, GPT-5.4)

| Modelo | Tempo | Tool calls | Desp. | Parou? | Sequência |
|--------|-------|------------|-------|--------|-----------|
| grok-4-1-fast-reasoning | 12.2s | 2 | 0 | Sim | items_list → items_publish |
| moonshotai/kimi-k2-instruct | 1.9s | 2 | 0 | Sim | items_list → items_publish |
| gpt-5.4 | 3.5s | 2 | 0 | Sim | items_list → items_publish |

**Observações:**
- Os três tiveram comportamento perfeito: 2 tool calls, 0 desperdício, pararam sozinhos
- Kimi K2 foi o mais rápido (1.9s) — custo-benefício excelente para tool execution
- GPT-5.4 equilibra velocidade (3.5s) e qualidade
- Grok 4.1 mais lento (12.2s) provavelmente pelo overhead de reasoning, mas preciso e completo
- Todos identificaram corretamente o item 42 ("Incêndio na zona sul") entre 3 resultados
- Todos responderam de forma concisa sem narração desnecessária

**Respostas finais:**
- Grok 4.1: "A matéria 'Incêndio na zona sul deixa 3 desabrigados' (ID 42) foi publicada com sucesso no jornal principal (ID 1) em 2026-03-19T12:00:00."
- Kimi K2: "Matéria 'Incêndio na zona sul deixa 3 desabrigados' publicada com sucesso no jornal principal."
- GPT-5.4: "Matéria publicada com sucesso no jornal principal."

## 2026-03-19 — Rodada 3 (Modelos locais — Ollama, RTX 4070 8GB)

| Modelo | Tempo | Tool calls | Desp. | Parou? | Sequência |
|--------|-------|------------|-------|--------|-----------|
| qwen3:8b (local, 5.2 GB) | 20.3s | 2 | 0 | Sim | items_list → items_publish |
| phi4-mini (local, 2.5 GB) | 15.3s | 0 | — | — | FALHOU (formato errado + placeholder) |

**Observações:**
- **Qwen3:8B surpreendeu:** comportamento perfeito, idêntico aos modelos top-tier. 2 tool calls, 0 desperdício, parou sozinho. Mais lento (20.3s) por rodar em GPU local vs API cloud, mas precisão impecável
- **Phi4-mini falhou completamente:** não conseguiu seguir o formato de tool_call (usou `tool_call{` sem fence markdown), e usou placeholder `<ID do item>` em vez de chamar items_list primeiro. Modelo de 3.8B é muito pequeno para seguir instruções de formato de tool calling
- Qwen3:8B roda confortável na RTX 4070 (5.2 GB de 8 GB disponíveis)
- Phi4-mini cabe fácil (2.5 GB) mas não serve para tool execution — floor de qualidade identificado
- Inferência local é ~5-10x mais lenta que API cloud mas custo zero por token

**Resposta final:**
- Qwen3:8B: "A matéria 'Incêndio na zona sul deixa 3 desabrigados' foi publicada com sucesso no jornal principal. O status do item foi atualizado para 'published' e a data de publicação foi registrada como 2026-03-19T12:00:00."
- Phi4-mini: FALHOU — output malformado com placeholder

## 2026-03-19 — Rodada 4 (MiniMax SynLogic-7B local)

| Modelo | Tempo | Tool calls | Desp. | Parou? | Sequência |
|--------|-------|------------|-------|--------|-----------|
| synlogic-7b (local, 4.7 GB) | 0.7s | 0 | — | — | FALHOU (formato incompatível) |

**Observações:**
- SynLogic-7B é um modelo de **lógica e raciocínio**, não de agentic tool calling
- Usa formato XML `<tool_call>` em vez do markdown fence ` ```tool_call` do Symbiote
- Além de errar o formato, **inventou resultados** em vez de esperar — alucinação completa
- Modelo baseado em Qwen2 fine-tuned para SynLogic (tarefas de raciocínio lógico), não generalista
- Não serve para tool execution apesar de caber na GPU

## 2026-03-19 — Rodada 5 (Modelos locais MLX — Mac Mini M4, 16 GB)

| Modelo | Tempo | Tool calls | Desp. | Parou? | Sequência |
|--------|-------|------------|-------|--------|-----------|
| qwen3-14b-4bit (MLX, ~8 GB) | 23.5s | 1 | 0 | Sim | items_publish (ID inventado) |
| moonlight-16b-a3b-4bit (MLX, ~9 GB) | 29.3s | 0 | — | — | FALHOU (formato errado + ID inventado) |

**Observações:**
- **Qwen3-14B-4bit (MLX):** Parou sozinho e seguiu o formato ` ```tool_call`, mas **pulou items_list** e foi direto ao items_publish com item_id=12345 (inventado). Em produção teria falhado com "item not found". Precisão parcial: formato correto, lógica errada. Curiosamente, o Qwen3:8B via Ollama fez o fluxo completo corretamente — a quantização 4-bit do 14B pode estar prejudicando o raciocínio de planejamento
- **Moonlight-16B-A3B-4bit (MLX):** Carregou após fix do `trust_remote_code`, mas **falhou no formato** — respondeu com JSON puro em vez do markdown fence ` ```tool_call`. Além disso, também inventou item_id=12345 sem listar. Modelo MoE (16B total, 3B ativo) — os 3B ativos não são suficientes para seguir instruções de formato complexo
- MLX server rodando em `mlx.minimac.local:11434` com API compatível OpenAI (swap de modelos on-demand)

## 2026-03-19 — Rodada 6 (Qwen3-8B 4bit/8bit, Gemma 3, Phi-4 — MLX Mac Mini M4, 16 GB)

| Modelo | Tempo | Tool calls | Desp. | Parou? | Sequência |
|--------|-------|------------|-------|--------|-----------|
| qwen3-8b-4bit (MLX, ~4.5 GB) | 24.5s | 1 | 0 | Sim | items_publish (ID inventado) |
| qwen3-8b-8bit (MLX, ~8.9 GB) | 33.7s | 2 | 0 | Sim | items_list → items_publish |
| gemma-3-12b-it-4bit (MLX, 8.1 GB) | 59.7s | 2 | 0 | Sim | items_list → items_publish |
| phi-4-4bit (MLX, 8.3 GB) | 41.9s | 2 | 0 | Sim | items_list → items_publish |

**Observações:**
- **Qwen3-8B-4bit (MLX):** Inventou item_id=123 sem listar primeiro — mesmo problema do Qwen3-14B-4bit
- **Qwen3-8B-8bit (MLX):** PERFEITO — fluxo completo items_list → items_publish(42), 33.7s. **Confirma que a quantização 4-bit é o problema**, não o modelo nem o runtime MLX. O mesmo Qwen3-8B em 8-bit planeja corretamente o multi-step. Resposta: "A matéria 'Incêndio na zona sul deixa 3 desabrigados' foi publicada com sucesso no jornal principal."
- **Gemma 3 12B IT:** Comportamento perfeito — fluxo completo items_list → items_publish, identificou item 42, parou sozinho. Porém o mais lento de todos os modelos testados (59.7s). Resposta final contém artefato `<end_of_turn>` que precisaria ser stripped
- **Phi-4 14B:** Também perfeito — mesmo fluxo correto, 41.9s. Resposta limpa e concisa: "A matéria sobre o incêndio foi publicada com sucesso no jornal principal." Nota: este é o Phi-4 completo (14B), não o Phi-4 Mini (3.8B) que falhou na rodada 3
- Ambos mais lentos que modelos API (esperado para MLX local), mas corretos e custo zero
- Phi-4 14B é ~30% mais rápido que Gemma 3 12B no MLX apesar de maior — melhor otimização MLX

---

## Ranking atual (brief mode, semantic loading)

| # | Modelo | Infra | Velocidade | Precisão | Stop condition | Custo-benefício |
|---|--------|-------|-----------|----------|----------------|-----------------|
| 1 | moonshotai/kimi-k2-instruct | API (SymGateway) | 1.9s | 100% | Perfeito | Excelente |
| 2 | gpt-5.4 | API (SymGateway) | 3.5s | 100% | Perfeito | Muito bom |
| 3 | grok-4-1-fast-reasoning | API (SymGateway) | 12.2s | 100% | Perfeito | Bom (lento) |
| 4 | qwen3:8b | Ollama (RTX 4070) | 20.3s | 100% | Perfeito | Excelente (custo zero) |
| 5 | qwen3-8b-8bit | MLX (Mac Mini M4) | 33.7s | 100% | Perfeito | Muito bom (custo zero) |
| 6 | phi-4-4bit | MLX (Mac Mini M4) | 41.9s | 100% | Perfeito | Bom (custo zero, lento) |
| 7 | gemma-3-12b-it-4bit | MLX (Mac Mini M4) | 59.7s | 100% | Perfeito | Bom (custo zero, mais lento) |
| 8 | qwen3-14b-4bit | MLX (Mac Mini M4) | 23.5s | 50%** | Perfeito | Parcial (pula planejamento) |
| 9 | qwen3-8b-4bit | MLX (Mac Mini M4) | 24.5s | 50%** | Perfeito | Parcial (pula planejamento) |
| 10 | llama-3.3-70b-versatile | API (SymGateway) | 17.0s | 100%* | Falhou | Ruim (desperdiça 80%) |
| 11 | phi4-mini (3.8B) | Ollama (RTX 4070) | — | 0% | — | Inviável (abaixo do floor) |
| 12 | synlogic-7b (7.6B) | Ollama (RTX 4070) | — | 0% | — | Inviável (modelo de reasoning, não agent) |
| 13 | moonlight-16b-a3b-4bit | MLX (Mac Mini M4) | 29.3s | 0% | — | Inviável (formato errado, 3B ativos) |

\* Llama completa a tarefa mas não para, gerando custo desnecessário.
\*\* Qwen3 MLX (8B e 14B) seguem formato e param, mas inventam item_id sem listar primeiro — falha de planejamento.

**Floor de qualidade identificado:** modelos abaixo de ~7-8B parâmetros não conseguem seguir instruções de formato para text-based tool calling. O Qwen3:8B é o menor modelo testado que funciona perfeitamente.

---

## Insights

1. **O problema de "LLM não sabe parar" (B-25) é do modelo, não da implementação.** Modelos melhores seguem a instrução de stop condition sem safety nets extras.

2. **Para brief mode, modelos mid-tier são suficientes.** Kimi K2 (gratuito/barato) teve performance idêntica ao GPT-5.4 (premium). Não precisa de modelo top-tier para tool execution.

3. **Reasoning overhead pode não valer a pena.** Grok 4.1 com reasoning levou 12.2s para uma tarefa que o Kimi fez em 1.9s com resultado idêntico. Para tool execution simples, reasoning é overhead.

4. **O prompt agentic funciona.** Todos os modelos com 100% de precisão chamaram as tools corretas com parâmetros corretos. A instrução "do not narrate, just call the tool" foi respeitada.

5. **Quantização 4-bit degrada raciocínio multi-step.** Qwen3-8B em 4-bit (MLX) inventa IDs sem listar primeiro, mas o mesmo modelo em 8-bit (MLX) faz o fluxo completo corretamente. A quantização 4-bit preserva a capacidade de seguir formato, mas perde a capacidade de planejamento multi-step (decidir "preciso listar antes de publicar"). Para tool calling agentic, **8-bit é o mínimo recomendado para modelos locais MLX**.

---

## Modelos a testar (futuro)

- [ ] claude-sonnet-4-6 (mid-tier Anthropic)
- [ ] claude-haiku-4-5-20251001 (budget Anthropic)
- [ ] gpt-4o (mid-tier OpenAI)
- [ ] gpt-5-mini (budget OpenAI)
- [ ] llama-3.1-8b-instant (budget API — comparar com qwen3:8b local)
- [ ] qwen/qwen3-32b (open source mid-tier via SymGateway)
- [ ] o3-mini (reasoning budget)
- [x] qwen3:8b (local Ollama — PASSED, melhor custo-benefício local)
- [x] phi4-mini (local Ollama — FAILED, abaixo do floor)
- [x] qwen3-8b-4bit (MLX Mac Mini — PARCIAL, formato OK mas pula items_list, igual 14B)
- [x] qwen3-8b-8bit (MLX Mac Mini — PASSED, perfeito! Confirma que 4-bit é o problema)
- [x] qwen3-14b-4bit (MLX Mac Mini — PARCIAL, formato OK mas pula items_list)
- [x] moonlight-16b-a3b-4bit (MLX Mac Mini — FAILED, formato errado + ID inventado)
- [x] gemma-3-12b-it-4bit (MLX Mac Mini — PASSED, perfeito mas lento 59.7s)
- [x] phi-4-4bit (MLX Mac Mini — PASSED, perfeito, 41.9s)
