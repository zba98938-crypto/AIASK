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
            'validation_profile': {'profile': 'event_trade_validation', 'validation_focus': 'candidate_target_only', 'primary_validation_layer': 'target', 'objective_profile': 'high_precision', 'trade_density_preference': 'low', 'regime_required': True, 'cost_robust_required': True, 'entry_selectivity': 'strict_event_breakout', 'preferred_regime': 'event_follow_through_with_structure_confirmation', 'avoid_regime': 'false_breakout_or_post_event_mean_reversion', 'event_prefilter_required': True, 'event_prefilter_profile': 'announcement_flow_sector_v1', 'event_prefilter_min_confirmations': 1},
            'targeting_policy': {'target_symbol_policy': 'strict_intersection', 'universe_scope': 'liquid_event_breakout_leaders', 'universe_expansion_policy': 'forbid'},
            'event_prefilter': {'required': True, 'profile': 'announcement_flow_sector_v1', 'allowed_sources': ['announcement', 'fund_flow', 'sector_catalyst'], 'observed_sources': [], 'observed_confirmation_count': 0, 'confirmation_count': 0, 'min_confirmations': 1, 'passed': False, 'status': 'missing', 'focus_industries': [], 'event_anchor': {}, 'anchor_strength': None},
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

class _LLMProxyStrategyGeneratorContextMixin:
        @staticmethod
        def _safe_float(value: Any) -> float:
            try:
                return float(value or 0.0)
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_context.py:360: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                return 0.0

        @staticmethod
        def _normalize_code_list(values: Any, limit: int = 12) -> list[str]:
            codes: list[str] = []
            seen: set[str] = set()

            def visit(value: Any):
                if value is None:
                    return
                if isinstance(value, dict):
                    for key in ('code', 'symbol', 'stock_code'):
                        if value.get(key) is not None:
                            visit(value.get(key))
                    for key in ('codes', 'symbols', 'stock_codes', 'target_symbols'):
                        if value.get(key) is not None:
                            visit(value.get(key))
                    return
                if isinstance(value, (list, tuple, set)):
                    for item in value:
                        visit(item)
                    return
                raw = str(value or '').strip()
                if not raw:
                    return
                if any(sep in raw for sep in [',', ';', '|', '\n', '\t', ' ']):
                    normalized = raw.replace(';', ',').replace('|', ',').replace('\n', ',').replace('\t', ',').replace(' ', ',')
                    for part in normalized.split(','):
                        visit(part)
                    return
                code = raw.split('.')[0].strip()
                if not code or code in seen:
                    return
                seen.add(code)
                codes.append(code)

            visit(values)
            return codes[: max(1, min(int(limit or 12), 40))]

        @classmethod
        def _summarize_symbol_frame(cls, code: str, frame: pd.DataFrame) -> Optional[dict[str, Any]]:
            if frame is None or frame.empty or 'close' not in frame.columns:
                return None
            compact = frame.tail(min(len(frame), 120)).copy()
            close = pd.to_numeric(compact['close'], errors='coerce').dropna()
            if len(close) < 20:
                return None
            volume = pd.to_numeric(compact.get('volume'), errors='coerce').dropna() if 'volume' in compact.columns else pd.Series(dtype=float)
            sma5 = float(close.tail(5).mean())
            sma20 = float(close.tail(20).mean())
            latest = float(close.iloc[-1])
            return_5d = (latest / float(close.iloc[-6]) - 1.0) if len(close) >= 6 and float(close.iloc[-6]) else 0.0
            return_20d = (latest / float(close.iloc[-21]) - 1.0) if len(close) >= 21 and float(close.iloc[-21]) else 0.0
            volatility_20d = float(close.pct_change().tail(20).std(ddof=0) or 0.0)
            volume_ratio_20 = float(volume.tail(5).mean() / max(float(volume.tail(20).mean() or 1.0), 1.0)) if len(volume) >= 20 else 1.0
            trend_state = 'sideways'
            if latest >= sma20 and sma5 >= sma20:
                trend_state = 'uptrend'
            elif latest < sma20 and sma5 < sma20:
                trend_state = 'downtrend'
            return {
                'code': code,
                'close': round(latest, 6),
                'return_5d': round(return_5d, 6),
                'return_20d': round(return_20d, 6),
                'volatility_20d': round(volatility_20d, 6),
                'price_vs_sma20': 'above' if latest >= sma20 else 'below',
                'sma5_vs_sma20': 'above' if sma5 >= sma20 else 'below',
                'trend_state': trend_state,
                'volume_ratio_20': round(volume_ratio_20, 6),
            }

        @classmethod
        def _rank_symbol_context(cls, item: dict[str, Any]) -> float:
            score = 0.0
            score += cls._safe_float(item.get('return_20d')) * 5.0
            score += cls._safe_float(item.get('return_5d')) * 2.0
            score -= cls._safe_float(item.get('volatility_20d')) * 1.5
            if item.get('trend_state') == 'uptrend':
                score += 0.35
            if item.get('price_vs_sma20') == 'above':
                score += 0.12
            if item.get('sma5_vs_sma20') == 'above':
                score += 0.08
            score += min(max(cls._safe_float(item.get('volume_ratio_20')) - 1.0, -0.5), 0.5) * 0.3
            market_cap = cls._safe_float(item.get('market_cap'))
            if market_cap > 0:
                score += min(market_cap / 1_000_000_000_000, 0.15)
            pe_ratio = item.get('pe_ratio')
            if isinstance(pe_ratio, (int, float)) and 0 < float(pe_ratio) < 40:
                score += 0.05
            pb_ratio = item.get('pb_ratio')
            if isinstance(pb_ratio, (int, float)) and 0 < float(pb_ratio) < 8:
                score += 0.03
            financial_snapshot = dict(item.get('financial_snapshot') or {})
            if cls._safe_float(financial_snapshot.get('revenue_growth')) > 0:
                score += 0.05
            if cls._safe_float(financial_snapshot.get('profit_growth')) > 0:
                score += 0.05
            factor_snapshot = dict(item.get('factor_snapshot') or {})
            positive_factor_count = len([v for v in factor_snapshot.values() if isinstance(v, (int, float)) and float(v) > 0])
            score += min(positive_factor_count, 3) * 0.03
            return round(score, 6)

        async def _load_universe_rows(self, db) -> list[dict[str, Any]]:
            if not hasattr(db, 'list_stock_universe'):
                return []
            rows: list[dict[str, Any]] = []
            offset = 0
            while len(rows) < RESEARCH_UNIVERSE_SCAN_LIMIT:
                batch_limit = min(RESEARCH_UNIVERSE_PAGE_SIZE, RESEARCH_UNIVERSE_SCAN_LIMIT - len(rows))
                if batch_limit <= 0:
                    break
                try:
                    batch = await db.list_stock_universe(limit=batch_limit, offset=offset)
                except TypeError:
                    batch = await db.list_stock_universe(limit=batch_limit)
                except Exception:
                    break
                if not batch:
                    break
                rows.extend([dict(item or {}) for item in batch])
                offset += len(batch)
                if len(batch) < batch_limit:
                    break
            return rows

        async def _fetch_factor_snapshot(self, db, codes: list[str], snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
            if not codes or not hasattr(db, 'get_factor_values'):
                return {}
            factor_names: list[str] = []
            for payload in (dict(snapshot.get('factor_ic_trend') or {}), dict(snapshot.get('factor_ic') or {})):
                for key in payload.keys():
                    factor_name = str(key or '').strip()
                    if factor_name and factor_name not in factor_names:
                        factor_names.append(factor_name)
            factor_names = factor_names[:3]
            if not factor_names:
                return {}
            factor_snapshot: dict[str, dict[str, Any]] = {code: {} for code in codes}
            for factor_name in factor_names:
                try:
                    rows = await db.get_factor_values(codes, factor_name)
                except Exception as _swallowed:
                    logger.warning("packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_context.py:504: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                    continue
                latest_by_code: dict[str, tuple[str, float]] = {}
                for row in list(rows or []):
                    code = str((row or {}).get('stock_code') or (row or {}).get('code') or '').strip()
                    if not code:
                        continue
                    factor_date = str((row or {}).get('factor_date') or '')
                    factor_value = self._safe_float((row or {}).get('factor_value'))
                    current = latest_by_code.get(code)
                    if current is None or factor_date >= current[0]:
                        latest_by_code[code] = (factor_date, factor_value)
                for code, payload in latest_by_code.items():
                    factor_snapshot.setdefault(code, {})[factor_name] = payload[1]
            return {code: values for code, values in factor_snapshot.items() if values}

        async def build_shared_research_context(
            self,
            db,
            snapshot: Optional[dict[str, Any]],
            *,
            parent_strategies: Optional[list[dict]] = None,
            history_summary: Optional[list[dict]] = None,
        ) -> dict[str, Any]:
            base_snapshot = dict(snapshot or {})
            base_snapshot.pop('_shared_generation_context', None)
            return await self._build_research_context(
                db,
                base_snapshot,
                parent_strategies=parent_strategies,
                history_summary=history_summary,
                research_task={},
            )

        @classmethod
        def _build_market_background_context(
            cls,
            *,
            symbol_insights: Optional[list[dict[str, Any]]] = None,
            candidate_universe: Optional[list[dict[str, Any]]] = None,
            universe_scan: Optional[dict[str, Any]] = None,
            top_industries: Optional[dict[str, Any]] = None,
            cache_reused: bool = False,
        ) -> dict[str, Any]:
            symbol_insights = [dict(item or {}) for item in list(symbol_insights or [])]
            candidate_universe = [dict(item or {}) for item in list(candidate_universe or [])]
            universe_scan_payload = dict(universe_scan or {})
            return {
                'available': bool(symbol_insights or candidate_universe or universe_scan_payload or top_industries),
                'symbol_count': len(symbol_insights),
                'candidate_universe_count': len(candidate_universe),
                'symbol_insight_codes': cls._normalize_code_list(
                    [item.get('code') for item in symbol_insights],
                    limit=RESEARCH_SYMBOL_DETAIL_LIMIT,
                ),
                'candidate_universe_symbols': cls._normalize_code_list(
                    [item.get('code') for item in candidate_universe],
                    limit=RESEARCH_CANDIDATE_POOL_LIMIT,
                ),
                'total_stock_count': int(universe_scan_payload.get('total_stock_count') or 0),
                'scanned_stock_count': int(universe_scan_payload.get('scanned_stock_count') or 0),
                'data_ready_count': int(universe_scan_payload.get('data_ready_count') or 0),
                'coverage_ratio': universe_scan_payload.get('coverage_ratio'),
                'top_industries': dict(top_industries or universe_scan_payload.get('top_industries') or {}),
                'cache_reused': bool(cache_reused or universe_scan_payload.get('cache_reused')),
            }

        @classmethod
        def _build_task_target_context(
            cls,
            *,
            research_task: Optional[dict[str, Any]],
            symbol_insights: Optional[list[dict[str, Any]]] = None,
            candidate_universe: Optional[list[dict[str, Any]]] = None,
            status: str,
            blocked_by_target_universe: bool = False,
        ) -> dict[str, Any]:
            task = normalize_research_task_contract(research_task or {})
            symbol_insights = [dict(item or {}) for item in list(symbol_insights or [])]
            candidate_universe = [dict(item or {}) for item in list(candidate_universe or [])]
            requested_target_symbols = cls._normalize_code_list(task.get('target_symbols'))
            target_symbol_set = set(requested_target_symbols)
            matched_target_symbols = cls._normalize_code_list(
                [
                    item.get('code')
                    for item in [*symbol_insights, *candidate_universe]
                    if str(item.get('code') or '').strip() in target_symbol_set
                ],
                limit=RESEARCH_CANDIDATE_POOL_LIMIT,
            )
            return {
                'targeted_task': bool(requested_target_symbols),
                'status': str(status or 'broad_market_context'),
                'blocked_by_target_universe': bool(blocked_by_target_universe),
                'requested_target_symbols': requested_target_symbols,
                'matched_target_symbols': matched_target_symbols,
                'focus_industries': [
                    str(item).strip()
                    for item in list(task.get('focus_industries') or [])
                    if str(item).strip()
                ],
                'focus_markets': [
                    str(item).strip()
                    for item in list(task.get('focus_markets') or [])
                    if str(item).strip()
                ],
                'symbol_count': len(symbol_insights),
                'candidate_universe_count': len(candidate_universe),
                'symbol_insight_codes': cls._normalize_code_list(
                    [item.get('code') for item in symbol_insights],
                    limit=RESEARCH_SYMBOL_DETAIL_LIMIT,
                ),
                'candidate_universe_symbols': cls._normalize_code_list(
                    [item.get('code') for item in candidate_universe],
                    limit=RESEARCH_CANDIDATE_POOL_LIMIT,
                ),
            }

        @classmethod
        def _build_blocked_research_context(
            cls,
            *,
            snapshot: dict[str, Any],
            research_task: Optional[dict[str, Any]],
            parent_strategies: Optional[list[dict]] = None,
            history_summary: Optional[list[dict]] = None,
            universe_total_count: int = 0,
            top_industries: Optional[dict[str, Any]] = None,
            market_background_context: Optional[dict[str, Any]] = None,
            cache_reused: bool = False,
        ) -> dict[str, Any]:
            task = normalize_research_task_contract(research_task or {})
            market_regime = {
                'fg_level': snapshot.get('fg_level'),
                'fear_greed_index': snapshot.get('fear_greed_index'),
                'hot_sectors': list(snapshot.get('hot_sectors') or [])[:4],
                'cold_sectors': list(snapshot.get('cold_sectors') or [])[:3],
                'factor_ic': dict(snapshot.get('factor_ic') or {}),
                'factor_ic_trend': dict(snapshot.get('factor_ic_trend') or {}),
                'factor_research': dict(snapshot.get('factor_research') or {}),
            }
            task_target_context = cls._build_task_target_context(
                research_task=task,
                symbol_insights=[],
                candidate_universe=[],
                status='blocked_by_target_universe',
                blocked_by_target_universe=True,
            )
            background_context = dict(
                market_background_context
                or cls._build_market_background_context(
                    universe_scan={
                        'total_stock_count': universe_total_count,
                        'scanned_stock_count': 0,
                        'data_ready_count': 0,
                        'coverage_ratio': 0.0,
                        'cache_reused': cache_reused,
                    },
                    top_industries=top_industries,
                    cache_reused=cache_reused,
                )
            )
            top_categories = {
                str(key): int(value)
                for key, value in sorted(
                    dict(snapshot.get('category_counts') or {}).items(),
                    key=lambda item: item[1],
                    reverse=True,
                )[:5]
            }
            return {
                'research_task': {
                    'task_id': task.get('task_id'),
                    'theme': task.get('theme'),
                    'opportunity_type': task.get('opportunity_type'),
                    'focus_industries': list(task.get('focus_industries') or []),
                    'focus_markets': list(task.get('focus_markets') or []),
                    'target_symbols': list(task.get('target_symbols') or []),
                    'priority': task.get('priority'),
                    'strategy_preferences': list(task.get('strategy_preferences') or []),
                    'generation_limit': task.get('generation_limit'),
                    'rationale': task.get('rationale'),
                    'task_source': task.get('task_source'),
                },
                'market_regime': market_regime,
                'market_breadth': {
                    'symbol_count': 0,
                    'trend_up_count': 0,
                    'trend_down_count': 0,
                    'avg_return_20d': 0.0,
                    'avg_volatility_20d': 0.0,
                },
                'symbol_insights': [],
                'candidate_universe': [],
                'symbol_insight_codes': [],
                'candidate_universe_symbols': [],
                'target_context_status': 'blocked_by_target_universe',
                'blocked_by_target_universe': True,
                'task_target_context': task_target_context,
                'market_background_context': background_context,
                'universe_scan': {
                    'total_stock_count': universe_total_count,
                    'scanned_stock_count': 0,
                    'data_ready_count': 0,
                    'coverage_ratio': 0.0,
                    'detail_symbol_count': 0,
                    'candidate_universe_count': 0,
                    'top_industries': dict(top_industries or {}),
                    'cache_reused': bool(cache_reused),
                },
                'selection_framework': {
                    'technical': ['trend_state', 'return_20d', 'return_5d', 'volume_ratio_20', 'price_vs_sma20'],
                    'fundamental': ['market_cap', 'pe_ratio', 'pb_ratio', 'revenue_growth', 'profit_growth'],
                    'factor_names': [
                        str(key)
                        for key in list(
                            ((snapshot.get('factor_research') or {}).get('summary') or {}).get('top_factor_names')
                            or (snapshot.get('factor_research') or {}).get('active_factors')
                            or list((snapshot.get('factor_ic_trend') or {}).keys())[:3]
                        )[:3]
                    ],
                },
                'analysis_scope': {
                    'scan_mode': 'target_context_blocked',
                    'scan_limit': RESEARCH_UNIVERSE_SCAN_LIMIT,
                    'kline_scan_limit': RESEARCH_KLINE_SCAN_LIMIT,
                    'detail_limit': RESEARCH_SYMBOL_DETAIL_LIMIT,
                    'candidate_pool_limit': RESEARCH_CANDIDATE_POOL_LIMIT,
                },
                'population_state': {
                    'listed_count': int(snapshot.get('listed_count') or universe_total_count or 0),
                    'incubating_count': int(snapshot.get('incubating_count') or 0),
                    'top_categories': top_categories,
                },
                'parent_context': [
                    {
                        'id': item.get('id'),
                        'name': item.get('name'),
                        'strategy_type': item.get('strategy_type'),
                        'status': item.get('status'),
                    }
                    for item in list(parent_strategies or [])[:3]
                ],
                'experiment_feedback': [
                    {
                        'generator_type': item.get('generator_type'),
                        'status': item.get('status'),
                        'decision': item.get('decision'),
                        'final_score': item.get('final_score'),
                    }
                    for item in list(history_summary or [])[:4]
                ],
            }
        @classmethod
        def _reuse_shared_research_context(
            cls,
            shared_context: dict[str, Any],
            *,
            snapshot: dict[str, Any],
            parent_strategies: Optional[list[dict]] = None,
            history_summary: Optional[list[dict]] = None,
            research_task: Optional[dict[str, Any]] = None,
        ) -> Optional[dict[str, Any]]:
            if not shared_context:
                return None
            research_task = normalize_research_task_contract(research_task or {})
            task_target_symbols = cls._normalize_code_list(research_task.get('target_symbols'))
            task_focus_industries = [str(item).strip() for item in list(research_task.get('focus_industries') or []) if str(item).strip()]
            task_focus_markets = [str(item).strip() for item in list(research_task.get('focus_markets') or []) if str(item).strip()]
            target_symbol_set = set(task_target_symbols)
            task_focus_market_set = set(task_focus_markets)
            symbol_insights = [dict(item or {}) for item in list(shared_context.get('symbol_insights') or [])]
            candidate_universe = [dict(item or {}) for item in list(shared_context.get('candidate_universe') or [])]
            market_background_context = cls._build_market_background_context(
                symbol_insights=symbol_insights,
                candidate_universe=candidate_universe,
                universe_scan=shared_context.get('universe_scan'),
                cache_reused=True,
            )

            def _matches(item: dict[str, Any]) -> bool:
                if not item:
                    return False
                code = str(item.get('code') or '').strip()
                industry_text = str(item.get('industry') or item.get('sector') or '')
                market = str(item.get('market') or '').strip()
                if target_symbol_set and code not in target_symbol_set:
                    return False
                if task_focus_industries and not any(keyword in industry_text for keyword in task_focus_industries):
                    return False
                if task_focus_market_set and market not in task_focus_market_set:
                    return False
                return True

            has_filters = bool(task_target_symbols or task_focus_industries or task_focus_markets)
            if has_filters:
                filtered_symbols = [item for item in symbol_insights if _matches(item)]
                filtered_candidates = [item for item in candidate_universe if _matches(item)]
                if task_target_symbols and not filtered_symbols and not filtered_candidates:
                    # PR-S23: target_symbols 没命中共享 cache（cache 默认按市值取前 N，
                    # BULK 矩阵抽中的小盘股 / market_cap NULL 股票天然不在其中）。
                    # 不再直接 short-circuit 到 blocked_research_context，
                    # 而是让上层 _build_research_context 重新走一次完整查询路径，
                    # 那条路径会用 list_stocks_by_codes by-code 把 target 补进 universe_rows。
                    return None
                if task_focus_industries and not filtered_symbols and not filtered_candidates:
                    return None
                if task_target_symbols:
                    symbol_insights = filtered_symbols
                    candidate_universe = filtered_candidates
                else:
                    symbol_insights = filtered_symbols or symbol_insights[: max(1, min(len(symbol_insights), RESEARCH_SYMBOL_DETAIL_LIMIT))]
                    candidate_universe = filtered_candidates or candidate_universe[: max(1, min(len(candidate_universe), RESEARCH_CANDIDATE_POOL_LIMIT))]

            symbol_insights = [dict(item) for item in symbol_insights[:RESEARCH_SYMBOL_DETAIL_LIMIT]]
            candidate_universe = [dict(item) for item in candidate_universe[:RESEARCH_CANDIDATE_POOL_LIMIT]]
            trend_up_count = len([item for item in symbol_insights if item.get('trend_state') == 'uptrend'])
            trend_down_count = len([item for item in symbol_insights if item.get('trend_state') == 'downtrend'])
            avg_return_20d = round(sum(cls._safe_float(item.get('return_20d')) for item in symbol_insights) / max(len(symbol_insights), 1), 6) if symbol_insights else 0.0
            avg_volatility_20d = round(sum(cls._safe_float(item.get('volatility_20d')) for item in symbol_insights) / max(len(symbol_insights), 1), 6) if symbol_insights else 0.0
            market_regime = {
                'fg_level': snapshot.get('fg_level'),
                'fear_greed_index': snapshot.get('fear_greed_index'),
                'hot_sectors': list(snapshot.get('hot_sectors') or [])[:4],
                'cold_sectors': list(snapshot.get('cold_sectors') or [])[:3],
                'factor_ic': dict(snapshot.get('factor_ic') or {}),
                'factor_ic_trend': dict(snapshot.get('factor_ic_trend') or {}),
                'factor_research': dict(snapshot.get('factor_research') or {}),
            }
            universe_scan = dict(shared_context.get('universe_scan') or {})
            universe_scan.update({
                'detail_symbol_count': len(symbol_insights),
                'candidate_universe_count': len(candidate_universe),
                'cache_reused': True,
            })
            target_context_status = (
                'targeted_active'
                if task_target_symbols
                else ('filtered_market_context' if has_filters else 'broad_market_context')
            )
            task_target_context = cls._build_task_target_context(
                research_task=research_task,
                symbol_insights=symbol_insights,
                candidate_universe=candidate_universe,
                status=target_context_status,
            )
            analysis_scope = dict(shared_context.get('analysis_scope') or {})
            if has_filters:
                analysis_scope['scan_mode'] = target_context_status
            return {
                **dict(shared_context or {}),
                'research_task': {
                    'task_id': research_task.get('task_id'),
                    'theme': research_task.get('theme'),
                    'opportunity_type': research_task.get('opportunity_type'),
                    'focus_industries': task_focus_industries,
                    'focus_markets': task_focus_markets,
                    'target_symbols': task_target_symbols,
                    'priority': research_task.get('priority'),
                    'strategy_preferences': list(research_task.get('strategy_preferences') or []),
                    'generation_limit': research_task.get('generation_limit'),
                    'rationale': research_task.get('rationale'),
                    'task_source': research_task.get('task_source'),
                },
                'market_regime': market_regime,
                'market_breadth': {
                    'symbol_count': len(symbol_insights),
                    'trend_up_count': trend_up_count,
                    'trend_down_count': trend_down_count,
                    'avg_return_20d': avg_return_20d,
                    'avg_volatility_20d': avg_volatility_20d,
                },
                'symbol_insights': symbol_insights,
                'candidate_universe': candidate_universe,
                'symbol_insight_codes': list(task_target_context.get('symbol_insight_codes') or []),
                'candidate_universe_symbols': list(task_target_context.get('candidate_universe_symbols') or []),
                'target_context_status': target_context_status,
                'blocked_by_target_universe': False,
                'task_target_context': task_target_context,
                'market_background_context': market_background_context,
                'universe_scan': universe_scan,
                'analysis_scope': analysis_scope,
                'selection_framework': {
                    'technical': ['trend_state', 'return_20d', 'return_5d', 'volume_ratio_20', 'price_vs_sma20'],
                    'fundamental': ['market_cap', 'pe_ratio', 'pb_ratio', 'revenue_growth', 'profit_growth'],
                    'factor_names': [
                        str(key)
                        for key in list(
                            ((snapshot.get('factor_research') or {}).get('summary') or {}).get('top_factor_names')
                            or (snapshot.get('factor_research') or {}).get('active_factors')
                            or list((snapshot.get('factor_ic_trend') or {}).keys())[:3]
                        )[:3]
                    ],
                },
                'parent_context': [
                    {
                        'id': item.get('id'),
                        'name': item.get('name'),
                        'strategy_type': item.get('strategy_type'),
                        'status': item.get('status'),
                    }
                    for item in list(parent_strategies or [])[:3]
                ],
                'experiment_feedback': [
                    {
                        'generator_type': item.get('generator_type'),
                        'status': item.get('status'),
                        'decision': item.get('decision'),
                        'final_score': item.get('final_score'),
                    }
                    for item in list(history_summary or [])[:4]
                ],
            }
        async def _build_research_context(
            self,
            db,
            snapshot: Optional[dict[str, Any]],
            *,
            parent_strategies: Optional[list[dict]] = None,
            history_summary: Optional[list[dict]] = None,
            research_task: Optional[dict[str, Any]] = None,
        ) -> dict[str, Any]:
            snapshot = snapshot or {}
            research_task = normalize_research_task_contract(research_task or {})
            shared_generation_context = dict(snapshot.get('_shared_generation_context') or {})
            shared_research_context = dict(shared_generation_context.get('research_context') or {})
            if shared_research_context:
                reused_context = self._reuse_shared_research_context(
                    shared_research_context,
                    snapshot=snapshot,
                    parent_strategies=parent_strategies,
                    history_summary=history_summary,
                    research_task=research_task,
                )
                if reused_context is not None:
                    return reused_context
            universe_rows = await self._load_universe_rows(db)
            universe_total_count = 0
            if hasattr(db, 'count_stock_universe'):
                try:
                    universe_total_count = int(await db.count_stock_universe())
                except Exception:
                    universe_total_count = 0
            if universe_total_count <= 0:
                universe_total_count = len(universe_rows)

            # PR-S23: 当 research_task.target_symbols 命中的股票不在
            # 默认按市值排序的前 RESEARCH_UNIVERSE_SCAN_LIMIT 名 universe 内时
            # （例如 BULK 矩阵抽中的 market_cap NULL 中小盘股），by-code 直接
            # 把它们补进 universe_rows，避免 _build_research_context 走
            # _build_blocked_research_context 把整条 LLM 路径 short-circuit 掉。
            task_target_symbols_preview = self._normalize_code_list(
                (research_task or {}).get('target_symbols')
            )
            if task_target_symbols_preview and hasattr(db, 'list_stocks_by_codes'):
                existing_codes = {
                    str((row or {}).get('code') or '').strip()
                    for row in universe_rows
                }
                missing_codes = [
                    code for code in task_target_symbols_preview
                    if code and code not in existing_codes
                ]
                if missing_codes:
                    try:
                        extra_rows = await db.list_stocks_by_codes(missing_codes)
                    except Exception:
                        extra_rows = []
                    if extra_rows:
                        universe_rows = list(universe_rows) + [
                            dict(item or {}) for item in extra_rows
                        ]

            breadth_rows: list[dict[str, Any]] = []
            symbol_insights: list[dict[str, Any]] = []
            candidate_universe: list[dict[str, Any]] = []
            task_target_symbols = self._normalize_code_list(research_task.get('target_symbols'))
            task_focus_industries = [str(item).strip() for item in list(research_task.get('focus_industries') or []) if str(item).strip()]
            task_focus_markets = [str(item).strip() for item in list(research_task.get('focus_markets') or []) if str(item).strip()]
            has_market_filters = bool(task_focus_industries or task_focus_markets)
            filtered_rows = list(universe_rows)
            top_industries: dict[str, int] = {}
            if universe_rows:
                industry_counts: dict[str, int] = {}
                for row in universe_rows:
                    industry = str((row or {}).get('industry') or (row or {}).get('sector') or '未分类').strip() or '未分类'
                    industry_counts[industry] = industry_counts.get(industry, 0) + 1
                top_industries = {
                    str(key): int(value)
                    for key, value in sorted(industry_counts.items(), key=lambda item: item[1], reverse=True)[:8]
                }
            if task_target_symbols:
                target_set = set(task_target_symbols)
                targeted = [row for row in filtered_rows if str((row or {}).get('code') or "").strip() in target_set]
                if not targeted:
                    return self._build_blocked_research_context(
                        snapshot=snapshot,
                        research_task=research_task,
                        parent_strategies=parent_strategies,
                        history_summary=history_summary,
                        universe_total_count=universe_total_count,
                        top_industries=top_industries,
                        cache_reused=False,
                    )
                filtered_rows = targeted
            elif task_focus_industries:
                industry_filtered = [
                    row for row in filtered_rows
                    if any(keyword in str((row or {}).get('industry') or (row or {}).get('sector') or "") for keyword in task_focus_industries)
                ]
                if industry_filtered:
                    filtered_rows = industry_filtered
            if task_focus_markets:
                market_filtered = [row for row in filtered_rows if str((row or {}).get('market') or "").strip() in set(task_focus_markets)]
                if market_filtered:
                    filtered_rows = market_filtered
            if task_target_symbols and not filtered_rows:
                return self._build_blocked_research_context(
                    snapshot=snapshot,
                    research_task=research_task,
                    parent_strategies=parent_strategies,
                    history_summary=history_summary,
                    universe_total_count=universe_total_count,
                    top_industries=top_industries,
                    cache_reused=False,
                )
            scan_rows = list(filtered_rows[: min(len(filtered_rows), RESEARCH_KLINE_SCAN_LIMIT)])

            for row in scan_rows:
                code = str((row or {}).get('code') or '').strip()
                if not code:
                    continue
                try:
                    klines = await db.get_klines(code, limit=180)
                except Exception:
                    klines = []
                if not klines:
                    continue
                frame = pd.DataFrame(klines)
                summary = self._summarize_symbol_frame(code, frame)
                if summary is None:
                    continue
                enriched = {
                    **dict(row or {}),
                    **summary,
                    'name': (row or {}).get('name') or code,
                    'industry': (row or {}).get('industry') or (row or {}).get('sector'),
                    'sector': (row or {}).get('sector') or (row or {}).get('industry'),
                    'market_cap': (row or {}).get('market_cap'),
                    'pe_ratio': (row or {}).get('pe_ratio'),
                    'pb_ratio': (row or {}).get('pb_ratio'),
                }
                breadth_rows.append(enriched)
            if breadth_rows:
                symbol_insights = [dict(item) for item in breadth_rows[:RESEARCH_SYMBOL_DETAIL_LIMIT]]
                scored = []
                for item in breadth_rows:
                    ranked = dict(item)
                    ranked['screen_score'] = self._rank_symbol_context(ranked)
                    scored.append(ranked)
                scored.sort(key=lambda item: (self._safe_float(item.get('screen_score')), self._safe_float(item.get('market_cap'))), reverse=True)
                candidate_universe = [dict(item) for item in scored[:RESEARCH_CANDIDATE_POOL_LIMIT]]
                candidate_codes = [str(item.get('code') or '') for item in candidate_universe if str(item.get('code') or '').strip()]
                factor_snapshot = await self._fetch_factor_snapshot(db, candidate_codes, snapshot)
                for item in candidate_universe[:RESEARCH_FINANCIAL_DETAIL_LIMIT]:
                    if hasattr(db, 'get_financials'):
                        try:
                            financials = await db.get_financials(item['code'], limit=1)
                        except Exception:
                            financials = []
                        item['financial_snapshot'] = dict((financials or [None])[0] or {})
                for item in candidate_universe:
                    item['factor_snapshot'] = dict(factor_snapshot.get(str(item.get('code') or '')) or {})
                    item['screen_score'] = self._rank_symbol_context(item)
                candidate_universe.sort(key=lambda item: (self._safe_float(item.get('screen_score')), self._safe_float(item.get('market_cap'))), reverse=True)

            if task_target_symbols and not symbol_insights and not candidate_universe:
                return self._build_blocked_research_context(
                    snapshot=snapshot,
                    research_task=research_task,
                    parent_strategies=parent_strategies,
                    history_summary=history_summary,
                    universe_total_count=universe_total_count,
                    top_industries=top_industries,
                    cache_reused=False,
                )

            if not symbol_insights and not task_target_symbols:
                for code in DEFAULT_CODES:
                    try:
                        klines = await db.get_klines(code, limit=180)
                    except Exception:
                        klines = []
                    if not klines:
                        continue
                    frame = pd.DataFrame(klines)
                    summary = self._summarize_symbol_frame(code, frame)
                    if summary is not None:
                        symbol_insights.append(summary)
                candidate_universe = [dict(item, screen_score=self._rank_symbol_context(item)) for item in symbol_insights[: min(len(symbol_insights), RESEARCH_CANDIDATE_POOL_LIMIT)]]

            trend_up_count = len([item for item in symbol_insights if item.get('trend_state') == 'uptrend'])
            trend_down_count = len([item for item in symbol_insights if item.get('trend_state') == 'downtrend'])
            avg_return_20d = round(sum(self._safe_float(item.get('return_20d')) for item in symbol_insights) / max(len(symbol_insights), 1), 6) if symbol_insights else 0.0
            avg_volatility_20d = round(sum(self._safe_float(item.get('volatility_20d')) for item in symbol_insights) / max(len(symbol_insights), 1), 6) if symbol_insights else 0.0
            category_counts = dict(snapshot.get('category_counts') or {})
            top_categories = {
                str(key): int(value)
                for key, value in sorted(category_counts.items(), key=lambda item: item[1], reverse=True)[:5]
            }
            scanned_stock_count = len(universe_rows) if universe_rows else len(symbol_insights)
            data_ready_count = len(breadth_rows) if breadth_rows else len(symbol_insights)
            coverage_ratio = round(scanned_stock_count / max(universe_total_count, 1), 6) if universe_total_count else 0.0
            target_context_status = (
                'targeted_active'
                if task_target_symbols
                else ('filtered_market_context' if has_market_filters else 'broad_market_context')
            )
            task_target_context = self._build_task_target_context(
                research_task=research_task,
                symbol_insights=symbol_insights,
                candidate_universe=candidate_universe,
                status=target_context_status,
            )
            market_background_context = self._build_market_background_context(
                symbol_insights=symbol_insights if not task_target_symbols else [],
                candidate_universe=candidate_universe if not task_target_symbols else [],
                universe_scan={
                    'total_stock_count': universe_total_count,
                    'scanned_stock_count': scanned_stock_count,
                    'data_ready_count': data_ready_count,
                    'coverage_ratio': coverage_ratio,
                    'cache_reused': False,
                },
                top_industries=top_industries,
                cache_reused=False,
            )
            return {
                'research_task': {
                    'task_id': research_task.get('task_id'),
                    'theme': research_task.get('theme'),
                    'opportunity_type': research_task.get('opportunity_type'),
                    'focus_industries': task_focus_industries,
                    'focus_markets': task_focus_markets,
                    'target_symbols': task_target_symbols,
                    'priority': research_task.get('priority'),
                    'strategy_preferences': list(research_task.get('strategy_preferences') or []),
                    'generation_limit': research_task.get('generation_limit'),
                    'rationale': research_task.get('rationale'),
                    'task_source': research_task.get('task_source'),
                },
                'market_regime': {
                    'fg_level': snapshot.get('fg_level'),
                    'fear_greed_index': snapshot.get('fear_greed_index'),
                    'hot_sectors': list(snapshot.get('hot_sectors') or [])[:4],
                    'cold_sectors': list(snapshot.get('cold_sectors') or [])[:3],
                    'factor_ic': dict(snapshot.get('factor_ic') or {}),
                    'factor_ic_trend': dict(snapshot.get('factor_ic_trend') or {}),
                    'factor_research': dict(snapshot.get('factor_research') or {}),
                },
                'market_breadth': {
                    'symbol_count': len(symbol_insights),
                    'trend_up_count': trend_up_count,
                    'trend_down_count': trend_down_count,
                    'avg_return_20d': avg_return_20d,
                    'avg_volatility_20d': avg_volatility_20d,
                },
                'symbol_insights': symbol_insights,
                'candidate_universe': candidate_universe,
                'symbol_insight_codes': list(task_target_context.get('symbol_insight_codes') or []),
                'candidate_universe_symbols': list(task_target_context.get('candidate_universe_symbols') or []),
                'target_context_status': target_context_status,
                'blocked_by_target_universe': False,
                'task_target_context': task_target_context,
                'market_background_context': market_background_context,
                'universe_scan': {
                    'total_stock_count': universe_total_count,
                    'scanned_stock_count': scanned_stock_count,
                    'data_ready_count': data_ready_count,
                    'coverage_ratio': coverage_ratio,
                    'detail_symbol_count': len(symbol_insights),
                    'candidate_universe_count': len(candidate_universe),
                    'top_industries': top_industries,
                    'cache_reused': False,
                },
                'selection_framework': {
                    'technical': ['trend_state', 'return_20d', 'return_5d', 'volume_ratio_20', 'price_vs_sma20'],
                    'fundamental': ['market_cap', 'pe_ratio', 'pb_ratio', 'revenue_growth', 'profit_growth'],
                    'factor_names': [
                        str(key)
                        for key in list(
                            ((snapshot.get('factor_research') or {}).get('summary') or {}).get('top_factor_names')
                            or (snapshot.get('factor_research') or {}).get('active_factors')
                            or list((snapshot.get('factor_ic_trend') or {}).keys())[:3]
                        )[:3]
                    ],
                },
                'analysis_scope': {
                    'scan_mode': (
                        'target_context_only'
                        if task_target_symbols
                        else (
                            'filtered_universe_scan_with_focused_detail'
                            if has_market_filters
                            else 'broad_universe_scan_with_focused_detail'
                        )
                    ),
                    'scan_limit': RESEARCH_UNIVERSE_SCAN_LIMIT,
                    'kline_scan_limit': RESEARCH_KLINE_SCAN_LIMIT,
                    'detail_limit': RESEARCH_SYMBOL_DETAIL_LIMIT,
                    'candidate_pool_limit': RESEARCH_CANDIDATE_POOL_LIMIT,
                },
                'population_state': {
                    'listed_count': int(snapshot.get('listed_count') or universe_total_count or 0),
                    'incubating_count': int(snapshot.get('incubating_count') or 0),
                    'top_categories': top_categories,
                },
                'parent_context': [
                    {
                        'id': item.get('id'),
                        'name': item.get('name'),
                        'strategy_type': item.get('strategy_type'),
                        'status': item.get('status'),
                    }
                    for item in list(parent_strategies or [])[:3]
                ],
                'experiment_feedback': [
                    {
                        'generator_type': item.get('generator_type'),
                        'status': item.get('status'),
                        'decision': item.get('decision'),
                        'final_score': item.get('final_score'),
                    }
                    for item in list(history_summary or [])[:4]
                ],
            }

        @staticmethod
        def _summarize_research_context(context: Optional[dict[str, Any]]) -> dict[str, Any]:
            payload = dict(context or {})
            breadth = dict(payload.get('market_breadth') or {})
            regime = dict(payload.get('market_regime') or {})
            universe_scan = dict(payload.get('universe_scan') or {})
            candidate_universe = list(payload.get('candidate_universe') or [])
            task_target_context = dict(payload.get('task_target_context') or {})
            market_background_context = dict(payload.get('market_background_context') or {})
            blocked_by_target_universe = bool(payload.get('blocked_by_target_universe'))
            task_targeted = bool(task_target_context.get('targeted_task'))
            context_mode = (
                'blocked_target_context'
                if blocked_by_target_universe
                else ('target_only' if task_targeted else 'broad_market')
            )
            return {
                'context_mode': context_mode,
                'symbol_count': int(breadth.get('symbol_count') or 0),
                'trend_up_count': int(breadth.get('trend_up_count') or 0),
                'trend_down_count': int(breadth.get('trend_down_count') or 0),
                'avg_return_20d': breadth.get('avg_return_20d'),
                'avg_volatility_20d': breadth.get('avg_volatility_20d'),
                'candidate_universe_count': len(candidate_universe),
                'candidate_codes': [str((item or {}).get('code') or '') for item in candidate_universe[:5]],
                'universe_total_count': int(universe_scan.get('total_stock_count') or 0),
                'universe_scanned_count': int(universe_scan.get('scanned_stock_count') or 0),
                'data_ready_count': int(universe_scan.get('data_ready_count') or 0),
                'coverage_ratio': universe_scan.get('coverage_ratio'),
                'cache_reused': bool(universe_scan.get('cache_reused')),
                'fg_level': regime.get('fg_level'),
                'fear_greed_index': regime.get('fear_greed_index'),
                'hot_sectors': list(regime.get('hot_sectors') or [])[:3],
                'cold_sectors': list(regime.get('cold_sectors') or [])[:2],
                'target_context_status': payload.get('target_context_status'),
                'blocked_by_target_universe': blocked_by_target_universe,
                'task_targeted': task_targeted,
                'task_target_symbol_count': len(list(task_target_context.get('requested_target_symbols') or [])),
                'task_target_matched_count': len(list(task_target_context.get('matched_target_symbols') or [])),
                'target_context_symbol_count': int(task_target_context.get('symbol_count') or 0),
                'target_context_candidate_count': int(task_target_context.get('candidate_universe_count') or 0),
                'market_background_available': bool(market_background_context.get('available')),
                'market_background_symbol_count': int(market_background_context.get('symbol_count') or 0),
                'market_background_candidate_count': int(market_background_context.get('candidate_universe_count') or 0),
            }

