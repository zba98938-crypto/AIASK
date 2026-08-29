# AIASK Integration Copy

This directory is a copied, isolated snapshot of the AIASK project from:

`C:/Users/walking/Desktop/aiask/`

It was copied into Hermes Agent without moving the original project.

## What is included

- `desktop/` - focused AIASK Desktop workbench source for Agent HTTP readiness, Financial Manager guardrails, MCP visibility, and mock/live validation.
- `packages/agent/` - AIASK FastAPI Agent runtime and safe tool facades.
- `packages/akshare-mcp/` - AKShare MCP server, stock/finance tools, data source routing, data sync, readiness/freshness checks, research/quant/strategy services.
- `packages/aiask-quant-core/` - quant primitives, backtest engines, factor calculators, storage, signal/risk helpers.
- `packages/finance-mcp-servers/` - Tongdaxin, Tonghuashun, Eastmoney, and QMT MCP servers.
- `packages/strategy-factory/` - strategy/factor/incubation factory domain package.
- `scripts/` - data sync, backfill, readiness, factory, monitoring, and ops scripts.
- `.codex/skills/aiask-*` - original AIASK project skills.
- `.claude/mcp-servers-recommended.json` - AIASK MCP reference config.

The original full frontend was intentionally not copied after user direction. This integration now includes a focused Desktop workbench that satisfies product readiness gates without importing backend packages or bypassing Agent HTTP.

## What was intentionally excluded

Secrets, generated files, environments, caches, logs, and runtime outputs were not copied:

- `.env`, `.env.bak_*`
- `.git/`
- `.venv/`
- `node_modules/`
- `dist/`
- `__pycache__/`
- `.pytest_cache/`
- `.mcp_cache/`
- `logs/`, `cache/`
- `test-results/`, `playwright-report/`
- `frontend-screenshots/`, `_gray_full_logs/`, `tmp/`, `output/`, `reports/`

## Hermes exposure points

The AIASK skills were also copied into Hermes in-repo skills at:

`skills/finance/aiask-*`

Local MCP catalog entries were added under:

- `optional-mcps/aiask-akshare/`
- `optional-mcps/aiask-tongdaxin/`
- `optional-mcps/aiask-eastmoney/`

## Local install notes

Keep dependency installation isolated from Hermes core. AIASK package dependencies are exact-pinned and each package lock file must be regenerated and reviewed with any version change; do not merge them into Hermes core dependencies without a dedicated compatibility pass.

The root `pyproject.toml` is an integration descriptor only. Install backend packages individually from `packages/` in the order below.

Configuration variables are documented in `env.example`. Do not store real secrets in this repository.

AIASK packages require **Python `>=3.12,<3.14`** (Hermes core may still run on 3.11). Always use a **separate venv** — do not install these into the Hermes core `.venv`.

### macOS / Linux

```bash
cd "/path/to/Hermes agent/integrations/aiask"
python3.12 -m venv "${AIASK_RUNTIME_HOME:-$HOME/.aiask}/venv"
source "${AIASK_RUNTIME_HOME:-$HOME/.aiask}/venv/bin/activate"
pip install -U pip
pip install -e packages/aiask-quant-core
pip install -e packages/strategy-factory
pip install -e packages/akshare-mcp
pip install -e packages/finance-mcp-servers
pip install -e packages/agent
export AIASK_VENV_PYTHON="${AIASK_RUNTIME_HOME:-$HOME/.aiask}/venv/bin/python"
export AIASK_PROJECT_ROOT="$PWD"
```

### Windows

```bash
cd "C:\path\to\Hermes agent\integrations\aiask"
py -3.12 -m venv "%LOCALAPPDATA%\hermes-aiask\venv"
"%LOCALAPPDATA%\hermes-aiask\venv\Scripts\python" -m pip install -U pip
"%LOCALAPPDATA%\hermes-aiask\venv\Scripts\python" -m pip install -e packages/aiask-quant-core
"%LOCALAPPDATA%\hermes-aiask\venv\Scripts\python" -m pip install -e packages/strategy-factory
"%LOCALAPPDATA%\hermes-aiask\venv\Scripts\python" -m pip install -e packages/akshare-mcp
"%LOCALAPPDATA%\hermes-aiask\venv\Scripts\python" -m pip install -e packages/finance-mcp-servers
"%LOCALAPPDATA%\hermes-aiask\venv\Scripts\python" -m pip install -e packages/agent
set AIASK_VENV_PYTHON=%LOCALAPPDATA%\hermes-aiask\venv\Scripts\python.exe
set AIASK_PROJECT_ROOT=%CD%
```

Hermes MCP catalog entries call `scripts/run_mcp_stdio.py`, which re-execs `AIASK_VENV_PYTHON` when set. If unset, it probes the platform-default venv path, then falls back to the current Python with copied source paths added.

Useful overrides:

```bash
# macOS/Linux
AIASK_VENV_PYTHON=${AIASK_RUNTIME_HOME:-$HOME/.aiask}/venv/bin/python
AIASK_PROJECT_ROOT=/path/to/Hermes agent/integrations/aiask
AIASK_SQLITE_PATH=$HOME/.aiask/data/db/akshare_mcp.sqlite3
AKSHARE_MCP_SQLITE_PATH=$HOME/.aiask/data/db/akshare_mcp.sqlite3
```

## Safety boundaries

Preserve AIASK's original boundaries:

- Desktop talks to Agent HTTP only.
- Model-visible Agent tools should stay behind `agent_*` facades.
- AKShare managers are internal/provider-facing; do not expose raw manager names directly to model tools.
- Strategy, factor, incubation, execution, gateway delivery, plugin mutation, file/terminal/browser writes, and live trading paths require explicit guardrails.
- Tonghuashun/QMT live trading/order paths must remain token/confirmation guarded.
