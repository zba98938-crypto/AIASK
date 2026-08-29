"""Bulk stock-strategy matrix planning for P0 factory expansion."""


from __future__ import annotations

from ...domain import constants as _matrix_const

import logging
import math
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List

from ...domain.constants import (
    STOCK_FIRST_ROUTER_TELEMETRY_ENABLED,
    STOCK_STRATEGY_MATRIX_BATCH_SIZE,
    STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK,
    STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK,
    STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD,
    STRATEGY_FACTORY_VECTOR_REUSE_MIN_SAMPLES,
    STRATEGY_FACTORY_VECTOR_REUSE_MIN_SIMILARITY,
    STRATEGY_FACTORY_VECTOR_REUSE_TOPN,
    preferred_strategy_types_for_factor,
)
from .._opportunity_utils import _MarketOpportunityScannerUtilityMixin
from .._stock_universe_loader import filter_stock_universe_rows_by_codes, load_stock_universe_rows
from ..factory_market_views import build_full_market_topn_payload
from ..factory_execution import resolve_runtime_mode_flags
from .._matrix_vector_reuse import VectorReuseService
from .._runtime_toggles import stock_direction_gate_enabled
from ..research_plane_contract import build_task_artifact
from ..stock_strategy_router import StockRegimeProfile, route_strategies
from ..sector_taxonomy import (
    normalize_sector_labels,
    sector_profiles_for_label,
    sector_family_biases,
    sector_match_strength,
)

logger = logging.getLogger(__name__)


