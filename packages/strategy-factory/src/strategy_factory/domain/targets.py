"""策略工厂状态、任务合同与目标池工具。"""
from __future__ import annotations

# --- targets_parts/matching.py ---
import math
from typing import Any, Dict, List, Optional

from .constants import REPRESENTATIVE_STOCKS

_VALIDATION_PEER_CODES_BY_FAMILY: Dict[str, List[str]] = {
    "momentum": ["300750", "601012", "002415", "300059", "002594", "601318"],
    "ma_cross": ["300750", "601012", "002415", "300059", "002594", "601318"],
    "volatility_breakout": ["300750", "601012", "002415", "300059", "002594", "601318"],
    "event_structure_breakout": ["688336", "600519", "000858", "601318"],
    "quality_factor": ["600519", "000858", "600036", "000333", "600276", "601318"],
    "value_factor": ["600519", "000858", "600036", "000333", "600276", "601318"],
    "growth_factor": ["300750", "601012", "002594", "300124", "300308", "002415"],
    "multi_factor": ["600519", "000858", "600036", "000333", "300750", "601318"],
}

_VALIDATION_PEER_UNIVERSE_BY_FAMILY: Dict[str, List[str]] = {
    "momentum": [
        "300750", "601012", "002415", "300059", "002594", "601318",
        "002460", "300308", "300124", "603259", "600438", "000063",
    ],
    "ma_cross": [
        "300750", "601012", "002415", "300059", "002594", "601318",
        "002460", "300308", "300124", "603259", "600438", "000063",
    ],
    "volatility_breakout": [
        "300750", "601012", "002415", "300059", "002594", "601318",
        "300308", "002460", "603259", "000063", "688111", "300274",
    ],
    "event_structure_breakout": [
        "688336", "600519", "000858", "601318",
        "688187", "688303", "688599", "300620",
    ],
    "quality_factor": [
        "600519", "000858", "600036", "000333", "600276", "601318",
        "600809", "600887", "002415", "603288", "601888", "600690",
    ],
    "value_factor": [
        "600519", "000858", "600036", "000333", "600276", "601318",
        "600887", "601166", "600309", "000651", "600690", "601888",
    ],
    "growth_factor": [
        "300750", "601012", "002594", "300124", "300308", "002415",
        "688111", "300274", "688981", "002460", "300059", "603986",
    ],
    "multi_factor": [
        "600519", "000858", "600036", "000333", "300750", "601318",
        "601012", "002594", "603288", "600276", "300059", "002415",
    ],
}

_FACTOR_RANK_VALIDATION_SAMPLE_MIN = 12

# 统计验证可行下限：任何策略的验证面板有效股票数都必须高于 IC 引擎每期最小样本数
# (aiask_quant_core.validation 的 min_samples_per_period=10)。低于该值时每个截面期的
# IC 会被丢弃 → walk_forward n_folds=0 / oos_ic_mean=0 → Gate-3 误判"无统计证据"。
# 留 2 只裕量（12 > 10），且 _build_strategy_panels 取数后还会因 K 线长度不足再过滤，
# 故面板默认会取更多（见 FACTORY_VALIDATION_PANEL_SAMPLE_SIZE）。
_STATISTICAL_PANEL_MIN_STOCKS = 12


def _is_index_reference_code(value: Any) -> bool:
    """Identify explicit mainland index namespaces, without excluding ETFs.

    Bare 000001 is a stock. Exchange-qualified sh000001/000001.SH is an
    index. Keep index data available for benchmarks; this is a target check.
    """
    code = str(value or "").strip().lower()
    if "." in code:
        number, market = code.rsplit(".", 1)
        if market in {"sh", "sz", "bj"}:
            code = market + number
    return (
        len(code) == 8 and code[2:].isdigit()
        and (code.startswith("sh000") or code.startswith("sz399") or code.startswith("bj899"))
    ) or (len(code) == 6 and code.isdigit() and code.startswith("399"))


def _non_tradable_index_target_codes(payload: Optional[dict]) -> List[str]:
    return [code for code in _extract_target_codes_from_payload(payload, limit=40)
            if _is_index_reference_code(code)]


def _primary_strategy_family(payload: Optional[dict]) -> str:
    item = dict(payload or {})
    for source in (
        item.get("candidate_family"),
        item.get("preferred_strategy_types"),
        item.get("allowed_strategy_types"),
        item.get("strategy_preferences"),
    ):
        values = _normalize_string_list(source, limit=1)
        if values:
            return str(values[0] or "").strip().lower()
    return ""


def _default_holding_window_for_family(
    family: str,
    *,
    task_source: str,
    horizon: str = "",
) -> dict[str, Any]:
    normalized_family = str(family or "").strip().lower()
    normalized_horizon = str(horizon or "").strip().lower()
    if any(token in normalized_horizon for token in ("1_5", "5d", "intraday")):
        return {"max_days": 5}
    if any(token in normalized_horizon for token in ("5_20", "20d", "swing")):
        return {"max_days": 20}
    if normalized_family == "quality_factor":
        return {"min_days": 30, "max_days": 84}
    if normalized_family == "ma_cross":
        return {"min_days": 14, "max_days": 48}
    if normalized_family == "momentum":
        return {"min_days": 14, "max_days": 42}
    if normalized_family == "growth_factor":
        return {"min_days": 18, "max_days": 60}
    if task_source == "event_driven":
        return {"max_days": 10}
    return {"max_days": 20}


