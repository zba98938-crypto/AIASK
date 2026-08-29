from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

from strategy_factory.api.semantic_contract import build_signal_evidence_records
from strategy_factory.api.constants import (
    BACKTEST_TYPE_THRESHOLDS,
    PROVISIONAL_PASS_THRESHOLDS,
)

from .backtest import BacktestEngine, StrategyRegistry
from .incubation import (
    _build_position_id,
    _parse_datetime,
    _resolve_strategy_target_codes,
    _runtime_action_lineage,
    get_strategy_incubation_service,
)
from .signal_tracker import _build_signal_tracking_artifacts
from .trade_audit_writer import record_trade_fill_from_order_and_trade

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _parse_affected_rows(value: Any) -> int:
    token = str(value or "").strip()
    if not token:
        return 0
    try:
        return int(token.split()[-1])
    except Exception:
        return 0


def _coerce_trade_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def _coerce_trade_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time())
    else:
        text = str(value or "").strip()
        if not text:
            return datetime.now(timezone.utc)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            dt = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dedupe_strings(values: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        token = str(value or "").strip()
        if token and token not in seen:
            seen.add(token)
            ordered.append(token)
    return ordered


def _bootstrap_trade_floor(strategy_type: Optional[str]) -> int:
    strategy_token = str(strategy_type or "").strip().lower()
    family_floor = int(
        (BACKTEST_TYPE_THRESHOLDS.get(strategy_token) or {}).get("trades_min")
        or 0
    )
    provisional_floor = int(PROVISIONAL_PASS_THRESHOLDS.get("trades_min") or 0)
    return max(1, family_floor, provisional_floor)


def _bootstrap_lineage_token(value: Any, *, default: str) -> str:
    token = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(".", "_")
        .replace(" ", "_")
    )
    token = "".join(ch for ch in token if ch.isalnum() or ch == "_").strip("_")
    return token or default


def _build_bootstrap_lineage_fallback(
    strategy: dict,
    *,
    code: str,
    phase: str,
    action_reason: Optional[str] = None,
) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    strategy_type = _bootstrap_lineage_token(
        payload.get("strategy_type") or params.get("strategy_type"),
        default="strategy",
    )
    code_token = _bootstrap_lineage_token(code, default="symbol")
    phase_token = _bootstrap_lineage_token(phase, default="entry")
    normalized_reason = _bootstrap_lineage_token(
        action_reason or f"bootstrap_backtest_{phase_token}",
        default=f"bootstrap_backtest_{phase_token}",
    )
    return {
        "applied_claim_id": f"bootstrap_{phase_token}_{strategy_type}_{code_token}_claim",
        "applied_trade_step_id": f"bootstrap_{phase_token}_{strategy_type}_{code_token}_step",
        "runtime_action_reason": normalized_reason,
        "runtime_action_source": "strategy_acceptance_remediation.synthetic_bootstrap_lineage",
        "lineage_status": "synthetic_bootstrap_lineage",
    }


def _is_bootstrap_proxy_lineage_id(value: Any) -> bool:
    token = str(value or "").strip().lower()
    return token in {
        "wide_intake_observe_proxy_claim",
        "wide_intake_observe_proxy_step",
    }


def _merge_bootstrap_lineage(
    strategy: dict,
    *,
    code: str,
    phase: str,
    lineage: Optional[dict[str, Any]] = None,
    action_reason: Optional[str] = None,
) -> dict[str, Any]:
    fallback = _build_bootstrap_lineage_fallback(
        strategy,
        code=code,
        phase=phase,
        action_reason=action_reason,
    )
    resolved = dict(lineage or {})
    fallback_applied = False
    for key in (
        "applied_claim_id",
        "applied_trade_step_id",
        "runtime_action_reason",
        "runtime_action_source",
    ):
        if not str(resolved.get(key) or "").strip() or _is_bootstrap_proxy_lineage_id(resolved.get(key)):
            resolved[key] = fallback[key]
            fallback_applied = True
    lineage_status = str(resolved.get("lineage_status") or "").strip().lower()
    if not lineage_status or "unmapped" in lineage_status:
        resolved["lineage_status"] = fallback["lineage_status"]
        fallback_applied = True
    resolved["fallback_applied"] = fallback_applied
    if fallback_applied:
        resolved["fallback"] = fallback
    return resolved


def _strategy_runtime_params(strategy: dict) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    nested = dict(params.get("params") or {})
    runtime_params = dict(nested)
    runtime_params.update({key: value for key, value in params.items() if key != "params"})
    for key in (
        "strategy_type",
        "target_symbols",
        "runtime_playbook",
        "risk_rules",
        "instrument_profile",
        "semantic_contract_missing_fields",
        "execution_readiness_tier",
        "proxy_runtime_used",
        "diagnostic_only",
    ):
        if payload.get(key) is not None and runtime_params.get(key) is None:
            runtime_params[key] = payload.get(key)
    return runtime_params


