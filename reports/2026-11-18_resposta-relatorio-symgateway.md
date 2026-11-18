# Resposta do Symbiote ao relatório do SymGateway sobre as versões 0.8.10 a 0.8.12

Data: 2026-11-18 · Correções na engine: Symbiote v0.8.13 · Lido: o relatório de vocês e a branch `agent/sgt-symbiote-subject-retencao` (commit `497e9703`), só leitura

Podem fixar a v0.8.12: a análise de vocês está certa no que decide isso, e nada do lado do SymGateway é obrigatório. Este documento confirma o que conferimos, corrige dois pontos, aponta o que a retenção da branch ainda deixa para trás e diz o que a engine vai passar a fazer para vocês não precisarem dos contornos.

Os testes de vocês (51 da integração, 33 do Assistant) nós não rodamos.

## O que conferimos e bate

- **forge-llm.** Só o extra `llm` o instala. Quem não instala o extra não recebe a v0.7.18.
- **Motivo de parada `budget`.** Só aparece com teto configurado. Os dois tetos (`max_turn_tokens`, `max_turn_llm_calls`) têm padrão 0, que é sem teto.
- **Contagem de tokens da Anthropic.** A mudança está no `ForgeLLMAdapter`. Com adaptador próprio, vale o que o adaptador de vocês reporta.
- **Resumo ao fechar.** No modo padrão são as últimas cinco mensagens emendadas, texto literal, e `kernel.prune()` não o apaga. `session_summary_mode="off"` resolve desde a v0.8.10, e a branch aplica isso também aos symbiotas já criados.
- **Migrações.** Aplicadas sozinhas ao subir.

## Duas correções

**Migração de partida.** A v0.8.8, que é o `main` de vocês, está na migração 5 do PostgreSQL. A 6 é da v0.8.9, que só existe na branch. Em produção o salto é de 5 para 12. Não muda nada na prática; vale só se alguém conferir o número.

**A sessão fechada não é reaberta.** O comentário de `close_session` na branch diz que o turno seguinte reabre a sessão. `get_or_create_session` só encontra sessão ativa: depois do fechamento, ele cria outra com a mesma `external_key`. Cada turno vira uma linha nova em `sessions` e uma em `session_scores`. Como o histórico que o modelo vê é o de vocês, nada se perde na conversa.

## O que a retenção da branch ainda deixa

Para uma pessoa que continua usando o Assistant, três coisas nunca envelhecem:

- **`audit_log`.** Guarda os argumentos de cada chamada de ferramenta, depois do redator que vocês instalam. `kernel.prune()` não tem janela para essa tabela.
- **Linhas de `sessions` e de `session_scores`.** Uma por turno. Não têm texto de conversa, mas crescem sem limite.
- **A janela de auditoria de 90 dias.** Ela só é aplicada apagando o symbiota inteiro, e isso exige que todas as sessões tenham terminado há mais de 90 dias. Quem usa o Assistant toda semana nunca chega lá.

Nada disso impede o land da branch: o que ela promete para mensagens, traces e resumos está cumprido. São os restos que ela não alcança com o que a engine oferece hoje.

## O que muda na engine

O furo que vocês acharam é uma lacuna da engine, e vale para qualquer host. Registramos como B-100 e B-101 e corrigimos as duas no Symbiote v0.8.13, já publicado. Tudo desligado por padrão, como as janelas de hoje.

| Novo em `kernel.prune()` | `KernelConfig` | O que remove |
|---|---|---|
| `summaries_days=N` | `summary_retention_days` | o resumo das sessões fechadas há mais de N dias |
| `audit_days=N` | `audit_retention_days` | linhas de `audit_log`, `reflection_audit` e `skill_review_audit` criadas há mais de N dias, e os pedidos de aprovação resolvidos há mais de N dias, em qualquer sessão |
| `sessions_days=N` | `session_retention_days` | as sessões fechadas há mais de N dias, com tudo que está preso a elas (mensagens, traces, scores, auditoria) |
| `basis="age"` | `retention_basis="age"` | faz as janelas de mensagens e de traces valerem pela idade da linha, também em sessão ativa |

As memórias de longo prazo continuam com a janela delas (`memory_retention_days`) e não são apagadas junto com a sessão.

### O que vocês vão poder apagar

- **O `UPDATE sessions SET summary = NULL`** direto na nossa tabela: `summaries_days` faz isso, e o esquema do Symbiote deixa de ser tocado por fora.
- **O fechamento da sessão a cada turno e o período de retenção dentro da `external_key`.** Com `retention_basis="age"`, as mensagens e os traces envelhecem também numa sessão que fica aberta por meses.
- **O apagamento do symbiota como janela de auditoria**, se quiserem: `audit_days` e `sessions_days` aplicam os 90 dias também a quem continua ativo.

Uma configuração equivalente à de vocês ficaria assim:

```python
KernelConfig(
    message_retention_days=30,
    trace_retention_days=30,
    retention_basis="age",
    summary_retention_days=30,
    audit_retention_days=90,
    usage_retention_days=90,
    session_retention_days=90,
)
kernel.prune()   # do cron de vocês, como hoje
```

Um cuidado para quando adotarem: `sessions_days` só alcança sessão fechada. Uma sessão que nunca é fechada fica com a linha em `sessions`, já sem mensagens. O laço de vocês que fecha as sessões paradas continua útil para isso.

## Sobre o teto de gasto por turno

Cabe para o caso dos 109 mil tokens. `max_turn_llm_calls` funciona com qualquer adaptador. `max_turn_tokens` conta o que o adaptador reporta em `LLMResponse.usage` ou por `report_usage`; se o adaptador de vocês não reporta o uso, esse teto nunca dispara.
