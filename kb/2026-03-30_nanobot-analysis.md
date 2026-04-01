# Nanobot Monitor — Relatório 2026-03-30

**Período:** 7086f57 (2026-03-17) → 842b8b2 (2026-03-30)
**Commits:** 174
**Linhas:** +14.325 / -2.648
**Versão:** v0.1.4.post6

---

## CRÍTICA — Segurança

### 1. Remoção completa do LiteLLM (supply chain)
- **Commits:** `38ce054`, `3dfdab7`
- **O que mudou:** LiteLLM removido como dependência após vulnerabilidade de supply chain. Substituído por SDKs nativos (`anthropic`, `openai`).
- **Por que importa:** Elimina vetor de ataque supply chain. Reduz -1034 linhas de código.
- **Arquivos:** `nanobot/providers/litellm_provider.py` (removido), `nanobot/providers/openai_compat_provider.py` (novo), `nanobot/providers/anthropic_provider.py` (novo)
- **Sugestão SymbiOS:** Verificar se usam litellm. Se sim, migrar para SDKs nativos.
- **Sugestão Symbiote:** Garantir que provider registry usa SDKs diretos, não wrappers terceiros.

### 2. Verificação SPF/DKIM para emails
- **Commit:** `6e428b7`
- **O que mudou:** Email channel agora verifica Authentication-Results headers (SPF/DKIM pass). Rejeita emails que falham verificação.
- **Por que importa:** Previne spoofing de email — atacante não consegue mais se passar por usuário autorizado.
- **Arquivos:** `nanobot/channels/email.py`
- **Sugestão SymbiOS:** Implementar verificação SPF/DKIM se tiver canal email.
- **Sugestão Symbiote:** N/A (sem canal email).

### 3. Validação de URLs de mídia no Telegram
- **Commit:** `4b05228`
- **O que mudou:** URLs remotas de mídia passam por `validate_url_target()` antes de envio, prevenindo SSRF via media URLs.
- **Arquivos:** `nanobot/channels/telegram.py`
- **Sugestão ambos:** Validar URLs de mídia em todos os canais com SSRF check.

### 4. Zombie process reaping no shell
- **Commits:** `e423cee`, `e2e1c9c`
- **O que mudou:** `os.waitpid(pid, WNOHANG)` no finally block após timeout kill. Previne leak de processos zombie.
- **Arquivos:** `nanobot/agent/tools/shell.py`
- **Sugestão ambos:** Adicionar reap de processos zombie em exec tools.

### 5. Deny patterns não-configuráveis no ExecTool
- **Commits:** `746d7f5` → `1c39a4d` (revert parcial)
- **O que mudou:** Flag `enable` adicionada ao ExecTool. `deny_patterns` customizáveis foram **removidos** — só os hardcoded valem.
- **Por que importa:** Previne que config malicioso desabilite safety guards.
- **Sugestão ambos:** Safety guards devem ser hardcoded, não configuráveis.

---

## ALTA — Funcionalidades Estruturais

### 6. OpenAI-Compatible API
- **Commits:** `1814272`, `a068497`, `5550105`, `d9a5080`, `5635907`, `5e99b81`
- **O que mudou:** Endpoint `/v1/chat/completions` e `/v1/models` com session isolation. Permite usar nanobot como backend OpenAI-compatible.
- **Arquivos:** `nanobot/api/server.py`
- **Sugestão SymbiOS:** Avaliar se faz sentido expor API OpenAI-compatible para integração com ferramentas externas.

### 7. Streaming nativo em canais
- **Commits:** `bd621df`, `9d5e511`, `e79b9f4`, `cf25a58`, `5ff9146`
- **O que mudou:** `send_delta()` em BaseChannel, think-tag filtering centralizado, delta coalescing, fallback automático para canais sem streaming.
- **Arquivos:** `nanobot/channels/base.py`, `nanobot/agent/loop.py`, `nanobot/channels/telegram.py`, `nanobot/cli/stream.py`
- **Sugestão SymbiOS:** Implementar streaming progressivo. Pattern: `send_delta()` → coalesce → filter think tags → dispatch.
- **Sugestão Symbiote:** Adicionar streaming ao canal Telegram.

### 8. CompositeHook — Hooks composáveis
- **Commits:** `f08de72`, `758c4e7`, `842b8b2`
- **O que mudou:** Pattern decorator para compor múltiplos hooks no lifecycle do agente. Error isolation per-hook.
- **Arquivos:** `nanobot/agent/hook.py`
- **Sugestão SymbiOS:** Adotar pattern de hooks composáveis para extensibilidade do agent loop.

### 9. Per-session locks (concorrência)
- **Commit:** `97fe9ab`
- **O que mudou:** Lock global substituído por locks por sessão. Sessões diferentes processam em paralelo; mesma sessão serializa.
- **Arquivos:** `nanobot/agent/loop.py`
- **Sugestão SymbiOS:** Migrar de lock global para per-session se suportar múltiplas sessões.