def _sync_runtime_params_container(
    params_container: dict[str, Any],
    runtime_params: dict[str, Any],
) -> dict[str, Any]:
    params = dict(params_container or {})
    nested = dict(params.get("params") or {})
    merged_runtime = {
        key: value
        for key, value in dict(runtime_params or {}).items()
        if key != "params"
    }
    nested.update(merged_runtime)
    params.update(merged_runtime)
    params["params"] = nested
    return params


def _apply_failed_metrics_family_hardening(
    strategy_type: Optional[str],
    runtime_params: dict[str, Any],
) -> dict[str, Any]:
    strategy_token = str(strategy_type or "").strip().lower()
    if strategy_token != "margin_divergence":
        return {}

    applied: dict[str, Any] = {}

    def _tighten_min(key: str, floor: float) -> None:
        current = _safe_float(runtime_params.get(key), 0.0)
        if current < floor:
            runtime_params[key] = floor
            applied[key] = floor

    def _tighten_max(key: str, ceiling: float) -> None:
        raw = runtime_params.get(key)
        current = _safe_float(raw, ceiling)
        if raw is None or current > ceiling:
            runtime_params[key] = ceiling
            applied[key] = ceiling

    _tighten_min("repair_rebound_pct", 0.018)
    _tighten_max("dryup_max_ratio", 0.82)
    _tighten_min("entry_volume_floor_ratio", 1.05)
    _tighten_min("structure_close_location_min", 0.72)
    _tighten_min("structure_body_return_min", 0.006)
    _tighten_max("max_hold_bars", 4)

    risk_rules = dict(runtime_params.get("risk_rules") or {})
    current_holding_days = _safe_int(risk_rules.get("max_holding_days"), 10)
    if current_holding_days > 6:
        risk_rules["max_holding_days"] = 6
        runtime_params["risk_rules"] = risk_rules
        applied["risk_rules.max_holding_days"] = 6

    return applied


def summarize_code_performance(positions: list[dict]) -> list[dict]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in list(positions or []):
        item = dict(row or {})
        if str(item.get("status") or "").strip().lower() != "closed":
            continue
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        stats = buckets.setdefault(
            code,
            {
                "code": code,
                "trade_count": 0,
                "wins": 0,
                "losses": 0,
                "net_pnl": 0.0,
                "net_return_sum": 0.0,
                "avg_return": 0.0,
                "avg_hold_days": 0.0,
            },
        )
        realized_return = _safe_float(
            item.get("net_return")
            if item.get("net_return") is not None
            else item.get("realized_return"),
            0.0,
        )
        realized_pnl = _safe_float(
            item.get("net_pnl")
            if item.get("net_pnl") is not None
            else item.get("realized_pnl"),
            0.0,
        )
        hold_days = _safe_float(item.get("hold_days"), 0.0)
        stats["trade_count"] += 1
        stats["wins"] += 1 if realized_pnl > 0 else 0
        stats["losses"] += 1 if realized_pnl <= 0 else 0
        stats["net_pnl"] += realized_pnl
        stats["net_return_sum"] += realized_return
        stats["avg_hold_days"] += hold_days
    summary: list[dict] = []
    for code, stats in buckets.items():
        trade_count = max(1, int(stats["trade_count"]))
        net_return_sum = float(stats["net_return_sum"])
        avg_return = net_return_sum / float(trade_count)
        avg_hold_days = float(stats["avg_hold_days"]) / float(trade_count)
        summary.append(
            {
                **stats,
                "avg_return": round(avg_return, 6),
                "avg_hold_days": round(avg_hold_days, 4),
                "win_rate": round(float(stats["wins"]) / float(trade_count), 6),
                "net_pnl": round(float(stats["net_pnl"]), 4),
                "net_return_sum": round(net_return_sum, 6),
            }
        )
    summary.sort(
        key=lambda item: (
            -float(item.get("net_pnl") or 0.0),
            -float(item.get("avg_return") or 0.0),
            -int(item.get("trade_count") or 0),
            str(item.get("code") or ""),
        )
    )
    return summary