def _default_validation_profile_for_task(
    *,
    family: str,
    task_source: str,
    validation_focus: str,
) -> dict[str, Any]:
    normalized_family = str(family or "").strip().lower()
    normalized_focus = _normalize_validation_focus(validation_focus) or (
        "event_target_only" if task_source == "event_driven" else "target_plus_representative"
    )
    if normalized_family == "macro_timing":
        profile = "macro_regime_validation"
    elif task_source == "event_driven" or normalized_focus == "event_target_only":
        profile = "event_trade_validation"
        normalized_focus = "event_target_only"
    elif normalized_family == "quality_factor" and normalized_focus in {
        "candidate_target_only",
        "target_only",
        "target_plus_family_peer",
    }:
        profile = "trade_rule_validation"
    elif normalized_family in {"value_factor", "quality_factor", "growth_factor", "multi_factor", "sentiment", "sentiment_factor"}:
        profile = "factor_rank_validation"
    else:
        profile = "trade_rule_validation"
    return {
        "profile": profile,
        "validation_focus": normalized_focus,
        "primary_validation_layer": "target" if normalized_focus in {"candidate_target_only", "event_target_only", "target_only"} else "combined",
    }


def _normalize_validation_focus(value: Any) -> str:
    return str(value or "").strip().lower()


def _resolve_validation_focus_layer(validation_focus: str) -> str:
    focus = _normalize_validation_focus(validation_focus)
    if focus in {"candidate_target_only", "event_target_only", "target_only"}:
        return "target_only"
    if focus in {"target_plus_family_peer", "family_peer", "target_plus_peer"}:
        return "family_peer"
    if focus in {"sector_peer", "target_plus_sector_peer"}:
        return "sector_peer"
    return "broad_market"


def _code_board_bucket(code: str) -> str:
    token = str(code or "").strip()
    if token.startswith("688"):
        return "star"
    if token.startswith("300"):
        return "chi_next"
    if token.startswith(("600", "601", "603", "605")):
        return "sh_main"
    if token.startswith(("000", "001", "002", "003")):
        return "sz_main"
    return "other"


def _code_prefix_bucket(code: str) -> str:
    token = str(code or "").strip()
    if len(token) >= 3:
        return token[:3]
    if len(token) >= 2:
        return token[:2]
    return token


def _score_peer_candidate(target_codes: List[str], candidate_code: str) -> tuple[int, int, str]:
    candidate = str(candidate_code or "").strip()
    if not candidate:
        return (0, 0, "")
    if not target_codes:
        return (0, 0, candidate)
    target_boards = {_code_board_bucket(code) for code in target_codes}
    target_prefixes = {_code_prefix_bucket(code) for code in target_codes}
    board_score = 2 if _code_board_bucket(candidate) in target_boards else 0
    prefix_score = 1 if _code_prefix_bucket(candidate) in target_prefixes else 0
    return (board_score + prefix_score, board_score, candidate)


def _resolve_dynamic_family_peers(
    strategy_type: str,
    target_codes: List[str],
    *,
    sample_size: int,
) -> List[str]:
    family = str(strategy_type or "").strip().lower()
    anchor_peers = [
        code for code in list(_VALIDATION_PEER_CODES_BY_FAMILY.get(family) or [])
        if code not in set(target_codes)
    ]
    peer_universe = list(_VALIDATION_PEER_UNIVERSE_BY_FAMILY.get(family) or [])
    if not peer_universe and not anchor_peers:
        return []
    deduped_universe = [
        code for code in dict.fromkeys([*peer_universe, *anchor_peers])
        if code not in set(target_codes) and code not in set(anchor_peers)
    ]
    ranked = sorted(
        deduped_universe,
        key=lambda code: _score_peer_candidate(target_codes, code),
        reverse=True,
    )
    limit = max(sample_size * 2, 8)
    return list(dict.fromkeys([*anchor_peers, *ranked]))[:limit]


