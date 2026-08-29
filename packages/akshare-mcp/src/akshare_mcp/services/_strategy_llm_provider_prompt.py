"""外部 AI 策略生成 provider。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import pandas as pd
from strategy_factory.api.semantic_contract import apply_target_symbol_policy, normalize_research_task_contract

from ..env_loader import load_mcp_env


class StrategyLLMRequestError(RuntimeError):
    def __init__(self, message: str, *, metrics: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.metrics = dict(metrics or {})


@dataclass
class StrategyLLMConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_sec: float = 30.0
    connect_timeout_sec: float = 8.0
    write_timeout_sec: float = 10.0
    pool_timeout_sec: float = 5.0
    temperature: float = 0.3
    max_tokens: int = 900
    retry_count: int = 2
    retry_backoff_sec: float = 1.0
    initial_compact_level: int = 0
    # 与 public/runtime 口径对齐:单次抖动不应把整轮 LLM 锁死。
    recent_timeout_minimal_streak: int = 3
    recent_timeout_cooldown_sec: float = 120.0
    max_concurrency: int = 3
    strict: bool = False

    @classmethod
    def from_env(cls) -> "StrategyLLMConfig":
        load_mcp_env(override=False, only_prefixes=('STRATEGY_LLM_',))
        enabled = str(os.getenv("STRATEGY_LLM_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}
        timeout_sec = float(os.getenv("STRATEGY_LLM_TIMEOUT_SEC", "30") or 30)
        initial_compact_level = max(0, min(2, int(os.getenv("STRATEGY_LLM_INITIAL_COMPACT_LEVEL", "0") or 0)))
        recent_timeout_minimal_streak = max(1, min(8, int(os.getenv("STRATEGY_LLM_RECENT_TIMEOUT_MINIMAL_STREAK", "3") or 3)))
        recent_timeout_cooldown_sec = max(0.0, float(os.getenv("STRATEGY_LLM_RECENT_TIMEOUT_COOLDOWN_SEC", "120") or 120))
        return cls(
            enabled=enabled,
            provider=str(os.getenv("STRATEGY_LLM_PROVIDER", "openai_compatible") or "openai_compatible"),
            base_url=str(os.getenv("STRATEGY_LLM_BASE_URL", "") or "").strip(),
            api_key=str(os.getenv("STRATEGY_LLM_API_KEY", "") or "").strip(),
            model=str(os.getenv("STRATEGY_LLM_MODEL", "") or "").strip(),
            timeout_sec=timeout_sec,
            connect_timeout_sec=float(os.getenv("STRATEGY_LLM_CONNECT_TIMEOUT_SEC", str(min(timeout_sec, 8.0))) or min(timeout_sec, 8.0)),
            write_timeout_sec=float(os.getenv("STRATEGY_LLM_WRITE_TIMEOUT_SEC", str(min(timeout_sec, 10.0))) or min(timeout_sec, 10.0)),
            pool_timeout_sec=float(os.getenv("STRATEGY_LLM_POOL_TIMEOUT_SEC", str(min(timeout_sec, 5.0))) or min(timeout_sec, 5.0)),
            temperature=float(os.getenv("STRATEGY_LLM_TEMPERATURE", "0.3") or 0.3),
            max_tokens=max(128, int(os.getenv("STRATEGY_LLM_MAX_TOKENS", "900") or 900)),
            retry_count=max(0, int(os.getenv("STRATEGY_LLM_RETRY_COUNT", "2") or 2)),
            retry_backoff_sec=max(0.0, float(os.getenv("STRATEGY_LLM_RETRY_BACKOFF_SEC", "1.0") or 1.0)),
            initial_compact_level=initial_compact_level,
            recent_timeout_minimal_streak=recent_timeout_minimal_streak,
            recent_timeout_cooldown_sec=recent_timeout_cooldown_sec,
            max_concurrency=max(1, min(16, int(os.getenv("STRATEGY_LLM_MAX_CONCURRENCY", "3") or 3))),
            strict=str(os.getenv("STRATEGY_LLM_STRICT_MODE", "")).strip().lower() in {"1", "true", "yes", "on"},
        )


def _family_hypothesis_requirements() -> dict[str, dict[str, Any]]:
    return {
        "momentum": {
            "required_fields": [
                "trend_persistence_logic",
                "failure_scenario",
                "false_breakout_filter",
            ],
            "note": "必须说明趋势持续逻辑、失效情景，以及如何过滤假突破。",
        },
        "quality_factor": {
            "required_fields": [
                "quality_metrics",
                "holding_consistency_explanation",
                "quality_drift_detection",
            ],
            "note": "必须说明核心质量指标、持有期为何与质量扩散一致，以及如何识别质量漂移。",
        },
        "ma_cross": {
            "required_fields": [
                "trend_noise_separation",
                "range_filter",
                "volume_confirmation",
            ],
            "note": "必须说明如何区分趋势与噪声、如何过滤横盘，以及如何做量能确认。",
        },
    }


class _StrategyLLMProviderPromptMixin:
        @classmethod
        def _build_prompt(
            cls,
            snapshot: dict[str, Any],
            market_summary: dict[str, Any],
            research_context: Optional[dict[str, Any]],
            parent_strategies: list[dict[str, Any]],
            history_summary: list[dict[str, Any]],
            limit: int,
            research_task: Optional[dict[str, Any]] = None,
            compact_level: int = 0,
        ) -> tuple[str, str]:
            requested_limit = cls._normalize_limit(limit)
            profile_name = cls._prompt_profile_name(compact_level)
            normalized_task = normalize_research_task_contract(research_task)
            target_alignment_contract = dict(normalized_task.get('target_alignment_contract') or {})
            prompt_target_symbol_rule = cls._prompt_target_symbol_rule(normalized_task)
            compact_market_summary = cls._compact_market_summary(market_summary, compact_level=compact_level)
            compact_research_context = cls._compact_research_context(research_context, compact_level=compact_level)
            compact_task = cls._compact_research_task(research_task, compact_level=compact_level)
            task_target_context = dict(compact_research_context.get('task_target_context') or {})
            market_background_context = dict(compact_research_context.get('market_background_context') or {})
            targeted_context_only = bool(task_target_context.get('targeted_task') or compact_task.get('target_symbols'))
            if targeted_context_only and not task_target_context.get('requested_target_symbols'):
                task_target_context = {
                    **task_target_context,
                    'targeted_task': True,
                    'requested_target_symbols': list(compact_task.get('target_symbols') or []),
                    'matched_target_symbols': cls._normalize_code_list([
                        task_target_context.get('matched_target_symbols'),
                        compact_research_context.get('candidate_universe_symbols'),
                        compact_research_context.get('symbol_insight_codes'),
                        compact_task.get('target_symbols'),
                    ], limit=6),
                }
            prompt_research_context: dict[str, Any]
            if targeted_context_only:
                prompt_research_context = {
                    'target_context_status': compact_research_context.get('target_context_status'),
                    'blocked_by_target_universe': compact_research_context.get('blocked_by_target_universe'),
                    'task_target_context': {
                        key: value
                        for key, value in task_target_context.items()
                        if value not in (None, [], {}, "")
                    },
                }
            else:
                prompt_research_context = {
                    key: value
                    for key, value in compact_research_context.items()
                    if value not in (None, [], {}, "")
                }
            strategy_context_block = dict(
                prompt_research_context.get('strategy_context')
                or compact_research_context.get('strategy_context')
                or {}
            )
            structured_research_context = any(
                prompt_research_context.get(key) not in (None, [], {}, "")
                for key in ('strategy_context', 'backtest_summary', 'regime_panel', 'capacity_panel', 'generalization_seed')
            )
            futures_instrument_profile = dict(strategy_context_block.get('instrument_profile') or {})
            objective_profile = str(
                normalized_task.get('objective_profile')
                or strategy_context_block.get('objective_profile')
                or prompt_research_context.get('objective_profile')
                or compact_research_context.get('objective_profile')
                or ''
            ).strip().lower()
            has_futures_research_context = (
                str(futures_instrument_profile.get('asset_class') or '').strip().lower() == 'futures'
            )
            high_precision_requested = bool(
                objective_profile == 'high_precision' or has_futures_research_context
            )
            candidate_domain_label = '期货跨月/日频' if has_futures_research_context else '股票日频'
            structured_context_rule = (
                '如果 research_context 提供了 strategy_context,backtest_summary,regime_panel,capacity_panel,generalization_seed，'
                '必须把这些结构化研究块当作 primary evidence 使用，不能忽略，也不能被宽泛市场背景覆盖。'
                if structured_research_context
                else ''
            )
            futures_contract_rule = (
                '如果 research_context.strategy_context.instrument_profile.asset_class=futures，'
                'candidate 必须显式提供 instrument_profile，并保留 asset_class,underlying,curve_legs,roll_rule；'
                'portfolio_spec.position_assumption 只能使用 paired_futures_spread 或 single_futures_directional；'
                'execution_assumptions 必须额外包含 margin_rate,contract_multiplier,liquidity_bucket,max_contracts_per_rebalance；'
                '如 DSL 信号基于价差或曲线投影序列，允许把该序列映射到 close，但必须在 dsl.metadata 中写明 signal_reference_series 与 trade_leg_definition。'
                if has_futures_research_context
                else ''
            )
            high_precision_rule = (
                '当前任务追求 high_precision，高优先级候选必须低频、条件收窄、显式写出 preferred_regime 与 avoid_regime，'
                '并在 hypothesis_artifact / validation_profile 中包含 objective_profile=high_precision,trade_density_preference,entry_selectivity,regime_required,cost_robust_required；'
                '对“常驻市场、泛信号、无失败模式、无成本敏感性”的候选直接降权。'
                if high_precision_requested
                else ''
            )
            max_target_symbols = max(
                1,
                int(
                    target_alignment_contract.get('max_candidate_target_symbols')
                    or max(1, min(len(list(normalized_task.get('target_symbols') or [])) or 5, 8))
                ),
            )
            min_target_coverage_ratio = float(target_alignment_contract.get('min_coverage_ratio') or 0.0)
            min_target_intersection_ratio = float(target_alignment_contract.get('min_intersection_ratio') or 0.0)
            min_target_overlap_count = int(target_alignment_contract.get('min_required_overlap_count') or 0)
            strict_snapshot_target_pool = (
                str(normalized_task.get('task_source') or '').strip().lower() == 'snapshot'
                and bool(target_alignment_contract.get('strict_target_subset_required'))
                and bool(normalized_task.get('target_symbols'))
            )
            disallow_market_fallback = strict_snapshot_target_pool and not target_alignment_contract.get('market_fallback_allowed', True)
            focus_strategy_families = list(target_alignment_contract.get('focus_strategy_families') or [])
            family_hypothesis_requirements = _family_hypothesis_requirements()
            strict_snapshot_rule = ''
            if strict_snapshot_target_pool:
                strict_snapshot_rule = (
                    f'这是定向 target pool 任务，target_symbols 只能从 research_task.target_symbols 中选择，'
                    f'不得扩展到 candidate_universe 或全市场；候选至少要覆盖 {max(1, min_target_overlap_count)} 只 research_task 目标，'
                    f'且 target_symbols 不得超过 {max_target_symbols} 只。'
                )
            target_context_only_rule = (
                '这是定向任务，只能使用 research_context.task_target_context 中的标的证据；'
                '不得引用 market_background_context，也不得借 broad candidate_universe 扩展 target_symbols；'
                '不允许退回 candidate_universe。'
                if targeted_context_only
                else ''
            )
            explicit_same_theme_rule = (
                '如果 research_task 显式提供 same_theme_symbols 或 theme_members，只能把它们视为 research_task 自带的辅助 target context 使用。'
                if any(normalized_task.get(key) for key in ('same_theme_symbols', 'theme_members'))
                else ''
            )

            if compact_level >= 2:
                example_symbols = cls._normalize_code_list([
                    compact_task.get('target_symbols'),
                    task_target_context.get('candidate_universe_symbols') if targeted_context_only else market_background_context.get('candidate_universe_symbols'),
                    task_target_context.get('symbol_insight_codes') if targeted_context_only else market_background_context.get('symbol_insight_codes'),
                    compact_research_context.get('candidate_universe_symbols'),
                    compact_research_context.get('symbol_insight_codes'),
                ], limit=2)
                if not example_symbols:
                    example_symbols = cls._normalize_code_list((research_task or {}).get('target_symbols'), limit=2)
                required_contract_fields = [
                    'holding_horizon',
                    'trade_plan',
                    'risk_rules',
                    'position_sizing',
                    'execution_notes',
                    'rebalance_rule',
                    'portfolio_spec',
                    'execution_assumptions',
                    'validation_profile',
                ]
                compact_candidate_limit = 1 if requested_limit <= 1 else min(requested_limit, 2)
                output_contract = {
                    'root': 'json_object',
                    'required': ['candidates'],
                    'analysis_fields': [],
                    'candidate_fields': ['name', 'strategy_type', 'generator_mode', 'hypothesis', 'hypothesis_artifact', 'holding_horizon', 'evidence_chain', 'prediction_contract', 'trade_prediction_contract', 'confidence_contract', 'trade_plan', 'risk_rules', 'position_sizing', 'execution_notes', 'rebalance_rule', 'portfolio_spec', 'execution_assumptions', 'validation_profile', 'target_symbols', 'stock_pool', 'dsl', 'tags'],
                    'required_candidate_fields': ['name', 'strategy_type', 'hypothesis_artifact', 'evidence_chain', 'prediction_contract', 'trade_prediction_contract', 'target_symbols', 'stock_pool', 'dsl', *required_contract_fields],
                    'hypothesis_required_fields': ['alpha_hypothesis', 'failure_mode', 'target_universe_hypothesis', 'family_hint', 'holding_rationale', 'alpha_half_life', 'cost_sensitivity_grid', 'position_model', 'capacity_assumption', 'market_regime_assumption', 'validation_focus'],
                    'family_hypothesis_requirements': family_hypothesis_requirements,
                    'dsl_required_fields': ['version', 'timeframe', 'entry', 'exit', 'metadata'],
                    'contract_required_fields': required_contract_fields,
                    'trade_prediction_contract_required_fields': ['stock_code', 'prediction_as_of', 'target_trading_date', 'direction', 'confidence', 'horizon', 'evidence_refs'],
                    'target_symbol_rule': prompt_target_symbol_rule,
                    'target_alignment_contract': {
                        'max_target_symbols': max_target_symbols,
                        'min_target_coverage_ratio': round(min_target_coverage_ratio, 4),
                        'min_target_intersection_ratio': round(min_target_intersection_ratio, 4),
                        'min_target_overlap_count': max(0, min_target_overlap_count),
                        'disallow_market_fallback': disallow_market_fallback,
                    },
                    'prefer_single_high_confidence_candidate': True,
                    'candidate_limit': compact_candidate_limit,
                    'generation_mode': 'hypothesis_first_lowering_with_optional_open_dsl',
                }
                system_prompt = ''.join([
                    '你是量化策略助手，只返回严格 JSON。',
                    f'先构造 hypothesis_artifact，再把 hypothesis lower 成最多 {compact_candidate_limit} 个可执行{candidate_domain_label} DSL candidate。',
                    '允许最多 1 个 candidate 走 open DSL 直出模式；只有在 hypothesis 与经济语义已经完整时才允许这样做。',
                    '如果 research_task 提供了 event_id/theme_code/direction/evidence_summary，必须围绕该事件证据输出。',
                    '不要 analysis，不要解释，不要 markdown。',
                    '返回根对象 {"candidates":[...]}。',
                    'candidate 必须包含 name,strategy_type,generator_mode,hypothesis,hypothesis_artifact,holding_horizon,evidence_chain,prediction_contract,trade_plan,risk_rules,position_sizing,execution_notes,rebalance_rule,portfolio_spec,execution_assumptions,validation_profile,target_symbols,stock_pool,dsl,tags。',
                    'hypothesis_artifact 必须包含 alpha_hypothesis,failure_mode,target_universe_hypothesis,family_hint,holding_rationale,alpha_half_life,cost_sensitivity_grid,position_model,capacity_assumption,market_regime_assumption,validation_focus。',
                    '生成顺序必须固定为 evidence_chain -> prediction_contract -> trade_plan -> dsl；不要跳过证据链直接写 trade_plan 或 DSL。',
                    'evidence_chain.evidences[] 必须先列出支持该策略的结构化证据，每条证据都要有 evidence_id,source_type,direction,summary；如果证据只是代理信号，必须显式标记 proxy_only。',
                    'prediction_contract.claims[] 必须引用 evidence_ids，并且每条 claim 都必须包含 failure_condition；若 claim 同时引用正反方向证据，必须提供 conflict_resolution_rule。',
                    'trade_plan 的每个 entry/exit/step 都必须带 node_id 与 claim_ids；单标的趋势策略的 regime 描述必须量化，不能裸写“明显震荡/高波动/趋势较强”这类模糊词。',
                    '如果 strategy_type 是 momentum / quality_factor / ma_cross，hypothesis_artifact 还必须包含 family_specific_hypothesis，并满足 family_hypothesis_requirements。',
                    '先保证 hypothesis_artifact 的经济含义完整，再输出 candidate；不要把自己退化成 DSL 填空器。',
                    '如果走 open DSL 直出模式，generator_mode 必须是 llm_defined，tags 必须包含 open_dsl 和 llm_defined；且 holding_horizon,trade_plan,risk_rules,position_sizing,rebalance_rule,portfolio_spec,execution_assumptions,validation_profile,holding_rationale,cost_sensitivity_grid,position_model,capacity_assumption,market_regime_assumption 全都不能缺。',
                    'portfolio_spec / execution_assumptions / validation_profile 必须给出完整对象，不得省略，也不得依赖系统回填默认值。',
                    'validation_profile 必须使用工厂标准口径，不要自造 profile 名称或 layer；target-only / candidate_target_only 任务默认应回到 trade_rule_validation + target 这一类 canonical 合同。',
                    'trade_prediction_contract must be a machine-verifiable object, not prose. Required fields: stock_code,prediction_as_of,target_trading_date,direction,confidence,horizon,evidence_refs. direction must be up|down|neutral; confidence must be 0-1; evidence_refs must reference evidence_chain.evidences[].evidence_id.',
                    structured_context_rule,
                    futures_contract_rule,
                    high_precision_rule,
                    target_context_only_rule,
                    explicit_same_theme_rule,
                    'dsl 必须是对象，且必须包含 version,timeframe,entry,exit,metadata。',
                    'dsl.metadata 必须回填 target_symbols,stock_pool,portfolio_spec,execution_assumptions,validation_profile,targeting_policy,constraint_check；期货上下文下还必须回填 instrument_profile。',
                    strict_snapshot_rule,
                    '字段仅限 open/high/low/close/volume；指标优先使用 sma,ema,roc,rsi,volume_ratio,adx,turnover_rate,upper_shadow_ratio,rolling_count,slope；',
                    '条件运算仅限 gt,gte,lt,lte,cross_above,cross_below；组合仅限 all,any,not。',
                    '不要使用 highest/lowest/atr/stddev，也不要写 close 与 highest/lowest 的交叉突破。',
                    'volume_ratio 右侧优先用 value≈1.0；rsi 右侧优先用 value 40/60；不要把 volume_ratio/rsi/roc 直接和 open/high/low/close/volume 比较。',
                ])
                user_payload = {
                    'task': 'generate_compact_hypotheses_then_candidates',
                    'prompt_profile': profile_name,
                    'limit': compact_candidate_limit,
                    'research_task': compact_task,
                    'research_context': prompt_research_context,
                    'market_hint': dict(compact_research_context.get('market_regime') or {}) if not targeted_context_only else {},
                    'output_contract': output_contract,
                    'output_example': cls._minimal_output_example(example_symbols),
                }
                if not user_payload['market_hint']:
                    user_payload.pop('market_hint', None)
                if not user_payload['research_context']:
                    user_payload.pop('research_context', None)
                if not user_payload['research_task']:
                    user_payload.pop('research_task', None)
                user_prompt = json.dumps(user_payload, ensure_ascii=False, default=str, separators=(',', ':'))
                return system_prompt, user_prompt

            analysis_fields = ['market_regime', 'style_bias', 'hypothesis', 'evidence', 'risk_focus', 'selection_notes', 'universe_view', 'selection_plan', 'trade_plan']
            analysis_length_rule = 'analysis 每个字段必须短：字符串不超过 60 个字，列表最多 2 项，不要复述输入。' if compact_level >= 1 else 'analysis 需要结构化且基于输入证据。'
            candidate_priority_rule = '优先返回 1 个高置信、可执行候选；不要为了凑数量返回弱候选。' if compact_level >= 1 else '按 limit 返回高质量候选。'
            if strict_snapshot_target_pool:
                context_rule = strict_snapshot_rule
            elif targeted_context_only:
                context_rule = (
                    'target_symbols 必须来自 research_context.task_target_context.requested_target_symbols '
                    '或 matched_target_symbols；如果 task_target_context 无法建立，不允许生成候选。'
                )
            elif prompt_target_symbol_rule == 'strict_intersection_with_research_task':
                context_rule = (
                    '如果 research_task.target_symbols 与 candidate_universe 有交集，'
                    'target_symbols 必须只取交集；如果没有交集，不允许退回 candidate_universe。'
                    '只有在 research_task 显式提供 same_theme_symbols 或 theme_members 时，'
                    '才允许只在该同主题集合内补充候选。'
                )
            else:
                context_rule = '如果 research_task.target_symbols 与 candidate_universe 有交集，target_symbols 必须只取交集；如果没有交集，才允许退回 candidate_universe。'
            if target_context_only_rule:
                context_rule = ''.join([target_context_only_rule, explicit_same_theme_rule, context_rule])
            event_rule = '如果 research_task 提供 event_id/theme_code/direction/evidence_summary，必须优先围绕该事件主题、方向和证据构建候选。'
            system_prompt = ''.join([
                'CRITICAL OUTPUT SHAPE: return exactly one JSON object with only two top-level keys: analysis and candidates. Do not put analysis subfields at the root. Do not return markdown.',
                '你是量化策略研究员。必须输出严格 JSON，不要输出解释文本。',
                f'先基于输入的市场研究上下文给出结构化 analysis，并先形成 hypothesis_artifact，再 lower 为可执行的{candidate_domain_label}策略 DSL 候选。',
                (
                    '你拿到的是程序整理后的定向研究上下文，必须只使用 research_context.task_target_context 中的真实标的证据。'
                    if targeted_context_only
                    else (
                        '你拿到的是程序整理后的期货跨月研究上下文，必须优先使用 strategy_context/backtest_summary/regime_panel/capacity_panel/generalization_seed 中的真实研究证据。'
                        if has_futures_research_context
                        else '你拿到的是程序从股票数据库扫描、聚合、压缩后的研究上下文，必须优先使用 market_background_context / candidate_universe 中的真实股票数据。'
                    )
                ),
                '每个候选策略必须明确目标标的或标的池，不允许只给抽象模板。',
                '如果提供了 research_task，必须围绕该任务的市场机会、行业或目标股票池生成候选，而不是泛化输出。',
                context_rule,
                event_rule,
                'trade_prediction_contract must be a machine-verifiable object, not prose. Required fields: stock_code,prediction_as_of,target_trading_date,direction,confidence,horizon,evidence_refs. direction must be up|down|neutral; confidence must be 0-1; evidence_refs must reference evidence_chain.evidences[].evidence_id.',
                structured_context_rule,
                futures_contract_rule,
                high_precision_rule,
                '允许字段: open/high/low/close/volume。',
                '允许指标: sma, ema, roc, rsi, stddev, zscore, highest, lowest, volume_ratio, atr, adx, turnover_rate, upper_shadow_ratio, rolling_count, slope。',
                '允许条件运算: gt, gte, lt, lte, eq, ne, cross_above, cross_below。',
                '允许组合: all, any, not。',
                '优先生成可中等频率触发的策略：最近一年通常至少 1-6 次完整交易，不要只有单边长期持有。',
                'entry/exit 各自尽量不超过 2-3 个子条件，避免过度稀疏和过拟合。',
                '窗口优先 3-30 日；volume_ratio 阈值优先 0.95-1.10；ROC 阈值绝对值优先 0.3%-3%；RSI 优先 35/65 或 40/60 一类稳健区间。',
                    '必须提供明确 exit 规则，并兼顾趋势延续或回撤退出。',
                    analysis_length_rule,
                    candidate_priority_rule,
                    f"analysis 必须包含: {', '.join(analysis_fields)}。",
                    '根对象只允许包含 analysis 与 candidates；market_regime/style_bias/hypothesis/evidence/risk_focus/selection_notes/universe_view/selection_plan/trade_plan 都必须放在 analysis 对象内部，不能出现在根对象。',
                '每个 candidate 必须包含: name, description, rationale, strategy_type, generator_mode, hypothesis, hypothesis_artifact, holding_horizon, evidence_chain, prediction_contract, trade_plan, risk_rules, position_sizing, execution_notes, rebalance_rule, portfolio_spec, execution_assumptions, validation_profile, target_symbols, stock_pool, selection_logic, dsl, tags。',
                '生成顺序固定为 evidence_chain -> prediction_contract -> trade_plan -> dsl，不允许只给 trade_plan 或 DSL 而省略前两层合同。',
                'evidence_chain.evidences[] 至少包含 evidence_id,source_type,direction,summary；prediction_contract.claims[] 至少包含 claim_id,evidence_ids,expected_move,failure_condition；若 claim 出现相反方向证据，必须包含 conflict_resolution_rule。',
                'trade_plan 的每个 entry/exit/step 都必须包含 node_id 与 claim_ids；regime 相关描述必须量化，不能直接写“明显震荡/趋势较强/高波动”这类模糊词。',
                'hypothesis_artifact 必须包含: alpha_hypothesis, failure_mode, target_universe_hypothesis, family_hint, holding_rationale, alpha_half_life, cost_sensitivity_grid, position_model, capacity_assumption, market_regime_assumption, validation_focus。',
                '如果 strategy_type 是 momentum / quality_factor / ma_cross，hypothesis_artifact 还必须包含 family_specific_hypothesis，并满足 family_hypothesis_requirements。',
                '如果直接输出 open DSL candidate，generator_mode 必须是 llm_defined，tags 必须包含 open_dsl,llm_defined；并且完整给出 holding_horizon,trade_plan,risk_rules,position_sizing,rebalance_rule,portfolio_spec,execution_assumptions,validation_profile,holding_rationale,cost_sensitivity_grid,position_model,capacity_assumption,market_regime_assumption。',
                'holding_horizon / trade_plan / risk_rules / position_sizing / rebalance_rule / portfolio_spec / execution_assumptions / validation_profile 必须是完整对象，不得留空，不得依赖系统回填默认值。',
                'portfolio_spec 必须至少包含 position_assumption,target_weight_scheme；execution_assumptions 必须至少包含 commission_rate,slippage_bps,tradability_filter,slippage_model；validation_profile 必须至少包含 profile,validation_focus,primary_validation_layer。',
                'validation_profile 必须使用工厂标准口径，不要自造 profile 名称或 layer；target-only / candidate_target_only 任务默认应回到 trade_rule_validation + target 这一类 canonical 合同。',
                'open DSL 候选的 dsl 优先使用标准 entry/exit 结构；如果使用 signals.entry/signals.exit，也必须保持 {"op":"all|any","conditions":[...]} 的对象格式。',
                'DSL 条件节点必须使用标准对象格式 {"op":...,"left":...,"right":...}，不要使用 {"gt":[...]} 这类简写。',
                f'target_symbols 数量建议 1-{max_target_symbols} 只；stock_pool 必须包含 selection_mode 与 symbols；dsl.metadata 必须回填 target_symbols,stock_pool,portfolio_spec,execution_assumptions,validation_profile,targeting_policy,constraint_check；期货上下文下还必须回填 instrument_profile。',
                '不要生成 Python 代码，不要生成自然语言规则，只能生成 JSON DSL。',
            ])
            output_contract = {
                'root': 'json_object',
                'required': ['analysis', 'candidates'],
                'root_schema': {
                    'analysis': {field: 'string|array|object' for field in analysis_fields},
                    'candidates': ['candidate_object'],
                },
                'invalid_root_examples': [
                    {
                        'market_regime': 'wrong: belongs under analysis',
                        'hypothesis': 'wrong: belongs under analysis or candidate',
                        'trade_plan': 'wrong: belongs under candidate or analysis, never root',
                    },
                ],
                'valid_root_example': {
                    'analysis': {
                        'market_regime': 'short regime summary',
                        'hypothesis': 'short portfolio-level hypothesis',
                        'selection_plan': ['short plan item'],
                    },
                    'candidates': [
                        {
                            'name': 'candidate name',
                            'strategy_type': 'ma_cross',
                            'generator_mode': 'llm_defined',
                            'target_symbols': ['600000'],
                            'dsl': {'version': '1.0', 'timeframe': 'daily', 'entry': {}, 'exit': {}, 'metadata': {}},
                        },
                    ],
                },
                'analysis_fields': analysis_fields,
                'required_candidate_fields': ['name', 'strategy_type', 'generator_mode', 'hypothesis', 'hypothesis_artifact', 'holding_horizon', 'evidence_chain', 'prediction_contract', 'trade_prediction_contract', 'trade_plan', 'risk_rules', 'position_sizing', 'execution_notes', 'rebalance_rule', 'portfolio_spec', 'execution_assumptions', 'validation_profile', 'target_symbols', 'stock_pool', 'dsl'],
                'trade_prediction_contract_required_fields': ['stock_code', 'prediction_as_of', 'target_trading_date', 'direction', 'confidence', 'horizon', 'evidence_refs'],
                'target_symbol_rule': prompt_target_symbol_rule,
                'target_alignment_contract': {
                    'max_target_symbols': max_target_symbols,
                    'min_target_coverage_ratio': round(min_target_coverage_ratio, 4),
                    'min_target_intersection_ratio': round(min_target_intersection_ratio, 4),
                    'min_target_overlap_count': max(0, min_target_overlap_count),
                    'disallow_market_fallback': disallow_market_fallback,
                    'focus_strategy_families': focus_strategy_families[:4],
                },
                'family_hypothesis_requirements': family_hypothesis_requirements,
                'prefer_single_high_confidence_candidate': compact_level >= 1,
                'candidate_fields': ['name', 'description', 'rationale', 'strategy_type', 'generator_mode', 'hypothesis', 'hypothesis_artifact', 'holding_horizon', 'evidence_chain', 'prediction_contract', 'trade_prediction_contract', 'trade_plan', 'risk_rules', 'position_sizing', 'execution_notes', 'rebalance_rule', 'portfolio_spec', 'execution_assumptions', 'validation_profile', 'target_symbols', 'stock_pool', 'selection_logic', 'dsl', 'tags'],
                'hypothesis_required_fields': ['alpha_hypothesis', 'failure_mode', 'target_universe_hypothesis', 'family_hint', 'holding_rationale', 'alpha_half_life', 'cost_sensitivity_grid', 'position_model', 'capacity_assumption', 'market_regime_assumption', 'validation_focus'],
                'task_alignment': ['research_task.theme', 'research_task.opportunity_type', 'research_task.target_symbols', 'research_task.preferred_strategy_types', 'research_task.validation_focus'],
                'max_selection_logic_items': 2 if compact_level >= 1 else 3,
                'max_conditions_per_side': 3,
                'analysis_max_items': 2 if compact_level >= 1 else 4,
            }
            user_payload = {
                'task': 'generate_stock_daily_dsl_candidates',
                'prompt_profile': profile_name,
                'limit': requested_limit,
                'snapshot': cls._compact_snapshot(snapshot or {}, compact_level=compact_level),
                'market_summary': compact_market_summary,
                'research_context': prompt_research_context,
                'research_task': compact_task,
                'output_contract': output_contract,
            }
            compact_parents = cls._compact_parent_strategies(parent_strategies, compact_level=compact_level)
            compact_history = cls._compact_history_summary(history_summary, compact_level=compact_level)
            if compact_parents:
                user_payload['parent_strategies'] = compact_parents
            if compact_history:
                user_payload['recent_experiments'] = compact_history
            user_prompt = json.dumps(user_payload, ensure_ascii=False, default=str, separators=(',', ':'))
            return system_prompt, user_prompt
