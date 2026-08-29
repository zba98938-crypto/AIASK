---
name: aiask-strategy-factory
description: Use this skill when working on AIASK Strategy Factory code, including the public facade, scheduler/cycle pipeline, observe-first and stock-first modes, stock strategy router, admission authority, trade prediction contract, budget feedback, readiness, quality gates, submitter, paper bridge, theme exposure/regression, runtime bootstrap used by Agent and AKShare, or Strategy Factory tests.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - strategy_factory.develop
required_mcp: []
side_effect: read_only
---

# AIASK Strategy Factory

## Workflow

1. Read [references/facade-and-contracts.md](references/facade-and-contracts.md) before changing public imports, facade exports, manager contracts, Agent factory facades, or cross-package boundaries.
2. Read [references/factory-cycle-and-quality-gates.md](references/factory-cycle-and-quality-gates.md) before changing scheduling, cycle status, observe-first/stock-first behavior, candidate routing, gates, submitter, or readiness.
3. Keep Strategy Factory domain logic inside `packages/strategy-factory`; do not push orchestration into AKShare MCP managers or Desktop.
4. Keep model-visible access through Agent `agent_*` facades or ActionIntent routes.
5. Add targeted tests for changes to strategy lifecycle, routing, readiness, or trade prediction contracts.

## Hard Rules

- Do not expose raw `strategy_manager` as a model-visible Agent tool.
- Do not route generated strategies into live trading by default.
- Preserve traceability for candidate intake, gate decisions, submission, paper/diagnostic observation, and incubation handoff.
- Keep large artifacts compact; avoid persisting raw quality/backtest payload bloat.
- Treat readiness and partial-status resolution as user-visible operational contracts.

## Key Files

- `packages/strategy-factory/src/strategy_factory/api/facade.py`
- `packages/strategy-factory/src/strategy_factory/application/factory_scheduler.py`
- `packages/strategy-factory/src/strategy_factory/application/cycle_pipeline.py`
- `packages/strategy-factory/src/strategy_factory/application/services/`
- `packages/strategy-factory/src/strategy_factory/application/stock_strategy_router.py`
- `packages/strategy-factory/src/strategy_factory/application/trade_prediction_contract.py`
- `packages/strategy-factory/src/strategy_factory/runtime/default_bootstrap.py`
- `packages/strategy-factory/src/strategy_factory/runtime/`
- `packages/strategy-factory/tests/`
