"""Unified alpha research artifact builder.

Combines factor governance, event context, and Chinese financial semantic
signals into one reusable artifact for factory/runtime consumers.
"""

from __future__ import annotations

from typing import Any

from ...infrastructure.mcp_services import (
    get_event_context_builder,
    get_financial_semantic_service_factory,
)


class AlphaResearchService:
    MAX_FOCUS_CODES = 3

    @staticmethod
    def _normalize_codes(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        if not text:
            return []
        return [item.strip() for item in text.split(",") if item.strip()]

    @classmethod
    def _extract_focus_codes(cls, snapshot: dict[str, Any]) -> list[str]:
        codes = []
        for key in ("candidate_codes", "codes", "target_symbols"):
            codes.extend(cls._normalize_codes(snapshot.get(key)))
        event_driven = dict(snapshot.get("event_driven") or {})
        for item in list(event_driven.get("events") or []):
            if not isinstance(item, dict):
                continue
            for key in ("target_symbols", "stock_pool", "candidate_codes"):
                codes.extend(cls._normalize_codes(item.get(key)))
        seen = set()
        ordered = []
        for code in codes:
            token = str(code or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            ordered.append(token)
        return ordered[: cls.MAX_FOCUS_CODES]

    @staticmethod
    def _top_counts(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for item in list(rows or []):
            for entry in list(item.get(field) or []):
                if isinstance(entry, dict):
                    key = str(entry.get("tag") or "").strip()
                    value = int(entry.get("count") or 0)
                else:
                    key = str(entry or "").strip()
                    value = 1
                if not key:
                    continue
                counts[key] = int(counts.get(key, 0)) + max(value, 1)
        return [
            {"tag": key, "count": count}
            for key, count in sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[:10]
        ]

    @classmethod
    async def build(
        cls,
        snapshot: dict[str, Any],
        *,
        factor_artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        focus_codes = cls._extract_focus_codes(snapshot)
        if not focus_codes:
            return {
                "available": False,
                "reason": "no_focus_codes",
                "focus_codes": [],
                "source_chain": ["alpha_research_service"],
            }

        build_event_context = get_event_context_builder()
        semantic_service = get_financial_semantic_service_factory()()
        per_code = []
        warnings = []
        for code in focus_codes:
            try:
                event_context = await build_event_context(code, news_limit=8, notice_days=20, report_limit=5)
                raw_texts = [
                    dict(item or {})
                    for item in list(event_context.get("raw_texts") or [])
                    if isinstance(item, dict)
                ]
                semantic_signal = await semantic_service.analyze_documents(raw_texts)
                per_code.append(
                    {
                        "code": code,
                        "event_score": float(event_context.get("score") or 0.0),
                        "event_sentiment": event_context.get("sentiment"),
                        "event_direction": event_context.get("event_direction"),
                        "event_intensity": event_context.get("event_intensity"),
                        "event_tags": list(event_context.get("event_tags") or [])[:8],
                        "event_risks": list(event_context.get("risks") or [])[:6],
                        "event_reasons": list(event_context.get("reasons") or [])[:6],
                        "hard_veto_eligible": bool(event_context.get("hard_veto_eligible")),
                        "semantic_signal": {
                            "available": bool(semantic_signal.get("available")),
                            "provider": semantic_signal.get("provider"),
                            "model": semantic_signal.get("model"),
                            "score": semantic_signal.get("score"),
                            "sentiment": semantic_signal.get("sentiment"),
                            "entity_sentiment": semantic_signal.get("entity_sentiment"),
                            "event_sentiment": semantic_signal.get("event_sentiment"),
                            "surprise": semantic_signal.get("surprise"),
                            "credibility": semantic_signal.get("credibility"),
                            "event_types": list(semantic_signal.get("event_types") or [])[:8],
                            "risk_tags": list(semantic_signal.get("risk_tags") or [])[:8],
                            "fallback_reason": semantic_signal.get("fallback_reason"),
                        },
                    }
                )
            except Exception as exc:
                warnings.append(f"{code}: {exc}")

        if not per_code:
            return {
                "available": False,
                "reason": "event_context_unavailable",
                "focus_codes": focus_codes,
                "warnings": warnings,
                "source_chain": ["alpha_research_service", "decision_event_builder"],
            }

        factor_summary = dict((factor_artifact or {}).get("summary") or {})
        average_event_score = sum(float(item.get("event_score") or 0.0) for item in per_code) / max(len(per_code), 1)
        semantic_rows = [dict(item.get("semantic_signal") or {}) for item in per_code if isinstance(item.get("semantic_signal"), dict)]
        semantic_available = [row for row in semantic_rows if bool(row.get("available"))]
        average_semantic_score = (
            sum(float(row.get("score") or 0.0) for row in semantic_available) / max(len(semantic_available), 1)
            if semantic_available
            else None
        )
        top_event_tags = cls._top_counts(per_code, "event_tags")
        top_risk_tags = cls._top_counts(semantic_rows, "risk_tags")
        provider = str((semantic_available[0] or {}).get("provider") or "").strip() if semantic_available else None
        model = str((semantic_available[0] or {}).get("model") or "").strip() if semantic_available else None
        return {
            "available": True,
            "focus_codes": focus_codes,
            "per_code": per_code,
            "warnings": warnings,
            "source_chain": [
                "alpha_research_service",
                "decision_event_builder",
                "financial_semantic_service",
            ],
            "summary": {
                "focus_code_count": len(focus_codes),
                "average_event_score": round(float(average_event_score), 4),
                "average_semantic_score": round(float(average_semantic_score), 4) if average_semantic_score is not None else None,
                "semantic_available_count": len(semantic_available),
                "semantic_provider": provider,
                "semantic_model": model,
                "hard_veto_count": sum(1 for item in per_code if bool(item.get("hard_veto_eligible"))),
                "bullish_code_count": sum(1 for item in per_code if str(item.get("event_direction") or "").strip().lower() == "bullish"),
                "top_event_tags": top_event_tags,
                "top_risk_tags": top_risk_tags,
                "factor_active_count": int(factor_summary.get("active_factor_count") or 0),
                "active_candidate_count": int(factor_summary.get("active_candidate_count") or 0),
            },
        }


__all__ = ["AlphaResearchService"]
