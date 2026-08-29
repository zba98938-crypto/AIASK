from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "score,expected",
    [
        (39.99, "D"),
        (40.0, "C"),
        (55.0, "B"),
        (70.0, "A"),
        (80.0, "S"),
        (85.0, "SS"),
        (90.0, "SSS"),
    ],
)
def test_trade_quality_panel_grade_ladder_supports_s_tiers(score, expected):
    from strategy_factory.application.panels import _grade_for_total_score

    assert _grade_for_total_score(score) == expected

import asyncio
from types import MethodType


def _semantic_ready_strategy(strategy_type: str = "ma_cross") -> dict:
    return {
        "id": "candidate_quality_fix",
        "name": "quality fix candidate",
        "strategy_type": strategy_type,
        "params": {
            "evidence_chain": {"evidences": [{"id": "ev1", "source": "test"}]},
            "prediction_contract": {"claims": [{"id": "cl1", "evidence_ids": ["ev1"]}]},
            "confidence_contract": {"probability": 0.62, "support_samples": 24},
        },
    }


def test_admission_uses_authoritative_backtest_expectancy():
    from strategy_factory.application.submission_gate import runner

    result = runner._attach_admission_evaluations(
        _semantic_ready_strategy("ma_cross"),
        {"profile": "trade_rule_validation", "validation_focus": "target_plus_representative"},
        {
            "passed": True,
            "profile": "trade_rule_validation",
            "validation_focus": "target_plus_representative",
            "post_cost_sharpe": 1.1,
        },
        backtest_metrics={
            "event_window_metrics": {
                "post_cost_sharpe": 1.1,
                "sharpe_ratio": 1.1,
                "total_return": 0.24,
                "target_layer_oos_return": 0.18,
                "max_drawdown": 0.08,
                "trade_count": 24,
                "trades_count": 24,
                "avg_holding_days": 12,
                "trade_density": 0.4,
                "win_rate": 0.62,
                "profit_factor": 2.1,
                "expectancy": 123.45,
                "expectancy_return": 0.031,
            }
        },
    )

    all_reasons = [
        reason
        for evaluation in result["admission_evaluations"].values()
        for reason in evaluation.get("reasons", [])
    ]
    assert not any("expectancy" in reason and "<= 0" in reason for reason in all_reasons)
    assert result["metric_source_audit"]["expectancy"] == "event_window_metrics"


def test_incubation_admission_requires_configured_s_grade(monkeypatch):
    from strategy_factory.application.submission_gate import runner

    monkeypatch.setenv("STRATEGY_FACTORY_MIN_VALIDATION_GRADE", "S")
    strong_gate = {
        "passed": True,
        "profile": "trade_rule_validation",
        "validation_focus": "target_plus_representative",
        "post_cost_sharpe": 1.1,
        "sharpe_ratio": 1.1,
        "total_return": 0.24,
        "target_layer_oos_return": 0.18,
        "max_drawdown": 0.08,
        "trade_count": 24,
        "trades_count": 24,
        "avg_holding_days": 12,
        "trade_density": 0.4,
        "win_rate": 0.62,
        "profit_factor": 2.1,
        "expectancy": 123.45,
        "parameter_perturbation_trade_stability": 0.85,
    }
    blocked = runner._attach_admission_evaluations(
        _semantic_ready_strategy("ma_cross"),
        {"profile": "trade_rule_validation", "validation_focus": "target_plus_representative"},
        strong_gate,
        validation_report={"rating": {"grade": "B", "total_score": 58.0}},
    )
    passed = runner._attach_admission_evaluations(
        _semantic_ready_strategy("ma_cross"),
        {"profile": "trade_rule_validation", "validation_focus": "target_plus_representative"},
        strong_gate,
        validation_report={"rating": {"grade": "S", "total_score": 81.0}},
    )

    assert blocked["research_candidate_ready"] is True
    assert blocked["incubation_candidate_ready"] is False
    assert "validation_grade_b_below_minimum_s_for_incubation" in blocked["admission_block_reasons"]
    assert passed["incubation_candidate_ready"] is True
    assert passed["strict_incubation_ready"] is True


