"""SQLite 策略超市 Mixin — 孵化账户 / 孵化指标 / 模拟交易"""


import json
import logging
from datetime import date, datetime
from typing import Any, List, Optional

from aiask_quant_core.storage.trade_audit_writer import aggregate_trade_position

logger = logging.getLogger(__name__)

_EXECUTION_AUDIT_REQUIRED_TABLES = (
    "strategy_candidate_evidence",
    "strategy_signal_evidence",
    "strategy_trade_positions",
    "strategy_trade_position_fills",
)
_EXECUTION_AUDIT_REQUIRED_COLUMNS = {
    "paper_orders": ("signal_id", "position_id"),
    "paper_trades": ("signal_id", "position_id"),
}
_EXECUTION_AUDIT_REQUIRED_MIGRATIONS = (
    "paper_trades_best_effort_position_backfill_v1",
    "strategy_candidate_evidence_native_backfill_v1",
    "strategy_signal_evidence_native_backfill_v1",
    "strategy_trade_positions_roundtrip_backfill_v1",
)


def _safe_rules_dict(value) -> dict:
    """Convert risk_rules-like values to a dict before JSON serialization."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _string(value: Any) -> str:
    return str(value or "").strip()


def _coerce_ts(value: Any) -> Any:
    if value is None or isinstance(value, (datetime, date)):
        return value
    text = _string(value)
    if not text:
        return None
    for parser in (
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")),
        lambda item: datetime.combine(date.fromisoformat(item[:10]), datetime.min.time()),
    ):
        try:
            return parser(text)
        except Exception:
            continue
    return value

from aiask_quant_core.storage.runtime_hooks import (
    get_execution_audit_gate_evaluator,
    get_execution_audit_snapshot_builder,
    get_execution_audit_snapshot_metadata,
    get_signal_evidence_builder,
)


def _fallback_execution_audit_gate(
    audit_summary: Optional[dict[str, Any]],
    *,
    strategy_type: Optional[str] = None,
    bootstrap_trade_floor: Optional[int] = None,
    production_trade_floor: int = 20,
) -> tuple[str, list[str], dict[str, bool], dict[str, float | int | None]]:
    summary = dict(audit_summary or {})
    resolved_bootstrap_floor = max(1, _safe_int(bootstrap_trade_floor, 2))
    resolved_production_floor = max(_safe_int(production_trade_floor, 20), resolved_bootstrap_floor)
    realized_trade_count = _safe_int(
        summary.get("real_paper_round_trip_count")
        or summary.get("real_paper_round_trips")
        or summary.get("realized_trade_count")
    )
    bootstrap_round_trip_count = _safe_int(
        summary.get("bootstrap_round_trip_count")
        or summary.get("bootstrap_round_trips")
    )
    closed_round_trip_count = _safe_int(
        summary.get("closed_round_trip_count")
        or summary.get("total_realized_trade_count")
        or realized_trade_count + bootstrap_round_trip_count
    )
    mapped_position_count = _safe_int(summary.get("mapped_position_count"))
    incomplete_position_count = _safe_int(summary.get("incomplete_position_count"))
    order_count = _safe_int(summary.get("order_count"))
    filled_order_count = _safe_int(summary.get("filled_order_count"))
    trade_count = _safe_int(summary.get("trade_count"))
    nav_observation_days = _safe_int(summary.get("nav_observation_days"))
    evidence_status = _string(summary.get("evidence_status")) or None
    runtime_evidence_present = bool(
        mapped_position_count > 0
        or incomplete_position_count > 0
        or order_count > 0
        or filled_order_count > 0
        or trade_count > 0
        or nav_observation_days > 0
        or evidence_status in {"ready", "empty", "bootstrap_pending"}
        or _string(summary.get("account_id"))
        or _string(summary.get("paper_account_id"))
    )
    trade_expectancy = _safe_float(summary.get("trade_expectancy"))
    pnl_conversion_efficiency = _safe_float(summary.get("pnl_conversion_efficiency"))
    execution_conversion_efficiency = _safe_float(summary.get("execution_conversion_efficiency"))
    metrics = {
        "realized_trade_count": realized_trade_count,
        "real_paper_round_trips": realized_trade_count,
        "bootstrap_round_trips": bootstrap_round_trip_count,
        "closed_round_trips": closed_round_trip_count,
        "trade_expectancy": trade_expectancy,
        "pnl_conversion_efficiency": pnl_conversion_efficiency,
        "execution_conversion_efficiency": execution_conversion_efficiency,
        "bootstrap_trade_floor": resolved_bootstrap_floor,
        "required_trade_count": resolved_production_floor,
    }
    metric_passes = {
        "bootstrap_trade_count": realized_trade_count >= resolved_bootstrap_floor,
        "realized_trade_count": realized_trade_count >= resolved_production_floor,
        "trade_expectancy": trade_expectancy is not None and trade_expectancy > 0.0,
        "pnl_conversion_efficiency": pnl_conversion_efficiency is not None and pnl_conversion_efficiency > 0.0,
        "execution_conversion_efficiency": (
            execution_conversion_efficiency is not None and execution_conversion_efficiency >= 0.20
        ),
    }
    reasons: list[str] = []
    if not summary or (realized_trade_count <= 0 and not runtime_evidence_present):
        return "missing", ["execution_audit_missing"], metric_passes, metrics
    if realized_trade_count <= 0:
        return "bootstrap_pending", ["execution_audit_bootstrap_pending"], metric_passes, metrics
    if realized_trade_count < resolved_bootstrap_floor:
        return "insufficient_samples", [f"realized_trade_count<{resolved_bootstrap_floor}"], metric_passes, metrics
    if not metric_passes["trade_expectancy"]:
        reasons.append("trade_expectancy<=0")
    if not metric_passes["pnl_conversion_efficiency"]:
        reasons.append("pnl_conversion_efficiency<=0")
    if not metric_passes["execution_conversion_efficiency"]:
        reasons.append("execution_conversion_efficiency<0.20")
    if reasons:
        return "failed_metrics", reasons, metric_passes, metrics
    if realized_trade_count < resolved_production_floor:
        return "bootstrap_ready", [f"realized_trade_count<{resolved_production_floor}"], metric_passes, metrics
    return "passed", [], metric_passes, metrics


__all__ = [
    "Any",
    "List",
    "Optional",
    "_EXECUTION_AUDIT_REQUIRED_COLUMNS",
    "_EXECUTION_AUDIT_REQUIRED_MIGRATIONS",
    "_EXECUTION_AUDIT_REQUIRED_TABLES",
    "_coerce_ts",
    "_fallback_execution_audit_gate",
    "_safe_float",
    "_safe_int",
    "_safe_rules_dict",
    "_string",
    "aggregate_trade_position",
    "date",
    "datetime",
    "get_execution_audit_gate_evaluator",
    "get_execution_audit_snapshot_builder",
    "get_execution_audit_snapshot_metadata",
    "get_signal_evidence_builder",
    "json",
    "logger",
    "logging",
]
