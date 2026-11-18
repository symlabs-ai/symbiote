# Resposta do Symbiote aos pedidos 17 a 19 do SymbiOS

Data: 2026-11-18 · Base: Symbiote v0.8.11 · Entrega: na release seguinte à v0.8.11

Os três pedidos entraram na engine, com as quatro decisões como vocês responderam. Nenhum foi rejeitado. A referência completa está em `docs/HOST_INTEGRATION.md`, nas seções "Narrowing a session's tools", "Delegation between symbiotes published at different times" e "Registering the operations yourself".

## 17. Restringir as ferramentas de uma sessão

```python
visitante = kernel.start_session(
    symbiote_id, subject_key="visitante-42", revision="2026-11-a",
    tools_allowed=["buscar_produtos", "search_knowledge"],
)
kernel.set_session_tools(visitante.id, ["buscar_produtos"])   # vale no próximo turno
kernel.set_session_tools(visitante.id, None)                   # tira a restrição
```

- **Só estreita.** A sessão vê e chama a interseção entre a lista dela e o que a configuração lida autoriza (a revisão, ou a atual). Uma ferramenta na lista que a configuração não autoriza continua recusada.
- **`None` é sem restrição; `[]` é nenhuma ferramenta.**
- **Fora da lista, fora do prompt.** A ferramenta não vai ao modelo, nos dois `tool_visibility`. Se for pedida, o portão de política recusa e grava `blocked` na auditoria. O modelo lê `Tool 'x' blocked: Tool 'x' is not available in this conversation`.
- **Acompanha a delegação.** A sub-sessão do colega carrega a mesma lista, e `spawn` precisa estar nela para a delegação acontecer.
- **Chamada adiada.** `resolve_pending` confere a lista que a sessão tem na hora em que a chamada finalmente roda.
- **Registro.** `Session.tools_allowed` e as linhas de `inspector.list_sessions`.
- **Sessão que já existe.** `get_or_create_session(..., tools_allowed=[...])` dá a lista à sessão que criar; a que já existe mantém a dela. Para mudar, `set_session_tools`.

### Meta-ferramentas de modo

A restrição vale para elas, e o prompt deixa de mandar usar as cortadas, inclusive o bloco "Context Access". O complemento que vocês pediram existe:

```python
kernel.environment.mode_tools(symbiote_id)
# [] | ["get_tool_schema"] | ["search_memories", "search_knowledge"] | os três
liberadas = ["buscar_produtos", *kernel.environment.mode_tools(symbiote_id)]
```

Dois comportamentos que vocês precisam conhecer:

- **`on_demand` sem `search_knowledge`:** a sessão fica sem alcançar o conhecimento. A engine não injeta nada no lugar, porque deixar de fora pode ser a intenção do host.
- **`index` sem `get_tool_schema`:** a sessão recebe os esquemas completos das ferramentas dela em vez do índice. Isso não amplia acesso (são as ferramentas que ela já pode chamar) e evita que fiquem inutilizáveis.

### O que vocês podem apagar

O embrulho de `environment.get_config` que tira as ferramentas no turno de visitante.

## 18. Delegação quando o colega não tem o mesmo rótulo

```python
kernel.set_default_revision(colega_id, "colega-v7")     # None: sem padrão
kernel.get_default_revision(colega_id)

publico = kernel.start_session(symbiote_id, revision="loja-v3", delegation="strict")
kernel.set_session_delegation(publico.id, "open")       # vale no próximo turno
```

| Sessão que delega | O colega responde com |
|---|---|
| presa a `L` | a revisão `L` dele, se existir e não estiver aposentada; senão a padrão dele; senão a configuração atual |
| não presa | a configuração atual |
| `delegation="strict"`, presa a `L` | a revisão `L`; senão a padrão; senão a delegação é recusada |
| `delegation="strict"`, não presa | a padrão; senão a delegação é recusada |

- **A padrão só vale como destino de delegação.** `start_session` sem `revision` continua lendo a configuração atual.
- **Uma por symbiota.** Definir outra move a padrão. Precisa ser uma revisão que aceita sessões.
- **O modo é da sessão**, como vocês pediram, e acompanha a delegação: a sub-sessão também fica estrita.
- **Recusa.** O `spawn` falha com `delegation to 'colega' refused: it has no revision 'loja-v3' and no default revision, and this conversation only delegates to a published revision`. O modelo lê isso como erro da ferramenta, nenhuma sub-sessão é criada, e a linha de auditoria da chamada fica com `result = 'error:DelegationRefusedError'`.
- **Registro.** A sub-sessão grava a revisão que respondeu; `is_default` aparece em cada revisão e em `inspector.list_revisions`; `Session.delegation` e `inspector.list_sessions` mostram o modo.