def test_statistical_admission_distinguishes_missing_from_zero():
    """P1 update (R5.1, audit P1-prep): explicit 0.0 is now classified
    as ``missing`` rather than ``weak``. The audit observed that
    ``factor_validation_bootstrap`` writes 0.0 as a placeholder when an
    empty walk-forward / bootstrap run happens, so 0.0 should not be
    treated as "weak signal" — it's "no signal at all".

    The old assertion (``walk_forward_ic_ir 0.000 < ...``) is preserved
    here as a regression guard, but flipped: we now assert it is *not*
    emitted, and instead the per-metric ``missing_<m>`` codes are.
    """
    from strategy_factory.application.submission_gate import runner

    missing = runner._evaluate_statistical_admission(
        _semantic_ready_strategy("value_factor"),
        {"profile": "factor_rank_validation"},
        {},
    )

    assert "missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity" in missing["reasons"]
    assert not any(reason.startswith("walk_forward_ic_ir 0.000") for reason in missing["reasons"])
    assert missing["statistical_metric_missing_counts"]["wf_ic_ir"] == 1

    explicit_zero = runner._evaluate_statistical_admission(
        _semantic_ready_strategy("value_factor"),
        {"profile": "factor_rank_validation"},
        {
            "wf_ic_ir": 0.0,
            "pkf_ic": 0.0,
            "bootstrap_ci_lower": 0.0,
            "param_sensitivity": 0.0,
        },
    )

    # Post-P1: explicit 0.0 -> missing (placeholder semantics).
    assert any(reason.startswith("missing_statistical_metrics") for reason in explicit_zero["reasons"])
    # And the per-metric structured codes appear:
    for m in ("wf_ic_ir", "pkf_ic", "bootstrap_ci_lower", "param_sensitivity"):
        assert f"missing_{m}" in explicit_zero["reasons"]
    # Threshold-comparison messages should NOT appear, because the value is
    # now classified as missing not present-real.
    assert not any(reason.startswith("walk_forward_ic_ir 0.000")
                   for reason in explicit_zero["reasons"])
    assert not any(reason.startswith("purged_kfold_ic 0.000")
                   for reason in explicit_zero["reasons"])


def test_statistical_gate_uses_validation_and_backtest_metrics():
    from strategy_factory.application.submission_gate import runner

    result = asyncio.run(
        runner._run_statistical_gate(
            None,
            _semantic_ready_strategy("value_factor"),
            profile={"profile": "factor_rank_validation"},
            validation_report={
                "walk_forward": {"oos_rank_ic_ir": 0.35},
                "purged_kfold": {"oos_rank_ic_mean": 0.03},
                "bootstrap_ci": {"ci_lower": 0.02},
            },
            backtest_metrics={"parameter_perturbation_trade_stability": 0.85},
        )
    )

    assert result["passed"] is True
    assert result["wf_ic_ir"] == 0.35
    assert result["pkf_ic"] == 0.03
    assert result["bootstrap_ci_lower"] == 0.02
    assert result["param_sensitivity"] == 0.15
    assert not any(reason.startswith("missing_statistical_metrics") for reason in result["reasons"])
    assert result["metric_source_audit"]["wf_ic_ir"] == "validation_report.walk_forward"


def test_statistical_gate_derives_param_sensitivity_from_backtest_contract_only():
    from strategy_factory.application.submission_gate import runner

    result = asyncio.run(
        runner._run_statistical_gate(
            None,
            _semantic_ready_strategy("value_factor"),
            profile={"profile": "factor_rank_validation"},
            validation_report={
                "walk_forward": {"oos_rank_ic_ir": 0.35},
                "purged_kfold": {"oos_rank_ic_mean": 0.03},
                "bootstrap_ci": {"ci_lower": 0.02},
            },
            backtest_metrics={
                "backtest_metrics_contract": {
                    "status": "present",
                    "parameter_perturbation_trade_stability": 0.86,
                }
            },
        )
    )

    assert result["passed"] is True
    assert result["param_sensitivity"] == 0.14
    assert "missing_param_sensitivity" not in result["reasons"]
    assert (
        result["metric_source_audit"]["param_sensitivity"]
        == "backtest_metrics.parameter_perturbation_trade_stability_inverse"
    )


def test_compact_scalar_metrics_preserves_gate3_quality_evidence():
    from strategy_factory.application.compact_contracts import compact_scalar_metrics

    compact = compact_scalar_metrics(
        {
            "trade_count": 12,
            "trades_count": 12,
            "post_cost_sharpe": 1.12,
            "wf_ic_ir": 0.42,
            "pkf_ic": 0.05,
            "bootstrap_ci_lower": 0.03,
            "param_sensitivity": 0.14,
            "parameter_perturbation_trade_stability": 0.86,
            "deflated_sharpe_ratio": 0.21,
            "pbo": 0.28,
            "white_reality_check_pvalue": 0.09,
            "hansen_spa_pvalue": 0.08,
            "backtest_metrics_contract_status": "present",
            "metric_source_audit": {
                "param_sensitivity": "backtest_metrics.parameter_perturbation_trade_stability_inverse",
                "pkf_ic": "validation_report.purged_kfold",
            },
            "period_robustness": {"first_half_ic": 0.05, "second_half_ic": 0.04},
            "gate_3_evaluation": [
                {"metric": "pkf_ic", "status": "pass", "value": 0.05},
            ],
            "equity_curve": [1, 2, 3],
            "trades": [{"id": "too-heavy"}],
        }
    )

    assert compact["trade_count"] == 12
    assert compact["param_sensitivity"] == 0.14
    assert compact["parameter_perturbation_trade_stability"] == 0.86
    assert compact["backtest_metrics_contract_status"] == "present"
    assert compact["metric_source_audit"]["pkf_ic"] == "validation_report.purged_kfold"
    assert compact["period_robustness"]["first_half_ic"] == 0.05
    assert compact["gate_3_evaluation"][0]["status"] == "pass"
    assert "equity_curve" not in compact
    assert "trades" not in compact


