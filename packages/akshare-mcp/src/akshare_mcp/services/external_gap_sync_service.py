"""External gap-fill sync for DB-only factory data.

This service is intentionally limited to the database synchronization layer.
Factories must continue to read only SQLite. External providers are used only
when explicitly enabled to fill fields that the local TDX installation cannot
provide.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def external_gap_fill_enabled() -> bool:
    value = (
        os.getenv("ENABLE_EXTERNAL_GAP_FILL")
        or os.getenv("TDX_ENABLE_EXTERNAL_GAP_FILL")
        or ""
    )
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def external_gap_fill_provider() -> str:
    return (
        os.getenv("EXTERNAL_GAP_FILL_PROVIDER")
        or os.getenv("TDX_EXTERNAL_GAP_FILL_PROVIDER")
        or "akshare_free"
    ).strip().lower()


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "" or value == "--":
        return None
    try:
        text = str(value).strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1]
        numeric = float(text)
        return numeric if numeric == numeric else None
    except (TypeError, ValueError):
        return None


def _norm_date(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    try:
        text = str(int(float(text)))
    except (TypeError, ValueError):
        pass
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    return text


def _to_ts_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        left, right = raw.split(".", 1)
        return f"{left}.{right}"
    bare = raw.split(".", 1)[0]
    if bare.startswith(("6", "9", "5", "11", "113", "118")):
        return f"{bare}.SH"
    if bare.startswith(("8", "4", "92")):
        return f"{bare}.BJ"
    return f"{bare}.SZ"


def _to_exchange_ts_code(code: Any, exchange: str) -> str:
    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    suffix = "SH" if exchange.upper() in {"SSE", "SH"} else "SZ"
    return f"{digits.zfill(6)}.{suffix}"


def _iter_df_tuples(df: Any) -> tuple[list[Any], list[tuple[Any, ...]]]:
    if df is None:
        return [], []
    if hasattr(df, "empty") and bool(df.empty):
        return [], []
    if hasattr(df, "columns") and hasattr(df, "itertuples"):
        try:
            columns = list(df.columns)
            rows = [tuple(item) for item in df.itertuples(index=False, name=None)]
            return columns, rows
        except Exception:
            return [], []
    return [], []


def _at(row: tuple[Any, ...], index: int) -> Any:
    return row[index] if len(row) > index else None


def _date_ymd(value: str) -> str:
    return _norm_date(value).replace("-", "")


def _recent_ymd_dates(days: int) -> list[str]:
    end = datetime.now()
    return [
        (end - timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(max(1, int(days)))
    ]


def _money_yi_to_yuan(value: Any) -> Optional[float]:
    numeric = _safe_float(value)
    return numeric * 100000000.0 if numeric is not None else None


def _source_priority() -> str:
    return "external_gap_fill_free"


class ExternalGapSyncService:
    """Fill DB gaps with explicit free providers in the sync layer only."""

    def __init__(
        self,
        *,
        universe: Optional[list[str]] = None,
        north_days: int = 365,
        margin_days: int = 90,
        margin_detail_days: int = 30,
        margin_detail_limit: int = 300,
    ) -> None:
        self.universe = list(universe or [])
        self.north_days = max(1, int(os.getenv("EXTERNAL_GAP_FILL_NORTH_DAYS", str(north_days)) or north_days))
        self.margin_days = max(1, int(os.getenv("EXTERNAL_GAP_FILL_MARGIN_DAYS", str(margin_days)) or margin_days))
        self.margin_detail_days = max(
            1,
            int(os.getenv("EXTERNAL_GAP_FILL_MARGIN_DETAIL_DAYS", str(margin_detail_days)) or margin_detail_days),
        )
        self.margin_detail_limit = max(
            1,
            int(os.getenv("EXTERNAL_GAP_FILL_MARGIN_DETAIL_LIMIT", str(margin_detail_limit)) or margin_detail_limit),
        )

    async def run_all(self, db) -> dict[str, Any]:
        if not external_gap_fill_enabled():
            return {"enabled": False, "skipped": True, "reason": "external_gap_fill_disabled"}
        provider = external_gap_fill_provider()
        if provider in {"akshare", "akshare_free", "free"}:
            ak = self._build_akshare_client()
            if ak is None:
                recorded = await self._record_unavailable_for_empty_targets(
                    db,
                    "akshare_unavailable",
                    {
                        "north_fund_flow": "akshare.stock_hsgt_hist_em",
                        "margin_market_flow": "akshare.stock_margin_sse/szse",
                        "margin_detail": "akshare.stock_margin_detail_sse/szse",
                    },
                )
                return {
                    "enabled": True,
                    "provider": "akshare_free",
                    "ok": False,
                    "error": "akshare unavailable",
                    "source_unavailable": recorded,
                }
            north = await self._safe_sync(
                db,
                "north_fund_flow",
                "akshare.stock_hsgt_hist_em",
                lambda: self.sync_north_fund_from_akshare(db, ak),
            )
            margin = await self._safe_sync(
                db,
                "margin_market_flow",
                "akshare.stock_margin_sse/szse",
                lambda: self.sync_margin_market_from_akshare(db, ak),
            )
            margin_detail = await self._safe_sync(
                db,
                "margin_detail",
                "akshare.stock_margin_detail_sse/szse",
                lambda: self.sync_margin_detail_from_akshare(db, ak),
            )
            return {
                "enabled": True,
                "provider": "akshare_free",
                "north_fund_flow": north,
                "margin_market_flow": margin,
                "margin_detail": margin_detail,
            }

        recorded = await self._record_unavailable_for_empty_targets(
            db,
            f"unsupported_free_external_gap_provider:{provider}",
        )
        return {
            "enabled": True,
            "provider": provider,
            "ok": False,
            "error": "unsupported external gap-fill provider; use akshare_free",
            "source_unavailable": recorded,
        }

    async def _safe_sync(
        self,
        db,
        data_key: str,
        provider: str,
        func: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            result = await func()
            return {"ok": True, **dict(result or {})}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("[ExternalGapSync] %s failed: %s", provider, error)
            recorded = await self._record_source_unavailable_if_empty(
                db,
                data_key,
                provider,
                error,
            )
            return {
                "ok": False,
                "updated": 0,
                "error": error,
                "source_unavailable": recorded,
            }

    async def _record_unavailable_for_empty_targets(
        self,
        db,
        reason: str,
        providers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        providers = providers or {
            "north_fund_flow": "akshare.stock_hsgt_hist_em",
            "margin_market_flow": "akshare.stock_margin_sse/szse",
            "margin_detail": "akshare.stock_margin_detail_sse/szse",
        }
        result = {}
        for key, provider in providers.items():
            result[key] = await self._record_source_unavailable_if_empty(
                db,
                key,
                provider,
                reason,
            )
        return result

    async def _record_source_unavailable_if_empty(
        self,
        db,
        data_key: str,
        provider: str,
        reason: str,
    ) -> dict[str, Any]:
        tables = {
            "north_fund_flow": "north_fund_flow",
            "margin_market_flow": "margin_market_flow",
            "margin_detail": "margin_detail",
        }
        table = tables.get(data_key)
        if not table:
            return {"recorded": False, "reason": "unknown_data_key"}
        try:
            async with db.acquire() as conn:
                table_rows = int(await conn.fetchval(f"SELECT COUNT(*) FROM {table}") or 0)
        except Exception as exc:
            table_rows = 0
            logger.debug("[ExternalGapSync] count %s failed: %s", table, exc)
        if table_rows > 0:
            return {"recorded": False, "table_rows": table_rows}
        await self._record_completeness(
            db,
            data_key,
            0,
            {
                "provider": provider,
                "reason": reason,
                "primary_source": "tdx",
                "fallback_policy": "sync_layer_only",
            },
        )
        return {"recorded": True, "table_rows": 0}

    def _build_akshare_client(self):
        try:
            import akshare as ak

            return ak
        except Exception as exc:
            logger.warning("[ExternalGapSync] AkShare init failed: %s", exc)
            return None

    async def sync_north_fund_from_akshare(self, db, ak) -> dict[str, Any]:
        df = await asyncio.to_thread(ak.stock_hsgt_hist_em, symbol="北向资金")
        _, rows = _iter_df_tuples(df)
        accepted = 0
        async with db.acquire() as conn:
            for row in rows:
                trade_date = _norm_date(_at(row, 0))
                if not trade_date:
                    continue
                north_money = _safe_float(_at(row, 1))
                if north_money is None:
                    continue
                await conn.execute(
                    """
                    INSERT INTO north_fund_flow (
                        trade_date, north_money, south_money, net_amount,
                        ggt_ss, ggt_sz, hgt, sgt, source, source_priority, updated_at
                    ) VALUES ($1, $2, NULL, $2, NULL, NULL, NULL, NULL,
                              'akshare.stock_hsgt_hist_em', $3, CURRENT_TIMESTAMP)
                    ON CONFLICT (trade_date) DO UPDATE SET
                        north_money = EXCLUDED.north_money,
                        net_amount = EXCLUDED.net_amount,
                        source = EXCLUDED.source,
                        source_priority = EXCLUDED.source_priority,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    trade_date,
                    north_money,
                    _source_priority(),
                )
                accepted += 1
        await self._record_completeness(
            db,
            "north_fund_flow",
            accepted,
            {
                "provider": "akshare.stock_hsgt_hist_em",
                "reason": "tdx_field_unavailable",
                "source_priority": _source_priority(),
                "unit_note": "AkShare EastMoney northbound history; net field is used as provided.",
            },
        )
        return {"updated": accepted, "provider": "akshare.stock_hsgt_hist_em"}

    async def sync_margin_market_from_akshare(self, db, ak) -> dict[str, Any]:
        start, end = self._window(self.margin_days)
        accepted = 0
        failed_dates = 0

        sse_df = await asyncio.to_thread(ak.stock_margin_sse, start_date=start, end_date=end)
        _, sse_rows = _iter_df_tuples(sse_df)
        async with db.acquire() as conn:
            for row in sse_rows:
                if await self._insert_margin_market_row(
                    conn,
                    trade_date=_norm_date(_at(row, 0)),
                    exchange_id="SSE",
                    rzye=_safe_float(_at(row, 1)),
                    rzmre=_safe_float(_at(row, 2)),
                    rzche=None,
                    rqye=_safe_float(_at(row, 5)),
                    rqmcl=_safe_float(_at(row, 4)),
                    rqyl=_safe_float(_at(row, 3)),
                    rzrqye=_safe_float(_at(row, 6)),
                    source="akshare.stock_margin_sse",
                ):
                    accepted += 1

        for ymd in _recent_ymd_dates(self.margin_days):
            try:
                szse_df = await asyncio.to_thread(ak.stock_margin_szse, date=ymd)
            except Exception as exc:
                failed_dates += 1
                logger.debug("[ExternalGapSync] stock_margin_szse %s failed: %s", ymd, exc)
                await asyncio.sleep(0.05)
                continue
            _, szse_rows = _iter_df_tuples(szse_df)
            if not szse_rows:
                continue
            async with db.acquire() as conn:
                for row in szse_rows[:1]:
                    if await self._insert_margin_market_row(
                        conn,
                        trade_date=_norm_date(ymd),
                        exchange_id="SZSE",
                        rzye=_money_yi_to_yuan(_at(row, 1)),
                        rzmre=_money_yi_to_yuan(_at(row, 0)),
                        rzche=None,
                        rqye=_money_yi_to_yuan(_at(row, 4)),
                        rqmcl=_money_yi_to_yuan(_at(row, 2)),
                        rqyl=_money_yi_to_yuan(_at(row, 3)),
                        rzrqye=_money_yi_to_yuan(_at(row, 5)),
                        source="akshare.stock_margin_szse",
                    ):
                        accepted += 1
            await asyncio.sleep(0.05)

        await self._record_completeness(
            db,
            "margin_market_flow",
            accepted,
            {
                "provider": "akshare.stock_margin_sse/szse",
                "reason": "tdx_field_unavailable",
                "source_priority": _source_priority(),
                "failed_szse_date_count": failed_dates,
                "unit_note": "SZSE summary values are converted from 100M units to raw units.",
            },
        )
        return {"updated": accepted, "start": start, "end": end, "failed_szse_dates": failed_dates}

    async def _insert_margin_market_row(
        self,
        conn,
        *,
        trade_date: str,
        exchange_id: str,
        rzye: Optional[float],
        rzmre: Optional[float],
        rzche: Optional[float],
        rqye: Optional[float],
        rqmcl: Optional[float],
        rqyl: Optional[float],
        rzrqye: Optional[float],
        source: str,
    ) -> bool:
        if not trade_date or all(v is None for v in (rzye, rzmre, rzche, rqye, rqmcl, rqyl, rzrqye)):
            return False
        await conn.execute(
            """
            INSERT INTO margin_market_flow (
                trade_date, exchange_id, rzye, rzmre, rzche,
                rqye, rqmcl, rqyl, rzrqye,
                source, source_priority, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, CURRENT_TIMESTAMP)
            ON CONFLICT (trade_date, exchange_id) DO UPDATE SET
                rzye = EXCLUDED.rzye,
                rzmre = EXCLUDED.rzmre,
                rzche = EXCLUDED.rzche,
                rqye = EXCLUDED.rqye,
                rqmcl = EXCLUDED.rqmcl,
                rqyl = EXCLUDED.rqyl,
                rzrqye = EXCLUDED.rzrqye,
                source = EXCLUDED.source,
                source_priority = EXCLUDED.source_priority,
                updated_at = CURRENT_TIMESTAMP
            """,
            trade_date,
            exchange_id,
            rzye,
            rzmre,
            rzche,
            rqye,
            rqmcl,
            rqyl,
            rzrqye,
            source,
            _source_priority(),
        )
        return True

    async def sync_margin_detail_from_akshare(self, db, ak) -> dict[str, Any]:
        accepted = 0
        failed_dates = 0
        universe = {_to_ts_code(code) for code in self.universe if _to_ts_code(code)}

        for ymd in _recent_ymd_dates(self.margin_detail_days):
            try:
                sse_df = await asyncio.to_thread(ak.stock_margin_detail_sse, date=ymd)
                _, sse_rows = _iter_df_tuples(sse_df)
                accepted += await self._insert_margin_detail_rows(
                    db,
                    exchange="SSE",
                    ymd=ymd,
                    rows=sse_rows,
                    universe=universe,
                    source="akshare.stock_margin_detail_sse",
                )
            except Exception as exc:
                failed_dates += 1
                logger.debug("[ExternalGapSync] stock_margin_detail_sse %s failed: %s", ymd, exc)

            try:
                szse_df = await asyncio.to_thread(ak.stock_margin_detail_szse, date=ymd)
                _, szse_rows = _iter_df_tuples(szse_df)
                accepted += await self._insert_margin_detail_rows(
                    db,
                    exchange="SZSE",
                    ymd=ymd,
                    rows=szse_rows,
                    universe=universe,
                    source="akshare.stock_margin_detail_szse",
                )
            except Exception as exc:
                failed_dates += 1
                logger.debug("[ExternalGapSync] stock_margin_detail_szse %s failed: %s", ymd, exc)

            await asyncio.sleep(0.05)

        await self._record_completeness(
            db,
            "margin_detail",
            accepted,
            {
                "provider": "akshare.stock_margin_detail_sse/szse",
                "reason": "tdx_field_unavailable",
                "source_priority": _source_priority(),
                "failed_date_count": failed_dates,
                "days": self.margin_detail_days,
            },
        )
        return {"updated": accepted, "days": self.margin_detail_days, "failed_dates": failed_dates}

    async def _insert_margin_detail_rows(
        self,
        db,
        *,
        exchange: str,
        ymd: str,
        rows: list[tuple[Any, ...]],
        universe: set[str],
        source: str,
    ) -> int:
        if not rows:
            return 0
        accepted = 0
        async with db.acquire() as conn:
            for row in rows:
                if exchange == "SSE":
                    trade_date = _norm_date(_at(row, 0) or ymd)
                    ts_code = _to_exchange_ts_code(_at(row, 1), "SSE")
                    rzye = _safe_float(_at(row, 3))
                    rzmre = _safe_float(_at(row, 4))
                    rzche = _safe_float(_at(row, 5))
                    rqyl = _safe_float(_at(row, 6))
                    rqmcl = _safe_float(_at(row, 7))
                    rqchl = _safe_float(_at(row, 8))
                    rqye = None
                    rzrqye = rzye
                else:
                    trade_date = _norm_date(ymd)
                    ts_code = _to_exchange_ts_code(_at(row, 0), "SZSE")
                    rzmre = _safe_float(_at(row, 2))
                    rzye = _safe_float(_at(row, 3))
                    rqmcl = _safe_float(_at(row, 4))
                    rqyl = _safe_float(_at(row, 5))
                    rqye = _safe_float(_at(row, 6))
                    rzrqye = _safe_float(_at(row, 7))
                    rzche = None
                    rqchl = None
                if not trade_date or not ts_code or (universe and ts_code not in universe):
                    continue
                if all(v is None for v in (rzye, rqye, rzmre, rqyl, rzche, rqchl, rqmcl, rzrqye)):
                    continue
                await conn.execute(
                    """
                    INSERT INTO margin_detail (
                        trade_date, ts_code, rzye, rqye, rzmre,
                        rqyl, rzche, rqchl, rqmcl, rzrqye,
                        source, source_priority, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                              $11, $12, CURRENT_TIMESTAMP)
                    ON CONFLICT (trade_date, ts_code) DO UPDATE SET
                        rzye = EXCLUDED.rzye,
                        rqye = EXCLUDED.rqye,
                        rzmre = EXCLUDED.rzmre,
                        rqyl = EXCLUDED.rqyl,
                        rzche = EXCLUDED.rzche,
                        rqchl = EXCLUDED.rqchl,
                        rqmcl = EXCLUDED.rqmcl,
                        rzrqye = EXCLUDED.rzrqye,
                        source = EXCLUDED.source,
                        source_priority = EXCLUDED.source_priority,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    trade_date,
                    ts_code,
                    rzye,
                    rqye,
                    rzmre,
                    rqyl,
                    rzche,
                    rqchl,
                    rqmcl,
                    rzrqye,
                    source,
                    _source_priority(),
                )
                accepted += 1
        return accepted

    @staticmethod
    def _window(days: int) -> tuple[str, str]:
        end = datetime.now()
        start = end - timedelta(days=max(1, int(days)))
        return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    async def _record_completeness(self, db, key: str, count: int, detail: dict[str, Any]) -> None:
        if not hasattr(db, "save_tdx_data_completeness"):
            return
        await db.save_tdx_data_completeness(
            key,
            "ok" if int(count or 0) > 0 else "source_unavailable",
            row_count=int(count or 0),
            detail={
                "primary_source": "tdx",
                "source_priority": _source_priority(),
                **dict(detail or {}),
            },
        )