### 10. Multimodal tool perception
- **Commits:** `71a88da`, `9f10ce0`, `445a96a`
- **O que mudou:** Tools podem processar imagens nativamente. `read_file()` lê imagens, `web_fetch()` extrai conteúdo multimodal.
- **Arquivos:** `nanobot/agent/tools/filesystem.py`, `nanobot/agent/tools/web.py`, `nanobot/agent/context.py`
- **Sugestão ambos:** Avaliar suporte a visão nativa nas tools.

### 11. Prompt cache optimization (Anthropic)
- **Commit:** `bd09cc3`
- **O que mudou:** System prompt tornado estático (timestamp movido para prefixo de mensagem). Cache breakpoint na penúltima mensagem. ~90% saving em tokens cached.
- **Arquivos:** `nanobot/agent/context.py`, `nanobot/providers/anthropic_provider.py`
- **Sugestão SymbiOS:** Implementar cache breakpoints — impacto direto em custo.

### 12. Media message support cross-channel
- **Commits:** `bc9f861`, `25288f9`, `11e1bbb`, `d7373db`
- **O que mudou:** Agent context suporta mídia. WhatsApp, QQ, WeChat enviam/recebem imagens e arquivos.
- **Arquivos:** `nanobot/agent/tools/message.py`, `nanobot/channels/whatsapp.py`, `nanobot/channels/qq.py`, `nanobot/channels/weixin.py`

### 13. Message send retry com backoff exponencial
- **Commits:** `5e9fa28`, `f0f0bf0`
- **O que mudou:** `send_max_retries` (default 3) com backoff exponencial (1s, 2s, 4s).
- **Arquivos:** `nanobot/channels/manager.py`
- **Sugestão ambos:** Adicionar retry com backoff em envio de mensagens.

---

## MÉDIA — Providers e Refatorações

### 14. Novos providers: Mistral, OpenVINO, Step Fun
- **Commits:** `7878340`, `f64ae3b`, `813de55`
- **O que mudou:** 3 novos providers, todos OpenAI-compatible backend.
- **Arquivos:** `nanobot/providers/registry.py`

### 15. Canal WeChat (WeiXin) pessoal
- **Commits:** múltiplos
- **O que mudou:** Novo canal via HTTP long-poll (ilinkai API). QR login, media upload CDN, session persistence.
- **Arquivos:** `nanobot/channels/weixin.py`

### 16. Channel login abstraction
- **Commit:** `556b21d`
- **O que mudou:** `login()` abstraído em BaseChannel. CLI usa interface unificada.
- **Arquivos:** `nanobot/channels/base.py`

### 17. Cron: timezone + workspace-scoped + run history
- **Commits:** `13d6c0a`, `4a7d7b8`, `c33e01e`, `09ad9a4`
- **O que mudou:** Timezone IANA configurável, store por workspace (não mais global), histórico de execuções.
- **Arquivos:** `nanobot/cron/`, `nanobot/config/schema.py`

### 18. Feishu: CardKit streaming + code blocks + thread reply
- **Commits:** `0ba7129`, `d9cb729`, `4145f3e`

### 19. Gemini thought signatures + o1 max_completion_tokens
- **Commits:** `af84b1b`, `b5302b6`, `ef10df9`

### 20. Onboard wizard interativo
- **Commits:** múltiplos
- **O que mudou:** Setup interativo via CLI com autocomplete de modelos.
- **Arquivos:** `nanobot/cli/onboard.py`

### 21. Unified agent runner lifecycle
- **Commits:** `5bf0f6f`, `e7d371e`
- **O que mudou:** Runner extraído e compartilhado. Subagent preserva progresso em falha.

### 22. /status command
- **Commits:** `a628741`, `064ca25`
- **O que mudou:** Comando `/status` mostra info de runtime (provider, model, channels, uptime).

### 23. Reestruturação de testes
- **O que mudou:** Testes reorganizados em subpastas (`tests/tools/`, `tests/providers/`, `tests/security/`, `tests/config/`, `tests/cron/`, `tests/cli/`).

---

## BAIXA — Docs e Cosmético

- Docs: providers info, README crypto disclaimer, channel plugin guide, Mistral intro
- Fix: grammar no skill-creator, whitespace cleanup, f-string cleanup
- mypy proposal (sem implementação)
- Logo com fundo transparente

---

## Resumo de Impacto para SymbiOS e Symbiote

| Prioridade | Item | SymbiOS | Symbiote |
|------------|------|---------|----------|
| CRÍTICA | Remover litellm / usar SDKs nativos | ✅ Verificar | ✅ Verificar |
| CRÍTICA | SPF/DKIM em email | ✅ Se aplicável | — |
| CRÍTICA | SSRF check em media URLs | ✅ | ✅ |
| CRÍTICA | Zombie process reaping | ✅ | ✅ |
| ALTA | Streaming em canais | ✅ Prioridade | ✅ Telegram |
| ALTA | Per-session locks | ✅ | — |
| ALTA | Prompt cache optimization | ✅ Custo direto | — |
| ALTA | CompositeHook pattern | ✅ | — |
| ALTA | Message retry + backoff | ✅ | ✅ |
| MÉDIA | Novos providers | Avaliar | — |
| MÉDIA | Cron timezone/scoping | ✅ Se usa cron | — |
