#!/usr/bin/env python3
"""Recompute the SCORE_85 loop counters (X / Y-bound / Z) from a runtime DB.

Read-only. Replaces the "scoring SQL" that ``SCORE_85_SOLUTION_PLAN.md`` §10
referred to but never committed.

Why the numbers are defined this way
------------------------------------
* **Z uses ``skill_lcb_5d``, never ``hit_rate_lcb_5d``.** The 2026-10-23
  scoring CSV filled its ``lcb_5d`` column from ``primary_hit_rate_lcb`` for
  combo_vmr (0.4541, a Wilson lower bound on a ~0.54 hit rate, positive for
  nearly any strategy) and from the skill LCB for every other row.  combo's
  real skill LCB is negative (-0.087 on 10-20, -0.074 on 10-23), so the plan's
  "only combo reaches 85" path rested on the wrong field.
* **Y is reported as an upper bound, not a score.** The skill component is 20
  points and needs 5d skill LCB >= +10%; the plan's rubric gives every other
  component at most 95-20=75 (evidence 35 + consistency 10 + risk 20 +
  execution 10).  A strategy without LCB >= +10% therefore cannot reach 85
  whatever else it scores.  ``y_upper_bound`` counts strategies that clear
  that necessary condition; it is not a claim they score 85.
* **Session labels.** Rows dated on a weekend, or after the newest
  fully-synced ``kline_1d`` session, are counted separately: they cannot be
  joined to market data.

Usage::

    python scripts/check_score85_progress.py            # authoritative DB
    python scripts/check_score85_progress.py --db PATH  # e.g. a snapshot
    python scripts/check_score85_progress.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

SKILL_LCB_FOR_FULL_SKILL = 0.10
FULL_SESSION_MIN_CODES = 1000
INACTIVE_DECISIONS = ("rejected", "deprecated", "archived")


def _default_db() -> tuple[Path, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/aiask-quant-core/src"))
    from aiask_quant_core.runtime_paths import resolve_sqlite_path_with_source

    return resolve_sqlite_path_with_source()


def _one(con: sqlite3.Connection, sql: str, *args: Any) -> Any:
    row = con.execute(sql, args).fetchone()
    return row[0] if row else None


def _synced_session(con: sqlite3.Connection) -> dict[str, Any]:
    latest_raw = _one(con, "SELECT MAX(time) FROM kline_1d")
    if not latest_raw:
        return {"latest_bar_date": None, "synced_session": None, "coverage": {}}
    latest = date.fromisoformat(str(latest_raw)[:10])
    coverage: dict[str, int] = {}
    synced = None
    for offset in range(21):
        day = latest - timedelta(days=offset)
        n = _one(
            con,
            "SELECT COUNT(*) FROM kline_1d WHERE time >= ? AND time < ?",
            day.isoformat(),
            (day + timedelta(days=1)).isoformat(),
        ) or 0
        if n:
            coverage[day.isoformat()] = int(n)
        if synced is None and n >= FULL_SESSION_MIN_CODES:
            synced = day
            break
    return {
        "latest_bar_date": latest.isoformat(),
        "synced_session": synced.isoformat() if synced else None,
        "coverage": coverage,
    }


def _label_audit(con: sqlite3.Connection, table: str, column: str, synced: str | None, since: str) -> dict[str, Any]:
    rows = con.execute(
        f"SELECT substr({column}, 1, 10) AS d, COUNT(*) FROM {table} WHERE {column} >= ? GROUP BY d ORDER BY d",
        (since,),
    ).fetchall()
    weekend: dict[str, int] = {}
    ahead: dict[str, int] = {}
    for day, n in rows:
        try:
            parsed = date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        if parsed.weekday() >= 5:
            weekend[day] = n
        elif synced and day > synced:
            ahead[day] = n
    return {"weekend_labels": weekend, "ahead_of_synced_session": ahead}


def collect(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    session = _synced_session(con)
    synced = session["synced_session"]
    since = (date.fromisoformat(synced) - timedelta(days=14)).isoformat() if synced else "2026-01-01"

    latest_metrics = """
        WITH l AS (
          SELECT m.*, ROW_NUMBER() OVER (PARTITION BY strategy_id ORDER BY metric_date DESC, id DESC) rn
          FROM strategy_incubation_metrics m)
        SELECT strategy_id, metric_date, decision, effective_n_5d, hit_rate_5d,
               hit_rate_lcb_5d, skill_lcb_5d, nav, max_drawdown
        FROM l WHERE rn = 1"""
    rows = [dict(zip(
        ("strategy_id", "metric_date", "decision", "effective_n_5d", "hit_rate_5d",
         "hit_rate_lcb_5d", "skill_lcb_5d", "nav", "max_drawdown"), r))
        for r in con.execute(latest_metrics)]
    tracked = [r for r in rows if str(r["decision"] or "").lower() not in INACTIVE_DECISIONS]
    measurable = [r for r in tracked if (r["effective_n_5d"] or 0) > 0]
    positive = sorted(
        (r for r in measurable if r["skill_lcb_5d"] is not None and r["skill_lcb_5d"] > 0),
        key=lambda r: r["skill_lcb_5d"], reverse=True,
    )
    y_bound = [r for r in positive if r["skill_lcb_5d"] >= SKILL_LCB_FOR_FULL_SKILL]
    # Rows where the hit-rate LCB would be mistaken for skill: shows how much
    # the 10-23 CSV's column mix-up inflates a "positive LCB" count.
    hit_lcb_positive = sum(1 for r in measurable if (r["hit_rate_lcb_5d"] or 0) > 0)

    def _counts(sql: str, *args: Any) -> dict[str, int]:
        return {str(k): int(v) for k, v in con.execute(sql, args).fetchall()}

    result = {
        "db": str(db_path),
        "market_data": session,
        "x_measurable_pool": len(measurable),
        "tracked_strategies": len(tracked),
        "z_positive_skill_lcb_5d": len(positive),
        "z_list": [
            {k: r[k] for k in ("strategy_id", "metric_date", "effective_n_5d", "skill_lcb_5d", "nav", "max_drawdown")}
            for r in positive[:20]
        ],
        "y_upper_bound_skill_lcb_ge_10pct": len(y_bound),
        "max_skill_lcb_5d": max((r["skill_lcb_5d"] for r in measurable if r["skill_lcb_5d"] is not None), default=None),
        "hit_rate_lcb_positive_not_skill": hit_lcb_positive,
        "pipeline": {
            "calibration_models": _counts("SELECT status, COUNT(*) FROM calibration_models GROUP BY 1"),
            "prediction_executions": _one(con, "SELECT COUNT(*) FROM prediction_executions"),
            "outcomes_by_status": _counts(
                "SELECT score_status, COUNT(*) FROM strategy_trade_prediction_outcomes GROUP BY 1"
            ),
            "factory_runs_since": _counts(
                "SELECT status, COUNT(*) FROM strategy_factory_runs WHERE started_at >= ? GROUP BY 1", since
            ),
            "signal_forward_returns_latest": {
                str(k): v for k, v in con.execute(
                    "SELECT forward_days, MAX(calculated_at) FROM signal_forward_returns GROUP BY 1"
                ).fetchall()
            },
        },
        "session_label_audit": {
            "strategy_signals.signal_date": _label_audit(con, "strategy_signals", "signal_date", synced, since),
            "strategy_incubation_metrics.metric_date": _label_audit(
                con, "strategy_incubation_metrics", "metric_date", synced, since
            ),
            "paper_nav.nav_date": _label_audit(con, "paper_nav", "nav_date", synced, since),
            "strategy_trade_predictions.prediction_as_of": _label_audit(
                con, "strategy_trade_predictions", "prediction_as_of", synced, since
            ),
        },
    }
    con.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--db", type=Path, help="SQLite path (default: authoritative runtime DB)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.db:
        db_path, source = args.db, "--db"
    else:
        db_path, source = _default_db()
    result = collect(Path(db_path))
    result["db_source"] = source
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    md = result["market_data"]
    print(f"db: {result['db']} ({source})")
    print(f"kline_1d: latest bar {md['latest_bar_date']}, newest synced session {md['synced_session']}")
    print(f"X measurable pool (tracked, effN>0): {result['x_measurable_pool']} of {result['tracked_strategies']} tracked")
    print(f"Z skill_lcb_5d > 0: {result['z_positive_skill_lcb_5d']}  (max {result['max_skill_lcb_5d']})")
    for item in result["z_list"]:
        print(f"    {item['strategy_id']}  lcb={item['skill_lcb_5d']:.4f}  nav={item['nav']}  mdd={item['max_drawdown']}")
    print(f"Y upper bound (skill_lcb_5d >= {SKILL_LCB_FOR_FULL_SKILL:.0%}): {result['y_upper_bound_skill_lcb_ge_10pct']}")
    print(f"  (hit_rate_lcb_5d > 0 — NOT skill, for contrast: {result['hit_rate_lcb_positive_not_skill']})")
    print("pipeline:", json.dumps(result["pipeline"], ensure_ascii=False, default=str))
    for name, audit in result["session_label_audit"].items():
        if audit["weekend_labels"] or audit["ahead_of_synced_session"]:
            print(f"label audit {name}: weekend={audit['weekend_labels']} ahead_of_sync={audit['ahead_of_synced_session']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
