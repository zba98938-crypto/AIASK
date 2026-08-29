---
name: aiask-akshare-manager-plane
description: Use this skill when working on AKShare MCP manager modules, manager_protocol envelopes, side-effect metadata, read-only/stateful/trade-risk classification, strategy/quant/risk/portfolio/watchlist/user/paper/live/execution managers, factory event manager actions, or Agent safety wrappers around manager behavior.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - manager_plane.develop
required_mcp:
  - aiask-akshare
side_effect: read_only
---

# AIASK AKShare Manager Plane

## Workflow

1. Read [references/manager-catalog.md](references/manager-catalog.md) before changing manager actions, handler files, or manager contracts.
2. Read [references/side-effects-and-safety.md](references/side-effects-and-safety.md) before changing side-effect levels, confirmation requirements, trade-risk behavior, or Agent wrappers.
3. Keep manager internals hidden behind manager protocol, MCP tools, and Agent `agent_*` facades.
4. Add negative tests for any new stateful or trade-risk action.

## Hard Rules

- Do not expose raw manager names as model-visible Agent tools.
- Keep read-only, stateful, and trade-risk actions distinguishable in metadata.
- Live trading/order operations require explicit broker-token guardrails.
- Manager failures should return structured envelopes, not silent success.

## Key Files

- `packages/akshare-mcp/src/akshare_mcp/tools/manager_protocol.py`
- `packages/akshare-mcp/src/akshare_mcp/tools/managers/`
- `packages/akshare-mcp/src/akshare_mcp/contracts/`
- `packages/agent/src/aiask_agent/adapters/`
