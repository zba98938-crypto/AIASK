"""Insight manager: investment insights and report artifact generation."""

from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..manager_protocol import fail_with_meta, normalize_manager_kwargs, normalize_manager_payload, ok_with_meta


_PLACEHOLDER_PATTERN = re.compile(r"\{([a-zA-Z0-9_]+)\}")
_TEMPLATE_FILES = {
    "daily": "daily_report_template.md",
    "weekly": "weekly_report_template.md",
    "monthly": "monthly_report_template.md",
}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _is_truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _pct_obj(value: float, window: str, basis: str) -> dict:
    return {
        "value": round(value * 100.0, 2),
        "unit": "%",
        "precision": "0.01",
        "window": window,
        "basis": basis,
    }


def _compute_risk_metrics(returns: list[float], window: str) -> dict:
    """基于真实收益序列计算核心风险指标（非占位符）。"""
    if not returns:
        return {
            "max_drawdown": {
                "value": None,
                "unit": "%",
                "precision": "0.01",
                "window": window,
                "reason": "缺少可计算收益序列",
            },
            "volatility": {
                "value": None,
                "unit": "%/年化",
                "precision": "0.01",
                "window": window,
                "reason": "缺少可计算收益序列",
            },
            "var": {
                "value": None,
                "unit": "%",
                "precision": "0.01",
                "window": window,
                "reason": "缺少可计算收益序列",
            },
            "cvar": {
                "value": None,
                "unit": "%",
                "precision": "0.01",
                "window": window,
                "reason": "缺少可计算收益序列",
            },
        }

    # 最大回撤
    nav = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        nav *= (1.0 + r)
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # 年化波动（252交易日）
    vol_ann = 0.0
    if len(returns) >= 2:
        try:
            vol_ann = statistics.pstdev(returns) * (252 ** 0.5)
        except Exception:
            vol_ann = 0.0

    sorted_rets = sorted(returns)
    n = len(sorted_rets)
    q_idx = max(0, int(0.05 * n) - 1)
    q05 = sorted_rets[q_idx]
    var95 = max(0.0, -q05)
    tail = [x for x in sorted_rets if x <= q05]
    cvar95 = max(0.0, -(sum(tail) / len(tail))) if tail else var95

    return {
        "max_drawdown": {
            "value": round(max_dd * 100.0, 2),
            "unit": "%",
            "precision": "0.01",
            "window": window,
            "method": "historical",
        },
        "volatility": {
            "value": round(vol_ann * 100.0, 2),
            "unit": "%/年化",
            "precision": "0.01",
            "window": window,
            "method": "std * sqrt(252)",
        },
        "var": {
            "value": round(var95 * 100.0, 2),
            "unit": "%",
            "precision": "0.01",
            "window": window,
            "confidence": "95%",
            "method": "historical",
        },
        "cvar": {
            "value": round(cvar95 * 100.0, 2),
            "unit": "%",
            "precision": "0.01",
            "window": window,
            "confidence": "95%",
            "method": "historical",
        },
    }


def _extract_codes_and_weights(kwargs: dict) -> tuple[list[str], list[float], str]:
    holdings = kwargs.get("holdings")
    if isinstance(holdings, str):
        try:
            holdings = json.loads(holdings)
        except Exception:
            holdings = None
    if isinstance(holdings, list) and holdings:
        codes = []
        ws = []
        for h in holdings:
            if not isinstance(h, dict):
                continue
            code = str(h.get("code") or "").strip()
            if not code:
                continue
            w = _safe_float(h.get("weight"), 0.0)
            codes.append(code)
            ws.append(w)
        if codes:
            s = sum(ws)
            if s <= 0:
                ws = [1.0 / len(codes)] * len(codes)
            else:
                ws = [x / s for x in ws]
            return codes, ws, "holdings_weighted"

    codes = kwargs.get("codes") or kwargs.get("symbols") or []
    if isinstance(codes, str):
        try:
            codes = json.loads(codes)
        except Exception:
            codes = [x.strip() for x in codes.split(",") if x.strip()]
    if isinstance(codes, list):
        codes = [str(x).strip() for x in codes if str(x).strip()]
    else:
        codes = []
    if codes:
        return codes[:10], [1.0 / min(len(codes), 10)] * min(len(codes), 10), "equal_weight_input_codes"

    # 无持仓输入时，用沪深300代理，避免占位符
    return ["000300"], [1.0], "proxy_benchmark"


