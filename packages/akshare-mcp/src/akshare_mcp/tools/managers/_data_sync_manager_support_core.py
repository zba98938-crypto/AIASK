"""数据同步管理器 - MCP 工具层，供用户/AI 按需触发同步任务。

与 DataSyncScheduler (services/data_sync_scheduler.py) 的区别：
- DataSyncScheduler 是后台自动调度器（启动时 + 每日 15:30）
- 本模块是 MCP 工具，通过 ``data_sync_manager(action=...)`` 按需执行
- sync_daily/sync_init.py 是独立脚本，用于深度历史全量回填
"""

import asyncio
from typing import Any
import argparse
import contextlib
import importlib.util
import json
import logging
import os
from io import StringIO
from pathlib import Path
from datetime import datetime, timedelta
from ...storage import get_db
from ...utils import ok, fail
from ..manager_protocol import normalize_manager_payload

logger = logging.getLogger(__name__)

def _normalize_kwargs(kwargs: dict) -> dict:
    raw = kwargs.get("kwargs")
    if isinstance(raw, dict):
        kwargs = {**kwargs, **raw}
    elif isinstance(raw, str):
        try:
            extra = json.loads(raw or "{}")
            if isinstance(extra, dict):
                kwargs = {**kwargs, **extra}
        except Exception:
            pass
    return kwargs

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[6]

def _core_market_script_path() -> Path:
    return _repo_root() / "scripts" / "audit_sync_core_market_data.py"

def _factor_context_script_path() -> Path:
    return _repo_root() / "scripts" / "audit_sync_factor_context_data.py"

def _now_local() -> datetime:
    return datetime.now().astimezone()

def _normalize_codes(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = str(raw).replace(";", ",").split(",")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        token = str(item).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized

def _decode_json_obj(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}

def _as_bool(value, default: bool = False) -> bool:
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

def _compute_next_run(schedule: str, now: datetime | None = None) -> datetime:
    current = (now or _now_local()).astimezone()
    normalized = str(schedule or "daily").strip().lower()
    delta_map = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "weekly": timedelta(days=7),
        "monthly": timedelta(days=30),
    }
    if normalized not in delta_map:
        raise ValueError(f"不支持的schedule: {schedule}")
    return (current + delta_map[normalized]).replace(microsecond=0)

