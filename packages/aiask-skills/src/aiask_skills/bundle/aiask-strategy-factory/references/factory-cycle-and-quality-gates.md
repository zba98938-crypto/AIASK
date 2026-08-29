# Strategy Factory Cycle, Routing, And Quality Gates

## Current Runtime Shape

The factory is no longer just a linear scheduler. Current implementation includes:

- Explicit cycle pipeline and canonical stage mapping.
- Observe-first and stock-first execution modes.
- Admission authority for formal/provisional/rejected/deferred candidates.
- Stock strategy router and matrix wiring.
- Budget feedback from lifecycle, forward windows, runtime alerts, risk events, and promotion reviews.
- Readiness service and partial/success/failed status resolution.
- Theme exposure, theme graph propagation, and theme regression.
- Paper trading bridge and diagnostic/paper observation paths.
- Trade prediction contract.

## Quality And Submission Flow

Core areas:

- Candidate collection and research-plane/governance-plane contracts.
- Backtest filter and statistical robustness metrics.
- Deduplication.
- Submission gate and submitter actions.
- Elimination/probation and lifecycle handoff.
- Compact artifacts and summary reporting.

Do not reintroduce legacy-only gate assumptions when observe-first or stock-first modes are enabled.

## Tests To Consider

- `test_cycle_pipeline.py`
- `test_cycle_status_resolution.py`
- `test_cycle_readiness_status.py`
- `test_candidate_pipeline_observe_first.py`
- `test_factory_execution_modes.py`
- `test_stock_strategy_router_p1_2.py`
- `test_router_matrix_wiring_p1_2.py`
- `test_router_summary_artifacts.py`
- `test_admission_authority.py`
- `test_backtest_filter_smoke.py`
- `test_backtest_robustness_metrics.py`
- `test_runtime_toggles.py`
- `test_readiness_service.py`
- `test_theme_exposure_builder.py`
- `test_theme_regression.py`
- `test_paper_trading_bridge.py`

## Runner Notes

Inspect current `scripts/factories/`, `scripts/ops/`, and root runner files before referencing a launcher. Some historical root scripts may be deleted or replaced in the working tree.
