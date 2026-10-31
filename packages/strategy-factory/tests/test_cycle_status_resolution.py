"""Cycle status resolution tests (P2 / R6 / Property 5 / Property 6).

Verifies the new ``FactoryRunStatus`` taxonomy:

    - ``success``: formal incubation admission > 0 and no infra/LLM degradation.
    - ``success_observe_only``: candidates entered only zero-capital observation.
    - ``success_no_submission``: gate_3_passed > 0 but submitted == 0.
    - ``success_no_strategy``: gate_3_passed == 0 but the cycle ran cleanly
      (no infra/llm degradation).
    - ``partial_infra``: warmup / collect / persistence reported failures.
    - ``partial_llm``: LLM timeout ratio or no-spec ratio above thresholds.
    - ``failed``: pre-existing FAILED current_status, or non-recoverable
      flow.

Priority: failed > skipped > partial_infra > partial_llm >
success_no_strategy > success_no_submission > success.
"""

from __future__ import annotations

from strategy_factory.application.run_models import (
    FactoryRunStatus,
    StageStatus,
    resolve_run_status,
)


def _stage(status: str = "completed", *, hard_failure: bool = False,
           degraded: bool = False) -> dict:
    return {
        "status": status,
        "hard_failure": hard_failure,
        "degraded": degraded,
    }


def _ok_stages() -> dict:
    """All canonical stages completed successfully."""
    return {
        "warmup": _stage("completed"),
        "collect": _stage("completed"),
        "factor_research": _stage("completed"),
        "readiness": _stage("completed"),
        "spawn": _stage("completed"),
        "autonomy": _stage("completed"),
        "quality_gate": _stage("completed"),
        "backtest": _stage("completed"),
        "deduplicate": _stage("completed"),
        "submit": _stage("completed"),
        "elimination": _stage("completed"),
    }


# ---------------------------------------------------------------------------
# Success variants
# ---------------------------------------------------------------------------

def test_success_when_submitted_and_gate3_pass() -> None:
    summary = {
        "gate_3_passed": 3,
        "submitted": 3,
        "formal_incubation_count": 3,
        "autonomy_task_count": 8,
    }
    status = resolve_run_status(
        "success", _ok_stages(),
        summary=summary,
    )
    assert status == FactoryRunStatus.SUCCESS


def test_success_no_submission_when_gate3_pass_but_zero_submitted() -> None:
    summary = {"gate_3_passed": 2, "submitted": 0, "autonomy_task_count": 8}
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.SUCCESS_NO_SUBMISSION


def test_success_no_strategy_when_no_gate3_pass() -> None:
    summary = {"gate_3_passed": 0, "submitted": 0, "autonomy_task_count": 8}
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.SUCCESS_NO_STRATEGY


def test_observe_first_with_observed_candidates_is_observe_only() -> None:
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "autonomy_task_count": 8,
        "observe_first_enabled": True,
        "observed_candidate_count": 5,
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.SUCCESS_OBSERVE_ONLY


def test_observe_admissions_are_observe_only_without_legacy_gate3_pass() -> None:
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "autonomy_task_count": 8,
        "observe_incubation_count": 3,
        "diagnostic_observation_count": 1,
        "observe_admitted_count": 4,
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.SUCCESS_OBSERVE_ONLY


# ---------------------------------------------------------------------------
# partial_infra
# ---------------------------------------------------------------------------

def test_partial_infra_when_warmup_failed() -> None:
    stages = _ok_stages()
    stages["warmup"] = _stage("failed", hard_failure=True)
    summary = {
        "gate_3_passed": 1, "submitted": 1, "autonomy_task_count": 8,
        "warmup_failed": 2, "sync_task_failed_count": 2,
    }
    status = resolve_run_status("success", stages, summary=summary)
    assert status == FactoryRunStatus.PARTIAL_INFRA


def test_partial_infra_when_collect_partial() -> None:
    stages = _ok_stages()
    stages["collect"] = _stage("partial", degraded=True)
    summary = {"gate_3_passed": 1, "submitted": 1}
    status = resolve_run_status("success", stages, summary=summary)
    assert status == FactoryRunStatus.PARTIAL_INFRA


