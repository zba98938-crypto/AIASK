"""SQLite adapter mixin for factor/decision text context storage."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from aiask_quant_core.vector_collection_scope import resolve_dimension_scoped_version, resolve_vector_collection_name
from ..strategy_factory_json_budget import bounded_json_text


logger = logging.getLogger(__name__)


class _BaseMixin:
    _SOURCE_TIER_ALIASES = {
        "a": "tier_a",
        "official": "tier_a",
        "official_disclosure": "tier_a",
        "tier_a": "tier_a",
        "b": "tier_b",
        "institutional": "tier_b",
        "paid": "tier_b",
        "tier_b": "tier_b",
        "c": "tier_c",
        "media": "tier_c",
        "open_media": "tier_c",
        "tier_c": "tier_c",
    }

    _SOURCE_TIER_DEFAULT_RELIABILITY = {
        "tier_a": 0.92,
        "tier_b": 0.82,
        "tier_c": 0.45,
    }

    _RADAR_JSON_FIELD_LIMITS = {
        "summary": 16 * 1024,
        "degraded_flags": 16 * 1024,
        "source_doc_uids": 16 * 1024,
        "source_chain": 24 * 1024,
        "extraction": 32 * 1024,
        "confirmations": 32 * 1024,
        "risk_flags": 16 * 1024,
        "metadata": 24 * 1024,
    }

    @staticmethod
    def _coerce_context_date(value: Any) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw[:10])
        except Exception:
            pass
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) >= 8:
            try:
                return date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
            except Exception:
                return None
        return None

    @staticmethod
    def _coerce_context_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clean_context_text(value: Any, *, max_len: int = 4000) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return text[:max_len]

    @classmethod
    def _normalize_source_tier(cls, value: Any, *, provider: Any = None, source: Any = None) -> str:
        raw = str(value or "").strip().lower()
        if raw in cls._SOURCE_TIER_ALIASES:
            return cls._SOURCE_TIER_ALIASES[raw]
        provider_text = str(provider or source or "").strip().lower()
        if any(token in provider_text for token in ("cninfo", "sse", "szse", "bse", "csrc", "巨潮", "上交所", "深交所", "北交所", "证监会")):
            return "tier_a"
        if any(token in provider_text for token in ("wind", "ifind", "choice", "tushare")):
            return "tier_b"
        return "tier_c"

    @classmethod
    def _default_reliability_score(cls, source_tier: str) -> float:
        return cls._SOURCE_TIER_DEFAULT_RELIABILITY.get(str(source_tier or "").strip().lower(), 0.35)

    @classmethod
    def _pick_document_provider(cls, item: dict[str, Any], *, source: str = "") -> str:
        return cls._clean_context_text(
            item.get("provider") or item.get("data_provider") or item.get("origin") or source,
            max_len=120,
        )

    @classmethod
    def _pick_document_original_id(cls, item: dict[str, Any]) -> str:
        return cls._clean_context_text(
            item.get("original_id")
            or item.get("art_code")
            or item.get("announcement_id")
            or item.get("ann_id")
            or item.get("id")
            or item.get("code"),
            max_len=240,
        )

    @classmethod
    def _build_document_checksum(cls, item: dict[str, Any], *, title: str, body: str, url: str, published_at: datetime | None) -> str:
        explicit = cls._clean_context_text(item.get("checksum"), max_len=128)
        if explicit:
            return explicit
        basis = "|".join(
            [
                str(item.get("provider") or item.get("source") or ""),
                title or "",
                url or "",
                published_at.isoformat() if published_at else "",
                body[:4000] if body else "",
            ]
        )
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()

    @classmethod
    def _coerce_context_datetime(cls, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        raw = str(value or "").strip()
        if not raw:
            return None
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            return datetime.fromisoformat(normalized)
        except Exception:
            pass
        parsed_date = cls._coerce_context_date(raw)
        return datetime.combine(parsed_date, datetime.min.time()) if parsed_date else None

    @staticmethod
    def _json_list(values: Any) -> list[Any]:
        if values is None:
            return []
        if isinstance(values, list):
            return values
        if isinstance(values, (tuple, set)):
            return list(values)
        text = str(values or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [item.strip() for item in text.replace(";", ",").split(",") if item.strip()]

    @classmethod
    def _pick_document_content(cls, item: dict[str, Any]) -> str:
        for key in ("content", "text", "summary", "title", "headline", "name"):
            text = cls._clean_context_text(item.get(key))
            if text:
                return text
        return ""

    @classmethod
    def _pick_document_title(cls, item: dict[str, Any]) -> str:
        for key in ("title", "headline", "name", "summary"):
            text = cls._clean_context_text(item.get(key), max_len=240)
            if text:
                return text
        content = cls._pick_document_content(item)
        return cls._clean_context_text(content[:120], max_len=120)

    @classmethod
    def _pick_document_summary(cls, item: dict[str, Any]) -> str:
        for key in ("summary", "abstract", "description", "content", "text"):
            text = cls._clean_context_text(item.get(key), max_len=1000)
            if text:
                return text
        return ""

    @classmethod
    def _pick_document_body(cls, item: dict[str, Any]) -> str:
        for key in ("body", "content", "text", "summary", "title", "headline", "name"):
            text = cls._clean_context_text(item.get(key), max_len=20000)
            if text:
                return text
        return ""

    @classmethod
    def _pick_document_source(cls, doc_type: str, item: dict[str, Any]) -> str:
        source = cls._clean_context_text(item.get("source") or item.get("provider") or item.get("origin"), max_len=120)
        return source or str(doc_type or "unknown").strip().lower()

    @classmethod
    def _pick_document_url(cls, item: dict[str, Any]) -> str:
        return cls._clean_context_text(item.get("url") or item.get("link") or item.get("pdf_url"), max_len=1000)

    @classmethod
    def _build_market_doc_uid(cls, stock_code: str, doc_type: str, item: dict[str, Any]) -> str:
        explicit = cls._clean_context_text(
            item.get("doc_uid") or item.get("document_id") or item.get("id") or item.get("uuid"),
            max_len=200,
        )
        if explicit:
            return explicit
        url = cls._pick_document_url(item)
        if url:
            return url
        title = cls._pick_document_title(item)
        body = cls._pick_document_body(item)[:1000]
        published_at = cls._coerce_context_date(item.get("published_at") or item.get("date") or item.get("time"))
        basis = "|".join(
            [
                str(stock_code or "").strip(),
                str(doc_type or "").strip().lower(),
                title,
                published_at.isoformat() if published_at else "",
                body,
            ]
        )
        return f"mdoc_{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def _count_text_tokens(text: str) -> int:
        normalized = str(text or "").strip()
        if not normalized:
            return 0
        return len(normalized.split()) if " " in normalized else len(normalized)

    @classmethod
    def _chunk_document_text(
        cls,
        text: str,
        *,
        chunk_size: int = 800,
        overlap: int = 120,
    ) -> list[str]:
        normalized = " ".join(str(text or "").replace("\r", " ").split())
        if not normalized:
            return []
        resolved_chunk_size = max(200, int(chunk_size or 800))
        resolved_overlap = max(0, min(int(overlap or 120), resolved_chunk_size // 2))
        if len(normalized) <= resolved_chunk_size:
            return [normalized]
        chunks: list[str] = []
        start = 0
        while start < len(normalized):
            end = min(len(normalized), start + resolved_chunk_size)
            chunk = normalized[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(normalized):
                break
            start = max(end - resolved_overlap, start + 1)
        return chunks

    @staticmethod
    def _headline_horizon_days(doc_type: str) -> int:
        normalized = str(doc_type or "").strip().lower()
        if normalized == "news":
            return 5
        if normalized == "notice":
            return 10
        if normalized == "research":
            return 20
        return 5

    @staticmethod
    def _headline_direction(label: str) -> str:
        normalized = str(label or "").strip().lower()
        if normalized == "bullish":
            return "up"
        if normalized == "bearish":
            return "down"
        return "flat"

    @classmethod
    def _build_headline_label_id(
        cls,
        *,
        doc_uid: str,
        headline: str,
        label: str,
        event_type: str | None,
    ) -> str:
        basis = "|".join([doc_uid, headline, label, str(event_type or "")])
        return f"hlabel_{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:24]}"

    @classmethod
    def _extract_headline_label_rows(
        cls,
        stock_code: str,
        doc_type: str,
        item: dict[str, Any],
        *,
        doc_id: int | None,
        doc_uid: str,
        published_at: datetime | None,
    ) -> list[dict[str, Any]]:
        headline = cls._pick_document_title(item)
        if not headline:
            return []
        from aiask_quant_core.storage.runtime_hooks import (
            get_event_extractor,
            get_headline_sentiment_classifier,
        )

        summary = cls._pick_document_summary(item)
        body = cls._pick_document_body(item)
        classifier = get_headline_sentiment_classifier()
        extractor = get_event_extractor()
        label = classifier(headline) if callable(classifier) else "neutral"
        extraction = (
            extractor(
                [{"title": headline, "text": " ".join(part for part in [headline, summary, body[:200]] if part)}],
                top_n=1,
            )
            if callable(extractor)
            else {}
        )
        event_tags = list(extraction.get("event_tags") or [])
        event_type = str(event_tags[0].get("tag") or "").strip() if event_tags else None
        keywords = list(dict(extraction.get("keyword_hits") or {}).keys())[:8]
        keyword_count = len(keywords)
        intensity = "high" if keyword_count >= 3 else "medium" if keyword_count >= 1 else "low"
        confidence = min(0.95, 0.55 + keyword_count * 0.10)
        label_id = cls._build_headline_label_id(
            doc_uid=doc_uid,
            headline=headline,
            label=label,
            event_type=event_type,
        )
        payload = {
            "doc_uid": doc_uid,
            "headline": headline,
            "summary": summary,
            "source": item.get("source") or item.get("provider") or item.get("origin"),
            "event_tags": event_tags,
            "keyword_hits": dict(extraction.get("keyword_hits") or {}),
        }
        return [
            {
                "label_id": label_id,
                "doc_id": doc_id,
                "doc_uid": doc_uid,
                "stock_code": str(stock_code or "").strip(),
                "doc_type": str(doc_type or "").strip().lower(),
                "published_at": published_at,
                "headline": headline,
                "label": label,
                "event_type": event_type,
                "direction": cls._headline_direction(label),
                "horizon_days": cls._headline_horizon_days(doc_type),
                "intensity": intensity,
                "confidence": round(confidence, 4),
                "keywords": keywords,
                "payload": payload,
            }
        ]

    def _decode_market_event_normalized(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row or {})
        for key in ("entity_codes", "theme_codes", "source_doc_uids", "source_types", "provider_chain"):
            payload[key] = self._decode_json_field(payload.get(key), [])
        payload["metadata"] = self._decode_json_field(payload.get("metadata"), {})
        return payload

    @classmethod
    def _radar_json_text(cls, field_name: str, value: Any, *, default: Any) -> str:
        payload = default if value is None else value
        return bounded_json_text(
            f"stock_radar.{field_name}",
            payload,
            max_bytes=cls._RADAR_JSON_FIELD_LIMITS.get(field_name, 16 * 1024),
        )

    @classmethod
    def _build_stock_radar_run_id(cls, mode: Any = None) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        token = uuid.uuid4().hex[:10]
        prefix = cls._clean_context_text(mode or "radar", max_len=40).replace(" ", "_") or "radar"
        return f"radar_{prefix}_{stamp}_{token}"

    @classmethod
    def _build_stock_radar_candidate_id(cls, item: dict[str, Any]) -> str:
        explicit = cls._clean_context_text(item.get("candidate_id"), max_len=220)
        if explicit:
            return explicit
        basis = "|".join(
            [
                cls._clean_context_text(item.get("run_id"), max_len=220),
                cls._clean_context_text(item.get("symbol") or item.get("stock_code") or item.get("code"), max_len=40),
                cls._clean_context_text(item.get("event_id"), max_len=220),
                cls._clean_context_text(item.get("event_type") or "unknown", max_len=120),
            ]
        )
        return f"radar_cand_{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:24]}"

    @classmethod
    def _stock_radar_tier(cls, value: Any, *, score: float | None = None) -> str:
        raw = cls._clean_context_text(value, max_len=40).lower()
        if raw in {"alert", "watch", "observe", "reject"}:
            return raw
        resolved = float(score or 0)
        if resolved >= 80:
            return "alert"
        if resolved >= 60:
            return "watch"
        if resolved >= 40:
            return "observe"
        return "reject"

    def _decode_stock_radar_run(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row or {})
        payload["summary"] = self._decode_json_field(payload.get("summary"), {})
        payload["degraded_flags"] = self._decode_json_field(payload.get("degraded_flags"), [])
        payload["metadata"] = self._decode_json_field(payload.get("metadata"), {})
        return payload

    def _decode_stock_radar_candidate(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row or {})
        for key in ("source_doc_uids", "source_chain", "risk_flags"):
            payload[key] = self._decode_json_field(payload.get(key), [])
        for key in ("extraction", "confirmations"):
            payload[key] = self._decode_json_field(payload.get(key), {})
        return payload

    def _decode_stock_radar_push_log(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row or {})
        payload["metadata"] = self._decode_json_field(payload.get("metadata"), {})
        return payload
