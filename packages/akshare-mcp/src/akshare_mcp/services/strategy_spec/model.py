"""StrategySpec data model facade."""


from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from aiask_quant_core.strategy_explanation import build_strategy_explanation


from . import constants as _constants
from . import defaults as _defaults
from . import dsl_builder as _dsl_builder
from . import normalizers as _normalizers
from . import runtime_contracts as _runtime_contracts

for _module in (
    _constants,
    _normalizers,
    _defaults,
    _dsl_builder,
    _runtime_contracts,
):
    globals().update(
        {
            name: getattr(_module, name)
            for name in dir(_module)
            if not name.startswith("__")
        }
    )

def _strategy_spec_to_candidate(self, source: str, experiment_id: str) -> dict:
# --- model_parts/spec_types.py ---
    metadata = dict(self.metadata or {})
    source_candidate = dict(metadata.get("source_candidate") or {})
    source_candidate_params = dict(source_candidate.get("params") or {})

    def _list_value(*values: Any) -> list[Any]:
        for value in values:
            if isinstance(value, (list, tuple, set)) and value:
                return list(value)
        return []

    def _dict_value(*values: Any) -> dict[str, Any]:
        for value in values:
            if isinstance(value, dict) and value:
                return dict(value)
        return {}

    def _scalar_value(*values: Any) -> Any:
        for value in values:
            if value not in (None, "", [], {}):
                return value
        return None

    target_symbols = _normalize_code_list(
        metadata.get("target_symbols"),
        source_candidate.get("target_symbols"),
        metadata.get("stock_pool"),
        source_candidate.get("stock_pool"),
        dict(self.params or {}).get("target_symbols"),
        source_candidate_params.get("target_symbols"),
        dict(self.params or {}).get("stock_pool"),
        source_candidate_params.get("stock_pool"),
    )
    stock_pool = _dict_value(
        metadata.get("stock_pool"),
        source_candidate.get("stock_pool"),
        dict(self.params or {}).get("stock_pool"),
        source_candidate_params.get("stock_pool"),
        {"selection_mode": "explicit", "symbols": list(target_symbols)} if target_symbols else {},
    )
    research_task = _safe_normalize_research_task(_dict_value(
        metadata.get("research_task"),
        source_candidate.get("research_task"),
        dict(self.params or {}).get("research_task"),
        source_candidate_params.get("research_task"),
    ))
    candidate_family = str(
        _scalar_value(
            metadata.get("candidate_family"),
            source_candidate.get("candidate_family"),
            dict(self.params or {}).get("candidate_family"),
            source_candidate_params.get("candidate_family"),
            research_task.get("candidate_family"),
        )
        or ""
    ).strip().lower() or None
    candidate_family_id = str(
        _scalar_value(
            metadata.get("candidate_family_id"),
            source_candidate.get("candidate_family_id"),
            dict(self.params or {}).get("candidate_family_id"),
            source_candidate_params.get("candidate_family_id"),
            research_task.get("candidate_family_id"),
        )
        or ""
    ).strip() or None
    event_context = _dict_value(
        metadata.get("event_context"),
        source_candidate.get("event_context"),
        dict(self.params or {}).get("event_context"),
        source_candidate_params.get("event_context"),
    )
    selection_logic = _list_value(
        metadata.get("selection_logic"),
        source_candidate.get("selection_logic"),
    )
    research_scope = _dict_value(
        metadata.get("research_scope"),
        source_candidate.get("research_scope"),
    )
    hypothesis_artifact = _dict_value(
        metadata.get("hypothesis_artifact"),
        source_candidate.get("hypothesis_artifact"),
    )
    event_prefilter = _dict_value(
        metadata.get("event_prefilter"),
        source_candidate.get("event_prefilter"),
        dict(self.params or {}).get("event_prefilter"),
        source_candidate_params.get("event_prefilter"),
        hypothesis_artifact.get("event_prefilter"),
    )
    event_anchor = _dict_value(
        metadata.get("event_anchor"),
        source_candidate.get("event_anchor"),
        dict(self.params or {}).get("event_anchor"),
        source_candidate_params.get("event_anchor"),
        event_prefilter.get("event_anchor"),
        hypothesis_artifact.get("event_anchor"),
    )
    candidate_origin = _scalar_value(
        metadata.get("candidate_origin"),
        source_candidate.get("candidate_origin"),
        dict(self.params or {}).get("candidate_origin"),
        source_candidate_params.get("candidate_origin"),
    ) or "research_signal"
    target_pool_source = _scalar_value(
        metadata.get("target_pool_source"),
        source_candidate.get("target_pool_source"),
        dict(self.params or {}).get("target_pool_source"),
        source_candidate_params.get("target_pool_source"),
    ) or ("event_anchor" if event_anchor else "explicit_task")
    source_symbol_summary = _dict_value(
        metadata.get("source_symbol_summary"),
        source_candidate.get("source_symbol_summary"),
        research_task.get("source_symbol_summary"),
    )
    backtest_metrics = _dict_value(
        metadata.get("backtest_metrics"),
        source_candidate.get("backtest_metrics"),
        dict(self.params or {}).get("backtest_metrics"),
        source_candidate_params.get("backtest_metrics"),
    )
    task_source = _task_source(research_task, event_context)
    prediction_trace_id = normalize_prediction_trace_id(
        _scalar_value(
            metadata.get("prediction_trace_id"),
            source_candidate.get("prediction_trace_id"),
            dict(self.params or {}).get("prediction_trace_id"),
            source_candidate_params.get("prediction_trace_id"),
        ),
        _scalar_value(
            metadata.get("trace_id"),
            source_candidate.get("trace_id"),
            dict(self.params or {}).get("trace_id"),
            source_candidate_params.get("trace_id"),
        ),
        fallback=f"pred_{uuid4().hex[:12]}",
    )
    explicit_research_validation_contract = _dict_value(
        metadata.get("research_validation_contract"),
        source_candidate.get("research_validation_contract"),
        dict(self.params or {}).get("research_validation_contract"),
        source_candidate_params.get("research_validation_contract"),
    )
    research_validation_contract_source = _resolve_source_label(
        ("metadata", metadata.get("research_validation_contract")),
        ("source_candidate", source_candidate.get("research_validation_contract")),
        ("params", dict(self.params or {}).get("research_validation_contract")),
        ("source_candidate_params", source_candidate_params.get("research_validation_contract")),
    )
    holding_rationale = _scalar_value(
        metadata.get("holding_rationale"),
        source_candidate.get("holding_rationale"),
        hypothesis_artifact.get("holding_rationale"),
    )
    failure_mode = _scalar_value(
        metadata.get("failure_mode"),
        source_candidate.get("failure_mode"),
        hypothesis_artifact.get("failure_mode"),
    )
    alpha_half_life = _scalar_value(
        metadata.get("alpha_half_life"),
        source_candidate.get("alpha_half_life"),
        hypothesis_artifact.get("alpha_half_life"),
    )
    market_regime_assumption = _scalar_value(
        metadata.get("market_regime_assumption"),
        source_candidate.get("market_regime_assumption"),
        hypothesis_artifact.get("market_regime_assumption"),
    )
    evidence_chain = _dict_value(
        metadata.get("evidence_chain"),
        source_candidate.get("evidence_chain"),
        dict(self.params or {}).get("evidence_chain"),
        source_candidate_params.get("evidence_chain"),
    )
    prediction_contract = _dict_value(
        metadata.get("prediction_contract"),
        source_candidate.get("prediction_contract"),
        dict(self.params or {}).get("prediction_contract"),
        source_candidate_params.get("prediction_contract"),
    )
    confidence_contract = _dict_value(
        metadata.get("confidence_contract"),
        source_candidate.get("confidence_contract"),
        dict(self.params or {}).get("confidence_contract"),
        source_candidate_params.get("confidence_contract"),
    )
    evidence_alignment_audit = _dict_value(
        metadata.get("evidence_alignment_audit"),
        source_candidate.get("evidence_alignment_audit"),
        dict(self.params or {}).get("evidence_alignment_audit"),
        source_candidate_params.get("evidence_alignment_audit"),
    )
    market_facts = normalize_market_evidence_facts(
        metadata.get("market_facts"),
        source_candidate.get("market_facts"),
        dict(self.params or {}).get("market_facts"),
        source_candidate_params.get("market_facts"),
        evidence_chain.get("market_facts") if isinstance(evidence_chain, dict) else None,
    )
    if market_facts:
        evidence_chain = dict(evidence_chain or {})
        evidence_chain["market_facts"] = list(market_facts)
        evidence_alignment_audit = {
            **dict(evidence_alignment_audit or {}),
            **_build_market_fact_gate_audit(market_facts),
        }
    dsl_support_audit = _dict_value(
        metadata.get("dsl_support_audit"),
        source_candidate.get("dsl_support_audit"),
        dict(self.params or {}).get("dsl_support_audit"),
        source_candidate_params.get("dsl_support_audit"),
    )
    claim_to_trade_plan_map = _dict_value(
        metadata.get("claim_to_trade_plan_map"),
        source_candidate.get("claim_to_trade_plan_map"),
        dict(self.params or {}).get("claim_to_trade_plan_map"),
        source_candidate_params.get("claim_to_trade_plan_map"),
    )
    trade_plan_to_dsl_map = _dict_value(
        metadata.get("trade_plan_to_dsl_map"),
        source_candidate.get("trade_plan_to_dsl_map"),
        dict(self.params or {}).get("trade_plan_to_dsl_map"),
        source_candidate_params.get("trade_plan_to_dsl_map"),
    )
    explicit_dsl = _dict_value(
        metadata.get("dsl"),
        source_candidate.get("dsl"),
        dict(self.params or {}).get("dsl"),
        source_candidate_params.get("dsl"),
    )
    holding_horizon_source = _resolve_source_label(
        ("metadata", metadata.get("holding_horizon")),
        ("source_candidate", source_candidate.get("holding_horizon")),
        ("params", dict(self.params or {}).get("holding_horizon")),
        ("source_candidate_params", source_candidate_params.get("holding_horizon")),
    )
    holding_horizon = _dict_value(
        metadata.get("holding_horizon"),
        source_candidate.get("holding_horizon"),
        dict(self.params or {}).get("holding_horizon"),
        source_candidate_params.get("holding_horizon"),
    )
    if not holding_horizon:
        holding_horizon = _default_holding_horizon(
            self.strategy_type,
            research_task,
            task_source,
            alpha_half_life=alpha_half_life,
        )
    if alpha_half_life in (None, "", [], {}):
        alpha_half_life = holding_horizon.get("alpha_half_life") or holding_horizon.get("max_days")
    if market_regime_assumption in (None, "", [], {}):
        market_regime_assumption = _default_market_regime_assumption(
            self.strategy_type,
            task_source,
        )
    if holding_rationale not in (None, "", [], {}) and hypothesis_artifact.get("holding_rationale") in (None, "", [], {}):
        hypothesis_artifact["holding_rationale"] = holding_rationale
    if failure_mode not in (None, "", [], {}) and hypothesis_artifact.get("failure_mode") in (None, "", [], {}):
        hypothesis_artifact["failure_mode"] = failure_mode
    if market_regime_assumption not in (None, "", [], {}) and hypothesis_artifact.get("market_regime_assumption") in (None, "", [], {}):
        hypothesis_artifact["market_regime_assumption"] = market_regime_assumption
    if event_prefilter and hypothesis_artifact.get("event_prefilter") in (None, "", [], {}):
        hypothesis_artifact["event_prefilter"] = dict(event_prefilter)
    holding_horizon = _merge_holding_semantics(
        holding_horizon,
        holding_rationale=holding_rationale,
        alpha_half_life=alpha_half_life,
    )
    trade_plan_source = _resolve_source_label(
        ("metadata", metadata.get("trade_plan")),
        ("source_candidate", source_candidate.get("trade_plan")),
        ("params", dict(self.params or {}).get("trade_plan")),
        ("source_candidate_params", source_candidate_params.get("trade_plan")),
    )
    trade_plan = _dict_value(
        metadata.get("trade_plan"),
        source_candidate.get("trade_plan"),
        dict(self.params or {}).get("trade_plan"),
        source_candidate_params.get("trade_plan"),
    )
    if not trade_plan:
        trade_plan = _default_trade_plan(self.strategy_type, task_source)
    trade_plan = _ensure_trade_plan_execution_nodes(self.strategy_type, trade_plan)
    risk_rules_source = _resolve_source_label(
        ("metadata", metadata.get("risk_rules")),
        ("source_candidate", source_candidate.get("risk_rules")),
        ("params", dict(self.params or {}).get("risk_rules")),
        ("source_candidate_params", source_candidate_params.get("risk_rules")),
    )
    risk_rules = _dict_value(
        metadata.get("risk_rules"),
        source_candidate.get("risk_rules"),
        dict(self.params or {}).get("risk_rules"),
        source_candidate_params.get("risk_rules"),
    )
    if not risk_rules:
        risk_rules = _default_risk_rules(task_source, holding_horizon)
    position_sizing = _dict_value(
        metadata.get("position_sizing"),
        source_candidate.get("position_sizing"),
        dict(self.params or {}).get("position_sizing"),
        source_candidate_params.get("position_sizing"),
    )
    if not position_sizing:
        position_sizing = _default_position_sizing(target_symbols)
    rebalance_rule = _dict_value(
        metadata.get("rebalance_rule"),
        source_candidate.get("rebalance_rule"),
        dict(self.params or {}).get("rebalance_rule"),
        source_candidate_params.get("rebalance_rule"),
    )
    if not rebalance_rule:
        rebalance_rule = _default_rebalance_rule(
            self.strategy_type,
            task_source,
            holding_horizon=holding_horizon,
            alpha_half_life=alpha_half_life,
        )
    rebalance_rule = _merge_rebalance_semantics(
        rebalance_rule,
        task_source=task_source,
        holding_horizon=holding_horizon,
        alpha_half_life=alpha_half_life,
    )
    portfolio_spec = _dict_value(
        metadata.get("portfolio_spec"),
        source_candidate.get("portfolio_spec"),
        dict(self.params or {}).get("portfolio_spec"),
        source_candidate_params.get("portfolio_spec"),
    )
    if not portfolio_spec:
        portfolio_spec = _default_portfolio_spec(target_symbols)
    execution_assumptions_source = _resolve_source_label(
        ("metadata", metadata.get("execution_assumptions")),
        ("source_candidate", source_candidate.get("execution_assumptions")),
        ("params", dict(self.params or {}).get("execution_assumptions")),
        ("source_candidate_params", source_candidate_params.get("execution_assumptions")),
    )
