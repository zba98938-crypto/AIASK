"""Unified vector storage mixin for market / quant / strategy derived objects."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from typing import Any, Iterable, List, Optional

from aiask_quant_core.vector_collection_scope import (
    LEGACY_MARKET_DOC_COLLECTION,
    MARKET_DOC_PROFILE_TYPES,
    is_profile_scoped_collection,
    kline_collection_name,
    market_doc_collection_name,
    market_doc_search_scopes,
    normalize_market_doc_types,
    normalize_profile_type,
    resolve_vector_collection_name,
    vector_collection_candidates,
)


class _VectorUnifiedUtilsMixin:
        LEGACY_MARKET_DOC_COLLECTION = LEGACY_MARKET_DOC_COLLECTION
        MARKET_DOC_PROFILE_TYPES = MARKET_DOC_PROFILE_TYPES

        @staticmethod
        def _normalize_profile_type(value: Any) -> str | None:
            return normalize_profile_type(value)

        @staticmethod
        def _resolve_vector_collection_name(collection_name: Any, profile_type: Any = None) -> str:
            return resolve_vector_collection_name(collection_name, profile_type)

        @staticmethod
        def _vector_collection_candidates(collection_name: Any, profile_type: Any = None) -> List[str]:
            return vector_collection_candidates(collection_name, profile_type)

        @staticmethod
        def _is_profile_scoped_collection(collection_name: Any) -> bool:
            return is_profile_scoped_collection(collection_name)

        @staticmethod
        def _market_doc_collection_name(doc_type: Any) -> str:
            return market_doc_collection_name(doc_type)

        @staticmethod
        def _normalize_market_doc_types(values: Any) -> List[str]:
            return normalize_market_doc_types(values)

        @staticmethod
        def _market_doc_search_scopes(doc_types: Any = None) -> List[tuple[str, str | None]]:
            return market_doc_search_scopes(doc_types)

        @staticmethod
        def _kline_collection_name(vector_method: Any, window_size: Any) -> str:
            return kline_collection_name(vector_method, window_size)

        @staticmethod
        def _normalize_hybrid_query_text(value: Any) -> str:
            return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())

        def _hybrid_query_terms(cls, value: Any) -> List[str]:
            text = cls._normalize_hybrid_query_text(value)
            if not text:
                return []
            split_terms = [
                item.strip()
                for item in re.split(r"[\s,;|/，。；、：:（）()\[\]【】]+", text)
                if item and item.strip()
            ]
            terms: list[str] = []
            seen: set[str] = set()
            for item in [text, *split_terms]:
                token = str(item or "").strip()
                if not token or token in seen:
                    continue
                seen.add(token)
                terms.append(token)
            if len(split_terms) <= 1 and len(text) >= 4:
                for width in (2, 3):
                    if len(text) <= width:
                        continue
                    for idx in range(len(text) - width + 1):
                        token = text[idx: idx + width].strip()
                        if len(token) < width or token in seen:
                            continue
                        seen.add(token)
                        terms.append(token)
                        if len(terms) >= 12:
                            return terms
            return terms[:12]

        def _fts5_query(cls, value: Any) -> str:
            terms = cls._hybrid_query_terms(value)
            quoted: list[str] = []
            for term in terms:
                token = str(term or "").strip()
                if not token:
                    continue
                quoted.append(f'"{token.replace(chr(34), chr(34) + chr(34))}"')
            return " OR ".join(quoted[:8])

        def _hybrid_lexical_score(cls, query_text: str, title: str, content: str) -> float:
            normalized_query = cls._normalize_hybrid_query_text(query_text)
            if not normalized_query:
                return 0.0
            haystack = cls._normalize_hybrid_query_text(f"{title or ''} {content or ''}").lower()
            title_text = cls._normalize_hybrid_query_text(title).lower()
            if not haystack:
                return 0.0
            normalized_query = normalized_query.lower()
            terms = [item.lower() for item in cls._hybrid_query_terms(normalized_query)]
            if not terms:
                return 0.0
            full_match = 1.0 if normalized_query in haystack else 0.0
            matched_terms = sum(1 for term in terms if term in haystack)
            matched_title_terms = sum(1 for term in terms if term in title_text)
            term_score = matched_terms / max(len(terms), 1)
            title_bonus = matched_title_terms / max(len(terms), 1)
            score = 0.6 * full_match + 0.3 * term_score + 0.1 * title_bonus
            return round(max(0.0, min(score, 1.0)), 6)

        @staticmethod
        def _hybrid_dense_score(value: Any) -> Optional[float]:
            if value is None:
                return None
            try:
                resolved = float(value)
            except (TypeError, ValueError):
                return None
            return round(max(0.0, min(resolved, 1.0)), 6)

        def _hybrid_score(
            cls,
            *,
            dense_score: Optional[float],
            lexical_score: Optional[float],
        ) -> Optional[float]:
            has_dense = dense_score is not None
            has_lexical = lexical_score is not None
            if not has_dense and not has_lexical:
                return None
            if has_dense and has_lexical:
                return round((float(dense_score) * 0.65) + (float(lexical_score) * 0.35), 6)
            if has_dense:
                return round(float(dense_score), 6)
            return round(float(lexical_score or 0.0), 6)

        def _decode_vector_collection(self, row: dict) -> dict:
            result = dict(row)
            result["metadata"] = self._decode_json_field(result.get("metadata"), {})
            return result

        def _decode_unified_vector_profile(self, row: dict) -> dict:
            result = dict(row)
            result["embedding"] = self._decode_json_field(result.get("embedding_json"), [])
            result["metadata"] = self._decode_json_field(result.get("metadata"), {})
            return result

        def _decode_unified_vector_snapshot(self, row: dict) -> dict:
            result = dict(row)
            result["index_params"] = self._decode_json_field(result.get("index_params"), {})
            result["metrics"] = self._decode_json_field(result.get("metrics"), {})
            result["metadata"] = self._decode_json_field(result.get("metadata"), {})
            return result

        def _decode_unified_vector_item(self, row: dict) -> dict:
            result = dict(row)
            result["embedding"] = self._decode_json_field(result.get("embedding_json"), [])
            result["metadata"] = self._decode_json_field(result.get("metadata"), {})
            return result

        def _decode_kline_pattern_window(self, row: dict) -> dict:
            result = dict(row)
            result["payload"] = self._decode_json_field(result.get("payload"), {})
            result["metadata"] = self._decode_json_field(result.get("metadata"), {})
            return result

        @staticmethod
        def _vector_similarity(left: List[float], right: List[float], metric: str = "cosine") -> float:
            lv = [float(item) for item in list(left or [])]
            rv = [float(item) for item in list(right or [])]
            if not lv or not rv or len(lv) != len(rv):
                return 0.0
            if any(not math.isfinite(item) for item in [*lv, *rv]):
                return 0.0
            normalized_metric = str(metric or "cosine").strip().lower()
            if normalized_metric in {"ip", "inner_product"}:
                return round(float(sum(l * r for l, r in zip(lv, rv))), 6)
            if normalized_metric in {"l2", "euclidean"}:
                distance = math.sqrt(sum((l - r) * (l - r) for l, r in zip(lv, rv)))
                return round(1.0 / (1.0 + float(distance)), 6)
            left_norm = math.sqrt(sum(item * item for item in lv))
            right_norm = math.sqrt(sum(item * item for item in rv))
            if left_norm <= 1e-12 or right_norm <= 1e-12:
                return 0.0
            return round(float(sum(l * r for l, r in zip(lv, rv)) / (left_norm * right_norm)), 6)

        @staticmethod
        def _normalize_embedding(values: Any) -> List[float]:
            resolved = [float(item) for item in list(values or [])]
            if not resolved:
                return []
            norm = math.sqrt(sum(item * item for item in resolved))
            if norm <= 1e-12:
                return []
            return [float(item / norm) for item in resolved]

        @staticmethod
        def _coerce_date_value(value: Any) -> Optional[date]:
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
        def _snapshot_bucket_rows(snapshot: Optional[dict]) -> List[dict]:
            metadata = dict((snapshot or {}).get("metadata") or {})
            return [dict(item or {}) for item in list(metadata.get("centroids") or []) if dict(item or {}).get("bucket_id")]

        def _resolve_query_buckets(
            cls,
            snapshot: Optional[dict],
            query_embedding: List[float],
        ) -> tuple[Optional[str], List[str]]:
            bucket_rows = cls._snapshot_bucket_rows(snapshot)
            normalized_query = cls._normalize_embedding(query_embedding)
            if not bucket_rows or not normalized_query:
                return None, []
            index_params = dict((snapshot or {}).get("index_params") or {})
            neighbor_count = max(0, min(int(index_params.get("neighbor_count") or 2), 8))
            scored: list[tuple[str, float, list[str]]] = []
            for row in bucket_rows:
                bucket_id = str(row.get("bucket_id") or "").strip()
                centroid = cls._normalize_embedding(row.get("centroid") or [])
                if not bucket_id or not centroid or len(centroid) != len(normalized_query):
                    continue
                scored.append(
                    (
                        bucket_id,
                        cls._vector_similarity(normalized_query, centroid, metric="cosine"),
                        [str(item).strip() for item in list(row.get("neighbors") or []) if str(item).strip()],
                    )
                )
            if not scored:
                return None, []
            scored.sort(key=lambda item: item[1], reverse=True)
            primary_bucket = scored[0][0]
            candidate_buckets = [primary_bucket]
            for neighbor in scored[0][2][:neighbor_count]:
                if neighbor not in candidate_buckets:
                    candidate_buckets.append(neighbor)
            return primary_bucket, candidate_buckets

        @staticmethod
        def _kline_pattern_profile_type(
            *,
            window_size: int,
            vector_method: str,
            period: str = "daily",
            adjust: str = "",
        ) -> str:
            return "|".join(
                [
                    str(vector_method or "returns").strip().lower(),
                    str(period or "daily").strip().lower(),
                    str(adjust or "").strip().lower(),
                    str(int(window_size or 0)),
                ]
            )
