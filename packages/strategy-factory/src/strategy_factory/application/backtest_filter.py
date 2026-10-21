"""策略工厂回测初筛。"""


from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import logging
import time
from copy import deepcopy
from statistics import median
from typing import Any, Dict, List, Optional

import numpy as np

from ..api.contracts import FactoryBacktestAssumptions
from ..domain.constants import (
    BACKTEST_AI_PROTOTYPE_THRESHOLDS,
    BACKTEST_CODE_TIMEOUT_SEC,
    BACKTEST_CONCURRENCY,
    BACKTEST_CODE_CONCURRENCY,
    BACKTEST_DEFAULT_THRESHOLDS,
    FACTORY_SUBMISSION_MIN_BACKTEST_TRADES,
    BACKTEST_TYPE_THRESHOLDS,
    REPRESENTATIVE_STOCKS,
    PARAM_PERTURB_VARIANT_CAP,
    PARAM_PERTURB_BUDGET_SEC,
)
from ..domain.targets import _build_target_alignment_contract
from ..domain.targets import _extract_target_codes_from_payload
from ..domain.targets import _normalize_target_codes
from ..domain.targets import _normalize_research_task_contract
from ..infrastructure.mcp_services import get_backtest_engine_class
from .candidate_contract import (
    apply_resolved_candidate_envelope,
    build_candidate_contract_hash,
    build_factory_backtest_assumptions,
    build_portfolio_candidate_contract,
    build_tested_object_hash,
)
from .param_perturbation_study import run_parameter_perturbation_study
from .runtime import get_strategy_factory_package as _runtime_get_strategy_factory_package


logger = logging.getLogger(__name__)

def _compat_setting(name: str, default: Any) -> Any:
    return default