def _build_schedule_params(task_type: str, kwargs: dict, codes: list[str]) -> dict:
    params: dict = {}
    if kwargs.get("priority"):
        params["priority"] = str(kwargs.get("priority"))

    if task_type == "core_market":
        params["years"] = max(int(kwargs.get("years", 1) or 1), 1)
        params["north_days"] = max(int(kwargs.get("north_days", 365) or 365), 1)
        params["margin_days"] = max(int(kwargs.get("margin_days", 90) or 90), 1)
        params["calendar_year"] = int(kwargs.get("calendar_year") or _now_local().year)
        stock_codes = _normalize_codes(kwargs.get("stock_codes"))
        if stock_codes:
            params["stock_codes"] = stock_codes
        elif codes:
            params["stock_codes"] = list(codes)
    elif task_type == "factor_context":
        params["news_days"] = max(int(kwargs.get("news_days", 30) or 30), 1)
        params["notice_days"] = max(int(kwargs.get("notice_days", 30) or 30), 1)
        params["item_limit"] = max(int(kwargs.get("item_limit", 10) or 10), 1)
        params["active_pool_limit"] = max(int(kwargs.get("active_pool_limit", 12) or 12), 1)
        params["task_run_limit"] = max(int(kwargs.get("task_run_limit", 50) or 50), 1)
        params["scope_sources"] = str(
            kwargs.get("scope_sources") or "explicit,representative,active_pool,factory_targets"
        ).strip()
    elif task_type == "market_temperature_snapshot_cache":
        params["limit"] = max(1, min(int(kwargs.get("limit", 1000) or 1000), 1000))
        params["top_n"] = max(0, min(int(kwargs.get("top_n", 20) or 20), 50))
        params["min_bars"] = max(2, min(int(kwargs.get("min_bars", 20) or 20), 120))
        if kwargs.get("as_of"):
            params["as_of"] = str(kwargs.get("as_of")).strip()
    elif task_type == "market_text_source_ingest":
        def _market_text_int(name: str, default: int, minimum: int = 0) -> int:
            raw = kwargs.get(name)
            try:
                value = int(default if raw is None or raw == "" else raw)
            except (TypeError, ValueError):
                value = int(default)
            return max(value, minimum)

        stock_codes = _normalize_codes(kwargs.get("stock_codes") or kwargs.get("codes"))
        if stock_codes:
            params["stock_codes"] = stock_codes
        doc_types = _normalize_codes(kwargs.get("doc_types"))
        if doc_types:
            params["doc_types"] = [str(item).strip().lower() for item in doc_types if str(item).strip()]
        params["news_limit"] = _market_text_int("news_limit", 50, 0)
        params["notice_limit"] = _market_text_int("notice_limit", 80, 0)
        params["official_notice_limit"] = _market_text_int("official_notice_limit", 30, 0)
        params["notice_days"] = _market_text_int("notice_days", 30, 1)
        params["code_notice_limit"] = _market_text_int("code_notice_limit", 2, 0)
        params["code_notice_code_limit"] = _market_text_int("code_notice_code_limit", 20, 0)
        params["research_code_limit"] = _market_text_int("research_code_limit", 30, 0)
        params["research_per_code"] = _market_text_int("research_per_code", 2, 0)
        params["chunk_size"] = _market_text_int("chunk_size", 1000, 200)
        params["overlap"] = _market_text_int("overlap", 120, 0)
        params["version"] = str(kwargs.get("version", "v1") or "v1").strip()
        params["embed"] = _as_bool(kwargs.get("embed", True), True)
        params["build_snapshot"] = _as_bool(kwargs.get("build_snapshot", True), True)
        params["activate_snapshot"] = _as_bool(kwargs.get("activate_snapshot", True), True)
        params["allow_network"] = _as_bool(kwargs.get("allow_network", True), True)
        params["dry_run"] = _as_bool(kwargs.get("dry_run", False), False)
    elif task_type == "vector_backfill_market_docs":
        stock_codes = _normalize_codes(kwargs.get("stock_codes"))
        if stock_codes:
            params["stock_codes"] = stock_codes
        elif codes:
            params["stock_codes"] = list(codes)
        doc_types = _normalize_codes(kwargs.get("doc_types"))
        if doc_types:
            params["doc_types"] = [str(item).strip().lower() for item in doc_types if str(item).strip()]
        if kwargs.get("start_date"):
            params["start_date"] = str(kwargs.get("start_date")).strip()
        if kwargs.get("end_date"):
            params["end_date"] = str(kwargs.get("end_date")).strip()
        params["limit"] = max(int(kwargs.get("limit", 500) or 500), 1)
        params["batch_size"] = max(int(kwargs.get("batch_size", 100) or 100), 1)
        params["chunk_size"] = max(int(kwargs.get("chunk_size", 800) or 800), 200)
        params["overlap"] = max(int(kwargs.get("overlap", 120) or 120), 0)
        params["version"] = str(kwargs.get("version", "v1") or "v1").strip()
        params["embed"] = _as_bool(kwargs.get("embed", True), True)
        params["dry_run"] = _as_bool(kwargs.get("dry_run", False), False)
        params["rebuild_existing"] = _as_bool(kwargs.get("rebuild_existing", False), False)
        params["include_legacy_research_docs"] = _as_bool(
            kwargs.get("include_legacy_research_docs", False),
            False,
        )
        params["build_snapshot"] = _as_bool(kwargs.get("build_snapshot", False), False)
        params["activate_snapshot"] = _as_bool(kwargs.get("activate_snapshot", True), True)
        if kwargs.get("index_version"):
            params["index_version"] = str(kwargs.get("index_version")).strip()
    elif task_type == "vector_backfill_kline_patterns":
        stock_codes = _normalize_codes(kwargs.get("stock_codes"))
        if stock_codes:
            params["stock_codes"] = stock_codes
        elif codes:
            params["stock_codes"] = list(codes)
        params["code_limit"] = max(int(kwargs.get("code_limit", 200) or 200), 1)
        params["window_size"] = max(int(kwargs.get("window_size", kwargs.get("days", 20)) or 20), 5)
        params["lookback_days"] = max(int(kwargs.get("lookback_days", 180) or 180), params["window_size"])
        params["max_windows_per_code"] = max(int(kwargs.get("max_windows_per_code", 1) or 1), 1)
        params["step_days"] = max(int(kwargs.get("step_days", 5) or 5), 1)
        params["vector_method"] = str(kwargs.get("vector_method", "returns") or "returns").strip().lower()
        params["period"] = str(kwargs.get("period", "daily") or "daily").strip().lower()
        params["adjust"] = str(kwargs.get("adjust", "") or "").strip().lower()
        params["version"] = str(kwargs.get("version", "v1") or "v1").strip()
        params["dry_run"] = _as_bool(kwargs.get("dry_run", False), False)
        params["rebuild_existing"] = _as_bool(kwargs.get("rebuild_existing", False), False)
        params["build_snapshot"] = _as_bool(kwargs.get("build_snapshot", False), False)
        params["activate_snapshot"] = _as_bool(kwargs.get("activate_snapshot", True), True)
        if kwargs.get("index_version"):
            params["index_version"] = str(kwargs.get("index_version")).strip()
    elif task_type == "vector_backfill_stock_profiles":
        stock_codes = _normalize_codes(kwargs.get("stock_codes"))
        if stock_codes:
            params["stock_codes"] = stock_codes
        elif codes:
            params["stock_codes"] = list(codes)
        profile_types = _normalize_codes(kwargs.get("profile_types") or kwargs.get("similarity_types"))
        if profile_types:
            params["profile_types"] = [str(item).strip().lower() for item in profile_types if str(item).strip()]
        params["code_limit"] = max(int(kwargs.get("code_limit", 200) or 200), 1)
        params["kline_limit"] = max(int(kwargs.get("kline_limit", 90) or 90), 30)
        params["version"] = str(kwargs.get("version", "v1") or "v1").strip()
        params["dry_run"] = _as_bool(kwargs.get("dry_run", False), False)
        params["rebuild_existing"] = _as_bool(kwargs.get("rebuild_existing", False), False)
        params["build_snapshot"] = _as_bool(kwargs.get("build_snapshot", False), False)
        params["activate_snapshot"] = _as_bool(kwargs.get("activate_snapshot", True), True)
        if kwargs.get("index_version"):
            params["index_version"] = str(kwargs.get("index_version")).strip()
    elif task_type == "vector_backfill_factor_candidates":
        params["limit"] = max(int(kwargs.get("limit", 200) or 200), 1)
        codes_arg = _normalize_codes(kwargs.get("codes") or kwargs.get("stock_codes"))
        if codes_arg:
            params["codes"] = list(codes_arg)
        if kwargs.get("status"):
            params["status"] = str(kwargs.get("status")).strip().lower()
        if kwargs.get("family"):
            params["family"] = str(kwargs.get("family")).strip().lower()
        params["version"] = str(kwargs.get("version", "v1") or "v1").strip()
        params["dry_run"] = _as_bool(kwargs.get("dry_run", False), False)
        params["rebuild_existing"] = _as_bool(kwargs.get("rebuild_existing", False), False)
        params["build_snapshot"] = _as_bool(kwargs.get("build_snapshot", False), False)
        params["activate_snapshot"] = _as_bool(kwargs.get("activate_snapshot", True), True)
        params["seed_if_empty"] = _as_bool(kwargs.get("seed_if_empty", True), True)
        params["seed_limit"] = max(int(kwargs.get("seed_limit", params["limit"]) or params["limit"]), 1)
        params["seed_rebuild_existing"] = _as_bool(kwargs.get("seed_rebuild_existing", False), False)
        if kwargs.get("index_version"):
            params["index_version"] = str(kwargs.get("index_version")).strip()
    elif task_type == "factor_external_research_ingest":
        params["limit"] = max(int(kwargs.get("limit", 20) or 20), 1)
        codes_arg = _normalize_codes(kwargs.get("codes") or kwargs.get("stock_codes"))
        if codes_arg:
            params["codes"] = list(codes_arg)
        if kwargs.get("sources") is not None:
            params["sources"] = kwargs.get("sources")
        params["allow_network"] = _as_bool(kwargs.get("allow_network", True), True)
        params["timeout_sec"] = max(float(kwargs.get("timeout_sec", 8.0) or 8.0), 1.0)
        params["create_candidates"] = _as_bool(kwargs.get("create_candidates", True), True)
        params["dry_run"] = _as_bool(kwargs.get("dry_run", False), False)
        params["rebuild_existing"] = _as_bool(kwargs.get("rebuild_existing", False), False)
        params["backfill_vectors"] = _as_bool(kwargs.get("backfill_vectors", True), True)
        params["vector_limit"] = max(int(kwargs.get("vector_limit", kwargs.get("limit", 200)) or 200), 1)
        params["version"] = str(kwargs.get("version", "v1") or "v1").strip()
        params["build_snapshot"] = _as_bool(kwargs.get("build_snapshot", False), False)
        params["activate_snapshot"] = _as_bool(kwargs.get("activate_snapshot", True), True)
        params["vector_rebuild_existing"] = _as_bool(kwargs.get("vector_rebuild_existing", False), False)
        if kwargs.get("status"):
            params["status"] = str(kwargs.get("status")).strip().lower()
        if kwargs.get("family"):
            params["family"] = str(kwargs.get("family")).strip().lower()
        if kwargs.get("index_version"):
            params["index_version"] = str(kwargs.get("index_version")).strip()
    elif task_type == "vector_build_snapshot":
        params["collection_name"] = str(kwargs.get("collection_name") or kwargs.get("collection") or "").strip()
        if kwargs.get("profile_type"):
            params["profile_type"] = str(kwargs.get("profile_type")).strip()
        if kwargs.get("version"):
            params["version"] = str(kwargs.get("version")).strip()
        if kwargs.get("index_version"):
            params["index_version"] = str(kwargs.get("index_version")).strip()
        params["limit_profiles"] = max(int(kwargs.get("limit_profiles", 5000) or 5000), 1)
        if kwargs.get("bucket_count") is not None:
            params["bucket_count"] = max(int(kwargs.get("bucket_count") or 1), 1)
        params["activate"] = _as_bool(kwargs.get("activate", True), True)
    elif task_type == "vector_benchmark_collection":
        params["collection_name"] = str(kwargs.get("collection_name") or kwargs.get("collection") or "").strip()
        if kwargs.get("profile_type"):
            params["profile_type"] = str(kwargs.get("profile_type")).strip()
        if kwargs.get("version"):
            params["version"] = str(kwargs.get("version")).strip()
        if kwargs.get("index_version"):
            params["index_version"] = str(kwargs.get("index_version")).strip()
        params["sample_size"] = max(int(kwargs.get("sample_size", 30) or 30), 1)
        params["top_k"] = max(int(kwargs.get("top_k", 10) or 10), 1)
        params["limit_profiles"] = max(int(kwargs.get("limit_profiles", 5000) or 5000), 10)
        params["metric"] = str(kwargs.get("metric", "cosine") or "cosine").strip().lower()
        params["persist_snapshot_metrics"] = _as_bool(kwargs.get("persist_snapshot_metrics", True), True)
    elif task_type == "vector_optimize_bootstrap":
        params["scope"] = str(kwargs.get("scope", "full") or "full").strip().lower()
        params["dry_run"] = _as_bool(kwargs.get("dry_run", False), False)
        params["resume"] = _as_bool(kwargs.get("resume", True), True)
        params["batch_size"] = max(int(kwargs.get("batch_size", 500) or 500), 1)
        params["build_snapshot"] = _as_bool(kwargs.get("build_snapshot", True), True)
        params["activate_snapshot"] = _as_bool(kwargs.get("activate_snapshot", True), True)
        if kwargs.get("cursor"):
            params["cursor"] = str(kwargs.get("cursor")).strip()
    elif task_type == "factor_validation_bootstrap":
        params["status"] = str(kwargs.get("status", "review") or "review").strip().lower()
        params["max_candidates"] = max(int(kwargs.get("max_candidates", 50) or 50), 1)
        params["horizon_days"] = max(int(kwargs.get("horizon_days", 10) or 10), 1)
        params["max_dates"] = max(int(kwargs.get("max_dates", 60) or 60), 5)
        params["lookback_bars"] = max(int(kwargs.get("lookback_bars", 220) or 220), 80)
        params["min_cross_section"] = max(int(kwargs.get("min_cross_section", 100) or 100), 3)
        params["promote"] = _as_bool(kwargs.get("promote", True), True)
        params["resume"] = _as_bool(kwargs.get("resume", True), True)
        params["dry_run"] = _as_bool(kwargs.get("dry_run", False), False)
        params["persist_outputs"] = _as_bool(kwargs.get("persist_outputs", True), True)
        if kwargs.get("universe_limit") is not None:
            params["universe_limit"] = max(int(kwargs.get("universe_limit") or 1), 1)
        candidate_ids = _normalize_codes(kwargs.get("candidate_ids") or kwargs.get("artifact_ids"))
        if candidate_ids:
            params["candidate_ids"] = candidate_ids
        if kwargs.get("family"):
            params["family"] = str(kwargs.get("family")).strip().lower()
        stock_codes = _normalize_codes(kwargs.get("stock_codes") or kwargs.get("codes"))
        if stock_codes:
            params["stock_codes"] = stock_codes
    else:
        period = kwargs.get("period")
        if period:
            params["period"] = str(period)

    return params