def test_factor_pool_broad_validation_expands_narrow_diagnostic_targets():
    from strategy_factory.application.backtest_filter import BacktestFilter

    candidate = {
        "strategy_type": "momentum",
        "target_symbols": ["600519", "000001"],
        "params": {
            "target_symbols": ["600519", "000001"],
            "candidate_provenance": {
                "generator_mode": "factor_pool",
                "source_candidate_artifact_id": "factor-strong",
            },
        },
        "candidate_provenance": {
            "generator_mode": "factor_pool",
            "source_candidate_artifact_id": "factor-strong",
        },
        "validation_profile": {
            "profile": "factor_rank_validation",
            "validation_focus": "broad_generalization",
            "primary_validation_layer": "combined",
        },
    }

    evaluated_codes, target_codes, representative_codes, code_source, validation_focus = (
        BacktestFilter._resolve_backtest_plan(candidate)
    )

    assert target_codes == ["600519", "000001"]
    assert validation_focus == "broad_generalization"
    assert code_source == "target_plus_representative"
    assert len(evaluated_codes) > len(target_codes)
    assert any(code in evaluated_codes for code in representative_codes)


def test_factor_pool_validation_summary_becomes_gate3_statistical_report():
    from strategy_factory.application._submitter_actions.runner import (
        _StrategySubmitterActionsMixin,
    )

    params = {
        "factor_name": "gp_factor_5",
        "factor_pool_factor_id": "factor-strong",
        "source_candidate_artifact_id": "factor-strong",
        "source_validation_artifact_id": "factor-strong",
        "factor_pool_validation_summary": {
            "quality_status": "promoted",
            "quality_score": 94.5,
            "metrics": {
                "sample_dates": 60,
                "rank_ic_mean": 0.206818,
                "rank_ic_std": 0.141591,
                "rank_ic_ir": 1.460668,
            },
            "rating": {
                "grade": "A",
                "recommendation": "promote",
                "total_score": 88.6079,
                "governance": {
                    "raw_metrics": {
                        "avg_stability_ratio": 0.93,
                        "avg_degradation": 0.01,
                        "deflated_sharpe": 1.0,
                        "pbo": 0.0,
                        "white_reality_check_p_value": 0.0,
                        "hansen_spa_p_value": 0.0,
                    }
                },
            },
            "evidence_summary": {"avg_cross_section_n": 299.4},
            "persisted_outputs": {"ic_history_rows_total": 73},
            "qc_labels": {
                "rank_ic_ir": 0.0,
                "bootstrap_ci_lower": 0.0,
                "oos_pass": False,
                "oos_grade": "unknown",
            },
            "qc_shelf_decision": {"decision": "retire"},
            "qc_autoshelf_applied": False,
        },
    }

    report = _StrategySubmitterActionsMixin._factor_pool_validation_report_from_params(
        params,
        {"strategy_type": "momentum", "factor_pool_metadata": {"factor_name": "gp_factor_5"}},
        {"factor_name": "strategy:momentum"},
    )

    assert report["validation_report_source"] == "active_factor_pool_validation_summary"
    assert report["factor_name"] == "gp_factor_5"
    assert report["walk_forward"]["oos_rank_ic_ir"] == 1.460668
    assert report["purged_kfold"]["oos_rank_ic_mean"] == 0.206818
    assert report["bootstrap_ci"]["ci_lower"] > 0.0
    assert report["statistical_metrics"]["param_sensitivity"]["value"] == 0.07
    assert report["statistical_metrics"]["period_robustness"]["value"]["second_half_ic"] == 0.196818
    assert report["multiple_testing"]["deflated_sharpe"]["dsr"] == 1.0
    assert report["active_factor_pool_validation"]["ic_history_rows"] == 73


def test_factor_pool_validation_summary_does_not_override_qc_blocked_summary():
    from strategy_factory.application._submitter_actions.runner import (
        _StrategySubmitterActionsMixin,
    )

    base = {"factor_name": "strategy:momentum"}
    report = _StrategySubmitterActionsMixin._factor_pool_validation_report_from_params(
        {
            "factor_pool_validation_summary": {
                "quality_status": "promoted",
                "metrics": {
                    "sample_dates": 60,
                    "rank_ic_mean": 0.2,
                    "rank_ic_std": 0.1,
                    "rank_ic_ir": 1.0,
                },
                "rating": {"recommendation": "promote"},
                "qc_labels": {"oos_available": True, "oos_pass": False},
                "qc_shelf_decision": {"decision": "retire"},
            }
        },
        {"strategy_type": "momentum"},
        base,
    )

    assert report is base


