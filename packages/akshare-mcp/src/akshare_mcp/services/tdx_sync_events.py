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


class _EventsSyncMixin:
    async def _sync_kzz_basic(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        listings = await asyncio.to_thread(data_source.get_tdx_stock_list, "32", 1)
        listings = listings[:self.limit_kzz]
        ok_count = 0
        for item in listings:
            code = item.get("full_code") or item.get("code") or ""
            if not code:
                continue
            try:
                cb = await asyncio.to_thread(data_source.get_cb_info, code)
                payload = (cb or {}).get("data") or {}
                if not payload:
                    continue
                # 把 market_data.get_cb_info 返回的契约形式转回 tdx_storage 期望
                # （tdx_storage 期望与 tdx_tqcenter.get_kzz_info 同构）
                kz_payload = {
                    "kzz_code": payload.get("KZZCode") or item.get("code") or code.split(".")[0],
                    "stock_code": payload.get("HSCode"),
                    "set_code": payload.get("set_code"),
                    "convert_price": payload.get("ZGPrice"),
                    "current_rate": payload.get("CurRate"),
                    "remain_size_wan": payload.get("RestScope"),
                    "putback_price": payload.get("PutBack"),
                    "force_redeem_price": payload.get("ForceRedeem"),
                    "convert_date": payload.get("ZGDate"),
                    "end_price": payload.get("EndPrice"),
                    "end_date": payload.get("EndDate"),
                    "convert_rate": payload.get("ZGRate"),
                    "real_value": payload.get("RealValue"),
                    "expire_yield": payload.get("ExpireYield"),
                    "kzz_score": payload.get("KZZScore"),
                    "stock_score": payload.get("HSScore"),
                    "redeem_date": payload.get("RedeemDate"),
                    "redeem_price": payload.get("RedeemPrice"),
                    "put_date": payload.get("PutDate"),
                    "put_price": payload.get("PutPrice"),
                    "convert_code": payload.get("ZGCode"),
                    "stock_price": payload.get("AGPrice"),
                    "kzz_price": payload.get("KZZPrice"),
                    "premium_rate": payload.get("KZZYj"),
                    "convert_value": payload.get("ZGValue"),
                }
                await db.save_tdx_kzz(kz_payload)
                ok_count += 1
            except Exception as exc:
                logger.debug("[TdxSync] kzz %s: %s", code, exc)
        return {"updated": ok_count, "tried": len(listings)}

    # ------------------------------------------------------------------
    # 11. ipo_events
    # ------------------------------------------------------------------

    async def _sync_ipo_events(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        try:
            res = await asyncio.to_thread(data_source.get_ipo_info, 2, 1)
        except Exception as exc:
            return {"inserted": 0, "error": str(exc)}
        if not res.get("success"):
            return {"inserted": 0, "reason": res.get("message")}
        items = res.get("data") or []
        inserted = 0
        async with db.acquire() as conn:
            for it in items:
                sg_date = (it.get("SGDate") or "").replace("-", "")
                code = it.get("code") or it.get("SGCode") or ""
                if not sg_date or not code:
                    continue
                title = f"{it.get('type', '')}申购：{it.get('name', '')} ({code})"
                desc = (
                    f"sg_code={it.get('SGCode')}  sg_price={it.get('SGPrice')}  "
                    f"max_sg={it.get('MaxSG')}  pe_issue={it.get('PE_Issue')}"
                )
                try:
                    # events 表实际 schema: (code, event_type, event_date, title, description)
                    # 用 code+event_type+event_date 作为软主键去重
                    existing = await conn.fetchval(
                        "SELECT 1 FROM events WHERE code=$1 AND event_type='tdx_ipo' AND event_date=$2",
                        code, sg_date,
                    )
                    if existing:
                        continue
                    await conn.execute(
                        """
                        INSERT INTO events (code, event_type, event_date, title, description)
                        VALUES ($1, 'tdx_ipo', $2, $3, $4)
                        """,
                        code, sg_date, title, desc,
                    )
                    inserted += 1
                except Exception as exc:
                    logger.debug("[TdxSync] events upsert ipo %s: %s", code, exc)
        return {"inserted": inserted}

    # ------------------------------------------------------------------
    # 12. divid_events — 增量分红
    # ------------------------------------------------------------------

    async def _sync_divid_events(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        codes = await self._resolve_universe(db, self.limit_relation)
        # 仅同步今年的分红
        year_start = datetime.now().strftime("%Y0101")
        year_end = datetime.now().strftime("%Y%m%d")
        inserted = 0
        async with db.acquire() as conn:
            for code in codes:
                try:
                    rows = await asyncio.to_thread(
                        data_source.get_divid_factors,
                        code, year_start, year_end,
                    )
                except Exception:
                    continue
                bare = code.split(".")[0] if "." in code else code
                for r in rows or []:
                    d = (r.get("date") or "").replace("-", "")
                    if not d:
                        continue
                    title = f"分红/送配：{bare}"
                    desc = (
                        f"type={r.get('type')}  bonus={r.get('bonus')}  "
                        f"share_bonus={r.get('share_bonus')}  allotment={r.get('allotment')}  "
                        f"allot_price={r.get('allot_price')}"
                    )
                    try:
                        exists = await conn.fetchval(
                            "SELECT 1 FROM events WHERE code=$1 AND event_type='tdx_dividend' AND event_date=$2",
                            bare, d,
                        )
                        if exists:
                            continue
                        await conn.execute(
                            """
                            INSERT INTO events (code, event_type, event_date, title, description)
                            VALUES ($1, 'tdx_dividend', $2, $3, $4)
                            """,
                            bare, d, title, desc,
                        )
                        inserted += 1
                    except Exception as exc:
                        logger.debug("[TdxSync] events upsert divid %s: %s", code, exc)
        return {"inserted": inserted}

    # ------------------------------------------------------------------
    # 13. financial_pro → tdx_financial_pro
    # ------------------------------------------------------------------
