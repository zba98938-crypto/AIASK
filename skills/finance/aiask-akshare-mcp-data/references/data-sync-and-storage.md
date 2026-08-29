# AKShare MCP Data Sync And Storage

## Data Source Routing

Current code uses local/market-source routing around:

- TDX local `vipdoc`.
- TDX/TQCenter online or local proxy paths where available.
- Tushare or AKShare when local-only policy permits.

Important files:

- `data_source/market_data.py`
- `data_source/tdx_tqcenter.py`
- `services/tdx_sync_service.py`
- `tools/db_freshness.py`

Preserve explicit source, fallback, stale, empty, degraded, and optional-unavailable states.

## SQLite Storage

Primary storage packages:

- `packages/akshare-mcp/src/akshare_mcp/storage/sqlite/`
- `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/`

Current storage concerns include:

- Market schemas and TDX phase storage.
- Strategy/factory JSON caps.
- Event/theme graph tables.
- Vector/search storage.
- Strategy/incubation/runtime tables.
- Data completeness and freshness tracking.

Do not hardcode database paths. Respect env-based configuration.

## Sync And Warmup

Scripts and tests cover core market warmup, factor context warmup, freshness checks, and data readiness. Avoid full syncs in normal unit tests. Prefer dry-run/status/readiness checks before mutation-heavy sync.

Relevant tests:

- `test_data_source_tdx_routing.py`
- `test_tdx_phase3_tools_e2e.py`
- `test_tdx_storage_phase8.py`
- `test_p0_1_data_readiness.py`
- `test_warmup_audit_scripts_contract.py`
- `test_market_data_db_first.py`
- `test_sqlite_runtime_compat.py`