def _resolve_strategy_sample_selection(
    strategy_type: str,
    params: dict,
    sample_size: int = 6,
) -> dict[str, Any]:
    validation_profile = dict(params.get("validation_profile") or {})
    profile_name = str(validation_profile.get("profile") or "").strip().lower()
    requested_sample_size = max(1, int(sample_size or 6))
    # 兜底防线：任何调用点（含 dedup 的 sample_size=4）传入的样本数都不得低于统计可行下限，
    # 否则 IC 引擎每期样本 < 10 → OOS 证据恒空 → Gate-3 全拒（策略工厂零产出主因）。
    effective_sample_size = max(requested_sample_size, _STATISTICAL_PANEL_MIN_STOCKS)
    statistical_sample_min = _STATISTICAL_PANEL_MIN_STOCKS
    if profile_name == "factor_rank_validation":
        statistical_sample_min = max(_STATISTICAL_PANEL_MIN_STOCKS, _FACTOR_RANK_VALIDATION_SAMPLE_MIN)
        effective_sample_size = max(effective_sample_size, statistical_sample_min)
    research_task = dict(params.get("research_task") or {})
    validation_focus = str(
        validation_profile.get("validation_focus")
        or research_task.get("validation_focus")
        or ""
    ).strip().lower()
    validation_focus_layer = _resolve_validation_focus_layer(validation_focus)
    target_codes = _extract_target_codes_from_payload(
        {"strategy_type": strategy_type, "params": params},
        limit=max(effective_sample_size, 12),
    )
    static_family_peers = list(
        _VALIDATION_PEER_CODES_BY_FAMILY.get(str(strategy_type or "").strip().lower()) or []
    )
    dynamic_family_peers = _resolve_dynamic_family_peers(
        strategy_type,
        target_codes,
        sample_size=effective_sample_size,
    )

    sample_selection_mode = "representative_only"
    sample_alignment_reason = "broad_market_representative_fallback"
    if validation_focus_layer == "target_only" and target_codes:
        combined = list(dict.fromkeys([*target_codes, *dynamic_family_peers, *REPRESENTATIVE_STOCKS]))
        sample_selection_mode = "target_plus_dynamic_family_peer"
        sample_alignment_reason = "target_only_with_family_aligned_dynamic_peers"
    elif validation_focus_layer == "family_peer" and target_codes:
        combined = list(dict.fromkeys([*target_codes, *dynamic_family_peers, *static_family_peers]))
        sample_selection_mode = "family_peer_dynamic_panel"
        sample_alignment_reason = "family_peer_panel_aligned_to_target_codes"
    elif validation_focus_layer == "sector_peer" and target_codes:
        combined = list(dict.fromkeys([*target_codes, *dynamic_family_peers, *REPRESENTATIVE_STOCKS]))
        sample_selection_mode = "sector_peer_proxy_panel"
        sample_alignment_reason = "sector_peer_proxy_via_family_and_representative_mix"
    elif target_codes and dynamic_family_peers:
        combined = list(dict.fromkeys([*target_codes, *dynamic_family_peers, *REPRESENTATIVE_STOCKS]))
        validation_focus_layer = "family_peer"
        sample_selection_mode = "target_plus_dynamic_family_peer"
        sample_alignment_reason = "target_codes_present_promoted_to_family_peer_panel"
    else:
        combined = list(dict.fromkeys([*target_codes, *REPRESENTATIVE_STOCKS]))

    requested_size = max(
        effective_sample_size,
        min(len(combined), max(effective_sample_size, len(target_codes))),
    )
    sample_codes = combined[:requested_size]
    return {
        "sample_codes": sample_codes,
        "target_codes": list(target_codes),
        "family_peer_codes": list(dynamic_family_peers[: max(effective_sample_size, 8)]),
        "validation_focus": validation_focus or None,
        "validation_focus_layer": validation_focus_layer,
        "sample_selection_mode": sample_selection_mode,
        "sample_alignment_reason": sample_alignment_reason,
        "sample_code_count": int(len(sample_codes)),
        "requested_sample_size": int(requested_sample_size),
        "effective_sample_size": int(effective_sample_size),
        "statistical_sample_min": int(statistical_sample_min),
        "statistical_sample_expanded": bool(effective_sample_size > requested_sample_size),
    }


async def _update_strategy_status(db, strategy_id: str, status: str, **kwargs) -> None:
    try:
        await db.update_strategy_status(strategy_id, status, **kwargs)
    except TypeError:
        await db.update_strategy_status(strategy_id, status)


def _normalize_target_codes(values: Any, limit: int = 12) -> List[str]:
    codes: List[str] = []
    seen: set[str] = set()

    def visit(value: Any):
        if value is None:
            return
        if isinstance(value, dict):
            for key in ("code", "symbol", "stock_code"):
                if value.get(key) is not None:
                    visit(value.get(key))
            for key in ("codes", "symbols", "stock_codes", "target_symbols"):
                if value.get(key) is not None:
                    visit(value.get(key))
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)
            return
        raw = str(value or "").strip()
        if not raw:
            return
        if any(sep in raw for sep in [",", ";", "|", "\n", "\t", " "]):
            normalized = raw.replace(";", ",").replace("|", ",").replace("\n", ",").replace("\t", ",").replace(" ", ",")
            for part in normalized.split(","):
                visit(part)
            return
        if _is_index_reference_code(raw) and "." in raw:
            number, market = raw.lower().rsplit(".", 1)
            code = market + number
        else:
            code = raw.split(".")[0].strip()
        if not code or code in seen:
            return
        seen.add(code)
        codes.append(code)

    visit(values)
    return codes[: max(1, min(int(limit or 12), 40))]


def _normalize_string_list(values: Any, limit: int = 12) -> List[str]:
    items: List[str] = []
    seen: set[str] = set()

    def visit(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)
            return
        raw = str(value or "").strip()
        if not raw:
            return
        if any(sep in raw for sep in [",", ";", "|", "\n", "\t"]):
            normalized = raw.replace(";", ",").replace("|", ",").replace("\n", ",").replace("\t", ",")
            for part in normalized.split(","):
                visit(part)
            return
        if raw in seen:
            return
        seen.add(raw)
        items.append(raw)

    visit(values)
    return items[: max(1, min(int(limit or 12), 40))]


def _normalize_lower_string_list(values: Any, limit: int = 12) -> List[str]:
    return [
        str(item).strip().lower()
        for item in _normalize_string_list(values, limit=limit)
        if str(item).strip()
    ]


def _candidate_generation_mode(candidate: Optional[dict]) -> str:
    payload = dict(candidate or {})
    for source in (
        payload,
        dict(payload.get("params") or {}),
        dict(payload.get("candidate_provenance") or {}),
        dict(payload.get("generation_reason") or {}),
        dict(payload.get("research_task") or {}),
    ):
        if not isinstance(source, dict):
            continue
        for key in ("generator_type", "generator_mode"):
            value = str(source.get(key) or "").strip().lower()
            if value:
                return value
    return ""


