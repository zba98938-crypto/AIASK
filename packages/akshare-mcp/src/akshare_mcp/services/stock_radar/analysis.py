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

def _clean(value: Any, limit: int = 4000) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return " ".join(text.split())[:limit] if text else ""


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _positive_int(value: Any, default: int, *, minimum: int = 0, maximum: int = 1000) -> int:
    try:
        parsed = int(default if value is None or value == "" else value)
    except Exception:
        parsed = int(default)
    return max(minimum, min(parsed, maximum))


def _normalize_codes(raw: Any) -> list[str]:
    if raw is None:
        return []
    values = raw if isinstance(raw, (list, tuple, set)) else str(raw).replace(";", ",").split(",")
    out: list[str] = []
    for value in values:
        token = _clean(value, 40)
        if token and token not in out:
            out.append(token)
    return out


def _source_tier(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"tier_a", "official", "a"}:
        return "tier_a"
    if token in {"tier_b", "institutional", "b"}:
        return "tier_b"
    return "tier_c"


def _direction_for_event(event_type: str) -> str:
    if event_type in {rule[0] for rule in RISK_RULES}:
        return "negative"
    if event_type in {"policy_news"}:
        return "neutral"
    return "positive"


def _event_type_name(event_type: str) -> str:
    return {
        "major_contract": "重大合同",
        "ai_compute_cooperation": "AI/算力合作",
        "robotics_order": "机器人订单",
        "ma_restructuring": "并购重组",
        "private_placement": "定增",
        "buyback": "回购",
        "earnings_forecast_up": "业绩预增",
        "state_owned_investment": "国资入股",
        "shareholder_reduction": "减持",
        "investigation": "立案调查",
        "inquiry_letter": "问询函",
        "earnings_warning": "业绩预警",
        "pledge_risk": "质押风险",
        "policy_news": "政策新闻",
        "fund_flow_confirmation": "资金共振",
        "dragon_tiger_anomaly": "龙虎榜异动",
        "late_session_volume": "尾盘放量",
    }.get(event_type, event_type)