def _build_task_payload(task_type: str, codes: list[str], payload: dict | None = None) -> dict:
    merged = dict(payload or {})
    if codes:
        merged["codes"] = list(codes)
    if task_type == "core_market":
        stock_codes = _normalize_codes(merged.get("stock_codes"))
        if not stock_codes and codes:
            stock_codes = list(codes)
        if stock_codes:
            merged["stock_codes"] = stock_codes
    elif task_type == "factor_context":
        merged["codes"] = list(codes)
    elif task_type == "market_temperature_snapshot_cache":
        merged["limit"] = max(1, min(int(merged.get("limit", 1000) or 1000), 1000))
        merged["top_n"] = max(0, min(int(merged.get("top_n", 20) or 20), 50))
        merged["min_bars"] = max(2, min(int(merged.get("min_bars", 20) or 20), 120))
        if merged.get("as_of"):
            merged["as_of"] = str(merged.get("as_of")).strip()
    elif task_type == "market_text_source_ingest":
        stock_codes = _normalize_codes(merged.get("stock_codes") or merged.get("codes"))
        if not stock_codes and codes:
            stock_codes = list(codes)
        if stock_codes:
            merged["stock_codes"] = stock_codes
        doc_types = _normalize_codes(merged.get("doc_types"))
        if doc_types:
            merged["doc_types"] = [str(item).strip().lower() for item in doc_types if str(item).strip()]
    elif task_type == "vector_backfill_market_docs":
        stock_codes = _normalize_codes(merged.get("stock_codes"))
        if not stock_codes and codes:
            stock_codes = list(codes)
        if stock_codes:
            merged["stock_codes"] = stock_codes
        doc_types = _normalize_codes(merged.get("doc_types"))
        if doc_types:
            merged["doc_types"] = [str(item).strip().lower() for item in doc_types if str(item).strip()]
    elif task_type == "vector_backfill_kline_patterns":
        stock_codes = _normalize_codes(merged.get("stock_codes"))
        if not stock_codes and codes:
            stock_codes = list(codes)
        if stock_codes:
            merged["stock_codes"] = stock_codes
    elif task_type == "vector_backfill_stock_profiles":
        stock_codes = _normalize_codes(merged.get("stock_codes"))
        if not stock_codes and codes:
            stock_codes = list(codes)
        if stock_codes:
            merged["stock_codes"] = stock_codes
        profile_types = _normalize_codes(merged.get("profile_types") or merged.get("similarity_types"))
        if profile_types:
            merged["profile_types"] = [str(item).strip().lower() for item in profile_types if str(item).strip()]
    elif task_type == "vector_backfill_factor_candidates":
        record_codes = _normalize_codes(merged.get("codes") or merged.get("stock_codes"))
        if record_codes:
            merged["codes"] = record_codes
    elif task_type == "factor_external_research_ingest":
        record_codes = _normalize_codes(merged.get("codes") or merged.get("stock_codes"))
        if record_codes:
            merged["codes"] = record_codes
    elif task_type == "vector_build_snapshot":
        if merged.get("collection_name"):
            merged["collection_name"] = str(merged.get("collection_name")).strip()
    elif task_type == "vector_benchmark_collection":
        if merged.get("collection_name"):
            merged["collection_name"] = str(merged.get("collection_name")).strip()
    elif task_type == "vector_optimize_bootstrap":
        merged["scope"] = str(merged.get("scope", "full") or "full").strip().lower()
    elif task_type == "factor_validation_bootstrap":
        candidate_ids = _normalize_codes(merged.get("candidate_ids") or merged.get("artifact_ids"))
        if candidate_ids:
            merged["candidate_ids"] = candidate_ids
        stock_codes = _normalize_codes(merged.get("stock_codes") or merged.get("codes"))
        if stock_codes:
            merged["stock_codes"] = stock_codes
    return merged

