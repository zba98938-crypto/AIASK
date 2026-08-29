"""Stage-status decision logic now owned by Strategy Factory (Phase 3).

This logic moved out of ``akshare_mcp.services.factor_scheduler`` as a closed set
of pure functions. These tests own the contract on this side of the boundary; the
host keeps a thin delegating shim, and its own tests verify that the shim
preserves behaviour.

The governing rule is fail-closed: an unrecognised stage status resolves to
``failed``, never ``completed``.
"""

from __future__ import annotations

import pytest

from strategy_factory.api.runtime import (
    RUN_STATUSES,
    STAGE_STATUSES,
    build_run_summary,
    build_stage_result,
    normalize_stage_status,
    quality_status,
    resolve_run_status,
    summarize_stage_results,
)


# --- public surface ------------------------------------------------------------


def test_the_logic_is_reachable_from_the_public_api() -> None:
    """Outside packages must not need strategy_factory.domain directly."""
    from strategy_factory.api import runtime

    for name in (
        "normalize_stage_status",
        "build_stage_result",
        "summarize_stage_results",
        "resolve_run_status",
        "quality_status",
        "build_run_summary",
    ):
        assert name in runtime.__all__, f"{name} must be part of the public surface"


def test_vocabularies_are_closed() -> None:
    assert STAGE_STATUSES == ("completed", "partial", "skipped", "failed")
    assert RUN_STATUSES == ("success", "partial", "skipped", "failed")


# --- normalization -------------------------------------------------------------


@pytest.mark.parametrize("token", ["completed", "success", "done", "ok", " DONE "])
def test_success_tokens(token: str) -> None:
    assert normalize_stage_status(token) == "completed"


@pytest.mark.parametrize("token", ["partial", "degraded", "warning", "warn"])
def test_degradation_tokens(token: str) -> None:
    assert normalize_stage_status(token) == "partial"


@pytest.mark.parametrize("token", ["skipped", "disabled", "not_needed", "noop"])
def test_skip_tokens(token: str) -> None:
    assert normalize_stage_status(token) == "skipped"


@pytest.mark.parametrize(
    "token", ["failed", "error", "timeout", "timed_out", "cancelled", "crashed", "blocked"]
)
def test_failure_tokens_including_the_ones_that_used_to_pass(token: str) -> None:
    assert normalize_stage_status(token) == "failed"


@pytest.mark.parametrize("token", [None, "", "mystery", 0, [], {}])
def test_unknown_fails_closed(token) -> None:
    assert normalize_stage_status(token) == "failed"


def test_every_normalized_value_is_in_the_vocabulary() -> None:
    for token in ("done", "warn", "noop", "timeout", "nonsense", None):
        assert normalize_stage_status(token) in STAGE_STATUSES


# --- stage results -------------------------------------------------------------


def test_stage_result_shape() -> None:
    result = build_stage_result("batch_compute", status="completed")

    assert result["stage"] == "batch_compute"
    assert result["status"] == "completed"
    assert result["ok"] is True
    assert result["degraded"] is False
    assert result["attempt_count"] == 1


def test_timeout_stage_is_not_ok() -> None:
    result = build_stage_result("x", status="timeout")
    assert result["status"] == "failed"
    assert result["ok"] is False


def test_partial_is_ok_but_degraded() -> None:
    result = build_stage_result("x", status="degraded")
    assert result["ok"] is True
    assert result["degraded"] is True


def test_counts_derive_from_payload_lists_when_absent() -> None:
    result = build_stage_result(
        "x", status="partial", payload={"warnings": ["a", "b"], "failures": ["f"]}
    )
    assert result["warning_count"] == 2
    assert result["failure_count"] == 1


def test_explicit_counts_win_over_derived_ones() -> None:
    result = build_stage_result(
        "x", status="partial", payload={"warnings": ["a"], "warning_count": 9}
    )
    assert result["warning_count"] == 9


def test_retry_metadata_only_appears_with_a_boundary() -> None:
    assert "retryable" not in build_stage_result("x", status="failed")

    bounded = build_stage_result("x", status="failed", retry_boundary="stage")
    assert bounded["retryable"] is True

    # A completed stage is not retryable even when a boundary is given.
    completed = build_stage_result("x", status="completed", retry_boundary="stage")
    assert completed["retryable"] is False


def test_payload_keys_are_preserved() -> None:
    result = build_stage_result("x", status="completed", payload={"computed": 42})
    assert result["computed"] == 42


# --- summaries -----------------------------------------------------------------


