# Resposta do Symbiote à réplica do SymbiOS

Data: 2026-11-17 · Base: Symbiote v0.8.10 · Entrega: Symbiote v0.8.11

Os dois pontos que vocês mantiveram entraram na engine: a sessão presa a uma revisão (A) e o teto de gasto por turno (B). O ponto C foi retirado por vocês depois da medição, e concordamos com a leitura dos números.

A referência completa está em `docs/HOST_INTEGRATION.md`, nas seções "Revisions: changing a symbiote while it serves people" e "Spending ceiling of a turn".

## A. Sessão presa a uma revisão

Entrou como vocês desenharam, com os nomes abaixo.

```python
kernel.create_revision(symbiote_id, "2026-11-a", note="política de reembolso revisada")
sessao = kernel.start_session(symbiote_id, subject_key="visitante-42", revision="2026-11-a")
rascunho = kernel.start_session(symbiote_id)      # sem revisão: a configuração atual

kernel.list_revisions(symbiote_id)
kernel.inspector.list_revisions(symbiote_id)      # com as sessões presas a cada uma
kernel.set_session_revision(sessao.id, "2026-11-b")   # ou None: volta para a atual
kernel.retire_revision(symbiote_id, "2026-11-a")      # não aceita sessão nova
```

**O que a revisão guarda:** nome, papel e persona; o `EnvironmentConfig` inteiro, com a lista de ferramentas; e os itens de conhecimento, copiados. Remover um item depois não o tira da revisão.

**O que fica fora, compartilhado por todas as sessões:** memórias, procedimentos, pessoas atendidas (`subject_key`) e os textos do harness, como vocês propuseram.

**Política de ferramentas:** não existe override por turno. A revisão é criada a partir do que as chamadas comuns já validaram e gravaram. Numa sessão presa, a ferramenta que a revisão não lista é recusada mesmo que a configuração atual permita, e a que ela lista roda mesmo que a atual não permita mais.

**Registro:** `Session.revision`, `inspector.list_sessions(revision=...)`, a coluna `revision` em `turn_contexts` e em `llm_usage`, e `inspector.usage_by("revision", symbiote_id)` para comparar duas revisões.

### Cinco decisões que vocês precisam conhecer

A réplica deixava estes pontos em aberto. Decidimos assim:

- **A definição das ferramentas não é versionada.** A revisão fixa quais ferramentas a sessão pode chamar, não a URL nem os parâmetros de uma ferramenta HTTP. Se o dono editar a definição, a sessão presa enxerga a edição. Para versionar a definição, registrem a nova com outro id.
- **O que roda depois do encerramento segue a configuração atual.** Resumo, reflexão, sonho e revisão de procedimentos são do symbiota, não da conversa. A revisão vale durante os turnos.
- **Sessão que já existe não troca de revisão sozinha.** `get_or_create_session(..., revision="v2")` dá a revisão à sessão que criar; a que já existe continua na que tem, e `Session.revision` diz qual. Para mover, `set_session_revision`. Assim a conversa em andamento não muda de configuração porque vocês publicaram outra.
- **Delegação por rótulo.** Uma sessão presa a `L` que chama `spawn` delega para a revisão `L` do colega, se ele tiver uma que não foi aposentada. Se não tiver, o colega lê a configuração atual dele. Para publicar uma equipe, criem o mesmo rótulo em cada membro.
- **Rótulo nunca é reaproveitado.** `create_revision` com um rótulo que o symbiota já tem falha. Uma revisão aposentada não aceita sessão nova, e as que já estão presas continuam.

### O que vocês podem apagar

O embrulho das três leituras guiado por contextvar, inclusive o acesso a `kernel._identity.get`, e o teste que avisa se a engine passar a ler a persona por outro caminho. A engine agora resolve a revisão nos mesmos três pontos (`EnvironmentManager.get_config`, `IdentityManager.get`, `KnowledgeService.query`) e tem os testes dela para isso.

Um aviso para a migração: dentro de um turno de sessão presa, `kernel.environment.get_config()` devolve a configuração da revisão. Para ler a que está gravada agora, usem `kernel.environment.current_config()`. `configure()` sempre edita a atual.

## B. Teto de gasto por turno

```python
kernel.environment.configure(
    symbiote_id=symbiote_id,
    max_turn_llm_calls=8,      # funciona sempre
    max_turn_tokens=60_000,    # precisa de adapter que informa uso
)
outcome = kernel.turn(session_id, texto)
outcome.stop_reason == "budget"
```

Os dois campos nascem desligados (`0`). Isto fecha o B-36.

- **O que é contado:** toda chamada ao modelo feita para o turno, inclusive as de um symbiota delegado, e os tokens que os adapters informaram.
- **Quando é conferido:** antes de cada rodada do laço de ferramentas. A primeira chamada sempre roda, e uma única chamada pode passar do teto: o teto impede a próxima, não corta uma no meio.
- **O que para:** nenhuma chamada nova começa, nem a chamada extra de resposta que o limite de passos faz. O turno devolve um texto fixo e `stop_reason="budget"`, e vocês decidem o que mostrar.
- **`max_turn_tokens` depende de uso informado.** Se o adapter de vocês não chamar `report_usage` nem devolver `usage`, esse teto não tem o que contar. `max_turn_llm_calls` não depende disso.
- **É por turno.** Teto por sessão, por dia ou por visitante fica com o host, somando `inspector.usage_totals`.

## C. Textos do prompt por idioma

Retirado por vocês. Conferimos os arquivos da medição: 560 chamadas, 250 respostas com texto na condição em inglês e 240 na condição em português, uma troca de idioma em cada, as duas em resposta a "thanks!". Não há efeito.

Uma ressalva nossa, que não muda a conclusão: a medição foi na v0.8.9. Os textos que entraram na v0.8.10 (o bloco de pedidos resolvidos, o aviso de procedimento em teste e a mensagem de ação pendente) não estavam nos prompts medidos. São da mesma natureza dos que vocês mediram.

## O que muda ao atualizar

- Migrações 16 e 17 no SQLite, 10 e 11 no PostgreSQL, aplicadas ao subir.
- `stop_reason` ganha o valor `budget`, só para symbiotas com teto configurado.
- Sessões e as linhas de `list_sessions`, `list_turn_contexts` e `list_llm_calls` ganham a chave `revision`.
- `POST /sessions` passa pelo kernel e aceita `revision`.
