"""Evidence-first alpha helpers for Strategy Factory candidates.

The helpers in this module are intentionally pure: they only inspect a
candidate and the current market snapshot, then attach compact evidence,
direction, confidence, and generation-quality diagnostics. Storage, LLM calls,
and trading side effects stay outside the domain layer.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, time, timezone
from typing import Any, Mapping, Optional

_EMPTY_VALUES = (None, "", [], {})
_DIRECTION_ALIASES = {
    "up": "up",
    "bullish": "up",
    "long": "up",
    "buy": "up",
    "rise": "up",
    "rising": "up",
    "positive": "up",
    "down": "down",
    "bearish": "down",
    "short": "down",
    "sell": "down",
    "fall": "down",
    "falling": "down",
    "negative": "down",
    "neutral": "neutral",
    "flat": "neutral",
    "sideways": "neutral",
    "hold": "neutral",
}
_FACTOR_SOURCE_TYPES = {"factor_ic_validated", "active_factor_pool"}
_EVENT_SOURCE_TYPES = {"event_catalyst"}
_NON_PROXY_SOURCE_TYPES = {
    "factor_ic_validated",
    "active_factor_pool",
    "event_catalyst",
    "price_volume_confirmation",
    "fund_flow",
    "regime_context",
}
_TEMPLATE_SOURCE_TYPES = {"template_fallback", "rule_template_contract", "strategy_logic"}
_SHORT_BIAS_TYPES = {"mean_reversion_short"}

def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if value in _EMPTY_VALUES:
        return []
    return [value]


def _string(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        fallback = float(default)
    except (TypeError, ValueError):
        fallback = 0.0
    if not math.isfinite(fallback):
        fallback = 0.0
    try:
        if value in _EMPTY_VALUES:
            return fallback
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        fallback = int(default)
    except (TypeError, ValueError):
        fallback = 0
    try:
        if value in _EMPTY_VALUES:
            return fallback
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(parsed):
        return fallback
    return int(parsed)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _candidate_value(candidate: Mapping[str, Any], key: str) -> Any:
    params = _as_dict(candidate.get("params"))
    if candidate.get(key) not in _EMPTY_VALUES:
        return candidate.get(key)
    if params.get(key) not in _EMPTY_VALUES:
        return params.get(key)
    return None


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in _EMPTY_VALUES:
            return value
    return None


def _sanitize_metric_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_metric_dict(value)
    if isinstance(value, list):
        return [_sanitize_metric_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_metric_value(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return value
        return parsed if math.isfinite(parsed) else 0.0
    return value


def _sanitize_metric_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _sanitize_metric_value(item) for key, item in dict(value or {}).items()}


def _normalize_direction(value: Any) -> Optional[str]:
    token = _string(value).lower().replace("-", "_").replace(" ", "_")
    if not token:
        return None
    if token in _DIRECTION_ALIASES:
        return _DIRECTION_ALIASES[token]
    if any(part in token for part in ("short", "bear", "sell", "risk_off", "negative")):
        return "down"
    if any(part in token for part in ("neutral", "hedge", "sideways")):
        return "neutral"
    if any(part in token for part in ("long", "bull", "buy", "repair", "breakout", "positive")):
        return "up"
    return None


def _direction_from_expected_move(value: Any) -> Optional[str]:
    if value in _EMPTY_VALUES:
        return None
    if isinstance(value, Mapping):
        payload = dict(value)
        for key in ("direction", "expected_direction", "bias"):
            direction = _normalize_direction(payload.get(key))
            if direction:
                return direction
        for key in ("return", "return_pct", "pct", "move", "expected_return"):
            if payload.get(key) in _EMPTY_VALUES:
                continue
            numeric = _safe_float(payload.get(key))
            if numeric > 0:
                return "up"
            if numeric < 0:
                return "down"
        return None
    direction = _normalize_direction(value)
    if direction:
        return direction
    text = _string(value)
    try:
        numeric = float(text.rstrip("%"))
    except (TypeError, ValueError):
        return None
    if "%" in text:
        numeric /= 100.0
    if numeric > 0:
        return "up"
    if numeric < 0:
        return "down"
    return "neutral"


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
            if len(digits) < 8:
                return None
            try:
                dt = datetime(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
            except Exception:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _evidence_time_is_post_hoc(evidence_time: Any, prediction_as_of: Any) -> bool:
    ev_ts = _coerce_iso_ts(evidence_time)
    as_of_ts = _coerce_iso_ts(prediction_as_of)
    if not ev_ts or not as_of_ts:
        return False
    try:
        return datetime.fromisoformat(ev_ts) > datetime.fromisoformat(as_of_ts)
    except Exception:
        return False


def _evidence_source_type(raw_source: Any, candidate_source: str) -> str:
    source = _string(raw_source).lower()
    if source in {
        "factor_ic_validated",
        "active_factor_pool",
        "event_catalyst",
        "price_volume_confirmation",
        "fund_flow",
        "regime_context",
        "template_fallback",
    }:
        return source
    if source in {"factor_ic", "factor_research", "factor"}:
        return "factor_ic_validated"
    if source in {"factor_pool", "active_pool"}:
        return "active_factor_pool"
    if source in {"event_driven", "announcement", "news", "sector_catalyst"}:
        return "event_catalyst"
    if source in {"fund_flow", "north_fund", "margin"}:
        return "fund_flow"
    if source in {"fear_greed", "volatility", "macro", "regime"}:
        return "regime_context"
    if source in {"technical", "price_action", "volume", "kline", "ohlcv", "market_microstructure"}:
        return "price_volume_confirmation"
    if source in {"rule_template_contract", "strategy_logic", "quota_fill", "signal_variation", "unknown"}:
        return "template_fallback"
    if candidate_source == "factor_ic":
        return "factor_ic_validated"
    if candidate_source == "factor_pool":
        return "active_factor_pool"
    if candidate_source == "event_driven":
        return "event_catalyst"
    if candidate_source == "fund_flow":
        return "fund_flow"
    if candidate_source in {"fear_greed", "volatility"}:
        return "regime_context"
    return "template_fallback"


def _ic_direction(value: Any, *, strategy_type: str = "") -> Optional[str]:
    ic_value = _safe_float(value)
    if ic_value > 0:
        return "down" if strategy_type in _SHORT_BIAS_TYPES else "up"
    if ic_value < 0:
        return "up" if strategy_type in _SHORT_BIAS_TYPES else "down"
    return None


def _entry_from_existing_evidence(
    evidence: Mapping[str, Any],
    *,
    candidate_source: str,
    strategy_type: str,
    prediction_as_of: Any,
    index: int,
) -> dict[str, Any]:
    payload = dict(evidence or {})
    source_type = _evidence_source_type(payload.get("source_type") or payload.get("source"), candidate_source)
    support_metric = _sanitize_metric_dict(_as_dict(payload.get("support_metric") or payload.get("metrics")))
    direction = (
        _normalize_direction(payload.get("direction"))
        or _direction_from_expected_move(payload.get("expected_move"))
        or _ic_direction(
            _first_non_empty(
                support_metric.get("ic_value"),
                support_metric.get("rank_ic"),
                payload.get("ic_value"),
                payload.get("rank_ic"),
            ),
            strategy_type=strategy_type,
        )
    )
    proxy_only = bool(payload.get("proxy_only")) or source_type in _TEMPLATE_SOURCE_TYPES
    evidence_time = _first_non_empty(
        payload.get("evidence_time"),
        payload.get("occurred_at"),
        payload.get("as_of"),
        support_metric.get("evidence_time"),
    )
    post_hoc = _evidence_time_is_post_hoc(evidence_time, prediction_as_of)
    return {
        "evidence_id": _string(payload.get("evidence_id") or payload.get("id")) or f"ev_existing_{index}",
        "source_type": source_type,
        "direction": direction or "neutral",
        "weight": _evidence_weight(source_type, payload),
        "proxy_only": proxy_only,
        "summary": _string(payload.get("summary") or payload.get("description")) or source_type,
        "support_metric": support_metric,
        "evidence_time": evidence_time,
        "post_hoc": post_hoc,
    }


def _evidence_weight(source_type: str, payload: Mapping[str, Any]) -> float:
    support_metric = _as_dict(payload.get("support_metric") or payload.get("metrics"))
    if source_type == "factor_ic_validated":
        return _clamp(abs(_safe_float(_first_non_empty(support_metric.get("ic_value"), payload.get("ic_value")))) * 5.0, 0.25, 0.8)
    if source_type == "active_factor_pool":
        fitness = abs(_safe_float(_first_non_empty(support_metric.get("fitness"), payload.get("fitness"))))
        ic_value = abs(_safe_float(_first_non_empty(support_metric.get("current_ic"), payload.get("current_ic"))))
        return _clamp(0.25 + fitness * 0.08 + ic_value * 3.0, 0.25, 0.75)
    if source_type == "event_catalyst":
        strength = abs(_safe_float(_first_non_empty(payload.get("strength"), support_metric.get("strength"), 0.5), 0.5))
        return _clamp(0.35 + strength * 0.25, 0.35, 0.75)
    if source_type == "fund_flow":
        return 0.45
    if source_type == "price_volume_confirmation":
        return 0.35
    if source_type == "regime_context":
        return 0.25
    return 0.12


def _factor_research(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return _as_dict(snapshot.get("factor_research"))


def _factor_maps(snapshot: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    artifact = _factor_research(snapshot)
    ranked_factors = [dict(item or {}) for item in _as_list(artifact.get("ranked_factors")) if isinstance(item, Mapping)]
    factor_ic: dict[str, float] = {}
    factor_trend: dict[str, str] = {}
    for item in ranked_factors:
        name = _string(item.get("factor_name") or item.get("name"))
        if not name:
            continue
        factor_ic[name] = _safe_float(item.get("ic_value"))
        factor_trend[name] = _string(item.get("trend")).lower() or "flat"
    if factor_ic:
        return factor_ic, factor_trend
    for item in [
        *[dict(row or {}) for row in _as_list(artifact.get("active_factor_context")) if isinstance(row, Mapping)],
        *[dict(row or {}) for row in _as_list(snapshot.get("active_factor_context")) if isinstance(row, Mapping)],
    ]:
        name = _string(item.get("factor_name") or item.get("name"))
        if not name:
            continue
        factor_ic[name] = _safe_float(
            _first_non_empty(item.get("ic_value"), item.get("current_ic"), item.get("rank_ic"))
        )
        factor_trend[name] = _string(item.get("trend")).lower() or (
            "rising" if factor_ic[name] > 0 else "falling" if factor_ic[name] < 0 else "flat"
        )
    if factor_ic:
        return factor_ic, factor_trend
    return (
        {str(key): _safe_float(value) for key, value in _as_dict(snapshot.get("factor_ic")).items()},
        {str(key): _string(value).lower() or "flat" for key, value in _as_dict(snapshot.get("factor_ic_trend")).items()},
    )


def _active_factor_records(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    artifact = _factor_research(snapshot)
    records: list[dict[str, Any]] = []
    for raw in [
        *_as_list(artifact.get("ranked_factors")),
        *_as_list(artifact.get("active_factor_context")),
        *_as_list(snapshot.get("active_factor_context")),
    ]:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        name = _string(item.get("factor_name") or item.get("name"))
        if not name:
            continue
        ic_value = _safe_float(
            _first_non_empty(item.get("ic_value"), item.get("current_ic"), item.get("rank_ic"))
        )
        records.append(
            {
                "factor_name": name,
                "family": _string(item.get("family")).lower(),
                "ic_value": ic_value,
                "trend": _string(item.get("trend")).lower()
                or ("rising" if ic_value > 0 else "falling" if ic_value < 0 else "flat"),
            }
        )
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        name = str(record.get("factor_name"))
        existing = deduped.get(name)
        if existing is None or abs(_safe_float(record.get("ic_value"))) > abs(_safe_float(existing.get("ic_value"))):
            deduped[name] = record
    return list(deduped.values())


def _inferred_factor_names_for_candidate(
    candidate: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    factor_ic: Mapping[str, float],
) -> list[str]:
    strategy_type = _string(candidate.get("strategy_type")).lower()
    records = _active_factor_records(snapshot)
    if not records and factor_ic:
        records = [
            {"factor_name": name, "family": "", "ic_value": _safe_float(value), "trend": ""}
            for name, value in dict(factor_ic).items()
        ]
    if not records:
        return []

    positive = [item for item in records if _safe_float(item.get("ic_value")) > 0]
    negative = [item for item in records if _safe_float(item.get("ic_value")) < 0]

    def _rank(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: abs(_safe_float(item.get("ic_value"))), reverse=True)

    def _family_or_name_matches(item: Mapping[str, Any], *tokens: str) -> bool:
        text = " ".join([
            _string(item.get("factor_name")).lower(),
            _string(item.get("family")).lower(),
        ])
        return any(token and token in text for token in tokens)

    if strategy_type in {"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout"}:
        preferred = [item for item in positive if _family_or_name_matches(item, "momentum", "trend", "breakout")]
        chosen = _rank(preferred or positive or records)[:1]
    elif strategy_type in {"mean_reversion_short", "margin_divergence"}:
        preferred = [item for item in positive if _family_or_name_matches(item, "momentum", "crowding", "trend")]
        chosen = _rank(preferred or positive or records)[:1]
    elif strategy_type in {"rsi", "gap_fill"}:
        preferred = [item for item in negative if _family_or_name_matches(item, "reversal", "mean", "liquidity", "custom")]
        chosen = _rank(preferred or negative or records)[:1]
    elif strategy_type in {"multi_factor", "fundamental_multi_factor"}:
        chosen = _rank(records)[:3]
    elif strategy_type in {"quality_factor", "value_factor", "growth_factor"}:
        preferred = [
            item
            for item in records
            if _family_or_name_matches(item, "quality", "value", "growth", "liquidity", "custom", "momentum")
        ]
        chosen = _rank(preferred or records)[:1]
    else:
        chosen = _rank(records)[:1]
    return [_string(item.get("factor_name")) for item in chosen if _string(item.get("factor_name"))]


def _selected_factor_names(candidate: Mapping[str, Any]) -> list[str]:
    params = _as_dict(candidate.get("params"))
    names = []
    for value in (
        params.get("factor_name"),
        candidate.get("factor_name"),
        params.get("factor_pool_factor_name"),
        candidate.get("factor_pool_factor_name"),
        _as_dict(candidate.get("factor_pool_metadata")).get("factor_name"),
    ):
        token = _string(value)
        if token:
            names.append(token)
    for mapping in (
        _as_dict(params.get("factor_weights")),
        _as_dict(candidate.get("factor_weights")),
    ):
        names.extend(_string(key) for key in mapping if _string(key))
    return list(dict.fromkeys(names))


def _candidate_source(candidate: Mapping[str, Any]) -> str:
    generation_reason = _as_dict(candidate.get("generation_reason"))
    return _string(generation_reason.get("source") or candidate.get("source")).lower() or "unknown"


def _snapshot_factor_entries(candidate: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    params = _as_dict(candidate.get("params"))
    strategy_type = _string(candidate.get("strategy_type")).lower()
    entries: list[dict[str, Any]] = []
    factor_ic, factor_trend = _factor_maps(snapshot)
    selected_names = _selected_factor_names(candidate)
    if not selected_names:
        selected_names = _inferred_factor_names_for_candidate(candidate, snapshot, factor_ic)

    direct_ic = _first_non_empty(
        params.get("factor_ic"),
        params.get("factor_pool_current_ic"),
        candidate.get("factor_ic"),
        _as_dict(candidate.get("factor_pool_metadata")).get("current_ic"),
    )
    if direct_ic not in _EMPTY_VALUES:
        name = selected_names[0] if selected_names else _string(params.get("factor_name") or "candidate_factor")
        entries.append(
            {
                "evidence_id": f"ev_factor_{len(entries) + 1}",
                "source_type": "factor_ic_validated"
                if _candidate_source(candidate) == "factor_ic"
                else "active_factor_pool",
                "direction": _ic_direction(direct_ic, strategy_type=strategy_type) or "neutral",
                "weight": _clamp(abs(_safe_float(direct_ic)) * 5.0, 0.25, 0.8),
                "proxy_only": False,
                "summary": f"{name} IC={_safe_float(direct_ic):.4f}",
                "support_metric": {
                    "factor_name": name,
                    "ic_value": round(_safe_float(direct_ic), 6),
                    "trend": _string(params.get("factor_ic_trend") or _as_dict(candidate.get("factor_pool_metadata")).get("trend")) or None,
                },
            }
        )

    for name in selected_names:
        if name not in factor_ic:
            continue
        if any(_as_dict(entry.get("support_metric")).get("factor_name") == name for entry in entries):
            continue
        ic_value = _safe_float(factor_ic.get(name))
        entries.append(
            {
                "evidence_id": f"ev_factor_{len(entries) + 1}",
                "source_type": "factor_ic_validated",
                "direction": _ic_direction(ic_value, strategy_type=strategy_type) or "neutral",
                "weight": _clamp(abs(ic_value) * 5.0, 0.25, 0.8),
                "proxy_only": False,
                "summary": f"{name} IC={ic_value:.4f} trend={factor_trend.get(name, 'flat')}",
                "support_metric": {
                    "factor_name": name,
                    "ic_value": round(ic_value, 6),
                    "trend": factor_trend.get(name, "flat"),
                },
            }
        )

    source = _candidate_source(candidate)
    if not entries and source == "factor_ic":
        trigger = _as_dict(candidate.get("trigger_signal"))
        name = _string(trigger.get("factor"))
        ic_value = trigger.get("value")
        if name and ic_value not in _EMPTY_VALUES:
            entries.append(
                {
                    "evidence_id": "ev_factor_trigger",
                    "source_type": "factor_ic_validated",
                    "direction": _ic_direction(ic_value, strategy_type=strategy_type) or "neutral",
                    "weight": _clamp(abs(_safe_float(ic_value)) * 5.0, 0.25, 0.8),
                    "proxy_only": False,
                    "summary": f"{name} trigger IC={_safe_float(ic_value):.4f}",
                    "support_metric": {
                        "factor_name": name,
                        "ic_value": round(_safe_float(ic_value), 6),
                        "trend": _string(trigger.get("trend")) or None,
                    },
                }
            )
    return entries


def _event_entries(candidate: Mapping[str, Any], prediction_as_of: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    event_anchor = _as_dict(_candidate_value(candidate, "event_anchor"))
    event_prefilter = _as_dict(_candidate_value(candidate, "event_prefilter"))
    if not event_anchor:
        event_anchor = _as_dict(event_prefilter.get("event_anchor"))
    trigger = _as_dict(candidate.get("trigger_signal"))
    event_id = _string(
        _first_non_empty(
            event_anchor.get("id"),
            event_prefilter.get("event_id"),
            trigger.get("event_id"),
        )
    )
    source = _string(event_anchor.get("source") or _candidate_source(candidate)).lower()
    if not event_id:
        return entries
    evidence_time = _first_non_empty(
        event_anchor.get("evidence_time"),
        event_anchor.get("occurred_at"),
        event_prefilter.get("evidence_time"),
        event_prefilter.get("occurred_at"),
        trigger.get("evidence_time"),
    )
    strength = _safe_float(_first_non_empty(event_anchor.get("strength"), event_prefilter.get("anchor_strength"), 0.5), 0.5)
    entries.append(
        {
            "evidence_id": event_id or "ev_event_anchor",
            "source_type": "event_catalyst",
            "direction": _normalize_direction(event_anchor.get("direction") or event_prefilter.get("direction")) or "neutral",
            "weight": _clamp(0.35 + strength * 0.25, 0.35, 0.75),
            "proxy_only": False,
            "summary": _string(event_prefilter.get("evidence_summary")) or f"event catalyst {event_id or source}",
            "support_metric": {
                "event_id": event_id or None,
                "theme_code": _first_non_empty(event_anchor.get("theme_code"), event_prefilter.get("theme_code")),
                "source": source,
                "strength": strength,
            },
            "evidence_time": evidence_time,
            "post_hoc": _evidence_time_is_post_hoc(evidence_time, prediction_as_of),
        }
    )
    return entries


def _fund_flow_entries(candidate: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    source = _candidate_source(candidate)
    trigger = _as_dict(candidate.get("trigger_signal"))
    entries: list[dict[str, Any]] = []
    north_3d = _safe_float(_first_non_empty(trigger.get("value") if trigger.get("field") == "north_fund_3d_net" else None, snapshot.get("north_fund_3d_net")))
    margin_5d = _safe_float(_first_non_empty(trigger.get("value") if trigger.get("field") == "margin_5d_change_pct" else None, snapshot.get("margin_5d_change_pct")))
    if source == "fund_flow" or abs(north_3d) >= 5_000_000_000:
        if abs(north_3d) >= 5_000_000_000:
            direction = "up" if north_3d > 0 else "down"
            entries.append(
                {
                    "evidence_id": "ev_fund_flow_north",
                    "source_type": "fund_flow",
                    "direction": direction,
                    "weight": 0.45,
                    "proxy_only": False,
                    "summary": f"north_fund_3d_net={north_3d:.0f}",
                    "support_metric": {"north_fund_3d_net": north_3d},
                }
            )
    if source == "fund_flow" or abs(margin_5d) >= 2.0:
        if abs(margin_5d) >= 2.0:
            direction = "up" if margin_5d > 0 else "down"
            entries.append(
                {
                    "evidence_id": "ev_fund_flow_margin",
                    "source_type": "fund_flow",
                    "direction": direction,
                    "weight": 0.35,
                    "proxy_only": False,
                    "summary": f"margin_5d_change_pct={margin_5d:.2f}",
                    "support_metric": {"margin_5d_change_pct": margin_5d},
                }
            )
    return entries


def _regime_entries(candidate: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    source = _candidate_source(candidate)
    if source not in {"fear_greed", "volatility", "quota_fill", "signal_variation"}:
        return []
    fear_greed = _safe_float(snapshot.get("fear_greed_index"), 50.0)
    volatility = _safe_float(_as_dict(snapshot.get("fg_components")).get("volatility"), 50.0)
    if fear_greed <= 35:
        direction = "neutral"
    elif fear_greed >= 70:
        direction = "up"
    elif volatility >= 65:
        direction = "neutral"
    else:
        direction = "neutral"
    return [
        {
            "evidence_id": "ev_regime_context",
            "source_type": "regime_context",
            "direction": direction,
            "weight": 0.22,
            "proxy_only": source in {"quota_fill", "signal_variation"},
            "summary": f"fear_greed={fear_greed:.1f}, volatility={volatility:.1f}",
            "support_metric": {"fear_greed_index": fear_greed, "volatility": volatility},
        }
    ]


def _template_entry(candidate: Mapping[str, Any]) -> dict[str, Any]:
    source = _candidate_source(candidate)
    strategy_type = _string(candidate.get("strategy_type")).lower()
    return {
        "evidence_id": "ev_template_fallback",
        "source_type": "template_fallback",
        "direction": "neutral",
        "weight": 0.1,
        "proxy_only": True,
        "summary": f"{strategy_type or 'strategy'} template fallback",
        "support_metric": {
            "source": source,
            "strategy_type": strategy_type,
            "template_entry_bias": _as_dict(_candidate_value(candidate, "trade_plan")).get("entry_bias"),
        },
    }
