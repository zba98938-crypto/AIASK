"""TDX sync service — Phase 2 of TDX migration.

Provides 13 incremental sync tasks for the 8 TDX-specific tables and the
``stocks`` / ``market_blocks`` / ``block_stocks`` shared tables. Each task is
idempotent; a failure of one task should not block the others.

The service is invoked from ``data_sync_scheduler.run_once`` after the
existing K-line / financial phases (so we keep the legacy pipeline as a
fallback during the cut-over).

Tasks:
- sync_stock_basic              全 A 股票名称 → ``stocks``
- sync_sector_basic             板块列表 + 成分 → ``market_blocks`` / ``block_stocks``
- sync_relation                 板块归属 → ``tdx_relation``
- sync_more_info                88 字段每日快照 → ``tdx_stock_extra``
- sync_consensus                GO 一致预期 → ``tdx_consensus``
- sync_gpjy_daily               个股 GP 字段（按 _by_date 取最新）→ ``tdx_gpjy_daily``
- sync_bkjy_daily               板块 BK 字段 → ``tdx_bkjy_daily``
- sync_scjy_daily               市场 SC 字段 → ``tdx_scjy_daily``
- sync_kzz_basic                可转债基础数据 → ``tdx_kzz_basic``
- sync_ipo_events               新股新债申购 → ``events`` 表
- sync_divid_events             分红配股 → ``events`` 表（增量）
- sync_financial_pro            专业财务 (FN) → ``tdx_financial_pro``
- sync_trading_dates            交易日历 → ``trading_dates``
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, date
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# 默认 GP/BK/SC/FN 字段集合（取信号最强、字段最稳定的子集；
# 全字段同步可通过 .env 覆盖）
#
# 注意：通达信"股票数据包"是付费功能。免费版本下 GP/BK/SC 仅以下子集
# 实测能返回真值（probe_tdx_all + probe_shapes 实测）。其他字段（龙虎榜、
# 融资融券明细、陆股通、大宗交易等）需购买股票数据包。
# 用 ``TDX_SYNC_GP_FIELDS / TDX_SYNC_BK_FIELDS / TDX_SYNC_SC_FIELDS``
# 环境变量覆盖；或通过 ``TDX_SYNC_FREE_TIER=0`` 关闭"仅免费字段"过滤。
FREE_GP_FIELDS = ["GP03", "GP11", "GP12", "GP13", "GP25", "GP36"]
FULL_GP_FIELDS = [
    "GP01", "GP02", "GP03", "GP04", "GP06", "GP07", "GP11", "GP12", "GP13",
    "GP14", "GP15", "GP16", "GP21", "GP24", "GP25", "GP31", "GP42",
]

FREE_BK_FIELDS = ["BK9", "BK12", "BK13", "BK17"]   # 涨跌家数 / 涨停家数 / 跌停家数 / 开盘成交
FULL_BK_FIELDS = [
    "BK5", "BK6", "BK7", "BK9", "BK10", "BK11", "BK12",
    "BK15", "BK16", "BK17", "BK18", "BK19",
]

FREE_SC_FIELDS = ["SC01", "SC02", "SC20", "SC25", "SC36"]
FULL_SC_FIELDS = [
    "SC01", "SC02", "SC03", "SC04", "SC11", "SC15", "SC16", "SC17",
    "SC20", "SC23", "SC25", "SC27", "SC28", "SC30", "SC31", "SC34",
    "SC38", "SC42",
]


def _resolve_field_set(env_key: str, free: list, full: list) -> list:
    """根据 env 选择字段集合：``TDX_SYNC_<KIND>_FIELDS`` 优先，
    否则 ``TDX_SYNC_FREE_TIER=1``（默认）= 免费集；``=0`` = 完整集。"""
    explicit = os.getenv(env_key, "").strip()
    if explicit:
        return [t.strip() for t in explicit.split(",") if t.strip()]
    if os.getenv("TDX_SYNC_FREE_TIER", "1") in ("0", "false", "no"):
        return list(full)
    return list(free)


DEFAULT_GP_FIELDS = _resolve_field_set("TDX_SYNC_GP_FIELDS", FREE_GP_FIELDS, FULL_GP_FIELDS)
DEFAULT_BK_FIELDS = _resolve_field_set("TDX_SYNC_BK_FIELDS", FREE_BK_FIELDS, FULL_BK_FIELDS)
DEFAULT_SC_FIELDS = _resolve_field_set("TDX_SYNC_SC_FIELDS", FREE_SC_FIELDS, FULL_SC_FIELDS)

# 项目内常用的 FN 字段（参照 TDX_DATA_SOURCE_MIGRATION_PLAN.md 附录 A）
DEFAULT_FN_FIELDS = [
    "FN1",    # 基本每股收益
    "FN4",    # 每股净资产
    "FN6",    # 净资产收益率
    "FN40",   # 资产总计
    "FN63",   # 负债合计
    "FN72",   # 所有者权益合计
    "FN107",  # 经营现金流净额
    "FN159",  # 流动比率
    "FN160",  # 速动比率
    "FN183",  # 营收增长率
    "FN184",  # 净利润增长率
    "FN197",  # 净资产收益率
    "FN199",  # 销售净利率
    "FN202",  # 销售毛利率
    "FN206",  # 扣非净利润
    "FN210",  # 资产负债率
    "FN230",  # 营业收入
    "FN232",  # 归母净利润
    "FN238",  # 总股本
    "FN239",  # 流通A股
    "FN308",  # 近一年归母净利润
    "FN319",  # TTM 营业总收入
]

# GO 字段：完整 47 项（一次性单点查询，开销小）
DEFAULT_GO_FIELDS = [f"GO{i}" for i in range(1, 48)]


def _split_value_pairs(payload: Any) -> Tuple[Optional[float], Optional[float]]:
    """tqcenter 的 GP/BK/SC ``Value`` 通常是长度 2 的字符串列表。"""
    if isinstance(payload, list):
        if len(payload) == 0:
            return None, None
        a = payload[0] if len(payload) >= 1 else None
        b = payload[1] if len(payload) >= 2 else None
        return _to_float(a), _to_float(b)
    if isinstance(payload, dict):
        return _to_float(payload.get("Value")), None
    return _to_float(payload), None


def _to_float(val: Any) -> Optional[float]:
    if val is None or val == "" or val == "--":
        return None
    try:
        v = float(val)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


class _DerivedSyncMixin:
    async def _sync_derived_factory_market_data(self, db) -> Dict[str, Any]:
        """Derive factory-facing DB tables from TDX raw tables."""
        north = await self._derive_north_fund_flow(db)
        margin_market = await self._derive_margin_market_flow(db)
        margin_detail = await self._derive_margin_detail(db)
        internals = await self._derive_factory_market_internals(db)
        return {
            "north_fund_flow": north,
            "margin_market_flow": margin_market,
            "margin_detail": margin_detail,
            "strategy_factory_market_internals": internals,
        }

    async def _derive_north_fund_flow(self, db) -> Dict[str, Any]:
        inserted = 0
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    trade_date,
                    MAX(CASE WHEN sc_code = 'SC02' THEN value_a END) AS hgt,
                    MAX(CASE WHEN sc_code = 'SC20' THEN value_a END) AS sgt
                FROM tdx_scjy_daily
                WHERE sc_code IN ('SC02', 'SC20')
                GROUP BY trade_date
                ORDER BY trade_date DESC
                LIMIT 120
                """
            )
            for row in rows:
                hgt = _to_float(row.get("hgt"))
                sgt = _to_float(row.get("sgt"))
                if hgt is None and sgt is None:
                    continue
                north_money = (hgt or 0.0) + (sgt or 0.0)
                await conn.execute(
                    """
                    INSERT INTO north_fund_flow (
                        trade_date, north_money, south_money, net_amount,
                        hgt, sgt, updated_at
                    ) VALUES ($1, $2, NULL, $2, $3, $4, CURRENT_TIMESTAMP)
                    ON CONFLICT (trade_date) DO UPDATE SET
                        north_money = EXCLUDED.north_money,
                        net_amount = EXCLUDED.net_amount,
                        hgt = EXCLUDED.hgt,
                        sgt = EXCLUDED.sgt,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    row.get("trade_date"),
                    north_money,
                    hgt,
                    sgt,
                )
                inserted += 1
        if hasattr(db, "save_tdx_data_completeness"):
            await db.save_tdx_data_completeness(
                "north_fund_flow",
                "ok" if inserted else "missing",
                row_count=inserted,
                detail={"source": "tdx_scjy_daily", "fields": ["SC02", "SC20"]},
            )
        return {"updated": inserted}

    async def _derive_margin_market_flow(self, db) -> Dict[str, Any]:
        inserted = 0
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    trade_date,
                    MAX(CASE WHEN sc_code = 'SC01' THEN value_a END) AS rzye,
                    MAX(CASE WHEN sc_code = 'SC25' THEN value_a END) AS rzmre,
                    MAX(CASE WHEN sc_code = 'SC25' THEN value_b END) AS rqmcl
                FROM tdx_scjy_daily
                WHERE sc_code IN ('SC01', 'SC25')
                GROUP BY trade_date
                ORDER BY trade_date DESC
                LIMIT 120
                """
            )
            for row in rows:
                rzye = _to_float(row.get("rzye"))
                rzmre = _to_float(row.get("rzmre"))
                rqmcl = _to_float(row.get("rqmcl"))
                if rzye is None and rzmre is None and rqmcl is None:
                    continue
                await conn.execute(
                    """
                    INSERT INTO margin_market_flow (
                        trade_date, exchange_id, rzye, rzmre, rzche,
                        rqye, rqmcl, rqyl, rzrqye, updated_at
                    ) VALUES ($1, 'TDX', $2, $3, NULL, NULL, $4, NULL, $2, CURRENT_TIMESTAMP)
                    ON CONFLICT (trade_date, exchange_id) DO UPDATE SET
                        rzye = EXCLUDED.rzye,
                        rzmre = EXCLUDED.rzmre,
                        rqmcl = EXCLUDED.rqmcl,
                        rzrqye = EXCLUDED.rzrqye,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    row.get("trade_date"),
                    rzye,
                    rzmre,
                    rqmcl,
                )
                inserted += 1
        if hasattr(db, "save_tdx_data_completeness"):
            await db.save_tdx_data_completeness(
                "margin_market_flow",
                "ok" if inserted else "missing",
                row_count=inserted,
                detail={"source": "tdx_scjy_daily", "fields": ["SC01", "SC25"]},
            )
        return {"updated": inserted}

    async def _derive_margin_detail(self, db) -> Dict[str, Any]:
        inserted = 0
        async with db.acquire() as conn:
            latest_date = await conn.fetchval("SELECT MAX(trade_date) FROM tdx_gpjy_daily")
            if latest_date is None:
                if hasattr(db, "save_tdx_data_completeness"):
                    await db.save_tdx_data_completeness(
                        "margin_detail",
                        "missing",
                        row_count=0,
                        detail={"source": "tdx_gpjy_daily", "reason": "no_rows"},
                    )
                return {"updated": 0}
            rows = await conn.fetch(
                """
                SELECT
                    code,
                    MAX(CASE WHEN gp_code = 'GP03' THEN value_a END) AS rzye,
                    MAX(CASE WHEN gp_code = 'GP11' THEN value_a END) AS rzmre,
                    MAX(CASE WHEN gp_code = 'GP11' THEN value_b END) AS rzche,
                    MAX(CASE WHEN gp_code = 'GP12' THEN value_a END) AS rqye,
                    MAX(CASE WHEN gp_code = 'GP12' THEN value_b END) AS rqmcl,
                    MAX(CASE WHEN gp_code = 'GP13' THEN value_a END) AS rzrqye
                FROM tdx_gpjy_daily
                WHERE trade_date = $1
                  AND gp_code IN ('GP03', 'GP11', 'GP12', 'GP13')
                GROUP BY code
                """,
                latest_date,
            )
            for row in rows:
                values = {
                    "rzye": _to_float(row.get("rzye")),
                    "rzmre": _to_float(row.get("rzmre")),
                    "rzche": _to_float(row.get("rzche")),
                    "rqye": _to_float(row.get("rqye")),
                    "rqmcl": _to_float(row.get("rqmcl")),
                    "rzrqye": _to_float(row.get("rzrqye")),
                }
                if all(v is None for v in values.values()):
                    continue
                code = str(row.get("code") or "")
                ts_code = code if "." in code else f"{code}.TDX"
                await conn.execute(
                    """
                    INSERT INTO margin_detail (
                        trade_date, ts_code, rzye, rqye, rzmre,
                        rqyl, rzche, rqchl, rqmcl, rzrqye, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, NULL, $6, NULL, $7, $8, CURRENT_TIMESTAMP)
                    ON CONFLICT (trade_date, ts_code) DO UPDATE SET
                        rzye = EXCLUDED.rzye,
                        rqye = EXCLUDED.rqye,
                        rzmre = EXCLUDED.rzmre,
                        rzche = EXCLUDED.rzche,
                        rqmcl = EXCLUDED.rqmcl,
                        rzrqye = EXCLUDED.rzrqye,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    latest_date,
                    ts_code,
                    values["rzye"],
                    values["rqye"],
                    values["rzmre"],
                    values["rzche"],
                    values["rqmcl"],
                    values["rzrqye"],
                )
                inserted += 1
        if hasattr(db, "save_tdx_data_completeness"):
            await db.save_tdx_data_completeness(
                "margin_detail",
                "ok" if inserted else "missing",
                as_of_date=latest_date,
                row_count=inserted,
                detail={"source": "tdx_gpjy_daily", "fields": ["GP03", "GP11", "GP12", "GP13"]},
            )
        return {"updated": inserted}

    async def _derive_factory_market_internals(self, db) -> Dict[str, Any]:
        snapshot_date = datetime.now().strftime("%Y-%m-%d")
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH ranked AS (
                    SELECT
                        code, time, close, volume,
                        ROW_NUMBER() OVER (PARTITION BY code ORDER BY time DESC) AS rn
                    FROM kline_1d
                    WHERE code NOT LIKE 'sh%' AND code NOT LIKE 'sz399%'
                ),
                pivoted AS (
                    SELECT
                        code,
                        MAX(CASE WHEN rn = 1 THEN close END) AS c1,
                        MAX(CASE WHEN rn = 1 THEN volume END) AS v1,
                        MAX(CASE WHEN rn = 5 THEN close END) AS c5,
                        MAX(CASE WHEN rn = 20 THEN close END) AS c20,
                        AVG(CASE WHEN rn BETWEEN 2 AND 20 THEN volume END) AS vavg
                    FROM ranked
                    WHERE rn <= 20
                    GROUP BY code
                )
                SELECT
                    COUNT(*) AS symbol_count,
                    SUM(CASE WHEN c5 IS NOT NULL AND c1 > c5 THEN 1 ELSE 0 END) AS trend_up_count,
                    SUM(CASE WHEN c5 IS NOT NULL AND c1 < c5 THEN 1 ELSE 0 END) AS trend_down_count,
                    AVG(CASE WHEN c5 > 0 THEN (c1 - c5) / c5 * 100 END) AS avg_return_5d,
                    AVG(CASE WHEN c20 > 0 THEN (c1 - c20) / c20 * 100 END) AS avg_return_20d,
                    AVG(CASE WHEN vavg > 0 THEN v1 / vavg END) AS avg_volume_ratio
                FROM pivoted
                WHERE c1 IS NOT NULL
                """
            )
        payload = dict(row or {})
        symbol_count = int(payload.get("symbol_count") or 0)
        trend_up = int(payload.get("trend_up_count") or 0)
        trend_down = int(payload.get("trend_down_count") or 0)
        breadth_score = ((trend_up - trend_down) / symbol_count) if symbol_count else 0.0
        sector_summary = {}
        if hasattr(db, "get_sector_rotation_summary"):
            try:
                sector_summary = await db.get_sector_rotation_summary(limit=5)
            except Exception:
                sector_summary = {}
        margin_summary = {}
        if hasattr(db, "get_recent_margin_summary"):
            try:
                margin_summary = await db.get_recent_margin_summary(days=10, sample_limit=10)
            except Exception:
                margin_summary = {}
        item = {
            "snapshot_date": snapshot_date,
            "engine": "tdx_db_derived_v1",
            "symbol_count": symbol_count,
            "trend_up_count": trend_up,
            "trend_down_count": trend_down,
            "avg_return_5d": round(float(payload.get("avg_return_5d") or 0.0), 4),
            "avg_return_20d": round(float(payload.get("avg_return_20d") or 0.0), 4),
            "avg_volume_ratio": round(float(payload.get("avg_volume_ratio") or 1.0), 4),
            "breadth_score": round(float(breadth_score), 6),
            "margin_proxy_5d_change_pct": (margin_summary or {}).get("margin_balance_change_5d") or 0.0,
            "hot_sectors": list((sector_summary or {}).get("hot_sectors") or []),
            "cold_sectors": list((sector_summary or {}).get("cold_sectors") or []),
            "metadata": {
                "source": "tdx_sync_service",
                "sector_source": (sector_summary or {}).get("source"),
                "margin_source": (margin_summary or {}).get("source"),
            },
        }
        saved = {}
        if hasattr(db, "save_factory_market_internal_snapshot"):
            saved = await db.save_factory_market_internal_snapshot(item)
        if hasattr(db, "save_tdx_data_completeness"):
            await db.save_tdx_data_completeness(
                "strategy_factory_market_internals",
                "ok" if symbol_count else "missing",
                as_of_date=snapshot_date,
                row_count=1 if symbol_count else 0,
                detail={"source": "kline_1d+tdx_bkjy_daily+margin_market_flow"},
            )
        return {"updated": 1 if saved else 0, "symbol_count": symbol_count}

    async def _sync_external_gap_data(self, db) -> Dict[str, Any]:
        from .external_gap_sync_service import ExternalGapSyncService

        svc = ExternalGapSyncService(universe=self.universe)
        return await svc.run_all(db)