def test_research_task_timeout_skips_without_local_fallback(monkeypatch):
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    class SlowGateway:
        async def generate_factory_candidates(self, *args, **kwargs):
            await asyncio.sleep(0.05)
            return {"generation": {"candidates": [{"id": "late_local_candidate"}]}}

    scheduler = StrategyFactoryScheduler(db_provider=lambda: object())
    scheduler._resolve_research_task_timeout_sec = MethodType(lambda self: 0.001, scheduler)

    cycle = asyncio.run(
        scheduler._generate_for_research_task(
            SlowGateway(),
            object(),
            {"date": "2026-05-22"},
            {"task_id": "timeout_task", "task_source": "snapshot", "generation_limit": 1},
        )
    )

    llm_generation = cycle["llm_generation"]
    assert cycle["generated_count"] == 0
    assert cycle["generation"]["candidates"] == []
    assert llm_generation["task_timeout_skip"] is True
    assert llm_generation["task_timeout_policy"] == "skip_without_local_fallback"
    assert llm_generation["external_provider"]["status"] == "skipped_timeout"


def test_bulk_research_tasks_get_longer_timeout(monkeypatch):
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    scheduler = StrategyFactoryScheduler()
    monkeypatch.setenv("STRATEGY_FACTORY_BULK_RESEARCH_TASK_TIMEOUT_SEC", "360")

    timeout_sec = scheduler._resolve_effective_research_task_timeout_sec(
        object(),
        {"task_id": "bulk_matrix_2026-05-22_600000_value_factor", "task_source": "bulk_stock_matrix"},
        base_timeout=180.0,
    )

    assert timeout_sec == 360.0


def test_success_summary_exposes_quality_diagnostics():
    from strategy_factory.application._cycle_success_summary import build_success_run_summary

    summary = build_success_run_summary(
        trace_id="trace_quality",
        snapshot={},
        candidates=[],
        passed=[],
        unique=[],
        eliminated=[],
        spawn_report={},
        submit_result={
            "created_strategy_pool": 0,
            "created_audit_only": 1,
            "gate3_quality_recorded": 1,
            "gate3_record_quality_qualified_count": 1,
            "gate3_record_diagnostic_only_count": 1,
            "gate_3_failure_reason_topn": [{"reason_code": "missing_statistical_metrics:wf_ic_ir", "count": 1}],
            "strategies": [
                {
                    "validation_grade": "D",
                    "record_only": True,
                    "gate3_record_only": True,
                    "gate3_record_diagnostic_only": True,
                    "metric_source_audit": {"expectancy": "event_window_metrics"},
                    "gate_b": {
                        "statistical_metric_missing_counts": {"wf_ic_ir": 1},
                    },
                },
                {
                    "validation_grade": "S",
                    "effective_validation_grade": "S",
                    "gate3_record_only": True,
                    "gate3_quality_recorded": True,
                    "gate3_record_quality_qualified": True,
                }
            ],
        },
        quality_gate_report={},
        backtest_report={},
        autonomy_summary={
            "external_llm_status_counts": {"skipped_timeout": 1},
            "pipeline_fallback_counts": {"empty_confirmations": 1},
            "task_timeout_skip_count": 1,
            "task_timeout_policy": "skip_without_local_fallback",
        },
        task_scan_summary={},
        task_source_counts={},
        bulk_stock_matrix_family_counts={},
        bulk_stock_matrix_allocation_pass_counts={},
        factor_research_summary={},
        factor_refresh_summary={},
        readiness_summary={},
        warmup_summary={},
        backtest_audit_summary={},
        submission_audit_summary={},
        vector_summary={},
        elapsed=1.0,
    )

    assert summary["llm_status_counts"] == {"skipped_timeout": 1}
    assert summary["pipeline_fallback_counts"] == {"empty_confirmations": 1}
    assert summary["gate_3_failure_topn"] == [{"reason_code": "missing_statistical_metrics:wf_ic_ir", "count": 1}]
    assert summary["validation_grade_counts"] == {"D": 1, "S": 1}
    assert summary["diagnostic_validation_grade_counts"] == {"D": 1}
    assert summary["qualified_validation_grade_counts"] == {"S": 1}
    assert summary["latest_validation_grade"] == "S"
    assert summary["latest_qualified_validation_grade"] == "S"
    assert summary["latest_diagnostic_validation_grade"] == "D"
    assert summary["gate3_quality_recorded"] == 1
    assert summary["gate3_record_diagnostic_only_count"] == 1
    assert summary["statistical_metric_missing_counts"]["wf_ic_ir"] == 1
    assert summary["metric_source_audit"]["expectancy"]["event_window_metrics"] == 1


