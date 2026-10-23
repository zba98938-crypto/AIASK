"""Directional scorecard for the 85% target (WP-D).

The scorecard answers one question with four gate values that can each be
``pass``, ``fail`` or ``unmeasurable``. It never returns a boolean for a gate and
never substitutes a point estimate for a lower bound.

Definitions are frozen in ``METRIC_DEFINITION_VERSION``:

* independent unit = **trading day** (``prediction_as_of``). Same-day
  predictions across names, strategies and horizons count once, because their
  forward windows overlap and Wilson's i.i.d. assumption does not hold across
  them. Per-day hit rates are averaged with a block bootstrap whose block is at
  least the horizon.
* ``hit_rate`` = mean of per-day hit rates (each day equally weighted).
  ``pooled_hit_rate`` is reported alongside as a diagnostic only.
* ``selection_uplift`` = per-day hit rate minus the **same-day all-market** base
  rate, paired by day, with the horizon as the overlap correction.
* ``profit_factor`` and ``net_return_lcb95`` are mandatory companions: without
  them a 90% hit rate with PF 0.83 passes as success, which is exactly the
  failure this project already measured.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from .direction_stats import (
    block_bootstrap_lcb,
    mean,
    profit_factor,
    wilson_lcb,
)
from .horizon_scorer import HORIZON_SCORE_VERSION

SCORECARD_VERSION = "directional_scorecard.v2"
METRIC_DEFINITION_VERSION = "directional_hit_rate.v3"

GATE_PASS = "pass"
GATE_FAIL = "fail"
GATE_UNMEASURABLE = "unmeasurable"

MIN_TRADING_DAYS = 250
HIT_RATE_LCB_THRESHOLD = 0.80
MIN_PROFIT_FACTOR = 1.5
MIN_REGIME_COVERAGE = 2


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    token = str(value or "").strip()
    if not token:
        return None
    try:
        return datetime.strptime(token[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _block_size(horizons: Sequence[int]) -> int:
    horizon = max(horizons) if horizons else 1
    return max(5, min(int(horizon), 60))


def _gate(value: Optional[float], predicate: Any) -> str:
    if value is None:
        return GATE_UNMEASURABLE
    return GATE_PASS if predicate(value) else GATE_FAIL


def _rows(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        for key in ("data", "rows", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
        return []
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    return []


async def _fetch(db: Any, sql: str, *args: Any) -> list[dict[str, Any]]:
    fetch = getattr(db, "fetch", None)
    if not callable(fetch):
        return []
    return _rows(await fetch(sql, *args))


def _parse_horizon(value: Any) -> Optional[int]:
    from .horizon_scorer import parse_horizon

    return parse_horizon(value)


@dataclass(slots=True)
class ScoredPrediction:
    prediction_id: str
    strategy_id: str
    stock_code: str
    as_of: Optional[date]
    entry: Optional[date]
    exit: Optional[date]
    direction: str
    horizon: Optional[int]
    net_return: float
    direction_hit: Optional[bool]
    confidence: Optional[float] = None


async def load_scored_predictions(
    db: Any,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    limit: int = 200000,
) -> list[ScoredPrediction]:
    where = ["o.score_version = $1", "o.score_status IN ('ok', 'neutral_direction_excluded')"]
    args: list[Any] = [HORIZON_SCORE_VERSION]
    if start is not None:
        args.append(start.isoformat())
        where.append(f"substr(p.prediction_as_of, 1, 10) >= ${len(args)}")
    if end is not None:
        args.append(end.isoformat())
        where.append(f"substr(p.prediction_as_of, 1, 10) <= ${len(args)}")
    args.append(int(limit))
    rows = await _fetch(
        db,
        f"""
        SELECT
            p.prediction_id            AS prediction_id,
            p.strategy_id              AS strategy_id,
            p.stock_code               AS stock_code,
            p.prediction_as_of         AS prediction_as_of,
            p.direction                AS direction,
            p.horizon                  AS horizon,
            p.confidence               AS confidence,
            json_extract(o.outcome_json, '$.net_return')   AS net_return,
            json_extract(o.outcome_json, '$.entry_date')   AS entry_date,
            json_extract(o.outcome_json, '$.exit_date')    AS exit_date,
            json_extract(o.outcome_json, '$.direction_hit') AS direction_hit
        FROM strategy_trade_prediction_outcomes o
        JOIN strategy_trade_predictions p ON p.prediction_id = o.prediction_id
        WHERE {" AND ".join(where)}
        ORDER BY p.prediction_as_of, p.stock_code
        LIMIT ${len(args)}
        """,
        *args,
    )
    out: list[ScoredPrediction] = []
    for row in rows:
        net = _finite(row.get("net_return"))
        if net is None:
            continue
        hit_raw = row.get("direction_hit")
        hit = None if hit_raw is None else bool(hit_raw)
        out.append(
            ScoredPrediction(
                prediction_id=str(row.get("prediction_id") or ""),
                strategy_id=str(row.get("strategy_id") or ""),
                stock_code=str(row.get("stock_code") or ""),
                as_of=_day(row.get("prediction_as_of")),
                entry=_day(row.get("entry_date")),
                exit=_day(row.get("exit_date")),
                direction=str(row.get("direction") or "").lower(),
                horizon=_parse_horizon(row.get("horizon")),
                net_return=net,
                direction_hit=hit,
                confidence=_finite(row.get("confidence")),
            )
        )
    return out


async def load_market_panel(db: Any, *, start: date, end: date) -> dict[str, dict[date, float]]:
    """``{code: {day: close}}`` plus opens under the ``__open__`` sentinel key.

    ``close`` is the only field the base rate needs; the entry price uses the
    next session's open, so both are kept.
    """
    rows = await _fetch(
        db,
        """
        SELECT code, substr(time, 1, 10) AS day, open, close
        FROM kline_1d
        WHERE time >= $1 AND time < $2
        ORDER BY code, time
        """,
        start.isoformat(),
        (end + timedelta(days=1)).isoformat(),
    )
    panel: dict[str, dict[date, float]] = {}
    opens: dict[str, dict[date, float]] = {}
    for row in rows:
        day = _day(row.get("day"))
        close = _finite(row.get("close"))
        open_ = _finite(row.get("open"))
        code = str(row.get("code") or "")
        if day is None or close is None or not code:
            continue
        panel.setdefault(code, {})[day] = close
        if open_ is not None:
            opens.setdefault(code, {})[day] = open_
    if opens:
        panel["__open__"] = opens  # type: ignore[assignment]
    return panel


def market_base_rate(
    panel: dict[str, dict[date, float]],
    *,
    entry_day: date,
    horizon: int,
    cost: float,
    flat_band: float,
) -> Optional[float]:
    """Share of the whole market whose net horizon return clears the band.

    The base uses the *same* entry day and horizon as the prediction it is
    compared to; anything else turns the comparison into an apples-to-oranges
    number (the mistake that previously produced a 0.474 baseline).
    """
    opens = panel.get("__open__") or {}
    up = 0
    total = 0
    for code, closes in panel.items():
        if code == "__open__":
            continue
        code_opens = opens.get(code) or {}
        entry_open = code_opens.get(entry_day)
        if entry_open is None or entry_open <= 0:
            continue
        days = sorted(day for day in closes if day >= entry_day)
        if len(days) <= horizon:
            continue
        exit_close = closes[days[horizon]]
        net = exit_close / (entry_open * (1.0 + cost)) - 1.0
        total += 1
        if net > flat_band:
            up += 1
    return (up / total) if total else None


def _market_regime(panel: dict[str, dict[date, float]], day: date, lookback: int = 20) -> Optional[str]:
    """Ex-ante regime from the median 20-session return across the panel."""
    returns: list[float] = []
    for code, closes in panel.items():
        if code == "__open__":
            continue
        days = sorted(d for d in closes if d <= day)
        if len(days) < lookback + 1:
            continue
        head = closes[days[-lookback - 1]]
        tail = closes[days[-1]]
        if head > 0:
            returns.append(tail / head - 1.0)
    centre = mean(returns)
    if centre is None:
        return None
    return "risk_on" if centre > 0 else "risk_off"


@dataclass(slots=True)
class DayObservation:
    day: date
    hits: int
    total: int
    net_returns: list[float] = field(default_factory=list)

    @property
    def hit_rate(self) -> Optional[float]:
        return (self.hits / self.total) if self.total else None


def _group_by_day(predictions: Iterable[ScoredPrediction]) -> dict[date, DayObservation]:
    days: dict[date, DayObservation] = {}
    for item in predictions:
        if item.as_of is None or item.direction_hit is None:
            continue
        bucket = days.setdefault(item.as_of, DayObservation(day=item.as_of, hits=0, total=0))
        bucket.total += 1
        bucket.hits += 1 if item.direction_hit else 0
        bucket.net_returns.append(item.net_return)
    return days


async def build_directional_scorecard(
    db: Any,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    cost: float = 0.0021,
    flat_band: float = 0.0020,
) -> dict[str, Any]:
    predictions = await load_scored_predictions(db, start=start, end=end)
    days = _group_by_day(predictions)
    day_observations = [days[key] for key in sorted(days)]
    day_rates = [obs.hit_rate for obs in day_observations if obs.hit_rate is not None]
    horizons = [item.horizon for item in predictions if item.horizon]
    block = _block_size(horizons)

    pooled_total = sum(obs.total for obs in day_observations)
    pooled_hits = sum(obs.hits for obs in day_observations)
    hit_rate = mean(day_rates)
    lcb, boot_mean = block_bootstrap_lcb(day_rates, block_size=block) if day_rates else (None, None)
    pooled_lcb = wilson_lcb(pooled_hits, pooled_total) if pooled_total else None

    # Base rate per (as-of day, horizon). The panel is loaded once over the
    # window the scored predictions actually need.
    by_day_rows: dict[date, list[ScoredPrediction]] = {}
    for item in predictions:
        if item.as_of is not None and item.direction_hit is not None:
            by_day_rows.setdefault(item.as_of, []).append(item)
    uplift_by_day: list[float] = []
    base_rates: dict[str, Optional[float]] = {}
    regimes: dict[date, Optional[str]] = {}
    if day_observations:
        entry_days = [item.entry for item in predictions if item.entry]
        exit_days = [item.exit for item in predictions if item.exit]
        panel_start = min(entry_days) if entry_days else min(obs.day for obs in day_observations)
        panel_end = max(exit_days) if exit_days else max(obs.day for obs in day_observations)
        panel = await load_market_panel(db, start=panel_start, end=panel_end)
        for obs in day_observations:
            items = by_day_rows.get(obs.day) or []
            entries = [item.entry for item in items if item.entry]
            horizons_here = sorted({item.horizon for item in items if item.horizon})
            if not entries or not horizons_here:
                continue
            horizon = horizons_here[0]
            base = market_base_rate(panel, entry_day=min(entries), horizon=horizon, cost=cost, flat_band=flat_band)
            base_rates[f"{obs.day.isoformat()}:{horizon}"] = base
            if base is not None and obs.hit_rate is not None:
                uplift_by_day.append(obs.hit_rate - base)
            regimes[obs.day] = _market_regime(panel, obs.day)
    else:
        panel = {}

    uplift = mean(uplift_by_day) if uplift_by_day else None
    uplift_lcb, _ = block_bootstrap_lcb(uplift_by_day, block_size=block) if len(uplift_by_day) > 1 else (None, None)
    daily_net = [mean(obs.net_returns) for obs in day_observations]
    net_series = [value for value in daily_net if value is not None]
    net_lcb, _ = block_bootstrap_lcb(net_series, block_size=block) if net_series else (None, None)
    pf = profit_factor(item.net_return for item in predictions if item.direction_hit is not None)
    regime_coverage = len({value for value in regimes.values() if value})

    abstain = await _abstain_rate(db)
    n_days = len(day_observations)

    gates = {
        "n_trading_days_ge_250": _gate(float(n_days) if n_days else None, lambda value: value >= MIN_TRADING_DAYS),
        "hit_rate_lcb_gt_0_80": _gate(lcb, lambda value: value > HIT_RATE_LCB_THRESHOLD),
        "selection_uplift_lcb_gt_0": _gate(uplift_lcb, lambda value: value > 0),
        "profit_factor_ge_1_5": _gate(pf, lambda value: value >= MIN_PROFIT_FACTOR),
        "net_return_lcb_gt_0": _gate(net_lcb, lambda value: value > 0),
        "regime_coverage_ge_2": _gate(float(regime_coverage) if regime_coverage else None, lambda value: value >= MIN_REGIME_COVERAGE),
    }
    reason_codes = list(_reason_codes(gates, n_days, pooled_total, pf, base_rates))
    return {
        "scorecard_version": SCORECARD_VERSION,
        "metric_definition_version": METRIC_DEFINITION_VERSION,
        "score_version": HORIZON_SCORE_VERSION,
        "n_trading_days": n_days,
        "n_scored_predictions": pooled_total,
        "hit_rate": hit_rate,
        "hit_rate_lcb95": lcb,
        "hit_rate_bootstrap_mean": boot_mean,
        "pooled_hit_rate": (pooled_hits / pooled_total) if pooled_total else None,
        "pooled_wilson_lcb95": pooled_lcb,
        "selection_uplift": uplift,
        "selection_uplift_lcb95": uplift_lcb,
        "profit_factor": pf,
        "net_return_lcb95": net_lcb,
        "mean_daily_net_return": mean(net_series),
        "regime_coverage": regime_coverage,
        "regimes": {day.isoformat(): value for day, value in sorted(regimes.items())},
        "abstain_rate": abstain,
        "cost": cost,
        "flat_band": flat_band,
        "bootstrap_block": block,
        "gates": gates,
        "overall": _overall(gates),
        "reason_codes": reason_codes,
        "unmeasurable_counts": {
            "selection_uplift_days_missing_base": len([obs for obs in day_observations if obs.day.isoformat() not in {k.split(":")[0] for k, v in base_rates.items() if v is not None}]) if day_observations else 0,
        },
    }


def _reason_codes(
    gates: dict[str, str],
    n_days: int,
    pooled_total: int,
    pf: Optional[float],
    base_rates: dict[str, Optional[float]],
) -> list[str]:
    codes: list[str] = []
    if pooled_total == 0:
        codes.append("no_scorable_predictions")
    if n_days < MIN_TRADING_DAYS:
        codes.append("insufficient_trading_days")
    if pf is None:
        codes.append("profit_factor_unavailable")
    if base_rates and all(value is None for value in base_rates.values()):
        codes.append("market_base_rate_unavailable")
    for name, value in gates.items():
        if value == GATE_UNMEASURABLE and name not in {"n_trading_days_ge_250"}:
            codes.append(f"unmeasurable:{name}")
    return codes


def _overall(gates: dict[str, str]) -> str:
    values = set(gates.values())
    if GATE_FAIL in values:
        return GATE_FAIL
    if values == {GATE_PASS}:
        return GATE_PASS
    return GATE_UNMEASURABLE


async def _abstain_rate(db: Any) -> Optional[float]:
    rows = await _fetch(
        db,
        """
        SELECT action, COUNT(*) AS n
        FROM prediction_executions
        GROUP BY action
        """,
    )
    counts = {str(row.get("action")): int(row.get("n") or 0) for row in rows}
    ordered = counts.get("ordered", 0)
    abstained = counts.get("abstained", 0)
    total = ordered + abstained
    return (abstained / total) if total else None


__all__ = [
    "GATE_FAIL",
    "GATE_PASS",
    "GATE_UNMEASURABLE",
    "METRIC_DEFINITION_VERSION",
    "SCORECARD_VERSION",
    "build_directional_scorecard",
    "load_market_panel",
    "load_scored_predictions",
    "market_base_rate",
]