def test_factor_research_partial_quality_shortfall_is_success_no_strategy() -> None:
    stages = _ok_stages()
    stages["factor_research"] = _stage("partial", degraded=True)
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "factor_research_degraded": True,
        "governed_blocked_candidate_count": 8,
    }
    status = resolve_run_status("success", stages, summary=summary)
    assert status == FactoryRunStatus.SUCCESS_NO_STRATEGY


def test_factor_research_partial_with_llm_degradation_is_partial_llm() -> None:
    stages = _ok_stages()
    stages["factor_research"] = _stage("partial", degraded=True)
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "llm_status_counts": {"succeeded": 1, "non_executable": 2},
    }
    status = resolve_run_status("success", stages, summary=summary)
    assert status == FactoryRunStatus.PARTIAL_LLM


def test_partial_infra_when_factor_research_failed_hard() -> None:
    stages = _ok_stages()
    stages["factor_research"] = _stage("failed", hard_failure=True)
    summary = {"gate_3_passed": 0, "submitted": 0}
    status = resolve_run_status("success", stages, summary=summary)
    assert status == FactoryRunStatus.PARTIAL_INFRA


def test_partial_infra_when_persistence_failure_count_positive() -> None:
    summary = {"gate_3_passed": 1, "submitted": 1}
    status = resolve_run_status(
        "success", _ok_stages(),
        persistence_failure_count=2,
        summary=summary,
    )
    assert status == FactoryRunStatus.PARTIAL_INFRA


# ---------------------------------------------------------------------------
# partial_llm
# ---------------------------------------------------------------------------