def _build_target_alignment_contract(
    research_task: Optional[dict],
    *,
    candidate: Optional[dict] = None,
) -> dict[str, Any]:
    task = dict(research_task or {})
    task_source = str(task.get("task_source") or "snapshot").strip().lower() or "snapshot"
    target_symbols = _normalize_target_codes(
        [
            task.get("target_symbols"),
            task.get("stock_pool"),
            (task.get("event_context") or {}).get("target_symbols"),
        ],
        limit=12,
    )
    target_count = len(target_symbols)
    targeted_snapshot = task_source == "snapshot" and target_count > 0
    event_targeted = task_source == "event_driven" and target_count > 0

    family_hints = set(
        _normalize_lower_string_list(
            [
                task.get("allowed_strategy_types"),
                task.get("preferred_strategy_types"),
                task.get("strategy_preferences"),
            ],
            limit=12,
        )
    )
    payload = dict(candidate or {})
    candidate_strategy_type = str(
        payload.get("strategy_type")
        or payload.get("candidate_family")
        or task.get("candidate_family")
        or ""
    ).strip().lower()
    if candidate_strategy_type:
        family_hints.add(candidate_strategy_type)
    generator_mode = _candidate_generation_mode(payload) or str(
        task.get("generator_type") or task.get("generator_mode") or ""
    ).strip().lower()
    candidate_tags = {
        str(tag).strip().lower()
        for tag in list(payload.get("tags") or [])
        if str(tag).strip()
    }

    single_family_hint = next(iter(family_hints)) if len(family_hints) == 1 else ""
    strategy_hint = candidate_strategy_type or single_family_hint
    is_pipeline_staged = (
        generator_mode == "pipeline_staged"
        or "pipeline_staged" in candidate_tags
        or "generator_pipeline_staged" in candidate_tags
    )
    is_rl_bandit = (
        generator_mode == "rl_bandit"
        or "rl_bandit" in candidate_tags
        or "generator_rl_bandit" in candidate_tags
        or "rl_evolved" in candidate_tags
    )
    pipeline_rsi = targeted_snapshot and strategy_hint == "rsi" and (is_pipeline_staged or not payload)
    pipeline_ma_cross = targeted_snapshot and strategy_hint == "ma_cross" and (is_pipeline_staged or not payload)
    rl_bandit_momentum = targeted_snapshot and strategy_hint == "momentum" and is_rl_bandit
    rl_bandit_volatility_breakout = targeted_snapshot and strategy_hint == "volatility_breakout" and is_rl_bandit

    min_coverage_ratio = 0.0
    min_intersection_ratio = 0.0
    min_target_layer_stability = 0.0
    strict_target_subset_required = False
    reject_market_fallback = False
    default_target_cap = min(target_count, 8) if target_count > 0 else 0
    contract_profile = "untargeted"

    if event_targeted:
        strict_target_subset_required = True
        reject_market_fallback = False
        min_coverage_ratio = 1.0
        min_intersection_ratio = 0.5 if target_count > 1 else 1.0
        min_target_layer_stability = 0.4
        default_target_cap = min(target_count, 8)
        contract_profile = "event_targeted"
    elif targeted_snapshot:
        strict_target_subset_required = True
        reject_market_fallback = True
        min_coverage_ratio = 0.75
        min_intersection_ratio = 0.25
        min_target_layer_stability = 0.35
        default_target_cap = min(target_count, 4 if target_count >= 4 else target_count)
        contract_profile = "snapshot_targeted"

        if pipeline_ma_cross:
            min_intersection_ratio = 0.35 if target_count >= 4 else min_intersection_ratio
            min_target_layer_stability = 0.42
            default_target_cap = min(target_count, 4 if target_count >= 4 else target_count)
            contract_profile = "pipeline_staged_ma_cross"
        if pipeline_rsi:
            min_intersection_ratio = 0.5 if target_count >= 6 else max(min_intersection_ratio, 0.35)
            min_target_layer_stability = 0.55
            default_target_cap = min(target_count, 4 if target_count >= 4 else target_count)
            contract_profile = "pipeline_staged_rsi"
        if rl_bandit_momentum:
            min_coverage_ratio = 0.85
            min_intersection_ratio = 0.45 if target_count >= 6 else max(min_intersection_ratio, 0.35)
            min_target_layer_stability = 0.5
            default_target_cap = min(target_count, 6 if target_count >= 6 else target_count)
            contract_profile = "rl_bandit_momentum"
        if rl_bandit_volatility_breakout:
            min_coverage_ratio = 0.85
            min_intersection_ratio = 0.2 if target_count >= 8 else max(min_intersection_ratio, 0.15)
            min_target_layer_stability = 0.45
            default_target_cap = min(target_count, 6 if target_count >= 6 else target_count)
            contract_profile = "rl_bandit_volatility_breakout"

    min_required_overlap_count = (
        max(1, int(math.ceil(target_count * min_intersection_ratio)))
        if target_count > 0 and strict_target_subset_required
        else (1 if target_count > 0 and targeted_snapshot else 0)
    )
    min_target_sample_count = min(
        target_count,
        max(1, min_required_overlap_count),
    ) if target_count > 0 and targeted_snapshot else (1 if event_targeted and target_count > 0 else 0)
    max_candidate_target_symbols = (
        min(target_count, max(default_target_cap, min_required_overlap_count))
        if target_count > 0
        else 0
    )

    return {
        "profile": contract_profile,
        "task_source": task_source,
        "targeted_snapshot": targeted_snapshot,
        "event_targeted": event_targeted,
        "quality_gate_enabled": targeted_snapshot or event_targeted,
        "strict_target_subset_required": strict_target_subset_required,
        "market_fallback_allowed": not reject_market_fallback,
        "min_coverage_ratio": round(min_coverage_ratio, 4),
        "min_intersection_ratio": round(min_intersection_ratio, 4),
        "min_required_overlap_count": int(min_required_overlap_count),
        "min_target_sample_count": int(min_target_sample_count),
        "min_target_layer_stability": round(min_target_layer_stability, 4),
        "max_candidate_target_symbols": int(max_candidate_target_symbols),
        "focus_strategy_families": sorted(family_hints)[:6],
        "generator_mode": generator_mode or None,
    }


