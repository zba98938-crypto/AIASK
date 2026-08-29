"""Execution-quality evaluation helpers."""
from __future__ import annotations

# --- execution_quality_parts/execution_metrics.py ---
from datetime import datetime, timezone
from typing import Any, Optional

from .common import (
    _confidence_diagnostics_enabled,
    _safe_float,
    _safe_int,
    _string,
    metric_bucket_value,
)
from .confidence import (
    EXECUTION_FILLED_ORDER_STRONG,
    EXECUTION_FILLED_ORDER_WEAK,
    EXECUTION_NAV_CONVERSION_STRONG,
    EXECUTION_NAV_CONVERSION_WEAK,
    EXECUTION_NAV_RETURN_STRONG,
    EXECUTION_SIGNAL_TO_FILL_STRONG,
    EXECUTION_SIGNAL_TO_FILL_WEAK,
    EXPECTED_FORWARD_DAYS,
    SIGNAL_QUALITY_OVERLAP_FACTORS,
    SIGNAL_QUALITY_PRIMARY_DEFAULT,
    evaluate_execution_audit_gate,
    evaluate_confidence_contract,
)

def resolve_signal_quality_horizons(holding_period_bucket: Optional[str]) -> tuple[int, int]:
    bucket = str(holding_period_bucket or "").strip().lower()
    if bucket in {"intraday", "short", "short_term", "fast", "event"}:
        return (1, 5)
    if bucket in {"position", "long", "long_term", "slow", "trend"}:
        return (10, 20)
    return SIGNAL_QUALITY_PRIMARY_DEFAULT


def _fallback_effective_n(sample_count: int, horizon: int) -> int:
    sample_n = max(int(sample_count or 0), 0)
    if sample_n <= 0:
        return 0
    factor = float(SIGNAL_QUALITY_OVERLAP_FACTORS.get(int(horizon or 0), max(int(horizon or 1), 1)))
    if factor <= 0:
        factor = 1.0
    return max(1, min(sample_n, int(sample_n / factor)))


def derive_signal_quality(
    signal_stats: Optional[dict],
    *,
    holding_period_bucket: Optional[str] = None,
) -> dict:
    stats = dict(signal_stats or {})
    raw_signal_count = _safe_int(stats.get("raw_signal_count") or stats.get("total_signals"))
    forward_signal_count = _safe_int(stats.get("signals_with_forward_returns_count"), raw_signal_count)
    observed_days = [
        days
        for days in EXPECTED_FORWARD_DAYS
        if any(
            metric_bucket_value(stats.get(field), days) is not None
            for field in (
                "hit_rate",
                "hit_rate_lcb",
                "skill_lcb",
                "sample_count",
                "forward_ic",
                "forward_sharpe",
            )
        )
    ]
    missing_days = [days for days in EXPECTED_FORWARD_DAYS if days not in observed_days]
    coverage_ratio = round(len(observed_days) / len(EXPECTED_FORWARD_DAYS), 4) if EXPECTED_FORWARD_DAYS else 0.0
    signal_coverage_ratio = round(forward_signal_count / raw_signal_count, 4) if raw_signal_count > 0 else 0.0
    primary_horizon, secondary_horizon = resolve_signal_quality_horizons(holding_period_bucket)

    by_horizon: dict[str, dict[str, Any]] = {}
    fallback_sample_count = max(forward_signal_count, raw_signal_count)

    for horizon in EXPECTED_FORWARD_DAYS:
        raw_hit_rate = metric_bucket_value(stats.get("hit_rate"), horizon)
        hit_rate_lcb = metric_bucket_value(stats.get("hit_rate_lcb"), horizon)
        null_hit_rate = metric_bucket_value(stats.get("null_hit_rate"), horizon)
        skill_lcb = metric_bucket_value(stats.get("skill_lcb"), horizon)
        recent_hit_rate = metric_bucket_value(stats.get("recent_hit_rate"), horizon)
        recent_hit_rate_lcb = metric_bucket_value(stats.get("recent_hit_rate_lcb"), horizon)
        recent_skill_lcb = metric_bucket_value(stats.get("recent_skill_lcb"), horizon)
        stability_gap = metric_bucket_value(stats.get("stability_gap"), horizon)
        sample_count = _safe_int(metric_bucket_value(stats.get("sample_count"), horizon), fallback_sample_count if raw_hit_rate is not None else 0)
        effective_n = _safe_int(metric_bucket_value(stats.get("effective_n"), horizon), _fallback_effective_n(sample_count, horizon))
        neutral_count = _safe_int(metric_bucket_value(stats.get("neutral_count"), horizon), 0)
        forward_ic = metric_bucket_value(stats.get("forward_ic"), horizon)
        forward_sharpe = metric_bucket_value(stats.get("forward_sharpe"), horizon)

        if hit_rate_lcb is None and raw_hit_rate is not None:
            hit_rate_lcb = raw_hit_rate
        if null_hit_rate is None and raw_hit_rate is not None:
            null_hit_rate = 0.5
        if skill_lcb is None and hit_rate_lcb is not None:
            skill_lcb = float(hit_rate_lcb) - float(null_hit_rate or 0.5)
        if recent_hit_rate is None and raw_hit_rate is not None:
            recent_hit_rate = raw_hit_rate
        if recent_hit_rate_lcb is None and recent_hit_rate is not None:
            recent_hit_rate_lcb = recent_hit_rate
        if recent_skill_lcb is None and recent_hit_rate_lcb is not None:
            recent_skill_lcb = float(recent_hit_rate_lcb) - float(null_hit_rate or 0.5)
        if stability_gap is None and raw_hit_rate is not None and recent_hit_rate is not None:
            stability_gap = abs(float(raw_hit_rate) - float(recent_hit_rate))

        by_horizon[str(horizon)] = {
            "horizon": horizon,
            "hit_rate": raw_hit_rate,
            "hit_rate_lcb": hit_rate_lcb,
            "null_hit_rate": null_hit_rate,
            "skill_lcb": skill_lcb,
            "recent_hit_rate": recent_hit_rate,
            "recent_hit_rate_lcb": recent_hit_rate_lcb,
            "recent_skill_lcb": recent_skill_lcb,
            "stability_gap": stability_gap,
            "sample_count": sample_count,
            "effective_n": effective_n,
            "neutral_count": neutral_count,
            "forward_ic": forward_ic,
            "forward_sharpe": forward_sharpe,
        }

    primary = dict(by_horizon.get(str(primary_horizon)) or {})
    secondary = dict(by_horizon.get(str(secondary_horizon)) or {})
    primary_skill_lcb = _safe_float(primary.get("skill_lcb"))
    secondary_skill_lcb = _safe_float(secondary.get("skill_lcb"))
    recent_primary_skill_lcb = _safe_float(primary.get("recent_skill_lcb"))
    recent_secondary_skill_lcb = _safe_float(secondary.get("recent_skill_lcb"))
    stability_gap = _safe_float(primary.get("stability_gap"))

    return {
        "primary_horizon": primary_horizon,
        "secondary_horizon": secondary_horizon,
        "observed_forward_days": observed_days,
        "missing_forward_days": missing_days,
        "coverage_ratio": coverage_ratio,
        "signal_coverage_ratio": signal_coverage_ratio,
        "primary_sample_count": _safe_int(primary.get("sample_count")),
        "secondary_sample_count": _safe_int(secondary.get("sample_count")),
        "primary_effective_n": _safe_int(primary.get("effective_n")),
        "secondary_effective_n": _safe_int(secondary.get("effective_n")),
        "primary_hit_rate": _safe_float(primary.get("hit_rate")),
        "primary_hit_rate_lcb": _safe_float(primary.get("hit_rate_lcb")),
        "secondary_hit_rate": _safe_float(secondary.get("hit_rate")),
        "secondary_hit_rate_lcb": _safe_float(secondary.get("hit_rate_lcb")),
        "primary_skill_lcb": primary_skill_lcb,
        "secondary_skill_lcb": secondary_skill_lcb,
        "primary_signal_skill_lcb": primary_skill_lcb,
        "secondary_signal_skill_lcb": secondary_skill_lcb,
        "recent_primary_hit_rate": _safe_float(primary.get("recent_hit_rate")),
        "recent_secondary_hit_rate": _safe_float(secondary.get("recent_hit_rate")),
        "recent_primary_skill_lcb": recent_primary_skill_lcb,
        "recent_secondary_skill_lcb": recent_secondary_skill_lcb,
        "stability_gap": stability_gap,
        "hit_rate_lcb_method": str(stats.get("hit_rate_lcb_method") or "wilson_ess_approx"),
        "effective_n_method": str(stats.get("effective_n_method") or "overlap_adjusted_ess_v1"),
        "recent_window_days": _safe_int(stats.get("recent_window_days"), 20),
        "neutral_band_epsilon": _safe_float(stats.get("neutral_band_epsilon")),
        "by_horizon": by_horizon,
    }


