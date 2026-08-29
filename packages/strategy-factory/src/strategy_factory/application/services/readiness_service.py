"""ReadinessService – evaluates factory readiness from snapshot + factor data.

Extracted from FactoryCycleRunner._build_factory_readiness (P4 refactor).
The service is stateless and can be called independently of the scheduler.
"""

from __future__ import annotations

from typing import Any

from ...domain.constants import (
    FACTORY_READINESS_MIN_COMPLETION_RATIO,
    FACTORY_READINESS_MIN_SCORE,
    is_factory_factor_auto_refresh_enabled,
    is_factory_readiness_hard_block_enabled,
    is_factory_runtime_enabled,
    resolve_factory_readiness_min_governed_active_candidates,
    resolve_factory_readiness_min_governed_active_families,
    resolve_event_runtime_mode,
)

READINESS_CONTRACT_VERSION = "strategy_factory.readiness.v1"
READINESS_AUTHORITY_CONTRACT_VERSION = "strategy_factory.readiness.authority.v1"


def _dedupe_reason_codes(values: list[str] | None = None) -> list[str]:
    codes: list[str] = []
    for value in list(values or []):
        code = str(value or "").strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def build_readiness_authority(
    *,
    can_proceed: bool,
    hard_gate_enabled: bool,
    blocking_reason_codes: list[str] | None = None,
    critical_blocking_reason_codes: list[str] | None = None,
    warning_reason_codes: list[str] | None = None,
    effective_blocking_reason_codes: list[str] | None = None,
    skip_reason: str | None = None,
    blocking_stage: str = "readiness",
) -> dict[str, Any]:
    blocked = not bool(can_proceed)
    blocking_codes = _dedupe_reason_codes(blocking_reason_codes)
    critical_codes = _dedupe_reason_codes(critical_blocking_reason_codes)
    warning_codes = _dedupe_reason_codes(warning_reason_codes)
    effective_codes = _dedupe_reason_codes(
        effective_blocking_reason_codes
        if effective_blocking_reason_codes is not None
        else (blocking_codes if blocking_codes else warning_codes)
    )
    blocking_reason_codes_source = "none"
    if blocked:
        if blocking_codes:
            blocking_reason_codes_source = "blockers"
        elif effective_codes:
            blocking_reason_codes_source = "warnings"
    return {
        "authority_contract_version": READINESS_AUTHORITY_CONTRACT_VERSION,
        "decision": "proceed" if not blocked else "blocked",
        "blocked": blocked,
        "hard_gate": bool(hard_gate_enabled),
        "gate_mode": "hard" if hard_gate_enabled else "soft",
        "blocking_stage": blocking_stage if blocked else None,
        "blocking_reason_codes": blocking_codes if blocked else [],
        "effective_blocking_reason_codes": effective_codes if blocked else [],
        "raw_blocking_reason_codes": blocking_codes if blocked else [],
        "blocking_reason_codes_source": blocking_reason_codes_source,
        "critical_blocking_reason_codes": critical_codes if blocked else [],
        "warning_reason_codes": warning_codes,
        "skip_reason": (str(skip_reason or "").strip() or None) if blocked else None,
    }


