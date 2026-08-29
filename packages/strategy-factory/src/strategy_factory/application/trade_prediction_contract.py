"""Trade prediction contract helpers.

The existing semantic ``prediction_contract`` explains claims and evidence.
This module adds a sibling contract focused on one frozen, machine-verifiable
trade prediction. P0 intentionally stops at normalization, validation, and
stable hashing; outcome scoring belongs to later phases.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Optional

TRADE_PREDICTION_CONTRACT_VERSION = "strategy_factory.trade_prediction_contract.v1"
TRADE_PREDICTION_CONTRACT_READY = "ready"
TRADE_PREDICTION_CONTRACT_REJECTED = "rejected"
DERIVED_FROM_LEGACY_CONTRACT = "derived_from_legacy_contract"
EXPLICIT_CONTRACT = "explicit"

_EMPTY_VALUES = (None, "", [], {})
_DIRECTION_ALIASES = {
    "up": "up",
    "bullish": "up",
    "long": "up",
    "buy": "up",
    "rise": "up",
    "rising": "up",
    "上涨": "up",
    "看多": "up",
    "down": "down",
    "bearish": "down",
    "short": "down",
    "sell": "down",
    "fall": "down",
    "falling": "down",
    "下跌": "down",
    "看空": "down",
    "neutral": "neutral",
    "flat": "neutral",
    "sideways": "neutral",
    "hold": "neutral",
    "震荡": "neutral",
    "中性": "neutral",
}
_REQUIRED_FIELDS = (
    "strategy_id",
    "stock_code",
    "prediction_as_of",
    "target_trading_date",
    "direction",
    "confidence",
    "horizon",
    "evidence_refs",
    "contract_version",
    "contract_source",
)

_PREDICTION_AS_OF_KEYS = (
    "prediction_as_of",
    "as_of",
    "as_of_date",
    "snapshot_date",
    "started_at",
    "created_at",
)


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


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in _EMPTY_VALUES:
            return value
    return None


def _candidate_value(candidate: Mapping[str, Any], key: str) -> Any:
    params = _as_dict(candidate.get("params"))
    if candidate.get(key) not in _EMPTY_VALUES:
        return candidate.get(key)
    if params.get(key) not in _EMPTY_VALUES:
        return params.get(key)
    return None


def _explicit_contract_value(candidate: Mapping[str, Any], key: str) -> Any:
    params = _as_dict(candidate.get("params"))
    for value in (candidate.get("trade_prediction_contract"), params.get("trade_prediction_contract")):
        contract = _as_dict(value)
        if contract.get(key) not in _EMPTY_VALUES:
            return contract.get(key)
    return None


def _prediction_context_as_of(candidate: Mapping[str, Any], snapshot: Optional[Mapping[str, Any]]) -> Any:
    params = _as_dict(candidate.get("params"))
    research_task = _as_dict(_candidate_value(candidate, "research_task"))
    for key in _PREDICTION_AS_OF_KEYS:
        value = _first_non_empty(
            candidate.get(key),
            params.get(key),
            _explicit_contract_value(candidate, key),
            research_task.get(key),
        )
        if value not in _EMPTY_VALUES:
            return value
    snap = _as_dict(snapshot)
    for key in (*_PREDICTION_AS_OF_KEYS, "date"):
        value = snap.get(key)
        if value not in _EMPTY_VALUES:
            return value
    return None


def attach_trade_prediction_context(
    candidate: Mapping[str, Any],
    *,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Attach the cycle prediction timestamp used by contract derivation."""

    payload = dict(candidate or {})
    as_of = _prediction_context_as_of(payload, snapshot)
    if as_of in _EMPTY_VALUES:
        return payload
    params = _as_dict(payload.get("params"))
    if params.get("prediction_as_of") in _EMPTY_VALUES:
        params["prediction_as_of"] = as_of
    snap = _as_dict(snapshot)
    snapshot_date = _first_non_empty(snap.get("date"), snap.get("snapshot_date"), params.get("snapshot_date"))
    if snapshot_date not in _EMPTY_VALUES and params.get("snapshot_date") in _EMPTY_VALUES:
        params["snapshot_date"] = snapshot_date
    payload["params"] = params
    if payload.get("prediction_as_of") in _EMPTY_VALUES:
        payload["prediction_as_of"] = as_of
    if snapshot_date not in _EMPTY_VALUES and payload.get("snapshot_date") in _EMPTY_VALUES:
        payload["snapshot_date"] = snapshot_date
    return payload


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
        normalized = text.replace("Z", "+00:00")
        parsers = (
            lambda item: datetime.fromisoformat(item),
            lambda item: datetime.combine(date.fromisoformat(item[:10]), time.min),
        )
        dt = None
        for parser in parsers:
            try:
                dt = parser(normalized)
                break
            except Exception:
                continue
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