async def _update_schedule_runtime(db, schedule_id: str, *, last_run: datetime, next_run: datetime) -> None:
    async with db.acquire() as conn:
        try:
            await conn.execute(
                """
                UPDATE sync_schedules
                SET last_run = $2, next_run = $3, updated_at = CURRENT_TIMESTAMP
                WHERE schedule_id = $1
                """,
                schedule_id,
                last_run,
                next_run,
            )
        except Exception:
            await conn.execute(
                """
                UPDATE sync_schedules
                SET last_run = $2, next_run = $3
                WHERE schedule_id = $1
                """,
                schedule_id,
                last_run,
                next_run,
            )

async def _execute_sync_task(
    db,
    *,
    task_type: str,
    codes: list[str],
    priority: str,
    payload: dict | None = None,
    trigger: str = "manual",
    schedule_id: str | None = None,
) -> dict:
    task_id = f"sync_{task_type}_{int(datetime.now().timestamp())}"
    results = {"success": 0, "failed": 0, "errors": []}
    final_status = "completed"
    effective_codes = _normalize_codes(codes)
    effective_payload = _build_task_payload(task_type, effective_codes, payload)

    if task_type not in {
        "core_market",
        "factor_context",
        "market_temperature_snapshot_cache",
        "market_text_source_ingest",
        "vector_backfill_market_docs",
        "vector_backfill_kline_patterns",
        "vector_backfill_stock_profiles",
        "vector_backfill_factor_candidates",
        "factor_external_research_ingest",
        "vector_build_snapshot",
        "vector_benchmark_collection",
        "vector_optimize_bootstrap",
        "factor_validation_bootstrap",
    } and not effective_codes:
        raise ValueError("需要提供codes参数")

    try:
        async with db.acquire() as conn:
            await conn.execute(
                """INSERT INTO sync_tasks (task_id, task_type, codes, priority, status, created_at)
                   VALUES ($1, $2, $3, $4, 'running', CURRENT_TIMESTAMP)""",
                task_id,
                task_type,
                effective_codes,
                priority,
            )
    except Exception as e:
        logger.warning(f"[DataSyncManager] 写入任务记录失败: {e}")

    timeout_sec = _sync_task_timeout_sec(task_type)
    try:
        runner = None
        if task_type == "kline":
            runner = _sync_klines_now(effective_codes)
        elif task_type == "quote":
            runner = _sync_quotes_now(effective_codes)
        elif task_type == "financial":
            runner = _sync_financials_check(effective_codes)
        elif task_type == "core_market":
            runner = _sync_core_market_now(effective_payload)
        elif task_type == "factor_context":
            runner = _sync_factor_context_now(effective_payload)
        elif task_type == "market_temperature_snapshot_cache":
            runner = _sync_market_temperature_snapshot_cache_now(effective_payload)
        elif task_type == "market_text_source_ingest":
            runner = _sync_market_text_source_ingest_now(effective_payload)
        elif task_type == "vector_backfill_market_docs":
            runner = _sync_vector_backfill_market_docs_now(effective_payload)
        elif task_type == "vector_backfill_kline_patterns":
            runner = _sync_vector_backfill_kline_patterns_now(effective_payload)
        elif task_type == "vector_backfill_stock_profiles":
            runner = _sync_vector_backfill_stock_profiles_now(effective_payload)
        elif task_type == "vector_backfill_factor_candidates":
            runner = _sync_vector_backfill_factor_candidates_now(effective_payload)
        elif task_type == "factor_external_research_ingest":
            runner = _sync_factor_external_research_ingest_now(effective_payload)
        elif task_type == "vector_build_snapshot":
            runner = _sync_vector_build_snapshot_now(effective_payload)
        elif task_type == "vector_benchmark_collection":
            runner = _sync_vector_benchmark_collection_now(effective_payload)
        elif task_type == "vector_optimize_bootstrap":
            runner = _sync_vector_optimize_bootstrap_now(effective_payload)
        elif task_type == "factor_validation_bootstrap":
            runner = _sync_factor_validation_bootstrap_now(effective_payload)
        else:
            runner = _sync_klines_now(effective_codes)

        if timeout_sec is not None:
            results = await asyncio.wait_for(runner, timeout=timeout_sec)
        else:
            results = await runner

        if results.get("failed", 0) > 0 and results.get("success", 0) == 0:
            final_status = "failed"
    except asyncio.TimeoutError:
        final_status = "failed"
        results["failed"] = max(int(results.get("failed") or 0), 1)
        results["errors"].append(f"{task_type}_timeout_after_{timeout_sec:g}s")
        logger.warning(
            "[DataSyncManager] %s sync timed out after %.3fs",
            task_type,
            timeout_sec,
        )
    except Exception as e:
        final_status = "failed"
        results["errors"].append(str(e))
        logger.warning(f"[DataSyncManager] 同步执行异常: {e}")

    try:
        async with db.acquire() as conn:
            try:
                await conn.execute(
                    """
                    UPDATE sync_tasks
                    SET status = $1, error_message = $2, updated_at = CURRENT_TIMESTAMP, completed_at = CURRENT_TIMESTAMP
                    WHERE task_id = $3
                    """,
                    final_status,
                    "; ".join(results.get("errors") or [])[:2000] or None,
                    task_id,
                )
            except Exception:
                await conn.execute(
                    "UPDATE sync_tasks SET status = $1 WHERE task_id = $2",
                    final_status,
                    task_id,
                )
    except Exception:
        pass

    return {
        "task_id": task_id,
        "task_type": task_type,
        "codes_count": len(effective_codes),
        "priority": priority,
        "status": final_status,
        "results": results,
        "trigger": trigger,
        "schedule_id": schedule_id,
        "message": f'同步完成: 成功 {results.get("success", 0)}, 失败 {results.get("failed", 0)}',
    }