def test_success_summary_exposes_market_temperature_context():
    from strategy_factory.application._cycle_success_summary import build_success_run_summary

    summary = build_success_run_summary(
        trace_id="trace_market_temperature",
        snapshot={},
        candidates=[],
        passed=[],
        unique=[],
        eliminated=[],
        spawn_report={},
        submit_result={},
        quality_gate_report={},
        backtest_report={},
        autonomy_summary={},
        task_scan_summary={},
        task_source_counts={},
        bulk_stock_matrix_family_counts={},
        bulk_stock_matrix_allocation_pass_counts={},
        factor_research_summary={},
        factor_refresh_summary={},
        readiness_summary={
            "market_temperature_context": {
                "available": True,
                "as_of": "2026-06-08",
                "temperature": 61.5,
                "state": "warm",
                "quality_status": "healthy",
                "readiness_status": "ready",
                "staleness_days": 0,
                "source_chain": ["market_temperature_snapshots"],
            }
        },
        warmup_summary={},
        backtest_audit_summary={},
        submission_audit_summary={},
        vector_summary={},
        elapsed=1.0,
    )

    assert summary["market_temperature_context_available"] is True
    assert summary["market_temperature"] == 61.5
    assert summary["market_temperature_state"] == "warm"
    assert summary["market_temperature_as_of"] == "2026-06-08"
    assert summary["market_temperature_quality_status"] == "healthy"
    assert summary["market_temperature_readiness_status"] == "ready"
    assert summary["market_temperature_context"]["source_chain"] == ["market_temperature_snapshots"]


def test_success_summary_maps_llm_provider_diagnostics_into_status_counts():
    from strategy_factory.application._cycle_success_summary import build_success_run_summary

    summary = build_success_run_summary(
        trace_id="trace_llm_mapping",
        snapshot={},
        candidates=[],
        passed=[],
        unique=[],
        eliminated=[],
        spawn_report={},
        submit_result={},
        quality_gate_report={},
        backtest_report={},
        autonomy_summary={
            "external_llm_status_counts": {"non_executable": 2},
            "pipeline_fallback_counts": {
                "cooldown_skip": 3,
                "local_fallback_preferred_or_skip": 10,
                "target_context_blocked": 1,
                "502 Bad Gateway": 4,
            },
            "external_llm_request_status_counts": {"cooldown_skip": 2, "failed": 4},
            "external_llm_last_error": "Server error '502 Bad Gateway'",
        },
        task_scan_summary={},
        task_source_counts={},
        bulk_stock_matrix_family_counts={},
        bulk_stock_matrix_allocation_pass_counts={},
        factor_research_summary={},
        factor_refresh_summary={},
        readiness_summary={},
        warmup_summary={},
        backtest_audit_summary={},
        submission_audit_summary={},
        vector_summary={},
        elapsed=1.0,
    )

    counts = summary["llm_status_counts"]
    assert counts["non_executable"] == 2
    assert counts["provider_cooldown_skip"] == 2
    assert counts["provider_http_502"] == 4
    assert summary["pipeline_fallback_counts"]["cooldown_skip"] == 3
    assert "local_fallback_preferred_or_skip" not in counts
    assert "target_context_blocked" not in counts


def test_scheduler_feedback_uses_gate3_failure_results():
    from strategy_factory.application._factory_scheduler_loop import (
        update_scheduler_family_gate_feedback,
    )

    results = {
        "stages": {
            "submit": {
                "gate_3_input": 3,
                "gate_3_passed": 0,
                "gate_3_failed": 3,
                "submitted": 0,
                "created_audit_only": 3,
                "gate_3_failure_reason_topn": [
                    {"reason_code": "weak_wf_ic_ir", "count": 3}
                ],
                "incubation_budget_summary": {
                    "family_counts": {"ma_cross": 3},
                },
            },
        },
        "summary": {},
    }

    feedback, update = update_scheduler_family_gate_feedback({}, results, cycle_count=1)

    ma_cross = feedback["ma_cross"]
    assert ma_cross["ema_submit_count"] == 0.0
    assert ma_cross["gate_3_input_count"] == 3
    assert ma_cross["gate_3_passed_count"] == 0
    assert ma_cross["gate_failure_rate"] == 1.0
    assert ma_cross["cooldown_active"] is True
    assert ma_cross["suppressed"] is True
    assert update["control_counts"]["suppress"] == 1


def test_scheduler_feedback_exploration_reset_skips_cycle_zero():
    from strategy_factory.application._factory_scheduler_loop import (
        update_scheduler_family_gate_feedback,
    )

    feedback, update = update_scheduler_family_gate_feedback(
        {"ma_cross": {"ema_submit_count": 0.0}},
        {"stages": {"submit": {"gate_3_input": 0}}},
        cycle_count=0,
        ema_floor=0.15,
        exploration_reset_interval=20,
    )

    assert feedback["ma_cross"]["ema_submit_count"] == 0.15
    assert feedback["ma_cross"]["ema_submit_count"] != 0.5
    assert update["control_counts"]["normal"] == 1