async def _series_returns_from_kline(code: str, limit: int = 21) -> list[float]:
    try:
        from ..market.kline import get_kline_data

        res = await get_kline_data(code=code, period="daily", start_date="", end_date="", limit=limit, adjust="")
        if not res.get("success") or not isinstance(res.get("data"), list):
            return []
        data = sorted(res["data"], key=lambda x: str(x.get("date", "")))
        closes = [_safe_float(x.get("close"), 0.0) for x in data if _safe_float(x.get("close"), 0.0) > 0]
        rets = []
        for i in range(1, len(closes)):
            rets.append((closes[i] / closes[i - 1]) - 1.0)
        return rets[-20:]
    except Exception:
        return []


async def _portfolio_returns(codes: list[str], weights: list[float]) -> list[float]:
    series_list = []
    valid_weights = []
    for c, w in zip(codes, weights):
        rets = await _series_returns_from_kline(c, limit=25)
        if rets:
            series_list.append(rets)
            valid_weights.append(max(0.0, float(w)))
    if not series_list:
        return []
    m = min(len(s) for s in series_list)
    if m <= 0:
        return []
    sw = sum(valid_weights)
    if sw <= 0:
        valid_weights = [1.0 / len(series_list)] * len(series_list)
    else:
        valid_weights = [x / sw for x in valid_weights]
    out = []
    for i in range(-m, 0):
        out.append(sum(valid_weights[j] * series_list[j][i] for j in range(len(series_list))))
    return out


