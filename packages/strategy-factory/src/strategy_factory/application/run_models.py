"""Run/stage status helpers for strategy factory execution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

from ..infrastructure.env_loader import load_strategy_llm_env


class StageStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"


class FactoryRunStatus(str, Enum):
    SUCCESS = "success"
    SUCCESS_OBSERVE_ONLY = "success_observe_only"
    SUCCESS_NO_SUBMISSION = "success_no_submission"
    SUCCESS_NO_STRATEGY = "success_no_strategy"
    PARTIAL = "partial"  # legacy alias retained for read-path compatibility
    PARTIAL_INFRA = "partial_infra"
    PARTIAL_LLM = "partial_llm"
    SKIPPED = "skipped"
    FAILED = "failed"


_STAGE_STATUS_ALIASES = {
    "completed": StageStatus.COMPLETED,
    "complete": StageStatus.COMPLETED,
    "success": StageStatus.COMPLETED,
    "succeeded": StageStatus.COMPLETED,
    "done": StageStatus.COMPLETED,
    "partial": StageStatus.PARTIAL,
    "degraded": StageStatus.PARTIAL,
    "warning": StageStatus.PARTIAL,
    "warnings": StageStatus.PARTIAL,
    "skipped": StageStatus.SKIPPED,
    "disabled": StageStatus.SKIPPED,
    "noop": StageStatus.SKIPPED,
    "failed": StageStatus.FAILED,
    "error": StageStatus.FAILED,
}

_RUN_STATUS_ALIASES = {
    "success": FactoryRunStatus.SUCCESS,
    "success_observe_only": FactoryRunStatus.SUCCESS_OBSERVE_ONLY,
    "success_no_submission": FactoryRunStatus.SUCCESS_NO_SUBMISSION,
    "success_no_strategy": FactoryRunStatus.SUCCESS_NO_STRATEGY,
    "partial": FactoryRunStatus.PARTIAL,
    "partial_infra": FactoryRunStatus.PARTIAL_INFRA,
    "partial_llm": FactoryRunStatus.PARTIAL_LLM,
    "skipped": FactoryRunStatus.SKIPPED,
    "failed": FactoryRunStatus.FAILED,
}

# P2 (R6): priority ordering for status resolution. Higher index = higher
# priority; ties are broken by the natural order of detection (failed first,
# success last). When multiple conditions trigger we always return the
# highest-priority status. ``PARTIAL`` is a *legacy* status that the new
# resolver never emits — kept in the enum for read-path back-compat.
_FACTORY_RUN_STATUS_PRIORITY: tuple[FactoryRunStatus, ...] = (
    FactoryRunStatus.FAILED,
    FactoryRunStatus.SKIPPED,
    FactoryRunStatus.PARTIAL_INFRA,
    FactoryRunStatus.PARTIAL_LLM,
    FactoryRunStatus.SUCCESS_OBSERVE_ONLY,
    FactoryRunStatus.SUCCESS_NO_STRATEGY,
    FactoryRunStatus.SUCCESS_NO_SUBMISSION,
    FactoryRunStatus.SUCCESS,
)
STAGE_RESULT_CONTRACT_VERSION = 1


def normalize_stage_status(value: Any, default: StageStatus = StageStatus.COMPLETED) -> StageStatus:
    if isinstance(value, StageStatus):
        return value
    token = str(value or "").strip().lower()
    return _STAGE_STATUS_ALIASES.get(token, default)


def normalize_run_status(value: Any, default: FactoryRunStatus = FactoryRunStatus.FAILED) -> FactoryRunStatus:
    if isinstance(value, FactoryRunStatus):
        return value
    token = str(value or "").strip().lower()
    return _RUN_STATUS_ALIASES.get(token, default)


@dataclass(slots=True)
class StageResult:
    stage: str
    trace_id: str
    status: StageStatus
    ok: bool
    hard_failure: bool = False
    degraded: bool = False
    skip_reason: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.payload)
        raw_status = data.pop("status", None)
        data.pop("ok", None)
        data.pop("hard_failure", None)
        data.pop("degraded", None)
        data.pop("skip_reason", None)
        warnings = list(data.get("warnings") or [])
        blockers = list(data.get("blockers") or [])
        persistence_failures = list(data.get("persistence_failures") or [])
        result = {
            "stage": self.stage,
            "trace_id": self.trace_id,
            "stage_contract_version": STAGE_RESULT_CONTRACT_VERSION,
            "status": self.status.value,
            "ok": bool(self.ok),
            "hard_failure": bool(self.hard_failure),
            "degraded": bool(self.degraded),
            "warning_count": int(data.get("warning_count") or len(warnings)),
            "blocker_count": int(data.get("blocker_count") or len(blockers)),
            "persistence_failure_count": int(
                data.get("persistence_failure_count") or len(persistence_failures)
            ),
            **data,
        }
        if self.skip_reason:
            result["skip_reason"] = self.skip_reason
        if raw_status is not None and normalize_stage_status(raw_status) != self.status:
            result["raw_status"] = raw_status
        return result


def build_stage_result(
    stage: str,
    trace_id: str,
    payload: Optional[Mapping[str, Any]] = None,
    *,
    status: StageStatus | str,
    ok: Optional[bool] = None,
    hard_failure: bool = False,
    degraded: Optional[bool] = None,
    skip_reason: Optional[str] = None,
) -> dict[str, Any]:
    effective_status = normalize_stage_status(status)
    effective_payload = dict(payload or {})
    effective_skip_reason = str(
        skip_reason or effective_payload.get("skip_reason") or ""
    ).strip() or None
    effective_degraded = bool(
        effective_payload.get("degraded")
        if degraded is None
        else degraded
    ) or effective_status == StageStatus.PARTIAL
    effective_ok = (
        effective_status != StageStatus.FAILED
        if ok is None
        else bool(ok)
    )
    return StageResult(
        stage=stage,
        trace_id=trace_id,
        status=effective_status,
        ok=effective_ok,
        hard_failure=bool(hard_failure),
        degraded=effective_degraded,
        skip_reason=effective_skip_reason,
        payload=effective_payload,
    ).to_dict()


def summarize_stage_results(stages: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    counts = {item.value: 0 for item in StageStatus}
    failed_stages: list[str] = []
    partial_stages: list[str] = []
    skipped_stages: list[str] = []
    hard_failure_count = 0
    degraded_stage_count = 0
    skip_reasons: list[str] = []

    for stage_name, payload in dict(stages or {}).items():
        stage_payload = dict(payload or {})
        status = normalize_stage_status(stage_payload.get("status"))
        counts[status.value] = counts.get(status.value, 0) + 1
        if status == StageStatus.FAILED:
            failed_stages.append(stage_name)
        elif status == StageStatus.PARTIAL:
            partial_stages.append(stage_name)
        elif status == StageStatus.SKIPPED:
            skipped_stages.append(stage_name)
        if bool(stage_payload.get("hard_failure")):
            hard_failure_count += 1
        if bool(stage_payload.get("degraded")) or status == StageStatus.PARTIAL:
            degraded_stage_count += 1
        reason = str(stage_payload.get("skip_reason") or "").strip()
        if reason:
            skip_reasons.append(reason)

    return {
        "stage_status_counts": counts,
        "failed_stage_count": len(failed_stages),
        "partial_stage_count": len(partial_stages),
        "skipped_stage_count": len(skipped_stages),
        "hard_failure_count": hard_failure_count,
        "degraded_stage_count": degraded_stage_count,
        "failed_stages": failed_stages,
        "partial_stages": partial_stages,
        "skipped_stages": skipped_stages,
        "skip_reasons": skip_reasons,
    }


_INFRA_STAGES: frozenset[str] = frozenset({
    "warmup",
    "collect",
    "snapshot",
    "readiness",
    "persistence",
})

_LLM_TIMEOUT_RATIO_DEFAULT = 0.30
_LLM_NO_SPEC_RATIO_DEFAULT = 0.50
_LLM_PROVIDER_ERROR_RATIO_DEFAULT = 0.30

_FACTOR_RESEARCH_INFRA_SUMMARY_KEYS = (
    "factor_research_db_error_count",
    "factor_research_database_error_count",
    "factor_research_persistence_failure_count",
    "factor_research_runtime_error_count",
)
_FACTOR_RESEARCH_INFRA_ERROR_MARKERS = (
    "database",
    "sqlite",
    "db_error",
    "persistence",
    "runtimeerror",
    "exception",
)
_LLM_PROVIDER_ERROR_KEYS = {
    "provider_http_error",
    "provider_http_502",
    "provider_http_5xx",
    "provider_5xx",
    "http_502",
    "http_5xx",
    "bad_gateway",
    "gateway_error",
    "provider_error",
    "external_provider_error",
    "model_provider_error",
    "upstream_error",
    "api_error",
}
_LLM_PROVIDER_ERROR_KEY_MARKERS = (
    "provider_http",
    "provider_5xx",
    "http_502",
    "http_5xx",
    "bad_gateway",
    "gateway_error",
    "provider_error",
    "upstream_error",
)
_LLM_PROVIDER_ERROR_SUMMARY_KEYS = (
    "provider_http_error_count",
    "provider_http_5xx_count",
    "provider_5xx_error_count",
    "provider_error_count",
    "llm_provider_error_count",
)


def _safe_count(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _resolve_llm_timeout_partial_threshold() -> float:
    """Resolve the LLM timeout-ratio threshold for `partial_llm` (R7.5).

    Reads STRATEGY_FACTORY_LLM_TIMEOUT_PARTIAL_THRESHOLD; clamps to the
    open interval (0, 1); falls back to the default 0.30 on bad input.
    """
    load_strategy_llm_env()
    raw = os.getenv(
        "STRATEGY_FACTORY_LLM_TIMEOUT_PARTIAL_THRESHOLD",
        str(_LLM_TIMEOUT_RATIO_DEFAULT),
    )
    try:
        v = float(raw)
    except Exception:
        return _LLM_TIMEOUT_RATIO_DEFAULT
    if v <= 0.0 or v >= 1.0:
        return _LLM_TIMEOUT_RATIO_DEFAULT
    return v


def _resolve_llm_no_spec_partial_threshold() -> float:
    """Resolve the LLM no-spec ratio threshold for `partial_llm`."""
    load_strategy_llm_env()
    raw = os.getenv(
        "STRATEGY_FACTORY_LLM_NO_SPEC_PARTIAL_THRESHOLD",
        str(_LLM_NO_SPEC_RATIO_DEFAULT),
    )
    try:
        v = float(raw)
    except Exception:
        return _LLM_NO_SPEC_RATIO_DEFAULT
    if v <= 0.0 or v >= 1.0:
        return _LLM_NO_SPEC_RATIO_DEFAULT
    return v


def _resolve_llm_provider_error_partial_threshold() -> float:
    """Resolve the provider-error ratio threshold for `partial_llm`."""
    load_strategy_llm_env()
    raw = os.getenv(
        "STRATEGY_FACTORY_LLM_PROVIDER_ERROR_PARTIAL_THRESHOLD",
        str(_LLM_PROVIDER_ERROR_RATIO_DEFAULT),
    )
    try:
        v = float(raw)
    except Exception:
        return _LLM_PROVIDER_ERROR_RATIO_DEFAULT
    if v <= 0.0 or v >= 1.0:
        return _LLM_PROVIDER_ERROR_RATIO_DEFAULT
    return v


def _is_infra_degraded(
    stages: Mapping[str, Mapping[str, Any]],
    summary: Optional[Mapping[str, Any]] = None,
    *,
    persistence_failure_count: int = 0,
) -> bool:
    """Return True if any infrastructure-class stage is failed or partial,
    or if the run had any persistence failures, or if warmup reported
    failed sync_tasks.

    Infra stages currently include warmup / collect / snapshot / readiness
    / persistence. ``factor_research`` is handled separately: research
    quality shortfalls are not infrastructure failures, but explicit
    runtime/database/persistence failures still are. The ``summary`` overlay
    (if provided) is inspected for ``warmup_failed`` and
    ``sync_task_failed_count`` since cycle_runner_parts/normalizers.py
    already exposes them at top level.
    """
    if _safe_count(persistence_failure_count) > 0:
        return True
    overlay = dict(summary or {})
    if _safe_count(overlay.get("warmup_failed")) > 0:
        return True
    if _safe_count(overlay.get("sync_task_failed_count")) > 0:
        return True
    if any(_safe_count(overlay.get(key)) > 0 for key in _FACTOR_RESEARCH_INFRA_SUMMARY_KEYS):
        return True
    for stage_name, payload in dict(stages or {}).items():
        stage_payload = dict(payload or {})
        if stage_name == "factor_research":
            if _is_factor_research_infra_degraded(stage_payload):
                return True
            continue
        if stage_name not in _INFRA_STAGES:
            continue
        status = normalize_stage_status(stage_payload.get("status"))
        if status in {StageStatus.FAILED, StageStatus.PARTIAL}:
            return True
        if bool(stage_payload.get("hard_failure")):
            return True
    return False


def _is_factor_research_infra_degraded(payload: Mapping[str, Any]) -> bool:
    stage_payload = dict(payload or {})
    status = normalize_stage_status(stage_payload.get("status"))
    hard_failure = bool(stage_payload.get("hard_failure"))
    if status == StageStatus.FAILED and hard_failure:
        return True

    error_text = " ".join(
        str(stage_payload.get(key) or "")
        for key in (
            "error",
            "error_message",
            "exception",
            "exception_type",
            "failure_reason",
            "reason",
        )
    ).strip().lower()
    if not error_text:
        return False
    if status == StageStatus.FAILED and any(marker in error_text for marker in _FACTOR_RESEARCH_INFRA_ERROR_MARKERS):
        return True
    if hard_failure and any(marker in error_text for marker in _FACTOR_RESEARCH_INFRA_ERROR_MARKERS):
        return True
    return False


def _llm_timeout_ratio(summary: Mapping[str, Any]) -> float:
    """Best-effort estimate of the LLM-task timeout ratio for the cycle.

    Falls back to 0 when neither ``llm_status_counts`` nor
    ``task_timeout_skip_count`` are populated.
    """
    overlay = dict(summary or {})
    timeout_skip = int(overlay.get("task_timeout_skip_count") or 0)
    autonomy_total = int(overlay.get("autonomy_task_count") or 0)
    if autonomy_total > 0 and timeout_skip > 0:
        return float(timeout_skip) / float(autonomy_total)

    counts = dict(overlay.get("llm_status_counts") or {})
    if counts:
        total = sum(int(v or 0) for v in counts.values())
        if total > 0:
            timeout_keys = (
                "external_llm_timeout",
                "pipeline_stage_timeout",
                "bulk_research_timeout",
                "timeout",
            )
            timeout_count = sum(int(counts.get(k) or 0) for k in timeout_keys)
            if timeout_count > 0:
                return float(timeout_count) / float(total)

    return 0.0


def _llm_no_spec_ratio(summary: Mapping[str, Any]) -> float:
    overlay = dict(summary or {})
    counts = dict(overlay.get("llm_status_counts") or {})
    if not counts:
        return 0.0
    total = sum(int(v or 0) for v in counts.values())
    if total <= 0:
        return 0.0
    no_spec_keys = ("non_executable", "returned_empty", "schema_invalid",
                    "empty_output")
    no_spec = sum(int(counts.get(k) or 0) for k in no_spec_keys)
    return float(no_spec) / float(total) if no_spec > 0 else 0.0


def _is_llm_provider_error_key(key: Any) -> bool:
    token = str(key or "").strip().lower()
    if not token:
        return False
    if token in {"target_context_blocked", "local_fallback_preferred_or_skip"}:
        return False
    if token in _LLM_PROVIDER_ERROR_KEYS:
        return True
    return any(marker in token for marker in _LLM_PROVIDER_ERROR_KEY_MARKERS)


def _llm_provider_error_count(summary: Mapping[str, Any]) -> int:
    overlay = dict(summary or {})
    count = sum(_safe_count(overlay.get(key)) for key in _LLM_PROVIDER_ERROR_SUMMARY_KEYS)
    for bucket_name in ("llm_status_counts", "external_llm_status_counts"):
        for key, value in dict(overlay.get(bucket_name) or {}).items():
            if _is_llm_provider_error_key(key):
                count += _safe_count(value)
    # pipeline_fallback_counts.cooldown_skip 表示因 provider 近期过载/超时进入冷却而跳过请求，
    # 属 provider 侧降级，应计入 provider 错误；local_fallback_preferred_or_skip /
    # target_context_blocked 由 _is_llm_provider_error_key 显式排除，不计入。
    for key, value in dict(overlay.get("pipeline_fallback_counts") or {}).items():
        token = str(key or "").strip().lower()
        if token == "cooldown_skip" or _is_llm_provider_error_key(key):
            count += _safe_count(value)
    return count


def _llm_provider_error_ratio(summary: Mapping[str, Any]) -> float:
    overlay = dict(summary or {})
    provider_error_count = _llm_provider_error_count(overlay)
    if provider_error_count <= 0:
        return 0.0

    autonomy_total = _safe_count(overlay.get("autonomy_task_count"))
    if autonomy_total > 0:
        return float(provider_error_count) / float(autonomy_total)

    counts = dict(overlay.get("llm_status_counts") or overlay.get("external_llm_status_counts") or {})
    total = sum(_safe_count(v) for v in counts.values())
    if total > 0:
        return float(provider_error_count) / float(total)

    request_total = max(
        _safe_count(overlay.get("external_llm_real_request_count")),
        _safe_count(overlay.get("external_llm_network_request_count")),
        _safe_count(overlay.get("external_llm_attempt_count")),
    )
    if request_total > 0:
        return float(provider_error_count) / float(request_total)

    return 1.0


def _is_llm_degraded(
    summary: Mapping[str, Any] | None,
    *,
    timeout_threshold: float | None = None,
    no_spec_threshold: float | None = None,
    provider_error_threshold: float | None = None,
) -> bool:
    """Return True if LLM-side metrics for the cycle are degraded enough
    to warrant ``partial_llm`` status (R7.5).

    Three independent conditions are OR-ed:
      - timeout-task ratio exceeds ``timeout_threshold``
        (default from STRATEGY_FACTORY_LLM_TIMEOUT_PARTIAL_THRESHOLD, 0.30);
      - "no executable spec" ratio exceeds ``no_spec_threshold``
        (default from STRATEGY_FACTORY_LLM_NO_SPEC_PARTIAL_THRESHOLD, 0.50).
      - provider-error ratio exceeds ``provider_error_threshold``
        (default from STRATEGY_FACTORY_LLM_PROVIDER_ERROR_PARTIAL_THRESHOLD,
        0.30).
    """
    overlay = dict(summary or {})
    llm_status_counts = dict(overlay.get("llm_status_counts") or {})
    local_fallback_count = max(
        _safe_count(overlay.get("research_local_rule_candidate_count")),
        _safe_count(llm_status_counts.get("fallback_only")),
        _safe_count(llm_status_counts.get("local_rule")),
        _safe_count(llm_status_counts.get("template")),
    )
    external_effective_count = max(
        _safe_count(overlay.get("external_llm_effective_response_count")),
        _safe_count(overlay.get("research_external_autonomy_candidate_count")),
    )
    if local_fallback_count > 0 and external_effective_count <= 0:
        return True
    eff_timeout_threshold = (
        float(timeout_threshold)
        if timeout_threshold is not None
        else _resolve_llm_timeout_partial_threshold()
    )
    eff_no_spec_threshold = (
        float(no_spec_threshold)
        if no_spec_threshold is not None
        else _resolve_llm_no_spec_partial_threshold()
    )
    eff_provider_error_threshold = (
        float(provider_error_threshold)
        if provider_error_threshold is not None
        else _resolve_llm_provider_error_partial_threshold()
    )
    if _llm_timeout_ratio(overlay) > eff_timeout_threshold:
        return True
    if _llm_no_spec_ratio(overlay) > eff_no_spec_threshold:
        return True
    # Provider-side 错误判定（http 5xx / 网关错误 / cooldown_skip 等）：
    # - 当存在可靠分母 autonomy_task_count 时，用比例阈值判定（单个错误占比低于阈值不算降级，
    #   与 strategy-factory cycle-status 契约一致：1/6 provider_error 不升级 partial_llm）；
    # - 当缺少 autonomy_task_count（如运维诊断仅给 pipeline_fallback_counts / provider 计数）时，
    #   无法计算有意义的比例，任何 provider 硬错误都判定为降级。
    autonomy_total = _safe_count(overlay.get("autonomy_task_count"))
    if autonomy_total <= 0 and _llm_provider_error_count(overlay) > 0:
        return True
    if _llm_provider_error_ratio(overlay) > eff_provider_error_threshold:
        return True
    return False


def resolve_run_status(
    current_status: FactoryRunStatus | str,
    stages: Mapping[str, Mapping[str, Any]],
    *,
    persistence_failure_count: int = 0,
    summary: Optional[Mapping[str, Any]] = None,
) -> FactoryRunStatus:
    """Compute the cycle's final ``status`` per R6.

    The resolver follows the priority order in
    ``_FACTORY_RUN_STATUS_PRIORITY``: ``failed > skipped > partial_infra >
    partial_llm > success_no_strategy > success_no_submission > success``.

    The new path never emits ``PARTIAL`` (legacy). The ``current_status``
    argument is honored only when it is already ``SKIPPED`` or ``FAILED``
    (the cycle decided to short-circuit upstream); ``PARTIAL`` from the
    legacy path is treated as "needs reclassification".

    The ``summary`` argument is the running ``results["summary"]`` dict.
    It is optional for backward compatibility: when omitted the resolver
    falls back to the old simple ``stage failed/partial -> PARTIAL`` rule
    so existing callers that haven't been updated still produce a sane
    legacy value.
    """
    normalized_current = normalize_run_status(current_status)
    if normalized_current in {FactoryRunStatus.SKIPPED, FactoryRunStatus.FAILED}:
        return normalized_current

    stage_summary = summarize_stage_results(stages)

    # If caller didn't supply summary, use the old behavior. This keeps
    # legacy callers (and old test fixtures) green without forcing a flag day.
    if summary is None:
        if (
            stage_summary["failed_stage_count"] > 0
            or stage_summary["partial_stage_count"] > 0
            or int(persistence_failure_count or 0) > 0
        ):
            return FactoryRunStatus.PARTIAL
        return FactoryRunStatus.SUCCESS

    # New path: classify by category.
    # 1) Hard failures escalate to FAILED only if the entire pipeline
    #    aborted; that's a stronger signal than "an infra stage degraded".
    if stage_summary["failed_stage_count"] > 0 \
            and stage_summary["hard_failure_count"] > 0 \
            and stage_summary["failed_stage_count"] >= max(
                1, len(stage_summary["failed_stages"])
            ) \
            and not _is_infra_degraded(stages, summary,
                                       persistence_failure_count=persistence_failure_count):
        # Pure non-infra stage failure with hard_failure=True ⇒ FAILED.
        return FactoryRunStatus.FAILED

    # 2) Infra degradation
    if _is_infra_degraded(stages, summary,
                          persistence_failure_count=persistence_failure_count):
        return FactoryRunStatus.PARTIAL_INFRA

    # 3) LLM degradation
    if _is_llm_degraded(summary):
        return FactoryRunStatus.PARTIAL_LLM

    # 4) Success variants — distinguish by submission outcome
    overlay = dict(summary or {})
    formal_incubation_count = max(
        _safe_count(overlay.get("formal_incubation_count")),
        _safe_count(dict(overlay.get("submission_lane_counts") or {}).get("formal_incubation")),
    )
    observed_candidate_count = max(
        _safe_count(overlay.get("observed_candidate_count")),
        _safe_count(overlay.get("observe_admitted_count")),
        _safe_count(overlay.get("observe_incubation_count")),
        _safe_count(overlay.get("diagnostic_observation_count")),
    )
    if formal_incubation_count > 0:
        return FactoryRunStatus.SUCCESS
    if observed_candidate_count > 0:
        return FactoryRunStatus.SUCCESS_OBSERVE_ONLY

    gate_3_passed = int(overlay.get("gate_3_passed") or 0)
    submitted = int(overlay.get("submitted") or 0)

    if gate_3_passed <= 0:
        return FactoryRunStatus.SUCCESS_NO_STRATEGY
    if submitted <= 0:
        return FactoryRunStatus.SUCCESS_NO_SUBMISSION
    return FactoryRunStatus.SUCCESS_NO_SUBMISSION


def is_product_success_status(value: Any) -> bool:
    """Return whether a run status represents full product success."""

    return normalize_run_status(value) is FactoryRunStatus.SUCCESS


__all__ = [
    "FactoryRunStatus",
    "StageResult",
    "STAGE_RESULT_CONTRACT_VERSION",
    "StageStatus",
    "build_stage_result",
    "is_product_success_status",
    "normalize_run_status",
    "normalize_stage_status",
    "resolve_run_status",
    "summarize_stage_results",
]
