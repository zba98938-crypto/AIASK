#!/usr/bin/env python3
"""Split mixed-dimension market doc profiles, re-embed 256-d fallback rows to 1536-d, and rebuild final snapshots."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
EXTRA_SRC_ROOTS = [
    SRC_ROOT,
    REPO_ROOT / "packages" / "strategy-factory" / "src",
]
for root in reversed(EXTRA_SRC_ROOTS):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

from akshare_mcp.services.text_embedding import (  # noqa: E402
    StrategyTextEmbeddingService,
    get_strategy_text_embedding_service,
)
from akshare_mcp.services.unified_vector_governance import (  # noqa: E402
    audit_vector_collection_quality,
    build_vector_collection_snapshot,
)
from akshare_mcp.storage import close_db, get_db  # noqa: E402
from akshare_mcp.vector_collection_scope import (  # noqa: E402
    normalize_market_doc_types,
    resolve_dimension_scoped_version,
    resolve_vector_collection_name,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _affected_rows(tag: str) -> int:
    text = str(tag or "").strip()
    if not text:
        return 0
    try:
        return int(text.split()[-1])
    except Exception:
        return 0


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = str(value or "").strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}
    return {}


async def _fetch_version_dim_counts(
    db,
    *,
    collection_name: str,
    profile_type: str,
) -> list[dict[str, Any]]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT version, vector_dim, COUNT(*) AS row_count
            FROM vector_profiles
            WHERE collection_name = $1
              AND COALESCE(profile_type, '') = $2
            GROUP BY version, vector_dim
            ORDER BY version, vector_dim
            """,
            collection_name,
            profile_type,
        )
    return [
        {
            "version": str(dict(row).get("version") or ""),
            "vector_dim": int(dict(row).get("vector_dim") or 0),
            "row_count": int(dict(row).get("row_count") or 0),
        }
        for row in rows
    ]


async def _split_base_version(
    db,
    *,
    collection_name: str,
    profile_type: str,
    base_version: str,
    source_dims: list[int],
) -> dict[str, Any]:
    dim_counts = {
        int(row["vector_dim"]): int(row["row_count"])
        for row in await _fetch_version_dim_counts(db, collection_name=collection_name, profile_type=profile_type)
        if str(row["version"] or "") == base_version
    }
    unexpected_dims = sorted(dim for dim in dim_counts if dim not in source_dims)
    if unexpected_dims:
        raise RuntimeError(
            f"{collection_name}/{profile_type} has unexpected dims in {base_version}: {unexpected_dims}"
        )
    if not dim_counts:
        return {
            "status": "skipped",
            "collection_name": collection_name,
            "profile_type": profile_type,
            "base_version": base_version,
            "reason": "base_version_empty",
            "split_versions": {},
        }

    split_versions = {
        dim: resolve_dimension_scoped_version(base_version, dim)
        for dim in source_dims
        if int(dim_counts.get(dim) or 0) > 0
    }
    existing_targets = {
        row["version"]: row["row_count"]
        for row in await _fetch_version_dim_counts(db, collection_name=collection_name, profile_type=profile_type)
        if str(row["version"] or "") in set(split_versions.values())
    }
    if existing_targets:
        raise RuntimeError(
            f"{collection_name}/{profile_type} already has target split versions populated: {existing_targets}"
        )

    now_iso = _utc_now_iso()
    updated_profiles: dict[str, int] = {}
    updated_profile_store: dict[str, int] = {}
    async with db.acquire() as conn:
        async with conn.transaction():
            for dim, target_version in split_versions.items():
                profile_tag = await conn.execute(
                    """
                    UPDATE vector_profiles
                    SET version = $4,
                        metadata = COALESCE(metadata, '{}') || jsonb_build_object(
                            'base_version', $3,
                            'profile_version', $4,
                            'dimension_split', true,
                            'dimension_split_at', $5
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE collection_name = $1
                      AND COALESCE(profile_type, '') = $2
                      AND version = $3
                      AND vector_dim = $6
                    """,
                    collection_name,
                    profile_type,
                    base_version,
                    target_version,
                    now_iso,
                    int(dim),
                )
                updated_profiles[target_version] = _affected_rows(profile_tag)
                if getattr(db, "supports_sqlite_python", lambda: False)():
                    store_tag = await conn.execute(
                        """
                        UPDATE vector_profile_store
                        SET version = $4,
                            metadata = COALESCE(metadata, '{}') || jsonb_build_object(
                                'base_version', $3,
                                'profile_version', $4,
                                'dimension_split', true,
                                'dimension_split_at', $5
                            ),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE collection_name = $1
                          AND COALESCE(profile_type, '') = $2
                          AND version = $3
                          AND vector_dim = $6
                        """,
                        collection_name,
                        profile_type,
                        base_version,
                        target_version,
                        now_iso,
                        int(dim),
                    )
                    updated_profile_store[target_version] = _affected_rows(store_tag)
    return {
        "status": "completed",
        "collection_name": collection_name,
        "profile_type": profile_type,
        "base_version": base_version,
        "split_versions": {str(dim): version for dim, version in split_versions.items()},
        "updated_profiles": updated_profiles,
        "updated_profile_store": updated_profile_store,
    }