def _task_default_target_symbol_policy(task_source: str) -> str:
    return "strict_intersection" if task_source == "event_driven" else "prefer_intersection"


def _task_default_universe_expansion_policy(task_source: str) -> str:
    return "allow_same_theme_only" if task_source == "event_driven" else "allow_market_fallback"


def _task_default_validation_focus(task_source: str) -> str:
    return "event_target_only" if task_source == "event_driven" else "target_plus_representative"

# --- targets_parts/selection.py ---
def _task_default_preference_strength(task_source: str) -> str:
    return "medium" if task_source == "event_driven" else "soft"


def _task_default_preference_reason(task_source: str, preferred_strategy_types: list[str]) -> str:
    if task_source == "event_driven":
        if preferred_strategy_types:
            return f"event_theme_bias:{','.join(preferred_strategy_types[:3])}"
        return "event_theme_bias"
    if preferred_strategy_types:
        return f"snapshot_regime_bias:{','.join(preferred_strategy_types[:3])}"
    return "snapshot_regime_bias"


def _normalize_event_window_config(task: Optional[dict]) -> dict[str, Any]:
    payload = dict(task or {})
    event_window = dict(payload.get("event_window") or {})
    estimation_window = dict(payload.get("estimation_window") or {})
    holding_window = dict(payload.get("holding_window") or {})
    horizon = str(payload.get("horizon") or "").strip().lower()
    primary_family = _primary_strategy_family(payload)
    task_source = str(payload.get("task_source") or "snapshot").strip().lower() or "snapshot"

    if not event_window:
        if task_source == "event_driven":
            event_window = {"pre_days": 1, "post_days": 10}
        else:
            event_window = {"pre_days": 0, "post_days": 20}
    if not estimation_window:
        estimation_window = {"lookback_days": 60}
    if not holding_window:
        holding_window = _default_holding_window_for_family(
            primary_family,
            task_source=task_source,
            horizon=horizon,
        )

    return {
        "event_window": event_window,
        "estimation_window": estimation_window,
        "holding_window": holding_window,
    }


def _compact_task_metadata_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        preview = [
            item
            for item in value[:8]
            if isinstance(item, (str, int, float, bool)) or item is None
        ]
        return preview or None
    if isinstance(value, dict):
        compact = {
            str(key): item
            for key, item in list(value.items())[:12]
            if isinstance(item, (str, int, float, bool)) or item is None
        }
        if compact:
            return compact
    return None


def _summarize_factor_research_metadata(factor_research: Optional[dict]) -> dict[str, Any]:
    payload = dict(factor_research or {})
    summary = dict(payload.get("summary") or {})
    freshness_repair = dict(payload.get("freshness_repair") or {})
    compact = {
        "top_factor_names": list(summary.get("top_factor_names") or payload.get("active_factors") or [])[:6],
        "preferred_strategy_types": _normalize_string_list(payload.get("preferred_strategy_types"), limit=6),
        "degraded": bool(payload.get("degraded")),
    }
    for key in (
        "active_candidate_count",
        "candidate_pool_size",
        "registry_size",
        "freshness_days",
        "refresh_status",
    ):
        value = summary.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    if freshness_repair:
        compact["freshness_repair"] = {
            key: freshness_repair.get(key)
            for key in (
                "refresh_attempted",
                "refresh_status",
                "refresh_trigger",
                "fallback_reason",
                "stale_days",
            )
            if freshness_repair.get(key) not in (None, "", [], {})
        }
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def _compact_research_task_metadata(metadata: Optional[dict]) -> dict[str, Any]:
    payload = dict(metadata or {})
    compact: dict[str, Any] = {}
    factor_research = _summarize_factor_research_metadata(payload.get("factor_research"))
    if factor_research:
        compact["factor_research"] = factor_research
    for key, value in payload.items():
        if key == "factor_research":
            continue
        compact_value = _compact_task_metadata_value(value)
        if compact_value not in (None, "", [], {}):
            compact[str(key)] = compact_value
    return compact


