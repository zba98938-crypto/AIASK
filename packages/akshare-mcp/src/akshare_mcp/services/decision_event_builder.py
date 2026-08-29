"""Event/text context builder for unified decision."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from ..services.document_index import build_document_index
from ..services.event_extraction import extract_events
from ..services.llm_alpha import TextSignalPipeline
from ..tools.news.news_feed import get_stock_news
from ..tools.news.notices import get_stock_notices
from ..tools.news.research import get_research_reports
from ..utils import resolve_security_code
from .decision_pipeline_shared import build_context_meta, clamp, unique_texts


EVENT_VETO_RULES: dict[str, dict[str, Any]] = {
    "regulatory_investigation": {
        "keywords": ("立案", "调查", "被查", "留置", "处罚", "涉嫌违法", "监管函", "证监会"),
        "severity": "critical",
        "direction": "bearish",
        "horizon": "mid",
        "candidate_actions": ["暂停新开仓", "核查公告原文", "等待监管结论"],
        "hard_veto": True,
    },
    "st_or_delist": {
        "keywords": ("ST", "*ST", "退市", "终止上市", "退市风险警示"),
        "severity": "critical",
        "direction": "bearish",
        "horizon": "long",
        "candidate_actions": ["回避新增仓位", "评估退市风险", "确认交易限制"],
        "hard_veto": True,
    },
    "trading_suspension": {
        "keywords": ("停牌", "临时停牌", "停牌核查"),
        "severity": "high",
        "direction": "bearish",
        "horizon": "short",
        "candidate_actions": ["等待复牌信息", "关注停牌原因", "避免流动性误判"],
        "hard_veto": True,
    },
    "earnings_blowup": {
        "keywords": ("爆雷", "预亏", "亏损", "下修", "减值", "大幅下滑", "业绩修正"),
        "severity": "high",
        "direction": "bearish",
        "horizon": "mid",
        "candidate_actions": ["下调预期收益", "复核估值模型", "关注业绩修复时点"],
        "hard_veto": False,
    },
    "positive_catalyst": {
        "keywords": ("预增", "扭亏", "超预期", "回购", "增持", "中标", "获批", "签约"),
        "severity": "medium",
        "direction": "bullish",
        "horizon": "mid",
        "candidate_actions": ["跟踪兑现节奏", "观察二次确认信号"],
        "hard_veto": False,
    },
}


def _pick_text(item: dict[str, Any]) -> str:
    for key in ("text", "content", "summary", "title", "headline"):
        value = item.get(key)
        if value:
            return str(value).strip()
    return ""


async def _call_text_source(tool, *args, timeout_sec: float = 12.0) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(tool, *args), timeout=timeout_sec)
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": f"timeout>{float(timeout_sec):.1f}s",
            "data": None,
            "cached": False,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "data": None, "cached": False}


def _classify_event_candidates(documents: list[dict[str, Any]]) -> dict[str, Any]:
    veto_candidates: list[dict[str, Any]] = []
    evidence_links: list[dict[str, Any]] = []
    candidate_actions: list[str] = []
    negative_hits = 0
    positive_hits = 0
    level_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    max_level = "low"
    dominant_horizon = "short"

    for doc in documents:
        text = " ".join([str(doc.get("title") or ""), str(doc.get("text") or "")]).strip()
        if not text:
            continue
        for label, rule in EVENT_VETO_RULES.items():
            hits = [keyword for keyword in rule["keywords"] if keyword in text]
            if not hits:
                continue
            severity = str(rule["severity"])
            if level_rank[severity] > level_rank[max_level]:
                max_level = severity
            dominant_horizon = str(rule["horizon"])
            direction = str(rule["direction"])
            if direction == "bearish":
                negative_hits += len(hits)
            else:
                positive_hits += len(hits)
            candidate_actions.extend(list(rule.get("candidate_actions") or []))
            evidence = {
                "category": label,
                "severity": severity,
                "direction": direction,
                "hard_veto": bool(rule.get("hard_veto")),
                "keywords": hits,
                "doc_id": doc.get("doc_id"),
                "title": doc.get("title"),
                "date": doc.get("date"),
                "source": doc.get("source"),
                "summary": text[:140],
            }
            veto_candidates.append(evidence)
            evidence_links.append(
                {
                    "doc_id": doc.get("doc_id"),
                    "title": doc.get("title"),
                    "date": doc.get("date"),
                    "source": doc.get("source"),
                    "doc_type": doc.get("doc_type"),
                    "category": label,
                }
            )

    hard_veto_eligible = any(bool(item.get("hard_veto")) for item in veto_candidates)
    if hard_veto_eligible:
        direction = "bearish"
    elif positive_hits > negative_hits:
        direction = "bullish"
    elif negative_hits > positive_hits:
        direction = "bearish"
    else:
        direction = "neutral"

    intensity = "low"
    total_hits = positive_hits + negative_hits
    if max_level == "critical":
        intensity = "critical"
    elif max_level == "high" or total_hits >= 4:
        intensity = "high"
    elif total_hits >= 2:
        intensity = "medium"

    return {
        "event_direction": direction,
        "event_intensity": intensity,
        "event_level": max_level,
        "event_horizon": dominant_horizon,
        "candidate_actions": unique_texts(candidate_actions),
        "hard_veto_eligible": hard_veto_eligible,
        "veto_candidates": veto_candidates[:8],
        "evidence_links": evidence_links[:12],
    }


def _build_persistable_headline_labels(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .event_extraction import extract_events
    from .sentiment import SentimentAnalyzer

    labels: list[dict[str, Any]] = []
    for doc in documents:
        title = str(doc.get("title") or "").strip()
        if not title:
            continue
        extraction = extract_events([{"title": title, "text": _pick_text(doc)}], top_n=1)
        event_tags = list(extraction.get("event_tags") or [])
        event_type = str(event_tags[0].get("tag") or "").strip() if event_tags else None
        label = SentimentAnalyzer._classify_headline(title)
        labels.append(
            {
                "doc_uid": doc.get("doc_uid"),
                "headline": title,
                "label": label,
                "event_type": event_type,
                "direction": "up" if label == "bullish" else "down" if label == "bearish" else "flat",
                "horizon_days": 5 if str(doc.get("doc_type") or doc.get("type") or "").lower() == "news" else 10,
                "keywords": list(dict(extraction.get("keyword_hits") or {}).keys())[:8],
                "published_at": doc.get("date"),
            }
        )
    return labels[:20]


async def build_event_context(
    code: str,
    *,
    news_limit: int = 12,
    notice_days: int = 30,
    report_limit: int = 6,
) -> dict[str, Any]:
    """Aggregate news/notices/research into a structured event context."""
    normalized_code = resolve_security_code(code)
    if not normalized_code:
        raise ValueError("需要提供股票代码")

    warnings: list[str] = []
    fallback_reasons: list[str] = []
    source_chain = [
        "decision_event_builder",
        "news.get_stock_news",
        "news.get_stock_notices",
        "news.get_research_reports",
        "services.llm_alpha.TextSignalPipeline",
        "services.event_extraction",
    ]
    news_items: list[dict[str, Any]] = []
    notice_items: list[dict[str, Any]] = []
    report_items: list[dict[str, Any]] = []

    end_date = date.today()
    start_date = end_date - timedelta(days=max(1, int(notice_days)))

    news_resp, notice_resp, report_resp = await asyncio.gather(
        _call_text_source(get_stock_news, normalized_code, max(1, int(news_limit)), timeout_sec=12.0),
        _call_text_source(
            get_stock_notices,
            start_date.isoformat(),
            end_date.isoformat(),
            ["全部"],
            normalized_code,
            timeout_sec=12.0,
        ),
        _call_text_source(get_research_reports, normalized_code, "", max(1, int(report_limit)), timeout_sec=12.0),
    )

    if news_resp.get("success") and isinstance(news_resp.get("data"), list):
        news_items = [dict(item) for item in (news_resp.get("data") or []) if isinstance(item, dict)]
    else:
        message = str(news_resp.get("error", "unknown"))
        warnings.append(f"stock_news:{message}")
        fallback_reasons.append(f"stock_news:{message}")

    if notice_resp.get("success") and isinstance(notice_resp.get("data"), dict):
        notice_items = [
            dict(item) for item in (notice_resp.get("data", {}).get("events") or []) if isinstance(item, dict)
        ]
    else:
        message = str(notice_resp.get("error", "unknown"))
        warnings.append(f"stock_notices:{message}")
        fallback_reasons.append(f"stock_notices:{message}")

    if report_resp.get("success"):
        report_data = report_resp.get("data")
        if isinstance(report_data, dict):
            report_items = [dict(item) for item in (report_data.get("reports") or []) if isinstance(item, dict)]
        elif isinstance(report_data, list):
            report_items = [dict(item) for item in report_data if isinstance(item, dict)]
    else:
        message = str(report_resp.get("error", "unknown"))
        warnings.append(f"research_reports:{message}")
        fallback_reasons.append(f"research_reports:{message}")

    merged_items: list[dict[str, Any]] = []
    for item in news_items[: max(1, int(news_limit))]:
        merged_items.append(
            {
                "type": "news",
                "date": item.get("date"),
                "title": item.get("title") or item.get("headline") or "",
                "source": item.get("source") or "stock_news",
                "text": _pick_text(item),
                "url": item.get("url"),
            }
        )
    for item in notice_items[: max(4, int(news_limit))]:
        merged_items.append(
            {
                "type": "notice",
                "date": item.get("date"),
                "title": item.get("title") or item.get("name") or "",
                "source": item.get("source") or "stock_notices",
                "text": _pick_text(item),
                "url": item.get("url"),
            }
        )
    for item in report_items[: max(1, int(report_limit))]:
        report_text = " ".join(
            str(value).strip()
            for value in [item.get("title"), item.get("rating"), item.get("institution")]
            if value
        ).strip()
        merged_items.append(
            {
                "type": "research",
                "date": item.get("date"),
                "title": item.get("title") or "",
                "source": item.get("institution") or "research_reports",
                "text": report_text,
                "rating": item.get("rating"),
                "target_price": item.get("targetPrice"),
                "url": item.get("url"),
            }
        )

    document_index = build_document_index(merged_items)
    documents = document_index.get("documents", [])
    texts = [item["text"] for item in documents if item.get("text")]
    text_signal = TextSignalPipeline.aggregate_signals(texts)
    extraction = extract_events(documents)
    event_tags = extraction.get("event_tags", [])
    event_summary = extraction.get("summary_counts", {})
    structured_events = _classify_event_candidates(documents)

    reasons: list[str] = []
    risks: list[str] = []
    score = 50.0 + float(text_signal.get("signal_score", 0.0) or 0.0) * 6.0

    sentiment = str(text_signal.get("sentiment") or "neutral")
    if sentiment == "bullish":
        score += 8.0
        reasons.append("文本信号整体偏利好")
    elif sentiment == "bearish":
        score -= 10.0
        risks.append("文本信号整体偏利空")

    for item in event_tags:
        tag = str(item.get("tag") or "")
        if tag == "业绩景气":
            score += 5.0
            reasons.append("近阶段出现业绩景气类事件")
        if tag == "资本运作":
            score += 3.0
            reasons.append("存在回购或增持等资本运作线索")
        if tag == "监管风险":
            score -= 18.0
            risks.append("近阶段出现监管风险或处罚类标签")

    event_direction = str(structured_events.get("event_direction") or sentiment)
    if event_direction == "bullish":
        score += 4.0
    elif event_direction == "bearish":
        score -= 6.0

    hard_risk = bool(structured_events.get("hard_veto_eligible"))
    if hard_risk:
        risks.append("结构化事件识别到强风险候选，需等待风险落地或解除")
        score -= 18.0

    event_intensity = str(structured_events.get("event_intensity") or "low")
    if event_intensity == "critical":
        score -= 8.0
    elif event_intensity == "high" and event_direction == "bearish":
        score -= 4.0

    if not risks and event_direction == "bullish":
        reasons.append("结构化事件识别显示催化剂方向偏正面")

    score = round(clamp(score, 0.0, 100.0), 2)
    meta = build_context_meta(
        source="event_context",
        source_chain=source_chain,
        asof_value=document_index.get("stats", {}).get("latest_dates", [None])[0],
        warnings=warnings,
        fallback_reason=fallback_reasons,
        missing_fields=["documents"] if not documents else [],
        degraded=bool(warnings or not documents),
        cached=bool(news_resp.get("cached") or notice_resp.get("cached") or report_resp.get("cached")),
    )
    return {
        "code": normalized_code,
        "score": score,
        "sentiment": sentiment,
        "signal_score": float(text_signal.get("signal_score", 0.0) or 0.0),
        "positive_count": int(text_signal.get("positive_count", 0) or 0),
        "negative_count": int(text_signal.get("negative_count", 0) or 0),
        "evidence": text_signal.get("evidence", []),
        "event_tags": event_tags,
        "event_summary": {
            **event_summary,
            "news_count": len(news_items),
            "notice_count": len(notice_items),
            "report_count": len(report_items),
        },
        "event_direction": event_direction,
        "event_intensity": event_intensity,
        "event_horizon": structured_events.get("event_horizon"),
        "event_level": structured_events.get("event_level"),
        "candidate_actions": structured_events.get("candidate_actions", []),
        "hard_veto_eligible": structured_events.get("hard_veto_eligible"),
        "veto_candidates": structured_events.get("veto_candidates", []),
        "evidence_links": structured_events.get("evidence_links", []),
        "headline_labels": _build_persistable_headline_labels(documents),
        "hard_risk": hard_risk,
        "reasons": unique_texts(reasons),
        "risks": unique_texts(risks),
        "raw_texts": documents[:10],
        **meta,
    }
