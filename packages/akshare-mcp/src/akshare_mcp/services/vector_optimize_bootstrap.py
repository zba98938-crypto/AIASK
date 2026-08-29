"""Bootstrap orchestration for the pure-SQLite multi-vector layer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..vector_collection_scope import KLINE_COLLECTION_SPECS
from .factor_candidate_seed import seed_factor_candidate_records
from .factor_candidate_vector_backfill import backfill_factor_candidate_vectors
from .pattern_embedding_pipeline import backfill_kline_pattern_vectors
from .stock_profile_pipeline import backfill_stock_profile_vectors
from .unified_vector_governance import build_vector_collection_snapshot
from .vector_backfill import backfill_market_document_vectors


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _as_bool(value: Any, default: bool = False) -> bool:
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


def _normalize_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 5000) -> int:
    try:
        resolved = int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        resolved = int(default)
    return max(minimum, min(resolved, maximum))


async def _latest_resume_cursor(db) -> str:
    async with db.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT next_cursor
            FROM vector_optimization_runs
            WHERE status IN ('completed', 'partial')
              AND COALESCE(next_cursor, '') != ''
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
    return str(value or "").strip()


async def _load_code_batch(db, *, cursor: str, batch_size: int) -> list[str]:
    async with db.acquire() as conn:
        cols = await conn.fetch("SELECT name AS column_name FROM pragma_table_info('stocks')")
        col_names = {str(row.get("column_name") or "") for row in cols}
        code_col = "stock_code" if "stock_code" in col_names else "code"
        rows = await conn.fetch(
            f"""
            SELECT {code_col} AS code
            FROM stocks
            WHERE {code_col} > $1
            ORDER BY {code_col} ASC
            LIMIT $2
            """,
            str(cursor or ""),
            int(batch_size),
        )
    return [str(row.get("code") or "").strip() for row in rows if str(row.get("code") or "").strip()]


async def _upsert_stock_graph_nodes(db, codes: list[str], *, dry_run: bool) -> dict[str, int]:
    if dry_run or not codes:
        return {"nodes": 0, "edges": 0}
    nodes = 0
    edges = 0
    async with db.acquire() as conn:
        cols = await conn.fetch("SELECT name AS column_name FROM pragma_table_info('stocks')")
        col_names = {str(row.get("column_name") or "") for row in cols}
        code_col = "stock_code" if "stock_code" in col_names else "code"
        rows = await conn.fetch(
            f"""
            SELECT {code_col} AS code, stock_name, industry, market_cap, pe_ratio, pb_ratio
            FROM stocks
            WHERE {code_col} IN ($1)
            """,
            codes,
        )
        latest_financials = await conn.fetch(
            """
            SELECT f.*
            FROM financials f
            JOIN (
                SELECT stock_code, MAX(report_date) AS report_date
                FROM financials
                WHERE stock_code IN ($1)
                GROUP BY stock_code
            ) latest
              ON latest.stock_code = f.stock_code AND latest.report_date = f.report_date
            """,
            codes,
        )
        financial_by_code = {str(row.get("stock_code") or ""): dict(row) for row in latest_financials}
        for row in rows:
            payload = dict(row)
            code = str(payload.get("code") or "").strip()
            if not code:
                continue
            stock_node = f"stock:{code}"
            await conn.execute(
                """
                INSERT INTO vector_graph_nodes (node_key, node_type, entity_id, stock_code, label, metadata, created_at, updated_at)
                VALUES ($1, 'stock', $2, $2, $3, $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_key) DO UPDATE SET
                    label = EXCLUDED.label,
                    metadata = EXCLUDED.metadata,
                    updated_at = CURRENT_TIMESTAMP
                """,
                stock_node,
                code,
                payload.get("stock_name") or code,
                json.dumps(payload, ensure_ascii=False, default=str),
            )
            nodes += 1
            industry = str(payload.get("industry") or "").strip()
            if industry:
                industry_node = f"industry:{industry}"
                await conn.execute(
                    """
                    INSERT INTO vector_graph_nodes (node_key, node_type, entity_id, label, metadata, created_at, updated_at)
                    VALUES ($1, 'industry', $2, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (node_key) DO UPDATE SET
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    industry_node,
                    industry,
                    json.dumps({"industry": industry}, ensure_ascii=False, default=str),
                )
                nodes += 1
                await conn.execute(
                    """
                    INSERT INTO vector_graph_edges (edge_key, source_node_key, target_node_key, relation_type, weight, metadata, created_at, updated_at)
                    VALUES ($1, $2, $3, 'belongs_to_industry', 1.0, $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (edge_key) DO UPDATE SET
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    f"{stock_node}->belongs_to_industry->{industry_node}",
                    stock_node,
                    industry_node,
                    json.dumps({"industry": industry}, ensure_ascii=False, default=str),
                )
                edges += 1
            fin = financial_by_code.get(code)
            if fin:
                report_date = str(fin.get("report_date") or "").strip()
                report_node = f"financial_report:{code}:{report_date}"
                await conn.execute(
                    """
                    INSERT INTO vector_graph_nodes (node_key, node_type, entity_id, stock_code, label, metadata, created_at, updated_at)
                    VALUES ($1, 'financial_report', $2, $3, $4, $5, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (node_key) DO UPDATE SET
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    report_node,
                    f"{code}:{report_date}",
                    code,
                    report_date,
                    json.dumps(fin, ensure_ascii=False, default=str),
                )
                nodes += 1
                await conn.execute(
                    """
                    INSERT INTO vector_graph_edges (edge_key, source_node_key, target_node_key, relation_type, weight, metadata, created_at, updated_at)
                    VALUES ($1, $2, $3, 'has_financial_report', 1.0, $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (edge_key) DO UPDATE SET
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    f"{stock_node}->has_financial_report->{report_node}",
                    stock_node,
                    report_node,
                    json.dumps({"report_date": report_date}, ensure_ascii=False, default=str),
                )
                edges += 1
    return {"nodes": nodes, "edges": edges}


def _compact_errors(result: dict[str, Any]) -> list[str]:
    errors = [str(item) for item in list((result or {}).get("errors") or []) if str(item)]
    return errors[:20] + ([f"...and {len(errors) - 20} more"] if len(errors) > 20 else [])


async def _maybe_snapshot(
    db,
    *,
    collection_name: str,
    version: str,
    build_snapshot: bool,
    activate_snapshot: bool,
    profile_type: str | None = None,
    source: str,
) -> dict[str, Any] | None:
    if not build_snapshot:
        return None
    return await build_vector_collection_snapshot(
        db,
        collection_name=collection_name,
        version=version,
        profile_type=profile_type,
        limit_profiles=100000,
        activate=activate_snapshot,
        source=source,
    )


async def run_vector_optimize_bootstrap(
    db,
    *,
    scope: str = "full",
    dry_run: Any = False,
    resume: Any = True,
    batch_size: Any = 500,
    cursor: str | None = None,
    build_snapshot: Any = True,
    activate_snapshot: Any = True,
) -> dict[str, Any]:
    resolved_scope = str(scope or "full").strip().lower() or "full"
    resolved_dry_run = _as_bool(dry_run, False)
    resolved_resume = _as_bool(resume, True)
    resolved_batch_size = _normalize_positive_int(batch_size, 500, minimum=1, maximum=5000)
    resolved_build_snapshot = _as_bool(build_snapshot, True)
    resolved_activate_snapshot = _as_bool(activate_snapshot, True)
    resolved_cursor = str(cursor or "").strip()
    if resolved_resume and not resolved_cursor:
        resolved_cursor = await _latest_resume_cursor(db)
    codes = await _load_code_batch(db, cursor=resolved_cursor, batch_size=resolved_batch_size)
    next_cursor = codes[-1] if codes else ""
    more_available = bool(codes) and len(codes) >= resolved_batch_size
    run_id = f"vector_opt_{_now_id()}"
    quality_flags: list[str] = []
    stats: dict[str, Any] = {
        "run_id": run_id,
        "scope": resolved_scope,
        "cursor": resolved_cursor,
        "next_cursor": next_cursor,
        "batch_size": resolved_batch_size,
        "code_count": len(codes),
        "more_available": more_available,
        "dry_run": resolved_dry_run,
        "collections": {},
        "snapshots": {},
    }
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO vector_optimization_runs (
                run_id, status, scope, cursor, next_cursor, batch_size, dry_run, stats, quality_flags, created_at, updated_at
            )
            VALUES ($1, 'running', $2, $3, $4, $5, $6, $7, $8, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            run_id,
            resolved_scope,
            resolved_cursor or None,
            next_cursor or None,
            resolved_batch_size,
            1 if resolved_dry_run else 0,
            json.dumps(stats, ensure_ascii=False, default=str),
            json.dumps(quality_flags, ensure_ascii=False, default=str),
        )
    status = "completed"
    try:
        if not codes:
            quality_flags.append("no_codes_to_process")
        graph_result = await _upsert_stock_graph_nodes(db, codes, dry_run=resolved_dry_run)
        stats["graph"] = graph_result

        stock_result = await backfill_stock_profile_vectors(
            db,
            stock_codes=codes,
            code_limit=len(codes) or resolved_batch_size,
            profile_types=["fundamental", "technical", "both"],
            version="stock-profile-v1",
            rebuild_existing=False,
            dry_run=resolved_dry_run,
        )
        stats["collections"]["stock_profile_embeddings"] = stock_result
        if _compact_errors(stock_result):
            quality_flags.append("stock_profile_errors")
        snapshot = await _maybe_snapshot(
            db,
            collection_name="stock_profile_embeddings",
            version="stock-profile-v1",
            build_snapshot=resolved_build_snapshot and not resolved_dry_run,
            activate_snapshot=resolved_activate_snapshot,
            source="vector_optimize_bootstrap.stock_profiles",
        )
        if snapshot:
            stats["snapshots"]["stock_profile_embeddings"] = snapshot

        kline_specs = [
            ("returns", 20),
            ("price_volume", 60),
            ("ohlc", 30),
            ("technical", 20),
        ]
        for method, window_size in kline_specs:
            spec = KLINE_COLLECTION_SPECS.get((method, window_size), {})
            result = await backfill_kline_pattern_vectors(
                db,
                stock_codes=codes,
                code_limit=len(codes) or resolved_batch_size,
                window_size=window_size,
                lookback_days=max(180, window_size * 4),
                max_windows_per_code=1,
                step_days=5,
                vector_method=method,
                period="daily",
                adjust="",
                version="v1",
                rebuild_existing=False,
                dry_run=resolved_dry_run,
            )
            collection_name = str(result.get("collection_name") or spec.get("collection_name") or "")
            stats["collections"][collection_name or f"kline_{method}_{window_size}"] = result
            if _compact_errors(result):
                quality_flags.append(f"kline_errors:{method}:{window_size}")
            snapshot = await _maybe_snapshot(
                db,
                collection_name=collection_name,
                version="v1",
                build_snapshot=resolved_build_snapshot and not resolved_dry_run and bool(collection_name),
                activate_snapshot=resolved_activate_snapshot,
                profile_type=str(result.get("profile_type") or "") or None,
                source=f"vector_optimize_bootstrap.kline.{method}.{window_size}",
            )
            if snapshot and collection_name:
                stats["snapshots"][collection_name] = snapshot

        docs_result = await backfill_market_document_vectors(
            db,
            stock_codes=codes,
            doc_types=["news", "notice", "research"],
            limit=max(len(codes), 1),
            batch_size=min(max(len(codes), 1), 500),
            embed=True,
            chunk_size=800,
            overlap=120,
            version="v1",
            rebuild_existing=False,
            dry_run=resolved_dry_run,
            include_legacy_research_docs=True,
        )
        stats["collections"]["market_doc_chunks"] = docs_result
        if int(docs_result.get("candidate_docs") or 0) <= 0:
            quality_flags.append("market_documents_empty")

        factor_seed_result = await seed_factor_candidate_records(
            db,
            limit=resolved_batch_size,
            codes=codes,
            rebuild_existing=False,
            dry_run=resolved_dry_run,
        )
        stats["collections"]["factor_candidate_seed"] = factor_seed_result
        if _compact_errors(factor_seed_result):
            quality_flags.append("factor_candidate_seed_errors")

        factor_result = await backfill_factor_candidate_vectors(
            db,
            limit=resolved_batch_size,
            version="v1",
            rebuild_existing=False,
            dry_run=resolved_dry_run,
        )
        stats["collections"]["factor_candidate_embeddings"] = factor_result
        if int(factor_result.get("processed_records") or 0) <= 0:
            quality_flags.append("factor_candidates_empty")
        factor_snapshot = await _maybe_snapshot(
            db,
            collection_name="factor_candidate_embeddings",
            version="v1",
            build_snapshot=resolved_build_snapshot and not resolved_dry_run,
            activate_snapshot=resolved_activate_snapshot,
            source="vector_optimize_bootstrap.factor_candidates",
        )
        if factor_snapshot:
            stats["snapshots"]["factor_candidate_embeddings"] = factor_snapshot

        health = await db.get_strategy_vector_health() if hasattr(db, "get_strategy_vector_health") else {}
        stats["health"] = health
        status = "partial" if quality_flags and not resolved_dry_run else "completed"
    except Exception as exc:
        status = "failed"
        quality_flags.append(f"bootstrap_exception:{type(exc).__name__}")
        stats["error"] = f"{type(exc).__name__}: {exc}"
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE vector_optimization_runs
            SET status = $2,
                next_cursor = $3,
                stats = $4,
                quality_flags = $5,
                updated_at = CURRENT_TIMESTAMP,
                completed_at = CURRENT_TIMESTAMP
            WHERE run_id = $1
            """,
            run_id,
            status,
            next_cursor or None,
            json.dumps(stats, ensure_ascii=False, default=str),
            json.dumps(quality_flags, ensure_ascii=False, default=str),
        )
    return {
        "run_id": run_id,
        "status": status,
        "scope": resolved_scope,
        "cursor": resolved_cursor or None,
        "next_cursor": next_cursor or None,
        "batch_size": resolved_batch_size,
        "processed_codes": len(codes),
        "more_available": more_available,
        "dry_run": resolved_dry_run,
        "quality_flags": quality_flags,
        "stats": stats,
    }