def _normalize_research_task_contract(task: Optional[dict]) -> dict[str, Any]:
    payload = dict(task or {})
    task_source = str(payload.get("task_source") or "snapshot").strip().lower() or "snapshot"
    compact_metadata = _compact_research_task_metadata(payload.get("metadata") or {})
    if compact_metadata:
        payload = {**payload, "metadata": compact_metadata}
    elif "metadata" in payload:
        payload = {key: value for key, value in payload.items() if key != "metadata"}

    preferred_strategy_types = _normalize_string_list(
        payload.get("preferred_strategy_types") or payload.get("strategy_preferences"),
        limit=8,
    )
    allowed_strategy_types = _normalize_string_list(payload.get("allowed_strategy_types"), limit=12)
    target_symbols = _normalize_target_codes(
        [
            payload.get("target_symbols"),
            payload.get("stock_pool"),
            (payload.get("event_context") or {}).get("target_symbols"),
        ],
        limit=12,
    )
    stock_pool = dict(payload.get("stock_pool") or {})
    if target_symbols and not stock_pool:
        stock_pool = {"selection_mode": "explicit", "symbols": list(target_symbols)}
    targeted_snapshot = task_source == "snapshot" and len(target_symbols) > 0
    event_targeted = task_source == "event_driven" and len(target_symbols) > 0

    target_symbol_policy_explicit = payload.get("target_symbol_policy") is not None
    universe_expansion_policy_explicit = payload.get("universe_expansion_policy") is not None
    target_symbol_policy = str(
        payload.get("target_symbol_policy")
        or _task_default_target_symbol_policy(task_source)
    ).strip().lower() or _task_default_target_symbol_policy(task_source)
    universe_expansion_policy = str(
        payload.get("universe_expansion_policy")
        or _task_default_universe_expansion_policy(task_source)
    ).strip().lower() or _task_default_universe_expansion_policy(task_source)
    if targeted_snapshot and not target_symbol_policy_explicit:
        target_symbol_policy = "strict_intersection"
    if targeted_snapshot and not universe_expansion_policy_explicit:
        universe_expansion_policy = "forbid"
    if event_targeted and not universe_expansion_policy_explicit:
        universe_expansion_policy = "allow_same_theme_only"
    preference_strength = str(
        payload.get("preference_strength")
        or _task_default_preference_strength(task_source)
    ).strip().lower() or _task_default_preference_strength(task_source)
    preference_reason = str(
        payload.get("preference_reason")
        or _task_default_preference_reason(task_source, preferred_strategy_types)
    ).strip() or _task_default_preference_reason(task_source, preferred_strategy_types)
    validation_focus_explicit = payload.get("validation_focus") is not None
    validation_focus = str(
        payload.get("validation_focus")
        or _task_default_validation_focus(task_source)
    ).strip().lower() or _task_default_validation_focus(task_source)
    primary_family = _primary_strategy_family(
        {
            **payload,
            "preferred_strategy_types": preferred_strategy_types,
            "allowed_strategy_types": allowed_strategy_types,
            "strategy_preferences": preferred_strategy_types,
        }
    )
    if (
        primary_family == "quality_factor"
        and not validation_focus_explicit
        and task_source != "event_driven"
        and validation_focus == "target_plus_representative"
    ):
        validation_focus = "candidate_target_only"
    validation_profile = dict(payload.get("validation_profile") or {})
    if not validation_profile:
        validation_profile = _default_validation_profile_for_task(
            family=primary_family,
            task_source=task_source,
            validation_focus=validation_focus,
        )
    else:
        merged_profile = _default_validation_profile_for_task(
            family=primary_family,
            task_source=task_source,
            validation_focus=str(validation_profile.get("validation_focus") or validation_focus),
        )
        validation_profile = {
            **merged_profile,
            **validation_profile,
        }
        if (
            primary_family == "quality_factor"
            and str(validation_profile.get("validation_focus") or validation_focus).strip().lower()
            in {"candidate_target_only", "target_only", "target_plus_family_peer"}
        ):
            validation_profile["profile"] = "trade_rule_validation"
    target_alignment_contract = _build_target_alignment_contract(
        {
            **payload,
            "task_source": task_source,
            "preferred_strategy_types": preferred_strategy_types,
            "allowed_strategy_types": allowed_strategy_types,
            "strategy_preferences": preferred_strategy_types,
            "target_symbols": target_symbols,
            "stock_pool": stock_pool,
        }
    )

    event_windows = _normalize_event_window_config({**payload, "task_source": task_source, "horizon": payload.get("horizon")})
    task_signature = _build_task_signature({
        **payload,
        "task_source": task_source,
        "target_symbols": target_symbols,
        "validation_focus": validation_focus,
    })

    return {
        **payload,
        "task_source": task_source,
        "preferred_strategy_types": preferred_strategy_types,
        "allowed_strategy_types": allowed_strategy_types,
        "strategy_preferences": list(preferred_strategy_types),
        "target_symbols": target_symbols,
        "stock_pool": stock_pool,
        "target_symbol_policy": target_symbol_policy,
        "target_symbol_policy_explicit": target_symbol_policy_explicit,
        "universe_expansion_policy": universe_expansion_policy,
        "universe_expansion_policy_explicit": universe_expansion_policy_explicit,
        "preference_strength": preference_strength,
        "preference_reason": preference_reason,
        "validation_focus": validation_focus,
        "validation_profile": validation_profile,
        "target_alignment_contract": target_alignment_contract,
        **event_windows,
        "task_signature": task_signature,
        "target_symbols_signature": ",".join(sorted(target_symbols)),
    }


def _normalize_strategy_type_preferences(task: Optional[dict]) -> List[str]:
    return list(_normalize_research_task_contract(task).get("preferred_strategy_types") or [])


