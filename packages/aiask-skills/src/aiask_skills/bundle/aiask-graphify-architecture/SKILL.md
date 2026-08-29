---
name: aiask-graphify-architecture
description: Use when analyzing AIASK or another repository with Graphify/code-graph evidence, including architecture discovery, endpoint maps, dependency graphs, cross-package impact analysis, package-boundary audits, implemented capability mapping, readiness reviews, and durable architecture reports.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - architecture.analyze
required_mcp: []
side_effect: read_only
---

# AIASK Graphify Architecture

## Workflow

1. Read [references/graphify-workflow.md](references/graphify-workflow.md) before producing architecture, capability, endpoint, impact, or readiness reports.
2. Treat Graphify artifacts as evidence, not final truth. Confirm important conclusions with current code, manifests, and tests.
3. Reuse existing graph artifacts unless the user asks to rebuild or artifacts are missing.
4. Separate current architecture, implemented capabilities, and readiness risks.
5. Do not read `.env`, operate on runtime DB/log/cache/broker state, or trigger live trading paths.

## AIASK Graph Artifacts

Prefer:

- `reports/code-graph/full-2026-05-29/curated/CURATED_SUMMARY.json`
- `reports/code-graph/full-2026-05-29/curated/endpoint-map.json`
- `reports/code-graph/full-2026-05-29/curated/cross-package-edges.json`

These artifacts predate several June changes, so verify against current worktree for Desktop agent pages, financial manager, factory events, Quant Core, and recent Strategy/AKShare updates.

## Useful Current Entrypoints

- `packages/agent/src/aiask_agent/server.py`
- `packages/akshare-mcp/src/akshare_mcp/server.py`
- `packages/strategy-factory/src/strategy_factory/api/facade.py`
- `packages/aiask-quant-core/src/aiask_quant_core/`
- `packages/finance-mcp-servers/src/aiask_finance_mcp/`
