"""Factor candidate memory vector backfill pipeline for unified vector storage."""

from __future__ import annotations

from typing import Any

from .factor_candidate_storage import list_factor_candidate_records_async
from .factor_research_memory import build_factor_candidate_vector_profile


def _normalize_codes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = str(raw).replace(";", ",").split(",")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _normalize_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 5000) -> int:
    try:
        resolved = int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        resolved = int(default)
    return max(minimum, min(resolved, maximum))


async def _profile_exists(db, *, entity_id: str, version: str) -> bool:
    if not hasattr(db, "list_vector_profiles"):
        return False
    try:
        rows = await db.list_vector_profiles(
            collection_name="factor_candidate_embeddings",
            entity_id=entity_id,
            version=version,
            limit=1,
        )
    except Exception:
        return False
    return bool(rows)


async def backfill_factor_candidate_vectors(
    db,
    *,
    limit: Any = 200,
    codes: Any = None,
    status: str | None = None,
    family: str | None = None,
    version: str = "v1",
    rebuild_existing: Any = False,
    dry_run: Any = False,
) -> dict[str, Any]:
    resolved_limit = _normalize_positive_int(limit, 200, minimum=1, maximum=2000)
    resolved_codes = _normalize_codes(codes)
    resolved_rebuild_existing = bool(rebuild_existing)
    resolved_dry_run = bool(dry_run)
    records = await list_factor_candidate_records_async(
        limit=resolved_limit,
        codes=resolved_codes or None,
        status=str(status).strip().lower() if status else None,
        family=str(family).strip().lower() if family else None,
    )
    results = {
        "limit": resolved_limit,
        "codes": resolved_codes,
        "status": str(status).strip().lower() if status else None,
        "family": str(family).strip().lower() if family else None,
        "version": str(version or "v1"),
        "rebuild_existing": resolved_rebuild_existing,
        "dry_run": resolved_dry_run,
        "candidate_records": len(records),
        "processed_records": 0,
        "skipped_records": 0,
        "skipped_existing_profiles": 0,
        "saved_profiles": 0,
        "errors": [],
    }
    collection_saved = False

    for record in records:
        payload = build_factor_candidate_vector_profile(record, version=str(version or "v1"))
        if not payload:
            results["skipped_records"] += 1
            continue
        results["processed_records"] += 1
        if not resolved_rebuild_existing and await _profile_exists(
            db,
            entity_id=str(payload.get("entity_id") or ""),
            version=str(payload.get("version") or "v1"),
        ):
            results["skipped_existing_profiles"] += 1
            continue
        if resolved_dry_run:
            results["saved_profiles"] += 1
            continue
        try:
            if not collection_saved and hasattr(db, "save_vector_collection"):
                await db.save_vector_collection(
                    {
                        "collection_name": "factor_candidate_embeddings",
                        "entity_family": "factor_candidate",
                        "backend": str(getattr(db, "get_vector_backend", lambda: "sqlite_python")() or "sqlite_python"),
                        "metric": str(payload.get("metric") or "cosine"),
                        "model_id": str(payload.get("model_id") or "factor-memory-v1"),
                        "vector_dim": int(payload.get("vector_dim") or len(payload.get("embedding") or [])),
                        "status": "active",
                        "metadata": {
                            "domain": "quant-research",
                            "notes": "factor candidate memory",
                        },
                    }
                )
                collection_saved = True
            await db.save_vector_profile(payload)
            results["saved_profiles"] += 1
        except Exception as exc:
            results["errors"].append(
                f"{str(record.get('artifact_id') or 'unknown')}:{type(exc).__name__}"
            )

    if len(results["errors"]) > 20:
        total = len(results["errors"])
        results["errors"] = list(results["errors"][:20]) + [f"...及其他 {total - 20} 个错误"]
    return results