def test_budget_feedback_controls_gate3_failure_rate():
    from strategy_factory.application._budget_feedback import resolve_feedback_metrics

    cooldown = resolve_feedback_metrics(
        {
            "ma_cross": {
                "strategy_count": 2,
                "gate_failure_rate": 0.75,
                "gate_3_input_count": 2,
            }
        },
        family="ma_cross",
    )
    suppressed = resolve_feedback_metrics(
        {
            "ma_cross": {
                "strategy_count": 3,
                "gate_failure_rate": 1.0,
                "gate_3_input_count": 3,
            }
        },
        family="ma_cross",
    )

    assert cooldown["family_control_mode"] == "cooldown"
    assert "family_gate_failure_rate_cooldown" in cooldown["control_reasons"]
    assert suppressed["family_control_mode"] == "suppress"
    assert suppressed["control_mode"] == "suppress"
    assert "family_gate_failure_rate_suppress" in suppressed["control_reasons"]


def test_budget_feedback_watch_review_score_zero_is_observe_not_freeze():
    from strategy_factory.application._budget_feedback import resolve_feedback_metrics

    metrics = resolve_feedback_metrics(
        {
            "momentum": {
                "strategy_count": 1,
                "promotion_review_count": 1,
                "promotion_review_status": "watch",
                "promotion_review_recommendation": "observe",
                "promotion_review_score": 0.0,
            }
        },
        family="momentum",
    )

    assert metrics["control_mode"] == "cooldown"
    assert metrics["skill_control_mode"] == "cooldown"
    assert "family_promotion_review_watch" in metrics["control_reasons"]
    assert "family_promotion_review_score_freeze" not in metrics["control_reasons"]
    assert "family_promotion_review_score_freeze" not in metrics["skill_control_reasons"]


def test_budget_feedback_rejected_review_score_zero_still_freezes():
    from strategy_factory.application._budget_feedback import resolve_feedback_metrics

    metrics = resolve_feedback_metrics(
        {
            "momentum": {
                "strategy_count": 1,
                "promotion_review_count": 1,
                "promotion_review_status": "rejected",
                "promotion_review_recommendation": "deprecate",
                "promotion_review_score": 0.0,
            }
        },
        family="momentum",
    )

    assert metrics["control_mode"] == "freeze"
    assert metrics["skill_control_mode"] == "freeze"
    assert "family_promotion_review_rejected" in metrics["control_reasons"]
    assert "family_promotion_review_score_freeze" in metrics["control_reasons"]


def test_budget_feedback_summary_aggregates_validation_quality_rates():
    from strategy_factory.application._budget_feedback import (
        normalize_feedback_input_contract,
    )

    payload = normalize_feedback_input_contract(
        {
            "momentum": {
                "strategy_count": 3,
                "raw_validation_a_rate": 2 / 3,
                "raw_validation_b_rate": 1 / 3,
                "raw_validation_d_rate": 0.0,
                "raw_validation_total_score_mean": 70.0,
                "strict_incubation_ready_rate": 1.0,
            },
            "ma_cross": {
                "strategy_count": 1,
                "raw_validation_a_rate": 0.0,
                "raw_validation_b_rate": 0.0,
                "raw_validation_d_rate": 1.0,
                "raw_validation_total_score_mean": 35.0,
                "strict_incubation_ready_rate": 0.0,
            },
        },
        summary={"strategy_count": 4},
    )
    summary = payload["summary"]

    assert summary["raw_validation_a_rate"] == pytest.approx(0.5)
    assert summary["raw_validation_b_rate"] == pytest.approx(0.25)
    assert summary["raw_validation_d_rate"] == pytest.approx(0.25)
    assert summary["raw_validation_total_score_mean"] == pytest.approx(61.25)
    assert summary["strict_incubation_ready_rate"] == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_budget_feedback_source_sampling_keeps_mature_incubating_tail():
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )

    class FakeDB:
        async def list_strategies(self, status, strategy_type=None, limit=20, offset=0):
            if status == "incubating":
                return [
                    {
                        "id": f"incubating_{idx}",
                        "status": "incubating",
                        "strategy_type": "momentum",
                    }
                    for idx in range(int(limit or 0))
                ]
            if status == "listed":
                return [
                    {
                        "id": f"listed_{idx}",
                        "status": "listed",
                        "strategy_type": "momentum",
                    }
                    for idx in range(int(limit or 0))
                ]
            if status == "submitted":
                return [
                    {
                        "id": f"submitted_{idx}",
                        "status": "submitted",
                        "strategy_type": "momentum",
                        "params": {
                            "incubation_budget": {"track": "formal_incubation"}
                        },
                    }
                    for idx in range(int(limit or 0))
                ]
            return []

    rows = await FactorResearchBuilder._list_feedback_source_strategies(
        FakeDB(),
        limit=180,
    )
    ids = {str(row.get("id")) for row in rows}

    assert "incubating_70" in ids
    assert "incubating_119" in ids
    assert "incubating_135" not in ids
    assert any(item.startswith("listed_") for item in ids)
    assert any(item.startswith("submitted_") for item in ids)


