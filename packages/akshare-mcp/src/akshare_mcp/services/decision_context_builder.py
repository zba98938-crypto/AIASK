"""Builders for unified decision stock/user context."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from typing import Any

from ..storage import get_db
from ..tools.finance import get_stock_info
from ..tools.fund_flow import get_north_fund_holding, get_stock_fund_flow
from ..tools.investment_analysis import get_investment_analysis
from ..tools.market.order_book import get_order_book
from ..tools.semantic.diagnosis import _build_evidence
from ..tools.semantic.industry_chain import get_industry_chain
from ..utils import resolve_security_code
from .market_data_access import FALLBACK_DB_ONLY, get_quote_snapshot_response
from .decision_pipeline_shared import build_context_meta, clamp, latest_timestamp, safe_float, safe_int, unique_texts


def _normalize_preferences(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return decoded
        except Exception:
            return {}
    return {}


def _risk_level_to_bucket(risk_level: str | None) -> str:
    value = str(risk_level or "").strip().lower()
    if value in {"aggressive", "high", "high_risk", "积极", "进取"}:
        return "aggressive"
    if value in {"conservative", "low", "low_risk", "稳健", "保守"}:
        return "conservative"
    return "moderate"


def _is_successful_response(resp: Any) -> bool:
    return isinstance(resp, dict) and bool(resp.get("success"))


def _resp_data(resp: Any) -> dict[str, Any]:
    if not _is_successful_response(resp) or not isinstance(resp.get("data"), dict):
        return {}
    return dict(resp.get("data") or {})


def _response_timestamp_candidates(resp: Any, data: dict[str, Any], *data_fields: str) -> list[Any]:
    candidates: list[Any] = []
    if isinstance(resp, dict):
        candidates.extend([resp.get("asof_time"), resp.get("timestamp")])
    if isinstance(data, dict):
        for field in data_fields:
            candidates.append(data.get(field))
    return candidates


_TECHNICAL_NOISE_PATTERNS = (
    'asyncio', 'event loop', 'coroutine', 'traceback', 'poolconnectionholder',
    'connectiondoesnotexist', 'at 0x', 'object at ', 'future:', '<', '>',
)


def _sanitize_warning_message(message: str) -> str:
    """将 Python 异常技术细节替换为简短友好的标签，避免污染业务字段。"""
    msg_lower = message.lower()
    if any(pat in msg_lower for pat in _TECHNICAL_NOISE_PATTERNS):
        # 取冒号前第一段作为简短标识，其余丢弃
        short = message.split(':')[0].strip()
        # 若仍然过长，只保留前 60 字符
        return short[:60] if len(short) > 60 else short
    return message[:120] if len(message) > 120 else message


def _append_issue(
    *,
    warnings: list[str],
    fallback_reasons: list[str],
    label: str,
    response: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    message = str(error or (response or {}).get("error") or "").strip()
    if message:
        sanitized = _sanitize_warning_message(message)
        warnings.append(f"{label}:{sanitized}")
        fallback_reasons.append(f"{label}:{sanitized}")
    else:
        warnings.append(label)


async def _call_sync_tool(tool, *args, timeout_sec: float | None = None) -> dict[str, Any]:
    try:
        runner = asyncio.to_thread(tool, *args)
        if timeout_sec and timeout_sec > 0:
            return await asyncio.wait_for(runner, timeout=timeout_sec)
        return await runner
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": f"timeout>{float(timeout_sec):.1f}s" if timeout_sec else "timeout",
            "data": None,
            "cached": False,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "data": None, "cached": False}


async def _call_async_tool(tool, *args, timeout_sec: float | None = None, **kwargs) -> dict[str, Any]:
    try:
        runner = tool(*args, **kwargs)
        if timeout_sec and timeout_sec > 0:
            return await asyncio.wait_for(runner, timeout=timeout_sec)
        return await runner
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": f"timeout>{float(timeout_sec):.1f}s" if timeout_sec else "timeout",
            "data": None,
            "cached": False,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "data": None, "cached": False}


def _estimate_liquidity_score(amount: float | None, bid_depth: int | None, ask_depth: int | None, spread_pct: float | None) -> float:
    amount_score = 0.0
    if amount is not None:
        amount_score = clamp(math.log10(max(amount, 1.0)) / 10.0, 0.0, 1.0)
    depth_score = 0.0
    if bid_depth is not None and ask_depth is not None:
        depth_score = clamp(math.log10(max(bid_depth + ask_depth, 1.0)) / 6.0, 0.0, 1.0)
    spread_score = 1.0
    if spread_pct is not None:
        spread_score = clamp(1.0 - min(abs(spread_pct), 5.0) / 5.0, 0.0, 1.0)
    return round(clamp(amount_score * 0.5 + depth_score * 0.25 + spread_score * 0.25, 0.0, 1.0), 4)


def _build_market_snapshot(
    quote_data: dict[str, Any],
    order_book_data: dict[str, Any],
    risk_context: dict[str, Any],
) -> dict[str, Any]:
    bids = list(order_book_data.get("bids") or [])
    asks = list(order_book_data.get("asks") or [])
    top_bid = safe_float((bids[0] or {}).get("price")) if bids else None
    top_ask = safe_float((asks[0] or {}).get("price")) if asks else None
    bid_depth = sum(int(item.get("volume") or 0) for item in bids[:5]) if bids else None
    ask_depth = sum(int(item.get("volume") or 0) for item in asks[:5]) if asks else None
    mid_price = None
    if top_bid is not None and top_ask is not None:
        mid_price = (top_bid + top_ask) / 2.0
    spread_pct = None
    if mid_price and mid_price > 0 and top_bid is not None and top_ask is not None:
        spread_pct = round(((top_ask - top_bid) / mid_price) * 100.0, 4)

    price = safe_float(quote_data.get("price"))
    amount = safe_float(quote_data.get("amount"))
    volume = safe_int(quote_data.get("volume"))
    change_pct = safe_float(quote_data.get("changePercent"))
    volatility_20d = safe_float(risk_context.get("volatility_20d"))
    suspended = bool(price in (None, 0.0) and volume in (None, 0))
    extreme_volatility = bool(
        (change_pct is not None and abs(change_pct) >= 9.5)
        or (volatility_20d is not None and volatility_20d >= 0.08)
    )

    return {
        "price": price,
        "change_pct": change_pct,
        "volume": volume,
        "amount": amount,
        "open": safe_float(quote_data.get("open")),
        "high": safe_float(quote_data.get("high")),
        "low": safe_float(quote_data.get("low")),
        "pre_close": safe_float(quote_data.get("preClose")),
        "top_bid": top_bid,
        "top_ask": top_ask,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "spread_pct": spread_pct,
        "liquidity_score": _estimate_liquidity_score(amount, bid_depth, ask_depth, spread_pct),
        "suspended": suspended,
        "extreme_volatility": extreme_volatility,
    }


def _flow_bias(main_inflow: float | None, north_ratio: float | None) -> str:
    if (main_inflow or 0.0) > 0 and (north_ratio or 0.0) > 0:
        return "bullish"
    if (main_inflow or 0.0) < 0 and (north_ratio or 0.0) <= 0:
        return "bearish"
    return "neutral"


def _build_fund_flow_snapshot(
    fund_flow_data: dict[str, Any],
    north_holding_data: dict[str, Any],
) -> dict[str, Any]:
    main_inflow = safe_float(fund_flow_data.get("mainNetInflow"))
    north_ratio = safe_float(north_holding_data.get("ratio"))
    return {
        "main_net_inflow": main_inflow,
        "super_large_net_inflow": safe_float(fund_flow_data.get("superLargeNetInflow")),
        "large_net_inflow": safe_float(fund_flow_data.get("largeNetInflow")),
        "middle_net_inflow": safe_float(fund_flow_data.get("middleNetInflow")),
        "small_net_inflow": safe_float(fund_flow_data.get("smallNetInflow")),
        "north_hold_shares": safe_float(north_holding_data.get("shares")),
        "north_hold_ratio": north_ratio,
        "north_hold_change": safe_float(north_holding_data.get("change")),
        "flow_bias": _flow_bias(main_inflow, north_ratio),
    }


def _pick_industry_snapshot(industry_resp: dict[str, Any], industry_keyword: str) -> dict[str, Any]:
    chains = []
    upstream_match: dict[str, Any] | None = None  # 通过 industry_keyword 匹配到的链
    if _is_successful_response(industry_resp):
        raw = industry_resp.get("data", {})
        if isinstance(raw, dict):
            chains = [dict(item) for item in (raw.get("chains") or []) if isinstance(item, dict)]
            # P2-4.5.2 fix: 检查 message 是否含"未找到"标记,识别真实 not_found 场景
            # 历史问题(诊断报告 §4.5.2):"白色家电" 强制 fuzzy match 到 new_energy 新能源链
            # 修复:基于 industry_keyword 与 chain.name/segments 字符串匹配,严格度比"无脑 chains[0]"高
            if isinstance(raw.get("message"), str) and "未找到" in raw["message"]:
                # graceful not_found:返回全部预置但显式标 matched=False
                return {
                    "industry_keyword": industry_keyword,
                    "matched": False,
                    "chains": [],
                    "related_segments": [],
                    "match_status": "not_found_in_preset_chains",
                    "message": raw["message"],
                }

    if not chains:
        return {
            "industry_keyword": industry_keyword,
            "matched": False,
            "chains": [],
            "related_segments": [],
        }

    # P2-4.5.2 fix: 基于 industry_keyword 字符串匹配选 chain,而不是无脑 chains[0]
    keyword_normalized = str(industry_keyword or "").strip().lower()
    if keyword_normalized:
        for chain in chains:
            chain_name = str(chain.get("name") or "").lower()
            chain_id = str(chain.get("id") or "").lower()
            # 链名/ID 直接含关键词
            if keyword_normalized in chain_name or keyword_normalized in chain_id:
                upstream_match = chain
                break
            # 关键词出现在某段中
            for segment_field in ("upstream", "midstream", "downstream"):
                segments = chain.get(segment_field) or []
                if any(keyword_normalized in str(seg).lower() for seg in segments):
                    upstream_match = chain
                    break
            if upstream_match:
                break

    # 没找到精确/模糊匹配 → graceful 标 fuzzy 状态,提示 AI 可信度低
    if upstream_match is None:
        # 不再无脑 chains[0],而是 matched=False + match_status=fuzzy_fallback
        return {
            "industry_keyword": industry_keyword,
            "matched": False,
            "chains": [],  # 不返回错位 chain,避免 AI 误读
            "related_segments": [],
            "match_status": "no_keyword_match_in_chains",
            "available_chain_count": len(chains),
        }

    best = upstream_match
    related_segments = unique_texts(best.get("upstream", []), best.get("midstream", []), best.get("downstream", []))
    return {
        "industry_keyword": industry_keyword,
        "matched": True,
        "chain_id": best.get("id"),
        "chain_name": best.get("name"),
        "chains": [best] + [c for c in chains[:3] if c is not best][:2],
        "related_segments": related_segments[:12],
        "match_status": "keyword_matched",
    }


def _build_fallback_stock_assessment(
    *,
    quote_data: dict[str, Any],
    fund_flow_snapshot: dict[str, Any],
    market_snapshot: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str], str, str]:
    evidence: list[dict[str, Any]] = []
    highlights: list[str] = []
    risks: list[str] = []

    change_pct = safe_float(quote_data.get("changePercent"))
    if change_pct is not None:
        text = f"实时涨跌幅 {change_pct:.2f}%"
        evidence.append({"category": "market", "signal": "change_pct", "value": change_pct, "interpretation": text})
        if change_pct >= 0:
            highlights.append(text)
        else:
            risks.append(text)

    main_inflow = safe_float(fund_flow_snapshot.get("main_net_inflow"))
    if main_inflow is not None:
        flow_text = f"主力资金净流入 {main_inflow:.0f} 元"
        evidence.append({"category": "fund_flow", "signal": "main_net_inflow", "value": main_inflow, "interpretation": flow_text})
        if main_inflow >= 0:
            highlights.append(flow_text)
        else:
            risks.append(flow_text)

    liquidity_score = safe_float(market_snapshot.get("liquidity_score"))
    if liquidity_score is not None:
        liquidity_text = f"流动性评分 {liquidity_score:.2f}"
        evidence.append({"category": "market", "signal": "liquidity_score", "value": liquidity_score, "interpretation": liquidity_text})
        if liquidity_score >= 0.45:
            highlights.append(liquidity_text)
        else:
            risks.append("盘口深度偏弱，交易冲击成本可能抬升")

    suspended = bool(market_snapshot.get("suspended"))
    if suspended:
        risks.append("行情快照显示近似停牌/无成交状态")

    if len(highlights) >= 2 and not suspended:
        recommendation = "hold"
        recommendation_text = "基础行情和资金流偏稳，可跟踪等待更完整证据。"
    elif risks and not highlights:
        recommendation = "wait"
        recommendation_text = "缺少完整基本面骨架且市场证据偏弱，建议继续等待。"
    else:
        recommendation = "wait"
        recommendation_text = "已用降级数据构建骨架，建议结合更多证据后再决策。"

    return evidence, unique_texts(highlights), unique_texts(risks), recommendation, recommendation_text


async def build_stock_context(code: str) -> dict[str, Any]:
    """Build structured stock context with graceful fallbacks and market snapshots."""
    normalized_code = resolve_security_code(code)
    if not normalized_code:
        raise ValueError("需要提供股票代码")

    warnings: list[str] = []
    fallback_reasons: list[str] = []
    source_chain = ["decision_context_builder"]
    analysis_context: dict[str, Any] = {}
    analysis_result, info_resp, quote_resp, fund_flow_resp, north_resp, order_book_resp = await asyncio.gather(
        _call_async_tool(get_investment_analysis, normalized_code, timeout_sec=20.0),
        _call_sync_tool(get_stock_info, normalized_code, timeout_sec=10.0),
        _call_async_tool(get_quote_snapshot_response, normalized_code, fallback_mode=FALLBACK_DB_ONLY, timeout_sec=10.0),
        _call_sync_tool(get_stock_fund_flow, normalized_code, timeout_sec=10.0),
        _call_sync_tool(get_north_fund_holding, normalized_code, timeout_sec=10.0),
        _call_sync_tool(get_order_book, normalized_code, timeout_sec=10.0),
    )

    if _is_successful_response(analysis_result):
        analysis_context = dict(analysis_result.get("data") or {})
        source_chain.append("tools.investment_analysis")
    else:
        _append_issue(
            warnings=warnings,
            fallback_reasons=fallback_reasons,
            label="investment_analysis",
            response=analysis_result,
        )

    for label, resp in (
        ("stock_info", info_resp),
        ("realtime_quote", quote_resp),
        ("stock_fund_flow", fund_flow_resp),
        ("north_fund_holding", north_resp),
        ("order_book", order_book_resp),
    ):
        if _is_successful_response(resp):
            source_chain.append(f"tools.{label}")
        else:
            _append_issue(warnings=warnings, fallback_reasons=fallback_reasons, label=label, response=resp)

    basic_info = analysis_context.get("basic_info", {}) if isinstance(analysis_context.get("basic_info"), dict) else {}
    price_context = analysis_context.get("price_context", {}) if isinstance(analysis_context.get("price_context"), dict) else {}
    valuation = analysis_context.get("valuation", {}) if isinstance(analysis_context.get("valuation"), dict) else {}
    risk = analysis_context.get("risk", {}) if isinstance(analysis_context.get("risk"), dict) else {}
    info_data = _resp_data(info_resp)
    quote_data = _resp_data(quote_resp)
    fund_flow_data = _resp_data(fund_flow_resp)
    north_data = _resp_data(north_resp)
    order_book_data = _resp_data(order_book_resp)

    name = str(basic_info.get("name") or info_data.get("name") or quote_data.get("name") or "")
    industry = str(basic_info.get("industry") or info_data.get("industry") or "")
    industry_resp = {"success": False, "error": "industry_keyword_missing", "data": None}
    if industry:
        industry_resp = await _call_sync_tool(get_industry_chain, industry, "", timeout_sec=8.0)
        if _is_successful_response(industry_resp):
            source_chain.append("tools.industry_chain")
        else:
            _append_issue(
                warnings=warnings,
                fallback_reasons=fallback_reasons,
                label="industry_chain",
                response=industry_resp,
            )

    market_snapshot = _build_market_snapshot(quote_data, order_book_data, risk)
    fund_flow_snapshot = _build_fund_flow_snapshot(fund_flow_data, north_data)
    industry_chain_snapshot = _pick_industry_snapshot(industry_resp, industry)

    if analysis_context:
        evidence, highlights, risks, recommendation, recommendation_text = _build_evidence(analysis_context)
    else:
        evidence, highlights, risks, recommendation, recommendation_text = _build_fallback_stock_assessment(
            quote_data=quote_data,
            fund_flow_snapshot=fund_flow_snapshot,
            market_snapshot=market_snapshot,
        )

    stock_score = {"buy": 80.0, "hold": 62.0, "wait": 45.0, "sell": 22.0}.get(recommendation, 50.0)
    stock_score += len(highlights) * 3.5
    stock_score -= len(risks) * 3.0
    if fund_flow_snapshot.get("flow_bias") == "bullish":
        stock_score += 4.0
    elif fund_flow_snapshot.get("flow_bias") == "bearish":
        stock_score -= 4.0
    if market_snapshot.get("suspended"):
        stock_score -= 15.0
    stock_score = round(clamp(stock_score, 0.0, 100.0), 2)

    security_status = {
        "is_st": name.upper().startswith("ST") or name.upper().startswith("*ST"),
        "suspended": bool(market_snapshot.get("suspended")),
        "list_date": str(info_data.get("listDate") or ""),
        "industry": industry,
        "liquidity_score": market_snapshot.get("liquidity_score"),
    }

    missing_fields = []
    if not analysis_context:
        missing_fields.append("analysis_context")
    if market_snapshot.get("price") is None:
        missing_fields.append("market_snapshot.price")
    if fund_flow_snapshot.get("main_net_inflow") is None:
        missing_fields.append("fund_flow_snapshot.main_net_inflow")
    if not industry_chain_snapshot.get("matched"):
        missing_fields.append("industry_chain_snapshot")
    if not valuation:
        warnings.append("valuation_context_missing")
        missing_fields.append("valuation")
    if not risk:
        warnings.append("risk_context_missing")
        missing_fields.append("risk")

    stock_asof_value = latest_timestamp(
        price_context.get("analysis_date"),
        *( _response_timestamp_candidates(info_resp, info_data, "reportDate") ),
        *( _response_timestamp_candidates(quote_resp, quote_data, "data_timestamp", "time", "trade_time") ),
        *( _response_timestamp_candidates(fund_flow_resp, fund_flow_data, "tradeDate", "date") ),
        *( _response_timestamp_candidates(north_resp, north_data, "date") ),
        *( _response_timestamp_candidates(order_book_resp, order_book_data, "timestamp") ),
        *( _response_timestamp_candidates(industry_resp, {}, "timestamp") ),
        (analysis_result or {}).get("timestamp"),
    )

    meta = build_context_meta(
        source="stock_context",
        source_chain=source_chain,
        asof_value=stock_asof_value,
        warnings=warnings,
        fallback_reason=fallback_reasons,
        missing_fields=missing_fields,
        degraded=bool(fallback_reasons or missing_fields),
        cached=bool((analysis_result or {}).get("cached") or quote_resp.get("cached")),
    )

    return {
        "code": normalized_code,
        "name": name,
        "analysis_date": str(meta["updated_at"]),
        "analysis_context": analysis_context,
        "recommendation": recommendation,
        "recommendation_text": recommendation_text,
        "score": stock_score,
        "evidence": evidence,
        "highlights": highlights,
        "risks": risks,
        "current_price": safe_float(price_context.get("current_price") or quote_data.get("price")),
        "volatility_20d": safe_float(risk.get("volatility_20d")),
        "market_snapshot": market_snapshot,
        "fund_flow_snapshot": fund_flow_snapshot,
        "industry_chain_snapshot": industry_chain_snapshot,
        "security_status": security_status,
        **meta,
    }


async def build_user_context(user_id: str | None) -> dict[str, Any]:
    """Best-effort user context builder. Missing user data never blocks the pipeline."""
    normalized_user_id = str(user_id or "").strip()
    result: dict[str, Any] = {
        "user_id": normalized_user_id or None,
        "risk_level": "moderate",
        "risk_bucket": "moderate",
        "kyc_level": None,
        "preferences": {},
        "weighted_profile": None,
        "profile_source": "anonymous",
    }
    warnings: list[str] = []
    source_chain = ["decision_context_builder"]

    if not normalized_user_id:
        meta = build_context_meta(
            source="user_context",
            source_chain=[*source_chain, "anonymous_fallback"],
            asof_value=datetime.now().astimezone().isoformat(),
            warnings=["anonymous_user"],
            fallback_reason=["anonymous_fallback"],
            degraded=True,
            cached=False,
        )
        return {
            **result,
            "profile_source": "anonymous_fallback",
            **meta,
        }

    db = get_db()
    rows: list[Any] = []
    app_users_hit = False
    users_hit = False

    try:
        async with db.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    "SELECT risk_level, preferences FROM app_users WHERE id = $1 LIMIT 1",
                    normalized_user_id,
                )
                if row:
                    app_users_hit = True
                    prefs = _normalize_preferences(row.get("preferences"))
                    risk_level = str(row.get("risk_level") or "").strip() or "moderate"
                    result["risk_level"] = risk_level
                    result["risk_bucket"] = _risk_level_to_bucket(risk_level)
                    result["preferences"] = prefs
                    result["kyc_level"] = prefs.get("kyc_level")
                    result["profile_source"] = "app_users"
                    source_chain.append("db.app_users")
            except Exception as exc:
                warnings.append(f"app_users:{_sanitize_warning_message(str(exc))}")

            try:
                row = await conn.fetchrow(
                    "SELECT settings FROM users WHERE id = $1 LIMIT 1",
                    normalized_user_id,
                )
                if row:
                    users_hit = True
                    settings = _normalize_preferences(row.get("settings"))
                    if not app_users_hit:
                        result["preferences"] = settings
                        result["kyc_level"] = settings.get("kyc_level") or settings.get("risk_level")
                        result["risk_level"] = str(settings.get("risk_level") or result["risk_level"] or "moderate")
                        result["risk_bucket"] = _risk_level_to_bucket(result["risk_level"])
                        result["profile_source"] = "users"
                    source_chain.append("db.users")
            except Exception as exc:
                warnings.append(f"users:{_sanitize_warning_message(str(exc))}")

            try:
                rows = await conn.fetch(
                    """SELECT neuroticism, openness, herd_tendency, greed_fear_axis, confidence, created_at
                       FROM user_profile_snapshots
                       WHERE user_id = $1
                       ORDER BY created_at DESC
                       LIMIT 20""",
                    normalized_user_id,
                )
                if rows:
                    now = datetime.now(timezone.utc)
                    decay_rate = math.log(2) / 7.0
                    totals = {
                        "neuroticism": 0.0,
                        "openness": 0.0,
                        "herd_tendency": 0.0,
                        "greed_fear_axis": 0.0,
                        "confidence": 0.0,
                    }
                    total_weight = 0.0
                    for row in rows:
                        created_at = row["created_at"]
                        # P3-5.17 fix: created_at 可能是 string(SQLite 路径)而非 datetime
                        # 历史问题(诊断报告 §5.17):'str' object has no attribute 'tzinfo'
                        if isinstance(created_at, str):
                            try:
                                created_at = datetime.fromisoformat(
                                    created_at.replace("Z", "+00:00")
                                )
                            except (TypeError, ValueError):
                                # 解析失败 → 跳过此条 snapshot
                                continue
                        if created_at is None:
                            continue
                        if not isinstance(created_at, datetime):
                            continue
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        age_days = (now - created_at).total_seconds() / 86400.0
                        weight = math.exp(-decay_rate * age_days)
                        total_weight += weight
                        for key in totals:
                            totals[key] += weight * float(row[key] or 0.0)
                    if total_weight > 0:
                        result["weighted_profile"] = {
                            key: round(value / total_weight, 4)
                            for key, value in totals.items()
                        }
                    source_chain.append("db.user_profile_snapshots")
            except Exception as exc:
                warnings.append(f"user_profile_snapshots:{_sanitize_warning_message(str(exc))}")
    except Exception as exc:
        warnings.append(f"db_acquire:{_sanitize_warning_message(str(exc))}")

    if not app_users_hit and not users_hit:
        result["profile_source"] = "fallback"

    meta = build_context_meta(
        source="user_context",
        source_chain=source_chain,
        asof_value=datetime.now().astimezone().isoformat(),
        warnings=warnings,
        fallback_reason=["user_profile_fallback"] if not app_users_hit and not users_hit else None,
        missing_fields=["weighted_profile"] if result.get("weighted_profile") is None else [],
        degraded=bool(warnings or not app_users_hit and not users_hit),
        cached=False,
    )
    return {
        **result,
        **meta,
    }
