# Migração Clark → Discovery + Tool Loop Nativo

> Plano de migração do Clark (YouNews) para usar o Discovery Service e
> ChatRunner nativo do Symbiote v0.2.5+, eliminando o `YouNewsChatRunner`
> customizado e o registro manual de tools.

## Status: ✅ Migração concluída (2026-03-19)

Todos os passos foram implementados. O Clark agora usa:
- `DiscoveredToolLoader` com `header_factory` para auth dinâmica
- `ChatRunner` nativo com `tool_loop=True`
- `<symbiote-chat>` web component com adapter pattern
- Fallback automático para `register_clark_tools()` se não houver tools descobertas

---

## Crítica do YouNews (2026-03-19) — Resolvida

### Falha crítica identificada → RESOLVIDA

O `DiscoveredToolLoader` não suportava `body_template`, `optional_params`
nem `array_params`. Agora o loader **auto-deriva** esses campos a partir do
schema OpenAPI armazenado no `DiscoveredTool`:

- POST/PUT/PATCH: `body_template` gerado das properties (excluindo path params)
- GET/DELETE: `optional_params` derivado de params não-required
- `array_params` detectado via `type: "array"` no schema
- `header_factory` aceito como argumento global no `load()`

### Regressão UX: badges SSE → NÃO SE APLICA

O YouNews continua usando `clark_streaming.py` com `emit_event()` para
`tool_start`/`tool_done`. O `<symbiote-chat>` web component renderiza os
badges automaticamente. Não há regressão.

### Frontend usa tool_ids → RESOLVIDO

O `clark.html` foi refatorado para o web component `<symbiote-chat>`.
O adapter (`clark-adapter.js`) usa `data.name` como label.
Único tool_id hardcoded (`yn_search`) permanece inalterado (handler custom).

### Ordem dos passos corrigida → EXECUTADA

1. ✅ Verificar frontend (hardcoded tool_ids) — OK, refatorado para web component
2. ✅ Estender DiscoveredToolLoader (body_template, optional_params, header_factory)
3. ✅ Substituir register_clark_tools por DiscoveredToolLoader + search custom
4. ✅ Remover YouNewsChatRunner (deletado `clark_runner.py`)
5. ✅ Testar fluxo completo
6. ✅ SSE tool badges — já funcionam via `clark_streaming.py` + `<symbiote-chat>`

### Sugestões aceitas → IMPLEMENTADAS

- **Feature flag**: fallback automático — se não há tools descobertas, usa `register_clark_tools()` legacy
- **Tag dedicada**: `symbiote classify --approve Items,Compose,Inbox,Capture,Analytics,Search` disponível
- **ContextVar helpers**: `set_auth_token`, `_get_user_id` etc. mantidos em `clark_tools.py`

---

## O que foi feito (Symbiote v0.2.5+)

1. `symbiote discover --url http://localhost:8000` → 240 tools com tags do OpenAPI
2. Todas aprovadas no server hosted (porta 8008)
3. `tool_tags=["Items", "Compose", "Inbox", "View", "Capture", "Analytics", "Search"]` configurado
4. `ChatRunner` nativo com tool loop validado (benchmark: Kimi K2, 10.3s, 100% precisão)
5. `pyproject.toml` já aponta para `symbiote>=v0.2.5`
6. `_register_clark_runner()` usa `ChatRunner` nativo com `native_tools=True`
7. `DiscoveredToolLoader.load()` aceita `header_factory` e auto-deriva `body_template`/`optional_params`/`array_params`
8. `symbiote classify` — auto-aprovação de tools por tags OpenAPI (`--approve`, `--disable-rest`, `--reset`, `--summary`)

## O que foi feito (YouNews)

1. `clark_runner.py` deletado (dead code — `YouNewsChatRunner` não era mais usado)
2. `app.py` migrado: `_register_clark_tools_discovery()` usa `DiscoveredToolLoader` + search custom
3. Fallback: se não há tools descobertas, cai para `register_clark_tools()` legacy
4. `clark_streaming.py` docstring atualizada (referência ao YouNewsChatRunner removida)
5. `<symbiote-chat>` web component em produção com adapter completo
6. Vendor bundle `symbiote-chat.js` já está na versão mais recente

## Arquitetura final

```
┌─ YouNews app.py ──────────────────────────────────────────┐
│                                                            │
│  _init_clark_kernel()                                      │
│    ├── SymbioteKernel (config, LLM)                        │
│    ├── _register_clark_tools_discovery()                    │
│    │     ├── DiscoveredToolLoader.load(header_factory=...)  │
│    │     │     └── auto-derive body_template/optional_params│
│    │     ├── yn_search (custom handler, in-process)         │
│    │     └── fallback → register_clark_tools() legacy       │
│    └── _register_clark_runner()                             │
│          └── ChatRunner(native_tools=True, tool_loop=True)  │
│                                                            │
│  Frontend: <symbiote-chat> + clark-adapter.js               │
│    ├── SSE streaming (text_delta, tool_start, tool_done)    │
│    ├── Tool badges automáticos                              │
│    └── Adapter hooks (textFilter, onMessageRendered, etc.)  │
└────────────────────────────────────────────────────────────┘
```

## Validação

- [x] Clark responde com tools no endpoint `/clark/chat`
- [x] Clark faz tool loop (list → publish) sem `YouNewsChatRunner`
- [x] SSE streaming funciona em `/clark/chat/stream` (text_delta + response_done + tool badges)
- [x] Auth token é propagado para tools HTTP (header_factory no DiscoveredToolLoader)
- [x] Search funciona (handler custom, sem deadlock)
- [x] Tags filtram corretamente (Clark não vê tools de Admin/Config/Plugins)

## Referências

- Symbiote v0.2.5 CHANGELOG: tool loop, semantic loading, discovery --url
- Symbiote v0.2.6: classify command, DiscoveredToolLoader header_factory
- Benchmark: `scripts/compare_modes_llm.py` — 13 modelos testados, Kimi K2 melhor custo-benefício
- Discovery report: 240 tools, 29 tags, 62 no contexto do Clark
