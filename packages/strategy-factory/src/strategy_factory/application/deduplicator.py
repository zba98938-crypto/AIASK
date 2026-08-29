"""策略工厂去重分析。"""


from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import numpy as np

from .candidate_contract import build_candidate_identity_signature, build_tested_object_hash
from .runtime import get_strategy_factory_package as _runtime_get_strategy_factory_package
from .utils import _extract_event_context as _local_extract_event_context
from ..domain.constants import DEDUP_CONCURRENCY
from ..domain.strategy_profile import infer_candidate_strategy_profile
from ..domain.targets import _build_task_signature, _extract_target_codes_from_payload, _normalize_research_task_contract

if TYPE_CHECKING:
    from ..api.contracts import VectorSearchGateway

def _compat_setting(name: str, default):
    return default


def _extract_event_context(*args, **kwargs):
    return _local_extract_event_context(*args, **kwargs)


def _get_strategy_factory_package():
    return _runtime_get_strategy_factory_package()

logger = logging.getLogger(__name__)

class Deduplicator:
    """去除与已有策略参数过于相似的候选。"""

    # 参数/向量相似度阈值。默认沿用历史值 (0.85 / 0.93)，行为零变化。
    # 通过 env 抬高阈值可放松去重（让边际去重命中的候选保留），用于过渡期
    # 解除"唯一存活候选被自重复折叠"的堵点 — 详见架构现状梳理文档 §去重墙。
    # 取值被 clamp 到 [0.50, 1.0]，1.0 等价于"几乎只折叠完全相同的策略"。
    def _resolve_dedup_threshold(_env_name: str, _default: float) -> float:
        try:
            raw = os.getenv(_env_name)
            if raw is None:
                return float(_default)
            return max(0.50, min(1.0, float(str(raw).strip())))
        except Exception:
            return float(_default)

    THRESHOLD = _resolve_dedup_threshold("STRATEGY_FACTORY_DEDUP_PARAM_THRESHOLD", 0.85)
    VECTOR_TRIGGER_THRESHOLD = 0.65
    VECTOR_THRESHOLD = _resolve_dedup_threshold("STRATEGY_FACTORY_DEDUP_VECTOR_THRESHOLD", 0.93)
    del _resolve_dedup_threshold
    MAX_VECTOR_CANDIDATES = 8
    MAX_REFRESH_PER_LINEAGE = 2
    MAX_REVISION_PER_LINEAGE = 3
    DEFAULT_BEHAVIOR_BUILD_TIMEOUT_SEC = 8.0
    DEFAULT_PREWARM_TIMEOUT_SEC = 15.0

    def __init__(self, *, vector_gateway: Optional["VectorSearchGateway"] = None):
        self.last_report: dict = {
            "summary": {
                "input_count": 0,
                "kept_count": 0,
                "dropped_count": 0,
                "vector_checks": 0,
                "existing_scan_count": 0,
                "coarse_candidate_count": 0,
                "coarse_filtered_count": 0,
                "coarse_hit_ratio": 0.0,
                "coarse_tag_hit_count": 0,
                "coarse_target_hit_count": 0,
                "vector_candidate_count": 0,
                "vector_candidate_trimmed_count": 0,
            },
            "kept": [],
            "dropped": [],
        }
        self._behavior_cache: Dict[str, Optional[List[dict]]] = {}
        self._vector_gateway = vector_gateway
        self._vector_engine = getattr(vector_gateway, "raw", vector_gateway) if vector_gateway is not None else None

    def _get_vector_gateway(self) -> "VectorSearchGateway":
        if self._vector_gateway is None:
            from ..infrastructure.runtime_adapters import VectorSearchGatewayImpl

            self._vector_gateway = VectorSearchGatewayImpl()
            self._vector_engine = getattr(self._vector_gateway, "raw", self._vector_gateway)
        return self._vector_gateway

    @staticmethod
    def _normalize_strategy_type(value: object) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _bucket_existing_by_type(cls, rows: List[dict]) -> Dict[str, List[dict]]:
        buckets: Dict[str, List[dict]] = {}
        for item in list(rows or []):
            strategy_type = cls._normalize_strategy_type((item or {}).get("strategy_type"))
            if not strategy_type:
                continue
            buckets.setdefault(strategy_type, []).append(item)
        return buckets

    @staticmethod
    def _tag_overlap(left_payload: Optional[dict], right_payload: Optional[dict]) -> float:
        left = {
            str(item).strip().lower()
            for item in list((left_payload or {}).get("tags") or [])
            if str(item).strip()
        }
        right = {
            str(item).strip().lower()
            for item in list((right_payload or {}).get("tags") or [])
            if str(item).strip()
        }
        if not left or not right:
            return 0.0
        union = left | right
        if not union:
            return 0.0
        return round(len(left & right) / len(union), 4)

    @staticmethod
    def _has_explicit_universe(payload: Optional[dict]) -> bool:
        return bool(_extract_target_codes_from_payload(payload or {}, limit=20))

    @classmethod
    def _has_exact_target_universe_match(
        cls,
        left_payload: Optional[dict],
        right_payload: Optional[dict],
    ) -> bool:
        left_codes = {
            str(code).strip()
            for code in _extract_target_codes_from_payload(left_payload or {}, limit=20)
            if str(code).strip()
        }
        right_codes = {
            str(code).strip()
            for code in _extract_target_codes_from_payload(right_payload or {}, limit=20)
            if str(code).strip()
        }
        return bool(left_codes and right_codes and left_codes == right_codes)

    @classmethod
    def _has_material_target_divergence(
        cls,
        candidate: Optional[dict],
        existing_item: Optional[dict],
        target_overlap: Optional[float],
    ) -> bool:
        if target_overlap is None or target_overlap >= 0.8:
            return False
        if not cls._has_explicit_universe(candidate) or not cls._has_explicit_universe(existing_item):
            return False
        return True

    def _select_vector_candidates(self, suspicious: List[dict]) -> List[Tuple[dict, float]]:
        ranked = sorted(
            list(suspicious or []),
            key=lambda item: (
                float(item.get("effective_similarity") or 0.0),
                float(item.get("target_overlap") or 0.0) if item.get("target_overlap") is not None else -1.0,
                float(item.get("tag_overlap") or 0.0),
            ),
            reverse=True,
        )
        selected: List[Tuple[dict, float]] = []
        for item in ranked[: self.MAX_VECTOR_CANDIDATES]:
            existing_item = dict(item.get("existing_item") or {})
            if not existing_item:
                continue
            selected.append((existing_item, float(item.get("effective_similarity") or 0.0)))
        return selected

    @staticmethod
    def _extract_parent_strategy_ids(candidate: Optional[dict]) -> set[str]:
        item = dict(candidate or {})
        metadata = dict(item.get("metadata") or {})
        generation_reason = dict(item.get("generation_reason") or {})
        research_task = dict(item.get("research_task") or {})
        lineage = dict(item.get("lineage") or {})
        parent_ids: set[str] = set()
        for source in (item, metadata, generation_reason, research_task, lineage):
            for key in ("parent_strategy_id", "parent_candidate_id"):
                value = str(source.get(key) or "").strip()
                if value:
                    parent_ids.add(value)
            for key in ("parent_strategy_ids", "parent_candidate_ids"):
                values = source.get(key)
                if isinstance(values, (list, tuple, set)):
                    parent_ids.update(str(value or "").strip() for value in values if str(value or "").strip())
        return parent_ids

    @staticmethod
    def _candidate_strategy_profile(candidate: Optional[dict]) -> dict:
        item = dict(candidate or {})
        if not item:
            return {}
        params = dict(item.get("params") or {})
        profile = dict(item.get("strategy_profile") or params.get("strategy_profile") or {})
        if not profile:
            profile = dict(infer_candidate_strategy_profile(item) or {})
        return {
            str(key): value
            for key, value in profile.items()
            if value not in (None, [], {}, "")
        }

    @classmethod
    def _report_item(cls, candidate: Optional[dict]) -> dict:
        item = dict(candidate or {})
        profile = cls._candidate_strategy_profile(item)
        return {
            "strategy_type": item.get("strategy_type"),
            "generator_type": item.get("generator_type"),
            "params": item.get("params"),
            "target_symbols": item.get("target_symbols") or [],
            "stock_pool": item.get("stock_pool") or {},
            "tags": item.get("tags") or [],
            "spawn_reason": item.get("spawn_reason"),
            "dedup_result": item.get("dedup_result"),
            "strategy_profile": profile,
            "candidate_family_id": profile.get("candidate_family_id"),
            "holding_period_bucket": profile.get("holding_period_bucket"),
            "alpha_source": profile.get("alpha_source"),
            "risk_level": profile.get("risk_level"),
            "regime_fit": profile.get("regime_fit"),
            "generator_mode": profile.get("generator_mode"),
        }

    @staticmethod
    def _candidate_refresh_rank(candidate: Optional[dict]) -> tuple[float, float, float]:
        item = dict(candidate or {})
        metrics = dict(item.get('backtest_metrics') or (item.get('backtest_result') or {}).get('metrics') or {})
        sharpe = float(metrics.get('sharpe_ratio') or 0.0)
        total_return = float(metrics.get('total_return') or 0.0)
        max_drawdown = float(metrics.get('max_drawdown') or 1.0)
        return (round(sharpe, 6), round(total_return, 6), round(-max_drawdown, 6))

    @staticmethod
    def _extract_quality_snapshot(item: Optional[dict]) -> dict[str, Any]:
        payload = dict(item or {})
        params = dict(payload.get("params") or {})
        quality_summary = dict(
            payload.get("quality_summary")
            or payload.get("quality_gate_summary")
            or dict(payload.get("quality_gate") or {}).get("summary")
            or {}
        )
        evidence = dict(
            payload.get("candidate_evidence_status")
            or params.get("candidate_evidence_status")
            or quality_summary.get("candidate_evidence_status")
            or {}
        )
        promotion_review = dict(
            payload.get("promotion_review")
            or payload.get("review_report")
            or quality_summary.get("promotion_review")
            or {}
        )
        backtest_metrics = dict(
            payload.get("backtest_metrics")
            or (payload.get("backtest_result") or {}).get("metrics")
            or quality_summary.get("backtest_metrics")
            or {}
        )
        observed_forward_days = list(
            evidence.get("observed_forward_days")
            or evidence.get("forward_days")
            or []
        )
        missing_forward_days = list(evidence.get("missing_forward_days") or [])
        raw_validation_grade = str(
            payload.get("raw_validation_grade")
            or quality_summary.get("raw_validation_grade")
            or evidence.get("raw_validation_grade")
            or payload.get("validation_grade")
            or quality_summary.get("validation_grade")
            or evidence.get("validation_grade")
            or ""
        ).strip().upper()
        effective_validation_grade = str(
            payload.get("validation_grade")
            or payload.get("effective_validation_grade")
            or quality_summary.get("validation_grade")
            or quality_summary.get("effective_validation_grade")
            or evidence.get("validation_grade")
            or ""
        ).strip().upper()
        promotion_ready = bool(
            evidence.get("promotion_ready")
            or quality_summary.get("promotion_ready")
        )
        total_signals = int(
            evidence.get("total_signals")
            or evidence.get("signal_total_signals")
            or quality_summary.get("signal_total_signals")
            or 0
        )
        minimum_signal_count = int(evidence.get("minimum_signal_count") or 10)
        forward_coverage_ratio = float(
            evidence.get("forward_window_coverage_ratio")
            or quality_summary.get("forward_window_coverage_ratio")
            or (
                len(observed_forward_days) / max(len(observed_forward_days) + len(missing_forward_days), 1)
                if observed_forward_days or missing_forward_days
                else 0.0
            )
            or 0.0
        )
        return {
            "validation_grade": effective_validation_grade or raw_validation_grade or None,
            "raw_validation_grade": raw_validation_grade or None,
            "effective_validation_grade": effective_validation_grade or raw_validation_grade or None,
            "promotion_ready": promotion_ready,
            "total_signals": total_signals,
            "minimum_signal_count": minimum_signal_count,
            "forward_coverage_ratio": round(max(min(forward_coverage_ratio, 1.0), 0.0), 4),
            "promotion_review_score": float(
                promotion_review.get("score")
                or quality_summary.get("promotion_review_score")
                or 0.0
            ),
            "promotion_review_status": str(
                promotion_review.get("status")
                or quality_summary.get("promotion_review_status")
                or ""
            ).strip().lower(),
            "promotion_review_recommendation": str(
                promotion_review.get("recommendation")
                or quality_summary.get("promotion_review_recommendation")
                or ""
            ).strip().lower(),
            "sharpe_ratio": float(backtest_metrics.get("sharpe_ratio") or 0.0),
            "total_return": float(backtest_metrics.get("total_return") or 0.0),
            "max_drawdown": float(backtest_metrics.get("max_drawdown") or 0.0),
        }

    @classmethod
    def _quality_snapshot_score(cls, item: Optional[dict]) -> tuple[float, bool]:
        snapshot = cls._extract_quality_snapshot(item)
        comparable = any(
            [
                snapshot.get("raw_validation_grade"),
                snapshot.get("validation_grade"),
                snapshot.get("promotion_ready"),
                snapshot.get("total_signals"),
                snapshot.get("forward_coverage_ratio"),
                snapshot.get("promotion_review_score"),
                snapshot.get("sharpe_ratio"),
            ]
        )
        if not comparable:
            return 0.0, False
        grade_score = {
            "A": 1.0,
            "B": 0.85,
            "C": 0.7,
            "D": 0.45,
        }.get(
            str(snapshot.get("raw_validation_grade") or snapshot.get("validation_grade") or "").upper(),
            0.25,
        )
        signal_ratio = min(
            float(snapshot.get("total_signals") or 0.0)
            / max(float(snapshot.get("minimum_signal_count") or 10.0), 1.0),
            1.0,
        )
        review_score = max(min(float(snapshot.get("promotion_review_score") or 0.0), 1.0), 0.0)
        sharpe_score = max(min((float(snapshot.get("sharpe_ratio") or 0.0) + 1.0) / 4.0, 1.0), 0.0)
        total_return_score = max(min(float(snapshot.get("total_return") or 0.0), 1.0), -1.0)
        drawdown_penalty = max(min(float(snapshot.get("max_drawdown") or 0.0), 1.0), 0.0)
        score = (
            grade_score * 0.26
            + signal_ratio * 0.18
            + float(snapshot.get("forward_coverage_ratio") or 0.0) * 0.22
            + (0.16 if bool(snapshot.get("promotion_ready")) else 0.0)
            + review_score * 0.1
            + sharpe_score * 0.06
            + max(total_return_score, 0.0) * 0.04
            - drawdown_penalty * 0.04
        )
        return round(score, 6), True

    @staticmethod
    def _lineage_counter(item: Optional[dict], *keys: str) -> int:
        payload = dict(item or {})
        params = dict(payload.get("params") or {})
        lineage = dict(
            payload.get("candidate_lineage_contract")
            or payload.get("lineage")
            or params.get("candidate_lineage_contract")
            or params.get("lineage")
            or {}
        )
        for source in (payload, params, lineage):
            for key in keys:
                try:
                    value = int(source.get(key) or 0)
                except Exception:
                    value = 0
                if value > 0:
                    return value
        return 0

    @classmethod
    def _suggest_holding_bucket_shift(cls, bucket: str | None) -> str | None:
        mapping = {
            "short": "medium",
            "medium": "long",
            "long": "medium",
        }
        token = str(bucket or "").strip().lower()
        return mapping.get(token) or ("medium" if token else None)

    @classmethod
    def _suggest_generator_mode_shift(cls, generator_mode: str | None) -> str | None:
        token = str(generator_mode or "").strip().lower()
        if not token:
            return "external_llm"
        return "external_llm" if token != "external_llm" else "rule"

    @classmethod
    def _lineage_quality_pressure(
        cls,
        candidate: Optional[dict],
        existing_item: Optional[dict],
        *,
        refresh_lineage_depth: int,
        revision_lineage_depth: int,
        exact_target_universe_match: bool,
    ) -> dict[str, Any]:
        candidate_snapshot = cls._extract_quality_snapshot(candidate)
        existing_snapshot = cls._extract_quality_snapshot(existing_item)
        raw_grade = str(
            existing_snapshot.get("raw_validation_grade")
            or candidate_snapshot.get("raw_validation_grade")
            or ""
        ).strip().upper()
        explicit_streak = max(
            cls._lineage_counter(
                candidate,
                "consecutive_raw_validation_d_count",
                "raw_validation_d_streak",
                "consecutive_low_quality_count",
                "low_quality_lineage_count",
            ),
            cls._lineage_counter(
                existing_item,
                "consecutive_raw_validation_d_count",
                "raw_validation_d_streak",
                "consecutive_low_quality_count",
                "low_quality_lineage_count",
            ),
        )
        low_quality_active = raw_grade == "D"
        streak = explicit_streak
        if low_quality_active and streak <= 0:
            streak = max(refresh_lineage_depth, revision_lineage_depth)
        candidate_profile = cls._candidate_strategy_profile(candidate)
        existing_profile = cls._candidate_strategy_profile(existing_item)
        candidate_holding_bucket = str(candidate_profile.get("holding_period_bucket") or "").strip().lower()
        existing_holding_bucket = str(existing_profile.get("holding_period_bucket") or "").strip().lower()
        candidate_generator_mode = str(candidate_profile.get("generator_mode") or "").strip().lower()
        existing_generator_mode = str(existing_profile.get("generator_mode") or "").strip().lower()
        holding_bucket_shift_applied = bool(
            candidate_holding_bucket
            and existing_holding_bucket
            and candidate_holding_bucket != existing_holding_bucket
        )
        generator_mode_shift_applied = bool(
            candidate_generator_mode
            and existing_generator_mode
            and candidate_generator_mode != existing_generator_mode
        )
        universe_shift_applied = not exact_target_universe_match
        structural_shift_applied = bool(
            holding_bucket_shift_applied
            or generator_mode_shift_applied
            or universe_shift_applied
        )
        required_shift = bool(low_quality_active and streak >= 2)
        retire_lineage = bool(low_quality_active and streak >= cls.MAX_REVISION_PER_LINEAGE)
        return {
            "low_quality_lineage_active": low_quality_active,
            "low_quality_lineage_streak": streak if low_quality_active else 0,
            "lineage_structural_shift_required": required_shift,
            "lineage_structural_shift_applied": structural_shift_applied,
            "holding_bucket_shift_applied": holding_bucket_shift_applied,
            "generator_mode_shift_applied": generator_mode_shift_applied,
            "universe_shift_applied": universe_shift_applied,
            "recommended_holding_bucket_shift": (
                cls._suggest_holding_bucket_shift(existing_holding_bucket or candidate_holding_bucket)
                if required_shift and not holding_bucket_shift_applied
                else None
            ),
            "recommended_generator_mode_shift": (
                cls._suggest_generator_mode_shift(existing_generator_mode or candidate_generator_mode)
                if required_shift and not generator_mode_shift_applied
                else None
            ),
            "recommended_universe_shift": bool(required_shift and not universe_shift_applied),
            "lineage_retire_recommended": retire_lineage,
            "lineage_quality_basis_grade": raw_grade or None,
        }

    @classmethod
    def _refresh_improvement_snapshot(
        cls,
        candidate: Optional[dict],
        existing_item: Optional[dict],
    ) -> dict[str, Any]:
        candidate_score, candidate_comparable = cls._quality_snapshot_score(candidate)
        existing_score, existing_comparable = cls._quality_snapshot_score(existing_item)
        if not candidate_comparable and not existing_comparable:
            return {
                "required": False,
                "passed": True,
                "candidate_score": candidate_score,
                "existing_score": existing_score,
            }
        improvement_margin = round(candidate_score - existing_score, 6)
        candidate_snapshot = cls._extract_quality_snapshot(candidate)
        existing_snapshot = cls._extract_quality_snapshot(existing_item)
        passed = bool(
            improvement_margin >= 0.05
            or (
                bool(candidate_snapshot.get("promotion_ready"))
                and not bool(existing_snapshot.get("promotion_ready"))
            )
            or (
                float(candidate_snapshot.get("forward_coverage_ratio") or 0.0)
                - float(existing_snapshot.get("forward_coverage_ratio") or 0.0)
                >= 0.2
            )
            or (
                float(candidate_snapshot.get("promotion_review_score") or 0.0)
                - float(existing_snapshot.get("promotion_review_score") or 0.0)
                >= 0.08
            )
        )
        return {
            "required": True,
            "passed": passed,
            "candidate_score": candidate_score,
            "existing_score": existing_score,
            "candidate_snapshot": candidate_snapshot,
            "existing_snapshot": existing_snapshot,
        }

    @staticmethod
    def _lineage_operation_depth(item: Optional[dict], *, mode: str) -> int:
        payload = dict(item or {})
        params = dict(payload.get("params") or {})
        lineage = dict(
            payload.get("candidate_lineage_contract")
            or payload.get("lineage")
            or params.get("candidate_lineage_contract")
            or params.get("lineage")
            or {}
        )
        keys = (
            f"{mode}_count",
            f"{mode}_depth",
            f"lineage_{mode}_count",
            f"lineage_{mode}_depth",
            f"consecutive_{mode}_count",
        )
        for source in (payload, params, lineage):
            for key in keys:
                try:
                    value = int(source.get(key) or 0)
                except Exception:
                    value = 0
                if value > 0:
                    return value
        return 0
    @classmethod
    def _collapse_refresh_existing_candidates(cls, unique: List[dict]) -> tuple[List[dict], List[dict]]:
        collapsed: List[dict] = []
        dropped: List[dict] = []
        kept_by_strategy: dict[str, int] = {}

        for candidate in unique:
            detail = dict(candidate.get('dedup_result') or {})
            if not detail.get('refresh_existing') or str(detail.get('refresh_mode') or '').strip().lower() != 'refresh_metrics_only':
                collapsed.append(candidate)
                continue
            matched_strategy_id = str(detail.get('matched_strategy_id') or '').strip()
            if not matched_strategy_id:
                collapsed.append(candidate)
                continue
            current_rank = cls._candidate_refresh_rank(candidate)
            if matched_strategy_id not in kept_by_strategy:
                kept_by_strategy[matched_strategy_id] = len(collapsed)
                collapsed.append(candidate)
                continue

            kept_index = kept_by_strategy[matched_strategy_id]
            kept_candidate = collapsed[kept_index]
            kept_rank = cls._candidate_refresh_rank(kept_candidate)
            if current_rank > kept_rank:
                previous = dict(kept_candidate)
                previous_detail = dict(previous.get('dedup_result') or {})
                previous_detail.update({
                    'duplicate': True,
                    'duplicate_level': 'refresh_existing_conflict',
                    'match_type': 'refresh_existing',
                    'reason': f"同一目标策略 {matched_strategy_id} 已有更优刷新候选，按回测表现折叠当前候选",
                })
                previous['dedup_result'] = previous_detail
                dropped.append(previous)
                collapsed[kept_index] = candidate
            else:
                current = dict(candidate)
                current_detail = dict(current.get('dedup_result') or {})
                current_detail.update({
                    'duplicate': True,
                    'duplicate_level': 'refresh_existing_conflict',
                    'match_type': 'refresh_existing',
                    'reason': f"同一目标策略 {matched_strategy_id} 已有更优刷新候选，按回测表现折叠当前候选",
                })
                current['dedup_result'] = current_detail
                dropped.append(current)

        return collapsed, dropped

    @staticmethod
    def _merge_metrics(left: Optional[dict], right: Optional[dict]) -> dict:
        merged: dict[str, int] = {}
        for key in {
            "scanned_count",
            "coarse_candidate_count",
            "coarse_tag_hit_count",
            "coarse_target_hit_count",
            "vector_candidate_count",
            "vector_candidate_trimmed_count",
        }:
            merged[key] = int((left or {}).get(key) or 0) + int((right or {}).get(key) or 0)
        return merged

    @staticmethod
    def _select_detail(base_detail: Optional[dict], intra_detail: Optional[dict]) -> dict:
        left = dict(base_detail or {})
        right = dict(intra_detail or {})
        if (
            left.get("refresh_existing")
            and right.get("duplicate")
            and not str(right.get("matched_strategy_id") or "").strip()
        ):
            return left
        if right.get("duplicate"):
            return right
        if left.get("duplicate"):
            return left
        if left.get("refresh_existing"):
            return left
        if right.get("refresh_existing"):
            return right
        left_similarity = float(left.get("effective_similarity") or 0.0)
        right_similarity = float(right.get("effective_similarity") or 0.0)
        return right if right_similarity > left_similarity else left

    async def _analyze_against_existing(
        self,
        candidates: List[dict],
        existing_by_type: Dict[str, List[dict]],
        db,
    ) -> List[tuple[dict, dict, dict]]:
        concurrency = max(1, int(_compat_setting("DEDUP_CONCURRENCY", DEDUP_CONCURRENCY) or 1))
        sem = asyncio.Semaphore(concurrency)

        async def _run(candidate: dict) -> tuple[dict, dict, dict]:
            strategy_type = self._normalize_strategy_type(candidate.get("strategy_type"))
            persisted_bucket = list(existing_by_type.get(strategy_type, []))
            async with sem:
                detail, metrics = await self._find_duplicate(candidate, persisted_bucket, db)
            return candidate, detail, metrics

        return await asyncio.gather(*[_run(candidate) for candidate in list(candidates or [])])

    async def deduplicate(self, candidates: List[dict], db) -> List[dict]:
        # PR-S9: 分页扫描全部 listed + incubating，避免被 limit=500 截断而漏检
        existing: List[dict] = []
        max_total = int(os.getenv("STRATEGY_FACTORY_DEDUP_MAX_EXISTING", "5000") or 5000)
        page_size = int(os.getenv("STRATEGY_FACTORY_DEDUP_PAGE_SIZE", "500") or 500)
        page_size = max(50, min(page_size, 2000))
        for status in ("listed", "incubating", "submitted"):
            try:
                submitted_loader = None
                if status == "submitted" and hasattr(db, "list_submitted_strategies_for_dedup"):
                    submitted_loader = getattr(db, "list_submitted_strategies_for_dedup")
                # 优先尝试分页；DB 不支持 offset 时降级到单次拉取
                offset = 0
                pages_loaded = 0
                while len(existing) < max_total:
                    try:
                        if callable(submitted_loader):
                            rows = await submitted_loader(limit=page_size, offset=offset)
                        else:
                            rows = await db.list_strategies(status, limit=page_size, offset=offset)
                    except TypeError:
                        # 旧接口不接受 offset
                        if callable(submitted_loader):
                            rows = await submitted_loader(limit=page_size) if offset == 0 else []
                        else:
                            rows = await db.list_strategies(status, limit=page_size) if offset == 0 else []
                    rows = list(rows or [])
                    if not rows:
                        break
                    existing.extend(rows)
                    pages_loaded += 1
                    if len(rows) < page_size:
                        break
                    offset += len(rows)
                    if pages_loaded > 50:  # 防止失控
                        break
            except Exception as exc:
                logger.warning("deduplicator: failed to load %s strategies: %s", status, exc)

        existing_by_type = self._bucket_existing_by_type(existing)
        await self._prewarm_candidate_behaviors(candidates, db)
        analyzed_candidates = await self._analyze_against_existing(candidates, existing_by_type, db)

        unique: List[dict] = []
        dropped: List[dict] = []
        vector_checks = 0
        refreshed_existing = 0
        existing_scan_count = 0
        coarse_candidate_count = 0
        coarse_tag_hit_count = 0
        coarse_target_hit_count = 0
        vector_candidate_count = 0
        vector_candidate_trimmed_count = 0
        intra_batch_checks = 0

        for candidate, persisted_detail, persisted_metrics in analyzed_candidates:
            strategy_type = self._normalize_strategy_type(candidate.get("strategy_type"))
            metrics = dict(persisted_metrics or {})
            detail = dict(persisted_detail or {})
            intra_bucket = [item for item in unique if self._normalize_strategy_type(item.get("strategy_type")) == strategy_type]
            if intra_bucket and not detail.get("duplicate"):
                intra_batch_checks += 1
                intra_detail, intra_metrics = await self._find_duplicate(candidate, intra_bucket, db)
                metrics = self._merge_metrics(metrics, intra_metrics)
                detail = self._select_detail(detail, intra_detail)
            candidate["dedup_result"] = detail
            existing_scan_count += int(metrics.get("scanned_count") or 0)
            coarse_candidate_count += int(metrics.get("coarse_candidate_count") or 0)
            coarse_tag_hit_count += int(metrics.get("coarse_tag_hit_count") or 0)
            coarse_target_hit_count += int(metrics.get("coarse_target_hit_count") or 0)
            vector_candidate_count += int(metrics.get("vector_candidate_count") or 0)
            vector_candidate_trimmed_count += int(metrics.get("vector_candidate_trimmed_count") or 0)
            if detail.get("vector_checked"):
                vector_checks += 1
            if detail.get("duplicate"):
                dropped.append({**candidate})
                continue
            if detail.get("refresh_existing"):
                refreshed_existing += 1
            unique.append(candidate)

        collapsed_unique, collapsed_dropped = self._collapse_refresh_existing_candidates(unique)
        dropped.extend(collapsed_dropped)
        unique = collapsed_unique
        refreshed_existing = len([item for item in unique if dict(item.get("dedup_result") or {}).get("refresh_existing")])
        coarse_filtered_count = max(existing_scan_count - coarse_candidate_count, 0)
        coarse_hit_ratio = round(coarse_candidate_count / existing_scan_count, 4) if existing_scan_count else 0.0
        refresh_decision_basis_counts: dict[str, int] = {}
        revision_trigger_reason_counts: dict[str, int] = {}
        tested_object_hash_changed_count = 0
        existing_identity_available_count = 0
        existing_tested_object_available_count = 0
        for item in [*unique, *dropped]:
            dedup_result = dict(item.get("dedup_result") or {})
            refresh_decision_basis = str(dedup_result.get("refresh_decision_basis") or "").strip().lower()
            if refresh_decision_basis:
                refresh_decision_basis_counts[refresh_decision_basis] = (
                    refresh_decision_basis_counts.get(refresh_decision_basis, 0) + 1
                )
            revision_trigger_reason = str(dedup_result.get("revision_trigger_reason") or "").strip().lower()
            if revision_trigger_reason:
                revision_trigger_reason_counts[revision_trigger_reason] = (
                    revision_trigger_reason_counts.get(revision_trigger_reason, 0) + 1
                )
            if bool(dedup_result.get("tested_object_hash_changed", dedup_result.get("tested_object_changed"))):
                tested_object_hash_changed_count += 1
            if bool(dedup_result.get("existing_identity_available")):
                existing_identity_available_count += 1
            if bool(dedup_result.get("existing_tested_object_available")):
                existing_tested_object_available_count += 1
        self.last_report = {
            "summary": {
                "input_count": len(candidates),
                "existing_count": len(existing),
                "existing_scan_count": existing_scan_count,
                "coarse_candidate_count": coarse_candidate_count,
                "coarse_filtered_count": coarse_filtered_count,
                "coarse_hit_ratio": coarse_hit_ratio,
                "coarse_tag_hit_count": coarse_tag_hit_count,
                "coarse_target_hit_count": coarse_target_hit_count,
                "kept_count": len(unique),
                "dropped_count": len(dropped),
                "refreshed_existing_count": refreshed_existing,
                "vector_checks": vector_checks,
                "vector_candidate_count": vector_candidate_count,
                "vector_candidate_trimmed_count": vector_candidate_trimmed_count,
                "candidate_analysis_concurrency": max(1, int(_compat_setting("DEDUP_CONCURRENCY", DEDUP_CONCURRENCY) or 1)),
                "persisted_existing_phase_count": len(analyzed_candidates),
                "intra_batch_check_count": intra_batch_checks,
                "param_threshold": self.THRESHOLD,
                "vector_threshold": self.VECTOR_THRESHOLD,
                "refresh_decision_basis_counts": refresh_decision_basis_counts,
                "revision_trigger_reason_counts": revision_trigger_reason_counts,
                "tested_object_hash_changed_count": tested_object_hash_changed_count,
                "existing_identity_available_count": existing_identity_available_count,
                "existing_tested_object_available_count": existing_tested_object_available_count,
            },
            "kept": [self._report_item(item) for item in unique],
            "dropped": [self._report_item(item) for item in dropped],
        }
        return unique

    @staticmethod
    def _candidate_task_signature(candidate: Optional[dict]) -> str:
        payload = dict(candidate or {})
        research_task = _normalize_research_task_contract(payload.get("research_task") or {})
        event_context = dict(payload.get("event_context") or {}) or _extract_event_context(research_task)
        return _build_task_signature({**research_task, **event_context})

    @staticmethod
    def _existing_task_signature(existing_item: Optional[dict]) -> str:
        payload = dict(existing_item or {})
        params = dict(payload.get("params") or {})
        explicit_signature = str(params.get("task_signature") or payload.get("task_signature") or "").strip()
        if explicit_signature:
            return explicit_signature

        raw_research_task = params.get("research_task") or payload.get("research_task") or {}
        raw_event_context = params.get("event_context") or payload.get("event_context") or {}
        if not raw_research_task and not raw_event_context:
            return ""

        research_task = _normalize_research_task_contract(raw_research_task)
        event_context = dict(raw_event_context or {}) or _extract_event_context(research_task)
        signature = _build_task_signature({**research_task, **event_context})
        if not any(
            [
                research_task.get("task_source"),
                research_task.get("task_id"),
                research_task.get("event_id"),
                research_task.get("theme_code"),
                event_context.get("event_id"),
                event_context.get("theme_code"),
            ]
        ):
            return ""
        return str(signature).strip()

    @staticmethod
    def _candidate_identity_signature(candidate: Optional[dict]) -> str:
        return build_candidate_identity_signature(candidate)

    @staticmethod
    def _candidate_tested_object_hash(candidate: Optional[dict]) -> str:
        payload = dict(candidate or {})
        params = dict(payload.get("params") or {})
        explicit_hash = str(payload.get("tested_object_hash") or params.get("tested_object_hash") or "").strip()
        if explicit_hash:
            return explicit_hash
        return build_tested_object_hash(candidate)

    @staticmethod
    def _has_explicit_identity_contract(item: Optional[dict]) -> bool:
        payload = dict(item or {})
        params = dict(payload.get("params") or {})
        for source in (payload, params):
            for key in (
                "portfolio_spec",
                "execution_assumptions",
                "validation_profile",
                "holding_horizon",
                "trade_plan",
                "risk_rules",
                "rebalance_rule",
                "stock_pool",
                "target_pool_id",
                "lineage",
            ):
                value = source.get(key)
                if value not in (None, "", [], {}):
                    return True
        return False

    @staticmethod
    def _has_explicit_tested_object_hash(item: Optional[dict]) -> bool:
        payload = dict(item or {})
        params = dict(payload.get("params") or {})
        return bool(str(payload.get("tested_object_hash") or params.get("tested_object_hash") or "").strip())

    @classmethod
    def _existing_identity_signature(cls, existing_item: Optional[dict]) -> str:
        if not cls._has_explicit_identity_contract(existing_item):
            return ""
        return build_candidate_identity_signature(existing_item)

    @classmethod
    def _existing_tested_object_hash(cls, existing_item: Optional[dict]) -> str:
        return cls._candidate_tested_object_hash(existing_item)

    @staticmethod
    def _decision_detail(decision: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(decision or {})
        return {
            "refresh_decision_basis": payload.get("refresh_decision_basis"),
            "revision_trigger_reason": payload.get("revision_trigger_reason"),
            "refresh_improvement_required": payload.get("refresh_improvement_required"),
            "refresh_improvement_passed": payload.get("refresh_improvement_passed"),
            "refresh_candidate_score": payload.get("refresh_candidate_score"),
            "refresh_existing_score": payload.get("refresh_existing_score"),
            "refresh_lineage_limit_reached": payload.get("refresh_lineage_limit_reached"),
            "revision_lineage_limit_reached": payload.get("revision_lineage_limit_reached"),
            "identity_changed": payload.get("identity_changed"),
            "tested_object_changed": payload.get("tested_object_changed"),
            "tested_object_hash_changed": payload.get("tested_object_hash_changed"),
            "task_signature_changed": payload.get("task_signature_changed"),
            "legacy_identity_partial": payload.get("legacy_identity_partial"),
            "tested_object_backfill_incomplete": payload.get("tested_object_backfill_incomplete"),
            "parent_lineage_matched": payload.get("parent_lineage_matched"),
            "existing_identity_available": payload.get("existing_identity_available"),
            "existing_tested_object_available": payload.get("existing_tested_object_available"),
            "candidate_tested_object_hash": payload.get("candidate_tested_object_hash"),
            "existing_tested_object_hash": payload.get("existing_tested_object_hash"),
            "low_quality_lineage_active": payload.get("low_quality_lineage_active"),
            "low_quality_lineage_streak": payload.get("low_quality_lineage_streak"),
            "lineage_structural_shift_required": payload.get("lineage_structural_shift_required"),
            "lineage_structural_shift_applied": payload.get("lineage_structural_shift_applied"),
            "recommended_holding_bucket_shift": payload.get("recommended_holding_bucket_shift"),
            "recommended_generator_mode_shift": payload.get("recommended_generator_mode_shift"),
            "recommended_universe_shift": payload.get("recommended_universe_shift"),
            "lineage_retire_recommended": payload.get("lineage_retire_recommended"),
            "lineage_quality_basis_grade": payload.get("lineage_quality_basis_grade"),
        }

    @classmethod
    def _semantic_result_priority(
        cls,
        decision: Optional[dict[str, Any]],
        match: Optional[dict[str, Any]],
    ) -> tuple[int, int, int, int, float, float]:
        payload = dict(decision or {})
        basis = str(payload.get("refresh_decision_basis") or "").strip().lower()
        basis_rank = {
            "same_tested_object_and_identity": 6,
            "same_tested_object_but_legacy_identity_partial": 5,
            "same_tested_object_with_legacy_identity_backfill": 5,
            "legacy_partial_parent_lineage_refresh": 5,
            "legacy_partial_event_context_refresh": 5,
            "legacy_partial_task_signature_refresh": 5,
            "tested_object_changed": 4,
            "identity_changed": 3,
            "task_signature_changed": 2,
            "target_universe_changed": 1,
        }
        return (
            2 if payload.get("refresh_existing") else 1,
            basis_rank.get(basis, 0),
            1 if payload.get("parent_lineage_matched") else 0,
            1 if payload.get("exact_target_universe_match") else 0,
            float((match or {}).get("target_overlap") or -1.0),
            float((match or {}).get("effective_similarity") or 0.0),
        )

    @classmethod
    def _semantic_result_from_decision(
        cls,
        candidate: Optional[dict],
        match: Optional[dict[str, Any]],
        existing_item: Optional[dict],
        decision: Optional[dict[str, Any]],
    ) -> Optional[tuple[tuple[int, int, int, int, float, float], dict[str, Any]]]:
        payload = dict(decision or {})
        match_payload = dict(match or {})
        decision_detail = cls._decision_detail(payload)
        basis = str(payload.get("refresh_decision_basis") or "").strip().lower()
        effective_similarity = float(match_payload.get("effective_similarity") or 0.0)
        if payload.get("refresh_existing"):
            if basis not in {
                "same_tested_object_and_identity",
                "same_tested_object_with_legacy_identity_backfill",
                "legacy_partial_parent_lineage_refresh",
                "legacy_partial_event_context_refresh",
                "legacy_partial_task_signature_refresh",
            }:
                return None
            return cls._semantic_result_priority(payload, match_payload), {
                "duplicate": False,
                "refresh_existing": True,
                "duplicate_level": "refresh_existing",
                "match_type": "semantic",
                "refresh_mode": "refresh_metrics_only",
                "reason": (
                    f"命中同一 tested object（{basis}），即使综合相似度 {effective_similarity:.4f} < "
                    f"阈值 {cls.THRESHOLD:.2f} 仍转为刷新复用"
                ),
                "threshold": cls.THRESHOLD,
                "vector_threshold": cls.VECTOR_THRESHOLD,
                "vector_checked": False,
                "task_signature": cls._candidate_task_signature(candidate),
                **decision_detail,
                **match_payload,
            }
        if payload.get("spawn_revision_from_existing"):
            return cls._semantic_result_priority(payload, match_payload), {
                "duplicate": False,
                "refresh_existing": False,
                "duplicate_level": "spawn_revision_from_existing",
                "match_type": "semantic",
                "refresh_mode": "spawn_revision_from_existing",
                "parent_strategy_id": (existing_item or {}).get("id"),
                "reason": (
                    f"命中语义级修订条件（{basis or 'revision'}），即使综合相似度 {effective_similarity:.4f} < "
                    f"阈值 {cls.THRESHOLD:.2f} 仍保留为基于已有策略派生的新实验"
                ),
                "threshold": cls.THRESHOLD,
                "vector_threshold": cls.VECTOR_THRESHOLD,
                "vector_checked": False,
                "task_signature": cls._candidate_task_signature(candidate),
                **decision_detail,
                **match_payload,
            }
        return None
    @classmethod
    def _evaluate_existing_match_decision(
        cls,
        candidate: Optional[dict],
        match: Optional[dict],
        existing_item: Optional[dict] = None,
    ) -> dict[str, Any]:
        candidate = dict(candidate or {})
        match = dict(match or {})
        existing_payload = dict(existing_item or {})
        matched_status = str(match.get("matched_status") or "").strip().lower()
        matched_strategy_id = str(match.get("matched_strategy_id") or "").strip()
        parent_strategy_ids = cls._extract_parent_strategy_ids(candidate)
        parent_lineage_matched = bool(matched_strategy_id and matched_strategy_id in parent_strategy_ids)
        explicit_candidate_universe = cls._has_explicit_universe(candidate)
        explicit_existing_universe = cls._has_explicit_universe(existing_payload)
        exact_target_universe_match = (
            cls._has_exact_target_universe_match(candidate, existing_payload)
            if existing_payload and explicit_candidate_universe and explicit_existing_universe
            else False
        )
        target_overlap = float(match.get("target_overlap") or 0.0)
        research_task = _normalize_research_task_contract(candidate.get("research_task") or {})
        event_context = dict(candidate.get("event_context") or {}) or _extract_event_context(research_task)
        has_event_context = bool(
            event_context.get("event_id")
            or event_context.get("theme_code")
            or research_task.get("task_source") == "event_driven"
            or str(candidate.get("source") or "").startswith("strategy_factory:")
        )
        candidate_signature = cls._candidate_task_signature(candidate)
        existing_signature = cls._existing_task_signature(existing_payload) if existing_payload else ""
        candidate_identity = cls._candidate_identity_signature(candidate)
        existing_identity = cls._existing_identity_signature(existing_payload) if existing_payload else ""
        candidate_tested_object_hash = cls._candidate_tested_object_hash(candidate)
        existing_tested_object_hash = cls._existing_tested_object_hash(existing_payload) if existing_payload else ""
        tested_object_changed = None
        if existing_payload and candidate_tested_object_hash and existing_tested_object_hash:
            tested_object_changed = candidate_tested_object_hash != existing_tested_object_hash
        identity_changed = None
        if existing_payload and candidate_identity and existing_identity:
            identity_changed = candidate_identity != existing_identity
        task_signature_changed = None
        if existing_payload and candidate_signature and existing_signature:
            task_signature_changed = candidate_signature != existing_signature
        legacy_identity_partial = bool(
            existing_payload
            and (
                not cls._has_explicit_identity_contract(existing_payload)
                or not cls._has_explicit_tested_object_hash(existing_payload)
            )
        )
        refresh_improvement = cls._refresh_improvement_snapshot(candidate, existing_payload)
        refresh_lineage_depth = max(
            cls._lineage_operation_depth(candidate, mode="refresh"),
            cls._lineage_operation_depth(existing_payload, mode="refresh"),
        )
        revision_lineage_depth = max(
            cls._lineage_operation_depth(candidate, mode="revision"),
            cls._lineage_operation_depth(existing_payload, mode="revision"),
        )
        tested_object_backfill_incomplete = bool(
            existing_payload
            and not cls._has_explicit_tested_object_hash(existing_payload)
            and legacy_identity_partial
        )
        decision = {
            "refresh_existing": False,
            "spawn_revision_from_existing": False,
            "refresh_decision_basis": None,
            "revision_trigger_reason": None,
            "parent_lineage_matched": parent_lineage_matched,
            "candidate_task_signature": candidate_signature or None,
            "existing_task_signature": existing_signature or None,
            "task_signature_changed": task_signature_changed,
            "candidate_identity_signature": candidate_identity or None,
            "existing_identity_signature": existing_identity or None,
            "existing_identity_available": bool(existing_payload and existing_identity),
            "identity_changed": identity_changed,
            "candidate_tested_object_hash": candidate_tested_object_hash or None,
            "existing_tested_object_hash": existing_tested_object_hash or None,
            "tested_object_changed": tested_object_changed,
            "tested_object_hash_changed": tested_object_changed,
            "existing_tested_object_available": bool(existing_payload and existing_tested_object_hash),
            "legacy_identity_partial": legacy_identity_partial,
            "tested_object_backfill_incomplete": tested_object_backfill_incomplete,
            "exact_target_universe_match": exact_target_universe_match,
            "refresh_improvement_required": bool(refresh_improvement.get("required")),
            "refresh_improvement_passed": bool(refresh_improvement.get("passed")),
            "refresh_candidate_score": refresh_improvement.get("candidate_score"),
            "refresh_existing_score": refresh_improvement.get("existing_score"),
            "refresh_lineage_limit_reached": refresh_lineage_depth >= cls.MAX_REFRESH_PER_LINEAGE,
            "revision_lineage_limit_reached": revision_lineage_depth >= cls.MAX_REVISION_PER_LINEAGE,
        }
        decision.update(
            cls._lineage_quality_pressure(
                candidate,
                existing_payload,
                refresh_lineage_depth=refresh_lineage_depth,
                revision_lineage_depth=revision_lineage_depth,
                exact_target_universe_match=exact_target_universe_match,
            )
        )
        if matched_status not in {"incubating", "listed", "published"} or not matched_strategy_id:
            decision["refresh_decision_basis"] = "matched_strategy_not_refreshable"
            return decision
        if (
            decision.get("lineage_retire_recommended")
            and decision.get("lineage_structural_shift_required")
            and not decision.get("lineage_structural_shift_applied")
        ):
            decision["refresh_decision_basis"] = "low_quality_lineage_retired"
            return decision
        if not existing_payload:
            decision["refresh_existing"] = bool(has_event_context and explicit_candidate_universe)
            decision["refresh_decision_basis"] = (
                "event_context_fallback"
                if decision["refresh_existing"]
                else ("parent_lineage_without_existing_context" if parent_lineage_matched else "insufficient_existing_context")
            )
            return decision
        legacy_partial_refresh_allowed = bool(
            legacy_identity_partial
            and not bool(decision.get("existing_identity_available"))
            and exact_target_universe_match
            and bool(decision.get("refresh_improvement_passed"))
            and not bool(decision.get("refresh_lineage_limit_reached"))
            and (
                (parent_lineage_matched and task_signature_changed is not True)
                or (
                    has_event_context
                    and explicit_candidate_universe
                    and task_signature_changed is not True
                )
                or (task_signature_changed is False)
            )
        )
        if tested_object_changed is True:
            if legacy_partial_refresh_allowed:
                if (
                    decision.get("lineage_structural_shift_required")
                    and not decision.get("lineage_structural_shift_applied")
                ):
                    decision["refresh_decision_basis"] = "low_quality_lineage_refresh_blocked"
                    return decision
                decision["refresh_existing"] = True
                if parent_lineage_matched:
                    decision["refresh_decision_basis"] = "legacy_partial_parent_lineage_refresh"
                elif has_event_context and explicit_candidate_universe:
                    decision["refresh_decision_basis"] = "legacy_partial_event_context_refresh"
                else:
                    decision["refresh_decision_basis"] = "legacy_partial_task_signature_refresh"
                return decision
            decision["refresh_decision_basis"] = "tested_object_changed"
            decision["spawn_revision_from_existing"] = bool(
                not decision.get("revision_lineage_limit_reached")
                and (
                    parent_lineage_matched
                    or target_overlap >= 0.8
                    or exact_target_universe_match
                    or task_signature_changed is False
                )
            )
            if decision["spawn_revision_from_existing"]:
                if (
                    decision.get("lineage_structural_shift_required")
                    and not decision.get("lineage_structural_shift_applied")
                ):
                    decision["spawn_revision_from_existing"] = False
                    decision["refresh_decision_basis"] = "low_quality_lineage_shift_required"
                    return decision
                decision["revision_trigger_reason"] = decision["refresh_decision_basis"]
            elif decision.get("revision_lineage_limit_reached"):
                decision["refresh_decision_basis"] = "revision_limit_reached"
            return decision
        if identity_changed is True:
            decision["refresh_decision_basis"] = "identity_changed"
            decision["spawn_revision_from_existing"] = bool(
                not decision.get("revision_lineage_limit_reached")
                and (
                    parent_lineage_matched
                    or target_overlap >= 0.8
                    or exact_target_universe_match
                )
            )
            if decision["spawn_revision_from_existing"]:
                if (
                    decision.get("lineage_structural_shift_required")
                    and not decision.get("lineage_structural_shift_applied")
                ):
                    decision["spawn_revision_from_existing"] = False
                    decision["refresh_decision_basis"] = "low_quality_lineage_shift_required"
                    return decision
                decision["revision_trigger_reason"] = decision["refresh_decision_basis"]
            elif decision.get("revision_lineage_limit_reached"):
                decision["refresh_decision_basis"] = "revision_limit_reached"
            return decision
        if task_signature_changed is True:
            decision["refresh_decision_basis"] = "task_signature_changed"
            decision["spawn_revision_from_existing"] = bool(
                not decision.get("revision_lineage_limit_reached")
                and (parent_lineage_matched or target_overlap >= 0.8)
            )
            if decision["spawn_revision_from_existing"]:
                if (
                    decision.get("lineage_structural_shift_required")
                    and not decision.get("lineage_structural_shift_applied")
                ):
                    decision["spawn_revision_from_existing"] = False
                    decision["refresh_decision_basis"] = "low_quality_lineage_shift_required"
                    return decision
                decision["revision_trigger_reason"] = decision["refresh_decision_basis"]
            elif decision.get("revision_lineage_limit_reached"):
                decision["refresh_decision_basis"] = "revision_limit_reached"
            return decision
        if explicit_candidate_universe and explicit_existing_universe and not exact_target_universe_match:
            decision["refresh_decision_basis"] = "target_universe_changed"
            decision["spawn_revision_from_existing"] = bool(
                not decision.get("revision_lineage_limit_reached")
                and (parent_lineage_matched or target_overlap >= 0.8)
            )
            if decision["spawn_revision_from_existing"]:
                if (
                    decision.get("lineage_structural_shift_required")
                    and not decision.get("lineage_structural_shift_applied")
                ):
                    decision["spawn_revision_from_existing"] = False
                    decision["refresh_decision_basis"] = "low_quality_lineage_shift_required"
                    return decision
                decision["revision_trigger_reason"] = decision["refresh_decision_basis"]
            elif decision.get("revision_lineage_limit_reached"):
                decision["refresh_decision_basis"] = "revision_limit_reached"
            return decision
        if candidate_tested_object_hash and existing_tested_object_hash and candidate_tested_object_hash == existing_tested_object_hash:
            if legacy_identity_partial:
                if (
                    decision.get("lineage_structural_shift_required")
                    and not decision.get("lineage_structural_shift_applied")
                ):
                    decision["refresh_decision_basis"] = "low_quality_lineage_refresh_blocked"
                    return decision
                if (
                    not decision.get("refresh_improvement_required")
                    or bool(decision.get("refresh_improvement_passed"))
                ) and not bool(decision.get("refresh_lineage_limit_reached")) and (
                    parent_lineage_matched
                    or (
                        has_event_context
                        and explicit_candidate_universe
                        and (exact_target_universe_match or target_overlap >= 0.8)
                    )
                    or (
                        task_signature_changed is False
                        and (exact_target_universe_match or target_overlap >= 0.8)
                    )
                ):
                    decision["refresh_existing"] = True
                    if parent_lineage_matched:
                        decision["refresh_decision_basis"] = "same_tested_object_with_legacy_identity_parent_lineage"
                    elif has_event_context and explicit_candidate_universe:
                        decision["refresh_decision_basis"] = "same_tested_object_with_legacy_identity_event_context"
                    else:
                        decision["refresh_decision_basis"] = "same_tested_object_with_legacy_identity_backfill"
                    return decision
                if decision.get("refresh_lineage_limit_reached"):
                    decision["refresh_decision_basis"] = "refresh_limit_reached"
                    return decision
                if decision.get("refresh_improvement_required") and not decision.get("refresh_improvement_passed"):
                    decision["refresh_decision_basis"] = "same_tested_object_without_improvement"
                    return decision
                decision["refresh_decision_basis"] = "same_tested_object_but_legacy_identity_partial"
                decision["spawn_revision_from_existing"] = bool(
                    not decision.get("revision_lineage_limit_reached")
                    and (
                        parent_lineage_matched
                        or target_overlap >= 0.8
                        or exact_target_universe_match
                        or task_signature_changed is False
                    )
                )
                if decision["spawn_revision_from_existing"]:
                    if (
                        decision.get("lineage_structural_shift_required")
                        and not decision.get("lineage_structural_shift_applied")
                    ):
                        decision["spawn_revision_from_existing"] = False
                        decision["refresh_decision_basis"] = "low_quality_lineage_shift_required"
                        return decision
                    decision["revision_trigger_reason"] = decision["refresh_decision_basis"]
                elif decision.get("revision_lineage_limit_reached"):
                    decision["refresh_decision_basis"] = "revision_limit_reached"
                return decision
            if (
                decision.get("lineage_structural_shift_required")
                and not decision.get("lineage_structural_shift_applied")
            ):
                decision["refresh_decision_basis"] = "low_quality_lineage_refresh_blocked"
                return decision
            if decision.get("refresh_lineage_limit_reached"):
                decision["refresh_decision_basis"] = "refresh_limit_reached"
                return decision
            if decision.get("refresh_improvement_required") and not decision.get("refresh_improvement_passed"):
                decision["refresh_decision_basis"] = "same_tested_object_without_improvement"
                return decision
            decision["refresh_existing"] = True
            decision["refresh_decision_basis"] = "same_tested_object_and_identity"
            return decision
        decision["refresh_decision_basis"] = "no_refresh_basis"
        return decision

    @classmethod
    def _should_refresh_existing(
        cls,
        candidate: Optional[dict],
        match: Optional[dict],
        existing_item: Optional[dict] = None,
    ) -> bool:
        return bool(
            cls._evaluate_existing_match_decision(candidate, match, existing_item).get("refresh_existing")
        )

    @classmethod
    def _should_spawn_revision_from_existing(
        cls,
        candidate: Optional[dict],
        match: Optional[dict],
        existing_item: Optional[dict],
    ) -> bool:
        return bool(
            cls._evaluate_existing_match_decision(candidate, match, existing_item).get("spawn_revision_from_existing")
        )

    async def _find_duplicate(self, candidate: dict, existing: list, db) -> tuple[dict, dict]:
        best_match: Optional[dict] = None
        semantic_match: Optional[dict[str, Any]] = None
        semantic_match_priority: Optional[tuple[int, int, int, int, float, float]] = None
        suspicious: List[dict] = []
        candidate_params = self._normalize_params(candidate.get("params"))
        metrics = {
            "scanned_count": 0,
            "coarse_candidate_count": 0,
            "coarse_tag_hit_count": 0,
            "coarse_target_hit_count": 0,
            "vector_candidate_count": 0,
            "vector_candidate_trimmed_count": 0,
        }
        for existing_item in existing:
            metrics["scanned_count"] += 1
            existing_params = self._normalize_params(existing_item.get("params"))
            param_similarity = self._param_sim(candidate_params, existing_params)
            target_overlap = self._target_overlap(candidate, existing_item)
            material_target_divergence = self._has_material_target_divergence(candidate, existing_item, target_overlap)
            tag_overlap = self._tag_overlap(candidate, existing_item)
            existing_dedup = dict(existing_item.get("dedup_result") or {})
            if tag_overlap > 0:
                metrics["coarse_tag_hit_count"] += 1
            if target_overlap is not None and target_overlap > 0:
                metrics["coarse_target_hit_count"] += 1
            effective_similarity = self._effective_similarity(param_similarity, target_overlap)
            match = {
                "matched_strategy_id": existing_item.get("id") or existing_dedup.get("matched_strategy_id"),
                "matched_name": (
                    existing_item.get("name")
                    or existing_dedup.get("matched_name")
                    or existing_item.get("strategy_type")
                ),
                "matched_status": existing_item.get("status") or existing_dedup.get("matched_status"),
                "param_similarity": round(param_similarity, 4),
                "target_overlap": target_overlap,
                "effective_similarity": round(effective_similarity, 4),
            }
            if best_match is None or effective_similarity > best_match.get("effective_similarity", 0):
                best_match = match
            decision: Optional[dict[str, Any]] = None
            decision_detail: dict[str, Any] = {}
            if not material_target_divergence:
                decision = self._evaluate_existing_match_decision(candidate, match, existing_item)
                decision_detail = self._decision_detail(decision)
                semantic_result = self._semantic_result_from_decision(candidate, match, existing_item, decision)
                if semantic_result is not None:
                    semantic_priority, semantic_detail = semantic_result
                    if semantic_match_priority is None or semantic_priority > semantic_match_priority:
                        semantic_match_priority = semantic_priority
                        semantic_match = semantic_detail
            if effective_similarity >= self.THRESHOLD and not material_target_divergence:
                overlap_text = f", 目标池重合度 {target_overlap:.4f}" if target_overlap is not None else ""
                if decision is None:
                    decision = self._evaluate_existing_match_decision(candidate, match, existing_item)
                    decision_detail = self._decision_detail(decision)
                if decision.get("refresh_existing"):
                    return {
                        "duplicate": False,
                        "refresh_existing": True,
                        "duplicate_level": "refresh_existing",
                        "match_type": "parameter",
                        "refresh_mode": "refresh_metrics_only",
                        "reason": f"综合相似度 {effective_similarity:.4f} ≥ 阈值 {self.THRESHOLD:.2f}（参数 {param_similarity:.4f}{overlap_text}），命中已有策略并转为刷新复用",
                        "threshold": self.THRESHOLD,
                        "vector_threshold": self.VECTOR_THRESHOLD,
                        "vector_checked": False,
                        "task_signature": self._candidate_task_signature(candidate),
                        **decision_detail,
                        **match,
                    }, metrics
                if decision.get("spawn_revision_from_existing"):
                    return {
                        "duplicate": False,
                        "refresh_existing": False,
                        "duplicate_level": "spawn_revision_from_existing",
                        "match_type": "parameter",
                        "refresh_mode": "spawn_revision_from_existing",
                        "parent_strategy_id": existing_item.get("id"),
                        "reason": (
                            f"综合相似度 {effective_similarity:.4f} ≥ 阈值 {self.THRESHOLD:.2f}，"
                            f"但已识别为策略对象变更（{decision.get('refresh_decision_basis') or 'revision'}），转为基于已有策略派生新实验"
                        ),
                        "threshold": self.THRESHOLD,
                        "vector_threshold": self.VECTOR_THRESHOLD,
                        "vector_checked": False,
                        "task_signature": self._candidate_task_signature(candidate),
                        **decision_detail,
                        **match,
                    }, metrics
                return {
                    "duplicate": True,
                    "duplicate_level": "parameter",
                    "match_type": "parameter",
                    "refresh_mode": None,
                    "reason": f"综合相似度 {effective_similarity:.4f} ≥ 阈值 {self.THRESHOLD:.2f}（参数 {param_similarity:.4f}{overlap_text}）",
                    "threshold": self.THRESHOLD,
                    "vector_threshold": self.VECTOR_THRESHOLD,
                    "vector_checked": False,
                    "task_signature": self._candidate_task_signature(candidate),
                    **decision_detail,
                    **match,
                }, metrics
            if effective_similarity >= self.VECTOR_TRIGGER_THRESHOLD:
                suspicious.append({
                    "existing_item": existing_item,
                    "param_similarity": round(param_similarity, 4),
                    "target_overlap": target_overlap,
                    "tag_overlap": round(tag_overlap, 4),
                    "effective_similarity": round(effective_similarity, 4),
                })

        metrics["coarse_candidate_count"] = len(suspicious)
        if semantic_match is not None:
            return semantic_match, metrics
        vector_candidates = self._select_vector_candidates(suspicious)
        metrics["vector_candidate_count"] = len(vector_candidates)
        metrics["vector_candidate_trimmed_count"] = max(0, len(suspicious) - len(vector_candidates))
        vector_detail = await self._vector_check(candidate, vector_candidates, db) if vector_candidates else None
        resolved_vector_threshold = self.VECTOR_THRESHOLD
        vector_keep_reason: Optional[str] = None
        if vector_detail:
            vector_similarity = float(vector_detail.get("similarity") or 0.0)
            param_similarity = float(vector_detail.get("param_similarity") or 0.0)
            target_overlap = vector_detail.get("target_overlap")
            has_candidate_universe = bool(_extract_target_codes_from_payload(candidate, limit=20))
            if has_candidate_universe and target_overlap is not None and target_overlap < 0.8:
                vector_keep_reason = (
                    f"目标池重合度 {target_overlap:.4f} < 0.80，保留为独立候选"
                )
                return {
                    "duplicate": False,
                    "refresh_existing": False,
                    "duplicate_level": "unique",
                    "match_type": None,
                    "refresh_mode": None,
                    "reason": vector_keep_reason,
                    "threshold": self.THRESHOLD,
                    "vector_threshold": self.VECTOR_THRESHOLD,
                    "vector_checked": True,
                    "param_similarity": round(param_similarity, 4),
                    "target_overlap": target_overlap,
                    "effective_similarity": round(vector_detail.get("effective_similarity", 0.0), 4),
                    "vector_similarity": round(vector_similarity, 4),
                    "vector_backend": vector_detail.get("backend"),
                    "matched_strategy_id": vector_detail.get("matched_strategy_id"),
                    "matched_name": vector_detail.get("matched_name"),
                    "matched_status": vector_detail.get("matched_status"),
                }, metrics
            require_param_confirmation = False
            if has_candidate_universe and target_overlap is None:
                resolved_vector_threshold = max(self.VECTOR_THRESHOLD, 0.98)
                require_param_confirmation = True
            vector_confirmed = vector_similarity >= resolved_vector_threshold
            if require_param_confirmation:
                vector_confirmed = vector_confirmed and param_similarity >= self.THRESHOLD
            if vector_confirmed:
                if require_param_confirmation:
                    reason = (
                        f"行为向量相似度 {vector_similarity:.4f} ≥ 阈值 {resolved_vector_threshold:.2f}，"
                        f"且目标池缺失场景下参数相似度 {param_similarity:.4f} ≥ {self.THRESHOLD:.2f}"
                    )
                else:
                    reason = f"行为向量相似度 {vector_similarity:.4f} ≥ 阈值 {resolved_vector_threshold:.2f}"
                return {
                    "duplicate": True,
                    "duplicate_level": "vector",
                    "match_type": "vector",
                    "refresh_mode": None,
                    "reason": reason,
                    "threshold": self.THRESHOLD,
                    "vector_threshold": resolved_vector_threshold,
                    "vector_checked": True,
                    "param_similarity": round(param_similarity, 4),
                    "target_overlap": target_overlap,
                    "effective_similarity": round(vector_detail.get("effective_similarity", 0.0), 4),
                    "vector_similarity": round(vector_similarity, 4),
                    "vector_backend": vector_detail.get("backend"),
                    "matched_strategy_id": vector_detail.get("matched_strategy_id"),
                    "matched_name": vector_detail.get("matched_name"),
                    "matched_status": vector_detail.get("matched_status"),
                }, metrics
            if require_param_confirmation and vector_similarity >= self.VECTOR_THRESHOLD:
                vector_keep_reason = (
                    f"行为向量相似但命中策略缺少目标池信息，参数相似度 {param_similarity:.4f} < {self.THRESHOLD:.2f}，暂不判重"
                )

        return {
            "duplicate": False,
            "refresh_existing": False,
            "duplicate_level": "unique",
            "match_type": None,
            "refresh_mode": None,
            "reason": vector_keep_reason or "未命中重复策略",
            "threshold": self.THRESHOLD,
            "vector_threshold": resolved_vector_threshold,
            "vector_checked": vector_detail is not None,
            "param_similarity": round((vector_detail or best_match or {}).get("param_similarity", 0.0), 4),
            "target_overlap": (vector_detail or best_match or {}).get("target_overlap"),
            "effective_similarity": round((vector_detail or best_match or {}).get("effective_similarity", 0.0), 4),
            "vector_similarity": round((vector_detail or {}).get("similarity", 0.0), 4),
            "vector_backend": (vector_detail or {}).get("backend"),
            "matched_strategy_id": (vector_detail or best_match or {}).get("matched_strategy_id"),
            "matched_name": (vector_detail or best_match or {}).get("matched_name"),
            "matched_status": (vector_detail or best_match or {}).get("matched_status"),
        }, metrics

    async def _prewarm_candidate_behaviors(self, candidates: List[dict], db) -> None:
        payloads = [
            (str(item.get("strategy_type") or "").strip(), self._normalize_params(item.get("params")))
            for item in list(candidates or [])
            if str(item.get("strategy_type") or "").strip()
        ]
        if payloads:
            unique_payloads: List[Tuple[str, dict]] = []
            seen_keys: set[str] = set()
            for strategy_type, params in payloads:
                cache_key = self._behavior_cache_key(strategy_type, params)
                if cache_key in seen_keys:
                    continue
                seen_keys.add(cache_key)
                unique_payloads.append((strategy_type, params))
            timeout_sec = self._resolve_prewarm_timeout_sec()
            try:
                await asyncio.wait_for(
                    self._bounded_behavior_gather(unique_payloads, db),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "deduplicator: prewarm timed out for %s payloads after %.2fs; continuing without full behavior cache",
                    len(unique_payloads),
                    timeout_sec,
                )

    @staticmethod
    def _normalize_params(value: object) -> dict:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _behavior_cache_key(strategy_type: str, params: Optional[dict]) -> str:
        return f"{strategy_type}:{json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str)}"

    @classmethod
    def _resolve_timeout_sec(cls, setting_name: str, default: float) -> float:
        try:
            resolved = float(_compat_setting(setting_name, default) or default)
        except (TypeError, ValueError):
            return float(default)
        return float(default) if resolved <= 0 else resolved
    @classmethod
    def _resolve_behavior_build_timeout_sec(cls) -> float:
        return cls._resolve_timeout_sec(
            "DEDUP_BEHAVIOR_BUILD_TIMEOUT_SEC",
            cls.DEFAULT_BEHAVIOR_BUILD_TIMEOUT_SEC,
        )

    @classmethod
    def _resolve_prewarm_timeout_sec(cls) -> float:
        return cls._resolve_timeout_sec(
            "DEDUP_PREWARM_TIMEOUT_SEC",
            cls.DEFAULT_PREWARM_TIMEOUT_SEC,
        )

    async def _bounded_behavior_gather(self, payloads: List[Tuple[str, dict]], db) -> List[Optional[List[dict]]]:
        if not payloads:
            return []
        concurrency = max(1, int(_compat_setting("DEDUP_CONCURRENCY", DEDUP_CONCURRENCY) or 1))
        sem = asyncio.Semaphore(concurrency)
        timeout_sec = self._resolve_behavior_build_timeout_sec()
        ordered_keys: List[str] = []
        unique_payloads: Dict[str, Tuple[str, dict]] = {}

        for strategy_type, params in list(payloads or []):
            normalized_params = self._normalize_params(params)
            cache_key = self._behavior_cache_key(strategy_type, normalized_params)
            ordered_keys.append(cache_key)
            unique_payloads.setdefault(cache_key, (strategy_type, normalized_params))

        async def _run(cache_key: str, strategy_type: str, params: dict) -> Optional[List[dict]]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._build_behavior_klines(strategy_type, params, db),
                        timeout=timeout_sec,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "deduplicator: behavior build timed out for %s after %.2fs",
                        cache_key,
                        timeout_sec,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("deduplicator: behavior build failed for %s: %s", cache_key, exc)
                return None

        unique_items = list(unique_payloads.items())
        gathered = await asyncio.gather(*[
            _run(cache_key, strategy_type, params)
            for cache_key, (strategy_type, params) in unique_items
        ])
        results_by_key = {
            cache_key: result
            for (cache_key, _payload), result in zip(unique_items, gathered)
        }
        return [results_by_key.get(cache_key) for cache_key in ordered_keys]

    async def _vector_check(self, candidate: dict, suspicious: List[Tuple[dict, float]], db) -> Optional[dict]:
        query_strategy_type = str(candidate.get("strategy_type") or "").strip()
        query_params = self._normalize_params(candidate.get("params"))
        suspicious_payloads = [
            (str(existing_item.get("strategy_type") or "").strip(), self._normalize_params(existing_item.get("params")))
            for existing_item, _ in list(suspicious or [])
            if str(existing_item.get("strategy_type") or "").strip()
        ]
        gathered = await self._bounded_behavior_gather([(query_strategy_type, query_params), *suspicious_payloads], db)
        query_klines = gathered[0] if gathered else None
        if not query_klines:
            return None

        candidate_klines_dict: Dict[str, List[dict]] = {}
        match_meta: Dict[str, dict] = {}
        for idx, (existing_item, effective_similarity) in enumerate(suspicious):
            params = self._normalize_params(existing_item.get("params"))
            klines = gathered[idx + 1] if idx + 1 < len(gathered) else None
            if not klines:
                continue
            code = str(existing_item.get("id") or existing_item.get("name") or f"candidate_{idx}")
            candidate_klines_dict[code] = klines
            match_meta[code] = {
                "matched_strategy_id": existing_item.get("id"),
                "matched_name": existing_item.get("name") or existing_item.get("strategy_type"),
                "matched_status": existing_item.get("status"),
                "param_similarity": self._param_sim(candidate.get("params", {}), params),
                "target_overlap": self._target_overlap(candidate, existing_item),
                "effective_similarity": effective_similarity,
            }
        if not candidate_klines_dict:
            return None

        gateway = self._get_vector_gateway()
        results = gateway.find_similar_patterns(
            query_klines=query_klines,
            candidate_klines_dict=candidate_klines_dict,
            top_k=1,
            method="returns",
            metric="cosine",
            backend="index",
            allow_fallback=True,
        )
        if not results:
            return None
        top = results[0]
        meta = match_meta.get(str(top.get("code")), {})
        return {
            "similarity": float(top.get("similarity") or 0.0),
            "backend": str(getattr(gateway, "last_backend_used", "") or ""),
            **meta,
        }

    async def _build_behavior_klines(self, strategy_type: str, params: dict, db) -> Optional[List[dict]]:
        factory_pkg = _get_strategy_factory_package()
        cache_key = self._behavior_cache_key(strategy_type, params)
        if cache_key in self._behavior_cache:
            return self._behavior_cache[cache_key]
        build_strategy_panels = getattr(factory_pkg, "_build_strategy_panels", None)
        if not callable(build_strategy_panels):
            self._behavior_cache[cache_key] = None
            return None
        panels = await build_strategy_panels(strategy_type, params, db, sample_size=4)
        series = panels.get("strategy_returns")
        if series is None or len(series) < 30:
            self._behavior_cache[cache_key] = None
            return None
        nonzero_ratio = float(np.count_nonzero(series)) / len(series)
        if nonzero_ratio < 0.1:
            self._behavior_cache[cache_key] = None
            return None
        price = 100.0
        pseudo_klines: List[dict] = []
        for ret in np.asarray(series[-60:], dtype=np.float64):
            open_price = price
            price = open_price * (1 + float(ret))
            pseudo_klines.append({
                "open": round(open_price, 6),
                "high": round(max(open_price, price), 6),
                "low": round(min(open_price, price), 6),
                "close": round(price, 6),
                "volume": 1.0,
            })
        self._behavior_cache[cache_key] = pseudo_klines
        return pseudo_klines

    def get_last_report(self) -> dict:
        return self.last_report

    @staticmethod
    def _target_overlap(left_payload: Optional[dict], right_payload: Optional[dict]) -> Optional[float]:
        left = set(_extract_target_codes_from_payload(left_payload or {}, limit=20))
        right = set(_extract_target_codes_from_payload(right_payload or {}, limit=20))
        if not left or not right:
            return None
        union = left | right
        if not union:
            return None
        return round(len(left & right) / len(union), 4)

    @classmethod
    def _effective_similarity(cls, param_similarity: float, target_overlap: Optional[float]) -> float:
        if target_overlap is None:
            return float(param_similarity)
        return round((float(param_similarity) + float(target_overlap)) / 2.0, 4)

    @staticmethod
    def _param_sim(left: dict, right: dict) -> float:
        keys = set(left.keys()) | set(right.keys())
        if not keys:
            return 0.0
        sims: List[float] = []
        for key in keys:
            if key not in left or key not in right:
                sims.append(0.0)
                continue
            left_value = left[key]
            right_value = right[key]
            if isinstance(left_value, (int, float)) and isinstance(right_value, (int, float)):
                denom = max(abs(left_value), abs(right_value), 1e-9)
                sims.append(1.0 - abs(left_value - right_value) / denom)
            elif left_value == right_value:
                sims.append(1.0)
            else:
                sims.append(0.0)
        return float(np.mean(sims)) if sims else 0.0

