#!/usr/bin/env bash
# Prepara o ambiente do demo: .venv com o Symbiote e os extras de LLM, web,
# browser e MCP, e o Chromium do Playwright (~170 MB, uma vez só).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if ! command -v uv >/dev/null; then
  echo "Instale o uv: https://docs.astral.sh/uv/" >&2
  exit 1
fi
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv
VIRTUAL_ENV=$PWD/.venv uv pip install -e ".[dev,llm,browser,extract]" "mcp>=1.6,<2"
.venv/bin/python -m playwright install chromium
[ -f demo/demo.env ] || { cp demo/demo.env.example demo/demo.env; chmod 600 demo/demo.env; }
echo "Pronto. Rode: bash demo/run.sh"
