"""Strategy generators: rule-based and LLM-proxy strategy candidate generation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from copy import deepcopy
from typing import Any, Optional

import pandas as pd

from ..env_loader import load_mcp_env

def _get_strategy_factory_imports():
    from strategy_factory.api import (
        extract_event_context as _extract_event_context,
        preferred_strategy_types_for_factor,
    )
    from strategy_factory.api.constants import (
        CATEGORY_MINIMUMS,
        LLM_FAN_OUT_COUNT,
        PIPELINE_MODE,
        PIPELINE_STAGE_TIMEOUTS,
        PIPELINE_STAGE_TIMEOUT_SEC,
    )
    from strategy_factory.api.semantic_contract import apply_target_symbol_policy, normalize_research_task_contract
    return {
        "CATEGORY_MINIMUMS": CATEGORY_MINIMUMS,
        "LLM_FAN_OUT_COUNT": LLM_FAN_OUT_COUNT,
        "PIPELINE_MODE": PIPELINE_MODE,
        "PIPELINE_STAGE_TIMEOUTS": PIPELINE_STAGE_TIMEOUTS,
        "PIPELINE_STAGE_TIMEOUT_SEC": PIPELINE_STAGE_TIMEOUT_SEC,
        "extract_event_context": _extract_event_context,
        "preferred_strategy_types_for_factor": preferred_strategy_types_for_factor,
        "apply_target_symbol_policy": apply_target_symbol_policy,
        "normalize_research_task_contract": normalize_research_task_contract,
    }


import functools as _functools


@_functools.lru_cache(maxsize=1)
def _sf():
    return _get_strategy_factory_imports()


class _LazyProxy:
    """Module-level proxy to defer strategy_factory imports until first access."""
    def __getattr__(self, name):
        return _sf()[name]

_lazy = _LazyProxy()


def __getattr__(name):
    _map = {
        "CATEGORY_MINIMUMS": "CATEGORY_MINIMUMS",
        "LLM_FAN_OUT_COUNT": "LLM_FAN_OUT_COUNT",
        "PIPELINE_MODE": "PIPELINE_MODE",
        "_extract_event_context": "extract_event_context",
        "preferred_strategy_types_for_factor": "preferred_strategy_types_for_factor",
        "apply_target_symbol_policy": "apply_target_symbol_policy",
        "normalize_research_task_contract": "normalize_research_task_contract",
    }
    if name in _map:
        return _sf()[_map[name]]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


CATEGORY_MINIMUMS = _sf()["CATEGORY_MINIMUMS"]
LLM_FAN_OUT_COUNT = _sf()["LLM_FAN_OUT_COUNT"]
PIPELINE_MODE = _sf()["PIPELINE_MODE"]
_extract_event_context = _sf()["extract_event_context"]
preferred_strategy_types_for_factor = _sf()["preferred_strategy_types_for_factor"]
apply_target_symbol_policy = _sf()["apply_target_symbol_policy"]
normalize_research_task_contract = _sf()["normalize_research_task_contract"]

from .llm_alpha import LLMAlphaMiner
from .data_pipeline import normalize_klines
from .strategy_dsl import compile_strategy_blueprint
from .strategy_llm_provider import StrategyLLMProvider, get_strategy_llm_provider
from .strategy_pipeline import get_strategy_pipeline
from .strategy_spec import (
    DEFAULT_CODES,
    RESEARCH_CANDIDATE_POOL_LIMIT,
    RESEARCH_FINANCIAL_DETAIL_LIMIT,
    RESEARCH_KLINE_SCAN_LIMIT,
    RESEARCH_SYMBOL_DETAIL_LIMIT,
    RESEARCH_UNIVERSE_PAGE_SIZE,
    RESEARCH_UNIVERSE_SCAN_LIMIT,
    StrategySpec,
)

logger = logging.getLogger(__name__)


def _resolve_strategy_generators_imports() -> dict[str, Any]:
    try:
        from . import strategy_generators as public_module

        sf_fn = getattr(public_module, "_sf", _sf)
        return dict(sf_fn() or {})
    except Exception:
        return dict(_sf() or {})


def _rule_template_contract(strategy_type: str) -> dict[str, Any]:
    contracts: dict[str, dict[str, Any]] = {
        'volatility_breakout': {
            'template_generation_profile': 'conservative_breakout',
            'portfolio_spec': {'position_assumption': 'equal_weight_proxy', 'target_weight_scheme': 'equal_weight', 'weight_method': 'volatility_budget', 'max_position_pct': 0.18},
            'risk_rules': {'stop_loss_pct': 0.07, 'take_profit_pct': 0.16, 'max_holding_days': 15, 'max_position_pct': 0.18},
            'validation_profile': {'profile': 'trade_rule_validation', 'validation_focus': 'target_plus_representative', 'primary_validation_layer': 'target'},
            'targeting_policy': {'target_symbol_policy': 'dynamic_signal_universe', 'universe_scope': 'liquid_large_mid', 'universe_expansion_policy': 'trend_leaders_only'},
            'rule_template_contract': {'template_generation_profile': 'conservative_breakout', 'applicable_universe': {'market_cap': 'mid_large', 'liquidity': 'high', 'style_bias': 'trend_expansion'}, 'target_layer': 'target', 'default_risk_constraints': {'stop_loss_pct': 0.07, 'take_profit_pct': 0.16, 'max_holding_days': 15, 'max_position_pct': 0.18}, 'portfolio_weight_method': 'volatility_budget'},
        },
        'event_structure_breakout': {
            'template_generation_profile': 'high_precision_event_structure',
            'portfolio_spec': {'position_assumption': 'equal_weight_proxy', 'target_weight_scheme': 'equal_weight', 'weight_method': 'event_structure_breakout_weight', 'max_position_pct': 0.14},
            'risk_rules': {'stop_loss_pct': 0.06, 'take_profit_pct': 0.16, 'max_holding_days': 12, 'max_position_pct': 0.14},
            'validation_profile': {'profile': 'event_trade_validation', 'validation_focus': 'candidate_target_only', 'primary_validation_layer': 'target', 'objective_profile': 'high_precision', 'trade_density_preference': 'low', 'regime_required': True, 'cost_robust_required': True, 'entry_selectivity': 'strict_event_breakout', 'preferred_regime': 'event_follow_through_with_structure_confirmation', 'avoid_regime': 'false_breakout_or_post_event_mean_reversion'},
            'targeting_policy': {'target_symbol_policy': 'strict_intersection', 'universe_scope': 'liquid_event_breakout_leaders', 'universe_expansion_policy': 'forbid'},
            'rule_template_contract': {'template_generation_profile': 'high_precision_event_structure', 'applicable_universe': {'market_cap': 'mid_large', 'liquidity': 'high', 'style_bias': 'event_structure_follow_through'}, 'target_layer': 'target', 'default_risk_constraints': {'stop_loss_pct': 0.06, 'take_profit_pct': 0.16, 'max_holding_days': 12, 'max_position_pct': 0.14}, 'portfolio_weight_method': 'event_structure_breakout_weight'},
        },
        'gap_fill': {
            'template_generation_profile': 'conservative_mean_reversion',
            'portfolio_spec': {'position_assumption': 'equal_weight_proxy', 'target_weight_scheme': 'equal_weight', 'weight_method': 'repair_equal_weight', 'max_position_pct': 0.14},
            'risk_rules': {'stop_loss_pct': 0.05, 'take_profit_pct': 0.12, 'max_holding_days': 8, 'max_position_pct': 0.14},
            'validation_profile': {'profile': 'trade_rule_validation', 'validation_focus': 'target_plus_representative', 'primary_validation_layer': 'target'},
            'targeting_policy': {'target_symbol_policy': 'dynamic_signal_universe', 'universe_scope': 'liquid_repair_candidates', 'universe_expansion_policy': 'oversold_repair_only'},
            'rule_template_contract': {'template_generation_profile': 'conservative_mean_reversion', 'applicable_universe': {'market_cap': 'all_liquid', 'liquidity': 'medium_high', 'style_bias': 'oversold_repair'}, 'target_layer': 'target', 'default_risk_constraints': {'stop_loss_pct': 0.05, 'take_profit_pct': 0.12, 'max_holding_days': 8, 'max_position_pct': 0.14}, 'portfolio_weight_method': 'repair_equal_weight'},
        },
        'mean_reversion_short': {
            'template_generation_profile': 'conservative_mean_reversion',
            'portfolio_spec': {'position_assumption': 'equal_weight_proxy', 'target_weight_scheme': 'equal_weight', 'weight_method': 'short_horizon_equal_weight', 'max_position_pct': 0.12},
            'risk_rules': {'stop_loss_pct': 0.05, 'take_profit_pct': 0.1, 'max_holding_days': 7, 'max_position_pct': 0.12},
            'validation_profile': {'profile': 'trade_rule_validation', 'validation_focus': 'target_plus_representative', 'primary_validation_layer': 'target'},
            'targeting_policy': {'target_symbol_policy': 'dynamic_signal_universe', 'universe_scope': 'liquid_defensive_reversion', 'universe_expansion_policy': 'short_horizon_only'},
            'rule_template_contract': {'template_generation_profile': 'conservative_mean_reversion', 'applicable_universe': {'market_cap': 'all_liquid', 'liquidity': 'high', 'style_bias': 'defensive_mean_reversion'}, 'target_layer': 'target', 'default_risk_constraints': {'stop_loss_pct': 0.05, 'take_profit_pct': 0.1, 'max_holding_days': 7, 'max_position_pct': 0.12}, 'portfolio_weight_method': 'short_horizon_equal_weight'},
        },
        'sector_rotation': {
            'template_generation_profile': 'conservative_rotation',
            'portfolio_spec': {'position_assumption': 'equal_weight_proxy', 'target_weight_scheme': 'equal_weight', 'weight_method': 'sector_score_tilt', 'max_position_pct': 0.15},
            'risk_rules': {'stop_loss_pct': 0.08, 'take_profit_pct': 0.18, 'max_holding_days': 20, 'max_position_pct': 0.15},
            'validation_profile': {'profile': 'trade_rule_validation', 'validation_focus': 'target_plus_representative', 'primary_validation_layer': 'combined'},
            'targeting_policy': {'target_symbol_policy': 'sector_leader_rotation', 'universe_scope': 'liquid_sector_leaders', 'universe_expansion_policy': 'sector_relative_strength'},
            'rule_template_contract': {'template_generation_profile': 'conservative_rotation', 'applicable_universe': {'market_cap': 'mid_large', 'liquidity': 'high', 'style_bias': 'sector_leadership'}, 'target_layer': 'combined', 'default_risk_constraints': {'stop_loss_pct': 0.08, 'take_profit_pct': 0.18, 'max_holding_days': 20, 'max_position_pct': 0.15}, 'portfolio_weight_method': 'sector_score_tilt'},
        },
        'north_capital_track': {
            'template_generation_profile': 'conservative_flow',
            'portfolio_spec': {'position_assumption': 'equal_weight_proxy', 'target_weight_scheme': 'equal_weight', 'weight_method': 'flow_score_tilt', 'max_position_pct': 0.16},
            'risk_rules': {'stop_loss_pct': 0.07, 'take_profit_pct': 0.16, 'max_holding_days': 12, 'max_position_pct': 0.16},
            'validation_profile': {'profile': 'trade_rule_validation', 'validation_focus': 'target_plus_representative', 'primary_validation_layer': 'combined'},
            'targeting_policy': {'target_symbol_policy': 'northbound_eligible_focus', 'universe_scope': 'northbound_liquid_core', 'universe_expansion_policy': 'flow_leaders_only'},
            'rule_template_contract': {'template_generation_profile': 'conservative_flow', 'applicable_universe': {'northbound_eligible': True, 'liquidity': 'high', 'style_bias': 'capital_flow_leaders'}, 'target_layer': 'combined', 'default_risk_constraints': {'stop_loss_pct': 0.07, 'take_profit_pct': 0.16, 'max_holding_days': 12, 'max_position_pct': 0.16}, 'portfolio_weight_method': 'flow_score_tilt'},
        },
        'margin_divergence': {
            'template_generation_profile': 'conservative_flow',
            'portfolio_spec': {'position_assumption': 'equal_weight_proxy', 'target_weight_scheme': 'equal_weight', 'weight_method': 'divergence_tilt', 'max_position_pct': 0.14},
            'risk_rules': {'stop_loss_pct': 0.06, 'take_profit_pct': 0.14, 'max_holding_days': 10, 'max_position_pct': 0.14},
            'validation_profile': {'profile': 'trade_rule_validation', 'validation_focus': 'target_plus_representative', 'primary_validation_layer': 'target'},
            'targeting_policy': {'target_symbol_policy': 'margin_activity_focus', 'universe_scope': 'liquid_margin_active', 'universe_expansion_policy': 'divergence_repair_only'},
            'rule_template_contract': {'template_generation_profile': 'conservative_flow', 'applicable_universe': {'margin_active': True, 'liquidity': 'high', 'style_bias': 'capital_divergence'}, 'target_layer': 'target', 'default_risk_constraints': {'stop_loss_pct': 0.06, 'take_profit_pct': 0.14, 'max_holding_days': 10, 'max_position_pct': 0.14}, 'portfolio_weight_method': 'divergence_tilt'},
        },
    }
    return deepcopy(contracts.get(str(strategy_type or '').strip().lower()) or {})


class RuleStrategyGenerator:
    @staticmethod
    def _factor_research_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
        factor_research = dict(snapshot.get('factor_research') or {})
        summary = dict(factor_research.get('summary') or {})
        return {
            'top_factor_names': list(summary.get('top_factor_names') or factor_research.get('active_factors') or [])[:3],
            'preferred_strategy_types': [
                str(item).strip()
                for item in list(factor_research.get('preferred_strategy_types') or [])
                if str(item).strip()
            ][:4],
            'degraded': bool(factor_research.get('degraded')),
        }

    @classmethod
    def _build_rule_spec(
        cls,
        strategy_type: str,
        *,
        fg: int,
        regime: str,
        source: str,
        factor_summary: dict[str, Any],
    ) -> Optional[StrategySpec]:
        templates: dict[str, dict[str, Any]] = {
            'momentum': {
                'params': {'lookback': 15, 'threshold': 0.018},
                'name': 'AI 动量强化',
                'description': '高情绪或因子偏强阶段偏向动量追随。',
            },
            'ma_cross': {
                'params': {'short_period': 6, 'long_period': 24},
                'name': 'AI 均线趋势',
                'description': '趋势确认阶段用均线结构过滤噪音。',
            },
            'rsi': {
                'params': {
                    'rsi_period': 12,
                    'oversold': 18,
                    'overbought': 64,
                    'regime_filter_enabled': True,
                    'allowed_entry_regimes': ['bear_calm', 'bear_volatile'],
                    'noise_filter_enabled': True,
                    'noise_window': 6,
                    'noise_ceiling': 6.0,
                    'regime_break_threshold': 0.015,
                    'mean_reversion_exit_min_hold_bars': 4,
                    'mean_reversion_exit_buffer': -0.002,
                    'max_hold_bars': 6,
                    'adverse_regime_exit_enabled': True,
                    'adverse_exit_regimes': ['range_volatile'],
                    'adverse_noise_ceiling': 6.0,
                },
                'name': 'AI RSI 反转',
                'description': '仅在超跌后出现止跌修复迹象时参与高精度反转。',
            },
            'value_factor': {
                'params': {'lookback': 60, 'buy_quantile': 0.8, 'sell_quantile': 0.2},
                'name': 'AI 价值回归',
                'description': '估值修复阶段偏向价值/反转组合。',
            },
            'quality_factor': {
                'params': {'lookback': 50, 'buy_quantile': 0.75, 'sell_quantile': 0.25},
                'name': 'AI 质量精选',
                'description': '质量因子占优阶段偏向盈利能力与稳健性筛选。',
            },
            'growth_factor': {
                'params': {'lookback': 40, 'buy_quantile': 0.78, 'sell_quantile': 0.22},
                'name': 'AI 成长加速',
                'description': '成长因子活跃阶段偏向高景气扩张。',
            },
            'multi_factor': {
                'params': {'factor_weights': {'value': 0.4, 'quality': 0.35, 'momentum': 0.25}},
                'name': 'AI 多因子平衡',
                'description': '因子共振时优先使用多因子组合。',
            },
            'macro_timing': {
                'params': {'fear_threshold': 24, 'greed_threshold': 74, 'lookback': 36},
                'name': 'AI 宏观择时',
                'description': '只在恐慌修复与波动稳定共振时参与宏观择时。',
            },
            'volatility_breakout': {
                'params': {'lookback': 20, 'threshold': 0.025},
                'name': 'AI 波动突破',
                'description': '趋势扩张与波动放大阶段偏向波动率突破。',
            },
            'event_structure_breakout': {
                'params': {'breakout_window': 12, 'breakout_buffer_pct': 0.002, 'contraction_window': 5, 'contraction_max_range_ratio': 0.06, 'volume_window': 8, 'breakout_volume_ratio_min': 1.0, 'structure_window': 4, 'structure_close_location_min': 0.62, 'structure_body_return_min': 0.003, 'event_impulse_window': 5, 'event_impulse_threshold': 0.015, 'max_hold_bars': 8, 'breakout_failure_close_buffer': -0.012, 'adverse_volume_ratio_max': 0.85},
                'name': 'AI 事件结构突破',
                'description': '事件催化后缩量整理并放量突破时入场，偏向低频高把握的结构延续。',
            },
            'gap_fill': {
                'params': {'gap_threshold': 0.02, 'rsi_period': 5, 'oversold': 24, 'overbought': 58},
                'name': 'AI 跳空回补',
                'description': '情绪错杀或事件冲击后偏向短线回补机会。',
            },
            'mean_reversion_short': {
                'params': {'rsi_period': 6, 'oversold': 26, 'overbought': 62},
                'name': 'AI 短线回归',
                'description': '震荡与防御环境下偏向短周期均值回归。',
            },
            'sector_rotation': {
                'params': {'lookback': 20, 'factor_weights': {'momentum': 0.45, 'quality': 0.30, 'value': 0.25}},
                'name': 'AI 行业轮动',
                'description': '主题扩散与风格切换阶段偏向行业轮动打分。',
            },
            'north_capital_track': {
                'params': {'lookback': 15, 'threshold': 0.015},
                'name': 'AI 北向跟踪',
                'description': '资金偏好明确时偏向价量共振的北向跟踪。',
            },
            'margin_divergence': {
                'params': {'fear_threshold': 40, 'greed_threshold': 60, 'lookback': 15},
                'name': 'AI 融资背离',
                'description': '价格与量能出现背离时偏向融资分歧修复。',
            },
        }
        template = templates.get(strategy_type)
        if template is None:
            return None
        template_contract = _rule_template_contract(strategy_type)
        metadata = {
            'generator_type': 'rule',
            'generation_reason': {
                'source': source,
                'fg': fg,
                'regime': regime,
                'factor_research': factor_summary,
                'template_generation_profile': template_contract.get('template_generation_profile'),
                'rule_template_contract': dict(template_contract.get('rule_template_contract') or {}),
            },
        }
        for key in ('portfolio_spec', 'risk_rules', 'validation_profile', 'targeting_policy', 'rule_template_contract'):
            value = template_contract.get(key)
            if value:
                metadata[key] = deepcopy(value)
        return StrategySpec(
            strategy_type=strategy_type,
            params=dict(template['params']),
            name=str(template['name']),
            description=str(template['description']),
            tags=['rule', 'factor_research' if source == 'factor_research' else 'fear_greed'],
            metadata=metadata,
        )

    def generate(
        self,
        snapshot: dict,
        limit: int = 2,
        *,
        preferred_types: Optional[list[str]] = None,
    ) -> list[StrategySpec]:
        fg = int(snapshot.get('fear_greed_index') or 50)
        regime = 'greed' if fg >= 60 else ('fear' if fg < 45 else 'neutral')
        factor_summary = self._factor_research_summary(snapshot)
        factor_preferred_types = [
            item for item in factor_summary.get('preferred_strategy_types') or []
            if item in CATEGORY_MINIMUMS
        ]
        requested_types = [
            str(item).strip()
            for item in list(preferred_types or [])
            if str(item).strip() in CATEGORY_MINIMUMS
        ]
        if not factor_preferred_types:
            for factor_name in list(factor_summary.get('top_factor_names') or []):
                for strategy_type in preferred_strategy_types_for_factor(factor_name):
                    if strategy_type in CATEGORY_MINIMUMS and strategy_type not in factor_preferred_types:
                        factor_preferred_types.append(strategy_type)
        regime_defaults = (
            ['momentum', 'volatility_breakout', 'north_capital_track', 'ma_cross', 'quality_factor']
            if regime == 'greed'
            else ['value_factor', 'quality_factor', 'mean_reversion_short', 'gap_fill', 'rsi']
        )
        preferred_anchor = requested_types or factor_preferred_types
        strategy_order = list(dict.fromkeys([*requested_types, *factor_preferred_types, *regime_defaults]))
        specs: list[StrategySpec] = []
        for index, strategy_type in enumerate(strategy_order):
            source = 'factor_research' if index < len(preferred_anchor) and preferred_anchor else 'fear_greed'
            spec = self._build_rule_spec(
                strategy_type,
                fg=fg,
                regime=regime,
                source=source,
                factor_summary=factor_summary,
            )
            if spec is not None:
                specs.append(spec)
        return specs[: max(1, min(int(limit or 2), 10))]


class _LLMProxyStrategyGeneratorExternalMixin:
        @staticmethod
        def _dedupe_specs(specs: list[StrategySpec]) -> list[StrategySpec]:
            unique: list[StrategySpec] = []
            seen: set[tuple[str, str]] = set()
            for spec in list(specs or []):
                key = (
                    str(spec.strategy_type or ''),
                    json.dumps(spec.params or {}, sort_keys=True, ensure_ascii=False, default=str),
                )
                if key in seen:
                    continue
                seen.add(key)
                unique.append(spec)
            return unique

        @staticmethod
        def _candidate_payload_looks_normalized(candidate: Optional[dict[str, Any]]) -> bool:
            payload = dict(candidate or {})
            if not payload:
                return False
            if bool(payload.get("_legacy_contract_defaults_applied")):
                return True
            if isinstance(payload.get("params"), dict) and payload.get("params"):
                return True
            required_contract_keys = (
                "portfolio_spec",
                "execution_assumptions",
                "validation_profile",
            )
            if all(isinstance(payload.get(key), dict) and payload.get(key) for key in required_contract_keys):
                return True
            return False

        @staticmethod
        def _pipeline_run_timeout_sec() -> float:
            load_mcp_env(override=False, only_prefixes=("STRATEGY_LLM_",))
            configured = os.getenv('STRATEGY_LLM_PIPELINE_RUN_TIMEOUT_SEC')
            if configured is not None:
                try:
                    value = float(configured or 20)
                except Exception:
                    value = 20.0
                return max(5.0, min(value, 600.0))

            stage_default = 10.0
            stage_timeouts: dict[str, Any] = {}
            try:
                strategy_generator_imports = _resolve_strategy_generators_imports()
                stage_default = float(strategy_generator_imports.get('PIPELINE_STAGE_TIMEOUT_SEC') or 10.0)
                stage_timeouts = dict(strategy_generator_imports.get('PIPELINE_STAGE_TIMEOUTS') or {})
            except Exception:
                stage_default = 10.0
                stage_timeouts = {}

            total_stage_budget = 0.0
            for timeout in stage_timeouts.values():
                try:
                    total_stage_budget += max(1.0, float(timeout or 0.0))
                except Exception:
                    total_stage_budget += max(1.0, stage_default)
            if total_stage_budget <= 0.0:
                total_stage_budget = max(5.0, stage_default * 5.0)

            buffer_sec = max(5.0, min(60.0, total_stage_budget * 0.2))
            return max(5.0, min(total_stage_budget + buffer_sec, 600.0))

        async def _run_external_provider_request(
            self,
            *,
            snapshot: dict[str, Any],
            frame,
            frame_cache: dict[str, pd.DataFrame],
            research_context: dict[str, Any],
            parent_strategies: list[dict[str, Any]],
            history_summary: list[dict[str, Any]],
            research_task: Optional[dict[str, Any]],
            request_limit: int,
            request_index: int,
        ) -> dict[str, Any]:
            try:
                provider_payload = await self.external_provider.generate_candidates(
                    snapshot=snapshot or {},
                    market_frame=frame,
                    research_context=research_context,
                    parent_strategies=list(parent_strategies or []),
                    history_summary=history_summary,
                    research_task=research_task,
                    limit=request_limit,
                )
                request_metrics = dict((provider_payload or {}).get('request_metrics') or {})
                analysis = dict((provider_payload or {}).get('analysis') or {})
                raw_candidates = list((provider_payload or {}).get('raw_candidates') or [])
                normalized_candidates = list((provider_payload or {}).get('candidates') or [])
                returned_candidates = raw_candidates or normalized_candidates
                all_specs: list[StrategySpec] = []
                viable_specs: list[StrategySpec] = []
                precompile_rejections: list[dict[str, Any]] = []
                open_dsl_candidate_count = 0
                open_dsl_compiled_candidate_count = 0
                open_dsl_viable_candidate_count = 0
                open_dsl_rejected_count = 0
                open_dsl_rejections: list[dict[str, Any]] = []
                for candidate_index, candidate in enumerate(returned_candidates):
                    normalized_candidate = (
                        dict(normalized_candidates[candidate_index] or {})
                        if candidate_index < len(normalized_candidates)
                        else None
                    )
                    candidate_tags = {
                        str(item or "").strip().lower()
                        for item in list((candidate or {}).get("tags") or [])
                        if str(item or "").strip()
                    }
                    candidate_generator_mode = str((candidate or {}).get("generator_mode") or "").strip().lower()
                    candidate_strategy_type = str((candidate or {}).get("strategy_type") or "").strip().lower()
                    open_dsl_candidate = bool(
                        candidate_tags.intersection({"open_dsl", "llm_defined", "llm_defined_dsl", "daily_dsl"})
                        or candidate_generator_mode in {"open_dsl", "llm_defined", "llm_defined_dsl"}
                        or candidate_strategy_type in {"open_dsl", "llm_defined"}
                    )
                    if open_dsl_candidate:
                        open_dsl_candidate_count += 1
                    if isinstance(candidate, dict) and not self._candidate_payload_looks_normalized(normalized_candidate):
                        normalized_candidate = StrategyLLMProvider._normalize_candidate_payload(
                            candidate,
                            research_task=research_task,
                            allow_legacy_contract_defaults=True,
                        )
                    spec = self._build_external_candidate_spec(
                        candidate,
                        provider_payload or {},
                        frame_cache,
                        frame,
                        normalized_candidate=normalized_candidate,
                        research_task=research_task,
                    )
                    if spec is None:
                        reject_reasons = list(
                            candidate.get("_open_dsl_reject_reasons")
                            or ((normalized_candidate or {}).get("_open_dsl_reject_reasons") if isinstance(normalized_candidate, dict) else None)
                            or candidate.get("_normalize_reject_reasons")
                            or candidate.get("_hypothesis_reject_reasons")
                            or candidate.get("_hypothesis_compile_reject_reasons")
                            or candidate.get("_generator_precompile_reject_reasons")
                            or ((normalized_candidate or {}).get("_normalize_reject_reasons") if isinstance(normalized_candidate, dict) else None)
                            or ((normalized_candidate or {}).get("_hypothesis_reject_reasons") if isinstance(normalized_candidate, dict) else None)
                            or ((normalized_candidate or {}).get("_hypothesis_compile_reject_reasons") if isinstance(normalized_candidate, dict) else None)
                            or ((normalized_candidate or {}).get("_generator_precompile_reject_reasons") if isinstance(normalized_candidate, dict) else None)
                            or []
                        )
                        if open_dsl_candidate and reject_reasons:
                            open_dsl_rejected_count += 1
                            if len(open_dsl_rejections) < 8:
                                open_dsl_rejections.append(
                                    {
                                        'name': str((candidate or {}).get('name') or ''),
                                        'strategy_type': str((candidate or {}).get('strategy_type') or ''),
                                        'reject_reasons': reject_reasons,
                                    }
                                )
                        if reject_reasons and len(precompile_rejections) < 8:
                            precompile_rejections.append(
                                {
                                    'name': str((candidate or {}).get('name') or ''),
                                    'strategy_type': str((candidate or {}).get('strategy_type') or ''),
                                    'reject_reasons': reject_reasons,
                                }
                            )
                        continue
                    all_specs.append(spec)
                    if str((spec.metadata or {}).get('candidate_lane') or '').strip().lower() == 'l3_open_dsl':
                        open_dsl_compiled_candidate_count += 1
                    if self._is_viable_external_spec(spec):
                        viable_specs.append(spec)
                        if str((spec.metadata or {}).get('candidate_lane') or '').strip().lower() == 'l3_open_dsl':
                            open_dsl_viable_candidate_count += 1
                viable_specs.sort(key=self._spec_preflight_score, reverse=True)
                all_specs.sort(key=self._spec_preflight_score, reverse=True)
                non_executable_candidate_count = max(0, len(returned_candidates) - len(all_specs))
                return {
                    'status': 'succeeded',
                    'request_index': request_index,
                    'analysis': analysis,
                    'request_report': {
                        'request_index': request_index,
                        'request_limit': request_limit,
                        'status': 'succeeded',
                        'returned_candidate_count': len(returned_candidates),
                        'compiled_candidate_count': len(all_specs),
                        'non_executable_candidate_count': non_executable_candidate_count,
                        'viable_candidate_count': len(viable_specs),
                        'open_dsl_candidate_count': open_dsl_candidate_count,
                        'open_dsl_compiled_candidate_count': open_dsl_compiled_candidate_count,
                        'open_dsl_viable_candidate_count': open_dsl_viable_candidate_count,
                        'open_dsl_rejected_count': open_dsl_rejected_count,
                        'open_dsl_rejections': open_dsl_rejections,
                        'precompile_rejected_count': len(precompile_rejections),
                        'precompile_rejections': precompile_rejections,
                        'candidate_names': [str((item or {}).get('name') or '') for item in returned_candidates[:4]],
                        'candidate_preflight': [
                            {
                                'name': spec.name,
                                'score': self._spec_preflight_score(spec),
                                'entry_count': int(dict(spec.metadata.get('dsl_activity') or {}).get('entry_count') or 0),
                                'exit_count': int(dict(spec.metadata.get('dsl_activity') or {}).get('exit_count') or 0),
                                'target_symbols': list(spec.metadata.get('target_symbols') or []),
                                'preflight_symbol': spec.metadata.get('preflight_symbol'),
                            }
                            for spec in all_specs[:4]
                        ],
                        'analysis': analysis,
                        'request_metrics': request_metrics,
                    },
                    'successful_without_specs': bool(returned_candidates) and not all_specs,
                    'all_specs': all_specs,
                    'viable_specs': viable_specs,
                    'exception': None,
                }
            except Exception as exc:
                metrics = dict(getattr(exc, 'metrics', {}) or {})
                request_status = str(metrics.get('status') or '').strip().lower()
                if request_status not in {'compatibility_skip', 'cooldown_skip'}:
                    request_status = 'failed'
                # 提升可观测性：把 metrics 里的 last_error 文本和首条 attempt 错误拼到日志里。
                # 否则 %r 仅展示 "StrategyLLMRequestError('... after N attempts: ValueError')"
                # 这种没有原因的壳，根因要去翻 stages/run_id 持久化才能拿到。
                last_error_type = metrics.get('last_error_type') or exc.__class__.__name__
                last_error_text = metrics.get('last_error')
                last_status_code = metrics.get('last_error_status_code')
                first_attempt: dict[str, Any] = {}
                attempts_list = metrics.get('attempts') or []
                if isinstance(attempts_list, list) and attempts_list:
                    candidate_attempt = attempts_list[0]
                    if isinstance(candidate_attempt, dict):
                        first_attempt = candidate_attempt
                first_attempt_error = str(first_attempt.get('error') or '')
                first_attempt_type = str(first_attempt.get('error_type') or '')
                last_error_summary = str(last_error_text or first_attempt_error or '').strip()
                if not last_error_summary:
                    last_error_summary = exc.__class__.__name__
                logger.warning(
                    'LLMProxyStrategyGenerator external provider failed at request_index=%s limit=%s, '
                    'retrying/fallback: %r '
                    '| status=%s last_error_type=%s status_code=%s last_error=%r '
                    'first_attempt_error_type=%s first_attempt_error=%r',
                    request_index,
                    request_limit,
                    exc,
                    request_status,
                    last_error_type,
                    last_status_code,
                    last_error_summary[:300],
                    first_attempt_type or last_error_type,
                    first_attempt_error[:300],
                )
                return {
                    'status': 'failed',
                    'request_index': request_index,
                    'analysis': {},
                    'request_report': {
                        'request_index': request_index,
                        'request_limit': request_limit,
                        'status': request_status,
                        'error_type': metrics.get('last_error_type') or exc.__class__.__name__,
                        'error': metrics.get('last_error') or str(exc) or exc.__class__.__name__,
                        'request_metrics': metrics,
                    },
                    'successful_without_specs': False,
                    'all_specs': [],
                    'viable_specs': [],
                    'exception': exc,
                }

        async def _frame_from_codes(self, db, codes: list[str], limit: int = 180) -> Optional[pd.DataFrame]:
            for code in list(dict.fromkeys([str(item or '').strip() for item in list(codes or []) if str(item or '').strip()])):
                try:
                    klines = await db.get_klines(code, limit=limit)
                except Exception:
                    klines = []
                if not klines:
                    continue
                frame = pd.DataFrame(normalize_klines(klines))
                if not frame.empty and 'close' in frame.columns:
                    return frame.tail(120).copy()
            return None

        @classmethod
        def _build_synthetic_market_frame(cls, research_context: Optional[dict[str, Any]] = None) -> Optional[pd.DataFrame]:
            context = dict(research_context or {})
            task_target_context = dict(context.get('task_target_context') or {})
            targeted_task = bool(
                task_target_context.get('targeted_task')
                or list((context.get('research_task') or {}).get('target_symbols') or [])
            )
            if bool(context.get('blocked_by_target_universe')):
                return None
            if targeted_task:
                target_symbols = set(cls._normalize_code_list([
                    task_target_context.get('matched_target_symbols'),
                    task_target_context.get('requested_target_symbols'),
                    (context.get('research_task') or {}).get('target_symbols'),
                ], limit=8))
                sources = [
                    dict(item or {})
                    for item in [*list(context.get('symbol_insights') or []), *list(context.get('candidate_universe') or [])]
                    if (
                        not target_symbols
                        or str((item or {}).get('code') or '').strip() in target_symbols
                    )
                ]
                if not sources:
                    return None
            else:
                sources = list(context.get('symbol_insights') or []) + list(context.get('candidate_universe') or [])
            close_values = []
            volume_values = []
            for item in sources:
                try:
                    close = float((item or {}).get('close') or 0.0)
                except Exception:
                    close = 0.0
                if close <= 0:
                    continue
                close_values.append(close)
                try:
                    volume = float((item or {}).get('volume') or (item or {}).get('market_cap') or 0.0)
                except Exception:
                    volume = 0.0
                volume_values.append(max(volume, 1.0))
                if len(close_values) >= 12:
                    break
            if not close_values:
                return None
            rows = max(24, len(close_values) * 4)
            closes = []
            volumes = []
            for idx in range(rows):
                base_close = close_values[idx % len(close_values)]
                drift = 1.0 + ((idx % 6) - 2) * 0.0025
                closes.append(round(base_close * drift, 6))
                base_volume = volume_values[idx % len(volume_values)] if volume_values else 1.0
                volumes.append(float(max(base_volume * (1.0 + ((idx % 5) - 2) * 0.03), 1.0)))
            return pd.DataFrame({
                'open': closes,
                'high': [value * 1.003 for value in closes],
                'low': [value * 0.997 for value in closes],
                'close': closes,
                'volume': volumes,
            })

        async def _build_market_frame(self, db, research_task: Optional[dict[str, Any]] = None) -> Optional[pd.DataFrame]:
            research_task = normalize_research_task_contract(research_task or {})
            target_codes = self._normalize_code_list(research_task.get('target_symbols'))
            target_frame = await self._frame_from_codes(db, target_codes, limit=180)
            if target_frame is not None:
                return target_frame
            if target_codes:
                return None

            primary_codes: list[str] = []
            if hasattr(db, 'list_stock_universe'):
                try:
                    rows = await db.list_stock_universe(limit=3, offset=0)
                    primary_codes = [str((row or {}).get('code') or '').strip() for row in rows if str((row or {}).get('code') or '').strip()]
                except Exception:
                    primary_codes = []
            return await self._frame_from_codes(db, [*primary_codes, *DEFAULT_CODES], limit=180)

        async def _build_symbol_frame_cache(self, db, research_context: Optional[dict[str, Any]] = None, research_task: Optional[dict[str, Any]] = None) -> dict[str, pd.DataFrame]:
            cache: dict[str, pd.DataFrame] = {}
            research_context = dict(research_context or {})
            research_task = normalize_research_task_contract(research_task or {})
            task_target_context = dict(research_context.get('task_target_context') or {})
            targeted_task = bool(task_target_context.get('targeted_task') or list(research_task.get('target_symbols') or []))
            code_sources: list[Any] = [research_task.get('target_symbols')]
            if targeted_task:
                code_sources.extend([
                    task_target_context.get('matched_target_symbols'),
                    task_target_context.get('candidate_universe_symbols'),
                    task_target_context.get('symbol_insight_codes'),
                ])
            else:
                code_sources.extend([
                    [item.get('code') for item in list(research_context.get('candidate_universe') or [])],
                    [item.get('code') for item in list(research_context.get('symbol_insights') or [])],
                ])
            codes = self._normalize_code_list(code_sources, limit=10)
            for code in codes:
                frame = await self._frame_from_codes(db, [code], limit=180)
                if frame is not None:
                    cache[code] = frame
            return cache

        def _candidate_frame_codes(
            self,
            candidate: dict[str, Any],
            research_task: Optional[dict[str, Any]] = None,
        ) -> list[str]:
            research_task = dict(research_task or {})
            return self._normalize_code_list([
                candidate.get('target_symbols'),
                candidate.get('stock_pool'),
                ((candidate.get('dsl') or {}).get('metadata') or {}).get('target_symbols'),
                research_task.get('target_symbols'),
            ], limit=10)

        def _build_external_candidate_spec(
            self,
            candidate: dict[str, Any],
            provider_payload: dict[str, Any],
            frame_cache: dict[str, pd.DataFrame],
            default_frame: Optional[pd.DataFrame],
            normalized_candidate: Optional[dict[str, Any]] = None,
            research_task: Optional[dict[str, Any]] = None,
        ) -> Optional[StrategySpec]:
            frame_candidates: list[tuple[str, pd.DataFrame]] = []
            for code in self._candidate_frame_codes(candidate, research_task=research_task):
                frame = frame_cache.get(code)
                if frame is not None and not frame.empty:
                    frame_candidates.append((code, frame))
            if not frame_candidates and default_frame is not None and not default_frame.empty:
                frame_candidates.append(('__default__', default_frame))

            best_spec: Optional[StrategySpec] = None
            best_rank: Optional[tuple[float, int, int]] = None
            for code, frame in frame_candidates:
                spec = self._external_candidate_to_spec(candidate, provider_payload or {}, market_frame=frame)
                if spec is None and normalized_candidate:
                    spec = self._external_candidate_to_spec(
                        normalized_candidate,
                        provider_payload or {},
                        market_frame=frame,
                    )
                if spec is None:
                    continue
                metadata = dict(spec.metadata or {})
                metadata['preflight_symbol'] = None if code == '__default__' else code
                spec.metadata = metadata
                activity = dict(metadata.get('dsl_activity') or {})
                rank = (
                    self._spec_preflight_score(spec),
                    int(min(activity.get('entry_count') or 0, activity.get('exit_count') or 0)),
                    int(activity.get('active_days') or 0),
                )
                if best_rank is None or rank > best_rank:
                    best_spec = spec
                    best_rank = rank
            return best_spec
