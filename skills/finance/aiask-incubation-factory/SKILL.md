---
name: aiask-incubation-factory
description: Use this skill when working on AIASK incubation factory code, incubation pipeline stages, diagnostic/paper observation intake, signal generation, forward verification, hit-rate matrix/reporting, feedback writing, promotion gates, execution audit gates, lifecycle surfaces, Desktop incubation UI, or incubation tests.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - incubation.develop
required_mcp: []
side_effect: read_only
---

# AIASK Incubation Factory

## Workflow

1. Read [references/runner-phases.md](references/runner-phases.md) before changing runners, daemon/status/dry-run behavior, phase error handling, or factory status.
2. Read [references/pipeline-and-feedback.md](references/pipeline-and-feedback.md) before changing stage transitions, observation intake, forward verification, hit-rate matrix, promotion gates, feedback writes, or Desktop/Agent status.
3. Preserve dry-run behavior and explicit write paths.
4. Keep diagnostic/paper/live boundaries visible; paper or diagnostic observation is not live trading readiness.
5. Add tests for stage, gate, hit-rate, or feedback behavior changes.

## Key Files

- `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/`
- `packages/akshare-mcp/src/akshare_mcp/services/incubation.py`
- `packages/akshare-mcp/src/akshare_mcp/services/incubation_pipeline.py`
- `packages/akshare-mcp/src/akshare_mcp/services/signal_tracker_parts/`
- `packages/strategy-factory/src/strategy_factory/runtime/incubation.py`
- `packages/agent/src/aiask_agent/adapters/strategy_factory.py`
- `packages/agent/src/aiask_agent/adapters/desktop_ops.py`
- `desktop/src/pages/EnhancedFinancePages.tsx`
