# Research, LLM, Vector, And Sentiment

## Strategy And LLM Runtime

Current services include staged strategy generation, strategy specs, runtime contracts, LLM provider normalization/runtime, markdown/prose JSON extraction, event-stream replay handling, provider cooldown/availability logic, and staged-output validators.

Important files:

- `services/_strategy_llm_provider_runtime.py`
- `services/_strategy_llm_provider_normalize.py`
- `services/_strategy_generators_*`
- `services/strategy_spec/`
- `services/strategy_stages.py`

Useful tests:

- `test_strategy_llm_provider_runtime.py`
- `test_strategy_stages_validators.py`
- `test_strategy_pipeline_quality_fixes.py`
- `test_strategy_review_workflow_contract.py`

## Factor And Regime Research

Current areas include factor catalog, external research, profile/regime analysis, per-symbol regime, batch IC neutralization, forward horizons, factor validation bootstrap, and QC pipeline integration.

Useful tests:

- `test_factor_catalog_p1_3.py`
- `test_factor_external_research.py`
- `test_profile_regime_p1_1.py`
- `test_per_symbol_regime_p0_3.py`
- `test_batch_ic_neutralize_p0_2.py`
- `test_forward_horizons_p2_2.py`
- `test_qc_pipeline_p2_1.py`

## Vector/Search/Governance

Vector services include platform backends, indexes, profiles, search, governance, unified vector benchmark, optimize bootstrap, and backfill. Keep vector contracts explicit and avoid runtime-inference-only behavior.

Useful tests:

- `test_tool_catalog_vector_contracts.py`
- `test_reduce_semantics.py`
- `test_provider_contracts.py`