# --- model_parts/spec_validation.py ---
    execution_assumptions = _dict_value(
        metadata.get("execution_assumptions"),
        source_candidate.get("execution_assumptions"),
        dict(self.params or {}).get("execution_assumptions"),
        source_candidate_params.get("execution_assumptions"),
    )
    if not execution_assumptions:
        execution_assumptions = _default_execution_assumptions(task_source)
    runtime_playbook_source = _resolve_source_label(
        ("metadata", metadata.get("runtime_playbook")),
        ("source_candidate", source_candidate.get("runtime_playbook")),
        ("params", dict(self.params or {}).get("runtime_playbook")),
        ("source_candidate_params", source_candidate_params.get("runtime_playbook")),
    )
    runtime_playbook = _dict_value(
        metadata.get("runtime_playbook"),
        source_candidate.get("runtime_playbook"),
        dict(self.params or {}).get("runtime_playbook"),
        source_candidate_params.get("runtime_playbook"),
    )
    validation_profile = _dict_value(
        metadata.get("validation_profile"),
        source_candidate.get("validation_profile"),
        dict(self.params or {}).get("validation_profile"),
        source_candidate_params.get("validation_profile"),
    )
    default_validation_profile = _default_validation_profile(self.strategy_type, research_task, task_source)
    if not validation_profile:
        validation_profile = dict(default_validation_profile)
    else:
        validation_profile = {
            **dict(default_validation_profile),
            **dict(validation_profile),
        }
    if not event_prefilter:
        event_prefilter = _default_event_prefilter(self.strategy_type, validation_profile)
    targeting_policy = _dict_value(
        metadata.get("targeting_policy"),
        source_candidate.get("targeting_policy"),
        dict(self.params or {}).get("targeting_policy"),
        source_candidate_params.get("targeting_policy"),
    )
    if not targeting_policy:
        targeting_policy = _default_targeting_policy(research_task)
    constraint_check = _dict_value(
        metadata.get("constraint_check"),
        source_candidate.get("constraint_check"),
        dict(self.params or {}).get("constraint_check"),
        source_candidate_params.get("constraint_check"),
    )
    if not constraint_check:
        constraint_check = _default_constraint_check(
            target_symbols=list(target_symbols),
            research_task=research_task,
            targeting_policy=targeting_policy,
        )
    position_model = _scalar_value(
        metadata.get("position_model"),
        source_candidate.get("position_model"),
        hypothesis_artifact.get("position_model"),
        position_sizing.get("mode"),
        portfolio_spec.get("position_assumption"),
    )
    capacity_assumption = _dict_value(
        metadata.get("capacity_assumption"),
        source_candidate.get("capacity_assumption"),
        hypothesis_artifact.get("capacity_assumption"),
    )
    capacity_assumption_source = _resolve_source_label(
        ("metadata", metadata.get("capacity_assumption")),
        ("source_candidate", source_candidate.get("capacity_assumption")),
        ("hypothesis_artifact", hypothesis_artifact.get("capacity_assumption")),
    )
    cost_sensitivity_grid = _dict_value(
        metadata.get("cost_sensitivity_grid"),
        source_candidate.get("cost_sensitivity_grid"),
        hypothesis_artifact.get("cost_sensitivity_grid"),
    )
    cost_sensitivity_grid_source = _resolve_source_label(
        ("metadata", metadata.get("cost_sensitivity_grid")),
        ("source_candidate", source_candidate.get("cost_sensitivity_grid")),
        ("hypothesis_artifact", hypothesis_artifact.get("cost_sensitivity_grid")),
    )
    instrument_profile = _normalize_instrument_profile(
        _dict_value(
            metadata.get("instrument_profile"),
            source_candidate.get("instrument_profile"),
            dict(self.params or {}).get("instrument_profile"),
            source_candidate_params.get("instrument_profile"),
            research_task.get("instrument_profile"),
        ),
        target_symbols=list(target_symbols),
        source_symbol_summary=source_symbol_summary,
    )
    execution_semantic_contract = _resolve_execution_semantic_contract(
        strategy_type=self.strategy_type,
        params={**dict(self.params or {}), "target_symbols": list(target_symbols)},
        target_symbols=list(target_symbols),
        trade_plan=trade_plan,
        holding_horizon=holding_horizon,
        risk_rules=risk_rules,
        position_sizing=position_sizing,
        stock_pool=stock_pool,
        prediction_contract=prediction_contract,
        instrument_profile=instrument_profile,
        explicit_dsl=explicit_dsl,
        existing_claim_to_trade_plan_map=claim_to_trade_plan_map,
        existing_trade_plan_to_dsl_map=trade_plan_to_dsl_map,
        existing_dsl_support_audit=dsl_support_audit,
    )
    if execution_semantic_contract.get("dsl_support_audit"):
        dsl_support_audit = dict(execution_semantic_contract.get("dsl_support_audit") or {})
    if execution_semantic_contract.get("claim_to_trade_plan_map"):
        claim_to_trade_plan_map = dict(execution_semantic_contract.get("claim_to_trade_plan_map") or {})
    if execution_semantic_contract.get("trade_plan_to_dsl_map"):
        trade_plan_to_dsl_map = dict(execution_semantic_contract.get("trade_plan_to_dsl_map") or {})
    family_specialization = _default_family_specialization(
        self.strategy_type,
        task_source,
        holding_horizon=holding_horizon,
        rebalance_rule=rebalance_rule,
    )
    family_specialization.update(
        _dict_value(
            metadata.get("family_specialization"),
            source_candidate.get("family_specialization"),
            dict(self.params or {}).get("family_specialization"),
            source_candidate_params.get("family_specialization"),
            hypothesis_artifact.get("family_specific_hypothesis"),
        )
    )
    expected_turnover_band = (
        _normalize_turnover_band(
            holding_horizon.get("expected_turnover_band")
            or rebalance_rule.get("expected_turnover_band")
        )
        or _derive_half_life_semantics(alpha_half_life).get("expected_turnover_band")
    )
    capacity_bucket = _resolve_capacity_bucket(
        dict(capacity_assumption),
        target_symbols=list(target_symbols),
        position_model=str(position_model or ""),
    )
    if not capacity_assumption:
        capacity_assumption = {
            "max_position_pct": portfolio_spec.get("max_position_pct"),
            "symbol_count": len(target_symbols),
            "capacity_bucket": capacity_bucket,
        }
    if capacity_assumption and hypothesis_artifact.get("capacity_assumption") in (None, "", [], {}):
        hypothesis_artifact["capacity_assumption"] = dict(capacity_assumption)
    if not cost_sensitivity_grid:
        cost_sensitivity_grid = {
            "base_case": {
                "commission_rate": execution_assumptions.get("commission_rate"),
                "slippage_bps": execution_assumptions.get("slippage_bps"),
                "tradability_filter": execution_assumptions.get("tradability_filter"),
                "slippage_model": execution_assumptions.get("slippage_model"),
                "market_impact_bps": execution_assumptions.get("market_impact_bps"),
            },
            "source": "strategy_spec_execution_defaults",
        }
    if cost_sensitivity_grid and hypothesis_artifact.get("cost_sensitivity_grid") in (None, "", [], {}):
        hypothesis_artifact["cost_sensitivity_grid"] = dict(cost_sensitivity_grid)
    position_sizing_rationale = _resolve_position_sizing_rationale(
        position_model=str(position_model or ""),
        target_symbols=list(target_symbols),
        capacity_bucket=capacity_bucket,
        expected_turnover_band=expected_turnover_band or "medium",
    )
    position_sizing.setdefault("capacity_bucket", capacity_bucket or None)
    position_sizing.setdefault("expected_turnover_band", expected_turnover_band or None)
    position_sizing.setdefault("position_sizing_rationale", position_sizing_rationale)
    portfolio_spec.setdefault("capacity_bucket", capacity_bucket or None)
    portfolio_spec.setdefault("expected_turnover_band", expected_turnover_band or None)
    portfolio_spec.setdefault("position_sizing_rationale", position_sizing_rationale)
    execution_assumptions.setdefault("capacity_bucket", capacity_bucket or None)
    execution_assumptions.setdefault(
        "turnover_cost_class",
        _resolve_turnover_cost_class(
            execution_assumptions=execution_assumptions,
            expected_turnover_band=expected_turnover_band or "medium",
            capacity_bucket=capacity_bucket,
        ),
    )
    execution_assumptions.setdefault("expected_turnover_band", expected_turnover_band or None)
    trade_plan.setdefault("cooldown_window_days", holding_horizon.get("cooldown_window_days"))
    trade_plan.setdefault("expected_turnover_band", expected_turnover_band or None)
    risk_rules.setdefault("cooldown_window_days", holding_horizon.get("cooldown_window_days"))
    if not runtime_playbook:
        runtime_playbook = _default_runtime_playbook(
            self.strategy_type,
            holding_horizon=holding_horizon,
            trade_plan=trade_plan,
            risk_rules=risk_rules,
            portfolio_spec=portfolio_spec,
            execution_assumptions=execution_assumptions,
            instrument_profile=instrument_profile,
            backtest_metrics=backtest_metrics,
        )
    runtime_playbook = _enrich_runtime_playbook_provenance(
        runtime_playbook,
        strategy_type=self.strategy_type,
        prediction_contract=prediction_contract,
        trade_plan=trade_plan,
        claim_to_trade_plan_map=claim_to_trade_plan_map,
        trade_plan_to_dsl_map=trade_plan_to_dsl_map,
        source_priority={
            "holding_horizon": holding_horizon_source,
            "trade_plan": trade_plan_source,
            "risk_rules": risk_rules_source,
            "execution_assumptions": execution_assumptions_source,
            "runtime_playbook": runtime_playbook_source,
        },
        runtime_playbook_source=runtime_playbook_source,
    )
    regime_filter_contract = _dict_value(
        metadata.get("regime_filter_contract"),
        source_candidate.get("regime_filter_contract"),
        dict(self.params or {}).get("regime_filter_contract"),
        source_candidate_params.get("regime_filter_contract"),
    )
    if not regime_filter_contract:
        regime_filter_contract = _build_regime_filter_contract(
            self.strategy_type,
            market_regime_assumption=market_regime_assumption,
            instrument_profile=instrument_profile,
            runtime_playbook=runtime_playbook,
        )
    drawdown_invalidation_contract = _dict_value(
        metadata.get("drawdown_invalidation_contract"),
        source_candidate.get("drawdown_invalidation_contract"),
        dict(self.params or {}).get("drawdown_invalidation_contract"),
        source_candidate_params.get("drawdown_invalidation_contract"),
    )
    if not drawdown_invalidation_contract:
        drawdown_invalidation_contract = _build_drawdown_invalidation_contract(
            self.strategy_type,
            instrument_profile=instrument_profile,
            runtime_playbook=runtime_playbook,
            target_symbols=list(target_symbols),
        )
    thesis_invalidation_contract = _dict_value(
        metadata.get("thesis_invalidation_contract"),
        source_candidate.get("thesis_invalidation_contract"),
        dict(self.params or {}).get("thesis_invalidation_contract"),
        source_candidate_params.get("thesis_invalidation_contract"),
    )
    if not thesis_invalidation_contract:
        thesis_invalidation_contract = _build_thesis_invalidation_contract(
            self.strategy_type,
            trade_plan=trade_plan,
            runtime_playbook=runtime_playbook,
            instrument_profile=instrument_profile,
            drawdown_invalidation_contract=drawdown_invalidation_contract,
        )
    parameter_coherence_audit = _dict_value(
        metadata.get("parameter_coherence_audit"),
        source_candidate.get("parameter_coherence_audit"),
        dict(self.params or {}).get("parameter_coherence_audit"),
        source_candidate_params.get("parameter_coherence_audit"),
    )
    if not parameter_coherence_audit:
        parameter_coherence_audit = _build_parameter_coherence_audit(
            self.strategy_type,
            holding_horizon=holding_horizon,
            rebalance_rule=rebalance_rule,
            runtime_playbook=runtime_playbook,
            instrument_profile=instrument_profile,
            backtest_metrics=backtest_metrics,
        )
    runtime_semantic_diagnostics = _resolve_runtime_semantic_diagnostics(
        strategy_type=self.strategy_type,
        params={**dict(self.params or {}), "runtime_playbook": runtime_playbook},
        target_symbols=list(target_symbols),
        instrument_profile=instrument_profile,
        runtime_playbook=runtime_playbook,
        evidence_chain=evidence_chain,
        prediction_contract=prediction_contract,
        confidence_contract=confidence_contract,
        execution_semantic_contract=execution_semantic_contract,
    )
    execution_semantic_gap_reasons = list(
        dict.fromkeys(
            [
                *list(execution_semantic_contract.get("execution_semantic_gap_reasons") or []),
                *list(runtime_semantic_diagnostics.get("execution_semantic_gap_reasons") or []),
            ]
        )
    )
    holding_bucket = _classify_holding_bucket(holding_horizon)
    default_research_validation_contract = _default_research_validation_contract_payload(
        strategy_type=self.strategy_type,
        target_symbols=list(target_symbols),
        research_task=research_task,
        validation_profile=validation_profile,
        candidate_family=candidate_family,
        holding_bucket=holding_bucket,
    )
    default_research_sections = {
        field_name: dict(default_research_validation_contract.get(field_name) or {})
        for field_name in (
            "walk_forward_config",
            "baseline_reference",
            "cash_sleeve_policy",
            "cost_sensitivity_grid",
            "capacity_execution",
            "multiple_testing",
            "admission_thresholds",
            "family_holding_bucket",
        )
    }
    derived_capacity_execution_contract = {
        **dict(capacity_assumption or {}),
        "capacity_bucket": capacity_bucket,
        "position_model": position_model,
        "max_position_pct": portfolio_spec.get("max_position_pct"),
        "market_impact_bps": execution_assumptions.get("market_impact_bps"),
        "slippage_bps": execution_assumptions.get("slippage_bps"),
        "commission_rate": execution_assumptions.get("commission_rate"),
        "tradability_filter": execution_assumptions.get("tradability_filter"),
    }
    capacity_execution_contract = _merge_contract_payloads(
        default_research_sections.get("capacity_execution"),
        derived_capacity_execution_contract,
        dict(explicit_research_validation_contract.get("capacity_execution") or {}),
    )
    derived_family_holding_bucket_contract = {
        "family": candidate_family
        or family_specialization.get("family")
        or family_specialization.get("family_id")
        or self.strategy_type,
        "holding_bucket": holding_bucket,
        "expected_turnover_band": expected_turnover_band,
    }
    family_holding_bucket_contract = _merge_contract_payloads(
        default_research_sections.get("family_holding_bucket"),
        dict(explicit_research_validation_contract.get("family_holding_bucket") or {}),
        derived_family_holding_bucket_contract,
    )
    derived_admission_thresholds = _merge_contract_payloads(
        dict(explicit_research_validation_contract.get("admission_thresholds") or {}),
        {"validation_profile": dict(validation_profile or {})},
    )