async def _build_split_snapshots(
    db,
    *,
    collection_name: str,
    profile_type: str,
    profile_versions: list[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for profile_version in profile_versions:
        result = await build_vector_collection_snapshot(
            db,
            collection_name=collection_name,
            version=profile_version,
            index_version=f"{profile_version}__split",
            profile_type=profile_type,
            activate=False,
            source="scripts.remediate_market_doc_embedding_dimensions.split_snapshot",
        )
        results.append(result)
    return results


async def _build_strict_embedding_service(target_dim: int) -> StrategyTextEmbeddingService:
    base_service = get_strategy_text_embedding_service()
    config = replace(base_service.config, allow_hash_fallback=False)
    service = StrategyTextEmbeddingService(config)
    smoke = await service.smoke_check(force=True)
    if str(smoke.get("status") or "").strip().lower() != "passed":
        await service.close()
        raise RuntimeError(f"strict embedding smoke check failed: {smoke}")
    if bool(smoke.get("fallback_used")):
        await service.close()
        raise RuntimeError(f"strict embedding smoke check still used fallback: {smoke}")
    if int(smoke.get("vector_length") or 0) != int(target_dim):
        await service.close()
        raise RuntimeError(f"strict embedding smoke check expected dim={target_dim}, got {smoke}")
    return service


async def _load_reembed_candidates(
    db,
    *,
    collection_name: str,
    profile_type: str,
    source_version: str,
    target_version: str,
    source_dim: int,
    limit: int | None = None,
    exclude_entity_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    async with db.acquire() as conn:
        sql = """
            SELECT
                vp.entity_type,
                vp.entity_id,
                vp.stock_code,
                vp.profile_type,
                vp.metric,
                vp.metadata,
                c.chunk_text,
                c.chunk_no,
                c.source,
                c.title,
                c.published_at,
                d.doc_uid,
                d.url,
                d.author
            FROM vector_profiles vp
            LEFT JOIN vector_profiles target_vp
              ON target_vp.collection_name = vp.collection_name
             AND COALESCE(target_vp.profile_type, '') = COALESCE(vp.profile_type, '')
             AND target_vp.entity_type = vp.entity_type
             AND target_vp.entity_id = vp.entity_id
             AND target_vp.version = $5
            JOIN market_doc_chunks c
              ON c.stock_code = vp.stock_code
             AND COALESCE(LOWER(c.doc_type), '') = COALESCE(LOWER(vp.profile_type), '')
            JOIN market_documents d
              ON d.id = c.doc_id
             AND (d.doc_uid || ':' || c.chunk_no) = vp.entity_id
            WHERE vp.collection_name = $1
              AND COALESCE(vp.profile_type, '') = $2
              AND vp.version = $3
              AND vp.vector_dim = $4
              AND target_vp.id IS NULL
        """
        params: list[Any] = [
            collection_name,
            profile_type,
            source_version,
            int(source_dim),
            target_version,
        ]
        if exclude_entity_ids:
            sql += f"\n              AND vp.entity_id NOT IN (${len(params) + 1})"
            params.append([str(item) for item in exclude_entity_ids if str(item or "").strip()])
        sql += """
            ORDER BY vp.updated_at DESC, vp.created_at DESC, vp.id DESC
        """
        if limit and int(limit) > 0:
            sql += f" LIMIT ${len(params) + 1}"
            params.append(int(limit))
        rows = await conn.fetch(sql, *params)
    return [dict(row) for row in rows]


async def _count_reembed_candidates(
    db,
    *,
    collection_name: str,
    profile_type: str,
    source_version: str,
    target_version: str,
    source_dim: int,
) -> int:
    async with db.acquire() as conn:
        return int(
            await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM vector_profiles vp
                LEFT JOIN vector_profiles target_vp
                  ON target_vp.collection_name = vp.collection_name
                 AND COALESCE(target_vp.profile_type, '') = COALESCE(vp.profile_type, '')
                 AND target_vp.entity_type = vp.entity_type
                 AND target_vp.entity_id = vp.entity_id
                 AND target_vp.version = $5
                WHERE vp.collection_name = $1
                  AND COALESCE(vp.profile_type, '') = $2
                  AND vp.version = $3
                  AND vp.vector_dim = $4
                  AND target_vp.id IS NULL
                """,
                collection_name,
                profile_type,
                source_version,
                int(source_dim),
                target_version,
            )
            or 0
        )


async def _count_unique_entities(
    db,
    *,
    collection_name: str,
    profile_type: str,
    versions: list[str],
) -> int:
    filtered_versions = [str(item or "").strip() for item in versions if str(item or "").strip()]
    if not filtered_versions:
        return 0
    async with db.acquire() as conn:
        return int(
            await conn.fetchval(
                """
                SELECT COUNT(DISTINCT entity_id)
                FROM vector_profiles
                WHERE collection_name = $1
                  AND COALESCE(profile_type, '') = $2
                  AND version IN ($3)
                """,
                collection_name,
                profile_type,
                filtered_versions,
            )
            or 0
        )


async def _reembed_into_target_version(
    db,
    *,
    collection_name: str,
    profile_type: str,
    source_version: str,
    target_version: str,
    source_dim: int,
    target_dim: int,
    limit: int | None,
    concurrency: int,
    batch_size: int,
    pause_seconds: float,
) -> dict[str, Any]:
    service = await _build_strict_embedding_service(target_dim)
    resolved_batch_size = max(1, int(batch_size or 100))
    total_candidates = await _count_reembed_candidates(
        db,
        collection_name=collection_name,
        profile_type=profile_type,
        source_version=source_version,
        target_version=target_version,
        source_dim=source_dim,
    )
    if limit and int(limit) > 0:
        total_candidates = min(total_candidates, int(limit))
    if total_candidates <= 0:
        await service.close()
        return {
            "status": "skipped",
            "reason": "no_source_rows",
            "source_version": source_version,
            "target_version": target_version,
            "candidate_rows": 0,
            "processed_rows": 0,
            "saved_profiles": 0,
            "failed_rows": 0,
            "batch_count": 0,
            "failures": [],
        }

    semaphore = asyncio.Semaphore(max(1, int(concurrency or 4)))
    failures: list[dict[str, Any]] = []
    failed_entity_ids: set[str] = set()
    saved = 0
    processed = 0
    batch_count = 0
    target_model_id = str(service.config.model or service.config.local_model_path or "text-embedding-3-small").strip() or "text-embedding-3-small"

    async def _worker(row: dict[str, Any]) -> None:
        nonlocal saved
        async with semaphore:
            chunk_text = str(row.get("chunk_text") or "").strip()
            entity_id = str(row.get("entity_id") or "").strip()
            if not chunk_text or not entity_id:
                failures.append(
                    {
                        "entity_id": entity_id,
                        "error": "missing_chunk_text",
                    }
                )
                return
            try:
                result = await service.embed_text_with_info(chunk_text)
                embedding = list(result.get("embedding") or [])
                if not embedding:
                    raise RuntimeError("empty_embedding")
                if bool(result.get("fallback_used")):
                    raise RuntimeError(f"fallback_used:{result.get('fallback_error') or 'unknown'}")
                if int(result.get("vector_dim") or len(embedding)) != int(target_dim):
                    raise RuntimeError(f"unexpected_dim:{result.get('vector_dim') or len(embedding)}")
                existing_metadata = _json_dict(row.get("metadata"))
                metadata = {
                    **existing_metadata,
                    "base_version": existing_metadata.get("base_version") or source_version.replace(f"__d{source_dim}", ""),
                    "profile_version": target_version,
                    "reembedded_from_version": source_version,
                    "reembedded_from_dim": int(source_dim),
                    "embedding_provider": result.get("provider"),
                    "requested_embedding_provider": result.get("requested_provider"),
                    "embedding_fallback_used": False,
                    "embedding_fallback_error": None,
                    "reembedded_at": _utc_now_iso(),
                    "reembedded_by": "scripts.remediate_market_doc_embedding_dimensions",
                }
                await db.save_vector_profile(
                    {
                        "collection_name": collection_name,
                        "entity_type": str(row.get("entity_type") or "market_doc_chunk"),
                        "entity_id": entity_id,
                        "stock_code": row.get("stock_code"),
                        "profile_type": profile_type,
                        "model_id": target_model_id,
                        "vector_dim": int(target_dim),
                        "metric": str(row.get("metric") or "cosine"),
                        "version": target_version,
                        "signature": hashlib.sha1(
                            f"{entity_id}|{target_model_id}|{chunk_text}".encode("utf-8")
                        ).hexdigest(),
                        "status": "active",
                        "embedding": embedding,
                        "metadata": metadata,
                    }
                )
                saved += 1
                if saved % 50 == 0:
                    print(
                        json.dumps(
                            {
                                "phase": "reembed_progress",
                                "collection_name": collection_name,
                                "profile_type": profile_type,
                                "target_version": target_version,
                                "saved_profiles": saved,
                                "candidate_rows": len(rows),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            except Exception as exc:  # pragma: no cover - operational guard
                if entity_id:
                    failed_entity_ids.add(entity_id)
                failures.append(
                    {
                        "entity_id": entity_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )

    try:
        remaining_limit = int(limit) if limit and int(limit) > 0 else None
        while True:
            current_limit = min(resolved_batch_size, remaining_limit) if remaining_limit is not None else resolved_batch_size
            rows = await _load_reembed_candidates(
                db,
                collection_name=collection_name,
                profile_type=profile_type,
                source_version=source_version,
                target_version=target_version,
                source_dim=source_dim,
                limit=current_limit,
                exclude_entity_ids=sorted(failed_entity_ids),
            )
            if not rows:
                break
            batch_count += 1
            processed += len(rows)
            await asyncio.gather(*[_worker(row) for row in rows])
            if remaining_limit is not None:
                remaining_limit = max(0, remaining_limit - len(rows))
                if remaining_limit <= 0:
                    break
            print(
                json.dumps(
                    {
                        "phase": "reembed_batch_complete",
                        "collection_name": collection_name,
                        "profile_type": profile_type,
                        "target_version": target_version,
                        "batch_count": batch_count,
                        "processed_rows": processed,
                        "candidate_rows": total_candidates,
                        "saved_profiles": saved,
                        "failed_rows": len(failures),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if pause_seconds > 0:
                await asyncio.sleep(max(0.0, float(pause_seconds)))
    finally:
        await service.close()
    return {
        "status": "completed" if not failures else "degraded",
        "source_version": source_version,
        "target_version": target_version,
        "candidate_rows": total_candidates,
        "processed_rows": processed,
        "saved_profiles": saved,
        "failed_rows": len(failures),
        "batch_count": batch_count,
        "failures": failures[:20],
    }


async def _refresh_collection_metadata(
    db,
    *,
    collection_name: str,
    active_snapshot_version: str,
    active_profile_version: str,
    split_versions: list[str],
    vector_dim: int,
) -> None:
    collection = await db.get_vector_collection(collection_name)
    payload = dict(collection or {})
    metadata = _json_dict(payload.get("metadata"))
    metadata.update(
        {
            "active_profile_versions": sorted([str(item) for item in split_versions if str(item).strip()]),
            "preferred_profile_version": active_profile_version,
            "dimension_remediation": {
                "status": "completed",
                "active_profile_version": active_profile_version,
                "active_snapshot_version": active_snapshot_version,
                "updated_at": _utc_now_iso(),
            },
        }
    )
    await db.save_vector_collection(
        {
            "collection_name": collection_name,
            "entity_family": str(payload.get("entity_family") or "document_chunk"),
            "backend": str(payload.get("backend") or db.get_vector_backend()),
            "metric": str(payload.get("metric") or "cosine"),
            "model_id": str(payload.get("model_id") or "text-embedding-3-small"),
            "vector_dim": int(vector_dim),
            "normalization": str(payload.get("normalization") or "unit"),
            "status": str(payload.get("status") or "active"),
            "active_version": active_snapshot_version,
            "metadata": metadata,
        }
    )


async def remediate_market_doc_vectors(args: argparse.Namespace) -> dict[str, Any]:
    db = get_db()
    doc_types = normalize_market_doc_types(args.doc_types) or ["news", "notice", "research"]
    fallback_dim = int(args.fallback_dim)
    target_dim = int(args.target_dim)
    source_dims = [fallback_dim, target_dim]
    summary: dict[str, Any] = {
        "base_version": args.base_version,
        "doc_types": doc_types,
        "fallback_dim": fallback_dim,
        "target_dim": target_dim,
        "started_at": _utc_now_iso(),
        "collections": {},
    }

    for doc_type in doc_types:
        collection_name = resolve_vector_collection_name(args.collection_name, doc_type)
        split_result = await _split_base_version(
            db,
            collection_name=collection_name,
            profile_type=doc_type,
            base_version=args.base_version,
            source_dims=source_dims,
        )
        split_versions = list(dict(split_result.get("split_versions") or {}).values())
        if split_versions:
            split_snapshots = await _build_split_snapshots(
                db,
                collection_name=collection_name,
                profile_type=doc_type,
                profile_versions=split_versions,
            )
        else:
            split_snapshots = []

        fallback_version = resolve_dimension_scoped_version(args.base_version, fallback_dim)
        target_version = resolve_dimension_scoped_version(args.base_version, target_dim)
        pre_unique_entities = await _count_unique_entities(
            db,
            collection_name=collection_name,
            profile_type=doc_type,
            versions=[fallback_version, target_version],
        )
        reembed_result = await _reembed_into_target_version(
            db,
            collection_name=collection_name,
            profile_type=doc_type,
            source_version=fallback_version,
            target_version=target_version,
            source_dim=fallback_dim,
            target_dim=target_dim,
            limit=args.limit,
            concurrency=args.concurrency,
            batch_size=args.batch_size,
            pause_seconds=args.pause_seconds,
        )
        if args.pause_seconds > 0:
            await asyncio.sleep(max(0.0, float(args.pause_seconds)))
        final_index_version = f"{target_version}__final"
        final_snapshot = await build_vector_collection_snapshot(
            db,
            collection_name=collection_name,
            version=target_version,
            index_version=final_index_version,
            profile_type=doc_type,
            activate=True,
            source="scripts.remediate_market_doc_embedding_dimensions.final_snapshot",
        )
        final_qa = await audit_vector_collection_quality(
            db,
            collection_name=collection_name,
            profile_type=doc_type,
            profile_version=target_version,
            index_version=final_index_version,
            expected_profile_count=pre_unique_entities,
            expect_active=True,
        )
        await _refresh_collection_metadata(
            db,
            collection_name=collection_name,
            active_snapshot_version=final_index_version,
            active_profile_version=target_version,
            split_versions=[fallback_version, target_version],
            vector_dim=target_dim,
        )
        post_counts = await _fetch_version_dim_counts(db, collection_name=collection_name, profile_type=doc_type)
        summary["collections"][collection_name] = {
            "profile_type": doc_type,
            "split": split_result,
            "split_snapshots": split_snapshots,
            "pre_unique_entities": pre_unique_entities,
            "reembed": reembed_result,
            "final_snapshot": final_snapshot,
            "final_qa": final_qa,
            "post_version_dim_counts": post_counts,
        }

    summary["finished_at"] = _utc_now_iso()
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-name", default="market_doc_chunks")
    parser.add_argument("--base-version", required=True)
    parser.add_argument("--doc-types", nargs="*", default=["news", "notice", "research"])
    parser.add_argument("--fallback-dim", type=int, default=256)
    parser.add_argument("--target-dim", type=int, default=1536)
    parser.add_argument("--limit", type=int, default=0, help="Optional limit per doc_type for re-embedding candidates.")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--pause-seconds", type=float, default=0.25)
    return parser.parse_args()


async def _async_main() -> int:
    args = _parse_args()
    try:
        summary = await remediate_market_doc_vectors(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        await close_db()


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
