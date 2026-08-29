"""因子研究 prompt 构建器。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np

from .db_first_market_context import (
    load_db_first_document_context,
    load_db_first_stock_fund_flow,
)
from ..tools.fund_flow import get_stock_fund_flow
from ..tools.news.news_feed import get_stock_news
from ..tools.news.notices import get_stock_notices
from ..tools.news.research import get_research_reports
from ..tools.managers.quant_mgr_helpers import (
    _compute_alternative_factors_for_code,
    _extract_news_items,
    _load_valuation_snapshot,
    _safe_float,
    _select_financial_snapshot,
    _sort_klines_ascending,
)
from .factor_candidate_compiler import SUPPORTED_FACTOR_FIELDS, SUPPORTED_FACTOR_FUNCTIONS


@dataclass
class FactorMiningPrompt:
    """因子研究 prompt 合同。"""

    system_prompt: str
    user_prompt: str
    context_summary: dict[str, Any]
    request_payload: dict[str, Any]
    source_chain: list[str]
    schema_path: str


_ALLOWED_OPERATORS = sorted(SUPPORTED_FACTOR_FUNCTIONS)
_DEFAULT_FIELD_HINTS = sorted(SUPPORTED_FACTOR_FIELDS)


def _summarize_klines(klines: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = _sort_klines_ascending(klines or [])
    closes = [_safe_float(item.get("close"), 0.0) for item in ordered if _safe_float(item.get("close"), 0.0) > 0]
    volumes = [_safe_float(item.get("volume"), 0.0) for item in ordered]
    if not closes:
        return {}

    latest_close = closes[-1]
    ret_20 = ((latest_close - closes[-20]) / closes[-20]) if len(closes) >= 20 and closes[-20] > 0 else 0.0
    ret_60 = ((latest_close - closes[-60]) / closes[-60]) if len(closes) >= 60 and closes[-60] > 0 else 0.0

    returns = []
    for idx in range(1, len(closes)):
        prev = closes[idx - 1]
        if prev <= 0:
            continue
        returns.append((closes[idx] - prev) / prev)
    annual_vol = float(np.std(returns) * np.sqrt(252)) if len(returns) > 1 else 0.0

    volume_ratio_5_20 = 1.0
    if len(volumes) >= 20:
        v5 = float(np.mean(volumes[-5:]))
        v20 = float(np.mean(volumes[-20:]))
        volume_ratio_5_20 = (v5 / v20) if v20 > 0 else 1.0

    return {
        "latest_close": round(float(latest_close), 4),
        "momentum_20d": round(float(ret_20), 6),
        "momentum_60d": round(float(ret_60), 6),
        "volatility_20d": round(float(annual_vol), 6),
        "volume_ratio_5_20": round(float(volume_ratio_5_20), 6),
        "bars": len(ordered),
        "latest_date": str((ordered[-1] or {}).get("date") or ""),
    }


def _extract_titles(payload: Any, *, limit: int = 6) -> list[str]:
    if isinstance(payload, list):
        items = [dict(item) for item in payload if isinstance(item, dict)]
    else:
        items = _extract_news_items(payload)
    titles = []
    for item in items:
        title = str(item.get("title") or item.get("headline") or "").strip()
        if title:
            titles.append(title[:120])
        if len(titles) >= limit:
            break
    return titles


async def build_factor_mining_prompt(
    db,
    codes: list[str],
    *,
    candidate_count: int = 8,
    lookback_bars: int = 180,
    alternative_lookback_days: int = 30,
    headline_limit: int = 6,
    schema_path: str = "",
    memory_context: Optional[dict[str, Any]] = None,
) -> FactorMiningPrompt:
    """构建结构化因子研究 prompt。"""

    from .factor_llm_provider import get_factor_candidate_schema_path

    normalized_codes = [str(code).strip() for code in list(codes or []) if str(code).strip()]
    if not normalized_codes:
        raise ValueError("codes 不能为空")

    summary_rows: list[dict[str, Any]] = []
    source_chain = ["db.get_klines", "db.get_financials"]
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=max(7, int(alternative_lookback_days)))

    for code in normalized_codes[:8]:
        klines = []
        try:
            klines = await db.get_klines(code, limit=max(120, int(lookback_bars)))
        except Exception:
            klines = []

        financial_snapshot = {}
        try:
            financials = await db.get_financials(code, limit=4)
            financial_snapshot = _select_financial_snapshot(financials) or {}
        except Exception:
            financial_snapshot = {}

        try:
            valuation_snapshot = await _load_valuation_snapshot(db, code)
        except Exception:
            valuation_snapshot = {}

        try:
            alternative_factors, alt_source_chain = await _compute_alternative_factors_for_code(
                db=db,
                code=code,
                lookback_days=max(7, int(alternative_lookback_days)),
                limit=max(5, int(headline_limit)),
            )
            source_chain.extend(list(alt_source_chain or []))
        except Exception:
            alternative_factors = {}

        db_context = {"news": [], "notices": [], "research": []}
        try:
            db_context, db_source_chain = await load_db_first_document_context(
                db,
                code,
                start_date=start_date,
                end_date=end_date,
                news_limit=max(5, int(headline_limit)),
                notice_limit=max(3, headline_limit // 2),
                research_limit=max(5, int(headline_limit)),
            )
            source_chain.extend(db_source_chain)
        except Exception:
            db_context = {"news": [], "notices": [], "research": []}

        news_items = list(db_context.get("news") or [])
        try:
            if not news_items:
                news_items = _extract_news_items(get_stock_news(code, limit=max(5, int(headline_limit))))
                if news_items:
                    source_chain.append("tools.news.get_stock_news")
        except Exception:
            news_items = []

        notice_items = list(db_context.get("notices") or [])
        try:
            if not notice_items:
                notice_items = _extract_news_items(
                    get_stock_notices(
                        start_date=start_date.isoformat(),
                        end_date=end_date.isoformat(),
                        stock_code=code,
                    )
                )
                if notice_items:
                    source_chain.append("tools.news.get_stock_notices")
        except Exception:
            notice_items = []

        research_items = list(db_context.get("research") or [])
        try:
            if not research_items:
                research_items = _extract_news_items(get_research_reports(code, limit=max(5, int(headline_limit))))
                if research_items:
                    source_chain.append("tools.news.get_research_reports")
        except Exception:
            research_items = []

        news_titles = _extract_titles(news_items, limit=headline_limit)
        notice_titles = _extract_titles(notice_items, limit=max(3, headline_limit // 2))
        research_titles = _extract_titles(research_items, limit=max(3, headline_limit // 2))

        fund_flow_data = {}
        try:
            fund_flow_data, flow_source_chain = await load_db_first_stock_fund_flow(db, code)
            source_chain.extend(flow_source_chain)
        except Exception:
            fund_flow_data = {}

        try:
            if not fund_flow_data:
                fund_flow_payload = get_stock_fund_flow(code)
                if (
                    isinstance(fund_flow_payload, dict)
                    and fund_flow_payload.get("success")
                    and isinstance(fund_flow_payload.get("data"), dict)
                ):
                    fund_flow_data = dict(fund_flow_payload.get("data") or {})
                    source_chain.append("tools.fund_flow.get_stock_fund_flow")
        except Exception:
            fund_flow_data = fund_flow_data or {}

        summary_rows.append(
            {
                "code": code,
                "market": _summarize_klines(klines if isinstance(klines, list) else []),
                "financials": {
                    "roe": round(float(_safe_float(financial_snapshot.get("roe"), 0.0)), 4),
                    "roa": round(float(_safe_float(financial_snapshot.get("roa"), 0.0)), 4),
                    "gross_margin": round(float(_safe_float(financial_snapshot.get("gross_margin"), 0.0)), 4),
                    "debt_ratio": round(float(_safe_float(financial_snapshot.get("debt_ratio"), 0.0)), 4),
                    "revenue_growth": round(float(_safe_float(financial_snapshot.get("revenue_growth"), 0.0)), 4),
                    "profit_growth": round(float(_safe_float(financial_snapshot.get("profit_growth"), 0.0)), 4),
                },
                "valuation": {
                    "pe_ratio": round(float(_safe_float(valuation_snapshot.get("pe_ratio"), 0.0)), 4),
                    "pb_ratio": round(float(_safe_float(valuation_snapshot.get("pb_ratio"), 0.0)), 4),
                    "price": round(float(_safe_float(valuation_snapshot.get("price"), 0.0)), 4),
                },
                "alternative_factors": {
                    "news_sentiment_score": round(float(_safe_float((alternative_factors.get("sentiment") or {}).get("score_raw"), 0.0)), 6),
                    "event_score": round(float(_safe_float((alternative_factors.get("event") or {}).get("score_raw"), 0.0)), 6),
                    "capital_flow_score": round(float(_safe_float((alternative_factors.get("capital_flow") or {}).get("score_raw"), 0.0)), 6),
                    "alternative_composite_score": round(float(_safe_float((alternative_factors.get("alternative_composite") or {}).get("score_raw"), 0.0)), 6),
                },
                "recent_headlines": {
                    "news": news_titles,
                    "notices": notice_titles,
                    "research": research_titles,
                },
                "fund_flow": dict(fund_flow_data or {}),
            }
        )

    request_rows = []
    for row in summary_rows:
        request_rows.append(
            {
                "code": row.get("code"),
                "market": row.get("market"),
                "non_dsl_context": {
                    "financials": row.get("financials"),
                    "valuation": row.get("valuation"),
                    "alternative_factors": row.get("alternative_factors"),
                    "recent_headlines": row.get("recent_headlines"),
                    "fund_flow": row.get("fund_flow"),
                },
            }
        )

    request_payload = {
        "task": "factor_candidate_generation",
        "candidate_count": max(1, min(int(candidate_count), 16)),
        "codes": normalized_codes[:8],
        "allowed_operators": list(_ALLOWED_OPERATORS),
        "field_hints": list(_DEFAULT_FIELD_HINTS),
        "dsl_contract": {
            "supported_fields": list(_DEFAULT_FIELD_HINTS),
            "supported_functions": list(_ALLOWED_OPERATORS),
            "evaluation_semantics": [
                "expression_dsl is evaluated on a single-stock daily time-series frame",
                "rank/zscore/ts_* are time-series operators over one stock history, not cross-sectional operators",
                "inputs and expression_dsl may reference only field_hints and allowed_operators",
                "non_dsl_context is for hypothesis writing only and must never appear in inputs or expression_dsl",
            ],
            "valid_examples": [
                "momentum_20d",
                "zscore(momentum_20d, 20) - zscore(volatility_20d, 20)",
                "rank(delta(close, 5)) + zscore(volume_ratio_5_20, 10)",
            ],
            "strict_admission_policy": {
                "minimum_evidence": {
                    "sample_dates": 60,
                    "avg_cross_section_n": 80,
                    "ic_history_rows": 60,
                    "abs_rank_ic_mean": 0.025,
                    "rank_ic_ir": 0.25,
                    "positive_ratio": 0.52,
                },
                "governance": [
                    "design candidates to survive multiple-testing checks, not only quick IC screening",
                    "avoid small variations of common basis factors when they are likely to show high PBO",
                    "prefer economically distinct interactions with stable OOS behavior and low reality-check risk",
                    "do not repeat memory_context failed patterns, especially PBO high or weak reality-check failures",
                ],
            },
        },
        "context_rows": request_rows,
        "output_contract": {
            "root_fields": ["candidates", "analysis", "warnings"],
            "candidate_fields": [
                "name",
                "hypothesis",
                "family",
                "inputs",
                "expression_dsl",
                "expected_holding_period",
                "expected_regime",
                "complexity_hint",
                "novelty_rationale",
            ],
        },
    }
    if isinstance(memory_context, dict) and memory_context:
        request_payload["memory_context"] = memory_context

    source_chain = list(dict.fromkeys(str(item) for item in source_chain if str(item).strip()))

    system_prompt = (
        "You are a quantitative equity factor researcher for A-share markets. "
        "Generate original factor candidates from the provided context. "
        "Return only JSON with root fields candidates, analysis, warnings. "
        "Each candidate must contain: name, hypothesis, family, inputs, expression_dsl, "
        "expected_holding_period, expected_regime, complexity_hint, novelty_rationale. "
        "Do not output Python, SQL, markdown, or prose outside JSON. "
        "Use only human-readable DSL expressions and the allowed operators provided in the context. "
        "expression_dsl runs on a single-stock daily time-series frame, so do not write cross-sectional rank/zscore semantics. "
        "Use only fields listed in field_hints. Never reference any key nested under non_dsl_context inside inputs or expression_dsl. "
        "If memory_context is provided, learn from past successes, avoid repeating failures, "
        "and avoid generating duplicates of highly similar historical candidates. "
        "The formal gate requires strict cross-section IC evidence plus low multiple-testing risk; "
        "quick IC alone is not enough."
    )
    user_prompt = (
        "Research context:\n"
        f"{json.dumps(request_payload, ensure_ascii=False, indent=2, default=str)}\n\n"
        "Generate the best candidate factors for near-term research iteration. "
        "Prefer candidates that are testable, not redundant, and grounded in the supplied fields. "
        "Favor expressions that can plausibly pass PBO, White Reality Check, and Hansen SPA governance, "
        "instead of close variants of common momentum, reversal, volatility, or liquidity basis factors. "
        "Before finalizing, self-check that every inputs item appears in field_hints and every expression_dsl can be executed by the stated DSL contract."
    )
    context_summary = {
        "codes": normalized_codes[:8],
        "candidate_count": max(1, min(int(candidate_count), 16)),
        "rows": summary_rows,
        "memory_context": memory_context if isinstance(memory_context, dict) and memory_context else {},
    }

    return FactorMiningPrompt(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        context_summary=context_summary,
        request_payload=request_payload,
        source_chain=source_chain,
        schema_path=schema_path or get_factor_candidate_schema_path(),
    )