def test_partial_llm_when_timeout_ratio_above_threshold() -> None:
    summary = {
        "gate_3_passed": 1, "submitted": 1,
        "autonomy_task_count": 8,
        "task_timeout_skip_count": 4,  # 4/8 = 0.5 > 0.30
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.PARTIAL_LLM


def test_partial_llm_when_no_spec_ratio_above_threshold() -> None:
    summary = {
        "gate_3_passed": 1, "submitted": 1,
        "llm_status_counts": {
            "succeeded": 2,
            "non_executable": 4,
        },  # 4/6 = 0.667 > 0.50
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.PARTIAL_LLM


def test_no_partial_llm_when_below_thresholds() -> None:
    summary = {
        "gate_3_passed": 1, "submitted": 1, "formal_incubation_count": 1,
        "autonomy_task_count": 10,
        "task_timeout_skip_count": 2,  # 2/10 = 0.20 < 0.30
        "llm_status_counts": {
            "succeeded": 8,
            "non_executable": 2,  # 2/10 = 0.20 < 0.50
        },
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.SUCCESS


# ---------------------------------------------------------------------------
# Priority — multiple conditions
# ---------------------------------------------------------------------------

def test_no_partial_llm_for_single_provider_error_below_threshold() -> None:
    summary = {
        "gate_3_passed": 1,
        "submitted": 1,
        "formal_incubation_count": 1,
        "autonomy_task_count": 6,
        "task_timeout_skip_count": 1,  # 1/6 = 0.167 < 0.30
        "llm_status_counts": {
            "succeeded": 4,
            "provider_error": 1,  # 1/6 = 0.167 < 0.30
            "non_executable": 1,  # 1/6 = 0.167 < 0.50
        },
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.SUCCESS


def test_partial_llm_when_provider_error_ratio_above_threshold() -> None:
    summary = {
        "gate_3_passed": 1,
        "submitted": 1,
        "autonomy_task_count": 4,
        "llm_status_counts": {
            "succeeded": 2,
            "provider_error": 2,  # 2/4 = 0.50 > 0.30
        },
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.PARTIAL_LLM


def test_pipeline_fallback_only_is_partial_llm() -> None:
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "autonomy_task_count": 6,
        "task_timeout_skip_count": 0,
        "llm_status_counts": {
            "succeeded": 1,
            "skipped_after_pipeline_empty": 4,
            "fallback_only": 1,
            "provider_cooldown_skip": 3,
        },
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.PARTIAL_LLM


def test_local_candidates_without_autonomy_attempt_are_not_llm_degradation() -> None:
    """Skipped autonomy with no external request must not imply provider failure."""
    stages = _ok_stages()
    stages["autonomy"] = _stage("skipped")
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "autonomy_task_count": 0,
        "external_llm_status": "skipped",
        "external_llm_network_request_count": 0,
        "external_llm_effective_response_count": 0,
        "research_local_rule_candidate_count": 4,
    }
    status = resolve_run_status("success", stages, summary=summary)
    assert status == FactoryRunStatus.SUCCESS_NO_STRATEGY


def test_local_fallback_after_real_external_attempt_is_partial_llm() -> None:
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "autonomy_task_count": 2,
        "external_llm_network_request_count": 2,
        "external_llm_effective_response_count": 0,
        "research_local_rule_candidate_count": 2,
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.PARTIAL_LLM


def test_duplicate_provider_error_projections_count_same_event_once() -> None:
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "autonomy_task_count": 10,
        "external_llm_network_request_count": 10,
        "provider_error_count": 1,
        "provider_http_error_count": 1,
        "provider_http_5xx_count": 1,
        "llm_status_counts": {"succeeded": 9, "provider_error": 1},
        "external_llm_status_counts": {"succeeded": 9, "provider_error": 1},
        "pipeline_fallback_counts": {"cooldown_skip": 1},
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.SUCCESS_NO_STRATEGY


def test_priority_infra_over_llm() -> None:
    """When both infra and LLM are degraded, infra wins."""
    stages = _ok_stages()
    stages["warmup"] = _stage("failed", hard_failure=True)
    summary = {
        "gate_3_passed": 0, "submitted": 0,
        "warmup_failed": 1, "sync_task_failed_count": 1,
        "autonomy_task_count": 4,
        "task_timeout_skip_count": 3,  # llm degraded too
    }
    status = resolve_run_status("success", stages, summary=summary)
    assert status == FactoryRunStatus.PARTIAL_INFRA


def test_priority_partial_infra_over_success_no_strategy() -> None:
    stages = _ok_stages()
    stages["warmup"] = _stage("partial", degraded=True)
    summary = {"gate_3_passed": 0, "submitted": 0,
               "warmup_failed": 1, "sync_task_failed_count": 1}
    status = resolve_run_status("success", stages, summary=summary)
    assert status == FactoryRunStatus.PARTIAL_INFRA


def test_priority_failed_short_circuit() -> None:
    summary = {"gate_3_passed": 0}
    status = resolve_run_status(
        FactoryRunStatus.FAILED,
        _ok_stages(),
        summary=summary,
    )
    assert status == FactoryRunStatus.FAILED


def test_priority_skipped_short_circuit() -> None:
    summary = {"gate_3_passed": 0}
    status = resolve_run_status(
        FactoryRunStatus.SKIPPED,
        _ok_stages(),
        summary=summary,
    )
    assert status == FactoryRunStatus.SKIPPED


# ---------------------------------------------------------------------------
# Property 6: new resolver never emits legacy ``partial``
# ---------------------------------------------------------------------------

def test_new_resolver_never_emits_legacy_partial() -> None:
    """For all reasonable combinations, the resolver with summary=... must
    never write the legacy ``partial`` status."""
    cases = [
        ("success", _ok_stages(), {}),
        ("success", _ok_stages(), {"gate_3_passed": 0, "submitted": 0}),
        ("success", _ok_stages(), {"gate_3_passed": 5, "submitted": 5}),
        ("success", {**_ok_stages(), "warmup": _stage("failed")},
         {"warmup_failed": 1}),
        ("success", _ok_stages(),
         {"autonomy_task_count": 10, "task_timeout_skip_count": 5}),
    ]
    for current, stages, summary in cases:
        result = resolve_run_status(current, stages, summary=summary)
        assert result != FactoryRunStatus.PARTIAL, (
            f"resolver emitted legacy partial for inputs "
            f"current={current!r}, summary={summary!r}, result={result.value!r}"
        )


# ---------------------------------------------------------------------------
# Backwards compatibility: when summary is not provided, old behavior holds
# ---------------------------------------------------------------------------

def test_legacy_path_returns_partial_when_summary_omitted() -> None:
    """If a caller hasn't been updated to pass summary, the resolver
    should still produce the legacy partial value to avoid breaking
    tests / callers that haven't migrated yet."""
    stages = _ok_stages()
    stages["warmup"] = _stage("failed", hard_failure=True)
    status = resolve_run_status("success", stages)
    assert status == FactoryRunStatus.PARTIAL


def test_legacy_path_returns_success_for_clean_run() -> None:
    status = resolve_run_status("success", _ok_stages())
    assert status == FactoryRunStatus.SUCCESS
