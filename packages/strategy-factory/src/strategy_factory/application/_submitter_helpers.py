"""策略工厂候选提交。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time as _time
from collections import Counter
from typing import TYPE_CHECKING, Any, List, Optional
from uuid import uuid4

from .candidate_contract import apply_resolved_candidate_envelope
from .quality_gates import build_completed_gate_3_report
from .quality_reporting import build_quality_report, normalize_quality_gate_result
from .submission_gate import run_submission_quality_gate as _local_run_submission_quality_gate
from .utils import (
    _auto_name as _local_auto_name,
    _extract_event_context as _local_extract_event_context,
    _update_strategy_status as _local_update_strategy_status,
    get_strategy_factory_package as _local_get_strategy_factory_package,
)
from ..domain.constants import (
    FACTORY_SUBMISSION_MIN_BACKTEST_TRADES,
    FACTORY_SUBMISSION_MIN_EVENT_TARGET_COVERAGE,
    FACTORY_SUBMISSION_REJECT_GENERIC_AI_NAMES,
    FACTORY_SUBMISSION_REQUIRE_STRICT_PASS_FOR_REFRESH,
    FACTORY_SUBMISSION_REQUIRE_TASK_PREFERENCE_MATCH,
    SUBMIT_CONCURRENCY,
)
from ..domain.strategy_profile import infer_candidate_strategy_profile
from ..domain.targets import _build_task_signature, _normalize_research_task_contract, _normalize_target_codes
from ..infrastructure.mcp_services import build_strategy_vector_profile, get_factor_pool_gateway

if TYPE_CHECKING:
    from ..api.contracts import FactorPoolGateway, IncubationGateway, RiskGateway, ValidationGateway

logger = logging.getLogger(__name__)

def _compat_setting(name: str, default):
    return default


def _auto_name(*args, **kwargs):
    return _local_auto_name(*args, **kwargs)


def _extract_event_context(*args, **kwargs):
    return _local_extract_event_context(*args, **kwargs)


def get_strategy_factory_package():
    return _local_get_strategy_factory_package()


async def _update_strategy_status(*args, **kwargs):
    return await _local_update_strategy_status(*args, **kwargs)


class _CompatValidationGateway:
    """Resolve validation runner through the legacy patch-point at call time."""

    async def run_validation_report(self, strategy_type: str, params: dict[str, Any], db) -> Optional[dict]:
        from ..api.quality_reporting import run_validation_report

        return await run_validation_report(strategy_type, dict(params or {}), db)


class _CompatRiskGateway:
    """Resolve risk runner through the legacy patch-point at call time."""

    async def run_risk_report(self, strategy_type: str, params: dict[str, Any], db) -> Optional[dict]:
        from ..api.quality_reporting import run_risk_report

        return await run_risk_report(strategy_type, dict(params or {}), db)


class _StrategySubmitterHelpersMixin:
        @staticmethod
        def _get_optional_db_method(db, name: str):
            method = getattr(db, name, None)
            if method is None or not callable(method):
                return None
            if type(method).__module__.startswith("unittest.mock") and not hasattr(method, "await_count"):
                return None
            return method

        @classmethod
        async def _call_optional_db_method(cls, db, name: str, *args, **kwargs):
            method = cls._get_optional_db_method(db, name)
            if method is None:
                return None
            result = method(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        def _get_validation_gateway(self) -> "ValidationGateway":
            if self._validation_gateway is None:
                self._validation_gateway = _CompatValidationGateway()
            return self._validation_gateway

        def _get_risk_gateway(self) -> "RiskGateway":
            if self._risk_gateway is None:
                self._risk_gateway = _CompatRiskGateway()
            return self._risk_gateway

        def _get_incubation_gateway(self) -> "IncubationGateway":
            if self._incubation_gateway is None:
                from ..infrastructure.runtime_adapters import IncubationGatewayImpl

                self._incubation_gateway = IncubationGatewayImpl()
            return self._incubation_gateway

        def _get_factor_pool_gateway(self) -> "FactorPoolGateway":
            if getattr(self, "_factor_pool_gateway", None) is None:
                self._factor_pool_gateway = get_factor_pool_gateway()
            return self._factor_pool_gateway

        @staticmethod
        def _candidate_name(candidate: dict, existing_strategy: Optional[dict] = None) -> str:
            existing_strategy = dict(existing_strategy or {})
            explicit_name = str(candidate.get("name") or "").strip()
            # Always prefer auto_name for descriptive naming (includes target stock + style)
            # LLM-generated names like "AI 多因子平衡 V1" are too generic
            auto = _auto_name(candidate["strategy_type"], candidate["params"])
            if existing_strategy.get("name"):
                return str(existing_strategy["name"])
            if auto and auto != f"{candidate['strategy_type']}策略":
                return auto
            return str(explicit_name or auto)

        @staticmethod
        def _candidate_style_tokens(candidate: dict) -> set[str]:
            payload = dict(candidate or {})
            tags = {str(tag).strip().lower() for tag in list(payload.get("tags") or []) if str(tag).strip()}
            tokens = set()
            strategy_type = str(payload.get("strategy_type") or "").strip().lower()
            if strategy_type:
                tokens.add(strategy_type)
            tokens.update(tag for tag in tags if "_" in tag or tag.isalpha())

            if "trend_following" in tags:
                tokens.update({"momentum", "ma_cross"})
            if "mean_reversion" in tags:
                tokens.update({"rsi", "value_factor"})
            if "defensive" in tags:
                tokens.update({"quality_factor", "value_factor"})

            params = dict(payload.get("params") or {})
            dsl = dict(params.get("dsl") or payload.get("dsl") or {})
            for branch in (dsl.get("entry"), dsl.get("exit")):
                if not isinstance(branch, dict):
                    continue
                for side in ("all", "any", "not"):
                    conditions = branch.get(side)
                    if not isinstance(conditions, list):
                        continue
                    for cond in conditions:
                        left = dict((cond or {}).get("left") or {})
                        right = dict((cond or {}).get("right") or {})
                        indicators = {
                            str(left.get("indicator") or "").strip().lower(),
                            str(right.get("indicator") or "").strip().lower(),
                        }
                        if "rsi" in indicators:
                            tokens.add("rsi")
                        if indicators & {"sma", "ema"}:
                            op = str((cond or {}).get("op") or "").strip().lower()
                            if op in {"cross_above", "cross_below"}:
                                tokens.add("ma_cross")
            return {token for token in tokens if token}

        @classmethod
        def _is_generic_ai_name(cls, name: str, candidate: dict) -> bool:
            strategy_type = str((candidate or {}).get("strategy_type") or "").strip().lower()
            tags = {str(tag).strip().lower() for tag in list((candidate or {}).get("tags") or []) if str(tag).strip()}
            normalized = str(name or "").strip()
            if not normalized:
                return True
            if strategy_type != "dsl_rule" and not ({"external_llm", "ai_generated"} & tags):
                return False
            lowered = normalized.lower()
            if lowered in {strategy_type, f"{strategy_type}策略", "策略", "dsl_rule策略"}:
                return True
            return any(pattern.match(normalized) for pattern in cls._GENERIC_AI_NAME_PATTERNS)

        @staticmethod
        def _candidate_symbols(candidate: dict) -> list[str]:
            payload = dict(candidate or {})
            params = dict(payload.get("params") or {})
            return _normalize_target_codes(
                [
                    payload.get("target_symbols"),
                    payload.get("stock_pool"),
                    params.get("target_symbols"),
                    params.get("stock_pool"),
                    (params.get("dsl") or {}).get("metadata"),
                ],
                limit=16,
            )

        @staticmethod
        def _normalize_optional_float(value: Any) -> Optional[float]:
            try:
                if value is None or value == "":
                    return None
                return float(value)
            except Exception:
                return None

        @staticmethod
        def _normalize_optional_int(value: Any) -> Optional[int]:
            try:
                if value is None or value == "":
                    return None
                return int(float(value))
            except Exception:
                return None

        @staticmethod
        def _normalize_string_list(values: Any, *, limit: int = 8) -> list[str]:
            items: list[str] = []
            seen: set[str] = set()
            for item in list(values or []):
                text = str(item or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                items.append(text)
                if len(items) >= limit:
                    break
            return items

        @staticmethod
        def _normalize_object_dict(value: Any) -> dict[str, Any]:
            return {
                str(key): item
                for key, item in dict(value or {}).items()
                if item not in (None, [], {}, "")
            }

        @classmethod
        def _candidate_report_params(cls, candidate: Optional[dict]) -> dict[str, Any]:
            payload = apply_resolved_candidate_envelope(candidate)
            params = dict(payload.get("params") or {})

            def _assign_list(key: str, *values: Any) -> None:
                for value in values:
                    if isinstance(value, (list, tuple, set)) and value:
                        params[key] = list(value)
                        return

            def _assign_dict(key: str, *values: Any) -> None:
                for value in values:
                    if isinstance(value, dict) and value:
                        params[key] = dict(value)
                        return

            _assign_list(
                "target_symbols",
                payload.get("target_symbols"),
                params.get("target_symbols"),
            )
            _assign_dict(
                "stock_pool",
                payload.get("stock_pool"),
                params.get("stock_pool"),
            )
            _assign_dict(
                "research_task",
                payload.get("research_task"),
                params.get("research_task"),
            )
            _assign_dict(
                "event_context",
                payload.get("event_context"),
                params.get("event_context"),
            )
            _assign_dict(
                "holding_horizon",
                payload.get("holding_horizon"),
                params.get("holding_horizon"),
            )
            _assign_dict(
                "trade_plan",
                payload.get("trade_plan"),
                params.get("trade_plan"),
            )
            _assign_dict(
                "risk_rules",
                payload.get("risk_rules"),
                params.get("risk_rules"),
            )
            _assign_dict(
                "position_sizing",
                payload.get("position_sizing"),
                params.get("position_sizing"),
            )
            _assign_dict(
                "rebalance_rule",
                payload.get("rebalance_rule"),
                params.get("rebalance_rule"),
            )
            _assign_dict(
                "portfolio_spec",
                payload.get("portfolio_spec"),
                params.get("portfolio_spec"),
            )
            _assign_dict(
                "execution_assumptions",
                payload.get("execution_assumptions"),
                params.get("execution_assumptions"),
            )
            _assign_dict(
                "validation_profile",
                payload.get("validation_profile"),
                params.get("validation_profile"),
            )
            _assign_dict(
                "targeting_policy",
                payload.get("targeting_policy"),
                params.get("targeting_policy"),
            )
            _assign_dict(
                "factor_pool_validation_summary",
                payload.get("factor_pool_validation_summary"),
                params.get("factor_pool_validation_summary"),
                dict(payload.get("factor_pool_metadata") or {}).get("validation_summary"),
            )
            _assign_dict(
                "constraint_check",
                payload.get("constraint_check"),
                params.get("constraint_check"),
            )
            return params

        @classmethod
        def _candidate_strategy_profile(
            cls,
            candidate: Optional[dict],
            existing: Optional[dict] = None,
        ) -> dict[str, Any]:
            payload = dict(candidate or {})
            existing_payload = dict(existing or {})
            if not payload and not existing_payload:
                return {}

            params = dict(payload.get("params") or {})
            existing_params = dict(existing_payload.get("params") or {})
            existing_provenance = dict(existing_params.get("candidate_provenance") or {})
            explicit_profile = cls._normalize_object_dict(
                payload.get("strategy_profile")
                or params.get("strategy_profile")
                or existing_provenance.get("strategy_profile")
                or existing_payload.get("strategy_profile")
                or existing_params.get("strategy_profile")
                or {}
            )
            merged_candidate = {
                **existing_payload,
                **payload,
                "params": {**existing_params, **params},
            }
            inferred_profile = cls._normalize_object_dict(
                infer_candidate_strategy_profile(merged_candidate)
            )
            return {**inferred_profile, **explicit_profile}

        @classmethod
        def _candidate_provenance(cls, candidate: Optional[dict], existing: Optional[dict] = None) -> dict[str, Any]:
            payload = dict(candidate or {})
            existing_payload = dict(existing or {})
            params = dict(payload.get("params") or {})
            existing_params = dict(existing_payload.get("params") or {})
            params_provenance = {
                key: params.get(key)
                for key in (
                    "source_candidate_artifact_id",
                    "source_generation_artifact_id",
                    "source_validation_artifact_id",
                    "memory_record_id",
                    "candidate_family",
                    "candidate_name",
                    "candidate_grade",
                    "candidate_registry_stage",
                    "validation_score",
                    "expected_regime",
                    "expected_holding_period",
                    "latest_validation_at",
                    "latest_validation_age_days",
                    "admission_block_reasons",
                    "candidate_evidence_status",
                    "holding_period_bucket",
                    "pool_profile",
                    "volatility_bucket",
                    "liquidity_bucket",
                    "family_mix_constraints",
                    "alpha_source",
                    "risk_level",
                    "regime_fit",
                    "generator_mode",
                    "generator_type",
                    "direction_bias",
                    "candidate_family_id",
                    "validation_profile",
                    "target_symbol_count",
                )
                if params.get(key) not in (None, [], {}, "")
            }
            if (
                "validation_score" not in params_provenance
                and params.get("candidate_validation_score") not in (None, [], {}, "")
            ):
                params_provenance["validation_score"] = params.get("candidate_validation_score")
            if (
                "validation_profile" not in params_provenance
                and params.get("validation_profile_name") not in (None, [], {}, "")
            ):
                params_provenance["validation_profile"] = params.get("validation_profile_name")
            explicit_provenance = dict(
                {
                    **params_provenance,
                    **dict(
                        payload.get("candidate_provenance")
                        or params.get("candidate_provenance")
                        or {}
                    ),
                }
            )
            existing_provenance = {
                **dict(existing_params.get("candidate_provenance") or {}),
                **explicit_provenance,
            }
            strategy_profile = cls._candidate_strategy_profile(payload, existing_payload)
            research_task = _normalize_research_task_contract(
                payload.get("research_task") or existing_params.get("research_task") or {}
            )
            event_context = dict(payload.get("event_context") or {}) or dict(research_task.get("event_context") or {})

            source_candidate_artifact_id = (
                str(
                    payload.get("source_candidate_artifact_id")
                    or research_task.get("source_candidate_artifact_id")
                    or event_context.get("source_candidate_artifact_id")
                    or existing_provenance.get("source_candidate_artifact_id")
                    or ""
                ).strip()
                or None
            )
            candidate_family = (
                str(
                    payload.get("candidate_family")
                    or research_task.get("candidate_family")
                    or strategy_profile.get("strategy_family")
                    or existing_provenance.get("candidate_family")
                    or ""
                ).strip()
                or None
            )
            candidate_name = (
                str(
                    payload.get("candidate_name")
                    or research_task.get("candidate_name")
                    or existing_provenance.get("candidate_name")
                    or payload.get("name")
                    or ""
                ).strip()
                or None
            )
            candidate_grade = (
                str(
                    payload.get("candidate_grade")
                    or research_task.get("candidate_grade")
                    or existing_provenance.get("candidate_grade")
                    or ""
                ).strip()
                or None
            )
            validation_score = cls._normalize_optional_float(
                payload.get("validation_score")
                if payload.get("validation_score") is not None
                else research_task.get("validation_score")
            )
            if validation_score is None:
                validation_score = cls._normalize_optional_float(existing_provenance.get("validation_score"))
            expected_regime = cls._normalize_string_list(
                payload.get("expected_regime")
                or research_task.get("expected_regime")
                or existing_provenance.get("expected_regime")
                or []
            )
            expected_holding_period = cls._normalize_optional_int(
                payload.get("expected_holding_period")
                if payload.get("expected_holding_period") is not None
                else research_task.get("expected_holding_period")
            )
            if expected_holding_period is None:
                expected_holding_period = cls._normalize_optional_int(existing_provenance.get("expected_holding_period"))
            source_generation_artifact_id = (
                str(
                    payload.get("source_generation_artifact_id")
                    or research_task.get("source_generation_artifact_id")
                    or existing_provenance.get("source_generation_artifact_id")
                    or ""
                ).strip()
                or None
            )
            source_validation_artifact_id = (
                str(
                    payload.get("source_validation_artifact_id")
                    or research_task.get("source_validation_artifact_id")
                    or source_candidate_artifact_id
                    or existing_provenance.get("source_validation_artifact_id")
                    or ""
                ).strip()
                or None
            )
            memory_record_id = (
                str(
                    payload.get("memory_record_id")
                    or research_task.get("memory_record_id")
                    or existing_provenance.get("memory_record_id")
                    or ""
                ).strip()
                or None
            )
            candidate_registry_stage = (
                str(
                    payload.get("candidate_registry_stage")
                    or research_task.get("candidate_registry_stage")
                    or existing_provenance.get("candidate_registry_stage")
                    or ""
                ).strip()
                or None
            )
            latest_validation_at = (
                str(
                    payload.get("latest_validation_at")
                    or research_task.get("latest_validation_at")
                    or existing_provenance.get("latest_validation_at")
                    or ""
                ).strip()
                or None
            )
            latest_validation_age_days = cls._normalize_optional_int(
                payload.get("latest_validation_age_days")
                if payload.get("latest_validation_age_days") is not None
                else research_task.get("latest_validation_age_days")
            )
            if latest_validation_age_days is None:
                latest_validation_age_days = cls._normalize_optional_int(existing_provenance.get("latest_validation_age_days"))
            admission_block_reasons = cls._normalize_string_list(
                payload.get("admission_block_reasons")
                or research_task.get("admission_block_reasons")
                or existing_provenance.get("admission_block_reasons")
                or [],
                limit=12,
            )
            candidate_evidence_status = dict(
                payload.get("candidate_evidence_status")
                or research_task.get("candidate_evidence_status")
                or existing_provenance.get("candidate_evidence_status")
                or {}
            )
            holding_period_bucket = (
                str(
                    payload.get("holding_period_bucket")
                    or research_task.get("holding_period_bucket")
                    or strategy_profile.get("holding_period_bucket")
                    or existing_provenance.get("holding_period_bucket")
                    or ""
                ).strip()
                or None
            )
            alpha_source = (
                str(
                    payload.get("alpha_source")
                    or research_task.get("alpha_source")
                    or strategy_profile.get("alpha_source")
                    or existing_provenance.get("alpha_source")
                    or ""
                ).strip()
                or None
            )
            risk_level = (
                str(
                    payload.get("risk_level")
                    or research_task.get("risk_level")
                    or strategy_profile.get("risk_level")
                    or existing_provenance.get("risk_level")
                    or ""
                ).strip()
                or None
            )
            regime_fit = (
                str(
                    payload.get("regime_fit")
                    or research_task.get("regime_fit")
                    or strategy_profile.get("regime_fit")
                    or existing_provenance.get("regime_fit")
                    or ""
                ).strip()
                or None
            )
            generator_mode = (
                str(
                    payload.get("generator_mode")
                    or strategy_profile.get("generator_mode")
                    or existing_provenance.get("generator_mode")
                    or ""
                ).strip()
                or None
            )
            direction_bias = (
                str(
                    payload.get("direction_bias")
                    or strategy_profile.get("direction_bias")
                    or existing_provenance.get("direction_bias")
                    or ""
                ).strip()
                or None
            )
            candidate_family_id = (
                str(
                    payload.get("candidate_family_id")
                    or strategy_profile.get("candidate_family_id")
                    or existing_provenance.get("candidate_family_id")
                    or ""
                ).strip()
                or None
            )
            validation_profile_name = (
                str(
                    payload.get("validation_profile_name")
                    or strategy_profile.get("validation_profile")
                    or existing_provenance.get("validation_profile")
                    or ""
                ).strip()
                or None
            )
            target_symbol_count = cls._normalize_optional_int(
                payload.get("target_symbol_count")
                if payload.get("target_symbol_count") is not None
                else strategy_profile.get("target_symbol_count")
            )
            if target_symbol_count is None:
                target_symbol_count = cls._normalize_optional_int(existing_provenance.get("target_symbol_count"))
            pool_profile = (
                str(
                    payload.get("pool_profile")
                    or research_task.get("pool_profile")
                    or strategy_profile.get("pool_profile")
                    or existing_provenance.get("pool_profile")
                    or ""
                ).strip()
                or None
            )
            volatility_bucket = (
                str(
                    payload.get("volatility_bucket")
                    or research_task.get("volatility_bucket")
                    or strategy_profile.get("volatility_bucket")
                    or existing_provenance.get("volatility_bucket")
                    or ""
                ).strip()
                or None
            )
            liquidity_bucket = (
                str(
                    payload.get("liquidity_bucket")
                    or research_task.get("liquidity_bucket")
                    or strategy_profile.get("liquidity_bucket")
                    or existing_provenance.get("liquidity_bucket")
                    or ""
                ).strip()
                or None
            )
            family_mix_constraints = cls._normalize_object_dict(
                payload.get("family_mix_constraints")
                or research_task.get("family_mix_constraints")
                or existing_provenance.get("family_mix_constraints")
                or {}
            )

            provenance = {
                "source_candidate_artifact_id": source_candidate_artifact_id,
                "source_generation_artifact_id": source_generation_artifact_id,
                "source_validation_artifact_id": source_validation_artifact_id,
                "memory_record_id": memory_record_id,
                "candidate_family": candidate_family,
                "candidate_name": candidate_name,
                "candidate_grade": candidate_grade,
                "candidate_registry_stage": candidate_registry_stage,
                "validation_score": validation_score,
                "expected_regime": expected_regime,
                "expected_holding_period": expected_holding_period,
                "latest_validation_at": latest_validation_at,
                "latest_validation_age_days": latest_validation_age_days,
                "admission_block_reasons": admission_block_reasons,
                "candidate_evidence_status": candidate_evidence_status,
                "strategy_profile": strategy_profile,
                "holding_period_bucket": holding_period_bucket,
                "pool_profile": pool_profile,
                "volatility_bucket": volatility_bucket,
                "liquidity_bucket": liquidity_bucket,
                "family_mix_constraints": family_mix_constraints,
                "alpha_source": alpha_source,
                "risk_level": risk_level,
                "regime_fit": regime_fit,
                "generator_mode": generator_mode,
                "direction_bias": direction_bias,
                "candidate_family_id": candidate_family_id,
                "validation_profile": validation_profile_name,
                "target_symbol_count": target_symbol_count,
                "task_source": str(research_task.get("task_source") or "").strip() or None,
                "task_id": str(research_task.get("task_id") or "").strip() or None,
                "task_key": str(research_task.get("task_key") or "").strip() or None,
                "event_id": str(research_task.get("event_id") or event_context.get("event_id") or "").strip() or None,
                "theme_code": str(research_task.get("theme_code") or event_context.get("theme_code") or "").strip() or None,
            }
            if explicit_provenance:
                provenance.update(
                    {
                        key: value
                        for key, value in explicit_provenance.items()
                        if value not in (None, [], {}, "")
                    }
                )
            return {
                key: value
                for key, value in provenance.items()
                if value not in (None, [], {}, "")
            }
