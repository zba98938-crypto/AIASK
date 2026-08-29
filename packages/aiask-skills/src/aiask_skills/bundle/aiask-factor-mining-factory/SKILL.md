---
name: aiask-factor-mining-factory
description: Use this skill when working on AIASK factor mining factory code, factor catalog, QC pipeline, mining cycles, search engines, candidate validation, sandboxing, IC neutralization, per-symbol regime/profile analysis, forward horizons, active factor pool governance, maintenance, quant manager actions, or factor factory Desktop/Agent surfaces.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - factor_mining.develop
required_mcp: []
side_effect: read_only
---

# AIASK Factor Mining Factory

## Workflow

1. Read [references/mining-cycle.md](references/mining-cycle.md) before changing orchestration, runner modes, scheduler behavior, or Desktop/Agent factory status.
2. Read [references/engines-validation-pool.md](references/engines-validation-pool.md) before changing engines, QC, validation, factor catalog, regime/profile inputs, active pool admission, feedback, or maintenance.
3. Keep mined factors DB-backed and governed; do not admit candidates without validation/sandbox/QC.
4. Preserve source, regime, IC, forward-horizon, family, and pool-state metadata.
5. Prefer status/maintenance/once validation; avoid long-running schedule loops unless requested.

## Key Files

- `packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/`
- `packages/akshare-mcp/src/akshare_mcp/services/factor_catalog.py`
- `packages/akshare-mcp/src/akshare_mcp/services/factor_analysis.py`
- `packages/akshare-mcp/src/akshare_mcp/tools/quant.py`
- `packages/akshare-mcp/src/akshare_mcp/tools/managers/quant_mgr_classic.py`
- `packages/strategy-factory/src/strategy_factory/runtime/factor_mining.py`
- `packages/agent/src/aiask_agent/adapters/desktop_ops.py`
- `packages/agent/src/aiask_agent/routes/desktop_finance.py`