async def _enrich_daily_kwargs(kwargs: dict) -> dict:
    """为日报自动补全关键字段，避免 '-' 占位符。"""
    report_date = kwargs.get("report_date") or datetime.now().strftime("%Y-%m-%d")
    data_window = kwargs.get("data_window", "T-20D ~ T")

    # 1) 先取语义日报聚合结果（已含优先级：SQLite -> Tushare -> AkShare）
    market_summary = {}
    stats = {}
    capital_flow = {}
    sentiment = kwargs.get("market_sentiment", "neutral")
    highlights = []
    hot_sectors = []
    try:
        from ..semantic.daily_report import generate_daily_report as _gen_daily

        daily_res = await _gen_daily(date=report_date)
        if daily_res.get("success") and isinstance(daily_res.get("data"), dict):
            d = daily_res["data"]
            market_summary = d.get("market_summary") or {}
            stats = d.get("stats") or {}
            capital_flow = d.get("capital_flow") or {}
            sentiment = d.get("sentiment") or sentiment
            highlights = d.get("highlights") or []
            hot_sectors = d.get("hot_sectors") or []
    except Exception:
        pass

    # 2) 指数摘要
    if market_summary:
        parts = []
        for code, item in market_summary.items():
            name = item.get("name") or code
            close_v = _safe_float(item.get("close"), 0.0)
            chg = _safe_float(item.get("change_pct"), 0.0)
            parts.append(f"{name}({code}) {close_v:.2f}点 / {chg:+.2f}%")
        index_summary = f"{' ; '.join(parts)}（窗口: T-1D ~ T, 单位: 点/%）"
    else:
        index_summary = "指数数据暂不可用（原因: 当前数据源未返回有效行情）"

    # 3) 资金与事件
    event_text = "无重大公开事件"
    try:
        from ..news.news_feed import get_market_news

        news_res = get_market_news(limit=3)
        if news_res.get("success") and isinstance(news_res.get("data"), list) and news_res["data"]:
            first = news_res["data"][0]
            event_text = str(first.get("title") or first.get("summary") or event_text)
    except Exception:
        pass

    north = (capital_flow.get("north_fund") or {}) if isinstance(capital_flow, dict) else {}
    capital_and_events = {
        "north_fund": {
            "net_inflow": _safe_float(north.get("net_inflow"), 0.0),
            "sh_connect": _safe_float(north.get("sh_connect"), 0.0),
            "sz_connect": _safe_float(north.get("sz_connect"), 0.0),
            "unit": "亿元(数据源口径)",
            "precision": "0.01",
            "window": "T-1D ~ T",
        },
        "major_event": event_text,
    }

    # 4) 组合收益、基准、贡献
    codes, weights, basis = _extract_codes_and_weights(kwargs)
    port_rets = await _portfolio_returns(codes, weights)
    bench_rets = await _portfolio_returns(["000300"], [1.0])

    last_port = port_rets[-1] if port_rets else (bench_rets[-1] if bench_rets else 0.0)
    last_bench = bench_rets[-1] if bench_rets else 0.0
    daily_return = _pct_obj(last_port, "T-1D ~ T", basis)
    vs_benchmark = {
        "value": round((last_port - last_bench) * 100.0, 2),
        "unit": "%",
        "precision": "0.01",
        "window": "T-1D ~ T",
        "benchmark": "000300",
    }

    contrib_payload = {"top_contributors": [], "top_detractors": [], "unit": "%", "precision": "0.01", "window": "T-1D ~ T"}
    try:
        from ..market.quote import get_batch_quotes

        q = get_batch_quotes(codes[:10]) if codes else {"success": False}
        quotes = ((q.get("data") or {}).get("quotes") or []) if q.get("success") else []
        rows = []
        for i, item in enumerate(quotes):
            cp = _safe_float(item.get("changePercent"), 0.0)
            w = weights[i] if i < len(weights) else (1.0 / max(1, len(quotes)))
            rows.append(
                {
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "change_pct": round(cp, 2),
                    "contribution_pct": round(cp * w, 2),
                }
            )
        if rows:
            rows_sorted = sorted(rows, key=lambda x: x["contribution_pct"], reverse=True)
            contrib_payload["top_contributors"] = rows_sorted[:3]
            contrib_payload["top_detractors"] = list(reversed(rows_sorted[-3:]))
    except Exception:
        pass
    if not contrib_payload["top_contributors"] and market_summary:
        proxy_rows = []
        for code, item in market_summary.items():
            proxy_rows.append({
                "code": code,
                "name": item.get("name", code),
                "change_pct": round(_safe_float(item.get("change_pct"), 0.0), 2),
                "contribution_pct": round(_safe_float(item.get("change_pct"), 0.0) / max(1, len(market_summary)), 2),
            })
        proxy_rows = sorted(proxy_rows, key=lambda x: x["contribution_pct"], reverse=True)
        contrib_payload["top_contributors"] = proxy_rows[:2]
        contrib_payload["top_detractors"] = list(reversed(proxy_rows[-2:]))
        contrib_payload["basis"] = "index_proxy_equal_weight"

    # 5) 风险指标
    risk_returns = port_rets if port_rets else bench_rets
    risk_basis = basis if port_rets else "proxy_benchmark_000300"
    risk_metrics = _compute_risk_metrics(risk_returns, data_window)
    risk_metrics["basis"] = risk_basis

    # 6) 告警
    alerts = []
    if _safe_float(vs_benchmark.get("value"), 0.0) <= -1.5:
        alerts.append("相对基准显著跑输告警（阈值: -1.5%）")
    if _safe_float((risk_metrics.get("max_drawdown") or {}).get("value"), 0.0) >= 8.0:
        alerts.append("回撤风险告警（阈值: Max Drawdown >= 8%）")
    if isinstance(stats, dict):
        lu = int(_safe_float(stats.get("limit_up_count"), 0))
        ld = int(_safe_float(stats.get("limit_down_count"), 0))
        if ld > max(10, lu * 1.2):
            alerts.append("市场广谱风险告警（跌停显著高于涨停）")
    if not alerts:
        alerts.append("无触发告警（规则: 跑输<-1.5%、回撤>=8%、跌停显著高于涨停）")

    # 7) 执行摘要
    if isinstance(kwargs.get("execution_summary"), dict):
        execution_summary = kwargs["execution_summary"]
    else:
        execution_summary = {
            "orders": kwargs.get("orders", 0),
            "fills": kwargs.get("fills", 0),
            "fill_rate": kwargs.get("fill_rate", "N/A"),
            "slippage_bps": kwargs.get("slippage_bps", "N/A"),
            "window": "T-1D ~ T",
            "note": "若需精确执行统计，请传入 orders/fills/fill_rate/slippage_bps 或接入 execution_manager.summary",
        }

    # 8) 次日观察清单
    watchlist_items = []
    if hot_sectors:
        for sec in hot_sectors[:3]:
            watchlist_items.append(f"观察板块延续性: {sec.get('name', '')}（涨幅{_safe_float(sec.get('change_pct'), 0.0):.2f}%）")
    for item in contrib_payload.get("top_detractors", [])[:2]:
        watchlist_items.append(f"跟踪拖累修复: {item.get('name') or item.get('code')}（贡献{_safe_float(item.get('contribution_pct'), 0.0):.2f}%）")
    if not watchlist_items:
        watchlist_items = ["暂无高置信度关注标的（原因: 输入持仓/板块数据不足）"]

    kwargs.update(
        {
            "report_date": report_date,
            "data_window": data_window,
            "index_summary": index_summary,
            "market_sentiment": sentiment,
            "capital_and_events": capital_and_events,
            "daily_return": daily_return,
            "vs_benchmark": vs_benchmark,
            "contributors_and_detractors": contrib_payload,
            "core_risk_metrics": risk_metrics,
            "risk_exposure": risk_metrics,
            "daily_alerts": alerts,
            "alert_status": {
                "count": len(alerts),
                "items": alerts,
                "window": "T-1D ~ T",
            },
            "execution_summary": execution_summary,
            "watchlist_next_day": {
                "items": watchlist_items,
                "window": "T+1",
            },
            "watchlist": watchlist_items,
            "exceptions": kwargs.get("exceptions", []),
            "next_actions": kwargs.get("next_actions", watchlist_items[:3]),
            "data_limitations": kwargs.get(
                "data_limitations",
                "若个别字段为空，原因为数据源当日无可用记录；已按 SQLite -> Tushare -> AkShare 降级",
            ),
            "highlights": highlights,
        }
    )
    return kwargs


