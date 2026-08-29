"""Candidate strategy profile normalization for generation, gates and submission."""

from __future__ import annotations

import json
from typing import Any, Optional

from .targets import _extract_target_codes_from_payload, _normalize_research_task_contract

_HOLDING_BUCKET_BY_TYPE = {
    "momentum": "short",
    "ma_cross": "medium",
    "rsi": "short",
    "volatility_breakout": "short",
    "event_structure_breakout": "short",
    "gap_fill": "short",
    "mean_reversion_short": "short",
    "value_factor": "long",
    "quality_factor": "medium",
    "growth_factor": "medium",
    "multi_factor": "medium",
    "macro_timing": "medium",
    "sector_rotation": "medium",
    "north_capital_track": "medium",
    "margin_divergence": "medium",
    "dsl_rule": "medium",
}

_ALPHA_SOURCE_BY_TYPE = {
    "momentum": "technical",
    "ma_cross": "technical",
    "rsi": "technical",
    "volatility_breakout": "technical",
    "event_structure_breakout": "technical",
    "gap_fill": "technical",
    "mean_reversion_short": "technical",
    "value_factor": "fundamental",
    "quality_factor": "fundamental",
    "growth_factor": "fundamental",
    "multi_factor": "multi_factor",
    "macro_timing": "macro",
    "sector_rotation": "rotation",
    "north_capital_track": "capital_flow",
    "margin_divergence": "capital_flow",
    "dsl_rule": "hybrid",
}

_RISK_LEVEL_BY_TYPE = {
    "momentum": "high",
    "ma_cross": "medium",
    "rsi": "medium",
    "volatility_breakout": "high",
    "event_structure_breakout": "high",
    "gap_fill": "medium",
    "mean_reversion_short": "medium",
    "value_factor": "medium",
    "quality_factor": "low",
    "growth_factor": "high",
    "multi_factor": "medium",
    "macro_timing": "medium",
    "sector_rotation": "medium",
    "north_capital_track": "medium",
    "margin_divergence": "medium",
    "dsl_rule": "medium",
}

_POOL_PROFILE_BY_TYPE = {
    "momentum": "high_vol_growth",
    "volatility_breakout": "high_vol_growth",
    "event_structure_breakout": "high_vol_growth",
    "growth_factor": "high_vol_growth",
    "gap_fill": "high_vol_growth",
    "mean_reversion_short": "high_vol_growth",
    "rsi": "high_vol_growth",
    "quality_factor": "low_vol_defensive",
    "value_factor": "low_vol_defensive",
    "macro_timing": "low_vol_defensive",
    "north_capital_track": "cycle_resource",
    "sector_rotation": "cycle_resource",
    "margin_divergence": "cycle_resource",
    "ma_cross": "cycle_resource",
    "dsl_rule": "mixed",
}


def _normalize_tags(values: Any) -> list[str]:
    items = values if isinstance(values, list) else [values]
    tags: list[str] = []
    seen: set[str] = set()
    for value in items:
        raw = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
        if not raw or raw in seen:
            continue
        seen.add(raw)
        tags.append(raw)
    return tags


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _holding_bucket_from_task(strategy_type: str, task: dict[str, Any], candidate: dict[str, Any]) -> str:
    if str(candidate.get("holding_period_bucket") or "").strip():
        return str(candidate.get("holding_period_bucket")).strip().lower()
    horizon = dict(candidate.get("holding_horizon") or {})
    if not horizon:
        horizon = dict(task.get("holding_window") or {})
    max_days = _safe_int(horizon.get("max_days"))
    if max_days > 0:
        if max_days <= 5:
            return "short"
        if max_days <= 20:
            return "medium"
        return "long"
    return _HOLDING_BUCKET_BY_TYPE.get(strategy_type, "medium")


def _infer_regime_fit(strategy_type: str, task: dict[str, Any], snapshot: Optional[dict[str, Any]]) -> str:
    if str(task.get("regime_fit") or "").strip():
        return str(task.get("regime_fit")).strip().lower()
    haystack = " ".join(
        [
            str(task.get("theme") or ""),
            str(task.get("opportunity_type") or ""),
            str(task.get("candidate_family") or ""),
            str((task.get("event_context") or {}).get("direction") or ""),
        ]
    ).lower()
    if any(token in haystack for token in ("trend", "breakout", "momentum", "risk_on")):
        return "trend_expansion"
    if any(token in haystack for token in ("reversal", "oversold", "repair", "risk_off", "defensive")):
        return "mean_reversion"
    if any(token in haystack for token in ("rotation", "neutral", "range", "sideways")):
        return "rotation_balanced"
    if any(token in haystack for token in ("event", "announcement", "news")):
        return "event_sensitive"
    fg = 50.0
    try:
        fg = float((snapshot or {}).get("fear_greed_index") or 50.0)
    except (TypeError, ValueError):
        fg = 50.0
    if strategy_type in {"rsi", "value_factor", "gap_fill", "mean_reversion_short"} or fg <= 40:
        return "mean_reversion"
    if strategy_type in {"momentum", "growth_factor", "volatility_breakout", "event_structure_breakout", "north_capital_track"} or fg >= 60:
        return "trend_expansion"
    if strategy_type in {"sector_rotation", "margin_divergence"}:
        return "rotation_balanced"
    return "rotation_balanced"


