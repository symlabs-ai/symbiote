"""A tiny MCP server (stdio) the demo loads with ``kernel.load_mcp_tools``.

Run standalone with ``python demo/mcp_server.py``; the demo starts it as a
subprocess through ``forge_llm.mcp.McpToolset.from_stdio``.
"""

from __future__ import annotations

import secrets
import string

from mcp.server.fastmcp import FastMCP

server = FastMCP("utilidades-mcp")

_UNITS = {
    ("km", "mi"): 0.621371, ("mi", "km"): 1.609344,
    ("kg", "lb"): 2.204623, ("lb", "kg"): 0.453592,
    ("l", "gal"): 0.264172, ("gal", "l"): 3.785412,
    ("m", "ft"): 3.280840, ("ft", "m"): 0.3048,
}


@server.tool()
def converter_unidades(valor: float, de: str, para: str) -> str:
    """Converte valores entre unidades: km/mi, kg/lb, l/gal, m/ft e temperaturas c/f."""
    de, para = de.lower().strip(), para.lower().strip()
    if (de, para) == ("c", "f"):
        return f"{valor} °C = {valor * 9 / 5 + 32:.2f} °F"
    if (de, para) == ("f", "c"):
        return f"{valor} °F = {(valor - 32) * 5 / 9:.2f} °C"
    factor = _UNITS.get((de, para))
    if factor is None:
        raise ValueError(f"conversão {de}->{para} não suportada")
    return f"{valor} {de} = {valor * factor:.4g} {para}"


@server.tool()
def gerar_senha(tamanho: int = 16, simbolos: bool = True) -> str:
    """Gera uma senha aleatória forte (8 a 64 caracteres)."""
    tamanho = max(8, min(64, int(tamanho)))
    alphabet = string.ascii_letters + string.digits + ("!@#$%&*-_" if simbolos else "")
    return "".join(secrets.choice(alphabet) for _ in range(tamanho))


@server.tool()
def contar_texto(texto: str) -> dict:
    """Conta palavras, caracteres e linhas de um texto."""
    return {
        "palavras": len(texto.split()),
        "caracteres": len(texto),
        "linhas": texto.count("\n") + 1 if texto else 0,
    }


if __name__ == "__main__":
    server.run()
