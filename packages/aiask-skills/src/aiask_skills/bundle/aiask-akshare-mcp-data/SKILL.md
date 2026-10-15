---
name: aiask-akshare-mcp-data
description: Use this skill when working on AIASK AKShare MCP data systems, FastMCP startup profiles/transports, tool registration, TDX/TQCenter/Tushare/AKShare routing, SQLite persistence, data sync, freshness/readiness gates, warmup audits, market storage, or quant data quality contracts.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - market_data.read
required_mcp:
  - aiask-akshare
side_effect: read_only
---

# AIASK AKShare MCP Data

## Workflow

1. Read [references/mcp-runtime-and-tool-surface.md](references/mcp-runtime-and-tool-surface.md) before changing server startup, profiles, transports, tool registration, resources, prompts, or runtime skill tools.
2. Read [references/data-sync-and-storage.md](references/data-sync-and-storage.md) before changing data source routing, TDX/TQCenter behavior, SQLite schemas/adapters, sync scripts, readiness checks, or freshness gates.
3. Keep data acquisition/storage ownership in AKShare MCP or Quant Core; do not push it into Agent or Desktop.
4. Preserve explicit data source metadata, stale/degraded/fallback signals, and local-only behavior.
5. Avoid mutation-heavy sync in tests unless explicitly scoped.

## Hard Rules

- Respect `TDX_LOCAL_ONLY`, `AKSHARE_MCP_SQLITE_PATH`, and `AIASK_SQLITE_PATH`.
- Do not make Tushare or online providers unconditional startup dependencies.
- HTTP MCP transports must keep loopback/auth/origin safeguards.
- Runtime AKShare skill tools (`list_skills` / `run_skill`) are not the same as the repository skills under `skills/finance/`.
- Do not silently hide stale, empty, fallback, or optional-source failures.

## Key Files

- `packages/akshare-mcp/src/akshare_mcp/server.py`
- `packages/akshare-mcp/src/akshare_mcp/data_source/`
- `packages/akshare-mcp/src/akshare_mcp/storage/sqlite/`
- `packages/akshare-mcp/src/akshare_mcp/tools/db_freshness.py`
- `packages/akshare-mcp/src/akshare_mcp/services/tdx_sync_service.py`
- `packages/akshare-mcp/scripts/`