def _sync_task_timeout_sec(task_type: str) -> float | None:
    normalized = str(task_type or "").strip().lower()
    env_names = {
        "core_market": "DATA_SYNC_CORE_MARKET_TIMEOUT_SEC",
        "factor_context": "DATA_SYNC_FACTOR_CONTEXT_TIMEOUT_SEC",
        "market_text_source_ingest": "DATA_SYNC_MARKET_TEXT_SOURCE_INGEST_TIMEOUT_SEC",
        "factor_validation_bootstrap": "DATA_SYNC_FACTOR_VALIDATION_BOOTSTRAP_TIMEOUT_SEC",
    }
    defaults = {
        "core_market": 45.0,
        "factor_context": 45.0,
        "market_text_source_ingest": 1800.0,
        "factor_validation_bootstrap": 1800.0,
    }
    env_name = env_names.get(normalized)
    if env_name is None:
        return None
    raw = str(os.getenv(env_name, str(defaults[normalized])) or defaults[normalized]).strip()
    try:
        value = float(raw)
    except Exception:
        value = defaults[normalized]
    return max(0.001, min(value, 1800.0))

async def _run_due_schedules(
    db,
    *,
    force: bool = False,
    limit: int = 20,
    schedule_id: str | None = None,
    task_type: str | None = None,
) -> dict:
    now = _now_local()
    async with db.acquire() as conn:
        if force:
            if schedule_id:
                rows = await conn.fetch(
                    """
                    SELECT * FROM sync_schedules
                    WHERE enabled = true AND schedule_id = $1
                    ORDER BY COALESCE(next_run, created_at) ASC
                    LIMIT $2
                    """,
                    schedule_id,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM sync_schedules
                    WHERE enabled = true
                    ORDER BY COALESCE(next_run, created_at) ASC
                    LIMIT $1
                    """,
                    limit,
                )
        else:
            if schedule_id:
                rows = await conn.fetch(
                    """
                    SELECT * FROM sync_schedules
                    WHERE enabled = true
                      AND schedule_id = $1
                      AND (next_run IS NULL OR next_run <= $2)
                    ORDER BY COALESCE(next_run, created_at) ASC
                    LIMIT $3
                    """,
                    schedule_id,
                    now,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM sync_schedules
                    WHERE enabled = true
                      AND (next_run IS NULL OR next_run <= $1)
                    ORDER BY COALESCE(next_run, created_at) ASC
                    LIMIT $2
                    """,
                    now,
                    limit,
                )

    if task_type:
        normalized_task_type = str(task_type).strip().lower()
        rows = [
            row for row in rows
            if str(dict(row).get("task_type") or "").strip().lower() == normalized_task_type
        ]
    rows = list(rows)[: max(1, int(limit or 20))]

    executions = []
    for row in rows:
        schedule = dict(row)
        schedule["params"] = _decode_json_obj(schedule.get("params"))
        task_type = str(schedule.get("task_type") or "kline")
        codes = _normalize_codes(schedule.get("codes"))
        params = dict(schedule.get("params") or {})
        priority = str(params.get("priority") or "normal")
        payload = _build_task_payload(task_type, codes, params)
        run_started_at = _now_local()
        result = await _execute_sync_task(
            db,
            task_type=task_type,
            codes=codes,
            priority=priority,
            payload=payload,
            trigger="schedule",
            schedule_id=str(schedule.get("schedule_id")),
        )
        next_run = _compute_next_run(str(schedule.get("schedule") or "daily"), run_started_at)
        await _update_schedule_runtime(
            db,
            str(schedule.get("schedule_id")),
            last_run=run_started_at,
            next_run=next_run,
        )
        executions.append(
            {
                "schedule_id": schedule.get("schedule_id"),
                "task_type": task_type,
                "schedule": schedule.get("schedule"),
                "last_run": run_started_at.isoformat(),
                "next_run": next_run.isoformat(),
                "task": result,
            }
        )

    return {
        "matched": len(rows),
        "executed": len(executions),
        "force": force,
        "schedules": executions,
    }

