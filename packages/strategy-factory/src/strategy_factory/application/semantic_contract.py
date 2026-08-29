"""Semantic contract helpers for high-confidence candidate auditing."""
from __future__ import annotations

# --- semantic_contract_parts/normalizers.py ---
import os
from datetime import date, datetime, time, timezone
from typing import Any, Mapping, Optional

_EMPTY_VALUES = (None, "", [], {})
_SUPPORTED_DSL_FIELDS = {"open", "high", "low", "close", "volume"}
_SUPPORTED_DSL_INDICATORS = {
    "sma",
    "ema",
    "roc",
    "rsi",
    "stddev",
    "zscore",
    "highest",
    "lowest",
    "volume_ratio",
    "atr",
    "adx",
    "turnover_rate",
    "upper_shadow_ratio",
    "rolling_count",
    "slope",
}
_SUPPORTED_DSL_COMPARE_OPS = {
    "gt",
    "gte",
    "lt",
    "lte",
    "eq",
    "ne",
    "cross_above",
    "cross_below",
}
_SUPPORTED_DSL_BINARY_OPS = {"add", "sub", "mul", "div", "max", "min"}
_CONFIDENCE_CONTRACT_VERSION = "p2-stable/v1"
# 模板主导度阈值：default/backfill 兜底证据链里,只要存在任何模板兜底成分
# (template_dominance_score > 0),该 evidence 即标 proxy_only=True / source_type=template_fallback。
# 历史 bug:旧阈值 >=0.999 把 score∈[0,0.999) 的候选(含 score=0 的纯模板兜底、无真实 pack 者)
# 错标成 proxy_only=False/price_volume_confirmation,伪装真实价量证据。与 policy_contract.py
# 的 template_fallback_used(>0) 及 market_evidence.py 的 template_fallback_used(>0) 口径对齐。
_TEMPLATE_FALLBACK_DOMINANCE_THRESHOLD = 0.0
_TREND_EXECUTABLE_DSL_TYPES = {"ma_cross", "momentum", "volatility_breakout", "event_structure_breakout"}
_AMBIGUOUS_REGIME_TOKENS = {
    "明显震荡",
    "震荡",
    "高波动",
    "低波动",
    "趋势良好",
    "趋势较强",
    "市场不好",
    "市场较差",
    "风险较高",
    "风险较低",
    "high noise",
    "range bound",
    "chop",
    "noisy",
    "volatile",
}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in _EMPTY_VALUES:
            return value
    return None


