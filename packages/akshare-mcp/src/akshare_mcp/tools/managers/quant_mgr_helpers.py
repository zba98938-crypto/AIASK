"""Quant manager helpers: utility functions, sentiment analysis, factor analysis."""

import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Optional

import numpy as np

from ...services.db_first_market_context import (
    load_db_first_document_context,
    load_db_first_stock_fund_flow,
)
from ..news.news_feed import get_stock_news
from ..news.notices import get_stock_notices
from ..news.research import get_research_reports

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentiment token lists
# ---------------------------------------------------------------------------

POSITIVE_SENTIMENT_TOKENS = [
    "buy",
    "upgrade",
    "outperform",
    "\u5229\u597d",
    "\u4e0a\u8c03",
    "\u589e\u6301",
    "\u8d85\u9884\u671f",
    "\u7a81\u7834",
]

NEGATIVE_SENTIMENT_TOKENS = [
    "sell",
    "downgrade",
    "underperform",
    "\u5229\u7a7a",
    "\u4e0b\u8c03",
    "\u51cf\u6301",
    "\u4e0d\u53ca\u9884\u671f",
    "\u98ce\u9669",
]

# ---------------------------------------------------------------------------
# Generic utility helpers
# ---------------------------------------------------------------------------


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        parsed = float(value)
        if not math.isfinite(parsed):
            return default
        return parsed
    except (TypeError, ValueError):
        return default


