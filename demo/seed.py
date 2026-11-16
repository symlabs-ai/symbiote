"""Initial state: two symbiotes, persona, knowledge and the sandbox files.

Runs on every start but only fills what is missing, so whatever the presenter
changed (config, revoked tools, memories) survives a restart. "Zerar demo"
deletes the data directory and gets a fresh seed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from symbiote.core.models import MemoryEntry

SOL = "Sol"
RESEARCHER = "Pesquisador"

# The chat renders Markdown: a source cited as a link is clickable and reads
# better than a raw address on its own line.
SOURCES_RULE = (
    "- Ao citar uma página da web, escreva um link Markdown com o título como texto e a "
    "URL completa, como `[Título da página](https://site.com/caminho)`, seguido do veículo "
    "e da data quando houver. Nunca escreva a URL sem `https://` nem solta numa linha à parte."
)

SOL_PERSONA = {
    "role": "Sol, assistente pessoal multipropósito",
    "tone": "caloroso, direto e sem enrolação",
    "language": "português do Brasil",
    "instructions": (
        "Você cuida do dia a dia de quem conversa com você: tarefas, agenda, contatos, "
        "notas, finanças da casa, arquivos da pasta de trabalho, contas e pesquisas.\n"
        "- Quando houver um pedido de ação, use as tools em vez de só explicar como fazer.\n"
        "- Só diga que criou, concluiu, apagou ou enviou algo depois que a tool dessa ação "
        "devolveu sucesso neste turno. Para mexer numa tarefa existente, busque o id com "
        "`list_tasks` antes.\n"
        "- Antes de falar de 'hoje', 'amanhã' ou prazos, use a tool `agora`.\n"
        "- Para somar ou converter valores, use `calcular` (nunca faça conta de cabeça).\n"
        "- Tarefas, agenda, contatos, notas, despesas e e-mail vêm das tools da API pessoal "
        "(`list_tasks`, `list_events` e as outras). Se elas não estiverem entre as suas tools, "
        "diga que ainda não foram carregadas (passo 4 do Roteiro, ou na aba Tools: 'Descobrir "
        "na API pessoal' e depois 'Carregar aprovadas no kernel') e não procure esses dados "
        "nos arquivos.\n"
        "- Para ler arquivos use `fs_read` e `fs_list`; `bash` só quando nenhuma outra tool resolver.\n"
        "- Para pesquisar na web ou ler um site público, delegue ao symbiote 'Pesquisador' com a "
        "tool `spawn` (target_symbiote='Pesquisador').\n"
        "- Quando a pessoa apenas compartilhar um fato ou um link, reconheça o que ela disse. "
        "Trate o relato dela sobre si como contexto da conversa, sem pedir comprovação pública. "
        "Um link sozinho não é um pedido de pesquisa: só abra ou pesquise se ela pedir "
        "informações, verificação ou uma ação que precise disso. Ao delegar, peça somente "
        "os dados necessários para responder ao pedido, sem acrescentar uma lista de campos.\n"
        "- Para agir num site onde a pessoa tem conta (Google Agenda, Google Cloud), use as tools "
        "`meu_chrome_*`: elas usam o Chrome dela. Se a tool disser que o Chrome não aceita "
        "conexão ou que é preciso permitir, explique a ela em português os passos que a mensagem "
        "traz e tente de novo quando ela avisar. Se aparecer uma tela de login, peça que ela "
        "entre nessa janela do Chrome e te avise. Nunca digite senha nem código de verificação. "
        "Cada clique e campo preenchido pede a aprovação dela. A agenda da API pessoal é a do "
        "demo; o Google Agenda é outra, no Chrome.\n"
        "- Arquivos: `fs_read`, `fs_write` e `fs_list` usam caminhos relativos à pasta de trabalho.\n"
        "- Respeite as preferências e regras que aparecem nas suas memórias.\n"
        "- Quando pedirem para você lembrar de algo, confirme. O que a pessoa ensina vai "
        "para a sua memória quando a sessão é encerrada e volta nas próximas conversas; "
        "nunca diga que não consegue lembrar.\n"
        "- Respostas curtas; use listas quando ajudar. Nunca mostre IDs técnicos.\n"
        + SOURCES_RULE
    ),
}

RESEARCHER_PERSONA = {
    "role": "Pesquisador, especialista em buscar e ler conteúdo na web",
    "tone": "objetivo",
    "language": "português do Brasil",
    "instructions": (
        "Use `web_extract` para ler uma página a partir da URL e as tools `browser_*` quando "
        "precisar navegar ou interagir. Pesquise apenas os dados pedidos; depois de uma "
        "busca por identificador, leia as fontes relevantes e pare quando tiver evidência "
        "suficiente ou quando não houver como confirmar a identificação. Não repita buscas "
        "equivalentes por variações do mesmo identificador. Responda em até 8 linhas.\n"
        + SOURCES_RULE
    ),
}

KNOWLEDGE = [
    (
        "Rotina da casa",
        "note",
        "A Lia (filha, 9 anos) tem natação às terças e quintas, 18h30, no Clube Atlântico. "
        "O lixo reciclável sai às quartas de manhã. A diarista vem às sextas.",
    ),
    (
        "Mudança para Curitiba",
        "document",
        "A mudança da família para Curitiba está prevista para março de 2027. "
        "Transportadora escolhida: Mudanças Pinheiro (orçamento de R$ 7.800). "
        "Pendências: escolher a escola da Lia e transferir o plano de saúde.",
    ),
    (
        "Plano de saúde",
        "reference",
        "Plano Vida Mais, carteirinha 0042-118. Reembolso de consultas em até 30 dias pelo app. "
        "Pediatra da Lia: Dr. Ricardo Lemos.",
    ),
]

# Memories for the dream step (tour step 13), so a fresh demo has something
# for each deterministic phase to act on. Topics stay away from what the
# other tour steps ask about, so they rarely enter the recall before step 13.
# (type, content, tags, importance, days since last used)
DREAM_SEEDS = [
    # prune: decay = days unused × (1 − importance) > 30. Their words must
    # not appear in the tour's prompts: entering a recall counts as use and
    # saves a memory from the prune (tests/test_tour_seeds.py).
    ("factual", "O Wi-Fi da casa de praia em Ubatuba se chamava PraiaNet.",
     ["praia"], 0.2, 160),
    ("factual", "A assinatura da revista de jardinagem foi cancelada.",
     ["assinaturas"], 0.25, 120),
    # reconcile: the same preference stated twice (same type, same tags,
    # ≥ 80% of the words in common); the lower-importance copy goes
    ("preference", "Prefere que lembretes de aniversário cheguem com uma semana de antecedência.",
     ["aniversarios", "lembretes"], 0.6, 2),
    ("preference", "Prefere que os lembretes de aniversário cheguem com uma semana de antecedência.",
     ["aniversarios", "lembretes"], 0.45, 5),
    # generalize (mode full): 3 procedures sharing 2 tags become one rule
    ("procedural", "Para reservar hotel: comparar 3 opções no mapa, conferir cancelamento grátis "
     "e salvar a confirmação na pasta viagem/.", ["viagem", "reserva", "hotel"], 0.6, 3),
    ("procedural", "Para comprar passagem aérea: comparar horários em 2 sites, conferir se a bagagem "
     "está incluída e salvar o e-ticket na pasta viagem/.", ["viagem", "reserva", "voo"], 0.6, 3),
    ("procedural", "Para alugar carro: comparar 3 locadoras, conferir seguro e franquia "
     "e salvar o voucher na pasta viagem/.", ["viagem", "reserva", "carro"], 0.55, 3),
]

SANDBOX_FILES = {
    "notas/lista-de-compras.md": "# Lista de compras\n\n- café\n- leite sem lactose\n- pão integral\n- maçãs\n",
    "viagem/roteiro-lisboa.md": (
        "# Lisboa — 5 dias\n\n1. Alfama e Castelo de São Jorge\n2. Belém (Torre, Jerónimos, pastéis)\n"
        "3. Sintra (Pena e Regaleira)\n4. LX Factory e Cais do Sodré\n5. Livre — compras\n"
    ),
    "financas/orcamento-2026.csv": (
        "categoria,limite_mensal\nmercado,1200\ntransporte,450\nlazer,400\nsaude,300\ncasa,600\n"
    ),
}


def _seed_dream_memories(kernel: Any, symbiote_id: str) -> None:
    now = datetime.now(tz=UTC)
    for mem_type, content, tags, importance, days in DREAM_SEEDS:
        when = now - timedelta(days=days)
        kernel.memory.store(MemoryEntry(
            symbiote_id=symbiote_id, type=mem_type, scope="global", content=content,
            tags=tags, importance=importance, source="user",
            created_at=when, last_used_at=when,
        ))


def _ensure_symbiote(kernel: Any, name: str, role: str, persona: dict) -> tuple[str, bool]:
    existing = kernel.find_symbiote_by_name(name)
    if existing is not None:
        return existing.id, False
    return kernel.create_symbiote(name=name, role=role, persona=persona).id, True


def seed(
    kernel: Any,
    *,
    sandbox: Path,
    sol_tools: list[str],
    researcher_tools: list[str],
    prompt_caching: bool,
) -> tuple[str, str]:
    """Create what is missing; return (sol_id, researcher_id)."""
    for rel, content in SANDBOX_FILES.items():
        path = sandbox / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    sol_id, sol_new = _ensure_symbiote(kernel, SOL, "assistente pessoal", SOL_PERSONA)
    researcher_id, researcher_new = _ensure_symbiote(
        kernel, RESEARCHER, "pesquisador web", RESEARCHER_PERSONA
    )

    if sol_new:
        kernel.environment.configure(
            symbiote_id=sol_id,
            tools=sol_tools,
            tool_mode="brief",
            max_tool_iterations=8,
            tool_call_timeout=120.0,
            loop_timeout=300.0,
            # A real LLM is always configured: extract facts with it (falls back
            # to keyword extraction if the call fails).
            reflection_mode="llm",
            # Off until the dream step turns it on: an automatic dream after
            # 3 closed sessions would consume the seeds before the step shows them.
            dream_mode="off",
            dream_min_sessions=3,
            prompt_caching=prompt_caching,
        )
        for name, entry_type, content in KNOWLEDGE:
            kernel.knowledge.register_source(sol_id, name, content=content, entry_type=entry_type)
        _seed_dream_memories(kernel, sol_id)

    if researcher_new:
        kernel.environment.configure(
            symbiote_id=researcher_id,
            tools=researcher_tools,
            # Visibility by tag: the researcher only sees web, browser and
            # utility tools, not the assistant's personal tools.
            tool_tags=["web", "browser", "utilidades"],
            tool_mode="brief",
            max_tool_iterations=8,
            tool_call_timeout=60.0,
            loop_timeout=180.0,
            prompt_caching=prompt_caching,
        )
    return sol_id, researcher_id
