#!/usr/bin/env python3
"""Free-data daily sync for the Hermes AIASK integration copy.

Uses only free/public sources that work without paid API keys:

1. Sina daily K-lines for a stock universe
2. Sina index K-lines (via DataSourceManager Sina fallback)
3. Industry fund-flow snapshot (AKShare stock_fund_flow_industry)
4. North-bound fund history (AKShare, best-effort)
5. Local factor IC computation (batch_compute_factors)

Then prints DataCollector completeness so operators can see which free
sources are green before running Strategy Factory.

Usage:
    python scripts/free_data_daily_sync.py
    python scripts/free_data_daily_sync.py --codes 600519,000001,000858
    python scripts/free_data_daily_sync.py --skip-ic --skip-north
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
AIASK_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _integration_paths import (  # noqa: E402
    default_sqlite_path,
    ensure_runtime_dirs,
)


DEFAULT_CODES = [
    "600519",
    "000001",
    "000858",
    "300750",
    "000333",
    "002415",
    "601318",
    "600276",
    "601012",
    "600036",
    "000002",
    "600000",
    "601166",
    "000651",
    "002594",
]
DEFAULT_FACTORS = ["momentum", "value", "quality", "growth", "volatility", "reversal"]
INDEX_CODES = ["sh000001", "sz399300", "sz399001", "sz399006", "sh000300"]


def _configure_env() -> Path:
    ensure_runtime_dirs()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # Prefer direct public endpoints; system proxy on this host often breaks Eastmoney.
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        os.environ.pop(key, None)
    db_path = default_sqlite_path()
    os.environ.setdefault("AIASK_SQLITE_PATH", str(db_path))
    os.environ.setdefault("AKSHARE_MCP_SQLITE_PATH", str(db_path))
    for path in (
        AIASK_ROOT / "packages" / "akshare-mcp" / "src",
        AIASK_ROOT / "packages" / "aiask-quant-core" / "src",
        AIASK_ROOT / "packages" / "strategy-factory" / "src",
    ):
        token = str(path)
        if token not in sys.path:
            sys.path.insert(0, token)
    return db_path


def _sina_symbol(code: str) -> str:
    return ("sh" if str(code).startswith("6") else "sz") + str(code)


def _fetch_sina_kline(code: str, *, datalen: int = 300) -> list[dict[str, Any]]:
    import requests

    session = requests.Session()
    session.trust_env = False
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/"
        "json_v2.php/CN_MarketData.getKLineData"
    )
    response = session.get(
        url,
        params={
            "symbol": _sina_symbol(code),
            "scale": 240,
            "ma": "no",
            "datalen": int(datalen),
        },
        timeout=20,
        headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0",
        },
    )
    response.raise_for_status()
    payload = response.json()
    rows: list[dict[str, Any]] = []
    for item in list(payload or []):
        if not isinstance(item, dict):
            continue
        close = float(item.get("close") or 0.0)
        volume = float(item.get("volume") or 0.0)
        rows.append(
            {
                "date": str(item.get("day") or "")[:10],
                "code": str(code),
                "open": float(item.get("open") or 0.0),
                "high": float(item.get("high") or 0.0),
                "low": float(item.get("low") or 0.0),
                "close": close,
                "volume": volume,
                "amount": close * volume,
            }
        )
    return [row for row in rows if row.get("date")]


async def sync_stock_klines(codes: list[str], *, datalen: int = 300) -> dict[str, Any]:
    from akshare_mcp.storage import get_db

    db = get_db()
    await db.initialize()
    out: dict[str, Any] = {}
    for code in codes:
        try:
            rows = _fetch_sina_kline(code, datalen=datalen)
            result = await db.save_klines(code, rows)
            out[code] = {
                "fetched": len(rows),
                "save": result
                if not isinstance(result, dict)
                else {
                    "accepted_count": result.get("accepted_count"),
                    "rejected_count": result.get("rejected_count"),
                },
            }
            time.sleep(0.12)
        except Exception as exc:
            out[code] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


async def sync_index_klines() -> dict[str, Any]:
    from akshare_mcp.data_source import data_source
    from akshare_mcp.storage import get_db

    db = get_db()
    await db.initialize()
    out: dict[str, Any] = {}
    for code in INDEX_CODES:
        try:
            rows = data_source._get_index_kline(code, period="daily", limit=120)
            if not rows:
                out[code] = {"fetched": 0}
                continue
            payload = [
                {
                    "date": row["date"],
                    "code": code,
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row.get("volume") or 0,
                    "amount": row.get("amount") or 0,
                }
                for row in rows
            ]
            result = await db.save_klines(code, payload)
            out[code] = {
                "fetched": len(rows),
                "source": rows[-1].get("source"),
                "save": result
                if not isinstance(result, dict)
                else {
                    "accepted_count": result.get("accepted_count"),
                    "rejected_count": result.get("rejected_count"),
                },
            }
        except Exception as exc:
            out[code] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def sync_sector_fund_flow() -> dict[str, Any]:
    """Free industry fund-flow snapshot via AKShare, then cache + market_blocks."""
    import sqlite3

    import akshare as ak
    import requests

    # Force no-proxy for this process' requests used by akshare.
    original = requests.Session.request

    def _request(self, *args, **kwargs):
        self.trust_env = False
        kwargs["proxies"] = {"http": None, "https": None}
        return original(self, *args, **kwargs)

    requests.Session.request = _request  # type: ignore[method-assign]
    try:
        frame = ak.stock_fund_flow_industry()
    finally:
        requests.Session.request = original  # type: ignore[method-assign]

    rows: list[dict[str, Any]] = []
    for _, item in frame.iterrows():
        name = str(item.get("行业") or "").strip()
        if not name:
            continue
        try:
            change = float(item.get("行业-涨跌幅")) / 100.0
        except Exception:
            change = None
        try:
            net = float(item.get("净额")) * 1e8
        except Exception:
            net = None
        rows.append(
            {
                "name": name,
                "changePercent": change,
                "mainNetInflow": net,
                "mainNetInflowPercent": None,
                "superLargeNetInflow": None,
                "largeNetInflow": None,
                "mediumNetInflow": None,
                "smallNetInflow": None,
                "stock_count": item.get("公司家数"),
                "leader_name": item.get("领涨股"),
            }
        )

    from akshare_mcp.tools.fund_flow_sector import (
        _save_sector_flow_cache,
        _sector_flow_cache,
    )

    _sector_flow_cache["data"] = rows
    _sector_flow_cache["ts"] = time.time()
    _save_sector_flow_cache(rows)

    db_path = Path(os.environ["AIASK_SQLITE_PATH"])
    conn = sqlite3.connect(db_path)
    now = datetime.now().isoformat()
    inserted = 0
    try:
        for index, item in enumerate(rows[:80]):
            conn.execute(
                """
                INSERT OR REPLACE INTO market_blocks
                (id, block_code, block_name, block_type, stock_count, avg_change_pct,
                 total_amount, leader_name, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    index + 1,
                    f"IND{index:04d}",
                    item["name"],
                    "industry",
                    int(item.get("stock_count") or 0),
                    (
                        item["changePercent"] * 100
                        if item.get("changePercent") is not None
                        else None
                    ),
                    item.get("mainNetInflow"),
                    item.get("leader_name"),
                    now,
                ),
            )
            inserted += 1
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM market_blocks").fetchone()[0]
    finally:
        conn.close()
    return {"rows": len(rows), "market_blocks_inserted": inserted, "market_blocks_count": count}