def resolve_governed_pool_state(factor_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = dict(factor_summary or {})
    factor_source_mode = str(summary.get("factor_source_mode") or "").strip().lower()
    active_candidate_count = int(summary.get("active_candidate_count") or 0)
    governed_source_candidate_count = int(summary.get("governed_source_candidate_count") or 0)
    governed_candidate_pool_mode = (
        str(
            summary.get("governed_candidate_pool_mode")
            or summary.get("active_pool_mode")
            or ""
        ).strip().lower()
        or None
    )
    governed_candidate_pool_provisional = bool(
        summary.get("governed_candidate_pool_provisional")
    ) or governed_candidate_pool_mode == "provisional_validated_watch"
    governed_candidate_pool_active = bool(
        active_candidate_count > 0
        and (
            factor_source_mode == "governed_candidate_pool"
            or factor_source_mode == "active_factor_pool_fallback"
            or governed_source_candidate_count > 0
            or governed_candidate_pool_mode in {
                "strict_governed",
                "provisional_validated_watch",
                "active_factor_pool_fallback",
            }
        )
    )
    return {
        "mode": governed_candidate_pool_mode,
        "active": governed_candidate_pool_active,
        "provisional": governed_candidate_pool_provisional,
    }


def _has_legacy_factor_signal_summary(factor_summary: dict[str, Any] | None = None) -> bool:
    summary = dict(factor_summary or {})
    factor_source_mode = str(summary.get("factor_source_mode") or "").strip().lower()
    if factor_source_mode:
        return False
    if int(summary.get("active_factor_count") or 0) > 0:
        return True
    if list(summary.get("top_factor_names") or []):
        return True
    if list(summary.get("preferred_strategy_types") or []):
        return True
    return False


def resolve_factor_refresh_trigger(
    factor_research: dict[str, Any] | None = None,
    *,
    factor_summary: dict[str, Any] | None = None,
) -> str | None:
    """Return the preferred scheduler refresh trigger for a factor artifact."""

    artifact = dict(factor_research or {})
    summary = dict(factor_summary or artifact.get("summary") or {})
    if bool(artifact.get("lightweight_mock_fallback")) or bool(summary.get("lightweight_mock_fallback")):
        return None
    factor_source_mode = str(summary.get("factor_source_mode") or "").strip().lower()
    active_candidate_count = int(summary.get("active_candidate_count") or 0)
    governed_source_candidate_count = int(summary.get("governed_source_candidate_count") or 0)
    scheduler_recent_success = bool(summary.get("scheduler_recent_success"))
    scheduler_last_run = summary.get("scheduler_last_run")
    governed_pool_state = resolve_governed_pool_state(summary)
    governed_candidate_pool_active = bool(governed_pool_state.get("active"))

    if bool(summary.get("stale")):
        return "stale_artifact"
    if bool(summary.get("governed_pool_missing_after_scheduler_success")) or (
        scheduler_recent_success and not governed_candidate_pool_active
    ):
        return "governed_pool_missing_after_scheduler_success"
    if governed_candidate_pool_active:
        return None
    if _has_legacy_factor_signal_summary(summary):
        return None
    if factor_source_mode == "seed_fallback":
        return (
            "seed_fallback_without_governed_pool"
            if scheduler_last_run
            else "scheduler_warmup_missing_governed_pool"
        )
    if not scheduler_last_run and active_candidate_count <= 0 and governed_source_candidate_count <= 0:
        return "scheduler_warmup_missing_governed_pool"
    return None


def resolve_governed_pool_runtime_state(
    factor_summary: dict[str, Any] | None = None,
    *,
    factor_refresh: dict[str, Any] | None = None,
    factor_refresh_recommendation_reason: str | None = None,
) -> str:
    summary = dict(factor_summary or {})
    refresh = dict(factor_refresh or {})
    governed_pool_state = resolve_governed_pool_state(summary)
    governed_candidate_pool_active = bool(governed_pool_state.get("active"))
    scheduler_recent_success = bool(summary.get("scheduler_recent_success"))
    factor_source_mode = str(summary.get("factor_source_mode") or "").strip().lower()
    refresh_attempted = bool(refresh.get("refresh_attempted"))
    refresh_status = str(refresh.get("refresh_status") or "").strip().lower()
    auto_refresh_enabled = bool(refresh.get("auto_refresh_enabled"))
    governed_pool_missing_after_scheduler_success = bool(
        factor_source_mode == "governed_pool_missing_after_scheduler_success"
        or bool(summary.get("governed_pool_missing_after_scheduler_success"))
        or (scheduler_recent_success and not governed_candidate_pool_active)
    )
    if governed_candidate_pool_active:
        return "governed_pool_active"
    if governed_pool_missing_after_scheduler_success:
        return "blocked_by_governed_pool"
    if refresh_attempted and refresh_status in {"failed", "timeout"}:
        return "blocked_by_governed_pool"
    if auto_refresh_enabled and (refresh_attempted or factor_refresh_recommendation_reason):
        return "refreshing_pool"
    return "blocked_by_governed_pool"


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _safe_optional_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_optional_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_status(value: Any) -> str | None:
    status = str(value or "").strip().lower()
    return status or None


def _safe_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _resolve_market_temperature_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    market_internals = (
        dict(snapshot.get("market_internals") or {})
        if isinstance(snapshot.get("market_internals"), dict)
        else {}
    )
    candidates = [
        snapshot.get("market_temperature_context"),
        snapshot.get("market_temperature"),
        snapshot.get("market_temperature_snapshot"),
        market_internals.get("market_temperature"),
    ]
    if (
        not any(candidate is not None and candidate != {} for candidate in candidates)
        and isinstance(snapshot.get("market"), dict)
        and (
            str(snapshot.get("contract_version") or "").startswith("market_temperature")
            or isinstance(snapshot.get("quality"), dict)
        )
    ):
        candidates.append(snapshot)

    for candidate in candidates:
        if candidate in (None, ""):
            continue
        if isinstance(candidate, dict):
            payload = dict(candidate)
        else:
            payload = {"temperature": candidate}
        if payload:
            return payload
    return {}


def resolve_market_temperature_context(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize optional market-temperature context already present in a snapshot."""

    root = dict(snapshot or {}) if isinstance(snapshot, dict) else {}
    payload = _resolve_market_temperature_payload(root)
    if not payload:
        return {
            "available": False,
            "as_of": None,
            "temperature": None,
            "state": None,
            "quality_status": None,
            "readiness_status": None,
            "staleness_days": None,
            "degraded": False,
            "warnings": [],
            "source_chain": [],
        }

    market = dict(payload.get("market") or {}) if isinstance(payload.get("market"), dict) else {}
    quality = dict(payload.get("quality") or {}) if isinstance(payload.get("quality"), dict) else {}
    readiness = (
        dict(payload.get("readiness") or payload.get("cache_readiness") or {})
        if isinstance(payload.get("readiness") or payload.get("cache_readiness"), dict)
        else {}
    )
    cache = dict(payload.get("cache") or {}) if isinstance(payload.get("cache"), dict) else {}

    temperature = _safe_optional_float(
        _first_present(payload.get("temperature"), payload.get("market_temperature"), market.get("temperature"))
    )
    as_of = _first_present(
        payload.get("as_of"),
        payload.get("date"),
        payload.get("data_timestamp"),
        cache.get("as_of"),
        readiness.get("as_of"),
    )
    state = _safe_status(
        _first_present(payload.get("state"), payload.get("market_state"), market.get("state"))
    )
    quality_status = _safe_status(
        _first_present(payload.get("quality_status"), quality.get("status"))
    )
    readiness_status = _safe_status(
        _first_present(
            payload.get("readiness_status"),
            readiness.get("readiness_status"),
            readiness.get("status"),
        )
    )
    staleness_days = _safe_optional_float(
        _first_present(payload.get("staleness_days"), readiness.get("staleness_days"))
    )
    warnings = _safe_text_list(payload.get("warnings"))
    for warning in _safe_text_list(quality.get("warnings")) + _safe_text_list(readiness.get("warnings")):
        if warning not in warnings:
            warnings.append(warning)
    source_chain = _safe_text_list(payload.get("source_chain"))
    degraded_statuses = {"degraded", "empty", "failed", "failure", "unavailable", "missing"}
    stale_statuses = {"stale", "expired"}
    not_ready_statuses = {"missing", "failed", "failure", "unavailable", "not_ready", "blocked"}
    degraded = bool(payload.get("degraded")) or quality_status in degraded_statuses
    stale = readiness_status in stale_statuses or quality_status in stale_statuses
    not_ready = readiness_status in not_ready_statuses

    available = bool(
        temperature is not None
        or as_of
        or state
        or quality_status
        or readiness_status
        or source_chain
    )
    return {
        "available": available,
        "as_of": str(as_of).strip() if as_of not in (None, "") else None,
        "temperature": temperature,
        "state": state,
        "quality_status": quality_status,
        "readiness_status": readiness_status,
        "staleness_days": staleness_days,
        "degraded": degraded,
        "stale": stale,
        "not_ready": not_ready,
        "warnings": warnings,
        "source_chain": source_chain,
        "stock_count": _safe_optional_int(
            _first_present(payload.get("stock_count"), payload.get("sample_count"), market.get("stock_count"))
        ),
        "industry_count": _safe_optional_int(
            _first_present(payload.get("industry_count"), quality.get("industry_count"))
        ),
        "contract_version": _first_present(payload.get("contract_version"), root.get("contract_version")),
    }


class ReadinessService:
    """Stateless evaluator for factory readiness.

    Accepts a market snapshot and factor research artifact, returns a
    structured readiness dict that drives the run/skip decision.
    """

    def evaluate(
        self,
        snapshot: dict[str, Any],
        factor_research: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return readiness assessment dict.

        Args:
            snapshot: Market data snapshot from DataCollector.
            factor_research: Factor research artifact (may be None/empty).

        Returns:
            Dict with ``can_proceed``, ``readiness_score``, ``warnings``,
            ``blockers`` and various diagnostic fields.
        """
        factor_artifact = dict(factor_research or {})
        factor_summary = dict(factor_artifact.get("summary") or {})
        factor_refresh = dict(factor_artifact.get("freshness_repair") or {})
        factor_refresh_recommendation_reason = resolve_factor_refresh_trigger(
            factor_artifact,
            factor_summary=factor_summary,
        )
        factor_source_mode = str(factor_summary.get("factor_source_mode") or "").strip().lower()
        active_candidate_count = int(factor_summary.get("active_candidate_count") or 0)
        governed_source_candidate_count = int(
            factor_summary.get("governed_source_candidate_count") or 0
        )
        governed_pool_state = resolve_governed_pool_state(factor_summary)
        governed_candidate_pool_mode = governed_pool_state.get("mode")
        governed_candidate_pool_provisional = bool(governed_pool_state.get("provisional"))
        governed_candidate_pool_provisional_spillover_policy = dict(
            factor_summary.get("governed_candidate_pool_provisional_spillover_policy") or {}
        )
        governed_blocked_candidate_count = int(
            factor_summary.get("governed_blocked_candidate_count") or 0
        )
        governed_pending_candidate_count = int(
            factor_summary.get("governed_pending_candidate_count") or 0
        )
        governed_ineligible_candidate_count = int(
            factor_summary.get("governed_ineligible_candidate_count") or 0
        )
        governed_candidate_pool_provisional_pending_count = int(
            factor_summary.get("governed_candidate_pool_provisional_pending_count") or 0
        )
        governed_candidate_pool_strict_shortfall_count = int(
            factor_summary.get("governed_candidate_pool_strict_shortfall_count") or 0
        )
        family_preference_order = [
            str(item or "").strip().lower()
            for item in list(factor_summary.get("family_preference_order") or [])
            if str(item or "").strip()
        ]
        family_preference_source_mode = (
            str(factor_summary.get("family_preference_source_mode") or "").strip().lower() or None
        )
        stock_family_allocation_count = int(
            factor_summary.get("stock_family_allocation_count") or 0
        )
        stock_family_allocation_source_mode = (
            str(factor_summary.get("stock_family_allocation_source_mode") or "").strip().lower() or None
        )
        governed_blocked_ratio = self._safe_float(
            factor_summary.get("governed_blocked_ratio"),
            default=0.0,
        )
        governed_pending_ratio = self._safe_float(
            factor_summary.get("governed_pending_ratio"),
            default=0.0,
        )
        budget_feedback_strategy_count = int(
            factor_summary.get("budget_feedback_strategy_count") or 0
        )
        budget_feedback_zero_signal_strategy_count = int(
            factor_summary.get("budget_feedback_zero_signal_strategy_count") or 0
        )
        budget_feedback_zero_signal_ratio = self._safe_float(
            factor_summary.get("budget_feedback_zero_signal_ratio"),
            default=0.0,
        )
        budget_feedback_forward_window_coverage_ratio = self._safe_float(
            factor_summary.get("budget_feedback_forward_window_coverage_ratio"),
            default=1.0,
        )
        budget_feedback_promotion_ready_count = int(
            factor_summary.get("budget_feedback_promotion_ready_count") or 0
        )
        budget_feedback_promotion_ready_ratio = self._safe_float(
            factor_summary.get("budget_feedback_promotion_ready_ratio"),
            default=1.0,
        )
        budget_feedback_promotion_review_count = int(
            factor_summary.get("budget_feedback_promotion_review_count") or 0
        )
        budget_feedback_promotion_review_coverage_ratio = self._safe_float(
            factor_summary.get("budget_feedback_promotion_review_coverage_ratio"),
            default=1.0,
        )
        budget_feedback_evidence_debt_strategy_count = int(
            factor_summary.get("budget_feedback_evidence_debt_strategy_count") or 0
        )
        budget_feedback_evidence_debt_ratio = self._safe_float(
            factor_summary.get("budget_feedback_evidence_debt_ratio"),
            default=0.0,
        )
        paper_observation_backlog_count = int(
            factor_summary.get("paper_observation_backlog_count")
            or factor_summary.get("paper_observation_active_count")
            or 0
        )
        paper_observation_backlog_status = (
            str(factor_summary.get("paper_observation_backlog_status") or "").strip().lower()
            or ("empty" if paper_observation_backlog_count <= 0 else "queued")
        )
        paper_observation_intake_stale = bool(
            factor_summary.get("paper_observation_intake_stale")
            or paper_observation_backlog_status in {"stale", "handoff_failed", "intake_stale"}
        )
        paper_observation_last_recognized_at = factor_summary.get("paper_observation_last_recognized_at")
        incubation_factory_health = dict(factor_summary.get("incubation_factory_health") or {})
        if paper_observation_backlog_count > 0 and incubation_factory_health:
            paper_observation_intake_stale = paper_observation_intake_stale or not bool(
                incubation_factory_health.get("healthy", True)
            )
        feedback_generator_mode_control_mode_counts = dict(
            factor_summary.get("feedback_generator_mode_control_mode_counts") or {}
        )
        suppressed_generator_modes = [
            str(item or "").strip().lower()
            for item in list(factor_summary.get("suppressed_generator_modes") or [])
            if str(item or "").strip()
        ]
        external_llm_provider_control_mode = (
            str(factor_summary.get("external_llm_provider_control_mode") or "").strip().lower()
            or None
        )
        external_llm_provider_control_reasons = [
            str(item or "").strip()
            for item in list(factor_summary.get("external_llm_provider_control_reasons") or [])
            if str(item or "").strip()
        ]
        governed_freshness_days = factor_summary.get("governed_freshness_days")
        scheduler_recent_success = bool(factor_summary.get("scheduler_recent_success"))
        scheduler_llm_validation_status = factor_summary.get("scheduler_llm_validation_status")
        governed_exclusion_reason_counts = dict(
            factor_summary.get("governed_exclusion_reason_counts") or {}
        )
        governed_blocking_reason_counts = dict(
            factor_summary.get("governed_blocking_reason_counts") or {}
        )
        governed_pending_reason_counts = dict(
            factor_summary.get("governed_pending_reason_counts") or {}
        )
        governed_ineligible_reason_counts = dict(
            factor_summary.get("governed_ineligible_reason_counts") or {}
        )
        governed_risk_counts = dict(factor_summary.get("governed_risk_counts") or {})
        active_family_count = len(list(factor_summary.get("active_family_names") or []))
        min_governed_active_candidate_count = (
            resolve_factory_readiness_min_governed_active_candidates()
        )
        min_governed_active_family_count = (
            resolve_factory_readiness_min_governed_active_families()
        )
        governed_candidate_pool_active = bool(governed_pool_state.get("active"))
        governed_pool_missing_after_scheduler_success = bool(
            factor_source_mode == "governed_pool_missing_after_scheduler_success"
            or bool(factor_summary.get("governed_pool_missing_after_scheduler_success"))
            or (scheduler_recent_success and not governed_candidate_pool_active)
        )
        governed_pool_runtime_state = resolve_governed_pool_runtime_state(
            factor_summary,
            factor_refresh=factor_refresh,
            factor_refresh_recommendation_reason=factor_refresh_recommendation_reason,
        )

        sources = dict(snapshot.get("sources") or {})
        event_source = dict(sources.get("event_driven") or {})
        event_state = dict(snapshot.get("event_driven") or {})
        completion = dict(snapshot.get("completeness") or {})
        completion_ratio = self._safe_float(completion.get("completion_ratio"), default=1.0)
        market_temperature_context = resolve_market_temperature_context(snapshot)
        hard_block = is_factory_readiness_hard_block_enabled()
        governed_supply_viable = bool(
            governed_candidate_pool_active
            and active_candidate_count >= min_governed_active_candidate_count
            and active_family_count >= min_governed_active_family_count
            and governed_pool_runtime_state
            not in {"blocked_by_governed_pool", "governed_pool_missing_after_scheduler_success"}
        )
        governed_supply_recoverable = bool(
            governed_source_candidate_count >= min_governed_active_candidate_count
            and (
                governed_candidate_pool_provisional
                or governed_candidate_pool_provisional_pending_count > 0
                or governed_candidate_pool_strict_shortfall_count > 0
                or (active_candidate_count > 0 and active_family_count > 0)
            )
        )
        governed_supply_hard_block = not (
            governed_supply_viable or governed_supply_recoverable
        )
        external_llm_provider_suppress_active = bool(
            external_llm_provider_control_mode in {"suppress", "cooldown"}
            or "external_llm" in suppressed_generator_modes
            or int(feedback_generator_mode_control_mode_counts.get("suppress") or 0) > 0
        )

        event_runtime_mode = resolve_event_runtime_mode()

        warnings: list[str] = []
        blockers: list[str] = []
        critical_blockers: list[str] = []
        score = 1.0

        if bool(snapshot.get("degraded")):
            warnings.append("snapshot_degraded")
            score -= 0.12
        if completion_ratio < FACTORY_READINESS_MIN_COMPLETION_RATIO:
            blockers.append("snapshot_completion_too_low")
            score -= 0.28
        elif completion_ratio < 0.9:
            warnings.append("snapshot_completion_low")
            score -= 0.08

        event_status = str(event_source.get("status") or "unknown").strip().lower() or "unknown"
        if event_status != "success":
            warnings.append(f"event_driven_{event_status}")
            score -= 0.08 if event_status == "partial" else 0.14
        if (
            event_status == "success"
            and event_runtime_mode not in {"readonly", "disabled"}
            and int(event_state.get("tasks_ready_count") or 0) <= 0
        ):
            warnings.append("event_driven_no_ready_tasks")
            score -= 0.03
        if bool(market_temperature_context.get("available")):
            if bool(market_temperature_context.get("degraded")):
                warnings.append("market_temperature_context_degraded")
                score -= 0.03
            market_temperature_staleness = market_temperature_context.get("staleness_days")
            if bool(market_temperature_context.get("stale")) or (
                market_temperature_staleness is not None
                and self._safe_float(market_temperature_staleness, default=0.0) > 3.0
            ):
                warnings.append("market_temperature_context_stale")
                score -= 0.02
            if bool(market_temperature_context.get("not_ready")):
                warnings.append("market_temperature_context_not_ready")
                score -= 0.03

        if bool(factor_summary.get("degraded")):
            warnings.append("factor_research_degraded")
            score -= 0.14
        if governed_candidate_pool_provisional:
            warnings.append("governed_candidate_pool_provisional")
            score -= 0.04
        if external_llm_provider_suppress_active:
            warnings.append(
                "external_llm_provider_cooldown"
                if external_llm_provider_control_mode == "cooldown"
                else "external_llm_provider_suppressed"
            )
            score -= 0.03 if external_llm_provider_control_mode == "cooldown" else 0.05
        if not governed_candidate_pool_active:
            warnings.append("governed_candidate_pool_inactive")
            if governed_supply_recoverable:
                if "governed_candidate_pool_shortfall_recoverable" not in warnings:
                    warnings.append("governed_candidate_pool_shortfall_recoverable")
                score -= 0.10
            else:
                if hard_block:
                    blockers.append("governed_candidate_pool_required")
                    critical_blockers.append("governed_candidate_pool_required")
                elif "governed_candidate_pool_required" not in warnings:
                    warnings.append("governed_candidate_pool_required")
                score -= 0.22
        if governed_blocked_ratio >= 0.25:
            warnings.append("governed_candidate_pool_blocked_candidates")
        if governed_blocked_ratio >= 0.75:
            warnings.append("governed_candidate_pool_blocked_ratio_high")
            if hard_block and governed_supply_hard_block:
                blockers.append("governed_candidate_pool_blocked_ratio_high")
            score -= 0.12
        elif governed_blocked_ratio >= 0.40:
            warnings.append("governed_candidate_pool_blocked_ratio_elevated")
            score -= 0.06
        if governed_pending_ratio >= 0.75:
            warnings.append("governed_candidate_pool_promotion_backlog_high")
            if hard_block and governed_supply_hard_block:
                blockers.append("governed_candidate_pool_promotion_backlog_high")
            score -= 0.06
        elif governed_pending_ratio >= 0.40:
            warnings.append("governed_candidate_pool_promotion_backlog_elevated")
            if hard_block and governed_supply_hard_block:
                blockers.append("governed_candidate_pool_promotion_backlog_elevated")
            score -= 0.03
        if budget_feedback_zero_signal_ratio >= 0.75:
            warnings.append("incubating_zero_signal_backlog_high")
            score -= 0.10
        elif budget_feedback_zero_signal_ratio >= 0.40:
            warnings.append("incubating_zero_signal_backlog_elevated")
            score -= 0.05
        if budget_feedback_forward_window_coverage_ratio <= 0.25:
            warnings.append("incubating_forward_window_coverage_low")
            score -= 0.08
        elif budget_feedback_forward_window_coverage_ratio <= 0.50:
            warnings.append("incubating_forward_window_coverage_elevated")
            score -= 0.04
        if budget_feedback_promotion_ready_ratio <= 0.10 and budget_feedback_strategy_count >= 4:
            warnings.append("incubating_promotion_ready_gap_high")
            score -= 0.05
        elif budget_feedback_promotion_ready_ratio <= 0.25 and budget_feedback_strategy_count >= 3:
            warnings.append("incubating_promotion_ready_gap_elevated")
            score -= 0.03

        # P0-3 fix: factory_readiness_score 与 cohort 实际产出脱节
        # 历史问题(诊断报告 §2.3):143 strategies submitted 但 promotion_ready_rate=0 / zero_signal_rate=100%,
        # 而 readiness_score 仍 0.7-0.84 显示"可继续 proceed",AI Agent 误判系统健康
        # 修复策略:
        #   - cohort >= 50 且 promotion_ready=0: 强制 critical_blocker(integration health 失败)
        #   - cohort >= 50 且 zero_signal_rate >= 0.95: 强制 critical_blocker
        # 这两条阻塞会让 can_proceed=False,readiness_score 降到 0.1 附近,真实反映系统状态
        if budget_feedback_strategy_count >= 50:
            if budget_feedback_promotion_ready_count == 0 and budget_feedback_promotion_ready_ratio <= 0.001:
                if "cohort_promotion_ready_complete_failure" not in warnings:
                    warnings.append("cohort_promotion_ready_complete_failure")
                if "cohort_promotion_ready_complete_failure" not in blockers:
                    blockers.append("cohort_promotion_ready_complete_failure")
                if hard_block:
                    critical_blockers.append("cohort_promotion_ready_complete_failure")
                score -= 0.40
            if budget_feedback_zero_signal_ratio >= 0.95:
                if "cohort_zero_signal_pervasive" not in warnings:
                    warnings.append("cohort_zero_signal_pervasive")
                if "cohort_zero_signal_pervasive" not in blockers:
                    blockers.append("cohort_zero_signal_pervasive")
                if hard_block:
                    critical_blockers.append("cohort_zero_signal_pervasive")
                score -= 0.30
        if (
            budget_feedback_promotion_review_coverage_ratio <= 0.10
            and budget_feedback_strategy_count >= 4
        ):
            warnings.append("incubating_promotion_review_gap_high")
            score -= 0.04
        elif (
            budget_feedback_promotion_review_coverage_ratio <= 0.25
            and budget_feedback_strategy_count >= 3
        ):
            warnings.append("incubating_promotion_review_gap_elevated")
            score -= 0.02
        if (
            budget_feedback_evidence_debt_ratio >= 0.82
            and budget_feedback_strategy_count >= 6
        ):
            warnings.append("incubating_evidence_debt_high")
            severe_governance_drag = (
                governed_pending_ratio >= 0.25
                or governed_blocked_ratio >= 0.25
            )
            if hard_block and severe_governance_drag and governed_supply_hard_block:
                blockers.append("incubating_evidence_debt_high")
            score -= 0.12
        elif budget_feedback_evidence_debt_ratio >= 0.45 and budget_feedback_strategy_count >= 3:
            warnings.append("incubating_evidence_debt_elevated")
            score -= 0.05
        if paper_observation_backlog_count >= 50:
            warnings.append("paper_observation_backlog_high")
            score -= 0.08
        elif paper_observation_backlog_count >= 10:
            warnings.append("paper_observation_backlog_elevated")
            score -= 0.04
        if paper_observation_backlog_count > 0 and paper_observation_intake_stale:
            warnings.append("paper_observation_intake_stale")
            if hard_block:
                blockers.append("paper_observation_intake_stale")
            score -= 0.12
        if governed_pool_missing_after_scheduler_success:
            warnings.append("factor_scheduler_recent_success_without_governed_pool")
            if governed_supply_hard_block:
                if hard_block:
                    blockers.append("governed_candidate_pool_missing_after_scheduler_success")
                    critical_blockers.append("governed_candidate_pool_missing_after_scheduler_success")
                elif "governed_candidate_pool_missing_after_scheduler_success" not in warnings:
                    warnings.append("governed_candidate_pool_missing_after_scheduler_success")
                score -= 0.18
            else:
                score -= 0.08
        if governed_pool_runtime_state == "refreshing_pool":
            warnings.append("governed_candidate_pool_refreshing")
            score -= 0.05
        elif governed_pool_runtime_state == "blocked_by_governed_pool" and not governed_pool_missing_after_scheduler_success:
            warnings.append("governed_candidate_pool_refresh_blocked")
            if governed_supply_hard_block:
                if hard_block:
                    blockers.append("governed_candidate_pool_unavailable_after_refresh")
                    critical_blockers.append("governed_candidate_pool_unavailable_after_refresh")
                elif "governed_candidate_pool_unavailable_after_refresh" not in warnings:
                    warnings.append("governed_candidate_pool_unavailable_after_refresh")
                score -= 0.18
            else:
                score -= 0.10
        if bool(factor_summary.get("stale")):
            if governed_candidate_pool_active:
                warnings.append("factor_research_history_stale_governed_pool_active")
                score -= 0.06
            else:
                blockers.append("factor_research_stale")
                score -= 0.32
        if governed_candidate_pool_active:
            if governed_freshness_days is None:
                warnings.append("governed_candidate_pool_freshness_unknown")
                score -= 0.05
            elif self._safe_float(governed_freshness_days, default=0.0) > 3:
                warnings.append("governed_candidate_pool_stale")
                score -= 0.08
        refresh_status = str(factor_refresh.get("refresh_status") or "").strip().lower()
        if bool(factor_refresh.get("refresh_attempted")) and refresh_status not in {
            "success",
            "not_needed",
        }:
            warnings.append(f"factor_refresh_{refresh_status or 'unknown'}")
            score -= 0.08

        score = max(min(round(score, 4), 1.0), 0.0)
        can_proceed = not critical_blockers and (
            not hard_block or (score >= FACTORY_READINESS_MIN_SCORE and not blockers)
        )
        effective_blocking_reason_codes = (
            blockers
            if blockers
            else (warnings if (not can_proceed and score < FACTORY_READINESS_MIN_SCORE) else [])
        )
        authority = build_readiness_authority(
            can_proceed=can_proceed,
            hard_gate_enabled=hard_block,
            blocking_reason_codes=blockers,
            critical_blocking_reason_codes=critical_blockers,
            warning_reason_codes=warnings,
            effective_blocking_reason_codes=effective_blocking_reason_codes,
            skip_reason="readiness_blocked",
        )

        return {
            "readiness_contract_version": READINESS_CONTRACT_VERSION,
            "runtime_enabled": is_factory_runtime_enabled(),
            "event_runtime_mode": event_runtime_mode,
            "auto_refresh_enabled": bool(
                is_factory_factor_auto_refresh_enabled()
                and bool(factor_refresh.get("auto_refresh_enabled"))
            ),
            "hard_block_enabled": hard_block,
            "min_score": FACTORY_READINESS_MIN_SCORE,
            "min_completion_ratio": FACTORY_READINESS_MIN_COMPLETION_RATIO,
            "readiness_score": score,
            "can_proceed": can_proceed,
            "warnings": warnings,
            "warning_count": len(warnings),
            "blockers": blockers,
            "blocker_count": len(blockers),
            "critical_blockers": critical_blockers,
            "critical_blocker_count": len(critical_blockers),
            "snapshot_completion_ratio": completion_ratio,
            "snapshot_degraded": bool(snapshot.get("degraded")),
            "market_temperature_context": market_temperature_context,
            "market_temperature_context_available": bool(
                market_temperature_context.get("available")
            ),
            "market_temperature_context_quality_status": market_temperature_context.get(
                "quality_status"
            ),
            "market_temperature_context_readiness_status": market_temperature_context.get(
                "readiness_status"
            ),
            "market_temperature_context_staleness_days": market_temperature_context.get(
                "staleness_days"
            ),
            "event_status": event_status,
            "event_task_ready_count": int(event_state.get("tasks_ready_count") or 0),
            "factor_research_stale": bool(factor_summary.get("stale")),
            "factor_research_degraded": bool(factor_summary.get("degraded")),
            "factor_source_mode": factor_summary.get("factor_source_mode"),
            "governed_candidate_pool_active": governed_candidate_pool_active,
            "governed_candidate_pool_runtime_state": governed_pool_runtime_state,
            "governed_supply_viable": governed_supply_viable,
            "governed_supply_recoverable": governed_supply_recoverable,
            "governed_candidate_pool_mode": governed_candidate_pool_mode,
            "governed_candidate_pool_provisional": governed_candidate_pool_provisional,
            "governed_candidate_pool_provisional_spillover_policy": governed_candidate_pool_provisional_spillover_policy,
            "governed_candidate_pool_provisional_spillover_policy_status": factor_summary.get(
                "governed_candidate_pool_provisional_spillover_policy_status"
            ),
            "governed_pool_missing_after_scheduler_success": governed_pool_missing_after_scheduler_success,
            "active_candidate_count": active_candidate_count,
            "family_preference_order": family_preference_order,
            "family_preference_source_mode": family_preference_source_mode,
            "governed_source_candidate_count": governed_source_candidate_count,
            "governed_blocked_candidate_count": governed_blocked_candidate_count,
            "governed_blocked_ratio": governed_blocked_ratio,
            "governed_pending_candidate_count": governed_pending_candidate_count,
            "governed_pending_ratio": governed_pending_ratio,
            "governed_ineligible_candidate_count": governed_ineligible_candidate_count,
            "governed_candidate_pool_provisional_pending_count": governed_candidate_pool_provisional_pending_count,
            "governed_candidate_pool_strict_shortfall_count": governed_candidate_pool_strict_shortfall_count,
            "budget_feedback_strategy_count": budget_feedback_strategy_count,
            "budget_feedback_zero_signal_strategy_count": budget_feedback_zero_signal_strategy_count,
            "budget_feedback_zero_signal_ratio": budget_feedback_zero_signal_ratio,
            "budget_feedback_forward_window_coverage_ratio": budget_feedback_forward_window_coverage_ratio,
            "budget_feedback_promotion_ready_count": budget_feedback_promotion_ready_count,
            "budget_feedback_promotion_ready_ratio": budget_feedback_promotion_ready_ratio,
            "budget_feedback_promotion_review_count": budget_feedback_promotion_review_count,
            "budget_feedback_promotion_review_coverage_ratio": budget_feedback_promotion_review_coverage_ratio,
            "budget_feedback_evidence_debt_strategy_count": budget_feedback_evidence_debt_strategy_count,
            "budget_feedback_evidence_debt_ratio": budget_feedback_evidence_debt_ratio,
            "paper_observation_backlog_count": paper_observation_backlog_count,
            "paper_observation_backlog_status": paper_observation_backlog_status,
            "paper_observation_intake_stale": paper_observation_intake_stale,
            "paper_observation_last_recognized_at": paper_observation_last_recognized_at,
            "incubation_factory_health": incubation_factory_health,
            "feedback_generator_mode_control_mode_counts": feedback_generator_mode_control_mode_counts,
            "suppressed_generator_modes": suppressed_generator_modes,
            "external_llm_provider_control_mode": external_llm_provider_control_mode,
            "external_llm_provider_control_reasons": external_llm_provider_control_reasons,
            "external_llm_provider_suppress_active": external_llm_provider_suppress_active,
            "governed_freshness_days": governed_freshness_days,
            "governed_exclusion_reason_counts": governed_exclusion_reason_counts,
            "governed_blocking_reason_counts": governed_blocking_reason_counts,
            "governed_pending_reason_counts": governed_pending_reason_counts,
            "governed_ineligible_reason_counts": governed_ineligible_reason_counts,
            "governed_risk_counts": governed_risk_counts,
            "active_family_count": active_family_count,
            "active_regime_count": len(list(factor_summary.get("active_regime_names") or [])),
            "min_governed_active_candidate_count": min_governed_active_candidate_count,
            "min_governed_active_family_count": min_governed_active_family_count,
            "stock_family_allocation_count": stock_family_allocation_count,
            "stock_family_allocation_source_mode": stock_family_allocation_source_mode,
            "scheduler_recent_success": scheduler_recent_success,
            "scheduler_llm_validation_status": scheduler_llm_validation_status,
            "factor_refresh_attempted": bool(factor_refresh.get("refresh_attempted")),
            "factor_refresh_status": factor_refresh.get("refresh_status"),
            "factor_refresh_recommended": factor_refresh_recommendation_reason is not None,
            "factor_refresh_recommendation_reason": factor_refresh_recommendation_reason,
            "authority": authority,
            "authority_contract_version": authority.get("authority_contract_version"),
            "decision": authority.get("decision"),
            "blocked": authority.get("blocked"),
            "hard_gate": authority.get("hard_gate"),
            "gate_mode": authority.get("gate_mode"),
            "blocking_stage": authority.get("blocking_stage"),
            "blocking_reason_codes": list(authority.get("blocking_reason_codes") or []),
            "effective_blocking_reason_codes": list(
                authority.get("effective_blocking_reason_codes") or []
            ),
            "raw_blocking_reason_codes": list(
                authority.get("raw_blocking_reason_codes") or []
            ),
            "blocking_reason_codes_source": authority.get("blocking_reason_codes_source"),
            "critical_blocking_reason_codes": list(
                authority.get("critical_blocking_reason_codes") or []
            ),
            "warning_reason_codes": list(authority.get("warning_reason_codes") or []),
            "skip_reason": authority.get("skip_reason"),
        }

    @staticmethod
    def _safe_float(value: Any, *, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

__all__ = [
    "ReadinessService",
    "READINESS_AUTHORITY_CONTRACT_VERSION",
    "READINESS_CONTRACT_VERSION",
    "build_readiness_authority",
    "resolve_factor_refresh_trigger",
    "resolve_governed_pool_runtime_state",
    "resolve_governed_pool_state",
    "resolve_market_temperature_context",
]