def _as_code_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass
        return [x.strip() for x in text.split(",") if x.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _parse_date_value(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    for parser in (
        lambda item: date.fromisoformat(item[:10]),
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")).date(),
    ):
        try:
            return parser(text)
        except Exception:
            continue
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        try:
            return date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
        except Exception:
            return None
    return None


def _sort_klines_ascending(klines: list[dict]) -> list[dict]:
    rows = [dict(item) for item in list(klines or []) if isinstance(item, dict)]
    return sorted(
        rows,
        key=lambda item: (
            _parse_date_value(item.get("date")) or date.min,
            str(item.get("date") or ""),
        ),
    )


def _select_financial_snapshot(financials: Any, *, as_of_date: Optional[date] = None) -> dict[str, Any]:
    rows = []
    if isinstance(financials, dict):
        rows = [dict(financials)]
    elif isinstance(financials, list):
        rows = [dict(item) for item in financials if isinstance(item, dict)]
    if not rows:
        return {}

    if as_of_date is None:
        return rows[0]

    dated_rows = []
    for row in rows:
        report_date = _parse_date_value(row.get("report_date"))
        if report_date is None:
            continue
        dated_rows.append((report_date, row))
    dated_rows.sort(key=lambda item: item[0], reverse=True)
    for report_date, row in dated_rows:
        if report_date <= as_of_date:
            return row
    return {}


async def _load_valuation_snapshot(
    db,
    code: str,
    *,
    as_of_date: Optional[date] = None,
) -> dict[str, Any]:
    normalized_code = str(code or "").strip()
    if not normalized_code:
        return {}

    if as_of_date is not None and hasattr(db, "acquire"):
        try:
            async with db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT time, pe, pb, mkt_cap, price
                    FROM stock_quotes
                    WHERE code = $1 AND time <= date($2, '+1 day')
                    ORDER BY time DESC
                    LIMIT 1
                    """,
                    normalized_code,
                    as_of_date.isoformat(),
                )
            if row:
                payload = dict(row)
                return {
                    "pe_ratio": _safe_float(payload.get("pe"), default=0.0),
                    "pb_ratio": _safe_float(payload.get("pb"), default=0.0),
                    "market_cap": _safe_float(payload.get("mkt_cap"), default=0.0),
                    "price": _safe_float(payload.get("price"), default=0.0),
                    "as_of_date": _parse_date_value(payload.get("time")),
                }
        except Exception:
            pass

    # Fallback: 从 stocks 表取静态 PE/PB（适用于因子计算场景）
    if hasattr(db, "acquire"):
        try:
            async with db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT pe_ratio, pb_ratio, market_cap FROM stocks WHERE stock_code = $1",
                    normalized_code,
                )
            if row:
                payload = dict(row)
                pe = _safe_float(payload.get("pe_ratio"), default=0.0)
                pb = _safe_float(payload.get("pb_ratio"), default=0.0)
                if pe > 0 or pb > 0:
                    return {
                        "pe_ratio": pe,
                        "pb_ratio": pb,
                        "market_cap": _safe_float(payload.get("market_cap"), default=0.0),
                        "source": "stocks_table",
                    }
        except Exception:
            pass

    if hasattr(db, "get_stock_info"):
        try:
            payload = await db.get_stock_info(normalized_code)
            if isinstance(payload, dict):
                return {
                    "pe_ratio": _safe_float(payload.get("pe_ratio"), default=0.0),
                    "pb_ratio": _safe_float(payload.get("pb_ratio"), default=0.0),
                    "market_cap": _safe_float(payload.get("market_cap"), default=0.0),
                    "price": _safe_float(payload.get("price"), default=0.0),
                    "as_of_date": as_of_date,
                }
        except Exception:
            return {}
    return {}


def _compute_scalar_factor_bundle(
    closes: list[float],
    *,
    financial_snapshot: Optional[dict[str, Any]] = None,
    valuation_snapshot: Optional[dict[str, Any]] = None,
    factors: Optional[list[str]] = None,
) -> dict[str, float]:
    normalized_closes = [float(item) for item in list(closes or []) if item is not None]
    requested = set(factors or [])
    financial_snapshot = dict(financial_snapshot or {})
    valuation_snapshot = dict(valuation_snapshot or {})
    factor_values: dict[str, float] = {}

    if "momentum" in requested:
        m20 = (
            (normalized_closes[-1] - normalized_closes[-20]) / normalized_closes[-20]
            if len(normalized_closes) >= 20 and normalized_closes[-20]
            else 0.0
        )
        m60 = (
            (normalized_closes[-1] - normalized_closes[-60]) / normalized_closes[-60]
            if len(normalized_closes) >= 60 and normalized_closes[-60]
            else 0.0
        )
        factor_values["momentum"] = float((m20 + m60) / 2.0)

    if "value" in requested:
        pe = _safe_float(
            valuation_snapshot.get("pe_ratio", financial_snapshot.get("pe_ratio")),
            default=0.0,
        )
        pb = _safe_float(
            valuation_snapshot.get("pb_ratio", financial_snapshot.get("pb_ratio")),
            default=0.0,
        )
        components = []
        if pe > 0:
            components.append(1.0 / pe)
        if pb > 0:
            components.append(1.0 / pb)
        factor_values["value"] = float(sum(components) / len(components)) if components else 0.0

    if "quality" in requested:
        roe = _safe_float(financial_snapshot.get("roe"), default=0.0)
        roa = _safe_float(financial_snapshot.get("roa"), default=0.0)
        gross_margin = _safe_float(financial_snapshot.get("gross_margin"), default=0.0)
        debt_ratio = _safe_float(financial_snapshot.get("debt_ratio"), default=1.0)
        factor_values["quality"] = float(
            (roe / 30.0 if roe > 0 else 0.0) * 0.4
            + (roa / 15.0 if roa > 0 else 0.0) * 0.3
            + (gross_margin / 50.0 if gross_margin > 0 else 0.0) * 0.2
            + ((1.0 - debt_ratio) if 0 <= debt_ratio < 1 else 0.0) * 0.1
        )

    if "growth" in requested:
        revenue_growth = _safe_float(financial_snapshot.get("revenue_growth"), default=0.0)
        profit_growth = _safe_float(financial_snapshot.get("profit_growth"), default=0.0)
        factor_values["growth"] = float(
            max(min((revenue_growth + profit_growth) / 200.0, 1.0), -1.0)
        )

    if "volatility" in requested:
        prices = np.array(normalized_closes, dtype=float)
        returns = np.diff(prices) / prices[:-1] if len(prices) > 1 else np.array([], dtype=float)
        annual_vol = float(np.std(returns) * np.sqrt(252)) if len(returns) > 1 else 0.0
        factor_values["volatility"] = float(1.0 / annual_vol) if annual_vol > 0 else 0.0

    if "reversal" in requested:
        factor_values["reversal"] = float(
            -((normalized_closes[-1] - normalized_closes[-6]) / normalized_closes[-6])
        ) if len(normalized_closes) >= 6 and normalized_closes[-6] else 0.0

    return factor_values


# ---------------------------------------------------------------------------
# Sentiment scoring
# ---------------------------------------------------------------------------


def _headline_sentiment_score(headlines: list[str]) -> tuple[float, dict]:
    if not headlines:
        return 0.0, {"positive_hits": 0, "negative_hits": 0, "coverage": 0.0}

    pos_hits = 0
    neg_hits = 0
    for title in headlines:
        text = str(title or "").lower()
        if not text:
            continue
        pos_hits += sum(1 for token in POSITIVE_SENTIMENT_TOKENS if token in text)
        neg_hits += sum(1 for token in NEGATIVE_SENTIMENT_TOKENS if token in text)

    total = len(headlines)
    score = (pos_hits - neg_hits) / max(total * 2, 1)
    coverage = (pos_hits + neg_hits) / max(total, 1)
    return float(_clip(score, -1.0, 1.0)), {
        "positive_hits": int(pos_hits),
        "negative_hits": int(neg_hits),
        "coverage": float(_clip(coverage, 0.0, 1.0)),
    }


def _extract_news_items(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    if not payload.get("success"):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        events = data.get("events")
        if isinstance(events, list):
            return [x for x in events if isinstance(x, dict)]
    return []


def _sort_rows_by_trade_date_desc(rows: list[dict]) -> list[dict]:
    return sorted(
        [dict(item) for item in list(rows or []) if isinstance(item, dict)],
        key=lambda item: (
            _parse_date_value(item.get("trade_date")) or date.min,
            str(item.get("trade_date") or ""),
        ),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Numpy helpers
# ---------------------------------------------------------------------------


def _rank_transform(vec: np.ndarray) -> np.ndarray:
    n = len(vec)
    if n <= 1:
        return np.zeros(n, dtype=np.float64)
    order = np.argsort(vec)
    ranks = np.empty(n, dtype=np.float64)
    ranks[order] = np.linspace(-1.0, 1.0, n)
    return ranks


def _filter_quant_artifacts(artifacts: list[dict]) -> list[dict]:
    out = []
    for item in artifacts:
        strategy = str(item.get("strategy") or "").lower()
        if strategy.startswith("quant_") or strategy.startswith("feature_store"):
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Alternative factors computation (sentiment + event + capital flow + north)
# ---------------------------------------------------------------------------


async def _compute_alternative_factors_for_code(
    db,
    code: str,
    lookback_days: int = 30,
    limit: int = 20,
) -> tuple[dict, list[str]]:
    code = str(code or "").strip()
    source_chain: list[str] = []
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=max(7, int(lookback_days)))

    db_context = {"news": [], "notices": [], "research": []}
    try:
        db_context, db_source_chain = await load_db_first_document_context(
            db,
            code,
            start_date=start_date,
            end_date=end_date,
            news_limit=max(5, int(limit)),
            notice_limit=max(5, int(limit)),
            research_limit=max(5, int(limit)),
        )
        source_chain.extend(db_source_chain)
    except Exception:
        db_context = {"news": [], "notices": [], "research": []}

    news_items = list(db_context.get("news") or [])
    if not news_items:
        news_payload = get_stock_news(code, limit=max(5, int(limit)))
        source_chain.append("tools.news.get_stock_news")
        news_items = _extract_news_items(news_payload)

    notice_items = list(db_context.get("notices") or [])
    if not notice_items:
        notice_payload = get_stock_notices(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            stock_code=code,
        )
        source_chain.append("tools.news.get_stock_notices")
        notice_items = _extract_news_items(notice_payload)

    research_items = list(db_context.get("research") or [])
    if not research_items:
        research_payload = get_research_reports(code, limit=max(5, int(limit)))
        source_chain.append("tools.news.get_research_reports")
        research_items = _extract_news_items(research_payload)

    flow_data = {}
    try:
        flow_data, flow_source_chain = await load_db_first_stock_fund_flow(db, code)
        source_chain.extend(flow_source_chain)
    except Exception:
        flow_data = {}

    north_rows = []
    north_days = max(5, min(int(lookback_days), 30))
    try:
        getter = getattr(db, "get_north_fund_history", None)
        if callable(getter):
            north_rows = await getter(days=north_days, end_date=end_date)
            north_rows = _sort_rows_by_trade_date_desc(north_rows)
            if north_rows:
                source_chain.append("db.get_north_fund_history")
    except Exception:
        north_rows = []

    headlines = []
    for item in news_items + notice_items + research_items:
        title = item.get("title")
        if title:
            headlines.append(str(title))

    sentiment_raw, sentiment_hits = _headline_sentiment_score(headlines)

    notice_count = len(notice_items)
    research_count = len(research_items)
    news_count = len(news_items)
    event_intensity = _clip(
        (notice_count * 1.2 + research_count * 0.8 + news_count * 0.3) / max(1.0, float(lookback_days)),
        0.0,
        1.0,
    )

    main_inflow = 0.0
    large_inflow = 0.0
    small_inflow = 0.0
    if isinstance(flow_data, dict) and flow_data:
        main_inflow = _safe_float(flow_data.get("mainNetInflow"), 0.0)
        large_inflow = _safe_float(flow_data.get("largeNetInflow"), 0.0) + _safe_float(
            flow_data.get("superLargeNetInflow"), 0.0
        )
        small_inflow = _safe_float(flow_data.get("smallNetInflow"), 0.0)

    capital_flow_raw = np.tanh(main_inflow / 5e8) if abs(main_inflow) > 0 else 0.0
    institutional_raw = np.tanh((large_inflow - small_inflow) / 5e8) if (large_inflow or small_inflow) else 0.0
    capital_behavior_raw = float(_clip(0.65 * capital_flow_raw + 0.35 * institutional_raw, -1.0, 1.0))

    north_flow_raw = 0.0
    north_totals: list[float] = []
    if north_rows:
        tail = north_rows[:5]
        north_totals = [_safe_float(x.get("north_money"), 0.0) for x in tail]
    if north_totals:
        north_flow_raw = float(np.tanh((np.mean(north_totals) if north_totals else 0.0) / 1e9))

    composite_raw = float(
        _clip(
            0.35 * sentiment_raw + 0.25 * (2.0 * event_intensity - 1.0) + 0.30 * capital_behavior_raw + 0.10 * north_flow_raw,
            -1.0,
            1.0,
        )
    )

    return (
        {
            "sentiment": {
                "score_raw": float(sentiment_raw),
                "score": float((sentiment_raw + 1.0) / 2.0),
                "news_count": news_count,
                "headline_count": len(headlines),
                "hits": sentiment_hits,
            },
            "event": {
                "score_raw": float(2.0 * event_intensity - 1.0),
                "score": float(event_intensity),
                "notice_count": notice_count,
                "research_count": research_count,
                "window_days": int(lookback_days),
            },
            "capital_flow": {
                "score_raw": float(capital_behavior_raw),
                "score": float((capital_behavior_raw + 1.0) / 2.0),
                "main_net_inflow": float(main_inflow),
                "large_net_inflow": float(large_inflow),
                "small_net_inflow": float(small_inflow),
                "north_flow_score_raw": float(north_flow_raw),
            },
            "alternative_composite": {
                "score_raw": float(composite_raw),
                "score": float((composite_raw + 1.0) / 2.0),
                "method": "weighted(sentiment,event,capital,north)",
            },
        },
        source_chain,
    )
