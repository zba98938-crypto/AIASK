"""LLM pipeline diagnostics tests (P3 / R7 / Property 7).

Covers:
1. Staged-pipeline reason classification
   (`empty_output / schema_invalid / non_executable / target_context_blocked
   / pipeline_timeout / provider_output_format_failure / unknown`).
2. ``pipeline_fallback_breakdown`` shape
   (`by_reason / by_stage / by_stage_reason`).
3. Research-task timeout classification
   (`external_llm_timeout / pipeline_stage_timeout / bulk_research_timeout`).
4. ``partial_llm`` status escalation when timeout ratio or no-spec ratio
   exceeds threshold (Property 5 / Property 7).
5. Env-driven threshold override.
"""

from __future__ import annotations

from akshare_mcp.services._strategy_generators_generate import (
    StagedPipelineReason,
    _build_pipeline_fallback_breakdown,
    classify_staged_pipeline_reason,
)
from strategy_factory.application._factory_scheduler_loop import (
    ResearchTaskTimeoutKind,
    _classify_research_task_timeout_kind,
)
from strategy_factory.application.run_models import (
    FactoryRunStatus,
    _is_llm_degraded,
    _llm_no_spec_ratio,
    _llm_provider_error_count,
    _llm_timeout_ratio,
    _resolve_llm_timeout_partial_threshold,
    resolve_run_status,
)


# ---------------------------------------------------------------------------
# Reason classification (R7.1)
# ---------------------------------------------------------------------------

def test_classify_staged_pipeline_reason_empty_output() -> None:
    assert classify_staged_pipeline_reason("returned_empty") == StagedPipelineReason.EMPTY_OUTPUT
    assert classify_staged_pipeline_reason("no_executable_specs") == StagedPipelineReason.EMPTY_OUTPUT
    assert classify_staged_pipeline_reason("empty_output") == StagedPipelineReason.EMPTY_OUTPUT


def test_classify_staged_pipeline_reason_schema_invalid() -> None:
    assert classify_staged_pipeline_reason("invalid_output:factor_research") == StagedPipelineReason.SCHEMA_INVALID
    assert classify_staged_pipeline_reason("schema_invalid") == StagedPipelineReason.SCHEMA_INVALID


def test_classify_staged_pipeline_reason_non_executable() -> None:
    assert classify_staged_pipeline_reason("non_executable") == StagedPipelineReason.NON_EXECUTABLE


def test_classify_staged_pipeline_reason_target_context_blocked() -> None:
    assert classify_staged_pipeline_reason("target_context_blocked") == StagedPipelineReason.TARGET_CONTEXT_BLOCKED


def test_classify_staged_pipeline_reason_provider_format_failure() -> None:
    assert classify_staged_pipeline_reason(
        "provider_output_format_failure"
    ) == StagedPipelineReason.PROVIDER_FORMAT_FAILURE


def test_classify_staged_pipeline_reason_pipeline_timeout() -> None:
    assert classify_staged_pipeline_reason("pipeline_timeout") == StagedPipelineReason.PIPELINE_TIMEOUT


def test_classify_staged_pipeline_reason_unknown_falls_back() -> None:
    assert classify_staged_pipeline_reason("") == StagedPipelineReason.UNKNOWN
    assert classify_staged_pipeline_reason(None) == StagedPipelineReason.UNKNOWN
    assert classify_staged_pipeline_reason("garbage_token") == StagedPipelineReason.UNKNOWN


# ---------------------------------------------------------------------------
# pipeline_fallback_breakdown shape (R7.2)
# ---------------------------------------------------------------------------

def test_pipeline_fallback_breakdown_aggregates_by_stage_and_reason() -> None:
    stage_fallback_reasons = {
        "factor_research": "returned_empty",
        "rule_compose": "invalid_output:rule_compose",
        "spec_finalize": "non_executable",
    }
    out = _build_pipeline_fallback_breakdown(
        stage_fallback_reasons,
        invalid_output_stage_ids=[],
    )
    assert out["by_reason"] == {
        StagedPipelineReason.EMPTY_OUTPUT: 1,
        StagedPipelineReason.SCHEMA_INVALID: 1,
        StagedPipelineReason.NON_EXECUTABLE: 1,
    }
    assert out["by_stage"] == {
        "factor_research": 1,
        "rule_compose": 1,
        "spec_finalize": 1,
    }
    assert out["by_stage_reason"] == {
        f"factor_research:{StagedPipelineReason.EMPTY_OUTPUT}": 1,
        f"rule_compose:{StagedPipelineReason.SCHEMA_INVALID}": 1,
        f"spec_finalize:{StagedPipelineReason.NON_EXECUTABLE}": 1,
    }


