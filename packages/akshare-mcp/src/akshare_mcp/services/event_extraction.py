"""Rule-based event extraction shared by text-oriented MCP tools."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

EVENT_RULES: dict[str, tuple[str, ...]] = {
    "业绩景气": ("业绩", "增长", "盈利", "预增", "扭亏", "超预期"),
    "订单合同": ("中标", "签约", "订单", "合同"),
    "资本运作": ("回购", "增持", "减持", "定增", "融资"),
    "产品技术": ("突破", "创新", "发布", "获批", "研发"),
    "监管风险": ("处罚", "违规", "诉讼", "质押", "ST", "退市", "风险"),
}

ENTITY_PATTERNS: dict[str, str] = {
    "stock_code": r"\b\d{6}\b",
    "percentage": r"\b\d+(?:\.\d+)?%\b",
}


def _normalize_texts(items: list[Any]) -> list[str]:
    texts: list[str] = []
    for item in list(items or []):
        if isinstance(item, dict):
            value = item.get("text") or item.get("content") or item.get("summary") or item.get("title")
        else:
            value = item
        text = str(value or "").strip()
        if text:
            texts.append(text)
    return texts


def extract_events(items: list[Any], *, top_n: int = 8) -> dict[str, Any]:
    texts = _normalize_texts(items)
    matched_docs = 0
    event_counter: Counter[str] = Counter()
    keyword_hits: Counter[str] = Counter()
    samples: dict[str, str] = {}

    for text in texts:
        matched = False
        for tag, keywords in EVENT_RULES.items():
            hits = [keyword for keyword in keywords if keyword in text]
            if not hits:
                continue
            matched = True
            event_counter[tag] += 1
            for keyword in hits:
                keyword_hits[keyword] += 1
            samples.setdefault(tag, text[:120])
        if matched:
            matched_docs += 1

    entities: dict[str, list[str]] = {}
    for entity_type, pattern in ENTITY_PATTERNS.items():
        values: list[str] = []
        for text in texts:
            values.extend(re.findall(pattern, text))
        entities[entity_type] = sorted(set(values))[:20]

    events = [
        {
            "tag": tag,
            "count": count,
            "sample_text": samples.get(tag),
        }
        for tag, count in event_counter.most_common(max(1, int(top_n)))
    ]

    return {
        "events": events,
        "event_tags": [{"tag": item["tag"], "count": item["count"]} for item in events],
        "entities": entities,
        "keyword_hits": dict(keyword_hits.most_common(20)),
        "summary_counts": {
            "documents": len(texts),
            "matched_documents": matched_docs,
            "unique_event_types": len(event_counter),
        },
    }