async def _count_enabled_schedules(db, *, task_type: str) -> int:
    async with db.acquire() as conn:
        try:
            value = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM sync_schedules
                WHERE enabled = true AND task_type = $1
                """,
                str(task_type or "").strip().lower(),
            )
            return int(value or 0)
        except Exception:
            return 0

async def _ensure_runtime_warmup_schedule(db, *, task_type: str) -> dict:
    normalized_task_type = str(task_type or "core_market").strip().lower() or "core_market"
    schedule_id = f"schedule_runtime_{normalized_task_type}"
    params = _build_schedule_params(normalized_task_type, {}, [])
    next_run = _now_local().replace(microsecond=0)
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sync_schedules (schedule_id, task_type, codes, schedule, params, enabled, next_run, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, CURRENT_TIMESTAMP)
            ON CONFLICT (schedule_id) DO UPDATE SET
                task_type = EXCLUDED.task_type,
                codes = EXCLUDED.codes,
                schedule = EXCLUDED.schedule,
                params = EXCLUDED.params,
                enabled = EXCLUDED.enabled,
                next_run = EXCLUDED.next_run
            """,
            schedule_id,
            normalized_task_type,
            [],
            "daily",
            json.dumps(params, ensure_ascii=False, default=str),
            True,
            next_run,
        )
    return {
        "schedule_id": schedule_id,
        "task_type": normalized_task_type,
        "schedule": "daily",
        "enabled": True,
        "params": params,
        "next_run": next_run.isoformat(),
    }

