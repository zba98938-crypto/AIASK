"""Bounded JSON contracts for Strategy Factory runtime results.

The factory may create large intermediate objects while computing a run, but
public run results and storage-facing payloads must stay small.  These helpers
turn raw backtest/gate payloads into count-rich summaries and deliberately drop
curves, trades, fills, and full candidate lists.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

COMPACT_PREVIEW_LIMIT = 12
COMPACT_ARTIFACT_MAX_BYTES = 64 * 1024

HEAVY_JSON_KEYS = frozenset(
    {
        "backtest_result",
        "raw_backtest_result",
        "raw_result",
        "raw_results",
        "quality_gate",
        "gate_report",
        "passed_candidates",
        "failed_candidates",
        "equity_curve",
        "cash_curve",
        "gross_exposure_curve",
        "net_exposure_curve",
        "exposure_curve",
        "trades",
        "fills",
        "orders",
        "positions",
        "round_trip_positions",
        "component_metrics",
        "event_window_metrics",
        "event_window_samples",
        "raw_events",
        "samples",
        "klines",
        "ohlcv",
    }
)

LIST_KEYS_WITH_FULL_RESULTS = frozenset({"passed", "failed", "candidates", "items", "results"})

SCALAR_METRIC_KEYS = (
    "sharpe",
    "sharpe_ratio",
    "annualized_return",
    "total_return",
    "max_drawdown",
    "volatility",
    "win_rate",
    "trade_count",
    "trades_count",
    "turnover",
    "turnover_proxy",
    "profit_factor",
    "expectancy",
    "expectancy_return",
    "payoff_ratio",
    "breakeven_win_rate",
    "post_cost_sharpe",
    "wf_ic_ir",
    "pkf_ic",
    "bootstrap_ci_lower",
    "param_sensitivity",
    "deflated_sharpe_ratio",
    "deflated_sharpe_proxy",
    "raw_sharpe_proxy",
    "pbo",
    "pbo_proxy",
    "cpcv_pbo",
    "reality_check_pvalue_proxy",
    "spa_pvalue_proxy",
    "white_reality_check_pvalue",
    "hansen_spa_pvalue",
    "cohort_effective_trials",
    "batch_correlation_multiplier",
    "batch_correlation_sibling_count",
    "bootstrap_ci_width",
    "sample_count",
    "required_sample_count",
    "evaluated_code_count",
    "successful_code_count",
    "target_layer_oos_return",
    "target_layer_abnormal_return",
    "trade_density",
    "avg_holding_days",
    "parameter_perturbation_trade_stability",
    "backtest_metrics_contract_status",
    "primary_validation_layer",
    "validation_focus",
    "code_source",
    "run_correction_mode",
    "multiple_testing_mode",
    "multiple_testing_inject_status",
    "batch_correlation_mode",
    "event_window_hit_ratio",
    "post_event_decay",
)


def json_size_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    except Exception:
        return 0


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def json_node_summary(value: Any, *, storage_mode: str = "dropped_large_payload") -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {
            "storage_mode": storage_mode,
            "node_type": "dict",
            "key_count": len(value),
            "keys": [str(key) for key in list(value.keys())[:16]],
            "size_bytes": json_size_bytes(value),
        }
    if isinstance(value, (list, tuple)):
        return {
            "storage_mode": storage_mode,
            "node_type": "list",
            "item_count": len(value),
            "size_bytes": json_size_bytes(value),
        }
    return {
        "storage_mode": storage_mode,
        "node_type": type(value).__name__,
        "size_bytes": json_size_bytes(value),
    }

def compact_json(
    value: Any,
    *,
    depth: int = 0,
    max_list_items: int = COMPACT_PREVIEW_LIMIT,
    max_dict_items: int = 24,
) -> Any:
    if value in (None, "", [], {}):
        return {} if isinstance(value, Mapping) else [] if isinstance(value, list) else value
    if _is_scalar(value):
        return value
    if depth >= 3:
        return json_node_summary(value)
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:max_dict_items]:
            key = str(raw_key)
            if item in (None, "", [], {}):
                continue
            if key in HEAVY_JSON_KEYS and isinstance(item, (Mapping, list, tuple)):
                compact[f"{key}_summary"] = json_node_summary(item)
                continue
            if key in LIST_KEYS_WITH_FULL_RESULTS and isinstance(item, list):
                compact[f"{key}_summary"] = json_node_summary(item)
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


def compact_research_task(task: Any) -> dict[str, Any]:
    payload = dict(task or {}) if isinstance(task, Mapping) else {}
    keys = (
        "task_id",
        "task_key",
        "task_source",
        "opportunity_type",
        "theme_code",
        "event_id",
        "candidate_family",
        "factor_name",
        "validation_focus",
        "target_symbol_count",
    )
    compact = {key: payload.get(key) for key in keys if payload.get(key) not in (None, "", [], {})}
    target_symbols = list(payload.get("target_symbols") or [])
    if target_symbols:
        compact["target_symbols"] = target_symbols[:COMPACT_PREVIEW_LIMIT]
        compact["target_symbol_count"] = len(target_symbols)
    return compact


def compact_event_window_metrics(metrics: Any) -> dict[str, Any]:
    payload = dict(metrics or {}) if isinstance(metrics, Mapping) else {}
    compact: dict[str, Any] = {}
    for key in (
        "event_study_mode",
        "event_sample_count",
        "event_anchor_count",
        "control_group_count",
        "event_sample_source",
        "traceable_to_event_samples",
        "event_audit_incomplete",
        "bhar",
        "abnormal_return",
        "total_return",
        "post_event_decay",
        "hit_ratio",
        "estimation_days_used",
        "post_days_used",
    ):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    anchors = payload.get("event_time_anchors")
    if isinstance(anchors, list):
        compact["event_time_anchors"] = anchors[:8]
        compact["event_time_anchor_count"] = len(anchors)
    return compact


def compact_scalar_metrics(metrics: Any) -> dict[str, Any]:
    payload = dict(metrics or {}) if isinstance(metrics, Mapping) else {}
    compact: dict[str, Any] = {}
    for key in SCALAR_METRIC_KEYS:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    for key in (
        "constraint_check",
        "target_quality_summary",
        "contamination_summary",
        "cost_assumptions",
        "backtest_assumptions",
        "backtest_metrics_contract",
        "execution_reality",
        "metric_source_audit",
        "period_robustness",
        "gate_3_evaluation",
        "multiple_testing",
    ):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            compact[key] = compact_json(value)
    event_metrics = compact_event_window_metrics(payload.get("event_window_metrics"))
    if event_metrics:
        compact["event_window_metrics"] = event_metrics
    return compact


def _target_symbols(payload: Mapping[str, Any], limit: int = COMPACT_PREVIEW_LIMIT) -> list[Any]:
    values = payload.get("target_symbols")
    if not values:
        params = dict(payload.get("params") or {}) if isinstance(payload.get("params"), Mapping) else {}
        values = params.get("target_symbols") or params.get("requested_target_symbols")
    if not values:
        task = dict(payload.get("research_task") or {}) if isinstance(payload.get("research_task"), Mapping) else {}
        values = task.get("target_symbols")
    if isinstance(values, (str, int)):
        return [values]
    return list(values or [])[:limit]


def compact_candidate_brief(candidate: Any, *, score: Any = None) -> dict[str, Any]:
    payload = dict(candidate or {}) if isinstance(candidate, Mapping) else {}
    research_task = dict(payload.get("research_task") or {}) if isinstance(payload.get("research_task"), Mapping) else {}
    provenance = dict(payload.get("candidate_provenance") or {}) if isinstance(payload.get("candidate_provenance"), Mapping) else {}
    gate_1 = dict(payload.get("gate_1_result") or {}) if isinstance(payload.get("gate_1_result"), Mapping) else {}
    gate_1_metrics = dict(gate_1.get("metrics") or {}) if isinstance(gate_1.get("metrics"), Mapping) else {}
    backtest_metrics = compact_scalar_metrics(payload.get("backtest_metrics") or {})
    outcome = compact_json(payload.get("backtest_outcome") or payload.get("backtest_result") or {})
    brief = {
        "strategy_type": payload.get("strategy_type"),
        "generator_type": payload.get("generator_type"),
        "candidate_family": payload.get("candidate_family") or provenance.get("candidate_family"),
        "strategy_profile": payload.get("strategy_profile") or provenance.get("strategy_profile"),
        "task_source": research_task.get("task_source"),
        "task_id": research_task.get("task_id"),
        "opportunity_type": research_task.get("opportunity_type"),
        "target_symbols": _target_symbols(payload),
        "candidate_contract_hash": payload.get("candidate_contract_hash"),
        "execution_contract_hash": payload.get("execution_contract_hash"),
        "tested_object_hash": payload.get("tested_object_hash"),
        "gate_1_passed": gate_1.get("passed"),
        "avg_sharpe": gate_1_metrics.get("avg_sharpe") or backtest_metrics.get("sharpe_ratio"),
        "priority_score": score if score is not None else gate_1_metrics.get("gate_2_priority_score"),
        "backtest_metrics": backtest_metrics,
        "backtest_outcome": outcome,
    }
    return {key: value for key, value in brief.items() if value not in (None, "", [], {})}


def compact_backtest_report(report: Any, *, preview_limit: int = COMPACT_PREVIEW_LIMIT) -> dict[str, Any]:
    payload = dict(report or {}) if isinstance(report, Mapping) else {}
    summary = compact_json(payload.get("summary") or {})
    diagnostics = dict(payload.get("diagnostics") or {}) if isinstance(payload.get("diagnostics"), Mapping) else {}

    passed_source = diagnostics.get("passed_preview")
    if passed_source is None:
        passed_source = payload.get("passed") or []
    failed_source = diagnostics.get("failed_preview")
    if failed_source is None:
        failed_source = payload.get("failed") or []

    result: dict[str, Any] = {
        "summary": summary,
        "diagnostics": {
            "passed_preview": [
                compact_candidate_brief(item) for item in list(passed_source or [])[:preview_limit]
            ],
            "failed_preview": [
                compact_candidate_brief(item) for item in list(failed_source or [])[:preview_limit]
            ],
            "passed_preview_count": min(len(list(passed_source or [])), preview_limit),
            "failed_preview_count": min(len(list(failed_source or [])), preview_limit),
            "passed_total_count": int(
                dict(summary or {}).get("passed_count") or len(list(payload.get("passed") or []))
            ),
            "failed_total_count": int(
                dict(summary or {}).get("failed_count") or len(list(payload.get("failed") or []))
            ),
        },
        "contract_note": "compact_backtest_summary_only",
    }
    for key in ("failed_reason_counts", "thresholds_by_type"):
        if isinstance(summary, Mapping) and summary.get(key) not in (None, "", [], {}):
            result["summary"][key] = compact_json(summary.get(key))
    dropped = [
        key
        for key in ("passed", "failed", "passed_candidates", "failed_candidates")
        if key in payload
    ]
    if dropped:
        result["dropped_heavy_fields"] = dropped
    return result


def _compact_gate_section(section: Any, *, preview_limit: int = COMPACT_PREVIEW_LIMIT) -> dict[str, Any]:
    payload = dict(section or {}) if isinstance(section, Mapping) else {}
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if value in (None, "", [], {}):
            continue
        if key == "report":
            compact[key] = compact_backtest_report(value, preview_limit=preview_limit)
            continue
        if key in {"passed_candidates", "failed_candidates"}:
            values = list(value or []) if isinstance(value, list) else []
            compact[key] = [compact_candidate_brief(item) for item in values[:preview_limit]]
            compact[f"{key}_count"] = len(values)
            compact[f"{key}_is_brief"] = True
            continue
        if key in {"failed", "passed"} and isinstance(value, list):
            compact[key] = [compact_json(item, max_list_items=4, max_dict_items=8) for item in value[:preview_limit]]
            compact[f"{key}_count"] = len(value)
            compact[f"{key}_is_preview"] = True
            continue
        compact[key] = compact_json(value)
    return compact


def compact_quality_gate_report(report: Any, *, preview_limit: int = COMPACT_PREVIEW_LIMIT) -> dict[str, Any]:
    payload = dict(report or {}) if isinstance(report, Mapping) else {}
    compact: dict[str, Any] = {}
    for key in ("gate_0", "pre_gate", "gate_1", "gate_2", "gate_3", "final_decision"):
        if key in payload:
            compact[key] = _compact_gate_section(payload.get(key), preview_limit=preview_limit)
    for key, value in payload.items():
        if key in compact or value in (None, "", [], {}):
            continue
        compact[key] = compact_json(value)
    compact["contract_note"] = "compact_quality_gate_summary_only"
    return compact


def bounded_payload(
    payload: Any,
    *,
    field_name: str,
    max_bytes: int = COMPACT_ARTIFACT_MAX_BYTES,
) -> dict[str, Any]:
    original_size = json_size_bytes(payload)
    compact = compact_json(payload)
    compact_size = json_size_bytes(compact)
    if compact_size <= max_bytes:
        if isinstance(compact, Mapping):
            return dict(compact)
        return {"value": compact}
    return {
        "storage_mode": "dropped_large_payload",
        "field_name": field_name,
        "truncated": True,
        "original_size_bytes": original_size,
        "compact_size_bytes": compact_size,
        "top_level_keys": [str(key) for key in list(dict(payload or {}).keys())[:24]]
        if isinstance(payload, Mapping)
        else [],
    }
