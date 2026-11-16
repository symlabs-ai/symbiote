# Symbiote demo

Um chat com um assistente pessoal (o **Sol**) e dez abas que mostram, enquanto acontece, o que o kernel faz por dentro: contexto montado, chamadas ao modelo, tools, aprovações, memória, aprendizado, skills, harness e web. Roda só na sua máquina e conversa com um LLM de verdade (Anthropic, OpenAI ou pelo SymGateway).

O código do demo também é um exemplo de integração: `runtime.py` monta um `SymbioteKernel` embutido do jeito que um host deveria (LLM, tools de todo tipo, gate de aprovação, hooks, histórico injetado a cada turno, encerramento de sessão).

## Como rodar

```bash
bash demo/setup.sh   # uma vez: .venv, extras [llm,browser,extract], mcp e o Chromium (~170 MB)
bash demo/run.sh     # http://127.0.0.1:8010
```

Na primeira abertura a página pede o provider e a chave. Ela é validada com uma chamada curta e gravada em `demo/demo.env` (fora do git, permissão 0600); depois disso o demo abre direto. A engrenagem no topo da página abre as **configurações do LLM**: mostra o provider, o servidor, os modelos e a chave (mascarada) em uso, deixa editar cada um e tem um botão **Testar**, que faz chamadas reais com os valores do formulário (lista de modelos do gateway, uma chamada curta ao modelo principal e outra ao auxiliar) e mostra o resultado e o tempo de cada passo sem salvar nada. "Salvar e aplicar" valida, grava no `demo.env` e reinicia o kernel do demo. Modelos padrão: `claude-sonnet-4-5` + `claude-haiku-4-5` (Anthropic) ou `gpt-4.1` + `gpt-4.1-mini` (OpenAI). O modelo auxiliar roda o trabalho de fundo (reflexão por LLM, evolução do harness, seleção semântica de tools, dream, review de skills).

**Pelo SymGateway.** Escolha *SymGateway* na tela de abertura, cole a chave do gateway (ela substitui a do provider e precisa de `models.read`, `work_sessions.create` e `gateway.llm.invoke`) e clique em *Listar modelos liberados*: a lista vem de `GET /v1/models`, e o modelo principal sai dela. A URL fica em branco para o gateway público (`https://symgateway.symlabs.ai`); o projeto só se a chave for de um projeto (vai em `X-Project-Slug`). Cada sessão do Symbiote abre uma sessão de trabalho no gateway (`POST /_api/work-sessions`), e todas as chamadas daquela conversa, inclusive a reflexão e o dream ao encerrar, levam o mesmo `X-SymGateway-Session-Id`. Quem faz isso é o `SymGatewayLLMAdapter` do kernel (`src/symbiote/adapters/llm/symgateway.py`).

Opcional, com qualquer provider: `SYMGATEWAY_API_KEY` e `SYMGATEWAY_BASE_URL` no `demo.env` habilitam a tool `web_search`. Ela não fala com um buscador direto: invoca o serviço `websearch` do Objective `web_seek` do gateway (`POST /v1/objectives/invoke`), e o gateway escolhe a conta, a credencial e os fallbacks. A chave precisa de `objectives.use` e `external_services.invoke`, que `gateway.llm.invoke` não inclui; sem elas a tool falha com o motivo dado pelo gateway e o Pesquisador cai para `web_extract` e o navegador. `DEMO_SEARCH_OBJECTIVE` no `demo.env` troca o Objective; `none` volta ao proxy Brave (`/proxy/brave/web-search`). As configurações do LLM mostram o caminho em uso na linha "Busca na web".

## O que tem em cada aba

