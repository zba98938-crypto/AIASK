"""Stage-status normalization must fail closed.

``FactorScheduler._normalize_stage_status`` previously returned ``completed`` for
any unrecognised token. That meant a stage reporting ``timeout``, ``cancelled``
or ``crashed`` was recorded as completed, and ``_resolve_run_status`` then
reported the whole run as ``success`` — an optimistic verdict on a broken run.

These tests pin the closed vocabulary and the run-status resolution built on it.
"""

from __future__ import annotations

import pytest

from akshare_mcp.services.factor_scheduler import FactorScheduler


normalize = FactorScheduler._normalize_stage_status
resolve = FactorScheduler._resolve_run_status
summarize = FactorScheduler._summarize_stage_results
build_stage = FactorScheduler._build_stage_result


# --- the closed vocabulary ------------------------------------------------------


@pytest.mark.parametrize(
    "token", ["completed", "complete", "success", "succeeded", "done", "ok", "COMPLETED", " done "]
)
def test_success_tokens_normalize_to_completed(token: str) -> None:
    assert normalize(token) == "completed"


@pytest.mark.parametrize("token", ["partial", "degraded", "warning", "warn"])
def test_degradation_tokens_normalize_to_partial(token: str) -> None:
    assert normalize(token) == "partial"


@pytest.mark.parametrize("token", ["skipped", "disabled", "not_needed", "noop"])
def test_skip_tokens_normalize_to_skipped(token: str) -> None:
    assert normalize(token) == "skipped"


@pytest.mark.parametrize(
    "token",
    ["failed", "error", "timeout", "timed_out", "cancelled", "canceled", "crashed", "blocked"],
)
def test_failure_tokens_normalize_to_failed(token: str) -> None:
    """A stage that timed out or was cancelled is not a completed stage."""
    assert normalize(token) == "failed", (
        f"{token!r} must not be reported as anything other than failed"
    )


@pytest.mark.parametrize("token", ["", None, "garbage", "who_knows", 0, []])
def test_unknown_tokens_fail_closed(token) -> None:
    assert normalize(token) == "failed", (
        "an unrecognised status must fail closed; reporting it as completed lets "
        "a broken stage pass as success"
    )


def test_normalization_is_idempotent() -> None:
    for token in ("completed", "partial", "skipped", "failed"):
        assert normalize(normalize(token)) == normalize(token)


# --- run status resolution -----------------------------------------------------


def test_all_completed_resolves_to_success() -> None:
    stages = {"a": {"status": "completed"}, "b": {"status": "done"}}
    assert resolve(stages) == "success"


def test_a_timed_out_stage_does_not_resolve_to_success() -> None:
    """The regression this fix targets, stated end to end."""
    stages = {"a": {"status": "completed"}, "b": {"status": "timeout"}}
    assert resolve(stages) != "success"
    assert resolve(stages) == "partial"


def test_a_stage_with_an_unknown_status_does_not_resolve_to_success() -> None:
    stages = {"a": {"status": "completed"}, "b": {"status": "mystery"}}
    assert resolve(stages) != "success"


def test_every_stage_failing_resolves_to_failed() -> None:
    stages = {"a": {"status": "failed"}, "b": {"status": "crashed"}}
    assert resolve(stages) == "failed"


def test_partial_stage_resolves_to_partial() -> None:
    stages = {"a": {"status": "completed"}, "b": {"status": "degraded"}}
    assert resolve(stages) == "partial"


def test_all_skipped_resolves_to_skipped() -> None:
    stages = {"a": {"status": "skipped"}, "b": {"status": "disabled"}}
    assert resolve(stages) == "skipped"


def test_empty_stage_map_resolves_to_success() -> None:
    """No stages ran, so there is nothing to have failed."""
    assert resolve({}) == "success"


# --- summary counts ------------------------------------------------------------


def test_summary_counts_each_status_bucket() -> None:
    stages = {
        "a": {"status": "completed"},
        "b": {"status": "partial"},
        "c": {"status": "skipped"},
        "d": {"status": "timeout"},
    }
    summary = summarize(stages)

    assert summary["stage_status_counts"]["completed"] == 1
    assert summary["stage_status_counts"]["partial"] == 1
    assert summary["stage_status_counts"]["skipped"] == 1
    assert summary["stage_status_counts"]["failed"] == 1
    assert summary["failed_stages"] == ["d"]


def test_summary_names_the_failing_stage_for_diagnosis() -> None:
    summary = summarize({"batch_compute": {"status": "cancelled"}})
    assert summary["failed_stages"] == ["batch_compute"]


# --- stage result construction -------------------------------------------------


def test_build_stage_result_marks_a_timeout_as_not_ok() -> None:
    result = build_stage("batch_compute", status="timeout")

    assert result["status"] == "failed"
    assert result["ok"] is False
    assert result["degraded"] is False


def test_build_stage_result_marks_partial_as_degraded_but_ok() -> None:
    result = build_stage("llm_factor_mining", status="degraded")

    assert result["status"] == "partial"
    assert result["ok"] is True
    assert result["degraded"] is True


def test_build_stage_result_flags_retryability_only_with_a_boundary() -> None:
    without = build_stage("x", status="failed")
    assert "retryable" not in without

    with_boundary = build_stage("x", status="failed", retry_boundary="stage")
    assert with_boundary["retryable"] is True
    assert with_boundary["retry_boundary"] == "stage"


def test_build_stage_result_preserves_caller_payload() -> None:
    result = build_stage(
        "batch_compute",
        status="completed",
        payload={"computed": 7, "warnings": ["w1"], "attempt_count": 2},
    )

    assert result["computed"] == 7
    assert result["warning_count"] == 1
    assert result["attempt_count"] == 2
