"""Stable quality report construction from normalized gate evidence."""

from __future__ import annotations

from typing import Any, Optional

from ._quality_reporting_normalizers import (
    _derive_review_issue_buckets,
    _normalize_committee_review,
    _normalized_string_list,
    normalize_quality_gate_result,
    safe_metric_value,
)
from .market_evidence import summarize_market_fact_gate


def build_quality_report(
    strategy_id: str,
    strategy_type: Optional[str],
    quality_gate: Optional[dict],
    validation_report: Optional[dict],
    risk_report: Optional[dict],
    dedup_report: Optional[dict],
    backtest_metrics: Optional[dict],
    snapshot: Optional[dict],
    status_after_review: Optional[str],
    review_source: str,
    report_type: str,
    spawn_reason: Optional[str] = None,
    submission_audit: Optional[dict] = None,
) -> dict:
    normalized_gate = normalize_quality_gate_result(quality_gate)
    validation = dict(validation_report or {})
    rating = validation.get("rating") or {}
    dedup = dict(dedup_report or {})
    backtest = dict(backtest_metrics or {})
    audit = dict(submission_audit or {})
    candidate_provenance = dict(audit.get("candidate_provenance") or {})
    strategy_profile = dict(candidate_provenance.get("strategy_profile") or {})
    event_window_metrics = dict(backtest.get("event_window_metrics") or {})
    cost_assumptions = dict(backtest.get("cost_assumptions") or {})
    backtest_assumptions = dict(backtest.get("backtest_assumptions") or {})
    economic_semantics = dict(backtest.get("economic_semantics") or {})
    execution_reality = {
        "market_ruleset": cost_assumptions.get("market_ruleset") or backtest_assumptions.get("market_ruleset"),
        "sell_tax_rate": (
            cost_assumptions.get("sell_tax_rate")
            if cost_assumptions.get("sell_tax_rate") is not None
            else backtest_assumptions.get("sell_tax_rate")
        ),
        "min_trade_lot": (
            cost_assumptions.get("min_trade_lot")
            if cost_assumptions.get("min_trade_lot") is not None
            else backtest_assumptions.get("min_trade_lot")
        ),
        "t_plus_one": (
            cost_assumptions.get("t_plus_one")
            if cost_assumptions.get("t_plus_one") is not None
            else backtest_assumptions.get("t_plus_one")
        ),
        "arrival_price_policy": (
            cost_assumptions.get("arrival_price_policy")
            or backtest_assumptions.get("arrival_price_policy")
        ),
        "market_impact_bps": (
            cost_assumptions.get("market_impact_bps")
            if cost_assumptions.get("market_impact_bps") is not None
            else backtest_assumptions.get("market_impact_bps")
        ),
        "implementation_shortfall_proxy": (
            cost_assumptions.get("implementation_shortfall_proxy")
            if cost_assumptions.get("implementation_shortfall_proxy") is not None
            else backtest_assumptions.get("implementation_shortfall_proxy")
        ),
        "max_position_pct": backtest_assumptions.get("max_position_pct"),
        "target_weight_scheme": backtest_assumptions.get("target_weight_scheme"),
        "position_assumption": backtest.get("position_assumption") or backtest_assumptions.get("position_assumption"),
        "tradability_filter": backtest_assumptions.get("tradability_filter"),
        "capacity_bucket": backtest_assumptions.get("capacity_bucket") or economic_semantics.get("capacity_bucket"),
        "turnover_cost_class": backtest_assumptions.get("turnover_cost_class") or economic_semantics.get("turnover_cost_class"),
        "position_sizing_rationale": backtest_assumptions.get("position_sizing_rationale") or economic_semantics.get("position_sizing_rationale"),
        "expected_turnover_band": backtest_assumptions.get("expected_turnover_band") or economic_semantics.get("expected_turnover_band"),
        "market_regime_assumption": backtest_assumptions.get("market_regime_assumption") or economic_semantics.get("market_regime_assumption"),
    }
    submission_lane = audit.get("submission_lane")
    final_status = audit.get("final_status") or status_after_review
    direct_trade_candidate = bool(audit.get("direct_trade_candidate"))
    live_review_ready = bool(audit.get("live_review_ready"))
    paper_lane_ready = bool(audit.get("paper_lane_ready"))
    paper_account_id = audit.get("paper_account_id") or audit.get("live_review_account_id")
    paper_account_status = audit.get("paper_account_status")
    runtime_control_mode = audit.get("runtime_control_mode")
    runtime_control_status = audit.get("runtime_control_status")
    promotion_review_id = audit.get("promotion_review_id")
    promotion_review_status = audit.get("promotion_review_status")
    promotion_review_recommendation = audit.get("promotion_review_recommendation")
    pool_admission_applied = bool(audit.get("pool_admission_applied"))
    promotion_applied_transition = dict(audit.get("promotion_applied_transition") or {})
    runtime_bootstrap_eligible = (
        bool(audit.get("runtime_bootstrap_eligible"))
        if audit.get("runtime_bootstrap_eligible") is not None
        else None
    )
    runtime_bootstrap_reason = audit.get("runtime_bootstrap_reason")
    runtime_bootstrap_budget_tier = audit.get("runtime_bootstrap_budget_tier")
    runtime_playbook_present = (
        bool(audit.get("runtime_playbook_present"))
        if audit.get("runtime_playbook_present") is not None
        else None
    )
    execution_audit_gate_status = (
        audit.get("execution_audit_gate_status")
        or normalized_gate.get("execution_audit_gate_status")
    )
    execution_audit_gate_reasons = list(
        audit.get("execution_audit_gate_reasons")
        or normalized_gate.get("execution_audit_gate_reasons")
        or []
    )
    execution_hard_gate_passed = (
        bool(audit.get("execution_hard_gate_passed"))
        if audit.get("execution_hard_gate_passed") is not None
        else (
            bool(normalized_gate.get("execution_hard_gate_passed"))
            if normalized_gate.get("execution_hard_gate_passed") is not None
            else None
        )
    )
    execution_audit_snapshot_id = audit.get("execution_audit_snapshot_id")
    execution_audit_as_of = audit.get("execution_audit_as_of")
    trace_id = audit.get("trace_id")
    correlation_id = audit.get("correlation_id")
    factory_run_id = audit.get("factory_run_id")
    parent_task_run_id = audit.get("parent_task_run_id")
    source_action = audit.get("source_action")
    submission_action = dict(audit.get("submission_action") or {})
    submission_action_type = audit.get("submission_action_type")
    submission_action_trigger = audit.get("submission_action_trigger")
    submission_action_gaps = list(audit.get("submission_action_gaps") or [])
    submission_action_fallback_conditions = list(audit.get("submission_action_fallback_conditions") or [])
    submission_action_next_step = audit.get("submission_action_next_step")
    submission_action_completed = bool(audit.get("submission_action_completed"))
    committee_review = _normalize_committee_review(
        audit.get("committee_review") or snapshot.get("committee_review")
    )
    trade_quality_adjustment = dict(validation.get("trade_quality_adjustment") or {})
    effective_validation_grade = (
        normalized_gate.get("effective_validation_grade")
        or normalized_gate.get("validation_grade")
        or rating.get("grade")
    )
    raw_validation_grade = normalized_gate.get("raw_validation_grade") or rating.get("grade")
    validation_total_score = safe_metric_value(rating, "total_score")
    raw_validation_total_score = (
        safe_metric_value(rating, "total_score")
        if trade_quality_adjustment.get("applied")
        else safe_metric_value(rating, "base_total_score", "total_score")
    )
    raw_validation_baseline_total_score = safe_metric_value(
        rating,
        "base_total_score",
        "total_score",
    )
    semantic_summary_fields: dict[str, Any] = {}
    for field_name in (
        "evidence_chain",
        "prediction_contract",
        "confidence_contract",
        "evidence_alignment_audit",
        "legacy_semantic_contract",
        "contradiction_count",
        "proxy_dependency_score",
    ):
        value = audit.get(field_name)
        if value in (None, "", [], {}):
            value = normalized_gate.get(field_name)
        if value in (None, "", [], {}):
            continue
        semantic_summary_fields[field_name] = value
    evidence_chain = dict(semantic_summary_fields.get("evidence_chain") or {})
    evidence_alignment_audit = dict(semantic_summary_fields.get("evidence_alignment_audit") or {})
    market_fact_gate = summarize_market_fact_gate(
        evidence_chain.get("market_facts"),
        audit.get("market_facts"),
        snapshot.get("market_facts"),
    )
    if market_fact_gate.get("market_facts"):
        evidence_chain["market_facts"] = list(market_fact_gate.get("market_facts") or [])
        semantic_summary_fields["evidence_chain"] = evidence_chain
    evidence_alignment_audit = {
        **evidence_alignment_audit,
        "market_fact_gate_status": market_fact_gate.get("market_fact_gate_status"),
        "hard_fact_count": market_fact_gate.get("hard_fact_count"),
        "degraded_fact_count": market_fact_gate.get("degraded_fact_count"),
        "evidence_debt_reasons": list(market_fact_gate.get("evidence_debt_reasons") or []),
    }
    if evidence_alignment_audit:
        semantic_summary_fields["evidence_alignment_audit"] = evidence_alignment_audit
    pool_profile = (
        candidate_provenance.get("pool_profile")
        or strategy_profile.get("pool_profile")
    )
    volatility_bucket = (
        candidate_provenance.get("volatility_bucket")
        or strategy_profile.get("volatility_bucket")
    )
    liquidity_bucket = (
        candidate_provenance.get("liquidity_bucket")
        or strategy_profile.get("liquidity_bucket")
    )
    family_mix_constraints = dict(candidate_provenance.get("family_mix_constraints") or {})
    stop_rule_source = (
        backtest_assumptions.get("stop_rule_source")
        or backtest_assumptions.get("stop_loss_mode")
        or ("atr_bucketed" if pool_profile else None)
        or "fixed_pct_legacy"
    )
    risk_regime_fit = (
        candidate_provenance.get("regime_fit")
        or strategy_profile.get("regime_fit")
        or "unknown"
    )
    trend_cluster_ratio = (
        audit.get("trend_cluster_ratio")
        if audit.get("trend_cluster_ratio") is not None
        else candidate_provenance.get("trend_cluster_ratio")
    )
    diversification_debt = _normalized_string_list(
        audit.get("diversification_debt")
        or candidate_provenance.get("diversification_debt")
    )
    pool_profile_distribution = dict(
        audit.get("pool_profile_distribution")
        or candidate_provenance.get("pool_profile_distribution")
        or {}
    )
    review_issue_buckets, review_issue_primary = _derive_review_issue_buckets(
        strategy_type,
        candidate_provenance=candidate_provenance,
        strategy_profile=strategy_profile,
        market_fact_gate=market_fact_gate,
        backtest_assumptions=backtest_assumptions,
        audit=audit,
    )
    summary = {
        "strategy_id": strategy_id,
        "strategy_type": strategy_type,
        "status_after_review": status_after_review,
        "final_status": final_status,
        "validation_grade": effective_validation_grade,
        "raw_validation_grade": raw_validation_grade,
        "effective_validation_grade": effective_validation_grade,
        "validation_grade_adjustment_reason": (
            normalized_gate.get("validation_grade_adjustment_reason")
            or trade_quality_adjustment.get("adjustment_reason")
        ),
        "validation_total_score": validation_total_score,
        "raw_validation_total_score": raw_validation_total_score,
        "raw_validation_baseline_total_score": raw_validation_baseline_total_score,
        "review_source": review_source,
        "primary_validation_layer": normalized_gate.get("primary_validation_layer"),
        "admission_stage": normalized_gate.get("admission_stage"),
        "incubation_pass_mode": normalized_gate.get("incubation_pass_mode"),
        "research_candidate_ready": bool(normalized_gate.get("research_candidate_ready")),
        "incubation_candidate_ready": bool(normalized_gate.get("incubation_candidate_ready")),
        "live_candidate_ready": bool(normalized_gate.get("live_candidate_ready")),
        "gate_b_review_decision": normalized_gate.get("gate_b_review_decision"),
        "business_admission_decision": dict(normalized_gate.get("business_admission_decision") or {}),
        "benchmark_comparison": dict(normalized_gate.get("benchmark_comparison") or {}),
        "cost_sensitivity_summary": dict(normalized_gate.get("cost_sensitivity_summary") or {}),
        "cash_sleeve_audit": dict(normalized_gate.get("cash_sleeve_audit") or {}),
        "family_holding_bucket": dict(normalized_gate.get("family_holding_bucket") or {}),
        "submission_lane": submission_lane,
        "direct_trade_candidate": direct_trade_candidate,
        "live_review_ready": live_review_ready,
        "paper_lane_ready": paper_lane_ready,
        "paper_account_id": paper_account_id,
        "paper_account_status": paper_account_status,
        "runtime_control_mode": runtime_control_mode,
        "runtime_control_status": runtime_control_status,
        "runtime_bootstrap_eligible": runtime_bootstrap_eligible,
        "runtime_bootstrap_reason": runtime_bootstrap_reason,
        "runtime_bootstrap_budget_tier": runtime_bootstrap_budget_tier,
        "runtime_playbook_present": runtime_playbook_present,
        "execution_audit_gate_status": execution_audit_gate_status,
        "execution_audit_gate_reasons": execution_audit_gate_reasons,
        "execution_hard_gate_passed": execution_hard_gate_passed,
        "execution_audit_snapshot_id": execution_audit_snapshot_id,
        "execution_audit_as_of": execution_audit_as_of,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "factory_run_id": factory_run_id,
        "parent_task_run_id": parent_task_run_id,
        "source_action": source_action,
        "promotion_review_id": promotion_review_id,
        "promotion_review_status": promotion_review_status,
        "promotion_review_recommendation": promotion_review_recommendation,
        "pool_admission_applied": pool_admission_applied,
        "submission_action_type": submission_action_type,
        "submission_action_trigger": submission_action_trigger,
        "submission_action_gaps": submission_action_gaps,
        "submission_action_fallback_conditions": submission_action_fallback_conditions,
        "submission_action_next_step": submission_action_next_step,
        "submission_action_completed": submission_action_completed,
        "refresh_mode": audit.get("refresh_mode") or dedup.get("refresh_mode"),
        "source_candidate_artifact_id": candidate_provenance.get("source_candidate_artifact_id"),
        "candidate_family": candidate_provenance.get("candidate_family"),
        "candidate_family_id": candidate_provenance.get("candidate_family_id"),
        "holding_period_bucket": candidate_provenance.get("holding_period_bucket"),
        "alpha_source": candidate_provenance.get("alpha_source"),
        "risk_level": candidate_provenance.get("risk_level"),
        "regime_fit": candidate_provenance.get("regime_fit"),
        "risk_regime_fit": risk_regime_fit,
        "generator_mode": candidate_provenance.get("generator_mode"),
        "pool_profile": pool_profile,
        "volatility_bucket": volatility_bucket,
        "liquidity_bucket": liquidity_bucket,
        "evidence_gate_status": market_fact_gate.get("market_fact_gate_status"),
        "hard_fact_count": market_fact_gate.get("hard_fact_count"),
        "degraded_fact_count": market_fact_gate.get("degraded_fact_count"),
        "evidence_debt_reasons": list(market_fact_gate.get("evidence_debt_reasons") or []),
        "stop_rule_source": stop_rule_source,
        "trend_cluster_ratio": trend_cluster_ratio,
        "diversification_debt": diversification_debt,
        "pool_profile_distribution": pool_profile_distribution,
        "review_issue_buckets": review_issue_buckets,
        "review_issue_primary": review_issue_primary,
        "market_ruleset": execution_reality.get("market_ruleset"),
        "target_weight_scheme": execution_reality.get("target_weight_scheme"),
        "position_assumption": execution_reality.get("position_assumption"),
        "capacity_bucket": execution_reality.get("capacity_bucket"),
        "turnover_cost_class": execution_reality.get("turnover_cost_class"),
        "position_sizing_rationale": execution_reality.get("position_sizing_rationale"),
        "expected_turnover_band": execution_reality.get("expected_turnover_band"),
        "market_regime_assumption": execution_reality.get("market_regime_assumption"),
        "committee_decision": committee_review.get("decision"),
        "committee_final_score": committee_review.get("final_score"),
        "strict_incubation_ready": bool(normalized_gate.get("strict_incubation_ready")),
        "strict_incubation_blocked": bool(normalized_gate.get("strict_incubation_blocked")),
        "cohort_effective_trials": normalized_gate.get("cohort_effective_trials"),
        "batch_correlation_mode": normalized_gate.get("batch_correlation_mode"),
        "batch_correlation_sibling_count": normalized_gate.get("batch_correlation_sibling_count"),
        "multiple_testing_cohort_mode": dict(normalized_gate.get("multiple_testing_registry") or {}).get(
            "multiple_testing_cohort_mode"
        ),
        **semantic_summary_fields,
    }
    if spawn_reason:
        summary["spawn_reason"] = spawn_reason
    report = {
        "report_type": report_type,
        "passed": bool(normalized_gate.get("passed")),
        "final_status": final_status,
        "summary": summary,
        "quality_gate": normalized_gate,
        "validation_report": validation,
        "risk_report": dict(risk_report or {}),
        "dedup_report": dedup,
        "backtest_metrics": backtest,
        "constraint_check": dict(backtest.get("constraint_check") or {}),
        "validation_profile": {
            "profile": normalized_gate.get("profile"),
            "validation_focus": normalized_gate.get("validation_focus"),
            "primary_validation_layer": normalized_gate.get("primary_validation_layer"),
        },
        "admission_stage": normalized_gate.get("admission_stage"),
        "incubation_pass_mode": normalized_gate.get("incubation_pass_mode"),
        "research_candidate_ready": bool(normalized_gate.get("research_candidate_ready")),
        "incubation_candidate_ready": bool(normalized_gate.get("incubation_candidate_ready")),
        "live_candidate_ready": bool(normalized_gate.get("live_candidate_ready")),
        "gate_b_review_decision": normalized_gate.get("gate_b_review_decision"),
        "business_admission_decision": dict(normalized_gate.get("business_admission_decision") or {}),
        "benchmark_comparison": dict(normalized_gate.get("benchmark_comparison") or {}),
        "cost_sensitivity_summary": dict(normalized_gate.get("cost_sensitivity_summary") or {}),
        "cash_sleeve_audit": dict(normalized_gate.get("cash_sleeve_audit") or {}),
        "family_holding_bucket": dict(normalized_gate.get("family_holding_bucket") or {}),
        "submission_lane": submission_lane,
        "direct_trade_candidate": direct_trade_candidate,
        "live_review_ready": live_review_ready,
        "paper_lane_ready": paper_lane_ready,
        "paper_account_id": paper_account_id,
        "paper_account_status": paper_account_status,
        "runtime_control_mode": runtime_control_mode,
        "runtime_control_status": runtime_control_status,
        "runtime_bootstrap_eligible": runtime_bootstrap_eligible,
        "runtime_bootstrap_reason": runtime_bootstrap_reason,
        "runtime_bootstrap_budget_tier": runtime_bootstrap_budget_tier,
        "runtime_playbook_present": runtime_playbook_present,
        "execution_audit_gate_status": execution_audit_gate_status,
        "execution_audit_gate_reasons": execution_audit_gate_reasons,
        "execution_hard_gate_passed": execution_hard_gate_passed,
        "execution_audit_snapshot_id": execution_audit_snapshot_id,
        "execution_audit_as_of": execution_audit_as_of,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "factory_run_id": factory_run_id,
        "parent_task_run_id": parent_task_run_id,
        "source_action": source_action,
        "promotion_review_id": promotion_review_id,
        "promotion_review_status": promotion_review_status,
        "promotion_review_recommendation": promotion_review_recommendation,
        "pool_admission_applied": pool_admission_applied,
        "promotion_applied_transition": promotion_applied_transition,
        "submission_action": submission_action,
        "submission_action_type": submission_action_type,
        "submission_action_trigger": submission_action_trigger,
        "submission_action_gaps": submission_action_gaps,
        "submission_action_fallback_conditions": submission_action_fallback_conditions,
        "submission_action_next_step": submission_action_next_step,
        "submission_action_completed": submission_action_completed,
        "admission_block_reasons": list(normalized_gate.get("admission_block_reasons") or []),
        "admission_evaluations": dict(normalized_gate.get("admission_evaluations") or {}),
        "strict_incubation_ready": bool(normalized_gate.get("strict_incubation_ready")),
        "strict_incubation_blocked": bool(normalized_gate.get("strict_incubation_blocked")),
        "validation_grade": effective_validation_grade,
        "raw_validation_grade": raw_validation_grade,
        "effective_validation_grade": effective_validation_grade,
        "validation_grade_adjustment_reason": (
            normalized_gate.get("validation_grade_adjustment_reason")
            or trade_quality_adjustment.get("adjustment_reason")
        ),
        "validation_total_score": validation_total_score,
        "raw_validation_total_score": raw_validation_total_score,
        "raw_validation_baseline_total_score": raw_validation_baseline_total_score,
        "admission_review_context": dict(normalized_gate.get("admission_review_context") or {}),
        "cohort_effective_trials": normalized_gate.get("cohort_effective_trials"),
        "event_window_config": dict(backtest.get("event_window_config") or {}),
        "event_window_metrics": event_window_metrics,
        "position_assumption": backtest.get("position_assumption"),
        "cost_assumptions": cost_assumptions,
        "explicit_cost_breakdown": dict(backtest.get("explicit_cost_breakdown") or {}),
        "implicit_cost_breakdown": dict(backtest.get("implicit_cost_breakdown") or {}),
        "tradability_summary": dict(backtest.get("tradability_summary") or {}),
        "capacity_summary": dict(backtest.get("capacity_summary") or {}),
        "implementation_shortfall_model_source": backtest.get("implementation_shortfall_model_source"),
        "implementation_shortfall_components": dict(backtest.get("implementation_shortfall_components") or {}),
        "backtest_assumptions": backtest_assumptions,
        "execution_reality": execution_reality,
        "attempt_adjustment": dict(normalized_gate.get("attempt_adjustment") or {}),
        "run_correction": {
            "mode": normalized_gate.get("run_correction_mode"),
            "raw_sharpe_proxy": normalized_gate.get("raw_sharpe_proxy"),
            "deflated_sharpe_proxy": normalized_gate.get("deflated_sharpe_proxy"),
            "pbo_proxy": normalized_gate.get("pbo_proxy"),
            "reality_check_pvalue_proxy": normalized_gate.get("reality_check_pvalue_proxy"),
            "spa_pvalue_proxy": normalized_gate.get("spa_pvalue_proxy"),
            "multiple_testing_mode": normalized_gate.get("multiple_testing_mode"),
            "deflated_sharpe_ratio": normalized_gate.get("deflated_sharpe_ratio"),
            "deflated_sharpe_reference_sharpe": normalized_gate.get("deflated_sharpe_reference_sharpe"),
            "deflated_sharpe_effective_trials": normalized_gate.get("deflated_sharpe_effective_trials"),
            "batch_correlation_mode": normalized_gate.get("batch_correlation_mode"),
            "batch_correlation_multiplier": normalized_gate.get("batch_correlation_multiplier"),
            "batch_correlation_sibling_count": normalized_gate.get("batch_correlation_sibling_count"),
            "pbo": normalized_gate.get("pbo"),
            "white_reality_check_pvalue": normalized_gate.get("white_reality_check_pvalue"),
            "hansen_spa_pvalue": normalized_gate.get("hansen_spa_pvalue"),
            "multiple_testing": dict(normalized_gate.get("multiple_testing") or {}),
        },
        "multiple_testing_registry": dict(normalized_gate.get("multiple_testing_registry") or {}),
        "committee_review": committee_review,
        "task_signature": audit.get("task_signature"),
        "refresh_mode": audit.get("refresh_mode") or dedup.get("refresh_mode"),
        "task_preference": dict(audit.get("task_preference") or {}),
        "candidate_provenance": candidate_provenance,
        "strategy_profile": strategy_profile,
        "pool_profile": pool_profile,
        "volatility_bucket": volatility_bucket,
        "liquidity_bucket": liquidity_bucket,
        "family_mix_constraints": family_mix_constraints,
        "evidence_gate_status": market_fact_gate.get("market_fact_gate_status"),
        "hard_fact_count": market_fact_gate.get("hard_fact_count"),
        "degraded_fact_count": market_fact_gate.get("degraded_fact_count"),
        "evidence_debt_reasons": list(market_fact_gate.get("evidence_debt_reasons") or []),
        "trend_cluster_ratio": trend_cluster_ratio,
        "diversification_debt": diversification_debt,
        "pool_profile_distribution": pool_profile_distribution,
        "review_issue_buckets": review_issue_buckets,
        "review_issue_primary": review_issue_primary,
        "risk_regime_fit": risk_regime_fit,
        "stop_rule_source": stop_rule_source,
        "snapshot": dict(snapshot or {}),
    }
    for field_name, value in semantic_summary_fields.items():
        report[field_name] = value
    return report
