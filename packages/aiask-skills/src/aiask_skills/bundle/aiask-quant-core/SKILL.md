---
name: aiask-quant-core
description: Use this skill when working on AIASK Quant Core shared primitives, including backtest engines, factor calculators, strategy DSL/runtime, SQLite schemas and storage, TDX storage, vector storage, strategy trade prediction, JSON budget caps, signal tracking, risk/slippage models, or quant-core tests.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - quant_core.develop
required_mcp: []
side_effect: read_only
---

# AIASK Quant Core

## Workflow

1. Read [references/storage-and-contracts.md](references/storage-and-contracts.md) before changing SQLite schemas, storage adapters, JSON caps, vector storage, TDX storage, or strategy trade prediction.
2. Read [references/backtest-factor-dsl.md](references/backtest-factor-dsl.md) before changing backtest engines, factor calculators, strategy DSL, signal tracking, or risk/slippage primitives.
3. Keep Quant Core free of MCP server, Agent runtime, and Strategy Factory ownership logic.
4. Preserve data-shape compatibility for AKShare MCP and Strategy Factory callers.
5. Add or update package-local tests when schema, calculation semantics, or storage contracts change.

## Hard Rules

- Do not introduce Agent HTTP, MCP registration, or manager-plane dependencies into Quant Core.
- Do not change historical backtest/factor semantics silently.
- Do not allow unbounded JSON payload growth in strategy/factory tables.
- Preserve hash/integrity checks for strategy trade prediction records.
- Treat kline index/stock-code routing and schema migrations as contract-sensitive.

## Key Files

- `packages/aiask-quant-core/src/aiask_quant_core/backtest/`
- `packages/aiask-quant-core/src/aiask_quant_core/factor_calculator/`
- `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/`
- `packages/aiask-quant-core/src/aiask_quant_core/strategy_dsl.py`
- `packages/aiask-quant-core/src/aiask_quant_core/storage/trade_audit_writer.py`
- `packages/aiask-quant-core/tests/`