def _extract_amount_text(text: str) -> str:
    patterns = [
        r"(\d+(?:\.\d+)?\s*(?:亿元|亿|万元|万|元))",
        r"(\d+(?:\.\d+)?\s*(?:billion|million|RMB|CNY|USD))",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean(match.group(1), 80)
    return ""


def _extract_counterparties(text: str) -> list[str]:
    patterns = [
        r"与([\u4e00-\u9fffA-Za-z0-9（）()·,\-]{2,40})(?:签订|合作|达成|共同)",
        r"向([\u4e00-\u9fffA-Za-z0-9（）()·,\-]{2,40})(?:发行|出售|销售|供应)",
    ]
    out: list[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, text):
            token = _clean(match, 80).strip("，。、;； ")
            if token and token not in out:
                out.append(token)
            if len(out) >= 5:
                return out
    return out


def _stock_code_from_doc(doc: dict[str, Any]) -> str:
    for key in ("stock_code", "code", "symbol"):
        raw = _clean(doc.get(key), 40)
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == 6:
            return raw or digits
    return ""


def _bare_stock_code(value: Any) -> str:
    raw = _clean(value, 40)
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    return raw.split(".", 1)[0].strip()


def _clamp_float(value: Any, default: float = 0.0, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    return max(minimum, min(parsed, maximum))


def _list_strings(value: Any, *, limit: int = 8, item_limit: int = 120) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    out: list[str] = []
    for item in raw_items:
        token = _clean(item, item_limit)
        if token and token not in out:
            out.append(token)
        if len(out) >= limit:
            break
    return out


def _source_doc_uids_for_doc(doc: dict[str, Any]) -> list[str]:
    return [token for token in [_clean(doc.get("doc_uid") or doc.get("event_id"), 240)] if token]


def _is_positive_direction(extraction: dict[str, Any]) -> bool:
    return str(extraction.get("direction") or "").lower() in {"positive", "up", "bullish"}


def _is_negative_direction(extraction: dict[str, Any]) -> bool:
    return str(extraction.get("direction") or "").lower() in {"negative", "down", "bearish"}


def _extract_payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
    else:
        data = payload
    if isinstance(data, dict):
        for key in ("items", "rows", "data", "results", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
        if any(key in data for key in ("code", "name", "mainNetInflow", "netAmount", "total")):
            return [dict(data)]
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, dict)]
    return []


def _numeric_from_keys(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            cleaned = value.replace(",", "").replace("，", "").replace("%", "").strip()
            if not cleaned:
                continue
            value = cleaned
        try:
            return float(value)
        except Exception:
            continue
    return None


@dataclass(frozen=True)
class RadarExtraction:
    event_type: str
    direction: str
    importance_score: float
    sentiment_score: float
    themes: list[str]
    amount_text: str
    counterparties: list[str]
    risk_flags: list[str]
    summary: str
    confidence: float
    source_doc_uids: list[str]
    llm_status: str = "rules_only"
    status: str = "provisional"

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "direction": self.direction,
            "importance_score": round(float(self.importance_score), 4),
            "sentiment_score": round(float(self.sentiment_score), 4),
            "themes": list(self.themes),
            "amount_text": self.amount_text,
            "counterparties": list(self.counterparties),
            "risk_flags": list(self.risk_flags),
            "summary": self.summary,
            "confidence": round(float(self.confidence), 4),
            "source_doc_uids": list(self.source_doc_uids),
            "llm_status": self.llm_status,
            "status": self.status,
        }


def extract_radar_event(doc: dict[str, Any]) -> RadarExtraction | None:
    title = _clean(doc.get("title") or doc.get("event_name"), 300)
    summary = _clean(doc.get("summary") or doc.get("body") or doc.get("content") or title, 1200)
    text = f"{title} {summary}"
    if not text.strip():
        return None
    source_doc_uids = [token for token in [_clean(doc.get("doc_uid") or doc.get("event_id"), 240)] if token]

    risk_hits: list[tuple[str, float]] = []
    for event_type, keywords, importance in RISK_RULES:
        if any(keyword and keyword in text for keyword in keywords):
            risk_hits.append((event_type, importance))
    if risk_hits:
        event_type, importance = max(risk_hits, key=lambda item: item[1])
        risk_flags = [_event_type_name(item[0]) for item in risk_hits]
        return RadarExtraction(
            event_type=event_type,
            direction="negative",
            importance_score=importance,
            sentiment_score=-0.75,
            themes=[_event_type_name(event_type)],
            amount_text=_extract_amount_text(text),
            counterparties=_extract_counterparties(text),
            risk_flags=risk_flags,
            summary=summary or title,
            confidence=min(0.92, 0.6 + 0.08 * len(risk_hits)),
            source_doc_uids=source_doc_uids,
        )

    positive_hits: list[tuple[str, tuple[str, ...], float]] = []
    for event_type, keywords, themes, importance in POSITIVE_RULES:
        if any(keyword and keyword in text for keyword in keywords):
            positive_hits.append((event_type, themes, importance))
    if not positive_hits:
        return None
    has_stock_entity = bool(_stock_code_from_doc(doc))
    policy_hit = next((item for item in positive_hits if item[0] == "policy_news"), None)
    if not has_stock_entity and policy_hit is not None:
        event_type, themes, importance = policy_hit
    else:
        event_type, themes, importance = max(positive_hits, key=lambda item: item[2])
    all_themes: list[str] = []
    for _, hit_themes, _ in positive_hits:
        for theme in hit_themes:
            if theme not in all_themes:
                all_themes.append(theme)
    direction = _direction_for_event(event_type)
    return RadarExtraction(
        event_type=event_type,
        direction=direction,
        importance_score=importance,
        sentiment_score=0.66 if direction == "positive" else 0.1,
        themes=all_themes or [_event_type_name(event_type)],
        amount_text=_extract_amount_text(text),
        counterparties=_extract_counterparties(text),
        risk_flags=[],
        summary=summary or title,
        confidence=min(0.9, 0.58 + 0.08 * len(positive_hits)),
        source_doc_uids=source_doc_uids,
    )


def _unwrap_llm_extraction(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    for key in ("results", "event", "events", "extraction", "radar_event"):
        value = raw.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            first = next((item for item in value if isinstance(item, dict)), None)
            if first:
                return dict(first)
    return raw


def _validated_llm_extraction(raw: dict[str, Any], fallback: RadarExtraction, doc: dict[str, Any]) -> dict[str, Any] | None:
    payload = _unwrap_llm_extraction(raw)
    if not payload:
        return None
    fallback_payload = fallback.as_dict()
    event_type = _clean(payload.get("event_type") or payload.get("type") or fallback.event_type, 120)
    if event_type not in RADAR_EVENT_TYPES:
        event_type = fallback.event_type
    direction = _clean(payload.get("direction") or fallback.direction, 40).lower()
    if direction not in {"positive", "negative", "neutral"}:
        direction = _direction_for_event(event_type)
    importance_score = _clamp_float(payload.get("importance_score"), fallback.importance_score)
    sentiment_score = _clamp_float(payload.get("sentiment_score"), fallback.sentiment_score, minimum=-1.0, maximum=1.0)
    confidence = _clamp_float(payload.get("confidence"), fallback.confidence)
    themes = _list_strings(payload.get("themes"), limit=8, item_limit=80) or list(fallback.themes)
    risk_flags = _list_strings(payload.get("risk_flags"), limit=8, item_limit=120) or list(fallback.risk_flags)
    if direction == "negative" and not risk_flags:
        risk_flags = [_event_type_name(event_type)]
    source_uids = _list_strings(payload.get("source_doc_uids"), limit=8, item_limit=240) or fallback.source_doc_uids
    for uid in _source_doc_uids_for_doc(doc):
        if uid not in source_uids:
            source_uids.append(uid)
    summary = _clean(payload.get("summary") or fallback.summary, 1200)
    return {
        **fallback_payload,
        "event_type": event_type,
        "direction": direction,
        "importance_score": importance_score,
        "sentiment_score": sentiment_score,
        "themes": themes,
        "amount_text": _clean(payload.get("amount_text") or fallback.amount_text, 160),
        "counterparties": _list_strings(payload.get("counterparties"), limit=8, item_limit=120) or list(fallback.counterparties),
        "risk_flags": risk_flags,
        "summary": summary,
        "confidence": confidence,
        "source_doc_uids": source_uids,
        "llm_status": "ok",
        "status": "verified" if confidence >= 0.75 else "extracted",
    }


async def enhance_radar_event_with_llm(
    doc: dict[str, Any],
    fallback: RadarExtraction,
    *,
    allow_llm: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not allow_llm:
        extraction = fallback.as_dict()
        extraction["llm_status"] = "unavailable"
        extraction["status"] = "provisional"
        return extraction, {"status": "unavailable", "reason": "llm_disabled_by_network_policy"}

    try:
        from ..strategy_llm_provider import StrategyLLMRequestError, get_strategy_llm_provider
    except Exception as exc:
        extraction = fallback.as_dict()
        extraction["llm_status"] = "unavailable"
        extraction["status"] = "provisional"
        return extraction, {"status": "unavailable", "reason": "llm_provider_import_failed", "error": f"{type(exc).__name__}: {exc}"}

    provider = get_strategy_llm_provider()
    if not provider.is_enabled():
        extraction = fallback.as_dict()
        extraction["llm_status"] = "unavailable"
        extraction["status"] = "provisional"
        return extraction, {"status": "unavailable", "reason": "llm_provider_disabled"}

    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    pdf_meta = metadata.get("radar_pdf_parse") if isinstance(metadata.get("radar_pdf_parse"), dict) else {}
    pdf_text = _clean(pdf_meta.get("text"), LLM_BODY_LIMIT)
    body = _clean(doc.get("body") or doc.get("content") or doc.get("summary") or "", LLM_BODY_LIMIT)
    input_data = {
        "schema": "RadarEventExtraction",
        "allowed_event_types": sorted(RADAR_EVENT_TYPES),
        "title": _clean(doc.get("title") or doc.get("event_name"), 300),
        "summary": _clean(doc.get("summary"), 1200),
        "body": body,
        "pdf_text": pdf_text,
        "stock_code": _stock_code_from_doc(doc),
        "source": doc.get("source"),
        "source_tier": _source_tier(doc.get("source_tier")),
        "url": doc.get("url"),
        "rule_extraction": fallback.as_dict(),
        "requirements": {
            "fields": [
                "event_type",
                "direction",
                "importance_score",
                "sentiment_score",
                "themes",
                "amount_text",
                "counterparties",
                "risk_flags",
                "summary",
                "confidence",
                "source_doc_uids",
            ],
            "no_trade_instructions": True,
        },
    }
    system_prompt = (
        "You extract structured A-share disclosure/news events for an observation-only stock radar. "
        "Return JSON only. Do not include buy/sell/hold instructions. Keep evidence concise and respect source text."
    )
    try:
        raw = await provider.call_stage(
            stage_id="stock_radar_event_extraction",
            input_data=input_data,
            system_prompt=system_prompt,
            max_tokens=700,
            temperature=0.1,
            timeout_sec=float(os.getenv("AIASK_RADAR_LLM_TIMEOUT_SEC", "20") or 20),
        )
        validated = _validated_llm_extraction(raw, fallback, doc)
        if validated is None:
            extraction = fallback.as_dict()
            extraction["llm_status"] = "failed"
            extraction["status"] = "provisional"
            return extraction, {"status": "failed", "reason": "llm_output_invalid", "raw_keys": sorted(raw.keys()) if isinstance(raw, dict) else []}
        return validated, {"status": "ok"}
    except StrategyLLMRequestError as exc:
        extraction = fallback.as_dict()
        extraction["llm_status"] = "unavailable"
        extraction["status"] = "provisional"
        return extraction, {"status": "unavailable", "reason": "llm_request_failed", "metrics": dict(getattr(exc, "metrics", {}) or {})}
    except Exception as exc:
        extraction = fallback.as_dict()
        extraction["llm_status"] = "failed"
        extraction["status"] = "provisional"
        return extraction, {"status": "failed", "reason": "llm_exception", "error": f"{type(exc).__name__}: {exc}"}


def score_radar_candidate(
    *,
    extraction: dict[str, Any],
    source_tier: str,
    confirmations: dict[str, Any] | None = None,
    risk_flags: list[str] | None = None,
) -> dict[str, Any]:
    confirmations = dict(confirmations or {})
    risks = list(risk_flags or extraction.get("risk_flags") or [])
    event_importance = max(0.0, min(float(extraction.get("importance_score") or 0.0), 1.0)) * 40
    source_score = TIER_WEIGHTS.get(_source_tier(source_tier), 0.45) * 20

    fund_confirmed = bool(confirmations.get("fund_flow", {}).get("confirmed"))
    north_confirmed = bool(confirmations.get("north_fund", {}).get("confirmed"))
    dragon_confirmed = bool(confirmations.get("dragon_tiger", {}).get("confirmed"))
    capital_score = (8 if fund_confirmed else 0) + (5 if north_confirmed else 0) + (7 if dragon_confirmed else 0)
    capital_score = min(capital_score, 20)

    sector_heat = confirmations.get("sector_heat", {})
    try:
        sector_score = max(0.0, min(float(sector_heat.get("score") or 0.0), 1.0)) * 15
    except Exception:
        sector_score = 0.0

    risk_penalty = min(30.0, 10.0 * len(risks))
    for key in ("fund_flow", "north_fund", "dragon_tiger"):
        item = confirmations.get(key)
        if isinstance(item, dict) and item.get("conflict"):
            risk_penalty = min(30.0, risk_penalty + 5.0)
    if str(extraction.get("direction") or "").lower() in {"negative", "down", "bearish"}:
        risk_penalty = min(30.0, max(risk_penalty, 18.0))
    if str(extraction.get("status") or "").lower() == "provisional" and str(extraction.get("llm_status") or "") == "unavailable":
        risk_penalty = min(30.0, risk_penalty + 8.0)

    score = max(0.0, min(100.0, event_importance + source_score + capital_score + sector_score - risk_penalty))
    if score >= 80:
        tier = "alert"
    elif score >= 60:
        tier = "watch"
    elif score >= 40:
        tier = "observe"
    else:
        tier = "reject"
    return {
        "radar_score": round(score, 2),
        "tier": tier,
        "component_scores": {
            "event_importance": round(event_importance, 2),
            "source_credibility": round(source_score, 2),
            "capital_confirmation": round(capital_score, 2),
            "sector_heat": round(sector_score, 2),
            "risk_penalty": round(risk_penalty, 2),
        },
    }


def _high_confidence_candidate(candidate: dict[str, Any]) -> bool:
    extraction = candidate.get("extraction") if isinstance(candidate.get("extraction"), dict) else {}
    llm_status = str(extraction.get("llm_status") or "").strip().lower()
    status = str(extraction.get("status") or candidate.get("status") or "").strip().lower()
    try:
        confidence = float(extraction.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    if llm_status in {"", "unavailable", "rules_only", "failed", "fallback"}:
        return False
    if status in {"", "provisional", "degraded", "rejected"}:
        return False
    return confidence >= 0.75