def test_pipeline_fallback_breakdown_respects_invalid_output_ids() -> None:
    """invalid_output_stage_ids that don't show up in stage_fallback_reasons
    still get a schema_invalid entry so the operator can see them."""
    out = _build_pipeline_fallback_breakdown(
        {"factor_research": "returned_empty"},
        invalid_output_stage_ids=["spec_finalize"],
    )
    # factor_research counted from stage_fallback_reasons
    # spec_finalize counted from invalid_output_stage_ids
    assert out["by_stage"]["factor_research"] == 1
    assert out["by_stage"]["spec_finalize"] == 1
    assert out["by_reason"][StagedPipelineReason.EMPTY_OUTPUT] == 1
    assert out["by_reason"][StagedPipelineReason.SCHEMA_INVALID] == 1


def test_pipeline_fallback_breakdown_empty_inputs() -> None:
    out = _build_pipeline_fallback_breakdown({}, invalid_output_stage_ids=[])
    assert out == {"by_reason": {}, "by_stage": {}, "by_stage_reason": {}}


# ---------------------------------------------------------------------------
# Timeout classification (R7.3, Property 7)
# ---------------------------------------------------------------------------

def test_timeout_classification_bulk_task_source() -> None:
    """A bulk_stock_matrix task must always map to BULK_RESEARCH regardless
    of effective timeout."""
    kind = _classify_research_task_timeout_kind(
        {"task_source": "bulk_stock_matrix", "task_id": "bulk_matrix_x"},
        base_timeout_sec=180.0,
        effective_timeout_sec=120.0,
    )
    assert kind == ResearchTaskTimeoutKind.BULK_RESEARCH


def test_timeout_classification_external_llm_when_under_external_cap() -> None:
    kind = _classify_research_task_timeout_kind(
        {"task_source": "factor_context", "task_id": "x"},
        base_timeout_sec=180.0,
        effective_timeout_sec=120.0,
    )
    assert kind == ResearchTaskTimeoutKind.EXTERNAL_LLM


def test_timeout_classification_pipeline_stage_when_external_disabled() -> None:
    kind = _classify_research_task_timeout_kind(
        {
            "task_source": "factor_context",
            "task_id": "x",
            "disable_external_llm": True,
        },
        base_timeout_sec=180.0,
        effective_timeout_sec=120.0,
    )
    assert kind == ResearchTaskTimeoutKind.PIPELINE_STAGE


def test_timeout_classification_bulk_when_high_timeout() -> None:
    """Even non-bulk task source, if effective timeout >= 240s and exceeds
    the base, it's classified as BULK_RESEARCH (matches the bulk cap)."""
    kind = _classify_research_task_timeout_kind(
        {"task_source": "factor_context"},
        base_timeout_sec=120.0,
        effective_timeout_sec=360.0,
    )
    assert kind == ResearchTaskTimeoutKind.BULK_RESEARCH


# ---------------------------------------------------------------------------
# partial_llm escalation in resolve_run_status (R7.5, Property 5)
# ---------------------------------------------------------------------------

def _ok_stages() -> dict:
    return {
        "warmup": {"status": "completed"},
        "collect": {"status": "completed"},
        "readiness": {"status": "completed"},
        "spawn": {"status": "completed"},
        "autonomy": {"status": "completed"},
    }


def test_resolve_run_status_partial_llm_via_timeout_ratio() -> None:
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "autonomy_task_count": 10,
        "task_timeout_skip_count": 4,  # 0.40 > default 0.30
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.PARTIAL_LLM


def test_resolve_run_status_partial_llm_via_no_spec_ratio() -> None:
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "llm_status_counts": {
            "succeeded": 1,
            "non_executable": 4,    # 4/5 = 0.80
        },
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.PARTIAL_LLM