def _get_strategy_factory_package():
    return _runtime_get_strategy_factory_package()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _unique_reasons(reasons: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        token = str(reason or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def _has_explicit_research_task(candidate: Optional[dict[str, Any]]) -> bool:
    payload = dict(candidate or {})
    params = dict(payload.get("params") or {})
    envelope = dict(payload.get("resolved_candidate_envelope") or {})
    marker = envelope.get("had_explicit_research_task")
    if marker is None:
        marker = params.get("had_explicit_research_task")
    if marker is None:
        marker = payload.get("had_explicit_research_task")
    if marker is not None:
        return bool(marker)
    return bool(payload.get("research_task") or params.get("research_task"))


def _is_single_target_bulk_candidate_target_only(
    candidate: Optional[dict[str, Any]],
    *,
    research_task: Optional[dict[str, Any]] = None,
    validation_focus: Optional[str] = None,
    target_codes: Optional[list[str]] = None,
) -> bool:
    payload = dict(candidate or {})
    normalized_task = _normalize_research_task_contract(research_task or payload.get("research_task") or {})
    resolved_validation_focus = str(
        validation_focus if validation_focus is not None else normalized_task.get("validation_focus") or ""
    ).strip().lower()
    resolved_target_codes = list(
        target_codes
        if target_codes is not None
        else _extract_target_codes_from_payload(payload, limit=12)
    )
    return (
        str(normalized_task.get("task_source") or "").strip().lower() == "bulk_stock_matrix"
        and resolved_validation_focus == "candidate_target_only"
        and len(resolved_target_codes) == 1
    )


_TARGET_ONLY_VALIDATION_FOCUSES = frozenset({
    "candidate_target_only",
    "event_target_only",
    "target_only",
})


def _resolve_required_sample_count(
    candidate: Optional[dict[str, Any]],
    *,
    thresholds: Optional[dict[str, Any]] = None,
    research_task: Optional[dict[str, Any]] = None,
    validation_focus: Optional[str] = None,
    target_codes: Optional[list[str]] = None,
) -> int:
    payload = dict(candidate or {})
    normalized_task = _normalize_research_task_contract(research_task or payload.get("research_task") or {})
    resolved_validation_focus = str(
        validation_focus if validation_focus is not None else normalized_task.get("validation_focus") or ""
    ).strip().lower()
    resolved_target_codes = list(
        target_codes
        if target_codes is not None
        else _extract_target_codes_from_payload(payload, limit=12)
    )
    required_sample_count = max(1, int(dict(thresholds or {}).get("min_samples") or 1))
    if _is_single_target_bulk_candidate_target_only(
        payload,
        research_task=normalized_task,
        validation_focus=resolved_validation_focus,
        target_codes=resolved_target_codes,
    ):
        return 1

    if resolved_validation_focus not in _TARGET_ONLY_VALIDATION_FOCUSES or not resolved_target_codes:
        return required_sample_count

    # Target-only validation should not require more code samples than the basket
    # can actually provide. The floor still respects the target-alignment contract.
    target_sample_cap = max(1, len(resolved_target_codes))
    required_sample_count = min(required_sample_count, target_sample_cap)
    target_alignment_contract = _build_target_alignment_contract(normalized_task, candidate=payload)
    min_target_sample_count = max(0, int(target_alignment_contract.get("min_target_sample_count") or 0))
    if min_target_sample_count > 0:
        required_sample_count = max(
            required_sample_count,
            min(target_sample_cap, min_target_sample_count),
        )
    return max(1, required_sample_count)


def _preferred_target_order(candidate: Optional[dict[str, Any]]) -> list[str]:
    payload = dict(candidate or {})
    params = dict(payload.get("params") or {})
    envelope = dict(payload.get("resolved_candidate_envelope") or {})
    preferred: list[str] = []
    for source in (
        payload.get("requested_target_symbols"),
        params.get("requested_target_symbols"),
        envelope.get("requested_target_symbols"),
    ):
        preferred.extend(_normalize_target_codes(source, limit=12))
    return list(dict.fromkeys(preferred))


def _apply_preferred_code_order(codes: list[str], preferred: list[str]) -> list[str]:
    if not codes or not preferred:
        return list(codes)
    available = set(codes)
    ordered = [code for code in preferred if code in available]
    ordered.extend(code for code in codes if code not in ordered)
    return ordered


def _summarize_numeric_series(values: Any) -> dict[str, Any]:
    if not isinstance(values, (list, tuple)):
        return {}
    numeric: list[float] = []
    for item in values:
        try:
            numeric.append(float(item))
        except (TypeError, ValueError) as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/application/backtest_filter.py:192: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
    if not numeric:
        return {"points": len(list(values or []))} if values else {}
    return {
        "points": len(list(values)),
        "first": round(float(numeric[0]), 6),
        "last": round(float(numeric[-1]), 6),
        "min": round(float(min(numeric)), 6),
        "max": round(float(max(numeric)), 6),
    }


def _compact_small_mapping(value: Any, *, limit: int = 24) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, Any] = {}
    for key, item in list(value.items())[:limit]:
        if item in (None, "", [], {}):
            continue
        if isinstance(item, (str, int, float, bool)):
            compact[str(key)] = item
            continue
        if isinstance(item, dict):
            nested = {
                str(nested_key): nested_value
                for nested_key, nested_value in list(item.items())[:8]
                if isinstance(nested_value, (str, int, float, bool)) or nested_value is None
            }
            if nested:
                compact[str(key)] = nested
            else:
                compact[f"{key}_summary"] = {
                    "node_type": "dict",
                    "key_count": len(item),
                    "keys": [str(nested_key) for nested_key in list(item.keys())[:12]],
                }
            continue
        if isinstance(item, list):
            if item and all(isinstance(entry, (str, int, float, bool)) or entry is None for entry in item[:16]):
                compact[str(key)] = list(item[:16])
            compact[f"{key}_count"] = len(item)
            continue
    if len(value) > limit:
        compact["truncated_key_count"] = len(value) - limit
    return compact


def _compact_backtest_metric_payload(metric: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(metric or {})
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if key in {
            "equity_curve",
            "cash_curve",
            "gross_exposure_curve",
            "net_exposure_curve",
            "trades",
            "fills",
            "round_trip_positions",
            "component_metrics",
            "metrics",
            "event_window_metrics",
            "raw_events",
            "samples",
            "positions",
            "orders",
        }:
            continue
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            nested = _compact_small_mapping(value)
            if nested:
                compact[key] = nested
            continue
        if isinstance(value, list):
            if value and all(isinstance(item, (str, int, float, bool)) or item is None for item in value):
                compact[key] = list(value)[:16]
            compact[f"{key}_count"] = len(value)
            continue
        compact[key] = value

    for curve_key in ("equity_curve", "cash_curve", "gross_exposure_curve", "net_exposure_curve"):
        summary = _summarize_numeric_series(payload.get(curve_key))
        if summary:
            compact[f"{curve_key}_summary"] = summary
    return compact


def build_target_quality_gate_summary(
    candidate: Optional[dict],
    *,
    gate_1_metrics: Optional[dict[str, Any]] = None,
    backtest_result: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(candidate or {})
    research_task = _normalize_research_task_contract(payload.get("research_task") or {})
    target_alignment_contract = _build_target_alignment_contract(research_task, candidate=payload)
    target_codes = _extract_target_codes_from_payload(payload, limit=12)
    research_target_symbols = list(research_task.get("target_symbols") or target_codes)
    research_target_count = len(research_target_symbols)
    quality_gate_enabled = bool(target_alignment_contract.get("quality_gate_enabled"))

    constraint_check = dict(payload.get("constraint_check") or {})
    result_payload = dict(backtest_result or {})
    if isinstance(result_payload.get("constraint_check"), dict):
        constraint_check = dict(result_payload.get("constraint_check") or {})

    coverage_ratio_raw = constraint_check.get("coverage_ratio")
    intersection_ratio_raw = constraint_check.get("intersection_ratio")
    overlap_count_raw = constraint_check.get("target_overlap_count")
    coverage_ratio = None if coverage_ratio_raw is None else round(_safe_float(coverage_ratio_raw, 0.0), 4)
    intersection_ratio = None if intersection_ratio_raw is None else round(_safe_float(intersection_ratio_raw, 0.0), 4)
    overlap_count = (
        max(0, int(overlap_count_raw))
        if overlap_count_raw is not None
        else len(set(target_codes).intersection(research_target_symbols))
    )

    min_coverage_ratio = round(_safe_float(target_alignment_contract.get("min_coverage_ratio"), 0.0), 4)
    min_intersection_ratio = round(_safe_float(target_alignment_contract.get("min_intersection_ratio"), 0.0), 4)
    min_required_overlap_count = max(0, int(target_alignment_contract.get("min_required_overlap_count") or 0))
    min_target_sample_count = max(0, int(target_alignment_contract.get("min_target_sample_count") or 0))
    min_target_layer_stability = round(_safe_float(target_alignment_contract.get("min_target_layer_stability"), 0.0), 4)

    sampled_target_count = None
    target_sample_ratio = None
    target_layer_stability = None
    target_layer_dispersion = None
    target_sharpe = None
    representative_sharpe = None
    combined_sharpe = None

    gate_1_payload = dict(gate_1_metrics or {})
    if gate_1_payload:
        target_codes_payload = gate_1_payload.get("target_codes")
        if target_codes_payload is not None:
            sampled_target_count = len(list(target_codes_payload or []))
        if sampled_target_count is not None and research_target_count > 0:
            target_sample_ratio = round(sampled_target_count / research_target_count, 4)
        sharpe_values = [_safe_float(item, 0.0) for item in list(gate_1_payload.get("sharpe_values") or [])]
        if len(sharpe_values) >= 2:
            target_sharpe = round(_safe_float(gate_1_payload.get("avg_sharpe"), 0.0), 4)
            target_layer_dispersion = round(max(sharpe_values) - min(sharpe_values), 4)
            stability_denominator = max(abs(target_sharpe), 0.5) + 1.0
            target_layer_stability = round(
                max(0.0, min(1.0, 1.0 - target_layer_dispersion / stability_denominator)),
                4,
            )

    layers = dict(result_payload.get("layers") or {})
    if layers:
        target_layer = dict((layers.get("target") or {}))
        representative_layer = dict((layers.get("representative") or {}))
        combined_layer = dict((layers.get("combined") or {}))
        if sampled_target_count is None:
            sampled_target_count = len(list(target_layer.get("successful_codes") or []))
            if research_target_count > 0:
                target_sample_ratio = round(sampled_target_count / research_target_count, 4)
        target_metrics = dict(target_layer.get("metrics") or {})
        representative_metrics = dict(representative_layer.get("metrics") or {})
        combined_metrics = dict(combined_layer.get("metrics") or result_payload.get("metrics") or {})
        target_sharpe = round(_safe_float(target_metrics.get("sharpe_ratio"), 0.0), 4)
        representative_sharpe = round(_safe_float(representative_metrics.get("sharpe_ratio"), 0.0), 4)
        combined_sharpe = round(_safe_float(combined_metrics.get("sharpe_ratio"), 0.0), 4)
        stability_scale = max(abs(target_sharpe), abs(combined_sharpe), abs(representative_sharpe), 0.25)
        target_layer_dispersion = round(
            abs(target_sharpe - combined_sharpe) + abs(target_sharpe - representative_sharpe),
            4,
        )
        target_layer_stability = round(
            max(0.0, min(1.0, 1.0 - target_layer_dispersion / (stability_scale * 4.0))),
            4,
        )

    reasons: list[str] = []
    alignment_ok = True
    sample_sufficient = True
    target_layer_stable = True

    if quality_gate_enabled:
        if not target_codes:
            alignment_ok = False
            reasons.append("target_universe_alignment_too_low")
        elif coverage_ratio is not None and coverage_ratio < min_coverage_ratio:
            alignment_ok = False
            reasons.append("target_universe_alignment_too_low")
        elif intersection_ratio is not None and intersection_ratio < min_intersection_ratio:
            alignment_ok = False
            reasons.append("target_universe_alignment_too_low")
        elif min_required_overlap_count > 0 and overlap_count < min_required_overlap_count:
            alignment_ok = False
            reasons.append("target_universe_alignment_too_low")

        if sampled_target_count is not None and min_target_sample_count > 0 and sampled_target_count < min_target_sample_count:
            sample_sufficient = False
            reasons.append("target_sample_sufficiency_too_low")

        if (
            target_layer_stability is not None
            and min_target_layer_stability > 0.0
            and target_layer_stability < min_target_layer_stability
        ):
            target_layer_stable = False
            reasons.append("target_layer_stability_too_low")

    return {
        "profile": target_alignment_contract.get("profile"),
        "quality_gate_enabled": quality_gate_enabled,
        "targeted_snapshot": bool(target_alignment_contract.get("targeted_snapshot")),
        "research_target_count": research_target_count,
        "target_symbol_count": len(target_codes),
        "coverage_ratio": coverage_ratio,
        "min_coverage_ratio": min_coverage_ratio,
        "intersection_ratio": intersection_ratio,
        "min_intersection_ratio": min_intersection_ratio,
        "target_overlap_count": int(overlap_count),
        "min_required_overlap_count": min_required_overlap_count,
        "sampled_target_count": sampled_target_count,
        "min_target_sample_count": min_target_sample_count,
        "target_sample_ratio": target_sample_ratio,
        "target_layer_stability": target_layer_stability,
        "min_target_layer_stability": min_target_layer_stability,
        "target_layer_dispersion": target_layer_dispersion,
        "target_sharpe": target_sharpe,
        "representative_sharpe": representative_sharpe,
        "combined_sharpe": combined_sharpe,
        "alignment_ok": alignment_ok,
        "sample_sufficient": sample_sufficient,
        "target_layer_stable": target_layer_stable,
        "target_alignment_contract": dict(target_alignment_contract),
        "reasons": _unique_reasons(reasons),
    }


# ── 参数补充工具函数（Gate-2 使用） ──────────────────────────────────────
_TASK_META_KEYS = {
    "task_attempt_count", "task_stage_attempt_count", "candidate_local_attempt_count",
    "task_local_attempt_count", "candidate_local_selected_count", "task_local_selected_count",
    "task_network_request_count", "task_real_request_count", "task_compatibility_skip_count",
    "task_cooldown_skip_count", "task_compatibility_failure_count",
    "task_effective_response_count", "task_empty_200_response_count",
    "task_compatibility_failure_ratio", "task_effective_response_ratio",
    "task_selected_count",
}


def _ensure_strategy_params(strategy_type: str, params: dict, *, research_task: dict | None = None) -> dict:
    """确保 params 包含实际的策略参数，缺失时用 Spawner 默认值补充。

    PR-S21：如果传入了 research_task，且其中带有 stock_profile 驱动的
    ``param_search_space / profile_param_band``，优先用画像 band 的
    ``preferred`` 作为默认值，其次再用 spawner legacy default。
    这样在 candidate.params 为空（早期 LLM 失败、staged 流水线超时回退）时，
    BULK 路径仍能拿到画像差异化的默认参数，而不是统一回到 family 默认。
    """
    resolved = dict(params or {})
    strategy_params = {k: v for k, v in resolved.items() if k not in _TASK_META_KEYS}
    if not strategy_params:
        # PR-S21：先尝试从 task 的 param_search_space / profile_param_band 取 preferred
        band_params: dict = {}
        try:
            from ..domain.parameter_distribution_registry import (
                ParameterDistributionRegistry,
            )
            task = dict(research_task or {})
            band = task.get("param_search_space") or task.get("profile_param_band")
            band_sample = ParameterDistributionRegistry.sample_from_param_band(band, idx=0)
            if band_sample:
                band_params = dict(band_sample.get("params") or {})
        except Exception:
            band_params = {}

        try:
            from ..domain.spawner import StrategySpawner
            spawner = StrategySpawner.__new__(StrategySpawner)
            default_params = spawner._legacy_varied_defaults(str(strategy_type or "momentum"), 0, snapshot=None)
            merged_defaults = dict(default_params or {})
            if band_params:
                merged_defaults.update(band_params)
            if merged_defaults:
                resolved = {**merged_defaults, **resolved}
        except Exception:
            if band_params:
                resolved = {**band_params, **resolved}
    return resolved
# ── End 参数补充工具函数 ──────────────────────────────────────────────────


class BacktestFilter:
# --- backtest_filter_parts/normalizers.py ---
    """回测筛选候选策略。"""

    SHARPE_MIN = BACKTEST_DEFAULT_THRESHOLDS["sharpe_min"]
    MDD_MAX = BACKTEST_DEFAULT_THRESHOLDS["mdd_max"]
    TRADES_MIN = BACKTEST_DEFAULT_THRESHOLDS["trades_min"]
    _BACKTEST_REPORT_PREVIEW_LIMIT = 12
    _BACKTEST_RESULT_HEAVY_KEYS = frozenset(
        {
            "equity_curve",
            "cash_curve",
            "gross_exposure_curve",
            "net_exposure_curve",
            "trades",
            "fills",
            "orders",
            "positions",
            "round_trip_positions",
            "component_metrics",
            "raw_results",
            "raw_events",
            "samples",
        }
    )
    MIN_SAMPLES = BACKTEST_DEFAULT_THRESHOLDS["min_samples"]

    def __init__(self):
        self.last_report: dict = {
            "summary": {
                "input_count": 0,
                "passed_count": 0,
                "failed_count": 0,
                "strategy_type_counts": {},
                "passed_strategy_type_counts": {},
                "failed_strategy_type_counts": {},
                "failed_reason_counts": {},
                "thresholds_by_type": {},
            },
            "passed": [],
            "failed": [],
        }
        self.type_thresholds = dict(BACKTEST_TYPE_THRESHOLDS)
        self._kline_cache: dict[str, list] = {}

    async def preload_klines(self, db, codes: list[str] | None = None) -> None:
        """批量预取 K 线至缓存，后续 _test_one 复用。

        PR-S2: 单次失败重试 2 次（指数退避 0.2s/0.5s），均失败则缓存空列表，
        避免下游每次重试都重新打 DB。
        """
        representative_stocks = list(_compat_setting("REPRESENTATIVE_STOCKS", REPRESENTATIVE_STOCKS))
        code_concurrency = int(_compat_setting("BACKTEST_CODE_CONCURRENCY", BACKTEST_CODE_CONCURRENCY) or BACKTEST_CODE_CONCURRENCY)
        codes = list(dict.fromkeys(codes or representative_stocks))
        sem = asyncio.Semaphore(code_concurrency)

        async def _fetch_with_retry(code: str) -> None:
            backoffs = (0.0, 0.2, 0.5)
            last_exc: Exception | None = None
            for delay in backoffs:
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    klines = await db.get_klines(code, limit=500)
                    self._kline_cache[code] = klines or []
                    return
                except Exception as exc:
                    last_exc = exc
                    logger.debug(
                        "BacktestFilter: kline fetch attempt failed for %s (delay=%.1fs): %s",
                        code, delay, exc,
                    )
            # 3 次仍失败：缓存空列表，标记 last_exc 方便诊断
            self._kline_cache[code] = []
            logger.warning(
                "BacktestFilter: kline fetch failed after retries for %s: %s",
                code, last_exc,
            )

        async def _fetch(code: str) -> None:
            async with sem:
                await _fetch_with_retry(code)

        await asyncio.gather(*[_fetch(c) for c in codes if c not in self._kline_cache])

    def get_last_report(self) -> dict:
        return self.last_report

    @staticmethod
    def _count_by_strategy_type(items: List[dict]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in items:
            strategy_type = str(item.get("strategy_type") or "unknown")
            counts[strategy_type] = counts.get(strategy_type, 0) + 1
        return counts

    @staticmethod
    def _build_report_entry(candidate: dict) -> dict:
        return {
            "strategy_type": candidate.get("strategy_type"),
            "generator_type": candidate.get("generator_type"),
            "params": BacktestFilter._compact_json_for_report(candidate.get("params"), max_list_items=8),
            "spawn_reason": candidate.get("spawn_reason"),
            "generation_reason": BacktestFilter._compact_json_for_report(candidate.get("generation_reason") or {}),
            "target_symbols": candidate.get("target_symbols") or _extract_target_codes_from_payload(candidate),
            "stock_pool": BacktestFilter._compact_json_for_report(candidate.get("stock_pool") or {}),
            "selection_logic": BacktestFilter._compact_json_for_report(candidate.get("selection_logic") or []),
            "research_task": BacktestFilter._compact_research_task(candidate.get("research_task") or {}),
            "event_context": BacktestFilter._compact_json_for_report(candidate.get("event_context") or {}),
            "tags": list(candidate.get("tags") or [])[:16],
            "constraint_check": BacktestFilter._compact_json_for_report(candidate.get("constraint_check") or {}),
            "validation_profile": BacktestFilter._compact_json_for_report(candidate.get("validation_profile") or {}),
            "backtest_outcome": candidate.get("backtest_outcome") or {},
            "backtest_metrics": candidate.get("backtest_metrics") or {},
            "backtest_metrics_contract": candidate.get("backtest_metrics_contract") or {},
        }

    @classmethod
    def _json_size_bytes(cls, value: Any) -> int:
        try:
            return len(json.dumps(value or {}, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/application/backtest_filter.py:603: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return 0

    @classmethod
    def _json_node_summary(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {
                "storage_mode": "dropped_large_payload",
                "node_type": "dict",
                "key_count": len(value),
                "keys": [str(key) for key in list(value.keys())[:16]],
                "size_bytes": cls._json_size_bytes(value),
            }
        if isinstance(value, (list, tuple)):
            return {
                "storage_mode": "dropped_large_payload",
                "node_type": "list",
                "item_count": len(value),
                "size_bytes": cls._json_size_bytes(value),
            }
        return {
            "storage_mode": "dropped_large_payload",
            "node_type": type(value).__name__,
            "size_bytes": cls._json_size_bytes(value),
        }

    @classmethod
    def _compact_json_for_report(
        cls,
        value: Any,
        *,
        depth: int = 0,
        max_list_items: int = 12,
        max_dict_items: int = 24,
    ) -> Any:
        if value in (None, "", [], {}):
            return {} if isinstance(value, dict) else [] if isinstance(value, list) else value
        if isinstance(value, (str, int, float, bool)):
            return value
        if depth >= 3:
            return cls._json_node_summary(value)
        if isinstance(value, dict):
            compact: dict[str, Any] = {}
            for key, item in list(value.items())[:max_dict_items]:
                key_text = str(key)
                if item in (None, "", [], {}):
                    continue
                if key_text in cls._BACKTEST_RESULT_HEAVY_KEYS and isinstance(item, (dict, list, tuple)):
                    compact[f"{key_text}_summary"] = cls._json_node_summary(item)
                    continue
                compact[key_text] = cls._compact_json_for_report(
                    item,
                    depth=depth + 1,
                    max_list_items=max_list_items,
                    max_dict_items=max_dict_items,
                )
            if len(value) > max_dict_items:
                compact["truncated_key_count"] = len(value) - max_dict_items
            return compact
        if isinstance(value, (list, tuple)):
            if len(value) > max_list_items:
                preview = list(value)[:max_list_items]
            else:
                preview = list(value)
            return [
                cls._compact_json_for_report(
                    item,
                    depth=depth + 1,
                    max_list_items=max_list_items,
                    max_dict_items=max_dict_items,
                )
                for item in preview
            ] + ([{"truncated_item_count": len(value) - max_list_items}] if len(value) > max_list_items else [])
        return str(value)

    @classmethod
    def _compact_research_task(cls, task: Any) -> dict[str, Any]:
        payload = dict(task or {}) if isinstance(task, dict) else {}
        keys = (
            "task_id",
            "task_key",
            "task_source",
            "opportunity_type",
            "theme_code",
            "event_id",
            "candidate_family",
            "factor_name",
            "validation_focus",
            "target_symbols",
        )
        compact = {key: payload.get(key) for key in keys if payload.get(key) not in (None, "", [], {})}
        if "target_symbols" in compact and isinstance(compact["target_symbols"], list):
            compact["target_symbols"] = list(compact["target_symbols"])[:12]
            compact["target_symbol_count"] = len(payload.get("target_symbols") or [])
        return compact

    @classmethod
    def _compact_event_window_metrics(cls, metrics: Any) -> dict[str, Any]:
        payload = dict(metrics or {}) if isinstance(metrics, dict) else {}
        compact: dict[str, Any] = {}
        scalar_keys = (
            "event_study_mode",
            "event_sample_count",
            "event_anchor_count",
            "control_group_count",
            "event_sample_source",
            "traceable_to_event_samples",
            "event_audit_incomplete",
            "bhar",
            "abnormal_return",
            "total_return",
            "post_event_decay",
            "hit_ratio",
            "estimation_days_used",
            "post_days_used",
        )
        for key in scalar_keys:
            value = payload.get(key)
            if value not in (None, "", [], {}):
                compact[key] = value
        anchors = payload.get("event_time_anchors")
        if isinstance(anchors, list):
            compact["event_time_anchors"] = list(anchors[:8])
            compact["event_time_anchor_count"] = len(anchors)
        return compact

    @classmethod
    def _compact_backtest_result_payload(cls, result: Any) -> dict[str, Any]:
        payload = dict(result or {}) if isinstance(result, dict) else {}
        compact: dict[str, Any] = {}
        scalar_keys = (
            "passed",
            "reason_code",
            "reason",
            "strategy_type",
            "sample_count",
            "required_sample_count",
            "evaluated_code_count",
            "successful_code_count",
            "code_source",
            "primary_layer",
            "primary_validation_layer",
            "validation_focus",
            "candidate_contract_hash",
            "execution_contract_hash",
            "tested_object_hash",
            "candidate_identity_signature",
            "queue_wait_ms",
            "backtest_run_ms",
            "code_run_ms_total",
            "code_run_count",
            "avg_code_ms",
            "kline_cache_hit_count",
            "portfolio_backtest_mode",
            "portfolio_backtest_coverage",
            "implementation_shortfall_model_source",
            "position_assumption",
            "backtest_metrics_contract_status",
        )
        for key in scalar_keys:
            value = payload.get(key)
            if value not in (None, "", [], {}):
                compact[key] = value
        for key in ("evaluated_codes", "successful_codes", "target_codes", "representative_codes"):
            values = list(payload.get(key) or [])
            if values:
                compact[key] = values[:24]
                compact[f"{key}_count"] = len(values)
        for key in ("skipped_codes", "failed_codes"):
            values = [dict(item or {}) for item in list(payload.get(key) or []) if isinstance(item, dict)]
            if values:
                compact[key] = [
                    cls._compact_json_for_report(item, max_list_items=4, max_dict_items=8)
                    for item in values[: cls._BACKTEST_REPORT_PREVIEW_LIMIT]
                ]
                compact[f"{key}_count"] = len(values)
        for key in (
            "thresholds",
            "constraint_check",
            "target_quality_summary",
            "event_window_config",
            "contamination_summary",
            "cost_assumptions",
            "explicit_cost_breakdown",
            "implicit_cost_breakdown",
            "tradability_summary",
            "capacity_summary",
            "implementation_shortfall_components",
            "backtest_assumptions",
            "economic_semantics",
            "failed_metric",
            "backtest_metrics_contract",
        ):
            value = payload.get(key)
            if value not in (None, "", [], {}):
                compact[key] = cls._compact_json_for_report(value)
        metrics = _compact_backtest_metric_payload(payload.get("metrics") or {})
        if metrics:
            compact["metrics"] = metrics
        layers = dict(payload.get("layers") or {})
        if layers:
            compact_layers: dict[str, Any] = {}
            for layer_name, layer_payload in layers.items():
                layer = dict(layer_payload or {})
                compact_layers[str(layer_name)] = {
                    "requested_codes": list(layer.get("requested_codes") or [])[:24],
                    "requested_code_count": len(list(layer.get("requested_codes") or [])),
                    "successful_codes": list(layer.get("successful_codes") or [])[:24],
                    "successful_code_count": len(list(layer.get("successful_codes") or [])),
                    "sample_count": int(layer.get("sample_count") or 0),
                    "metrics": _compact_backtest_metric_payload(layer.get("metrics") or {}),
                    "trade_profile": cls._compact_json_for_report(layer.get("trade_profile") or {}),
                    "metrics_source": layer.get("metrics_source"),
                }
            compact["layers"] = compact_layers
        event_metrics = cls._compact_event_window_metrics(payload.get("event_window_metrics") or {})
        if event_metrics:
            compact["event_window_metrics"] = event_metrics
        dropped = sorted(key for key in payload.keys() if key in cls._BACKTEST_RESULT_HEAVY_KEYS)
        if dropped:
            compact["dropped_heavy_fields"] = dropped
        return compact

    @staticmethod
    def _build_failed_metric(field: str, operator: str, threshold: Any, actual: Any, label: str) -> dict:
        return {
            "field": field,
            "operator": operator,
            "threshold": threshold,
            "actual": actual,
            "label": label,
        }

    def _get_thresholds(self, strategy_type: str, candidate: Optional[dict] = None) -> dict:
        thresholds = {
            **dict(_compat_setting("BACKTEST_DEFAULT_THRESHOLDS", BACKTEST_DEFAULT_THRESHOLDS)),
            **(dict(_compat_setting("BACKTEST_TYPE_THRESHOLDS", BACKTEST_TYPE_THRESHOLDS)).get(strategy_type) or {}),
        }
        candidate = dict(candidate or {})
        tags = {str(tag).strip().lower() for tag in list(candidate.get("tags") or [])}
        generator_type = str(candidate.get("generator_type") or "").strip().lower()
        has_parent_strategy = bool(str(candidate.get("parent_strategy_id") or "").strip())
        if (
            strategy_type == "dsl_rule"
            or generator_type in {"external_llm", "llm_proxy", "pipeline_staged", "rl_bandit"}
            or "external_llm" in tags
            or "ai_generated" in tags
            or "llm_proxy_fallback" in tags
            or has_parent_strategy
        ):
            thresholds = {**thresholds, **dict(_compat_setting("BACKTEST_AI_PROTOTYPE_THRESHOLDS", BACKTEST_AI_PROTOTYPE_THRESHOLDS))}
        submission_trade_floor = self._resolve_submission_trade_count_floor(candidate)
        if submission_trade_floor > 0:
            thresholds["trades_min"] = max(float(thresholds.get("trades_min") or 0.0), submission_trade_floor)
        return thresholds

    @classmethod
    def _resolve_submission_trade_count_floor(cls, candidate: Optional[dict] = None) -> float:
        payload = apply_resolved_candidate_envelope(candidate or {})
        research_task = _normalize_research_task_contract(payload.get("research_task") or {})
        validation_focus = str(research_task.get("validation_focus") or "").strip().lower()
        target_codes = _extract_target_codes_from_payload(payload, limit=12)
        if not _is_single_target_bulk_candidate_target_only(
            payload,
            research_task=research_task,
            validation_focus=validation_focus,
            target_codes=target_codes,
        ):
            return 0.0

        trade_floor = float(FACTORY_SUBMISSION_MIN_BACKTEST_TRADES)
        try:
            from .submission_gate import (
                _resolve_validation_profile,
                _trade_gate_thresholds,
                resolve_attempt_adjustment,
            )

            profile = _resolve_validation_profile(payload)
            # `_trade_gate_thresholds(strategy, profile, attempt_adjustment, *,
            # admission_level)` takes three positional arguments. This call passed
            # only the profile, so it raised `missing 2 required positional
            # arguments` on every evaluation and the handler swallowed it — leaving
            # `trade_floor` at the generic default. The profile-specific floor was
            # therefore never applied, which silently *loosened* the submission gate
            # this function exists to compute. Use it the way `_evaluate_trade_profile`
            # does.
            attempt_adjustment = resolve_attempt_adjustment(payload)
            trade_floor = max(
                trade_floor,
                float(
                    _trade_gate_thresholds(
                        payload, profile, attempt_adjustment, admission_level="incubation"
                    ).get("trade_count_min")
                    or 0.0
                ),
            )
        except Exception as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/application/backtest_filter.py:882: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
        return max(0.0, trade_floor)

    @classmethod
    def _collect_preload_codes(cls, candidates: List[dict]) -> List[str]:
        ordered_codes: List[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            evaluated_codes, _, _, _, _ = cls._resolve_backtest_plan(candidate)
            for code in evaluated_codes:
                if code in seen:
                    continue
                seen.add(code)
                ordered_codes.append(code)
        return ordered_codes

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): BacktestFilter._json_safe(item)
                for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
            }
        if isinstance(value, (list, tuple, set)):
            return [BacktestFilter._json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _build_shared_result_key(self, candidate: dict) -> str:
        strategy_type = str(candidate.get("strategy_type") or "unknown").strip().lower() or "unknown"
        research_task = _normalize_research_task_contract(candidate.get("research_task") or {})
        evaluated_codes, target_codes, representative_codes, code_source, validation_focus = self._resolve_backtest_plan(candidate)
        contract_hash = build_candidate_contract_hash(candidate)
        sanitized_params = dict(candidate.get("params") or {})
        for key in (
            "candidate_contract_hash",
            "candidate_contract_snapshot",
            "candidate_identity_signature",
            "candidate_lineage_contract",
            "dsl_signature",
            "entry_exit_signature",
            "execution_contract_hash",
            "factor_signature",
            "logic_signature",
            "legacy_identity_partial",
            "resolved_candidate_envelope",
            "tested_object_hash",
            "tested_object_backfill_incomplete",
            "research_task",
            "requested_target_symbols",
            "target_symbols",
            "stock_pool",
            "had_explicit_research_task",
        ):
            sanitized_params.pop(key, None)
        signature_payload = {
            "candidate_contract_hash": contract_hash,
            "strategy_type": strategy_type,
            "params": sanitized_params,
            "research_task": research_task,
            "target_codes": target_codes,
            "representative_codes": representative_codes,
            "evaluated_codes": evaluated_codes,
            "code_source": code_source,
            "validation_focus": validation_focus,
            "execution_assumptions": dict(candidate.get("execution_assumptions") or {}),
            "portfolio_spec": dict(candidate.get("portfolio_spec") or {}),
            "generator_type": str(candidate.get("generator_type") or "").strip().lower(),
            "tags": sorted(
                str(tag).strip().lower()
                for tag in list(candidate.get("tags") or [])
                if str(tag).strip()
            ),
            "parent_strategy_id": str(candidate.get("parent_strategy_id") or "").strip(),
            "thresholds": self._get_thresholds(strategy_type, candidate),
        }
        serialized = json.dumps(
            self._json_safe(signature_payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"shared:{hashlib.sha1(serialized.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _build_candidate_exception_result(candidate: dict, error: BaseException) -> dict:
        return {
            "passed": False,
            "reason_code": "candidate_exception",
            "reason": f"候选策略回测异常: {type(error).__name__}",
            "strategy_type": candidate.get("strategy_type") or "unknown",
            "sample_count": 0,
            "required_sample_count": 0,
            "evaluated_code_count": 0,
            "successful_code_count": 0,
            "evaluated_codes": [],
            "successful_codes": [],
            "target_codes": _extract_target_codes_from_payload(candidate),
            "representative_codes": [],
            "code_source": "candidate_exception",
            "primary_layer": "none",
            "queue_wait_ms": 0.0,
            "backtest_run_ms": 0.0,
            "code_run_ms_total": 0.0,
            "code_run_count": 0,
            "failed_metrics": [],
            "failed_codes": [],
            "skipped_codes": [],
            "metrics": {},
            "error": f"{type(error).__name__}: {error}",
        }

    @staticmethod
    def _annotate_shared_result(candidate: dict, result: dict, *, key: str, reused: bool, reuse_count: int) -> dict:
        payload = deepcopy(dict(result or {}))
        payload["constraint_check"] = dict(candidate.get("constraint_check") or payload.get("constraint_check") or {})
        payload["shared_result_key"] = key
        payload["shared_result_reused"] = bool(reused)
        payload["shared_result_reuse_count"] = max(0, int(reuse_count or 0))
        return payload

    def _apply_result_to_candidate(
        self,
        candidate: dict,
        result: dict,
        passed: List[dict],
        failed: List[dict],
    ) -> None:
        compact_result = self._compact_backtest_result_payload(result)
        candidate["backtest_outcome"] = compact_result
        candidate.pop("backtest_result", None)
        derived_trade_metrics = self._derive_trade_validation_metrics(candidate, result)
        metrics_contract = self._build_backtest_metrics_contract(candidate, result)
        for key, value in derived_trade_metrics.items():
            if value not in (None, "", [], {}):
                metrics_contract[key] = value
        # Keep explicit stability field even when upstream study is absent so
        # downstream contracts can distinguish "missing study" (None) from
        # "field omitted by compaction".
        if "parameter_perturbation_trade_stability" not in metrics_contract:
            metrics_contract["parameter_perturbation_trade_stability"] = derived_trade_metrics.get(
                "parameter_perturbation_trade_stability"
            )
        candidate["backtest_metrics_contract"] = metrics_contract
        compact_event_metrics = self._compact_event_window_metrics(result.get("event_window_metrics") or {})
        candidate["backtest_metrics"] = {
            **dict(result.get("metrics") or {}),
            "constraint_check": dict(result.get("constraint_check") or {}),
            "target_quality_summary": dict(result.get("target_quality_summary") or {}),
            "validation_focus": result.get("validation_focus"),
            "primary_validation_layer": result.get("primary_validation_layer"),
            "event_window_config": dict(result.get("event_window_config") or {}),
            "contamination_summary": dict(result.get("contamination_summary") or {}),
            "cost_assumptions": dict(result.get("cost_assumptions") or {}),
            "explicit_cost_breakdown": dict(result.get("explicit_cost_breakdown") or {}),
            "implicit_cost_breakdown": dict(result.get("implicit_cost_breakdown") or {}),
            "tradability_summary": dict(result.get("tradability_summary") or {}),
            "capacity_summary": dict(result.get("capacity_summary") or {}),
            "implementation_shortfall_model_source": result.get("implementation_shortfall_model_source"),
            "implementation_shortfall_components": dict(result.get("implementation_shortfall_components") or {}),
            "position_assumption": result.get("position_assumption"),
            "execution_summary": dict(result.get("execution_summary") or {}),
            "cash_curve_summary": _summarize_numeric_series(result.get("cash_curve")),
            "gross_exposure_curve_summary": _summarize_numeric_series(result.get("gross_exposure_curve")),
            "net_exposure_curve_summary": _summarize_numeric_series(result.get("net_exposure_curve")),
            "target_layer_metrics": _compact_backtest_metric_payload(
                ((result.get("layers") or {}).get("target") or {}).get("metrics") or {}
            ),
            "representative_layer_metrics": _compact_backtest_metric_payload(
                ((result.get("layers") or {}).get("representative") or {}).get("metrics") or {}
            ),
            "combined_layer_metrics": _compact_backtest_metric_payload(
                ((result.get("layers") or {}).get("combined") or {}).get("metrics") or {}
            ),
            "event_window_metrics": compact_event_metrics,
            "target_layer_oos_return": float((((result.get("layers") or {}).get("target") or {}).get("metrics") or {}).get("total_return") or 0.0),
            "post_cost_sharpe": float((result.get("metrics") or {}).get("sharpe_ratio") or 0.0),
            "backtest_assumptions": dict(result.get("backtest_assumptions") or {}),
            "backtest_metrics_contract": metrics_contract,
            "backtest_metrics_contract_status": metrics_contract.get("status"),
            **derived_trade_metrics,
        }
        candidate["backtest_metrics"] = _compact_backtest_metric_payload(candidate.get("backtest_metrics") or {})
        if compact_event_metrics:
            candidate["backtest_metrics"]["event_window_metrics"] = compact_event_metrics
        candidate["backtest_metrics"]["backtest_metrics_contract"] = metrics_contract
        candidate["backtest_metrics"]["backtest_metrics_contract_status"] = metrics_contract.get("status")
        for key, value in derived_trade_metrics.items():
            if value not in (None, "", [], {}):
                candidate["backtest_metrics"][key] = value
        if "parameter_perturbation_trade_stability" not in candidate["backtest_metrics"]:
            candidate["backtest_metrics"]["parameter_perturbation_trade_stability"] = derived_trade_metrics.get(
                "parameter_perturbation_trade_stability"
            )
        if result.get("passed"):
            passed.append(candidate)
        else:
            failed.append(candidate)

    def _build_last_report(self, candidates: List[dict], passed: List[dict], failed: List[dict]) -> dict:
        failed_reason_counts: Dict[str, int] = {}
        thresholds_by_type: Dict[str, dict] = {}
        candidate_run_ms_total = 0.0
        code_run_ms_total = 0.0
        code_run_count = 0
        cache_hit_total = 0
        evaluated_code_total = 0
        shared_result_reused_count = 0
        shared_result_keys: set[str] = set()
        for item in candidates:
            strategy_type = str(item.get("strategy_type") or "unknown")
            result = item.get("backtest_outcome") or {}
            thresholds_by_type[strategy_type] = result.get("thresholds") or self._get_thresholds(strategy_type, item)
            candidate_run_ms_total += float(result.get("backtest_run_ms") or 0.0)
            code_run_ms_total += float(result.get("code_run_ms_total") or 0.0)
            code_run_count += int(result.get("code_run_count") or 0)
            cache_hit_total += int(result.get("kline_cache_hit_count") or 0)
            evaluated_code_total += int(result.get("evaluated_code_count") or 0)
            shared_result_reused_count += int(bool(result.get("shared_result_reused")))
            if int(result.get("shared_result_reuse_count") or 0) > 0 and str(result.get("shared_result_key") or "").strip():
                shared_result_keys.add(str(result.get("shared_result_key")))
        for item in failed:
            reason_code = str((item.get("backtest_outcome") or {}).get("reason_code") or "unknown")
            failed_reason_counts[reason_code] = failed_reason_counts.get(reason_code, 0) + 1
        candidate_count = len(candidates)
        return {
            "summary": {
                "input_count": candidate_count,
                "passed_count": len(passed),
                "failed_count": len(failed),
                "strategy_type_counts": self._count_by_strategy_type(candidates),
                "passed_strategy_type_counts": self._count_by_strategy_type(passed),
                "failed_strategy_type_counts": self._count_by_strategy_type(failed),
                "failed_reason_counts": failed_reason_counts,
                "thresholds_by_type": thresholds_by_type,
                "avg_candidate_ms": round(candidate_run_ms_total / candidate_count, 2) if candidate_count else 0.0,
                "avg_code_ms": round(code_run_ms_total / code_run_count, 2) if code_run_count else 0.0,
                "cache_hit_ratio": round(cache_hit_total / evaluated_code_total, 4) if evaluated_code_total else 0.0,
                "shared_result_reused_count": shared_result_reused_count,
                "shared_result_group_count": len(shared_result_keys),
            },
            "diagnostics": {
                "passed_preview": [
                    self._build_report_entry(item)
                    for item in passed[: self._BACKTEST_REPORT_PREVIEW_LIMIT]
                ],
                "failed_preview": [
                    self._build_report_entry(item)
                    for item in failed[: self._BACKTEST_REPORT_PREVIEW_LIMIT]
                ],
                "passed_preview_count": min(len(passed), self._BACKTEST_REPORT_PREVIEW_LIMIT),
                "failed_preview_count": min(len(failed), self._BACKTEST_REPORT_PREVIEW_LIMIT),
            },
        }

    async def filter(self, candidates: List[dict], db) -> List[dict]:
        for candidate in candidates:
            resolved_candidate = apply_resolved_candidate_envelope(candidate)
            candidate.clear()
            candidate.update(resolved_candidate)
        BacktestEngine = get_backtest_engine_class()
        passed: List[dict] = []
        failed: List[dict] = []
        candidate_concurrency = int(_compat_setting("BACKTEST_CONCURRENCY", BACKTEST_CONCURRENCY) or BACKTEST_CONCURRENCY)
        sem = asyncio.Semaphore(candidate_concurrency)
        preload_codes = self._collect_preload_codes(candidates)
        if preload_codes:
            await self.preload_klines(db, preload_codes)
        shared_key_by_candidate: dict[int, str] = {}
        shared_groups: dict[str, list[dict]] = {}
        shared_group_order: list[str] = []
        for candidate in candidates:
            shared_key = self._build_shared_result_key(candidate)
            shared_key_by_candidate[id(candidate)] = shared_key
            if shared_key not in shared_groups:
                shared_groups[shared_key] = []
                shared_group_order.append(shared_key)
            shared_groups[shared_key].append(candidate)
        unique_candidates = [shared_groups[key][0] for key in shared_group_order]

        async def _test_guarded(candidate: dict) -> tuple:
            queued_at = time.perf_counter()
            async with sem:
                started_at = time.perf_counter()
                result = await self._test_one(candidate, db, BacktestEngine)
                result["queue_wait_ms"] = round((started_at - queued_at) * 1000, 2)
                return candidate, result

        results = await asyncio.gather(
            *[_test_guarded(c) for c in unique_candidates],
            return_exceptions=True,
        )
        shared_results: dict[str, dict] = {}
        for candidate, item in zip(unique_candidates, results):
            shared_key = shared_key_by_candidate[id(candidate)]
            reuse_count = max(len(shared_groups.get(shared_key) or []) - 1, 0)
            if isinstance(item, BaseException):
                logger.warning(
                    "BacktestFilter candidate failed unexpectedly strategy_type=%s error=%s",
                    candidate.get("strategy_type"),
                    item,
                    exc_info=item,
                )
                result = self._build_candidate_exception_result(candidate, item)
            else:
                _, result = item
            shared_results[shared_key] = self._annotate_shared_result(
                candidate,
                result,
                key=shared_key,
                reused=False,
                reuse_count=reuse_count,
            )
        for candidate in candidates:
            shared_key = shared_key_by_candidate[id(candidate)]
            leader = (shared_groups.get(shared_key) or [candidate])[0]
            result = shared_results.get(shared_key)
            if result is None:
                result = self._annotate_shared_result(
                    candidate,
                    self._build_candidate_exception_result(candidate, RuntimeError("missing_shared_result")),
                    key=shared_key,
                    reused=False,
                    reuse_count=0,
                )
            elif candidate is not leader:
                result = self._annotate_shared_result(
                    candidate,
                    result,
                    key=shared_key,
                    reused=True,
                    reuse_count=max(len(shared_groups.get(shared_key) or []) - 1, 0),
                )
            self._apply_result_to_candidate(candidate, result, passed, failed)
        self.last_report = self._build_last_report(candidates, passed, failed)
        return passed

    @staticmethod
    def _build_median_summary(results: List[dict]) -> dict:
        if not results:
            return {}
        win_rates = [
            float(metric["win_rate"])
            for metric in results
            if metric.get("win_rate") is not None
        ]
        avg_holding_days = [
            float(metric["avg_holding_days"])
            for metric in results
            if metric.get("avg_holding_days") is not None
        ]
        turnover_values = [
            float(metric["turnover_proxy"])
            for metric in results
            if metric.get("turnover_proxy") is not None
        ]
        curve_points = [
            int(metric.get("curve_points") or len(metric.get("equity_curve") or []))
            for metric in results
            if int(metric.get("curve_points") or len(metric.get("equity_curve") or [])) > 0
        ]
        return {
            "sharpe_ratio": float(median([metric["sharpe_ratio"] for metric in results])),
            "total_return": float(median([metric["total_return"] for metric in results])),
            "max_drawdown": float(median([metric["max_drawdown"] for metric in results])),
            # None = unmeasurable (no closed round trip) and must not mix into
            # the median as 0.0; fall back to unmeasurable when nothing is set.
            "win_rate": float(median(win_rates)) if win_rates else None,
            "trades_count": float(median([metric["trades_count"] for metric in results])),
            "avg_holding_days": float(median(avg_holding_days)) if avg_holding_days else 0.0,
            "turnover_proxy": float(median(turnover_values)) if turnover_values else 0.0,
            "curve_points": int(max(curve_points)) if curve_points else 0,
        }

    @staticmethod
    def _filter_target_weight_map_for_codes(
        target_weight_map: Optional[dict[str, Any]],
        codes: List[str],
    ) -> dict[str, float]:
        if not isinstance(target_weight_map, dict):
            return {}
        filtered: dict[str, float] = {}
        for code in list(codes or []):
            token = str(code or "").strip()
            if not token or token not in target_weight_map:
                continue
            try:
                value = float(target_weight_map.get(token, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                filtered[token] = value
        return filtered
# --- backtest_filter_parts/policy.py ---

    async def _run_portfolio_engine_summary(
        self,
        *,
        candidate: dict,
        engine,
        codes: List[str],
        assumptions: FactoryBacktestAssumptions,
    ) -> Optional[dict[str, Any]]:
        if not _has_explicit_research_task(candidate):
            return None
        portfolio_runner = getattr(engine, "run_portfolio_backtest", None)
        if not callable(portfolio_runner):
            return None
        normalized_codes = [str(code or "").strip() for code in list(codes or []) if str(code or "").strip()]
        if len(normalized_codes) <= 1:
            return None

        market_data: dict[str, list] = {}
        for code in normalized_codes:
            klines = list(self._kline_cache.get(code) or [])
            if klines:
                market_data[code] = klines
        if len(market_data) <= 1:
            return None

        portfolio_params = {
            **dict(candidate.get("params") or {}),
            **assumptions.to_backtest_kwargs(),
        }
        filtered_weight_map = self._filter_target_weight_map_for_codes(
            assumptions.target_weight_map,
            list(market_data.keys()),
        )
        if filtered_weight_map:
            portfolio_params["target_weight_map"] = filtered_weight_map
        elif portfolio_params.get("target_weight_scheme") == "target_weight_map":
            portfolio_params["target_weight_scheme"] = "equal_weight"

        try:
            factory_pkg = _get_strategy_factory_package()
            to_thread = getattr(getattr(factory_pkg, "asyncio", None), "to_thread", asyncio.to_thread)
            result = await to_thread(
                portfolio_runner,
                market_data,
                candidate["strategy_type"],
                portfolio_params,
                True,
            )
        except Exception:
            logger.warning(
                "BacktestFilter portfolio engine failed strategy_type=%s codes=%s",
                candidate.get("strategy_type"),
                list(market_data.keys()),
                exc_info=True,
            )
            return None
        if not isinstance(result, dict) or not result.get("success"):
            return None
        payload = dict(result.get("data") or {})
        payload = self._merge_trade_profile_metrics(payload)
        payload["portfolio_engine_used"] = True
        payload.setdefault("component_count", len(market_data))
        payload.setdefault("component_codes", list(market_data.keys()))
        return payload

    async def _run_parameter_perturbation_study(
        self,
        candidate: dict,
        engine,
        *,
        primary_code: str,
        strategy_type: str,
        assumptions_kwargs: dict,
        baseline_metrics: Optional[dict],
    ) -> Optional[dict[str, Any]]:
        """Run the bounded parameter-perturbation study for one primary code.

        Returns the three statistic fields plus a ``parameter_perturbation_study``
        provenance dict to merge into the candidate metrics, or None when the
        candidate went through the portfolio engine (no per-code baseline).
        Fail-closed: missing klines / no numeric params / all variant runs
        failed produce None-valued stats with a stable reason, never 0.
        """
        klines = list(self._kline_cache.get(primary_code) or [])
        params = dict(candidate.get("params") or {})
        params.update(assumptions_kwargs)
        baseline_result = {
            "success": True,
            "data": dict(baseline_metrics or {}),
        }
        study = await run_parameter_perturbation_study(
            engine=engine,
            code=primary_code,
            klines=klines,
            strategy_type=strategy_type,
            base_params=params,
            assumptions_kwargs={},
            baseline_result=baseline_result,
            variant_cap=PARAM_PERTURB_VARIANT_CAP,
            budget_sec=PARAM_PERTURB_BUDGET_SEC,
            code_timeout_sec=BACKTEST_CODE_TIMEOUT_SEC,
        )
        return {
            "parameter_perturbation_trade_stability": study.get(
                "parameter_perturbation_trade_stability"
            ),
            "parameter_perturbation_trade_count_correlation": study.get(
                "parameter_perturbation_trade_count_correlation"
            ),
            "parameter_perturbation_direction_agreement": study.get(
                "parameter_perturbation_direction_agreement"
            ),
            "parameter_perturbation_study": study.get("parameter_perturbation_study"),
        }

    @staticmethod
    def _coerce_equity_curve(metric: dict) -> Optional[np.ndarray]:
        raw_curve = metric.get("equity_curve")
        if not isinstance(raw_curve, (list, tuple)):
            return None
        try:
            curve = np.asarray(raw_curve, dtype=float)
        except (TypeError, ValueError) as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/application/backtest_filter.py:1344: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return None
        if curve.ndim != 1 or curve.size < 2:
            return None
        if not np.all(np.isfinite(curve)) or float(curve[0]) <= 0 or np.any(curve <= 0):
            return None
        return curve

    @staticmethod
    def _resample_curve(curve: np.ndarray, target_len: int) -> np.ndarray:
        if curve.size == target_len:
            return curve.astype(float, copy=True)
        if target_len <= 1:
            return np.asarray([float(curve[-1])], dtype=float)
        source_x = np.linspace(0.0, 1.0, curve.size)
        target_x = np.linspace(0.0, 1.0, target_len)
        return np.interp(target_x, source_x, curve).astype(float, copy=False)

    @staticmethod
    def _weighted_average(values: List[float], weights: Optional[List[float]] = None) -> float:
        if not values:
            return 0.0
        if not weights or len(weights) != len(values) or sum(weights) <= 0:
            return float(sum(values) / len(values))
        total_weight = float(sum(weights))
        return float(sum(value * weight for value, weight in zip(values, weights)) / total_weight)

    @staticmethod
    def _normalize_weight_scheme(target_weight_scheme: str) -> str:
        normalized = str(target_weight_scheme or "single_name").strip().lower()
        if normalized in {"equal", "equal_weight_proxy"}:
            return "equal_weight"
        if not normalized:
            return "single_name"
        return normalized

    @staticmethod
    def _normalize_target_weight_map(
        codes: List[str],
        target_weight_map: Optional[dict[str, Any]],
    ) -> dict[str, float]:
        normalized_codes = [str(code or "").strip() for code in list(codes or []) if str(code or "").strip()]
        if not normalized_codes:
            return {}
        raw_map = dict(target_weight_map or {})
        weights: dict[str, float] = {}
        for code in normalized_codes:
            try:
                value = float(raw_map.get(code, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                weights[code] = value
        total = float(sum(weights.values()))
        if total <= 0:
            equal_weight = 1.0 / float(len(normalized_codes))
            return {code: equal_weight for code in normalized_codes}
        return {
            code: float(value) / total
            for code, value in weights.items()
        }

    @classmethod
    def _resolve_portfolio_allocation(
        cls,
        results: List[dict],
        *,
        target_weight_scheme: str,
        target_weight_map: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, float], str]:
        normalized_scheme = cls._normalize_weight_scheme(target_weight_scheme)
        codes = [
            str(metric.get("code") or "").strip()
            for metric in results
            if str(metric.get("code") or "").strip()
        ]
        if not codes:
            return {}, normalized_scheme
        if normalized_scheme == "single_name":
            return {codes[0]: 1.0}, normalized_scheme
        normalized_map = cls._normalize_target_weight_map(codes, target_weight_map)
        if not normalized_map:
            return {}, normalized_scheme
        return normalized_map, normalized_scheme

    @classmethod
    def _build_portfolio_curve_payload(
        cls,
        results: List[dict],
        *,
        target_weight_scheme: str,
        initial_capital: float,
        target_weight_map: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        if len(results) <= 1:
            return None

        allocation_weights, normalized_scheme = cls._resolve_portfolio_allocation(
            results,
            target_weight_scheme=target_weight_scheme,
            target_weight_map=target_weight_map,
        )
        if len(allocation_weights) <= 1:
            return None

        entries: List[tuple[str, np.ndarray, dict]] = []
        for metric in results:
            code = str(metric.get("code") or "").strip()
            if not code or code not in allocation_weights:
                continue
            curve = cls._coerce_equity_curve(metric)
            if curve is None:
                continue
            entries.append((code, curve / float(curve[0]), metric))
        if len(entries) <= 1:
            return None

        target_len = max(curve.size for _, curve, _ in entries)
        aggregated_curve = np.zeros(target_len, dtype=float)
        used_metrics: List[dict] = []
        used_weights: List[float] = []
        allocation_snapshot: dict[str, float] = {}
        for code, curve, metric in entries:
            weight = float(allocation_weights.get(code, 0.0) or 0.0)
            if weight <= 0:
                continue
            aggregated_curve += cls._resample_curve(curve, target_len) * weight
            allocation_snapshot[code] = round(weight, 6)
            used_metrics.append(metric)
            used_weights.append(weight)
        if len(used_metrics) <= 1:
            return None

        capital_base = max(float(initial_capital or 0.0), 1.0)
        aggregated_curve = aggregated_curve * capital_base
        allocation_mode = (
            "target_weight_map"
            if target_weight_map and normalized_scheme != "equal_weight"
            else "equal_weight"
        )
        aggregation_mode = (
            "portfolio_equal_weight"
            if allocation_mode == "equal_weight"
            else "portfolio_weighted"
        )
        return {
            "curve": aggregated_curve.astype(float, copy=False),
            "aggregation_mode": aggregation_mode,
            "allocation_mode": allocation_mode,
            "allocation_weights": allocation_snapshot,
            "component_count": len(used_metrics),
            "curve_points": int(target_len),
            "metrics": used_metrics,
            "weights": used_weights,
            "requested_weight_scheme": normalized_scheme,
        }

    @staticmethod
    def _metrics_from_equity_curve(curve: np.ndarray) -> dict:
        if curve.size < 2 or float(curve[0]) <= 0:
            return {
                "total_return": 0.0,
                "max_drawdown": 0.0,
                "sharpe_ratio": 0.0,
            }
        total_return = float(curve[-1] / curve[0] - 1.0)
        peaks = np.maximum.accumulate(curve)
        drawdowns = np.where(peaks > 0, (peaks - curve) / peaks, 0.0)
        max_drawdown = float(np.max(drawdowns)) if drawdowns.size else 0.0
        sharpe_ratio = 0.0
        prev = curve[:-1]
        curr = curve[1:]
        valid = prev > 0
        if np.any(valid):
            returns = (curr[valid] - prev[valid]) / prev[valid]
            returns = returns[np.isfinite(returns)]
            if returns.size > 1:
                std = float(np.std(returns))
                if std > 0:
                    annual_return = float(np.mean(returns)) * 252.0
                    annual_std = std * np.sqrt(252.0)
                    sharpe_ratio = float((annual_return - 0.02) / annual_std)
        return {
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe_ratio,
        }

    @classmethod
    def _summarize_portfolio_result_set(
        cls,
        results: List[dict],
        *,
        target_weight_scheme: str,
        initial_capital: float,
        target_weight_map: Optional[dict[str, Any]] = None,
    ) -> Optional[dict]:
        payload = cls._build_portfolio_curve_payload(
            results,
            target_weight_scheme=target_weight_scheme,
            initial_capital=initial_capital,
            target_weight_map=target_weight_map,
        )
        if payload is None:
            return None

        curve_metrics = list(payload.get("metrics") or [])
        portfolio_curve = np.asarray(payload["curve"], dtype=float)
        portfolio_metrics = cls._metrics_from_equity_curve(portfolio_curve)
        component_trade_counts = [max(float(metric.get("trades_count") or 0.0), 0.0) for metric in curve_metrics]
        trade_weights = component_trade_counts if sum(component_trade_counts) > 0 else None
        avg_holding_days = [
            float(metric.get("avg_holding_days") or 0.0)
            for metric in curve_metrics
        ]
        win_rates = [
            float(metric["win_rate"])
            for metric in curve_metrics
            if metric.get("win_rate") is not None
        ]
        if not win_rates:
            # all components unmeasurable: keep None, do not fabricate 0.0
            win_rates = None
        turnover_values = [
            float(metric.get("turnover_proxy") or 0.0)
            for metric in curve_metrics
        ]

        return {
            "sharpe_ratio": float(portfolio_metrics["sharpe_ratio"]),
            "total_return": float(portfolio_metrics["total_return"]),
            "max_drawdown": float(portfolio_metrics["max_drawdown"]),
            "win_rate": cls._weighted_average(win_rates, trade_weights) if win_rates is not None else None,
            "trades_count": float(sum(component_trade_counts)),
            "avg_holding_days": cls._weighted_average(avg_holding_days, trade_weights or list(payload.get("weights") or [])),
            "turnover_proxy": cls._weighted_average(turnover_values, list(payload.get("weights") or [])),
            "aggregation_mode": str(payload.get("aggregation_mode") or "portfolio_equal_weight"),
            "allocation_mode": str(payload.get("allocation_mode") or "equal_weight"),
            "allocation_weights": dict(payload.get("allocation_weights") or {}),
            "requested_weight_scheme": str(payload.get("requested_weight_scheme") or target_weight_scheme or "equal_weight"),
            "component_count": int(payload.get("component_count") or len(curve_metrics)),
            "portfolio_curve_points": int(payload.get("curve_points") or portfolio_curve.size),
            **cls._aggregate_trade_profile_metrics(
                curve_metrics,
                initial_capital=initial_capital,
            ),
        }

    @classmethod
    def _summarize_result_set(
        cls,
        results: List[dict],
        *,
        target_weight_scheme: str = "single_name",
        initial_capital: float = 100000.0,
        target_weight_map: Optional[dict[str, Any]] = None,
    ) -> dict:
        if not results:
            return {}
        normalized_scheme = cls._normalize_weight_scheme(target_weight_scheme)
        if normalized_scheme != "single_name":
            portfolio_summary = cls._summarize_portfolio_result_set(
                results,
                target_weight_scheme=normalized_scheme,
                initial_capital=initial_capital,
                target_weight_map=target_weight_map,
            )
            if portfolio_summary:
                return portfolio_summary
            return {
                **cls._build_median_summary(results),
                "aggregation_mode": "median_proxy",
                "requested_weight_scheme": normalized_scheme,
                "component_count": len(results),
            }
        return cls._build_median_summary(results)

    @classmethod
    def _aggregate_result_curve(
        cls,
        results: List[dict],
        *,
        target_weight_scheme: str = "single_name",
        initial_capital: float = 100000.0,
        target_weight_map: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        if not results:
            return None
        normalized_scheme = cls._normalize_weight_scheme(target_weight_scheme)
        if normalized_scheme != "single_name":
            payload = cls._build_portfolio_curve_payload(
                results,
                target_weight_scheme=normalized_scheme,
                initial_capital=initial_capital,
                target_weight_map=target_weight_map,
            )
            if payload is not None:
                return {
                    "curve": np.asarray(payload["curve"], dtype=float),
                    "aggregation_mode": str(payload.get("aggregation_mode") or "portfolio_equal_weight"),
                    "allocation_mode": str(payload.get("allocation_mode") or "equal_weight"),
                    "allocation_weights": dict(payload.get("allocation_weights") or {}),
                    "component_count": int(payload.get("component_count") or 0),
                    "curve_points": int(payload.get("curve_points") or 0),
                }

        curves: List[np.ndarray] = []
        for metric in results:
            curve = cls._coerce_equity_curve(metric)
            if curve is None:
                continue
            curves.append(curve / float(curve[0]))
        if not curves:
            return None

        if len(curves) == 1:
            curve = curves[0] * max(float(initial_capital or 0.0), 1.0)
            return {
                "curve": curve,
                "aggregation_mode": "single_name_curve",
                "component_count": 1,
                "curve_points": int(curve.size),
            }

        target_len = max(curve.size for curve in curves)
        aligned_curves = np.vstack([cls._resample_curve(curve, target_len) for curve in curves])
        if normalized_scheme != "single_name":
            aggregated = np.mean(aligned_curves, axis=0) * max(float(initial_capital or 0.0), 1.0)
            mode = "portfolio_equal_weight"
        else:
            aggregated = np.median(aligned_curves, axis=0) * max(float(initial_capital or 0.0), 1.0)
            mode = "curve_median_proxy"
        return {
            "curve": aggregated.astype(float, copy=False),
            "aggregation_mode": mode,
            "component_count": len(curves),
            "curve_points": int(target_len),
        }

    @staticmethod
    def _daily_returns_from_curve(curve: np.ndarray) -> np.ndarray:
        if curve.size < 2:
            return np.array([], dtype=float)
        prev = curve[:-1]
        curr = curve[1:]
        valid = prev > 0
        if not np.any(valid):
            return np.array([], dtype=float)
        returns = (curr[valid] - prev[valid]) / prev[valid]
        returns = returns[np.isfinite(returns)]
        return returns.astype(float, copy=False)

    @classmethod
    def _coerce_return_series(cls, value: Any) -> Optional[np.ndarray]:
        if not isinstance(value, (list, tuple, np.ndarray)):
            return None
        try:
            arr = np.asarray(value, dtype=float)
        except (TypeError, ValueError) as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/application/backtest_filter.py:1698: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return None
        arr = arr[np.isfinite(arr)]
        if arr.size <= 0:
            return None
        return arr.astype(float, copy=False)

    @staticmethod
    def _event_sample_float(sample: dict[str, Any], *keys: str) -> Optional[float]:
        for key in keys:
            if key not in sample or sample.get(key) is None:
                continue
            try:
                return float(sample.get(key) or 0.0)
            except (TypeError, ValueError) as _swallowed:
                logger.warning("packages/strategy-factory/src/strategy_factory/application/backtest_filter.py:1712: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue
        return None

    @staticmethod
    def _event_sample_list(value: Any, *, limit: int = 8) -> list[str]:
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple, set)):
            values = list(value)
        else:
            values = []
        ordered: list[str] = []
        seen: set[str] = set()
        for item in values:
            token = str(item or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            ordered.append(token)
        return ordered[: max(1, int(limit or 8))]
# --- backtest_filter_parts/evaluation.py ---

    @classmethod
    def _extract_event_samples(
        cls,
        *,
        candidate: Optional[dict[str, Any]],
        research_task: Optional[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        payload = dict(candidate or {})
        task = dict(research_task or {})
        sources: list[tuple[str, Any]] = [
            ("research_task.event_samples", task.get("event_samples")),
            ("research_task.event_sample_set", task.get("event_sample_set")),
            ("research_task.event_study.samples", dict(task.get("event_study") or {}).get("samples")),
            ("candidate.event_samples", payload.get("event_samples")),
            ("candidate.event_context.event_samples", dict(payload.get("event_context") or {}).get("event_samples")),
            ("candidate.event_context.samples", dict(payload.get("event_context") or {}).get("samples")),
        ]
        for source_name, value in sources:
            if isinstance(value, dict):
                value = value.get("samples")
            if not isinstance(value, list) or not value:
                continue
            normalized: list[dict[str, Any]] = []
            for idx, item in enumerate(value, 1):
                if not isinstance(item, dict):
                    continue
                sample = dict(item)
                event_id = str(sample.get("event_id") or task.get("event_id") or "").strip()
                if event_id:
                    sample["event_id"] = event_id
                event_time = str(
                    sample.get("event_time")
                    or sample.get("anchor_time")
                    or sample.get("anchor_date")
                    or ""
                ).strip()
                if event_time:
                    sample["event_time"] = event_time
                if not sample.get("sample_id"):
                    sample["sample_id"] = str(sample.get("event_id") or f"event_sample_{idx}")
                normalized.append(sample)
            if normalized:
                return normalized, source_name
        return [], None

    @classmethod
    def _build_minimal_event_samples(
        cls,
        *,
        candidate: Optional[dict[str, Any]],
        research_task: Optional[dict[str, Any]],
        target_results: Optional[List[dict]],
        representative_results: Optional[List[dict]],
        fallback_results: Optional[List[dict]],
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        payload = dict(candidate or {})
        task = dict(research_task or {})
        task_event_context = dict(task.get("event_context") or {})
        payload_event_context = dict(payload.get("event_context") or {})
        event_context = task_event_context or payload_event_context
        event_id = str(
            task.get("event_id")
            or payload.get("event_id")
            or event_context.get("event_id")
            or ""
        ).strip()
        target_codes = _extract_target_codes_from_payload(payload, limit=12)
        if not target_codes:
            target_codes = cls._event_sample_list(
                task.get("target_symbols")
                or event_context.get("target_symbols")
                or payload.get("target_symbols"),
                limit=12,
            )
        target_set = set(target_codes)
        source_results = list(target_results or [])
        if not source_results and fallback_results:
            source_results = [
                dict(item or {})
                for item in list(fallback_results or [])
                if not target_set or str((item or {}).get("code") or "").strip() in target_set
            ]
        if not source_results:
            return [], None

        event_window = dict(task.get("event_window") or {})
        estimation_window = dict(task.get("estimation_window") or {})
        representative_codes = cls._event_sample_list(
            event_context.get("control_group")
            or event_context.get("benchmark_symbols")
            or event_context.get("control_symbols")
            or [item.get("code") for item in list(representative_results or [])],
            limit=8,
        )
        representative_returns = [
            cls._event_sample_float(dict(item or {}), "total_return")
            for item in list(representative_results or [])
        ]
        representative_returns = [float(value) for value in representative_returns if value is not None]
        benchmark_return = cls._event_sample_float(
            event_context,
            "benchmark_return",
            "control_return",
            "baseline_return",
        )
        if benchmark_return is None and representative_returns:
            benchmark_return = float(sum(representative_returns) / len(representative_returns))
        if benchmark_return is None:
            benchmark_return = 0.0
        benchmark_source = "event_context_control_group" if representative_codes else "event_context_minimal_baseline"
        base_anchor = str(
            event_context.get("event_time")
            or event_context.get("event_date")
            or event_context.get("snapshot_date")
            or task.get("event_time")
            or task.get("event_date")
            or task.get("snapshot_date")
            or payload.get("snapshot_date")
            or event_id
            or ""
        ).strip()

        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(source_results, 1):
            sample_payload = dict(item or {})
            code = str(sample_payload.get("code") or "").strip()
            if target_set and code and code not in target_set:
                continue
            target_return = cls._event_sample_float(sample_payload, "total_return")
            if target_return is None:
                continue
            sample_event_id = event_id or f"auto_event_{code or idx}"
            sample_anchor = base_anchor or f"{sample_event_id}:{code or idx}"
            abnormal_return = float(target_return - benchmark_return)
            normalized.append(
                {
                    "sample_id": f"{sample_event_id}:{code or idx}",
                    "event_id": sample_event_id,
                    "event_time": sample_anchor,
                    "target_return": float(target_return),
                    "benchmark_return": float(benchmark_return),
                    "abnormal_return": abnormal_return,
                    "car": abnormal_return,
                    "bhar": abnormal_return,
                    "hit": abnormal_return > 0,
                    "benchmark_source": benchmark_source,
                    "control_group": representative_codes,
                    "pre_days": max(0, int(event_window.get("pre_days") or 0)),
                    "post_days": max(1, int(event_window.get("post_days") or 1)),
                    "estimation_days": max(0, int(estimation_window.get("lookback_days") or 0)),
                    "minimal_context_sample": True,
                }
            )
        return normalized, ("auto_context_minimal" if normalized else None)

    @classmethod
    def _build_event_sample_metrics(
        cls,
        *,
        candidate: Optional[dict[str, Any]],
        research_task: Optional[dict[str, Any]],
        target_results: Optional[List[dict]] = None,
        representative_results: Optional[List[dict]] = None,
        fallback_results: Optional[List[dict]] = None,
    ) -> Optional[dict[str, Any]]:
        event_samples, sample_source = cls._extract_event_samples(candidate=candidate, research_task=research_task)
        event_study_mode = "sample_driven"
        validation_focus = str(dict(research_task or {}).get("validation_focus") or "").strip().lower()
        if not event_samples and validation_focus == "event_target_only":
            event_samples, sample_source = cls._build_minimal_event_samples(
                candidate=candidate,
                research_task=research_task,
                target_results=target_results,
                representative_results=representative_results,
                fallback_results=fallback_results,
            )
            if event_samples:
                event_study_mode = "sample_driven_minimal"
        if not event_samples:
            return None

        event_audit_incomplete = event_study_mode != "sample_driven"
        sample_metrics: list[dict[str, Any]] = []
        benchmark_sources: dict[str, int] = {}
        event_time_anchors: list[str] = []
        event_sample_ids: list[str] = []
        unique_control_codes: set[str] = set()

        for sample in event_samples:
            abnormal_post = cls._coerce_return_series(
                sample.get("abnormal_returns") or sample.get("abnormal_post_returns")
            )
            post_target = cls._coerce_return_series(
                sample.get("post_returns") or sample.get("target_post_returns")
            )
            post_benchmark = cls._coerce_return_series(
                sample.get("benchmark_post_returns") or sample.get("control_post_returns")
            )
            estimation_returns = cls._coerce_return_series(
                sample.get("estimation_returns") or sample.get("benchmark_estimation_returns")
            )

            target_total_return = cls._event_sample_float(sample, "target_return")
            if target_total_return is None and post_target is not None:
                target_total_return = float(np.prod(1.0 + post_target) - 1.0)

            benchmark_total_return = cls._event_sample_float(sample, "benchmark_return", "control_return")
            if benchmark_total_return is None and post_benchmark is not None:
                benchmark_total_return = float(np.prod(1.0 + post_benchmark) - 1.0)

            abnormal_return = cls._event_sample_float(sample, "abnormal_return")
            if abnormal_return is None and target_total_return is not None and benchmark_total_return is not None:
                abnormal_return = float(target_total_return - benchmark_total_return)

            car = cls._event_sample_float(sample, "car")
            if car is None and abnormal_post is not None:
                car = float(np.sum(abnormal_post))

            bhar = cls._event_sample_float(sample, "bhar")
            if bhar is None and post_target is not None and post_benchmark is not None:
                denominator = max(float(np.prod(1.0 + post_benchmark)), 1e-9)
                bhar = float(np.prod(1.0 + post_target) / denominator - 1.0)
            elif bhar is None and abnormal_return is not None:
                bhar = float(abnormal_return)

            pre_event_abnormal_return = cls._event_sample_float(sample, "pre_event_abnormal_return")
            if pre_event_abnormal_return is None:
                pre_abnormal = cls._coerce_return_series(
                    sample.get("pre_abnormal_returns") or sample.get("abnormal_pre_returns")
                )
                if pre_abnormal is not None:
                    pre_event_abnormal_return = float(np.sum(pre_abnormal))

            hit_ratio = cls._event_sample_float(sample, "hit_ratio")
            if hit_ratio is None:
                hit_flag = sample.get("hit")
                if hit_flag is not None:
                    hit_ratio = 1.0 if bool(hit_flag) else 0.0
                elif abnormal_return is not None:
                    hit_ratio = 1.0 if abnormal_return > 0 else 0.0

            post_event_decay = cls._event_sample_float(sample, "post_event_decay", "decay")
            if post_event_decay is None and abnormal_post is not None and abnormal_post.size > 0:
                split = max(1, int(abnormal_post.size // 2))
                early_car = float(np.sum(abnormal_post[:split]))
                late_car = float(np.sum(abnormal_post[split:])) if abnormal_post.size > split else 0.0
                decay_denominator = max(abs(early_car), 0.01)
                post_event_decay = float(late_car / decay_denominator - 1.0)

            if all(metric is None for metric in (abnormal_return, car, bhar, hit_ratio)):
                continue

            control_codes = cls._event_sample_list(
                sample.get("control_group")
                or sample.get("benchmark_symbols")
                or sample.get("control_symbols")
            )
            unique_control_codes.update(control_codes)
            benchmark_source = str(
                sample.get("benchmark_source")
                or ("sample_control_group" if control_codes else "sample_baseline")
            ).strip().lower() or "sample_baseline"
            benchmark_sources[benchmark_source] = benchmark_sources.get(benchmark_source, 0) + 1

            pre_abnormal = cls._coerce_return_series(sample.get("pre_abnormal_returns") or sample.get("abnormal_pre_returns"))
            post_observation = post_target if post_target is not None else abnormal_post

            sample_id = str(sample.get("sample_id") or sample.get("event_id") or "").strip()
            if sample_id and sample_id not in event_sample_ids:
                event_sample_ids.append(sample_id)
            anchor = str(sample.get("event_time") or sample.get("event_id") or "").strip()
            if anchor and anchor not in event_time_anchors:
                event_time_anchors.append(anchor)

            sample_metrics.append(
                {
                    "target_total_return": float(target_total_return or 0.0),
                    "benchmark_total_return": float(benchmark_total_return or 0.0),
                    "abnormal_return": float(abnormal_return or 0.0),
                    "car": float(car if car is not None else abnormal_return or 0.0),
                    "bhar": float(bhar if bhar is not None else abnormal_return or 0.0),
                    "hit_ratio": float(hit_ratio if hit_ratio is not None else 0.0),
                    "pre_event_abnormal_return": float(pre_event_abnormal_return or 0.0),
                    "post_event_decay": float(post_event_decay or 0.0),
                    "pre_days_used": int(sample.get("pre_days") or (int(pre_abnormal.size) if pre_abnormal is not None else 0)),
                    "post_days_used": int(sample.get("post_days") or (int(post_observation.size) if post_observation is not None else 0)),
                    "estimation_days_used": int(sample.get("estimation_days") or (int(estimation_returns.size) if estimation_returns is not None else 0)),
                }
            )

        if not sample_metrics:
            return None

        def _mean_value(key: str) -> float:
            values = [float(item.get(key) or 0.0) for item in sample_metrics]
            return float(sum(values) / len(values)) if values else 0.0

        benchmark_source = max(benchmark_sources.items(), key=lambda item: item[1])[0] if benchmark_sources else "sample_baseline"
        return {
            "total_return": round(_mean_value("target_total_return"), 4),
            "benchmark_return": round(_mean_value("benchmark_total_return"), 4),
            "abnormal_return": round(_mean_value("abnormal_return"), 4),
            "car": round(_mean_value("car"), 4),
            "bhar": round(_mean_value("bhar"), 4),
            "hit_ratio": round(_mean_value("hit_ratio"), 4),
            "pre_event_abnormal_return": round(_mean_value("pre_event_abnormal_return"), 4),
            "post_event_decay": round(_mean_value("post_event_decay"), 4),
            "pre_days_used": int(round(_mean_value("pre_days_used"))),
            "post_days_used": int(round(_mean_value("post_days_used"))),
            "estimation_days_used": int(round(_mean_value("estimation_days_used"))),
            "benchmark_source": benchmark_source,
            "aggregation_mode": "event_sample_average",
            "component_count": len(_extract_target_codes_from_payload(candidate or {}, limit=12)),
            "curve_points": 0,
            "event_study_mode": event_study_mode,
            "event_sample_source": sample_source,
            "event_sample_count": len(sample_metrics),
            "event_anchor_count": len(event_time_anchors),
            "event_time_anchors": event_time_anchors[:8],
            "event_sample_ids": event_sample_ids[:8],
            "control_group_count": len(unique_control_codes),
            "traceable_to_event_samples": not event_audit_incomplete,
            "event_audit_incomplete": event_audit_incomplete,
        }

    @classmethod
    def _build_event_window_metrics(
        cls,
        *,
        candidate: Optional[dict[str, Any]],
        target_results: List[dict],
        representative_results: List[dict],
        fallback_results: List[dict],
        research_task: dict[str, Any],
        target_weight_scheme: str,
        initial_capital: float,
        target_weight_map: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        sample_metrics = cls._build_event_sample_metrics(
            candidate=candidate,
            research_task=research_task,
            target_results=target_results,
            representative_results=representative_results,
            fallback_results=fallback_results,
        )
        if sample_metrics is not None:
            return sample_metrics
        return {}

    @staticmethod
    def _build_backtest_assumptions(candidate: dict) -> FactoryBacktestAssumptions:
        return build_factory_backtest_assumptions(candidate)

    @staticmethod
    def _trade_density_observation_days(
        *,
        metrics: dict[str, Any],
        target_metrics: dict[str, Any],
        combined_metrics: dict[str, Any],
        event_window_metrics: dict[str, Any],
        lookback_days: float,
        post_days: float,
        trade_count: float,
        avg_holding_days: float,
    ) -> float:
        candidates: list[float] = [max(1.0, lookback_days + post_days)]
        for payload in (metrics, target_metrics, combined_metrics):
            curve_points = float(payload.get("portfolio_curve_points") or payload.get("curve_points") or 0.0)
            if curve_points > 0:
                candidates.append(curve_points)
        event_sample_days = float(event_window_metrics.get("estimation_days_used") or 0.0) + float(
            event_window_metrics.get("post_days_used") or 0.0
        )
        if event_sample_days > 0:
            candidates.append(event_sample_days)
        if trade_count > 0 and avg_holding_days > 0:
            candidates.append(float(lookback_days) + float(trade_count) * float(avg_holding_days))
        return max(1.0, *candidates)
# --- backtest_filter_parts/reporting.py ---
    @staticmethod
    def _finite_float(value: Any, default: float = 0.0) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not np.isfinite(result):
            return float(default)
        return float(result)

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        arr = np.asarray(values, dtype=float)
        arr = arr[np.isfinite(arr)]
        if arr.size <= 0:
            return 0.0
        return float(np.percentile(arr, percentile))

    @classmethod
    def _trade_profile_from_round_trips(
        cls,
        round_trips: Any,
        *,
        initial_capital: float = 100000.0,
    ) -> dict[str, Any]:
        rows = [dict(item or {}) for item in list(round_trips or []) if isinstance(item, dict)]
        closed_rows = [
            item
            for item in rows
            if str(item.get("status") or "").strip().lower() in {"", "closed"}
        ]
        if not closed_rows:
            return {
                "trade_profile_available": False,
                "trade_profile_source": "round_trip_positions",
                "trade_distribution": {
                    "available": False,
                    "source": "round_trip_positions",
                    "sample_count": 0,
                },
            }

        pnl_values = [cls._finite_float(item.get("realized_pnl")) for item in closed_rows]
        return_values = [
            cls._finite_float(item.get("realized_return"))
            for item in closed_rows
            if item.get("realized_return") is not None
        ]
        if not return_values and initial_capital > 0:
            return_values = [pnl / float(initial_capital) for pnl in pnl_values]
        holding_days = [
            cls._finite_float(item.get("hold_days"))
            for item in closed_rows
            if item.get("hold_days") is not None
        ]
        wins = [value for value in pnl_values if value > 0]
        losses = [value for value in pnl_values if value < 0]
        flats = [value for value in pnl_values if value == 0]
        gross_profit = float(sum(wins))
        gross_loss = float(abs(sum(losses)))
        profit_factor = (
            None
            if gross_loss <= 0 and gross_profit > 0
            else (gross_profit / gross_loss if gross_loss > 0 else 0.0)
        )
        expectancy = float(sum(pnl_values) / len(pnl_values)) if pnl_values else 0.0
        expectancy_return = float(sum(return_values) / len(return_values)) if return_values else 0.0
        avg_win = float(sum(wins) / len(wins)) if wins else 0.0
        avg_loss = float(abs(sum(losses) / len(losses))) if losses else 0.0
        payoff_ratio = float(avg_win / avg_loss) if avg_win > 0 and avg_loss > 0 else 0.0
        win_rate = float(len(wins) / len(pnl_values)) if pnl_values else 0.0
        breakeven_win_rate = float(avg_loss / (avg_win + avg_loss)) if (avg_win + avg_loss) > 0 else 0.0
        return {
            "trade_profile_available": True,
            "trade_profile_source": "round_trip_positions",
            "trade_count": int(len(pnl_values)),
            "trades_count": int(len(pnl_values)),
            "closed_round_trip_count": int(len(pnl_values)),
            "win_rate": round(win_rate, 6),
            "profit_factor": None if profit_factor is None else round(profit_factor, 6),
            "expectancy": round(expectancy, 6),
            "expectancy_return": round(expectancy_return, 6),
            "avg_win": round(avg_win, 6),
            "avg_loss": round(avg_loss, 6),
            "payoff_ratio": round(payoff_ratio, 6),
            "breakeven_win_rate": round(breakeven_win_rate, 6),
            "gross_profit": round(gross_profit, 6),
            "gross_loss": round(gross_loss, 6),
            "trade_distribution": {
                "available": True,
                "source": "round_trip_positions",
                "sample_count": int(len(pnl_values)),
                "win_count": int(len(wins)),
                "loss_count": int(len(losses)),
                "flat_count": int(len(flats)),
                "pnl_mean": round(float(np.mean(pnl_values)), 6),
                "pnl_median": round(float(np.median(pnl_values)), 6),
                "pnl_std": round(float(np.std(pnl_values)), 6),
                "pnl_min": round(float(min(pnl_values)), 6),
                "pnl_max": round(float(max(pnl_values)), 6),
                "pnl_p10": round(cls._percentile(pnl_values, 10), 6),
                "pnl_p25": round(cls._percentile(pnl_values, 25), 6),
                "pnl_p75": round(cls._percentile(pnl_values, 75), 6),
                "pnl_p90": round(cls._percentile(pnl_values, 90), 6),
                "return_mean": round(float(np.mean(return_values)), 6) if return_values else 0.0,
                "return_median": round(float(np.median(return_values)), 6) if return_values else 0.0,
                "return_p10": round(cls._percentile(return_values, 10), 6) if return_values else 0.0,
                "return_p90": round(cls._percentile(return_values, 90), 6) if return_values else 0.0,
                "avg_holding_days": round(float(np.mean(holding_days)), 6) if holding_days else 0.0,
                "holding_days_p50": round(cls._percentile(holding_days, 50), 6) if holding_days else 0.0,
                "holding_days_p90": round(cls._percentile(holding_days, 90), 6) if holding_days else 0.0,
            },
        }

    @classmethod
    def _trade_profile_from_trades(
        cls,
        trades: Any,
        *,
        initial_capital: float = 100000.0,
    ) -> dict[str, Any]:
        rows = [dict(item or {}) for item in list(trades or []) if isinstance(item, dict)]
        exit_rows = [
            item
            for item in rows
            if int(cls._finite_float(item.get("signal"), 0.0)) < 0
            or str(item.get("action") or "").strip().lower() in {"exit", "reduce", "sell"}
            or cls._finite_float(item.get("profit"), 0.0) != 0.0
        ]
        if not exit_rows:
            return {
                "trade_profile_available": False,
                "trade_profile_source": "trades",
                "trade_distribution": {
                    "available": False,
                    "source": "trades",
                    "sample_count": 0,
                },
            }
        round_trips = []
        for index, item in enumerate(exit_rows, 1):
            pnl = cls._finite_float(item.get("profit"))
            notional = abs(cls._finite_float(item.get("shares")) * cls._finite_float(item.get("price")))
            round_trips.append(
                {
                    "round_trip_id": f"trade_exit_{index}",
                    "status": "closed",
                    "realized_pnl": pnl,
                    "realized_return": pnl / notional if notional > 0 else (pnl / initial_capital if initial_capital > 0 else 0.0),
                    "hold_days": item.get("holding_days"),
                }
            )
        profile = cls._trade_profile_from_round_trips(round_trips, initial_capital=initial_capital)
        profile["trade_profile_source"] = "trades"
        distribution = dict(profile.get("trade_distribution") or {})
        distribution["source"] = "trades"
        profile["trade_distribution"] = distribution
        return profile

    @classmethod
    def _trade_profile_from_metric(cls, metric: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(metric or {})
        initial_capital = cls._finite_float(payload.get("initial_capital"), 100000.0)
        profile = cls._trade_profile_from_round_trips(
            payload.get("round_trip_positions"),
            initial_capital=initial_capital,
        )
        if not profile.get("trade_profile_available"):
            profile = cls._trade_profile_from_trades(
                payload.get("trades"),
                initial_capital=initial_capital,
            )
        if profile.get("trade_profile_available"):
            return profile
        return {
            "trade_profile_available": False,
            "trade_profile_source": "unavailable",
            "trade_profile_unavailable_reason": (
                "missing_round_trip_positions_and_trades"
                if not payload.get("round_trip_positions") and not payload.get("trades")
                else "no_closed_trade_pnl"
            ),
            "trade_distribution": dict(profile.get("trade_distribution") or {}),
        }

    @classmethod
    def _merge_trade_profile_metrics(cls, metric: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(metric or {})
        profile = cls._trade_profile_from_metric(payload)
        for key, value in profile.items():
            if value in (None, "", [], {}):
                continue
            payload[key] = value
        return payload

    @classmethod
    def _aggregate_trade_profile_metrics(
        cls,
        results: List[dict],
        *,
        initial_capital: float = 100000.0,
    ) -> dict[str, Any]:
        round_trips: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        for metric in list(results or []):
            payload = dict(metric or {})
            for item in list(payload.get("round_trip_positions") or []):
                if isinstance(item, dict):
                    round_trips.append(dict(item))
            if not round_trips:
                for item in list(payload.get("trades") or []):
                    if isinstance(item, dict):
                        trades.append(dict(item))
        profile = cls._trade_profile_from_round_trips(round_trips, initial_capital=initial_capital)
        if not profile.get("trade_profile_available"):
            profile = cls._trade_profile_from_trades(trades, initial_capital=initial_capital)
        if profile.get("trade_profile_available"):
            profile["trade_profile_aggregation"] = "component_closed_trades"
            return profile
        return {
            "trade_profile_available": False,
            "trade_profile_source": "unavailable",
            "trade_profile_unavailable_reason": "no_component_closed_trade_pnl",
            "trade_distribution": dict(profile.get("trade_distribution") or {}),
        }

    @staticmethod
    def _derive_trade_validation_metrics(candidate: dict, result: dict) -> dict[str, Any]:
        metrics = dict(result.get("metrics") or {})
        layers = dict(result.get("layers") or {})
        target_metrics = dict((layers.get("target") or {}).get("metrics") or {})
        representative_metrics = dict((layers.get("representative") or {}).get("metrics") or {})
        combined_metrics = dict((layers.get("combined") or {}).get("metrics") or {})
        event_window_metrics = dict(result.get("event_window_metrics") or {})
        research_task = _normalize_research_task_contract(candidate.get("research_task") or {})
        event_window = dict(research_task.get("event_window") or {})
        holding_window = dict(research_task.get("holding_window") or {})
        holding_horizon = dict(candidate.get("holding_horizon") or {})
        risk_rules = dict(candidate.get("risk_rules") or {})
        execution_assumptions = dict(candidate.get("execution_assumptions") or {})
        portfolio_spec = dict(candidate.get("portfolio_spec") or {})

        trade_count = float(metrics.get("trades_count") or metrics.get("trade_count") or 0.0)
        avg_holding_days = float(
            metrics.get("avg_holding_days")
            or holding_window.get("max_days")
            or holding_horizon.get("max_days")
            or risk_rules.get("max_holding_days")
            or 0.0
        )
        turnover_proxy = float(metrics.get("turnover_proxy") or 0.0)
        if turnover_proxy <= 0 and trade_count > 0:
            turnover_proxy = round(trade_count / max(avg_holding_days, 5.0), 4) if avg_holding_days > 0 else float(trade_count)

        target_layer_oos_return = float(target_metrics.get("total_return") or metrics.get("total_return") or 0.0)
        representative_return = float(representative_metrics.get("total_return") or 0.0)
        combined_return = float(combined_metrics.get("total_return") or metrics.get("total_return") or 0.0)
        event_window_return = float(
            event_window_metrics.get("bhar")
            or event_window_metrics.get("abnormal_return")
            or event_window_metrics.get("total_return")
            or target_layer_oos_return
            or combined_return
            or 0.0
        )
        target_layer_abnormal_return = round(
            float(event_window_metrics.get("abnormal_return"))
            if event_window_metrics.get("abnormal_return") is not None
            else target_layer_oos_return - representative_return,
            4,
        )

        post_event_decay = (
            round(float(event_window_metrics.get("post_event_decay")), 4)
            if event_window_metrics.get("post_event_decay") is not None
            else None
        )

        event_window_hit_ratio = (
            round(float(event_window_metrics.get("hit_ratio")), 4)
            if event_window_metrics.get("hit_ratio") is not None
            else None
        )

        lookback_days = float((research_task.get("estimation_window") or {}).get("lookback_days") or 60.0)
        post_days = float(event_window.get("post_days") or holding_window.get("max_days") or avg_holding_days or 20.0)
        observation_days = BacktestFilter._trade_density_observation_days(
            metrics=metrics,
            target_metrics=target_metrics,
            combined_metrics=combined_metrics,
            event_window_metrics=event_window_metrics,
            lookback_days=lookback_days,
            post_days=post_days,
            trade_count=trade_count,
            avg_holding_days=avg_holding_days,
        )
        trade_density = round(trade_count / observation_days * 20.0, 4)

        # Parameter stability is only real when the upstream perturbation
        # study supplied it. Do NOT invent it from cross-layer Sharpe
        # dispersion — that is a different signal and was previously
        # mislabeled into hard admission gates.
        if metrics.get("parameter_perturbation_trade_stability") is not None:
            parameter_stability = round(
                float(metrics.get("parameter_perturbation_trade_stability")), 4
            )
        else:
            parameter_stability = None
        # 扰动研究的另外两个统计量（提交侧真实证据）：
        # 交易数对扰动幅度的 Pearson 相关；扰动前后方向一致率。
        # 只在研究真实产出时透传，缺失保持 None，不用 0 补位。
        parameter_trade_count_correlation = (
            round(float(metrics.get("parameter_perturbation_trade_count_correlation")), 4)
            if metrics.get("parameter_perturbation_trade_count_correlation") is not None
            else None
        )
        parameter_direction_agreement = (
            round(float(metrics.get("parameter_perturbation_direction_agreement")), 4)
            if metrics.get("parameter_perturbation_direction_agreement") is not None
            else None
        )
        event_metrics_fabricated = bool(
            event_window_hit_ratio is None
            and post_event_decay is None
            and not event_window_metrics
        )
        expected_turnover_band = str(
            candidate.get("expected_turnover_band")
            or holding_horizon.get("expected_turnover_band")
            or dict(candidate.get("rebalance_rule") or {}).get("expected_turnover_band")
            or execution_assumptions.get("expected_turnover_band")
            or ""
        ).strip().lower() or None
        capacity_bucket = str(
            candidate.get("capacity_bucket")
            or execution_assumptions.get("capacity_bucket")
            or portfolio_spec.get("capacity_bucket")
            or ""
        ).strip().lower() or None
        turnover_cost_class = str(
            candidate.get("turnover_cost_class")
            or execution_assumptions.get("turnover_cost_class")
            or ""
        ).strip().lower() or None
        position_sizing_rationale = str(
            candidate.get("position_sizing_rationale")
            or dict(candidate.get("position_sizing") or {}).get("position_sizing_rationale")
            or portfolio_spec.get("position_sizing_rationale")
            or ""
        ).strip() or None

        trade_profile_metrics = BacktestFilter._trade_profile_from_metric(metrics)
        if not trade_profile_metrics.get("trade_profile_available"):
            primary_layer = str(result.get("primary_validation_layer") or result.get("primary_layer") or "").strip().lower()
            layer_profile = (
                dict((layers.get("target") or {}).get("trade_profile") or {})
                if primary_layer == "target"
                else dict((layers.get("combined") or {}).get("trade_profile") or {})
            )
            if layer_profile.get("trade_profile_available"):
                trade_profile_metrics = layer_profile

        return {
            "avg_holding_days": round(avg_holding_days, 4),
            "turnover_proxy": round(turnover_proxy, 4),
            "target_layer_oos_return": round(target_layer_oos_return, 4),
            "target_layer_abnormal_return": round(target_layer_abnormal_return, 4),
            "event_window_hit_ratio": (
                None if event_window_hit_ratio is None else round(event_window_hit_ratio, 4)
            ),
            "post_event_decay": None if post_event_decay is None else round(post_event_decay, 4),
            "trade_density": round(trade_density, 4),
            "parameter_perturbation_trade_stability": (
                None if parameter_stability is None else round(parameter_stability, 4)
            ),
            "parameter_perturbation_trade_count_correlation": parameter_trade_count_correlation,
            "parameter_perturbation_direction_agreement": parameter_direction_agreement,
            "event_metrics_fabricated": False,
            "event_study_mode": str(event_window_metrics.get("event_study_mode") or "").strip().lower() or None,
            "event_sample_count": int(event_window_metrics.get("event_sample_count") or 0),
            "event_anchor_count": int(event_window_metrics.get("event_anchor_count") or 0),
            "control_group_count": int(event_window_metrics.get("control_group_count") or 0),
            "event_sample_source": event_window_metrics.get("event_sample_source"),
            "event_time_anchors": list(event_window_metrics.get("event_time_anchors") or [])[:8],
            "traceable_to_event_samples": bool(event_window_metrics.get("traceable_to_event_samples")),
            "event_audit_incomplete": bool(event_window_metrics.get("event_audit_incomplete")),
            "expected_turnover_band": expected_turnover_band,
            "capacity_bucket": capacity_bucket,
            "turnover_cost_class": turnover_cost_class,
            "position_sizing_rationale": position_sizing_rationale,
            "market_regime_assumption": candidate.get("market_regime_assumption"),
            **trade_profile_metrics,
        }

    @staticmethod
    def _build_backtest_metrics_contract(candidate: dict, result: dict) -> dict[str, Any]:
        payload = dict(result or {})
        metrics = dict(payload.get("metrics") or {})
        layers = dict(payload.get("layers") or {})
        primary_layer = str(
            payload.get("primary_validation_layer")
            or payload.get("primary_layer")
            or "combined"
        ).strip().lower() or "combined"
        target_metrics = dict((layers.get("target") or {}).get("metrics") or {})
        combined_metrics = dict((layers.get("combined") or {}).get("metrics") or metrics)
        primary_metrics = (
            target_metrics
            if primary_layer == "target" and target_metrics
            else combined_metrics
        )
        sharpe_ratio = _safe_float(primary_metrics.get("sharpe_ratio"), _safe_float(metrics.get("sharpe_ratio")))
        post_cost_sharpe = _safe_float(
            primary_metrics.get("post_cost_sharpe"),
            _safe_float(metrics.get("post_cost_sharpe"), sharpe_ratio),
        )
        trades_count = int(
            primary_metrics.get("trades_count")
            or primary_metrics.get("trade_count")
            or metrics.get("trades_count")
            or metrics.get("trade_count")
            or 0
        )
        max_drawdown = _safe_float(primary_metrics.get("max_drawdown"), _safe_float(metrics.get("max_drawdown")))
        win_rate = _safe_float(primary_metrics.get("win_rate"), _safe_float(metrics.get("win_rate")))
        target_codes = list(payload.get("target_codes") or _extract_target_codes_from_payload(candidate))
        required_fields = {
            "sharpe_ratio": sharpe_ratio,
            "post_cost_sharpe": post_cost_sharpe,
            "trades_count": trades_count,
            "max_drawdown": max_drawdown,
            "validation_focus": payload.get("validation_focus"),
            "code_source": payload.get("code_source"),
        }
        missing_fields = [key for key, value in required_fields.items() if value in (None, "", [])]
        status = "present" if not missing_fields else ("partial" if len(missing_fields) < len(required_fields) else "missing")
        return {
            "contract_version": "strategy_factory.backtest_metrics_contract.v1",
            "status": status,
            "available": status != "missing",
            "missing_fields": missing_fields,
            "sharpe_ratio": round(sharpe_ratio, 6),
            "post_cost_sharpe": round(post_cost_sharpe, 6),
            "trade_count": trades_count,
            "trades_count": trades_count,
            "max_drawdown": round(max_drawdown, 6),
            "win_rate": round(win_rate, 6),
            "profit_factor": _safe_float(primary_metrics.get("profit_factor"), _safe_float(metrics.get("profit_factor"))),
            "expectancy": _safe_float(primary_metrics.get("expectancy"), _safe_float(metrics.get("expectancy"))),
            "expectancy_return": _safe_float(primary_metrics.get("expectancy_return"), _safe_float(metrics.get("expectancy_return"))),
            "payoff_ratio": _safe_float(primary_metrics.get("payoff_ratio"), _safe_float(metrics.get("payoff_ratio"))),
            "breakeven_win_rate": _safe_float(
                primary_metrics.get("breakeven_win_rate"),
                _safe_float(metrics.get("breakeven_win_rate")),
            ),
            "trade_distribution": dict(primary_metrics.get("trade_distribution") or metrics.get("trade_distribution") or {}),
            "validation_focus": payload.get("validation_focus"),
            "primary_validation_layer": primary_layer,
            "code_source": payload.get("code_source"),
            "target_codes": list(target_codes),
        }

    @staticmethod
    def _resolve_backtest_plan(candidate: dict) -> tuple[List[str], List[str], List[str], str, str]:
        candidate = apply_resolved_candidate_envelope(candidate)
        target_codes = _extract_target_codes_from_payload(candidate, limit=12)
        target_codes = _apply_preferred_code_order(target_codes, _preferred_target_order(candidate))
        raw_research_task = candidate.get("research_task") or {}
        research_task = _normalize_research_task_contract(raw_research_task)
        validation_profile = dict(candidate.get("validation_profile") or {})
        validation_focus = str(
            validation_profile.get("validation_focus")
            or research_task.get("validation_focus")
            or "target_plus_representative"
        ).strip().lower()
        representative_stocks = list(_compat_setting("REPRESENTATIVE_STOCKS", REPRESENTATIVE_STOCKS))
        representative_codes = [code for code in representative_stocks if code not in target_codes]
        has_explicit_research_task = _has_explicit_research_task(candidate)

        if not has_explicit_research_task:
            if validation_focus == "broad_generalization":
                evaluated_codes = list(dict.fromkeys([*target_codes, *representative_stocks]))
                return (
                    evaluated_codes,
                    target_codes,
                    representative_codes,
                    "target_plus_representative",
                    validation_focus,
                )
            if target_codes:
                return list(target_codes), target_codes, representative_codes, "candidate_target_symbols", "candidate_target_only"
            return list(representative_stocks), target_codes, representative_codes, "representative_only", "target_plus_representative"

        if validation_focus in {"candidate_target_only", "event_target_only", "target_only"} and target_codes:
            evaluated_codes = list(target_codes)
            code_source = validation_focus
        elif validation_focus == "broad_generalization":
            evaluated_codes = list(dict.fromkeys([*target_codes, *representative_stocks]))
            code_source = "target_plus_representative"
        else:
            selected_representatives = representative_codes[:2] if target_codes else representative_stocks[:2]
            evaluated_codes = list(dict.fromkeys([*target_codes, *selected_representatives]))
            code_source = "candidate_target_plus_representative" if target_codes else "representative_only"
        return evaluated_codes, target_codes, representative_codes, code_source, validation_focus

    @classmethod
    def _resolve_backtest_codes(cls, candidate: dict) -> tuple[List[str], List[str], List[str], str]:
        evaluated_codes, target_codes, representative_codes, code_source, _ = cls._resolve_backtest_plan(candidate)
        return evaluated_codes, target_codes, representative_codes, code_source
# --- backtest_filter_parts/models.py ---

    async def _test_one(self, candidate: dict, db, engine) -> dict:
        candidate = apply_resolved_candidate_envelope(candidate)
        factory_pkg = _get_strategy_factory_package()
        strategy_type = str(candidate.get("strategy_type") or "unknown")
        thresholds = self._get_thresholds(strategy_type, candidate)
        results: List[dict] = []
        successful_codes: List[str] = []
        skipped_codes: List[dict] = []
        failed_codes: List[dict] = []
        evaluated_codes, target_codes, representative_codes, code_source, validation_focus = self._resolve_backtest_plan(candidate)
        contract_snapshot = build_portfolio_candidate_contract(candidate)
        contract_hash = build_candidate_contract_hash(contract=contract_snapshot)
        execution_contract_hash = str(candidate.get("execution_contract_hash") or "").strip() or contract_hash
        tested_object_hash = str(candidate.get("tested_object_hash") or "").strip() or build_tested_object_hash(candidate)
        candidate_identity_signature = str(candidate.get("candidate_identity_signature") or "").strip()
        assumptions = self._build_backtest_assumptions(candidate)
        assumptions_kwargs = assumptions.to_backtest_kwargs()
        research_task = _normalize_research_task_contract(candidate.get("research_task"))
        has_explicit_research_task = _has_explicit_research_task(candidate)
        required_sample_count = _resolve_required_sample_count(
            candidate,
            thresholds=thresholds,
            research_task=research_task,
            validation_focus=validation_focus,
            target_codes=target_codes,
        )
        target_set = set(target_codes)
        layer_results: Dict[str, List[dict]] = {"target": [], "representative": []}
        layer_successful_codes: Dict[str, List[str]] = {"target": [], "representative": []}
        candidate_started_at = time.perf_counter()

        async def _run_one_code(code: str) -> dict:
            layer = "target" if code in target_set else "representative"
            started_at = time.perf_counter()
            cache_hit = False
            try:
                klines = self._kline_cache.get(code)
                if klines is not None:
                    cache_hit = True
                else:
                    klines = await db.get_klines(code, limit=500)
                    self._kline_cache[code] = klines or []
                if not klines or len(klines) < 100:
                    return {
                        "code": code,
                        "layer": layer,
                        "status": "skipped",
                        "reason": "insufficient_klines",
                        "available": len(klines or []),
                        "cache_hit": cache_hit,
                        "run_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    }
                try:
                    # PR-S1: 单只股票回测超时保护，命中后该 code 计入 failed，释放并发槽
                    _backtest_timeout = float(
                        _compat_setting(
                            "BACKTEST_CODE_TIMEOUT_SEC",
                            BACKTEST_CODE_TIMEOUT_SEC,
                        )
                        or BACKTEST_CODE_TIMEOUT_SEC
                    )
                    result = await asyncio.wait_for(
                        factory_pkg.asyncio.to_thread(
                            engine.run_backtest,
                            code,
                            klines,
                            candidate["strategy_type"],
                            {
                                **_ensure_strategy_params(
                                    candidate["strategy_type"],
                                    candidate["params"],
                                    research_task=candidate.get("research_task"),
                                ),
                                **assumptions_kwargs,
                            },
                            True,
                        ),
                        timeout=_backtest_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "BacktestFilter code backtest timed out strategy_type=%s code=%s timeout=%.1fs",
                        candidate.get("strategy_type"),
                        code,
                        _backtest_timeout,
                    )
                    return {
                        "code": code,
                        "layer": layer,
                        "status": "failed",
                        "reason": "timeout",
                        "timeout_sec": _backtest_timeout,
                        "cache_hit": cache_hit,
                        "run_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    }
                if result.get("success"):
                    return {
                        "code": code,
                        "layer": layer,
                        "status": "success",
                        "data": result["data"],
                        "cache_hit": cache_hit,
                        "run_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    }
                return {
                    "code": code,
                    "layer": layer,
                    "status": "failed",
                    "reason": "backtest_failed",
                    "cache_hit": cache_hit,
                    "run_ms": round((time.perf_counter() - started_at) * 1000, 2),
                }
            except Exception as exc:
                logger.warning(
                    "BacktestFilter code backtest failed strategy_type=%s code=%s error=%s",
                    candidate.get("strategy_type"),
                    code,
                    exc,
                    exc_info=exc,
                )
                return {
                    "code": code,
                    "layer": layer,
                    "status": "failed",
                    "reason": f"exception:{type(exc).__name__}",
                    "cache_hit": cache_hit,
                    "run_ms": round((time.perf_counter() - started_at) * 1000, 2),
                }

        code_concurrency = int(_compat_setting("BACKTEST_CODE_CONCURRENCY", BACKTEST_CODE_CONCURRENCY) or BACKTEST_CODE_CONCURRENCY)
        code_sem = asyncio.Semaphore(code_concurrency)

        async def _run_guarded(code: str) -> dict:
            async with code_sem:
                return await _run_one_code(code)

        code_results = await asyncio.gather(*[_run_guarded(code) for code in evaluated_codes])
        code_run_ms_total = 0.0
        kline_cache_hit_count = 0
        for item in code_results:
            code_run_ms_total += float(item.get("run_ms") or 0.0)
            if item.get("cache_hit"):
                kline_cache_hit_count += 1
            layer = str(item.get("layer") or "representative")
            code = str(item.get("code") or "")
            if item.get("status") == "success":
                result_data = dict(item.get("data") or {})
                result_data = self._merge_trade_profile_metrics(result_data)
                results.append(result_data)
                layer_results[layer].append(result_data)
                successful_codes.append(code)
                layer_successful_codes[layer].append(code)
            elif item.get("status") == "skipped":
                skipped_codes.append({
                    "code": code,
                    "reason": item.get("reason"),
                    "available": item.get("available", 0),
                    "layer": layer,
                })
            else:
                failed_codes.append({"code": code, "reason": item.get("reason"), "layer": layer})

        target_portfolio_metrics = await self._run_portfolio_engine_summary(
            candidate=candidate,
            engine=engine,
            codes=layer_successful_codes["target"],
            assumptions=assumptions,
        )
        combined_portfolio_metrics = await self._run_portfolio_engine_summary(
            candidate=candidate,
            engine=engine,
            codes=successful_codes,
            assumptions=assumptions,
        )

        target_metrics = target_portfolio_metrics or self._summarize_result_set(
            layer_results["target"],
            target_weight_scheme=assumptions.target_weight_scheme,
            initial_capital=assumptions.initial_capital,
            target_weight_map=assumptions.target_weight_map,
        )
        representative_metrics = self._summarize_result_set(
            layer_results["representative"],
            target_weight_scheme=assumptions.target_weight_scheme,
            initial_capital=assumptions.initial_capital,
        )
        combined_metrics = combined_portfolio_metrics or self._summarize_result_set(
            results,
            target_weight_scheme=assumptions.target_weight_scheme,
            initial_capital=assumptions.initial_capital,
            target_weight_map=assumptions.target_weight_map,
        )
        target_trade_profile = self._aggregate_trade_profile_metrics(
            layer_results["target"],
            initial_capital=assumptions.initial_capital,
        )
        representative_trade_profile = self._aggregate_trade_profile_metrics(
            layer_results["representative"],
            initial_capital=assumptions.initial_capital,
        )
        combined_trade_profile = self._aggregate_trade_profile_metrics(
            results,
            initial_capital=assumptions.initial_capital,
        )
        if target_metrics and not target_metrics.get("trade_profile_available"):
            target_metrics = {**target_metrics, **target_trade_profile}
        if representative_metrics and not representative_metrics.get("trade_profile_available"):
            representative_metrics = {**representative_metrics, **representative_trade_profile}
        if combined_metrics and not combined_metrics.get("trade_profile_available"):
            combined_metrics = {**combined_metrics, **combined_trade_profile}
        event_window_metrics = self._build_event_window_metrics(
            candidate=candidate,
            target_results=layer_results["target"],
            representative_results=layer_results["representative"],
            fallback_results=results,
            research_task=research_task,
            target_weight_scheme=assumptions.target_weight_scheme,
            initial_capital=assumptions.initial_capital,
            target_weight_map=assumptions.target_weight_map,
        )
        if validation_focus == "event_target_only":
            primary_results = layer_results["target"]
            primary_layer = "target"
        elif validation_focus == "broad_generalization":
            primary_results = results
            primary_layer = "combined"
        else:
            primary_results = layer_results["target"] if layer_results["target"] else results
            primary_layer = "target" if layer_results["target"] else "combined"
        primary_metrics_payload = (
            target_metrics
            if primary_layer == "target"
            else combined_metrics
        )
        if primary_layer == "target" and not target_metrics:
            primary_metrics_payload = combined_metrics
        sample_audit = (
            dict(primary_metrics_payload or {})
            if bool((primary_metrics_payload or {}).get("portfolio_engine_used"))
            else (dict(primary_results[0] or {}) if primary_results else (dict(results[0] or {}) if results else {}))
        )
        event_window_config = {
            "event_window": dict(research_task.get("event_window") or {}),
            "estimation_window": dict(research_task.get("estimation_window") or {}),
            "holding_window": dict(research_task.get("holding_window") or {}),
        }
        contamination_summary = {
            "validation_focus": validation_focus,
            "target_code_count": len(target_codes),
            "representative_code_count": len([code for code in evaluated_codes if code not in target_set]),
            "representative_included": bool([code for code in evaluated_codes if code not in target_set]),
            "mixed_layer_used": primary_layer == "combined",
        }

        def _finalize_result(result_payload: dict[str, Any]) -> dict[str, Any]:
            payload = dict(result_payload or {})
            payload["target_quality_summary"] = build_target_quality_gate_summary(
                candidate,
                backtest_result=payload,
            )
            payload["backtest_metrics_contract"] = self._build_backtest_metrics_contract(candidate, payload)
            payload["backtest_metrics_contract_status"] = str(
                dict(payload.get("backtest_metrics_contract") or {}).get("status") or "missing"
            ).strip().lower() or "missing"
            return payload

        base_result = {
            "passed": False,
            "reason_code": "unknown",
            "reason": "初筛回测未完成",
            "strategy_type": strategy_type,
            "sample_count": len(primary_results),
            "required_sample_count": required_sample_count,
            "evaluated_code_count": len(evaluated_codes),
            "successful_code_count": len(successful_codes),
            "evaluated_codes": evaluated_codes,
            "successful_codes": successful_codes,
            "target_codes": target_codes,
            "representative_codes": representative_codes,
            "code_source": code_source,
            "primary_layer": primary_layer,
            "primary_validation_layer": primary_layer,
            "validation_focus": validation_focus,
            "candidate_contract_hash": contract_hash,
            "execution_contract_hash": execution_contract_hash,
            "tested_object_hash": tested_object_hash,
            "candidate_identity_signature": candidate_identity_signature or None,
            "candidate_contract_snapshot": contract_snapshot,
            "logic_signature": str(candidate.get("logic_signature") or "").strip() or None,
            "dsl_signature": str(candidate.get("dsl_signature") or "").strip() or None,
            "factor_signature": str(candidate.get("factor_signature") or "").strip() or None,
            "entry_exit_signature": str(candidate.get("entry_exit_signature") or "").strip() or None,
            "queue_wait_ms": 0.0,
            "backtest_run_ms": round((time.perf_counter() - candidate_started_at) * 1000, 2),
            "code_run_ms_total": round(code_run_ms_total, 2),
            "code_run_count": len(code_results),
            "avg_code_ms": round(code_run_ms_total / len(code_results), 2) if code_results else 0.0,
            "kline_cache_hit_count": kline_cache_hit_count,
            "skipped_codes": skipped_codes,
            "failed_codes": failed_codes,
            "thresholds": thresholds,
            "constraint_check": dict(candidate.get("constraint_check") or {}),
            "event_window_config": event_window_config,
            "contamination_summary": contamination_summary,
            "cost_assumptions": dict(sample_audit.get("cost_assumptions") or {}),
            "explicit_cost_breakdown": dict(sample_audit.get("explicit_cost_breakdown") or {}),
            "implicit_cost_breakdown": dict(sample_audit.get("implicit_cost_breakdown") or {}),
            "tradability_summary": dict(sample_audit.get("tradability_summary") or {}),
            "capacity_summary": dict(sample_audit.get("capacity_summary") or {}),
            "economic_semantics": {
                "holding_rationale": candidate.get("holding_rationale"),
                "alpha_half_life": candidate.get("alpha_half_life"),
                "market_regime_assumption": candidate.get("market_regime_assumption"),
                "position_sizing_rationale": candidate.get("position_sizing_rationale"),
                "capacity_bucket": candidate.get("capacity_bucket")
                or dict(candidate.get("execution_assumptions") or {}).get("capacity_bucket"),
                "turnover_cost_class": candidate.get("turnover_cost_class")
                or dict(candidate.get("execution_assumptions") or {}).get("turnover_cost_class"),
                "expected_turnover_band": candidate.get("expected_turnover_band")
                or dict(candidate.get("holding_horizon") or {}).get("expected_turnover_band"),
                "economic_semantics_score": candidate.get("economic_semantics_score"),
                "economic_semantics_missing_fields": list(
                    candidate.get("economic_semantics_missing_fields") or []
                ),
            },
            "implementation_shortfall_model_source": sample_audit.get("implementation_shortfall_model_source"),
            "implementation_shortfall_components": dict(sample_audit.get("implementation_shortfall_components") or {}),
            "position_assumption": sample_audit.get("position_assumption"),
            "backtest_assumptions": assumptions.to_audit_dict(),
            "portfolio_backtest_mode": (
                "portfolio_engine_shared_cash"
                if bool((primary_metrics_payload or {}).get("portfolio_engine_used"))
                else (
                    "weighted_multi_name"
                    if assumptions.target_weight_scheme != "single_name" and len(target_codes) > 1
                    else "single_name"
                )
            ),
            "portfolio_backtest_coverage": (
                1.0
                if bool((primary_metrics_payload or {}).get("portfolio_engine_used"))
                else (
                    1.0
                    if assumptions.target_weight_scheme != "single_name" and len(target_codes) > 1 and bool(target_metrics)
                    else 0.0
                )
            ),
                "layers": {
                    "target": {
                        "requested_codes": target_codes,
                        "successful_codes": layer_successful_codes["target"],
                        "sample_count": len(layer_results["target"]),
                        "metrics": target_metrics,
                        "trade_profile": target_trade_profile,
                        "metrics_source": "portfolio_engine" if target_portfolio_metrics else "proxy_aggregation",
                    },
                    "representative": {
                        "requested_codes": representative_codes,
                        "successful_codes": layer_successful_codes["representative"],
                        "sample_count": len(layer_results["representative"]),
                        "metrics": representative_metrics,
                        "trade_profile": representative_trade_profile,
                        "metrics_source": "proxy_aggregation",
                    },
                    "combined": {
                        "requested_codes": evaluated_codes,
                        "successful_codes": successful_codes,
                        "sample_count": len(results),
                        "metrics": combined_metrics,
                        "trade_profile": combined_trade_profile,
                        "metrics_source": "portfolio_engine" if combined_portfolio_metrics else "proxy_aggregation",
                    },
                },
            "event_window_metrics": dict(event_window_metrics or target_metrics or combined_metrics),
            "metrics": {},
            "failed_metric": None,
        }

        primary_successful_codes = (
            list(layer_successful_codes["target"])
            if primary_layer == "target"
            else list(successful_codes)
        )
        if (
            has_explicit_research_task
            and
            assumptions.target_weight_scheme != "single_name"
            and len(primary_successful_codes) > 1
            and not bool((primary_metrics_payload or {}).get("portfolio_engine_used"))
        ):
            return _finalize_result({
                **base_result,
                "reason_code": "portfolio_engine_required",
                "reason": "多标的验证未获得真实组合回测结果",
                "failed_metric": self._build_failed_metric(
                    "portfolio_backtest_coverage",
                    "<",
                    1.0,
                    round(float(base_result.get("portfolio_backtest_coverage") or 0.0), 4),
                    "组合回测覆盖度",
                ),
            })

        if (
            str(research_task.get("task_source") or "").strip().lower() == "event_driven"
            and bool((event_window_metrics or {}).get("event_audit_incomplete"))
        ):
            return _finalize_result({
                **base_result,
                "reason_code": "event_audit_incomplete",
                "reason": "事件验证仅有最小样本代理，缺少正式事件审计证据",
                "failed_metric": self._build_failed_metric(
                    "event_audit_incomplete",
                    "==",
                    False,
                    bool((event_window_metrics or {}).get("event_audit_incomplete")),
                    "事件审计完整性",
                ),
            })

        if (
            str(research_task.get("task_source") or "").strip().lower() == "event_driven"
            and not bool((event_window_metrics or {}).get("traceable_to_event_samples"))
        ):
            return _finalize_result({
                **base_result,
                "reason_code": "event_samples_required",
                "reason": "事件验证缺少可追溯事件样本",
                "failed_metric": self._build_failed_metric(
                    "event_sample_count",
                    ">",
                    0,
                    int((event_window_metrics or {}).get("event_sample_count") or 0),
                    "事件样本数",
                ),
            })

        if len(primary_results) < required_sample_count:
            return _finalize_result({
                **base_result,
                "reason_code": "insufficient_samples",
                "reason": f"有效样本 {len(primary_results)} 小于要求 {required_sample_count}",
                "failed_metric": self._build_failed_metric("sample_count", "<", required_sample_count, len(primary_results), "有效样本数"),
            })

        avg = (
            dict(primary_metrics_payload or {})
            if bool((primary_metrics_payload or {}).get("portfolio_engine_used"))
            else self._summarize_result_set(
                primary_results,
                target_weight_scheme=assumptions.target_weight_scheme,
                initial_capital=assumptions.initial_capital,
                target_weight_map=assumptions.target_weight_map,
            )
        )

        # Parameter-perturbation study (submission-side evidence). Bounded: one
        # primary code x up to 4 numeric params x 4 factors = 16 backtests,
        # capped at 8 variants. Fail-closed: unavailable klines / no numeric
        # params / all runs failed -> stability=None with a reason, never 0.
        per_code_results: dict[str, dict] = {}
        for _r in primary_results:
            _rc = _r.get("code")
            if _rc and isinstance(_r, dict):
                per_code_results.setdefault(_rc, dict(_r.get("data") or {}))
        if avg and primary_successful_codes:
            study = await self._run_parameter_perturbation_study(
                candidate,
                engine,
                primary_code=primary_successful_codes[0],
                strategy_type=strategy_type,
                assumptions_kwargs=assumptions.to_backtest_kwargs(),
                baseline_metrics=per_code_results.get(primary_successful_codes[0]),
            )
            if study:
                avg.update(study)

        # ══════════════════════════════════════════════════════════════════
        # Gate-2 自适应评估
        # ══════════════════════════════════════════════════════════════════
        if avg and not bool(avg.get("trade_profile_available")):
            avg = {
                **avg,
                **self._aggregate_trade_profile_metrics(
                    primary_results,
                    initial_capital=assumptions.initial_capital,
                ),
            }

        strategy_sharpe = float(avg.get("sharpe_ratio", 0))
        strategy_return = float(avg.get("total_return", 0))
        strategy_trades = float(avg.get("trades_count", 0))
        strategy_mdd = abs(float(avg.get("max_drawdown", 0)))

        # 基准收益估算（从回测结果中提取）
        benchmark_returns_g2 = []
        for pr in primary_results:
            pr_data = pr.get("data") or {}
            bh = pr_data.get("buy_hold_return") or pr_data.get("benchmark_return")
            if bh is not None:
                benchmark_returns_g2.append(float(bh))
        avg_benchmark_return_g2 = (
            sum(benchmark_returns_g2) / len(benchmark_returns_g2)
            if benchmark_returns_g2
            else 0.0
        )

        # 市场状态
        if avg_benchmark_return_g2 > 0.10:
            regime_g2 = "bull"
            regime_sharpe_min_g2 = thresholds["sharpe_min"]
        elif avg_benchmark_return_g2 < -0.05:
            regime_g2 = "bear"
            regime_sharpe_min_g2 = -0.30
        else:
            regime_g2 = "neutral"
            regime_sharpe_min_g2 = 0.05

        # 超额 Sharpe
        benchmark_sharpe_g2 = avg_benchmark_return_g2 * 4.0  # 简化年化
        excess_sharpe_g2 = strategy_sharpe - benchmark_sharpe_g2

        # 多维度评分
        multi_score_g2 = 0.0
        excess_return_g2 = strategy_return - avg_benchmark_return_g2
        if excess_return_g2 > 0.05:
            multi_score_g2 += 30
        elif excess_return_g2 > 0.0:
            multi_score_g2 += 20
        elif excess_return_g2 > -0.03:
            multi_score_g2 += 10
        if strategy_trades >= thresholds["trades_min"] and strategy_sharpe > -1.0:
            multi_score_g2 += 25
        elif strategy_trades >= 1:
            multi_score_g2 += 10
        if strategy_mdd < thresholds["mdd_max"] * 0.5:
            multi_score_g2 += 20
        elif strategy_mdd < thresholds["mdd_max"]:
            multi_score_g2 += 12
        if excess_sharpe_g2 >= 0.20:
            multi_score_g2 += 15
        elif excess_sharpe_g2 >= 0.0:
            multi_score_g2 += 8
        if thresholds["trades_min"] <= strategy_trades <= 80:
            multi_score_g2 += 10

        # 软预筛:把 win_rate / profit_factor 纳入多维评分(observe-first 宽进下不硬淘汰,
        # 让交易质量双低的候选评分自然被压低、更难达 50 分线)。
        # 仅在指标真实可得(present_real)时计分;缺数据(0/无交易)不加不减,避免误伤缺数据候选。
        strategy_win_rate = float(avg.get("win_rate", 0) or 0)
        strategy_profit_factor = float(avg.get("profit_factor", 0) or 0)
        quality_score_g2 = 0.0
        if strategy_win_rate > 0:
            if strategy_win_rate >= 0.45:
                quality_score_g2 += 10
            elif strategy_win_rate >= 0.38:
                quality_score_g2 += 6
            elif strategy_win_rate >= 0.30:
                quality_score_g2 += 2
        if strategy_profit_factor > 0:
            if strategy_profit_factor >= 1.8:
                quality_score_g2 += 8
            elif strategy_profit_factor >= 1.3:
                quality_score_g2 += 5
            elif strategy_profit_factor >= 1.0:
                quality_score_g2 += 2
        multi_score_g2 += quality_score_g2

        # 综合判定（任一通过即可）
        passed_absolute_g2 = strategy_sharpe >= thresholds["sharpe_min"]
        passed_relative_g2 = excess_sharpe_g2 >= 0.15
        passed_regime_g2 = strategy_sharpe >= regime_sharpe_min_g2
        passed_multi_g2 = multi_score_g2 >= 50
        gate_2_passed = passed_absolute_g2 or passed_relative_g2 or passed_regime_g2 or passed_multi_g2

        # 硬约束
        if strategy_mdd > thresholds["mdd_max"]:
            gate_2_passed = False
        if strategy_trades < thresholds["trades_min"]:
            gate_2_passed = False

        avg["adaptive_gate_2"] = {
            "regime": regime_g2,
            "multi_score": round(multi_score_g2, 1),
            "quality_score": round(quality_score_g2, 1),
            "win_rate": round(strategy_win_rate, 4),
            "profit_factor": round(strategy_profit_factor, 4),
            "passed_absolute": passed_absolute_g2,
            "passed_relative": passed_relative_g2,
            "passed_regime": passed_regime_g2,
            "passed_multi": passed_multi_g2,
        }

        if not gate_2_passed:
            reason_parts = []
            if strategy_mdd > thresholds["mdd_max"]:
                reason_parts.append(f"mdd={strategy_mdd:.4f}>{thresholds['mdd_max']}")
            elif strategy_trades < thresholds["trades_min"]:
                reason_parts.append(f"trades={strategy_trades:.0f}<{thresholds['trades_min']}")
            else:
                reason_parts.append(f"sharpe={strategy_sharpe:.4f},regime={regime_g2},score={multi_score_g2:.0f}")
            return _finalize_result({
                **base_result,
                "reason_code": "adaptive_gate_2_failed",
                "reason": f"自适应评估未通过: {';'.join(reason_parts)}",
                "metrics": avg,
                "failed_metric": self._build_failed_metric("adaptive_score", "<", 50, round(multi_score_g2, 1), "自适应综合评分"),
            })

        return _finalize_result({
            **base_result,
            "passed": True,
            "reason_code": "passed",
            "reason": "通过初筛回测",
            "metrics": avg,
        })
