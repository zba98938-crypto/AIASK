"""SQLite Mixin for TDX-specific tables (Phase 8).

Write methods for the 8 TDX tables introduced in
``_schema_market_phase_8.py``.

All methods are async and follow the same conventions as ``KlineMixin`` /
``QuotesMixin`` (placeholder-based PG-flavored SQL rewritten by the
SQLiteConnection layer; UPSERT via ``INSERT ... ON CONFLICT ... DO UPDATE``).

Inputs are normalized to plain dicts with the canonical key names defined
in ``data_source.tdx_tqcenter`` so the sync layer can hand the SDK output
through unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == "" or val == "--":
        return None
    try:
        v = float(val)
        return v if v == v else None
    except (TypeError, ValueError) as _swallowed:
        logger.warning("packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/tdx_storage.py:31: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None


def _safe_int(val: Any) -> Optional[int]:
    f = _safe_float(val)
    return int(f) if f is not None else None


def _norm_date(val: Any) -> str:
    """Accept ``YYYYMMDD`` / ``YYYY-MM-DD`` / Timestamp / int and return ``YYYY-MM-DD``."""
    if val is None:
        return ""
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    try:
        s = str(int(float(s)))
    except (TypeError, ValueError) as _swallowed:
        logger.warning("packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/tdx_storage.py:49: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    return s


def _norm_yyyymmdd(val: Any) -> str:
    """Return ``YYYYMMDD`` plain string. Empty for invalid."""
    s = _norm_date(val)
    if s and len(s) == 10 and s[4] == "-":
        return s.replace("-", "")
    if s and len(s) == 8 and s.isdigit():
        return s
    return ""


class TdxStorageMixin:
    """SQLite write methods for TDX phase-8 tables."""

    # ------------------------------------------------------------------
    # tdx_financial_pro
    # ------------------------------------------------------------------

    async def save_tdx_financial(
        self,
        code: str,
        rows: list[dict],
    ) -> dict:
        """Persist long-format FN financial values.

        ``rows`` items: ``{report_date, announce_date, fn_code, value}``.
        report_date / announce_date in ``YYYYMMDD``.
        """
        if not code or not rows:
            return {"accepted": 0, "rejected": 0}
        accepted = 0
        async with self.acquire() as conn:
            for row in rows:
                fn_code = str(row.get("fn_code") or "").upper()
                report_date = _norm_yyyymmdd(row.get("report_date"))
                if not fn_code or not report_date:
                    continue
                announce_date = _norm_yyyymmdd(row.get("announce_date")) or None
                value = _safe_float(row.get("value"))
                await conn.execute(
                    """
                    INSERT INTO tdx_financial_pro
                        (code, report_date, announce_date, fn_code, value, updated_at)
                    VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)
                    ON CONFLICT (code, report_date, fn_code) DO UPDATE SET
                        announce_date = EXCLUDED.announce_date,
                        value = EXCLUDED.value,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    code, report_date, announce_date, fn_code, value,
                )
                accepted += 1
        return {"accepted": accepted, "rejected": 0}

    # ------------------------------------------------------------------
    # tdx_stock_extra
    # ------------------------------------------------------------------

    async def save_tdx_stock_extra(self, code: str, more_info: dict, *, trade_date: str = "") -> dict:
        """Persist a per-day snapshot of get_more_info (88 fields).

        more_info: raw dict from ``tq.get_more_info`` (string values).
        trade_date: ``YYYY-MM-DD`` (defaults to ``HqDate`` from payload).
        """
        if not code or not more_info:
            return {"accepted": 0}
        td = trade_date or _norm_date(more_info.get("HqDate"))
        if not td:
            from datetime import datetime
            td = datetime.now().strftime("%Y-%m-%d")
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tdx_stock_extra (
                    code, trade_date,
                    pe_ttm, pe_dynamic, pb_mrq, ps_ttm, dy_ratio,
                    turnover_rate, volume_ratio, zsz, ltsz, free_float_shares,
                    up_limit, down_limit, zaf, ma5, hist_high_52w, hist_low_52w,
                    fc_amo, fc_b, ever_zt_count, con_zaf_date_num, year_zt_day,
                    zjl_hb, kf_earn_money, rd_input_fee, cash_zj, staff_num,
                    ipo_price, beta_value,
                    recent_buyback_date, recent_release_date, recent_dz_date,
                    report_date, zt_date_recent, dt_date_recent, top_date_recent,
                    stop_jy_date_recent, tp_flag, raw_json, updated_at
                ) VALUES (
                    $1, $2,
                    $3, $4, $5, $6, $7,
                    $8, $9, $10, $11, $12,
                    $13, $14, $15, $16, $17, $18,
                    $19, $20, $21, $22, $23,
                    $24, $25, $26, $27, $28,
                    $29, $30,
                    $31, $32, $33,
                    $34, $35, $36, $37,
                    $38, $39, $40, CURRENT_TIMESTAMP
                )
                ON CONFLICT (code, trade_date) DO UPDATE SET
                    pe_ttm=EXCLUDED.pe_ttm, pe_dynamic=EXCLUDED.pe_dynamic,
                    pb_mrq=EXCLUDED.pb_mrq, ps_ttm=EXCLUDED.ps_ttm, dy_ratio=EXCLUDED.dy_ratio,
                    turnover_rate=EXCLUDED.turnover_rate, volume_ratio=EXCLUDED.volume_ratio,
                    zsz=EXCLUDED.zsz, ltsz=EXCLUDED.ltsz, free_float_shares=EXCLUDED.free_float_shares,
                    up_limit=EXCLUDED.up_limit, down_limit=EXCLUDED.down_limit,
                    zaf=EXCLUDED.zaf, ma5=EXCLUDED.ma5,
                    hist_high_52w=EXCLUDED.hist_high_52w, hist_low_52w=EXCLUDED.hist_low_52w,
                    fc_amo=EXCLUDED.fc_amo, fc_b=EXCLUDED.fc_b,
                    ever_zt_count=EXCLUDED.ever_zt_count,
                    con_zaf_date_num=EXCLUDED.con_zaf_date_num,
                    year_zt_day=EXCLUDED.year_zt_day,
                    zjl_hb=EXCLUDED.zjl_hb, kf_earn_money=EXCLUDED.kf_earn_money,
                    rd_input_fee=EXCLUDED.rd_input_fee, cash_zj=EXCLUDED.cash_zj,
                    staff_num=EXCLUDED.staff_num, ipo_price=EXCLUDED.ipo_price,
                    beta_value=EXCLUDED.beta_value,
                    recent_buyback_date=EXCLUDED.recent_buyback_date,
                    recent_release_date=EXCLUDED.recent_release_date,
                    recent_dz_date=EXCLUDED.recent_dz_date,
                    report_date=EXCLUDED.report_date,
                    zt_date_recent=EXCLUDED.zt_date_recent,
                    dt_date_recent=EXCLUDED.dt_date_recent,
                    top_date_recent=EXCLUDED.top_date_recent,
                    stop_jy_date_recent=EXCLUDED.stop_jy_date_recent,
                    tp_flag=EXCLUDED.tp_flag, raw_json=EXCLUDED.raw_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                code, td,
                _safe_float(more_info.get("StaticPE_TTM")),
                _safe_float(more_info.get("DynaPE")),
                _safe_float(more_info.get("PB_MRQ")),
                _safe_float(more_info.get("MorePE")),  # rough proxy
                _safe_float(more_info.get("DYRatio")),
                _safe_float(more_info.get("fHSL")),
                _safe_float(more_info.get("fLianB")),
                _safe_float(more_info.get("Zsz")),
                _safe_float(more_info.get("Ltsz")),
                _safe_float(more_info.get("FreeLtgb")),
                _safe_float(more_info.get("ZTPrice")),
                _safe_float(more_info.get("DTPrice")),
                _safe_float(more_info.get("ZAF")),
                _safe_float(more_info.get("MA5Value")),
                _safe_float(more_info.get("HisHigh")),
                _safe_float(more_info.get("HisLow")),
                _safe_float(more_info.get("FCAmo")),
                _safe_float(more_info.get("FCb")),
                _safe_int(more_info.get("EverZTCount")),
                _safe_int(more_info.get("ConZAFDateNum")),
                _safe_int(more_info.get("YearZTDay")),
                _safe_float(more_info.get("Zjl_HB")),
                _safe_float(more_info.get("KfEarnMoney")),
                _safe_float(more_info.get("RDInputFee")),
                _safe_float(more_info.get("CashZJ")),
                _safe_int(more_info.get("StaffNum")),
                _safe_float(more_info.get("IPO_Price")),
                _safe_float(more_info.get("BetaValue")),
                _norm_date(more_info.get("RecentHGDate")),
                _norm_date(more_info.get("RecentReleaseDate")),
                _norm_date(more_info.get("RecentDZDate")),
                _norm_date(more_info.get("ReportDate")),
                _norm_date(more_info.get("ZTDate_Recent")),
                _norm_date(more_info.get("DTDate_Recent")),
                _norm_date(more_info.get("TopDate_Recent")),
                _norm_date(more_info.get("StopJYDate_Recent")),
                str(more_info.get("TPFlag", "")),
                json.dumps(more_info, ensure_ascii=False, default=str),
            )
        return {"accepted": 1}

    # ------------------------------------------------------------------
    # tdx_consensus
    # ------------------------------------------------------------------

    _GO_COLUMN_MAP: dict[str, str] = {
        "GO1": "ipo_price", "GO2": "issue_volume_wan", "GO3": "target_price",
        "GO4": "consensus_year_t",
        "GO5": "eps_t", "GO6": "eps_t1", "GO7": "eps_t2",
        "GO8": "net_profit_t", "GO9": "net_profit_t1", "GO10": "net_profit_t2",
        "GO11": "revenue_t", "GO12": "revenue_t1", "GO13": "revenue_t2",
        "GO14": "op_profit_t", "GO15": "op_profit_t1", "GO16": "op_profit_t2",
        "GO17": "bvps_t", "GO18": "bvps_t1", "GO19": "bvps_t2",
        "GO20": "roe_t", "GO21": "roe_t1", "GO22": "roe_t2",
        "GO23": "pe_t", "GO24": "pe_t1", "GO25": "pe_t2",
        "GO26": "recent_release_date", "GO27": "recent_release_volume",
        "GO28": "next_report_date",
        "GO29": "inst_holding_count", "GO30": "inst_holding_volume",
        "GO31": "fund_holding_count", "GO32": "fund_holding_volume",
        "GO33": "total_shares_wan", "GO34": "float_shares_wan",
        "GO35": "forecast_report_date", "GO36": "forecast_low",
        "GO37": "forecast_high", "GO38": "forecast_yoy_low",
        "GO39": "forecast_yoy_high", "GO40": "flash_report_date",
        "GO41": "flash_net_profit", "GO42": "dividend_total",
        "GO43": "ipo_total", "GO44": "forecast_ex_low",
        "GO45": "forecast_ex_high", "GO46": "forecast_ex_yoy_low",
        "GO47": "forecast_ex_yoy_high",
    }
    _GO_DATE_FIELDS = {"recent_release_date", "next_report_date",
                        "forecast_report_date", "flash_report_date"}
    _GO_INT_FIELDS = {"consensus_year_t", "inst_holding_count", "fund_holding_count"}

    async def save_tdx_consensus(self, code: str, go_payload: dict,
                                  *, snapshot_date: str = "") -> dict:
        """Persist GO1..GO47 single-row snapshot."""
        if not code or not go_payload:
            return {"accepted": 0}
        from datetime import datetime
        sd = snapshot_date or datetime.now().strftime("%Y-%m-%d")

        # Build column->value map
        cols: dict[str, Any] = {}
        for go_key, col_name in self._GO_COLUMN_MAP.items():
            raw = go_payload.get(go_key)
            if raw is None:
                continue
            if col_name in self._GO_DATE_FIELDS:
                cols[col_name] = _norm_date(raw)
            elif col_name in self._GO_INT_FIELDS:
                cols[col_name] = _safe_int(raw)
            else:
                cols[col_name] = _safe_float(raw)

        # Build the dynamic insert. The list of columns is fixed per phase-8
        # schema so we hard-code positional order to keep it readable.
        col_names = ["code", "snapshot_date"] + list(self._GO_COLUMN_MAP.values()) + ["raw_json"]
        placeholders = ", ".join(f"${i+1}" for i in range(len(col_names)))
        update_assignments = ", ".join(
            f"{name}=EXCLUDED.{name}" for name in col_names if name not in ("code", "snapshot_date")
        )
        values = [code, sd] + [cols.get(c) for c in self._GO_COLUMN_MAP.values()] + [
            json.dumps(go_payload, ensure_ascii=False, default=str)
        ]

        sql = f"""
            INSERT INTO tdx_consensus ({', '.join(col_names)}, updated_at)
            VALUES ({placeholders}, CURRENT_TIMESTAMP)
            ON CONFLICT (code, snapshot_date) DO UPDATE SET
                {update_assignments},
                updated_at=CURRENT_TIMESTAMP
        """
        async with self.acquire() as conn:
            await conn.execute(sql, *values)
        return {"accepted": 1}

    # ------------------------------------------------------------------
    # tdx_gpjy_daily / tdx_bkjy_daily / tdx_scjy_daily — long-format helpers
    # ------------------------------------------------------------------

    async def _save_tdx_long_table(
        self,
        table: str,
        partition_col: str,
        partition_val: str,
        rows: Iterable[dict],
    ) -> dict:
        """Generic UPSERT for ``tdx_gpjy_daily / tdx_bkjy_daily``.

        rows items: ``{trade_date, gp_code|bk_code, value_a, value_b}``.
        """
        accepted = 0
        sub_col = "gp_code" if table == "tdx_gpjy_daily" else "bk_code"
        async with self.acquire() as conn:
            for row in rows:
                code_field = str(row.get(sub_col) or "").upper()
                td = _norm_date(row.get("trade_date"))
                if not code_field or not td:
                    continue
                await conn.execute(
                    f"""
                    INSERT INTO {table}
                        ({partition_col}, trade_date, {sub_col}, value_a, value_b, updated_at)
                    VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)
                    ON CONFLICT ({partition_col}, trade_date, {sub_col}) DO UPDATE SET
                        value_a=EXCLUDED.value_a,
                        value_b=EXCLUDED.value_b,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    partition_val, td, code_field,
                    _safe_float(row.get("value_a")),
                    _safe_float(row.get("value_b")),
                )
                accepted += 1
        return {"accepted": accepted}

    async def save_tdx_gpjy_daily(self, code: str, rows: list[dict]) -> dict:
        return await self._save_tdx_long_table("tdx_gpjy_daily", "code", code, rows)

    async def save_tdx_bkjy_daily(self, block_code: str, rows: list[dict]) -> dict:
        return await self._save_tdx_long_table("tdx_bkjy_daily", "block_code", block_code, rows)

    async def save_tdx_scjy_daily(self, rows: list[dict]) -> dict:
        """Market-level (SC) long-format. rows items: ``{trade_date, sc_code,
        value_a, value_b}``."""
        accepted = 0
        async with self.acquire() as conn:
            for row in rows:
                sc_code = str(row.get("sc_code") or "").upper()
                td = _norm_date(row.get("trade_date"))
                if not sc_code or not td:
                    continue
                await conn.execute(
                    """
                    INSERT INTO tdx_scjy_daily
                        (trade_date, sc_code, value_a, value_b, updated_at)
                    VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                    ON CONFLICT (trade_date, sc_code) DO UPDATE SET
                        value_a=EXCLUDED.value_a,
                        value_b=EXCLUDED.value_b,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    td, sc_code,
                    _safe_float(row.get("value_a")),
                    _safe_float(row.get("value_b")),
                )
                accepted += 1
        return {"accepted": accepted}

    # ------------------------------------------------------------------
    # tdx_kzz_basic
    # ------------------------------------------------------------------

    async def save_tdx_kzz(self, info: dict) -> dict:
        if not info or not info.get("kzz_code"):
            return {"accepted": 0}
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tdx_kzz_basic (
                    kzz_code, stock_code, set_code, convert_price, current_rate,
                    remain_size_wan, putback_price, force_redeem_price, convert_date,
                    end_price, end_date, convert_rate, real_value, expire_yield,
                    kzz_score, stock_score, redeem_date, redeem_price,
                    put_date, put_price, convert_code, stock_price, kzz_price,
                    premium_rate, convert_value, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12, $13, $14,
                    $15, $16, $17, $18,
                    $19, $20, $21, $22, $23,
                    $24, $25, CURRENT_TIMESTAMP
                )
                ON CONFLICT (kzz_code) DO UPDATE SET
                    stock_code=EXCLUDED.stock_code, set_code=EXCLUDED.set_code,
                    convert_price=EXCLUDED.convert_price,
                    current_rate=EXCLUDED.current_rate,
                    remain_size_wan=EXCLUDED.remain_size_wan,
                    putback_price=EXCLUDED.putback_price,
                    force_redeem_price=EXCLUDED.force_redeem_price,
                    convert_date=EXCLUDED.convert_date,
                    end_price=EXCLUDED.end_price, end_date=EXCLUDED.end_date,
                    convert_rate=EXCLUDED.convert_rate,
                    real_value=EXCLUDED.real_value,
                    expire_yield=EXCLUDED.expire_yield,
                    kzz_score=EXCLUDED.kzz_score, stock_score=EXCLUDED.stock_score,
                    redeem_date=EXCLUDED.redeem_date, redeem_price=EXCLUDED.redeem_price,
                    put_date=EXCLUDED.put_date, put_price=EXCLUDED.put_price,
                    convert_code=EXCLUDED.convert_code, stock_price=EXCLUDED.stock_price,
                    kzz_price=EXCLUDED.kzz_price, premium_rate=EXCLUDED.premium_rate,
                    convert_value=EXCLUDED.convert_value,
                    updated_at=CURRENT_TIMESTAMP
                """,
                info.get("kzz_code"),
                info.get("stock_code") or None,
                info.get("set_code") or None,
                _safe_float(info.get("convert_price")),
                _safe_float(info.get("current_rate")),
                _safe_float(info.get("remain_size_wan")),
                _safe_float(info.get("putback_price")),
                _safe_float(info.get("force_redeem_price")),
                _norm_date(info.get("convert_date")),
                _safe_float(info.get("end_price")),
                _norm_date(info.get("end_date")),
                _safe_float(info.get("convert_rate")),
                _safe_float(info.get("real_value")),
                _safe_float(info.get("expire_yield")),
                info.get("kzz_score") or None,
                info.get("stock_score") or None,
                _norm_date(info.get("redeem_date")),
                _safe_float(info.get("redeem_price")),
                _norm_date(info.get("put_date")),
                _safe_float(info.get("put_price")),
                info.get("convert_code") or None,
                _safe_float(info.get("stock_price")),
                _safe_float(info.get("kzz_price")),
                _safe_float(info.get("premium_rate")),
                _safe_float(info.get("convert_value")),
            )
        return {"accepted": 1}

    # ------------------------------------------------------------------
    # tdx_relation
    # ------------------------------------------------------------------

    async def save_tdx_relation(self, code: str, rows: list[dict]) -> dict:
        """Replace + insert all block memberships for ``code``."""
        if not code or rows is None:
            return {"accepted": 0}
        accepted = 0
        async with self.acquire() as conn:
            await conn.execute(
                "DELETE FROM tdx_relation WHERE code = $1", code,
            )
            for row in rows:
                block_code = str(row.get("block_code") or "")
                if not block_code:
                    continue
                await conn.execute(
                    """
                    INSERT INTO tdx_relation
                        (code, block_code, block_name, block_type, gp_num, updated_at)
                    VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)
                    ON CONFLICT (code, block_code) DO UPDATE SET
                        block_name=EXCLUDED.block_name,
                        block_type=EXCLUDED.block_type,
                        gp_num=EXCLUDED.gp_num,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    code, block_code,
                    row.get("block_name") or "",
                    row.get("block_type") or "",
                    _safe_int(row.get("gp_num")),
                )
                accepted += 1
        return {"accepted": accepted}

    # ------------------------------------------------------------------
    # 读取辅助 — 给上层 tools 复用
    # ------------------------------------------------------------------

    async def get_tdx_stock_extra(self, code: str, *, limit: int = 1) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM tdx_stock_extra
                WHERE code = $1
                ORDER BY trade_date DESC
                LIMIT $2
                """,
                code, int(limit),
            )
            return [dict(r) for r in rows]

    async def get_latest_tdx_stock_extra(self, code: str) -> Optional[dict]:
        rows = await self.get_tdx_stock_extra(code, limit=1)
        return rows[0] if rows else None

    async def get_tdx_consensus(self, code: str) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM tdx_consensus
                WHERE code = $1
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                code,
            )
            return dict(row) if row else None

    async def save_tdx_data_completeness(
        self,
        data_key: str,
        status: str,
        *,
        as_of_date: Any = None,
        row_count: int = 0,
        detail: Optional[dict] = None,
    ) -> dict:
        key = str(data_key or "").strip()
        if not key:
            return {"accepted": 0}
        normalized_status = str(status or "unknown").strip() or "unknown"
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tdx_data_completeness
                    (data_key, status, as_of_date, row_count, detail, updated_at)
                VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)
                ON CONFLICT (data_key) DO UPDATE SET
                    status = EXCLUDED.status,
                    as_of_date = EXCLUDED.as_of_date,
                    row_count = EXCLUDED.row_count,
                    detail = EXCLUDED.detail,
                    updated_at = CURRENT_TIMESTAMP
                """,
                key,
                normalized_status,
                _norm_date(as_of_date) if as_of_date else None,
                int(row_count or 0),
                json.dumps(detail or {}, ensure_ascii=False, default=str),
            )
        return {"accepted": 1}

    async def get_tdx_data_completeness(self, data_key: Optional[str] = None) -> dict:
        async with self.acquire() as conn:
            if data_key:
                row = await conn.fetchrow(
                    "SELECT * FROM tdx_data_completeness WHERE data_key = $1",
                    str(data_key),
                )
                if not row:
                    return {}
                payload = dict(row)
                try:
                    payload["detail"] = json.loads(payload.get("detail") or "{}")
                except Exception:
                    payload["detail"] = {}
                return payload
            rows = await conn.fetch(
                "SELECT * FROM tdx_data_completeness ORDER BY data_key ASC"
            )
        result = {}
        for row in rows:
            payload = dict(row)
            try:
                payload["detail"] = json.loads(payload.get("detail") or "{}")
            except Exception:
                payload["detail"] = {}
            result[str(payload.get("data_key"))] = payload
        return result

    async def get_sector_rotation_summary(self, limit: int = 5) -> dict:
        """Return hot/cold sector names derived from TDX BK daily data.

        Falls back to ``market_blocks`` industry snapshots when TDX BK daily
        data is unavailable so free public industry-flow feeds can still power
        factory collect.
        """
        fetch_limit = max(1, min(int(limit or 5), 20))
        async with self.acquire() as conn:
            latest_date = await conn.fetchval(
                "SELECT MAX(trade_date) FROM tdx_bkjy_daily"
            )
            if latest_date is None:
                block_rows = await conn.fetch(
                    """
                    SELECT block_name, avg_change_pct, total_amount, stock_count
                    FROM market_blocks
                    WHERE block_type = 'industry'
                      AND block_name IS NOT NULL
                      AND block_name != ''
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT $1
                    """,
                    max(fetch_limit * 8, 40),
                )
                items = [dict(row) for row in list(block_rows or [])]
                ranked = sorted(
                    items,
                    key=lambda item: (
                        float(item.get("avg_change_pct") or 0.0),
                        float(item.get("total_amount") or 0.0),
                    ),
                    reverse=True,
                )
                hot = [
                    str(item.get("block_name") or "").strip()
                    for item in ranked[:fetch_limit]
                    if str(item.get("block_name") or "").strip()
                ]
                cold = [
                    str(item.get("block_name") or "").strip()
                    for item in sorted(
                        items,
                        key=lambda item: (
                            float(item.get("avg_change_pct") or 0.0),
                            -float(item.get("total_amount") or 0.0),
                        ),
                    )[:fetch_limit]
                    if str(item.get("block_name") or "").strip()
                ]
                return {
                    "hot_sectors": hot,
                    "cold_sectors": cold,
                    "source": "market_blocks",
                    "status": "ok" if (hot or cold) else "degraded",
                    "reason": None if (hot or cold) else "no_market_blocks",
                    "items": ranked[: fetch_limit * 2],
                }
            rows = await conn.fetch(
                """
                SELECT
                    b.block_code,
                    COALESCE(m.block_name, b.block_code) AS block_name,
                    MAX(CASE WHEN b.bk_code = 'BK9' THEN b.value_a END) AS up_count,
                    MAX(CASE WHEN b.bk_code = 'BK9' THEN b.value_b END) AS down_count,
                    MAX(CASE WHEN b.bk_code = 'BK12' THEN b.value_a END) AS limit_up_count,
                    MAX(CASE WHEN b.bk_code = 'BK13' THEN b.value_a END) AS limit_down_count,
                    MAX(CASE WHEN b.bk_code = 'BK17' THEN b.value_a END) AS amount
                FROM tdx_bkjy_daily b
                LEFT JOIN market_blocks m ON m.block_code = b.block_code
                WHERE b.trade_date = $1
                GROUP BY b.block_code, COALESCE(m.block_name, b.block_code)
                """,
                latest_date,
            )
        items = []
        for row in rows:
            item = dict(row)
            score = (
                _safe_float(item.get("up_count")) or 0.0
            ) - (
                _safe_float(item.get("down_count")) or 0.0
            ) + 2.0 * (
                (_safe_float(item.get("limit_up_count")) or 0.0)
                - (_safe_float(item.get("limit_down_count")) or 0.0)
            )
            item["rotation_score"] = float(score)
            items.append(item)
        ranked = sorted(items, key=lambda item: item["rotation_score"], reverse=True)
        hot = [str(item.get("block_name") or item.get("block_code")) for item in ranked[:fetch_limit]]
        cold = [
            str(item.get("block_name") or item.get("block_code"))
            for item in sorted(items, key=lambda item: item["rotation_score"])[:fetch_limit]
        ]
        return {
            "hot_sectors": hot,
            "cold_sectors": cold,
            "trade_date": latest_date,
            "source": "tdx_bkjy_daily",
            "status": "ok" if (hot or cold) else "degraded",
            "items": ranked[: fetch_limit * 2],
        }

    async def get_tdx_relation(self, code: str) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM tdx_relation WHERE code = $1 ORDER BY block_type, block_code",
                code,
            )
            return [dict(r) for r in rows]

    async def get_tdx_kzz(self, kzz_code: str) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tdx_kzz_basic WHERE kzz_code = $1",
                kzz_code,
            )
            return dict(row) if row else None