| Aba | Mostra |
|---|---|
| Ao vivo | Linha do tempo de cada turno: contexto, chamadas ao modelo (texto bruto), tools com parâmetros e resultado, bloqueios da política, aprovações, iterações e motivo de parada. Ao lado, o que roda fora do turno |
| Contexto | O que o modelo viu no último turno: persona, memórias e knowledge injetados, histórico, tools visíveis × autorizadas, orçamento de tokens e as mensagens exatas enviadas |
| Memória | Memórias de longo prazo por categoria, ensinar um fato, testar o recall e o isolamento entre symbiotes |
| Aprendizado | O que encerrar a sessão produziu (score, memórias, reflexão), modos de reflexão e dream, relatórios de dream, histórico de scores, feedback 👍/👎 |
| Tools | Descoberta via OpenAPI com aprovação, tools HTTP declarativas, handlers Python, MCP, builtins, matriz registrada/visível/autorizada, tags, gate de aprovação, audit log, dados da API pessoal |
| Execução | Modos `instant`/`brief`/`long_run`/`auto`, carga `full`/`index`/`semantic`, `packed`/`on_demand`, limites, long-run com planejador e avaliador, traces, process engine |
| Skills | Review em segundo plano, quarentena e promoção, conteúdo dos `SKILL.md` |
| Harness | Prontidão para evoluir, ParameterTuner, versões das instruções com evolução e rollback, aprendizado entre symbiotes |
| Web | `web_extract`, `browser_*` com capturas de tela, delegação ao Pesquisador, guardas de rede |
| Sistema | Persona, knowledge, sessões, as capabilities `teach`/`show`/`reflect`/`work`, testes de segurança, retenção, CLI e Console no mesmo banco, trocar chave, zerar o demo |

O botão **Roteiro** abre 14 passos que percorrem tudo isso para uma apresentação.

## Como está montado

```
navegador ── <symbiote-chat mode="inline">  POST /api/chat/stream (SSE)
          └─ abas                            GET /api/events (SSE) + /api/*
servidor (FastAPI, 127.0.0.1)
  ├─ runtime.py    SymbioteKernel + RecordingLLM + DemoHook + ApprovalBroker
  ├─ /pa           API pessoal fictícia (tarefas, agenda, contatos, notas, despesas, e-mail simulado)
  ├─ mcp_server.py servidor MCP de exemplo (stdio)
  └─ demo/data/    demo.db, pa.db, sandbox/, skills/, shots/  (fora do git)
```

- **Dois symbiotes.** O Sol tem as tools pessoais; o Pesquisador tem as tools web e só enxerga as tags `web`/`browser`/`utilidades`. O Sol delega a ele com `spawn`.
- **Streaming.** O kernel valida a resposta antes de liberar o texto, então o chat recebe a resposta pronta. O texto bruto de cada chamada ao modelo aparece na aba Ao vivo.
- **Segurança local.** Bind em `127.0.0.1`, checagem do `Host`, e um token por execução exigido em toda chamada que muda estado. `fs_*` e `bash` ficam presos a `demo/data/sandbox`; `bash` e as tools de risco alto pedem aprovação.
- **Links e miniaturas.** Endereços na resposta viram links que abrem em outra aba, e os primeiros seis ganham um cartão com miniatura, título e site. Quem busca a página é o servidor do demo (`previews.py`, `POST /api/preview`): lê as tags Open Graph, baixa a imagem e a serve de `demo/data/previews/`, então o navegador não fala com o site citado. Cada endereço, redirecionamento e imagem passa pela mesma proteção do `web_extract` contra endereços internos; só entram imagens raster de até 3 MB. Página sem `og:image` fica sem cartão.
- **Dados.** Tudo fica em `demo/data/`. "Zerar demo" (aba Sistema) apaga e recria.

## Testes

```bash
.venv/bin/python -m pytest demo/tests
```

Sobem o servidor numa porta livre com um LLM roteirizado no lugar do provider: setup sem chave, proteção local, timeline de um turno, histórico injetado, aprovação pela página, bloqueio da política, descoberta → aprovação → chamada à API pessoal, MCP, aprendizado ao encerrar e os testes de segurança.
