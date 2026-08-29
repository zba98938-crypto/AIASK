"""Vector backfill services for unified market document storage."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any, Iterable, Optional


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


def _normalize_doc_types(raw: Any) -> list[str]:
    allowed = {"news", "notice", "research"}
    normalized: list[str] = []
    seen: set[str] = set()
    for item in _normalize_codes(raw):
        token = str(item or "").strip().lower()
        if not token or token not in allowed or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized or ["news", "notice", "research"]


def _coerce_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        pass
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        try:
            return date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
        except Exception:
            return None
    return None


def _normalize_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 5000) -> int:
    try:
        resolved = int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        resolved = int(default)
    return max(minimum, min(resolved, maximum))


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


async def _fetch_existing_doc_uids(db, doc_uids: Iterable[str]) -> set[str]:
    values = [str(item or "").strip() for item in doc_uids if str(item or "").strip()]
    if not values:
        return set()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT doc_uid FROM market_documents WHERE doc_uid IN ($1)",
            values,
        )
    return {str(dict(row).get("doc_uid") or "").strip() for row in rows if dict(row).get("doc_uid")}


def _build_doc_uid(db, stock_code: str, doc_type: str, item: dict[str, Any]) -> str:
    builder = getattr(db, "_build_market_doc_uid", None)
    if callable(builder):
        return str(builder(stock_code, doc_type, item))
    code = str(stock_code or "").strip()
    normalized_type = str(doc_type or "").strip().lower()
    title = str(item.get("title") or item.get("headline") or item.get("summary") or "").strip()
    text = str(item.get("content") or item.get("text") or item.get("summary") or "").strip()
    date_value = _coerce_date(item.get("date") or item.get("publish_date"))
    return f"{code}:{normalized_type}:{date_value.isoformat() if date_value else ''}:{title}:{text[:120]}"


def _group_backfill_items(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        code = str(row.get("stock_code") or "").strip()
        doc_type = str(row.get("doc_type") or "").strip().lower()
        if not code or not doc_type:
            continue
        grouped[(code, doc_type)].append(dict(row))
    return grouped


async def _load_legacy_vector_documents(
    db,
    *,
    stock_codes: list[str],
    doc_types: list[str],
    start_date: Optional[date],
    end_date: Optional[date],
    after_id: int,
    limit: int,
) -> list[dict[str, Any]]:
    params: list[Any] = [int(after_id)]
    where_clauses = ["id > $1"]
    idx = 2
    if stock_codes:
        where_clauses.append(f"stock_code IN (${idx})")
        params.append(stock_codes)
        idx += 1
    if doc_types:
        where_clauses.append(f"LOWER(COALESCE(doc_type, '')) IN (${idx})")
        params.append([str(item or "").strip().lower() for item in doc_types if str(item or "").strip()])
        idx += 1
    if start_date is not None:
        where_clauses.append(f"date >= ${idx}")
        params.append(start_date)
        idx += 1
    if end_date is not None:
        where_clauses.append(f"date <= ${idx}")
        params.append(end_date)
        idx += 1
    params.append(int(limit))
    async with db.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, stock_code, doc_type, content, date
            FROM vector_documents
            WHERE {' AND '.join(where_clauses)}
            ORDER BY id ASC
            LIMIT ${len(params)}
            """,
            *params,
        )
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        stock_code = str(payload.get("stock_code") or "").strip()
        doc_type = str(payload.get("doc_type") or "").strip().lower()
        content = str(payload.get("content") or "").strip()
        if not stock_code or not doc_type or not content:
            continue
        payloads.append(
            {
                "legacy_id": int(payload.get("id") or 0),
                "stock_code": stock_code,
                "doc_type": doc_type,
                "title": content[:120],
                "content": content,
                "date": payload.get("date"),
                "source": f"vector_documents_legacy.{doc_type}",
            }
        )
    return payloads