def _build_task_signature(payload: Optional[dict]) -> str:
    item = dict(payload or {})
    target_symbols = _normalize_target_codes(
        [
            item.get("target_symbols"),
            item.get("stock_pool"),
            (item.get("research_task") or {}).get("target_symbols"),
            (item.get("event_context") or {}).get("target_symbols"),
        ],
        limit=16,
    )
    parts = [
        str(item.get("task_source") or "").strip().lower(),
        str(item.get("event_id") or (item.get("event_context") or {}).get("event_id") or "").strip().lower(),
        str(item.get("theme_code") or (item.get("event_context") or {}).get("theme_code") or "").strip().lower(),
        str(item.get("direction") or (item.get("event_context") or {}).get("direction") or "").strip().lower(),
        str(item.get("validation_focus") or "").strip().lower(),
        ",".join(sorted(target_symbols)),
    ]
    return "|".join(parts)


def _apply_target_symbol_policy(
    candidate_symbols: Any,
    research_task: Optional[dict],
    *,
    fallback_symbols: Any = None,
    limit: int = 8,
) -> dict[str, Any]:
    task = _normalize_research_task_contract(research_task)
    target_alignment_contract = dict(task.get("target_alignment_contract") or {})
    research_symbols = list(task.get("target_symbols") or [])
    candidate_codes = _normalize_target_codes(candidate_symbols, limit=limit)
    fallback_codes = _normalize_target_codes(fallback_symbols, limit=limit)
    same_theme_codes = _normalize_target_codes(
        [
            task.get("same_theme_symbols"),
            task.get("theme_members"),
            (task.get("event_context") or {}).get("same_theme_symbols"),
            (task.get("event_context") or {}).get("theme_members"),
        ],
        limit=limit,
    )
    candidate_before = list(candidate_codes)
    intersection = [code for code in candidate_codes if code in set(research_symbols)]
    policy = str(task.get("target_symbol_policy") or "prefer_intersection").strip().lower()
    expansion_policy = str(task.get("universe_expansion_policy") or "allow_market_fallback").strip().lower()
    if (
        research_symbols
        and target_alignment_contract.get("strict_target_subset_required")
        and not task.get("target_symbol_policy_explicit")
        and not task.get("universe_expansion_policy_explicit")
        and not target_alignment_contract.get("market_fallback_allowed", True)
    ):
        expansion_policy = "forbid"

    resolved = list(candidate_codes)
    expansion_applied = False
    expansion_reason = ""
    expansion_source = ""
    violation = ""
    blocked_reason = ""

    same_theme_set = set(same_theme_codes)

    def _resolve_same_theme_subset() -> list[str]:
        same_theme_candidate = [code for code in candidate_before if code in same_theme_set]
        if same_theme_candidate:
            return same_theme_candidate
        return [code for code in fallback_codes if code in same_theme_set]

    if research_symbols:
        if policy == "strict_intersection":
            resolved = list(intersection)
            if candidate_before and resolved and set(candidate_before) != set(resolved):
                expansion_applied = True
                expansion_reason = "strict_intersection_trimmed"
                expansion_source = "research_task.target_symbols"
            if not resolved and expansion_policy == "allow_same_theme_only":
                same_theme_subset = _resolve_same_theme_subset()
                if same_theme_subset:
                    resolved = list(same_theme_subset)
                    expansion_applied = True
                    expansion_reason = "fallback_same_theme_symbols"
                    expansion_source = "same_theme_symbols"
                else:
                    blocked_reason = "same_theme_symbols_unavailable"
            if not resolved:
                violation = "strict_intersection_empty"
        elif policy == "prefer_intersection":
            if intersection:
                resolved = list(intersection)
                if set(candidate_before) != set(resolved):
                    expansion_applied = True
                    expansion_reason = "prefer_intersection_trimmed"
                    expansion_source = "research_task.target_symbols"
            elif expansion_policy == "forbid":
                resolved = []
                violation = "expansion_forbidden"
            elif expansion_policy == "allow_same_theme_only":
                same_theme_subset = _resolve_same_theme_subset()
                if same_theme_subset:
                    resolved = list(same_theme_subset)
                    expansion_applied = True
                    expansion_reason = "fallback_same_theme_symbols"
                    expansion_source = "same_theme_symbols"
                else:
                    resolved = []
                    violation = "same_theme_expansion_empty"
                    blocked_reason = "same_theme_symbols_unavailable"
            elif candidate_before:
                resolved = list(candidate_before)
                expansion_applied = True
                expansion_reason = "candidate_retained_without_intersection"
                expansion_source = "candidate_symbols"
            elif fallback_codes:
                resolved = list(fallback_codes)
                expansion_applied = True
                expansion_reason = "fallback_candidate_universe"
                expansion_source = "candidate_universe"
            else:
                resolved = list(research_symbols[:limit])
                expansion_applied = True
                expansion_reason = "fallback_research_symbols"
                expansion_source = "research_task.target_symbols"
        else:
            if candidate_before:
                resolved = list(candidate_before)
            elif fallback_codes:
                resolved = list(fallback_codes)
                expansion_applied = True
                expansion_reason = "fallback_candidate_universe"
                expansion_source = "candidate_universe"
            else:
                resolved = list(research_symbols[:limit])
                expansion_applied = True
                expansion_reason = "fallback_research_symbols"
                expansion_source = "research_task.target_symbols"
    elif not resolved and fallback_codes:
        resolved = list(fallback_codes)
        expansion_applied = True
        expansion_reason = "fallback_candidate_universe"
        expansion_source = "candidate_universe"

    resolved_limit = max(1, min(int(limit or 8), 40))
    contract_target_cap = int(target_alignment_contract.get("max_candidate_target_symbols") or 0)
    if contract_target_cap > 0:
        resolved_limit = min(resolved_limit, contract_target_cap)
    resolved = resolved[:resolved_limit]
    overlap_count = len(set(resolved).intersection(research_symbols))
    coverage_ratio = round(overlap_count / max(1, len(resolved)), 4) if resolved else 0.0
    intersection_ratio = round(overlap_count / max(1, len(research_symbols)), 4) if research_symbols else None
    alignment_violation = None
    min_coverage_ratio = float(target_alignment_contract.get("min_coverage_ratio") or 0.0)
    min_intersection_ratio = (
        None
        if target_alignment_contract.get("min_intersection_ratio") is None
        else float(target_alignment_contract.get("min_intersection_ratio") or 0.0)
    )
    min_required_overlap_count = int(target_alignment_contract.get("min_required_overlap_count") or 0)
    alignment_ok = True
    if target_alignment_contract.get("quality_gate_enabled"):
        if resolved_limit > 0 and len(candidate_before) > resolved_limit:
            expansion_applied = True
            expansion_reason = expansion_reason or "target_count_trimmed_by_contract"
            expansion_source = expansion_source or "target_alignment_contract"
        if not resolved and research_symbols:
            alignment_ok = False
            alignment_violation = "empty_target_symbols_after_alignment"
        elif coverage_ratio < min_coverage_ratio:
            alignment_ok = False
            alignment_violation = "coverage_ratio_below_contract"
        elif min_intersection_ratio is not None and (intersection_ratio or 0.0) < min_intersection_ratio:
            alignment_ok = False
            alignment_violation = "intersection_ratio_below_contract"
        elif min_required_overlap_count > 0 and overlap_count < min_required_overlap_count:
            alignment_ok = False
            alignment_violation = "target_overlap_count_below_contract"

    return {
        "target_symbols": resolved,
        "constraint_check": {
            "target_symbols_before_normalize": candidate_before,
            "target_symbols_after_normalize": list(resolved),
            "research_target_symbols": list(research_symbols),
            "same_theme_symbols": list(same_theme_codes),
            "target_symbol_policy": policy,
            "universe_expansion_policy": expansion_policy,
            "expansion_applied": expansion_applied,
            "expansion_reason": expansion_reason or None,
            "expansion_source": expansion_source or None,
            "constraint_violation": violation or None,
            "expansion_blocked_reason": blocked_reason or None,
            "coverage_ratio": coverage_ratio,
            "intersection_ratio": intersection_ratio,
            "target_overlap_count": int(overlap_count),
            "alignment_contract_ok": alignment_ok,
            "alignment_contract_violation": alignment_violation,
            "target_alignment_contract": dict(target_alignment_contract),
        },
    }


