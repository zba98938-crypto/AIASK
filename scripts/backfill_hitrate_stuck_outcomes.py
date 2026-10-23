#!/usr/bin/env python
"""WP-0.9 一次性回填：把卡死在 ``pending_market_data`` 的 daily outcome 按真实原因落库。

认证快照（docs/archive/hitrate_gates_2026-10-21.json）实测：600 条
``trade_prediction_score_daily_v1`` outcome 永久停在 ``pending_market_data``
（target 2026-10-19 共 200、10-20 共 400），判分器每次跑都原样重写
``pending_market_data``，没有任何重试或终态，记分卡因此只能报"0 scored"。

本脚本做两件事，全部幂等、可重复运行：

1. **重分类**（第一步）：对每条 stuck 行按 ``outcome_retry.classify`` 的证据链
   重新判定，并刷新 ``metadata.retry_attempts`` 与 ``stuck_reason``：

   * 数据**迟到**（日更停在 10-16，target 是 10-19/20）→ 保持
     ``pending_market_data``，reason 写 ``data_sync_stalled`` —— 日更恢复后
     判分器会自然重试并消解，不提前判死；
   * 数据**永久缺失**（该标的自身序列停早于 target、或重试预算耗尽）→ 终态
     ``data_unavailable``，reason 写 ``code_series_stale`` /
     ``daily_bar_missing_after_retry``；
   * 该预测已有 ``trade_prediction_horizon_net_v1`` 判分 → 终态
     ``data_unavailable``，reason 写 ``superseded_by_horizon_score``（WP-A
     的 outcome 才是权威判分，daily 行不再悬着）。

2. **WP-A 判分回补**（第二步）：对所有未定论的预测跑一次 ``HorizonScorer``
   （次日开入场、扣 0.21% 成本、持有 k 根收出场），结果写进
   ``trade_prediction_horizon_net_v1``。已判过的预测被
   ``pending_for_outcome`` 排除，重跑行数不变。

诚实边界：本脚本**不写 0 收益、不造判分**。数据迟到就是迟到
（``data_sync_stalled``，保持 pending），数据永久缺失就是缺失
（``data_unavailable`` + 原因码）。闸门脚本次日复算时，前者仍算 stuck
（数据没回来之前它确实没消解），后者归零 —— 两种终态在输出里分开计数。

Usage::

    /opt/miniconda3/envs/aiask/bin/python scripts/backfill_hitrate_stuck_outcomes.py
    /opt/miniconda3/envs/aiask/bin/python scripts/backfill_hitrate_stuck_outcomes.py --dry-run
    /opt/miniconda3/envs/aiask/bin/python scripts/backfill_hitrate_stuck_outcomes.py --db path --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "akshare-mcp" / "src"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "aiask-quant-core" / "src"))

logger = logging.getLogger("backfill_hitrate_stuck")

DAILY_SCORE_VERSION = "trade_prediction_score_daily_v1"
HORIZON_SCORE_VERSION = "trade_prediction_horizon_net_v1"
REASON_SUPERSEDED = "superseded_by_horizon_score"
STATUS_UNAVAILABLE = "data_unavailable"
STATUS_PENDING = "pending_market_data"


def _importables() -> dict[str, Any]:
    """Lazily import so ``--help`` stays cheap and each dependency is explicit."""
    from akshare_mcp.services.incubation_factory.outcome_retry import (
        StuckOutcome,
        classify,
        last_fully_synced_session,
        latest_bar_date,
        list_stuck_outcomes,
        market_coverage_on,
    )
    from akshare_mcp.services.incubation_factory.horizon_scorer import HorizonScorer
    from strategy_factory.infrastructure.factory_schema_ddl import init_strategy_tables

    return {
        "StuckOutcome": StuckOutcome,
        "classify": classify,
        "last_fully_synced_session": last_fully_synced_session,
        "latest_bar_date": latest_bar_date,
        "list_stuck_outcomes": list_stuck_outcomes,
        "market_coverage_on": market_coverage_on,
        "HorizonScorer": HorizonScorer,
        "init_strategy_tables": init_strategy_tables,
    }


def load_db(db_path: str) -> Any:
    if db_path and db_path != "default":
        from aiask_quant_core.storage.sqlite import SQLiteAdapter

        return SQLiteAdapter(path=db_path)
    from akshare_mcp.storage import get_db

    return get_db()


async def _ensure_tables(db: Any, init_strategy_tables: Any) -> None:
    """Create the WP-A/B/C tables on databases that predate them.

    The DDL body lives in strategy-factory's ``init_strategy_tables``
    (re-exported by quant-core); executing it here is part of wiring the
    backfill — a live db without the retry columns or ``calibration_models``
    cannot take the backfill results. Idempotent.
    """
    async with db.acquire() as conn:
        await init_strategy_tables(conn)


async def _horizon_row_exists(db: Any, prediction_id: str) -> bool:
    try:
        row = await db.fetchrow(
            "SELECT 1 AS one FROM strategy_trade_prediction_outcomes "
            "WHERE prediction_id = $1 AND score_version = $2 LIMIT 1",
            prediction_id,
            HORIZON_SCORE_VERSION,
        )
        return bool(row)
    except Exception:
        return False


async def _persist_reclassification(db: Any, item: Any, *, action: str, reason: str) -> None:
    status = STATUS_UNAVAILABLE if action == "finalize" else STATUS_PENDING
    attempts = int(item.attempts + (1 if action == "keep" else 0))
    try:
        await db.execute(
            "UPDATE strategy_trade_prediction_outcomes SET score_status = $1 "
            "WHERE outcome_id = $2 AND score_version = $3",
            status,
            item.outcome_id,
            DAILY_SCORE_VERSION,
        )
        await db.execute(
            "UPDATE strategy_trade_prediction_outcomes SET metadata = json_set("
            "COALESCE(metadata, '{}'), '$.retry_attempts', $1, '$.stuck_reason', $2) "
            "WHERE outcome_id = $3 AND score_version = $4",
            attempts,
            reason,
            item.outcome_id,
            DAILY_SCORE_VERSION,
        )
    except Exception as exc:
        logger.warning("reclassify write failed for %s: %s", item.outcome_id, exc)


async def _run_horizon_backfill(db: Any, HorizonScorer: Any, *, max_attempts: int) -> dict[str, Any]:
    """One WP-A scoring pass over everything still undetermined.

    ``pending_for_outcome=True`` + ``exclude_outcome_score_version`` (both
    set inside ``verify_pending``) already excludes predictions the horizon
    scorer decided on, so a second run of this script writes nothing new for
    them (重跑行数不变).
    """
    scorer = HorizonScorer(max_retry_attempts=max_attempts)
    try:
        result = await scorer.verify_pending(db, persist=True)
    except Exception as exc:
        logger.warning("horizon backfill failed: %s", exc)
        return {"status": "degraded", "reason": f"horizon_scoring_failed: {exc}"}
    return {
        "status": result.get("status"),
        "score_version": result.get("score_version"),
        "evaluated": int(result.get("evaluated") or 0),
        "score_status_counts": dict(result.get("score_status_counts") or {}),
        "reason_counts": dict(result.get("reason_counts") or {}),
    }


async def _final_statuses(db: Any) -> dict[str, Any]:
    async def _query(version: str) -> dict[str, int]:
        rows = await db.fetch(
            "SELECT score_status, count(*) AS n FROM strategy_trade_prediction_outcomes "
            "WHERE score_version = $1 GROUP BY score_status",
            version,
        )
        return {str(r.get("score_status")): int(r.get("n") or 0) for r in (rows or [])}

    return {
        "daily_statuses": await _query(DAILY_SCORE_VERSION),
        "horizon_statuses": await _query(HORIZON_SCORE_VERSION),
    }


async def run(db: Any, *, limit: int, max_attempts: int, dry_run: bool) -> dict[str, Any]:
    imports = _importables()
    classify = imports["classify"]

    # DDL (retry columns, prediction_executions, calibration_models) runs in both
    # modes: the adapter lazily initialises on first access anyway, and a dry run
    # must not crash on databases that predate WP-A. It writes nothing else.
    await _ensure_tables(db, imports["init_strategy_tables"])

    last_synced = await imports["last_fully_synced_session"](db)
    last_bar = await imports["latest_bar_date"](db)
    stuck = await imports["list_stuck_outcomes"](db, limit=limit, score_version=DAILY_SCORE_VERSION)

    code_latest: dict[str, Optional[date]] = {}
    coverage: dict[date, int] = {}
    counts: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    finalized = 0

    for item in stuck:
        if await _horizon_row_exists(db, item.prediction_id):
            action, reason = "finalize", REASON_SUPERSEDED
        else:
            if item.stock_code not in code_latest:
                code_latest[item.stock_code] = await imports["latest_bar_date"](db, code=item.stock_code)
            target = item.target_trading_date
            if target is not None and target not in coverage:
                coverage[target] = await imports["market_coverage_on"](db, target)
            action, reason = classify(
                item,
                latest_bar=last_bar,
                code_latest_bar=code_latest.get(item.stock_code),
                last_synced_session=last_synced,
                market_codes_on_target=coverage.get(target) if target else None,
            )
        counts[reason] = counts.get(reason, 0) + 1
        if action == "finalize":
            finalized += 1
        if not dry_run:
            await _persist_reclassification(db, item, action=action, reason=reason)
        if len(examples) < 10:
            examples.append(
                {
                    "prediction_id": item.prediction_id,
                    "code": item.stock_code,
                    "target": item.target_trading_date.isoformat() if item.target_trading_date else None,
                    "attempts": item.attempts,
                    "reason": reason,
                }
            )

    result: dict[str, Any] = {
        "step": "reclassify_stuck" + ("" if dry_run else " + horizon_backfill"),
        "dry_run": dry_run,
        "score_version": DAILY_SCORE_VERSION,
        "latest_bar": last_bar.isoformat() if last_bar else None,
        "last_synced_session": last_synced.isoformat() if last_synced else None,
        "examined": len(stuck),
        "reason_counts": counts,
        "finalized": finalized,
        "still_pending": len(stuck) - finalized,
        "examples": examples,
    }
    if not dry_run:
        result["horizon_backfill"] = await _run_horizon_backfill(
            db, imports["HorizonScorer"], max_attempts=max_attempts
        )
        result["final_state"] = await _final_statuses(db)
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="default", help="SQLite path, or 'default' for the shared live db")
    parser.add_argument("--limit", type=int, default=2000, help="max stuck rows to reclassify")
    parser.add_argument("--max-attempts", type=int, default=5, help="retry budget carried into the horizon pass")
    parser.add_argument("--dry-run", action="store_true", help="classify and report, write nothing")
    args = parser.parse_args(argv)

    db = load_db(args.db)
    result = asyncio.run(run(db, limit=args.limit, max_attempts=args.max_attempts, dry_run=args.dry_run))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