def _round_metric(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _safe_ratio(numerator: Any, denominator: Any, *, digits: int = 6) -> Optional[float]:
    denom = _safe_float(denominator)
    if denom is None or abs(denom) <= 1e-12:
        return None
    num = _safe_float(numerator) or 0.0
    return _round_metric(num / denom, digits)


def _default_execution_order_summary() -> dict:
    return {
        "total_orders": 0,
        "filled_orders": 0,
        "total_trades": 0,
        "trade_amount": 0.0,
    }


async def _load_execution_quality_inputs(db, strategy_id: str, *, nav_limit: int = 60) -> dict:
    account = None
    if hasattr(db, "get_paper_account_by_strategy"):
        account = await db.get_paper_account_by_strategy(strategy_id)
    elif hasattr(db, "acquire"):
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM paper_accounts WHERE strategy_id = $1 ORDER BY created_at LIMIT 1",
                strategy_id,
            )
        account = dict(row) if row else None

    if not account:
        return {
            "account": None,
            "order_summary": _default_execution_order_summary(),
            "nav_rows": [],
        }

    account_id = str(account.get("id") or "").strip()
    order_summary = _default_execution_order_summary()
    if account_id and hasattr(db, "get_paper_order_summary"):
        order_summary.update(dict(await db.get_paper_order_summary(account_id) or {}))
    elif account_id and hasattr(db, "acquire"):
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COALESCE((SELECT COUNT(*) FROM paper_orders WHERE account_id = $1), 0) AS total_orders,
                    COALESCE((SELECT COUNT(*) FROM paper_orders WHERE account_id = $1 AND status = 'filled'), 0) AS filled_orders,
                    COALESCE((SELECT COUNT(*) FROM paper_trades WHERE account_id = $1), 0) AS total_trades,
                    COALESCE((SELECT SUM(amount) FROM paper_trades WHERE account_id = $1), 0) AS trade_amount
                """,
                account_id,
            )
        order_summary.update(dict(row or {}))

    nav_rows = []
    if account_id and hasattr(db, "get_paper_nav_rows"):
        nav_rows = await db.get_paper_nav_rows(account_id, limit=nav_limit)
    elif account_id and hasattr(db, "acquire"):
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM paper_nav WHERE account_id = $1 ORDER BY nav_date DESC LIMIT $2",
                account_id,
                max(1, min(int(nav_limit or 60), 365)),
            )
        nav_rows = [dict(row) for row in rows]

    return {
        "account": dict(account),
        "order_summary": {
            "total_orders": _safe_int(order_summary.get("total_orders")),
            "filled_orders": _safe_int(order_summary.get("filled_orders"), _safe_int(order_summary.get("total_trades"))),
            "total_trades": _safe_int(order_summary.get("total_trades")),
            "trade_amount": _safe_float(order_summary.get("trade_amount")) or 0.0,
        },
        "nav_rows": [dict(row) for row in list(nav_rows or [])],
    }


def _compute_paper_nav_return(nav_rows: list[dict[str, Any]]) -> Optional[float]:
    rows = [dict(item or {}) for item in list(nav_rows or [])]
    if not rows:
        return None
    newest_total = _safe_float(rows[0].get("total_value"))
    oldest_total = _safe_float(rows[-1].get("total_value"))
    if (
        newest_total is not None
        and oldest_total is not None
        and abs(oldest_total) > 1e-12
    ):
        return _round_metric((newest_total / oldest_total) - 1.0)

    compounded = 1.0
    daily_points = 0
    for row in reversed(rows):
        daily_return = _safe_float(row.get("daily_return"))
        if daily_return is None:
            continue
        compounded *= 1.0 + daily_return
        daily_points += 1
    if daily_points:
        return _round_metric(compounded - 1.0)
    return None


async def build_execution_quality(
    db,
    strategy: Optional[dict[str, Any]],
    *,
    signal_quality: Optional[dict[str, Any]] = None,
    total_signals: int = 0,
) -> dict[str, Any]:
    strategy_payload = dict(strategy or {})
    strategy_id = _string(strategy_payload.get("id"))
    signal_payload = dict(signal_quality or {})
    if not strategy_id:
        return {
            "account_id": None,
            "audit": {},
            "evidence_status": "missing",
            "evidence_gap_codes": ["missing_strategy_id"],
            "execution_audit_gate_status": "missing",
            "execution_audit_gate_reasons": ["execution_audit_missing"],
            "execution_hard_gate_passed": False,
            "prediction_quality_label": "insufficient_evidence",
            "prediction_reasons": ["missing_strategy_id"],
            "execution_quality_label": "insufficient_evidence",
            "execution_reasons": ["missing_strategy_id"],
            "diagnosis": "await_more_evidence",
            "diagnosis_reasons": ["missing_strategy_id"],
        }

    loaded_inputs = await _load_execution_quality_inputs(db, strategy_id)
    account = dict(loaded_inputs.get("account") or {})
    order_summary = dict(loaded_inputs.get("order_summary") or {})
    nav_rows = [dict(item or {}) for item in list(loaded_inputs.get("nav_rows") or [])]

    order_count = _safe_int(order_summary.get("total_orders"))
    filled_order_count = _safe_int(
        order_summary.get("filled_orders"),
        _safe_int(order_summary.get("total_trades")),
    )
    trade_count = _safe_int(order_summary.get("total_trades"))
    trade_amount = _safe_float(order_summary.get("trade_amount")) or 0.0
    nav_observation_days = len(nav_rows)
    paper_nav_return = _compute_paper_nav_return(nav_rows)
    signal_count = max(_safe_int(total_signals), 0)

    audit_summary: dict[str, Any] = {}
    get_trade_audit_summary = getattr(db, "get_strategy_trade_audit_summary", None)
    if callable(get_trade_audit_summary):
        try:
            audit_summary = dict(await get_trade_audit_summary(strategy_id) or {})
        except Exception:
            audit_summary = {}

    if not audit_summary:
        gate_status, gate_reasons, metric_passes, hard_gate_metrics = evaluate_execution_audit_gate(
            {
                "order_count": order_count,
                "filled_order_count": filled_order_count,
                "trade_count": trade_count,
                "nav_observation_days": nav_observation_days,
            }
        )
        audit_summary = {
            "approximate": True,
            "audit_grade": False,
            "method": "execution_quality_inputs_fallback_v1",
            "source_tables": ["paper_accounts", "paper_orders", "paper_trades", "paper_nav"],
            "mapped_position_count": 0,
            "realized_trade_count": 0,
            "incomplete_position_count": 0,
            "trade_expectancy": None,
            "pnl_conversion_efficiency": None,
            "execution_conversion_efficiency": None,
            "execution_win_rate": None,
            "avg_win_loss_ratio": None,
            "realized_pnl_total": None,
            "audit_ready_for_hard_gate": gate_status == "passed",
            "execution_audit_gate_status": gate_status,
            "execution_audit_gate_reasons": gate_reasons,
            "hard_gate_metric_passes": metric_passes,
            "hard_gate_metrics": hard_gate_metrics,
        }

    realized_trade_count = _safe_int(audit_summary.get("realized_trade_count"))
    mapped_position_count = _safe_int(audit_summary.get("mapped_position_count"))
    incomplete_position_count = _safe_int(audit_summary.get("incomplete_position_count"))
    trade_expectancy = _safe_float(audit_summary.get("trade_expectancy"))
    pnl_conversion_efficiency = _safe_float(audit_summary.get("pnl_conversion_efficiency"))
    execution_conversion_efficiency = _safe_float(audit_summary.get("execution_conversion_efficiency"))
    execution_win_rate = _safe_float(audit_summary.get("execution_win_rate"))
    avg_win_loss_ratio = _safe_float(audit_summary.get("avg_win_loss_ratio"))
    realized_pnl_total = _safe_float(audit_summary.get("realized_pnl_total"))
    execution_audit_gate_status = (
        _string(audit_summary.get("execution_audit_gate_status")) or "missing"
    )
    execution_audit_gate_reasons = [
        _string(item)
        for item in list(audit_summary.get("execution_audit_gate_reasons") or [])
        if _string(item)
    ]
    execution_hard_gate_passed = bool(audit_summary.get("audit_ready_for_hard_gate"))

    signal_to_fill_ratio = _safe_ratio(filled_order_count, signal_count) if signal_count > 0 else None
    filled_order_ratio = _safe_ratio(filled_order_count, order_count) if order_count > 0 else None
    round_trip_close_rate = _safe_ratio(realized_trade_count, trade_count) if trade_count > 0 else None
    trade_density = _safe_ratio(trade_count, nav_observation_days) if nav_observation_days > 0 else None
    missed_trade_ratio = (
        _round_metric(max(0.0, 1.0 - signal_to_fill_ratio))
        if signal_to_fill_ratio is not None
        else None
    )
    primary_skill_lcb = _safe_float(signal_payload.get("primary_skill_lcb"))
    nav_conversion_proxy = None
    if paper_nav_return is not None and primary_skill_lcb is not None and primary_skill_lcb > 0:
        nav_conversion_proxy = _round_metric(paper_nav_return / primary_skill_lcb)
    elif realized_pnl_total is not None and trade_amount > 0:
        nav_conversion_proxy = _round_metric(realized_pnl_total / trade_amount)

    prediction_quality_label, prediction_reasons = _classify_prediction_quality(signal_payload)
    execution_quality_label, execution_reasons = _classify_execution_quality(
        total_signals=signal_count,
        order_count=order_count,
        filled_order_count=filled_order_count,
        trade_count=trade_count,
        nav_observation_days=nav_observation_days,
        signal_to_fill_ratio=signal_to_fill_ratio,
        filled_order_ratio=filled_order_ratio,
        paper_nav_return=paper_nav_return,
        nav_conversion_proxy=nav_conversion_proxy,
    )
    diagnosis, diagnosis_reasons = _combine_quality_diagnosis(
        prediction_quality_label,
        execution_quality_label,
        prediction_reasons=prediction_reasons,
        execution_reasons=execution_reasons,
    )

    evidence_gap_codes: list[str] = []
    if not (_string(account.get("id")) or _string(account.get("account_id"))):
        evidence_gap_codes.append("missing_paper_account")
    if order_count <= 0:
        evidence_gap_codes.append("missing_paper_orders")
    if trade_count <= 0:
        evidence_gap_codes.append("missing_paper_trades")
    if nav_observation_days <= 0:
        evidence_gap_codes.append("missing_paper_nav")
    if mapped_position_count <= 0:
        evidence_gap_codes.append("missing_trade_position_linkage")
    if realized_trade_count <= 0:
        evidence_gap_codes.append("missing_realized_trade_evidence")
    if execution_audit_gate_status and execution_audit_gate_status != "passed":
        evidence_gap_codes.append(f"execution_audit_gate:{execution_audit_gate_status}")

    if not account:
        evidence_status = "missing"
    elif order_count <= 0 and trade_count <= 0 and nav_observation_days <= 0 and realized_trade_count <= 0:
        evidence_status = "bootstrap_pending"
    elif execution_hard_gate_passed:
        evidence_status = "ready"
    elif realized_trade_count > 0 or mapped_position_count > 0 or trade_count > 0:
        evidence_status = "partial"
    else:
        evidence_status = "bootstrap_pending"

    return {
        "account_id": _string(account.get("id") or account.get("account_id")) or None,
        "account": account or None,
        "audit": audit_summary,
        "evidence_status": evidence_status,
        "evidence_gap_codes": _unique_tokens(evidence_gap_codes, limit=16),
        "order_count": order_count,
        "filled_order_count": filled_order_count,
        "trade_count": trade_count,
        "trade_amount": _round_metric(trade_amount, digits=4),
        "nav_observation_days": nav_observation_days,
        "paper_nav_return": paper_nav_return,
        "signal_to_fill_ratio": signal_to_fill_ratio,
        "signal_to_order_conversion": signal_to_fill_ratio,
        "filled_order_ratio": filled_order_ratio,
        "fill_rate": filled_order_ratio,
        "round_trip_close_rate": round_trip_close_rate,
        "nav_conversion_proxy": nav_conversion_proxy,
        "trade_density": trade_density,
        "missed_trade_ratio": missed_trade_ratio,
        "realized_trade_count": realized_trade_count,
        "mapped_position_count": mapped_position_count,
        "incomplete_position_count": incomplete_position_count,
        "trade_expectancy": trade_expectancy,
        "pnl_conversion_efficiency": pnl_conversion_efficiency,
        "execution_conversion_efficiency": execution_conversion_efficiency,
        "execution_win_rate": execution_win_rate,
        "avg_win_loss_ratio": avg_win_loss_ratio,
        "realized_pnl_total": realized_pnl_total,
        "prediction_quality_label": prediction_quality_label,
        "prediction_reasons": prediction_reasons,
        "execution_quality_label": execution_quality_label,
        "execution_reasons": execution_reasons,
        "diagnosis": diagnosis,
        "diagnosis_reasons": diagnosis_reasons,
        "execution_audit_gate_status": execution_audit_gate_status,
        "execution_audit_gate_reasons": execution_audit_gate_reasons,
        "execution_hard_gate_passed": execution_hard_gate_passed,
    }


def _classify_prediction_quality(signal_quality: Optional[dict]) -> tuple[str, list[str]]:
    quality = dict(signal_quality or {})
    primary_horizon = _safe_int(quality.get("primary_horizon"), 5)
    primary_skill_lcb = _safe_float(quality.get("primary_skill_lcb"))
    recent_primary_skill_lcb = _safe_float(quality.get("recent_primary_skill_lcb"))
    primary_effective_n = _safe_int(quality.get("primary_effective_n"))
    coverage_ratio = _safe_float(quality.get("coverage_ratio")) or 0.0

    weak_reasons: list[str] = []
    if primary_skill_lcb is not None and primary_skill_lcb <= 0:
        weak_reasons.append(f"{primary_horizon}D skill LCB {(primary_skill_lcb or 0.0):+.2%} <= 0")
    if recent_primary_skill_lcb is not None and recent_primary_skill_lcb < 0:
        weak_reasons.append(f"recent {primary_horizon}D skill LCB {recent_primary_skill_lcb:+.2%} < 0")
    if weak_reasons:
        return "weak", weak_reasons

    insufficient_reasons: list[str] = []
    if primary_effective_n < 20:
        insufficient_reasons.append(f"{primary_horizon}D effective_n {primary_effective_n} < 20")
    if coverage_ratio < 0.5:
        insufficient_reasons.append(f"forward coverage {coverage_ratio:.0%} < 50%")
    if insufficient_reasons:
        return "insufficient_evidence", insufficient_reasons

    strong_reasons: list[str] = []
    if primary_skill_lcb is not None and primary_skill_lcb >= 0.03:
        strong_reasons.append(f"{primary_horizon}D skill LCB {primary_skill_lcb:+.2%} >= 3%")
    if recent_primary_skill_lcb is not None and recent_primary_skill_lcb >= 0.01:
        strong_reasons.append(f"recent {primary_horizon}D skill LCB {recent_primary_skill_lcb:+.2%} >= 1%")
    if primary_effective_n >= 60:
        strong_reasons.append(f"{primary_horizon}D effective_n {primary_effective_n} >= 60")
    if coverage_ratio >= 0.75:
        strong_reasons.append(f"forward coverage {coverage_ratio:.0%} >= 75%")
    if len(strong_reasons) >= 3:
        return "strong", strong_reasons
    return "mixed", strong_reasons or [f"{primary_horizon}D signal edge remains positive but not yet strong"]


def _classify_execution_quality(
    *,
    total_signals: int,
    order_count: int,
    filled_order_count: int,
    trade_count: int,
    nav_observation_days: int,
    signal_to_fill_ratio: Optional[float],
    filled_order_ratio: Optional[float],
    paper_nav_return: Optional[float],
    nav_conversion_proxy: Optional[float],
) -> tuple[str, list[str]]:
    weak_reasons: list[str] = []
    strong_reasons: list[str] = []

    if signal_to_fill_ratio is not None and total_signals >= 10:
        if signal_to_fill_ratio < EXECUTION_SIGNAL_TO_FILL_WEAK:
            weak_reasons.append(
                f"signal_to_fill_ratio {signal_to_fill_ratio:.2f} < {EXECUTION_SIGNAL_TO_FILL_WEAK:.2f}"
            )
        elif signal_to_fill_ratio >= EXECUTION_SIGNAL_TO_FILL_STRONG:
            strong_reasons.append(
                f"signal_to_fill_ratio {signal_to_fill_ratio:.2f} >= {EXECUTION_SIGNAL_TO_FILL_STRONG:.2f}"
            )

    if filled_order_ratio is not None and order_count >= 3:
        if filled_order_ratio < EXECUTION_FILLED_ORDER_WEAK:
            weak_reasons.append(
                f"filled_order_ratio {filled_order_ratio:.2f} < {EXECUTION_FILLED_ORDER_WEAK:.2f}"
            )
        elif filled_order_ratio >= EXECUTION_FILLED_ORDER_STRONG:
            strong_reasons.append(
                f"filled_order_ratio {filled_order_ratio:.2f} >= {EXECUTION_FILLED_ORDER_STRONG:.2f}"
            )

    if paper_nav_return is not None and nav_observation_days >= 2:
        if paper_nav_return <= 0:
            weak_reasons.append(f"paper_nav_return {paper_nav_return:+.2%} <= 0")
        elif paper_nav_return >= EXECUTION_NAV_RETURN_STRONG:
            strong_reasons.append(f"paper_nav_return {paper_nav_return:+.2%} >= {EXECUTION_NAV_RETURN_STRONG:.0%}")

    if nav_conversion_proxy is not None and nav_observation_days >= 2:
        if nav_conversion_proxy < EXECUTION_NAV_CONVERSION_WEAK:
            weak_reasons.append(
                f"nav_conversion_proxy {nav_conversion_proxy:.2f} < {EXECUTION_NAV_CONVERSION_WEAK:.2f}"
            )
        elif nav_conversion_proxy >= EXECUTION_NAV_CONVERSION_STRONG:
            strong_reasons.append(
                f"nav_conversion_proxy {nav_conversion_proxy:.2f} >= {EXECUTION_NAV_CONVERSION_STRONG:.2f}"
            )

    has_execution_evidence = order_count > 0 or filled_order_count > 0 or trade_count > 0 or nav_observation_days > 0
    sufficient_execution_evidence = (
        order_count >= 5
        or filled_order_count >= 3
        or trade_count >= 3
        or nav_observation_days >= 5
    )

    if weak_reasons:
        return "weak", weak_reasons
    if not has_execution_evidence:
        return "insufficient_evidence", ["paper_orders / paper_trades / paper_nav evidence is not available yet"]
    if not sufficient_execution_evidence:
        return "insufficient_evidence", ["paper execution evidence is still too shallow for a stable label"]
    if len(strong_reasons) >= 2:
        return "strong", strong_reasons
    return "mixed", strong_reasons or ["paper execution conversion is visible but not yet decisive"]


def _combine_quality_diagnosis(
    prediction_quality_label: str,
    execution_quality_label: str,
    *,
    prediction_reasons: list[str],
    execution_reasons: list[str],
) -> tuple[str, list[str]]:
    if prediction_quality_label == "weak" and execution_quality_label == "weak":
        return "prediction_and_execution_weak", prediction_reasons + execution_reasons
    if prediction_quality_label == "weak":
        return "prediction_weak", prediction_reasons
    if execution_quality_label == "weak":
        return "execution_conversion_weak", execution_reasons
    if prediction_quality_label == "insufficient_evidence" and execution_quality_label == "insufficient_evidence":
        return "await_more_evidence", prediction_reasons + execution_reasons
    if prediction_quality_label == "insufficient_evidence":
        return "await_prediction_evidence", prediction_reasons
    if execution_quality_label == "insufficient_evidence":
        return "await_execution_evidence", execution_reasons
    return (
        "balanced",
        prediction_reasons
        + execution_reasons
        + ["signal quality and execution conversion are not flagging a primary weakness"],
    )


def _build_confidence_diagnostics(
    strategy: Optional[dict],
    quality_report: Optional[dict],
    signal_quality: Optional[dict],
) -> tuple[str, dict[str, Any]]:
    strategy_payload = dict(strategy or {})
    params = dict(strategy_payload.get("params") or {})
    report_payload = dict(quality_report or {})
    confidence_contract = dict(
        report_payload.get("confidence_contract")
        or params.get("confidence_contract")
        or {}
    )
    return evaluate_confidence_contract(
        confidence_contract,
        signal_quality=signal_quality,
    )


def _normalize_execution_quality_for_contract(execution_quality: Optional[dict]) -> dict[str, Any]:
    payload = dict(execution_quality or {})
    if _confidence_diagnostics_enabled():
        return payload
    for field_name in (
        "prediction_quality_label",
        "prediction_reasons",
        "execution_quality_label",
        "execution_reasons",
        "diagnosis",
        "diagnosis_reasons",
    ):
        payload.pop(field_name, None)
    return payload


def _unique_tokens(values: list[Any] | None, *, limit: int = 12) -> list[str]:
    items: list[str] = []
    for value in list(values or []):
        token = _string(value)
        if token and token not in items:
            items.append(token)
        if len(items) >= max(1, int(limit or 12)):
            break
    return items


def _build_signal_quality_snapshot(signal_quality: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(signal_quality or {})
    primary_effective_n = _safe_int(payload.get("primary_effective_n"))
    coverage_ratio = _safe_float(payload.get("coverage_ratio")) or 0.0
    primary_skill_lcb = _safe_float(payload.get("primary_skill_lcb"))
    recent_primary_skill_lcb = _safe_float(payload.get("recent_primary_skill_lcb"))
    stability_gap = _safe_float(payload.get("stability_gap"))
    if primary_effective_n < 20 or coverage_ratio < 0.25:
        status = "insufficient_evidence"
    elif primary_skill_lcb is None or primary_skill_lcb <= 0.0:
        status = "weak"
    elif (
        primary_effective_n >= 60
        and coverage_ratio >= 0.75
        and (recent_primary_skill_lcb or 0.0) > 0.0
        and (stability_gap is None or stability_gap <= 0.05)
    ):
        status = "strong"
    else:
        status = "candidate"
    return {
        "contract_version": "strategy_factory.signal_quality_snapshot.v2",
        "status": status,
        "primary_horizon": _safe_int(payload.get("primary_horizon"), 5),
        "secondary_horizon": _safe_int(payload.get("secondary_horizon"), 10),
        "primary_effective_n": primary_effective_n,
        "secondary_effective_n": _safe_int(payload.get("secondary_effective_n")),
        "primary_skill_lcb": primary_skill_lcb,
        "secondary_skill_lcb": _safe_float(payload.get("secondary_skill_lcb")),
        "recent_primary_skill_lcb": recent_primary_skill_lcb,
        "stability_gap": stability_gap,
        "coverage_ratio": coverage_ratio,
        "signal_coverage_ratio": _safe_float(payload.get("signal_coverage_ratio")),
        "observed_forward_days": list(payload.get("observed_forward_days") or []),
        "missing_forward_days": list(payload.get("missing_forward_days") or []),
    }

# --- execution_quality_parts/execution_distributions.py ---
from .common import (
    _quality_report_field,
    _safe_boolish,
    _safe_float,
    _safe_int,
    _string,
)


def _build_execution_quality_snapshot(execution_quality: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(execution_quality or {})
    audit = dict(payload.get("audit") or {})
    execution_audit_gate_status = _string(
        payload.get("execution_audit_gate_status") or audit.get("execution_audit_gate_status")
    ) or "missing"
    evidence_gap_codes = list(payload.get("evidence_gap_codes") or [])
    if execution_audit_gate_status in {
        "missing",
        "bootstrap_pending",
        "insufficient_samples",
        "bootstrap_ready",
        "insufficient_evidence",
    }:
        evidence_gap_codes.append(f"execution_audit_gate:{execution_audit_gate_status}")
    execution_quality_label = _string(payload.get("execution_quality_label")).lower()
    if execution_audit_gate_status == "passed":
        status = "strong"
    elif execution_quality_label in {"strong", "candidate", "weak", "insufficient_evidence"}:
        status = execution_quality_label
    elif execution_audit_gate_status == "failed_metrics":
        status = "weak"
    else:
        status = "insufficient_evidence"
    return {
        "contract_version": "strategy_factory.execution_quality_snapshot.v2",
        "status": status,
        "evidence_status": _string(payload.get("evidence_status")) or None,
        "evidence_gap_codes": _unique_tokens(evidence_gap_codes, limit=16),
        "execution_audit_gate_status": execution_audit_gate_status,
        "realized_trade_count": _safe_int(payload.get("realized_trade_count") or audit.get("realized_trade_count")),
        "order_count": _safe_int(payload.get("order_count")),
        "trade_count": _safe_int(payload.get("trade_count")),
        "mapped_position_count": _safe_int(payload.get("mapped_position_count") or audit.get("mapped_position_count")),
        "incomplete_position_count": _safe_int(
            payload.get("incomplete_position_count") or audit.get("incomplete_position_count")
        ),
        "realized_pnl_total": _safe_float(payload.get("realized_pnl_total") or audit.get("realized_pnl_total")),
        "fill_rate": _safe_float(payload.get("fill_rate")),
        "round_trip_close_rate": _safe_float(payload.get("round_trip_close_rate")),
        "trade_expectancy": _safe_float(payload.get("trade_expectancy")),
        "pnl_conversion_efficiency": _safe_float(payload.get("pnl_conversion_efficiency")),
        "execution_conversion_efficiency": _safe_float(payload.get("execution_conversion_efficiency")),
        "realized_slippage_vs_model": _safe_float(payload.get("realized_slippage_vs_model"))
        if payload.get("realized_slippage_vs_model") is not None
        else None,
        "realized_vs_modeled_cost_gap": _safe_float(
            payload.get("realized_vs_modeled_cost_gap")
            if payload.get("realized_vs_modeled_cost_gap") is not None
            else payload.get("realized_slippage_vs_model")
        ),
        "trade_density": _safe_float(payload.get("trade_density")),
        "entry_quality": _safe_float(payload.get("entry_quality") or payload.get("fill_rate")),
        "exit_discipline": _safe_float(
            payload.get("exit_discipline") or payload.get("round_trip_close_rate")
        ),
        "regime_mismatch_events": list(payload.get("regime_mismatch_events") or []),
        "missed_trade_ratio": _safe_float(payload.get("missed_trade_ratio"))
        if payload.get("missed_trade_ratio") is not None
        else None,
        "hard_gate_ready": bool(payload.get("execution_hard_gate_passed")),
    }


def _resolve_high_precision_overview_context(
    strategy: dict[str, Any],
    *,
    quality_report: Optional[dict[str, Any]],
    quality_gate: Optional[dict[str, Any]],
    quality_summary: Optional[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    validation_profile = dict(
        _quality_report_field(quality_report, quality_gate, quality_summary, "validation_profile")
        or dict(quality_report or {}).get("validation_profile")
        or payload.get("validation_profile")
        or params.get("validation_profile")
        or {}
    )
    research_context = dict(payload.get("research_context") or params.get("research_context") or {})
    hypothesis_artifact = dict(
        payload.get("hypothesis_artifact")
        or params.get("hypothesis_artifact")
        or research_context.get("hypothesis_artifact")
        or {}
    )
    cost_sensitivity_grid = dict(
        hypothesis_artifact.get("cost_sensitivity_grid")
        or payload.get("cost_sensitivity_grid")
        or params.get("cost_sensitivity_grid")
        or {}
    )
    market_regime_assumption = dict(
        hypothesis_artifact.get("market_regime_assumption")
        or payload.get("market_regime_assumption")
        or params.get("market_regime_assumption")
        or research_context.get("market_regime_assumption")
        or {}
    )
    objective_profile = (
        _string(_quality_report_field(quality_report, quality_gate, quality_summary, "objective_profile")).lower()
        or _string(validation_profile.get("objective_profile")).lower()
        or _string(hypothesis_artifact.get("objective_profile")).lower()
        or _string(research_context.get("objective_profile")).lower()
        or None
    )
    if not objective_profile and _string(payload.get("generator_mode")).lower() == "futures_calendar_research_adapter":
        objective_profile = "high_precision"
    preferred_regime = _string(
        market_regime_assumption.get("preferred_regime") or validation_profile.get("preferred_regime")
    ) or None
    avoid_regime = _string(
        market_regime_assumption.get("avoid_regime") or validation_profile.get("avoid_regime")
    ) or None
    holding_rationale = _string(hypothesis_artifact.get("holding_rationale")) or None
    failure_mode = hypothesis_artifact.get("failure_mode")
    event_prefilter = dict(
        hypothesis_artifact.get("event_prefilter")
        or payload.get("event_prefilter")
        or params.get("event_prefilter")
        or {}
    )
    trade_density_preference = _string(validation_profile.get("trade_density_preference")).lower() or None
    entry_selectivity = _string(validation_profile.get("entry_selectivity")).lower() or None
    event_prefilter_required = _safe_boolish(
        event_prefilter.get("required")
        if event_prefilter.get("required") is not None
        else validation_profile.get("event_prefilter_required"),
        default=objective_profile == "high_precision" and _string(payload.get("strategy_type")).lower() == "event_structure_breakout",
    )
    event_prefilter_summary = dict(
        _quality_report_field(quality_report, quality_gate, quality_summary, "event_prefilter_summary")
        or {}
    )
    if not event_prefilter_summary and (event_prefilter_required or event_prefilter):
        observed_sources = [
            str(item or "").strip()
            for item in list(event_prefilter.get("observed_sources") or [])
            if str(item or "").strip()
        ]
        event_prefilter_summary = {
            "available": True,
            "required": event_prefilter_required,
            "profile": _string(
                event_prefilter.get("profile") or validation_profile.get("event_prefilter_profile")
            ) or None,
            "allowed_sources": list(event_prefilter.get("allowed_sources") or []),
            "observed_sources": observed_sources,
            "observed_confirmation_count": len(observed_sources),
            "min_confirmations": int(
                event_prefilter.get("min_confirmations")
                or validation_profile.get("event_prefilter_min_confirmations")
                or 1
            ),
            "confirmation_count": int(event_prefilter.get("confirmation_count") or len(observed_sources)),
            "passed": bool(event_prefilter.get("passed")) if event_prefilter else (not event_prefilter_required),
            "status": _string(event_prefilter.get("status")) or ("passed" if event_prefilter.get("passed") else "missing"),
            "event_id": event_prefilter.get("event_id"),
            "theme_code": event_prefilter.get("theme_code"),
            "focus_industries": list(event_prefilter.get("focus_industries") or []),
            "evidence_summary": event_prefilter.get("evidence_summary"),
            "event_anchor": dict(event_prefilter.get("event_anchor") or {}),
            "anchor_strength": event_prefilter.get("anchor_strength"),
        }
    event_anchor_summary = dict(
        _quality_report_field(quality_report, quality_gate, quality_summary, "event_anchor_summary")
        or event_prefilter_summary.get("event_anchor")
        or {}
    )
    backtest_metrics_contract_status = _string(
        _quality_report_field(quality_report, quality_gate, quality_summary, "backtest_metrics_contract_status")
        or dict(strategy.get("backtest_metrics_contract") or {}).get("status")
        or dict(strategy.get("params") or {}).get("backtest_metrics_contract_status")
    ).lower() or None
    regime_required = _safe_boolish(
        validation_profile.get("regime_required"),
        default=objective_profile == "high_precision",
    )
    cost_robust_required = _safe_boolish(
        validation_profile.get("cost_robust_required")
        if validation_profile.get("cost_robust_required") is not None
        else hypothesis_artifact.get("cost_robust_required"),
        default=objective_profile == "high_precision",
    )
    precision_readiness = _string(
        _quality_report_field(quality_report, quality_gate, quality_summary, "precision_readiness")
    ).lower() or None
    regime_validation_summary = dict(
        _quality_report_field(quality_report, quality_gate, quality_summary, "regime_validation_summary")
        or {}
    )
    if not regime_validation_summary and objective_profile == "high_precision":
        regime_validation_summary = {
            "available": bool(preferred_regime or avoid_regime or regime_required),
            "preferred_regime": preferred_regime,
            "avoid_regime": avoid_regime,
            "regime_required": regime_required,
            "passed": (not regime_required) or bool(preferred_regime and avoid_regime),
        }
    if not precision_readiness and objective_profile == "high_precision":
        has_core_contract = bool(
            holding_rationale
            and failure_mode not in (None, "", [], {})
            and entry_selectivity
            and ((not event_prefilter_required) or bool(event_prefilter_summary.get("passed")))
            and ((not regime_required) or (preferred_regime and avoid_regime))
            and ((not cost_robust_required) or cost_sensitivity_grid)
        )
        if has_core_contract:
            precision_readiness = "observe"
    return {
        "objective_profile": objective_profile,
        "precision_readiness": precision_readiness,
        "regime_validation_summary": regime_validation_summary,
        "cost_robustness_summary": dict(
            _quality_report_field(quality_report, quality_gate, quality_summary, "cost_robustness_summary")
            or {}
        ),
        "trade_density_summary": dict(
            _quality_report_field(quality_report, quality_gate, quality_summary, "trade_density_summary")
            or {}
        ),
        "event_prefilter_summary": event_prefilter_summary,
        "event_anchor_summary": event_anchor_summary,
        "backtest_metrics_contract_status": backtest_metrics_contract_status,
        "preferred_regime": preferred_regime,
        "avoid_regime": avoid_regime,
        "holding_rationale": holding_rationale,
        "failure_mode": failure_mode,
        "cost_sensitivity_grid": cost_sensitivity_grid,
        "trade_density_preference": trade_density_preference,
        "entry_selectivity": entry_selectivity,
        "trade_density": _safe_float(
            _quality_report_field(quality_report, quality_gate, quality_summary, "trade_density")
        ),
        "avg_holding_days": _safe_float(
            _quality_report_field(quality_report, quality_gate, quality_summary, "avg_holding_days")
        ),
        "cost_robust_required": cost_robust_required,
    }


def _build_position_cycle_evidence(
    *,
    signal_quality: Optional[dict[str, Any]],
    execution_quality: Optional[dict[str, Any]],
    context: Optional[dict[str, Any]],
) -> dict[str, Any]:
    high_precision_context = dict(context or {})
    if _string(high_precision_context.get("objective_profile")).lower() != "high_precision":
        return {}
    signal_payload = dict(signal_quality or {})
    execution_payload = dict(execution_quality or {})
    audit_payload = dict(execution_payload.get("audit") or {})
    primary_horizon = _safe_int(signal_payload.get("primary_horizon"), 5)
    primary_bucket = dict((signal_payload.get("by_horizon") or {}).get(str(primary_horizon)) or {})
    cycle_count = max(
        _safe_int(execution_payload.get("realized_trade_count")),
        _safe_int(execution_payload.get("trade_count")),
        _safe_int(primary_bucket.get("effective_n")),
    )
    cycle_hit_rate = _safe_float(
        audit_payload.get("execution_win_rate")
        if audit_payload.get("execution_win_rate") is not None
        else primary_bucket.get("hit_rate")
    )
    cycle_hit_rate_lcb = _safe_float(primary_bucket.get("hit_rate_lcb"))
    avg_holding_days = _safe_float(high_precision_context.get("avg_holding_days"))
    regime_validation_summary = dict(high_precision_context.get("regime_validation_summary") or {})
    cost_robustness_summary = dict(high_precision_context.get("cost_robustness_summary") or {})
    trade_density_summary = dict(high_precision_context.get("trade_density_summary") or {})
    event_prefilter_summary = dict(high_precision_context.get("event_prefilter_summary") or {})
    regime_validation_passed = regime_validation_summary.get("passed")
    if regime_validation_passed is None and high_precision_context.get("preferred_regime") and high_precision_context.get("avoid_regime"):
        regime_validation_passed = True
    event_prefilter_passed = bool(event_prefilter_summary.get("passed")) if event_prefilter_summary.get("required") else True
    regime_consistency = 1.0 if regime_validation_passed else (
        0.0 if regime_validation_summary.get("available") else None
    )
    cost_robustness = 1.0 if cost_robustness_summary.get("passed") else (
        0.0 if cost_robustness_summary.get("required") else None
    )
    payoff_asymmetry = _safe_float(
        audit_payload.get("avg_win_loss_ratio")
        if audit_payload.get("avg_win_loss_ratio") is not None
        else execution_payload.get("avg_win_loss_ratio")
    )
    adverse_regime_avoidance = bool(
        high_precision_context.get("avoid_regime")
        and regime_validation_passed
        and not list(execution_payload.get("regime_mismatch_events") or [])
    )
    if not event_prefilter_passed:
        status = "insufficient_evidence"
    elif cycle_count >= 6 and (cycle_hit_rate_lcb or 0.0) > 0.5 and (regime_consistency or 0.0) >= 0.75:
        status = "strong"
    elif cycle_count >= 3 and (cycle_hit_rate or 0.0) >= 0.55 and (regime_consistency or 0.0) >= 0.5:
        status = "candidate"
    else:
        status = "insufficient_evidence"
    return {
        "contract_version": "strategy_factory.position_cycle_evidence.v1",
        "status": status,
        "cycle_count": cycle_count,
        "cycle_hit_rate": cycle_hit_rate,
        "cycle_hit_rate_lcb": cycle_hit_rate_lcb,
        "avg_holding_days": avg_holding_days,
        "regime_consistency": regime_consistency,
        "cost_robustness": cost_robustness,
        "trade_density": _safe_float(
            trade_density_summary.get("observed_trade_density")
            if trade_density_summary.get("observed_trade_density") is not None
            else high_precision_context.get("trade_density")
        ),
        "trade_density_preference": _string(trade_density_summary.get("preference")) or None,
        "payoff_asymmetry": payoff_asymmetry,
        "adverse_regime_avoidance": adverse_regime_avoidance,
        "event_prefilter_passed": event_prefilter_passed,
    }

