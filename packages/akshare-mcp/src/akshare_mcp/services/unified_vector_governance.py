"""Unified vector collection snapshot / ANN governance helpers."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

from ..vector_collection_scope import LEGACY_MARKET_DOC_COLLECTION, normalize_profile_type, resolve_vector_collection_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 5000) -> int:
    try:
        resolved = int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        resolved = int(default)
    return max(minimum, min(resolved, maximum))


def _bucket_label(index: int) -> str:
    return f"b_{int(index or 0):04d}"


def _normalize_embedding(values: Any) -> list[float]:
    resolved = [float(item) for item in list(values or [])]
    if not resolved:
        return []
    norm = math.sqrt(sum(item * item for item in resolved))
    if norm <= 1e-12:
        return []
    return [float(item / norm) for item in resolved]


def _vector_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return float(sum(l * r for l, r in zip(left, right)))


def _mean_embedding(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    if dim <= 0:
        return []
    totals = [0.0] * dim
    for vector in vectors:
        if len(vector) != dim:
            continue
        for idx, value in enumerate(vector):
            totals[idx] += float(value)
    return _normalize_embedding([value / max(len(vectors), 1) for value in totals])


def _build_bucket_layout(
    rows: list[dict[str, Any]],
    *,
    bucket_count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    valid: list[tuple[dict[str, Any], list[float]]] = []
    skipped: list[dict[str, Any]] = []
    for row in list(rows or []):
        payload = dict(row or {})
        embedding = _normalize_embedding(payload.get("embedding") or [])
        if not embedding:
            skipped.append(
                {
                    "profile_id": payload.get("id"),
                    "entity_id": payload.get("entity_id"),
                    "reason": "empty_embedding",
                }
            )
            continue
        valid.append((payload, embedding))
    if not valid:
        return {
            "profile_count": 0,
            "bucket_count": 0,
            "vector_dim": 0,
            "centroids": [],
            "metadata": {"skipped_profiles": skipped},
        }, []

    dim_counts: dict[int, int] = {}
    for _, embedding in valid:
        dim_counts[len(embedding)] = dim_counts.get(len(embedding), 0) + 1
    dominant_dim = max(dim_counts.items(), key=lambda item: (item[1], item[0]))[0]
    selected = [(payload, embedding) for payload, embedding in valid if len(embedding) == dominant_dim]
    for payload, embedding in valid:
        if len(embedding) != dominant_dim:
            skipped.append(
                {
                    "profile_id": payload.get("id"),
                    "entity_id": payload.get("entity_id"),
                    "reason": f"dim_mismatch:{len(embedding)}",
                }
            )
    if not selected:
        return {
            "profile_count": 0,
            "bucket_count": 0,
            "vector_dim": dominant_dim,
            "centroids": [],
            "metadata": {"skipped_profiles": skipped},
        }, []

    ordered = sorted(
        selected,
        key=lambda item: (
            str(item[0].get("stock_code") or ""),
            str(item[0].get("entity_id") or ""),
            int(item[0].get("id") or 0),
        ),
    )
    vectors = [embedding for _, embedding in ordered]
    resolved_bucket_count = max(1, min(int(bucket_count or 1), len(vectors)))
    if resolved_bucket_count == 1:
        centroids = [_mean_embedding(vectors)]
    else:
        initial = [
            int(round(idx * (len(vectors) - 1) / max(resolved_bucket_count - 1, 1)))
            for idx in range(resolved_bucket_count)
        ]
        centroids = [list(vectors[idx]) for idx in initial]
        for _ in range(12):
            assignments: list[list[int]] = [[] for _ in range(resolved_bucket_count)]
            for row_idx, vector in enumerate(vectors):
                sims = [_vector_similarity(vector, centroid) for centroid in centroids]
                best_idx = int(max(range(resolved_bucket_count), key=lambda idx: sims[idx]))
                assignments[best_idx].append(row_idx)
            updated: list[list[float]] = []
            max_shift = 0.0
            for centroid_idx, members in enumerate(assignments):
                if not members:
                    updated.append(list(centroids[centroid_idx]))
                    continue
                new_centroid = _mean_embedding([vectors[row_idx] for row_idx in members]) or list(centroids[centroid_idx])
                shift = math.sqrt(
                    sum((float(new_centroid[idx]) - float(centroids[centroid_idx][idx])) ** 2 for idx in range(dominant_dim))
                )
                max_shift = max(max_shift, float(shift))
                updated.append(list(new_centroid))
            centroids = updated
            if max_shift <= 1e-4:
                break

    bucket_members: list[list[int]] = [[] for _ in range(resolved_bucket_count)]
    assignments_meta: list[tuple[int, float]] = []
    for row_idx, vector in enumerate(vectors):
        sims = [_vector_similarity(vector, centroid) for centroid in centroids]
        best_idx = int(max(range(resolved_bucket_count), key=lambda idx: sims[idx]))
        best_score = float(sims[best_idx])
        bucket_members[best_idx].append(row_idx)
        assignments_meta.append((best_idx, best_score))

    centroid_rows: list[dict[str, Any]] = []
    for centroid_idx, centroid in enumerate(centroids):
        neighbors: list[str] = []
        if resolved_bucket_count > 1:
            scored_neighbors = []
            for other_idx, other_centroid in enumerate(centroids):
                if other_idx == centroid_idx:
                    continue
                scored_neighbors.append((other_idx, _vector_similarity(centroid, other_centroid)))
            scored_neighbors.sort(key=lambda item: item[1], reverse=True)
            neighbors = [_bucket_label(item[0]) for item in scored_neighbors[: min(2, len(scored_neighbors))]]
        centroid_rows.append(
            {
                "bucket_id": _bucket_label(centroid_idx),
                "centroid": [round(float(item), 8) for item in centroid],
                "size": len(bucket_members[centroid_idx]),
                "neighbors": neighbors,
                "mean_similarity": round(
                    float(
                        sum(assignments_meta[row_idx][1] for row_idx in bucket_members[centroid_idx]) / max(len(bucket_members[centroid_idx]), 1)
                    ),
                    6,
                ),
            }
        )

    items: list[dict[str, Any]] = []
    for row_idx, (payload, embedding) in enumerate(ordered):
        bucket_idx, coarse_score = assignments_meta[row_idx]
        items.append(
            {
                "profile_id": payload.get("id"),
                "entity_type": payload.get("entity_type"),
                "entity_id": payload.get("entity_id"),
                "stock_code": payload.get("stock_code"),
                "profile_type": payload.get("profile_type"),
                "model_id": payload.get("model_id"),
                "metric": payload.get("metric") or "cosine",
                "vector_dim": int(payload.get("vector_dim") or dominant_dim),
                "bucket_id": _bucket_label(bucket_idx),
                "coarse_score": round(float(coarse_score), 6),
                "embedding": [round(float(item), 8) for item in embedding],
                "metadata": {
                    **dict(payload.get("metadata") or {}),
                    "source_profile_version": payload.get("version"),
                },
            }
        )

    return {
        "profile_count": len(items),
        "bucket_count": resolved_bucket_count,
        "vector_dim": dominant_dim,
        "centroids": centroid_rows,
        "metadata": {
            "skipped_profiles": skipped,
            "dominant_dim": dominant_dim,
            "vector_dim_counts": {str(key): int(value) for key, value in sorted(dim_counts.items())},
            "cluster_sizes": {_bucket_label(idx): len(members) for idx, members in enumerate(bucket_members)},
        },
    }, items


async def audit_vector_collection_quality(
    db,
    *,
    collection_name: str,
    profile_type: str | None = None,
    profile_version: str | None = None,
    index_version: str | None = None,
    expected_profile_count: int | None = None,
    expect_active: bool = False,
) -> dict[str, Any]:
    resolved_collection = resolve_vector_collection_name(collection_name, profile_type)
    resolved_profile_type = normalize_profile_type(profile_type)
    resolved_profile_version = str(profile_version or "").strip() or None
    resolved_index_version = str(index_version or "").strip() or None

    profile_rows_count = 0
    index_item_rows_count = 0
    profile_store_missing_count: int | None = None
    index_item_store_missing_count: int | None = None
    profile_dim_mismatch_count = 0
    index_item_dim_mismatch_count = 0
    active_snapshot_count = 0
    profile_types: list[str] = []
    index_item_profile_types: list[str] = []
    snapshot_profile_types: list[str] = []
    profile_vector_dims: list[int] = []
    index_item_vector_dims: list[int] = []
    snapshot_vector_dims: list[int] = []

    if hasattr(db, "acquire"):
        try:
            async with db.acquire() as conn:
                profile_where = ["collection_name = $1"]
                profile_args: list[Any] = [resolved_collection]
                profile_idx = 2
                if resolved_profile_version:
                    profile_where.append(f"version = ${profile_idx}")
                    profile_args.append(resolved_profile_version)
                    profile_idx += 1
                if resolved_profile_type:
                    profile_where.append(f"COALESCE(profile_type, '') = ${profile_idx}")
                    profile_args.append(resolved_profile_type)
                    profile_idx += 1
                profile_where_sql = " AND ".join(profile_where)

                item_where = ["collection_name = $1"]
                item_args: list[Any] = [resolved_collection]
                item_idx = 2
                if resolved_index_version:
                    item_where.append(f"index_version = ${item_idx}")
                    item_args.append(resolved_index_version)
                    item_idx += 1
                if resolved_profile_type:
                    item_where.append(f"COALESCE(profile_type, '') = ${item_idx}")
                    item_args.append(resolved_profile_type)
                    item_idx += 1
                item_where_sql = " AND ".join(item_where)

                snapshot_where = ["collection_name = $1"]
                snapshot_args: list[Any] = [resolved_collection]
                snapshot_idx = 2
                if resolved_index_version:
                    snapshot_where.append(f"index_version = ${snapshot_idx}")
                    snapshot_args.append(resolved_index_version)
                    snapshot_idx += 1
                if resolved_profile_type:
                    snapshot_where.append(f"COALESCE(profile_type, '') = ${snapshot_idx}")
                    snapshot_args.append(resolved_profile_type)
                    snapshot_idx += 1
                snapshot_where_sql = " AND ".join(snapshot_where)
                active_scope_where = ["collection_name = $1"]
                active_scope_args: list[Any] = [resolved_collection]
                active_scope_idx = 2
                if resolved_profile_type:
                    active_scope_where.append(f"COALESCE(profile_type, '') = ${active_scope_idx}")
                    active_scope_args.append(resolved_profile_type)
                    active_scope_idx += 1
                active_scope_where_sql = " AND ".join(active_scope_where)

                profile_rows_count = int(
                    await conn.fetchval(
                        f"SELECT COUNT(*) FROM vector_profiles WHERE {profile_where_sql}",
                        *profile_args,
                    )
                    or 0
                )
                index_item_rows_count = int(
                    await conn.fetchval(
                        f"SELECT COUNT(*) FROM vector_index_items WHERE {item_where_sql}",
                        *item_args,
                    )
                    or 0
                )
                profile_dim_mismatch_count = int(
                    await conn.fetchval(
                        f"""
                        SELECT COUNT(*)
                        FROM vector_profiles
                        WHERE {profile_where_sql}
                          AND vector_dim <> jsonb_array_length(embedding_json)
                        """,
                        *profile_args,
                    )
                    or 0
                )
                index_item_dim_mismatch_count = int(
                    await conn.fetchval(
                        f"""
                        SELECT COUNT(*)
                        FROM vector_index_items
                        WHERE {item_where_sql}
                          AND vector_dim <> jsonb_array_length(embedding_json)
                        """,
                        *item_args,
                    )
                    or 0
                )
                active_snapshot_count = int(
                    await conn.fetchval(
                        f"""
                        SELECT COUNT(*)
                        FROM vector_index_snapshots
                        WHERE {active_scope_where_sql}
                          AND status = 'active'
                        """,
                        *active_scope_args,
                    )
                    or 0
                )

                profile_type_rows = await conn.fetch(
                    """
                    SELECT DISTINCT COALESCE(profile_type, '') AS profile_type
                    FROM vector_profiles
                    WHERE collection_name = $1
                      AND ($2 IS NULL OR version = $2)
                    ORDER BY COALESCE(profile_type, '')
                    """,
                    resolved_collection,
                    resolved_profile_version,
                )
                item_type_rows = await conn.fetch(
                    """
                    SELECT DISTINCT COALESCE(profile_type, '') AS profile_type
                    FROM vector_index_items
                    WHERE collection_name = $1
                      AND ($2 IS NULL OR index_version = $2)
                    ORDER BY COALESCE(profile_type, '')
                    """,
                    resolved_collection,
                    resolved_index_version,
                )
                snapshot_type_rows = await conn.fetch(
                    """
                    SELECT DISTINCT COALESCE(profile_type, '') AS profile_type
                    FROM vector_index_snapshots
                    WHERE collection_name = $1
                      AND ($2 IS NULL OR index_version = $2)
                    ORDER BY COALESCE(profile_type, '')
                    """,
                    resolved_collection,
                    resolved_index_version,
                )
                profile_dim_rows = await conn.fetch(
                    """
                    SELECT DISTINCT vector_dim
                    FROM vector_profiles
                    WHERE collection_name = $1
                      AND ($2 IS NULL OR version = $2)
                      AND ($3 IS NULL OR COALESCE(profile_type, '') = $3)
                    ORDER BY vector_dim
                    """,
                    resolved_collection,
                    resolved_profile_version,
                    resolved_profile_type,
                )
                item_dim_rows = await conn.fetch(
                    """
                    SELECT DISTINCT vector_dim
                    FROM vector_index_items
                    WHERE collection_name = $1
                      AND ($2 IS NULL OR index_version = $2)
                      AND ($3 IS NULL OR COALESCE(profile_type, '') = $3)
                    ORDER BY vector_dim
                    """,
                    resolved_collection,
                    resolved_index_version,
                    resolved_profile_type,
                )
                snapshot_dim_rows = await conn.fetch(
                    """
                    SELECT DISTINCT vector_dim
                    FROM vector_index_snapshots
                    WHERE collection_name = $1
                      AND ($2 IS NULL OR index_version = $2)
                      AND ($3 IS NULL OR COALESCE(profile_type, '') = $3)
                    ORDER BY vector_dim
                    """,
                    resolved_collection,
                    resolved_index_version,
                    resolved_profile_type,
                )
                profile_types = [str(dict(row).get("profile_type") or "") for row in profile_type_rows]
                index_item_profile_types = [str(dict(row).get("profile_type") or "") for row in item_type_rows]
                snapshot_profile_types = [str(dict(row).get("profile_type") or "") for row in snapshot_type_rows]
                profile_vector_dims = [int(dict(row).get("vector_dim") or 0) for row in profile_dim_rows if int(dict(row).get("vector_dim") or 0) > 0]
                index_item_vector_dims = [int(dict(row).get("vector_dim") or 0) for row in item_dim_rows if int(dict(row).get("vector_dim") or 0) > 0]
                snapshot_vector_dims = [int(dict(row).get("vector_dim") or 0) for row in snapshot_dim_rows if int(dict(row).get("vector_dim") or 0) > 0]

                if getattr(db, "supports_sqlite_python", lambda: False)():
                    profile_store_where_sql = (
                        profile_where_sql
                        .replace("collection_name", "p.collection_name")
                        .replace("version", "p.version")
                        .replace("COALESCE(profile_type, '')", "COALESCE(p.profile_type, '')")
                    )
                    item_store_where_sql = (
                        item_where_sql
                        .replace("collection_name", "i.collection_name")
                        .replace("index_version", "i.index_version")
                        .replace("COALESCE(profile_type, '')", "COALESCE(i.profile_type, '')")
                    )
                    profile_store_missing_count = int(
                        await conn.fetchval(
                            f"""
                            SELECT COUNT(*)
                            FROM vector_profiles p
                            LEFT JOIN vector_profile_store ps ON ps.profile_id = p.id
                            WHERE {profile_store_where_sql}
                              AND ps.profile_id IS NULL
                            """,
                            *profile_args,
                        )
                        or 0
                    )
                    index_item_store_missing_count = int(
                        await conn.fetchval(
                            f"""
                            SELECT COUNT(*)
                            FROM vector_index_items i
                            LEFT JOIN vector_index_item_store iv ON iv.item_id = i.id
                            WHERE {item_store_where_sql}
                              AND iv.item_id IS NULL
                            """,
                            *item_args,
                        )
                        or 0
                    )
        except Exception:
            profile_rows_count = 0
            index_item_rows_count = 0
            profile_store_missing_count = None
            index_item_store_missing_count = None
            profile_dim_mismatch_count = 0
            index_item_dim_mismatch_count = 0
            active_snapshot_count = 0
            profile_types = []
            index_item_profile_types = []
            snapshot_profile_types = []
            profile_vector_dims = []
            index_item_vector_dims = []
            snapshot_vector_dims = []

    if profile_rows_count == 0 and hasattr(db, "list_vector_profiles"):
        rows = await db.list_vector_profiles(
            collection_name=resolved_collection,
            profile_type=resolved_profile_type,
            version=resolved_profile_version,
            limit=100000,
        )
        profile_rows_count = len(list(rows or []))
        profile_dim_mismatch_count = sum(
            1
            for row in list(rows or [])
            if int((row or {}).get("vector_dim") or len((row or {}).get("embedding") or [])) != len((row or {}).get("embedding") or [])
        )
        if not profile_types:
            profile_types = sorted({str((row or {}).get("profile_type") or "") for row in list(rows or [])})

    if index_item_rows_count == 0 and hasattr(db, "list_vector_index_items") and resolved_index_version:
        item_rows = await db.list_vector_index_items(
            collection_name=resolved_collection,
            index_version=resolved_index_version,
            profile_type=resolved_profile_type,
            limit=100000,
        )
        index_item_rows_count = len(list(item_rows or []))
        index_item_dim_mismatch_count = sum(
            1
            for row in list(item_rows or [])
            if int((row or {}).get("vector_dim") or len((row or {}).get("embedding") or [])) != len((row or {}).get("embedding") or [])
        )
        if not index_item_profile_types:
            index_item_profile_types = sorted({str((row or {}).get("profile_type") or "") for row in list(item_rows or [])})

    if active_snapshot_count == 0 and hasattr(db, "list_vector_index_snapshots"):
        snapshot_rows = await db.list_vector_index_snapshots(
            collection_name=resolved_collection,
            profile_type=resolved_profile_type,
            limit=1000,
        )
        active_snapshot_count = sum(1 for row in list(snapshot_rows or []) if str((row or {}).get("status") or "").strip().lower() == "active")
        if not snapshot_profile_types:
            snapshot_profile_types = sorted({str((row or {}).get("profile_type") or "") for row in list(snapshot_rows or [])})

    expected_count = max(0, int(expected_profile_count or profile_rows_count))
    coverage_ratio = 1.0 if expected_count <= 0 else round(float(index_item_rows_count) / float(max(expected_count, 1)), 6)

    coverage_ok = expected_count <= 0 or index_item_rows_count >= expected_count
    orphan_checked = profile_store_missing_count is not None and index_item_store_missing_count is not None
    orphan_ok = (profile_store_missing_count or 0) == 0 and (index_item_store_missing_count or 0) == 0 if orphan_checked else True
    dim_ok = profile_dim_mismatch_count == 0 and index_item_dim_mismatch_count == 0
    vector_dim_ok = len(profile_vector_dims) <= 1 and len(index_item_vector_dims) <= 1 and len(snapshot_vector_dims) <= 1
    if expect_active:
        active_ok = active_snapshot_count == 1
    else:
        active_ok = active_snapshot_count <= 1

    expected_type_set = {resolved_profile_type} if resolved_profile_type else set()
    observed_type_set = {
        item for item in (profile_types + index_item_profile_types + snapshot_profile_types) if str(item or "").strip()
    }
    profile_type_ok = True
    if expected_type_set:
        profile_type_ok = observed_type_set.issubset(expected_type_set)

    quality_flags: list[str] = []
    if not coverage_ok:
        quality_flags.append("insufficient_index_coverage")
    if orphan_checked and not orphan_ok:
        quality_flags.append("orphan_store_rows")
    if not dim_ok:
        quality_flags.append("embedding_dim_mismatch")
    if not vector_dim_ok:
        quality_flags.append("mixed_vector_dimensions")
    if not active_ok:
        quality_flags.append("active_snapshot_not_unique")
    if not profile_type_ok:
        quality_flags.append("profile_type_inconsistent")

    checks = {
        "coverage": {
            "status": "passed" if coverage_ok else "degraded",
            "expected_profile_count": expected_count,
            "indexed_item_count": index_item_rows_count,
            "coverage_ratio": coverage_ratio,
        },
        "orphan_check": {
            "status": "passed" if orphan_ok else ("degraded" if orphan_checked else "skipped"),
            "checked": orphan_checked,
            "missing_profile_store_rows": profile_store_missing_count,
            "missing_index_item_store_rows": index_item_store_missing_count,
        },
        "dim_check": {
            "status": "passed" if dim_ok and vector_dim_ok else "degraded",
            "profile_dim_mismatch_count": profile_dim_mismatch_count,
            "index_item_dim_mismatch_count": index_item_dim_mismatch_count,
            "profile_vector_dims": profile_vector_dims,
            "index_item_vector_dims": index_item_vector_dims,
            "snapshot_vector_dims": snapshot_vector_dims,
        },
        "active_snapshot_uniqueness": {
            "status": "passed" if active_ok else "degraded",
            "expect_active": bool(expect_active),
            "active_snapshot_count": active_snapshot_count,
        },
        "profile_type_consistency": {
            "status": "passed" if profile_type_ok else "degraded",
            "expected_profile_type": resolved_profile_type,
            "profile_types": profile_types,
            "index_item_profile_types": index_item_profile_types,
            "snapshot_profile_types": snapshot_profile_types,
        },
    }
    factor_quality_governance: dict[str, Any] | None = None
    if resolved_collection == "factor_candidate_embeddings":
        try:
            from .factor_research_memory import get_factor_research_memory_service

            memory_stats = await get_factor_research_memory_service().summarize_memory_records(limit=500)
            factor_quality_governance = {
                "available": True,
                "external_evidence_count": int(memory_stats.get("external_evidence_records") or 0),
                "unvalidated_external_count": int(memory_stats.get("unvalidated_external_records") or 0),
                "validated_external_count": int(memory_stats.get("validated_external_records") or 0),
                "candidate_source_counts": dict(memory_stats.get("candidate_source_counts") or {}),
                "quality_flags": dict(memory_stats.get("quality_flags") or {}),
                "status_counts": dict(memory_stats.get("status_counts") or {}),
            }
        except Exception as exc:
            factor_quality_governance = {"available": False, "error": str(exc)[:160]}
    degraded = bool(quality_flags)
    return {
        "collection_name": resolved_collection,
        "profile_type": resolved_profile_type,
        "profile_version": resolved_profile_version,
        "index_version": resolved_index_version,
        "status": "degraded" if degraded else "passed",
        "degraded": degraded,
        "quality_flags": quality_flags,
        "checks": checks,
        "factor_quality_governance": factor_quality_governance,
    }


async def build_vector_collection_snapshot(
    db,
    *,
    collection_name: str,
    version: str | None = None,
    index_version: str | None = None,
    profile_type: str | None = None,
    limit_profiles: Any = 5000,
    bucket_count: Any = None,
    activate: bool = True,
    source: str = "vector_governance",
) -> dict[str, Any]:
    resolved_profile_type = normalize_profile_type(profile_type)
    resolved_collection = resolve_vector_collection_name(collection_name, resolved_profile_type)
    if not resolved_collection:
        raise ValueError("collection_name is required")
    if str(collection_name or "").strip() == LEGACY_MARKET_DOC_COLLECTION and not resolved_profile_type:
        return {
            "collection_name": resolved_collection,
            "requested_collection_name": str(collection_name or "").strip() or None,
            "profile_type": None,
            "profile_version": version,
            "index_version": index_version,
            "status": "failed",
            "degraded": True,
            "quality_flags": ["profile_type_required_for_market_doc_collection"],
            "sample_count": 0,
            "items_count": 0,
            "bucket_count": 0,
            "snapshot": None,
            "profile_index_name": None,
            "item_index_name": None,
            "reason": "profile_type_required_for_market_doc_collection",
        }
    resolved_limit_profiles = _normalize_positive_int(limit_profiles, 5000, minimum=1, maximum=100000)
    rows = await db.list_vector_profiles(
        collection_name=resolved_collection,
        profile_type=resolved_profile_type,
        version=version,
        limit=resolved_limit_profiles,
    )
    if not rows:
        return {
            "collection_name": resolved_collection,
            "profile_type": resolved_profile_type,
            "profile_version": version,
            "index_version": index_version,
            "status": "skipped",
            "degraded": False,
            "quality_flags": [],
            "sample_count": 0,
            "items_count": 0,
            "bucket_count": 0,
            "snapshot": None,
            "profile_index_name": None,
            "item_index_name": None,
            "reason": "no_profiles",
        }

    collection = await db.get_vector_collection(resolved_collection) if hasattr(db, "get_vector_collection") else None
    first_row = dict(rows[0] or {})
    resolved_profile_version = str(version or first_row.get("version") or "").strip() or None
    filtered_rows = [
        dict(row)
        for row in rows
        if str(dict(row).get("version") or "").strip() == resolved_profile_version
    ]
    if not filtered_rows:
        return {
            "collection_name": resolved_collection,
            "profile_type": profile_type,
            "profile_version": resolved_profile_version,
            "index_version": index_version,
            "status": "skipped",
            "sample_count": 0,
            "items_count": 0,
            "bucket_count": 0,
            "snapshot": None,
            "profile_index_name": None,
            "item_index_name": None,
            "reason": "no_profiles_for_version",
        }

    vector_dim_counts: dict[int, int] = {}
    for row in filtered_rows:
        resolved_dim = int(dict(row).get("vector_dim") or len(dict(row).get("embedding") or []) or 0)
        if resolved_dim > 0:
            vector_dim_counts[resolved_dim] = int(vector_dim_counts.get(resolved_dim) or 0) + 1
    if len(vector_dim_counts) > 1:
        return {
            "collection_name": resolved_collection,
            "requested_collection_name": str(collection_name or "").strip() or None,
            "profile_type": resolved_profile_type,
            "profile_version": resolved_profile_version,
            "index_version": index_version,
            "status": "failed",
            "degraded": True,
            "quality_flags": ["mixed_vector_dimensions"],
            "sample_count": len(filtered_rows),
            "items_count": 0,
            "bucket_count": 0,
            "snapshot": None,
            "profile_index_name": None,
            "item_index_name": None,
            "reason": "mixed_vector_dimensions",
            "vector_dim_counts": {str(key): int(value) for key, value in sorted(vector_dim_counts.items())},
        }

    resolved_index_version = str(index_version or resolved_profile_version or f"auto_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}").strip()
    resolved_metric = str(first_row.get("metric") or (collection or {}).get("metric") or "cosine").strip().lower()
    resolved_model_id = str(first_row.get("model_id") or (collection or {}).get("model_id") or "unknown").strip()
    resolved_vector_dim = next(iter(vector_dim_counts.keys())) if vector_dim_counts else int(first_row.get("vector_dim") or len(first_row.get("embedding") or []) or 0)
    if not resolved_profile_type:
        row_profile_types = {
            str(row.get("profile_type") or "").strip()
            for row in filtered_rows
            if str(row.get("profile_type") or "").strip()
        }
        resolved_profile_type = next(iter(row_profile_types)) if len(row_profile_types) == 1 else None
    if hasattr(db, "get_vector_dimension_contract"):
        contract = await db.get_vector_dimension_contract(
            collection_name=resolved_collection,
            profile_type=resolved_profile_type,
            model_id=resolved_model_id,
            version=resolved_profile_version,
        )
        expected_dim = int((contract or {}).get("vector_dim") or 0)
        if contract and expected_dim > 0 and int(resolved_vector_dim or 0) != expected_dim:
            return {
                "collection_name": resolved_collection,
                "requested_collection_name": str(collection_name or "").strip() or None,
                "profile_type": resolved_profile_type,
                "profile_version": resolved_profile_version,
                "index_version": index_version,
                "status": "failed",
                "degraded": True,
                "quality_flags": ["vector_dimension_contract_mismatch"],
                "sample_count": len(filtered_rows),
                "items_count": 0,
                "bucket_count": 0,
                "snapshot": None,
                "profile_index_name": None,
                "item_index_name": None,
                "reason": "vector_dimension_contract_mismatch",
                "expected_vector_dim": expected_dim,
                "actual_vector_dim": int(resolved_vector_dim or 0),
                "dimension_contract": contract,
            }
    resolved_bucket_count = _normalize_positive_int(
        bucket_count,
        default=max(1, min(int(math.sqrt(len(filtered_rows))) or 1, 256)),
        minimum=1,
        maximum=4096,
    )

    if hasattr(db, "save_vector_collection"):
        await db.save_vector_collection(
            {
                "collection_name": resolved_collection,
                "entity_family": str((collection or {}).get("entity_family") or first_row.get("entity_type") or "generic"),
                "backend": str((collection or {}).get("backend") or getattr(db, "get_vector_backend", lambda: "sqlite_python")() or "sqlite_python"),
                "metric": resolved_metric,
                "model_id": resolved_model_id,
                "vector_dim": resolved_vector_dim,
                "status": "active",
                "metadata": dict((collection or {}).get("metadata") or {}),
            }
        )

    snapshot = await db.save_vector_index_snapshot(
        {
            "collection_name": resolved_collection,
            "index_version": resolved_index_version,
            "status": "building",
            "model_id": resolved_model_id,
            "profile_type": resolved_profile_type,
            "metric": resolved_metric,
            "vector_dim": resolved_vector_dim,
            "sample_count": len(filtered_rows),
            "bucket_count": resolved_bucket_count,
            "index_params": {
                "bucket_strategy": "centroid_kmeans",
                "limit_profiles": resolved_limit_profiles,
                "neighbor_count": 2,
            },
            "metrics": {},
            "metadata": {
                "profile_version": resolved_profile_version,
                "source": source,
            },
            "built_at": _now_iso(),
        }
    )

    layout, items = _build_bucket_layout(filtered_rows, bucket_count=resolved_bucket_count)
    resolved_bucket_count = int(layout.get("bucket_count") or resolved_bucket_count)
    replace_result = await db.replace_vector_index_items(resolved_collection, resolved_index_version, items)

    profile_index_name = None
    if hasattr(db, "ensure_vector_profile_sqlite_python_index"):
        profile_index_name = await db.ensure_vector_profile_sqlite_python_index(
            collection_name=resolved_collection,
            version=resolved_profile_version,
            vector_dim=resolved_vector_dim,
            profile_type=resolved_profile_type,
            metric=resolved_metric,
        )
    item_index_name = None
    if hasattr(db, "ensure_vector_index_item_sqlite_python_index"):
        item_index_name = await db.ensure_vector_index_item_sqlite_python_index(
            collection_name=resolved_collection,
            index_version=resolved_index_version,
            vector_dim=resolved_vector_dim,
            metric=resolved_metric,
        )

    final_status = "active" if activate else "built"
    snapshot = await db.save_vector_index_snapshot(
        {
            "collection_name": resolved_collection,
            "index_version": resolved_index_version,
            "status": final_status,
            "model_id": resolved_model_id,
            "profile_type": resolved_profile_type,
            "metric": resolved_metric,
            "vector_dim": resolved_vector_dim,
            "sample_count": len(filtered_rows),
            "bucket_count": resolved_bucket_count,
            "index_params": {
                "bucket_strategy": "centroid_kmeans",
                "limit_profiles": resolved_limit_profiles,
                "neighbor_count": 2,
            },
            "metrics": {
                "items_count": int(replace_result.get("count") or 0),
                "avg_coarse_score": round(
                    float(sum(float(item.get("coarse_score") or 0.0) for item in items) / max(len(items), 1)),
                    6,
                ) if items else 0.0,
            },
            "metadata": {
                "profile_version": resolved_profile_version,
                "source": source,
                "profile_index_name": profile_index_name,
                "item_index_name": item_index_name,
                "centroids": list(layout.get("centroids") or []),
                "layout": dict(layout.get("metadata") or {}),
                "vector_dim_counts": {str(key): int(value) for key, value in sorted(vector_dim_counts.items())},
            },
            "built_at": _now_iso(),
            "activated_at": _now_iso() if activate else None,
        }
    )
    qa = await audit_vector_collection_quality(
        db,
        collection_name=resolved_collection,
        profile_type=resolved_profile_type,
        profile_version=resolved_profile_version,
        index_version=resolved_index_version,
        expected_profile_count=len(filtered_rows),
        expect_active=bool(activate),
    )
    snapshot = await db.save_vector_index_snapshot(
        {
            "collection_name": resolved_collection,
            "index_version": resolved_index_version,
            "status": final_status,
            "model_id": resolved_model_id,
            "profile_type": resolved_profile_type,
            "metric": resolved_metric,
            "vector_dim": resolved_vector_dim,
            "sample_count": len(filtered_rows),
            "bucket_count": resolved_bucket_count,
            "index_params": {
                "bucket_strategy": "centroid_kmeans",
                "limit_profiles": resolved_limit_profiles,
                "neighbor_count": 2,
            },
            "metrics": {
                "items_count": int(replace_result.get("count") or 0),
                "avg_coarse_score": round(
                    float(sum(float(item.get("coarse_score") or 0.0) for item in items) / max(len(items), 1)),
                    6,
                ) if items else 0.0,
            },
            "metadata": {
                "profile_version": resolved_profile_version,
                "source": source,
                "profile_index_name": profile_index_name,
                "item_index_name": item_index_name,
                "centroids": list(layout.get("centroids") or []),
                "layout": dict(layout.get("metadata") or {}),
                "qa": qa,
                "vector_dim_counts": {str(key): int(value) for key, value in sorted(vector_dim_counts.items())},
            },
            "built_at": _now_iso(),
            "activated_at": _now_iso() if activate else None,
        }
    )
    return {
        "collection_name": resolved_collection,
        "requested_collection_name": str(collection_name or "").strip() or None,
        "profile_type": resolved_profile_type,
        "profile_version": resolved_profile_version,
        "index_version": resolved_index_version,
        "status": "degraded" if qa.get("degraded") else final_status,
        "snapshot_status": final_status,
        "degraded": bool(qa.get("degraded")),
        "quality_flags": list(qa.get("quality_flags") or []),
        "qa": qa,
        "sample_count": len(filtered_rows),
        "items_count": int(replace_result.get("count") or 0),
        "bucket_count": resolved_bucket_count,
        "snapshot": snapshot,
        "profile_index_name": profile_index_name,
        "item_index_name": item_index_name,
    }