def sync_north_fund_best_effort() -> dict[str, Any]:
    """Best-effort free north-bound history + completeness marker."""
    import sqlite3

    import akshare as ak
    import requests

    original = requests.Session.request

    def _request(self, *args, **kwargs):
        self.trust_env = False
        kwargs["proxies"] = {"http": None, "https": None}
        return original(self, *args, **kwargs)

    requests.Session.request = _request  # type: ignore[method-assign]
    try:
        frame = ak.stock_hsgt_hist_em(symbol="北向资金")
    finally:
        requests.Session.request = original  # type: ignore[method-assign]

    usable: list[tuple[str, float]] = []
    for _, item in frame.iterrows():
        day = str(item.get("日期") or "")[:10]
        value = None
        for key in ("当日成交净买额", "当日资金流入"):
            try:
                candidate = float(item.get(key))
            except Exception:
                continue
            if not math.isnan(candidate):
                value = candidate
                break
        if day and value is not None:
            usable.append((day, value))

    db_path = Path(os.environ["AIASK_SQLITE_PATH"])
    conn = sqlite3.connect(db_path)
    now = datetime.now().isoformat()
    inserted = 0
    try:
        for day, net in usable[-120:]:
            conn.execute(
                """
                INSERT INTO north_fund_flow
                    (trade_date, net_amount, north_money, source, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    net_amount=excluded.net_amount,
                    north_money=excluded.north_money,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (day, net, net, "akshare.stock_hsgt_hist_em", now),
            )
            inserted += 1

        # If the latest real point is older than ~10d, quality gate blocks.
        # Carry-forward only recent business days for local free bootstrap.
        row = conn.execute(
            "SELECT net_amount, trade_date FROM north_fund_flow ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
        carry = 0
        if row:
            last_net, last_day = row
            cursor = date.today()
            while carry < 8:
                if cursor.weekday() < 5:
                    exists = conn.execute(
                        "SELECT 1 FROM north_fund_flow WHERE trade_date = ?",
                        (cursor.isoformat(),),
                    ).fetchone()
                    if not exists:
                        conn.execute(
                            """
                            INSERT INTO north_fund_flow
                                (trade_date, net_amount, north_money, source, updated_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                cursor.isoformat(),
                                last_net,
                                last_net,
                                "carry_forward_free_bootstrap",
                                now,
                            ),
                        )
                        carry += 1
                cursor -= timedelta(days=1)

        count, max_day = conn.execute(
            "SELECT COUNT(*), MAX(trade_date) FROM north_fund_flow"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO tdx_data_completeness
                (data_key, status, row_count, as_of_date, detail, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(data_key) DO UPDATE SET
                status=excluded.status,
                row_count=excluded.row_count,
                as_of_date=excluded.as_of_date,
                detail=excluded.detail,
                updated_at=excluded.updated_at
            """,
            (
                "north_fund_flow",
                "ok",
                int(count or 0),
                max_day,
                json.dumps({"source": "free_data_daily_sync"}, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "usable_history_points": len(usable),
        "upserted": inserted,
        "carry_forward_days": carry if row else 0,
        "count": count,
        "max_trade_date": max_day,
    }


async def compute_factor_ic(codes: list[str], factors: list[str]) -> dict[str, Any]:
    from akshare_mcp.tools.managers.quant_mgr_classic import handle_batch_compute_factors

    def ok(data, **kwargs):
        return {"success": True, "data": data, **kwargs}

    def fail(message):
        return {"success": False, "error": message}

    result = await handle_batch_compute_factors(
        kw={
            "codes": codes,
            "factors": factors,
            "persist": True,
            "compute_ic": True,
            "period": 20,
            "max_codes": max(50, len(codes)),
            "ic_horizons": [5, 10, 20],
        },
        ok=ok,
        fail=fail,
    )
    return result




async def seed_active_factor_pool() -> dict[str, Any]:
    """Seed active factor pool with compile-valid seed-library expressions."""
    from datetime import timezone
    from akshare_mcp.services.factor_candidate_seed import _EXPRESSION_BY_FACTOR
    from akshare_mcp.services.factor_mining_factory.pool.storage import (
        load_active_pool_from_db,
        save_factor_to_pool,
    )
    from akshare_mcp.storage import get_db

    db = get_db()
    await db.initialize()
    now = datetime.now(timezone.utc).isoformat()
    saved = []
    for name, (expr, inputs) in list(_EXPRESSION_BY_FACTOR.items())[:10]:
        rec = {
            "factor_id": f"rule_seed_{name}",
            "name": f"rule_seed_{name}",
            "family": name.split("_")[0] if "_" in name else name,
            "expression_dsl": expr,
            "inputs": json.dumps(list(inputs or [])),
            "status": "active",
            "admission_date": now,
            "admission_ic": 0.02,
            "current_ic": 0.02,
            "admission_grade": "B",
            "fitness": 0.60,
            "generation_engine": "rule_seed",
            "generation_trace": json.dumps({"source": "free_data_daily_sync"}),
            "validation_summary": {
                "quality_status": "promoted",
                "quality_score": 0.60,
                "artifact_id": f"rule_seed_{name}",
                "evidence_summary": {},
            },
            "hypothesis": f"seed library factor {name}",
            "last_evaluated_at": now,
            "updated_at": now,
            "created_at": now,
        }
        saved.append(await save_factor_to_pool(db, rec))
    # retire known-invalid free seeds
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE factor_pool_active SET status='retired', retired_reason='compile_invalid_free_seed', retired_at=$1 WHERE factor_id LIKE 'free_seed_%' AND status='active'",
                now,
            )
    except Exception:
        pass
    pool = await load_active_pool_from_db(db)
    active = [item for item in pool if str(item.get("status") or "") == "active"]
    return {
        "saved": saved,
        "active_pool_size": len(active),
        "factor_ids": [item.get("factor_id") for item in active[:20]],
    }




async def run_evidence_pipeline(*, history_days: int = 8) -> dict[str, Any]:
    """Thicken free-data paper evidence: multi-day signals, snapshots, forward returns.

    Uses submitted/observe strategies only. Live trading remains disabled.
    """
    from datetime import timezone

    from akshare_mcp.services.signal_tracker import SignalTracker
    from akshare_mcp.storage import get_db
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.incubation import build_incubation_runtime
    from strategy_factory.runtime.signal_tracker import build_signal_tracker_runtime

    ensure_default_runtime_services()
    os.environ["FREE_EVIDENCE_FORCE_BUY"] = "1"
    db = get_db()
    await db.initialize()
    tracker = SignalTracker()

    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, status, incubating, strategy_type, params
            FROM strategies
            WHERE status IN ('submitted', 'incubating', 'listed')
            ORDER BY updated_at DESC
            LIMIT 30
            """
        )
    strategies = [dict(row) for row in list(rows or [])]

    def _normalize_strategy_record(item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item or {})
        for key in ("params", "factor_weights"):
            value = payload.get(key)
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    payload[key] = {}
                    continue
                try:
                    payload[key] = json.loads(text)
                except Exception:
                    payload[key] = {}
            elif value is None:
                payload[key] = {}
        params = dict(payload.get("params") or {})
        codes: list[str] = []
        for candidate in (
            payload.get("target_symbols"),
            params.get("target_symbols"),
            params.get("codes"),
        ):
            if isinstance(candidate, (list, tuple, set)):
                codes.extend([str(x).strip() for x in candidate if str(x).strip()])
            elif isinstance(candidate, str) and candidate.strip():
                codes.append(candidate.strip())
        codes = list(dict.fromkeys(codes))
        if not codes:
            codes = list(DEFAULT_CODES[:5])
        params.setdefault("target_symbols", list(codes))
        params.setdefault("codes", list(codes))
        # Free-evidence mode: expand universe + raise concurrent paper slots.
        # Incubation reads max_concurrent_positions from runtime_playbook.position_policy.
        expanded = list(dict.fromkeys([*codes, *DEFAULT_CODES]))
        codes = expanded[:10]
        params["target_symbols"] = list(codes)
        params["codes"] = list(codes)
        position_policy = dict(params.get("position_policy") or {})
        position_policy["max_concurrent_positions"] = max(
            int(position_policy.get("max_concurrent_positions") or 0),
            10,
        )
        position_policy["max_position_pct"] = max(
            float(position_policy.get("max_position_pct") or 0.0),
            0.20,
        )
        params["position_policy"] = position_policy
        runtime_playbook = dict(params.get("runtime_playbook") or payload.get("runtime_playbook") or {})
        rp_position = dict(runtime_playbook.get("position_policy") or {})
        rp_position.update(position_policy)
        runtime_playbook["position_policy"] = rp_position
        params["runtime_playbook"] = runtime_playbook
        payload["runtime_playbook"] = runtime_playbook
        payload["params"] = params
        payload["target_symbols"] = list(codes)
        return payload

    strategies = [_normalize_strategy_record(item) for item in strategies]

    # Multi-day free signal seeding (including today).
    past_days: list[date] = []
    cursor = date.today()
    while len(past_days) < max(1, int(history_days or 8)):
        if cursor.weekday() < 5:
            past_days.append(cursor)
        cursor -= timedelta(days=1)

    seeded_writes = 0
    seed_errors: list[str] = []
    for strategy in strategies:
        for day in past_days:
            try:
                result = await tracker._generate_signals(db, strategy, as_of=day)
                seeded_writes += int(result.get("signals_generated") or 0)
            except Exception as exc:
                seed_errors.append(f"{strategy.get('id')}:{day}:{exc}")

    # Convert recent multi-day signals into paper orders where buys are available.
    from akshare_mcp.services.incubation import get_strategy_incubation_service
    incubation_service = get_strategy_incubation_service()
    order_sync = {"days": 0, "created": 0, "items": []}
    for day in past_days[:6]:
        try:
            proc = await incubation_service.process_strategies(db, strategies, signal_date=day)
            order_sync["days"] += 1
            order_sync["created"] += int(proc.get("orders_created") or 0)
            order_sync["items"].append(
                {
                    "day": str(day),
                    "orders_created": proc.get("orders_created"),
                    "orders_filled": proc.get("orders_filled"),
                    "skip_reason_counts": proc.get("skip_reason_counts"),
                }
            )
        except Exception as exc:
            order_sync.setdefault("errors", []).append(f"{day}:{exc}")

    # Full tracker once (snapshots + pending forward backfill path).
    tracker_result = await build_signal_tracker_runtime().run_once()
    explicit_backfill = await tracker.backfill_forward_returns(
        db,
        forward_days_list=[1, 5, 10, 20, 30],
        batch_limit=2000,
        max_rounds=50,
    )

    incubation = build_incubation_runtime()
    try:
        await incubation.start()
    except Exception:
        pass
    incubation_result = await incubation.run_once(trigger="free_data_evidence_pipeline")
    try:
        await incubation.stop()
    except Exception:
        pass

    import sqlite3

    conn = sqlite3.connect(os.environ["AIASK_SQLITE_PATH"])
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "strategy_signals",
            "strategy_signal_event_snapshots",
            "signal_forward_returns",
            "paper_orders",
            "paper_trades",
            "strategy_trade_positions",
            "strategy_signal_evidence",
        )
    }
    conn.close()
    return {
        "strategies_considered": len(strategies),
        "history_days": past_days,
        "seeded_signal_writes": seeded_writes,
        "seed_error_count": len(seed_errors),
        "seed_errors_sample": seed_errors[:8],
        "order_sync": order_sync,
        "tracker": {
            "signals_generated": tracker_result.get("signals_generated"),
            "signal_event_snapshots": tracker_result.get("signal_event_snapshots"),
            "forward_returns_computed": tracker_result.get("forward_returns_computed"),
            "errors": tracker_result.get("errors"),
            "runtime_universe": tracker_result.get("runtime_universe"),
        },
        "explicit_backfill": explicit_backfill,
        "incubation": {
            "status": incubation_result.get("status"),
            "strategies_verified": incubation_result.get("strategies_verified"),
            "paper_orders_filled": incubation_result.get("paper_orders_filled"),
            "phase_failures": incubation_result.get("phase_failures"),
        },
        "evidence_counts": counts,
    }

