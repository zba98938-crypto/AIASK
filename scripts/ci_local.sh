#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-/opt/miniconda3/envs/aiask/bin/python}"
packages=(agent aiask-quant-core finance-mcp-servers strategy-factory akshare-mcp aiask-skills data-mcp reference-host)

cd "$ROOT"
"$PY" -m compileall -q packages scripts tools
for package in "${packages[@]}"; do
  echo "== pytest: ${package} =="
  (cd "packages/${package}" && "$PY" -m pytest -q tests)
done