def _infer_direction_bias(strategy_type: str, task: dict[str, Any], candidate: dict[str, Any]) -> str:
    explicit = str(
        candidate.get("direction_bias")
        or task.get("direction_bias")
        or (task.get("event_context") or {}).get("direction")
        or task.get("direction")
        or ""
    ).strip().lower()
    if explicit in {"bullish", "up", "long", "long_only"}:
        return "long_only"
    if explicit in {"bearish", "down", "defensive"}:
        return "defensive_long"
    if strategy_type in {"rsi", "value_factor", "quality_factor", "gap_fill", "mean_reversion_short"}:
        return "mean_reversion_long"
    if strategy_type in {"momentum", "growth_factor", "ma_cross", "volatility_breakout", "event_structure_breakout", "north_capital_track"}:
        return "trend_follow_long"
    return "long_only"


def _infer_generator_mode(candidate: dict[str, Any], task: dict[str, Any]) -> str:
    explicit = str(
        candidate.get("generator_mode")
        or candidate.get("generator_type")
        or dict(candidate.get("params") or {}).get("generator_type")
        or task.get("task_source")
        or "rule"
    ).strip().lower()
    if explicit in {"external_llm", "pipeline_staged", "llm_proxy", "llm_proxy_fallback"}:
        return explicit
    if explicit in {"bulk_stock_matrix", "snapshot", "event_driven"}:
        return explicit
    return explicit or "rule"


