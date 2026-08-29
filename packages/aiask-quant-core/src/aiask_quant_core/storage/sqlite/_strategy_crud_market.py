"""SQLite 策略超市 Mixin — CRUD / 静态工具 / 工厂 / 质量报告 / 领域事件"""

import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from .strategy_factory_json_budget import bounded_json_text, strategy_json_field_max_bytes


class _StrategyCrudMarketMixin:
        async def _get_market_data_quality_gate(
            self,
            data_key: str,
            *,
            min_rows: int = 1,
            max_age_days: int = 10,
        ) -> dict:
            key = str(data_key or "").strip()
            if not key:
                return {
                    "data_key": "",
                    "status": "missing",
                    "row_count": 0,
                    "degraded": True,
                    "blocked": True,
                    "hard_fact_eligible": False,
                    "quality_flags": ["missing_data_key"],
                    "block_reasons": ["missing_data_key"],
                }
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM tdx_data_completeness WHERE data_key = $1",
                    key,
                )
            if not row:
                return {
                    "data_key": key,
                    "status": "missing",
                    "row_count": 0,
                    "degraded": True,
                    "blocked": True,
                    "hard_fact_eligible": False,
                    "quality_flags": ["completeness_missing"],
                    "block_reasons": ["completeness_missing"],
                }
            payload = dict(row)
            detail = self._decode_json_field(payload.get("detail"), {})
            status = str(payload.get("status") or "unknown").strip().lower() or "unknown"
            row_count = int(payload.get("row_count") or 0)
            as_of_date = self._coerce_date(payload.get("as_of_date"))
            age_days = None
            flags: list[str] = []
            reasons: list[str] = []
            if status != "ok":
                flags.append(f"status:{status}")
                reasons.append(f"status:{status}")
            if row_count < max(1, int(min_rows or 1)):
                flags.append("insufficient_rows")
                reasons.append("insufficient_rows")
            if as_of_date is None:
                flags.append("as_of_date_missing")
                reasons.append("as_of_date_missing")
            else:
                age_days = max(0, (date.today() - as_of_date).days)
                if age_days > max(0, int(max_age_days or 0)):
                    flags.append("stale")
                    reasons.append("stale")
            blocked = bool(reasons)
            return {
                "data_key": key,
                "status": status,
                "row_count": row_count,
                "as_of_date": as_of_date,
                "age_days": age_days,
                "max_age_days": max_age_days,
                "detail": detail if isinstance(detail, dict) else {},
                "degraded": blocked,
                "blocked": blocked,
                "hard_fact_eligible": not blocked,
                "quality_flags": flags,
                "block_reasons": reasons,
            }

        async def _get_margin_detail_suffix_coverage(self, end_date = None) -> dict:
            normalized_end_date = self._coerce_date(end_date) or date.today()
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        CASE
                            WHEN ts_code LIKE '%.SH' THEN 'SH'
                            WHEN ts_code LIKE '%.SZ' THEN 'SZ'
                            ELSE 'OTHER'
                        END AS suffix,
                        COUNT(*) AS row_count,
                        MAX(trade_date) AS as_of_date
                    FROM margin_detail
                    WHERE trade_date <= $1
                    GROUP BY suffix
                    """,
                    normalized_end_date,
                )
            coverage = {
                str(row.get("suffix") or "OTHER"): {
                    "row_count": int(row.get("row_count") or 0),
                    "as_of_date": row.get("as_of_date"),
                }
                for row in rows
            }
            missing = [suffix for suffix in ("SH", "SZ") if int((coverage.get(suffix) or {}).get("row_count") or 0) <= 0]
            return {
                "coverage": coverage,
                "missing_suffixes": missing,
                "degraded": bool(missing),
                "blocked": bool(missing),
                "hard_fact_eligible": not bool(missing),
            }

        async def save_daily_snapshot(self, snapshot_date, data: dict) -> None:
            normalized_snapshot_date = self._coerce_date(snapshot_date)
            if normalized_snapshot_date is None:
                raise ValueError("snapshot_date is required")
            async with self.acquire() as conn:
                await conn.execute(
                    """INSERT INTO daily_snapshot_history
                       (snapshot_date, fear_greed_index, fg_components, factor_ic, factor_ic_trend, factor_research,
                        north_fund_3d_net, margin_5d_change_pct, hot_sectors, cold_sectors,
                        listed_count, category_counts, summary, completeness, sources,
                        parameter_distribution_samples, parameter_distribution_summary,
                        failure_reasons, missing_fields, degraded)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                               $11, $12, $13, $14, $15, $16, $17,
                               $18, $19, $20)
                       ON CONFLICT (snapshot_date) DO UPDATE SET
                        fear_greed_index = EXCLUDED.fear_greed_index,
                        fg_components = EXCLUDED.fg_components,
                        factor_ic = EXCLUDED.factor_ic,
                        factor_ic_trend = EXCLUDED.factor_ic_trend,
                        factor_research = EXCLUDED.factor_research,
                        north_fund_3d_net = EXCLUDED.north_fund_3d_net,
                        margin_5d_change_pct = EXCLUDED.margin_5d_change_pct,
                        hot_sectors = EXCLUDED.hot_sectors,
                        cold_sectors = EXCLUDED.cold_sectors,
                        listed_count = EXCLUDED.listed_count,
                        category_counts = EXCLUDED.category_counts,
                        summary = EXCLUDED.summary,
                        completeness = EXCLUDED.completeness,
                        sources = EXCLUDED.sources,
                        parameter_distribution_samples = EXCLUDED.parameter_distribution_samples,
                        parameter_distribution_summary = EXCLUDED.parameter_distribution_summary,
                        failure_reasons = EXCLUDED.failure_reasons,
                        missing_fields = EXCLUDED.missing_fields,
                        degraded = EXCLUDED.degraded
                    """,
                    normalized_snapshot_date,
                    data.get("fear_greed_index"),
                    json.dumps(data.get("fg_components") or {}, ensure_ascii=False, default=str),
                    json.dumps(data.get("factor_ic") or {}, ensure_ascii=False, default=str),
                    json.dumps(data.get("factor_ic_trend") or {}, ensure_ascii=False, default=str),
                    bounded_json_text(
                        "daily_snapshot_history.factor_research",
                        data.get("factor_research") or {},
                        max_bytes=strategy_json_field_max_bytes(),
                    ),
                    data.get("north_fund_3d_net"),
                    data.get("margin_5d_change_pct"),
                    json.dumps(data.get("hot_sectors") or [], ensure_ascii=False, default=str),
                    json.dumps(data.get("cold_sectors") or [], ensure_ascii=False, default=str),
                    data.get("listed_count", 0),
                    json.dumps(data.get("category_counts") or {}, ensure_ascii=False, default=str),
                    json.dumps(data.get("summary") or {}, ensure_ascii=False, default=str),
                    json.dumps(data.get("completeness") or {}, ensure_ascii=False, default=str),
                    json.dumps(data.get("sources") or {}, ensure_ascii=False, default=str),
                    json.dumps(data.get("parameter_distribution_samples") or [], ensure_ascii=False, default=str),
                    json.dumps(data.get("parameter_distribution_summary") or {}, ensure_ascii=False, default=str),
                    json.dumps(data.get("failure_reasons") or [], ensure_ascii=False, default=str),
                    json.dumps(data.get("missing_fields") or [], ensure_ascii=False, default=str),
                    bool(data.get("degraded")),
                )

        def _decode_daily_snapshot(self, row: dict) -> dict:
            result = dict(row)
            for key in (
                "fg_components",
                "factor_ic",
                "factor_ic_trend",
                "factor_research",
                "category_counts",
                "summary",
                "completeness",
                "sources",
                "parameter_distribution_summary",
            ):
                result[key] = self._decode_json_field(result.get(key), {})
            for key in (
                "hot_sectors",
                "cold_sectors",
                "parameter_distribution_samples",
                "failure_reasons",
                "missing_fields",
            ):
                result[key] = self._decode_json_field(result.get(key), [])
            fg_level = result.get("fg_level")
            if not fg_level:
                fg_value = result.get("fear_greed_index")
                try:
                    fg_numeric = int(fg_value)
                except Exception:
                    fg_numeric = 50
                fg_level = "greed" if fg_numeric >= 70 else ("fear" if fg_numeric <= 30 else "neutral")
                result["fg_level"] = fg_level
            result.setdefault("date", str(result.get("snapshot_date") or ""))
            result.setdefault("fear_greed", result.get("fear_greed_index"))
            result.setdefault("sentiment", fg_level)
            result["north_fund"] = dict(result.get("north_fund") or {})
            result["north_fund"].setdefault("net_3d", result.get("north_fund_3d_net"))
            return result

        async def get_daily_snapshot(self, snapshot_date = None) -> Optional[dict]:
            normalized_snapshot_date = None if snapshot_date is None else self._coerce_date(snapshot_date)
            if snapshot_date is not None and normalized_snapshot_date is None:
                raise ValueError("snapshot_date is invalid")
            async with self.acquire() as conn:
                if normalized_snapshot_date is None:
                    row = await conn.fetchrow(
                        """
                        SELECT * FROM daily_snapshot_history
                        ORDER BY snapshot_date DESC
                        LIMIT 1
                        """
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        SELECT * FROM daily_snapshot_history
                        WHERE snapshot_date = $1
                        LIMIT 1
                        """,
                        normalized_snapshot_date,
                    )
            if not row:
                return None
            return self._decode_daily_snapshot(dict(row))

        async def get_north_fund_history(self, days: int = 30, end_date = None) -> List[dict]:
            normalized_end_date = self._coerce_date(end_date) or date.today()
            fetch_limit = max(1, min(int(days or 30), 365))
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT trade_date, north_money, south_money, net_amount, ggt_ss, ggt_sz, hgt, sgt
                    FROM north_fund_flow
                    WHERE trade_date <= $1
                    ORDER BY trade_date DESC
                    LIMIT $2
                    """,
                    normalized_end_date,
                    fetch_limit,
                )
            out: List[dict] = []
            for row in rows:
                trade_date = self._coerce_date(row.get("trade_date"))
                if trade_date is None:
                    continue
                out.append({
                    "trade_date": trade_date,
                    "north_money": float(row.get("north_money") or 0.0),
                    "south_money": float(row.get("south_money") or 0.0),
                    "net_amount": float(row.get("net_amount") or 0.0),
                    "ggt_ss": float(row.get("ggt_ss") or 0.0),
                    "ggt_sz": float(row.get("ggt_sz") or 0.0),
                    "hgt": float(row.get("hgt") or 0.0),
                    "sgt": float(row.get("sgt") or 0.0),
                    "source": "north_fund_flow",
                })
            return out

        async def get_recent_north_fund_summary(self, days: int = 3, sample_limit: int = 5, end_date = None) -> Optional[dict]:
            gate = await self._get_market_data_quality_gate(
                "north_fund_flow",
                min_rows=max(1, int(days or 3)),
                max_age_days=10,
            )
            rows = await self.get_north_fund_history(
                days=max(int(sample_limit or 5), int(days or 3), 1),
                end_date=end_date,
            )
            if gate.get("blocked") or not rows:
                return {
                    "days": max(1, int(days or 3)),
                    "sample_count": 0,
                    "raw_sample_count": len(rows),
                    "trade_dates": [],
                    "total_net": None,
                    "latest_trade_date": gate.get("as_of_date") or (rows[0].get("trade_date") if rows else None),
                    "stale_age_days": gate.get("age_days"),
                    "stale": "stale" in set(gate.get("quality_flags") or []),
                    "source": "north_fund_flow",
                    "series": rows,
                    "quality": gate,
                    "degraded": True,
                    "blocked": True,
                    "hard_fact_eligible": False,
                    "block_reasons": list(gate.get("block_reasons") or ["north_fund_quality_gate_failed"]),
                }
            selected = list(rows[: max(1, int(days or 3))])
            latest_trade_date = rows[0].get("trade_date") if rows else None
            stale_age_days = None
            stale = False
            if isinstance(latest_trade_date, date):
                stale_age_days = max(0, (date.today() - latest_trade_date).days)
                stale = stale_age_days > 7
            return {
                "days": max(1, int(days or 3)),
                "sample_count": len(rows),
                "trade_dates": [row.get("trade_date") for row in selected],
                "total_net": round(sum(float(row.get("north_money") or 0.0) for row in selected), 2),
                "latest_trade_date": latest_trade_date,
                "stale_age_days": stale_age_days,
                "stale": stale,
                "source": "north_fund_flow",
                "quality": gate,
                "degraded": bool(gate.get("degraded")),
                "blocked": bool(gate.get("blocked")),
                "hard_fact_eligible": bool(gate.get("hard_fact_eligible")),
                "block_reasons": list(gate.get("block_reasons") or []),
                "series": [
                    {"trade_date": row.get("trade_date"), "north_money": float(row.get("north_money") or 0.0)}
                    for row in rows
                ],
            }

        async def get_margin_market_history(self, days: int = 30, end_date = None) -> List[dict]:
            gate = await self._get_market_data_quality_gate(
                "margin_market_flow",
                min_rows=1,
                max_age_days=10,
            )
            if gate.get("blocked"):
                return []
            fetch_limit = max(1, min(int(days or 30), 365))
            normalized_end_date = self._coerce_date(end_date) or date.today()
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT trade_date, exchange_id, rzye, rzmre, rzche, rqye, rqmcl, rqyl, rzrqye
                    FROM margin_market_flow
                    WHERE trade_date <= $1
                    ORDER BY trade_date DESC, exchange_id ASC
                    LIMIT $2
                    """,
                    normalized_end_date,
                    fetch_limit * 3,
                )
            source = "margin_market_flow"
            normalized_rows: List[Dict[str, Any]] = []
            if rows:
                grouped: Dict[date, Dict[str, Any]] = {}
                for row in rows:
                    trade_date = self._coerce_date(row.get("trade_date"))
                    if trade_date is None:
                        continue
                    bucket = grouped.setdefault(
                        trade_date,
                        {
                            "trade_date": trade_date,
                            "marginBalance": 0.0,
                            "marginBuy": 0.0,
                            "marginRepay": 0.0,
                            "shortBalance": 0.0,
                            "shortSell": 0.0,
                            "shortVolume": 0.0,
                            "totalBalance": 0.0,
                            "source": source,
                        },
                    )
                    bucket["marginBalance"] += float(row.get("rzye") or 0.0)
                    bucket["marginBuy"] += float(row.get("rzmre") or 0.0)
                    bucket["marginRepay"] += float(row.get("rzche") or 0.0)
                    bucket["shortBalance"] += float(row.get("rqye") or 0.0)
                    bucket["shortSell"] += float(row.get("rqmcl") or 0.0)
                    bucket["shortVolume"] += float(row.get("rqyl") or 0.0)
                    bucket["totalBalance"] += float(row.get("rzrqye") or 0.0)
                normalized_rows = sorted(grouped.values(), key=lambda item: item["trade_date"], reverse=True)

            if normalized_rows:
                return normalized_rows[:fetch_limit]

            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        trade_date,
                        SUM(rzye) AS rzye,
                        SUM(rzmre) AS rzmre,
                        SUM(rzche) AS rzche,
                        SUM(rqye) AS rqye,
                        SUM(rqmcl) AS rqmcl,
                        SUM(rqyl) AS rqyl,
                        SUM(rzrqye) AS rzrqye
                    FROM margin_detail
                    WHERE trade_date <= $1
                    GROUP BY trade_date
                    ORDER BY trade_date DESC
                    LIMIT $2
                    """,
                    normalized_end_date,
                    fetch_limit,
                )
            out: List[dict] = []
            for row in rows:
                trade_date = self._coerce_date(row.get("trade_date"))
                if trade_date is None:
                    continue
                out.append({
                    "trade_date": trade_date,
                    "marginBalance": float(row.get("rzye") or 0.0),
                    "marginBuy": float(row.get("rzmre") or 0.0),
                    "marginRepay": float(row.get("rzche") or 0.0),
                    "shortBalance": float(row.get("rqye") or 0.0),
                    "shortSell": float(row.get("rqmcl") or 0.0),
                    "shortVolume": float(row.get("rqyl") or 0.0),
                    "totalBalance": float(row.get("rzrqye") or 0.0),
                    "source": "margin_detail_aggregate",
                })
            return out

        async def get_margin_detail_latest(self, limit: int = 20, ts_code = None, end_date = None) -> List[dict]:
            normalized_end_date = self._coerce_date(end_date) or date.today()
            normalized_ts_code = self._coerce_ts_code(ts_code)
            fetch_limit = max(1, min(int(limit or 20), 500))
            gate = await self._get_market_data_quality_gate(
                "margin_detail",
                min_rows=1,
                max_age_days=10,
            )
            if gate.get("blocked"):
                return []
            if normalized_ts_code is None:
                coverage = await self._get_margin_detail_suffix_coverage(end_date=normalized_end_date)
                if coverage.get("blocked"):
                    return []
            async with self.acquire() as conn:
                if normalized_ts_code:
                    rows = await conn.fetch(
                        """
                        SELECT trade_date, ts_code, rzye, rqye, rzmre, rqyl, rzche, rqchl, rqmcl, rzrqye
                        FROM margin_detail
                        WHERE trade_date <= $1 AND ts_code = $2
                        ORDER BY trade_date DESC, ts_code ASC
                        LIMIT $3
                        """,
                        normalized_end_date,
                        normalized_ts_code,
                        fetch_limit,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT trade_date, ts_code, rzye, rqye, rzmre, rqyl, rzche, rqchl, rqmcl, rzrqye
                        FROM margin_detail
                        WHERE trade_date <= $1
                        ORDER BY trade_date DESC, ts_code ASC
                        LIMIT $2
                        """,
                        normalized_end_date,
                        fetch_limit,
                    )
            out: List[dict] = []
            for row in rows:
                trade_date = self._coerce_date(row.get("trade_date"))
                if trade_date is None:
                    continue
                out.append({
                    "trade_date": trade_date,
                    "ts_code": str(row.get("ts_code") or ""),
                    "code": str(row.get("ts_code") or "").split(".", 1)[0],
                    "marginBalance": float(row.get("rzye") or 0.0),
                    "shortBalance": float(row.get("rqye") or 0.0),
                    "marginBuy": float(row.get("rzmre") or 0.0),
                    "shortVolume": float(row.get("rqyl") or 0.0),
                    "marginRepay": float(row.get("rzche") or 0.0),
                    "shortRepay": float(row.get("rqchl") or 0.0),
                    "shortSell": float(row.get("rqmcl") or 0.0),
                    "totalBalance": float(row.get("rzrqye") or 0.0),
                    "source": "margin_detail",
                })
            return out

        async def get_margin_ranking(self, top_n: int = 20, sort_by: str = "balance", end_date = None) -> List[dict]:
            normalized_end_date = self._coerce_date(end_date) or date.today()
            fetch_limit = max(1, min(int(top_n or 20), 200))
            gate = await self._get_market_data_quality_gate(
                "margin_detail",
                min_rows=1,
                max_age_days=10,
            )
            coverage = await self._get_margin_detail_suffix_coverage(end_date=normalized_end_date)
            if gate.get("blocked") or coverage.get("blocked"):
                return []
            sort_key = str(sort_by or "balance").lower()
            sort_column_map = {
                "balance": "rzrqye",
                "buy": "rzmre",
                "sell": "rqmcl",
            }
            sort_column = sort_column_map.get(sort_key, "rzrqye")
            sort_expr = f"CASE WHEN {sort_column} IS NULL OR {sort_column} = 'NaN' THEN NULL ELSE {sort_column} END"
            async with self.acquire() as conn:
                latest_trade_date = await conn.fetchval(
                    """
                    SELECT MAX(trade_date)
                    FROM margin_detail
                    WHERE trade_date <= $1
                    """,
                    normalized_end_date,
                )
                if latest_trade_date is None:
                    return []
                rows = await conn.fetch(
                    f"""
                    SELECT trade_date, ts_code, rzye, rqye, rzmre, rqyl, rzche, rqchl, rqmcl, rzrqye
                    FROM margin_detail
                    WHERE trade_date = $1
                    ORDER BY {sort_expr} DESC NULLS LAST, ts_code ASC
                    LIMIT $2
                    """,
                    latest_trade_date,
                    fetch_limit,
                )
            out: List[dict] = []
            for row in rows:
                trade_date = self._coerce_date(row.get("trade_date"))
                if trade_date is None:
                    continue
                out.append({
                    "trade_date": trade_date,
                    "ts_code": str(row.get("ts_code") or ""),
                    "code": str(row.get("ts_code") or "").split(".", 1)[0],
                    "marginBalance": float(row.get("rzye") or 0.0),
                    "shortBalance": float(row.get("rqye") or 0.0),
                    "marginBuy": float(row.get("rzmre") or 0.0),
                    "shortVolume": float(row.get("rqyl") or 0.0),
                    "marginRepay": float(row.get("rzche") or 0.0),
                    "shortRepay": float(row.get("rqchl") or 0.0),
                    "shortSell": float(row.get("rqmcl") or 0.0),
                    "totalBalance": float(row.get("rzrqye") or 0.0),
                    "source": "margin_detail_ranking",
                })
            return out

        async def get_recent_margin_summary(
            self,
            days: int = 10,
            sample_limit: int = 10,
            change_lookback_days: int = 5,
        ) -> Optional[dict]:
            gate = await self._get_market_data_quality_gate(
                "margin_market_flow",
                min_rows=max(1, int(change_lookback_days or 5) + 1),
                max_age_days=10,
            )
            if gate.get("blocked"):
                return {
                    "days": max(1, int(days or 10)),
                    "sample_count": 0,
                    "margin_balance_latest": None,
                    "margin_buy_latest": None,
                    "margin_balance_change_5d": None,
                    "recent_rows": [],
                    "series": [],
                    "source": "margin_market_flow",
                    "quality": gate,
                    "degraded": True,
                    "blocked": True,
                    "hard_fact_eligible": False,
                    "block_reasons": list(gate.get("block_reasons") or ["margin_market_quality_gate_failed"]),
                }
            fetch_limit = max(
                int(sample_limit or 10),
                int(days or 10),
                int(change_lookback_days or 5) + 1,
                1,
            )
            normalized_rows = await self.get_margin_market_history(days=fetch_limit)
            if not normalized_rows:
                return {
                    "days": max(1, int(days or 10)),
                    "sample_count": 0,
                    "margin_balance_latest": None,
                    "margin_buy_latest": None,
                    "margin_balance_change_5d": None,
                    "recent_rows": [],
                    "series": [],
                    "source": "margin_market_flow",
                    "quality": gate,
                    "degraded": True,
                    "blocked": True,
                    "hard_fact_eligible": False,
                    "block_reasons": ["margin_market_rows_unavailable"],
                }
            source = str((normalized_rows[0] or {}).get("source") or "margin_market_flow")
            detail_gate = await self._get_market_data_quality_gate(
                "margin_detail",
                min_rows=1,
                max_age_days=10,
            )
            detail_coverage = await self._get_margin_detail_suffix_coverage()

            latest = normalized_rows[0]
            older = normalized_rows[min(max(int(change_lookback_days or 5), 1), len(normalized_rows) - 1)]
            latest_balance = float(latest.get("marginBalance") or 0.0)
            older_balance = float(older.get("marginBalance") or 0.0)
            change_5d = None
            if older_balance > 0:
                change_5d = round((latest_balance - older_balance) / older_balance * 100, 2)
            stale_age_days = max(0, (date.today() - latest["trade_date"]).days) if isinstance(latest.get("trade_date"), date) else None
            stale = bool(stale_age_days is not None and stale_age_days > 7)
            return {
                "days": max(1, int(days or 10)),
                "sample_count": len(normalized_rows),
                "latest_trade_date": latest.get("trade_date"),
                "stale_age_days": stale_age_days,
                "stale": stale,
                "source": source,
                "quality": gate,
                "detail_quality": detail_gate,
                "detail_coverage": detail_coverage,
                "degraded": bool(gate.get("degraded") or detail_gate.get("degraded") or detail_coverage.get("degraded")),
                "blocked": bool(gate.get("blocked")),
                "hard_fact_eligible": bool(gate.get("hard_fact_eligible")),
                "block_reasons": list(gate.get("block_reasons") or []),
                "margin_balance_latest": round(latest_balance, 2),
                "margin_buy_latest": round(float(latest.get("marginBuy") or 0.0), 2),
                "margin_balance_change_5d": change_5d,
                "recent_rows": normalized_rows[: max(1, min(fetch_limit, 5))],
                "series": normalized_rows[:fetch_limit],
            }

        async def list_daily_snapshots(
            self,
            limit: int = 20,
            start_date = None,
            end_date = None,
        ) -> List[dict]:
            normalized_start_date = None if start_date is None else self._coerce_date(start_date)
            normalized_end_date = None if end_date is None else self._coerce_date(end_date)
            if start_date is not None and normalized_start_date is None:
                raise ValueError("start_date is invalid")
            if end_date is not None and normalized_end_date is None:
                raise ValueError("end_date is invalid")
            async with self.acquire() as conn:
                sql = "SELECT * FROM daily_snapshot_history WHERE 1=1"
                params: list = []
                idx = 1
                if normalized_start_date is not None:
                    sql += f" AND snapshot_date >= ${idx}"
                    params.append(normalized_start_date)
                    idx += 1
                if normalized_end_date is not None:
                    sql += f" AND snapshot_date <= ${idx}"
                    params.append(normalized_end_date)
                    idx += 1
                sql += f" ORDER BY snapshot_date DESC LIMIT ${idx}"
                params.append(max(1, min(int(limit or 20), 200)))
                rows = await conn.fetch(sql, *params)
            return [self._decode_daily_snapshot(dict(row)) for row in rows]