# --- model_parts/spec_serialization.py ---
    effective_research_sections = {
        "walk_forward_config": _merge_contract_payloads(
            default_research_sections.get("walk_forward_config"),
            dict(explicit_research_validation_contract.get("walk_forward_config") or {}),
        ),
        "baseline_reference": _merge_contract_payloads(
            default_research_sections.get("baseline_reference"),
            dict(explicit_research_validation_contract.get("baseline_reference") or {}),
        ),
        "cash_sleeve_policy": _merge_contract_payloads(
            default_research_sections.get("cash_sleeve_policy"),
            dict(explicit_research_validation_contract.get("cash_sleeve_policy") or {}),
        ),
        "cost_sensitivity_grid": _merge_contract_payloads(
            default_research_sections.get("cost_sensitivity_grid"),
            dict(cost_sensitivity_grid or {}),
            dict(explicit_research_validation_contract.get("cost_sensitivity_grid") or {}),
        ),
        "capacity_execution": dict(capacity_execution_contract or {}),
        "multiple_testing": _merge_contract_payloads(
            default_research_sections.get("multiple_testing"),
            dict(explicit_research_validation_contract.get("multiple_testing") or {}),
        ),
        "admission_thresholds": _merge_contract_payloads(
            default_research_sections.get("admission_thresholds"),
            derived_admission_thresholds,
        ),
        "family_holding_bucket": dict(family_holding_bucket_contract or {}),
    }
    research_field_provenance = {
        "walk_forward_config": normalize_field_provenance_token(
            research_validation_contract_source
            if effective_research_sections["walk_forward_config"]
            and explicit_research_validation_contract.get("walk_forward_config")
            else "derived"
            if effective_research_sections["walk_forward_config"]
            else "missing"
        ),
        "baseline_reference": normalize_field_provenance_token(
            research_validation_contract_source
            if effective_research_sections["baseline_reference"]
            and explicit_research_validation_contract.get("baseline_reference")
            else "derived"
            if effective_research_sections["baseline_reference"]
            else "missing"
        ),
        "cash_sleeve_policy": normalize_field_provenance_token(
            research_validation_contract_source
            if effective_research_sections["cash_sleeve_policy"]
            and explicit_research_validation_contract.get("cash_sleeve_policy")
            else "derived"
            if effective_research_sections["cash_sleeve_policy"]
            else "missing"
        ),
        "cost_sensitivity_grid": normalize_field_provenance_token(
            research_validation_contract_source
            if explicit_research_validation_contract.get("cost_sensitivity_grid")
            else _resolve_nonmissing_field_provenance(cost_sensitivity_grid_source)
            if dict(cost_sensitivity_grid or {})
            else "derived"
            if effective_research_sections["cost_sensitivity_grid"]
            else "missing"
        ),
        "capacity_execution": normalize_field_provenance_token(
            research_validation_contract_source
            if explicit_research_validation_contract.get("capacity_execution")
            else _resolve_nonmissing_field_provenance(capacity_assumption_source)
            if dict(capacity_assumption or {})
            else "derived"
            if effective_research_sections["capacity_execution"]
            else "missing"
        ),
        "multiple_testing": normalize_field_provenance_token(
            research_validation_contract_source
            if effective_research_sections["multiple_testing"]
            and explicit_research_validation_contract.get("multiple_testing")
            else "derived"
            if effective_research_sections["multiple_testing"]
            else "missing"
        ),
        "admission_thresholds": normalize_field_provenance_token(
            research_validation_contract_source
            if explicit_research_validation_contract.get("admission_thresholds")
            else "derived"
            if effective_research_sections["admission_thresholds"]
            else "missing"
        ),
        "family_holding_bucket": normalize_field_provenance_token(
            research_validation_contract_source
            if explicit_research_validation_contract.get("family_holding_bucket")
            else "derived"
            if effective_research_sections["family_holding_bucket"]
            else "missing"
        ),
    }
    recommended_defaults = {
        field_name: dict(default_research_validation_contract.get(field_name) or {})
        for field_name in effective_research_sections
        if not effective_research_sections.get(field_name)
        and dict(default_research_validation_contract.get(field_name) or {})
    }
    research_contract_hard_failures: list[dict[str, Any]] = []
    for field_name in list(runtime_semantic_diagnostics.get("semantic_contract_missing_fields") or []):
        token = str(field_name or "").strip()
        if token:
            research_contract_hard_failures.append(
                {
                    "field": token,
                    "issue": "semantic_contract_missing_field",
                    "reason_code": f"semantic_contract_missing:{token}",
                    "detail": "runtime semantic contract is not executable without this field",
                }
            )
    for reason_code in execution_semantic_gap_reasons:
        token = str(reason_code or "").strip()
        if token:
            research_contract_hard_failures.append(
                {
                    "issue": "execution_semantic_gap",
                    "reason_code": token,
                }
            )
    research_validation_contract = build_research_validation_contract(
        walk_forward_config=effective_research_sections.get("walk_forward_config"),
        baseline_reference=effective_research_sections.get("baseline_reference"),
        cash_sleeve_policy=effective_research_sections.get("cash_sleeve_policy"),
        cost_sensitivity_grid=effective_research_sections.get("cost_sensitivity_grid"),
        capacity_execution=effective_research_sections.get("capacity_execution"),
        multiple_testing=effective_research_sections.get("multiple_testing"),
        admission_thresholds=effective_research_sections.get("admission_thresholds"),
        family_holding_bucket=effective_research_sections.get("family_holding_bucket"),
        field_provenance=research_field_provenance,
        recommended_defaults=recommended_defaults,
        hard_failures=research_contract_hard_failures,
    )
    research_protocol_version = str(
        research_validation_contract.get("contract_version")
        or default_research_validation_contract.get("contract_version")
        or "strategy_factory.research_protocol.v2"
    ).strip() or "strategy_factory.research_protocol.v2"
    candidate_contract_version = CANDIDATE_CONTRACT_V2
    field_provenance = dict(research_validation_contract.get("field_provenance") or {})
    field_provenance_summary = dict(research_validation_contract.get("field_provenance_summary") or {})
    spec_completeness = str(research_validation_contract.get("spec_completeness") or "complete").strip() or "complete"
    completion_issues = list(research_validation_contract.get("completion_issues") or [])
    research_hard_failures = list(research_validation_contract.get("hard_failures") or [])
    candidate_params = {
        **dict(self.params or {}),
        "target_symbols": list(target_symbols),
        "stock_pool": dict(stock_pool),
        "research_task": dict(research_task),
        "event_context": dict(event_context),
        "holding_horizon": dict(holding_horizon),
        "trade_plan": dict(trade_plan),
        "risk_rules": dict(risk_rules),
        "position_sizing": dict(position_sizing),
        "rebalance_rule": dict(rebalance_rule),
        "portfolio_spec": dict(portfolio_spec),
        "execution_assumptions": dict(execution_assumptions),
        "runtime_playbook": dict(runtime_playbook),
        "validation_profile": dict(validation_profile),
        "event_prefilter": dict(event_prefilter),
        "targeting_policy": dict(targeting_policy),
        "constraint_check": dict(constraint_check),
        "hypothesis_artifact": dict(hypothesis_artifact),
        "holding_rationale": holding_rationale,
        "alpha_half_life": alpha_half_life,
        "cost_sensitivity_grid": dict(cost_sensitivity_grid),
        "position_model": position_model,
        "capacity_assumption": dict(capacity_assumption),
        "market_regime_assumption": market_regime_assumption,
        "instrument_profile": dict(instrument_profile),
        "regime_filter_contract": dict(regime_filter_contract),
        "parameter_coherence_audit": dict(parameter_coherence_audit),
        "thesis_invalidation_contract": dict(thesis_invalidation_contract),
        "drawdown_invalidation_contract": dict(drawdown_invalidation_contract),
        "position_sizing_rationale": position_sizing_rationale,
        "capacity_bucket": capacity_bucket,
        "turnover_cost_class": execution_assumptions.get("turnover_cost_class"),
        "expected_turnover_band": expected_turnover_band,
        "family_specialization": dict(family_specialization),
        "execution_semantic_mode": execution_semantic_contract.get("execution_semantic_mode"),
        "execution_semantic_gap": bool(execution_semantic_contract.get("execution_semantic_gap") or execution_semantic_gap_reasons),
        "execution_semantic_gap_reasons": execution_semantic_gap_reasons,
        "dsl_required": bool(execution_semantic_contract.get("dsl_required")),
        "dsl_compiled": bool(execution_semantic_contract.get("dsl_compiled")),
        "dsl_compile_failure_reasons": list(execution_semantic_contract.get("dsl_compile_failure_reasons") or []),
        "semantic_runtime_match": bool(runtime_semantic_diagnostics.get("semantic_runtime_match")),
        "runtime_family_data_source": runtime_semantic_diagnostics.get("runtime_family_data_source"),
        "proxy_runtime_used": bool(runtime_semantic_diagnostics.get("proxy_runtime_used")),
        "diagnostic_only": bool(runtime_semantic_diagnostics.get("diagnostic_only")),
        "execution_readiness_tier": runtime_semantic_diagnostics.get("execution_readiness_tier"),
        "semantic_contract_missing_fields": list(runtime_semantic_diagnostics.get("semantic_contract_missing_fields") or []),
        "economic_semantics_score": _scalar_value(
            metadata.get("economic_semantics_score"),
            source_candidate.get("economic_semantics_score"),
            hypothesis_artifact.get("economic_semantics_score"),
        ),
        "economic_semantics_missing_fields": _list_value(
            metadata.get("economic_semantics_missing_fields"),
            source_candidate.get("economic_semantics_missing_fields"),
            hypothesis_artifact.get("economic_semantics_missing_fields"),
        ),
        "validation_focus": _scalar_value(
            metadata.get("validation_focus"),
            source_candidate.get("validation_focus"),
            hypothesis_artifact.get("validation_focus"),
            validation_profile.get("validation_focus"),
        ),
        "candidate_family": candidate_family,
        "candidate_family_id": candidate_family_id,
        "candidate_origin": candidate_origin,
        "event_anchor": dict(event_anchor),
        "target_pool_source": target_pool_source,
        "prediction_trace_id": prediction_trace_id,
        "trace_id": prediction_trace_id,
        "research_validation_contract": dict(research_validation_contract),
        "research_protocol_version": research_protocol_version,
        "candidate_contract_version": candidate_contract_version,
        "field_provenance": dict(field_provenance),
        "field_provenance_summary": dict(field_provenance_summary),
        "spec_completeness": spec_completeness,
        "completion_issues": list(completion_issues),
        "hard_failures": list(research_hard_failures),
    }
    if market_facts:
        candidate_params["market_facts"] = list(market_facts)
    if backtest_metrics:
        candidate_params["backtest_metrics"] = dict(backtest_metrics)
    if source_symbol_summary:
        candidate_params["source_symbol_summary"] = dict(source_symbol_summary)
    if execution_semantic_contract.get("dsl"):
        candidate_params["dsl"] = dict(execution_semantic_contract.get("dsl") or {})
    for field_name, field_value in (
        ("evidence_chain", evidence_chain),
        ("prediction_contract", prediction_contract),
        ("confidence_contract", confidence_contract),
        ("evidence_alignment_audit", evidence_alignment_audit),
        ("dsl_support_audit", dsl_support_audit),
        ("claim_to_trade_plan_map", claim_to_trade_plan_map),
        ("trade_plan_to_dsl_map", trade_plan_to_dsl_map),
    ):
        if field_value:
            candidate_params[field_name] = dict(field_value)
    for field_name in ("legacy_semantic_contract", "contradiction_count", "proxy_dependency_score"):
        field_value = _scalar_value(
            metadata.get(field_name),
            source_candidate.get(field_name),
            dict(self.params or {}).get(field_name),
            source_candidate_params.get(field_name),
        )
        if field_value not in (None, "", [], {}):
            candidate_params[field_name] = field_value
    candidate_payload = {
        'name': self.name or str(source_candidate.get('name') or ''),
        'description': self.description or str(source_candidate.get('description') or ''),
        'strategy_type': self.strategy_type,
        'params': candidate_params,
        'spawn_reason': self.description or self.name or f'{source}:{self.strategy_type}',
        'hypothesis': _scalar_value(metadata.get('hypothesis'), source_candidate.get('hypothesis')),
        'holding_horizon': dict(holding_horizon),
        'trade_plan': dict(trade_plan),
        'risk_rules': dict(risk_rules),
        'position_sizing': dict(position_sizing),
        'execution_notes': _scalar_value(metadata.get('execution_notes'), source_candidate.get('execution_notes')),
        'rebalance_rule': dict(rebalance_rule),
        'portfolio_spec': dict(portfolio_spec),
        'execution_assumptions': dict(execution_assumptions),
        'runtime_playbook': dict(runtime_playbook),
        'validation_profile': dict(validation_profile),
        'event_prefilter': dict(event_prefilter),
        'targeting_policy': dict(targeting_policy),
        'constraint_check': dict(constraint_check),
        'hypothesis_artifact': dict(hypothesis_artifact),
        'hypothesis_artifact_id': _scalar_value(
            metadata.get('hypothesis_artifact_id'),
            source_candidate.get('hypothesis_artifact_id'),
            hypothesis_artifact.get('artifact_id'),
        ),
        'hypothesis_lowering_audit': _dict_value(
            metadata.get('hypothesis_lowering_audit'),
            source_candidate.get('hypothesis_lowering_audit'),
        ),
        'holding_rationale': holding_rationale,
        'failure_mode': failure_mode,
        'alpha_half_life': alpha_half_life,
        'cost_sensitivity_grid': _dict_value(
            cost_sensitivity_grid,
        ),
        'position_model': position_model,
        'capacity_assumption': dict(capacity_assumption),
        'market_regime_assumption': market_regime_assumption,
        'instrument_profile': dict(instrument_profile),
        'regime_filter_contract': dict(regime_filter_contract),
        'parameter_coherence_audit': dict(parameter_coherence_audit),
        'thesis_invalidation_contract': dict(thesis_invalidation_contract),
        'drawdown_invalidation_contract': dict(drawdown_invalidation_contract),
        'position_sizing_rationale': position_sizing_rationale,
        'capacity_bucket': capacity_bucket,
        'turnover_cost_class': execution_assumptions.get('turnover_cost_class'),
        'expected_turnover_band': expected_turnover_band,
        'family_specialization': dict(family_specialization),
        'execution_semantic_mode': execution_semantic_contract.get('execution_semantic_mode'),
        'execution_semantic_gap': bool(execution_semantic_contract.get('execution_semantic_gap') or execution_semantic_gap_reasons),
        'execution_semantic_gap_reasons': execution_semantic_gap_reasons,
        'dsl_required': bool(execution_semantic_contract.get('dsl_required')),
        'dsl_compiled': bool(execution_semantic_contract.get('dsl_compiled')),
        'dsl_compile_failure_reasons': list(execution_semantic_contract.get('dsl_compile_failure_reasons') or []),
        'semantic_runtime_match': bool(runtime_semantic_diagnostics.get('semantic_runtime_match')),
        'runtime_family_data_source': runtime_semantic_diagnostics.get('runtime_family_data_source'),
        'proxy_runtime_used': bool(runtime_semantic_diagnostics.get('proxy_runtime_used')),
        'diagnostic_only': bool(runtime_semantic_diagnostics.get('diagnostic_only')),
        'execution_readiness_tier': runtime_semantic_diagnostics.get('execution_readiness_tier'),
        'semantic_contract_missing_fields': list(runtime_semantic_diagnostics.get('semantic_contract_missing_fields') or []),
        'economic_semantics_score': _scalar_value(
            metadata.get('economic_semantics_score'),
            source_candidate.get('economic_semantics_score'),
            hypothesis_artifact.get('economic_semantics_score'),
        ),
        'economic_semantics_missing_fields': _list_value(
            metadata.get('economic_semantics_missing_fields'),
            source_candidate.get('economic_semantics_missing_fields'),
            hypothesis_artifact.get('economic_semantics_missing_fields'),
        ),
        'validation_focus': _scalar_value(
            metadata.get('validation_focus'),
            source_candidate.get('validation_focus'),
            hypothesis_artifact.get('validation_focus'),
            validation_profile.get('validation_focus'),
        ),
        'candidate_family': candidate_family,
        'candidate_family_id': candidate_family_id,
        'candidate_origin': candidate_origin,
        'event_anchor': dict(event_anchor),
        'target_pool_source': target_pool_source,
        'generation_reason': _dict_value(metadata.get('generation_reason'), source_candidate.get('generation_reason')),
        'committee_review': _dict_value(metadata.get('committee_review'), source_candidate.get('committee_review')),
        'generator_type': _scalar_value(metadata.get('generator_type'), source_candidate.get('generator_type'), source) or source,
        'optimizer_type': _scalar_value(metadata.get('optimizer_type'), source_candidate.get('optimizer_type')),
        'llm_prompt': _dict_value(metadata.get('llm_prompt'), source_candidate.get('llm_prompt')),
        'llm_response': _dict_value(metadata.get('llm_response'), source_candidate.get('llm_response')),
        'target_symbols': list(target_symbols),
        'stock_pool': dict(stock_pool),
        'selection_logic': list(selection_logic),
        'research_scope': dict(research_scope),
        'research_task': dict(research_task),
        'event_context': dict(event_context),
        'source_symbol_summary': dict(source_symbol_summary),
        'task_run_id': _scalar_value(metadata.get('task_run_id'), source_candidate.get('task_run_id')),
        'parent_strategy_id': _scalar_value(metadata.get('parent_strategy_id'), source_candidate.get('parent_strategy_id')),
        'pipeline_provenance': _dict_value(metadata.get('pipeline_provenance')),
        'experiment_id': experiment_id,
        'prediction_trace_id': prediction_trace_id,
        'trace_id': prediction_trace_id,
        'research_validation_contract': dict(research_validation_contract),
        'research_protocol_version': research_protocol_version,
        'candidate_contract_version': candidate_contract_version,
        'field_provenance': dict(field_provenance),
        'field_provenance_summary': dict(field_provenance_summary),
        'spec_completeness': spec_completeness,
        'completion_issues': list(completion_issues),
        'hard_failures': list(research_hard_failures),
        'tags': list(dict.fromkeys(['ai_generated', source, self.strategy_type, *(self.tags or [])])),
    }
    if backtest_metrics:
        candidate_payload['backtest_metrics'] = dict(backtest_metrics)
    if execution_semantic_contract.get("dsl"):
        candidate_payload["dsl"] = dict(execution_semantic_contract.get("dsl") or {})
    for field_name in (
        "evidence_chain",
        "prediction_contract",
        "confidence_contract",
        "evidence_alignment_audit",
        "dsl_support_audit",
        "claim_to_trade_plan_map",
        "trade_plan_to_dsl_map",
    ):
        if isinstance(candidate_params.get(field_name), dict) and candidate_params.get(field_name):
            candidate_payload[field_name] = dict(candidate_params.get(field_name) or {})
# --- model_parts/part_4.py ---
    for field_name in ("legacy_semantic_contract", "contradiction_count", "proxy_dependency_score"):
        if candidate_params.get(field_name) not in (None, "", [], {}):
            candidate_payload[field_name] = candidate_params.get(field_name)
    strategy_explanation = build_strategy_explanation(
        candidate_payload,
        source=source,
    )
    if strategy_explanation:
        candidate_payload["strategy_explanation"] = strategy_explanation
        candidate_params["strategy_explanation"] = strategy_explanation
    return candidate_payload


@dataclass
class StrategySpec:
    strategy_type: str
    params: dict[str, Any]
    name: str = ''
    description: str = ''
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_candidate(self, source: str, experiment_id: str) -> dict:
        return _strategy_spec_to_candidate(self, source, experiment_id)
