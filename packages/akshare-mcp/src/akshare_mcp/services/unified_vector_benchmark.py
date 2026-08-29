"""Exact-vs-ANN benchmark helpers for unified vector collections."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any

from ..storage.sqlite.vector_unified import VectorUnifiedMixin
from ..vector_collection_scope import resolve_vector_collection_name


def _normalize_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 5000) -> int:
    try:
        resolved = int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        resolved = int(default)
    return max(minimum, min(resolved, maximum))


def _percentile(values: list[float], pct: float) -> float:
    rows = sorted(float(item) for item in list(values or []))
    if not rows:
        return 0.0
    if len(rows) == 1:
        return round(rows[0], 6)
    rank = max(0.0, min(float(pct or 0.0), 100.0)) / 100.0 * (len(rows) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return round(rows[lower], 6)
    weight = rank - lower
    return round((rows[lower] * (1.0 - weight)) + (rows[upper] * weight), 6)


def _select_query_rows(rows: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    if len(rows) <= sample_size:
        return list(rows)
    if sample_size <= 1:
        return [rows[0]]
    picked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for idx in range(sample_size):
        candidate_idx = int(round(idx * (len(rows) - 1) / max(sample_size - 1, 1)))
        if candidate_idx in seen:
            continue
        seen.add(candidate_idx)
        picked.append(rows[candidate_idx])
    if len(picked) < sample_size:
        for idx, row in enumerate(rows):
            if idx in seen:
                continue
            picked.append(row)
            if len(picked) >= sample_size:
                break
    return picked[:sample_size]


def _dcg(ids: list[str], relevance: dict[str, float], top_k: int) -> float:
    score = 0.0
    for idx, item_id in enumerate(list(ids or [])[: max(1, int(top_k or 1))]):
        rel = max(0.0, float(relevance.get(str(item_id), 0.0) or 0.0))
        if rel <= 0:
            continue
        score += rel / math.log2(idx + 2.0)
    return float(score)


def _mrr(ids: list[str], relevant_ids: set[str], top_k: int) -> float:
    for idx, item_id in enumerate(list(ids or [])[: max(1, int(top_k or 1))]):
        if str(item_id) in relevant_ids:
            return round(1.0 / float(idx + 1), 6)
    return 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _VectorBenchmarkHelper(VectorUnifiedMixin):
    @staticmethod
    def _decode_json_field(value, default):
        return value if value is not None else default


_HELPER = _VectorBenchmarkHelper()


async def benchmark_vector_collection_search(
    db,
    *,
    collection_name: str,
    profile_type: str | None = None,
    version: str | None = None,
    index_version: str | None = None,
    sample_size: Any = 30,
    top_k: Any = 10,
    limit_profiles: Any = 5000,
    metric: str = "cosine",
    persist_snapshot_metrics: bool = True,
) -> dict[str, Any]:
    resolved_profile_type = str(profile_type or "").strip() or None
    resolved_collection = resolve_vector_collection_name(collection_name, resolved_profile_type)
    if not resolved_collection:
        raise ValueError("collection_name is required")
    resolved_sample_size = _normalize_positive_int(sample_size, 30, minimum=1, maximum=500)
    resolved_top_k = _normalize_positive_int(top_k, 10, minimum=1, maximum=100)
    resolved_limit_profiles = _normalize_positive_int(limit_profiles, 5000, minimum=10, maximum=100000)
    resolved_metric = str(metric or "cosine").strip().lower() or "cosine"

    collection = await db.get_vector_collection(resolved_collection) if hasattr(db, "get_vector_collection") else None
    active_version = str((collection or {}).get("active_version") or "").strip() or None
    resolved_index_version = str(index_version or active_version or "").strip() or None
    snapshot = None
    resolved_profile_version = str(version or "").strip() or None
    if resolved_index_version and hasattr(db, "list_vector_index_snapshots"):
        snapshots = await db.list_vector_index_snapshots(
            collection_name=resolved_collection,
            index_version=resolved_index_version,
            profile_type=resolved_profile_type,
            latest_only=True,
            limit=1,
        )
        snapshot = snapshots[0] if snapshots else None
        if snapshot and not resolved_profile_version:
            resolved_profile_version = (
                str((snapshot.get("metadata") or {}).get("profile_version") or "").strip()
                or str(snapshot.get("index_version") or "").strip()
                or None
            )
    if not resolved_profile_version and resolved_index_version:
        resolved_profile_version = resolved_index_version

    universe_source = "vector_profiles"
    universe_rows: list[dict[str, Any]] = []
    if resolved_index_version and hasattr(db, "list_vector_index_items"):
        universe_rows = await db.list_vector_index_items(
            collection_name=resolved_collection,
            index_version=resolved_index_version,
            profile_type=resolved_profile_type,
            limit=resolved_limit_profiles,
        )
        if universe_rows:
            universe_source = "vector_index_items"
    if not universe_rows:
        universe_rows = await db.list_vector_profiles(
            collection_name=resolved_collection,
            profile_type=resolved_profile_type,
            version=resolved_profile_version,
            limit=resolved_limit_profiles,
        )

    filtered_rows: list[dict[str, Any]] = []
    dim_counts: dict[int, int] = {}
    for row in list(universe_rows or []):
        embedding = _HELPER._normalize_embedding((row or {}).get("embedding") or [])
        if not embedding:
            continue
        payload = dict(row or {})
        payload["embedding"] = embedding
        filtered_rows.append(payload)
        dim_counts[len(embedding)] = dim_counts.get(len(embedding), 0) + 1
    dominant_dim = max(dim_counts.items(), key=lambda item: (item[1], item[0]))[0] if dim_counts else 0
    if dominant_dim > 0:
        filtered_rows = [row for row in filtered_rows if len(row.get("embedding") or []) == dominant_dim]
    filtered_rows.sort(key=lambda row: (str(row.get("stock_code") or ""), str(row.get("entity_id") or ""), int(row.get("id") or 0)))

    if len(filtered_rows) < 2:
        return {
            "collection_name": resolved_collection,
            "profile_type": resolved_profile_type,
            "profile_version": resolved_profile_version,
            "index_version": resolved_index_version,
            "sample_size": 0,
            "top_k": resolved_top_k,
            "status": "skipped",
            "reason": "insufficient_profiles",
            "universe_count": len(filtered_rows),
            "universe_source": universe_source,
        }

    query_rows = _select_query_rows(filtered_rows, min(resolved_sample_size, len(filtered_rows)))
    latency_exact_ms: list[float] = []
    latency_ann_ms: list[float] = []
    recall_scores: list[float] = []
    mrr_scores: list[float] = []
    ndcg_scores: list[float] = []
    backend_counts: dict[str, int] = {}
    coarse_candidate_counts: list[float] = []
    coarse_candidate_ratios: list[float] = []
    per_query: list[dict[str, Any]] = []

    for query_row in query_rows:
        query_entity_id = str(query_row.get("entity_id") or "").strip()
        query_embedding = list(query_row.get("embedding") or [])
        if not query_entity_id or not query_embedding:
            continue

        started_exact = time.perf_counter()
        exact_scored: list[dict[str, Any]] = []
        for candidate in filtered_rows:
            candidate_entity_id = str(candidate.get("entity_id") or "").strip()
            candidate_embedding = list(candidate.get("embedding") or [])
            if not candidate_entity_id or candidate_entity_id == query_entity_id:
                continue
            if len(candidate_embedding) != len(query_embedding):
                continue
            exact_scored.append(
                {
                    "entity_id": candidate_entity_id,
                    "similarity": _HELPER._vector_similarity(query_embedding, candidate_embedding, metric=resolved_metric),
                    "bucket_id": candidate.get("bucket_id"),
                }
            )
        exact_scored.sort(key=lambda item: float(item.get("similarity") or 0.0), reverse=True)
        exact_top = exact_scored[:resolved_top_k]
        latency_exact_ms.append((time.perf_counter() - started_exact) * 1000.0)

        started_ann = time.perf_counter()
        ann_result = await db.search_vector_collection(
            collection_name=resolved_collection,
            query_embedding=query_embedding,
            index_version=resolved_index_version,
            version=resolved_profile_version,
            profile_type=resolved_profile_type,
            exclude_entity_id=query_entity_id,
            limit=resolved_top_k,
            metric=resolved_metric,
        )
        latency_ann_ms.append((time.perf_counter() - started_ann) * 1000.0)

        ann_items = list((ann_result or {}).get("items") or [])
        ann_ids = [str(item.get("entity_id") or "").strip() for item in ann_items if str(item.get("entity_id") or "").strip()]
        exact_ids = [str(item.get("entity_id") or "").strip() for item in exact_top if str(item.get("entity_id") or "").strip()]
        exact_rel = {
            str(item.get("entity_id") or "").strip(): max(0.0, float(item.get("similarity") or 0.0))
            for item in exact_top
            if str(item.get("entity_id") or "").strip()
        }
        exact_relevant_ids = set(exact_ids)
        overlap = len([item_id for item_id in ann_ids if item_id in exact_relevant_ids])
        recall = float(overlap / max(len(exact_relevant_ids), 1))
        recall_scores.append(recall)
        mrr_scores.append(_mrr(ann_ids, exact_relevant_ids, resolved_top_k))
        idcg = _dcg(exact_ids, exact_rel, resolved_top_k)
        dcg = _dcg(ann_ids, exact_rel, resolved_top_k)
        ndcg_scores.append(round(float(dcg / idcg), 6) if idcg > 1e-12 else 0.0)

        backend_used = str((ann_result or {}).get("backend_used") or "unknown").strip() or "unknown"
        backend_counts[backend_used] = backend_counts.get(backend_used, 0) + 1

        candidate_bucket_ids = [str(item).strip() for item in list((ann_result or {}).get("candidate_bucket_ids") or []) if str(item).strip()]
        coarse_candidate_count = None
        if candidate_bucket_ids and any(str(row.get("bucket_id") or "").strip() for row in filtered_rows):
            coarse_candidate_count = sum(
                1
                for row in filtered_rows
                if str(row.get("entity_id") or "").strip() != query_entity_id
                and str(row.get("bucket_id") or "").strip() in candidate_bucket_ids
            )
            coarse_candidate_counts.append(float(coarse_candidate_count))
            coarse_candidate_ratios.append(float(coarse_candidate_count / max(len(filtered_rows) - 1, 1)))

        per_query.append(
            {
                "entity_id": query_entity_id,
                "backend_used": backend_used,
                "fallback_used": bool((ann_result or {}).get("fallback_used", False)),
                "fallback_reason": (ann_result or {}).get("fallback_reason"),
                "query_bucket_id": (ann_result or {}).get("query_bucket_id"),
                "candidate_bucket_ids": candidate_bucket_ids,
                "coarse_candidate_count": coarse_candidate_count,
                "exact_candidate_count": max(len(filtered_rows) - 1, 0),
                "recall_at_k": round(recall, 6),
                "mrr_at_k": mrr_scores[-1],
                "ndcg_at_k": ndcg_scores[-1],
                "exact_ms": round(latency_exact_ms[-1], 6),
                "ann_ms": round(latency_ann_ms[-1], 6),
            }
        )

    query_count = len(per_query)
    if query_count <= 0:
        return {
            "collection_name": resolved_collection,
            "profile_type": resolved_profile_type,
            "profile_version": resolved_profile_version,
            "index_version": resolved_index_version,
            "sample_size": 0,
            "top_k": resolved_top_k,
            "status": "skipped",
            "reason": "no_valid_queries",
            "universe_count": len(filtered_rows),
            "universe_source": universe_source,
        }

    result = {
        "collection_name": resolved_collection,
        "profile_type": resolved_profile_type,
        "profile_version": resolved_profile_version,
        "index_version": resolved_index_version,
        "status": "completed",
        "metric": resolved_metric,
        "top_k": resolved_top_k,
        "sample_size": query_count,
        "universe_count": len(filtered_rows),
        "universe_source": universe_source,
        "dominant_dim": dominant_dim,
        "backend_used_counts": backend_counts,
        "degraded_query_count": sum(1 for row in per_query if row.get("backend_used") == "exact_json"),
        "ann_backend_only_query_count": sum(
            1
            for row in per_query
            if row.get("backend_used") == "sqlite_python_index_item" and not bool(row.get("fallback_used", False))
        ),
        "coarse_pruning_query_count": len(coarse_candidate_counts),
        "retrieval_quality": {
            "recall_at_k": round(float(sum(recall_scores) / max(len(recall_scores), 1)), 6),
            "mrr_at_k": round(float(sum(mrr_scores) / max(len(mrr_scores), 1)), 6),
            "ndcg_at_k": round(float(sum(ndcg_scores) / max(len(ndcg_scores), 1)), 6),
        },
        "latency_ms": {
            "exact_p50": _percentile(latency_exact_ms, 50),
            "exact_p95": _percentile(latency_exact_ms, 95),
            "ann_p50": _percentile(latency_ann_ms, 50),
            "ann_p95": _percentile(latency_ann_ms, 95),
        },
        "coarse_pruning": {
            "avg_candidate_count": round(float(sum(coarse_candidate_counts) / max(len(coarse_candidate_counts), 1)), 6) if coarse_candidate_counts else None,
            "avg_candidate_ratio": round(float(sum(coarse_candidate_ratios) / max(len(coarse_candidate_ratios), 1)), 6) if coarse_candidate_ratios else None,
        },
        "queries": per_query,
        "benchmark_persisted": False,
    }
    if persist_snapshot_metrics and snapshot and hasattr(db, "save_vector_index_snapshot"):
        benchmark_metrics = {
            "status": result["status"],
            "metric": resolved_metric,
            "sample_size": query_count,
            "top_k": resolved_top_k,
            "backend_used_counts": dict(result["backend_used_counts"] or {}),
            "degraded_query_count": int(result["degraded_query_count"] or 0),
            "ann_backend_only_query_count": int(result["ann_backend_only_query_count"] or 0),
            "coarse_pruning_query_count": int(result["coarse_pruning_query_count"] or 0),
            "retrieval_quality": dict(result["retrieval_quality"] or {}),
            "latency_ms": dict(result["latency_ms"] or {}),
            "coarse_pruning": dict(result["coarse_pruning"] or {}),
            "benchmarked_at": _now_iso(),
        }
        snapshot_payload = dict(snapshot or {})
        snapshot_payload["metrics"] = {
            **dict(snapshot_payload.get("metrics") or {}),
            "benchmark": benchmark_metrics,
        }
        snapshot_payload["metadata"] = {
            **dict(snapshot_payload.get("metadata") or {}),
            "benchmark_updated_at": benchmark_metrics["benchmarked_at"],
        }
        await db.save_vector_index_snapshot(snapshot_payload)
        result["benchmark_persisted"] = True
    return result
