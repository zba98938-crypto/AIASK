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


class _MarketSyncMixin:
    async def _sync_trading_dates(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        start = (datetime.now().replace(month=1, day=1)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        result = await asyncio.to_thread(
            data_source.get_trading_dates,
            "SH", start, end, -1,
        )
        if not result.get("success"):
            return {"inserted": 0, "reason": result.get("message", "")}
        dates: List[str] = result.get("data") or []
        inserted = 0
        async with db.acquire() as conn:
            for d in dates:
                if not d or len(d) != 8 or not d.isdigit():
                    continue
                await conn.execute(
                    """
                    INSERT INTO trading_dates (trade_date, exchange, is_open)
                    VALUES ($1, 'SSE', 1)
                    ON CONFLICT (trade_date) DO UPDATE SET is_open = 1
                    """,
                    d,
                )
                inserted += 1
        return {"inserted": inserted, "first": dates[0] if dates else None,
                "last": dates[-1] if dates else None}

    # ------------------------------------------------------------------
    # 2. stock_basic
    # ------------------------------------------------------------------

    async def _sync_stock_basic(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        rows = await asyncio.to_thread(data_source.get_tdx_stock_list, "5", 1)
        if not rows:
            return {"inserted": 0}
        inserted = 0
        async with db.acquire() as conn:
            for item in rows:
                code = item.get("full_code") or item.get("code") or ""
                name = item.get("name") or ""
                if not code or not name:
                    continue
                # 兼容 stocks 表中的 stock_code / market 列
                # market: 后缀 -> 'SH' / 'SZ' / 'BJ'
                market = code.split(".")[-1] if "." in code else ""
                bare = code.split(".")[0] if "." in code else code
                await conn.execute(
                    """
                    INSERT INTO stocks (stock_code, stock_name, market, updated_at)
                    VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                    ON CONFLICT (stock_code) DO UPDATE SET
                        stock_name = EXCLUDED.stock_name,
                        market = EXCLUDED.market,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    bare, name, market,
                )
                inserted += 1
        return {"inserted": inserted}

    # ------------------------------------------------------------------
    # 3. quote snapshots
    # ------------------------------------------------------------------

    async def _sync_quote_snapshots(self, db) -> Dict[str, Any]:
        """Sync latest quote snapshots into stock_quotes from TDX only."""
        from ..data_source import data_source

        limit = int(os.getenv("TDX_SYNC_QUOTE_LIMIT", str(self.limit_more_info)))
        codes = await self._resolve_universe(db, limit)
        updated = 0
        tried = 0
        for code in codes:
            tried += 1
            try:
                raw = await asyncio.to_thread(data_source.get_realtime_quote, code)
                quote = raw.get("data") if isinstance(raw, dict) and raw.get("success") else raw
                if not isinstance(quote, dict) or not quote:
                    continue
                bare = str(code or quote.get("code") or "").split(".", 1)[0]
                payload = {
                    "code": bare,
                    "name": quote.get("name") or quote.get("stock_name"),
                    "price": _to_float(quote.get("price") or quote.get("last") or quote.get("close")),
                    "change_amt": _to_float(quote.get("change_amt") or quote.get("change")),
                    "change_pct": _to_float(quote.get("change_pct") or quote.get("pct_chg")),
                    "open": _to_float(quote.get("open")),
                    "high": _to_float(quote.get("high")),
                    "low": _to_float(quote.get("low")),
                    "prev_close": _to_float(quote.get("prev_close") or quote.get("pre_close")),
                    "volume": int(_to_float(quote.get("volume")) or 0),
                    "amount": _to_float(quote.get("amount")),
                    "pe": _to_float(quote.get("pe") or quote.get("pe_ttm")),
                    "pb": _to_float(quote.get("pb")),
                    "mkt_cap": _to_float(quote.get("mkt_cap") or quote.get("market_cap")),
                }
                if payload["price"] is None:
                    continue
                await db.save_quote(payload)
                updated += 1
            except Exception as exc:
                logger.debug("[TdxSync] quote %s: %s", code, exc)
        return {"updated": updated, "tried": tried}

    # ------------------------------------------------------------------
    # 4. index klines
    # ------------------------------------------------------------------

    async def _sync_index_klines(self, db) -> Dict[str, Any]:
        """Sync common index daily bars into kline_1d using prefixed codes."""
        from ..data_source import data_source

        index_map = {
            "sh000001": "000001.SH",
            "sh000300": "399300.SZ",
            "sz399001": "399001.SZ",
            "sz399006": "399006.SZ",
        }
        limit = int(os.getenv("TDX_SYNC_INDEX_KLINE_LIMIT", "500"))
        updated = 0
        rejected = 0
        for stored_code, tdx_code in index_map.items():
            try:
                raw_rows = await asyncio.to_thread(data_source.get_kline, tdx_code, "daily", limit)
            except Exception as exc:
                logger.debug("[TdxSync] index kline %s: %s", stored_code, exc)
                continue
            rows: list[dict] = []
            for item in list(raw_rows or []):
                if not isinstance(item, dict):
                    continue
                row = dict(item)
                row["code"] = stored_code
                rows.append(row)
            if not rows:
                continue
            try:
                report = await db.save_klines(stored_code, rows)
                updated += int(report.get("accepted_count") or 0)
                rejected += int(report.get("rejected_count") or 0)
            except Exception as exc:
                logger.debug("[TdxSync] save index kline %s: %s", stored_code, exc)
        return {"updated": updated, "rejected": rejected, "index_count": len(index_map)}

    # ------------------------------------------------------------------
    # 5. sector_basic
    # ------------------------------------------------------------------

    async def _sync_sector_basic(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        sectors = await asyncio.to_thread(data_source.get_sector_list, 1)
        if not sectors:
            result = {"sectors": 0, "members": 0}
            result["completeness"] = await self._save_tdx_completeness_snapshot(
                db,
                "sync_sector_basic",
                [
                    ("market_blocks", "updated_at", ""),
                    ("block_stocks", "updated_at", ""),
                ],
            )
            return result

        # 行业一级用 list 16；其他自定义板块来自 880xxx
        # 这里把所有 880xxx 板块同步到 market_blocks，成份股同步到 block_stocks
        wrote_blocks = 0
        wrote_members = 0
        async with db.acquire() as conn:
            for sec in sectors:
                bcode = sec.get("block_code") or ""
                bname = sec.get("block_name") or ""
                if not bcode:
                    continue
                # block_type 这里用 "tdx" 占位（具体 type 由 tdx_relation 给）
                await conn.execute(
                    """
                    INSERT INTO market_blocks (block_code, block_name, block_type, updated_at)
                    VALUES ($1, $2, 'tdx', CURRENT_TIMESTAMP)
                    ON CONFLICT (block_code, block_type) DO UPDATE SET
                        block_name = EXCLUDED.block_name,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    bcode, bname,
                )
                wrote_blocks += 1

            # 拉前 N 个板块的成分（避免一次性几千次调用）
            limit = int(os.getenv("TDX_SYNC_SECTOR_LIMIT", "60"))
            for sec in sectors[:limit]:
                bcode = sec.get("block_code") or ""
                if not bcode:
                    continue
                try:
                    members = await asyncio.to_thread(
                        data_source.get_stock_list_in_sector, bcode, 0, 0
                    )
                except Exception:
                    members = []
                for m in members or []:
                    code = m if isinstance(m, str) else (m.get("Code") if isinstance(m, dict) else "")
                    if not code:
                        continue
                    bare = code.split(".")[0] if "." in code else code
                    await conn.execute(
                        """
                        INSERT INTO block_stocks (block_code, stock_code, updated_at)
                        VALUES ($1, $2, CURRENT_TIMESTAMP)
                        ON CONFLICT (block_code, stock_code) DO UPDATE SET
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        bcode, bare,
                    )
                    wrote_members += 1
        result = {"sectors": wrote_blocks, "members": wrote_members}
        result["completeness"] = await self._save_tdx_completeness_snapshot(
            db,
            "sync_sector_basic",
            [
                ("market_blocks", "updated_at", ""),
                ("block_stocks", "updated_at", ""),
            ],
        )
        return result

    # ------------------------------------------------------------------
    # 4. more_info → tdx_stock_extra
    # ------------------------------------------------------------------

    async def _sync_gpjy_daily(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        codes = await self._resolve_universe(db, self.limit_gpjy)
        if not codes or not DEFAULT_GP_FIELDS:
            return {"updated": 0}
        # 实测：``get_gpjy_value_by_date(0,0)`` 全部返 ``--``；
        # 用 ``get_gpjy_value`` + 历史范围（最近 30 天）才能拿到真值。
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.now().strftime("%Y%m%d")
        start = (_dt.now() - _td(days=30)).strftime("%Y%m%d")

        chunk = 30
        ok_codes = 0
        for i in range(0, len(codes), chunk):
            batch = codes[i:i + chunk]
            try:
                data = await asyncio.to_thread(
                    data_source.get_gpjy_value,
                    batch, DEFAULT_GP_FIELDS, start, end,
                )
            except Exception as exc:
                logger.debug("[TdxSync] gpjy batch %s: %s", i, exc)
                continue
            for code, payload in (data or {}).items():
                if code == "ErrorId" or not isinstance(payload, dict):
                    continue
                rows: list[dict] = []
                for gp_code, items in payload.items():
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        date_val = item.get("Date")
                        a, b = _split_value_pairs(item.get("Value"))
                        if a is None and b is None:
                            continue
                        rows.append({
                            "trade_date": date_val,
                            "gp_code": gp_code,
                            "value_a": a, "value_b": b,
                        })
                if rows:
                    try:
                        await db.save_tdx_gpjy_daily(code, rows)
                        ok_codes += 1
                    except Exception:
                        continue
        return {"updated": ok_codes}

    # ------------------------------------------------------------------
    # 8. bkjy_daily → tdx_bkjy_daily
    # ------------------------------------------------------------------

    async def _sync_bkjy_daily(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        # 拉所有 880xxx 板块代码；限制并发量
        sectors = await asyncio.to_thread(data_source.get_sector_list, 0)
        block_codes = [s.get("block_code") for s in sectors if s.get("block_code", "").startswith("88")]
        block_codes = block_codes[:int(os.getenv("TDX_SYNC_BKJY_LIMIT", "600"))]
        if not block_codes or not DEFAULT_BK_FIELDS:
            return {"updated": 0}
        # 实测：``_by_date(0,0)`` 全 ``--``，需用历史范围调用
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.now().strftime("%Y%m%d")
        start = (_dt.now() - _td(days=30)).strftime("%Y%m%d")

        chunk = 20
        ok_blocks = 0
        for i in range(0, len(block_codes), chunk):
            batch = block_codes[i:i + chunk]
            try:
                data = await asyncio.to_thread(
                    data_source.get_bkjy_value,
                    batch, DEFAULT_BK_FIELDS, start, end,
                )
            except Exception as exc:
                logger.debug("[TdxSync] bkjy batch %s: %s", i, exc)
                continue
            for bcode, payload in (data or {}).items():
                if bcode == "ErrorId" or not isinstance(payload, dict):
                    continue
                rows: list[dict] = []
                for bk_code, items in payload.items():
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        date_val = item.get("Date")
                        a, b = _split_value_pairs(item.get("Value"))
                        if a is None and b is None:
                            continue
                        rows.append({
                            "trade_date": date_val,
                            "bk_code": bk_code,
                            "value_a": a, "value_b": b,
                        })
                if rows:
                    try:
                        await db.save_tdx_bkjy_daily(bcode, rows)
                        ok_blocks += 1
                    except Exception:
                        continue
        return {"updated": ok_blocks, "tried": len(block_codes)}

    # ------------------------------------------------------------------
    # 9. scjy_daily → tdx_scjy_daily
    # ------------------------------------------------------------------

    async def _sync_scjy_daily(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        if not DEFAULT_SC_FIELDS:
            return {"updated": 0}
        # 实测：``_by_date(0,0)`` 返 ``--``，用历史范围（最近 30 天）
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.now().strftime("%Y%m%d")
        start = (_dt.now() - _td(days=30)).strftime("%Y%m%d")

        try:
            data = await asyncio.to_thread(
                data_source.get_scjy_value,
                DEFAULT_SC_FIELDS, start, end,
            )
        except Exception as exc:
            return {"updated": 0, "error": str(exc)}
        if not isinstance(data, dict):
            return {"updated": 0}
        rows: list[dict] = []
        for sc_code, items in data.items():
            if sc_code == "ErrorId" or not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                date_val = item.get("Date")
                a, b = _split_value_pairs(item.get("Value"))
                if a is None and b is None:
                    continue
                rows.append({"trade_date": date_val, "sc_code": sc_code,
                             "value_a": a, "value_b": b})
        if rows:
            await db.save_tdx_scjy_daily(rows)
        return {"updated": len(rows)}

    # ------------------------------------------------------------------
    # 10. kzz_basic → tdx_kzz_basic
    # ------------------------------------------------------------------

    async def _sync_stock_fund_flow(self, db) -> Dict[str, Any]:
        """从 tdx_stock_extra 的当日主力净流入字段衍生 stock_fund_flow。"""
        from datetime import datetime as _dt

        snapshot_date = _dt.now().strftime("%Y-%m-%d")
        ok_count = 0
        async with db.acquire() as conn:
            # 拉当日 tdx_stock_extra（含 zjl_hb 主力净流入）
            rows = await conn.fetch(
                """
                SELECT t.code, t.zjl_hb, s.stock_name AS name
                FROM tdx_stock_extra t
                LEFT JOIN stocks s ON s.stock_code = t.code
                WHERE t.trade_date = $1
                  AND t.zjl_hb IS NOT NULL
                """,
                snapshot_date,
            )
            for row in rows:
                row = dict(row)
                code_full = row.get("code") or ""
                if not code_full:
                    continue
                bare = code_full.split(".")[0] if "." in code_full else code_full
                main_net_inflow_yuan = (row.get("zjl_hb") or 0.0) * 10000  # 万元 → 元
                try:
                    await conn.execute(
                        """
                        INSERT INTO stock_fund_flow (
                            code, trade_date, name,
                            main_net_inflow, main_inflow_percent,
                            super_large_net_inflow, large_net_inflow,
                            middle_net_inflow, small_net_inflow,
                            source, updated_at
                        ) VALUES (
                            $1, $2, $3, $4, NULL,
                            NULL, NULL, NULL, NULL,
                            'tqcenter.more_info', CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (code, trade_date) DO UPDATE SET
                            name = EXCLUDED.name,
                            main_net_inflow = EXCLUDED.main_net_inflow,
                            source = EXCLUDED.source,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        bare, snapshot_date, row.get("name") or "",
                        float(main_net_inflow_yuan),
                    )
                    ok_count += 1
                except Exception as exc:
                    logger.debug("[TdxSync] stock_fund_flow %s: %s", bare, exc)
        return {"updated": ok_count}

    # ------------------------------------------------------------------
    # 辅助：解析 universe + 解析 FN payload
    # ------------------------------------------------------------------