def _normalize_kwargs(kwargs: dict) -> dict:
    return normalize_manager_kwargs(kwargs)


def _dedupe_chain(values: list[str]) -> list[str]:
    chain = []
    seen = set()
    for value in values:
        label = str(value or "").strip()
        if not label or label in seen:
            continue
        chain.append(label)
        seen.add(label)
    return chain


def _repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".codex" / "skills" / "akshare-fund-manager-pro" / "assets" / "templates").exists():
            return parent
    return Path.cwd()


def _template_path(report_type: str) -> Path:
    root = _repo_root()
    file_name = _TEMPLATE_FILES.get(report_type, _TEMPLATE_FILES["daily"])
    return root / ".codex" / "skills" / "akshare-fund-manager-pro" / "assets" / "templates" / file_name


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _render_template(template_text: str, payload: dict) -> str:
    placeholders = set(_PLACEHOLDER_PATTERN.findall(template_text))
    merged = {k: _stringify(v) for k, v in payload.items()}
    for name in placeholders:
        merged.setdefault(name, "N/A（原因: 模板字段未提供）")

    output = template_text
    for key, value in merged.items():
        output = output.replace(f"{{{key}}}", value)
    return output


def _build_payload(report_type: str, kwargs: dict) -> dict:
    now = datetime.now()

    payload_raw = kwargs.get("payload", {})
    if isinstance(payload_raw, str):
        try:
            payload_raw = json.loads(payload_raw or "{}")
        except Exception:
            payload_raw = {}
    if not isinstance(payload_raw, dict):
        payload_raw = {}

    generic = {
        "report_type": report_type,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "data_window": kwargs.get("data_window", "T-20D ~ T"),
        "trading_calendar_basis": kwargs.get("trading_calendar_basis", "SSE/SZSE trading dates"),
        "data_sources": kwargs.get("data_sources", ["SQLite", "Tushare", "AkShare"]),
        "fallback_chain": kwargs.get("fallback_chain", "SQLite -> Tushare -> AkShare"),
        "core_risk_metrics": kwargs.get(
            "core_risk_metrics",
            {
                "max_drawdown": {"value": None, "unit": "%", "precision": "0.01", "reason": "未提供可计算序列"},
                "volatility": {"value": None, "unit": "%/年化", "precision": "0.01", "reason": "未提供可计算序列"},
                "var": {"value": None, "unit": "%", "precision": "0.01", "reason": "未提供可计算序列"},
                "cvar": {"value": None, "unit": "%", "precision": "0.01", "reason": "未提供可计算序列"},
            },
        ),
        "key_exceptions": kwargs.get("key_exceptions", []),
        "next_actions": kwargs.get("next_actions", []),
        "trigger_conditions": kwargs.get("trigger_conditions", []),
    }

    # Template field compatibility
    if report_type == "daily":
        generic.update(
            {
                "report_date": kwargs.get("report_date", now.strftime("%Y-%m-%d")),
                "owner": kwargs.get("owner", "default"),
                "index_summary": kwargs.get("index_summary", "指数数据暂不可用（原因: 未提供）"),
                "market_sentiment": kwargs.get("market_sentiment", "neutral"),
                "capital_and_events": kwargs.get("capital_and_events", {"reason": "未提供"}),
                "daily_return": kwargs.get("daily_return", {"value": None, "unit": "%", "precision": "0.01", "reason": "未提供"}),
                "vs_benchmark": kwargs.get("vs_benchmark", {"value": None, "unit": "%", "precision": "0.01", "reason": "未提供"}),
                "contributors_and_detractors": kwargs.get("contributors_and_detractors", {"reason": "未提供"}),
                "risk_exposure": kwargs.get("risk_exposure", generic["core_risk_metrics"]),
                "daily_alerts": kwargs.get("daily_alerts", ["未触发告警（默认）"]),
                "alert_status": kwargs.get("alert_status", {"count": 0, "items": ["未触发告警（默认）"]}),
                "exceptions": kwargs.get("exceptions", generic["key_exceptions"]),
                "execution_summary": kwargs.get("execution_summary", {"reason": "未提供执行数据"}),
                "watchlist_next_day": kwargs.get("watchlist_next_day", {"items": ["暂无关注标的（原因: 未提供）"]}),
                "watchlist": kwargs.get("watchlist", ["暂无关注标的（原因: 未提供）"]),
                "data_limitations": kwargs.get("data_limitations", generic["fallback_chain"]),
            }
        )
    elif report_type == "weekly":
        generic.update(
            {
                "week_range": kwargs.get("week_range", now.strftime("%Y-W%W")),
                "owner": kwargs.get("owner", "default"),
                "strategy_version": kwargs.get("strategy_version", "v1"),
                "weekly_return": kwargs.get("weekly_return", "-"),
                "benchmark_return": kwargs.get("benchmark_return", "-"),
                "excess_return": kwargs.get("excess_return", "-"),
                "vol_drawdown": kwargs.get("vol_drawdown", generic["core_risk_metrics"]),
                "exposure_change": kwargs.get("exposure_change", "-"),
                "factor_attribution": kwargs.get("factor_attribution", "-"),
                "stock_attribution": kwargs.get("stock_attribution", "-"),
                "trade_overview": kwargs.get("trade_overview", "-"),
                "slippage_cost": kwargs.get("slippage_cost", "-"),
                "weight_drift": kwargs.get("weight_drift", "-"),
                "risk_metrics": kwargs.get("risk_metrics", generic["core_risk_metrics"]),
                "alert_closure": kwargs.get("alert_closure", "-"),
                "key_risks": kwargs.get("key_risks", generic["key_exceptions"]),
                "rebalance_plan": kwargs.get("rebalance_plan", generic["next_actions"]),
                "focus_symbols": kwargs.get("focus_symbols", "-"),
                "risk_param_changes": kwargs.get("risk_param_changes", "-"),
                "data_limitations": kwargs.get("data_limitations", generic["fallback_chain"]),
            }
        )
    else:
        generic.update(
            {
                "month_range": kwargs.get("month_range", now.strftime("%Y-%m")),
                "owner": kwargs.get("owner", "default"),
                "portfolio_name": kwargs.get("portfolio_name", "default"),
                "monthly_return": kwargs.get("monthly_return", "-"),
                "ytd_return": kwargs.get("ytd_return", "-"),
                "benchmark_comparison": kwargs.get("benchmark_comparison", "-"),
                "nav_summary": kwargs.get("nav_summary", "-"),
                "industry_attribution": kwargs.get("industry_attribution", "-"),
                "factor_attribution": kwargs.get("factor_attribution", "-"),
                "stock_attribution": kwargs.get("stock_attribution", "-"),
                "trade_attribution": kwargs.get("trade_attribution", "-"),
                "core_risk_metrics": kwargs.get("core_risk_metrics", generic["core_risk_metrics"]),
                "stress_test_summary": kwargs.get("stress_test_summary", "-"),
                "extreme_scenarios": kwargs.get("extreme_scenarios", "-"),
                "execution_efficiency": kwargs.get("execution_efficiency", "-"),
                "cost_control": kwargs.get("cost_control", "-"),
                "data_quality_sync": kwargs.get("data_quality_sync", "-"),
                "effective_strategies": kwargs.get("effective_strategies", "-"),
                "ineffective_or_bias": kwargs.get("ineffective_or_bias", generic["key_exceptions"]),
                "process_improvements": kwargs.get("process_improvements", generic["next_actions"]),
                "allocation_plan": kwargs.get("allocation_plan", "-"),
                "position_risk_budget": kwargs.get("position_risk_budget", "-"),
                "watch_alert_plan": kwargs.get("watch_alert_plan", "-"),
                "data_limitations": kwargs.get("data_limitations", generic["fallback_chain"]),
            }
        )

    return {**generic, **payload_raw}


