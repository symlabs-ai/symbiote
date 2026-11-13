#!/usr/bin/env bash
# Sobe o demo em http://127.0.0.1:8010 (ou --port N).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [ ! -x .venv/bin/python ]; then
  echo "Ambiente não encontrado. Rode antes: bash demo/setup.sh" >&2
  exit 1
fi
exec .venv/bin/python -m demo "$@"
