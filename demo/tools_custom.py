"""Tools written as plain Python handlers (``register_descriptor``)."""

from __future__ import annotations

import ast
import operator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from symbiote.environment.descriptors import ToolDescriptor

_WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
             "sexta-feira", "sábado", "domingo"]

_OPS: dict[type, Any] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        if isinstance(node.op, ast.Pow) and abs(_eval(node.right)) > 100:
            raise ValueError("expoente grande demais")
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("só números e + - * / // % ** ( )")


def agora(params: dict) -> dict:
    tz = params.get("fuso") or "America/Sao_Paulo"
    now = datetime.now(ZoneInfo(tz))
    return {
        "data": now.date().isoformat(),
        "hora": now.strftime("%H:%M"),
        "dia_da_semana": _WEEKDAYS[now.weekday()],
        "fuso": tz,
    }


def calcular(params: dict) -> dict:
    expression = params.get("expressao")
    if not isinstance(expression, str) or not expression.strip():
        # Says what was expected and what came, so the model fixes the call
        # on the next iteration instead of reading a bare SyntaxError.
        got = ", ".join(sorted(params)) or "nenhum parâmetro"
        raise ValueError(f"informe o parâmetro 'expressao' (texto, ex.: '612.4 + 398.75'); recebi: {got}")
    try:
        tree = ast.parse(expression.replace(",", "."), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"expressão inválida: {expression!r}") from exc
    return {"expressao": expression, "resultado": round(_eval(tree), 6)}


def consultar_cotacao(params: dict) -> dict:
    # Fails on purpose: the demo uses it to show the circuit breaker, the
    # failure memory written at close_session and a low session score.
    raise ConnectionError(
        f"serviço de cotações indisponível (HTTP 503) para {params.get('moeda', '?')}"
    )


TOOLS: list[tuple[ToolDescriptor, Any]] = [
    (
        ToolDescriptor(
            tool_id="agora",
            name="Data e hora atuais",
            description="Data, hora e dia da semana atuais. Use antes de falar de 'hoje', 'amanhã' ou prazos.",
            parameters={"type": "object", "properties": {
                "fuso": {"type": "string", "description": "Fuso IANA, padrão America/Sao_Paulo"}}},
            tags=["utilidades"],
        ),
        agora,
    ),
    (
        ToolDescriptor(
            tool_id="calcular",
            name="Calculadora",
            description="Calcula uma expressão aritmética exata (ex.: '612.4 + 398.75'). Use para somas de dinheiro.",
            parameters={"type": "object", "properties": {
                "expressao": {"type": "string"}}, "required": ["expressao"]},
            tags=["utilidades"],
        ),
        calcular,
    ),
    (
        ToolDescriptor(
            tool_id="consultar_cotacao",
            name="Cotação de moeda",
            description="Cotação atual de uma moeda em reais (ex.: USD, EUR).",
            parameters={"type": "object", "properties": {
                "moeda": {"type": "string"}}, "required": ["moeda"]},
            tags=["financas"],
        ),
        consultar_cotacao,
    ),
]

CUSTOM_IDS = {descriptor.tool_id for descriptor, _ in TOOLS}

# Declarative HTTP tools offered as one-click examples (public APIs, no key).
HTTP_EXAMPLES: list[dict] = [
    {
        "tool_id": "clima_atual",
        "name": "Clima atual",
        "description": "Temperatura e vento agora numa coordenada (Open-Meteo). Passe latitude e longitude da cidade.",
        "method": "GET",
        "url_template": "https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,wind_speed_10m,weather_code&timezone=auto",
        "parameters": {"type": "object", "properties": {
            "latitude": {"type": "number"}, "longitude": {"type": "number"}},
            "required": ["latitude", "longitude"]},
        "tags": ["utilidades"],
    },
    {
        "tool_id": "buscar_cep",
        "name": "Buscar CEP",
        "description": "Endereço de um CEP brasileiro (ViaCEP). Só os 8 dígitos.",
        "method": "GET",
        "url_template": "https://viacep.com.br/ws/{cep}/json/",
        "parameters": {"type": "object", "properties": {"cep": {"type": "string"}},
                       "required": ["cep"]},
        "tags": ["utilidades"],
    },
]
