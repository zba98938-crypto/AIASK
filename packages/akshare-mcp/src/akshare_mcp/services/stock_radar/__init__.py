"""AIASK stock radar service.

The radar is an event-discovery layer over the unified market text/event
storage. It keeps external ingest explicit and degraded when unavailable, and
stores only structured candidate snapshots rather than trading instructions.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from ..market_text_source_ingest import run_market_text_source_ingest


RADAR_EVENT_TYPES = {
    "major_contract",
    "ai_compute_cooperation",
    "robotics_order",
    "ma_restructuring",
    "private_placement",
    "buyback",
    "earnings_forecast_up",
    "state_owned_investment",
    "shareholder_reduction",
    "investigation",
    "inquiry_letter",
    "earnings_warning",
    "pledge_risk",
    "policy_news",
    "fund_flow_confirmation",
    "dragon_tiger_anomaly",
    "late_session_volume",
}

POSITIVE_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...], float], ...] = (
    ("ai_compute_cooperation", ("AI", "人工智能", "算力", "大模型", "智算", "数据中心"), ("AI", "算力"), 0.88),
    ("robotics_order", ("机器人", "人形机器人", "工业机器人", "订单"), ("机器人",), 0.84),
    ("major_contract", ("重大合同", "签订合同", "中标", "项目合同", "订单", "采购合同", "销售合同"), ("重大合同",), 0.82),
    ("ma_restructuring", ("并购", "收购", "重大资产重组", "资产重组", "购买资产"), ("并购重组",), 0.80),
    ("private_placement", ("定增", "向特定对象发行", "非公开发行", "再融资"), ("定增",), 0.68),
    ("buyback", ("回购", "股份回购", "增持"), ("回购",), 0.72),
    ("earnings_forecast_up", ("业绩预增", "扭亏", "净利润增长", "业绩快报"), ("业绩",), 0.76),
    ("state_owned_investment", ("国资入股", "国有资本", "控股股东变更", "实际控制人变更"), ("国资",), 0.74),
    ("policy_news", ("政策", "方案", "规划", "指导意见", "通知", "支持"), ("政策",), 0.56),
)

RISK_RULES: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("shareholder_reduction", ("减持", "被动减持", "拟减持"), 0.82),
    ("investigation", ("立案", "调查", "证监会立案", "涉嫌违法", "行政处罚"), 0.92),
    ("inquiry_letter", ("问询函", "监管函", "关注函"), 0.70),
    ("earnings_warning", ("业绩预亏", "亏损", "业绩暴雷", "业绩预减", "商誉减值"), 0.88),
    ("pledge_risk", ("质押", "冻结", "司法冻结", "平仓风险"), 0.78),
)

TIER_WEIGHTS = {"tier_a": 1.0, "tier_b": 0.82, "tier_c": 0.5}
PDF_TEXT_LIMIT = 24_000
LLM_BODY_LIMIT = 8_000

from .analysis import (
    RadarExtraction,
    extract_radar_event,
    enhance_radar_event_with_llm,
    score_radar_candidate,
    _high_confidence_candidate,
    _clean,
    _as_bool,
    _positive_int,
    _normalize_codes,
    _source_tier,
    _direction_for_event,
    _event_type_name,
    _extract_amount_text,
    _extract_counterparties,
    _stock_code_from_doc,
    _bare_stock_code,
    _clamp_float,
    _list_strings,
    _source_doc_uids_for_doc,
    _is_positive_direction,
    _is_negative_direction,
    _extract_payload_items,
    _numeric_from_keys,
    _unwrap_llm_extraction,
    _validated_llm_extraction,
)
from .ingest import (
    fetch_rss_feed_documents,
    _configured_rss_feeds,
    _safe_feed_name,
    _rss_text,
    _pdf_url_from_doc,
    _looks_like_pdf_url,
    _pdf_cache_dir,
    _download_pdf_file,
    _parse_pdf_with_pymupdf,
    _parse_pdf_with_pdfplumber,
    _parse_pdf_with_paddleocr,
    _extract_pdf_text_from_file,
    _pdf_parse_status,
    _pdf_metadata_for_persist,
    _merge_document_metadata,
    _list_recent_market_documents,
    _persist_rss_documents,
)

def _candidate_event_id(doc: dict[str, Any], extraction: dict[str, Any]) -> str:
    basis = "|".join(
        [
            _clean(doc.get("doc_uid") or doc.get("event_id"), 240),
            _clean(extraction.get("event_type"), 120),
            _clean(doc.get("stock_code") or doc.get("code") or "MARKET", 40),
        ]
    )
    return f"radar_evt_{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:24]}"


def _status_from_tool_payload(payload: dict[str, Any]) -> str:
    if payload.get("success") is False:
        return "degraded"
    if payload.get("degraded") or payload.get("fallback_used") or payload.get("fallback_reason"):
        return "degraded"
    return "ok"


def _provider_meta(payload: dict[str, Any]) -> dict[str, Any]:
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    quality = meta.get("quality") if isinstance(meta.get("quality"), dict) else {}
    return {
        "source": payload.get("source"),
        "source_chain": payload.get("source_chain") or quality.get("source_chain"),
        "fallback_reason": payload.get("fallback_reason") or quality.get("fallback_reason"),
        "degraded": bool(payload.get("degraded") or meta.get("degraded")),
        "error": payload.get("error"),
    }


def _fund_flow_confirmation(payload: dict[str, Any], extraction: dict[str, Any]) -> dict[str, Any]:
    items = _extract_payload_items(payload)
    item = items[0] if items else {}
    net = _numeric_from_keys(
        item,
        (
            "mainNetInflow",
            "main_net_inflow",
            "mainBuyNet",
            "net_inflow",
            "netAmount",
            "net_amount",
        ),
    )
    if not items or net is None:
        return {"status": "degraded", "confirmed": False, "reason": "fund_flow_empty_or_missing_net", **_provider_meta(payload)}
    positive = _is_positive_direction(extraction)
    negative = _is_negative_direction(extraction)
    confirmed = (positive and net > 0) or (negative and net < 0)
    conflict = (positive and net < 0) or (negative and net > 0)
    return {
        "status": _status_from_tool_payload(payload),
        "confirmed": bool(confirmed),
        "conflict": bool(conflict),
        "main_net_inflow": net,
        "source": item.get("source") or payload.get("source"),
        **_provider_meta(payload),
    }


def _north_fund_confirmation(payload: dict[str, Any], extraction: dict[str, Any]) -> dict[str, Any]:
    items = _extract_payload_items(payload)
    totals = [
        value
        for item in items[:3]
        if (value := _numeric_from_keys(item, ("total", "north_money", "net_amount", "netAmount"))) is not None
    ]
    if not totals:
        return {"status": "degraded", "confirmed": False, "reason": "north_fund_empty_or_missing_total", **_provider_meta(payload)}
    recent_total = sum(totals)
    positive = _is_positive_direction(extraction)
    negative = _is_negative_direction(extraction)
    confirmed = (positive and recent_total > 0) or (negative and recent_total < 0)
    conflict = (positive and recent_total < 0) or (negative and recent_total > 0)
    return {
        "status": _status_from_tool_payload(payload),
        "confirmed": bool(confirmed),
        "conflict": bool(conflict),
        "recent_total": recent_total,
        "days_used": len(totals),
        "market_wide": True,
        **_provider_meta(payload),
    }


def _dragon_tiger_confirmation(payload: dict[str, Any], symbol: str, extraction: dict[str, Any]) -> dict[str, Any]:
    bare = _bare_stock_code(symbol)
    items = _extract_payload_items(payload)
    matched = [item for item in items if not bare or _bare_stock_code(item.get("code") or item.get("stock_code")) == bare]
    if not matched:
        return {
            "status": "degraded" if payload.get("degraded") or not items else "ok",
            "confirmed": False,
            "reason": "dragon_tiger_no_symbol_match" if items else "dragon_tiger_empty",
            "alias_policy": "alias_mapping_only",
            **_provider_meta(payload),
        }
    net_total = sum(_numeric_from_keys(item, ("netAmount", "net_amount", "net_buy")) or 0.0 for item in matched)
    positive = _is_positive_direction(extraction)
    negative = _is_negative_direction(extraction)
    confirmed = (positive and net_total > 0) or (negative and net_total < 0)
    conflict = (positive and net_total < 0) or (negative and net_total > 0)
    return {
        "status": _status_from_tool_payload(payload),
        "confirmed": bool(confirmed),
        "conflict": bool(conflict),
        "net_amount": net_total,
        "rows": matched[:5],
        "alias_policy": "alias_mapping_only",
        **_provider_meta(payload),
    }


def _sector_heat_confirmation(sector_payload: dict[str, Any], concept_payload: dict[str, Any], themes: list[str]) -> dict[str, Any]:
    tokens = [token.lower() for token in _list_strings(themes, limit=12, item_limit=80)]
    if not tokens:
        return {"status": "degraded", "score": 0.0, "themes": [], "reason": "themes_empty"}
    rows = [*(_extract_payload_items(sector_payload)), *(_extract_payload_items(concept_payload))]
    if not rows:
        return {
            "status": "degraded",
            "score": 0.0,
            "themes": list(themes),
            "reason": "sector_concept_flow_empty",
            "sector": _provider_meta(sector_payload),
            "concept": _provider_meta(concept_payload),
        }
    matches: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        name = _clean(row.get("name") or row.get("block_name") or row.get("industry") or row.get("concept"), 120)
        name_lower = name.lower()
        if not name_lower or not any(token and (token in name_lower or name_lower in token) for token in tokens):
            continue
        net = _numeric_from_keys(row, ("mainNetInflow", "main_net_inflow", "net_amount", "netAmount", "totalAmount"))
        change = _numeric_from_keys(row, ("changePercent", "change_pct", "avg_change_pct"))
        positive_net = 1.0 if net is not None and net > 0 else 0.0
        rank_score = max(0.0, 1.0 - idx / max(len(rows), 1))
        change_score = 0.2 if change is not None and change > 0 else 0.0
        score = min(1.0, 0.45 + 0.35 * positive_net + 0.2 * rank_score + change_score)
        matches.append({"name": name, "score": round(score, 4), "main_net_inflow": net, "change_percent": change, "source": row.get("source")})
    if not matches:
        return {
            "status": "degraded" if sector_payload.get("degraded") or concept_payload.get("degraded") else "ok",
            "score": 0.0,
            "themes": list(themes),
            "reason": "theme_not_in_top_sector_concept_flow",
            "sector": _provider_meta(sector_payload),
            "concept": _provider_meta(concept_payload),
        }
    score = max(float(item["score"]) for item in matches)
    return {
        "status": "ok",
        "score": round(score, 4),
        "themes": list(themes),
        "matches": matches[:6],
        "sector": _provider_meta(sector_payload),
        "concept": _provider_meta(concept_payload),
    }


def _late_session_from_bars(rows: list[dict[str, Any]], extraction: dict[str, Any]) -> dict[str, Any]:
    if len(rows) < 8:
        return {"status": "disabled", "confirmed": False, "reason": "minute_line_insufficient_bars", "bars": len(rows)}

    def _time_of(row: dict[str, Any]) -> str:
        raw = _clean(row.get("trade_time") or row.get("time") or row.get("datetime") or row.get("date"), 32)
        match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?!\d)", raw)
        if match:
            return f"{int(match.group(1)):02d}:{match.group(2)}"
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) >= 12:
            return f"{digits[-4:-2]}:{digits[-2:]}"
        if len(digits) == 4:
            return f"{digits[:2]}:{digits[2:]}"
        return raw

    late: list[dict[str, Any]] = []
    prev: list[dict[str, Any]] = []
    for row in rows:
        t = _time_of(row)
        if "14:30" <= t <= "15:00":
            late.append(row)
        elif "14:00" <= t < "14:30":
            prev.append(row)
    if not late or not prev:
        return {"status": "disabled", "confirmed": False, "reason": "late_session_window_unavailable", "bars": len(rows)}
    late_amount = sum(_numeric_from_keys(row, ("amount", "turnover", "money")) or 0.0 for row in late)
    prev_amount = sum(_numeric_from_keys(row, ("amount", "turnover", "money")) or 0.0 for row in prev)
    ratio = late_amount / prev_amount if prev_amount > 0 else 0.0
    first_close = _numeric_from_keys(late[0], ("close", "price"))
    last_close = _numeric_from_keys(late[-1], ("close", "price"))
    price_direction = 0.0 if first_close in {None, 0} or last_close is None else (last_close - first_close) / abs(first_close)
    confirmed = ratio >= 1.5 and ((_is_positive_direction(extraction) and price_direction >= 0) or (_is_negative_direction(extraction) and price_direction <= 0))
    return {
        "status": "ok",
        "confirmed": bool(confirmed),
        "amount_ratio": round(ratio, 4),
        "late_amount": late_amount,
        "previous_amount": prev_amount,
        "price_direction": round(price_direction, 6),
        "bars": len(rows),
    }


async def _late_session_confirmation(db: Any, symbol: str, extraction: dict[str, Any]) -> dict[str, Any]:
    bare = _bare_stock_code(symbol)
    if not bare or bare == "MARKET":
        return {"status": "disabled", "confirmed": False, "reason": "symbol_unavailable"}
    try:
        tables = (
            "stock_minute_bars",
            "minute_bars",
            "stock_minute_kline",
            "tdx_minute_kline",
            "tdx_minute_bars",
        )
        async with db.acquire() as conn:
            for table in tables:
                exists = await conn.fetchval("SELECT name FROM sqlite_master WHERE type = 'table' AND name = $1", table)
                if not exists:
                    continue
                columns = {str(row["name"]) for row in await conn.fetch(f"PRAGMA table_info({table})")}
                code_col = next((item for item in ("code", "stock_code", "symbol") if item in columns), None)
                time_col = next((item for item in ("trade_time", "time", "datetime", "date") if item in columns), None)
                if not code_col or not time_col:
                    continue
                rows = await conn.fetch(
                    f"""
                    SELECT *
                    FROM {table}
                    WHERE {code_col} = $1
                    ORDER BY {time_col} DESC
                    LIMIT 260
                    """,
                    bare,
                )
                if rows:
                    ordered = [dict(row) for row in reversed(rows)]
                    return {**_late_session_from_bars(ordered, extraction), "source": table}
        return {"status": "disabled", "confirmed": False, "reason": "minute_line_table_unavailable"}
    except Exception as exc:
        return {"status": "degraded", "confirmed": False, "reason": "minute_line_query_failed", "error": f"{type(exc).__name__}: {exc}"}


async def _confirmation_factors(db: Any, symbol: str, extraction: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "fund_flow": {"status": "degraded", "confirmed": False, "reason": "fund_flow_adapter_unavailable"},
        "north_fund": {"status": "degraded", "confirmed": False, "reason": "north_fund_adapter_unavailable"},
        "dragon_tiger": {"status": "degraded", "confirmed": False, "reason": "dragon_tiger_adapter_unavailable", "alias_policy": "alias_mapping_only"},
        "sector_heat": {"status": "degraded", "score": 0.0, "themes": list(extraction.get("themes") or []), "reason": "sector_heat_adapter_unavailable"},
    }
    if not symbol or symbol == "MARKET":
        return {
            **defaults,
            "late_session_volume": {"status": "disabled", "confirmed": False, "reason": "symbol_unavailable"},
            "symbol": symbol,
        }
    try:
        from akshare_mcp.tools import fund_flow as fund_flow_tools
    except Exception as exc:
        reason = f"fund_flow_tools_import_failed:{type(exc).__name__}"
        return {
            **{key: {**value, "reason": reason} for key, value in defaults.items()},
            "late_session_volume": await _late_session_confirmation(db, symbol, extraction),
            "symbol": symbol,
        }

    results = dict(defaults)
    bare = _bare_stock_code(symbol)
    try:
        payload = fund_flow_tools.get_stock_fund_flow(code=bare, prefer_db=True)
        results["fund_flow"] = _fund_flow_confirmation(payload if isinstance(payload, dict) else {}, extraction)
    except Exception as exc:
        results["fund_flow"] = {"status": "degraded", "confirmed": False, "reason": "fund_flow_failed", "error": f"{type(exc).__name__}: {exc}"}
    try:
        payload = fund_flow_tools.get_north_fund(days=3)
        results["north_fund"] = _north_fund_confirmation(payload if isinstance(payload, dict) else {}, extraction)
    except Exception as exc:
        results["north_fund"] = {"status": "degraded", "confirmed": False, "reason": "north_fund_failed", "error": f"{type(exc).__name__}: {exc}"}
    try:
        payload = fund_flow_tools.get_dragon_tiger(stock_code=bare)
        results["dragon_tiger"] = _dragon_tiger_confirmation(payload if isinstance(payload, dict) else {}, bare, extraction)
    except Exception as exc:
        results["dragon_tiger"] = {
            "status": "degraded",
            "confirmed": False,
            "reason": "dragon_tiger_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "alias_policy": "alias_mapping_only",
        }
    try:
        sector_payload = fund_flow_tools.get_sector_fund_flow(top_n=30)
    except Exception as exc:
        sector_payload = {"success": False, "error": f"{type(exc).__name__}: {exc}", "degraded": True}
    try:
        concept_payload = fund_flow_tools.get_concept_fund_flow(top_n=30)
    except Exception as exc:
        concept_payload = {"success": False, "error": f"{type(exc).__name__}: {exc}", "degraded": True}
    results["sector_heat"] = _sector_heat_confirmation(
        sector_payload if isinstance(sector_payload, dict) else {},
        concept_payload if isinstance(concept_payload, dict) else {},
        list(extraction.get("themes") or []),
    )
    results["late_session_volume"] = await _late_session_confirmation(db, symbol, extraction)
    results["symbol"] = symbol
    return {
        **results,
    }


def _source_chain(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "doc_uid": doc.get("doc_uid") or doc.get("event_id"),
            "source": doc.get("source"),
            "provider": doc.get("provider"),
            "source_tier": _source_tier(doc.get("source_tier")),
            "url": doc.get("url"),
            "published_at": doc.get("published_at"),
        }
    ]


async def run_stock_radar(
    db,
    *,
    mode: str = "dry_run",
    days: Any = 3,
    limit: Any = 80,
    stock_codes: Any = None,
    allow_network: Any = False,
    allow_llm: Any = False,
    embed: Any = False,
    parse_pdf: Any = True,
    include_rss: Any = True,
    ingest_market_text: Any = True,
) -> dict[str, Any]:
    resolved_mode = _clean(mode or "dry_run", 80) or "dry_run"
    resolved_days = _positive_int(days, 3, minimum=1, maximum=10)
    resolved_limit = _positive_int(limit, 80, minimum=1, maximum=500)
    resolved_allow_network = _as_bool(allow_network, False)
    requested_allow_llm = _as_bool(allow_llm, False)
    resolved_allow_llm = bool(resolved_allow_network and requested_allow_llm)
    resolved_embed = _as_bool(embed, False)
    resolved_parse_pdf = _as_bool(parse_pdf, True)
    resolved_include_rss = _as_bool(include_rss, True)
    resolved_ingest = _as_bool(ingest_market_text, True)
    started_at = datetime.now(timezone.utc).isoformat()
    run = await db.upsert_stock_radar_run(
        {
            "mode": resolved_mode,
            "status": "running",
            "started_at": started_at,
            "summary": {
                "days": resolved_days,
                "limit": resolved_limit,
                "allow_network": resolved_allow_network,
                "allow_llm": resolved_allow_llm,
                "stock_codes": _normalize_codes(stock_codes),
            },
            "degraded_flags": [] if resolved_allow_network else ["network_disabled"],
            "metadata": {"schema": "stock_radar_v1"},
        }
    )
    run_id = str(run.get("run_id") or "")
    degraded_flags: list[str] = [] if resolved_allow_network else ["network_disabled"]
    errors: list[dict[str, Any]] = []
    ingest_result: dict[str, Any] = {}
    rss_saved: dict[str, Any] = {}

    try:
        if resolved_ingest:
            ingest_result = await run_market_text_source_ingest(
                db,
                stock_codes=_normalize_codes(stock_codes),
                doc_types=["notice", "news"],
                news_limit=20,
                notice_limit=20,
                official_notice_limit=resolved_limit,
                notice_days=resolved_days,
                code_notice_limit=0,
                code_notice_code_limit=0,
                research_code_limit=0,
                embed=resolved_embed,
                build_snapshot=False,
                activate_snapshot=False,
                allow_network=resolved_allow_network,
                dry_run=False,
            )
            for error in list(ingest_result.get("errors") or []):
                errors.append(dict(error))
            if ingest_result.get("quality_flags"):
                degraded_flags.extend(str(item) for item in list(ingest_result.get("quality_flags") or []))

        rss_docs: list[dict[str, Any]] = []
        if resolved_include_rss:
            feeds = _configured_rss_feeds()
            if not feeds:
                degraded_flags.append("rss_feeds_not_configured")
            elif not resolved_allow_network:
                degraded_flags.append("rss_network_disabled")
            else:
                for feed in feeds:
                    try:
                        rss_docs.extend(fetch_rss_feed_documents(feed, limit=30))
                    except Exception as exc:
                        errors.append({"source": "rss", "feed_url": feed, "error": f"{type(exc).__name__}: {exc}"})
                rss_saved = await _persist_rss_documents(db, rss_docs, embed=resolved_embed)

        docs = await _list_recent_market_documents(db, days=resolved_days, limit=resolved_limit * 4)
        candidates: list[dict[str, Any]] = []
        for doc in docs:
            extraction_obj = extract_radar_event(doc)
            if extraction_obj is None:
                continue
            symbol = _stock_code_from_doc(doc) or "MARKET"
            if symbol == "MARKET" and extraction_obj.event_type not in {"policy_news"}:
                continue
            metadata = dict(doc.get("metadata") or {})
            pdf_status = _pdf_parse_status(doc, parse_pdf=resolved_parse_pdf, allow_network=resolved_allow_network)
            if pdf_status.get("status") in {"degraded", "disabled"}:
                degraded_flags.append(f"pdf_parse_{pdf_status.get('status')}")
            pdf_metadata = {
                "radar_pdf_parse": _pdf_metadata_for_persist(pdf_status, checksum=doc.get("checksum"))
            }
            if pdf_status.get("text") and not doc.get("body"):
                doc["body"] = _clean(pdf_status.get("text"), LLM_BODY_LIMIT)
            metadata.update(pdf_metadata)
            doc["metadata"] = {
                **metadata,
                "radar_pdf_parse": {
                    **pdf_metadata["radar_pdf_parse"],
                    "text": _clean(pdf_status.get("text"), LLM_BODY_LIMIT) if pdf_status.get("text") else "",
                },
            }
            if pdf_status.get("status") != "not_pdf":
                try:
                    if not await _merge_document_metadata(db, _clean(doc.get("doc_uid"), 240), pdf_metadata):
                        degraded_flags.append("pdf_metadata_persist_unavailable")
                except Exception as exc:
                    degraded_flags.append("pdf_metadata_persist_failed")
                    errors.append({"source": "market_documents", "error": f"{type(exc).__name__}: {exc}"})
            extraction, llm_meta = await enhance_radar_event_with_llm(
                doc,
                extraction_obj,
                allow_llm=resolved_allow_llm,
            )
            if llm_meta and llm_meta.get("status") != "ok":
                degraded_flags.append("llm_unavailable_rules_only" if llm_meta.get("status") == "unavailable" else "llm_failed_rules_only")
                errors.append({"source": "llm", **llm_meta})
            confirmations = await _confirmation_factors(db, symbol, extraction)
            score = score_radar_candidate(
                extraction=extraction,
                source_tier=_source_tier(doc.get("source_tier")),
                confirmations=confirmations,
                risk_flags=list(extraction.get("risk_flags") or []),
            )
            event_id = _candidate_event_id(doc, extraction)
            candidate = await db.upsert_stock_radar_candidate(
                {
                    "run_id": run_id,
                    "symbol": symbol,
                    "stock_name": doc.get("stock_name") or "",
                    "tier": score["tier"],
                    "radar_score": score["radar_score"],
                    "event_id": event_id,
                    "event_type": extraction["event_type"],
                    "direction": extraction["direction"],
                    "summary": extraction["summary"],
                    "source_doc_uids": extraction["source_doc_uids"],
                    "source_chain": _source_chain(doc),
                    "extraction": {**extraction, "score": score},
                    "confirmations": confirmations,
                    "risk_flags": extraction.get("risk_flags") or [],
                    "push_status": "pending",
                }
            )
            candidates.append(candidate)
            if len(candidates) >= resolved_limit:
                break

        tier_counts: dict[str, int] = {}
        for candidate in candidates:
            tier = str(candidate.get("tier") or "unknown")
            tier_counts[tier] = int(tier_counts.get(tier) or 0) + 1
        completed = await db.upsert_stock_radar_run(
            {
                "run_id": run_id,
                "mode": resolved_mode,
                "status": "completed",
                "started_at": started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "summary": {
                    "candidate_count": len(candidates),
                    "tier_counts": tier_counts,
                    "docs_scanned": len(docs),
                    "ingest": {
                        "totals": ingest_result.get("totals", {}),
                        "fetched": ingest_result.get("fetched", {}),
                    },
                    "rss": rss_saved,
                    "source_policy": "local_first_explicit_external_ingest",
                    "allow_network": resolved_allow_network,
                    "allow_llm": resolved_allow_llm,
                    "allow_llm_requested": requested_allow_llm,
                },
                "degraded_flags": sorted(dict.fromkeys(degraded_flags)),
                "error": None,
                "metadata": {
                    "event_types": sorted(RADAR_EVENT_TYPES),
                    "no_trade_instructions": True,
                    "allow_network": resolved_allow_network,
                    "allow_llm": resolved_allow_llm,
                    "allow_llm_requested": requested_allow_llm,
                },
            }
        )
        digest = await db.summarize_stock_radar(run_id=run_id, limit=20)
        return {
            "object": "stock_radar.run",
            "success": True,
            "data": {
                "run": completed,
                "candidates": candidates,
                "candidate_count": len(candidates),
                "tier_counts": tier_counts,
                "digest": digest,
                "degraded_flags": sorted(dict.fromkeys(degraded_flags)),
                "errors": errors,
            },
            "error": None,
        }
    except Exception as exc:
        failed = await db.upsert_stock_radar_run(
            {
                "run_id": run_id,
                "mode": resolved_mode,
                "status": "failed",
                "started_at": started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "summary": {
                    "errors": errors,
                    "allow_network": resolved_allow_network,
                    "allow_llm": resolved_allow_llm,
                    "allow_llm_requested": requested_allow_llm,
                },
                "degraded_flags": sorted(dict.fromkeys(degraded_flags)),
                "error": f"{type(exc).__name__}: {exc}",
                "metadata": {"schema": "stock_radar_v1"},
            }
        )
        return {
            "object": "stock_radar.run",
            "success": False,
            "data": {"run": failed, "degraded_flags": sorted(dict.fromkeys(degraded_flags)), "errors": errors},
            "error": f"{type(exc).__name__}: {exc}",
            "error_code": "STOCK_RADAR_RUN_FAILED",
        }


async def stock_radar_status(db, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(params or {})
    return await db.summarize_stock_radar(
        run_id=_clean(payload.get("run_id"), 220) or None,
        limit=_positive_int(payload.get("limit"), 20, minimum=1, maximum=200),
    )


async def stock_radar_candidates(db, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(params or {})
    candidates = await db.list_stock_radar_candidates(
        run_id=_clean(payload.get("run_id"), 220) or None,
        tier=_clean(payload.get("tier"), 40) or None,
        symbol=_clean(payload.get("symbol") or payload.get("stock_code"), 40) or None,
        min_score=float(payload["min_score"]) if payload.get("min_score") not in {None, ""} else None,
        limit=_positive_int(payload.get("limit"), 100, minimum=1, maximum=500),
    )
    return {"object": "stock_radar.candidates", "status": "ready", "candidates": candidates, "count": len(candidates)}


async def stock_radar_digest(db, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(params or {})
    digest = await db.summarize_stock_radar(
        run_id=_clean(payload.get("run_id"), 220) or None,
        limit=_positive_int(payload.get("limit"), 20, minimum=1, maximum=100),
    )
    channels = payload.get("channels") or ["wecom", "telegram"]
    if isinstance(channels, str):
        channels = [item.strip() for item in channels.split(",") if item.strip()]
    preview = _clean(payload.get("message") or digest.get("digest_preview"), 4000)
    if _as_bool(payload.get("record_preview"), False):
        run = digest.get("latest_run") if isinstance(digest.get("latest_run"), dict) else {}
        await db.save_stock_radar_push_log(
            {
                "run_id": run.get("run_id"),
                "channel": "preview",
                "platform": ",".join(str(item) for item in list(channels or [])),
                "status": "preview",
                "message_preview": preview,
                "candidate_count": len(list(digest.get("candidates") or [])),
                "metadata": {"no_trade_instructions": True},
            }
        )
    logs = await db.list_stock_radar_push_logs(
        run_id=(digest.get("latest_run") or {}).get("run_id") if isinstance(digest.get("latest_run"), dict) else None,
        limit=20,
    )
    return {
        "object": "stock_radar.digest",
        "status": digest.get("status") or "unknown",
        "channels": list(channels or []),
        "digest_preview": preview,
        "candidates": list(digest.get("candidates") or []),
        "push_logs": logs,
        "disclaimer": "observation_pool_only_no_buy_sell_instruction",
    }


async def push_stock_radar_digest(db, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(params or {})
    digest = await stock_radar_digest(db, {**payload, "record_preview": False})
    channels = payload.get("channels") or digest.get("channels") or ["wecom", "telegram"]
    if isinstance(channels, str):
        channels = [item.strip() for item in channels.split(",") if item.strip()]
    run = (await db.summarize_stock_radar(run_id=_clean(payload.get("run_id"), 220) or None, limit=50)).get("latest_run") or {}
    message = _clean(payload.get("message") or digest.get("digest_preview"), 4000)
    dry_run = _as_bool(payload.get("dry_run"), True)
    candidates = list(digest.get("candidates") or [])
    high_confidence_count = sum(1 for candidate in candidates if isinstance(candidate, dict) and _high_confidence_candidate(candidate))
    blocked_reason = "" if dry_run or high_confidence_count > 0 else "high_confidence_candidate_required"
    logs = []
    for channel in list(channels or []):
        logs.append(
            await db.save_stock_radar_push_log(
                {
                    "run_id": run.get("run_id"),
                    "channel": channel,
                    "platform": channel,
                    "target": payload.get("target"),
                    "status": "preview" if dry_run else "blocked" if blocked_reason else "queued",
                    "message_preview": message,
                    "candidate_count": len(candidates),
                    "error": blocked_reason or None,
                    "metadata": {
                        "dry_run": dry_run,
                        "gateway_required": True,
                        "no_trade_instructions": True,
                        "high_confidence_candidate_count": high_confidence_count,
                        "blocked_reason": blocked_reason or None,
                    },
                }
            )
        )
    if blocked_reason:
        return {
            "object": "stock_radar.push_digest",
            "success": False,
            "data": {
                "dry_run": dry_run,
                "channels": list(channels or []),
                "message_preview": message,
                "push_logs": logs,
                "gateway_status": "blocked_requires_high_confidence_candidate",
                "high_confidence_candidate_count": high_confidence_count,
            },
            "error": "stock radar digest delivery requires high-confidence non-provisional extraction",
            "error_code": "STOCK_RADAR_PUSH_REQUIRES_HIGH_CONFIDENCE",
        }
    return {
        "object": "stock_radar.push_digest",
        "success": True,
        "data": {
            "dry_run": dry_run,
            "channels": list(channels or []),
            "message_preview": message,
            "push_logs": logs,
            "gateway_status": "preview_recorded" if dry_run else "queued_for_gateway_adapter",
            "high_confidence_candidate_count": high_confidence_count,
        },
        "error": None,
    }


async def schedule_stock_radar_update(_: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(params or {})
    return {
        "object": "stock_radar.schedule_update",
        "success": True,
        "data": {
            "status": "preview",
            "enabled": bool(payload.get("enabled", False)),
            "schedule": payload.get("schedule") or "manual",
            "jobs": [
                "daily_after_close_announcements",
                "intraday_news_fund_radar",
                "late_session_after_1430",
            ],
            "auto_push": False,
            "detail": "Schedule intent recorded as preview; external automation remains opt-in.",
        },
        "error": None,
    }


def run_stock_radar_sync(db, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_stock_radar(db, **kwargs))


__all__ = [
    "RADAR_EVENT_TYPES",
    "extract_radar_event",
    "fetch_rss_feed_documents",
    "push_stock_radar_digest",
    "run_stock_radar",
    "run_stock_radar_sync",
    "schedule_stock_radar_update",
    "score_radar_candidate",
    "stock_radar_candidates",
    "stock_radar_digest",
    "stock_radar_status",
]