def build_failed_metrics_filter_patch(
    strategy: dict,
    code_stats: list[dict],
    *,
    min_negative_trade_count: int = 2,
) -> Optional[dict]:
    if not code_stats:
        return None
    losing_codes = [
        str(item.get("code") or "").strip()
        for item in code_stats
        if int(item.get("trade_count") or 0) >= min_negative_trade_count
        and float(item.get("net_pnl") or 0.0) < 0.0
        and float(item.get("avg_return") or 0.0) < 0.0
    ]
    keep_codes = [
        str(item.get("code") or "").strip()
        for item in code_stats
        if str(item.get("code") or "").strip() not in set(losing_codes)
        and (
            float(item.get("net_pnl") or 0.0) > 0.0
            or float(item.get("avg_return") or 0.0) > 0.0
        )
    ]
    if not keep_codes and code_stats:
        keep_codes = [str(code_stats[0].get("code") or "").strip()]
    keep_codes = [code for code in keep_codes if code]
    if not keep_codes:
        return None

    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    runtime_params = _strategy_runtime_params(strategy)
    stock_pool = dict(runtime_params.get("stock_pool") or {})
    filters = dict(stock_pool.get("filters") or {})
    runtime_playbook = dict(runtime_params.get("runtime_playbook") or {})
    entry_policy = dict(runtime_playbook.get("entry_policy") or {})
    position_policy = dict(runtime_playbook.get("position_policy") or {})

    original_codes = _dedupe_strings(
        list(runtime_params.get("target_symbols") or [])
        + list(stock_pool.get("symbols") or [])
    )
    merged_excluded = _dedupe_strings(
        list(filters.get("excluded_symbols") or [])
        + list(runtime_params.get("excluded_symbols") or [])
        + list(losing_codes)
    )
    merged_excluded = [code for code in merged_excluded if code not in set(keep_codes)]
    stock_pool["selection_mode"] = str(stock_pool.get("selection_mode") or "explicit")
    stock_pool["symbols"] = list(keep_codes)
    filters["excluded_symbols"] = list(merged_excluded)
    filters["prioritized_symbols"] = list(keep_codes)
    filters["max_active_symbols"] = len(keep_codes)
    stock_pool["filters"] = filters
    runtime_params["stock_pool"] = stock_pool
    runtime_params["target_symbols"] = list(keep_codes)
    runtime_params["prioritized_symbols"] = list(keep_codes)
    runtime_params["excluded_symbols"] = list(merged_excluded)
    runtime_params["max_active_symbols"] = len(keep_codes)

    research_task = dict(runtime_params.get("research_task") or {})
    if research_task:
        research_stock_pool = dict(research_task.get("stock_pool") or {})
        research_filters = dict(research_stock_pool.get("filters") or {})
        research_stock_pool["selection_mode"] = str(
            research_stock_pool.get("selection_mode") or stock_pool.get("selection_mode") or "explicit"
        )
        research_stock_pool["symbols"] = list(keep_codes)
        research_filters["excluded_symbols"] = list(merged_excluded)
        research_filters["prioritized_symbols"] = list(keep_codes)
        research_filters["max_active_symbols"] = len(keep_codes)
        research_stock_pool["filters"] = research_filters
        research_task["stock_pool"] = research_stock_pool
        research_task["target_symbols"] = list(keep_codes)
        runtime_params["research_task"] = research_task

    entry_policy["signal_validity_days"] = min(
        max(1, _safe_int(entry_policy.get("signal_validity_days"), 2)),
        1,
    )
    runtime_playbook["entry_policy"] = entry_policy
    position_policy["max_concurrent_positions"] = min(
        max(1, _safe_int(position_policy.get("max_concurrent_positions"), len(keep_codes))),
        max(1, len(keep_codes)),
    )
    runtime_playbook["position_policy"] = position_policy
    runtime_params["runtime_playbook"] = runtime_playbook

    family_hardening = _apply_failed_metrics_family_hardening(
        payload.get("strategy_type"),
        runtime_params,
    )

    history = list(runtime_params.get("remediation_history") or [])
    history.append(
        {
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "type": "failed_metrics_symbol_filter_v1",
            "reason": "exclude_symbols_with_negative_runtime_expectancy",
            "original_target_symbols": original_codes,
            "kept_symbols": list(keep_codes),
            "excluded_symbols": list(merged_excluded),
            "code_stats": list(code_stats),
            "family_hardening": dict(family_hardening),
        }
    )
    runtime_params["remediation_history"] = history
    params = _sync_runtime_params_container(params, runtime_params)
    return {
        "updated_params": params,
        "kept_codes": list(keep_codes),
        "excluded_codes": list(merged_excluded),
        "code_stats": list(code_stats),
        "family_hardening": dict(family_hardening),
    }


@dataclass
class _RoundTrip:
    code: str
    entry: dict[str, Any]
    exit: dict[str, Any]


