# Factor Mining Cycle

## Current Scope

Factor Mining Factory is more than the old runner. Current implementation includes:

- Mining cycles and maintenance.
- Search engines and evolutionary optimization.
- Factor catalog and candidate registry integration.
- QC pipeline and candidate validation.
- Batch IC neutralization and degrees-of-freedom guards.
- Profile/regime and per-symbol regime context.
- Forward horizons.
- Active pool governance and persistence.
- Feedback into Strategy Factory and incubation budget flows where wired.

## Entrypoints

Inspect current launchers before running. Historical root scripts may be deleted or moved; current worktree includes `scripts/factories/` and `scripts/ops/` candidates.

Service files:

- `services/factor_mining_factory/factory.py`
- `services/factor_mining_factory/qc_pipeline.py`
- `services/factor_mining_factory/scheduler.py`
- `services/factor_catalog.py`
- `tools/quant.py`
- `tools/managers/quant_mgr_classic.py`

## Safe Validation

Prefer targeted tests and status/maintenance flows. Avoid full scheduled loops during routine edits.

Useful tests:

- `packages/akshare-mcp/tests/test_factor_catalog_p1_3.py`
- `packages/akshare-mcp/tests/test_qc_pipeline_p2_1.py`
- `packages/akshare-mcp/tests/test_batch_ic_neutralize_p0_2.py`
- `packages/akshare-mcp/tests/test_profile_regime_p1_1.py`
- `packages/akshare-mcp/tests/test_per_symbol_regime_p0_3.py`
- `packages/akshare-mcp/tests/test_forward_horizons_p2_2.py`
