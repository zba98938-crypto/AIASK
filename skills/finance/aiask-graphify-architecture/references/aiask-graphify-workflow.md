# AIASK Graphify Workflow

## Evidence Sources

Prefer these local facts:

- `reports/code-graph/full-2026-05-29/curated/CURATED_SUMMARY.json`
- `reports/code-graph/full-2026-05-29/curated/endpoint-map.json`
- `reports/code-graph/full-2026-05-29/curated/cross-package-edges.json`
- `AGENT.md`, but treat current code as stronger than historical text
- package manifests:
  - `packages/agent/pyproject.toml`
  - `packages/akshare-mcp/pyproject.toml`
  - `packages/strategy-factory/pyproject.toml`
  - `packages/aiask-quant-core/pyproject.toml`
  - `packages/finance-mcp-servers/pyproject.toml`
  - `desktop/package.json`

## Key Implementation Areas

- Desktop API surface: `desktop/src/services/aiaskApi.ts`
- Agent HTTP runtime: `packages/agent/src/aiask_agent/server.py`
- Agent tool registry and safety: `packages/agent/src/aiask_agent/tool_registry.py`, `packages/agent/src/aiask_agent/tools/policy.py`
- AKShare MCP server and tools: `packages/akshare-mcp/src/akshare_mcp/server.py`, `tools/`, `services/`
- Strategy Factory public facade: `packages/strategy-factory/src/strategy_factory/api/facade.py`
- Quant Core: `packages/aiask-quant-core/src/aiask_quant_core/backtest`, `factor_calculator`, `storage/sqlite`
- Finance MCP guards: `packages/finance-mcp-servers/src/aiask_finance_mcp/_shared/trade_guard.py`

## Report Guidance

When the user asks what the project does, do not lead with risks. Lead with:

- what AIASK is
- how Desktop, Agent, MCP, Strategy Factory, Quant Core, and runners connect
- what implemented workflows exist
- how the project can help the user

Then add readiness and risk constraints.

## Validation

For report-only work, validate:

- expected report file exists
- required headings exist
- referenced local paths exist
- no secret-value patterns are present
- Markdown diff check passes

For code graph rebuilds:

```bash
python scripts/code_graph/build_aiask_code_graph.py --out reports/code-graph/full-2026-05-29 --clean
```

For broader readiness validation:

```bash
make test-agent
make test-finance
cd desktop && npm test
cd desktop && npm run typecheck
cd desktop && npm run build
```
