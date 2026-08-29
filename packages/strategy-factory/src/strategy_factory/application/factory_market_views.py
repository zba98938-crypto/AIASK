"""Factory market-facing derived views for research windows and Top N outputs."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Mapping, Sequence

DEFAULT_FULL_MARKET_TOPN = 20
DEFAULT_MAX_PER_INDUSTRY = 2
FULL_MARKET_TOPN_CONTRACT_VERSION = "strategy_factory.full_market_topn.v2"


def _string(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _list_of_strings(value: Any, *, limit: int | None = None) -> list[str]:
    result = [
        _string(item)
        for item in list(value or [])
        if _string(item)
    ]
    if limit is None:
        return result
    return result[: max(0, int(limit or 0))]


def _lookup(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _quantile(sorted_values: list[float], q: float) -> float:
    values = [float(item) for item in list(sorted_values or [])]
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    clamped_q = max(0.0, min(float(q or 0.0), 1.0))
    position = clamped_q * (len(values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def _score_distribution(scores: list[float]) -> dict[str, float]:
    normalized_scores = sorted(float(item) for item in list(scores or []))
    if not normalized_scores:
        return {}
    mean_score = sum(normalized_scores) / len(normalized_scores)
    variance = sum((score - mean_score) ** 2 for score in normalized_scores) / len(normalized_scores)
    return {
        "min": round(normalized_scores[0], 4),
        "p25": round(_quantile(normalized_scores, 0.25), 4),
        "p50": round(_quantile(normalized_scores, 0.50), 4),
        "p75": round(_quantile(normalized_scores, 0.75), 4),
        "p95": round(_quantile(normalized_scores, 0.95), 4),
        "max": round(normalized_scores[-1], 4),
        "std": round(math.sqrt(max(variance, 0.0)), 4),
    }


def _tie_cluster_summary(score_rows: Sequence[Mapping[str, Any]] | None) -> dict[str, float | int]:
    normalized_rows = [dict(item or {}) for item in list(score_rows or []) if isinstance(item, Mapping)]
    if not normalized_rows:
        return {}
    rounded_scores = [round(_float(dict(item).get("composite_score")), 4) for item in normalized_rows]
    counts = Counter(rounded_scores)
    top10_distinct = len({round(_float(dict(item).get("composite_score")), 4) for item in normalized_rows[:10]})
    largest_tie_size = max(counts.values()) if counts else 0
    equal_score_ratio = round(largest_tie_size / len(rounded_scores), 4) if rounded_scores else 0.0
    return {
        "largest_tie_size": int(largest_tie_size),
        "top10_distinct_score_count": int(top10_distinct),
        "equal_score_ratio": equal_score_ratio,
    }


def _component_activation_summary(score_rows: Sequence[Mapping[str, Any]] | None) -> dict[str, float]:
    normalized_rows = [dict(item or {}) for item in list(score_rows or []) if isinstance(item, Mapping)]
    if not normalized_rows:
        return {}
    component_names: list[str] = []
    for row in normalized_rows:
        for name in dict(row.get("component_scores") or {}).keys():
            token = _string(name)
            if token and token not in component_names:
                component_names.append(token)
    summary: dict[str, float] = {}
    for name in component_names:
        active_count = 0
        for row in normalized_rows:
            if abs(_float(dict(row.get("component_scores") or {}).get(name))) > 1e-9:
                active_count += 1
        summary[name] = round(active_count / len(normalized_rows), 4)
    return summary


def _dominant_component(constituents: Sequence[Mapping[str, Any]] | None) -> str | None:
    normalized_items = [dict(item or {}) for item in list(constituents or []) if isinstance(item, Mapping)]
    if not normalized_items:
        return None
    totals: dict[str, float] = {}
    for item in normalized_items:
        for key, value in dict(item.get("component_scores") or {}).items():
            totals[_string(key)] = totals.get(_string(key), 0.0) + abs(_float(value))
    if not totals:
        return None
    return sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))[0][0] or None


def _capped_component_counts(score_rows: Sequence[Mapping[str, Any]] | None) -> dict[str, int]:
    normalized_rows = [dict(item or {}) for item in list(score_rows or []) if isinstance(item, Mapping)]
    if not normalized_rows:
        return {}
    size_cap_hits = sum(
        1
        for row in normalized_rows
        if _float(dict(row.get("component_scores") or {}).get("size_score")) >= 11.999
    )
    return {
        "market_cap_score_hit_cap_count": int(size_cap_hits),
        "size_score_hit_cap_count": int(size_cap_hits),
    }


def _valuation_coverage_ratio(
    score_rows: Sequence[Mapping[str, Any]] | None,
    *,
    limit: int = 100,
) -> float:
    normalized_rows = [dict(item or {}) for item in list(score_rows or []) if isinstance(item, Mapping)][: max(1, int(limit or 100))]
    if not normalized_rows:
        return 0.0
    covered = sum(
        1
        for row in normalized_rows
        if abs(_float(dict(row.get("component_scores") or {}).get("valuation_score"))) > 1e-9
    )
    return round(covered / len(normalized_rows), 4)


def hydrate_full_market_topn_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(payload or {})
    metadata = dict(raw.get("metadata") or {})
    normalized = {**metadata, **raw}
    for key in (
        "score_contract_version",
        "score_quality",
        "score_distribution",
        "tie_cluster_summary",
        "component_activation_summary",
        "capped_component_counts",
        "active_factors",
        "hot_sectors",
        "cold_sectors",
        "stock_family_allocation_source_mode",
        "stock_family_allocation_avg_priority",
        "dominant_component",
        "valuation_signal_coverage_top100",
        "degraded_reasons",
    ):
        value = raw.get(key)
        if value in (None, "", [], {}):
            value = metadata.get(key)
        if value in (None, "", [], {}):
            continue
        normalized[key] = value
    if not normalized.get("metadata"):
        normalized["metadata"] = metadata
    return normalized


def _score_sort_key(row: Mapping[str, Any]) -> tuple[float, float, float, float, float, str]:
    payload = dict(row or {})
    component_scores = dict(payload.get("component_scores") or {})
    return (
        -_float(payload.get("composite_score")),
        -_float(component_scores.get("valuation_score")),
        -_float(component_scores.get("factor_alignment_score")),
        -_float(component_scores.get("allocation_score")),
        -_float(component_scores.get("size_score")),
        _string(payload.get("code")),
    )


def build_research_window_status(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(payload or {})
    loaded_stock_count = _int(
        _lookup(raw, "bulk_stock_matrix_loaded_stock_count", "loaded_stock_count")
    )
    eligible_stock_count = _int(
        _lookup(raw, "bulk_stock_matrix_eligible_stock_count", "eligible_stock_count")
    )
    planned_bulk_task_count = _int(
        _lookup(raw, "bulk_stock_matrix_planned_task_count", "planned_bulk_task_count")
    )
    selected_bulk_task_count = _int(
        _lookup(raw, "bulk_stock_task_count", "selected_bulk_task_count")
    )
    effective_task_budget = _int(
        _lookup(raw, "bulk_stock_matrix_effective_task_budget", "effective_task_budget")
    )
    requested_task_offset = _int(
        _lookup(raw, "bulk_stock_matrix_requested_task_offset", "requested_task_offset")
    )
    effective_task_offset = _int(
        _lookup(raw, "bulk_stock_matrix_effective_task_offset", "effective_task_offset")
    )
    next_task_offset = _int(
        _lookup(raw, "bulk_stock_matrix_next_task_offset", "next_task_offset")
    )
    batch_count = _int(
        _lookup(raw, "bulk_stock_matrix_batch_count", "batch_count")
    )
    selected_batch_count = _int(
        _lookup(raw, "bulk_stock_matrix_selected_batch_count", "selected_batch_count")
    )
    shard_count = _int(
        _lookup(raw, "bulk_stock_matrix_shard_count", "shard_count")
    )
    selected_shard_ids = [
        _int(item)
        for item in list(
            _lookup(raw, "bulk_stock_matrix_selected_shard_ids", "selected_shard_ids") or []
        )
        if _int(item) > 0
    ]
    stock_coverage_ratio = _float(
        _lookup(raw, "bulk_stock_matrix_stock_coverage_ratio", "stock_coverage_ratio")
    )
    available = any(
        value > 0
        for value in (
            loaded_stock_count,
            eligible_stock_count,
            planned_bulk_task_count,
            selected_bulk_task_count,
        )
    )
    return {
        "available": available,
        "loaded_stock_count": loaded_stock_count,
        "eligible_stock_count": eligible_stock_count,
        "planned_bulk_task_count": planned_bulk_task_count,
        "selected_bulk_task_count": selected_bulk_task_count,
        "effective_task_budget": effective_task_budget,
        "requested_task_offset": requested_task_offset,
        "effective_task_offset": effective_task_offset,
        "next_task_offset": next_task_offset,
        "batch_count": batch_count,
        "selected_batch_count": selected_batch_count,
        "shard_count": shard_count,
        "selected_shard_ids": selected_shard_ids,
        "stock_coverage_ratio": round(stock_coverage_ratio, 4),
    }


def select_full_market_topn_constituents(
    score_rows: Sequence[Mapping[str, Any]] | None,
    *,
    topn_n: int = DEFAULT_FULL_MARKET_TOPN,
    max_per_industry: int = DEFAULT_MAX_PER_INDUSTRY,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    industry_counts: Counter[str] = Counter()
    for raw in sorted(
        [dict(item or {}) for item in list(score_rows or []) if isinstance(item, Mapping)],
        key=_score_sort_key,
    ):
        row = dict(raw or {})
        if not bool(row.get("eligible", True)):
            continue
        industry = _string(row.get("industry")).lower() or "__unknown__"
        if industry_counts[industry] >= max(1, int(max_per_industry or 1)):
            continue
        selected.append(
            {
                "rank": _int(row.get("rank")),
                "selection_rank": len(selected) + 1,
                "code": _string(row.get("code")),
                "name": _string(row.get("name")) or _string(row.get("code")),
                "industry": _string(row.get("industry")) or None,
                "market_cap": row.get("market_cap"),
                "composite_score": round(_float(row.get("composite_score")), 4),
                "family_candidates": _list_of_strings(row.get("family_candidates"), limit=4),
                "component_scores": dict(row.get("component_scores") or {}),
            }
        )
        industry_counts[industry] += 1
        if len(selected) >= max(1, int(topn_n or DEFAULT_FULL_MARKET_TOPN)):
            break
    return selected


def build_full_market_topn_payload(
    *,
    as_of_date: str | None,
    universe_count: int,
    eligible_count: int,
    score_rows: Sequence[Mapping[str, Any]] | None,
    score_contract_version: str | None = None,
    active_factors: Sequence[str] | None = None,
    hot_sectors: Sequence[str] | None = None,
    cold_sectors: Sequence[str] | None = None,
    stock_family_allocation_source_mode: str | None = None,
    stock_family_allocation_avg_priority: float | None = None,
    topn_n: int = DEFAULT_FULL_MARKET_TOPN,
    max_per_industry: int = DEFAULT_MAX_PER_INDUSTRY,
    selection_method: str = "deterministic_bulk_priority_v2",
) -> dict[str, Any]:
    normalized_rows = sorted(
        [dict(item or {}) for item in list(score_rows or []) if isinstance(item, Mapping)],
        key=_score_sort_key,
    )
    constituents = select_full_market_topn_constituents(
        normalized_rows,
        topn_n=topn_n,
        max_per_industry=max_per_industry,
    )
    industry_distribution = Counter(
        _string(item.get("industry")) or "未分类"
        for item in list(constituents or [])
    )
    average_score = 0.0
    if constituents:
        average_score = round(
            sum(_float(item.get("composite_score")) for item in constituents) / len(constituents),
            4,
        )
    score_distribution = _score_distribution([
        _float(item.get("composite_score"))
        for item in normalized_rows
    ])
    tie_cluster_summary = _tie_cluster_summary(normalized_rows)
    component_activation_summary = _component_activation_summary(normalized_rows)
    capped_component_counts = _capped_component_counts(normalized_rows)
    valuation_signal_coverage_top100 = _valuation_coverage_ratio(normalized_rows, limit=100)
    normalized_active_factors = _list_of_strings(active_factors, limit=12)
    normalized_hot_sectors = _list_of_strings(hot_sectors, limit=20)
    normalized_cold_sectors = _list_of_strings(cold_sectors, limit=20)
    degraded_reasons: list[str] = []
    if not normalized_active_factors:
        degraded_reasons.append("missing_active_factors")
    if not normalized_hot_sectors and not normalized_cold_sectors:
        degraded_reasons.append("missing_sector_regime")
    if valuation_signal_coverage_top100 < 0.1:
        degraded_reasons.append("low_valuation_signal_coverage")
    if _int(tie_cluster_summary.get("top10_distinct_score_count")) < 3:
        degraded_reasons.append("low_top10_score_separation")
    score_quality = "degraded" if degraded_reasons else "healthy"
    dominant_component = _dominant_component(constituents)
    score_contract = _string(score_contract_version) or FULL_MARKET_TOPN_CONTRACT_VERSION
    metadata = {
        "score_contract_version": score_contract,
        "score_quality": score_quality,
        "active_factors": normalized_active_factors,
        "hot_sectors": normalized_hot_sectors,
        "cold_sectors": normalized_cold_sectors,
        "stock_family_allocation_source_mode": _string(stock_family_allocation_source_mode) or None,
        "stock_family_allocation_avg_priority": round(_float(stock_family_allocation_avg_priority), 4),
        "score_distribution": score_distribution,
        "tie_cluster_summary": tie_cluster_summary,
        "component_activation_summary": component_activation_summary,
        "capped_component_counts": capped_component_counts,
        "valuation_signal_coverage_top100": valuation_signal_coverage_top100,
        "dominant_component": dominant_component,
        "degraded_reasons": degraded_reasons,
    }
    return {
        "contract_version": FULL_MARKET_TOPN_CONTRACT_VERSION,
        "score_contract_version": score_contract,
        "available": bool(normalized_rows),
        "as_of_date": _string(as_of_date) or None,
        "selection_method": selection_method,
        "universe_count": _int(universe_count),
        "eligible_count": _int(eligible_count),
        "score_row_count": len(normalized_rows),
        "topn_n": max(1, int(topn_n or DEFAULT_FULL_MARKET_TOPN)),
        "score_quality": score_quality,
        "score_distribution": score_distribution,
        "tie_cluster_summary": tie_cluster_summary,
        "component_activation_summary": component_activation_summary,
        "capped_component_counts": capped_component_counts,
        "active_factors": normalized_active_factors,
        "hot_sectors": normalized_hot_sectors,
        "cold_sectors": normalized_cold_sectors,
        "stock_family_allocation_source_mode": _string(stock_family_allocation_source_mode) or None,
        "stock_family_allocation_avg_priority": round(_float(stock_family_allocation_avg_priority), 4),
        "valuation_signal_coverage_top100": valuation_signal_coverage_top100,
        "dominant_component": dominant_component,
        "degraded_reasons": degraded_reasons,
        "selection_rules": {
            "min_history_bars": 100,
            "max_per_industry": max(1, int(max_per_industry or DEFAULT_MAX_PER_INDUSTRY)),
            "order_by": "composite_score_desc,valuation_score_desc,factor_alignment_score_desc,allocation_score_desc,size_score_desc,code_asc",
            "source": selection_method,
        },
        "constituents": constituents,
        "industry_distribution": dict(industry_distribution),
        "average_topn_score": average_score,
        "metadata": metadata,
    }


def build_portfolio_candidate_from_topn(
    topn_payload: Mapping[str, Any] | None,
    *,
    run_id: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    payload = hydrate_full_market_topn_payload(topn_payload)
    constituents = [
        dict(item or {})
        for item in list(payload.get("constituents") or [])
        if _string(dict(item or {}).get("code"))
    ]
    topn_n = max(1, int(payload.get("topn_n") or len(constituents) or DEFAULT_FULL_MARKET_TOPN))
    codes = [
        _string(item.get("code"))
        for item in constituents
        if _string(item.get("code"))
    ]
    if not codes:
        raise ValueError("Top N constituents are required")
    weight = round(1.0 / len(codes), 8)
    weights = {code: weight for code in codes}
    if weights:
        last_code = list(weights.keys())[-1]
        weights[last_code] = round(1.0 - sum(value for key, value in weights.items() if key != last_code), 8)
    snapshot_id = _string(payload.get("snapshot_id")) or f"topn_{run_id}"
    strategy_id = _string(payload.get("portfolio_candidate_id")) or f"factory_topn_{run_id}"
    as_of_date = _string(payload.get("as_of_date"))
    selection_rules = dict(payload.get("selection_rules") or {})
    selection_diagnostics_summary = {
        "score_distribution": dict(payload.get("score_distribution") or {}),
        "tie_cluster_summary": dict(payload.get("tie_cluster_summary") or {}),
        "component_activation_summary": dict(payload.get("component_activation_summary") or {}),
        "capped_component_counts": dict(payload.get("capped_component_counts") or {}),
        "dominant_component": payload.get("dominant_component"),
    }
    return {
        "id": strategy_id,
        "name": f"全市场 Top {topn_n} 组合策略",
        "description": (
            f"基于策略工厂全市场统一评分生成的 Top {topn_n} 等权组合。"
            f"{' 评分日期 ' + as_of_date + '。' if as_of_date else ''}"
        ),
        "author_id": "strategy_factory",
        "strategy_type": "topn_equity_portfolio",
        "status": "draft",
        "tags": [
            "factory",
            "full_market_topn",
            "portfolio_candidate",
            "system_generated",
        ],
        "params": {
            "target_symbols": list(codes),
            "target_weights": dict(weights),
            "stock_pool": {"selection_mode": "explicit", "symbols": list(codes)},
            "rebalance_frequency": "weekly",
            "validation_focus": "target_plus_representative",
            "validation_profile": {
                "profile": "factor_rank_validation",
                "validation_focus": "target_plus_representative",
                "primary_validation_layer": "combined",
            },
            "metadata": {
                "selection_source": "full_market_topn",
                "selection_snapshot_id": snapshot_id,
                "selection_n": topn_n,
                "selection_rules": selection_rules,
                "factory_run_id": run_id,
                "trace_id": _string(trace_id) or None,
                "score_contract_version": _string(payload.get("score_contract_version")) or None,
                "score_quality": _string(payload.get("score_quality")) or None,
                "selection_diagnostics_summary": selection_diagnostics_summary,
            },
        },
        "factor_weights": {},
    }
