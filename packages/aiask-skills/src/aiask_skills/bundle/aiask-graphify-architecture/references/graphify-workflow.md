# Graphify / Code Graph Workflow

## Evidence Sources

Use the smallest set that proves the claim:

- Graph summary: node/edge counts, package subgraphs, top nodes.
- Endpoint map: matched, server-only, desktop-only endpoints.
- Cross-package edges: dependencies and blast radius.
- Manifests: package `pyproject.toml`, `desktop/package.json`.
- Entrypoints: Agent server/tool registry, Desktop API/view registry, AKShare MCP server, Strategy Factory facade, Quant Core storage/backtest, Finance MCP guards.
- Tests that enforce the boundary or behavior.

## AIASK Report Shape

Lead with:

1. Project positioning.
2. Runtime/package architecture.
3. Implemented capabilities and business workflows.
4. How the system helps the user's project.
5. Readiness matrix, risks, and validation gaps.
6. Next actions.

For diagnostics or reviews, lead with findings and evidence instead.

## Interpretation Rules

- Endpoint drift is not automatically a bug. Classify it as production endpoint, mock-only, text-only, future feature, deprecated route, or real gap.
- Cross-package edges are not automatically bad. Use them to choose contract tests and impact scope.
- Historical docs can explain intent, but current code and tests decide actual behavior.
- Mock UI coverage proves Desktop behavior, not live backend readiness.

## Rebuild Command

Use only when requested or when artifacts are missing/stale enough to block the task:

```bash
python scripts/code_graph/build_aiask_code_graph.py --out reports/code-graph/full-2026-05-29 --clean
```

## Safety

Do not include secret values, raw `.env`, DB dumps, logs, cache contents, or broker/runtime state in reports.
