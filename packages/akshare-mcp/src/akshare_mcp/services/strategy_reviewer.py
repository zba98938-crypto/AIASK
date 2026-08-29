"""MultiAgentStrategyReviewer — multi-agent committee review with execution-aware scoring."""

from __future__ import annotations

from typing import Any, Optional

from .strategy_lifecycle_shared import evaluate_confidence_contract
from .strategy_spec import StrategySpec


class MultiAgentStrategyReviewer:
    SUPPORTED_TYPES = {
        'momentum',
        'ma_cross',
        'rsi',
        'value_factor',
        'quality_factor',
        'growth_factor',
        'multi_factor',
        'macro_timing',
        'volatility_breakout',
        'event_structure_breakout',
        'gap_fill',
        'mean_reversion_short',
        'sector_rotation',
        'north_capital_track',
        'margin_divergence',
        'dsl_rule',
    }
    _TREND_EXECUTABLE_DSL_TYPES = {'momentum', 'ma_cross', 'volatility_breakout', 'event_structure_breakout'}

    @staticmethod
    def _factor_research_alignment(spec: StrategySpec, snapshot: dict) -> tuple[float, dict[str, Any]]:
        factor_research = dict(snapshot.get('factor_research') or {})
        preferred_types = [
            str(item).strip()
            for item in list(factor_research.get('preferred_strategy_types') or [])
            if str(item).strip()
        ]
        top_factor_names = [
            str(item).strip()
            for item in list(
                ((factor_research.get('summary') or {}).get('top_factor_names') or factor_research.get('active_factors') or [])
            )
            if str(item).strip()
        ]
        score_delta = 0.0
        if spec.strategy_type in preferred_types[:2]:
            score_delta += 0.16
        elif spec.strategy_type in preferred_types:
            score_delta += 0.1
        if factor_research and bool(factor_research.get('degraded')):
            score_delta -= 0.04
        return max(min(score_delta, 0.2), -0.08), {
            'preferred_strategy_types': preferred_types[:4],
            'top_factor_names': top_factor_names[:3],
            'artifact_degraded': bool(factor_research.get('degraded')),
            'aligned': spec.strategy_type in preferred_types,
        }

    @classmethod
    def _planner_score(cls, spec: StrategySpec, snapshot: dict) -> tuple[float, dict[str, Any]]:
        fg = int(snapshot.get('fear_greed_index') or 50)
        stype = spec.strategy_type
        base_score = 0.6
        if fg >= 60 and stype in {'momentum', 'ma_cross', 'volatility_breakout', 'event_structure_breakout', 'north_capital_track'}:
            base_score = 0.9
        elif fg < 45 and stype in {'rsi', 'value_factor', 'quality_factor', 'gap_fill', 'mean_reversion_short'}:
            base_score = 0.85
        elif stype == 'multi_factor':
            base_score = 0.82
        elif stype == 'macro_timing':
            base_score = 0.78
        elif stype == 'sector_rotation':
            base_score = 0.8
        elif stype == 'margin_divergence':
            base_score = 0.77
        elif stype == 'dsl_rule':
            base_score = 0.74
        factor_delta, factor_context = cls._factor_research_alignment(spec, snapshot)
        return max(0.05, min(1.0, round(base_score + factor_delta, 4))), {
            'fear_greed_index': fg,
            **factor_context,
        }

    @staticmethod
    def _risk_score(spec: StrategySpec) -> float:
        params = dict(spec.params or {})
        penalty = 0.0
        for key, value in params.items():
            if not isinstance(value, (int, float)):
                continue
            lowered = str(key).lower()
            if 'threshold' in lowered and float(value) > 0.05:
                penalty += 0.25
            if 'period' in lowered and float(value) < 3:
                penalty += 0.15
            if 'lookback' in lowered and float(value) < 5:
                penalty += 0.2
        return max(0.05, 1.0 - penalty)

    @staticmethod
    def _feasibility_score(spec: StrategySpec) -> float:
        return 1.0 if spec.strategy_type in MultiAgentStrategyReviewer.SUPPORTED_TYPES else 0.0

    @staticmethod
    def _novelty_score(spec: StrategySpec) -> float:
        tags = set(spec.tags or [])
        if 'external_llm' in tags:
            return 0.62
        if 'rl_evolved' in tags:
            return 0.66
        if 'llm_proxy' in tags or 'llm_proxy_fallback' in tags or 'local_rule_v1' in tags:
            return 0.58
        if 'rule' in tags:
            return 0.56
        return 0.54

    @staticmethod
    def _target_symbols(spec: StrategySpec) -> list[str]:
        metadata = dict(spec.metadata or {})
        payloads = [
            metadata.get('target_symbols'),
            metadata.get('stock_pool'),
            ((metadata.get('research_task') or {}).get('target_symbols') if isinstance(metadata.get('research_task'), dict) else None),
            spec.params.get('target_symbols') if isinstance(spec.params, dict) else None,
            spec.params.get('stock_pool') if isinstance(spec.params, dict) else None,
            (((spec.params.get('dsl') or {}).get('metadata') or {}).get('target_symbols') if isinstance(spec.params, dict) else None),
        ]
        codes: list[str] = []
        seen: set[str] = set()
        for payload in payloads:
            values = payload
            if isinstance(values, dict):
                values = values.get('symbols') or values.get('codes') or values.get('target_symbols')
            if not isinstance(values, (list, tuple, set)):
                values = [values] if values not in (None, '') else []
            for item in values:
                code = str(item or '').split('.')[0].strip()
                if code and code not in seen:
                    seen.add(code)
                    codes.append(code)
        return codes[:12]

    @staticmethod
    def _research_task(spec: StrategySpec) -> dict[str, Any]:
        metadata = dict(spec.metadata or {})
        return dict(metadata.get('research_task') or {})

    @classmethod
    def _execution_score(cls, spec: StrategySpec) -> tuple[float, list[str]]:
        metadata = dict(spec.metadata or {})
        execution = dict(metadata.get('execution_assumptions') or {})
        holding_horizon = dict(metadata.get('holding_horizon') or {})
        dsl = dict((spec.params or {}).get('dsl') or {})
        risk_rules = dict(metadata.get('risk_rules') or dsl.get('risk_rules') or (spec.params or {}).get('risk_rules') or {})
        issues: list[str] = []
        score = 0.62 if spec.strategy_type in cls.SUPPORTED_TYPES else 0.0

        if not holding_horizon:
            issues.append('missing_holding_horizon')
            score -= 0.12
        if not risk_rules:
            issues.append('missing_risk_rules')
            score -= 0.16
        if not execution:
            issues.append('missing_execution_assumptions')
            score -= 0.12
        else:
            if not execution.get('tradability_filter'):
                issues.append('tradability_filter_disabled')
                score -= 0.08
            if float(execution.get('slippage_bps') or 0.0) <= 0:
                issues.append('missing_slippage_assumption')
                score -= 0.06
            if not str(execution.get('slippage_model') or '').strip():
                issues.append('missing_slippage_model')
                score -= 0.05
        params = dict(spec.params or {})
        execution_quality = dict(
            params.get("execution_quality")
            or metadata.get("execution_quality")
            or metadata.get("execution_audit_summary")
            or params.get("execution_audit_summary")
            or {}
        )
        signal_to_order_conversion = float(
            execution_quality.get("signal_to_order_conversion")
            or execution_quality.get("signal_to_fill_ratio")
            or 0.0
        )
        filled_order_ratio = float(execution_quality.get("filled_order_ratio") or 0.0)
        trade_expectancy = execution_quality.get("trade_expectancy")
        pnl_conversion_efficiency = execution_quality.get("pnl_conversion_efficiency")
        execution_conversion_efficiency = execution_quality.get("execution_conversion_efficiency")
        if signal_to_order_conversion:
            if signal_to_order_conversion < 0.20:
                issues.append("signal_to_order_conversion_weak")
                score -= 0.08
        if filled_order_ratio:
            if filled_order_ratio < 0.65:
                issues.append("filled_order_ratio_weak")
                score -= 0.08
        if trade_expectancy is not None and float(trade_expectancy) <= 0:
            issues.append("trade_expectancy_non_positive")
            score -= 0.10
        if pnl_conversion_efficiency is not None and float(pnl_conversion_efficiency) < 0.25:
            issues.append("pnl_conversion_efficiency_weak")
            score -= 0.10
        if execution_conversion_efficiency is not None and float(execution_conversion_efficiency) < 0.20:
            issues.append("execution_conversion_efficiency_weak")
            score -= 0.12
        return max(0.05, min(1.0, round(score, 4))), issues

    @classmethod
    def _capacity_score(cls, spec: StrategySpec) -> tuple[float, list[str]]:
        metadata = dict(spec.metadata or {})
        execution = dict(metadata.get('execution_assumptions') or {})
        portfolio_spec = dict(metadata.get('portfolio_spec') or {})
        target_symbols = cls._target_symbols(spec)
        issues: list[str] = []
        score = 0.64 if spec.strategy_type in cls.SUPPORTED_TYPES else 0.0

        if not portfolio_spec:
            issues.append('missing_portfolio_spec')
            score -= 0.12
        if len(target_symbols) > 8:
            issues.append('target_universe_too_wide')
            score -= 0.08
        if float(execution.get('capacity_participation_rate') or 0.0) > 0.2:
            issues.append('capacity_participation_rate_too_high')
            score -= 0.12
        if float(execution.get('adv_ratio_limit') or 0.0) > 0.25:
            issues.append('adv_ratio_limit_too_high')
            score -= 0.1
        if not str(execution.get('capacity_bucket') or '').strip():
            issues.append('missing_capacity_bucket')
            score -= 0.04
        return max(0.05, min(1.0, round(score, 4))), issues

    @classmethod
    def _task_alignment_score(cls, spec: StrategySpec, snapshot: dict) -> tuple[float, list[str], dict[str, Any]]:
        metadata = dict(spec.metadata or {})
        research_task = cls._research_task(spec)
        preferred_types = [
            str(item).strip().lower()
            for item in list(research_task.get('preferred_strategy_types') or research_task.get('strategy_preferences') or [])
            if str(item).strip()
        ]
        allowed_types = {
            str(item).strip().lower()
            for item in list(research_task.get('allowed_strategy_types') or [])
            if str(item).strip()
        }
        task_symbols = cls._target_symbols(StrategySpec(strategy_type=spec.strategy_type, params=spec.params, metadata={'target_symbols': research_task.get('target_symbols'), 'stock_pool': research_task.get('stock_pool')}))
        candidate_symbols = cls._target_symbols(spec)
        factor_delta, factor_context = cls._factor_research_alignment(spec, snapshot)
        issues: list[str] = []
        score = 0.58 + factor_delta

        if allowed_types and spec.strategy_type not in allowed_types:
            issues.append('outside_allowed_strategy_types')
            score -= 0.25
        elif preferred_types and spec.strategy_type not in preferred_types:
            issues.append('not_in_preferred_strategy_types')
            score -= 0.12

        if task_symbols and candidate_symbols:
            overlap = len(set(task_symbols).intersection(candidate_symbols)) / max(1, len(set(candidate_symbols)))
            if overlap < 0.5:
                issues.append('target_universe_drift')
                score -= 0.14
            elif overlap >= 0.8:
                score += 0.08

        return max(0.05, min(1.0, round(score, 4))), issues, factor_context

    @classmethod
    def _semantic_consistency_score(cls, spec: StrategySpec) -> tuple[float, list[str], dict[str, Any]]:
        params = dict(spec.params or {})
        metadata = dict(spec.metadata or {})
        evidence_chain = dict(params.get("evidence_chain") or metadata.get("evidence_chain") or {})
        prediction_contract = dict(params.get("prediction_contract") or metadata.get("prediction_contract") or {})
        audit = dict(params.get("evidence_alignment_audit") or metadata.get("evidence_alignment_audit") or {})
        dsl_support_audit = dict(params.get("dsl_support_audit") or metadata.get("dsl_support_audit") or {})
        claim_to_trade_plan_map = dict(
            params.get("claim_to_trade_plan_map") or metadata.get("claim_to_trade_plan_map") or {}
        )
        trade_plan_to_dsl_map = dict(
            params.get("trade_plan_to_dsl_map") or metadata.get("trade_plan_to_dsl_map") or {}
        )
        runtime_playbook = dict(params.get("runtime_playbook") or metadata.get("runtime_playbook") or {})
        runtime_playbook_provenance = dict(runtime_playbook.get("_provenance") or {})
        confidence_contract = dict(params.get("confidence_contract") or metadata.get("confidence_contract") or {})
        regime_filter_contract = dict(
            params.get("regime_filter_contract") or metadata.get("regime_filter_contract") or {}
        )
        parameter_coherence_audit = dict(
            params.get("parameter_coherence_audit") or metadata.get("parameter_coherence_audit") or {}
        )
        thesis_invalidation_contract = dict(
            params.get("thesis_invalidation_contract") or metadata.get("thesis_invalidation_contract") or {}
        )
        drawdown_invalidation_contract = dict(
            params.get("drawdown_invalidation_contract") or metadata.get("drawdown_invalidation_contract") or {}
        )
        execution_semantic_mode = str(
            params.get("execution_semantic_mode") or metadata.get("execution_semantic_mode") or ""
        ).strip().lower()
        semantic_runtime_match = (
            bool(params.get("semantic_runtime_match"))
            if params.get("semantic_runtime_match") is not None
            else bool(metadata.get("semantic_runtime_match"))
            if metadata.get("semantic_runtime_match") is not None
            else True
        )
        runtime_family_data_source = str(
            params.get("runtime_family_data_source") or metadata.get("runtime_family_data_source") or ""
        ).strip().lower()
        if not runtime_family_data_source and spec.strategy_type in {"quality_factor", "value_factor", "growth_factor"}:
            runtime_family_data_source = "price_proxy_runtime"
        proxy_runtime_used = bool(params.get("proxy_runtime_used") or metadata.get("proxy_runtime_used"))
        if spec.strategy_type in {"quality_factor", "value_factor", "growth_factor"} and runtime_family_data_source != "fundamental_runtime":
            proxy_runtime_used = True
        diagnostic_only = bool(params.get("diagnostic_only") or metadata.get("diagnostic_only"))
        execution_readiness_tier = str(
            params.get("execution_readiness_tier") or metadata.get("execution_readiness_tier") or ""
        ).strip().lower()
        semantic_contract_missing_fields = [
            str(item).strip()
            for item in list(params.get("semantic_contract_missing_fields") or metadata.get("semantic_contract_missing_fields") or [])
            if str(item).strip()
        ]
        instrument_profile = dict(params.get("instrument_profile") or metadata.get("instrument_profile") or {})
        measurement_source = str(
            instrument_profile.get("measurement_source") or "default_board_profile"
        ).strip().lower() or "default_board_profile"
        measured_profile_complete = bool(instrument_profile.get("measured_profile_complete"))
        single_name_trend = spec.strategy_type in cls._TREND_EXECUTABLE_DSL_TYPES and len(cls._target_symbols(spec)) == 1
        execution_semantic_gap = bool(
            params.get("execution_semantic_gap") or metadata.get("execution_semantic_gap")
        )
        dsl_required = bool(params.get("dsl_required") or metadata.get("dsl_required"))
        if not dsl_required:
            dsl_required = spec.strategy_type in cls._TREND_EXECUTABLE_DSL_TYPES and len(cls._target_symbols(spec)) == 1
        dsl_compiled = bool(params.get("dsl_compiled") or metadata.get("dsl_compiled") or params.get("dsl"))
        confidence_status, confidence_diagnostics = evaluate_confidence_contract(confidence_contract)
        evidence_alignment_score = float(audit.get("evidence_alignment_score") or 0.0)
        semantic_integrity_score = float(
            audit.get("semantic_integrity_score")
            if audit.get("semantic_integrity_score") is not None
            else evidence_alignment_score
        )
        unsupported_rule_count = int(
            audit.get("unsupported_rule_count")
            or dsl_support_audit.get("unsupported_rule_count")
            or 0
        )
        proxy_dependency_score = float(
            audit.get("proxy_dependency_score")
            or params.get("proxy_dependency_score")
            or 0.0
        )
        compile_stable_missing_fields: list[str] = []
        if spec.strategy_type in cls._TREND_EXECUTABLE_DSL_TYPES | {"quality_factor", "value_factor", "growth_factor"}:
            if not evidence_chain:
                compile_stable_missing_fields.append("evidence_chain")
            if not prediction_contract:
                compile_stable_missing_fields.append("prediction_contract")
            if not confidence_contract:
                compile_stable_missing_fields.append("confidence_contract")
        if spec.strategy_type == "dsl_rule" or audit or dsl_support_audit or confidence_contract:
            if not audit:
                compile_stable_missing_fields.append("evidence_alignment_audit")
            if not dsl_support_audit:
                compile_stable_missing_fields.append("dsl_support_audit")
            if not claim_to_trade_plan_map or not isinstance(
                claim_to_trade_plan_map.get("claim_to_trade_step_ids"),
                dict,
            ):
                compile_stable_missing_fields.append("claim_to_trade_plan_map")
            if not trade_plan_to_dsl_map or not isinstance(
                trade_plan_to_dsl_map.get("trade_step_to_dsl_sections"),
                dict,
            ):
                compile_stable_missing_fields.append("trade_plan_to_dsl_map")
            if not runtime_playbook:
                compile_stable_missing_fields.append("runtime_playbook")
            else:
                source_claim_ids = list(
                    runtime_playbook.get("source_claim_ids")
                    or runtime_playbook_provenance.get("source_claim_ids")
                    or []
                )
                source_trade_step_ids = list(
                    runtime_playbook.get("source_trade_step_ids")
                    or runtime_playbook_provenance.get("source_trade_step_ids")
                    or []
                )
                derivation_labels = list(
                    runtime_playbook.get("derivation_labels")
                    or runtime_playbook_provenance.get("derivation_labels")
                    or []
                )
                derived_from_defaults = runtime_playbook.get("derived_from_defaults")
                if derived_from_defaults is None:
                    derived_from_defaults = runtime_playbook_provenance.get("derived_from_defaults")
                if not source_claim_ids:
                    compile_stable_missing_fields.append("runtime_playbook.source_claim_ids")
                if not source_trade_step_ids:
                    compile_stable_missing_fields.append("runtime_playbook.source_trade_step_ids")
                if derived_from_defaults is None or not derivation_labels:
                    compile_stable_missing_fields.append("runtime_playbook.derivation_labels")
            if spec.strategy_type in cls._TREND_EXECUTABLE_DSL_TYPES and not regime_filter_contract:
                compile_stable_missing_fields.append("regime_filter_contract")
            if spec.strategy_type in cls._TREND_EXECUTABLE_DSL_TYPES and not parameter_coherence_audit:
                compile_stable_missing_fields.append("parameter_coherence_audit")
            if spec.strategy_type in cls._TREND_EXECUTABLE_DSL_TYPES and not thesis_invalidation_contract:
                compile_stable_missing_fields.append("thesis_invalidation_contract")
            if spec.strategy_type in cls._TREND_EXECUTABLE_DSL_TYPES and not drawdown_invalidation_contract:
                compile_stable_missing_fields.append("drawdown_invalidation_contract")
        if dsl_required and not dsl_compiled:
            compile_stable_missing_fields.append("compiled_dsl")
        if dsl_required and execution_semantic_mode != "compiled_dsl":
            compile_stable_missing_fields.append("execution_semantic_mode")
        proxy_only_event_claim_count = int(audit.get("proxy_only_event_claim_count") or 0)
        hard_fail_reasons = [
            str(item).strip()
            for item in list(audit.get("hard_fail_reasons") or [])
            if str(item).strip()
        ]
        if semantic_contract_missing_fields or any(
            field_name in {"evidence_chain", "prediction_contract", "confidence_contract"}
            for field_name in compile_stable_missing_fields
        ):
            hard_fail_reasons.append("final_strategy_missing_semantic_contract")
        if proxy_runtime_used or (
            spec.strategy_type in {"quality_factor", "value_factor", "growth_factor"}
            and runtime_family_data_source != "fundamental_runtime"
        ):
            hard_fail_reasons.extend(
                [
                    "runtime_family_semantic_mismatch",
                    "proxy_runtime_not_allowed_for_formal_incubation",
                ]
            )
        if single_name_trend and (measurement_source == "default_board_profile" or not measured_profile_complete):
            hard_fail_reasons.append("default_profile_not_allowed_for_single_name_runtime")
        if not semantic_runtime_match:
            hard_fail_reasons.append("runtime_family_semantic_mismatch")
        hard_fail_reasons = list(dict.fromkeys(hard_fail_reasons))
        issues: list[str] = []
        if evidence_alignment_score < 0.75:
            issues.append("evidence_hypothesis_trade_plan_dsl_alignment_weak")
        if semantic_integrity_score < 0.75:
            issues.append("semantic_integrity_score_below_threshold")
        if confidence_status == "missing":
            issues.append("confidence_contract_missing")
        elif confidence_status == "insufficient":
            issues.append("confidence_contract_support_insufficient")
        if execution_semantic_gap:
            issues.append("execution_semantic_gap")
        for missing_field in compile_stable_missing_fields:
            issues.append(f"compile_stable_field_missing:{missing_field}")
        if unsupported_rule_count > 0:
            issues.append("dsl_contains_unsupported_rules")
        if proxy_only_event_claim_count > 0:
            issues.append("proxy_only_event_evidence_not_allowed")
        coherence_status = str(parameter_coherence_audit.get("status") or "").strip().lower()
        coherence_blockers = [
            str(item).strip()
            for item in list(parameter_coherence_audit.get("blockers") or [])
            if str(item).strip()
        ]
        if coherence_status == "failed" or coherence_blockers:
            issues.append("parameter_coherence_audit_failed")
        if spec.strategy_type in cls._TREND_EXECUTABLE_DSL_TYPES and not regime_filter_contract.get("quantified"):
            issues.append("regime_filter_contract_not_quantified")
        if spec.strategy_type in cls._TREND_EXECUTABLE_DSL_TYPES and not drawdown_invalidation_contract.get("apply_as_hard_gate"):
            issues.append("drawdown_invalidation_contract_not_hard_gated")
        if proxy_runtime_used:
            issues.append("proxy_runtime_used")
        if diagnostic_only:
            issues.append("diagnostic_only")
        if execution_readiness_tier and execution_readiness_tier != "formal_runtime_ready":
            issues.append(f"execution_readiness_tier:{execution_readiness_tier}")

        score = evidence_alignment_score * 0.55 + semantic_integrity_score * 0.45
        if confidence_status == "comparable_ready":
            score += 0.12
        elif confidence_status == "diagnostic_ready":
            score += 0.06
        elif confidence_status == "insufficient":
            score -= 0.08
        elif confidence_status == "missing":
            score -= 0.16
        score -= min(0.45, unsupported_rule_count * 0.18)
        score -= min(0.25, proxy_dependency_score * 0.25)
        score -= min(0.25, proxy_only_event_claim_count * 0.20)
        score -= min(0.30, len(hard_fail_reasons) * 0.10)
        score -= min(0.42, len(compile_stable_missing_fields) * 0.08)
        score -= min(0.20, len(coherence_blockers) * 0.08)
        if proxy_runtime_used:
            score -= 0.18
        if diagnostic_only:
            score -= 0.12
        return max(0.05, min(1.0, round(score, 4))), issues, {
            "confidence_contract_status": confidence_status,
            "confidence_diagnostics": confidence_diagnostics,
            "hard_fail_reasons": hard_fail_reasons,
            "unsupported_rule_count": unsupported_rule_count,
            "proxy_dependency_score": round(proxy_dependency_score, 4),
            "proxy_only_event_claim_count": proxy_only_event_claim_count,
            "evidence_alignment_score": round(evidence_alignment_score, 4),
            "semantic_integrity_score": round(semantic_integrity_score, 4),
            "compile_stable_missing_fields": compile_stable_missing_fields,
            "runtime_playbook_provenance": runtime_playbook_provenance,
            "execution_semantic_mode": execution_semantic_mode or None,
            "execution_semantic_gap": execution_semantic_gap,
            "semantic_runtime_match": semantic_runtime_match,
            "runtime_family_data_source": runtime_family_data_source or None,
            "proxy_runtime_used": proxy_runtime_used,
            "diagnostic_only": diagnostic_only,
            "execution_readiness_tier": execution_readiness_tier or None,
            "semantic_contract_missing_fields": semantic_contract_missing_fields,
            "measurement_source": measurement_source,
            "measured_profile_complete": measured_profile_complete,
            "dsl_required": dsl_required,
            "dsl_compiled": dsl_compiled,
            "regime_filter_contract": regime_filter_contract,
            "parameter_coherence_audit": parameter_coherence_audit,
            "thesis_invalidation_contract": thesis_invalidation_contract,
            "drawdown_invalidation_contract": drawdown_invalidation_contract,
        }

    @staticmethod
    def _revise_params(params: dict[str, Any]) -> dict[str, Any]:
        revised: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if isinstance(value, bool):
                revised[key] = value
                continue
            if isinstance(value, int):
                lowered = str(key).lower()
                if 'period' in lowered or 'lookback' in lowered:
                    revised[key] = max(3, min(value, 120))
                else:
                    revised[key] = value
                continue
            if isinstance(value, float):
                lowered = str(key).lower()
                if 'threshold' in lowered:
                    revised[key] = round(min(max(value, 0.003), 0.03), 6)
                else:
                    revised[key] = round(value, 6)
                continue
            revised[key] = value
        return revised

    def review(self, spec: StrategySpec, snapshot: dict) -> tuple[Optional[StrategySpec], dict[str, Any]]:
        planner, planner_context = self._planner_score(spec, snapshot)
        risk = self._risk_score(spec)
        feasibility = self._feasibility_score(spec)
        execution, execution_issues = self._execution_score(spec)
        capacity, capacity_issues = self._capacity_score(spec)
        task_alignment, alignment_issues, alignment_context = self._task_alignment_score(spec, snapshot)
        novelty = self._novelty_score(spec)
        semantic_consistency, semantic_issues, semantic_context = self._semantic_consistency_score(spec)
        final_score = round(
            semantic_consistency * 0.32
            + planner * 0.18
            + risk * 0.16
            + feasibility * 0.14
            + execution * 0.10
            + capacity * 0.06
            + task_alignment * 0.03
            + novelty * 0.01,
            4,
        )
        execution_floor_failed = execution < 0.5
        capacity_floor_failed = capacity < 0.5
        alignment_floor_failed = task_alignment < 0.45
        accept_blockers: list[str] = []
        if execution_floor_failed:
            accept_blockers.append('execution_floor_failed')
        if capacity_floor_failed:
            accept_blockers.append('capacity_floor_failed')
        if alignment_floor_failed:
            accept_blockers.append('task_alignment_floor_failed')
        hard_fail_reasons = list(semantic_context.get("hard_fail_reasons") or [])
        unsupported_rule_count = int(semantic_context.get("unsupported_rule_count") or 0)
        proxy_only_event_claim_count = int(semantic_context.get("proxy_only_event_claim_count") or 0)
        confidence_contract_status = str(semantic_context.get("confidence_contract_status") or "missing")
        compile_stable_missing_fields = list(semantic_context.get("compile_stable_missing_fields") or [])
        direct_reject = bool(hard_fail_reasons or unsupported_rule_count > 0 or proxy_only_event_claim_count > 0)
        if compile_stable_missing_fields:
            accept_blockers.append('compile_stable_contract_missing')
        if bool(semantic_context.get("execution_semantic_gap")):
            accept_blockers.append('execution_semantic_gap')
        parameter_coherence_audit = dict(semantic_context.get("parameter_coherence_audit") or {})
        if str(parameter_coherence_audit.get("status") or "").strip().lower() == "failed":
            accept_blockers.append("parameter_coherence_audit_failed")
        if "execution_conversion_efficiency_weak" in execution_issues:
            accept_blockers.append("execution_conversion_floor_failed")
        if direct_reject:
            decision = 'reject'
        elif semantic_consistency >= 0.75 and final_score >= 0.68 and feasibility > 0 and not accept_blockers:
            decision = 'accept'
        elif feasibility > 0 and (semantic_consistency < 0.75 or confidence_contract_status == "missing"):
            decision = 'revise'
        elif final_score >= 0.45 and feasibility > 0:
            decision = 'revise'
        else:
            decision = 'reject'
        suggestions: list[str] = []
        if semantic_issues:
            suggestions.append('先修正 evidence -> hypothesis -> trade_plan -> dsl 的一致性，再进入执行/容量层面的优化。')
        if feasibility <= 0:
            suggestions.append('策略类型未注册，拒绝进入自治工厂。')
        if risk < 0.7:
            suggestions.append('参数存在高风险取值，建议收敛阈值与周期。')
        if planner < 0.65:
            suggestions.append('策略与当前市场环境匹配度一般，建议进入观察或微调。')
        if planner_context.get('preferred_strategy_types') and not planner_context.get('aligned'):
            suggestions.append('策略未对齐当前 factor_research 偏好，建议优先验证因子主链推荐类型。')
        if alignment_issues:
            suggestions.append('策略与研究任务或目标池存在偏离，建议先修正 task alignment。')
        if execution_issues:
            suggestions.append('执行假设仍不完整，建议补齐 holding horizon / risk rules / execution assumptions。')
        if capacity_issues:
            suggestions.append('容量与仓位语义不足，建议补齐 position sizing / capacity 假设。')
        if hard_fail_reasons:
            suggestions.append('语义合同已触发 hard fail，需先消除 claim/evidence/DSL 映射错误。')
        if compile_stable_missing_fields:
            suggestions.append('当前仍缺少稳定编译产物（semantic lineage / playbook provenance），应先补齐后再允许 reviewer 直接 accept。')
        if accept_blockers:
            suggestions.append('评审护栏已阻止直接通过，需先补齐执行、容量或任务对齐基础约束。')

        reviewed = spec
        if decision == 'revise':
            reviewed = StrategySpec(
                strategy_type=spec.strategy_type,
                params=self._revise_params(spec.params),
                name=spec.name,
                description=spec.description,
                tags=list(dict.fromkeys([*(spec.tags or []), 'committee_revised'])),
                metadata=dict(spec.metadata or {}),
            )
        review = {
            'semantic_consistency_score': semantic_consistency,
            'planner_score': planner,
            'risk_score': risk,
            'feasibility_score': feasibility,
            'execution_score': execution,
            'capacity_score': capacity,
            'task_alignment_score': task_alignment,
            'novelty_score': novelty,
            'final_score': final_score,
            'decision': decision,
            'suggestions': suggestions,
            'semantic_context': semantic_context,
            'semantic_issues': semantic_issues,
            'confidence_contract_status': confidence_contract_status,
            'planner_context': planner_context,
            'task_alignment_context': alignment_context,
            'alignment_issues': alignment_issues,
            'execution_issues': execution_issues,
            'capacity_issues': capacity_issues,
            'accept_blockers': accept_blockers,
            'compile_stable_missing_fields': compile_stable_missing_fields,
        }
        if decision == 'reject':
            return None, review
        reviewed.metadata = {
            **dict(reviewed.metadata or {}),
            'committee_review': review,
        }
        return reviewed, review