@pytest.mark.asyncio
async def test_budget_feedback_fallback_includes_quality_report_fields():
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )
    from strategy_factory.infrastructure.mcp_services import clear_runtime_services

    class FakeDB:
        async def get_signal_stats(self, strategy_id):
            assert strategy_id == "factory_quality_a"
            return {
                "total_signals": 12,
                "hit_rate": {"1": 0.6, "5": 0.62},
                "skill_lcb": {"1": 0.04, "5": 0.05},
            }

        async def get_latest_strategy_quality_report(self, strategy_id):
            assert strategy_id == "factory_quality_a"
            return {
                "passed": 1,
                "summary": {
                    "validation_grade": "A",
                    "raw_validation_grade": "A",
                    "validation_total_score": 76.5,
                    "strict_incubation_ready": True,
                    "live_candidate_ready": False,
                },
            }

    clear_runtime_services()
    overview = await FactorResearchBuilder._load_feedback_evidence_overview(
        FakeDB(),
        {"id": "factory_quality_a"},
    )

    assert overview["strategy_id"] == "factory_quality_a"
    assert overview["raw_validation_grade"] == "A"
    assert overview["validation_total_score"] == 76.5
    assert overview["strict_incubation_ready"] is True
    assert overview["quality_report_passed"] is True


def test_spawner_feedback_blocks_failed_family_signal_variants():
    from strategy_factory.domain.spawner import StrategySpawner

    spawner = StrategySpawner()
    signal_candidates = [
        {
            "strategy_type": "ma_cross",
            "params": {"fast": 5},
            "trigger_thresholds": [{}, {}, {}],
        },
        {
            "strategy_type": "ma_cross",
            "params": {"fast": 10},
            "trigger_thresholds": [{}, {}, {}],
        },
    ]
    base_snapshot = {
        "fear_greed_index": 70,
        "north_fund_3d_net": 6_000_000_000,
    }

    expanded_without_feedback = spawner._expand_signal_variants(
        {**base_snapshot, "family_gate_feedback": {"ma_cross": {"ema_submit_count": 2.0}}},
        signal_candidates,
    )
    expanded_with_failed_feedback = spawner._expand_signal_variants(
        {
            **base_snapshot,
            "family_gate_feedback": {
                "ma_cross": {
                    "ema_submit_count": 2.0,
                    "gate_failure_rate": 1.0,
                    "cooldown_active": True,
                }
            },
        },
        signal_candidates,
    )

    assert len(expanded_without_feedback) > 0
    assert expanded_with_failed_feedback == []


def test_spawner_feedback_blocks_failed_family_quota_fill():
    from strategy_factory.domain.spawner import StrategySpawner

    spawner = StrategySpawner()
    current_candidates = [
        {
            "strategy_type": "quality_factor",
            "params": {},
            "trigger_thresholds": [{}],
        }
    ]
    base_snapshot = {
        "fear_greed_index": 50,
        "north_fund_3d_net": 0,
        "margin_5d_change_pct": 0,
        "completeness": {"completion_ratio": 1.0},
    }

    fill_without_feedback = spawner._fill_gaps(base_snapshot, current_candidates)
    assert "ma_cross" in {candidate["strategy_type"] for candidate in fill_without_feedback}
    assert any(
        candidate["strategy_type"] == "ma_cross"
        and candidate.get("quota_fill", {}).get("parameter_source") == "fixed_defaults"
        for candidate in fill_without_feedback
    )

    fill_with_failed_feedback = spawner._fill_gaps(
        {
            **base_snapshot,
            "family_gate_feedback": {
                "ma_cross": {
                    "ema_submit_count": 2.0,
                    "gate_failure_rate": 1.0,
                    "cooldown_active": True,
                }
            },
        },
        current_candidates,
    )

    filled_types = {candidate["strategy_type"] for candidate in fill_with_failed_feedback}
    assert "ma_cross" not in filled_types
    assert "gap_fill" in filled_types


def test_spawner_feedback_filters_failed_family_from_raw_signals():
    from strategy_factory.domain.spawner import StrategySpawner

    spawner = StrategySpawner()

    candidates = spawner.spawn(
        {
            "fear_greed_index": 50,
            "north_fund_3d_net": 0,
            "margin_5d_change_pct": 0,
            "completeness": {"completion_ratio": 1.0},
            "family_gate_feedback": {
                "ma_cross": {
                    "ema_submit_count": 2.0,
                    "gate_failure_rate": 1.0,
                    "cooldown_active": True,
                }
            },
        }
    )

    report_summary = spawner.get_last_report()["summary"]
    candidate_types = {candidate["strategy_type"] for candidate in candidates}
    assert "ma_cross" not in candidate_types
    assert report_summary["source_raw_counts"]["fear_greed"] >= 1
    assert report_summary["signal_feedback_limited_count"] >= 1
    assert report_summary["signal_feedback_limited_type_counts"]["ma_cross"] >= 1
    assert report_summary["signal_feedback_factor_by_type"]["ma_cross"] < 1.0


