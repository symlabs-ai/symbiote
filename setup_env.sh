#!/usr/bin/env bash
# Ambiente de desenvolvimento do Symbiote: .venv com Python 3.12 e o pacote em
# modo editável com os extras dev e llm. Uso: bash setup_env.sh && source .venv/bin/activate
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
uv venv --python 3.12 .venv && VIRTUAL_ENV=$PWD/.venv uv pip install -e ".[dev,llm]"