async def run_runtime_data_warmup(
    *,
    task_type: str = "core_market",
    force: bool = False,
    limit: int = 4,
    schedule_id: str | None = None,
    source: str = "runtime",
    bootstrap_missing: bool = True,
) -> dict:
    db = get_db()
    raw_task_type = str(task_type or "core_market").strip().lower() or "core_market"
    task_types = [item for item in _normalize_codes(raw_task_type) if item]
    if not task_types:
        task_types = ["core_market"]

    schedule_results = []
    schedules = []
    bootstrapped_task_types: list[str] = []
    bootstrapped_schedules: list[dict] = []
    for one_task_type in task_types:
        result = await _run_due_schedules(
            db,
            force=force,
            limit=max(1, int(limit or 4)),
            schedule_id=schedule_id,
            task_type=one_task_type,
        )
        if bootstrap_missing and result.get("matched", 0) <= 0:
            existing_schedule_count = await _count_enabled_schedules(db, task_type=one_task_type)
            if existing_schedule_count <= 0:
                bootstrapped = await _ensure_runtime_warmup_schedule(db, task_type=one_task_type)
                bootstrapped_task_types.append(one_task_type)
                bootstrapped_schedules.append(bootstrapped)
                result = await _run_due_schedules(
                    db,
                    force=force,
                    limit=max(1, int(limit or 4)),
                    schedule_id=None,
                    task_type=one_task_type,
                )
                result["bootstrapped"] = True
                result["bootstrap_schedule"] = bootstrapped
            else:
                result["bootstrapped"] = False
        else:
            result["bootstrapped"] = False
        schedule_results.append({"task_type": one_task_type, **result})
        schedules.extend(list(result.get("schedules") or []))

    failed_items = [
        item for item in schedules
        if str(((item.get("task") or {}).get("status") or "")).strip().lower() not in {"completed", "success"}
    ]
    status = "completed"
    ok = True
    matched_total = sum(int(item.get("matched") or 0) for item in schedule_results)
    executed_total = sum(int(item.get("executed") or 0) for item in schedule_results)
    if matched_total <= 0:
        status = "skipped"
    elif failed_items and len(failed_items) == len(schedules):
        status = "failed"
        ok = False
    elif failed_items:
        status = "partial"

    return {
        "ok": ok,
        "status": status,
        "source": source,
        "task_type": raw_task_type,
        "task_types": task_types,
        "force": bool(force),
        "bootstrap_missing": bool(bootstrap_missing),
        "bootstrapped_task_types": bootstrapped_task_types,
        "bootstrapped_schedules": bootstrapped_schedules,
        "schedule_id": schedule_id,
        "matched": matched_total,
        "executed": executed_total,
        "failed": len(failed_items),
        "executed_task_ids": [
            (item.get("task") or {}).get("task_id")
            for item in schedules
            if (item.get("task") or {}).get("task_id")
        ],
        "failed_schedule_ids": [
            item.get("schedule_id")
            for item in failed_items
            if item.get("schedule_id")
        ],
        "results_by_task_type": schedule_results,
        "schedules": schedules,
    }

