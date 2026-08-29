"""因子研究记忆服务：相似召回、去重惩罚、验证结果写回。"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from .factor_candidate_storage import (
    get_factor_candidate_record_async,
    list_factor_candidate_records_async,
    save_factor_candidate_record,
)
from .text_embedding import get_strategy_text_embedding_service

_FACTOR_MEMORY_VECTOR_DIM = 128


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_codes(codes: Any) -> list[str]:
    if isinstance(codes, str):
        raw = [item.strip() for item in codes.replace("|", ",").replace(";", ",").split(",")]
        return [item for item in raw if item]
    if isinstance(codes, (list, tuple, set)):
        return [str(item).strip() for item in codes if str(item).strip()]
    return []


def _normalize_text(text: Any) -> str:
    return " ".join(str(text or "").strip().split())


def _hash_text(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()


def _candidate_signature(candidate: dict[str, Any]) -> str:
    parts = [
        _normalize_text((candidate or {}).get("family")),
        _normalize_text((candidate or {}).get("name")),
        _normalize_text((candidate or {}).get("expression_dsl")),
        ",".join(sorted([_normalize_text(item) for item in list((candidate or {}).get("inputs") or []) if _normalize_text(item)])),
    ]
    return _hash_text("\n".join(parts))


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_]+", str(text or "").lower())
    return {token for token in tokens if token}


def _dense_vector_tokens(text: str) -> list[str]:
    normalized = _normalize_text(text).lower()
    if not normalized:
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _tokenize(normalized):
        if token not in seen:
            seen.add(token)
            tokens.append(token)
        if len(token) >= 2:
            for width in (2, 3):
                if len(token) < width:
                    continue
                for idx in range(len(token) - width + 1):
                    ngram = token[idx: idx + width]
                    if ngram and ngram not in seen:
                        seen.add(ngram)
                        tokens.append(ngram)
    return tokens


def build_factor_candidate_dense_vector(text: str, *, dim: int = _FACTOR_MEMORY_VECTOR_DIM) -> list[float]:
    resolved_dim = max(16, int(dim or _FACTOR_MEMORY_VECTOR_DIM))
    tokens = _dense_vector_tokens(text)
    if not tokens:
        return []
    vector = [0.0] * resolved_dim
    for token in tokens:
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big", signed=False) % resolved_dim
        sign = 1.0 if (digest[4] % 2 == 0) else -1.0
        weight = 1.0 + ((digest[5] % 7) / 10.0)
        vector[bucket] += sign * weight
    return _normalize_dense_vector(vector)


def _normalize_dense_vector(values: list[float]) -> list[float]:
    resolved = [float(item) for item in list(values or [])]
    if not resolved:
        return []
    norm = math.sqrt(sum(item * item for item in resolved))
    if norm <= 1e-12:
        return resolved
    return [round(item / norm, 10) for item in resolved]


def _normalize_tags(tags: Any) -> list[str]:
    normalized = []
    for item in list(tags or []):
        token = str(item or "").strip().lower().replace(" ", "_")
        if token:
            normalized.append(token)
    return list(dict.fromkeys(normalized))


def _cosine_similarity(left: list[float] | np.ndarray, right: list[float] | np.ndarray) -> float:
    try:
        lv = np.asarray(left if left is not None else [], dtype=np.float64)
        rv = np.asarray(right if right is not None else [], dtype=np.float64)
    except Exception:
        return 0.0
    if lv.ndim != 1 or rv.ndim != 1 or len(lv) == 0 or len(lv) != len(rv):
        return 0.0
    ln = float(np.linalg.norm(lv))
    rn = float(np.linalg.norm(rv))
    if ln <= 1e-12 or rn <= 1e-12:
        return 0.0
    score = float(np.dot(lv, rv) / (ln * rn))
    if not math.isfinite(score):
        return 0.0
    return max(-1.0, min(1.0, score))


def _lexical_similarity(query_doc: str, record_doc: str, *, family_match: bool = False, expression_match: bool = False) -> float:
    query_tokens = _tokenize(query_doc)
    record_tokens = _tokenize(record_doc)
    if not query_tokens or not record_tokens:
        base = 0.0
    else:
        union = query_tokens | record_tokens
        base = (len(query_tokens & record_tokens) / len(union)) if union else 0.0
    if family_match:
        base += 0.08
    if expression_match:
        base += 0.18
    return max(0.0, min(1.0, base))


def _derive_memory_status(validation: dict[str, Any] | None = None, explicit_status: str | None = None) -> str:
    if explicit_status:
        normalized = str(explicit_status).strip().lower()
        if normalized in {"success", "active", "validated", "review", "fail", "draft"}:
            return normalized
    rating = (validation or {}).get("rating") if isinstance(validation, dict) else {}
    recommendation = str((rating or {}).get("recommendation") or "").strip().lower()
    grade = str((rating or {}).get("grade") or "").strip().upper()
    if recommendation == "promote":
        return "success"
    if recommendation in {"reject"} or grade == "D":
        return "fail"
    if validation:
        return "review"
    return "draft"


def _is_unstable_candidate(validation: dict[str, Any] | None = None) -> bool:
    validation = validation if isinstance(validation, dict) else {}
    robustness = validation.get("robustness") if isinstance(validation.get("robustness"), dict) else {}
    if robustness.get("available") and str(robustness.get("grade") or "").strip().lower() == "weak":
        return True

    oos_report = validation.get("oos_validation") if isinstance(validation.get("oos_validation"), dict) else {}
    if not oos_report:
        oos_report = validation.get("oos") if isinstance(validation.get("oos"), dict) else {}
    oos_rating = oos_report.get("rating") if isinstance(oos_report.get("rating"), dict) else {}
    if oos_report.get("available") and str(oos_rating.get("grade") or "").strip().upper() in {"C", "D"}:
        return True

    warnings = [str(item or "").strip().lower() for item in list(validation.get("warnings") or []) if str(item or "").strip()]
    unstable_tokens = (
        "unstable",
        "insufficient_cross_section_dates_for_stable_validation",
        "oos_validation_unavailable",
        "robustness_unavailable",
    )
    return any(any(token in warning for token in unstable_tokens) for warning in warnings)


def _build_candidate_document(
    candidate: dict[str, Any],
    *,
    validation: dict[str, Any] | None = None,
    codes: list[str] | None = None,
) -> str:
    candidate = candidate if isinstance(candidate, dict) else {}
    validation = validation if isinstance(validation, dict) else {}
    rating = (validation or {}).get("rating") if isinstance(validation, dict) else {}
    metrics = (validation or {}).get("metrics") if isinstance(validation, dict) else {}
    evidence_lines: list[str] = []
    for evidence in list(validation.get("external_evidence") or [])[:3]:
        if not isinstance(evidence, dict):
            continue
        title = _normalize_text(evidence.get("title"))
        source = _normalize_text(evidence.get("source"))
        idea = _normalize_text(evidence.get("extracted_factor_idea"))
        if title or source or idea:
            evidence_lines.append(f"外部证据: {source} {title} {idea}".strip())
    lines = [
        "候选因子研究记忆",
        f"名称: {_normalize_text(candidate.get('name'))}",
        f"家族: {_normalize_text(candidate.get('family'))}",
        f"假设: {_normalize_text(candidate.get('hypothesis'))}",
        f"表达式: {_normalize_text(candidate.get('expression_dsl'))}",
        f"输入: {', '.join([_normalize_text(item) for item in list(candidate.get('inputs') or []) if _normalize_text(item)])}",
        f"持有期: {int(candidate.get('expected_holding_period') or 0)}",
        f"市场状态: {', '.join([_normalize_text(item) for item in list(candidate.get('expected_regime') or []) if _normalize_text(item)])}",
        f"覆盖代码: {', '.join(_normalize_codes(codes))}",
        f"评级: {_normalize_text((rating or {}).get('grade'))}",
        f"建议: {_normalize_text((rating or {}).get('recommendation'))}",
        f"横截面RankIC: {float((metrics or {}).get('rank_ic_mean', 0.0)):.6f}",
        *evidence_lines,
    ]
    return "\n".join(lines)


def build_factor_candidate_vector_profile(
    record: dict[str, Any],
    *,
    version: str = "v1",
) -> dict[str, Any] | None:
    artifact_id = str((record or {}).get("artifact_id") or "").strip()
    candidate = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
    if not artifact_id or not candidate:
        return None
    codes = _normalize_codes(record.get("codes"))
    document = _build_candidate_document(candidate, validation=record, codes=codes)
    embedding = build_factor_candidate_dense_vector(document)
    if not embedding:
        return None
    rating = record.get("rating") if isinstance(record.get("rating"), dict) else {}
    external_evidence = [
        item
        for item in list(record.get("external_evidence") or [])
        if isinstance(item, dict)
    ]
    entity_id = artifact_id
    return {
        "collection_name": "factor_candidate_embeddings",
        "entity_type": "factor_candidate_memory",
        "entity_id": entity_id,
        "stock_code": codes[0] if len(codes) == 1 else None,
        "profile_type": "memory",
        "model_id": "factor-memory-v1",
        "vector_dim": len(embedding),
        "metric": "cosine",
        "version": str(version or "v1"),
        "signature": hashlib.sha1(f"{entity_id}\n{document}\n{version}".encode("utf-8")).hexdigest(),
        "status": "active",
        "embedding": embedding,
        "metadata": {
            "artifact_id": artifact_id,
            "status": str(record.get("status") or "").strip().lower(),
            "codes": codes,
            "tags": _normalize_tags(record.get("tags")),
            "candidate_name": candidate.get("name"),
            "candidate_family": candidate.get("family"),
            "expression_dsl": candidate.get("expression_dsl"),
            "recommendation": rating.get("recommendation"),
            "grade": rating.get("grade"),
            "text_hash": _hash_text(document),
            "text_preview": document[:240],
            "memory_flags": deepcopy(record.get("memory_flags") or {}),
            "external_evidence_count": len(external_evidence),
            "external_evidence": deepcopy(external_evidence[:3]),
        },
    }


def _build_prompt_memory_summary(rows: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    summary = []
    for row in rows[: max(1, int(limit))]:
        candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else {}
        rating = row.get("rating") if isinstance(row.get("rating"), dict) else {}
        summary.append(
            {
                "artifact_id": row.get("artifact_id"),
                "status": row.get("status"),
                "name": candidate.get("name"),
                "family": candidate.get("family"),
                "hypothesis": candidate.get("hypothesis"),
                "expression_dsl": candidate.get("expression_dsl"),
                "grade": rating.get("grade"),
                "recommendation": rating.get("recommendation"),
                "codes": row.get("codes"),
                "tags": row.get("tags"),
            }
        )
    return summary


def _build_similarity_edge(
    candidate: dict[str, Any],
    match: dict[str, Any],
) -> dict[str, Any]:
    candidate = candidate if isinstance(candidate, dict) else {}
    match_candidate = match.get("candidate") if isinstance(match.get("candidate"), dict) else {}
    candidate_family = _normalize_text(candidate.get("family")).lower()
    candidate_expr = _normalize_text(candidate.get("expression_dsl"))
    matched_family = _normalize_text(match_candidate.get("family")).lower()
    matched_expr = _normalize_text(match_candidate.get("expression_dsl"))
    similarity = float(match.get("similarity", 0.0) or 0.0)
    match_status = str(match.get("status") or "").strip().lower()

    same_expression = bool(candidate_expr and candidate_expr == matched_expr)
    same_family = bool(candidate_family and candidate_family == matched_family)
    if same_expression or similarity >= 0.985:
        edge_type = "duplicate"
        reason = "与历史候选表达式高度重复"
    elif match_status == "fail" and similarity >= 0.92:
        edge_type = "failure_pattern"
        reason = "与历史失败候选模式高度相似"
    elif match_status == "success" and similarity >= 0.92:
        edge_type = "success_pattern"
        reason = "与历史成功候选模式高度相似"
    elif same_family and similarity >= 0.75:
        edge_type = "family_neighbor"
        reason = "与同家族历史候选存在较强重叠"
    else:
        edge_type = "semantic_neighbor"
        reason = "与历史候选存在语义邻近关系"

    return {
        "artifact_id": match.get("artifact_id"),
        "status": match.get("status"),
        "edge_type": edge_type,
        "reason": reason,
        "similarity": round(similarity, 6),
        "lexical_similarity": round(float(match.get("lexical_similarity", 0.0) or 0.0), 6),
        "embedding_similarity": round(float(match.get("embedding_similarity", 0.0) or 0.0), 6),
        "code_overlap": round(float(match.get("code_overlap", 0.0) or 0.0), 6),
        "matched_name": match_candidate.get("name"),
        "matched_family": match_candidate.get("family"),
        "matched_expression_dsl": match_candidate.get("expression_dsl"),
        "matched_grade": (match.get("rating") or {}).get("grade") if isinstance(match.get("rating"), dict) else None,
        "matched_recommendation": (match.get("rating") or {}).get("recommendation") if isinstance(match.get("rating"), dict) else None,
    }


def _derive_memory_tags(
    *,
    candidate: dict[str, Any],
    validation: dict[str, Any],
    status: str,
    input_tags: list[str] | None = None,
    similarity_edges: list[dict[str, Any]] | None = None,
) -> list[str]:
    rating = validation.get("rating") if isinstance(validation.get("rating"), dict) else {}
    tags = [status]
    if _normalize_text(candidate.get("family")):
        tags.append(f"family:{_normalize_text(candidate.get('family')).lower()}")
    grade = str(rating.get("grade") or "").strip().upper()
    if grade:
        tags.append(f"grade:{grade.lower()}")
    recommendation = str(rating.get("recommendation") or "").strip().lower()
    if recommendation:
        tags.append(f"recommendation:{recommendation}")
    if _is_unstable_candidate(validation):
        tags.append("unstable")

    top_edge = (similarity_edges or [{}])[0] if similarity_edges else {}
    edge_type = str(top_edge.get("edge_type") or "").strip().lower()
    similarity = float(top_edge.get("similarity", 0.0) or 0.0)
    if edge_type == "duplicate":
        tags.append("duplicate")
    elif edge_type == "failure_pattern":
        tags.append("failure_pattern")
    elif edge_type == "success_pattern":
        tags.append("success_pattern")
    if similarity >= 0.90:
        tags.append("near_duplicate")

    return _normalize_tags([*tags, *list(input_tags or [])])


def _derive_memory_flags(similarity_edges: list[dict[str, Any]], validation: dict[str, Any]) -> dict[str, Any]:
    top_edge = (similarity_edges or [{}])[0] if similarity_edges else {}
    similarity = float(top_edge.get("similarity", 0.0) or 0.0)
    edge_type = str(top_edge.get("edge_type") or "").strip().lower()
    return {
        "has_duplicate_neighbor": bool(edge_type == "duplicate" or similarity >= 0.98),
        "has_failure_pattern_neighbor": bool(edge_type == "failure_pattern"),
        "top_similarity": round(similarity, 6),
        "top_edge_type": edge_type or None,
        "top_match_status": str(top_edge.get("status") or "").strip().lower() or None,
        "unstable": bool(_is_unstable_candidate(validation)),
    }


class FactorResearchMemoryService:
    def __init__(self, embedding_service=None):
        self.embedding_service = embedding_service or get_strategy_text_embedding_service()

    async def _maybe_embed(self, text: str) -> dict[str, Any]:
        document = _normalize_text(text)
        if not document:
            return {
                "available": False,
                "vector": [],
                "provider": "",
                "model": "",
                "text_hash": "",
                "text_preview": "",
                "error": "empty_document",
            }
        if not getattr(self.embedding_service, "is_enabled", lambda: False)():
            return {
                "available": False,
                "vector": [],
                "provider": "",
                "model": "",
                "text_hash": _hash_text(document),
                "text_preview": document[:240],
                "error": "embedding_provider_not_configured",
            }
        try:
            vector = await self.embedding_service.embed_text(document)
            return {
                "available": True,
                "vector": [float(item) for item in list(vector or [])],
                "provider": str(getattr(getattr(self.embedding_service, "config", None), "provider", "") or ""),
                "model": str(getattr(getattr(self.embedding_service, "config", None), "model", "") or ""),
                "text_hash": _hash_text(document),
                "text_preview": document[:240],
                "error": "",
            }
        except Exception as exc:
            return {
                "available": False,
                "vector": [],
                "provider": str(getattr(getattr(self.embedding_service, "config", None), "provider", "") or ""),
                "model": str(getattr(getattr(self.embedding_service, "config", None), "model", "") or ""),
                "text_hash": _hash_text(document),
                "text_preview": document[:240],
                "error": str(exc),
            }

    async def record_validation_outcome(
        self,
        *,
        candidate: dict[str, Any],
        validation: dict[str, Any] | None = None,
        codes: list[str] | None = None,
        source_artifact_id: str | None = None,
        source_action: str | None = None,
        explicit_status: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_codes = _normalize_codes(codes)
        validation = validation if isinstance(validation, dict) else {}
        status = _derive_memory_status(validation=validation, explicit_status=explicit_status)
        similarity_matches = await self.recall_similar_candidates(
            candidate=candidate,
            codes=normalized_codes or None,
            limit=5,
        )
        similarity_edges = [_build_similarity_edge(candidate, match) for match in similarity_matches if isinstance(match, dict)]
        document = _build_candidate_document(candidate, validation=validation, codes=normalized_codes)
        embedding = await self._maybe_embed(document)
        candidate_payload = deepcopy(candidate if isinstance(candidate, dict) else {})
        rating = validation.get("rating") if isinstance(validation.get("rating"), dict) else {}
        metrics = validation.get("metrics") if isinstance(validation.get("metrics"), dict) else {}
        external_evidence = [
            deepcopy(item)
            for item in list(validation.get("external_evidence") or [])
            if isinstance(item, dict)
        ]
        derived_tags = _derive_memory_tags(
            candidate=candidate_payload,
            validation=validation,
            status=status,
            input_tags=tags,
            similarity_edges=similarity_edges,
        )
        memory_record = {
            "candidate": candidate_payload,
            "codes": normalized_codes,
            "status": status,
            "source_artifact_id": str(source_artifact_id or "").strip() or None,
            "source_action": str(source_action or "").strip() or None,
            "signature": _candidate_signature(candidate_payload),
            "rating": deepcopy(rating),
            "metrics": deepcopy(metrics),
            "external_evidence": external_evidence,
            "tags": derived_tags,
            "memory_flags": _derive_memory_flags(similarity_edges, validation),
            "similarity_edges": similarity_edges,
            "memory_document": {
                "text_hash": embedding.get("text_hash"),
                "text_preview": embedding.get("text_preview"),
            },
            "embedding": {
                "available": bool(embedding.get("available")),
                "provider": embedding.get("provider"),
                "model": embedding.get("model"),
                "vector": list(embedding.get("vector") or []),
                "error": embedding.get("error"),
            },
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        return save_factor_candidate_record(memory_record)

    async def get_memory_record(self, artifact_id: str) -> dict[str, Any] | None:
        return await get_factor_candidate_record_async(artifact_id)

    async def list_memory_records(
        self,
        *,
        limit: int = 20,
        codes: list[str] | None = None,
        status: str | None = None,
        family: str | None = None,
    ) -> list[dict[str, Any]]:
        return await list_factor_candidate_records_async(
            limit=limit,
            codes=codes,
            status=status,
            family=family,
        )

    async def recall_similar_candidates(
        self,
        *,
        candidate: dict[str, Any] | None = None,
        query_text: str | None = None,
        codes: list[str] | None = None,
        status: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        query_candidate = candidate if isinstance(candidate, dict) else {}
        query_codes = _normalize_codes(codes)
        query_doc = _normalize_text(
            query_text
            or _build_candidate_document(query_candidate, validation=None, codes=query_codes)
        )
        query_embedding = await self._maybe_embed(query_doc)
        query_dense_vector = build_factor_candidate_dense_vector(query_doc)
        query_family = _normalize_text(query_candidate.get("family")).lower()
        query_expr = _normalize_text(query_candidate.get("expression_dsl"))

        records = await self.list_memory_records(limit=max(50, int(limit) * 10), codes=query_codes or None, status=status)
        db_dense_scores: dict[str, float] = {}
        if query_dense_vector and records:
            entity_ids = [
                str(record.get("artifact_id") or "").strip()
                for record in records
                if str(record.get("artifact_id") or "").strip()
            ]
            if entity_ids:
                try:
                    from ..storage import get_db

                    db = get_db()
                    if hasattr(db, "search_vector_collection"):
                        dense_payload = await db.search_vector_collection(
                            query_embedding=query_dense_vector,
                            collection_name="factor_candidate_embeddings",
                            profile_type="memory",
                            entity_ids=entity_ids,
                            limit=max(20, min(int(limit) * 8, 200)),
                            metric="cosine",
                        )
                        dense_rows = list((dense_payload or {}).get("items") or [])
                    elif hasattr(db, "search_vector_profiles_by_embedding"):
                        dense_rows = await db.search_vector_profiles_by_embedding(
                            query_embedding=query_dense_vector,
                            collection_name="factor_candidate_embeddings",
                            profile_type="memory",
                            entity_ids=entity_ids,
                            limit=max(20, min(int(limit) * 8, 200)),
                            metric="cosine",
                        )
                    else:
                        dense_rows = []
                    for row in dense_rows:
                        entity_id = str(row.get("entity_id") or "").strip()
                        if not entity_id:
                            continue
                        db_dense_scores[entity_id] = float(row.get("similarity") or 0.0)
                except Exception:
                    db_dense_scores = {}
        scored = []
        for record in records:
            candidate_payload = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
            record_doc = _build_candidate_document(candidate_payload, validation=record, codes=_normalize_codes(record.get("codes")))
            record_family = _normalize_text(candidate_payload.get("family")).lower()
            record_expr = _normalize_text(candidate_payload.get("expression_dsl"))
            lexical = _lexical_similarity(
                query_doc,
                record_doc,
                family_match=bool(query_family and query_family == record_family),
                expression_match=bool(query_expr and query_expr == record_expr),
            )
            embedding_score = 0.0
            record_embedding = (record.get("embedding") or {}) if isinstance(record.get("embedding"), dict) else {}
            if query_embedding.get("available") and record_embedding.get("available"):
                embedding_score = max(
                    0.0,
                    _cosine_similarity(query_embedding.get("vector") or [], record_embedding.get("vector") or []),
                )
            dense_score = max(
                float(db_dense_scores.get(str(record.get("artifact_id") or "").strip()) or 0.0),
                float(embedding_score or 0.0),
            )
            code_overlap = 0.0
            record_codes = _normalize_codes(record.get("codes"))
            if query_codes and record_codes:
                union = set(query_codes) | set(record_codes)
                if union:
                    code_overlap = len(set(query_codes) & set(record_codes)) / len(union)
            similarity = max(dense_score, lexical * 0.9) + code_overlap * 0.05
            similarity = max(0.0, min(1.0, similarity))
            scored.append(
                {
                    "artifact_id": record.get("artifact_id"),
                    "status": record.get("status"),
                    "candidate": deepcopy(candidate_payload),
                    "rating": deepcopy(record.get("rating") or {}),
                    "codes": record_codes,
                    "tags": list(record.get("tags") or []),
                    "similarity": round(float(similarity), 6),
                    "lexical_similarity": round(float(lexical), 6),
                    "embedding_similarity": round(float(dense_score), 6),
                    "vector_similarity": round(float(db_dense_scores.get(str(record.get("artifact_id") or "").strip()) or 0.0), 6),
                    "code_overlap": round(float(code_overlap), 6),
                }
            )

        scored.sort(key=lambda item: (item.get("similarity", 0.0), item.get("artifact_id") or ""), reverse=True)
        return scored[: max(1, int(limit))]

    async def build_prompt_memory_context(
        self,
        *,
        codes: list[str] | None = None,
        limit: int = 8,
        query_text: str | None = None,
    ) -> dict[str, Any]:
        normalized_codes = _normalize_codes(codes)
        recent = await self.list_memory_records(limit=max(20, int(limit) * 4), codes=normalized_codes or None)
        success_rows = [item for item in recent if str(item.get("status") or "").strip().lower() == "success"]
        fail_rows = [item for item in recent if str(item.get("status") or "").strip().lower() == "fail"]
        review_rows = [item for item in recent if str(item.get("status") or "").strip().lower() == "review"]
        semantic_matches = await self.recall_similar_candidates(
            query_text=query_text or f"codes:{','.join(normalized_codes)} factor research memory",
            codes=normalized_codes or None,
            limit=max(3, min(6, int(limit))),
        )
        summary_stats = await self.summarize_memory_records(
            limit=max(30, int(limit) * 6),
            codes=normalized_codes or None,
        )
        return {
            "available": bool(success_rows or fail_rows or review_rows),
            "codes": normalized_codes,
            "success_examples": _build_prompt_memory_summary(success_rows, limit=3),
            "failure_examples": _build_prompt_memory_summary(fail_rows, limit=3),
            "review_examples": _build_prompt_memory_summary(review_rows, limit=2),
            "similar_matches": [
                {
                    "artifact_id": row.get("artifact_id"),
                    "status": row.get("status"),
                    "similarity": row.get("similarity"),
                    "name": (row.get("candidate") or {}).get("name"),
                    "family": (row.get("candidate") or {}).get("family"),
                    "expression_dsl": (row.get("candidate") or {}).get("expression_dsl"),
                }
                for row in semantic_matches
            ],
            "query_text_preview": _normalize_text(query_text)[:200] if query_text else "",
            "memory_count": len(recent),
            "summary_stats": summary_stats,
        }

    async def summarize_memory_records(
        self,
        *,
        limit: int = 200,
        codes: list[str] | None = None,
        status: str | None = None,
        family: str | None = None,
    ) -> dict[str, Any]:
        records = await self.list_memory_records(
            limit=max(1, int(limit)),
            codes=codes,
            status=status,
            family=family,
        )
        status_counts: Counter[str] = Counter()
        family_counts: Counter[str] = Counter()
        tag_counts: Counter[str] = Counter()
        top_similarities = []
        duplicate_like_count = 0
        failure_pattern_count = 0
        unstable_count = 0
        embedding_available_count = 0
        external_evidence_records = 0
        unvalidated_external_records = 0
        validated_external_records = 0
        source_counts: Counter[str] = Counter()
        stale_review_count = 0

        for record in records:
            record_status = str(record.get("status") or "").strip().lower()
            if record_status:
                status_counts[record_status] += 1

            candidate = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
            record_family = _normalize_text(candidate.get("family")).lower()
            if record_family:
                family_counts[record_family] += 1

            for tag in _normalize_tags(record.get("tags")):
                tag_counts[tag] += 1

            if bool(((record.get("embedding") or {}) if isinstance(record.get("embedding"), dict) else {}).get("available")):
                embedding_available_count += 1

            memory_flags = record.get("memory_flags") if isinstance(record.get("memory_flags"), dict) else {}
            external_evidence = [
                item
                for item in list(record.get("external_evidence") or [])
                if isinstance(item, dict)
            ]
            tags = _normalize_tags(record.get("tags"))
            if external_evidence or "external_factor_research" in tags or bool(memory_flags.get("seeded_from_external_research")):
                external_evidence_records += 1
                if record_status in {"review", "draft"}:
                    unvalidated_external_records += 1
                elif record_status in {"success", "fail", "validated"}:
                    validated_external_records += 1
                for evidence in external_evidence:
                    source = _normalize_text(evidence.get("source")).lower() or "unknown"
                    source_counts[source] += 1
            source_model = _normalize_text(candidate.get("source_model")).lower()
            generation_trace = candidate.get("generation_trace") if isinstance(candidate.get("generation_trace"), dict) else {}
            generation_mode = _normalize_text(generation_trace.get("mode")).lower()
            if source_model:
                source_counts[f"model:{source_model}"] += 1
            if generation_mode:
                source_counts[f"mode:{generation_mode}"] += 1
            if record_status == "review" and "requires_validation" in tags:
                stale_review_count += 1

            top_similarity = float(memory_flags.get("top_similarity", 0.0) or 0.0)
            if top_similarity > 0:
                top_similarities.append(top_similarity)
            if bool(memory_flags.get("has_duplicate_neighbor")):
                duplicate_like_count += 1
            if bool(memory_flags.get("has_failure_pattern_neighbor")):
                failure_pattern_count += 1
            if bool(memory_flags.get("unstable")):
                unstable_count += 1

        return {
            "total_records": len(records),
            "status_counts": dict(status_counts),
            "family_counts": dict(family_counts.most_common(8)),
            "top_tags": dict(tag_counts.most_common(12)),
            "duplicate_like_count": duplicate_like_count,
            "failure_pattern_count": failure_pattern_count,
            "unstable_count": unstable_count,
            "embedding_available_count": embedding_available_count,
            "external_evidence_records": external_evidence_records,
            "unvalidated_external_records": unvalidated_external_records,
            "validated_external_records": validated_external_records,
            "candidate_source_counts": dict(source_counts.most_common(12)),
            "quality_flags": {
                "unvalidated_external_evidence": unvalidated_external_records > 0,
                "duplicate_like_candidates": duplicate_like_count > 0,
                "failure_pattern_neighbors": failure_pattern_count > 0,
                "unstable_candidates": unstable_count > 0,
                "stale_review_candidates": stale_review_count > 0,
            },
            "avg_top_similarity": round(float(np.mean(top_similarities)), 6) if top_similarities else 0.0,
            "max_top_similarity": round(float(max(top_similarities)), 6) if top_similarities else 0.0,
        }

    async def annotate_generated_candidates(
        self,
        candidates: list[dict[str, Any]],
        *,
        codes: list[str] | None = None,
        high_similarity_threshold: float = 0.98,
    ) -> dict[str, Any]:
        normalized_codes = _normalize_codes(codes)
        rows = []
        warnings = []

        for candidate in list(candidates or []):
            if not isinstance(candidate, dict):
                continue
            matches = await self.recall_similar_candidates(
                candidate=candidate,
                codes=normalized_codes or None,
                limit=1,
            )
            top = matches[0] if matches else {}
            similarity = float(top.get("similarity", 0.0) or 0.0)
            top_edge = _build_similarity_edge(candidate, top) if top else {}
            if similarity >= float(high_similarity_threshold):
                warnings.append(
                    f"candidate {candidate.get('name') or 'unknown'} highly similar to memory {top.get('artifact_id')}"
                )
            duplicate_risk = "high" if similarity >= high_similarity_threshold else ("medium" if similarity >= 0.90 else "low")
            enriched = deepcopy(candidate)
            trace = dict(enriched.get("generation_trace") or {})
            trace["memory_similarity"] = {
                "top_artifact_id": top.get("artifact_id"),
                "top_status": top.get("status"),
                "similarity": round(similarity, 6),
                    "lexical_similarity": top.get("lexical_similarity", 0.0),
                    "embedding_similarity": top.get("embedding_similarity", 0.0),
                    "code_overlap": top.get("code_overlap", 0.0),
                    "edge_type": top_edge.get("edge_type"),
                    "reason": top_edge.get("reason"),
                }
            if top_edge:
                trace["memory_similarity_edges"] = [top_edge]
            enriched["generation_trace"] = trace
            enriched["memory_penalty"] = round(similarity, 6)
            enriched["duplicate_risk"] = duplicate_risk
            enriched["duplicate_reason"] = top_edge.get("reason") if top_edge else ""
            enriched["duplicate_block_recommended"] = bool(
                top_edge
                and (
                    top_edge.get("edge_type") in {"duplicate", "failure_pattern"}
                    or similarity >= float(high_similarity_threshold)
                )
            )
            rows.append(enriched)

        rows.sort(key=lambda item: (float(item.get("memory_penalty", 0.0)), str(item.get("name") or "")))
        return {
            "candidates": rows,
            "warnings": list(dict.fromkeys(warnings)),
        }

    def apply_duplicate_policy(
        self,
        candidates: list[dict[str, Any]],
        *,
        dedup_mode: str = "penalty",
        high_similarity_threshold: float = 0.98,
        failure_similarity_threshold: float = 0.93,
    ) -> dict[str, Any]:
        mode = str(dedup_mode or "penalty").strip().lower()
        if mode in {"none", "off"}:
            mode = "penalty"

        kept = []
        blocked = []
        warnings = []

        for candidate in list(candidates or []):
            if not isinstance(candidate, dict):
                continue
            memory_similarity = (
                ((candidate.get("generation_trace") or {}) if isinstance(candidate.get("generation_trace"), dict) else {}).get("memory_similarity")
                or {}
            )
            similarity = float(memory_similarity.get("similarity", 0.0) or 0.0)
            top_status = str(memory_similarity.get("top_status") or "").strip().lower()
            edge_type = str(memory_similarity.get("edge_type") or "").strip().lower()

            blocked_reason = ""
            if mode in {"block", "block_high_similarity"} and similarity >= float(high_similarity_threshold):
                blocked_reason = "high_similarity_duplicate"
            if not blocked_reason and mode in {"block", "block_failures", "hybrid"} and top_status == "fail" and similarity >= float(failure_similarity_threshold):
                blocked_reason = "similar_to_historical_failure"
            if not blocked_reason and mode in {"block", "block_recommended"} and bool(candidate.get("duplicate_block_recommended")):
                blocked_reason = f"memory_recommended_block:{edge_type or 'duplicate'}"

            if blocked_reason:
                row = deepcopy(candidate)
                row["blocked_reason"] = blocked_reason
                row["duplicate_blocked"] = True
                blocked.append(row)
                warnings.append(f"blocked {row.get('name') or 'unknown'} because {blocked_reason}")
            else:
                kept.append(deepcopy(candidate))

        return {
            "mode": mode,
            "kept_candidates": kept,
            "blocked_candidates": blocked,
            "summary": {
                "input_count": len([item for item in list(candidates or []) if isinstance(item, dict)]),
                "kept_count": len(kept),
                "blocked_count": len(blocked),
                "blocked_ratio": round((len(blocked) / max(1, len(kept) + len(blocked))), 6),
            },
            "warnings": list(dict.fromkeys(warnings)),
        }


    async def record_feedback(
        self,
        *,
        artifact_id: str,
        feedback: dict[str, Any],
        source_feedback_artifact_id: str | None = None,
        source_model_registry_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """将策略孵化期的真实结果写回 factor memory 记录。

        feedback 字段包括：
          - decay_detected: bool
          - regime_shift_detected: bool
          - crowding_elevated: bool
          - runtime_pressure_detected: bool
          - forward_return / forward_ic_5d: float
          - recommended_action: str (monitor|schedule_retrain|deprecate)
          - 其他任意补充字段
        """
        existing = await self.get_memory_record(artifact_id)
        if not existing:
            raise ValueError(f"memory record not found: {artifact_id}")

        decay_detected = bool(feedback.get("decay_detected"))
        regime_shift_detected = bool(feedback.get("regime_shift_detected"))
        crowding_elevated = bool(feedback.get("crowding_elevated"))
        runtime_pressure = bool(feedback.get("runtime_pressure_detected"))
        forward_return = feedback.get("forward_return") or feedback.get("forward_ic_5d")
        recommended_action = str(feedback.get("recommended_action") or "monitor").strip()

        current_status = str(existing.get("status") or "").strip()
        new_status = current_status
        if (decay_detected or regime_shift_detected) and current_status == "success":
            new_status = "degraded"

        feedback_entry: dict[str, Any] = {
            "source_feedback_artifact_id": source_feedback_artifact_id,
            "source_model_registry_artifact_id": source_model_registry_artifact_id,
            "decay_detected": decay_detected,
            "regime_shift_detected": regime_shift_detected,
            "crowding_elevated": crowding_elevated,
            "runtime_pressure_detected": runtime_pressure,
            "forward_return": forward_return,
            "recommended_action": recommended_action,
            "feedback_at": _now_iso(),
        }
        skip_keys = {
            "decay_detected", "regime_shift_detected", "crowding_elevated",
            "runtime_pressure_detected", "forward_return", "forward_ic_5d", "recommended_action",
        }
        for k, v in feedback.items():
            if k not in skip_keys:
                feedback_entry[k] = v

        existing_feedback = list(existing.get("runtime_feedback") or [])
        existing_feedback.insert(0, feedback_entry)
        existing_feedback = existing_feedback[:10]

        updated = {
            **existing,
            "status": new_status,
            "runtime_feedback": existing_feedback,
            "last_feedback_at": _now_iso(),
            "last_feedback_recommended_action": recommended_action,
            "updated_at": _now_iso(),
        }
        return save_factor_candidate_record(updated, artifact_id=artifact_id)


_factor_research_memory_service: Optional[FactorResearchMemoryService] = None


def get_factor_research_memory_service() -> FactorResearchMemoryService:
    global _factor_research_memory_service
    if _factor_research_memory_service is None:
        _factor_research_memory_service = FactorResearchMemoryService()
    return _factor_research_memory_service
