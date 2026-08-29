"""Chinese financial semantic analysis service with model-first fallback."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

import httpx

from ..env_loader import load_mcp_env

_POSITIVE_KEYWORDS = {
    "利好": 1.1,
    "超预期": 1.3,
    "增长": 0.8,
    "回购": 0.8,
    "增持": 0.9,
    "中标": 0.8,
    "签约": 0.7,
    "获批": 0.9,
    "分红": 0.5,
    "扭亏": 1.1,
    "景气": 0.6,
    "提价": 0.8,
}
_NEGATIVE_KEYWORDS = {
    "利空": 1.1,
    "低于预期": 1.2,
    "亏损": 1.0,
    "减持": 0.9,
    "处罚": 1.0,
    "违规": 1.1,
    "诉讼": 0.8,
    "退市": 1.4,
    "爆雷": 1.5,
    "下调": 0.8,
    "质押": 0.5,
    "风险": 0.5,
}
_SURPRISE_KEYWORDS = {
    "超预期": 1.0,
    "不及预期": 0.8,
    "大超": 1.0,
    "首次": 0.6,
    "创历史新高": 0.9,
    "突发": 0.8,
}
_EVENT_KEYWORDS = {
    "earnings": ["业绩", "利润", "营收", "预告", "快报", "财报", "扭亏"],
    "capital": ["回购", "增持", "减持", "定增", "融资", "配股", "可转债"],
    "order": ["订单", "中标", "合同", "签约", "客户"],
    "product": ["发布", "获批", "研发", "技术", "专利", "量产"],
    "regulation": ["处罚", "监管", "问询", "调查", "违规", "诉讼", "立案"],
    "macro": ["政策", "财政", "货币", "降准", "降息", "补贴"],
}
_RISK_KEYWORDS = {
    "regulation_risk": ["处罚", "监管", "问询", "调查", "违规", "立案"],
    "governance_risk": ["减持", "质押", "关联交易", "控制权", "失联"],
    "funding_risk": ["亏损", "融资", "债务", "现金流", "违约", "爆雷"],
    "execution_risk": ["延期", "不及预期", "下滑", "终止", "失败"],
}
_SOURCE_CREDIBILITY = {
    "notice": 0.95,
    "announcement": 0.95,
    "research": 0.82,
    "news": 0.68,
    "media": 0.68,
    "self_media": 0.35,
}


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        fallback = float(default)
    except Exception:
        fallback = 0.0
    if not math.isfinite(fallback):
        fallback = 0.0
    try:
        if value in (None, ""):
            return fallback
        parsed = float(value)
    except Exception:
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def _parse_iso_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for parser in (
        lambda text: date.fromisoformat(text[:10]),
        lambda text: datetime.fromisoformat(text.replace("Z", "+00:00")).date(),
    ):
        try:
            return parser(raw)
        except Exception:
            continue
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 8:
        try:
            return date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
        except Exception:
            return None
    return None


def _json_loads_loose(text: str) -> dict[str, Any]:
    content = str(text or "").strip()
    if not content:
        return {}
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", content, flags=re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(slots=True)
class FinancialSemanticConfig:
    enabled: bool = False
    provider: str = "rule_based"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_sec: float = 20.0
    connect_timeout_sec: float = 5.0
    write_timeout_sec: float = 10.0
    pool_timeout_sec: float = 5.0
    max_docs: int = 20
    max_text_chars: int = 1600
    temperature: float = 0.1

    @classmethod
    def from_env(cls) -> "FinancialSemanticConfig":
        load_mcp_env(
            override=False,
            only_prefixes=("FINANCIAL_SEMANTIC_", "STRATEGY_LLM_"),
        )

        def _env_text(name: str, *fallbacks: str, default: str = "") -> str:
            for key in (name, *fallbacks):
                raw = str(os.getenv(key, "") or "").strip()
                if raw:
                    return raw
            return str(default or "").strip()

        def _env_float(name: str, *fallbacks: str, default: float) -> float:
            raw = _env_text(name, *fallbacks, default=str(default))
            return _safe_float(raw, default)

        def _env_bool(name: str, *fallbacks: str, default: bool) -> bool:
            for key in (name, *fallbacks):
                raw = os.getenv(key)
                if raw is None:
                    continue
                return str(raw).strip().lower() in {"1", "true", "yes", "on"}
            return bool(default)

        timeout_sec = max(5.0, _env_float("FINANCIAL_SEMANTIC_TIMEOUT_SEC", default=20.0))
        provider = _env_text("FINANCIAL_SEMANTIC_PROVIDER", default="rule_based").lower() or "rule_based"
        return cls(
            enabled=_env_bool("FINANCIAL_SEMANTIC_ENABLED", default=provider == "rule_based"),
            provider=provider,
            base_url=_env_text("FINANCIAL_SEMANTIC_BASE_URL", "STRATEGY_LLM_BASE_URL"),
            api_key=_env_text("FINANCIAL_SEMANTIC_API_KEY", "STRATEGY_LLM_API_KEY"),
            model=_env_text("FINANCIAL_SEMANTIC_MODEL", "STRATEGY_LLM_MODEL"),
            timeout_sec=timeout_sec,
            connect_timeout_sec=max(1.0, _env_float("FINANCIAL_SEMANTIC_CONNECT_TIMEOUT_SEC", default=min(timeout_sec, 5.0))),
            write_timeout_sec=max(1.0, _env_float("FINANCIAL_SEMANTIC_WRITE_TIMEOUT_SEC", default=min(timeout_sec, 10.0))),
            pool_timeout_sec=max(1.0, _env_float("FINANCIAL_SEMANTIC_POOL_TIMEOUT_SEC", default=min(timeout_sec, 5.0))),
            max_docs=max(1, min(int(_env_float("FINANCIAL_SEMANTIC_MAX_DOCS", default=20)), 50)),
            max_text_chars=max(256, min(int(_env_float("FINANCIAL_SEMANTIC_MAX_TEXT_CHARS", default=1600)), 4000)),
            temperature=_clip(_env_float("FINANCIAL_SEMANTIC_TEMPERATURE", default=0.1), 0.0, 1.0),
        )

    def remote_enabled(self) -> bool:
        if not self.enabled:
            return False
        if self.provider == "rule_based":
            return False
        return bool(self.base_url and self.model)


class FinancialSemanticService:
    def __init__(self, config: Optional[FinancialSemanticConfig] = None):
        self.config = config or FinancialSemanticConfig.from_env()
        self._client: httpx.AsyncClient | None = None

    def _client_is_closed(self) -> bool:
        client = self._client
        if client is None:
            return True
        try:
            return bool(getattr(client, "is_closed"))
        except Exception:
            return False

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client_is_closed():
            self._client = httpx.AsyncClient(follow_redirects=True, http2=False)
        return self._client

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.aclose()
        except Exception:
            pass

    def is_enabled(self) -> bool:
        return bool(self.config.enabled)

    def _timeout(self) -> httpx.Timeout:
        timeout = max(_safe_float(self.config.timeout_sec, 20.0), 5.0)
        return httpx.Timeout(
            connect=max(1.0, min(_safe_float(self.config.connect_timeout_sec, min(timeout, 5.0)), timeout)),
            read=timeout,
            write=max(1.0, min(_safe_float(self.config.write_timeout_sec, min(timeout, 10.0)), timeout)),
            pool=max(1.0, min(_safe_float(self.config.pool_timeout_sec, min(timeout, 5.0)), timeout)),
        )

    def _normalize_documents(self, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for index, raw in enumerate(list(documents or [])[: self.config.max_docs]):
            if not isinstance(raw, dict):
                continue
            text = " ".join(
                str(raw.get(field) or "").strip()
                for field in ("title", "text", "content", "summary")
                if str(raw.get(field) or "").strip()
            ).strip()
            if not text:
                continue
            normalized.append(
                {
                    "index": index,
                    "type": str(raw.get("type") or raw.get("doc_type") or "news").strip().lower() or "news",
                    "source": str(raw.get("source") or "unknown").strip().lower() or "unknown",
                    "date": str(raw.get("date") or raw.get("published_at") or "").strip() or None,
                    "title": str(raw.get("title") or "").strip(),
                    "text": text[: self.config.max_text_chars],
                }
            )
        return normalized

    @staticmethod
    def _source_credibility(doc_type: str, source: str) -> float:
        if doc_type in _SOURCE_CREDIBILITY:
            return float(_SOURCE_CREDIBILITY[doc_type])
        if "公告" in source or "notice" in source:
            return 0.95
        if "研究" in source or "report" in source:
            return 0.82
        return 0.65

    @staticmethod
    def _recency_weight(value: Any) -> float:
        parsed = _parse_iso_date(value)
        if parsed is None:
            return 0.85
        age_days = max((date.today() - parsed).days, 0)
        return round(math.exp(-age_days / 45.0), 4)

    def _rule_based_document(self, item: dict[str, Any]) -> dict[str, Any]:
        text = str(item.get("text") or "")
        positive = sum(weight * len(re.findall(re.escape(keyword), text)) for keyword, weight in _POSITIVE_KEYWORDS.items())
        negative = sum(weight * len(re.findall(re.escape(keyword), text)) for keyword, weight in _NEGATIVE_KEYWORDS.items())
        raw_sentiment = positive - negative
        entity_sentiment = round(math.tanh(raw_sentiment / 3.0), 4)
        event_sentiment = entity_sentiment
        surprise = _clip(
            sum(weight * len(re.findall(re.escape(keyword), text)) for keyword, weight in _SURPRISE_KEYWORDS.items()) / 3.0,
            0.0,
            1.0,
        )
        doc_type = str(item.get("type") or "news").strip().lower() or "news"
        source = str(item.get("source") or "unknown").strip().lower() or "unknown"
        credibility = self._source_credibility(doc_type, source)
        recency = self._recency_weight(item.get("date"))
        event_types = [
            name
            for name, keywords in _EVENT_KEYWORDS.items()
            if any(keyword in text for keyword in keywords)
        ]
        risk_tags = [
            name
            for name, keywords in _RISK_KEYWORDS.items()
            if any(keyword in text for keyword in keywords)
        ]
        return {
            "index": item.get("index"),
            "entity_sentiment": entity_sentiment,
            "event_sentiment": event_sentiment,
            "surprise": round(float(surprise), 4),
            "credibility": round(float(credibility), 4),
            "recency": round(float(recency), 4),
            "event_types": event_types,
            "risk_tags": risk_tags,
            "summary": text[:120],
        }

    async def _request_openai_compatible(self, documents: list[dict[str, Any]]) -> dict[str, Any]:
        base_url = self.config.base_url.rstrip("/")
        endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        payload = {
            "model": self.config.model,
            "temperature": _clip(_safe_float(self.config.temperature, 0.1), 0.0, 1.0),
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是中文财经语义分析器。请严格输出 JSON，对每条文档给出 "
                        "entity_sentiment(-1到1), event_sentiment(-1到1), surprise(0到1), "
                        "credibility(0到1), event_types(数组), risk_tags(数组), summary(不超过40字)。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "documents": documents,
                            "schema": {
                                "documents": [
                                    {
                                        "index": 0,
                                        "entity_sentiment": 0.0,
                                        "event_sentiment": 0.0,
                                        "surprise": 0.0,
                                        "credibility": 0.0,
                                        "event_types": ["earnings"],
                                        "risk_tags": ["regulation_risk"],
                                        "summary": "示例",
                                    }
                                ]
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        client = await self._ensure_client()
        response = await client.post(endpoint, headers=headers, json=payload, timeout=self._timeout())
        response.raise_for_status()
        body = response.json()
        choices = list(body.get("choices") or []) if isinstance(body, dict) else []
        message = ((choices[0] or {}).get("message") or {}) if choices else {}
        return _json_loads_loose(str(message.get("content") or ""))

    async def _request_ollama(self, documents: list[dict[str, Any]]) -> dict[str, Any]:
        base_url = self.config.base_url.rstrip("/")
        endpoint = base_url if base_url.endswith("/api/chat") else f"{base_url}/api/chat"
        payload = {
            "model": self.config.model,
            "stream": False,
            "format": "json",
            "messages": [
                {
                    "role": "system",
                    "content": "你是中文财经语义分析器，只输出合法 JSON。",
                },
                {
                    "role": "user",
                    "content": json.dumps({"documents": documents}, ensure_ascii=False),
                },
            ],
        }
        client = await self._ensure_client()
        response = await client.post(endpoint, json=payload, timeout=self._timeout())
        response.raise_for_status()
        body = response.json()
        message = body.get("message") if isinstance(body, dict) else {}
        return _json_loads_loose(str((message or {}).get("content") or ""))

    async def _remote_documents(self, documents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not self.config.remote_enabled():
            raise RuntimeError("financial semantic remote provider not configured")
        if self.config.provider == "openai_compatible":
            body = await self._request_openai_compatible(documents)
        elif self.config.provider == "ollama":
            body = await self._request_ollama(documents)
        else:
            raise RuntimeError(f"unsupported financial semantic provider: {self.config.provider}")
        rows = [dict(item or {}) for item in list(body.get("documents") or []) if isinstance(item, dict)]
        return rows, body

    @staticmethod
    def _aggregate_rows(rows: list[dict[str, Any]], *, provider: str, model: str, fallback_reason: str | None = None) -> dict[str, Any]:
        if not rows:
            return {
                "available": False,
                "provider": provider,
                "model": model,
                "fallback_reason": fallback_reason or "empty_rows",
                "documents": [],
            }
        weighted_sentiments = []
        weighted_events = []
        weighted_surprises = []
        credibility_values = []
        risk_counts: dict[str, int] = {}
        event_counts: dict[str, int] = {}
        output_documents: list[dict[str, Any]] = []
        for row in rows:
            credibility = _clip(_safe_float(row.get("credibility"), 0.6), 0.0, 1.0)
            recency = _clip(_safe_float(row.get("recency"), 0.85), 0.0, 1.0)
            entity = _clip(_safe_float(row.get("entity_sentiment")), -1.0, 1.0)
            event = _clip(_safe_float(row.get("event_sentiment")), -1.0, 1.0)
            surprise_value = _clip(_safe_float(row.get("surprise")), 0.0, 1.0)
            weight = max(credibility * recency, 1e-6)
            weighted_sentiments.append(weight * entity)
            weighted_events.append(weight * event)
            weighted_surprises.append(weight * surprise_value)
            credibility_values.append(credibility)
            sanitized_row = dict(row)
            sanitized_row["entity_sentiment"] = round(entity, 4)
            sanitized_row["event_sentiment"] = round(event, 4)
            sanitized_row["surprise"] = round(surprise_value, 4)
            sanitized_row["credibility"] = round(credibility, 4)
            sanitized_row["recency"] = round(recency, 4)
            output_documents.append(sanitized_row)
            for tag in list(row.get("risk_tags") or []):
                name = str(tag).strip()
                if name:
                    risk_counts[name] = int(risk_counts.get(name, 0)) + 1
            for tag in list(row.get("event_types") or []):
                name = str(tag).strip()
                if name:
                    event_counts[name] = int(event_counts.get(name, 0)) + 1
        denom = max(len(rows), 1)
        entity_sentiment = sum(weighted_sentiments) / denom
        event_sentiment = sum(weighted_events) / denom
        surprise = sum(weighted_surprises) / denom
        credibility = float(sum(credibility_values) / len(credibility_values)) if credibility_values else 0.0
        composite = _clip(entity_sentiment * 0.45 + event_sentiment * 0.35 + surprise * 0.2, -1.0, 1.0)
        sentiment_label = "bullish" if composite >= 0.15 else ("bearish" if composite <= -0.15 else "neutral")
        top_risks = sorted(risk_counts.items(), key=lambda item: (item[1], item[0]), reverse=True)
        top_events = sorted(event_counts.items(), key=lambda item: (item[1], item[0]), reverse=True)
        return {
            "available": True,
            "provider": provider,
            "model": model,
            "fallback_reason": fallback_reason,
            "sentiment": sentiment_label,
            "score": round((composite + 1.0) * 50.0, 2),
            "entity_sentiment": round(entity_sentiment, 4),
            "event_sentiment": round(event_sentiment, 4),
            "surprise": round(float(surprise), 4),
            "credibility": round(float(credibility), 4),
            "event_types": [{"tag": name, "count": count} for name, count in top_events[:8]],
            "risk_tags": [{"tag": name, "count": count} for name, count in top_risks[:8]],
            "documents": output_documents[:20],
        }

    async def analyze_documents(self, documents: list[dict[str, Any]]) -> dict[str, Any]:
        normalized = self._normalize_documents(documents)
        if not normalized:
            return {
                "available": False,
                "provider": self.config.provider,
                "model": self.config.model,
                "fallback_reason": "empty_documents",
                "documents": [],
            }
        if self.config.remote_enabled():
            try:
                remote_rows, _ = await self._remote_documents(normalized)
                by_index = {int(row.get("index")): dict(row) for row in remote_rows if row.get("index") is not None}
                merged_rows = []
                for item in normalized:
                    row = dict(by_index.get(int(item.get("index") or 0)) or {})
                    row.setdefault("index", item.get("index"))
                    row.setdefault("credibility", self._source_credibility(item.get("type", "news"), item.get("source", "unknown")))
                    row.setdefault("recency", self._recency_weight(item.get("date")))
                    row.setdefault("event_types", [])
                    row.setdefault("risk_tags", [])
                    row.setdefault("summary", item.get("title") or item.get("text", "")[:120])
                    merged_rows.append(row)
                return self._aggregate_rows(
                    merged_rows,
                    provider=self.config.provider,
                    model=self.config.model,
                )
            except Exception as exc:
                fallback_rows = []
                for item in normalized:
                    fallback_rows.append(self._rule_based_document(item))
                aggregated = self._aggregate_rows(
                    fallback_rows,
                    provider="rule_based",
                    model="keyword_finance_baseline",
                    fallback_reason=str(exc),
                )
                aggregated["fallback_used"] = True
                aggregated["remote_provider"] = self.config.provider
                aggregated["remote_model"] = self.config.model
                return aggregated
        fallback_rows = [self._rule_based_document(item) for item in normalized]
        return self._aggregate_rows(
            fallback_rows,
            provider="rule_based",
            model="keyword_finance_baseline",
        )


_financial_semantic_service: Optional[FinancialSemanticService] = None


def get_financial_semantic_service() -> FinancialSemanticService:
    global _financial_semantic_service
    if _financial_semantic_service is None:
        _financial_semantic_service = FinancialSemanticService()
    return _financial_semantic_service


async def close_financial_semantic_service() -> None:
    global _financial_semantic_service
    service = _financial_semantic_service
    _financial_semantic_service = None
    if service is None:
        return
    await service.close()


__all__ = [
    "FinancialSemanticConfig",
    "FinancialSemanticService",
    "close_financial_semantic_service",
    "get_financial_semantic_service",
]