def _coerce_iso_date(value: Any) -> Optional[str]:
    if value in _EMPTY_VALUES:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _string(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except Exception:
        pass
    ts = _coerce_iso_ts(text)
    return ts[:10] if ts else None


def _add_business_days(start_date: date, days: int) -> date:
    resolved = start_date
    remaining = max(0, int(days or 0))
    while remaining > 0:
        resolved = resolved + timedelta(days=1)
        if resolved.weekday() < 5:
            remaining -= 1
    return resolved


def _horizon_business_days(value: Any) -> Optional[int]:
    token = _string(value).lower().replace("-", "_").replace(" ", "_")
    if not token:
        return None
    aliases = {
        "next_day": 1,
        "next_trading_day": 1,
        "next_session": 1,
        "t+1": 1,
        "t1": 1,
        "1d": 1,
        "1_day": 1,
        "one_day": 1,
        "daily": 1,
        "2d": 2,
        "2_day": 2,
        "3d": 3,
        "3_day": 3,
        "5d": 5,
        "5_day": 5,
    }
    if token in aliases:
        return aliases[token]
    if token.isdigit():
        try:
            return max(1, min(20, int(token)))
        except Exception:
            return None
    digits = "".join(ch for ch in token if ch.isdigit())
    if digits and token.endswith(("d", "day", "days")):
        try:
            return max(1, min(20, int(digits)))
        except Exception:
            return None
    return None


def _normalize_horizon_label(value: Any) -> Optional[str]:
    token = _string(value)
    if not token:
        return None
    days = _horizon_business_days(token)
    if days is not None and token.lower().replace("-", "_").replace(" ", "_").isdigit():
        return f"{days}d"
    return token


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value in _EMPTY_VALUES:
            return None
        return int(float(value))
    except Exception:
        return None


def _horizon_from_window(*values: Any) -> Optional[str]:
    for value in values:
        if value in _EMPTY_VALUES:
            continue
        if isinstance(value, Mapping):
            payload = dict(value or {})
            direct = _first_non_empty(
                payload.get("horizon"),
                payload.get("label"),
                payload.get("expected_horizon"),
                payload.get("primary_horizon"),
            )
            if direct not in _EMPTY_VALUES:
                return _normalize_horizon_label(direct)
            days = _safe_int(
                _first_non_empty(
                    payload.get("min_days"),
                    payload.get("target_days"),
                    payload.get("primary_horizon_days"),
                    payload.get("max_days"),
                    payload.get("alpha_half_life"),
                )
            )
            if days and days > 0:
                return f"{max(1, min(20, days))}d"
            continue
        token = _string(value)
        if token:
            return _normalize_horizon_label(token)
    return None


def _derive_target_date_from_horizon(prediction_as_of: Any, horizon: Any) -> Optional[str]:
    resolved_as_of = _coerce_iso_ts(prediction_as_of)
    days = _horizon_business_days(horizon)
    if not resolved_as_of or not days:
        return None
    try:
        base = datetime.fromisoformat(resolved_as_of.replace("Z", "+00:00")).date()
    except Exception:
        return None
    return _add_business_days(base, days).isoformat()


def _coerce_stock_code(value: Any) -> Optional[str]:
    token = _string(value).upper()
    if not token:
        return None
    if "." in token:
        return token
    digits = "".join(ch for ch in token if ch.isdigit())
    if len(digits) == 6:
        suffix = "SH" if digits.startswith(("5", "6", "9")) else "SZ"
        return f"{digits}.{suffix}"
    return token


def _first_stock_code(candidate: Mapping[str, Any], contract: Mapping[str, Any]) -> Optional[str]:
    direct = _first_non_empty(
        contract.get("stock_code"),
        contract.get("code"),
        contract.get("symbol"),
        _candidate_value(candidate, "stock_code"),
        _candidate_value(candidate, "code"),
        _candidate_value(candidate, "symbol"),
    )
    if direct not in _EMPTY_VALUES:
        return _coerce_stock_code(direct)
    for key in ("target_symbols", "symbols", "codes", "stock_codes"):
        values = _as_list(_candidate_value(candidate, key))
        if values:
            return _coerce_stock_code(values[0])
    stock_pool = _as_dict(_candidate_value(candidate, "stock_pool"))
    for key in ("symbols", "target_symbols", "codes", "stock_codes"):
        values = _as_list(stock_pool.get(key))
        if values:
            return _coerce_stock_code(values[0])
    return None


def _normalize_direction(value: Any) -> Optional[str]:
    token = _string(value).lower()
    if not token:
        return None
    return _DIRECTION_ALIASES.get(token)


def _direction_from_expected_move(value: Any) -> Optional[str]:
    if value in _EMPTY_VALUES:
        return None
    if isinstance(value, Mapping):
        payload = dict(value or {})
        for key in ("direction", "expected_direction", "bias"):
            resolved = _normalize_direction(payload.get(key))
            if resolved:
                return resolved
        for key in ("return", "return_pct", "pct", "move", "expected_return"):
            try:
                numeric = float(payload.get(key))
            except Exception:
                continue
            if numeric > 0:
                return "up"
            if numeric < 0:
                return "down"
        return None
    text = _string(value)
    resolved = _normalize_direction(text)
    if resolved:
        return resolved
    try:
        numeric = float(text.rstrip("%"))
        if "%" in text:
            numeric = numeric / 100.0
    except Exception:
        return None
    if numeric > 0:
        return "up"
    if numeric < 0:
        return "down"
    return "neutral"


def _direction_from_bias(value: Any) -> Optional[str]:
    token = _string(value).lower().replace("-", "_").replace(" ", "_")
    if not token:
        return None
    direct = _normalize_direction(token)
    if direct:
        return direct
    if any(part in token for part in ("short", "bear", "down", "risk_off")):
        return "down"
    if any(part in token for part in ("neutral", "hedge", "market_neutral")):
        return "neutral"
    if any(
        part in token
        for part in (
            "long",
            "bull",
            "trend_follow",
            "mean_reversion",
            "repair",
            "breakout",
            "rank",
            "quality",
            "growth",
            "value",
            "defensive",
        )
    ):
        return "up"
    return None


def _coerce_confidence(value: Any) -> Optional[float]:
    try:
        if value in _EMPTY_VALUES:
            return None
        resolved = float(value)
    except Exception:
        return None
    if resolved < 0.0 or resolved > 1.0:
        return None
    return round(resolved, 6)


def _confidence_from_contract(value: Any) -> Optional[float]:
    payload = _as_dict(value)
    if not payload:
        return None
    prediction_quality = _as_dict(
        payload.get("prediction_quality")
        or payload.get("probability_quality")
        or payload.get("quality")
    )
    return _coerce_confidence(
        _first_non_empty(
            payload.get("confidence"),
            payload.get("probability"),
            payload.get("calibrated_probability"),
            payload.get("raw_probability"),
            prediction_quality.get("calibrated_probability"),
            prediction_quality.get("raw_probability"),
            prediction_quality.get("probability"),
        )
    )


def _normalize_evidence_refs(value: Any) -> list[Any]:
    refs: list[Any] = []
    seen: set[str] = set()
    for item in _as_list(value):
        ref: Any
        if isinstance(item, Mapping):
            payload = {str(key): val for key, val in dict(item).items() if val not in _EMPTY_VALUES}
            ref_id = _first_non_empty(payload.get("id"), payload.get("evidence_id"), payload.get("event_id"))
            if ref_id not in _EMPTY_VALUES:
                payload.setdefault("id", _string(ref_id))
            ref = payload
        else:
            token = _string(item)
            if not token:
                continue
            ref = token
        marker = json.dumps(ref, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        refs.append(ref)
    return refs


def _derive_evidence_refs(candidate: Mapping[str, Any]) -> list[Any]:
    refs: list[Any] = []
    prediction_contract = _as_dict(_candidate_value(candidate, "prediction_contract"))
    for claim in _as_list(prediction_contract.get("claims")):
        claim_payload = _as_dict(claim)
        for key in ("evidence_refs", "evidence_ids", "evidences"):
            refs.extend(_as_list(claim_payload.get(key)))
    evidence_chain = _as_dict(_candidate_value(candidate, "evidence_chain"))
    for evidence in _as_list(evidence_chain.get("evidences") or evidence_chain.get("evidence")):
        evidence_payload = _as_dict(evidence)
        refs.append(
            _first_non_empty(
                evidence_payload.get("id"),
                evidence_payload.get("evidence_id"),
                evidence_payload.get("source"),
            )
            or evidence
        )
    return _normalize_evidence_refs(refs)


def _extract_explicit_contract(candidate: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    payload = dict(candidate or {})
    params = _as_dict(payload.get("params"))
    for value in (payload.get("trade_prediction_contract"), params.get("trade_prediction_contract")):
        if isinstance(value, Mapping) and value:
            return dict(value)
    contract_markers = {
        "stock_code",
        "target_trading_date",
        "direction",
        "confidence",
        "prediction_as_of",
        "horizon",
    }
    candidate_context_markers = {
        "params",
        "strategy_type",
        "candidate_id",
        "name",
        "target_symbols",
        "stock_pool",
        "research_task",
        "trade_plan",
        "holding_horizon",
        "evidence_chain",
        "prediction_contract",
        "confidence_contract",
        "tags",
        "spawn_reason",
        "market_evidence_pack",
        "alpha_thesis",
        "direction_resolution",
        "confidence_calibration",
    }
    if contract_markers.intersection(payload) and not any(
        payload.get(key) not in _EMPTY_VALUES for key in candidate_context_markers
    ):
        return dict(payload)
    return None


def _has_candidate_context(payload: Mapping[str, Any]) -> bool:
    return any(
        payload.get(key) not in _EMPTY_VALUES
        for key in (
            "params",
            "prediction_contract",
            "evidence_chain",
            "confidence_contract",
            "target_symbols",
            "stock_pool",
            "research_task",
            "trade_plan",
            "holding_horizon",
            "strategy_type",
            "candidate_id",
            "name",
            "market_evidence_pack",
            "alpha_thesis",
            "direction_resolution",
            "confidence_calibration",
        )
    )


def derive_trade_prediction_contract(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Build a minimum contract from legacy semantic fields."""

    payload = dict(candidate or {})
    params = _as_dict(payload.get("params"))
    confidence_contract = _as_dict(_candidate_value(payload, "confidence_contract"))
    holding_horizon = _as_dict(_candidate_value(payload, "holding_horizon"))
    trade_plan = _as_dict(_candidate_value(payload, "trade_plan"))
    prediction_contract = _as_dict(_candidate_value(payload, "prediction_contract"))
    research_task = _as_dict(_candidate_value(payload, "research_task"))
    market_evidence_pack = _as_dict(_candidate_value(payload, "market_evidence_pack"))
    alpha_thesis = _as_dict(_candidate_value(payload, "alpha_thesis"))
    direction_resolution = _as_dict(_candidate_value(payload, "direction_resolution"))
    confidence_calibration = _as_dict(_candidate_value(payload, "confidence_calibration"))
    claims = _as_list(prediction_contract.get("claims"))
    first_claim = _as_dict(claims[0]) if claims else {}
    prediction_as_of = _coerce_iso_ts(
        _first_non_empty(
            payload.get("prediction_as_of"),
            params.get("prediction_as_of"),
            payload.get("as_of"),
            payload.get("as_of_date"),
            params.get("as_of"),
            params.get("as_of_date"),
            payload.get("snapshot_date"),
            params.get("snapshot_date"),
            research_task.get("as_of"),
            research_task.get("as_of_date"),
            research_task.get("snapshot_date"),
            payload.get("started_at"),
            params.get("started_at"),
            payload.get("created_at"),
            params.get("created_at"),
        )
    )
    horizon = _normalize_horizon_label(
        _first_non_empty(
            payload.get("horizon"),
            params.get("horizon"),
            first_claim.get("horizon"),
            first_claim.get("expected_horizon"),
            holding_horizon.get("horizon"),
            holding_horizon.get("label"),
            trade_plan.get("holding_horizon"),
            trade_plan.get("horizon"),
        )
    ) or _horizon_from_window(
        holding_horizon,
        trade_plan.get("holding_window"),
        trade_plan.get("horizon_window"),
        prediction_contract.get("primary_horizon_days"),
        first_claim.get("expected_horizon"),
    )
    explicit_target_date = _coerce_iso_date(
        _first_non_empty(
            payload.get("target_trading_date"),
            params.get("target_trading_date"),
            first_claim.get("target_trading_date"),
            first_claim.get("target_date"),
            trade_plan.get("target_trading_date"),
        )
    )
    direction = _normalize_direction(
        _first_non_empty(
            direction_resolution.get("direction"),
            alpha_thesis.get("direction"),
            prediction_contract.get("direction"),
            prediction_contract.get("expected_direction"),
            payload.get("direction"),
            params.get("direction"),
            first_claim.get("direction"),
            first_claim.get("expected_direction"),
            trade_plan.get("direction"),
            payload.get("direction_bias"),
            params.get("direction_bias"),
            trade_plan.get("entry_bias"),
        )
    ) or _direction_from_expected_move(first_claim.get("expected_move")) or _direction_from_bias(
        _first_non_empty(
            trade_plan.get("entry_bias"),
            payload.get("direction_bias"),
            params.get("direction_bias"),
        )
    )
    confidence = _coerce_confidence(
        _first_non_empty(
            confidence_calibration.get("confidence"),
            alpha_thesis.get("confidence"),
            payload.get("confidence"),
            params.get("confidence"),
            prediction_contract.get("confidence"),
            first_claim.get("confidence"),
            first_claim.get("calibrated_confidence"),
            confidence_contract.get("calibrated_probability"),
            confidence_contract.get("probability"),
        )
    ) or _confidence_from_contract(confidence_contract)
    return {
        "contract_version": TRADE_PREDICTION_CONTRACT_VERSION,
        "contract_source": DERIVED_FROM_LEGACY_CONTRACT,
        "strategy_id": _string(
            _first_non_empty(
                payload.get("strategy_id"),
                payload.get("id"),
                params.get("strategy_id"),
                params.get("id"),
            )
        )
        or None,
        "stock_code": _first_stock_code(payload, {}),
        "prediction_as_of": prediction_as_of,
        "target_trading_date": explicit_target_date or _derive_target_date_from_horizon(prediction_as_of, horizon),
        "direction": direction,
        "confidence": confidence,
        "horizon": horizon,
        "evidence_refs": _derive_evidence_refs(payload),
        "direction_source": _string(
            _first_non_empty(
                direction_resolution.get("direction_source"),
                prediction_contract.get("direction_source"),
            )
        )
        or None,
        "confidence_source": _string(
            _first_non_empty(
                confidence_calibration.get("confidence_source"),
                prediction_contract.get("confidence_source"),
            )
        )
        or None,
        "evidence_quality_score": _coerce_confidence(
            _first_non_empty(
                confidence_calibration.get("evidence_quality_score"),
                prediction_contract.get("evidence_quality_score"),
            )
        ),
        "conflict_count": _safe_int(
            _first_non_empty(
                direction_resolution.get("conflict_count"),
                confidence_calibration.get("conflict_count"),
                prediction_contract.get("conflict_count"),
            )
        ),
        "template_fallback_used": bool(
            _first_non_empty(
                prediction_contract.get("template_fallback_used"),
                alpha_thesis.get("template_fallback_used"),
                market_evidence_pack.get("template_dominance_score"),
            )
        ),
    }


def normalize_trade_prediction_contract(candidate_or_contract: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an explicit contract or derive one from a strategy candidate."""

    payload = dict(candidate_or_contract or {})
    explicit = _extract_explicit_contract(payload)
    candidate_context = _has_candidate_context(payload)
    source = EXPLICIT_CONTRACT if explicit is not None else DERIVED_FROM_LEGACY_CONTRACT
    candidate = payload if candidate_context or explicit is None else {}
    raw = explicit if explicit is not None else derive_trade_prediction_contract(payload)
    contract = dict(raw or {})
    fallback = derive_trade_prediction_contract(payload) if explicit is not None and candidate_context else {}
    for field_name in (
        "stock_code",
        "prediction_as_of",
        "target_trading_date",
        "direction",
        "confidence",
        "horizon",
        "evidence_refs",
        "direction_source",
        "confidence_source",
        "evidence_quality_score",
        "conflict_count",
        "template_fallback_used",
    ):
        if contract.get(field_name) in _EMPTY_VALUES and fallback.get(field_name) not in _EMPTY_VALUES:
            contract[field_name] = fallback.get(field_name)
    contract["contract_version"] = _string(contract.get("contract_version")) or TRADE_PREDICTION_CONTRACT_VERSION
    contract["contract_source"] = _string(contract.get("contract_source")) or source
    contract["strategy_id"] = _string(
        _first_non_empty(
            contract.get("strategy_id"),
            _candidate_value(candidate, "strategy_id"),
            _candidate_value(candidate, "id"),
        )
    ) or None
    contract["stock_code"] = _first_stock_code(candidate, contract)
    contract["prediction_as_of"] = _coerce_iso_ts(contract.get("prediction_as_of"))
    target_trading_date = _coerce_iso_date(contract.get("target_trading_date"))
    if target_trading_date is None and (explicit is None or candidate_context):
        target_trading_date = _derive_target_date_from_horizon(
            contract.get("prediction_as_of"),
            contract.get("horizon"),
        )
    contract["target_trading_date"] = target_trading_date
    contract["direction"] = _normalize_direction(contract.get("direction"))
    contract["confidence"] = _coerce_confidence(contract.get("confidence"))
    contract["horizon"] = _string(contract.get("horizon")) or None
    contract["evidence_refs"] = _normalize_evidence_refs(
        contract.get("evidence_refs") or contract.get("evidence_ids")
    )
    for optional_key in (
        "target_price_range",
        "time_buckets",
        "entry_window",
        "exit_window",
        "risk_plan",
        "metadata",
    ):
        if optional_key in contract and isinstance(contract.get(optional_key), Mapping):
            contract[optional_key] = {
                str(key): value
                for key, value in dict(contract.get(optional_key) or {}).items()
                if value not in _EMPTY_VALUES
            }
    return {key: value for key, value in contract.items() if value not in (None, "")}


def validate_trade_prediction_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_trade_prediction_contract(contract)
    missing_fields = [
        field_name
        for field_name in _REQUIRED_FIELDS
        if normalized.get(field_name) in _EMPTY_VALUES
    ]
    reject_reasons = [f"missing:{field_name}" for field_name in missing_fields]
    if normalized.get("contract_version") != TRADE_PREDICTION_CONTRACT_VERSION:
        reject_reasons.append("invalid:contract_version")
    if normalized.get("direction") not in {"up", "down", "neutral"}:
        reject_reasons.append("invalid:direction")
    if normalized.get("confidence") is None:
        reject_reasons.append("invalid:confidence")
    status = TRADE_PREDICTION_CONTRACT_READY if not reject_reasons else TRADE_PREDICTION_CONTRACT_REJECTED
    return {
        "status": status,
        "contract": normalized,
        "missing_fields": sorted(set(missing_fields)),
        "reject_reasons": list(dict.fromkeys(reject_reasons)),
    }


def stable_trade_prediction_contract_hash(contract: Mapping[str, Any]) -> str:
    frozen = dict(contract or {})
    frozen.pop("contract_hash", None)
    frozen.pop("prediction_id", None)
    serialized = json.dumps(
        frozen,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def freeze_trade_prediction_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    validation = validate_trade_prediction_contract(contract)
    normalized = dict(validation.get("contract") or {})
    contract_hash = (
        stable_trade_prediction_contract_hash(normalized)
        if validation.get("status") == TRADE_PREDICTION_CONTRACT_READY
        else None
    )
    if contract_hash:
        normalized["contract_hash"] = contract_hash
    return {
        **validation,
        "contract": normalized,
        "contract_hash": contract_hash,
    }


def apply_trade_prediction_contract(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return candidate with P0 trade prediction diagnostics attached."""

    payload = dict(candidate or {})
    frozen = freeze_trade_prediction_contract(payload)
    contract = dict(frozen.get("contract") or {})
    params = {
        **_as_dict(payload.get("params")),
        "trade_prediction_contract": contract,
        "trade_prediction_contract_status": frozen.get("status"),
        "trade_prediction_contract_hash": frozen.get("contract_hash"),
        "trade_prediction_contract_missing_fields": list(frozen.get("missing_fields") or []),
        "trade_prediction_contract_reject_reasons": list(frozen.get("reject_reasons") or []),
    }
    return {
        **payload,
        "params": params,
        "trade_prediction_contract": contract,
        "trade_prediction_contract_status": frozen.get("status"),
        "trade_prediction_contract_hash": frozen.get("contract_hash"),
        "trade_prediction_contract_missing_fields": list(frozen.get("missing_fields") or []),
        "trade_prediction_contract_reject_reasons": list(frozen.get("reject_reasons") or []),
    }


__all__ = [
    "attach_trade_prediction_context",
    "DERIVED_FROM_LEGACY_CONTRACT",
    "EXPLICIT_CONTRACT",
    "TRADE_PREDICTION_CONTRACT_READY",
    "TRADE_PREDICTION_CONTRACT_REJECTED",
    "TRADE_PREDICTION_CONTRACT_VERSION",
    "apply_trade_prediction_contract",
    "derive_trade_prediction_contract",
    "freeze_trade_prediction_contract",
    "normalize_trade_prediction_contract",
    "stable_trade_prediction_contract_hash",
    "validate_trade_prediction_contract",
]