def _extract_candidate_origin_target_codes(payload: Optional[dict], limit: int = 12) -> List[str]:
    item = dict(payload or {})
    params = dict(item.get("params") or {})
    dsl = dict(params.get("dsl") or {})
    dsl_metadata = dict(dsl.get("metadata") or {})
    generation_reason = dict(item.get("generation_reason") or {})
    candidate_provenance = dict(
        item.get("candidate_provenance")
        or params.get("candidate_provenance")
        or {}
    )
    item_event_context = dict(item.get("event_context") or {})
    return _normalize_target_codes([
        item.get("target_symbols"),
        item.get("stock_pool"),
        item_event_context.get("target_symbols"),
        item_event_context.get("stock_pool"),
        params.get("target_symbols"),
        params.get("stock_pool"),
        params.get("event_context"),
        dsl_metadata.get("target_symbols"),
        dsl_metadata.get("stock_pool"),
        generation_reason.get("target_symbols"),
        generation_reason.get("stock_pool"),
        candidate_provenance.get("target_symbols"),
        candidate_provenance.get("stock_pool"),
    ], limit=limit)


def _extract_target_codes_from_payload(payload: Optional[dict], limit: int = 12) -> List[str]:
    item = dict(payload or {})
    candidate_codes = _extract_candidate_origin_target_codes(item, limit=limit)
    if candidate_codes:
        return list(candidate_codes)
    params = dict(item.get("params") or {})
    research_task = dict(item.get("research_task") or {})
    task_event_context = dict(research_task.get("event_context") or {})
    params_research_task = dict(params.get("research_task") or {})
    params_task_event_context = dict(params_research_task.get("event_context") or {})
    return _normalize_target_codes([
        research_task.get("target_symbols"),
        research_task.get("stock_pool"),
        task_event_context.get("target_symbols"),
        task_event_context.get("stock_pool"),
        params_research_task.get("target_symbols"),
        params_research_task.get("stock_pool"),
        params_task_event_context.get("target_symbols"),
        params_task_event_context.get("stock_pool"),
    ], limit=limit)


def _resolve_strategy_sample_codes(strategy_type: str, params: dict, sample_size: int = 6) -> List[str]:
    selection = _resolve_strategy_sample_selection(
        strategy_type,
        params,
        sample_size=sample_size,
    )
    return list(selection.get("sample_codes") or [])


__all__ = [
    "_update_strategy_status",
    "_normalize_target_codes",
    "_normalize_string_list",
    "_normalize_research_task_contract",
    "_normalize_strategy_type_preferences",
    "_build_target_alignment_contract",
    "_apply_target_symbol_policy",
    "_build_task_signature",
    "_extract_candidate_origin_target_codes",
    "_extract_target_codes_from_payload",
    "_resolve_strategy_sample_codes",
]