def _dedup_strings(values: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = _string(value)
        if not token or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def _string_list(value: Any) -> list[str]:
    return [token for token in (_string(item) for item in _as_list(value)) if token]


def _normalized_source_type(value: Any) -> Optional[str]:
    token = _string(value).lower()
    return token or None


def _normalize_conflict_resolution_rule(value: Any) -> Optional[Any]:
    if isinstance(value, Mapping):
        payload = {
            key: item
            for key, item in dict(value).items()
            if item not in _EMPTY_VALUES
        }
        return payload or None
    token = _string(value)
    return token or None


def _candidate_value(candidate: Optional[Mapping[str, Any]], key: str) -> Any:
    payload = dict(candidate or {})
    params = _as_dict(payload.get("params"))
    if key in payload and payload.get(key) not in _EMPTY_VALUES:
        return payload.get(key)
    if key in params and params.get(key) not in _EMPTY_VALUES:
        return params.get(key)
    return None


def _normalized_strategy_type(candidate: Optional[Mapping[str, Any]]) -> Optional[str]:
    token = _string(_candidate_value(candidate, "strategy_type") or dict(candidate or {}).get("strategy_type")).lower()
    return token or None


def _coerce_iso_ts(value: Any) -> Optional[str]:
    if value in _EMPTY_VALUES:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    else:
        text = _string(value)
        if not text:
            return None
        for parser in (
            lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")),
            lambda item: datetime.combine(date.fromisoformat(item[:10]), time.min),
        ):
            try:
                dt = parser(text)
                break
            except Exception:
                dt = None
        if dt is None:
            digits = "".join(ch for ch in text if ch.isdigit())
            if len(digits) >= 8:
                try:
                    dt = datetime(
                        int(digits[:4]),
                        int(digits[4:6]),
                        int(digits[6:8]),
                        tzinfo=timezone.utc,
                    )
                except Exception:
                    return None
            else:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _coerce_signal_ts(value: Any) -> Optional[str]:
    return _coerce_iso_ts(value)


def _quality_band(
    *,
    support_samples: int,
    brier_score: Optional[float],
    ece: Optional[float],
) -> str:
    if support_samples <= 0 and brier_score is None and ece is None:
        return "unknown"
    score = 0
    if support_samples >= 100:
        score += 2
    elif support_samples >= 50:
        score += 1
    if brier_score is not None:
        if brier_score <= 0.10:
            score += 2
        elif brier_score <= 0.20:
            score += 1
    if ece is not None:
        if ece <= 0.04:
            score += 2
        elif ece <= 0.08:
            score += 1
    if score >= 5:
        return "good"
    if score >= 3:
        return "fair"
    if score >= 1:
        return "medium"
    return "unknown"


def _collect_probability_payloads(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(candidate or {})
    evidence_chain = _as_dict(_candidate_value(payload, "evidence_chain"))
    prediction_contract = _as_dict(_candidate_value(payload, "prediction_contract"))
    confidence_contract = _as_dict(_candidate_value(payload, "confidence_contract"))
    prediction_quality = _as_dict(
        confidence_contract.get("prediction_quality")
        or confidence_contract.get("probability_quality")
    )
    prediction_interval = _as_dict(confidence_contract.get("prediction_interval"))
    claims = _normalize_claims(prediction_contract)
    evidences = _normalize_evidences(evidence_chain)
    raw_probs: list[float] = []
    calibrated_probs: list[float] = []
    support_samples: list[int] = []
    ece = _safe_float(
        _first_non_empty(
            prediction_quality.get("ece"),
            confidence_contract.get("ece"),
        )
    )
    brier_score = _safe_float(
        _first_non_empty(
            prediction_quality.get("brier_score"),
            confidence_contract.get("brier_score"),
        )
    )
    calibration_gap = _safe_float(
        _first_non_empty(
            prediction_quality.get("calibration_gap"),
            confidence_contract.get("calibration_gap"),
        )
    )

    def _append_probability(
        raw_value: Any,
        calibrated_value: Any,
        sample_size_value: Any,
    ) -> None:
        raw = _safe_float(raw_value)
        calibrated = _safe_float(calibrated_value)
        sample_size = _safe_int(sample_size_value)
        if raw is not None:
            raw_probs.append(raw)
        if calibrated is not None:
            calibrated_probs.append(calibrated)
        if sample_size > 0:
            support_samples.append(sample_size)

    _append_probability(
        _first_non_empty(
            prediction_quality.get("raw_probability"),
            confidence_contract.get("raw_probability"),
            confidence_contract.get("probability"),
        ),
        _first_non_empty(
            prediction_quality.get("calibrated_probability"),
            confidence_contract.get("calibrated_probability"),
        ),
        _first_non_empty(
            prediction_quality.get("support_samples"),
            prediction_quality.get("sample_size"),
            confidence_contract.get("support_samples"),
            confidence_contract.get("sample_size"),
        ),
    )

    for claim in claims:
        _append_probability(
            _first_non_empty(
                claim.get("raw_probability"),
                claim.get("probability"),
                claim.get("confidence"),
                claim.get("expected_probability"),
            ),
            _first_non_empty(
                claim.get("calibrated_probability"),
                claim.get("calibrated_confidence"),
            ),
            _first_non_empty(
                claim.get("support_samples"),
                claim.get("sample_size"),
            ),
        )

    for evidence in evidences:
        _append_probability(
            _first_non_empty(
                evidence.get("raw_confidence"),
                evidence.get("raw_probability"),
                evidence.get("confidence"),
            ),
            _first_non_empty(
                evidence.get("calibrated_confidence"),
                evidence.get("calibrated_probability"),
            ),
            _first_non_empty(
                evidence.get("support_samples"),
                evidence.get("sample_size"),
            ),
        )

    return {
        "existing_contract": confidence_contract,
        "prediction_quality": prediction_quality,
        "prediction_interval": prediction_interval,
        "claims": claims,
        "evidences": evidences,
        "raw_probs": raw_probs,
        "calibrated_probs": calibrated_probs,
        "support_samples": support_samples,
        "ece": ece,
        "brier_score": brier_score,
        "calibration_gap": calibration_gap,
    }

# --- semantic_contract_parts/policy.py ---
def synthesize_confidence_contract(
    candidate: Optional[Mapping[str, Any]],
    *,
    signal_quality: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(candidate or {})
    collected = _collect_probability_payloads(payload)
    existing_contract = dict(collected.get("existing_contract") or {})
    prediction_quality_payload = dict(collected.get("prediction_quality") or {})
    prediction_interval_payload = dict(collected.get("prediction_interval") or {})
    raw_probs = list(collected.get("raw_probs") or [])
    calibrated_probs = list(collected.get("calibrated_probs") or [])
    support_samples = max(
        [0, *[int(value) for value in list(collected.get("support_samples") or []) if int(value) > 0]],
        default=0,
    )
    if support_samples <= 0:
        support_samples = _safe_int(dict(signal_quality or {}).get("primary_effective_n"), 0)

    raw_probability = (
        round(sum(raw_probs) / len(raw_probs), 6)
        if raw_probs
        else _safe_float(
            _first_non_empty(
                prediction_quality_payload.get("raw_probability"),
                existing_contract.get("raw_probability"),
                existing_contract.get("probability"),
            )
        )
    )
    calibrated_probability = (
        round(sum(calibrated_probs) / len(calibrated_probs), 6)
        if calibrated_probs
        else _safe_float(
            _first_non_empty(
                prediction_quality_payload.get("calibrated_probability"),
                existing_contract.get("calibrated_probability"),
            )
        )
    )
    calibration_method = _string(
        _first_non_empty(
            prediction_quality_payload.get("calibration_method"),
            existing_contract.get("calibration_method"),
        )
    ) or ("none" if calibrated_probability is None else "system_blend")
    ece = _safe_float(collected.get("ece"))
    brier_score = _safe_float(collected.get("brier_score"))
    calibration_gap = _safe_float(collected.get("calibration_gap"))
    quality = _string(
        _first_non_empty(
            prediction_quality_payload.get("quality"),
            existing_contract.get("quality"),
        )
    ).lower() or _quality_band(
        support_samples=support_samples,
        brier_score=brier_score,
        ece=ece,
    )
    lower = _safe_float(
        _first_non_empty(
            prediction_interval_payload.get("lower"),
            existing_contract.get("lower"),
        )
    )
    upper = _safe_float(
        _first_non_empty(
            prediction_interval_payload.get("upper"),
            existing_contract.get("upper"),
        )
    )
    coverage_target = _safe_float(
        _first_non_empty(
            prediction_interval_payload.get("coverage_target"),
            existing_contract.get("coverage_target"),
        )
    )
    observed_coverage = _safe_float(
        _first_non_empty(
            prediction_interval_payload.get("observed_coverage"),
            existing_contract.get("observed_coverage"),
        )
    )
    coverage_proxy = _safe_float(
        _first_non_empty(
            prediction_interval_payload.get("coverage_proxy"),
            existing_contract.get("coverage_proxy"),
        )
    )
    coverage_gap = _safe_float(
        _first_non_empty(
            prediction_interval_payload.get("coverage_gap"),
            existing_contract.get("coverage_gap"),
        )
    )
    interval_method = _string(
        _first_non_empty(
            prediction_interval_payload.get("method"),
            existing_contract.get("prediction_interval_method"),
            existing_contract.get("interval_method"),
        )
    ) or "system_blend"

    warnings = [
        _string(item)
        for item in list(existing_contract.get("warnings") or [])
        if _string(item)
    ]
    if support_samples <= 0:
        warnings.append("缺少稳定 support_samples，confidence_contract 仅作诊断用途")
    if calibrated_probability is None:
        warnings.append("缺少 calibrated_probability，当前以原始置信度近似")
    if calibration_method in {"none", "raw"}:
        warnings.append("概率未经显式校准，可能存在系统性偏差")
    warnings = list(dict.fromkeys(warnings))

    return {
        "contract_version": _CONFIDENCE_CONTRACT_VERSION,
        "producer": "system",
        "prediction_quality": {
            "raw_probability": raw_probability,
            "calibrated_probability": calibrated_probability,
            "support_samples": support_samples,
            "calibration_method": calibration_method,
            "ece": ece,
            "brier_score": brier_score,
            "calibration_gap": calibration_gap,
            "quality": quality,
            "contract_version": _CONFIDENCE_CONTRACT_VERSION,
            "contract_version_stable": True,
        },
        "prediction_interval": {
            "lower": lower,
            "upper": upper,
            "coverage_target": coverage_target,
            "observed_coverage": observed_coverage,
            "coverage_proxy": coverage_proxy,
            "coverage_gap": coverage_gap,
            "method": interval_method,
        },
        "warnings": warnings,
        "source_inputs": {
            "llm": existing_contract,
            "prediction_contract": {
                "claim_count": len(list(collected.get("claims") or [])),
            },
            "evidence_chain": {
                "evidence_count": len(list(collected.get("evidences") or [])),
            },
            "signal_quality": {
                "primary_effective_n": _safe_int(dict(signal_quality or {}).get("primary_effective_n"), 0),
            },
        },
    }


def normalize_semantic_contract_fields(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(candidate or {})
    evidence_chain = _as_dict(_candidate_value(payload, "evidence_chain"))
    prediction_contract = _as_dict(_candidate_value(payload, "prediction_contract"))
    confidence_contract = synthesize_confidence_contract(payload)
    evidence_alignment_audit = _as_dict(_candidate_value(payload, "evidence_alignment_audit"))
    dsl_support_audit = _as_dict(_candidate_value(payload, "dsl_support_audit"))
    legacy_semantic_contract = _candidate_value(payload, "legacy_semantic_contract")
    contradiction_count = _candidate_value(payload, "contradiction_count")
    proxy_dependency_score = _candidate_value(payload, "proxy_dependency_score")
    return {
        "evidence_chain": evidence_chain,
        "prediction_contract": prediction_contract,
        "confidence_contract": confidence_contract,
        "evidence_alignment_audit": evidence_alignment_audit,
        "dsl_support_audit": dsl_support_audit,
        "legacy_semantic_contract": bool(legacy_semantic_contract) if legacy_semantic_contract is not None else None,
        "contradiction_count": (
            _safe_int(contradiction_count)
            if contradiction_count is not None
            else None
        ),
        "proxy_dependency_score": (
            round(_safe_float(proxy_dependency_score), 4)
            if proxy_dependency_score is not None
            else None
        ),
    }


def _contract_target_symbols(candidate: Mapping[str, Any]) -> list[str]:
    payload = dict(candidate or {})
    params = _as_dict(payload.get("params"))
    research_task = _as_dict(_candidate_value(payload, "research_task"))
    sources = (
        payload.get("target_symbols"),
        params.get("target_symbols"),
        _as_dict(payload.get("stock_pool")).get("codes"),
        _as_dict(params.get("stock_pool")).get("codes"),
        research_task.get("target_symbols"),
    )
    ordered: list[str] = []
    seen: set[str] = set()
    for source in sources:
        values = [source] if isinstance(source, str) else _as_list(source)
        for item in values:
            token = _string(item)
            if not token or token in seen:
                continue
            seen.add(token)
            ordered.append(token)
            if len(ordered) >= 12:
                return ordered
    return ordered


def _semantic_direction_bucket(value: Any) -> Optional[str]:
    token = _string(value).lower().replace("-", "_").replace(" ", "_")
    if not token:
        return None
    if token in {"up", "bullish", "long", "buy", "rise", "rising", "positive"}:
        return "up"
    if token in {"down", "bearish", "short", "sell", "fall", "falling", "negative"}:
        return "down"
    if token in {"neutral", "flat", "sideways", "hold"}:
        return "neutral"
    if any(part in token for part in ("short", "bear", "sell", "risk_off", "negative")):
        return "down"
    if any(part in token for part in ("neutral", "hedge", "sideways")):
        return "neutral"
    if any(part in token for part in ("long", "bull", "buy", "repair", "breakout", "positive", "rebound")):
        return "up"
    return None


def _semantic_contract_direction(candidate: Mapping[str, Any], strategy_type: Optional[str]) -> str:
    params = _as_dict(dict(candidate).get("params"))
    trade_plan = _as_dict(_candidate_value(candidate, "trade_plan"))
    direction_resolution = _as_dict(_candidate_value(candidate, "direction_resolution"))
    alpha_thesis = _as_dict(_candidate_value(candidate, "alpha_thesis"))
    prediction_contract = _as_dict(_candidate_value(candidate, "prediction_contract"))
    claims = _as_list(prediction_contract.get("claims"))
    first_claim = _as_dict(claims[0]) if claims else {}
    return (
        _semantic_direction_bucket(direction_resolution.get("direction"))
        or _semantic_direction_bucket(alpha_thesis.get("direction"))
        or _semantic_direction_bucket(dict(candidate).get("direction"))
        or _semantic_direction_bucket(params.get("direction"))
        or _semantic_direction_bucket(prediction_contract.get("direction"))
        or _semantic_direction_bucket(first_claim.get("direction"))
        or _semantic_direction_bucket(first_claim.get("expected_direction"))
        or _semantic_direction_bucket(first_claim.get("expected_move"))
        or _semantic_direction_bucket(trade_plan.get("direction"))
        or _semantic_direction_bucket(trade_plan.get("entry_bias"))
        or "neutral"
    )


def _semantic_contract_confidence(candidate: Mapping[str, Any], default: float = 0.45) -> float:
    params = _as_dict(dict(candidate).get("params"))
    confidence_calibration = _as_dict(_candidate_value(candidate, "confidence_calibration"))
    alpha_thesis = _as_dict(_candidate_value(candidate, "alpha_thesis"))
    prediction_contract = _as_dict(_candidate_value(candidate, "prediction_contract"))
    claims = _as_list(prediction_contract.get("claims"))
    first_claim = _as_dict(claims[0]) if claims else {}
    return round(
        max(
            0.0,
            min(
                1.0,
                _safe_float(
                    _first_non_empty(
                        confidence_calibration.get("confidence"),
                        alpha_thesis.get("confidence"),
                        dict(candidate).get("confidence"),
                        params.get("confidence"),
                        prediction_contract.get("confidence"),
                        first_claim.get("calibrated_confidence"),
                        first_claim.get("confidence"),
                        default,
                    ),
                    default,
                ),
            ),
        ),
        6,
    )


def _default_expected_move(strategy_type: Optional[str]) -> str:
    return "neutral"


def _default_horizon_days(candidate: Mapping[str, Any]) -> int:
    holding_horizon = _as_dict(_candidate_value(candidate, "holding_horizon"))
    risk_rules = _as_dict(_candidate_value(candidate, "risk_rules"))
    trade_plan = _as_dict(_candidate_value(candidate, "trade_plan"))
    return max(
        1,
        _safe_int(
            _first_non_empty(
                holding_horizon.get("max_days"),
                holding_horizon.get("alpha_half_life"),
                risk_rules.get("max_holding_days"),
                trade_plan.get("max_holding_days"),
            ),
            20,
        ),
    )


def _ensure_trade_plan_nodes(candidate: Mapping[str, Any], strategy_type: str) -> dict[str, Any]:
    trade_plan = _as_dict(_candidate_value(candidate, "trade_plan"))
    if not trade_plan:
        trade_plan = {
            "entry_bias": "signal_confirmed",
            "exit_bias": "signal_invalidated_or_time_stop",
        }

    entry = _as_dict(trade_plan.get("entry"))
    exit_payload = _as_dict(trade_plan.get("exit"))
    if not entry:
        entry = {
            "summary": _string(trade_plan.get("entry_bias")) or f"{strategy_type} entry signal confirmed",
        }
    if not exit_payload:
        exit_payload = {
            "summary": _string(trade_plan.get("exit_bias")) or f"{strategy_type} exit signal or risk stop",
        }
    entry.setdefault("node_id", "entry_step_1")
    entry.setdefault("phase", "entry")
    exit_payload.setdefault("node_id", "exit_step_1")
    exit_payload.setdefault("phase", "exit")
    trade_plan["entry"] = entry
    trade_plan["exit"] = exit_payload
    trade_plan.setdefault("semantic_generation_mode", "factory_semantic_contract_backfill")
    trade_plan.setdefault("strategy_type", strategy_type)
    return trade_plan


def _ensure_dsl_mapping(candidate: Mapping[str, Any], trade_plan: Mapping[str, Any]) -> dict[str, Any]:
    dsl = _as_dict(_candidate_value(candidate, "dsl"))
    if not dsl:
        return {}
    payload = dict(dsl)
    entry = _as_dict(payload.get("entry"))
    exit_payload = _as_dict(payload.get("exit"))
    entry_node_id = _string(_as_dict(trade_plan.get("entry")).get("node_id")) or "entry_step_1"
    exit_node_id = _string(_as_dict(trade_plan.get("exit")).get("node_id")) or "exit_step_1"
    if entry:
        entry.setdefault("trade_plan_node_id", entry_node_id)
        payload["entry"] = entry
    if exit_payload:
        exit_payload.setdefault("trade_plan_node_id", exit_node_id)
        payload["exit"] = exit_payload
    return payload


def _build_default_evidence_chain(
    candidate: Mapping[str, Any],
    *,
    strategy_type: str,
    target_symbols: list[str],
    horizon_days: int,
) -> dict[str, Any]:
    name = _string(_candidate_value(candidate, "name") or dict(candidate).get("name")) or strategy_type
    hypothesis = _string(_candidate_value(candidate, "hypothesis") or dict(candidate).get("hypothesis"))
    description = _string(dict(candidate).get("description"))
    params = _as_dict(dict(candidate).get("params"))
    generation_reason = _as_dict(dict(candidate).get("generation_reason") or params.get("generation_reason"))
    source = _string(
        generation_reason.get("source")
        or dict(candidate).get("generator_type")
        or params.get("generator_type")
        or "strategy_factory"
    ).lower() or "strategy_factory"
    thesis = hypothesis or description or f"{name} candidate generated by Strategy Factory"
    direction = _semantic_contract_direction(candidate, strategy_type)
    confidence = _semantic_contract_confidence(candidate, default=0.45)
    market_evidence_pack = _as_dict(_candidate_value(candidate, "market_evidence_pack"))
    template_dominance_score = _safe_float(market_evidence_pack.get("template_dominance_score"))
    non_proxy_evidence_ratio = _safe_float(market_evidence_pack.get("non_proxy_evidence_ratio"))
    # 诚实判定:这是 default/backfill 兜底构造器,ev_entry_signal 默认就是模板派生的 proxy。
    # 仅当候选带真实 market_evidence_pack 且其中有非模板证据(template_dominance<=阈值
    # 且 non_proxy_evidence_ratio>0)时才标 price_volume_confirmation。
    # 历史 bug:旧阈值 >=0.999 把无 pack(score=0,_safe_float(None)=0.0)与高模板主导
    # (0<score<0.999)的候选都错标成 proxy_only=False,伪装真实价量证据。
    has_real_evidence_backing = (
        bool(market_evidence_pack)
        and template_dominance_score <= _TEMPLATE_FALLBACK_DOMINANCE_THRESHOLD
        and non_proxy_evidence_ratio > 0.0
    )
    is_template_proxy = not has_real_evidence_backing
    return {
        "contract_version": "strategy_factory.semantic_contract.v1",
        "producer": "strategy_factory",
        "generation_mode": "factory_semantic_contract_backfill",
        "thesis": thesis,
        "evidences": [
            {
                "evidence_id": "ev_entry_signal",
                "source_type": "template_fallback" if is_template_proxy else "price_volume_confirmation",
                "direction": direction,
                "summary": thesis,
                "proxy_only": is_template_proxy,
                "raw_confidence": confidence,
                "calibrated_confidence": confidence,
                "target_symbols": list(target_symbols),
                "horizon_days": int(horizon_days),
                "claim_ids": ["claim_entry"],
                "support_metric": {
                    "strategy_type": strategy_type,
                    "source": source,
                    "direction_source": _as_dict(_candidate_value(candidate, "direction_resolution")).get("direction_source"),
                    "template_dominance_score": template_dominance_score,
                },
            },
            {
                "evidence_id": "ev_exit_risk",
                "source_type": "risk_contract",
                "direction": "down",
                "summary": "Exit when the entry thesis is invalidated by risk rules or signal decay.",
                "proxy_only": False,
                "target_symbols": list(target_symbols),
                "horizon_days": max(1, int(horizon_days // 2)),
                "claim_ids": ["claim_exit"],
            },
        ],
    }

# --- semantic_contract_parts/policy_contract.py ---
def _build_default_prediction_contract(
    candidate: Mapping[str, Any],
    *,
    strategy_type: str,
    target_symbols: list[str],
    horizon_days: int,
) -> dict[str, Any]:
    name = _string(_candidate_value(candidate, "name") or dict(candidate).get("name")) or strategy_type
    thesis = _string(_candidate_value(candidate, "hypothesis") or dict(candidate).get("description")) or name
    direction = _semantic_contract_direction(candidate, strategy_type)
    confidence = _semantic_contract_confidence(candidate, default=0.45)
    confidence_calibration = _as_dict(_candidate_value(candidate, "confidence_calibration"))
    direction_resolution = _as_dict(_candidate_value(candidate, "direction_resolution"))
    target = (
        "forward_return_positive"
        if direction == "up"
        else "forward_return_negative"
        if direction == "down"
        else "forward_return_neutral"
    )
    return {
        "contract_version": "strategy_factory.prediction_contract.v1",
        "producer": "strategy_factory",
        "generation_mode": "factory_semantic_contract_backfill",
        "primary_horizon_days": int(horizon_days),
        "target": target,
        "direction": direction,
        "confidence": confidence,
        "direction_source": direction_resolution.get("direction_source") or "semantic_contract_backfill",
        "confidence_source": confidence_calibration.get("confidence_source") or "semantic_contract_backfill",
        "evidence_quality_score": confidence_calibration.get("evidence_quality_score"),
        "conflict_count": direction_resolution.get("conflict_count"),
        "template_fallback_used": _safe_float(_as_dict(_candidate_value(candidate, "market_evidence_pack")).get("template_dominance_score")) > 0,
        "conflict_resolution_rule": {
            "policy": "evidence_vote_with_neutral_on_conflict",
            "tie_breaker": "neutral_when_margin_small",
        },
        "claims": [
            {
                "claim_id": "claim_entry",
                "claim_type": "entry",
                "summary": thesis,
                "expected_move": direction,
                "expected_horizon": int(horizon_days),
                "confidence": confidence,
                "calibrated_confidence": confidence,
                "evidence_ids": ["ev_entry_signal"],
                "failure_condition": "entry thesis invalidated by exit signal, stop loss, or time stop",
                "conflict_resolution_rule": {"policy": "evidence_vote_with_neutral_on_conflict"},
                "target_symbols": list(target_symbols),
            },
            {
                "claim_id": "claim_exit",
                "claim_type": "exit",
                "summary": "Risk or signal invalidation exits the position.",
                "expected_move": "down" if direction == "up" else "up" if direction == "down" else "neutral",
                "expected_horizon": max(1, int(horizon_days // 2)),
                "evidence_ids": ["ev_exit_risk"],
                "failure_condition": "entry thesis restored",
                "conflict_resolution_rule": {"policy": "risk_first"},
                "target_symbols": list(target_symbols),
            },
        ],
    }


def _claim_direction_bucket(value: Any) -> Optional[str]:
    token = _string(value).lower()
    if not token:
        return None
    if any(word in token for word in ("up", "long", "bull", "buy", "rise", "rebound")):
        return "up"
    if any(word in token for word in ("down", "short", "bear", "sell", "fall", "drop")):
        return "down"
    return None


def _claim_type_bucket(claim: Mapping[str, Any]) -> str:
    return _string(claim.get("claim_type") or claim.get("type") or claim.get("phase")).lower()


def _trade_plan_phase_bucket(phase: Optional[str], node: Mapping[str, Any]) -> str:
    text = " ".join(
        [
            _string(phase),
            _string(node.get("phase")),
            _string(node.get("node_id")),
            _string(node.get("summary")),
            _string(node.get("exit_bias")),
            _string(node.get("entry_bias")),
            _string(node.get("condition")),
            _string(node.get("trigger")),
        ]
    ).lower()
    if any(token in text for token in ("exit", "risk", "stop", "invalid", "sell")):
        return "exit"
    return "entry"


def _select_claim_ids_for_trade_plan_node(
    *,
    phase: str,
    claims: list[dict[str, Any]],
    primary_entry_claim_id: str,
) -> list[str]:
    if not claims:
        return []
    if phase == "exit":
        for claim in claims:
            claim_type = _claim_type_bucket(claim)
            if claim_type in {"exit", "risk", "stop", "invalidation"}:
                return [str(claim["claim_id"])]
        for claim in claims:
            if _claim_direction_bucket(claim.get("expected_move")) == "down":
                return [str(claim["claim_id"])]
        return [primary_entry_claim_id] if primary_entry_claim_id else [str(claims[0]["claim_id"])]

    for claim in claims:
        claim_type = _claim_type_bucket(claim)
        if claim_type in {"entry", "signal", "thesis"}:
            return [str(claim["claim_id"])]
    for claim in claims:
        if _claim_direction_bucket(claim.get("expected_move")) == "up":
            return [str(claim["claim_id"])]
    return [primary_entry_claim_id] if primary_entry_claim_id else [str(claims[0]["claim_id"])]


def _claim_evidence_ids(claims_by_id: Mapping[str, Mapping[str, Any]], claim_ids: list[str]) -> list[str]:
    evidence_ids: list[str] = []
    for claim_id in claim_ids:
        evidence_ids.extend(
            _string(item)
            for item in _as_list(_as_dict(claims_by_id.get(claim_id)).get("evidence_ids"))
            if _string(item)
        )
    return _dedup_strings(evidence_ids)


def _trade_plan_claim_ids_replaceable(claim_ids: list[str], claim_id_set: set[str]) -> bool:
    if not claim_ids:
        return True
    placeholder_ids = {"claim_entry", "claim_exit"}
    return all(claim_id in placeholder_ids and claim_id not in claim_id_set for claim_id in claim_ids)


def _backfill_trade_plan_claim_links(
    trade_plan: Mapping[str, Any],
    prediction_contract: Mapping[str, Any],
    existing_claim_to_trade_plan_map: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    claims = _normalize_claims(prediction_contract)
    claim_id_set = {str(claim.get("claim_id")) for claim in claims if claim.get("claim_id")}
    claims_by_id = {str(claim["claim_id"]): claim for claim in claims if claim.get("claim_id")}
    payload = dict(trade_plan or {})
    changed = False
    if not claims:
        return payload, _as_dict(existing_claim_to_trade_plan_map), changed

    primary_entry_claim_id = ""
    for claim in claims:
        if _claim_type_bucket(claim) in {"entry", "signal", "thesis"}:
            primary_entry_claim_id = str(claim["claim_id"])
            break
    if not primary_entry_claim_id:
        for claim in claims:
            if _claim_direction_bucket(claim.get("expected_move")) == "up":
                primary_entry_claim_id = str(claim["claim_id"])
                break
    if not primary_entry_claim_id:
        primary_entry_claim_id = str(claims[0]["claim_id"])

    def _repair_node(node: Mapping[str, Any], *, phase: Optional[str] = None, index: int = 0) -> dict[str, Any]:
        nonlocal changed
        node_payload = _as_dict(node)
        if not node_payload:
            return {}
        phase_bucket = _trade_plan_phase_bucket(phase, node_payload)
        existing_node_id = _string(
            node_payload.get("node_id")
            or node_payload.get("plan_node_id")
            or node_payload.get("trade_plan_node_id")
            or node_payload.get("id")
        )
        fallback_node_id = (
            f"{phase_bucket}_step_1"
            if phase in {"entry", "exit"} and phase_bucket in {"entry", "exit"}
            else f"{phase_bucket}_{index}"
        )
        node_id = existing_node_id or fallback_node_id
        if node_payload.get("node_id") != node_id:
            node_payload["node_id"] = node_id
            changed = True
        if not _string(node_payload.get("phase")):
            node_payload["phase"] = phase_bucket
            changed = True
        claim_ids = _dedup_strings(_as_list(node_payload.get("claim_ids")))
        if _trade_plan_claim_ids_replaceable(claim_ids, claim_id_set):
            selected_claim_ids = _select_claim_ids_for_trade_plan_node(
                phase=phase_bucket,
                claims=claims,
                primary_entry_claim_id=primary_entry_claim_id,
            )
            if selected_claim_ids and claim_ids != selected_claim_ids:
                node_payload["claim_ids"] = selected_claim_ids
                claim_ids = selected_claim_ids
                changed = True
        evidence_ids = _dedup_strings(_as_list(node_payload.get("evidence_ids")))
        if not evidence_ids and claim_ids and all(claim_id in claim_id_set for claim_id in claim_ids):
            selected_evidence_ids = _claim_evidence_ids(claims_by_id, claim_ids)
            if selected_evidence_ids:
                node_payload["evidence_ids"] = selected_evidence_ids
                changed = True
        return node_payload

    if _as_dict(payload.get("entry")):
        payload["entry"] = _repair_node(_as_dict(payload.get("entry")), phase="entry")
    if _as_dict(payload.get("exit")):
        payload["exit"] = _repair_node(_as_dict(payload.get("exit")), phase="exit")

    for key, default_phase in (
        ("entries", "entry"),
        ("exits", "exit"),
        ("nodes", None),
        ("steps", None),
    ):
        if key not in payload:
            continue
        repaired_items = []
        for index, item in enumerate(_as_list(payload.get(key))):
            repaired = _repair_node(_as_dict(item), phase=default_phase, index=index)
            if repaired:
                repaired_items.append(repaired)
        if repaired_items != _as_list(payload.get(key)):
            payload[key] = repaired_items
            changed = True

    trade_step_to_claim_ids: dict[str, list[str]] = {}
    claim_to_trade_step_ids: dict[str, list[str]] = {claim_id: [] for claim_id in sorted(claim_id_set)}
    for node in _normalized_trade_plan_nodes(payload):
        node_id = _string(node.get("node_id"))
        if not node_id:
            continue
        node_claim_ids = _dedup_strings(_as_list(node.get("claim_ids")))
        if not node_claim_ids:
            continue
        trade_step_to_claim_ids[node_id] = node_claim_ids
        for claim_id in node_claim_ids:
            if claim_id in claim_to_trade_step_ids:
                claim_to_trade_step_ids[claim_id].append(node_id)

    claim_to_trade_plan_map = {
        **_as_dict(existing_claim_to_trade_plan_map),
        "claim_to_trade_step_ids": {
            claim_id: _dedup_strings(step_ids)
            for claim_id, step_ids in claim_to_trade_step_ids.items()
        },
        "trade_step_to_claim_ids": trade_step_to_claim_ids,
    }
    claim_to_trade_plan_map["mapped_claim_count"] = sum(
        1 for step_ids in claim_to_trade_plan_map["claim_to_trade_step_ids"].values() if step_ids
    )
    claim_to_trade_plan_map["trade_step_count"] = len(trade_step_to_claim_ids)
    claim_to_trade_plan_map["unmapped_claim_ids"] = [
        claim_id
        for claim_id, step_ids in claim_to_trade_plan_map["claim_to_trade_step_ids"].items()
        if not step_ids
    ]
    return payload, claim_to_trade_plan_map, changed


def _attach_semantic_fields(payload: dict[str, Any], fields: Mapping[str, Any]) -> dict[str, Any]:
    params = _as_dict(payload.get("params"))
    for key, value in dict(fields or {}).items():
        if value in _EMPTY_VALUES:
            continue
        payload[key] = value
        params[key] = value
    payload["params"] = params
    return payload


def ensure_candidate_semantic_contract(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a candidate with complete evidence, prediction, and confidence contracts.

    This is a deterministic factory-side backfill. It preserves explicit LLM or
    DSL contracts, and only fills the missing pieces needed for G3 to audit the
    candidate instead of treating absent contracts as absent evidence.
    """
    payload = dict(candidate or {})
    params = _as_dict(payload.get("params"))
    strategy_type = _normalized_strategy_type(payload) or "strategy"
    target_symbols = _contract_target_symbols(payload)
    horizon_days = _default_horizon_days(payload)

    trade_plan = _ensure_trade_plan_nodes(payload, strategy_type)
    evidence_chain = _as_dict(_candidate_value(payload, "evidence_chain"))
    prediction_contract = _as_dict(_candidate_value(payload, "prediction_contract"))
    if not evidence_chain:
        evidence_chain = _build_default_evidence_chain(
            payload,
            strategy_type=strategy_type,
            target_symbols=target_symbols,
            horizon_days=horizon_days,
        )
    if not prediction_contract:
        prediction_contract = _build_default_prediction_contract(
            payload,
            strategy_type=strategy_type,
            target_symbols=target_symbols,
            horizon_days=horizon_days,
        )
    confidence_contract = synthesize_confidence_contract(
        {
            **payload,
            "trade_plan": trade_plan,
            "evidence_chain": evidence_chain,
            "prediction_contract": prediction_contract,
        }
    )
    claim_to_trade_plan_map = _as_dict(_candidate_value(payload, "claim_to_trade_plan_map"))
    trade_plan, claim_to_trade_plan_map, claim_ids_backfilled = _backfill_trade_plan_claim_links(
        trade_plan,
        prediction_contract,
        claim_to_trade_plan_map,
    )
    dsl = _ensure_dsl_mapping(payload, trade_plan)
    confidence_contract = synthesize_confidence_contract(
        {
            **payload,
            "trade_plan": trade_plan,
            "dsl": dsl or _as_dict(_candidate_value(payload, "dsl")),
            "evidence_chain": evidence_chain,
            "prediction_contract": prediction_contract,
        }
    )

    payload = _attach_semantic_fields(
        payload,
        {
            "trade_plan": trade_plan,
            "dsl": dsl,
            "evidence_chain": evidence_chain,
            "prediction_contract": prediction_contract,
            "confidence_contract": confidence_contract,
            "claim_to_trade_plan_map": claim_to_trade_plan_map,
            "semantic_contract_claim_ids_backfilled": bool(claim_ids_backfilled),
            "semantic_contract_backfill_source": (
                "prediction_contract_claims" if claim_ids_backfilled else "none"
            ),
            "semantic_contract_backfilled": True,
        },
    )
    return payload


def _normalize_evidences(evidence_chain: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidences = []
    for item in _as_list(evidence_chain.get("evidences")):
        payload = _as_dict(item)
        evidence_id = _string(payload.get("evidence_id") or payload.get("id"))
        if not evidence_id:
            continue
        evidences.append(
            {
                **payload,
                "evidence_id": evidence_id,
                "source_type": _string(payload.get("source_type")) or None,
                "direction": _string(payload.get("direction")).lower() or None,
                "horizon_days": (
                    _safe_int(payload.get("horizon_days"))
                    if payload.get("horizon_days") is not None
                    else None
                ),
                "raw_confidence": (
                    round(_safe_float(payload.get("raw_confidence")), 4)
                    if payload.get("raw_confidence") is not None
                    else None
                ),
                "calibrated_confidence": (
                    round(_safe_float(payload.get("calibrated_confidence")), 4)
                    if payload.get("calibrated_confidence") is not None
                    else None
                ),
                "proxy_only": bool(payload.get("proxy_only")),
                "event_type": _string(payload.get("event_type")) or None,
                "summary": _string(payload.get("summary")) or None,
                "doc_uid": _string(payload.get("doc_uid")) or None,
                "headline_label_id": _string(payload.get("headline_label_id")) or None,
                "freshness_ts": _coerce_iso_ts(payload.get("freshness_ts")),
                "support_metric": payload.get("support_metric"),
                "target_symbols": [
                    _string(symbol)
                    for symbol in _as_list(payload.get("target_symbols"))
                    if _string(symbol)
                ],
            }
        )
    return evidences


def inspect_strategy_dsl_support(raw_dsl: Any) -> dict[str, Any]:
    unsupported_fields: set[str] = set()
    unsupported_indicators: set[str] = set()
    unsupported_compare_ops: set[str] = set()
    unsupported_binary_ops: set[str] = set()
    malformed_node_count = 0
    fallback_node_count = 0

    def _walk_expr(node: Any) -> None:
        nonlocal malformed_node_count, fallback_node_count
        if node in _EMPTY_VALUES:
            return
        if isinstance(node, (int, float)):
            return
        if isinstance(node, str):
            token = _string(node).lower()
            if token and token not in _SUPPORTED_DSL_FIELDS:
                try:
                    float(token)
                except Exception:
                    fallback_node_count += 1
            return
        if not isinstance(node, Mapping):
            malformed_node_count += 1
            return
        payload = dict(node)
        indicator = _string(payload.get("indicator")).lower()
        if indicator:
            if indicator not in _SUPPORTED_DSL_INDICATORS:
                unsupported_indicators.add(indicator)
            field_name = _string(payload.get("field") or payload.get("column")).lower()
            if field_name and field_name not in _SUPPORTED_DSL_FIELDS:
                unsupported_fields.add(field_name)
            return
        field_name = _string(payload.get("field") or payload.get("column")).lower()
        if field_name:
            if field_name not in _SUPPORTED_DSL_FIELDS:
                unsupported_fields.add(field_name)
            return
        if "value" in payload:
            return
        binary_payload = payload.get("binary") if isinstance(payload.get("binary"), Mapping) else None
        if binary_payload is not None:
            _walk_expr(binary_payload)
            return
        op = _string(payload.get("op")).lower()
        if op and ("left" in payload or "right" in payload):
            if op not in _SUPPORTED_DSL_BINARY_OPS:
                unsupported_binary_ops.add(op)
            _walk_expr(payload.get("left"))
            _walk_expr(payload.get("right"))
            return
        shorthand_binary_keys = [
            key for key in payload.keys() if _string(key).lower() in _SUPPORTED_DSL_BINARY_OPS
        ]
        if shorthand_binary_keys:
            for key in shorthand_binary_keys:
                nested = payload.get(key)
                if isinstance(nested, (list, tuple)) and len(nested) >= 2:
                    _walk_expr(nested[0])
                    _walk_expr(nested[1])
                elif isinstance(nested, Mapping):
                    _walk_expr(dict(nested).get("left") or dict(nested).get("a"))
                    _walk_expr(dict(nested).get("right") or dict(nested).get("b"))
                else:
                    malformed_node_count += 1
            return
        shorthand_indicators = [
            key for key in payload.keys()
            if _string(key).lower() not in {"left", "right", "value", "binary", "field", "column", "window", "period"}
            and _string(key).lower() not in _SUPPORTED_DSL_COMPARE_OPS
            and _string(key).lower() not in {"all", "any", "not"}
        ]
        known_indicator_keys = [key for key in shorthand_indicators if _string(key).lower() in _SUPPORTED_DSL_INDICATORS]
        if known_indicator_keys:
            for key in known_indicator_keys:
                _walk_expr({"indicator": _string(key).lower(), **(_as_dict(payload.get(key)))})
            return
        if shorthand_indicators:
            for key in shorthand_indicators:
                unsupported_indicators.add(_string(key).lower())
            return
        fallback_node_count += 1

    def _walk_condition(node: Any) -> None:
        nonlocal malformed_node_count, fallback_node_count
        if node in _EMPTY_VALUES:
            return
        if not isinstance(node, Mapping):
            malformed_node_count += 1
            return
        payload = dict(node)
        if "all" in payload:
            for item in _as_list(payload.get("all")):
                _walk_condition(item)
            return
        if "any" in payload:
            for item in _as_list(payload.get("any")):
                _walk_condition(item)
            return
        if "not" in payload:
            _walk_condition(payload.get("not"))
            return
        op = _string(payload.get("op")).lower()
        if op:
            if op not in _SUPPORTED_DSL_COMPARE_OPS:
                unsupported_compare_ops.add(op)
            _walk_expr(payload.get("left"))
            _walk_expr(payload.get("right"))
            return
        shorthand_compare_keys = [
            key for key in payload.keys() if _string(key).lower() in _SUPPORTED_DSL_COMPARE_OPS
        ]
        if shorthand_compare_keys:
            for key in shorthand_compare_keys:
                nested = payload.get(key)
                if isinstance(nested, (list, tuple)) and len(nested) >= 2:
                    _walk_expr(nested[0])
                    _walk_expr(nested[1])
                elif isinstance(nested, Mapping):
                    nested_payload = dict(nested)
                    _walk_expr(nested_payload.get("left") or nested_payload.get("a"))
                    _walk_expr(nested_payload.get("right") or nested_payload.get("b"))
                else:
                    malformed_node_count += 1
            return
        fallback_node_count += 1

    payload = _as_dict(raw_dsl)
    entry = payload.get("entry")
    exit_payload = payload.get("exit")
    if entry in _EMPTY_VALUES:
        fallback_node_count += 1
    else:
        _walk_condition(entry)
    if exit_payload not in _EMPTY_VALUES:
        _walk_condition(exit_payload)

    unsupported_rule_count = (
        len(unsupported_fields)
        + len(unsupported_indicators)
        + len(unsupported_compare_ops)
        + len(unsupported_binary_ops)
        + malformed_node_count
        + fallback_node_count
    )
    return {
        "unsupported_rule_count": int(unsupported_rule_count),
        "unsupported_fields": sorted(unsupported_fields),
        "unsupported_indicators": sorted(unsupported_indicators),
        "unsupported_compare_ops": sorted(unsupported_compare_ops),
        "unsupported_binary_ops": sorted(unsupported_binary_ops),
        "malformed_node_count": int(malformed_node_count),
        "fallback_node_count": int(fallback_node_count),
    }


def _normalize_claims(prediction_contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    claims = []
    for item in _as_list(prediction_contract.get("claims")):
        payload = _as_dict(item)
        claim_id = _string(payload.get("claim_id") or payload.get("id"))
        if not claim_id:
            continue
        claims.append(
            {
                **payload,
                "claim_id": claim_id,
                "evidence_ids": _dedup_strings(
                    [
                        _string(value)
                        for value in _as_list(payload.get("evidence_ids"))
                    ]
                ),
                "expected_move": _string(payload.get("expected_move")).lower() or None,
                "expected_horizon": (
                    _safe_int(payload.get("expected_horizon"))
                    if payload.get("expected_horizon") is not None
                    else None
                ),
                "conflict_resolution_rule": _normalize_conflict_resolution_rule(
                    payload.get("conflict_resolution_rule")
                ),
            }
        )
    return claims


def _normalized_trade_plan_nodes(trade_plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def _append_node(node: Mapping[str, Any], *, phase: Optional[str] = None, index: int = 0) -> None:
        payload = _as_dict(node)
        if not payload:
            return
        node_id = _string(
            payload.get("node_id")
            or payload.get("plan_node_id")
            or payload.get("trade_plan_node_id")
            or payload.get("id")
        ) or f"{phase or 'node'}_{index}"
        claim_ids = _dedup_strings(_as_list(payload.get("claim_ids")))
        evidence_ids = _dedup_strings(_as_list(payload.get("evidence_ids")))
        nodes.append(
            {
                **payload,
                "node_id": node_id,
                "phase": phase or _string(payload.get("phase")).lower() or "node",
                "claim_ids": claim_ids,
                "evidence_ids": evidence_ids,
            }
        )

    entry = _as_dict(trade_plan.get("entry"))
    exit_payload = _as_dict(trade_plan.get("exit"))
    if entry:
        _append_node(entry, phase="entry")
    if exit_payload:
        _append_node(exit_payload, phase="exit")

    for phase in ("entries", "exits", "nodes", "steps"):
        for index, item in enumerate(_as_list(trade_plan.get(phase))):
            node_phase = "entry" if phase == "entries" else "exit" if phase == "exits" else phase
            _append_node(_as_dict(item), phase=node_phase, index=index)

    if not nodes and trade_plan:
        _append_node(trade_plan, phase="node")
    return nodes


def _collect_trade_plan_node_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, Mapping):
        payload = dict(value)
        for key in (
            "trade_plan_node_id",
            "trade_plan_step_id",
            "plan_node_id",
            "mapped_trade_plan_node_id",
            "node_id",
        ):
            token = _string(payload.get(key))
            if token:
                refs.append(token)
        for key in ("trade_plan_node_ids", "claim_ids", "mapped_trade_plan_node_ids"):
            refs.extend(_dedup_strings(_as_list(payload.get(key))))
        for child in payload.values():
            refs.extend(_collect_trade_plan_node_refs(child))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_collect_trade_plan_node_refs(item))
    return _dedup_strings(refs)

# --- semantic_contract_parts/evaluation.py ---
def _direction_bucket(value: Any) -> Optional[str]:
    token = _string(value).lower()
    if not token:
        return None
    if any(word in token for word in ("up", "long", "bull", "buy", "rise", "rebound")):
        return "up"
    if any(word in token for word in ("down", "short", "bear", "sell", "fall", "drop")):
        return "down"
    return None


def _condition_contains_compare_op(node: Any, ops: set[str]) -> bool:
    if node in _EMPTY_VALUES:
        return False
    if isinstance(node, Mapping):
        payload = dict(node)
        op = _string(payload.get("op")).lower()
        if op in ops:
            return True
        if "all" in payload:
            return any(_condition_contains_compare_op(item, ops) for item in _as_list(payload.get("all")))
        if "any" in payload:
            return any(_condition_contains_compare_op(item, ops) for item in _as_list(payload.get("any")))
        if "not" in payload:
            return _condition_contains_compare_op(payload.get("not"), ops)
        return any(_condition_contains_compare_op(item, ops) for item in payload.values())
    if isinstance(node, list):
        return any(_condition_contains_compare_op(item, ops) for item in node)
    return False


def _has_unquantified_regime_language(text: str) -> bool:
    normalized = _string(text).lower()
    if not normalized:
        return False
    if any(ch.isdigit() for ch in normalized):
        return False
    if any(symbol in normalized for symbol in (">", "<", ">=", "<=", "%")):
        return False
    return any(token in normalized for token in _AMBIGUOUS_REGIME_TOKENS)


def _audit_lagging_entry_without_lead_evidence(
    *,
    strategy_type: Optional[str],
    claims: list[dict[str, Any]],
    evidences: list[dict[str, Any]],
    trade_plan: Mapping[str, Any],
    dsl: Mapping[str, Any],
) -> dict[str, Any]:
    if strategy_type not in _TREND_EXECUTABLE_DSL_TYPES:
        return {
            "applies": False,
            "status": "not_applicable",
            "lagging_entry_detected": False,
            "lead_evidence_count": 0,
            "reasons": [],
        }
    entry_node = _as_dict(trade_plan.get("entry"))
    entry_text = " ".join(
        [
            _string(entry_node.get("summary")),
            _string(entry_node.get("entry_bias")),
            _string(entry_node.get("setup")),
            _string(entry_node.get("trigger")),
            _string(entry_node.get("condition")),
            _string(trade_plan.get("entry_bias")),
        ]
    ).lower()
    lagging_entry_detected = _condition_contains_compare_op(
        dsl.get("entry"),
        {"cross_above", "cross_below"},
    ) or any(
        token in entry_text
        for token in ("golden cross", "death cross", "cross", "金叉", "死叉", "confirmation", "确认", "上穿", "下穿")
    )

    lead_evidence_count = 0
    for evidence in evidences:
        source_type = _normalized_source_type(evidence.get("source_type"))
        if source_type not in {"price_action", "technical", "volume", "kline"}:
            lead_evidence_count += 1
            continue
        if _string(evidence.get("event_type")) or _string(evidence.get("doc_uid")) or _string(evidence.get("headline_label_id")):
            lead_evidence_count += 1
            continue
        support_metric = _string(evidence.get("support_metric")).lower()
        if support_metric and support_metric not in {"price", "close", "volume"}:
            lead_evidence_count += 1

    claim_failure_condition_count = sum(1 for claim in claims if _string(claim.get("failure_condition")))
    reasons: list[str] = []
    if lagging_entry_detected and lead_evidence_count <= 0:
        reasons.append("lagging_entry_has_no_lead_evidence")
    if lagging_entry_detected and claim_failure_condition_count <= 0:
        reasons.append("lagging_entry_claim_missing_failure_condition")
    status = "failed" if reasons else "passed"
    return {
        "applies": True,
        "status": status,
        "lagging_entry_detected": lagging_entry_detected,
        "lead_evidence_count": lead_evidence_count,
        "claim_failure_condition_count": claim_failure_condition_count,
        "reasons": reasons,
    }


def _audit_temporal_coherence(
    *,
    claims: list[dict[str, Any]],
    holding_horizon: Mapping[str, Any],
    risk_rules: Mapping[str, Any],
    rebalance_rule: Mapping[str, Any],
    trade_plan: Mapping[str, Any],
) -> dict[str, Any]:
    expected_horizons = [
        _safe_int(item.get("expected_horizon"))
        for item in claims
        if _safe_int(item.get("expected_horizon")) > 0
    ]
    max_claim_horizon = max(expected_horizons, default=0)
    min_claim_horizon = min(expected_horizons, default=0) if expected_horizons else 0
    max_holding_days = max(
        _safe_int(holding_horizon.get("max_days")),
        _safe_int(risk_rules.get("max_holding_days")),
        0,
    )
    rebalance_interval_days = _safe_int(
        rebalance_rule.get("frequency_days")
        or rebalance_rule.get("rebalance_interval_days")
    )
    signal_validity_days = _safe_int(
        _as_dict(trade_plan.get("entry")).get("signal_validity_days")
        or trade_plan.get("signal_validity_days")
    )

    reasons: list[str] = []
    if max_claim_horizon > 0 and max_holding_days > 0 and max_claim_horizon > max_holding_days:
        reasons.append("claim_expected_horizon_exceeds_holding_horizon")
    if min_claim_horizon > 0 and rebalance_interval_days > 0 and rebalance_interval_days > max(min_claim_horizon, max_holding_days or min_claim_horizon):
        reasons.append("rebalance_interval_exceeds_claim_horizon")
    if signal_validity_days > 0 and max_holding_days > 0 and signal_validity_days > max_holding_days:
        reasons.append("signal_validity_exceeds_holding_horizon")

    return {
        "status": "failed" if reasons else "passed",
        "reasons": reasons,
        "max_claim_horizon": max_claim_horizon or None,
        "min_claim_horizon": min_claim_horizon or None,
        "max_holding_days": max_holding_days or None,
        "rebalance_interval_days": rebalance_interval_days or None,
        "signal_validity_days": signal_validity_days or None,
    }


def _audit_ambiguous_regime_condition(
    *,
    market_regime_assumption: Mapping[str, Any],
    regime_filter_contract: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _as_dict(market_regime_assumption)
    quantified_filters = _as_list(
        regime_filter_contract.get("filters")
        or regime_filter_contract.get("quantified_filters")
        or payload.get("quantified_filters")
        or payload.get("conditions")
    )
    ambiguous_fields: list[str] = []
    for field_name in ("summary", "preferred_regime", "avoid_regime", "regime_note", "avoid_note"):
        if _has_unquantified_regime_language(_string(payload.get(field_name))):
            ambiguous_fields.append(field_name)
    reasons: list[str] = []
    if ambiguous_fields and not quantified_filters:
        reasons.append("ambiguous_regime_condition_not_allowed")
    return {
        "status": "failed" if reasons else "passed",
        "ambiguous_fields": ambiguous_fields,
        "quantified_filter_count": len(quantified_filters),
        "reasons": reasons,
    }


def audit_candidate_semantic_contract(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(candidate or {})
    strategy_type = _normalized_strategy_type(payload)
    evidence_chain = _as_dict(_candidate_value(payload, "evidence_chain"))
    prediction_contract = _as_dict(_candidate_value(payload, "prediction_contract"))
    confidence_contract = _as_dict(_candidate_value(payload, "confidence_contract"))
    trade_plan = _as_dict(_candidate_value(payload, "trade_plan"))
    dsl = _as_dict(_candidate_value(payload, "dsl"))
    dsl_required_for_audit = bool(_candidate_value(payload, "dsl_required")) or bool(dsl.get("entry") or dsl.get("exit"))
    holding_horizon = _as_dict(_candidate_value(payload, "holding_horizon"))
    risk_rules = _as_dict(_candidate_value(payload, "risk_rules"))
    rebalance_rule = _as_dict(_candidate_value(payload, "rebalance_rule"))
    market_regime_assumption = _as_dict(_candidate_value(payload, "market_regime_assumption"))
    regime_filter_contract = _as_dict(_candidate_value(payload, "regime_filter_contract"))
    research_task = _as_dict(_candidate_value(payload, "research_task"))
    event_context = _as_dict(_candidate_value(payload, "event_context"))
    dsl_support_audit = _as_dict(
        _candidate_value(payload, "dsl_support_audit")
    ) or inspect_strategy_dsl_support(dsl)

    evidences = _normalize_evidences(evidence_chain)
    evidence_by_id = {item["evidence_id"]: item for item in evidences}
    claims = _normalize_claims(prediction_contract)
    trade_plan_nodes = _normalized_trade_plan_nodes(trade_plan)
    contract_conflict_resolution_rule = _normalize_conflict_resolution_rule(
        prediction_contract.get("conflict_resolution_rule")
        or confidence_contract.get("conflict_resolution_rule")
    )

    using_new_contract = bool(prediction_contract)
    legacy_semantic_contract = not using_new_contract
    event_driven_strategy = (
        _string(research_task.get("task_source")).lower() == "event_driven"
        or bool(research_task.get("event_id"))
        or bool(event_context)
    )
    claim_missing_evidence_ids = 0
    claim_missing_evidence_refs = 0
    mapped_claims = 0
    contradiction_count = 0
    conflicting_claim_count = 0
    conflict_resolution_rule_missing_count = 0
    proxy_claim_count = 0
    proxy_only_event_claim_count = 0
    referenced_evidence_count = 0
    trade_plan_missing_claim_ids = 0
    trade_plan_missing_evidence_refs = 0

    for claim in claims:
        evidence_ids = list(claim.get("evidence_ids") or [])
        if not evidence_ids:
            claim_missing_evidence_ids += 1
            continue
        mapped_claims += 1
        referenced_evidence_count += len(evidence_ids)
        evidence_refs = [evidence_by_id.get(evidence_id) for evidence_id in evidence_ids]
        if any(item is None for item in evidence_refs):
            claim_missing_evidence_refs += sum(1 for item in evidence_refs if item is None)
        expected_direction = _direction_bucket(claim.get("expected_move"))
        claim_conflict_resolution_rule = (
            claim.get("conflict_resolution_rule") or contract_conflict_resolution_rule
        )
        resolved_evidence_refs = [item for item in evidence_refs if item is not None]
        if resolved_evidence_refs and all(bool(_as_dict(item).get("proxy_only")) for item in resolved_evidence_refs):
            proxy_claim_count += 1
        if (
            event_driven_strategy
            and resolved_evidence_refs
            and all(bool(_as_dict(item).get("proxy_only")) for item in resolved_evidence_refs)
            and all(
                _normalized_source_type(_as_dict(item).get("source_type")) in {"news", "sentiment"}
                for item in resolved_evidence_refs
            )
        ):
            proxy_only_event_claim_count += 1

        direction_buckets = [
            _direction_bucket(_as_dict(evidence).get("direction"))
            for evidence in resolved_evidence_refs
        ]
        same_direction_count = 0
        opposite_direction_count = 0
        non_null_direction_buckets = [direction for direction in direction_buckets if direction]
        if expected_direction:
            same_direction_count = sum(1 for direction in non_null_direction_buckets if direction == expected_direction)
            opposite_direction_count = sum(1 for direction in non_null_direction_buckets if direction != expected_direction)
            if same_direction_count > 0 and opposite_direction_count > 0:
                conflicting_claim_count += 1
                if not claim_conflict_resolution_rule:
                    conflict_resolution_rule_missing_count += 1
            elif opposite_direction_count > 0 and same_direction_count <= 0:
                contradiction_count += 1
        elif len(set(non_null_direction_buckets)) > 1:
            conflicting_claim_count += 1
            if not claim_conflict_resolution_rule:
                conflict_resolution_rule_missing_count += 1

    claim_id_set = {item.get("claim_id") for item in claims if item.get("claim_id")}
    for node in trade_plan_nodes:
        claim_ids = [claim_id for claim_id in list(node.get("claim_ids") or []) if claim_id]
        evidence_ids = [evidence_id for evidence_id in list(node.get("evidence_ids") or []) if evidence_id]
        if using_new_contract and not claim_ids:
            trade_plan_missing_claim_ids += 1
        if claim_ids and any(claim_id not in claim_id_set for claim_id in claim_ids):
            trade_plan_missing_claim_ids += sum(1 for claim_id in claim_ids if claim_id not in claim_id_set)
        if evidence_ids and any(evidence_id not in evidence_by_id for evidence_id in evidence_ids):
            trade_plan_missing_evidence_refs += sum(1 for evidence_id in evidence_ids if evidence_id not in evidence_by_id)

    dsl_entry_refs = _collect_trade_plan_node_refs(dsl.get("entry"))
    dsl_exit_refs = _collect_trade_plan_node_refs(dsl.get("exit"))
    available_entry_nodes = {
        node.get("node_id")
        for node in trade_plan_nodes
        if str(node.get("phase") or "").lower() in {"entry", "entries"}
    }
    available_exit_nodes = {
        node.get("node_id")
        for node in trade_plan_nodes
        if str(node.get("phase") or "").lower() in {"exit", "exits"}
    }
    dsl_entry_mapped = bool(dsl.get("entry")) and (
        bool(available_entry_nodes and set(dsl_entry_refs).intersection(available_entry_nodes))
        or bool(_as_dict(trade_plan.get("entry")))
    )
    dsl_exit_mapped = bool(dsl.get("exit")) and (
        bool(available_exit_nodes and set(dsl_exit_refs).intersection(available_exit_nodes))
        or bool(_as_dict(trade_plan.get("exit")))
    )

    claim_alignment_ratio = round(mapped_claims / max(1, len(claims)), 4) if claims else 0.0
    trade_plan_claim_ratio = round(
        sum(1 for node in trade_plan_nodes if node.get("claim_ids")) / max(1, len(trade_plan_nodes)),
        4,
    ) if trade_plan_nodes else 0.0
    dsl_mapping_ratio = round(
        (
            (1.0 if dsl_entry_mapped else 0.0)
            + (1.0 if dsl_exit_mapped else 0.0)
        ) / 2.0,
        4,
    ) if dsl.get("entry") or dsl.get("exit") else 0.0
    proxy_dependency_score = round(proxy_claim_count / max(1, mapped_claims), 4) if mapped_claims else 0.0
    applicable_alignment_scores = [claim_alignment_ratio, trade_plan_claim_ratio]
    if dsl_required_for_audit:
        applicable_alignment_scores.append(dsl_mapping_ratio)
    evidence_alignment_score = round(
        sum(applicable_alignment_scores) / max(1, len(applicable_alignment_scores)),
        4,
    ) if using_new_contract else 0.0
    semantic_integrity_score = round(
        max(0.0, evidence_alignment_score - min(0.5, contradiction_count * 0.25)),
        4,
    ) if using_new_contract else 0.0
    lagging_entry_audit = _audit_lagging_entry_without_lead_evidence(
        strategy_type=strategy_type,
        claims=claims,
        evidences=evidences,
        trade_plan=trade_plan,
        dsl=dsl,
    )
    temporal_coherence_audit = _audit_temporal_coherence(
        claims=claims,
        holding_horizon=holding_horizon,
        risk_rules=risk_rules,
        rebalance_rule=rebalance_rule,
        trade_plan=trade_plan,
    )
    ambiguous_regime_condition_audit = _audit_ambiguous_regime_condition(
        market_regime_assumption=market_regime_assumption,
        regime_filter_contract=regime_filter_contract,
    )

    hard_fail_reasons: list[str] = []
    if using_new_contract and claim_missing_evidence_ids > 0:
        hard_fail_reasons.append("prediction_contract_claim_missing_evidence_ids")
    if using_new_contract and conflict_resolution_rule_missing_count > 0:
        hard_fail_reasons.append("prediction_contract_conflict_resolution_rule_missing")
    if using_new_contract and trade_plan_missing_claim_ids > 0:
        hard_fail_reasons.append("trade_plan_node_missing_claim_ids")
    if using_new_contract and dsl_required_for_audit and not dsl_entry_mapped:
        hard_fail_reasons.append("dsl_entry_not_mapped_to_trade_plan")
    if using_new_contract and dsl_required_for_audit and not dsl_exit_mapped:
        hard_fail_reasons.append("dsl_exit_not_mapped_to_trade_plan")
    if using_new_contract and contradiction_count > 0:
        hard_fail_reasons.append("semantic_contract_contradiction_detected")
    if using_new_contract and proxy_only_event_claim_count > 0:
        hard_fail_reasons.append("proxy_only_event_evidence_not_allowed")
    if using_new_contract and dsl_required_for_audit and _safe_int(dsl_support_audit.get("unsupported_rule_count"), 0) > 0:
        hard_fail_reasons.append("dsl_contains_unsupported_rules")
    if using_new_contract and lagging_entry_audit.get("status") == "failed":
        hard_fail_reasons.append("lagging_entry_without_lead_evidence")
    if using_new_contract and temporal_coherence_audit.get("status") == "failed":
        hard_fail_reasons.append("temporal_coherence_audit_failed")
    if using_new_contract and ambiguous_regime_condition_audit.get("status") == "failed":
        hard_fail_reasons.append("ambiguous_regime_condition_not_allowed")

    alignment_status = "legacy" if legacy_semantic_contract else "aligned"
    if using_new_contract and hard_fail_reasons:
        alignment_status = "failed"
    elif using_new_contract and evidence_alignment_score < 0.75:
        alignment_status = "partial"

    return {
        "using_new_contract": using_new_contract,
        "legacy_semantic_contract": legacy_semantic_contract,
        "evidence_count": len(evidences),
        "claim_count": len(claims),
        "trade_plan_node_count": len(trade_plan_nodes),
        "claim_alignment_ratio": claim_alignment_ratio,
        "trade_plan_claim_ratio": trade_plan_claim_ratio,
        "dsl_mapping_ratio": dsl_mapping_ratio,
        "evidence_alignment_score": evidence_alignment_score,
        "semantic_integrity_score": semantic_integrity_score,
        "proxy_dependency_score": proxy_dependency_score,
        "contradiction_count": contradiction_count,
        "conflicting_claim_count": conflicting_claim_count,
        "conflict_resolution_rule_missing_count": conflict_resolution_rule_missing_count,
        "unsupported_rule_count": _safe_int(dsl_support_audit.get("unsupported_rule_count"), 0),
        "dsl_support_audit": dsl_support_audit,
        "proxy_only_event_claim_count": proxy_only_event_claim_count,
        "event_driven_strategy": event_driven_strategy,
        "claim_missing_evidence_ids": claim_missing_evidence_ids,
        "claim_missing_evidence_refs": claim_missing_evidence_refs,
        "trade_plan_missing_claim_ids": trade_plan_missing_claim_ids,
        "trade_plan_missing_evidence_refs": trade_plan_missing_evidence_refs,
        "dsl_entry_mapped": dsl_entry_mapped,
        "dsl_exit_mapped": dsl_exit_mapped,
        "referenced_evidence_count": referenced_evidence_count,
        "evidence_alignment_status": alignment_status,
        "lagging_entry_without_lead_evidence": lagging_entry_audit,
        "temporal_coherence_audit": temporal_coherence_audit,
        "ambiguous_regime_condition_audit": ambiguous_regime_condition_audit,
        "hard_fail_reasons": hard_fail_reasons,
    }

# --- semantic_contract_parts/reporting.py ---
def build_candidate_evidence_records(
    candidate: Optional[Mapping[str, Any]],
    *,
    strategy_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    payload = dict(candidate or {})
    evidence_chain = _as_dict(_candidate_value(payload, "evidence_chain"))
    if not evidence_chain:
        return []
    research_task = _as_dict(_candidate_value(payload, "research_task"))
    target_symbols = [
        _string(symbol)
        for symbol in _as_list(_candidate_value(payload, "target_symbols"))
        if _string(symbol)
    ]
    task_key = (
        _string(research_task.get("task_signature"))
        or _string(research_task.get("task_key"))
        or _string(_candidate_value(payload, "task_signature"))
        or _string(strategy_id)
    )
    if not task_key:
        return []

    records: list[dict[str, Any]] = []
    candidate_id = (
        _string(payload.get("candidate_id") or payload.get("id"))
        or _string(strategy_id)
        or task_key
    )
    candidate_artifact_id = _string(
        _first_non_empty(
            payload.get("candidate_artifact_id"),
            payload.get("source_candidate_artifact_id"),
            payload.get("hypothesis_artifact_id"),
        )
    ) or None
    experiment_id = _string(payload.get("experiment_id")) or None
    for evidence in _normalize_evidences(evidence_chain):
        evidence_symbols = list(evidence.get("target_symbols") or [])
        source_type = _string(evidence.get("source_type")) or "candidate_evidence"
        headline_label_id = _string(evidence.get("headline_label_id")) or None
        doc_uid = _string(evidence.get("doc_uid")) or None
        records.append(
            {
                "id": f"{candidate_id}:{evidence.get('evidence_id')}",
                "candidate_id": candidate_id,
                "strategy_id": strategy_id,
                "candidate_artifact_id": candidate_artifact_id,
                "experiment_id": experiment_id,
                "evidence_id": evidence.get("evidence_id"),
                "source_type": source_type,
                "event_type": _string(evidence.get("event_type") or research_task.get("event_type")) or None,
                "target_symbols": evidence_symbols or target_symbols,
                "direction": evidence.get("direction"),
                "horizon_days": evidence.get("horizon_days"),
                "raw_confidence": evidence.get("raw_confidence"),
                "calibrated_confidence": evidence.get("calibrated_confidence"),
                "freshness_ts": evidence.get("freshness_ts"),
                "proxy_only": bool(evidence.get("proxy_only")),
                "support_metric": evidence.get("support_metric"),
                "doc_uid": doc_uid,
                "headline_label_id": headline_label_id,
                "source_task_key": task_key,
                "task_key": task_key,
                "event_id": research_task.get("event_id"),
                "theme_code": _string(research_task.get("theme_code")),
                "symbol": (evidence_symbols or target_symbols or [None])[0],
                "evidence_type": source_type,
                "weight": _safe_float(evidence.get("raw_confidence"), 0.0),
                "evidence_payload": {
                    **evidence,
                    "strategy_id": strategy_id,
                    "candidate_id": candidate_id or None,
                    "candidate_artifact_id": candidate_artifact_id,
                    "experiment_id": experiment_id,
                    "headline_label_id": headline_label_id,
                    "doc_uid": doc_uid,
                },
            }
        )
    return records


# INVERT-DESIGN P1 改动D：信号 evidence 的市场状态（regime）维度。
_REGIME_DIMENSIONS: tuple[str, ...] = ("trend_regime", "vol_regime", "sentiment_regime")


def _normalize_regime_labels(regime: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """把传入的 regime 字典规整为三维标签，缺失维度填 'unknown'。"""
    source = dict(regime or {})
    labels: dict[str, str] = {}
    for dimension in _REGIME_DIMENSIONS:
        label = str(source.get(dimension) or "").strip().lower()
        labels[dimension] = label or "unknown"
    return labels


def _build_proxy_signal_evidence_records(
    payload: Mapping[str, Any],
    *,
    params: Mapping[str, Any],
    signal_id: Optional[str],
    position_id: Optional[str],
    account_id: Optional[str],
    signal_date: Any,
    code: Optional[str],
    regime_labels: Mapping[str, str],
) -> list[dict[str, Any]]:
    """INVERT-DESIGN P1 改动A 配套：为缺 evidence_chain 的宽进 observe 策略合成
    一条最小代理 evidence，使前向测量可进行。proxy_only=True，明确标记非正式语义契约。
    方向取 prediction_contract.direction，缺省按策略类型推 long(=up)。
    """
    strategy_id = _string(payload.get("id")) or None
    normalized_signal_id = _string(signal_id) or None
    if not strategy_id or not normalized_signal_id:
        return []
    prediction_contract = _as_dict(
        payload.get("prediction_contract") or dict(params or {}).get("prediction_contract")
    )
    direction = _string(prediction_contract.get("direction")).lower()
    if direction in {"long", "buy", "1"}:
        direction = "up"
    elif direction in {"short", "sell", "-1"}:
        direction = "down"
    elif direction not in {"up", "down", "neutral"}:
        direction = "neutral"
    resolved_code = (
        _string(code)
        or _string((list(payload.get("target_symbols") or params.get("target_symbols") or []) or [None])[0])
        or None
    )
    candidate_artifact_id = _string(
        _first_non_empty(
            payload.get("candidate_artifact_id"),
            payload.get("source_candidate_artifact_id"),
            dict(params or {}).get("source_candidate_artifact_id"),
        )
    ) or None
    experiment_id = _string(
        _first_non_empty(payload.get("experiment_id"), dict(params or {}).get("experiment_id"))
    ) or None
    signal_ts = _coerce_signal_ts(_first_non_empty(payload.get("signal_ts"), dict(params or {}).get("signal_ts"), signal_date))
    evidence_id = "wide_intake_observe_proxy"
    record_id = f"{normalized_signal_id}:{evidence_id}:proxy"
    return [
        {
            "id": record_id,
            "strategy_id": strategy_id,
            "signal_id": normalized_signal_id,
            "position_id": _string(position_id) or None,
            "account_id": _string(account_id) or None,
            "signal_date": signal_date,
            "signal_ts": signal_ts,
            "code": resolved_code,
            "symbol": resolved_code,
            "candidate_artifact_id": candidate_artifact_id,
            "experiment_id": experiment_id,
            "applied_claim_id": "wide_intake_observe_proxy_claim",
            "applied_trade_step_id": "wide_intake_observe_proxy_step",
            "evidence_id": evidence_id,
            "claim_ids": [],
            "evidence_type": "wide_intake_observe_proxy",
            "source_type": "wide_intake_observe_proxy",
            "direction": direction,
            "horizon_days": None,
            "raw_confidence": None,
            "calibrated_confidence": None,
            "proxy_only": True,
            "doc_uid": None,
            "headline_label_id": None,
            "weight": 0.0,
            **dict(regime_labels),
            "evidence_payload": {
                "build_mode": "wide_intake_observe_proxy",
                "strategy_id": strategy_id,
                "signal_id": normalized_signal_id,
                "signal_ts": signal_ts,
                "code": resolved_code,
                "direction": direction,
                "proxy_only": True,
                **dict(regime_labels),
            },
        }
    ]


def build_signal_evidence_records(
    strategy: Optional[Mapping[str, Any]],
    *,
    signal_id: Optional[str],
    position_id: Optional[str] = None,
    account_id: Optional[str] = None,
    signal_date: Any = None,
    code: Optional[str] = None,
    regime: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    payload = dict(strategy or {})
    params = _as_dict(payload.get("params"))
    evidence_chain = _as_dict(payload.get("evidence_chain") or params.get("evidence_chain"))
    prediction_contract = _as_dict(payload.get("prediction_contract") or params.get("prediction_contract"))
    # INVERT-DESIGN P1 改动D：解析当日市场状态标签（regime），写入每条 evidence。
    # 缺失时各维度归入 "unknown"，ForwardVerifier 据此分 regime 聚合命中率。
    regime_labels = _normalize_regime_labels(regime)
    claim_to_trade_plan_map = _as_dict(
        payload.get("claim_to_trade_plan_map") or params.get("claim_to_trade_plan_map")
    )
    claim_to_trade_step_ids = _as_dict(claim_to_trade_plan_map.get("claim_to_trade_step_ids"))
    if not evidence_chain:
        # INVERT-DESIGN P1 改动A 配套：宽进 observe 策略通常没有语义 evidence_chain
        # （fixed_defaults 填充物）。当 WIDE_INTAKE_OBSERVE 开启时，合成一条最小代理
        # evidence，使 ForwardVerifier 仍能对其做向前命中率测量（proxy_only=True，
        # 不冒充正式语义契约）。默认 OFF 时保持原 [] 行为，零变化。
        if str(os.getenv("STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}:
            return _build_proxy_signal_evidence_records(
                payload,
                params=params,
                signal_id=signal_id,
                position_id=position_id,
                account_id=account_id,
                signal_date=signal_date,
                code=code,
                regime_labels=regime_labels,
            )
        return []
    strategy_id = _string(payload.get("id")) or None
    normalized_signal_id = _string(signal_id) or None
    if not strategy_id or not normalized_signal_id:
        return []
    signal_ts = _coerce_signal_ts(_first_non_empty(payload.get("signal_ts"), params.get("signal_ts"), signal_date))
    target_symbols = [
        _string(symbol)
        for symbol in _as_list(payload.get("target_symbols") or params.get("target_symbols"))
        if _string(symbol)
    ]
    claims = _normalize_claims(prediction_contract)
    claim_refs_by_evidence: dict[str, list[str]] = {}
    for claim in claims:
        claim_id = _string(claim.get("claim_id"))
        if not claim_id:
            continue
        for evidence_id in _as_list(claim.get("evidence_ids")):
            token = _string(evidence_id)
            if not token:
                continue
            claim_refs_by_evidence.setdefault(token, []).append(claim_id)
    candidate_artifact_id = _string(
        _first_non_empty(
            payload.get("candidate_artifact_id"),
            payload.get("source_candidate_artifact_id"),
            params.get("source_candidate_artifact_id"),
            payload.get("hypothesis_artifact_id"),
            params.get("hypothesis_artifact_id"),
        )
    ) or None
    experiment_id = _string(_first_non_empty(payload.get("experiment_id"), params.get("experiment_id"))) or None
    records: list[dict[str, Any]] = []
    for evidence in _normalize_evidences(evidence_chain):
        evidence_symbols = list(evidence.get("target_symbols") or [])
        applied_claim_ids = _dedup_strings(
            [
                *claim_refs_by_evidence.get(_string(evidence.get("evidence_id")), []),
                *_as_list(evidence.get("claim_ids")),
            ]
        ) or [None]
        for applied_claim_id in applied_claim_ids:
            applied_trade_step_ids = _dedup_strings(
                _as_list(claim_to_trade_step_ids.get(_string(applied_claim_id)))
            ) or [None]
            for applied_trade_step_id in applied_trade_step_ids:
                record_id_suffix = _string(applied_claim_id) or "unclaimed"
                step_id_suffix = _string(applied_trade_step_id) or "unmapped_step"
                resolved_code = _string(code) or _string((evidence_symbols or target_symbols or [None])[0]) or None
                records.append(
                    {
                        "id": f"{normalized_signal_id}:{evidence.get('evidence_id')}:{record_id_suffix}:{step_id_suffix}",
                        "strategy_id": strategy_id,
                        "signal_id": normalized_signal_id,
                        "position_id": _string(position_id) or None,
                        "account_id": _string(account_id) or None,
                        "signal_date": signal_date,
                        "signal_ts": signal_ts,
                        "code": resolved_code,
                        "symbol": resolved_code,
                        "candidate_artifact_id": candidate_artifact_id,
                        "experiment_id": experiment_id,
                        "applied_claim_id": _string(applied_claim_id) or None,
                        "applied_trade_step_id": _string(applied_trade_step_id) or None,
                        "evidence_id": evidence.get("evidence_id"),
                        "claim_ids": [applied_claim_id] if _string(applied_claim_id) else [],
                        "evidence_type": _normalized_source_type(evidence.get("source_type")) or "signal_evidence",
                        "source_type": _normalized_source_type(evidence.get("source_type")) or "signal_evidence",
                        "direction": evidence.get("direction"),
                        "horizon_days": evidence.get("horizon_days"),
                        "raw_confidence": evidence.get("raw_confidence"),
                        "calibrated_confidence": evidence.get("calibrated_confidence"),
                        "proxy_only": bool(evidence.get("proxy_only")),
                        "doc_uid": _string(evidence.get("doc_uid")) or None,
                        "headline_label_id": _string(evidence.get("headline_label_id")) or None,
                        "weight": _safe_float(evidence.get("raw_confidence"), 0.0),
                        **regime_labels,
                        "evidence_payload": {
                            **evidence,
                            "strategy_id": strategy_id,
                            "signal_id": normalized_signal_id,
                            "candidate_artifact_id": candidate_artifact_id,
                            "experiment_id": experiment_id,
                            "applied_claim_id": _string(applied_claim_id) or None,
                            "applied_trade_step_id": _string(applied_trade_step_id) or None,
                            "signal_ts": signal_ts,
                            "code": resolved_code,
                            **regime_labels,
                        },
                    }
                )
    return records


__all__ = [
    "audit_candidate_semantic_contract",
    "build_candidate_evidence_records",
    "build_signal_evidence_records",
    "ensure_candidate_semantic_contract",
    "inspect_strategy_dsl_support",
    "normalize_semantic_contract_fields",
    "synthesize_confidence_contract",
]