def test_spawner_missing_feedback_keeps_normal_quota_fill_budget():
    from strategy_factory.domain.spawner import StrategySpawner

    spawner = StrategySpawner()
    assert spawner._family_negative_feedback_factor("ma_cross", {}) == 1.0
    assert spawner._family_negative_feedback_factor(
        "ma_cross",
        {"family_gate_feedback": {}},
    ) == 1.0


def test_strategy_factory_run_summary_compaction_preserves_feedback_diagnostics():
    import json

    from aiask_quant_core.storage.sqlite.strategy_factory_json_budget import bounded_json_text

    summary = {
        "trace_id": "trace_quality_compaction",
        "gate_3_input": 3,
        "gate_3_passed": 0,
        "gate_3_failed": 3,
        "scheduler_cycle_count": 2,
        "execution_mode": "stock_first_observe_primary",
        "engine_version": "strategy_factory.stock_first_observe.primary",
        "stock_first_flow": "observe_first",
        "observe_first_enabled": True,
        "observe_first_mode": "score_only",
        "observed_candidate_count": 8,
        "pre_observe_gate_removed": True,
        "pre_observe_hard_reject_count": 0,
        "gate3_pre_observe_block_count": 0,
        "legacy_gate_executed": False,
        "legacy_funnel_executed": False,
        "legacy_gate_report_mode": "not_executed",
        "evidence_scoring_mode": "observe_first_no_legacy_gate",
        "router_enabled": True,
        "router_strict": True,
        "router_candidate_stock_count": 2,
        "router_applied_count": 2,
        "router_status_counts": {"applied": 2},
        "router_fallback_reason_counts": {},
        "profile_summary_generated_count": 2,
        "selected_router_applied_count": 6,
        "selected_profile_summary_missing_count": 0,
        "task_source_counts": {"bulk_stock_matrix": 2},
        "bulk_stock_task_count": 2,
        "snapshot_task_count": 0,
        "cycle_pipeline_stage_order": ["warmup", "collect", "evidence_scoring", "observe_intake"],
        "family_gate_feedback_control_counts": {"suppress": 2, "cooldown": 1},
        "family_gate_feedback_updated_family_count": 3,
        "family_gate_feedback_active_families": ["momentum", "ma_cross", "value_factor"],
        "family_gate_feedback_gate_3_input": 3,
        "family_gate_feedback_gate_3_passed": 0,
        "family_gate_feedback_gate_3_failed": 3,
        "family_gate_feedback_failure_reason_topn": [
            {"reason_code": "weak_wf_ic_ir", "count": 3}
        ],
        "raw_results": [{"candidate_id": f"candidate_{index}", "payload": "x" * 256} for index in range(80)],
    }

    stored = json.loads(
        bounded_json_text("strategy_factory_runs.summary", summary, max_bytes=4096)
    )

    assert stored["storage_mode"] == "compact_json"
    assert stored["trace_id"] == "trace_quality_compaction"
    assert stored["gate_3_input"] == 3
    assert stored["gate_3_failed"] == 3
    assert stored["scheduler_cycle_count"] == 2
    assert stored["execution_mode"] == "stock_first_observe_primary"
    assert stored["engine_version"] == "strategy_factory.stock_first_observe.primary"
    assert stored["stock_first_flow"] == "observe_first"
    assert stored["observe_first_enabled"] is True
    assert stored["observed_candidate_count"] == 8
    assert stored["pre_observe_gate_removed"] is True
    assert stored["gate3_pre_observe_block_count"] == 0
    assert stored["legacy_gate_executed"] is False
    assert stored["legacy_funnel_executed"] is False
    assert stored["legacy_gate_report_mode"] == "not_executed"
    assert stored["evidence_scoring_mode"] == "observe_first_no_legacy_gate"
    assert stored["router_enabled"] is True
    assert stored["router_strict"] is True
    assert stored["router_applied_count"] == 2
    assert stored["router_status_counts"] == {"applied": 2}
    assert stored["profile_summary_generated_count"] == 2
    assert stored["selected_router_applied_count"] == 6
    assert stored["task_source_counts"] == {"bulk_stock_matrix": 2}
    assert stored["cycle_pipeline_stage_order"] == [
        "warmup",
        "collect",
        "evidence_scoring",
        "observe_intake",
    ]
    assert stored["family_gate_feedback_control_counts"] == {"suppress": 2, "cooldown": 1}
    assert stored["family_gate_feedback_updated_family_count"] == 3
    assert stored["family_gate_feedback_active_families"] == ["momentum", "ma_cross", "value_factor"]
    assert stored["family_gate_feedback_failure_reason_topn"] == [
        {"reason_code": "weak_wf_ic_ir", "count": 3}
    ]
    assert "raw_results" not in stored