def test_resolve_run_status_no_partial_llm_when_below_threshold() -> None:
    summary = {
        "gate_3_passed": 1,
        "submitted": 1,
        "autonomy_task_count": 10,
        "task_timeout_skip_count": 2,  # 0.20 < 0.30
        "llm_status_counts": {"succeeded": 8, "non_executable": 2},  # 0.20
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    # Below the degradation threshold, so the run must NOT be demoted to
    # PARTIAL_LLM. Which success variant it lands on is decided by the
    # submission outcome and is asserted by the strategy-factory suite; this
    # summary carries no formal incubation, so a plain SUCCESS is not expected.
    assert status is not FactoryRunStatus.PARTIAL_LLM
    assert status is FactoryRunStatus.SUCCESS_NO_SUBMISSION


# ---------------------------------------------------------------------------
# Env-driven threshold override (R7.5)
# ---------------------------------------------------------------------------

def test_resolve_llm_timeout_partial_threshold_env_override(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_FACTORY_LLM_TIMEOUT_PARTIAL_THRESHOLD", "0.50")
    assert _resolve_llm_timeout_partial_threshold() == 0.50

    # Bad value falls back to default
    monkeypatch.setenv("STRATEGY_FACTORY_LLM_TIMEOUT_PARTIAL_THRESHOLD", "garbage")
    assert _resolve_llm_timeout_partial_threshold() == 0.30

    # Out-of-range value falls back to default
    monkeypatch.setenv("STRATEGY_FACTORY_LLM_TIMEOUT_PARTIAL_THRESHOLD", "1.5")
    assert _resolve_llm_timeout_partial_threshold() == 0.30

    monkeypatch.setenv("STRATEGY_FACTORY_LLM_TIMEOUT_PARTIAL_THRESHOLD", "0")
    assert _resolve_llm_timeout_partial_threshold() == 0.30


def test_resolve_run_status_threshold_override_changes_status(monkeypatch) -> None:
    """With env-bumped threshold, the same summary that previously triggered
    partial_llm should now drop to success_no_strategy."""
    monkeypatch.setenv("STRATEGY_FACTORY_LLM_TIMEOUT_PARTIAL_THRESHOLD", "0.60")
    summary = {
        "gate_3_passed": 0,
        "submitted": 0,
        "autonomy_task_count": 10,
        "task_timeout_skip_count": 4,  # 0.40 < 0.60 with override
    }
    status = resolve_run_status("success", _ok_stages(), summary=summary)
    assert status == FactoryRunStatus.SUCCESS_NO_STRATEGY


# ---------------------------------------------------------------------------
# Helper-direct tests
# ---------------------------------------------------------------------------

def test_llm_timeout_ratio_uses_task_timeout_skip_count() -> None:
    summary = {"autonomy_task_count": 10, "task_timeout_skip_count": 3}
    assert _llm_timeout_ratio(summary) == 0.3


def test_llm_timeout_ratio_falls_back_to_status_counts() -> None:
    summary = {
        "llm_status_counts": {
            "succeeded": 5,
            "external_llm_timeout": 3,
            "pipeline_stage_timeout": 2,
        },
    }
    # 5 timeouts out of 10 total = 0.5
    assert _llm_timeout_ratio(summary) == 0.5


def test_llm_no_spec_ratio_zero_when_no_failures() -> None:
    summary = {"llm_status_counts": {"succeeded": 10}}
    assert _llm_no_spec_ratio(summary) == 0.0


def test_is_llm_degraded_short_circuits_on_timeout() -> None:
    summary = {"autonomy_task_count": 10, "task_timeout_skip_count": 5}
    assert _is_llm_degraded(summary) is True


def test_provider_http_error_counts_as_llm_degraded() -> None:
    summary = {"llm_status_counts": {"succeeded": 8, "provider_http_502": 1}}
    assert _llm_provider_error_count(summary) == 1
    assert _is_llm_degraded(summary) is True


def test_pipeline_cooldown_skip_counts_as_llm_degraded() -> None:
    summary = {
        "pipeline_fallback_counts": {
            "cooldown_skip": 3,
            "local_fallback_preferred_or_skip": 12,
            "target_context_blocked": 1,
        }
    }
    assert _llm_provider_error_count(summary) == 3
    assert _is_llm_degraded(summary) is True


def test_target_context_and_local_fallback_do_not_count_as_provider_errors() -> None:
    summary = {
        "pipeline_fallback_counts": {
            "local_fallback_preferred_or_skip": 12,
            "target_context_blocked": 2,
        },
        "llm_status_counts": {"succeeded": 5, "target_context_blocked": 2},
    }
    assert _llm_provider_error_count(summary) == 0
    assert _is_llm_degraded(summary) is False
