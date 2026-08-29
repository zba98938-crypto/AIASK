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


class _FinancialSyncMixin:
    async def _sync_more_info(self, db) -> Dict[str, Any]:
        """每日 88 字段快照同步 → tdx_stock_extra；同时把 PE/PB/市值/换手率/股息率
        回写到 ``stocks`` 表，让工厂的 ``list_stock_universe`` 直接命中。"""
        from ..data_source import data_source

        codes = await self._resolve_universe(db, self.limit_more_info)
        ok_count = 0
        for code in codes:
            try:
                info = await asyncio.to_thread(data_source.get_more_info, code)
                if not info:
                    continue
                await db.save_tdx_stock_extra(code, info)
                # 同步 stocks 表（PE/PB/市值/换手率/股息率/行业代理）
                bare = code.split(".")[0] if "." in code else code
                pe_ttm = _to_float(info.get("StaticPE_TTM"))
                pb_mrq = _to_float(info.get("PB_MRQ"))
                zsz = _to_float(info.get("Zsz"))         # 总市值（亿元）
                turnover = _to_float(info.get("fHSL"))   # 换手率
                div_yield = _to_float(info.get("DYRatio"))  # 股息率
                async with db.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE stocks SET
                            pe_ratio = COALESCE($2, pe_ratio),
                            pb_ratio = COALESCE($3, pb_ratio),
                            market_cap = COALESCE($4, market_cap),
                            turnover_rate = COALESCE($5, turnover_rate),
                            dividend_yield = COALESCE($6, dividend_yield),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE stock_code = $1
                        """,
                        bare, pe_ttm, pb_mrq,
                        # 把"亿元"转成"元"以匹配 stocks.market_cap 的项目约定
                        zsz * 1e8 if zsz is not None else None,
                        turnover, div_yield,
                    )
                ok_count += 1
            except Exception as exc:
                logger.debug("[TdxSync] more_info %s: %s", code, exc)
        return {"updated": ok_count, "tried": len(codes)}

    # ------------------------------------------------------------------
    # 5. consensus → tdx_consensus
    # ------------------------------------------------------------------

    async def _sync_consensus(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        codes = await self._resolve_universe(db, self.limit_consensus)
        if not codes:
            return {"updated": 0}
        # 一次性多股查询；失败时分小批
        chunk = 50
        ok_count = 0
        snapshot_date = datetime.now().strftime("%Y-%m-%d")
        for i in range(0, len(codes), chunk):
            batch = codes[i:i + chunk]
            try:
                data = await asyncio.to_thread(
                    data_source.get_gp_one_data, batch, DEFAULT_GO_FIELDS
                )
            except Exception as exc:
                logger.debug("[TdxSync] consensus batch %s: %s", i, exc)
                continue
            for code, payload in (data or {}).items():
                if code == "ErrorId":
                    continue
                try:
                    await db.save_tdx_consensus(code, payload, snapshot_date=snapshot_date)
                    ok_count += 1
                except Exception:
                    continue
        return {"updated": ok_count}

    # ------------------------------------------------------------------
    # 6. relation → tdx_relation
    # ------------------------------------------------------------------

    async def _sync_relation(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        codes = await self._resolve_universe(db, self.limit_relation)
        ok_count = 0
        for code in codes:
            try:
                rel = await asyncio.to_thread(data_source.get_relation, code)
                if not rel:
                    continue
                await db.save_tdx_relation(code, rel)
                # ── 把"行业"板块名作为 stocks.industry 兜底（rs_hyname 缺失时保命） ──
                bare = code.split(".")[0] if "." in code else code
                first_industry = next(
                    (r.get("block_name") for r in rel
                     if r.get("block_type") == "行业" and r.get("block_name")),
                    None,
                )
                first_concept = next(
                    (r.get("block_name") for r in rel
                     if r.get("block_type") == "概念" and r.get("block_name")),
                    None,
                )
                first_region = next(
                    (r.get("block_name") for r in rel
                     if r.get("block_type") == "地区" and r.get("block_name")),
                    None,
                )
                if first_industry or first_concept or first_region:
                    async with db.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE stocks SET
                                industry = COALESCE(NULLIF(industry, ''), $2),
                                sector = COALESCE(NULLIF(sector, ''), $3),
                                tdx_industry = COALESCE(NULLIF(tdx_industry, ''), $2),
                                tdx_region = COALESCE(NULLIF(tdx_region, ''), $4),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE stock_code = $1
                            """,
                            bare,
                            first_industry, first_industry, first_region,
                        )
                ok_count += 1
            except Exception as exc:
                logger.debug("[TdxSync] relation %s: %s", code, exc)
        result = {"updated": ok_count}
        result["completeness"] = await self._save_tdx_completeness_snapshot(
            db,
            "sync_relation",
            [("tdx_relation", "updated_at", "")],
        )
        return result

    # ------------------------------------------------------------------
    # 7. gpjy_daily → tdx_gpjy_daily
    # ------------------------------------------------------------------

    async def _sync_financial_pro(self, db) -> Dict[str, Any]:
        from ..data_source import data_source

        codes = await self._resolve_universe(db, self.limit_financial)
        # 只取最近一年
        year_start = (datetime.now().replace(month=1, day=1)).strftime("%Y0101")
        ok_codes = 0
        chunk = 5
        for i in range(0, len(codes), chunk):
            batch = codes[i:i + chunk]
            try:
                data = await asyncio.to_thread(
                    data_source.get_financial_data,
                    batch, DEFAULT_FN_FIELDS,
                    year_start, "",
                    "announce_time",
                )
            except Exception as exc:
                logger.debug("[TdxSync] financial batch %s: %s", i, exc)
                continue
            # data: {code: DataFrame} 或 {code: list[dict]}
            for code, df_or_rows in (data or {}).items():
                if code == "ErrorId":
                    continue
                rows = self._extract_fn_rows(df_or_rows)
                if not rows:
                    continue
                try:
                    await db.save_tdx_financial(code, rows)
                    ok_codes += 1
                except Exception:
                    continue
        return {"updated": ok_codes, "tried": len(codes)}

    # ------------------------------------------------------------------
    # 14. basic_financial → 通过 get_stock_info 把基础财务写入 ``financials`` 表
    #
    # tqcenter 的 ``get_financial_data`` 需要客户端单独下载"专业财务数据
    # 包"。在该数据包未下载时所有 FN 字段返回 ``"--"``。本任务用 always-on
    # 的 ``get_stock_info`` 接口拿基础财务（营收、净利润、ROE、EPS、BVPS、
    # 资产负债等），覆盖大多数业务需求；专业财务包到位后，
    # ``_sync_financial_pro`` 会自动给出更细粒度的 FN 字段。
    # ------------------------------------------------------------------

    async def _sync_basic_financial(self, db) -> Dict[str, Any]:
        from ..data_source import tdx_tqcenter as _tq

        codes = await self._resolve_universe(db, self.limit_financial)
        if not codes:
            return {"updated": 0}

        from datetime import datetime as _dt

        def _report_date(half_year_flag: Any) -> str:
            """根据 J_HalfYearFlag 与当前年份推断报告期 YYYY-MM-DD。"""
            try:
                m = int(half_year_flag) if half_year_flag is not None else 12
            except (TypeError, ValueError):
                m = 12
            year = _dt.now().year
            if m == 3:
                return f"{year}-03-31"
            if m == 6:
                return f"{year}-06-30"
            if m == 9:
                return f"{year}-09-30"
            return f"{year - 1}-12-31"

        ok_count = 0
        async with db.acquire() as conn:
            for code in codes:
                try:
                    info = await asyncio.to_thread(_tq.get_stock_info, code)
                except Exception as exc:
                    logger.debug("[TdxSync] basic_financial %s: %s", code, exc)
                    continue
                if not info or not isinstance(info, dict):
                    continue

                bare = code.split(".")[0] if "." in code else code

                # ── 1. 把行业 / 地区 / 上市日 / 股本回写到 stocks 表 ──
                industry = (info.get("rs_hyname") or "").strip() or None
                region = (info.get("tdx_dyname") or "").strip() or None
                # J_start 是 'YYYYMMDD' 整数字符串
                list_date_raw = (info.get("J_start") or "").strip()
                list_date = None
                if list_date_raw and list_date_raw.isdigit() and len(list_date_raw) == 8:
                    list_date = f"{list_date_raw[:4]}-{list_date_raw[4:6]}-{list_date_raw[6:]}"
                total_shares_wan = _to_float(info.get("J_zgb"))
                float_shares_wan = _to_float(info.get("ActiveCapital"))
                # 万股 → 股
                total_shares = total_shares_wan * 10000 if total_shares_wan else None
                float_shares = float_shares_wan * 10000 if float_shares_wan else None

                await conn.execute(
                    """
                    UPDATE stocks SET
                        industry = COALESCE($2, industry),
                        sector = COALESCE($2, sector),
                        list_date = COALESCE($3, list_date),
                        tdx_industry = COALESCE($2, tdx_industry),
                        tdx_region = COALESCE($4, tdx_region),
                        tdx_listed_date = COALESCE($3, tdx_listed_date),
                        tdx_total_shares = COALESCE($5, tdx_total_shares),
                        tdx_float_shares = COALESCE($6, tdx_float_shares),
                        list_status = COALESCE(list_status, 'L'),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE stock_code = $1
                    """,
                    bare, industry, list_date, region,
                    total_shares, float_shares,
                )

                # ── 2. 计算并写 financials（原逻辑） ──

                # 量纲：J_yysy/J_jly/J_zzc 等单位为"万元"，转回元
                revenue = _to_float(info.get("J_yysy"))
                if revenue is not None:
                    revenue *= 10000
                net_profit = _to_float(info.get("J_jly"))
                if net_profit is not None:
                    net_profit *= 10000
                total_assets = _to_float(info.get("J_zzc"))  # 万元
                total_liab = _to_float(info.get("J_zzc")) and _to_float(info.get("J_jzc"))
                # 实际：负债 = 总资产 - 净资产
                if total_assets is not None:
                    jzc = _to_float(info.get("J_jzc"))
                    debt_ratio = (1 - jzc / total_assets) * 100 if jzc is not None and total_assets > 0 else None
                else:
                    debt_ratio = None

                gross_margin = None
                yyly = _to_float(info.get("J_yyly"))   # 营业利润
                yysy_raw = _to_float(info.get("J_yysy"))  # 万元
                yycb = _to_float(info.get("J_yycb"))   # 营业成本
                if yysy_raw and yysy_raw > 0 and yycb is not None:
                    gross_margin = (yysy_raw - yycb) / yysy_raw * 100
                net_margin = None
                if yysy_raw and yysy_raw > 0 and _to_float(info.get("J_jly")) is not None:
                    net_margin = _to_float(info.get("J_jly")) / yysy_raw * 100

                payload = {
                    "stock_code": bare,
                    "report_date": _report_date(info.get("J_HalfYearFlag")),
                    "revenue": revenue,
                    "net_profit": net_profit,
                    "gross_margin": gross_margin,
                    "net_margin": net_margin,
                    "debt_ratio": debt_ratio,
                    "current_ratio": None,
                    "eps": _to_float(info.get("J_mgsy")),
                    "roe": _to_float(info.get("J_jyl")),
                    "bvps": _to_float(info.get("J_mgjzc")),
                    "roa": None,
                    "revenue_growth": None,
                    "profit_growth": None,
                }
                try:
                    await conn.execute(
                        """
                        INSERT INTO financials (
                            stock_code, report_date, revenue, net_profit,
                            gross_margin, net_margin, debt_ratio, current_ratio,
                            eps, roe, bvps, roa, revenue_growth, profit_growth,
                            updated_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                            CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (stock_code, report_date) DO UPDATE SET
                            revenue=EXCLUDED.revenue,
                            net_profit=EXCLUDED.net_profit,
                            gross_margin=EXCLUDED.gross_margin,
                            net_margin=EXCLUDED.net_margin,
                            debt_ratio=EXCLUDED.debt_ratio,
                            current_ratio=EXCLUDED.current_ratio,
                            eps=EXCLUDED.eps,
                            roe=EXCLUDED.roe,
                            bvps=EXCLUDED.bvps,
                            roa=EXCLUDED.roa,
                            revenue_growth=EXCLUDED.revenue_growth,
                            profit_growth=EXCLUDED.profit_growth,
                            updated_at=CURRENT_TIMESTAMP
                        """,
                        payload["stock_code"], payload["report_date"],
                        payload["revenue"], payload["net_profit"],
                        payload["gross_margin"], payload["net_margin"],
                        payload["debt_ratio"], payload["current_ratio"],
                        payload["eps"], payload["roe"], payload["bvps"],
                        payload["roa"], payload["revenue_growth"], payload["profit_growth"],
                    )
                    ok_count += 1
                except Exception as exc:
                    logger.debug("[TdxSync] financials upsert %s: %s", code, exc)
        return {"updated": ok_count, "tried": len(codes)}

    # ------------------------------------------------------------------
    # 15. stock_fund_flow → 个股资金流
    #
    # 来源：tdx_stock_extra 的 Zjl_HB (主力净流入,万元) + Zjl (主买净额,万元)
    # 设计：避免重复调 SDK，直接从 sync_more_info 已入库的快照衍生写入
    # stock_fund_flow 表，工厂层 (factor_prompt_builder /
    # decision_quant_builder) 的 db-first 路径就能命中。
    #
    # **重要**：免费版通达信在收盘后 Zjl/Zjl_HB 字段返回 0；盘中调才有真值。
    # L2 逐笔成交（TotalBVol/TotalSVol/L2TicNum）需要 L2 行情授权才能拿。
    # 因此本任务在盘后跑出来全是 0，工厂只在盘中跑同步时才有有效数据。
    # ------------------------------------------------------------------