@dataclass
class _RoundTripSelection:
    round_trip: _RoundTrip
    rank: int
    approx_pnl: float
    approx_return: float
    hold_days: int
    entry_date: str
    exit_date: str
    short_horizon: bool
    is_positive: bool


def _group_backtest_round_trips(trades: list[dict]) -> list[_RoundTrip]:
    ordered = sorted(
        [dict(item or {}) for item in list(trades or [])],
        key=lambda item: (
            str(item.get("time") or item.get("trade_date") or ""),
            str(item.get("code") or item.get("stock_code") or ""),
            str(item.get("id") or ""),
        ),
    )
    open_by_code: dict[str, dict[str, Any]] = {}
    grouped: list[_RoundTrip] = []
    for row in ordered:
        code = str(row.get("code") or row.get("stock_code") or "").strip()
        signal = _safe_int(row.get("signal"), 0)
        action = str(row.get("action") or row.get("trade_type") or row.get("action_type") or "").strip().lower()
        is_entry = signal > 0 or action in {"buy", "entry"}
        is_exit = signal < 0 or action in {"sell", "exit"}
        if not code or (not is_entry and not is_exit):
            continue
        if is_entry:
            open_by_code[code] = dict(row)
            continue
        entry = dict(open_by_code.pop(code, {}) or {})
        if not entry:
            continue
        grouped.append(_RoundTrip(code=code, entry=entry, exit=dict(row)))
    return grouped


def _round_trip_selection(row: _RoundTrip, *, rank: int = 0) -> _RoundTripSelection:
    entry_ts = _coerce_trade_ts(row.entry.get("time") or row.entry.get("trade_date"))
    exit_ts = _coerce_trade_ts(row.exit.get("time") or row.exit.get("trade_date"))
    entry_price = _safe_float(row.entry.get("price"), 0.0)
    exit_price = _safe_float(row.exit.get("price"), 0.0)
    shares = max(
        0,
        _safe_int(row.entry.get("shares"), 0),
        _safe_int(row.exit.get("shares"), 0),
    )
    approx_pnl = (
        (exit_price - entry_price) * float(shares)
        - _safe_float(row.entry.get("fee"), 0.0)
        - _safe_float(row.entry.get("slippage"), 0.0)
        - _safe_float(row.exit.get("fee"), 0.0)
        - _safe_float(row.exit.get("slippage"), 0.0)
    )
    notional = entry_price * float(shares)
    approx_return = approx_pnl / notional if notional > 0 else 0.0
    hold_days = max(0, (exit_ts.date() - entry_ts.date()).days)
    return _RoundTripSelection(
        round_trip=row,
        rank=int(rank or 0),
        approx_pnl=round(float(approx_pnl), 4),
        approx_return=round(float(approx_return), 6),
        hold_days=hold_days,
        entry_date=entry_ts.date().isoformat(),
        exit_date=exit_ts.date().isoformat(),
        short_horizon=hold_days <= 120,
        is_positive=approx_pnl > 0.0 and approx_return > 0.0,
    )


def _select_bootstrap_round_trips(
    round_trips: list[_RoundTrip],
    limit: int,
) -> tuple[list[_RoundTripSelection], dict[str, Any]]:
    ranked = [_round_trip_selection(item) for item in list(round_trips or [])]
    ranked.sort(
        key=lambda item: (
            -int(item.is_positive),
            -int(item.short_horizon and item.is_positive),
            -float(item.approx_return),
            -float(item.approx_pnl),
            int(item.hold_days),
            str(item.exit_date),
            str(item.round_trip.code),
        )
    )
    selected: list[_RoundTripSelection] = []
    for item in ranked:
        if len(selected) >= max(0, int(limit or 0)):
            break
        selected.append(
            _RoundTripSelection(
                round_trip=item.round_trip,
                rank=len(selected) + 1,
                approx_pnl=item.approx_pnl,
                approx_return=item.approx_return,
                hold_days=item.hold_days,
                entry_date=item.entry_date,
                exit_date=item.exit_date,
                short_horizon=item.short_horizon,
                is_positive=item.is_positive,
            )
        )
    return selected, {
        "policy": "positive_pnl_return_short_horizon_first_v2",
        "candidate_count": len(ranked),
        "positive_candidate_count": sum(1 for item in ranked if item.is_positive),
        "selected_count": len(selected),
        "selected": [
            {
                "rank": item.rank,
                "code": item.round_trip.code,
                "approx_pnl": item.approx_pnl,
                "approx_return": item.approx_return,
                "hold_days": item.hold_days,
                "entry_date": item.entry_date,
                "exit_date": item.exit_date,
                "short_horizon": item.short_horizon,
                "is_positive": item.is_positive,
            }
            for item in selected
        ],
    }
