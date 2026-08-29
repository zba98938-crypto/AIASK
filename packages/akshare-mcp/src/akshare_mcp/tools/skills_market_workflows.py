"""Extracted market-oriented skill workflows."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from ..utils import normalize_code
from . import skills_support as skill_support


def _skill_support():
    return skill_support


async def exec_market(params: Dict[str, Any]) -> Dict[str, Any]:
    from .market.kline import get_kline, get_kline_data, get_minute_kline
    from .market.order_book import get_order_book
    from .market.quote import get_realtime_quote

    skill_support = _skill_support()

    task = str(params.get("task") or "smoke_test").strip().lower()
    code = normalize_code(str(params.get("code") or "600519"))

    daily_limit = int(params.get("daily_limit", 30) or 30)
    minute_limit = int(params.get("minute_limit", 30) or 30)
    minute_period = str(params.get("minute_period") or "5m")
    start_date = params.get("start_date")
    end_date = params.get("end_date")

    steps: List[Dict[str, Any]] = []
    if task in {"smoke_test", "quick_scan"}:
        steps.append(skill_support._run_step("get_realtime_quote", get_realtime_quote, stock_code=code))
        if start_date or end_date:
            steps.append(
                await skill_support._run_step_async(
                    "get_kline_data",
                    get_kline_data,
                    code=code,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    limit=daily_limit,
                )
            )
        else:
            steps.append(
                await skill_support._run_step_async(
                    "get_kline",
                    get_kline,
                    stock_code=code,
                    period="daily",
                    limit=daily_limit,
                )
            )
        steps.append(
            skill_support._run_step(
                "get_minute_kline",
                get_minute_kline,
                stock_code=code,
                period=minute_period,
                limit=minute_limit,
            )
        )
        steps.append(skill_support._run_step("get_order_book", get_order_book, stock_code=code))
        return skill_support._finalize_skill_result(task, steps)

    if task in {"quote_only", "quote"}:
        steps.append(skill_support._run_step("get_realtime_quote", get_realtime_quote, stock_code=code))
        return skill_support._finalize_skill_result(task, steps)

    return {
        "task": task,
        "status": "unsupported_task",
        "steps": [],
        "summary": {
            "total_steps": 0,
            "failed_steps": [],
            "supported_tasks": ["smoke_test", "quick_scan", "quote_only"],
        },
    }


async def exec_fund_news(params: Dict[str, Any]) -> Dict[str, Any]:
    from .news import (
        get_analyst_ranking,
        get_market_news,
        get_stock_news,
        get_stock_notices,
        get_stock_research,
        search_research,
    )

    skill_support = _skill_support()

    task = str(params.get("task") or "news_digest").strip().lower()
    supported_tasks = ["news_digest", "research_digest", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    code = normalize_code(str(params.get("code") or "600519"))
    keyword = str(params.get("keyword") or code).strip()
    start_date, end_date = skill_support._default_notice_window(params)
    news_limit = max(1, skill_support._safe_int(params.get("news_limit"), 5))
    research_limit = max(1, skill_support._safe_int(params.get("research_limit"), 5))
    market_news_limit = max(1, skill_support._safe_int(params.get("market_news_limit"), 5))

    async def _stock_news():
        return await asyncio.to_thread(get_stock_news, stock_code=code, limit=news_limit)

    async def _market_news():
        return await asyncio.to_thread(get_market_news, limit=market_news_limit)

    async def _stock_notices():
        return await asyncio.to_thread(
            get_stock_notices,
            start_date=start_date,
            end_date=end_date,
            stock_code=code,
        )

    async def _stock_research():
        return await asyncio.to_thread(get_stock_research, stock_code=code, limit=research_limit)

    async def _search_research():
        return await asyncio.to_thread(
            search_research,
            keyword=keyword,
            stock_code=code,
            days=skill_support._safe_int(params.get("days"), 30),
        )

    async def _analyst_ranking():
        return await asyncio.to_thread(get_analyst_ranking, year=str(params.get("year") or ""))

    steps: List[Dict[str, Any]] = []
    if task in {"news_digest", "smoke_test"}:
        steps.append(await skill_support._run_step_async("get_stock_news", _stock_news))
        steps.append(await skill_support._run_step_async("get_stock_notices", _stock_notices))
        steps.append(await skill_support._run_step_async("get_market_news", _market_news))
    else:
        steps.append(await skill_support._run_step_async("get_stock_research", _stock_research))
        steps.append(await skill_support._run_step_async("search_research", _search_research))
        steps.append(await skill_support._run_step_async("get_analyst_ranking", _analyst_ranking))

    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["code"] = code
    result["summary"]["window"] = {"start_date": start_date, "end_date": end_date}
    return result


async def exec_fundamental(params: Dict[str, Any]) -> Dict[str, Any]:
    from .finance import get_financials, get_stock_info

    skill_support = _skill_support()

    task = str(params.get("task") or "fundamental_snapshot").strip().lower()
    supported_tasks = ["fundamental_snapshot", "financials_only", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    code = normalize_code(str(params.get("code") or "600519"))
    steps: List[Dict[str, Any]] = []
    if task in {"fundamental_snapshot", "smoke_test"}:
        steps.append(skill_support._run_step("get_stock_info", get_stock_info, stock_code=code))
    steps.append(await skill_support._run_step_async("get_financials", get_financials, stock_code=code))

    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["code"] = code
    return result


async def exec_macro_options_alerts(params: Dict[str, Any]) -> Dict[str, Any]:
    from .macro import get_macro_indicator
    from .options import get_option_chain

    skill_support = _skill_support()

    task = str(params.get("task") or "macro_options_brief").strip().lower()
    supported_tasks = ["macro_options_brief", "alert_blueprint", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    indicator = str(params.get("indicator") or "cpi").strip().lower()
    limit = max(1, skill_support._safe_int(params.get("limit"), 12))
    underlying = str(params.get("underlying") or "510050").strip()
    expiry_month = str(params.get("expiry_month") or "").strip()
    threshold = skill_support._safe_float(params.get("threshold"), 0.0)
    steps: List[Dict[str, Any]] = []

    async def _macro():
        return await asyncio.to_thread(get_macro_indicator, indicator=indicator, limit=limit)

    async def _options():
        return await asyncio.to_thread(
            get_option_chain,
            underlying=underlying,
            expiry_month=expiry_month,
            limit=max(20, limit * 10),
        )

    steps.append(await skill_support._run_step_async("get_macro_indicator", _macro))
    steps.append(await skill_support._run_step_async("get_option_chain", _options))
    steps.append(
        skill_support._static_step(
            "build_alert_blueprint",
            {
                "alert_name": str(params.get("alert_name") or f"{indicator}_{underlying}_monitor"),
                "threshold": threshold,
                "conditions": [
                    f"{indicator} change crosses {threshold}"
                    if threshold
                    else f"{indicator} surprises relative to prior print",
                    "Option open interest or implied skew changes materially",
                    "Escalate when macro direction and option positioning diverge",
                ],
            },
        )
    )
    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["indicator"] = indicator
    result["summary"]["underlying"] = underlying
    return result