def test_summary_buckets_and_names() -> None:
    summary = summarize_stage_results(
        {
            "a": {"status": "completed"},
            "b": {"status": "partial"},
            "c": {"status": "skipped"},
            "d": {"status": "cancelled"},
        }
    )

    assert summary["stage_status_counts"] == {
        "completed": 1,
        "partial": 1,
        "skipped": 1,
        "failed": 1,
    }
    assert summary["failed_stages"] == ["d"]
    assert summary["partial_stages"] == ["b"]
    assert summary["skipped_stages"] == ["c"]


def test_summary_tolerates_empty_and_none() -> None:
    for value in ({}, None):
        summary = summarize_stage_results(value)
        assert summary["failed_stage_count"] == 0


def test_summary_treats_a_missing_status_as_failed() -> None:
    summary = summarize_stage_results({"a": {}})
    assert summary["failed_stages"] == ["a"]


# --- run status ----------------------------------------------------------------


def test_all_completed_is_success() -> None:
    assert resolve_run_status({"a": {"status": "completed"}}) == "success"


def test_no_stages_is_success() -> None:
    assert resolve_run_status({}) == "success"
    assert resolve_run_status(None) == "success"


def test_failure_with_other_output_is_partial() -> None:
    assert resolve_run_status(
        {"a": {"status": "completed"}, "b": {"status": "failed"}}
    ) == "partial"


def test_failure_with_nothing_else_is_failed() -> None:
    assert resolve_run_status({"a": {"status": "failed"}}) == "failed"
    assert resolve_run_status(
        {"a": {"status": "failed"}, "b": {"status": "crashed"}}
    ) == "failed"


def test_a_timed_out_run_never_resolves_to_success() -> None:
    """The end-to-end statement of the fail-open bug this replaced."""
    assert resolve_run_status({"a": {"status": "timeout"}}) != "success"


def test_all_skipped_is_skipped() -> None:
    assert resolve_run_status(
        {"a": {"status": "skipped"}, "b": {"status": "disabled"}}
    ) == "skipped"


def test_partial_without_failure_is_partial() -> None:
    assert resolve_run_status({"a": {"status": "degraded"}}) == "partial"


def test_every_resolution_is_in_the_vocabulary() -> None:
    cases = [{}, {"a": {"status": "completed"}}, {"a": {"status": "failed"}}, {"a": {}}]
    for stages in cases:
        assert resolve_run_status(stages) in RUN_STATUSES


# --- quality status ------------------------------------------------------------


def test_quality_precedence_is_severity_ordered() -> None:
    assert quality_status(["failed", "degraded", "stale"]) == "failed"
    assert quality_status(["degraded", "stale"]) == "degraded"
    assert quality_status(["partial"]) == "degraded"
    assert quality_status(["stale"]) == "stale"
    assert quality_status([]) == "fresh"
    assert quality_status(None) == "fresh"


def test_quality_ignores_blank_flags() -> None:
    assert quality_status(["", "  ", None]) == "fresh"


def test_quality_is_case_insensitive() -> None:
    assert quality_status(["FAILED"]) == "failed"


# --- run summary ---------------------------------------------------------------


def test_run_summary_carries_identity_and_counters() -> None:
    summary = build_run_summary(
        {
            "run_id": "r1",
            "status": "success",
            "computed": "12",
            "errors": None,
            "quality_flags": ["stale"],
            "stages": {"a": {"status": "completed", "failure_count": 0}},
        }
    )

    assert summary["run_id"] == "r1"
    assert summary["computed"] == 12
    assert summary["errors"] == 0
    assert summary["quality_status"] == "stale"
    assert summary["stage_summary"]["stage_status_counts"]["completed"] == 1


def test_run_summary_tolerates_an_empty_result() -> None:
    summary = build_run_summary({})
    assert summary["run_id"] is None
    assert summary["computed"] == 0
    assert summary["quality_status"] == "fresh"


def test_run_summary_tolerates_none() -> None:
    assert build_run_summary(None)["computed"] == 0


def test_run_summary_flattens_llm_provider_details() -> None:
    summary = build_run_summary(
        {
            "llm_provider": {"health_status": "ok", "ready": True, "rebuild_count": "3"},
            "llm_mining": {"data": {"generation_mode": "llm", "fallback_used": False}},
        }
    )

    assert summary["llm_provider_health_status"] == "ok"
    assert summary["llm_provider_ready"] is True
    assert summary["llm_provider_rebuild_count"] == 3
    assert summary["llm_generation_mode"] == "llm"
    assert summary["llm_fallback_used"] is False


# --- purity --------------------------------------------------------------------


def test_the_module_has_no_io_dependencies() -> None:
    """These moved because they are pure; a new import would undo that."""
    import strategy_factory.domain.stage_status as module
    import inspect

    source = inspect.getsource(module)
    for forbidden in ("import sqlite3", "await ", "async def", "akshare", "requests"):
        assert forbidden not in source, f"stage_status must stay pure; found {forbidden!r}"