async def _load_market_aux_status(db) -> dict:
    async with db.acquire() as conn:
        def _iso_or_str(value) -> str | None:
            if value is None:
                return None
            return value.isoformat() if hasattr(value, "isoformat") else str(value)

        async def _fetch_meta(table: str, date_col: str = "trade_date") -> dict:
            try:
                row = await conn.fetchrow(
                    f"SELECT COUNT(*) AS cnt, MIN({date_col}) AS min_d, MAX({date_col}) AS max_d FROM {table}"
                )
            except Exception:
                row = None
            if not row:
                return {"count": 0, "min_date": None, "max_date": None}
            return {
                "count": int(row.get("cnt") or 0),
                "min_date": _iso_or_str(row.get("min_d")),
                "max_date": _iso_or_str(row.get("max_d")),
            }

        return {
            "north_fund_flow": await _fetch_meta("north_fund_flow"),
            "margin_market_flow": await _fetch_meta("margin_market_flow"),
            "margin_detail": await _fetch_meta("margin_detail"),
            "vector_documents": await conn.fetchval("SELECT COUNT(*) FROM vector_documents") or 0,
            "vector_documents_news": await conn.fetchval("SELECT COUNT(*) FROM vector_documents WHERE doc_type = 'news'") or 0,
            "vector_documents_notice": await conn.fetchval("SELECT COUNT(*) FROM vector_documents WHERE doc_type = 'notice'") or 0,
            "vector_documents_research": await conn.fetchval("SELECT COUNT(*) FROM vector_documents WHERE doc_type = 'research'") or 0,
            "market_documents": await conn.fetchval("SELECT COUNT(*) FROM market_documents") or 0,
            "market_doc_chunks": await conn.fetchval("SELECT COUNT(*) FROM market_doc_chunks") or 0,
            "market_doc_chunks_news": await conn.fetchval("SELECT COUNT(*) FROM market_doc_chunks WHERE doc_type = 'news'") or 0,
            "market_doc_chunks_notice": await conn.fetchval("SELECT COUNT(*) FROM market_doc_chunks WHERE doc_type = 'notice'") or 0,
            "market_doc_chunks_research": await conn.fetchval("SELECT COUNT(*) FROM market_doc_chunks WHERE doc_type = 'research'") or 0,
            "vector_collections": await conn.fetchval("SELECT COUNT(*) FROM vector_collections") or 0,
            "vector_profiles": await conn.fetchval("SELECT COUNT(*) FROM vector_profiles") or 0,
            "vector_index_snapshots": await conn.fetchval("SELECT COUNT(*) FROM vector_index_snapshots") or 0,
            "kline_pattern_windows": await conn.fetchval("SELECT COUNT(*) FROM kline_pattern_windows") or 0,
            "vector_profiles_kline_patterns": await conn.fetchval(
                "SELECT COUNT(*) FROM vector_profiles WHERE collection_name = 'kline_pattern_embeddings'"
            ) or 0,
            "vector_profiles_stock_profiles": await conn.fetchval(
                "SELECT COUNT(*) FROM vector_profiles WHERE collection_name = 'stock_profile_embeddings'"
            ) or 0,
            "vector_profiles_factor_candidates": await conn.fetchval(
                "SELECT COUNT(*) FROM vector_profiles WHERE collection_name = 'factor_candidate_embeddings'"
            ) or 0,
            "vector_dimension_contracts": await conn.fetchval("SELECT COUNT(*) FROM vector_dimension_contracts") or 0,
            "vector_graph_nodes": await conn.fetchval("SELECT COUNT(*) FROM vector_graph_nodes") or 0,
            "vector_graph_edges": await conn.fetchval("SELECT COUNT(*) FROM vector_graph_edges") or 0,
            "vector_optimization_runs": await conn.fetchval("SELECT COUNT(*) FROM vector_optimization_runs") or 0,
            "research_reports": await _fetch_meta("research_reports", date_col="publish_date"),
            "stock_fund_flow": await _fetch_meta("stock_fund_flow"),
        }