def infer_candidate_strategy_profile(
    candidate: Optional[dict[str, Any]],
    *,
    snapshot: Optional[dict[str, Any]] = None,
    research_task: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    item = dict(candidate or {})
    task = _normalize_research_task_contract({**dict(research_task or {}), **dict(item.get("research_task") or {})})
    strategy_type = str(item.get("strategy_type") or "unknown").strip().lower() or "unknown"
    target_symbols = _extract_target_codes_from_payload({**task, **item}, limit=12)
    candidate_family = str(
        item.get("candidate_family")
        or task.get("candidate_family")
        or task.get("opportunity_type")
        or strategy_type
    ).strip().lower() or strategy_type
    pool_profile = str(
        item.get("pool_profile")
        or task.get("pool_profile")
        or _POOL_PROFILE_BY_TYPE.get(strategy_type, "mixed")
    ).strip().lower() or "mixed"
    volatility_bucket = str(
        item.get("volatility_bucket")
        or task.get("volatility_bucket")
        or ("high" if pool_profile == "high_vol_growth" else "low" if pool_profile == "low_vol_defensive" else "medium")
    ).strip().lower() or "medium"
    liquidity_bucket = str(
        item.get("liquidity_bucket")
        or task.get("liquidity_bucket")
        or ("stable_low_velocity" if pool_profile == "low_vol_defensive" else "high_liquidity")
    ).strip().lower() or "high_liquidity"
    holding_period_bucket = _holding_bucket_from_task(strategy_type, task, item)
    alpha_source = str(
        item.get("alpha_source")
        or task.get("alpha_source")
        or _ALPHA_SOURCE_BY_TYPE.get(strategy_type, "hybrid")
    ).strip().lower() or "hybrid"
    risk_level = str(
        item.get("risk_level")
        or task.get("risk_level")
        or (
            "high"
            if pool_profile == "high_vol_growth"
            else "low"
            if pool_profile == "low_vol_defensive"
            else _RISK_LEVEL_BY_TYPE.get(strategy_type, "medium")
        )
    ).strip().lower() or "medium"
    regime_fit = _infer_regime_fit(strategy_type, task, snapshot)
    generator_mode = _infer_generator_mode(item, task)
    direction_bias = _infer_direction_bias(strategy_type, task, item)
    validation_profile = dict(item.get("validation_profile") or {})
    validation_profile_name = str(validation_profile.get("profile") or "").strip().lower() or None
    family_id_parts = [
        candidate_family,
        holding_period_bucket,
        alpha_source,
        str(len(target_symbols) or 0),
    ]
    return {
        "holding_period_bucket": holding_period_bucket,
        "strategy_family": candidate_family,
        "pool_profile": pool_profile,
        "volatility_bucket": volatility_bucket,
        "liquidity_bucket": liquidity_bucket,
        "alpha_source": alpha_source,
        "direction_bias": direction_bias,
        "risk_level": risk_level,
        "regime_fit": regime_fit,
        "generator_mode": generator_mode,
        "validation_profile": validation_profile_name,
        "candidate_family_id": "_".join(part for part in family_id_parts if part),
        "target_symbol_count": len(target_symbols),
        "target_symbols": list(target_symbols),
        "task_source": str(task.get("task_source") or "").strip().lower() or None,
    }


def apply_candidate_strategy_profile(
    candidate: Optional[dict[str, Any]],
    *,
    snapshot: Optional[dict[str, Any]] = None,
    research_task: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    item = dict(candidate or {})
    if not item:
        return {}

    profile = infer_candidate_strategy_profile(item, snapshot=snapshot, research_task=research_task)
    tags = _normalize_tags(item.get("tags") or [])
    derived_tags = _normalize_tags(
        [
            item.get("strategy_type"),
            item.get("candidate_family") or profile.get("strategy_family"),
            f"horizon_{profile.get('holding_period_bucket')}",
            f"alpha_{profile.get('alpha_source')}",
            f"risk_{profile.get('risk_level')}",
            f"regime_{profile.get('regime_fit')}",
            f"pool_{profile.get('pool_profile')}",
            f"vol_{profile.get('volatility_bucket')}",
            f"generator_{profile.get('generator_mode')}",
            f"task_{profile.get('task_source')}",
            "single_name" if int(profile.get("target_symbol_count") or 0) <= 1 else "basket_candidate",
        ]
    )
    merged_tags = list(dict.fromkeys([*tags, *derived_tags]))

    item["strategy_profile"] = profile
    item["tags"] = merged_tags
    item["candidate_family"] = item.get("candidate_family") or profile.get("strategy_family")
    item["holding_period_bucket"] = item.get("holding_period_bucket") or profile.get("holding_period_bucket")
    item["pool_profile"] = item.get("pool_profile") or profile.get("pool_profile")
    item["volatility_bucket"] = item.get("volatility_bucket") or profile.get("volatility_bucket")
    item["liquidity_bucket"] = item.get("liquidity_bucket") or profile.get("liquidity_bucket")
    item["alpha_source"] = item.get("alpha_source") or profile.get("alpha_source")
    item["risk_level"] = item.get("risk_level") or profile.get("risk_level")
    item["regime_fit"] = item.get("regime_fit") or profile.get("regime_fit")
    item["direction_bias"] = item.get("direction_bias") or profile.get("direction_bias")
    item["generator_mode"] = item.get("generator_mode") or profile.get("generator_mode")

    params = dict(item.get("params") or {})
    params["strategy_profile"] = dict(profile)
    dsl = dict(params.get("dsl") or {})
    if dsl:
        metadata = dict(dsl.get("metadata") or {})
        metadata["strategy_profile"] = dict(profile)
        metadata["strategy_tags"] = list(merged_tags)
        dsl["metadata"] = metadata
        params["dsl"] = dsl
    item["params"] = params
    return item


def candidate_signature(candidate: Optional[dict[str, Any]]) -> str:
    item = dict(candidate or {})
    params = dict(item.get("params") or {})
    for key in (
        "candidate_local_attempt_count",
        "candidate_local_selected_count",
        "factory_attempt_count",
        "factory_global_attempt_count",
        "factory_global_selected_count",
        "factory_selected_count",
        "task_attempt_count",
        "task_local_attempt_count",
        "task_local_selected_count",
        "task_selected_count",
        "research_task",
        "requested_target_symbols",
        "stock_pool",
        "target_symbols",
        "strategy_profile",
        "candidate_contract_hash",
        "candidate_contract_snapshot",
        "candidate_identity_signature",
        "candidate_lineage_contract",
        "dsl_signature",
        "entry_exit_signature",
        "execution_contract_hash",
        "factor_signature",
        "logic_signature",
        "legacy_identity_partial",
        "resolved_candidate_envelope",
        "tested_object_hash",
        "tested_object_backfill_incomplete",
    ):
        params.pop(key, None)
    payload = {
        "strategy_type": str(item.get("strategy_type") or "").strip().lower(),
        "candidate_family": str(item.get("candidate_family") or "").strip().lower(),
        "target_symbols": list(_extract_target_codes_from_payload(item, limit=12)),
        "params": params,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
