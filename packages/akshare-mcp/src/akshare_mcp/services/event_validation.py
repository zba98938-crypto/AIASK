"""Multi-source event signature + validation (sliced out)."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Any

from .event_constants import (
    SOURCE_TIER_A,
    SOURCE_TIER_B,
    SOURCE_TIER_C,
    _CONFLICT_CONFIDENCE_CAP,
    _MULTI_SOURCE_CONFIDENCE_CAP,
    _SINGLE_ANCHOR_CONFIDENCE_CAP,
    _TIER_DEFAULT_RELIABILITY,
    _coerce_date_text,
    _unique_list,
)

def _event_date_key(event: dict[str, Any]) -> str:
    for key in ("event_time", "publish_time", "evidence_time"):
        text = _coerce_date_text(event.get(key))
        if text:
            return text[:10]
    return "unknown_date"


_SIGNATURE_STOPWORDS = {
    "000001",
    "announcement",
    "announces",
    "company",
    "disclosure",
    "official",
    "says",
    "may",
    "from",
    "with",
    "about",
    "this",
    "that",
    "major",
}


def _title_keyword_signature(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "").strip().lower()
    theme_codes = _unique_list(event.get("theme_codes"), limit=3)
    if event_type and event_type not in {"announcement", "market_news"}:
        return "-".join([event_type, *theme_codes]) if theme_codes else event_type

    text = " ".join(
        [
            str(event.get("event_name") or ""),
            str(event.get("summary") or ""),
            " ".join(str(item or "") for item in list(event.get("theme_codes") or [])),
        ]
    ).lower()
    text = re.sub(r"\b\d{6}\b", " ", text)
    tokens = [
        token
        for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", text)
        if len(token) >= 2 and token not in _SIGNATURE_STOPWORDS
    ]
    if not tokens:
        return str(event.get("event_type") or "event")
    return "-".join(sorted(dict.fromkeys(tokens))[:8])


def _event_signature(event: dict[str, Any]) -> str:
    entity_codes = ",".join(sorted(_unique_list(event.get("entity_codes"), limit=20))) or "unknown_entity"
    basis = "|".join(
        [
            entity_codes,
            str(event.get("event_type") or "event").strip().lower() or "event",
            _event_date_key(event),
            _title_keyword_signature(event),
        ]
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]


def _event_signature_value(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    summary = metadata.get("validation_summary") if isinstance(metadata.get("validation_summary"), dict) else {}
    signature = str(metadata.get("event_signature") or summary.get("event_signature") or "").strip()
    return signature or _event_signature(event)


def _validation_summary_value(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    summary = metadata.get("validation_summary") if isinstance(metadata.get("validation_summary"), dict) else {}
    return dict(summary or {})


def _direction_bucket(value: Any) -> str:
    token = str(value or "neutral").strip().lower()
    if token in {"up", "positive", "bullish", "long"}:
        return "up"
    if token in {"down", "negative", "bearish", "short"}:
        return "down"
    return "neutral"


class MultiSourceEventValidator:
    """Validate occurrence and alpha confirmation without requiring new tables."""

    def validate(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for raw in [dict(item or {}) for item in list(events or []) if isinstance(item, dict)]:
            signature = _event_signature_value(raw)
            raw.setdefault("metadata", {})
            if isinstance(raw["metadata"], dict):
                raw["metadata"]["event_signature"] = signature
            grouped.setdefault(signature, []).append(raw)
        return [self._merge(signature, rows) for signature, rows in grouped.items()]

    def _merge(self, signature: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        primary = self._select_primary(rows)
        source_doc_uids = _unique_list([item.get("source_doc_uids") for item in rows], limit=100)
        provider_chain = _unique_list([item.get("provider_chain") for item in rows], limit=30)
        source_types = _unique_list([item.get("source_types") for item in rows], limit=30)
        entity_codes = _unique_list([item.get("entity_codes") for item in rows], limit=30)
        theme_codes = _unique_list([item.get("theme_codes") for item in rows], limit=20)
        official_anchor_count = max(
            self._summary_count(rows, "official_anchor_count"),
            self._summary_count(rows, "official_anchor_count") + self._new_raw_source_count(rows, SOURCE_TIER_A),
        )
        institutional_anchor_count = max(
            self._summary_count(rows, "institutional_anchor_count"),
            self._summary_count(rows, "institutional_anchor_count") + self._new_raw_source_count(rows, SOURCE_TIER_B),
        )
        media_confirm_count = max(
            self._summary_count(rows, "media_confirm_count"),
            self._summary_count(rows, "media_confirm_count") + self._new_raw_source_count(rows, SOURCE_TIER_C),
        )
        cross_source_count = max(
            len(provider_chain),
            len(source_doc_uids),
            *(max(1, int(item.get("cross_source_count") or _validation_summary_value(item).get("cross_source_count") or 1)) for item in rows),
            1,
        )
        directions = {_direction_bucket(item.get("direction")) for item in rows}
        conflict_count = max(
            1 if {"up", "down"}.issubset(directions) else 0,
            *(int(_validation_summary_value(item).get("conflict_count") or 0) for item in rows),
        )
        has_verified_anchor = official_anchor_count + institutional_anchor_count > 0
        has_invalid_core = any(str(item.get("status") or "").strip().lower() in {"rejected", "degraded"} for item in rows)

        if not entity_codes:
            occurrence_status = "rejected"
            alpha_status = "missing_entity_codes"
            status = "rejected"
            reject_reason = "missing_entity_codes"
            confidence_cap_reason = "missing_entity_codes"
        elif has_invalid_core and not has_verified_anchor:
            occurrence_status = str(primary.get("status") or "degraded")
            alpha_status = "source_degraded"
            status = str(primary.get("status") or "degraded")
            reject_reason = str(primary.get("reject_reason") or "source_degraded")
            confidence_cap_reason = reject_reason
        elif not has_verified_anchor:
            occurrence_status = "provisional"
            alpha_status = "news_only_rejected"
            status = "provisional"
            reject_reason = "news_only_or_low_tier_source"
            confidence_cap_reason = "news_only_requires_official_or_institutional_anchor"
        elif conflict_count:
            occurrence_status = "verified_conflicted"
            alpha_status = "conflicted"
            status = "verified"
            reject_reason = "direction_conflict"
            confidence_cap_reason = "direction_conflict"
        elif cross_source_count <= 1 and official_anchor_count + institutional_anchor_count == 1:
            occurrence_status = "verified_single_anchor"
            alpha_status = "single_anchor_unconfirmed"
            status = "verified"
            reject_reason = None
            confidence_cap_reason = "single_official_or_institutional_anchor"
        else:
            occurrence_status = "verified_multi_source"
            alpha_status = "confirmed"
            status = "verified"
            reject_reason = None
            confidence_cap_reason = None

        if conflict_count:
            direction = "neutral"
        else:
            direction = _direction_bucket(primary.get("direction"))

        if official_anchor_count:
            source_tier = SOURCE_TIER_A
        elif institutional_anchor_count:
            source_tier = SOURCE_TIER_B
        else:
            source_tier = SOURCE_TIER_C

        reliability = self._validated_reliability(
            rows,
            alpha_status=alpha_status,
            cross_source_count=cross_source_count,
        )
        event_id = f"mevt_{hashlib.sha1(signature.encode('utf-8')).hexdigest()[:24]}"
        checksum_basis = "|".join([signature, ",".join(source_doc_uids), ",".join(provider_chain)])
        validation_summary = {
            "event_signature": signature,
            "official_anchor_count": official_anchor_count,
            "institutional_anchor_count": institutional_anchor_count,
            "media_confirm_count": media_confirm_count,
            "cross_source_count": cross_source_count,
            "conflict_count": conflict_count,
            "provider_chain": list(provider_chain),
            "source_doc_uids": list(source_doc_uids),
            "occurrence_status": occurrence_status,
            "alpha_confirmation_status": alpha_status,
            "confidence_cap_reason": confidence_cap_reason,
        }
        metadata = dict(primary.get("metadata") or {})
        metadata.update(
            {
                "validation_summary": validation_summary,
                "event_signature": signature,
                "occurrence_status": occurrence_status,
                "alpha_confirmation_status": alpha_status,
                "confidence_cap_reason": confidence_cap_reason,
                "diagnostic_only": status != "verified" or alpha_status in {"news_only_rejected", "conflicted"},
            }
        )
        merged = dict(primary)
        merged.update(
            {
                "event_id": event_id,
                "event_anchor_id": event_id,
                "entity_codes": entity_codes,
                "theme_codes": theme_codes or list(primary.get("theme_codes") or []),
                "direction": direction,
                "source_doc_uids": source_doc_uids,
                "source_tier": source_tier,
                "source_types": source_types,
                "provider_chain": provider_chain,
                "reliability_score": reliability,
                "cross_source_count": cross_source_count,
                "status": status,
                "reject_reason": reject_reason,
                "checksum": hashlib.sha1(checksum_basis.encode("utf-8")).hexdigest(),
                "metadata": metadata,
            }
        )
        return merged

    @staticmethod
    def _summary_count(rows: list[dict[str, Any]], key: str) -> int:
        count = 0
        for item in rows:
            try:
                count = max(count, int(_validation_summary_value(item).get(key) or 0))
            except (TypeError, ValueError):
                continue
        return count

    @staticmethod
    def _new_raw_source_count(rows: list[dict[str, Any]], source_tier: str) -> int:
        existing_doc_uids = set(
            _unique_list(
                [
                    item.get("source_doc_uids")
                    for item in rows
                    if _validation_summary_value(item)
                ],
                limit=500,
            )
        )
        seen: set[str] = set()
        for item in rows:
            if _validation_summary_value(item):
                continue
            if str(item.get("source_tier") or "").lower() != source_tier:
                continue
            doc_uids = _unique_list(item.get("source_doc_uids"), limit=100)
            providers = _unique_list(item.get("provider_chain"), limit=30)
            for token in doc_uids or providers or [str(item.get("checksum") or item.get("event_id") or "")]:
                if token and token not in existing_doc_uids:
                    seen.add(token)
        return len(seen)

    @staticmethod
    def _select_primary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        tier_rank = {SOURCE_TIER_A: 3, SOURCE_TIER_B: 2, SOURCE_TIER_C: 1}
        return max(
            rows,
            key=lambda item: (
                tier_rank.get(str(item.get("source_tier") or "").lower(), 0),
                float(item.get("reliability_score") or 0.0),
                len(list(item.get("source_doc_uids") or [])),
            ),
        )

    @staticmethod
    def _validated_reliability(
        rows: list[dict[str, Any]],
        *,
        alpha_status: str,
        cross_source_count: int,
    ) -> float:
        max_score = max(float(item.get("reliability_score") or 0.0) for item in rows) if rows else 0.0
        if alpha_status == "single_anchor_unconfirmed":
            return round(min(max_score, _SINGLE_ANCHOR_CONFIDENCE_CAP), 4)
        if alpha_status == "confirmed":
            if any(str(item.get("source_tier") or "").lower() == SOURCE_TIER_A for item in rows):
                max_score = max(max_score, _TIER_DEFAULT_RELIABILITY[SOURCE_TIER_A])
            elif any(str(item.get("source_tier") or "").lower() == SOURCE_TIER_B for item in rows):
                max_score = max(max_score, _TIER_DEFAULT_RELIABILITY[SOURCE_TIER_B])
            uplifted = max_score + min(0.06, 0.025 * max(0, int(cross_source_count or 1) - 1))
            return round(min(uplifted, _MULTI_SOURCE_CONFIDENCE_CAP), 4)
        if alpha_status == "conflicted":
            return round(min(max_score, _CONFLICT_CONFIDENCE_CAP), 4)
        return round(min(max_score, 0.49), 4)
