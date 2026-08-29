"""Storage budgets for Strategy Factory JSON payloads.

The Strategy Factory can build large in-memory audit objects. SQLite should
persist compact, hashable summaries rather than repeated full payloads.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any


DEFAULT_JSON_FIELD_MAX_BYTES = 64 * 1024
DEFAULT_STRATEGY_PARAMS_MAX_BYTES = 32 * 1024
DEFAULT_HIGH_FREQ_SNAPSHOT_MAX_BYTES = 8 * 1024
DEFAULT_FULL_MARKET_RETENTION_RUNS = 1
DEFAULT_FULL_MARKET_TOPN = 200

FULL_LIST_KEYS = {"passed", "failed", "candidates", "results", "items"}
HEAVY_JSON_KEYS = {
    "admission_evaluations",
    "admission_review_context",
    "backtest_metrics",
    "backtest_result",
    "candidate_contract_snapshot",
    "candidate_provenance",
    "cash_curve",
    "claim_to_trade_plan_map",
    "component_metrics",
    "confidence_contract",
    "equity_curve",
    "event_window_metrics",
    "evidence_alignment_audit",
    "evidence_chain",
    "execution_audit_snapshot",
    "failed_candidates",
    "fills",
    "gross_exposure_curve",
    "incubation_budget",
    "klines",
    "market_facts",
    "multiple_testing_registry",
    "net_exposure_curve",
    "ohlcv",
    "orders",
    "passed_candidates",
    "positions",
    "prediction_contract",
    "quality_gate",
    "quality_gate_summary",
    "raw_backtest_result",
    "raw_events",
    "raw_result",
    "raw_results",
    "research_task",
    "research_validation_contract",
    "research_validation_contract_submission_adapter",
    "resolved_candidate_envelope",
    "round_trip_positions",
    "samples",
    "source_candidate_params",
    "stock_family_allocation",
    "trade_plan_to_dsl_map",
    "trades",
}

STRATEGY_PARAM_FULL_KEYS = {
    "adverse_close_break_pct",
    "adverse_exit_regimes",
    "adverse_regime_exit_enabled",
    "adverse_volume_break_ratio",
    "adverse_volume_ratio_max",
    "allowed_entry_regimes",
    "alpha_half_life",
    "bearish_regime_threshold",
    "breakout_buffer_pct",
    "breakout_failure_close_buffer",
    "breakout_volume_ratio_min",
    "breakout_window",
    "bullish_regime_threshold",
    "buy_quantile",
    "buy_threshold",
    "capacity_assumption",
    "capacity_bucket",
    "commission",
    "contraction_max_range_ratio",
    "contraction_window",
    "cost_sensitivity_grid",
    "direction_bias",
    "drawdown_invalidation_contract",
    "dryup_max_ratio",
    "dryup_window",
    "dsl",
    "entry_volume_floor_ratio",
    "event_impulse_threshold",
    "event_impulse_window",
    "event_prefilter",
    "execution_assumptions",
    "expected_turnover_band",
    "factor_weights",
    "family_specialization",
    "fear_threshold",
    "gap_threshold",
    "greed_threshold",
    "holding_horizon",
    "instrument_profile",
    "long_period",
    "lookback",
    "market_regime_assumption",
    "max_hold_bars",
    "mean_reversion_exit_buffer",
    "oversold",
    "overbought",
    "parameter_coherence_audit",
    "portfolio_spec",
    "position_model",
    "position_sizing",
    "position_sizing_rationale",
    "rebalance_rule",
    "regime_break_threshold",
    "regime_filter_contract",
    "regime_lookback",
    "regime_volatility_threshold",
    "regime_volatility_window",
    "repair_confirmation_enabled",
    "repair_drawdown_floor",
    "repair_rebound_pct",
    "risk_rules",
    "rsi_period",
    "runtime_recompile_backfill",
    "sell_quantile",
    "sell_threshold",
    "short_period",
    "slippage_rate",
    "stock_pool",
    "structure_body_return_min",
    "structure_close_location_min",
    "structure_window",
    "target_symbols",
    "targeting_policy",
    "thesis_invalidation_contract",
    "threshold",
    "trade_plan",
    "trade_prediction_contract",
    "trade_prediction_contract_missing_fields",
    "trade_prediction_contract_reject_reasons",
    "turnover_cost_class",
    "validation_profile",
    "volume_window",
}

STRATEGY_PARAM_REF_KEYS = {
    "candidate_contract_hash",
    "candidate_family",
    "candidate_family_id",
    "candidate_identity_signature",
    "candidate_lineage_contract",
    "candidate_provenance",
    "correlation_id",
    "execution_contract_hash",
    "execution_readiness_tier",
    "execution_semantic_gap",
    "execution_semantic_gap_reasons",
    "execution_semantic_mode",
    "factory_run_id",
    "generator_mode",
    "generator_type",
    "parent_task_run_id",
    "trade_prediction_contract_hash",
    "trade_prediction_contract_status",
    "trade_prediction_id",
    "prediction_trace_id",
    "source_action",
    "task_run_id",
    "trace_id",
}

QUALITY_REPORT_SUMMARY_PRIORITY_KEYS = (
    "strategy_id",
    "prediction_trace_id",
    "trace_id",
    "name",
    "status",
    "submission_lane",
    "planned_submission_lane",
    "final_status",
    "planned_final_status",
    "created_total",
    "created_strategy_pool",
    "created_audit_only",
    "gate_3_recorded",
    "record_only",
    "gate3_record_only",
    "validation_grade",
    "raw_validation_grade",
    "effective_validation_grade",
    "validation_total_score",
    "strict_incubation_ready",
    "strict_incubation_blocked",
    "formal_track_requested",
    "formal_track_auto_corrected",
    "formal_track_eligible",
    "formal_track_blockers",
    "observe_first_intake_requested",
    "observe_intake_requested",
    "incubation_budget_track",
    "incubation_budget_rank",
    "incubation_budget_priority_score",
    "incubation_budget_exploration_candidate",
    "runtime_bootstrap_eligible",
    "runtime_bootstrap_reason",
    "runtime_bootstrap_budget_tier",
    "runtime_playbook_present",
    "runtime_contract_missing_fields",
    "wide_intake_admitted",
    "pre_observe_hard_reject_reasons",
    "strategy_type_registered",
    "semantic_runtime_match",
    "runtime_family_data_source",
    "proxy_runtime_used",
    "diagnostic_only",
    "execution_readiness_tier",
    "semantic_contract_missing_fields",
    "trade_prediction_contract_status",
    "trade_prediction_contract_hash",
    "trade_prediction_contract_missing_fields",
    "trade_prediction_contract_reject_reasons",
    "trade_prediction_contract_observation_gap",
    "submission_action_type",
    "submission_action_trigger",
    "submission_action_gaps",
    "submission_action_fallback_conditions",
    "submission_action_next_step",
    "submission_action_completed",
    "admission_decision",
)


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(str(os.getenv(name) or "").strip() or default)
    except Exception:
        value = int(default)
    value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def strategy_json_field_max_bytes() -> int:
    return _env_int("STRATEGY_FACTORY_JSON_FIELD_MAX_BYTES", DEFAULT_JSON_FIELD_MAX_BYTES, minimum=4096)


def strategy_params_max_bytes() -> int:
    return _env_int(
        "STRATEGY_FACTORY_STRATEGY_PARAMS_MAX_BYTES",
        DEFAULT_STRATEGY_PARAMS_MAX_BYTES,
        minimum=4096,
    )


def high_freq_snapshot_max_bytes() -> int:
    return _env_int(
        "STRATEGY_FACTORY_HIGH_FREQ_SNAPSHOT_MAX_BYTES",
        DEFAULT_HIGH_FREQ_SNAPSHOT_MAX_BYTES,
        minimum=2048,
        maximum=64 * 1024,
    )


def full_market_score_retention_runs() -> int:
    return _env_int(
        "STRATEGY_FACTORY_FULL_MARKET_SCORE_RETENTION_RUNS",
        DEFAULT_FULL_MARKET_RETENTION_RUNS,
        minimum=1,
        maximum=1000,
    )


def full_market_score_topn() -> int:
    return _env_int(
        "STRATEGY_FACTORY_FULL_MARKET_SCORE_TOPN",
        DEFAULT_FULL_MARKET_TOPN,
        minimum=1,
        maximum=10000,
    )


def strategy_factory_sql_json_field_limits() -> dict[tuple[str, str], int]:
    snapshot_limit = high_freq_snapshot_max_bytes()
    return {
        ("daily_snapshot_history", "factor_research"): strategy_json_field_max_bytes(),
        ("strategies", "params"): strategy_params_max_bytes(),
        ("strategies", "factor_weights"): strategy_json_field_max_bytes(),
        ("strategy_status_events", "metadata"): strategy_json_field_max_bytes(),
        ("strategy_domain_events", "payload"): strategy_json_field_max_bytes(),
        ("strategy_task_runs", "payload"): strategy_json_field_max_bytes(),
        ("strategy_task_runs", "result"): strategy_json_field_max_bytes(),
        ("strategy_generation_experiments", "parameters"): strategy_json_field_max_bytes(),
        ("strategy_generation_experiments", "strategy_spec"): strategy_json_field_max_bytes(),
        ("strategy_generation_experiments", "evaluation"): strategy_json_field_max_bytes(),
        ("strategy_generation_experiments", "result"): strategy_json_field_max_bytes(),
        ("strategy_quality_reports", "summary"): strategy_json_field_max_bytes(),
        ("strategy_quality_reports", "quality_gate"): strategy_json_field_max_bytes(),
        ("strategy_quality_reports", "validation_report"): strategy_json_field_max_bytes(),
        ("strategy_quality_reports", "risk_report"): strategy_json_field_max_bytes(),
        ("strategy_quality_reports", "dedup_report"): strategy_json_field_max_bytes(),
        ("strategy_quality_reports", "backtest_metrics"): strategy_json_field_max_bytes(),
        ("strategy_quality_reports", "snapshot"): strategy_json_field_max_bytes(),
        ("strategy_incubation_pipeline_snapshots", "blockers"): snapshot_limit,
        ("strategy_incubation_pipeline_snapshots", "risk_flags"): snapshot_limit,
        ("strategy_incubation_pipeline_snapshots", "summary"): snapshot_limit,
        ("strategy_incubation_pipeline_snapshots", "metadata"): snapshot_limit,
        ("governance_report_snapshots", "issues"): snapshot_limit,
        ("governance_report_snapshots", "payload_jsonb"): snapshot_limit,
        ("strategy_runtime_risk_snapshots", "blockers"): snapshot_limit,
        ("strategy_runtime_risk_snapshots", "summary"): snapshot_limit,
        ("strategy_runtime_risk_snapshots", "metadata"): snapshot_limit,
        ("strategy_projection_snapshots", "projection"): snapshot_limit,
        ("strategy_projection_snapshots", "metadata"): snapshot_limit,
        ("strategy_closure_snapshots", "snapshot"): snapshot_limit,
        ("strategy_closure_snapshots", "metadata"): snapshot_limit,
        ("strategy_factory_run_artifacts", "payload_json"): strategy_json_field_max_bytes(),
        ("strategy_factory_scheduler_state", "payload_json"): strategy_json_field_max_bytes(),
        ("strategy_factory_runs", "summary"): strategy_json_field_max_bytes(),
        ("strategy_factory_runs", "stages"): strategy_json_field_max_bytes() * 2,
        ("strategy_factory_runs", "snapshot_summary"): strategy_json_field_max_bytes(),
        ("strategy_factory_topn_snapshots", "selection_rules"): strategy_json_field_max_bytes(),
        ("strategy_factory_topn_snapshots", "constituents"): strategy_json_field_max_bytes(),
        ("strategy_factory_topn_snapshots", "metadata"): strategy_json_field_max_bytes(),
        ("strategy_factory_task_evidence", "evidence_payload"): strategy_json_field_max_bytes(),
        ("strategy_execution_audit_snapshots", "verification"): strategy_json_field_max_bytes(),
        ("strategy_execution_audit_snapshots", "acceptance"): strategy_json_field_max_bytes(),
        ("strategy_execution_audit_snapshots", "audit_summary"): strategy_json_field_max_bytes(),
        ("strategy_execution_audit_snapshots", "snapshot"): strategy_json_field_max_bytes(),
        ("strategy_execution_audit_snapshots", "metadata"): strategy_json_field_max_bytes(),
        ("strategy_factory_dispatches", "metadata"): strategy_json_field_max_bytes(),
        ("strategy_factory_market_internals", "metadata"): strategy_json_field_max_bytes(),
        ("strategy_factory_event_clusters", "evidence"): strategy_json_field_max_bytes(),
        ("strategy_factory_theme_definitions", "metadata"): strategy_json_field_max_bytes(),
        ("strategy_factory_company_theme_exposures", "evidence"): strategy_json_field_max_bytes(),
        ("strategy_factory_event_signals", "evidence"): strategy_json_field_max_bytes(),
        ("strategy_factory_theme_edges", "evidence"): strategy_json_field_max_bytes(),
        ("strategy_factory_event_injections", "evidence"): strategy_json_field_max_bytes(),
        ("factory_tasks", "payload_json"): strategy_json_field_max_bytes(),
        ("factory_tasks", "artifact_refs_json"): strategy_json_field_max_bytes(),
        ("factory_task_attempts", "result_json"): strategy_json_field_max_bytes(),
    }


def json_size_bytes(value: Any) -> int:
    try:
        normalized = {} if value is None else value
        return len(json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    except Exception:
        return 0


def stable_json_hash(value: Any) -> str:
    try:
        normalized = {} if value is None else value
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        encoded = str(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def node_summary(value: Any, *, storage_mode: str = "dropped_large_payload") -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {
            "storage_mode": storage_mode,
            "node_type": "dict",
            "key_count": len(value),
            "keys": sorted(str(key) for key in list(value.keys())[:24]),
            "size_bytes": json_size_bytes(value),
            "payload_hash": stable_json_hash(value),
        }
    if isinstance(value, (list, tuple)):
        return {
            "storage_mode": storage_mode,
            "node_type": "list",
            "item_count": len(value),
            "size_bytes": json_size_bytes(value),
            "payload_hash": stable_json_hash(value),
        }
    return {
        "storage_mode": storage_mode,
        "node_type": type(value).__name__,
        "size_bytes": json_size_bytes(value),
        "payload_hash": stable_json_hash(value),
    }


def preview_list(value: Any, *, limit: int = 12) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
        return []
    preview: list[Any] = []
    for item in list(value)[:limit]:
        if _is_scalar(item):
            preview.append(item)
        elif isinstance(item, Mapping):
            preview.append(
                {
                    str(key): item.get(key)
                    for key in list(item.keys())[:8]
                    if _is_scalar(item.get(key)) or isinstance(item.get(key), (str, int, float, bool))
                }
            )
        else:
            preview.append(str(item))
    return preview


def compact_json(value: Any, *, depth: int = 0, max_list_items: int = 12, max_dict_items: int = 48) -> Any:
    if value in (None, "", [], {}):
        return {} if isinstance(value, Mapping) else [] if isinstance(value, list) else value
    if _is_scalar(value):
        return value
    if depth >= 4:
        return node_summary(value)
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:max_dict_items]:
            key = str(raw_key)
            if item in (None, "", [], {}):
                continue
            if key in HEAVY_JSON_KEYS and isinstance(item, (Mapping, list, tuple)):
                compact[f"{key}_summary"] = node_summary(item)
                continue
            if key in FULL_LIST_KEYS and isinstance(item, list):
                compact[f"{key}_summary"] = node_summary(item)
                continue
            compact[key] = compact_json(
                item,
                depth=depth + 1,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
            )
        if len(value) > max_dict_items:
            compact["truncated_key_count"] = len(value) - max_dict_items
        return compact
    if isinstance(value, (list, tuple)):
        values = list(value)
        preview = [
            compact_json(
                item,
                depth=depth + 1,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
            )
            for item in values[:max_list_items]
        ]
        if len(values) > max_list_items:
            preview.append({"truncated_item_count": len(values) - max_list_items})
        return preview
    return str(value)


def _assign_compact_value(
    target: dict[str, Any],
    key: str,
    item: Any,
    *,
    nested_limit: int = 80,
    list_limit: int = 16,
) -> None:
    if item in (None, "", [], {}):
        return
    if _is_scalar(item):
        target[key] = item
        return
    if isinstance(item, list):
        target[key] = preview_list(item, limit=list_limit)
        target[f"{key}_count"] = len(item)
        return
    if isinstance(item, Mapping):
        target[key] = _scalar_mapping(item, limit=nested_limit)
        return
    target[key] = str(item)


def _scalar_mapping(value: Any, *, limit: int = 40) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    keys = list(value.keys())
    for raw_key in keys[:limit]:
        key = str(raw_key)
        item = value.get(raw_key)
        if item in (None, "", [], {}):
            continue
        if _is_scalar(item):
            result[key] = item
        elif isinstance(item, list):
            result[key] = preview_list(item, limit=8)
            result[f"{key}_count"] = len(item)
        elif isinstance(item, Mapping):
            nested = {str(k): v for k, v in list(item.items())[:8] if _is_scalar(v)}
            result[key] = nested or node_summary(item)
        else:
            result[key] = str(item)
    if len(keys) > limit:
        result["truncated_key_count"] = len(keys) - limit
    return result


def compact_factor_research(value: Any, *, field_name: str, original_size: int) -> dict[str, Any]:
    payload = dict(value or {}) if isinstance(value, Mapping) else {}
    compact: dict[str, Any] = {
        "storage_mode": "compact_json",
        "field_name": field_name,
        "truncated": True,
        "original_size_bytes": int(original_size),
        "payload_hash": stable_json_hash(value),
        "top_level_keys": sorted(str(key) for key in payload.keys())[:80],
    }
    for key in (
        "degraded",
        "latest_factor_date",
        "freshness_days",
        "stale",
        "lightweight_mock_fallback",
        "normalized_allocation_entropy",
        "allocation_concentration_level",
    ):
        if payload.get(key) not in (None, "", [], {}):
            compact[key] = payload.get(key)
    for key in ("active_factors", "positive_rising_factors", "preferred_strategy_types", "family_preference_order"):
        values = preview_list(payload.get(key), limit=24)
        if values:
            compact[key] = values
            compact[f"{key}_count"] = len(payload.get(key) or [])
    if isinstance(payload.get("summary"), Mapping):
        compact["summary"] = _scalar_mapping(payload.get("summary"), limit=80)
    if isinstance(payload.get("active_candidate_pool"), Mapping):
        pool = dict(payload.get("active_candidate_pool") or {})
        pool_summary = _scalar_mapping(pool, limit=24)
        top_candidates = preview_list(pool.get("top_candidates"), limit=12)
        if top_candidates:
            pool_summary["top_candidates"] = top_candidates
            pool_summary["top_candidate_count"] = len(pool.get("top_candidates") or [])
        compact["active_candidate_pool"] = pool_summary
    allocation = payload.get("stock_family_allocation")
    if isinstance(allocation, Mapping):
        family_names: set[str] = set()
        samples: list[dict[str, Any]] = []
        for symbol, item in list(allocation.items())[:20]:
            item_payload = dict(item or {}) if isinstance(item, Mapping) else {}
            families = item_payload.get("families") or item_payload.get("family_scores") or item_payload
            family_preview: list[Any] = []
            if isinstance(families, Mapping):
                family_names.update(str(key) for key in families.keys())
                family_preview = [
                    {"family": str(key), "score": value}
                    for key, value in list(families.items())[:5]
                    if _is_scalar(value)
                ]
            elif isinstance(families, list):
                family_preview = preview_list(families, limit=5)
            samples.append({"symbol": str(symbol), "families": family_preview})
        compact["stock_family_allocation_summary"] = {
            "stock_count": len(allocation),
            "family_count": len(family_names),
            "family_names": sorted(family_names)[:40],
            "sample": samples,
            "payload_hash": stable_json_hash(allocation),
            "size_bytes": json_size_bytes(allocation),
        }
    for key in (
        "governed_candidates",
        "blocked_candidates",
        "top_candidate_lineage",
        "blocked_candidate_lineage",
        "search_route_actions",
        "source_chain",
    ):
        values = preview_list(payload.get(key), limit=12)
        if values:
            compact[key] = values
            compact[f"{key}_count"] = len(payload.get(key) or [])
    for key in ("family_reward_table", "family_debt_table", "active_family_summary", "active_regime_summary"):
        if isinstance(payload.get(key), Mapping):
            compact[key] = _scalar_mapping(payload.get(key), limit=24)
    for key in ("quality_flags", "scheduler_status", "freshness_repair"):
        if isinstance(payload.get(key), Mapping):
            compact[key] = compact_json(payload.get(key), max_dict_items=24)
        elif isinstance(payload.get(key), list):
            compact[key] = preview_list(payload.get(key), limit=12)
    return compact


def compact_strategy_factory_run_summary(value: Any, *, field_name: str, original_size: int) -> dict[str, Any]:
    payload = dict(value or {}) if isinstance(value, Mapping) else {}
    compact: dict[str, Any] = {
        "storage_mode": "compact_json",
        "field_name": field_name,
        "truncated": True,
        "original_size_bytes": int(original_size),
        "payload_hash": stable_json_hash(value),
        "top_level_keys": sorted(str(key) for key in payload.keys())[:80],
    }
    priority_keys = (
        "trace_id",
        "execution_mode",
        "engine_version",
        "runtime_enabled",
        "event_runtime_mode",
        "fear_greed",
        "listed_count",
        "snapshot_degraded",
        "snapshot_completion_ratio",
        "snapshot_failure_reason_count",
        "candidates_spawned",
        "autonomy_generated",
        "autonomy_task_count",
        "autonomy_completed_task_count",
        "autonomy_failed_task_count",
        "llm_status_counts",
        "pipeline_fallback_counts",
        "task_timeout_skip_count",
        "gate_3_input",
        "gate_3_passed",
        "gate_3_failed",
        "submitted",
        "created_audit_only",
        "gate_3_failure_reason_topn",
        "gate_3_failure_topn",
        "scheduler_cycle_count",
        "stock_first_flow",
        "observe_first_enabled",
        "observe_first_mode",
        "observed_candidate_count",
        "pre_observe_gate_removed",
        "pre_observe_hard_reject_count",
        "gate3_pre_observe_block_count",
        "legacy_gate_executed",
        "legacy_funnel_executed",
        "legacy_gate_report_mode",
        "evidence_scoring_mode",
        "router_artifact_contract_version",
        "router_enabled",
        "router_strict",
        "router_telemetry_enabled",
        "router_candidate_stock_count",
        "router_applied_count",
        "router_status_counts",
        "router_fallback_reason_counts",
        "router_family_counts",
        "router_holding_bucket_counts",
        "profile_summary_present_count",
        "profile_summary_missing_count",
        "profile_summary_generated_count",
        "selected_router_applied_count",
        "selected_profile_summary_missing_count",
        "task_source_counts",
        "bulk_stock_task_count",
        "snapshot_task_count",
        "created_total",
        "created_strategy_pool",
        "submission_lane_counts",
        "strategy_status_counts",
        "cycle_pipeline_stage_order",
        "cycle_pipeline_stage_aliases",
    )
    preserve_keys = [
        key for key in priority_keys if key in payload
    ] + [
        key
        for key in payload.keys()
        if str(key).startswith("family_gate_feedback")
    ]

    for key in preserve_keys:
        _assign_compact_value(compact, str(key), payload.get(key), nested_limit=120)

    scalar_preview_count = 0
    for raw_key, item in payload.items():
        key = str(raw_key)
        if key in compact or f"{key}_count" in compact:
            continue
        if not _is_scalar(item):
            continue
        _assign_compact_value(compact, key, item)
        scalar_preview_count += 1
        if scalar_preview_count >= 40:
            break
    if len(payload) > len(compact):
        compact["truncated_key_count"] = max(0, len(payload) - len(compact))
    return compact


def compact_strategy_quality_report_summary(value: Any, *, field_name: str, original_size: int) -> dict[str, Any]:
    payload = dict(value or {}) if isinstance(value, Mapping) else {}
    compact: dict[str, Any] = {
        "storage_mode": "compact_json",
        "field_name": field_name,
        "truncated": True,
        "original_size_bytes": int(original_size),
        "payload_hash": stable_json_hash(value),
        "top_level_keys": sorted(str(key) for key in payload.keys())[:80],
    }
    preserve_keys = [key for key in QUALITY_REPORT_SUMMARY_PRIORITY_KEYS if key in payload]
    preserve_keys.extend(
        key
        for key in payload.keys()
        if str(key).startswith("gate3_") and key not in preserve_keys
    )

    for key in preserve_keys:
        _assign_compact_value(compact, str(key), payload.get(key), nested_limit=80)

    scalar_preview_count = 0
    for raw_key, item in payload.items():
        key = str(raw_key)
        if key in compact or f"{key}_count" in compact:
            continue
        if not _is_scalar(item):
            continue
        _assign_compact_value(compact, key, item)
        scalar_preview_count += 1
        if scalar_preview_count >= 40:
            break
    if len(payload) > len(compact):
        compact["truncated_key_count"] = max(0, len(payload) - len(compact))
    return compact


def compact_strategy_params(value: Any, *, field_name: str, original_size: int) -> dict[str, Any]:
    payload = dict(value or {}) if isinstance(value, Mapping) else {}
    compact: dict[str, Any] = {}
    audit: dict[str, Any] = {
        "storage_mode": "compact_json",
        "field_name": field_name,
        "truncated": True,
        "original_size_bytes": int(original_size),
        "payload_hash": stable_json_hash(value),
    }
    dropped: dict[str, Any] = {}
    for raw_key, item in payload.items():
        key = str(raw_key)
        if item in (None, "", [], {}):
            continue
        if key == "incubation_budget" and isinstance(item, Mapping):
            compact[key] = _scalar_mapping(item, limit=24)
            continue
        if key in STRATEGY_PARAM_FULL_KEYS or key in STRATEGY_PARAM_REF_KEYS or _is_scalar(item):
            compact[key] = compact_json(item, max_dict_items=32, max_list_items=24)
            continue
        if key in HEAVY_JSON_KEYS or isinstance(item, (Mapping, list, tuple)):
            dropped[key] = node_summary(item)
            continue
        compact[key] = str(item)
    if dropped:
        audit["dropped_large_nodes"] = dropped
    compact["_storage_audit"] = audit
    return compact


def _strategy_params_have_heavy_nodes(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return any(str(key) in HEAVY_JSON_KEYS for key in value.keys())


def coerce_json_like(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    if text[0] not in "{[\"":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _contains_heavy_json_nodes(value: Any, *, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in HEAVY_JSON_KEYS:
                return True
            if key in FULL_LIST_KEYS and isinstance(item, list):
                return True
            if _contains_heavy_json_nodes(item, depth=depth + 1):
                return True
    if isinstance(value, (list, tuple)):
        return any(_contains_heavy_json_nodes(item, depth=depth + 1) for item in list(value)[:24])
    return False


def _is_already_budgeted_payload(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    storage_mode = str(value.get("storage_mode") or "").strip().lower()
    if storage_mode in {"compact_json", "dropped_large_payload", "inline_compact_json", "inline_fallback_summary"}:
        return bool(value.get("payload_hash") or value.get("original_size_bytes") or value.get("scrubbed_size_bytes"))
    storage_audit = value.get("_storage_audit")
    if isinstance(storage_audit, Mapping):
        audit_mode = str(storage_audit.get("storage_mode") or "").strip().lower()
        return audit_mode in {"compact_json", "dropped_large_payload"} and bool(
            storage_audit.get("payload_hash") or storage_audit.get("original_size_bytes")
        )
    return False


def compact_for_field(field_name: str, value: Any, *, max_bytes: int) -> Any:
    original_size = json_size_bytes(value)
    normalized = str(field_name or "").strip().lower()
    if "factor_research" in normalized:
        compacted = compact_factor_research(value, field_name=field_name, original_size=original_size)
    elif normalized == "strategy_factory_runs.summary":
        compacted = compact_strategy_factory_run_summary(value, field_name=field_name, original_size=original_size)
    elif normalized == "strategy_quality_reports.summary":
        compacted = compact_strategy_quality_report_summary(
            value,
            field_name=field_name,
            original_size=original_size,
        )
    elif normalized.endswith("strategies.params") or normalized == "strategies.params":
        compacted = compact_strategy_params(value, field_name=field_name, original_size=original_size)
    else:
        compacted = compact_json(value)
        if compacted != value or original_size > max_bytes:
            if isinstance(compacted, Mapping):
                compacted = dict(compacted)
                compacted.setdefault("storage_mode", "compact_json")
                compacted.setdefault("field_name", field_name)
                compacted.setdefault("truncated", True)
                compacted.setdefault("original_size_bytes", original_size)
                compacted.setdefault("payload_hash", stable_json_hash(value))
            else:
                compacted = {
                    "storage_mode": "compact_json",
                    "field_name": field_name,
                    "truncated": True,
                    "original_size_bytes": original_size,
                    "payload_hash": stable_json_hash(value),
                    "preview": compacted,
                }
    return compacted if compacted is not None else {}


def bounded_json_text(field_name: str, value: Any, *, max_bytes: int | None = None) -> str:
    limit = max(4096, int(max_bytes or strategy_json_field_max_bytes()))
    original = coerce_json_like({} if value is None else value)
    original_text = json.dumps(original, ensure_ascii=False, default=str)
    normalized = str(field_name or "").strip().lower()
    original_size = len(original_text.encode("utf-8"))
    if original_size <= limit and _is_already_budgeted_payload(original):
        return original_text
    force_compact = (
        "factor_research" in normalized
        or (
            (normalized.endswith("strategies.params") or normalized == "strategies.params")
            and _strategy_params_have_heavy_nodes(original)
        )
        or (
            normalized.startswith("strategy_factory_runs.")
            and _contains_heavy_json_nodes(original)
        )
    )
    if not force_compact and original_size <= limit:
        return original_text
    compacted = compact_for_field(field_name, original, max_bytes=limit)
    compacted_text = json.dumps(compacted or {}, ensure_ascii=False, default=str)
    if len(compacted_text.encode("utf-8")) <= limit:
        return compacted_text
    fallback = {
        "storage_mode": "dropped_large_payload",
        "field_name": str(field_name or "unknown"),
        "truncated": True,
        "original_size_bytes": len(original_text.encode("utf-8")),
        "scrubbed_size_bytes": len(compacted_text.encode("utf-8")),
        "payload_hash": stable_json_hash(original),
        "top_level_keys": sorted(str(key) for key in list((original or {}).keys())[:50])
        if isinstance(original, Mapping)
        else [],
    }
    return json.dumps(fallback, ensure_ascii=False, default=str)