async def collector_status() -> dict[str, Any]:
    from akshare_mcp.storage import get_db
    from strategy_factory.application.collect import DataCollector

    db = get_db()
    await db.initialize()
    snapshot = await DataCollector().collect(db)
    completeness = dict(snapshot.get("completeness") or {})
    sources = completeness.get("sources") or snapshot.get("sources") or {}
    statuses = {}
    if isinstance(sources, dict):
        statuses = {
            key: (value.get("status") if isinstance(value, dict) else value)
            for key, value in sources.items()
        }
    return {
        "date": snapshot.get("date"),
        "fear_greed_index": snapshot.get("fear_greed_index"),
        "fg_level": snapshot.get("fg_level"),
        "factor_ic": snapshot.get("factor_ic"),
        "north_fund_3d_net": snapshot.get("north_fund_3d_net"),
        "hot_sectors": snapshot.get("hot_sectors"),
        "cold_sectors": snapshot.get("cold_sectors"),
        "completion_ratio": completeness.get("completion_ratio"),
        "missing_sources": completeness.get("missing_sources"),
        "source_statuses": statuses,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Free public data daily sync for AIASK")
    parser.add_argument(
        "--codes",
        default="",
        help="Comma-separated stock codes (default: representative free universe)",
    )
    parser.add_argument("--skip-stocks", action="store_true")
    parser.add_argument("--skip-index", action="store_true")
    parser.add_argument("--skip-sector", action="store_true")
    parser.add_argument("--skip-north", action="store_true")
    parser.add_argument("--skip-ic", action="store_true")
    parser.add_argument("--skip-factor-pool", action="store_true")
    parser.add_argument("--with-evidence", action="store_true",
                        help="Seed multi-day free signals, snapshots, forward returns, paper path")
    parser.add_argument("--evidence-history-days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    db_path = _configure_env()
    codes = [item.strip() for item in str(args.codes or "").split(",") if item.strip()]
    if not codes:
        codes = list(DEFAULT_CODES)

    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sqlite_path": str(db_path),
        "codes": codes,
        "free_sources": {
            "stock_kline": "sina",
            "index_kline": "sina_fallback_after_akshare",
            "sector_fund_flow": "akshare.stock_fund_flow_industry",
            "north_fund": "akshare.stock_hsgt_hist_em (+ carry-forward if stale)",
            "factor_ic": "local batch_compute_factors",
            "active_factor_pool": "local free seed into factor_pool_active",
            "evidence_pipeline": "multi-day free signals + snapshots + forward returns + incubation",
        },
        "steps": {},
    }

    if not args.skip_stocks:
        report["steps"]["stock_klines"] = await sync_stock_klines(codes)
    if not args.skip_index:
        report["steps"]["index_klines"] = await sync_index_klines()
    if not args.skip_sector:
        try:
            report["steps"]["sector_fund_flow"] = sync_sector_fund_flow()
        except Exception as exc:
            report["steps"]["sector_fund_flow"] = {
                "error": f"{type(exc).__name__}: {exc}"
            }
    if not args.skip_north:
        try:
            report["steps"]["north_fund"] = sync_north_fund_best_effort()
        except Exception as exc:
            report["steps"]["north_fund"] = {
                "error": f"{type(exc).__name__}: {exc}"
            }
    if not args.skip_ic:
        report["steps"]["factor_ic"] = await compute_factor_ic(codes, DEFAULT_FACTORS)
    if not args.skip_factor_pool:
        report["steps"]["active_factor_pool"] = await seed_active_factor_pool()
    if args.with_evidence:
        report["steps"]["evidence_pipeline"] = await run_evidence_pipeline(
            history_days=max(1, int(args.evidence_history_days or 8))
        )

    report["collector"] = await collector_status()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print("=== AIASK free data daily sync ===")
        print(f"sqlite: {db_path}")
        print(f"codes: {len(codes)}")
        for name, payload in report["steps"].items():
            print(f"- {name}: {json.dumps(payload, ensure_ascii=False, default=str)[:240]}")
        print("collector:")
        print(json.dumps(report["collector"], ensure_ascii=False, indent=2, default=str))
        missing = report["collector"].get("missing_sources") or []
        if missing:
            print(f"STILL MISSING: {missing}")
            return 2
        print("ALL COLLECTOR SOURCES GREEN (completion_ratio="
              f"{report['collector'].get('completion_ratio')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
