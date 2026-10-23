#!/usr/bin/env python3
"""Evidence gates for the "prediction hit rate >= 85%" feasibility claim.

This is a diagnostic, not a test: it is deliberately named ``check_*.py`` so
pytest does not collect it. It answers "can 85% even be measured, and is there
any signal structure that could ever reach it" against the live database,
without writing anything.

Every section returns its own verdict, and "cannot be measured" is a different
verdict from "measured and below the bar". A section that cannot be measured
does not silently pass and does not return a default number.

Usage::

    python scripts/check_hitrate_evidence_gates.py
    python scripts/check_hitrate_evidence_gates.py --json
    python scripts/check_hitrate_evidence_gates.py --section data,predictions
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.path.expanduser("~/.aiask/data/db/akshare_mcp.sqlite3"))

ROUND_TRIP_COST = 0.0021
FLAT_BAND = 0.0020

SECTION_NAMES = ("data", "predictions", "baseline", "confidence", "selectivity", "factors", "joint")


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else None


def _finite(value: Any) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def wilson_lcb(hits: int, n: int, z: float = 1.96) -> Optional[float]:
    if n <= 0:
        return None
    p = hits / n
    denominator = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / denominator
    return max(0.0, min(1.0, center - margin))


def paired_diff(diffs: np.ndarray, *, overlap: int = 1) -> dict[str, Any]:
    """Per-date paired mean difference with an overlap correction.

    ``overlap`` is the horizon in trading days. Consecutive daily observations
    share ``overlap-1`` days of forward window, so the effective sample is
    ``n_days / overlap`` for the standard error. This is conservative and is
    stated in the output rather than hidden.
    """
    diffs = np.asarray([d for d in diffs if math.isfinite(float(d))], dtype=float)
    if diffs.size < 2:
        return {"n_days": int(diffs.size), "mean": None, "t": None, "lcb": None,
                "effective_n": None, "reason": "insufficient_days"}
    n = diffs.size
    mean = float(diffs.mean())
    sd = float(diffs.std(ddof=1))
    effective_n = max(1.0, n / max(1, overlap))
    if effective_n < 3:
        return {
            "n_days": int(n),
            "effective_n": round(effective_n, 2),
            "overlap_days": overlap,
            "mean": round(mean, 4),
            "sd": round(sd, 4),
            "t": None,
            "lcb": None,
            "reason": "effective_n_below_3_after_overlap_correction",
        }
    se = sd / math.sqrt(effective_n)
    t = mean / se if se > 0 else None
    lcb = mean - 1.96 * se
    return {
        "n_days": int(n),
        "effective_n": round(effective_n, 2),
        "overlap_days": overlap,
        "mean": round(mean, 4),
        "sd": round(sd, 4),
        "t": round(t, 2) if t is not None else None,
        "lcb": round(lcb, 4),
    }


def verdict(section: str, status: str, detail: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"section": section, "status": status, "detail": detail, "evidence": evidence or {}}


# ─────────────────────────────────────────────────────────────────────────────
# price panel
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Panel:
    bars: pd.DataFrame
    close: pd.DataFrame
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    volume: pd.DataFrame
    amount: pd.DataFrame
    latest_date: Optional[pd.Timestamp] = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, conn: sqlite3.Connection, *, max_codes: Optional[int] = None) -> "Panel":
        sql = """
            SELECT code, substr(time, 1, 10) AS day, open, high, low, close, volume, amount
            FROM kline_1d
            WHERE length(code) = 6
        """
        bars = pd.read_sql(sql, conn)
        bars["day"] = pd.to_datetime(bars["day"], errors="coerce")
        before = len(bars)
        bars = bars.dropna(subset=["day"]).drop_duplicates(["code", "day"]).sort_values(["code", "day"])
        notes = []
        if before != len(bars):
            notes.append(f"dropped {before - len(bars)} duplicate/unparseable kline rows")
        if max_codes:
            keep = sorted(bars["code"].unique())[:max_codes]
            bars = bars[bars["code"].isin(keep)]
            notes.append(f"restricted to {len(keep)} codes")
        pivots = {
            name: bars.pivot_table(index="day", columns="code", values=name).sort_index()
            for name in ("close", "open", "high", "low", "volume", "amount")
        }
        return cls(bars=bars, latest_date=bars["day"].max(), notes=notes, **pivots)

    def forward_net(self, horizon: int) -> pd.DataFrame:
        """Net return: next-session open entry, close ``horizon`` sessions later.

        Computed inside each code's own trading-day sequence and pivoted
        afterwards. Doing it on the pivoted panel instead would align by global
        row, so a stock with a missing bar would borrow a neighbouring session's
        price and produce a return that looks valid but is attributed to the
        wrong date.
        """
        bars = self.bars
        entry = bars.groupby("code")["open"].shift(-1)
        exit_ = bars.groupby("code")["close"].shift(-horizon)
        net = exit_ / entry - 1.0 - ROUND_TRIP_COST
        frame = bars.assign(net=net)
        return frame.pivot_table(index="day", columns="code", values="net")

    def cross_sectional_base(self, horizon: int, *, band: float = FLAT_BAND) -> pd.Series:
        net = self.forward_net(horizon)
        return (net > band).mean(axis=1).dropna()


# ─────────────────────────────────────────────────────────────────────────────
# sections: data / predictions / baseline / confidence
# ─────────────────────────────────────────────────────────────────────────────


def section_data(conn: sqlite3.Connection, panel: Panel) -> dict[str, Any]:
    bars = panel.bars
    per_year = (
        bars.groupby(bars["day"].dt.year)
        .agg(codes=("code", "nunique"), days=("day", "nunique"), rows=("code", "size"))
        .reset_index()
    )
    latest = panel.latest_date
    recent = bars[bars["day"] >= (latest - pd.Timedelta(days=7))] if latest is not None else bars
    recent_codes = int(recent["code"].nunique())
    recent_days = int(recent["day"].nunique())
    fill = {
        column: round(float(bars[column].notna().mean()), 4)
        for column in ("open", "high", "low", "close", "volume", "amount")
    }
    turnover_present = _one(conn, "SELECT count(turnover) * 1.0 / count(*) FROM kline_1d")
    intraday_rows = _one(conn, "SELECT count(*) FROM kline_intraday")
    intraday_codes = _one(conn, "SELECT count(DISTINCT code) FROM kline_intraday")
    calendar_days = _one(conn, "SELECT count(*) FROM trading_dates")
    calendar_latest = _one(conn, "SELECT max(trade_date) FROM trading_dates")
    sample = str(calendar_latest or "")
    calendar_format = "iso" if "-" in sample else "compact_yyyymmdd"
    status = "ok"
    detail = (
        f"kline_1d {len(bars)} rows / {int(bars['code'].nunique())} codes / "
        f"{int(bars['day'].nunique())} days, latest {latest.date() if latest is not None else 'n/a'}"
    )
    if recent_days < 3:
        status = "degraded"
        detail += f"; only {recent_days} recent days ({recent_codes} codes) — recent bars are not synced"
    if turnover_present is not None and float(turnover_present) == 0.0:
        detail += "; kline_1d.turnover is 100% NULL (turnover factor unavailable from this table)"
    return verdict(
        "data",
        status,
        detail,
        {
            "per_year": [
                {"year": int(row["day"]), "codes": int(row["codes"]), "days": int(row["days"]), "rows": int(row["rows"])}
                for row in per_year.to_dict("records")
            ],
            "column_fill": fill,
            "kline_turnover_fill": _finite(turnover_present),
            "recent_days": recent_days,
            "recent_codes": recent_codes,
            "intraday_rows": intraday_rows,
            "intraday_codes": intraday_codes,
            "trading_calendar_rows": calendar_days,
            "trading_calendar_latest": sample,
            "trading_calendar_format": calendar_format,
            "notes": panel.notes,
        },
    )


def _parse_horizon(token: Any) -> Optional[int]:
    text = str(token or "").strip().lower()
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    value = int(digits)
    return value if value > 0 else None


def _load_predictions(conn: sqlite3.Connection, latest: Optional[pd.Timestamp]) -> pd.DataFrame:
    df = pd.read_sql(
        """
        SELECT prediction_id, strategy_id, stock_code, substr(prediction_as_of, 1, 10) AS as_of,
               substr(target_trading_date, 1, 10) AS target_date, direction, horizon, confidence
        FROM strategy_trade_predictions
        """,
        conn,
    )
    df["as_of"] = pd.to_datetime(df["as_of"], errors="coerce")
    df["target_date"] = pd.to_datetime(df["target_date"], errors="coerce")
    df["horizon_days"] = df["horizon"].map(_parse_horizon)
    df["direction"] = df["direction"].astype(str).str.strip().str.lower()
    df["code"] = df["stock_code"].astype(str).str[:6]
    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    if latest is not None:
        df = df[df["as_of"] <= latest]
    return df


def score_predictions(pred: pd.DataFrame, panel: Panel) -> pd.DataFrame:
    """Score each prediction against its own horizon, next-open entry.

    Returns one row per prediction with ``net`` (None when the horizon is not
    complete) and ``hit`` (None when unscorable). Nothing is coerced to 0/1.
    """
    if pred.empty:
        return pred.assign(net=np.nan, hit=np.nan, base=np.nan, scorable=False)
    codes = set(pred["code"].unique())
    columns = [c for c in panel.close.columns if c in codes]
    horizons = sorted(pred["horizon_days"].dropna().unique())
    # ``net_by_h`` keeps only the predicted codes; ``market_by_h`` keeps the full
    # panel and is what the same-day base rate must be computed from. Computing
    # the base over predicted codes only would compare the strategy against
    # itself.
    net_by_h = {h: panel.forward_net(h)[columns] for h in horizons}
    market_by_h = {h: panel.forward_net(h) for h in horizons}
    rows = []
    for record in pred.itertuples(index=False):
        days = record.horizon_days
        net = None
        base = None
        table = net_by_h.get(days)
        if table is not None and record.code in table.columns and record.as_of in table.index:
            row = table.loc[record.as_of]
            value = row.get(record.code)
            net = float(value) if pd.notna(value) else None
        market = market_by_h.get(days)
        if market is not None and record.as_of in market.index:
            market_row = market.loc[record.as_of].dropna()
            base = float((market_row > FLAT_BAND).mean()) if len(market_row) else None
        direction = record.direction
        hit = None
        if net is not None:
            if direction == "up":
                hit = net > FLAT_BAND
            elif direction == "down":
                hit = net < -FLAT_BAND
            elif direction == "neutral":
                hit = abs(net) <= FLAT_BAND
        rows.append(
            {
                "prediction_id": record.prediction_id,
                "strategy_id": record.strategy_id,
                "code": record.code,
                "as_of": record.as_of,
                "direction": direction,
                "horizon_days": days,
                "confidence": record.confidence,
                "net": net,
                "base": base,
                "hit": hit,
                "scorable": net is not None,
            }
        )
    return pd.DataFrame(rows)


def _unscorable_reasons(scored: pd.DataFrame) -> dict[str, int]:
    if scored.empty:
        return {}
    reasons: dict[str, int] = {}
    for record in scored[~scored["scorable"]].itertuples(index=False):
        key = "horizon_invalid" if record.horizon_days is None else "horizon_incomplete"
        reasons[key] = reasons.get(key, 0) + 1
    return reasons


def section_predictions(conn: sqlite3.Connection, pred: pd.DataFrame, scored: pd.DataFrame) -> dict[str, Any]:
    total = len(pred)
    distinct_strategies = int(pred["strategy_id"].nunique())
    blocks_code = int(pred[["code", "as_of"]].drop_duplicates().shape[0])
    blocks_strategy = int(pred[["strategy_id", "code", "as_of"]].drop_duplicates().shape[0])
    as_of_dates = sorted(str(d.date()) for d in pred["as_of"].dropna().unique())
    direction_mix = {str(k): int(v) for k, v in pred["direction"].value_counts().items()}
    outcomes = _rows(
        conn,
        """
        SELECT score_version, score_status, count(*) AS n
        FROM strategy_trade_prediction_outcomes
        GROUP BY score_version, score_status
        """,
    )
    stuck = _one(
        conn,
        """
        SELECT count(*) FROM strategy_trade_predictions p
        WHERE date(p.target_trading_date) <= date('now')
          AND EXISTS (
              SELECT 1 FROM strategy_trade_prediction_outcomes o
              WHERE o.prediction_id = p.prediction_id
                AND o.score_version = 'trade_prediction_score_daily_v1'
                AND o.score_status = 'pending_market_data'
          )
        """,
    )
    orders = _one(conn, "SELECT count(*) FROM paper_orders")
    matched_target = _one(
        conn,
        """
        SELECT count(*) FROM paper_orders o
        WHERE EXISTS (
            SELECT 1 FROM strategy_trade_predictions p
            WHERE p.strategy_id = o.strategy_id
              AND substr(p.stock_code, 1, 6) = substr(o.code, 1, 6)
              AND date(p.target_trading_date) = date(o.signal_date)
        )
        """,
    )
    matched_window = _one(
        conn,
        """
        SELECT count(*) FROM paper_orders o
        WHERE EXISTS (
            SELECT 1 FROM strategy_trade_predictions p
            WHERE p.strategy_id = o.strategy_id
              AND substr(p.stock_code, 1, 6) = substr(o.code, 1, 6)
              AND date(p.prediction_as_of) <= date(o.signal_date)
              AND date(p.target_trading_date) >= date(o.signal_date)
        )
        """,
    )
    scorable = int(scored["scorable"].sum()) if not scored.empty else 0
    status = "ok"
    detail = (
        f"{total} predictions / {distinct_strategies} strategies; "
        f"blocks(code,as_of)={blocks_code}, blocks(strategy,code,as_of)={blocks_strategy}; "
        f"scorable={scorable}"
    )
    if distinct_strategies and total / max(1, distinct_strategies) < 1.05:
        status = "degraded"
        detail += "; one prediction per strategy — predictions are submission-time, not daily"
    return verdict(
        "predictions",
        status,
        detail,
        {
            "predictions": total,
            "distinct_strategies": distinct_strategies,
            "predictions_per_strategy": round(total / max(1, distinct_strategies), 3),
            "as_of_dates": as_of_dates,
            "direction_mix": direction_mix,
            "scorable": scorable,
            "unscorable_reasons": _unscorable_reasons(scored),
            "outcomes": outcomes,
            "stuck_pending_market_data": stuck,
            "paper_orders": orders,
            "orders_matched_target_date": matched_target,
            "orders_matched_window": matched_window,
            "blocks_code_as_of": blocks_code,
            "blocks_strategy_code_as_of": blocks_strategy,
        },
    )


def section_baseline(conn: sqlite3.Connection, panel: Panel, scored: pd.DataFrame) -> dict[str, Any]:
    data = scored.dropna(subset=["net"]) if not scored.empty else scored
    if data.empty:
        return verdict("baseline", "unmeasurable", "no scorable prediction", {})
    directional = data[data["direction"].isin(["up", "down"])]
    per_horizon: dict[str, Any] = {}
    for horizon, group in data.groupby("horizon_days"):
        blocks = group.drop_duplicates(["code", "as_of"])
        hits = int(pd.to_numeric(blocks["hit"], errors="coerce").fillna(0).sum())
        n = int(len(blocks))
        diffs = []
        for _, day_group in group.dropna(subset=["hit", "base"]).groupby("as_of"):
            day_blocks = day_group.drop_duplicates(["code"])
            if day_blocks.empty:
                continue
            diffs.append(float(day_blocks["hit"].mean()) - float(day_blocks["base"].iloc[0]))
        per_horizon[str(int(horizon))] = {
            "predictions": int(len(group)),
            "unique_blocks": n,
            "hits": hits,
            "hit_rate": round(hits / n, 4) if n else None,
            "wilson_lcb": round(wilson_lcb(hits, n), 4) if n else None,
            "uplift_vs_same_day_base": paired_diff(np.asarray(diffs), overlap=int(horizon)),
        }
    per_date = []
    for (day, horizon), group in directional.groupby(["as_of", "horizon_days"]):
        blocks = group.drop_duplicates(["code"])
        hit_rate = round(float(blocks["hit"].mean()), 4) if blocks["hit"].notna().any() else None
        base_values = group["base"].dropna()
        per_date.append(
            {
                "as_of": str(pd.Timestamp(day).date()),
                "horizon_days": int(horizon),
                "rows": int(len(group)),
                "unique_blocks": int(len(blocks)),
                "hit_rate": hit_rate,
                "same_day_base": round(float(base_values.iloc[0]), 4) if not base_values.empty else None,
            }
        )
    return verdict(
        "baseline",
        "measured",
        "hit rates are quoted both absolutely and against the same-day cross-sectional base",
        {
            "scorable_rows": int(len(data)),
            "directional_rows": int(len(directional)),
            "neutral_share": round(float((data["direction"] == "neutral").mean()), 4),
            "per_horizon": per_horizon,
            "per_date": sorted(per_date, key=lambda row: row["as_of"]),
        },
    )


def section_confidence(conn: sqlite3.Connection, pred: pd.DataFrame, scored: pd.DataFrame) -> dict[str, Any]:
    values = pred["confidence"].dropna()
    if values.empty:
        return verdict("confidence", "unmeasurable", "no confidence values", {})
    combos = pred[["horizon", "direction", "confidence"]].drop_duplicates()
    max_conf = float(values.max())
    per_combo = pred.groupby(["horizon", "direction"])["confidence"].nunique(dropna=True)
    one_to_one = bool(float((per_combo <= 1).mean()) >= 0.95) if len(per_combo) else False
    data = scored.dropna(subset=["net"]) if not scored.empty else scored
    monotonic: dict[str, Any] = {}
    for horizon, group in data.groupby("horizon_days"):
        buckets = []
        for confidence, rows in group.groupby("confidence"):
            blocks = rows.drop_duplicates(["code", "as_of"])
            if len(blocks) < 5:
                continue
            buckets.append((float(confidence), int(len(blocks)), float(blocks["hit"].mean())))
        buckets.sort()
        if len(buckets) >= 2:
            confs = pd.Series([b[0] for b in buckets])
            rates = pd.Series([b[2] for b in buckets])
            monotonic[str(int(horizon))] = {
                "buckets": len(buckets),
                "spearman": round(float(confs.corr(rates, method="spearman")), 3),
                "detail": [
                    {"confidence": round(c, 4), "blocks": n, "hit_rate": round(r, 4)} for c, n, r in buckets
                ],
            }
    brier = None
    usable = data.dropna(subset=["confidence", "hit"])
    if len(usable):
        brier = round(float(((usable["confidence"] - usable["hit"].astype(float)) ** 2).mean()), 4)
    status = "degraded" if (max_conf < 0.85 or one_to_one) else "ok"
    detail = (
        f"max confidence {max_conf:.4f}, rows>=0.85 {int((values >= 0.85).sum())}, "
        f"rows>=0.80 {int((values >= 0.80).sum())}, "
        f"distinct (horizon,direction,confidence) combos {len(combos)}"
    )
    if one_to_one:
        detail += "; confidence is a lookup of (horizon,direction), not a probability"
    return verdict(
        "confidence",
        status,
        detail,
        {
            "max_confidence": round(max_conf, 6),
            "rows_ge_085": int((values >= 0.85).sum()),
            "rows_ge_080": int((values >= 0.80).sum()),
            "combo_count": int(len(combos)),
            "one_to_one_with_horizon_direction": one_to_one,
            "raw_confidence_brier": brier,
            "monotonicity_by_horizon": monotonic,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# sections: selectivity / factors
# ─────────────────────────────────────────────────────────────────────────────

FACTOR_DEFS: dict[str, str] = {
    "mom_5": "5-day close momentum",
    "mom_20": "20-day close momentum",
    "mom_60": "60-day close momentum",
    "rev_1": "1-day reversal (negated 1d return)",
    "rev_5": "5-day reversal (negated 5d return)",
    "vol_20": "20-day realized volatility (negated: lower vol)",
    "amt_20": "log 20-day mean amount (size proxy)",
    "range_20": "20-day mean (high-low)/close (negated)",
    "close_pos_20": "close position inside the 20-day range",
    "vol_surge": "volume vs its 20-day mean",
}


def section_selectivity(conn: sqlite3.Connection, panel: Panel) -> dict[str, Any]:
    """How often is the *unconditional* base rate already >= 0.85?

    This is the ceiling for any selector: if 85% is never the market's base
    rate, a selector must supply the whole difference through stock picking. If
    it often is the base rate, the task reduces to identifying those days
    before the fact — which must then be measured separately, not assumed.
    """
    start = pd.Timestamp("2025-01-01")
    if int((panel.close.index >= start).sum()) < 30:
        return verdict("selectivity", "unmeasurable", "fewer than 30 sessions since 2025-01-01", {})
    daily = panel.close.pct_change(fill_method=None)
    equal_weight = daily.mean(axis=1)
    momentum = (1.0 + equal_weight).rolling(20).apply(np.prod, raw=True) - 1.0
    breadth = ((panel.close / panel.close.shift(20) - 1.0) > 0).mean(axis=1)
    by_horizon: dict[str, Any] = {}
    for horizon in (5, 20):
        base = panel.cross_sectional_base(horizon)
        base = base[base.index >= start]
        if base.empty:
            continue
        breadth_slice = breadth.reindex(base.index)
        regimes = {
            "all": base,
            "mom20>0": base[momentum.reindex(base.index) > 0.0],
            "mom20>2pct": base[momentum.reindex(base.index) > 0.02],
            "breadth_top50": base[breadth_slice >= breadth_slice.quantile(0.5)],
            "breadth_top10": base[breadth_slice >= breadth_slice.quantile(0.9)],
        }
        by_horizon[f"{horizon}d"] = {
            "days": int(len(base)),
            "mean_base": round(float(base.mean()), 4),
            "days_ge_085": int((base >= 0.85).sum()),
            "days_ge_080": int((base >= 0.80).sum()),
            "share_days_ge_085": round(float((base >= 0.85).mean()), 4),
            "share_days_ge_080": round(float((base >= 0.80).mean()), 4),
            "regimes": {
                name: {
                    "days": int(len(series)),
                    "mean_base": round(float(series.mean()), 4) if len(series) else None,
                    "share_ge_085": round(float((series >= 0.85).mean()), 4) if len(series) else None,
                }
                for name, series in regimes.items()
            },
        }
    normal = NormalDist()
    requirement = {
        str(base_rate): {
            f"q={q}": round(
                (normal.inv_cdf(0.85) - normal.inv_cdf(base_rate)) / normal.inv_cdf(1.0 - q), 3
            )
            for q in (0.10, 0.05, 0.01, 0.001)
        }
        for base_rate in (0.43, 0.50, 0.60, 0.70, 0.80)
    }
    detail = (
        "unconditional base rate >=85% on "
        + ", ".join(
            f"{k}: {v['days_ge_085']}/{v['days']} days ({v['share_days_ge_085'] * 100:.1f}%)"
            for k, v in by_horizon.items()
        )
    )
    return verdict(
        "selectivity",
        "measured",
        detail,
        {"base_rate_by_horizon": by_horizon, "required_std_for_85": requirement},
    )


def build_factors(panel: Panel) -> dict[str, pd.DataFrame]:
    """Compute factors inside each code's own trading-day sequence, then pivot.

    ``rolling`` on the pivoted panel counts panel rows, not a stock's own
    sessions; for a gappy series that silently changes what "20 days" means.
    """
    bars = panel.bars.sort_values(["code", "day"]).reset_index(drop=True)
    by_code = bars.groupby("code")
    close, high, low = bars["close"], bars["high"], bars["low"]
    volume, amount = bars["volume"], bars["amount"]
    daily = close / by_code["close"].shift(1) - 1.0

    def roll(series: pd.Series, window: int, how: str) -> pd.Series:
        grouped = series.groupby(bars["code"])
        if how == "mean":
            return grouped.rolling(window, min_periods=window).mean().reset_index(level=0, drop=True)
        if how == "std":
            return grouped.rolling(window, min_periods=window).std().reset_index(level=0, drop=True)
        if how == "min":
            return grouped.rolling(window, min_periods=window).min().reset_index(level=0, drop=True)
        if how == "max":
            return grouped.rolling(window, min_periods=window).max().reset_index(level=0, drop=True)
        raise ValueError(how)

    frame = bars[["day", "code"]].copy()
    frame["mom_5"] = close / by_code["close"].shift(5) - 1.0
    frame["mom_20"] = close / by_code["close"].shift(20) - 1.0
    frame["mom_60"] = close / by_code["close"].shift(60) - 1.0
    frame["rev_1"] = -daily
    frame["rev_5"] = -(close / by_code["close"].shift(5) - 1.0)
    frame["vol_20"] = -roll(daily, 20, "std")
    frame["amt_20"] = np.log1p(roll(amount, 20, "mean"))
    frame["range_20"] = -(roll(high - low, 20, "mean") / close)
    low20, high20 = roll(low, 20, "min"), roll(high, 20, "max")
    frame["close_pos_20"] = (close - low20) / (high20 - low20)
    frame["vol_surge"] = volume / roll(volume, 20, "mean")
    return {
        name: frame.pivot_table(index="day", columns="code", values=name).replace([np.inf, -np.inf], np.nan)
        for name in FACTOR_DEFS
    }


def section_factors(conn: sqlite3.Connection, panel: Panel) -> dict[str, Any]:
    """Cross-sectional scan: does any OHLCV factor put a small top slice at 85%?"""
    start = pd.Timestamp("2025-01-01")
    factors = build_factors(panel)
    results: dict[str, Any] = {}
    for horizon in (5, 20):
        net = panel.forward_net(horizon)
        net = net[net.index >= start]
        base = (net > FLAT_BAND).mean(axis=1).dropna()
        for name, factor in factors.items():
            factor = factor.reindex(index=net.index, columns=net.columns)
            ic_rows: list[float] = []
            top_rows: dict[float, list[float]] = {0.10: [], 0.05: [], 0.01: [], 0.001: []}
            base_rows: list[float] = []
            for day in net.index:
                x = factor.loc[day]
                y = net.loc[day]
                mask = x.notna() & y.notna()
                if int(mask.sum()) < 500:
                    continue
                xs, ys = x[mask], y[mask]
                ic_rows.append(float(xs.rank().corr(ys.rank())))
                ranks = xs.rank(ascending=False)
                base_rows.append(float((ys > FLAT_BAND).mean()))
                for q in top_rows:
                    count = max(1, int(len(xs) * q))
                    selected = ys[ranks <= count]
                    top_rows[q].append(float((selected > FLAT_BAND).mean()))
            if not ic_rows:
                continue
            ic = np.asarray(ic_rows)
            entry: dict[str, Any] = {
                "description": FACTOR_DEFS.get(name, name),
                "days": int(len(ic)),
                "rank_ic_mean": round(float(ic.mean()), 4),
                "rank_ic_ir": round(float(ic.mean() / ic.std(ddof=1)), 3) if ic.std(ddof=1) > 0 else None,
                "rank_ic_positive_share": round(float((ic > 0).mean()), 3),
            }
            for q, rates in top_rows.items():
                if not rates:
                    continue
                rates_arr = np.asarray(rates)
                base_arr = np.asarray(base_rows)
                diff = rates_arr - base_arr
                entry[f"top_{q}"] = {
                    "mean_hit": round(float(rates_arr.mean()), 4),
                    "mean_uplift": round(float(diff.mean()), 4),
                    "share_days_ge_085": round(float((rates_arr >= 0.85).mean()), 4),
                    "paired": paired_diff(diff, overlap=horizon),
                }
            results[f"{horizon}d:{name}"] = entry
    ranked = sorted(
        results.items(),
        key=lambda kv: (kv[1].get("top_0.001", {}).get("mean_uplift") or -9),
        reverse=True,
    )
    best = ranked[0][1].get("top_0.001", {}) if ranked else {}
    detail = (
        f"scanned {len(factors)} OHLCV factors on 5d/20d; best top-0.1% hit "
        f"{best.get('mean_hit')} (uplift {best.get('mean_uplift')})"
    )
    return verdict(
        "factors",
        "measured",
        detail,
        {"top_by_uplift": [{"key": k, **v} for k, v in ranked[:8]], "all": results},
    )


def section_joint(conn: sqlite3.Connection, panel: Panel) -> dict[str, Any]:
    """Can a factor plus a day filter together reach 85%?

    The selector picks the top ``k`` names by momentum-20 each day. The result
    is split by the same-day base rate so that "the market was up" is not
    counted as selector skill.
    """
    start = pd.Timestamp("2025-01-01")
    factor = build_factors(panel)["mom_20"]
    out: dict[str, Any] = {}
    for horizon in (5, 20):
        net = panel.forward_net(horizon)
        net = net[net.index >= start]
        factor_slice = factor.reindex(index=net.index, columns=net.columns)
        rows = []
        for day in net.index:
            x = factor_slice.loc[day]
            y = net.loc[day]
            mask = x.notna() & y.notna()
            if int(mask.sum()) < 500:
                continue
            xs, ys = x[mask], y[mask]
            base = float((ys > FLAT_BAND).mean())
            ranks = xs.rank(ascending=False)
            for k in (1, 5, 20):
                selected = ys[ranks <= k]
                rows.append(
                    {
                        "day": day,
                        "k": k,
                        "hit": float((selected > FLAT_BAND).mean()),
                        "base": base,
                        "n": int(mask.sum()),
                    }
                )
        frame = pd.DataFrame(rows)
        if frame.empty:
            continue
        summary: dict[str, Any] = {}
        for k, group in frame.groupby("k"):
            quartiles = group["base"].quantile([0.5, 0.9]).to_dict()
            high = group[group["base"] >= quartiles[0.9]]
            summary[f"k={k}"] = {
                "days": int(len(group)),
                "selected_hit_mean": round(float(group["hit"].mean()), 4),
                "share_days_hit_ge_085": round(float((group["hit"] >= 0.85).mean()), 4),
                "base_q50": round(float(quartiles[0.5]), 4),
                "base_q90": round(float(quartiles[0.9]), 4),
                "on_top_decile_base_days": {
                    "days": int(len(high)),
                    "selected_hit_mean": round(float(high["hit"].mean()), 4) if len(high) else None,
                    "share_days_hit_ge_085": round(float((high["hit"] >= 0.85).mean()), 4) if len(high) else None,
                },
            }
        out[f"{horizon}d"] = summary
    # --- can the day filter be made ex ante? -------------------------------
    # The split above uses the same-day base rate, which is computed from the
    # forward window and is therefore look-ahead. This block asks the only
    # question that matters for a real system: is there a feature available
    # *before* the trade that ranks days by that base rate?
    daily = panel.close.pct_change(fill_method=None)
    equal_weight = daily.mean(axis=1)
    ma20 = panel.close.rolling(20).mean()
    ex_ante = pd.DataFrame(
        {
            "market_ret_20": (1.0 + equal_weight).rolling(20).apply(np.prod, raw=True) - 1.0,
            "breadth_ma20": (panel.close > ma20).mean(axis=1),
            "mean_vol_20": daily.rolling(20).std().mean(axis=1),
            "dispersion_20": (panel.close / panel.close.shift(20) - 1.0).std(axis=1),
        }
    )
    day_filter: dict[str, Any] = {}
    for horizon in (5, 20):
        net = panel.forward_net(horizon)
        net = net[net.index >= start]
        base = (net > FLAT_BAND).mean(axis=1)
        table = ex_ante.reindex(base.index)
        forward_base = base
        predictive = {
            name: round(float(table[name].corr(forward_base, method="spearman")), 4)
            for name in table.columns
        }
        # composite: average of trailing-250d percentiles of each feature,
        # signed so that a higher value means "better day" per the correlation
        signed = pd.DataFrame(index=table.index)
        for name in table.columns:
            percentile = table[name].rolling(250, min_periods=60).rank(pct=True)
            sign = 1.0 if (predictive[name] or 0) >= 0 else -1.0
            signed[name] = sign * percentile
        composite = signed.mean(axis=1)
        top_days = composite >= composite.quantile(0.9)
        bottom_days = composite <= composite.quantile(0.1)
        factor_slice = factor.reindex(index=net.index, columns=net.columns)
        hits_ex_ante = []
        for day in net.index[top_days.reindex(net.index).fillna(False)]:
            x = factor_slice.loc[day]
            y = net.loc[day]
            mask = x.notna() & y.notna()
            if int(mask.sum()) < 500:
                continue
            top_one = y[mask][x[mask].rank(ascending=False) <= 1]
            hits_ex_ante.append(float((top_one > FLAT_BAND).mean()))
        day_filter[f"{horizon}d"] = {
            "spearman_feature_vs_day_base": predictive,
            "top_decile_days": int(top_days.sum()),
            "top_decile_mean_base": round(float(base[top_days.reindex(base.index).fillna(False)].mean()), 4),
            "all_days_mean_base": round(float(base.mean()), 4),
            "bottom_decile_mean_base": round(float(base[bottom_days.reindex(base.index).fillna(False)].mean()), 4),
            "ex_ante_top_decile_top1_hit": round(float(np.mean(hits_ex_ante)), 4) if hits_ex_ante else None,
            "ex_ante_top_decile_days": len(hits_ex_ante),
        }
    # --- out-of-sample split of the day filter ------------------------------
    # The in-sample correlation above is not evidence. Fix the feature set and
    # the rule on 2025, then evaluate 2026 untouched. A filter that reverses
    # sign out of sample is noise regardless of how it looked in 2025.
    oos: dict[str, Any] = {}
    mom = build_factors(panel)["mom_20"]
    for horizon in (5, 20):
        net = panel.forward_net(horizon)
        net = net[net.index >= start]
        base = (net > FLAT_BAND).mean(axis=1)
        table = pd.DataFrame({"base": base, "score": ex_ante.rank(pct=True).mean(axis=1)}).dropna()
        year_report = {}
        for year in (2025, 2026):
            subset = table[table.index.year == year]
            if len(subset) < 30:
                continue
            q90 = subset["score"].quantile(0.9)
            top = subset[subset["score"] >= q90]
            hits_all, hits_top = [], []
            for day in subset.index:
                x = mom.reindex(index=net.index, columns=net.columns).loc[day]
                y = net.loc[day]
                mask = x.notna() & y.notna()
                if int(mask.sum()) < 500:
                    continue
                hit = float((y[mask][x[mask].rank(ascending=False) <= 1] > FLAT_BAND).mean())
                hits_all.append(hit)
                if day in top.index:
                    hits_top.append(hit)
            year_report[str(year)] = {
                "days": int(len(subset)),
                "spearman_score_vs_base": round(float(subset["score"].corr(subset["base"], method="spearman")), 3),
                "mean_base": round(float(subset["base"].mean()), 4),
                "top_decile_days": int(len(top)),
                "top_decile_mean_base": round(float(top["base"].mean()), 4) if len(top) else None,
                "top1_all_days_hit": round(float(np.mean(hits_all)), 4) if hits_all else None,
                "top1_all_days_n": len(hits_all),
                "top1_on_top_decile_hit": round(float(np.mean(hits_top)), 4) if hits_top else None,
                "top1_on_top_decile_n": len(hits_top),
            }
        oos[f"{horizon}d"] = year_report
    best = 0.0
    for horizon_bucket in out.values():
        for k_bucket in horizon_bucket.values():
            best = max(best, k_bucket.get("on_top_decile_base_days", {}).get("selected_hit_mean") or 0.0)
    return verdict(
        "joint",
        "measured",
        f"momentum-20 top-k selection, split by same-day base rate; best conditional hit {round(best, 4)}",
        {"by_horizon": out, "ex_ante_day_filter": day_filter, "out_of_sample_by_year": oos},
    )



# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--section", default=",".join(SECTION_NAMES), help="comma separated subset")
    parser.add_argument("--max-codes", type=int, default=None, help="debug: restrict the price panel")
    args = parser.parse_args(list(argv) if argv is not None else None)

    wanted = {piece.strip() for piece in args.section.split(",") if piece.strip()}
    unknown = wanted - set(SECTION_NAMES)
    if unknown:
        raise SystemExit(f"unknown sections: {sorted(unknown)}")

    conn = _connect(Path(args.db))
    panel = Panel.load(conn, max_codes=args.max_codes)
    pred = _load_predictions(conn, panel.latest_date)
    scored = score_predictions(pred, panel)

    results: list[dict[str, Any]] = []
    if "data" in wanted:
        results.append(section_data(conn, panel))
    if "predictions" in wanted:
        results.append(section_predictions(conn, pred, scored))
    if "baseline" in wanted:
        results.append(section_baseline(conn, panel, scored))
    if "confidence" in wanted:
        results.append(section_confidence(conn, pred, scored))
    if "selectivity" in wanted:
        results.append(section_selectivity(conn, panel))
    if "factors" in wanted:
        results.append(section_factors(conn, panel))
    if "joint" in wanted:
        results.append(section_joint(conn, panel))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(args.db),
        "latest_bar": str(panel.latest_date.date()) if panel.latest_date is not None else None,
        "sections": results,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"db={args.db} latest_bar={payload['latest_bar']}")
        for item in results:
            print(f"\n[{item['section']}] {item['status']}: {item['detail']}")
            _print_section(item)
    return 0


def _print_section(item: dict[str, Any]) -> None:
    section = item["section"]
    ev = item["evidence"]
    if section == "data":
        for row in ev.get("per_year", []):
            print(f"  {row['year']}: codes={row['codes']} days={row['days']} rows={row['rows']}")
        print(f"  column fill: {ev.get('column_fill')}")
        print(f"  calendar: {ev.get('trading_calendar_rows')} rows, latest {ev.get('trading_calendar_latest')}")
    elif section == "predictions":
        print(
            f"  blocks: code/as_of={ev.get('blocks_code_as_of')} "
            f"strategy/code/as_of={ev.get('blocks_strategy_code_as_of')}"
        )
        print(f"  orders matched (target date): {ev.get('orders_matched_target_date')}/{ev.get('paper_orders')}")
        print(f"  orders matched (window): {ev.get('orders_matched_window')}/{ev.get('paper_orders')}")
        print(f"  stuck pending_market_data: {ev.get('stuck_pending_market_data')}")
        print(f"  outcomes: {ev.get('outcomes')}")
        print(f"  direction mix: {ev.get('direction_mix')}")
        print(f"  as_of dates: {ev.get('as_of_dates')}")
    elif section == "baseline":
        for horizon, row in ev.get("per_horizon", {}).items():
            print(
                f"  {horizon}d: blocks={row['unique_blocks']} hit={row['hit_rate']} "
                f"lcb={row['wilson_lcb']} uplift={row['uplift_vs_same_day_base']}"
            )
        for row in ev.get("per_date", []):
            print(
                f"    {row['as_of']} {row['horizon_days']}d: rows={row['rows']} blocks={row['unique_blocks']} "
                f"hit={row['hit_rate']} base={row['same_day_base']}"
            )
    elif section == "confidence":
        print(f"  combos={ev.get('combo_count')} max={ev.get('max_confidence')} rows>=0.85={ev.get('rows_ge_085')}")
        print(f"  one-to-one with (horizon,direction): {ev.get('one_to_one_with_horizon_direction')}")
        print(f"  raw confidence Brier: {ev.get('raw_confidence_brier')}")
        for horizon, row in ev.get("monotonicity_by_horizon", {}).items():
            print(f"    {horizon}d spearman={row['spearman']} buckets={row['buckets']}")
    elif section == "selectivity":
        for horizon, row in ev.get("base_rate_by_horizon", {}).items():
            print(f"  {horizon}: mean_base={row['mean_base']} days>=0.85={row['days_ge_085']}/{row['days']}")
            for name, regime in row["regimes"].items():
                print(
                    f"    {name}: days={regime['days']} mean_base={regime['mean_base']} "
                    f"share>=0.85={regime['share_ge_085']}"
                )
        print(f"  required cross-sectional std for 85%: {ev.get('required_std_for_85')}")
    elif section == "joint":
        for horizon, buckets in ev.get("by_horizon", {}).items():
            for k, row in buckets.items():
                top = row["on_top_decile_base_days"]
                print(
                    f"  {horizon} {k}: hit={row['selected_hit_mean']} share>=0.85={row['share_days_hit_ge_085']} "
                    f"| top-decile base days hit={top['selected_hit_mean']} share>=0.85={top['share_days_hit_ge_085']}"
                )
        for horizon, years in ev.get("out_of_sample_by_year", {}).items():
            for year, row in years.items():
                print(
                    f"  OOS {horizon} {year}: spearman={row['spearman_score_vs_base']} "
                    f"top10%base={row['top_decile_mean_base']} all={row['mean_base']} "
                    f"| top1 hit all={row['top1_all_days_hit']} (n={row['top1_all_days_n']}) "
                    f"on top10%={row['top1_on_top_decile_hit']} (n={row['top1_on_top_decile_n']})"
                )
        for horizon, row in ev.get("ex_ante_day_filter", {}).items():
            print(
                f"  ex-ante {horizon}: top-decile days={row['top_decile_days']} base={row['top_decile_mean_base']} "
                f"(all={row['all_days_mean_base']}, bottom={row['bottom_decile_mean_base']}) "
                f"top1 hit={row['ex_ante_top_decile_top1_hit']} on {row['ex_ante_top_decile_days']} days"
            )
            print(f"    feature vs day-base spearman: {row['spearman_feature_vs_day_base']}")
    elif section == "factors":
        for row in ev.get("top_by_uplift", []):
            top = row.get("top_0.001", {})
            print(
                f"  {row['key']}: IC={row.get('rank_ic_mean')} IR={row.get('rank_ic_ir')} "
                f"top0.1%=hit {top.get('mean_hit')} uplift {top.get('mean_uplift')} "
                f"days>=0.85 {top.get('share_days_ge_085')}"
            )


if __name__ == "__main__":
    sys.exit(main())
