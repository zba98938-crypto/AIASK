"""
SQLite 适配器 — K线数据 Mixin

提供 get_klines / save_klines / get_limit_up_stats 方法。
"""

import logging
import json
from typing import Optional, List, Dict, Any
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from aiask_quant_core.core.validators import validate_kline_list


logger = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE_TIME = time(hour=15, minute=0)


def _to_market_close_timestamp(value: datetime | date) -> datetime:
    """Normalize daily bars to Asia/Shanghai market close.

    The DB column is TEXT. Persisting naive midnight datetimes causes the
    stored UTC date to roll back to the previous calendar day, which breaks many
    `time` queries. Daily bars are therefore normalized to the market-close
    instant of the intended trade date.
    """

    if isinstance(value, datetime):
        if value.tzinfo is None:
            trade_date = value.date()
        else:
            trade_date = value.astimezone(MARKET_TZ).date()
    else:
        trade_date = value
    return datetime.combine(trade_date, MARKET_CLOSE_TIME, tzinfo=MARKET_TZ)


def _format_trade_date(value: datetime | date) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(MARKET_TZ)
        return value.strftime('%Y-%m-%d')
    return value.strftime('%Y-%m-%d')


def _normalize_intraday_period(value: Any) -> str:
    token = str(value or "").strip().lower()
    aliases = {
        "1": "1m",
        "5": "5m",
        "15": "15m",
        "30": "30m",
        "60": "60m",
        "1min": "1m",
        "5min": "5m",
        "15min": "15m",
        "30min": "30m",
        "60min": "60m",
        "minute": "1m",
    }
    return aliases.get(token, token or "1m")


def _parse_intraday_timestamp(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    elif isinstance(value, str):
        token = value.strip()
        if not token:
            return None
        if token.endswith("Z"):
            token = f"{token[:-1]}+00:00"
        token = token.replace("/", "-")
        try:
            dt = datetime.fromisoformat(token)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d %H:%M:%S", "%Y%m%d %H:%M"):
                try:
                    dt = datetime.strptime(token, fmt)
                    break
                except ValueError:
                    dt = None
            if dt is None:
                return token
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MARKET_TZ)
    else:
        dt = dt.astimezone(MARKET_TZ)
    return dt.isoformat()


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _json_text(value: Any, default: Any) -> str:
    if value in (None, ""):
        value = default
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = [value]
        value = parsed
    return json.dumps(value, ensure_ascii=False, default=str)