async def _load_legacy_research_reports(
    db,
    *,
    stock_codes: list[str],
    start_date: Optional[date],
    end_date: Optional[date],
    after_id: int,
    limit: int,
) -> list[dict[str, Any]]:
    params: list[Any] = [int(after_id)]
    where_clauses = ["id > $1"]
    idx = 2
    if stock_codes:
        where_clauses.append(f"code IN (${idx})")
        params.append(stock_codes)
        idx += 1
    if start_date is not None:
        where_clauses.append(f"publish_date >= ${idx}")
        params.append(start_date)
        idx += 1
    if end_date is not None:
        where_clauses.append(f"publish_date <= ${idx}")
        params.append(end_date)
        idx += 1
    params.append(int(limit))
    async with db.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, code, title, institution, analyst, publish_date, summary, pdf_url
            FROM research_reports
            WHERE {' AND '.join(where_clauses)}
            ORDER BY id ASC
            LIMIT ${len(params)}
            """,
            *params,
        )
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        stock_code = str(payload.get("code") or "").strip()
        title = str(payload.get("title") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        if not stock_code or not (title or summary):
            continue
        payloads.append(
            {
                "legacy_id": int(payload.get("id") or 0),
                "stock_code": stock_code,
                "doc_type": "research",
                "title": title,
                "summary": summary,
                "content": summary or title,
                "date": payload.get("publish_date"),
                "url": payload.get("pdf_url"),
                "author": payload.get("analyst"),
                "source": payload.get("institution") or "research_reports",
            }
        )
    return payloads


async def _save_grouped_backfill_rows(
    db,
    grouped_rows: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    embed: bool,
    chunk_size: int,
    overlap: int,
    version: str,
    rebuild_existing: bool,
    dry_run: bool,
) -> dict[str, Any]:
    result = {
        "candidate_docs": 0,
        "skipped_existing_docs": 0,
        "saved_docs": 0,
        "saved_chunks": 0,
        "embedded_chunks": 0,
        "profile_version_counts_by_doc_type": {},
        "vector_dims_by_doc_type": {},
    }
    for (stock_code, doc_type), items in grouped_rows.items():
        prepared: list[dict[str, Any]] = []
        for item in items:
            payload = dict(item)
            payload["doc_uid"] = _build_doc_uid(db, stock_code, doc_type, payload)
            prepared.append(payload)

        result["candidate_docs"] += len(prepared)
        if not rebuild_existing:
            existing_doc_uids = await _fetch_existing_doc_uids(db, [item.get("doc_uid") for item in prepared])
            result["skipped_existing_docs"] += sum(
                1 for item in prepared if str(item.get("doc_uid") or "") in existing_doc_uids
            )
            prepared = [item for item in prepared if str(item.get("doc_uid") or "") not in existing_doc_uids]

        if not prepared:
            continue
        if dry_run:
            result["saved_docs"] += len(prepared)
            continue

        saved = await db.save_market_documents(
            stock_code,
            doc_type,
            prepared,
            embed=embed,
            chunk_size=chunk_size,
            overlap=overlap,
            version=version,
        )
        result["saved_docs"] += int(saved.get("documents") or 0)
        result["saved_chunks"] += int(saved.get("chunks") or 0)
        result["embedded_chunks"] += int(saved.get("embedded_chunks") or 0)
        version_counts = dict(saved.get("profile_version_counts") or {})
        if version_counts:
            bucket = result["profile_version_counts_by_doc_type"].setdefault(str(doc_type or "").strip().lower(), {})
            for profile_version, count in version_counts.items():
                key = str(profile_version or "").strip()
                if not key:
                    continue
                bucket[key] = int(bucket.get(key) or 0) + int(count or 0)
        vector_dims = [int(item) for item in list(saved.get("vector_dims") or []) if int(item or 0) > 0]
        if vector_dims:
            dims_bucket = result["vector_dims_by_doc_type"].setdefault(str(doc_type or "").strip().lower(), [])
            for vector_dim in vector_dims:
                if vector_dim not in dims_bucket:
                    dims_bucket.append(vector_dim)
    return result


async def backfill_market_document_vectors(
    db,
    *,
    stock_codes: Any = None,
    doc_types: Any = None,
    start_date: Any = None,
    end_date: Any = None,
    limit: Any = 500,
    batch_size: Any = 100,
    embed: Any = True,
    chunk_size: Any = 800,
    overlap: Any = 120,
    version: str = "v1",
    rebuild_existing: Any = False,
    dry_run: Any = False,
    include_legacy_research_docs: Any = False,
) -> dict[str, Any]:
    """Backfill legacy market text into market_documents + market_doc_chunks + vector_profiles."""

    resolved_codes = _normalize_codes(stock_codes)
    resolved_doc_types = _normalize_doc_types(doc_types)
    resolved_start_date = _coerce_date(start_date)
    resolved_end_date = _coerce_date(end_date)
    resolved_limit = _normalize_positive_int(limit, 500, minimum=1, maximum=50000)
    resolved_batch_size = _normalize_positive_int(batch_size, 100, minimum=1, maximum=2000)
    resolved_embed = _normalize_bool(embed, True)
    resolved_chunk_size = _normalize_positive_int(chunk_size, 800, minimum=200, maximum=4000)
    resolved_overlap = _normalize_positive_int(overlap, 120, minimum=0, maximum=1500)
    resolved_version = str(version or "v1").strip() or "v1"
    resolved_rebuild_existing = _normalize_bool(rebuild_existing, False)
    resolved_dry_run = _normalize_bool(dry_run, False)
    resolved_include_legacy_research_docs = _normalize_bool(include_legacy_research_docs, False)

    results: dict[str, Any] = {
        "stock_codes": resolved_codes,
        "doc_types": resolved_doc_types,
        "start_date": resolved_start_date.isoformat() if resolved_start_date else None,
        "end_date": resolved_end_date.isoformat() if resolved_end_date else None,
        "limit": resolved_limit,
        "batch_size": resolved_batch_size,
        "embed": resolved_embed,
        "chunk_size": resolved_chunk_size,
        "overlap": resolved_overlap,
        "version": resolved_version,
        "rebuild_existing": resolved_rebuild_existing,
        "dry_run": resolved_dry_run,
        "include_legacy_research_docs": resolved_include_legacy_research_docs,
        "sources": {
            "vector_documents": {
                "candidate_docs": 0,
                "skipped_existing_docs": 0,
                "saved_docs": 0,
                "saved_chunks": 0,
                "embedded_chunks": 0,
            },
            "research_reports": {
                "candidate_docs": 0,
                "skipped_existing_docs": 0,
                "saved_docs": 0,
                "saved_chunks": 0,
                "embedded_chunks": 0,
            },
        },
    }

    remaining = resolved_limit
    vector_doc_types = [item for item in resolved_doc_types if item in {"news", "notice"}]
    if resolved_include_legacy_research_docs and "research" in resolved_doc_types:
        vector_doc_types.append("research")
    vector_doc_types = list(dict.fromkeys(vector_doc_types))

    if vector_doc_types and remaining > 0:
        after_id = 0
        while remaining > 0:
            fetch_limit = min(remaining, resolved_batch_size)
            rows = await _load_legacy_vector_documents(
                db,
                stock_codes=resolved_codes,
                doc_types=vector_doc_types,
                start_date=resolved_start_date,
                end_date=resolved_end_date,
                after_id=after_id,
                limit=fetch_limit,
            )
            if not rows:
                break
            after_id = max(int(item.get("legacy_id") or after_id) for item in rows)
            saved = await _save_grouped_backfill_rows(
                db,
                _group_backfill_items(rows),
                embed=resolved_embed,
                chunk_size=resolved_chunk_size,
                overlap=resolved_overlap,
                version=resolved_version,
                rebuild_existing=resolved_rebuild_existing,
                dry_run=resolved_dry_run,
            )
            for key in ("candidate_docs", "skipped_existing_docs", "saved_docs", "saved_chunks", "embedded_chunks"):
                results["sources"]["vector_documents"][key] += int(saved.get(key) or 0)
            if saved.get("profile_version_counts_by_doc_type"):
                bucket = results["sources"]["vector_documents"].setdefault("profile_version_counts_by_doc_type", {})
                for doc_type, version_counts in dict(saved.get("profile_version_counts_by_doc_type") or {}).items():
                    version_bucket = bucket.setdefault(str(doc_type or "").strip().lower(), {})
                    for profile_version, count in dict(version_counts or {}).items():
                        key = str(profile_version or "").strip()
                        if not key:
                            continue
                        version_bucket[key] = int(version_bucket.get(key) or 0) + int(count or 0)
            if saved.get("vector_dims_by_doc_type"):
                bucket = results["sources"]["vector_documents"].setdefault("vector_dims_by_doc_type", {})
                for doc_type, vector_dims in dict(saved.get("vector_dims_by_doc_type") or {}).items():
                    dims_bucket = bucket.setdefault(str(doc_type or "").strip().lower(), [])
                    for vector_dim in list(vector_dims or []):
                        resolved_dim = int(vector_dim or 0)
                        if resolved_dim > 0 and resolved_dim not in dims_bucket:
                            dims_bucket.append(resolved_dim)
            remaining -= len(rows)
            if len(rows) < fetch_limit:
                break

    if "research" in resolved_doc_types and remaining > 0:
        after_id = 0
        while remaining > 0:
            fetch_limit = min(remaining, resolved_batch_size)
            rows = await _load_legacy_research_reports(
                db,
                stock_codes=resolved_codes,
                start_date=resolved_start_date,
                end_date=resolved_end_date,
                after_id=after_id,
                limit=fetch_limit,
            )
            if not rows:
                break
            after_id = max(int(item.get("legacy_id") or after_id) for item in rows)
            saved = await _save_grouped_backfill_rows(
                db,
                _group_backfill_items(rows),
                embed=resolved_embed,
                chunk_size=resolved_chunk_size,
                overlap=resolved_overlap,
                version=resolved_version,
                rebuild_existing=resolved_rebuild_existing,
                dry_run=resolved_dry_run,
            )
            for key in ("candidate_docs", "skipped_existing_docs", "saved_docs", "saved_chunks", "embedded_chunks"):
                results["sources"]["research_reports"][key] += int(saved.get(key) or 0)
            if saved.get("profile_version_counts_by_doc_type"):
                bucket = results["sources"]["research_reports"].setdefault("profile_version_counts_by_doc_type", {})
                for doc_type, version_counts in dict(saved.get("profile_version_counts_by_doc_type") or {}).items():
                    version_bucket = bucket.setdefault(str(doc_type or "").strip().lower(), {})
                    for profile_version, count in dict(version_counts or {}).items():
                        key = str(profile_version or "").strip()
                        if not key:
                            continue
                        version_bucket[key] = int(version_bucket.get(key) or 0) + int(count or 0)
            if saved.get("vector_dims_by_doc_type"):
                bucket = results["sources"]["research_reports"].setdefault("vector_dims_by_doc_type", {})
                for doc_type, vector_dims in dict(saved.get("vector_dims_by_doc_type") or {}).items():
                    dims_bucket = bucket.setdefault(str(doc_type or "").strip().lower(), [])
                    for vector_dim in list(vector_dims or []):
                        resolved_dim = int(vector_dim or 0)
                        if resolved_dim > 0 and resolved_dim not in dims_bucket:
                            dims_bucket.append(resolved_dim)
            remaining -= len(rows)
            if len(rows) < fetch_limit:
                break

    totals = {
        "candidate_docs": 0,
        "skipped_existing_docs": 0,
        "saved_docs": 0,
        "saved_chunks": 0,
        "embedded_chunks": 0,
    }
    for source_result in results["sources"].values():
        for key in totals:
            totals[key] += int(source_result.get(key) or 0)
    profile_version_counts_by_doc_type: dict[str, dict[str, int]] = {}
    vector_dims_by_doc_type: dict[str, list[int]] = {}
    for source_result in results["sources"].values():
        for doc_type, version_counts in dict(source_result.get("profile_version_counts_by_doc_type") or {}).items():
            bucket = profile_version_counts_by_doc_type.setdefault(str(doc_type or "").strip().lower(), {})
            for profile_version, count in dict(version_counts or {}).items():
                key = str(profile_version or "").strip()
                if not key:
                    continue
                bucket[key] = int(bucket.get(key) or 0) + int(count or 0)
        for doc_type, dims in dict(source_result.get("vector_dims_by_doc_type") or {}).items():
            bucket = vector_dims_by_doc_type.setdefault(str(doc_type or "").strip().lower(), [])
            for vector_dim in list(dims or []):
                resolved_dim = int(vector_dim or 0)
                if resolved_dim > 0 and resolved_dim not in bucket:
                    bucket.append(resolved_dim)
    results.update(totals)
    results["profile_version_counts_by_doc_type"] = profile_version_counts_by_doc_type
    results["vector_dims_by_doc_type"] = vector_dims_by_doc_type
    results["processed_limit_reached"] = remaining <= 0
    return results