Duas regras que não estavam no pedido:

- **A revisão padrão não pode ser aposentada.** `retire_revision` falha com `ValidationError` enquanto ela for a padrão; movam a padrão antes. Sem isso, aposentar deixaria o symbiota sem padrão em silêncio, e a delegação aberta voltaria ao rascunho.
- **Sessão estrita e não presa** usa a padrão do colega, e é recusada se não houver. Estrito quer dizer "o colega só responde por uma revisão", também para quem testa o rascunho.

## 19. Ler uma descrição OpenAPI sem gravar nem autorizar

```python
from symbiote.discovery import http_config_for, parse_openapi
from symbiote.environment.descriptors import ToolDescriptor

erros: list[str] = []
for op in parse_openapi(spec, base_url="https://api.fornecedor.example", errors=erros):
    kernel.tool_gateway.register_http_tool(
        ToolDescriptor(
            tool_id=f"fornecedor__{op.tool_id}", name=op.name,
            description=op.description, parameters=op.parameters,
            tags=op.tags, risk_level=op.risk_level,
        ),
        http_config_for(
            op.method, op.url_template, op.parameters,
            header_factory=lambda: {"Authorization": f"Bearer {token()}"},
        ),
    )
```

- **`parse_openapi(spec, base_url=None, *, errors=None)`** devolve um `OpenAPIOperation` por operação: `tool_id` (nome sugerido), `operation_id`, `name`, `description`, `method`, `path` (com o modelo da query), `url_template`, `parameters`, `tags` e `risk_level`.
- **`http_config_for(method, url, parameters, *, headers=None, header_factory=None, allow_internal=False, timeout=30.0)`** devolve o `HttpToolConfig`.
- **Nenhuma das duas grava, registra ou autoriza.** As regras de codificação, origem e rede continuam na execução, dentro do gateway.
- **É a mesma conversão** que `discover_tools(spec=...)` grava: o caminho interno passou a usar essas funções.

Diferenças em relação ao que vocês pediram:

- **Operação malformada** é pulada e, se vocês passarem uma lista em `errors`, relatada. O que não é uma descrição OpenAPI (sem `paths`) dá `ValidationError`.
- **`base_url`** perde a barra final. Sem `base_url` e sem `servers`, `url_template` fica relativo.
- **`http_config_for`** aceita também `headers` fixos.
- **O tipo do corpo** (`multipart/form-data`, por exemplo) continua vindo dentro de `parameters`, na chave `_content_type`, e `http_config_for` o lê de lá.

### O que vocês podem apagar

O uso das oito funções privadas (`_iter_operations`, `_openapi_params_to_schema`, `_normalize_path`, `_query_template`, `_risk_for_operation`, `_slugify`, `_path_to_tool_id`, `_derive_http_config`). Elas continuam existindo, mas sem garantia de nome.

### Um cuidado com os nomes

Os ids de ferramenta são um espaço só por kernel: registrar um id de novo substitui a ferramenta para todos os symbiotas. Reproduzimos isso com duas descrições que têm `search`, carregadas para dois symbiotas pelo `load_discovered_tools`: a segunda carga trocou a URL da ferramenta do primeiro. Ficou no nosso backlog como B-99. Para vocês, que registram com prefixo por integração, o cuidado é manter o prefixo único por integração, não por symbiota.

## O que muda ao atualizar

- Migração 18 no SQLite, 12 no PostgreSQL, aplicada ao subir.
- Sessões e as linhas de `list_sessions` ganham `tools_allowed` e `delegation`; revisões e as linhas de `list_revisions` ganham `is_default`.
- `resolve_pending` passa a rodar como a sessão que pediu: sob a revisão a que ela está presa e dentro da lista dela. Antes, a política era conferida contra a configuração atual mesmo para sessão presa.
- A revisão de mesmo rótulo do colega passa a ser lida do banco na delegação. Antes vinha de um cache por processo, e uma revisão aposentada em outra réplica ainda recebia a delegação.