def _decode_json_text(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


class KlineMixin:
    """K线数据读写"""

    async def get_klines(
        self,
        code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """查询K线数据

        Args:
            code: 股票代码
            start_date: 开始日期 (YYYY-MM-DD 或 YYYY)
            end_date: 结束日期 (YYYY-MM-DD 或 YYYY)
            limit: 限制返回条数；当设置时返回“最近 N 根”，但结果仍按时间升序
        """
        async with self.acquire() as conn:
            base_query = """
                SELECT
                    time, code, open, high, low, close,
                    volume, amount, turnover, change_pct
                FROM kline_1d
                WHERE code = $1
            """
            query = base_query
            params: list = [code]
            param_idx = 2

            if start_date:
                if isinstance(start_date, str):
                    if len(start_date) == 4:
                        start_date = f"{start_date}-01-01"
                    start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
                    start_bound = datetime.combine(start_date_obj, time.min, tzinfo=MARKET_TZ)
                    query += f" AND time >= ${param_idx}"
                    params.append(start_bound)
                    param_idx += 1

            if end_date:
                if isinstance(end_date, str):
                    if len(end_date) == 4:
                        end_date = f"{end_date}-12-31"
                    end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()
                    end_bound = datetime.combine(end_date_obj + timedelta(days=1), time.min, tzinfo=MARKET_TZ)
                    query += f" AND time < ${param_idx}"
                    params.append(end_bound)
                    param_idx += 1

            if limit:
                params.append(limit)
                query = f"""
                    SELECT
                        time, code, open, high, low, close,
                        volume, amount, turnover, change_pct
                    FROM (
                        {query}
                        ORDER BY time DESC
                        LIMIT ${param_idx}
                    ) recent_bars
                    ORDER BY time ASC
                """
            else:
                query += " ORDER BY time ASC"

            rows = await conn.fetch(query, *params)

            return [
                {
                    'date': _format_trade_date(row['time']) if isinstance(row['time'], (datetime, date)) else str(row['time'] or '')[:10],
                    'code': row['code'],
                    'open': float(row['open']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'close': float(row['close']),
                    'volume': int(row['volume']),
                    'amount': float(row['amount']) if row['amount'] else None,
                    'turnover': float(row['turnover']) if row['turnover'] else None,
                    'change_pct': float(row['change_pct']) if row['change_pct'] else None,
                    'source': 'sqlite',
                }
                for row in rows
            ]

    async def get_index_klines(
        self,
        code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Read index daily bars from ``kline_1d``.

        Index rows are stored with market-prefixed codes such as ``sh000001``
        and ``sz399006`` to avoid colliding with stock codes.
        """
        raw = str(code or "").strip()
        aliases = {
            "000001": "sh000001",
            "sh000001": "sh000001",
            "000300": "sh000300",
            "sh000300": "sh000300",
            "399001": "sz399001",
            "sz399001": "sz399001",
            "399006": "sz399006",
            "sz399006": "sz399006",
        }
        normalized = aliases.get(raw.lower(), raw)
        return await self.get_klines(
            normalized,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )

    async def save_klines(self, code_or_klines, klines: Optional[List[Dict[str, Any]]] = None) -> dict:
        """批量保存K线数据

        Args:
            code_or_klines: 兼容参数。支持 save_klines(klines) 和 save_klines(code, klines)
            klines: K线数据列表（当第一个参数是 code 时使用）

        Returns:
            质量摘要字典，包含 accepted_count / rejected_count / accept_ratio
        """
        from datetime import datetime as _dt, date as _date

        if klines is None:
            code = None
            klines_list = code_or_klines
        else:
            code = str(code_or_klines) if code_or_klines is not None else None
            klines_list = klines

        if not klines_list:
            return {'accepted_count': 0, 'rejected_count': 0, 'accept_ratio': 1.0}

        def _parse_date(val):
            """安全解析日期，兼容多种格式"""
            if isinstance(val, (_dt, _date)):
                return val
            if isinstance(val, str):
                val = val.strip()[:10]
                for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
                    try:
                        return _dt.strptime(val, fmt)
                    except ValueError:
                        continue
            return None

        async with self.acquire() as conn:
            query = """
                INSERT INTO kline_1d (
                    time, code, open, high, low, close,
                    volume, amount, turnover, change_pct, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, CURRENT_TIMESTAMP)
                ON CONFLICT (time, code) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    amount = EXCLUDED.amount,
                    turnover = EXCLUDED.turnover,
                    change_pct = EXCLUDED.change_pct,
                    updated_at = CURRENT_TIMESTAMP
            """

            prepared_rows: list[dict] = []
            rejected_rows: list[dict] = []
            for idx, k in enumerate(klines_list):
                if not isinstance(k, dict):
                    rejected_rows.append({
                        "index": idx,
                        "reason": "row_is_not_dict",
                        "row": {"raw": k},
                    })
                    continue
                payload = dict(k)
                if code and not payload.get("code"):
                    payload["code"] = code
                prepared_rows.append(payload)

            validation_report = validate_kline_list(prepared_rows, return_report=True)
            rows = []
            rejected_rows.extend(list(validation_report.get("rejected") or []))

            for validated in list(validation_report.get("accepted") or []):
                parsed_date = _parse_date(validated.get('date'))
                row_code = validated.get('code')
                open_ = validated.get('open')
                high = validated.get('high')
                low = validated.get('low')
                close = validated.get('close')
                volume = validated.get('volume')
                if parsed_date is None:
                    rejected_rows.append({
                        "index": None,
                        "reason": "invalid_date",
                        "row": validated,
                    })
                    continue
                if row_code is None or open_ is None or high is None or low is None or close is None or volume is None:
                    rejected_rows.append({
                        "index": None,
                        "reason": "missing_required_fields",
                        "row": validated,
                    })
                    continue
                rows.append((
                    _to_market_close_timestamp(parsed_date), row_code, open_, high, low, close,
                    volume, validated.get('amount'), validated.get('turnover'), validated.get('change_pct')
                ))

            if rejected_rows:
                try:
                    from aiask_quant_core.storage.runtime_hooks import get_rejected_kline_recorder

                    recorder = get_rejected_kline_recorder()
                    if callable(recorder):
                        recorder(
                            stock_code=code,
                            rejected_rows=rejected_rows,
                            source="sqlite.save_klines",
                        )
                except Exception as exc:
                    logger.warning("Persist rejected kline rows failed for %s: %s", code or "unknown", exc)

            if rows:
                await conn.executemany(query, rows)
            elif rejected_rows:
                raise ValueError(
                    f"all kline rows rejected for {code or 'unknown'}: rejected_count={len(rejected_rows)}"
                )

            total = len(rows) + len(rejected_rows)
            accept_ratio = len(rows) / total if total > 0 else 1.0
            return {
                'accepted_count': len(rows),
                'rejected_count': len(rejected_rows),
                'accept_ratio': round(accept_ratio, 6),
            }

    async def save_intraday_bars(
        self,
        code_or_bars,
        bars: Optional[List[Dict[str, Any]]] = None,
        *,
        period: Optional[str] = None,
        adjust: str = "",
        source: Optional[str] = None,
    ) -> dict:
        """Upsert normalized intraday bars into ``kline_intraday``."""

        if bars is None:
            default_code = None
            bars_list = code_or_bars
        else:
            default_code = str(code_or_bars).strip() if code_or_bars is not None else None
            bars_list = bars

        if not bars_list:
            return {
                "accepted_count": 0,
                "rejected_count": 0,
                "accept_ratio": 1.0,
                "data_quality_status_counts": {},
            }

        rows: list[tuple] = []
        rejected_rows: list[dict] = []
        quality_counts: dict[str, int] = {}
        for idx, bar in enumerate(bars_list):
            if not isinstance(bar, dict):
                rejected_rows.append({"index": idx, "reason": "row_is_not_dict", "row": {"raw": bar}})
                continue
            payload = dict(bar)
            row_code = str(payload.get("code") or default_code or "").strip()
            row_period = _normalize_intraday_period(payload.get("period") or period)
            row_adjust = str(payload.get("adjust") if payload.get("adjust") is not None else adjust or "").strip()
            timestamp = _parse_intraday_timestamp(
                payload.get("timestamp")
                or payload.get("time")
                or payload.get("datetime")
                or payload.get("date_time")
                or payload.get("date")
            )
            open_ = _float_or_none(payload.get("open"))
            high = _float_or_none(payload.get("high"))
            low = _float_or_none(payload.get("low"))
            close = _float_or_none(payload.get("close"))
            if not row_code or not row_period or not timestamp:
                rejected_rows.append({"index": idx, "reason": "missing_identity_fields", "row": payload})
                continue
            if open_ is None or high is None or low is None or close is None:
                rejected_rows.append({"index": idx, "reason": "missing_ohlc_fields", "row": payload})
                continue
            quality_status = str(payload.get("data_quality_status") or "ok").strip() or "ok"
            if high < max(open_, low, close) or low > min(open_, high, close):
                quality_status = "invalid_ohlc"
            volume = _float_or_none(payload.get("volume") if payload.get("volume") is not None else payload.get("vol"))
            amount = _float_or_none(payload.get("amount"))
            row_source = str(payload.get("source") or source or "").strip() or None
            source_chain = _json_text(payload.get("source_chain"), [])
            rows.append(
                (
                    row_code,
                    row_period,
                    timestamp,
                    row_adjust,
                    open_,
                    high,
                    low,
                    close,
                    volume,
                    amount,
                    row_source,
                    source_chain,
                    quality_status,
                )
            )
            quality_counts[quality_status] = quality_counts.get(quality_status, 0) + 1

        if rows:
            async with self.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO kline_intraday (
                        code, period, timestamp, adjust,
                        open, high, low, close, volume, amount,
                        source, source_chain, data_quality_status,
                        created_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (code, period, timestamp, adjust) DO UPDATE SET
                        open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        amount = EXCLUDED.amount,
                        source = EXCLUDED.source,
                        source_chain = EXCLUDED.source_chain,
                        data_quality_status = EXCLUDED.data_quality_status,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    rows,
                )

        total = len(rows) + len(rejected_rows)
        accept_ratio = len(rows) / total if total > 0 else 1.0
        return {
            "accepted_count": len(rows),
            "rejected_count": len(rejected_rows),
            "accept_ratio": round(accept_ratio, 6),
            "data_quality_status_counts": quality_counts,
            "rejected": rejected_rows[:20],
        }

    async def list_intraday_bars(
        self,
        code: str,
        period: str,
        *,
        start_ts: Optional[str] = None,
        end_ts: Optional[str] = None,
        limit: Optional[int] = None,
        adjust: str = "",
    ) -> List[Dict[str, Any]]:
        """List intraday bars in timestamp ascending order."""

        where = ["code = $1", "period = $2", "adjust = $3"]
        params: list[Any] = [str(code or "").strip(), _normalize_intraday_period(period), str(adjust or "").strip()]
        if start_ts:
            params.append(_parse_intraday_timestamp(start_ts) or str(start_ts))
            where.append(f"timestamp >= ${len(params)}")
        if end_ts:
            params.append(_parse_intraday_timestamp(end_ts) or str(end_ts))
            where.append(f"timestamp <= ${len(params)}")
        query = f"""
            SELECT
                code, period, timestamp, adjust,
                open, high, low, close, volume, amount,
                source, source_chain, data_quality_status,
                created_at, updated_at
            FROM kline_intraday
            WHERE {" AND ".join(where)}
            ORDER BY datetime(timestamp) ASC, timestamp ASC
        """
        if limit is not None:
            try:
                limit_value = max(1, min(int(limit), 10000))
            except Exception:
                limit_value = 1000
            params.append(limit_value)
            query += f" LIMIT ${len(params)}"
        async with self.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [
            {
                "code": row["code"],
                "period": row["period"],
                "timestamp": row["timestamp"],
                "adjust": row["adjust"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]) if row["volume"] is not None else None,
                "amount": float(row["amount"]) if row["amount"] is not None else None,
                "source": row["source"],
                "source_chain": _decode_json_text(row["source_chain"], []),
                "data_quality_status": row["data_quality_status"] or "unknown",
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    async def get_limit_up_stats(self, target_date: Optional[date] = None) -> Optional[Dict[str, Any]]:
        """统计指定日期的涨跌停和涨跌家数

        通过比较 kline_1d 中同一股票相邻两个交易日的收盘价来判断涨跌停。
        A股主板涨跌停幅度为10%（ST为5%），此处用9.8%作为阈值以容忍精度误差。

        Args:
            target_date: 目标日期，默认为最近一个有数据的交易日

        Returns:
            包含 limit_up_count, limit_down_count, advance_count, decline_count 的字典，
            或在无数据时返回 None
        """
        async with self.acquire() as conn:
            if target_date is None:
                row = await conn.fetchrow(
                    "SELECT MAX(time) as latest FROM kline_1d"
                )
                if not row or not row['latest']:
                    return None
                target_date = row['latest']

            result = await conn.fetchrow("""
                WITH daily AS (
                    SELECT code, close,
                           LAG(close) OVER (PARTITION BY code ORDER BY time) AS prev_close
                    FROM kline_1d
                    WHERE date(time) BETWEEN date($1, '-5 days') AND date($1)
                )
                SELECT
                    COUNT(*) FILTER (WHERE prev_close > 0 AND (close - prev_close) / prev_close >= 0.098) AS limit_up_count,
                    COUNT(*) FILTER (WHERE prev_close > 0 AND (close - prev_close) / prev_close <= -0.098) AS limit_down_count,
                    COUNT(*) FILTER (WHERE close > prev_close) AS advance_count,
                    COUNT(*) FILTER (WHERE close < prev_close) AS decline_count
                FROM daily
                WHERE prev_close IS NOT NULL
                  AND close IS NOT NULL
                  AND code IN (SELECT DISTINCT code FROM kline_1d WHERE date(time) = date($1))
            """, target_date)

            if not result or result['advance_count'] is None:
                return None

            return {
                'limit_up_count': int(result['limit_up_count'] or 0),
                'limit_down_count': int(result['limit_down_count'] or 0),
                'advance_count': int(result['advance_count'] or 0),
                'decline_count': int(result['decline_count'] or 0),
            }