def _write_report_artifacts(
    report_type: str,
    markdown: str,
    payload: dict,
    output_dir: str,
    *,
    include_json: bool = False,
) -> dict:
    base_dir = Path(output_dir)
    if not base_dir.is_absolute():
        base_dir = _repo_root() / output_dir
    base_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{report_type}_report_{stamp}"
    md_path = base_dir / f"{stem}.md"

    md_path.write_text(markdown, encoding="utf-8")
    artifacts = {
        "markdown": str(md_path.as_posix()),
    }
    if include_json:
        json_path = base_dir / f"{stem}.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        artifacts["json"] = str(json_path.as_posix())
    return artifacts


def register_insight_manager(mcp):
    """Register insight manager tool."""

    @mcp.tool()
    async def insight_manager(action: str, params: dict | None = None, kwargs: Any = None):
        """
        Insight manager with unified action + kwargs protocol.

        Actions:
        - help
        - list
        - generate
        - daily_brief
        - generate_report
        """
        start_time = time.perf_counter()
        try:
            kwargs = normalize_manager_payload(params=params, kwargs=kwargs)

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name="insight_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name="insight_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            if action == "help":
                return _ok(
                    {
                        "supported_actions": {
                            "list": "list available operations",
                            "generate": "generate insight by topic or symbol",
                            "daily_brief": "generate daily market brief",
                            "generate_report": "auto-fill daily/weekly/monthly template and persist markdown (json optional)",
                            "help": "show help information",
                        }
                    },
                    source_chain=["insight_manager"],
                )

            if action == "list":
                return _ok(
                    {
                        "actions": [
                            {
                                "action": "generate",
                                "description": "generate market/sector/stock insight",
                                "kwargs": "topic(market|sector), code(optional)",
                            },
                            {
                                "action": "daily_brief",
                                "description": "daily brief summary",
                                "kwargs": "none",
                            },
                            {
                                "action": "generate_report",
                                "description": "produce markdown artifacts from templates; json export is optional",
                                "kwargs": "report_type(daily|weekly|monthly), payload(optional), output_dir(optional), include_json(optional)",
                            },
                        ],
                        "count": 3,
                    },
                    source_chain=["insight_manager"],
                )

            if action == "generate":
                topic = kwargs.get("topic", "market")
                code = kwargs.get("code")
                insight = {
                    "market": {
                        "title": "Market Overview",
                        "content": "Market is in a consolidation regime with selective risk-on signals.",
                        "key_points": [
                            "Major indices are range-bound.",
                            "Leadership is concentrated in growth sectors.",
                            "Liquidity is stable but rotation is fast.",
                        ],
                        "recommendation": "keep balanced positioning and tighten stop rules",
                    },
                    "sector": {
                        "title": "Sector Rotation",
                        "content": "Technology and consumption show stronger relative momentum.",
                        "key_points": [
                            "Momentum clusters in a few sectors.",
                            "Defensive sectors lag short-term.",
                            "Rotation speed remains high.",
                        ],
                        "recommendation": "focus on relative strength with risk budget constraints",
                    },
                }
                return _ok(
                    {
                        "topic": topic,
                        "code": code,
                        "insight": insight.get(topic, insight["market"]),
                        "generated_at": datetime.now().strftime("%Y-%m-%d"),
                    },
                    source_chain=["insight_manager"],
                )

            if action == "daily_brief":
                return _ok(
                    {
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "market_summary": "Indices moved in a narrow range with mild liquidity improvement.",
                        "hot_sectors": ["technology", "consumer", "pharma"],
                        "key_events": ["macro policy watch", "earnings season follow-up"],
                    },
                    source_chain=["insight_manager"],
                )

            if action == "generate_report":
                source_chain = ["insight_manager", "templates.report"]
                report_type = str(kwargs.get("report_type", "daily") or "daily").strip().lower()
                if report_type not in _TEMPLATE_FILES:
                    return _fail("report_type must be one of daily/weekly/monthly", source_chain=source_chain)

                if report_type == "daily":
                    kwargs = await _enrich_daily_kwargs(kwargs)
                    source_chain.extend(
                        [
                            "semantic.daily_report.generate_daily_report",
                            "news.news_feed.get_market_news",
                            "market.kline.get_kline_data",
                            "market.quote.get_batch_quotes",
                        ]
                    )

                tpl_path = _template_path(report_type)
                if not tpl_path.exists():
                    return _fail(f"template not found: {tpl_path.as_posix()}", source_chain=source_chain)

                payload = _build_payload(report_type, kwargs)
                template_text = tpl_path.read_text(encoding="utf-8")
                markdown = _render_template(template_text, payload)

                output_dir = str(kwargs.get("output_dir", "reports") or "reports")
                include_json = _is_truthy(
                    kwargs.get("include_json", kwargs.get("write_json")),
                    default=False,
                )
                artifacts = _write_report_artifacts(
                    report_type,
                    markdown,
                    payload,
                    output_dir,
                    include_json=include_json,
                )
                source_chain.append("filesystem.write_report_artifacts")

                return _ok(
                    {
                        "report_type": report_type,
                        "artifacts": artifacts,
                        "required_fields": {
                            "data_window": payload.get("data_window"),
                            "trading_calendar_basis": payload.get("trading_calendar_basis"),
                            "data_sources": payload.get("data_sources"),
                            "fallback_chain": payload.get("fallback_chain"),
                            "core_risk_metrics": payload.get("core_risk_metrics"),
                            "key_exceptions": payload.get("key_exceptions"),
                            "next_actions": payload.get("next_actions"),
                            "trigger_conditions": payload.get("trigger_conditions"),
                        },
                        "generated_at": payload.get("generated_at"),
                    },
                    source_chain=_dedupe_chain(source_chain),
                )

            return _fail(
                "Unknown action: {0}. Supported: help, list, generate, daily_brief, generate_report".format(action),
                source_chain=["insight_manager"],
            )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="insight_manager",
                action=action,
                started_at=start_time,
                source_chain=["insight_manager"],
            )
