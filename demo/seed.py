"""Initial state: two symbiotes, persona, knowledge and the sandbox files.

Runs on every start but only fills what is missing, so whatever the presenter
changed (config, revoked tools, memories) survives a restart. "Zerar demo"
deletes the data directory and gets a fresh seed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SOL = "Sol"
RESEARCHER = "Pesquisador"

SOL_PERSONA = {
    "role": "Sol, assistente pessoal multipropósito",
    "tone": "caloroso, direto e sem enrolação",
    "language": "português do Brasil",
    "instructions": (
        "Você cuida do dia a dia de quem conversa com você: tarefas, agenda, contatos, "
        "notas, finanças da casa, arquivos da pasta de trabalho, contas e pesquisas.\n"
        "- Aja com as tools em vez de explicar como fazer.\n"
        "- Antes de falar de 'hoje', 'amanhã' ou prazos, use a tool `agora`.\n"
        "- Para somar ou converter valores, use `calcular` (nunca faça conta de cabeça).\n"
        "- Para pesquisar na web ou abrir um site, delegue ao symbiote 'Pesquisador' com a "
        "tool `spawn` (target_symbiote='Pesquisador').\n"
        "- Arquivos: `fs_read`, `fs_write` e `fs_list` usam caminhos relativos à pasta de trabalho.\n"
        "- Respeite as preferências e regras que aparecem nas suas memórias.\n"
        "- Respostas curtas; use listas quando ajudar. Nunca mostre IDs técnicos."
    ),
}

RESEARCHER_PERSONA = {
    "role": "Pesquisador, especialista em buscar e ler conteúdo na web",
    "tone": "objetivo",
    "language": "português do Brasil",
    "instructions": (
        "Use `web_extract` para ler uma página a partir da URL e as tools `browser_*` quando "
        "precisar navegar ou interagir. Sempre cite as URLs usadas. Responda em até 8 linhas."
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
            reflection_mode="keyword",
            dream_mode="light",
            dream_min_sessions=3,
            prompt_caching=prompt_caching,
        )
        for name, entry_type, content in KNOWLEDGE:
            kernel.knowledge.register_source(sol_id, name, content=content, entry_type=entry_type)

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