class _MatrixPolicyMixin:
    @classmethod
    async def _fetch_history_bar_counts(
        cls,
        db,
        codes: Sequence[str],
        *,
        min_history_bars: int,
    ) -> tuple[dict[str, int], bool]:
        normalized_codes = [
            str(code or "").strip()
            for code in list(codes or [])
            if str(code or "").strip()
        ]
        if not normalized_codes:
            return {}, False

        deduped_codes = list(dict.fromkeys(normalized_codes))
        acquire = getattr(db, "acquire", None)
        if callable(acquire):
            history_counts: dict[str, int] = {}
            try:
                async with db.acquire() as conn:
                    for start in range(0, len(deduped_codes), cls._HISTORY_COUNT_CHUNK_SIZE):
                        chunk = deduped_codes[start : start + cls._HISTORY_COUNT_CHUNK_SIZE]
                        rows = await conn.fetch(
                            """
                            SELECT code, COUNT(*) AS bar_count
                            FROM kline_1d
                            WHERE code IN ($1)
                            GROUP BY code
                            """,
                            chunk,
                        )
                        for row in rows or []:
                            payload = dict(row or {})
                            code = str(payload.get("code") or "").strip()
                            if not code:
                                continue
                            history_counts[code] = max(0, int(payload.get("bar_count") or 0))
                return history_counts, True
            except Exception as exc:
                logger.debug("stock_strategy_matrix: bulk history count query failed, falling back to per-code: %s", exc)

        get_klines = getattr(db, "get_klines", None)
        if not callable(get_klines) or len(deduped_codes) > 256:
            return {}, False

        history_counts = {}
        query_limit = max(int(min_history_bars or 0), cls._MIN_HISTORY_BARS)
        for code in deduped_codes:
            try:
                klines = await get_klines(code, limit=query_limit)
            except Exception:
                history_counts[code] = 0
                continue
            history_counts[code] = len(list(klines or []))
        return history_counts, True

    @staticmethod
    def _resolve_task_cursor(
        *,
        planned_task_count: int,
        requested_task_offset: int,
        effective_task_budget: int,
    ) -> tuple[int, int, bool, bool]:
        if planned_task_count <= 0:
            return 0, 0, False, False

        requested = max(0, int(requested_task_offset or 0))
        effective_offset = requested
        task_offset_fallback = False
        if effective_offset >= planned_task_count:
            effective_offset = effective_offset % planned_task_count
            task_offset_fallback = requested > 0

        actual_budget = max(1, min(int(effective_task_budget or 1), planned_task_count))
        next_task_offset = (effective_offset + actual_budget) % planned_task_count
        cursor_wrapped = bool(task_offset_fallback or effective_offset + actual_budget >= planned_task_count)
        return effective_offset, next_task_offset, cursor_wrapped, task_offset_fallback

    @staticmethod
    def _task_target_code(task: dict[str, Any]) -> str:
        return str(((task or {}).get("target_symbols") or [None])[0] or "").strip()

    @classmethod
    def _pop_family_interleaved_task(
        cls,
        bucket: list[dict[str, Any]],
        *,
        used_codes: set[str],
    ) -> dict[str, Any] | None:
        if not bucket:
            return None
        scan_limit = min(len(bucket), max(6, len(used_codes) + 2))
        for index in range(scan_limit):
            code = cls._task_target_code(bucket[index] or {})
            if not code or code not in used_codes:
                return bucket.pop(index)
        return bucket.pop(0)

    @classmethod
    def _interleave_tasks_by_family(
        cls,
        tasks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if len(tasks) <= 1:
            return [dict(item or {}) for item in tasks]

        family_buckets: dict[str, list[dict[str, Any]]] = {}
        for item in tasks:
            task = dict(item or {})
            family = str(task.get("candidate_family") or "").strip().lower() or "unknown"
            family_buckets.setdefault(family, []).append(task)

        family_order = sorted(
            family_buckets.keys(),
            key=lambda family: (
                int((family_buckets[family][0] or {}).get("matrix_family_rank") or 0),
                int((family_buckets[family][0] or {}).get("matrix_stock_rank") or 0),
                family,
            ),
        )

        interleaved: list[dict[str, Any]] = []
        remaining = sum(len(bucket) for bucket in family_buckets.values())
        while remaining > 0:
            used_codes: set[str] = set()
            wave_progress = False
            for family in family_order:
                bucket = family_buckets.get(family) or []
                task = cls._pop_family_interleaved_task(bucket, used_codes=used_codes)
                if task is None:
                    continue
                interleaved.append(task)
                remaining -= 1
                wave_progress = True
                code = cls._task_target_code(task)
                if code:
                    used_codes.add(code)
            if not wave_progress:
                break

        if remaining > 0:
            for family in family_order:
                bucket = family_buckets.get(family) or []
                while bucket:
                    interleaved.append(bucket.pop(0))
        return interleaved

    @classmethod
    def _build_task(
        cls,
        row: dict[str, Any],
        *,
        family: str,
        rank: int,
        stock_rank: int,
        priority_score: float,
        snapshot: dict[str, Any],
        generation_limit: int,
        family_plan: dict[str, Any] | None = None,
        allocation_item: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or code).strip() or code
        holding_bucket = cls._holding_bucket_for_family(family)
        # PR-S19：从 row 中提取画像摘要供 holding_window / risk_level / param_band 修正
        profile_summary = cls._extract_profile_summary(row)
        alpha_source = cls._alpha_source_with_profile(family, profile_summary)
        risk_level = cls._risk_level_with_profile(family, profile_summary)
        resolved_family_plan = dict(family_plan or {})
        resolved_rank = max(1, int(resolved_family_plan.get("family_rank") or rank))
        validation_profile = cls._normalize_validation_profile(
            family,
            dict(resolved_family_plan.get("validation_profile") or {}),
        )
        budget_weight = max(
            0.0,
            min(
                cls._safe_float(resolved_family_plan.get("budget_weight") or resolved_family_plan.get("budget")),
                1.0,
            ),
        )
        failure_penalty = max(
            0.0,
            min(
                cls._safe_float(resolved_family_plan.get("failure_penalty"))
                or cls._default_failure_penalty_for_family(family, family_rank=resolved_rank),
                1.0,
            ),
        )
        priority = max(45, min(98, int(round(priority_score - (resolved_rank - 1) * 1.5))))
        # PR-S19：构造 profile_param_band 与 param_search_space
        profile_param_band = cls._param_band_for_profile(family, profile_summary)
        # 先把当前 task 已有的"family 默认 holding_window"作为最小默认空间，
        # 之后下游 spawner 可在此基础上做更细的合并。
        family_default_space = {
            "holding_window": dict(cls._holding_window_for_family(family)),
        }
        param_search_space = cls._merge_param_search_space(family_default_space, profile_param_band)

        task = {
            "task_id": f"bulk_matrix_{snapshot.get('date')}_{code}_{family}",
            "task_key": f"bulk_matrix:{snapshot.get('date')}:{code}:{family}",
            "task_source": "bulk_stock_matrix",
            "theme": f"stock_strategy_matrix_{family}",
            "title": f"逐股策略矩阵·{name}·{family}",
            "opportunity_type": "stock_strategy_matrix",
            "rationale": f"为 {name}({code}) 生成 {family} 家族候选，优先验证单股可执行策略。",
            "preferred_strategy_types": [family],
            "allowed_strategy_types": [family],
            "strategy_preferences": [family],
            "candidate_family": family,
            "candidate_family_id": f"{code}_{family}_{holding_bucket}",
            "holding_period_bucket": holding_bucket,
            "alpha_source": alpha_source,
            "risk_level": risk_level,
            "regime_fit": "trend_expansion" if family in {"momentum", "growth_factor"} else ("mean_reversion" if family in {"rsi", "value_factor"} else "rotation_balanced"),
            "direction_bias": "long_only",
            "generator_mode": "bulk_stock_matrix",
            "target_symbol_policy": "strict_intersection",
            "universe_expansion_policy": "forbid",
            "preference_strength": "hard",
            "preference_reason": f"stock_matrix:{code}:{family}",
            "validation_focus": str(validation_profile.get("validation_focus") or "candidate_target_only"),
            "validation_profile": validation_profile,
            # PR-S19：holding_window 受 profile 修正
            "holding_window": cls._holding_window_with_profile(family, profile_summary),
            "target_symbols": [code],
            "stock_pool": {"selection_mode": "explicit", "symbols": [code]},
            "focus_industries": [str(row.get("industry") or row.get("sector") or "").strip()] if str(row.get("industry") or row.get("sector") or "").strip() else [],
            "priority": priority,
            "generation_limit": generation_limit,
            "matrix_rank": resolved_rank,
            "matrix_stock_rank": stock_rank,
            "matrix_family_rank": resolved_rank,
            "matrix_priority_score": priority_score,
            "stock_family_budget": budget_weight,
            "stock_family_budget_weight": budget_weight,
            "stock_family_failure_penalty": failure_penalty,
            "source_symbol_summary": cls._summarize_symbol(row),
            "source_snapshot": {
                "fear_greed_index": cls._safe_float(snapshot.get("fear_greed_index") or 50.0),
                "fg_level": snapshot.get("fg_level"),
            },
        }

        # PR-S19：把画像摘要 + 参数带挂到 task，让下游 spawner / rule 生成器能消费
        if profile_summary:
            task["stock_profile_summary"] = {
                "primary_archetype": profile_summary.get("primary_archetype"),
                "secondary_archetypes": list(profile_summary.get("secondary_archetypes") or []),
                "candidate_factor_families": list(profile_summary.get("candidate_factor_families") or []),
                "factor_dimension_scores": dict(profile_summary.get("factor_dimension_scores") or {}),
                "recommended_families": list(profile_summary.get("recommended_families") or []),
                "profile_quality": profile_summary.get("profile_quality"),
                "feature_coverage": dict(profile_summary.get("feature_coverage") or {}),
            }
        router_status = dict(row.get("_stock_first_router") or {})
        if router_status:
            task["stock_first_router"] = router_status
        direction_gate_status = dict(row.get("_stock_direction_gate") or {})
        if direction_gate_status:
            task["stock_direction_gate"] = direction_gate_status
        if profile_param_band:
            task["profile_param_band"] = profile_param_band
        if param_search_space:
            task["param_search_space"] = param_search_space

        if allocation_item:
            task["stock_family_priority"] = max(0.0, min(cls._safe_float(allocation_item.get("priority")), 1.0))
            task["stock_family_allocation_source"] = allocation_item.get("source_mode") or "factor_research_stock_family_allocation"

        # PR-S22：vector reuse 命中信息（无命中或未启用时不写）
        reuse_hit = dict(resolved_family_plan.get("vector_reuse_hit") or {})
        if reuse_hit:
            task["vector_reuse_hit"] = {
                "strategy_id": reuse_hit.get("strategy_id"),
                "params": dict(reuse_hit.get("params") or {}),
                "similarity": float(reuse_hit.get("similarity") or 0.0),
                "source_code": reuse_hit.get("source_code"),
                "source": reuse_hit.get("source") or "listed_strategy",
            }
            # 如果 candidate.params 还为空，下游 _ensure_strategy_params 会去 task.param_search_space
            # 取 preferred；这里同时把 reused params 合到 param_search_space 作为 preferred 锚点。
            reused_params = dict(reuse_hit.get("params") or {})
            if reused_params:
                ps = dict(task.get("param_search_space") or {})
                for key, value in reused_params.items():
                    spec = dict(ps.get(key) or {})
                    spec["preferred"] = value
                    spec.setdefault("min", value)
                    spec.setdefault("max", value)
                    ps[key] = spec
                task["param_search_space"] = ps

        return cls._finalize_task(task)

    async def plan(self, db, snapshot: dict[str, Any]) -> dict[str, Any]:
        matrix_enabled = self._effective_stock_matrix_enabled(snapshot)
        router_enabled = self._effective_router_enabled(snapshot)
        router_strict = self._effective_router_strict(snapshot)
        if not matrix_enabled:
            task_artifact = build_task_artifact()
            self.last_report = {
                "summary": {
                    "enabled": False,
                    "task_count": 0,
                    "stock_count": 0,
                    "eligible_stock_count": 0,
                    "loaded_stock_count": 0,
                    "pages_loaded": 0,
                    "analysis_complete": False,
                    "analysis_stock_coverage_ratio": 0.0,
                    "family_counts": {},
                    "planned_family_counts": {},
                    "universe_limit": _matrix_const.STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
                    "requested_universe_offset": 0,
                    "effective_universe_offset": 0,
                    "universe_offset_fallback": False,
                    "next_universe_offset": 0,
                    "cursor_wrapped": False,
                    "cursor_mode": "task_offset",
                    "requested_task_offset": 0,
                    "effective_task_offset": 0,
                    "task_offset_fallback": False,
                    "next_task_offset": 0,
                    "task_cursor_wrapped": False,
                    "max_tasks_per_run": _matrix_const.STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN,
                    "max_candidates_per_run": _matrix_const.STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN,
                    "generation_limit_per_task": STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK,
                    "effective_task_budget": 0,
                    "estimated_candidate_count": 0,
                    "planned_task_count": 0,
                    "planned_candidate_count": 0,
                    "batch_size": STOCK_STRATEGY_MATRIX_BATCH_SIZE,
                    "batch_count": 0,
                    "selected_batch_count": 0,
                    "batch_task_counts": {},
                    "tasks_per_shard": STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD,
                    "shard_count": 0,
                    "selected_shard_count": 0,
                    "selected_shard_ids": [],
                    "stock_coverage_ratio": 0.0,
                    "allocation_mode": "stock_round_robin_by_family_rank",
                    "allocation_pass_counts": {},
                    "planned_allocation_pass_counts": {},
                    "overflow_task_count": 0,
                    "stock_family_allocation_count": 0,
                    "stock_family_allocation_applied_count": 0,
                    "stock_family_allocation_coverage_ratio": 0.0,
                    "min_history_bars": self._MIN_HISTORY_BARS,
                    "history_prefilter_applied": False,
                    "insufficient_history_filtered_count": 0,
                    "task_artifact_contract_version": task_artifact.get("contract_version"),
                    "task_artifact_available": bool(task_artifact.get("available")),
                },
                "tasks": [],
                "task_artifact": task_artifact,
            }
            return self.last_report

        universe_page_size = max(100, min(int(_matrix_const.STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT), 1000))
        try:
            rows, universe_meta = await load_stock_universe_rows(
                db,
                limit=_matrix_const.STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
                page_size=universe_page_size,
                start_offset=0,
            )
        except Exception as exc:
            # 顶层异常仍然兜底，但把错误信息留在 meta 中供 summary 暴露
            rows, universe_meta = [], {
                "pages_loaded": 0,
                "loaded_count": 0,
                "complete": False,
                "truncated": False,
                "page_size": universe_page_size,
                "last_error": str(exc),
                "last_error_type": type(exc).__name__,
                "last_error_offset": 0,
            }

        target_codes = self._normalize_codes(
            snapshot.get("candidate_codes")
            or snapshot.get("target_codes")
            or snapshot.get("requested_target_codes"),
            limit=64,
        )
        rows, target_filter_meta = filter_stock_universe_rows_by_codes(rows, target_codes)
        universe_meta = {**dict(universe_meta or {}), **target_filter_meta}

        hot_sectors = set(self._normalize_sector_labels(snapshot.get("hot_sectors") or [], limit=12))
        cold_sectors = set(self._normalize_sector_labels(snapshot.get("cold_sectors") or [], limit=12))
        active_factors = self._normalize_factor_names(snapshot)
        stock_family_allocation = self._normalize_stock_family_allocation(snapshot)
        candidate_rows = [row for row in rows if str(row.get("code") or "").strip()]

        # PR-S17: 批量加载 stock_profile_embeddings 画像并挂到 row
        # P2 第一步：先让 row 上能拿到 profile，summary 暴露覆盖率；具体消费留到 P2 后续。
        profile_load_error: str | None = None
        profile_load_error_type: str | None = None
        profile_loaded_count = 0
        # PR-S19/PR-S20：画像质量、原型分布与 verified strategy index 观测
        profile_quality_distribution: dict[str, int] = {}
        profile_archetype_distribution: dict[str, int] = {}
        verified_strategy_index_count = 0
        try:
            list_profiles = getattr(db, "list_vector_profiles", None)
            if callable(list_profiles) and candidate_rows:
                all_codes = [str(row.get("code") or "").strip() for row in candidate_rows]
                all_codes = [c for c in all_codes if c]
                profile_by_code: dict[str, dict] = {}
                # PR-S22：分批加载，确保 5000+ 池也能完整命中画像，而不是被 1500 截断。
                batch_size = 1500
                for batch_start in range(0, len(all_codes), batch_size):
                    batch = all_codes[batch_start : batch_start + batch_size]
                    if not batch:
                        continue
                    profiles = await list_profiles(
                        collection_name="stock_profile_embeddings",
                        stock_codes=batch,
                        limit=max(len(batch), 100),
                    )
                    for item in list(profiles or []):
                        if not isinstance(item, dict):
                            continue
                        code = str(item.get("stock_code") or item.get("code") or "").strip()
                        if code and code not in profile_by_code:
                            profile_by_code[code] = item
                for row in candidate_rows:
                    code = str(row.get("code") or "").strip()
                    if code in profile_by_code:
                        row["stock_profile"] = profile_by_code[code]
                profile_loaded_count = len(profile_by_code)
                # PR-S19：聚合 profile_quality / archetype 分布
                for prof in profile_by_code.values():
                    metadata = prof.get("metadata") or {}
                    if isinstance(metadata, str):
                        try:
                            import json as _json
                            metadata = _json.loads(metadata or "{}")
                        except Exception:
                            metadata = {}
                    summary = (metadata or {}).get("profile_summary") or {}
                    q = str(summary.get("profile_quality") or "unknown")
                    a = str(summary.get("primary_archetype") or "unknown")
                    profile_quality_distribution[q] = profile_quality_distribution.get(q, 0) + 1
                    profile_archetype_distribution[a] = profile_archetype_distribution.get(a, 0) + 1
        except Exception as exc:
            profile_load_error = str(exc)
            profile_load_error_type = type(exc).__name__

        lightweight_profile_generated_count = 0
        if router_enabled:
            for row in candidate_rows:
                if self._ensure_lightweight_profile_summary(row):
                    lightweight_profile_generated_count += 1

        # PR-S20/PR-S22: verified strategy index 观测 + 真复用
        # 当 _matrix_const.STRATEGY_FACTORY_VECTOR_REUSE_ENABLED=1 且索引样本充足时，会真正给 row 注入复用提示。
        verified_strategy_index_count = 0
        vector_reuse_service: VectorReuseService | None = None
        try:
            vector_reuse_service = await VectorReuseService.build_from_db(
                db,
                min_samples=STRATEGY_FACTORY_VECTOR_REUSE_MIN_SAMPLES,
                min_similarity=STRATEGY_FACTORY_VECTOR_REUSE_MIN_SIMILARITY,
                topn=STRATEGY_FACTORY_VECTOR_REUSE_TOPN,
            )
            verified_strategy_index_count = vector_reuse_service.index_count
        except Exception as exc:
            logger.debug("VectorReuseService build failed: %s", exc)
            verified_strategy_index_count = 0

        reuse_enabled = bool(
            _matrix_const.STRATEGY_FACTORY_VECTOR_REUSE_ENABLED
            and _matrix_const.STRATEGY_FACTORY_VECTOR_SIMILAR_PROFILE_ENABLED
            and vector_reuse_service is not None
            and vector_reuse_service.has_enough_samples()
        )
        min_history_bars = self._MIN_HISTORY_BARS
        history_counts, history_prefilter_applied = await self._fetch_history_bar_counts(
            db,
            [str(row.get("code") or "").strip() for row in candidate_rows],
            min_history_bars=min_history_bars,
        )
        filtered_rows = candidate_rows
        insufficient_history_filtered_count = 0
        if history_prefilter_applied:
            filtered_rows = [
                row
                for row in candidate_rows
                if int(history_counts.get(str(row.get("code") or "").strip()) or 0) >= min_history_bars
            ]
            insufficient_history_filtered_count = max(0, len(candidate_rows) - len(filtered_rows))

        family_preference_order = self._base_family_order(snapshot)
        family_preference_source = self._family_preference_source(snapshot)
        scoring_context = self._build_priority_scoring_context(
            filtered_rows,
            snapshot=snapshot,
            hot_sectors=hot_sectors,
            cold_sectors=cold_sectors,
            active_factors=active_factors,
            stock_family_allocation=stock_family_allocation,
        )
        ranked_entries: list[dict[str, Any]] = []
        for row in filtered_rows:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            allocation_item = dict(stock_family_allocation.get(code) or {})
            component_scores = self._row_priority_components(
                row,
                snapshot=snapshot,
                hot_sectors=hot_sectors,
                cold_sectors=cold_sectors,
                active_factors=active_factors,
                allocation_item=allocation_item,
                scoring_context=scoring_context,
            )
            row_score = round(sum(self._safe_float(value) for value in component_scores.values()), 4)
            family_plans = self._family_plans_for_row(
                row,
                snapshot=snapshot,
                hot_sectors=hot_sectors,
                cold_sectors=cold_sectors,
                active_factors=active_factors,
                allocation_item=allocation_item,
            )
            family_candidates = [
                str(plan.get("family") or "").strip().lower()
                for plan in list(family_plans or [])
                if str(plan.get("family") or "").strip()
            ]
            ranked_entries.append(
                {
                    "row": row,
                    "code": code,
                    "allocation_item": allocation_item,
                    "component_scores": component_scores,
                    "row_score": row_score,
                    "family_plans": family_plans,
                    "family_candidates": family_candidates,
                }
            )
        ranked_entries.sort(
            key=lambda entry: (
                -self._safe_float(entry.get("row_score")),
                -self._safe_float(dict(entry.get("component_scores") or {}).get("valuation_score")),
                -self._safe_float(dict(entry.get("component_scores") or {}).get("factor_alignment_score")),
                -self._safe_float(dict(entry.get("component_scores") or {}).get("allocation_score")),
                -self._safe_float(dict(entry.get("component_scores") or {}).get("size_score")),
                str(entry.get("code") or ""),
            )
        )

        effective_generation_limit = self._effective_generation_limit()
        effective_task_budget = self._effective_task_budget()
        row_plans: list[dict[str, Any]] = []
        full_market_score_rows: list[dict[str, Any]] = []
        max_family_depth = 0
        allocation_applied_count = 0
        # PR-S22：vector reuse 只对优先级最高的若干股票触发，避免全市场遍历搜索拖垮性能
        reuse_top_n = max(int(effective_task_budget) * 2, 60) if reuse_enabled else 0
        for stock_rank, entry in enumerate(ranked_entries, 1):
            row = dict(entry.get("row") or {})
            code = str(entry.get("code") or "").strip()
            allocation_item = dict(entry.get("allocation_item") or {})
            component_scores = dict(entry.get("component_scores") or {})
            row_score = round(self._safe_float(entry.get("row_score")), 4)
            family_plans = [dict(plan or {}) for plan in list(entry.get("family_plans") or [])]
            family_candidates = [
                str(item or "").strip().lower()
                for item in list(entry.get("family_candidates") or [])
                if str(item or "").strip()
            ]
            full_market_score_rows.append(
                {
                    "code": code,
                    "name": str(row.get("name") or code).strip() or code,
                    "industry": str(row.get("industry") or row.get("sector") or "").strip() or None,
                    "market_cap": self._safe_float(row.get("market_cap")),
                    "composite_score": row_score,
                    "component_scores": component_scores,
                    "family_candidates": family_candidates,
                    "eligible": True,
                    "rank": stock_rank,
                }
            )
            row_tasks: list[dict[str, Any]] = []
            for family_plan in family_plans:
                family = str(family_plan.get("family") or "").strip().lower()
                if not family:
                    continue
                # PR-S22：尝试 vector reuse 命中。只对优先级 top reuse_top_n 触发，节约成本。
                if reuse_enabled and vector_reuse_service is not None and stock_rank <= reuse_top_n:
                    try:
                        reuse_hit = await vector_reuse_service.match(row, family)
                    except Exception as exc:
                        logger.debug("VectorReuseService.match failed: %s", exc)
                        reuse_hit = None
                    if reuse_hit:
                        family_plan = dict(family_plan)
                        family_plan["vector_reuse_hit"] = reuse_hit
                row_tasks.append(
                    self._build_task(
                        row,
                        family=family,
                        rank=max(1, self._safe_int(family_plan.get("family_rank")) or len(row_tasks) + 1),
                        stock_rank=stock_rank,
                        priority_score=row_score,
                        snapshot=snapshot,
                        generation_limit=effective_generation_limit,
                        family_plan=family_plan,
                        allocation_item=allocation_item,
                    )
                )
            if not row_tasks:
                continue
            if allocation_item:
                allocation_applied_count += 1
            max_family_depth = max(max_family_depth, len(row_tasks))
            row_plans.append(
                {
                    "code": code,
                    "stock_rank": stock_rank,
                    "priority_score": row_score,
                    "tasks": row_tasks,
                }
            )

        batch_size = max(1, int(STOCK_STRATEGY_MATRIX_BATCH_SIZE))
        row_plan_batches: list[list[dict[str, Any]]] = [
            row_plans[start : start + batch_size]
            for start in range(0, len(row_plans), batch_size)
        ]
        for batch_id, batch_rows in enumerate(row_plan_batches, 1):
            batch_stock_count = len(batch_rows)
            for batch_stock_index, row_plan in enumerate(batch_rows, 1):
                row_plan["matrix_batch_id"] = batch_id
                row_plan["matrix_batch_stock_index"] = batch_stock_index
                row_plan["matrix_batch_stock_count"] = batch_stock_count

        planned_tasks: list[dict[str, Any]] = []
        planned_family_counts: dict[str, int] = {}
        planned_codes: list[str] = []
        planned_allocation_pass_counts: dict[str, int] = {}
        for allocation_pass in range(max_family_depth):
            pass_key = str(allocation_pass + 1)
            pass_count = 0
            for batch_rows in row_plan_batches:
                for row_plan in batch_rows:
                    row_tasks = list(row_plan.get("tasks") or [])
                    if allocation_pass >= len(row_tasks):
                        continue
                    task = dict(row_tasks[allocation_pass] or {})
                    task["matrix_allocation_pass"] = allocation_pass + 1
                    task["matrix_batch_id"] = int(row_plan.get("matrix_batch_id") or 1)
                    task["matrix_batch_stock_index"] = int(row_plan.get("matrix_batch_stock_index") or 1)
                    task["matrix_batch_stock_count"] = int(row_plan.get("matrix_batch_stock_count") or len(batch_rows) or 1)
                    planned_tasks.append(task)
                    family = str(task.get("candidate_family") or "").strip().lower()
                    if family:
                        planned_family_counts[family] = planned_family_counts.get(family, 0) + 1
                    code = str(row_plan.get("code") or "").strip()
                    if code and code not in planned_codes:
                        planned_codes.append(code)
                    pass_count += 1
            if pass_count > 0:
                planned_allocation_pass_counts[pass_key] = pass_count

        planned_tasks = self._interleave_tasks_by_family(planned_tasks)
        planned_tasks, family_task_caps = self._apply_family_pressure_caps(
            planned_tasks,
            effective_task_budget=effective_task_budget,
        )
        planned_family_counts = {}
        for task in planned_tasks:
            family = str(task.get("candidate_family") or "").strip().lower()
            if family:
                planned_family_counts[family] = planned_family_counts.get(family, 0) + 1
        for plan_slot, task in enumerate(planned_tasks, 1):
            task["matrix_plan_slot"] = plan_slot

        tasks_per_shard = max(1, int(STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD))
        shard_count = int(math.ceil(len(planned_tasks) / tasks_per_shard)) if planned_tasks else 0
        planned_batch_task_counts: dict[str, int] = {}
        for task in planned_tasks:
            batch_key = str(int(task.get("matrix_batch_id") or 1))
            planned_batch_task_counts[batch_key] = planned_batch_task_counts.get(batch_key, 0) + 1
        planned_batch_task_indexes: dict[str, int] = {}
        for index, task in enumerate(planned_tasks, 1):
            task["matrix_shard_id"] = int(math.ceil(index / tasks_per_shard))
            task["matrix_shard_task_index"] = ((index - 1) % tasks_per_shard) + 1
            task["matrix_shard_count"] = shard_count
            batch_key = str(int(task.get("matrix_batch_id") or 1))
            planned_batch_task_indexes[batch_key] = planned_batch_task_indexes.get(batch_key, 0) + 1
            task["matrix_batch_count"] = len(row_plan_batches)
            task["matrix_batch_task_index"] = planned_batch_task_indexes[batch_key]
            task["matrix_batch_task_count"] = int(planned_batch_task_counts.get(batch_key) or 0)

        planned_task_count = len(planned_tasks)
        requested_task_offset = max(
            0,
            int(
                snapshot.get("bulk_stock_matrix_task_offset")
                or snapshot.get("bulk_stock_matrix_universe_offset")
                or 0
            ),
        )
        effective_task_offset, next_task_offset, task_cursor_wrapped, task_offset_fallback = self._resolve_task_cursor(
            planned_task_count=planned_task_count,
            requested_task_offset=requested_task_offset,
            effective_task_budget=effective_task_budget,
        )

        tasks: list[dict[str, Any]] = []
        if planned_task_count > 0:
            actual_budget = max(1, min(effective_task_budget, planned_task_count))
            window_end = effective_task_offset + actual_budget
            if window_end <= planned_task_count:
                tasks = [dict(item or {}) for item in planned_tasks[effective_task_offset:window_end]]
            else:
                tasks = [
                    *[dict(item or {}) for item in planned_tasks[effective_task_offset:]],
                    *[dict(item or {}) for item in planned_tasks[: window_end % planned_task_count]],
                ]

        family_counts: dict[str, int] = {}
        selected_codes: list[str] = []
        allocation_pass_counts: dict[str, int] = {}
        batch_task_counts: dict[str, int] = {}
        batch_task_indexes: dict[str, int] = {}
        for index, task in enumerate(tasks, 1):
            task["matrix_budget_slot"] = index
            family = str(task.get("candidate_family") or "").strip().lower()
            if family:
                family_counts[family] = family_counts.get(family, 0) + 1
            code = str((task.get("target_symbols") or [None])[0] or "").strip()
            if code and code not in selected_codes:
                selected_codes.append(code)
            pass_key = str(int(task.get("matrix_allocation_pass") or 0))
            if pass_key and pass_key != "0":
                allocation_pass_counts[pass_key] = allocation_pass_counts.get(pass_key, 0) + 1
            batch_key = str(int(task.get("matrix_batch_id") or 1))
            batch_task_counts[batch_key] = batch_task_counts.get(batch_key, 0) + 1
        for task in tasks:
            batch_key = str(int(task.get("matrix_batch_id") or 1))
            batch_task_indexes[batch_key] = batch_task_indexes.get(batch_key, 0) + 1
            task["matrix_batch_task_index"] = batch_task_indexes[batch_key]
            task["matrix_batch_task_count"] = int(batch_task_counts.get(batch_key) or 0)

        selected_shard_ids = sorted(
            {
                int(task.get("matrix_shard_id") or 0)
                for task in tasks
                if int(task.get("matrix_shard_id") or 0) > 0
            }
        )

        eligible_stock_count = len(row_plans)
        stock_coverage_ratio = round(len(selected_codes) / eligible_stock_count, 4) if eligible_stock_count else 0.0
        analysis_stock_coverage_ratio = round(eligible_stock_count / len(rows), 4) if rows else 0.0
        overflow_task_count = max(planned_task_count - len(tasks), 0)
        stock_family_allocation_coverage_ratio = (
            round(allocation_applied_count / eligible_stock_count, 4)
            if eligible_stock_count
            else 0.0
        )
        full_market_topn = build_full_market_topn_payload(
            as_of_date=str(snapshot.get("date") or snapshot.get("snapshot_date") or "").strip() or None,
            universe_count=len(rows),
            eligible_count=eligible_stock_count,
            score_rows=full_market_score_rows,
            score_contract_version=str(scoring_context.get("score_contract_version") or ""),
            active_factors=list(scoring_context.get("active_factors") or []),
            hot_sectors=sorted(set(scoring_context.get("hot_sectors") or set())),
            cold_sectors=sorted(set(scoring_context.get("cold_sectors") or set())),
            stock_family_allocation_source_mode=scoring_context.get("allocation_source_mode"),
            stock_family_allocation_avg_priority=scoring_context.get("allocation_avg_priority"),
            selection_method="deterministic_bulk_priority_v2",
        )

        universe_load_error = universe_meta.get("last_error")
        universe_load_error_type = universe_meta.get("last_error_type")
        universe_load_error_offset = universe_meta.get("last_error_offset")
        skip_reason: str | None = None
        if not rows and universe_load_error:
            skip_reason = "universe_load_failed"
        report = {
            "summary": {
                "enabled": True,
                "task_count": len(tasks),
                "stock_count": len(selected_codes),
                "eligible_stock_count": eligible_stock_count,
                "loaded_stock_count": len(rows),
                "pages_loaded": int(universe_meta.get("pages_loaded") or 0),
                "target_code_filter_applied": bool(universe_meta.get("target_code_filter_applied")),
                "requested_target_codes": list(universe_meta.get("requested_target_codes") or []),
                "target_missing_codes": list(universe_meta.get("target_missing_codes") or []),
                "load_error": universe_load_error,
                "load_error_type": universe_load_error_type,
                "last_error": universe_load_error,
                "last_error_type": universe_load_error_type,
                "last_error_offset": universe_load_error_offset,
                "skip_reason": skip_reason,
                # PR-S17: stock_profile_embeddings 加载可观测
                "profile_loaded_count": profile_loaded_count,
                "profile_missing_count": max(0, len(candidate_rows) - profile_loaded_count),
                "profile_summary_generated_count": lightweight_profile_generated_count,
                "profile_load_error": profile_load_error,
                "profile_load_error_type": profile_load_error_type,
                # PR-S19: 画像质量与原型分布
                "profile_quality_distribution": profile_quality_distribution,
                "profile_archetype_distribution": profile_archetype_distribution,
                # PR-S20/PR-S22: 向量复用 / 相似画像观测指标
                # 当 _matrix_const.STRATEGY_FACTORY_VECTOR_REUSE_ENABLED=0 或样本不足时，
                # 这些值多数为 0/lookup_count 反映"观测层"行为；启用后是真实命中。
                "similar_profile_lookup_count": int(
                    vector_reuse_service.lookup_count if vector_reuse_service else profile_loaded_count
                ),
                "similar_profile_hit_count": int(vector_reuse_service.hit_count if vector_reuse_service else 0),
                "verified_strategy_index_count": int(verified_strategy_index_count),
                "vector_reuse_eligible_count": int(
                    vector_reuse_service.eligible_count if vector_reuse_service else 0
                ),
                "vector_reuse_count": int(vector_reuse_service.reuse_count if vector_reuse_service else 0),
                "vector_reuse_avg_similarity": float(
                    vector_reuse_service.avg_similarity if vector_reuse_service else 0.0
                ),
                "vector_reuse_enabled": bool(reuse_enabled),
                "analysis_complete": bool(universe_meta.get("complete")) and not bool(universe_meta.get("truncated")),
                "analysis_stock_coverage_ratio": analysis_stock_coverage_ratio,
                "family_counts": family_counts,
                "planned_family_counts": planned_family_counts,
                "universe_limit": _matrix_const.STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
                "requested_universe_offset": requested_task_offset,
                "effective_universe_offset": effective_task_offset,
                "universe_offset_fallback": task_offset_fallback,
                "next_universe_offset": next_task_offset,
                "cursor_wrapped": task_cursor_wrapped,
                "cursor_mode": "task_offset",
                "requested_task_offset": requested_task_offset,
                "effective_task_offset": effective_task_offset,
                "task_offset_fallback": task_offset_fallback,
                "next_task_offset": next_task_offset,
                "task_cursor_wrapped": task_cursor_wrapped,
                "max_tasks_per_run": _matrix_const.STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN,
                "max_candidates_per_run": _matrix_const.STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN,
                "families_per_stock": STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK,
                "generation_limit_per_task": effective_generation_limit,
                "effective_task_budget": effective_task_budget,
                "estimated_candidate_count": len(tasks) * effective_generation_limit,
                "planned_task_count": planned_task_count,
                "planned_candidate_count": planned_task_count * effective_generation_limit,
                "batch_size": batch_size,
                "batch_count": len(row_plan_batches),
                "selected_batch_count": len(batch_task_counts),
                "batch_task_counts": batch_task_counts,
                "tasks_per_shard": tasks_per_shard,
                "shard_count": shard_count,
                "selected_shard_count": len(selected_shard_ids),
                "selected_shard_ids": selected_shard_ids,
                "stock_coverage_ratio": stock_coverage_ratio,
                "family_preference_order": family_preference_order,
                "family_preference_source": family_preference_source,
                "family_task_caps": family_task_caps,
                "allocation_mode": (
                    "factor_research_stock_family_allocation"
                    if allocation_applied_count > 0
                    else "stock_round_robin_by_family_rank"
                ),
                "allocation_pass_counts": allocation_pass_counts,
                "planned_allocation_pass_counts": planned_allocation_pass_counts,
                "overflow_task_count": overflow_task_count,
                "stock_family_allocation_count": len(stock_family_allocation),
                "stock_family_allocation_applied_count": allocation_applied_count,
                "stock_family_allocation_coverage_ratio": stock_family_allocation_coverage_ratio,
                "min_history_bars": min_history_bars,
                "history_prefilter_applied": history_prefilter_applied,
                "insufficient_history_filtered_count": insufficient_history_filtered_count,
                "full_market_topn_contract_version": full_market_topn.get("contract_version"),
                "full_market_topn_available": bool(full_market_topn.get("available")),
                "full_market_topn_universe_count": int(full_market_topn.get("universe_count") or 0),
                "full_market_topn_eligible_count": int(full_market_topn.get("eligible_count") or 0),
                "full_market_topn_score_row_count": int(full_market_topn.get("score_row_count") or 0),
                "full_market_topn_n": int(full_market_topn.get("topn_n") or 0),
                "full_market_topn_average_score": full_market_topn.get("average_topn_score"),
                "full_market_topn_constituents_preview": [
                    {
                        "code": item.get("code"),
                        "name": item.get("name"),
                        "industry": item.get("industry"),
                        "composite_score": item.get("composite_score"),
                    }
                    for item in list(full_market_topn.get("constituents") or [])[:5]
                ],
            },
            "tasks": tasks,
            "full_market_topn": full_market_topn,
            "full_market_score_rows": full_market_score_rows,
        }
        router_telemetry = self._router_telemetry_for_rows(filtered_rows, selected_tasks=tasks)
        direction_gate_telemetry = self._direction_gate_telemetry_for_rows(filtered_rows, selected_tasks=tasks)
        report["router_artifact"] = {
            "contract_version": "strategy_factory.router_artifact.v1",
            "available": bool(STOCK_FIRST_ROUTER_TELEMETRY_ENABLED),
            **router_telemetry,
        }
        report["direction_gate_artifact"] = {
            "contract_version": "strategy_factory.direction_gate_artifact.v1",
            "available": True,
            **direction_gate_telemetry,
        }
        report["summary"].update(router_telemetry)
        report["summary"].update(direction_gate_telemetry)
        task_artifact = build_task_artifact(
            {
                "task_scan": report,
                "task_source_counts": {"bulk_stock_matrix": len(tasks)},
                "event_task_count": 0,
                "snapshot_task_count": 0,
                "bulk_stock_task_count": len(tasks),
            }
        )
        report["task_artifact"] = task_artifact
        report["summary"] = {
            **dict(report.get("summary") or {}),
            "task_artifact_contract_version": task_artifact.get("contract_version"),
            "task_artifact_available": bool(task_artifact.get("available")),
        }
        self.last_report = report
        return self.last_report
