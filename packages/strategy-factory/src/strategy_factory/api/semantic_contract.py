"""Public semantic and candidate-contract helpers for host adapters."""

from __future__ import annotations

from ..application.candidate_contract import (
    apply_resolved_candidate_envelope,
    build_alpha_identity_components,
    build_candidate_contract_backfill,
    build_candidate_contract_hash,
    build_candidate_identity_signature,
    build_dsl_signature,
    build_entry_exit_signature,
    build_execution_contract_hash,
    build_factor_signature,
    build_factory_backtest_assumptions,
    build_logic_signature,
    build_portfolio_candidate_contract,
    build_resolved_candidate_envelope,
    build_tested_object_hash,
    candidate_contract_value,
    resolve_candidate_targeting_policy,
    resolve_candidate_validation_profile,
)
from ..application.hypothesis_lowering_compiler import HypothesisLoweringCompiler
from ..application.market_evidence import (
    HARD_MARKET_FACT_METRICS,
    build_market_fact_gate_audit,
    normalize_market_evidence_fact,
    normalize_market_evidence_facts,
    summarize_market_fact_gate,
)
from ..application.precompile_contract import (
    PrecompileContractValidationResult,
    validate_precompile_candidate_contract,
)
from ..application.research_protocol_contract import (
    CANDIDATE_CONTRACT_V2,
    PREDICTION_TRACE_CONTRACT_VERSION,
    RESEARCH_PROTOCOL_CONTRACT_VERSION,
    SPEC_COMPLETENESS_REQUIRED_FIELDS,
    adapt_research_validation_contract_for_submission,
    build_completion_issues,
    build_field_provenance_summary,
    build_research_validation_contract,
    evaluate_research_validation_contract_admission,
    normalize_field_provenance_token,
    normalize_prediction_trace_id,
    resolve_spec_completeness,
)
from ..application.semantic_contract import (
    audit_candidate_semantic_contract,
    build_candidate_evidence_records,
    build_signal_evidence_records,
    ensure_candidate_semantic_contract,
    inspect_strategy_dsl_support,
    normalize_semantic_contract_fields,
    synthesize_confidence_contract,
)
from ..domain.targets import (
    _apply_target_symbol_policy,
    _build_target_alignment_contract,
    _extract_candidate_origin_target_codes,
    _extract_target_codes_from_payload,
    _normalize_research_task_contract,
    _normalize_strategy_type_preferences,
    _normalize_string_list,
    _normalize_target_codes,
    _resolve_strategy_sample_codes,
    _update_strategy_status,
)

apply_target_symbol_policy = _apply_target_symbol_policy
build_target_alignment_contract = _build_target_alignment_contract
extract_candidate_origin_target_codes = _extract_candidate_origin_target_codes
extract_target_codes_from_payload = _extract_target_codes_from_payload
normalize_research_task_contract = _normalize_research_task_contract
normalize_strategy_type_preferences = _normalize_strategy_type_preferences
normalize_string_list = _normalize_string_list
normalize_target_codes = _normalize_target_codes
resolve_strategy_sample_codes = _resolve_strategy_sample_codes
update_strategy_status = _update_strategy_status

__all__ = [
    "CANDIDATE_CONTRACT_V2",
    "HARD_MARKET_FACT_METRICS",
    "HypothesisLoweringCompiler",
    "PREDICTION_TRACE_CONTRACT_VERSION",
    "PrecompileContractValidationResult",
    "RESEARCH_PROTOCOL_CONTRACT_VERSION",
    "SPEC_COMPLETENESS_REQUIRED_FIELDS",
    "_apply_target_symbol_policy",
    "_build_target_alignment_contract",
    "_extract_candidate_origin_target_codes",
    "_extract_target_codes_from_payload",
    "_normalize_research_task_contract",
    "_normalize_strategy_type_preferences",
    "_normalize_string_list",
    "_normalize_target_codes",
    "_resolve_strategy_sample_codes",
    "_update_strategy_status",
    "adapt_research_validation_contract_for_submission",
    "apply_target_symbol_policy",
    "apply_resolved_candidate_envelope",
    "audit_candidate_semantic_contract",
    "build_alpha_identity_components",
    "build_candidate_contract_backfill",
    "build_candidate_contract_hash",
    "build_candidate_evidence_records",
    "build_candidate_identity_signature",
    "build_completion_issues",
    "build_dsl_signature",
    "build_entry_exit_signature",
    "build_execution_contract_hash",
    "build_factor_signature",
    "build_factory_backtest_assumptions",
    "build_field_provenance_summary",
    "build_logic_signature",
    "build_market_fact_gate_audit",
    "build_portfolio_candidate_contract",
    "build_research_validation_contract",
    "build_resolved_candidate_envelope",
    "build_signal_evidence_records",
    "build_target_alignment_contract",
    "build_tested_object_hash",
    "candidate_contract_value",
    "ensure_candidate_semantic_contract",
    "evaluate_research_validation_contract_admission",
    "extract_candidate_origin_target_codes",
    "extract_target_codes_from_payload",
    "inspect_strategy_dsl_support",
    "normalize_field_provenance_token",
    "normalize_market_evidence_fact",
    "normalize_market_evidence_facts",
    "normalize_prediction_trace_id",
    "normalize_research_task_contract",
    "normalize_semantic_contract_fields",
    "normalize_strategy_type_preferences",
    "normalize_string_list",
    "normalize_target_codes",
    "resolve_candidate_targeting_policy",
    "resolve_candidate_validation_profile",
    "resolve_strategy_sample_codes",
    "resolve_spec_completeness",
    "summarize_market_fact_gate",
    "synthesize_confidence_contract",
    "update_strategy_status",
    "validate_precompile_candidate_contract",
]
