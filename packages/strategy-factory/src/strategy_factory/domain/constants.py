"""策略工厂常量与配置"""

import math
import os
from typing import Dict, List, Optional

from ..infrastructure.env_loader import load_strategy_llm_env


load_strategy_llm_env()

# 回测用代表性股票（大盘/中盘/小盘各覆盖）
# 可通过环境变量 REPRESENTATIVE_STOCKS 覆盖，逗号分隔，如 "600519,000858,601318"
_DEFAULT_REPRESENTATIVE_STOCKS = [
    "300750", "000333", "002415", "601318", "600276",
    "601012", "000001", "600036", "600519", "000858",
]
_env_stocks = os.environ.get("REPRESENTATIVE_STOCKS", "").strip()
REPRESENTATIVE_STOCKS: List[str] = (
    [c.strip() for c in _env_stocks.split(",") if c.strip()]
    if _env_stocks else list(_DEFAULT_REPRESENTATIVE_STOCKS)
)

# 前端分类最低配额
CATEGORY_MINIMUMS = {
    "momentum": 3, "ma_cross": 3, "rsi": 3,
    "value_factor": 3, "quality_factor": 3, "growth_factor": 3,
    "multi_factor": 3, "macro_timing": 3,
    "volatility_breakout": 1, "event_structure_breakout": 1, "gap_fill": 1, "mean_reversion_short": 1,
    "sector_rotation": 1, "north_capital_track": 1, "margin_divergence": 1,
}

# 策略工厂默认种子/回退因子集合。
FACTORY_RESEARCH_SEED_FACTORS: List[str] = ["momentum", "value", "quality", "growth", "volatility", "reversal"]
FACTORY_RESEARCH_FALLBACK_FACTORS: List[str] = list(FACTORY_RESEARCH_SEED_FACTORS)
# 兼容旧调用点：逐步退化为 seed/fallback 集合，而非唯一主链输入。
FACTORY_RESEARCH_FACTORS: List[str] = list(FACTORY_RESEARCH_SEED_FACTORS)

# 优化 3：按因子类型设置差异化过期阈值（交易日计）
FACTOR_STALE_DAYS_BY_TYPE: Dict[str, int] = {
    "momentum": 2,
    "reversal": 2,
    "capital_flow": 2,
    "sentiment": 3,
    "volatility": 3,
    "breakout_structure": 3,
    "turnover_dynamics": 3,
    "growth": 5,
    "quality": 5,
    "value": 7,
    "multi_factor": 5,
    "liquidity": 4,
    "event": 3,
}
FACTOR_STALE_DAYS_DEFAULT: int = int(os.environ.get("STRATEGY_FACTORY_FACTOR_STALE_DAYS_DEFAULT", "2") or 2)

# 优化 4：因子拥挤度检测阈值
FACTOR_CONCENTRATION_COOLDOWN_RATIO: float = float(os.environ.get("STRATEGY_FACTORY_FACTOR_CONCENTRATION_COOLDOWN_RATIO", "0.30") or 0.30)
FACTOR_CONCENTRATION_FREEZE_RATIO: float = float(os.environ.get("STRATEGY_FACTORY_FACTOR_CONCENTRATION_FREEZE_RATIO", "0.45") or 0.45)

# 优化 6：因子→策略映射覆盖（JSON 格式环境变量）
import json as _json
import logging

logger = logging.getLogger(__name__)

_FACTOR_MAPPING_OVERRIDES_RAW = os.environ.get("STRATEGY_FACTORY_FACTOR_MAPPING_OVERRIDES", "").strip()
FACTOR_STRATEGY_MAPPING_OVERRIDES: Dict[str, tuple[str, ...]] = {}
if _FACTOR_MAPPING_OVERRIDES_RAW:
    try:
        _parsed = _json.loads(_FACTOR_MAPPING_OVERRIDES_RAW)
        FACTOR_STRATEGY_MAPPING_OVERRIDES = {
            str(k): tuple(str(v) for v in vs) for k, vs in _parsed.items()
        }
    except Exception as _swallowed:
        logger.warning("packages/strategy-factory/src/strategy_factory/domain/constants.py:72: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass

FACTOR_STRATEGY_MAPPING: Dict[str, tuple[str, ...]] = {
    "momentum": ("momentum", "ma_cross"),
    "trend_quality": ("growth_factor", "quality_factor", "ma_cross"),
    "price_volume_confirmation": ("momentum", "ma_cross", "quality_factor"),
    "breakout_structure": ("event_structure_breakout", "momentum", "volatility_breakout", "ma_cross"),
    "volatility_response": ("event_structure_breakout", "volatility_breakout", "ma_cross", "quality_factor"),
    "overextension_filter": ("mean_reversion_short", "rsi", "gap_fill"),
    "intraday_overnight_bridge": ("gap_fill", "mean_reversion_short", "ma_cross"),
    "turnover_dynamics": ("momentum", "ma_cross", "macro_timing"),
    "momentum_reversal_hybrid": ("mean_reversion_short", "momentum", "gap_fill"),
    "value": ("value_factor", "multi_factor"),
    "quality": ("quality_factor", "multi_factor"),
    "growth": ("growth_factor", "momentum"),
    "reversal": ("mean_reversion_short", "gap_fill", "rsi"),
    "volatility": ("event_structure_breakout", "volatility_breakout", "macro_timing", "ma_cross"),
    "liquidity": ("ma_cross", "macro_timing"),
    "capital_flow": ("north_capital_track", "momentum", "ma_cross"),
    "sentiment": ("momentum", "ma_cross"),
    "event": ("sector_rotation", "macro_timing", "momentum"),
    "alternative_composite": ("multi_factor", "momentum"),
    "multi_factor": ("multi_factor",),
}
# 应用环境变量覆盖
if FACTOR_STRATEGY_MAPPING_OVERRIDES:
    FACTOR_STRATEGY_MAPPING.update(FACTOR_STRATEGY_MAPPING_OVERRIDES)

# 初筛回测默认阈值与分层阈值
BACKTEST_DEFAULT_THRESHOLDS = {
    "sharpe_min": 0.30,
    "mdd_max": 0.35,
    "trades_min": 3,
    "min_samples": 3,
}

BACKTEST_AI_PROTOTYPE_THRESHOLDS = {
    "sharpe_min": 0.10,
    "mdd_max": 0.45,
    "trades_min": 1,
    "min_samples": 3,
}

# Temporary incubation thresholds for maybe_grant_provisional_incubation.
# Must be at least as strict as BACKTEST_DEFAULT_THRESHOLDS — provisional
# pass is a second chance after a failed quality gate, not a softer gate.
PROVISIONAL_PASS_THRESHOLDS = {
    "sharpe_min": 0.30,
    "mdd_max": 0.35,
    "trades_min": 3,
}

# 研究 / 孵化 / 实盘三层准入阈值。
# 现有 Gate-3 默认继续对齐 incubation 档，同时显式产出更宽松的 research
# 与更严格的 live 档，避免“研究通过”与“实盘可用”混为一谈。
RESEARCH_ADMISSION_THRESHOLDS: Dict[str, dict] = {
    "trade_profiles": {
        "default": {
            "post_cost_sharpe_min": 0.05,
            "trade_count_min": 3.0,
            "total_return_min": -0.03,
            "target_layer_oos_return_min": -0.02,
            "max_drawdown_max": 0.50,
            "event_window_hit_ratio_min": 0.0,
            "post_event_decay_min": -1.0,
            "trade_density_max": 1.4,
            "parameter_perturbation_trade_stability_min": 0.20,
        },
        "event_trade_validation": {
            "post_cost_sharpe_min": 0.08,
            "trade_count_min": 3.0,
            "total_return_min": -0.02,
            "target_layer_oos_return_min": -0.01,
            "max_drawdown_max": 0.48,
            "event_window_hit_ratio_min": 0.30,
            "post_event_decay_min": -0.9,
            "trade_density_max": 1.3,
            "parameter_perturbation_trade_stability_min": 0.20,
        },
    },
    "statistical_validation": {
        "walk_forward_ic_ir_min": 0.20,
        "purged_kfold_ic_min": 0.01,
        "bootstrap_ci_lower_min": -0.01,
        "param_sensitivity_max": 0.35,
    },
    "multiple_testing": {
        # DSR is a probability in [0, 1]; the previous -0.10 floor was a tautology
        # (every computed DSR passed). 0.0 still admits a DSR at the zero-quantile
        # of the null distribution — research-tier evidence, no better.
        "deflated_sharpe_ratio_min": 0.0,
        "pbo_max": 0.75,
        "white_reality_check_pvalue_max": 0.35,
        "hansen_spa_pvalue_max": 0.35,
    },
    "review": {
        "committee_final_score_min": 0.0,
        "promotion_review_score_min": 0.0,
    },
}

INCUBATION_ADMISSION_THRESHOLDS: Dict[str, dict] = {
    "trade_profiles": {
        "default": {
            "post_cost_sharpe_min": 0.10,
            "trade_count_min": 4.0,
            "total_return_min": -0.02,
            "target_layer_oos_return_min": -0.01,
            "max_drawdown_max": 0.45,
            "event_window_hit_ratio_min": 0.0,
            "post_event_decay_min": -1.0,
            "trade_density_max": 1.2,
            "parameter_perturbation_trade_stability_min": 0.25,
        },
        "event_trade_validation": {
            "post_cost_sharpe_min": 0.10,
            "trade_count_min": 4.0,
            "total_return_min": -0.01,
            "target_layer_oos_return_min": 0.0,
            "max_drawdown_max": 0.45,
            "event_window_hit_ratio_min": 0.40,
            "post_event_decay_min": -0.6,
            "trade_density_max": 0.9,
            "parameter_perturbation_trade_stability_min": 0.25,
        },
    },
    "statistical_validation": {
        "walk_forward_ic_ir_min": 0.30,
        "purged_kfold_ic_min": 0.02,
        "bootstrap_ci_lower_min": 0.0,
        "param_sensitivity_max": 0.30,
    },
    "multiple_testing": {
        "deflated_sharpe_ratio_min": 0.0,
        "pbo_max": 0.60,
        "white_reality_check_pvalue_max": 0.25,
        "hansen_spa_pvalue_max": 0.25,
    },
    "review": {
        "committee_final_score_min": 0.58,
        "promotion_review_score_min": 0.50,
    },
}

LIVE_ADMISSION_THRESHOLDS: Dict[str, dict] = {
    "trade_profiles": {
        "default": {
            "post_cost_sharpe_min": 0.35,
            "trade_count_min": 8.0,
            "total_return_min": 0.02,
            "target_layer_oos_return_min": 0.01,
            "max_drawdown_max": 0.25,
            "event_window_hit_ratio_min": 0.55,
            "post_event_decay_min": -0.40,
            "trade_density_max": 1.0,
            "parameter_perturbation_trade_stability_min": 0.50,
        },
        "event_trade_validation": {
            "post_cost_sharpe_min": 0.30,
            "trade_count_min": 6.0,
            "total_return_min": 0.01,
            "target_layer_oos_return_min": 0.02,
            "max_drawdown_max": 0.25,
            "event_window_hit_ratio_min": 0.67,
            "post_event_decay_min": -0.30,
            "trade_density_max": 0.9,
            "parameter_perturbation_trade_stability_min": 0.55,
        },
    },
    "statistical_validation": {
        "walk_forward_ic_ir_min": 0.45,
        "purged_kfold_ic_min": 0.04,
        "bootstrap_ci_lower_min": 0.02,
        "param_sensitivity_max": 0.20,
    },
    "multiple_testing": {
        "deflated_sharpe_ratio_min": 0.10,
        "pbo_max": 0.35,
        "white_reality_check_pvalue_max": 0.10,
        "hansen_spa_pvalue_max": 0.10,
    },
    "review": {
        "committee_final_score_min": 0.70,
        "promotion_review_score_min": 0.65,
    },
}

# 质量门 4 阶段验证阈值（strategy_manager._run_quality_gate 引用）
# 向后兼容：默认等同于孵化准入阈值。
QUALITY_GATE_THRESHOLDS = dict(INCUBATION_ADMISSION_THRESHOLDS["statistical_validation"])

VALIDATION_PROFILES = frozenset(
    {
        "factor_rank_validation",
        "trade_rule_validation",
        "event_trade_validation",
        "macro_regime_validation",
    }
)

VALIDATION_FOCUS_MODES = frozenset(
    {
        "event_target_only",
        "target_plus_representative",
        "broad_generalization",
        "candidate_target_only",
    }
)

REFRESH_MODES = frozenset(
    {
        "refresh_metrics_only",
        "spawn_revision_from_existing",
    }
)

TRADE_GATE_PROFILE_THRESHOLDS: Dict[str, dict[str, float]] = dict(
    INCUBATION_ADMISSION_THRESHOLDS["trade_profiles"]
)

# 基准 Sharpe 估算所用的假设年化波动率。它原先以字面量 0.15 出现在
# quality_gates 的年化收益除法里，读者无从判断该数字从何而来——它是假设值
# 而非观测量，因此命名并集中在此，以便被质疑和修改。
BENCHMARK_ASSUMED_ANNUAL_VOLATILITY = 0.15

# AI 原型策略临时孵化的风险报告阈值
RISK_REPORT_THRESHOLDS = {
    "var_percent_max": 4.5,
    "cvar_percent_max": 6.5,
    "stress_loss_percent_min": -25.0,
}

# 孵化期晋升/淘汰阈值
PROMOTION_THRESHOLDS = {
    "sharpe_min": 0.5,
    "mdd_max": 0.20,
    "hit_rate_blocker": 0.45,
    "hit_rate_risk_flag": 0.30,
}

DEPRECATION_THRESHOLDS = {
    "sharpe_negative": 0.0,
    "mdd_critical": 0.30,
}


def _env_names(name: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(name, str):
        return (name,)
    return tuple(str(item).strip() for item in name if str(item).strip())


def _env_raw(name: str | tuple[str, ...] | list[str], default: str) -> str:
    for candidate in _env_names(name):
        raw = os.getenv(candidate)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return text
    return str(default)


def _env_int(name: str | tuple[str, ...] | list[str], default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(_env_raw(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str | tuple[str, ...] | list[str], default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(_env_raw(name, str(default)) or default)
    except (TypeError, ValueError, OverflowError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(maximum, value))


def _env_bool(name: str | tuple[str, ...] | list[str], default: bool) -> bool:
    raw = _env_raw(name, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def resolve_stock_strategy_matrix_run_window(value: Optional[str] = None) -> str:
    window = str(
        value
        or _env_raw(
            (
                "STRATEGY_FACTORY_BULK_RUN_WINDOW",
                "STRATEGY_FACTORY_BULK_STOCK_MATRIX_RUN_WINDOW",
            ),
            "always",
        )
        or "always"
    ).strip().lower()
    return window if window in {"always", "off_hours", "market_hours"} else "always"


AUTONOMY_MAX_RESEARCH_TASKS = _env_int("STRATEGY_FACTORY_MAX_RESEARCH_TASKS", 12, minimum=1, maximum=20)
AUTONOMY_MAX_BULK_RESEARCH_TASKS = _env_int(
    (
        "STRATEGY_FACTORY_MAX_BULK_RESEARCH_TASKS",
        "STRATEGY_FACTORY_RESERVED_BULK_RESEARCH_TASKS",
        "STRATEGY_FACTORY_BULK_RESERVED_TASKS",
    ),
    20,
    minimum=0,
    maximum=50,
)
AUTONOMY_RESERVED_BULK_RESEARCH_TASKS = _env_int(
    (
        "STRATEGY_FACTORY_RESERVED_BULK_RESEARCH_TASKS",
        "STRATEGY_FACTORY_BULK_RESERVED_TASKS",
    ),
    max(0, AUTONOMY_MAX_BULK_RESEARCH_TASKS),
    minimum=0,
    maximum=50,
)
AUTONOMY_CANDIDATES_PER_TASK = _env_int("STRATEGY_FACTORY_CANDIDATES_PER_TASK", 4, minimum=1, maximum=8)
AUTONOMY_TASK_HARD_CAP = _env_int("STRATEGY_FACTORY_TASK_HARD_CAP", 24, minimum=4, maximum=50)
# 机会扫描宇宙大小：分页汇总后参与机会扫描的全市场股票上限（默认接近全量，支持配置）
OPPORTUNITY_UNIVERSE_LIMIT = _env_int("STRATEGY_FACTORY_OPPORTUNITY_UNIVERSE_LIMIT", 6000, minimum=50, maximum=10000)
# 每个任务的目标股票数量（默认 8，支持配置）
OPPORTUNITY_TARGET_SYMBOLS_PER_TASK = _env_int("STRATEGY_FACTORY_TARGET_SYMBOLS_PER_TASK", 8, minimum=3, maximum=20)
# 每个行业最多选几只（行业分散化控制）
OPPORTUNITY_MAX_PER_INDUSTRY = _env_int("STRATEGY_FACTORY_MAX_PER_INDUSTRY", 2, minimum=1, maximum=5)
_EVENT_TASK_GENERATION_LIMIT_MAX = _env_int("STRATEGY_FACTORY_EVENT_TASK_GENERATION_LIMIT_MAX", 8, minimum=2, maximum=10)
EVENT_TASK_GENERATION_LIMIT_MAX = max(
    _EVENT_TASK_GENERATION_LIMIT_MAX,
    min(10, AUTONOMY_CANDIDATES_PER_TASK + 1),
)
EVENT_SNAPSHOT_MIX_MAX = _env_int("STRATEGY_FACTORY_EVENT_SNAPSHOT_MIX_MAX", 4, minimum=1, maximum=8)
# 逐股策略矩阵生成入口默认关闭，避免未验证环境直接放量。
# 关闭时维持保守默认；显式开启后自动切换到 P2 级别容量默认值。
STOCK_STRATEGY_MATRIX_ENABLED: bool = _env_bool(
    ("STRATEGY_FACTORY_BULK_ENABLED", "STRATEGY_FACTORY_BULK_STOCK_MATRIX_ENABLED"),
    False,
)
# SR-1 (开发周期计划 Phase 1)：逐股诊断驱动的策略类型路由器。默认 OFF：
# 关闭时逐股矩阵维持现有 recommended_families + sector/factor 偏置选族（零变化）；
# 开启时由 StockStrategyRouter 依个股 regime+周期+排除项决定 family 集合。
STOCK_FIRST_ROUTER_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_STOCK_FIRST_ROUTER_ENABLED",
    False,
)
STOCK_FIRST_ROUTER_STRICT: bool = _env_bool(
    "STRATEGY_FACTORY_STOCK_FIRST_ROUTER_STRICT",
    False,
)
STOCK_FIRST_ROUTER_TELEMETRY_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_ROUTER_TELEMETRY_ENABLED",
    True,
)
# 个股方向闸：legacy 选族路径下，按个股 trend/reversal 方向剔除方向矛盾的 family
# （下跌/弱势剔除 momentum 等趋势族，上涨剔除纯反转族），避免动量策略被配到下跌标的。
# 默认 ON：这是修复方向错配的缺省行为；置 0 可紧急回到旧的无方向过滤逻辑。
STOCK_DIRECTION_GATE_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_DIRECTION_GATE_ENABLED",
    True,
)
STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT: int = _env_int(
    (
        "STRATEGY_FACTORY_BULK_UNIVERSE_LIMIT",
        "STRATEGY_FACTORY_BULK_STOCK_MATRIX_UNIVERSE_LIMIT",
    ),
    6000 if STOCK_STRATEGY_MATRIX_ENABLED else 500,
    minimum=50,
    maximum=10000,
)
STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK: int = _env_int(
    (
        "STRATEGY_FACTORY_BULK_FAMILIES_PER_STOCK",
        "STRATEGY_FACTORY_BULK_STOCK_MATRIX_FAMILIES_PER_STOCK",
    ),
    3,
    minimum=1,
    maximum=5,
)
STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN: int = _env_int(
    "STRATEGY_FACTORY_BULK_STOCK_MATRIX_MAX_TASKS_PER_RUN",
    15000 if STOCK_STRATEGY_MATRIX_ENABLED else 180,
    minimum=10,
    maximum=15000,
)
STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN: int = _env_int(
    "STRATEGY_FACTORY_BULK_STOCK_MATRIX_MAX_CANDIDATES_PER_RUN",
    15000 if STOCK_STRATEGY_MATRIX_ENABLED else 180,
    minimum=1,
    maximum=45000,
)
STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK: int = _env_int(
    "STRATEGY_FACTORY_BULK_STOCK_MATRIX_GENERATION_LIMIT_PER_TASK",
    1,
    minimum=1,
    maximum=3,
)
STOCK_STRATEGY_MATRIX_BATCH_SIZE: int = _env_int(
    "STRATEGY_FACTORY_BULK_BATCH_SIZE",
    100,
    minimum=10,
    maximum=500,
)
STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY: int = _env_int(
    "STRATEGY_FACTORY_BULK_CONCURRENCY",
    20,
    minimum=1,
    maximum=50,
)
STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD: int = _env_int(
    "STRATEGY_FACTORY_BULK_STOCK_MATRIX_TASKS_PER_SHARD",
    24,
    minimum=1,
    maximum=500,
)
STOCK_STRATEGY_MATRIX_RUN_WINDOW: str = resolve_stock_strategy_matrix_run_window()
# PR-S22 (策略工厂跑偏修复方案 P3 真复用)：
# - VECTOR_SIMILAR_PROFILE_ENABLED：开启相似画像查询，默认 1（观测）
# - VECTOR_REUSE_ENABLED：开启 task 级真复用，默认 0（无 verified 样本时不强行凑比例）
# - VECTOR_REUSE_MIN_SAMPLES：verified 索引最少需要多少条才允许真复用
# - VECTOR_REUSE_MIN_SIMILARITY：复用所需的最低相似度阈值
# - VECTOR_REUSE_TOPN：每只目标股票尝试复用的近邻上限
STRATEGY_FACTORY_VECTOR_SIMILAR_PROFILE_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_VECTOR_SIMILAR_PROFILE_ENABLED",
    True,
)
STRATEGY_FACTORY_VECTOR_REUSE_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_VECTOR_REUSE_ENABLED",
    False,
)
STRATEGY_FACTORY_VECTOR_REUSE_MIN_SAMPLES: int = _env_int(
    "STRATEGY_FACTORY_VECTOR_REUSE_MIN_SAMPLES",
    10,
    minimum=1,
    maximum=10000,
)
STRATEGY_FACTORY_VECTOR_REUSE_MIN_SIMILARITY: float = _env_float(
    "STRATEGY_FACTORY_VECTOR_REUSE_MIN_SIMILARITY",
    0.85,
    minimum=0.0,
    maximum=1.0,
)
STRATEGY_FACTORY_VECTOR_REUSE_TOPN: int = _env_int(
    "STRATEGY_FACTORY_VECTOR_REUSE_TOPN",
    5,
    minimum=1,
    maximum=20,
)
FACTORY_BACKLOG_RELAX_ENABLED: bool = _env_bool("STRATEGY_FACTORY_BACKLOG_RELAX_ENABLED", True)
STRATEGY_FACTORY_HIGH_CONFIDENCE_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_HIGH_CONFIDENCE_ENABLED",
    False,
)
STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED",
    False,
)
STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED",
    False,
)
STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED",
    True,
)
STRATEGY_FACTORY_QUALITY_UI_V2_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_QUALITY_UI_V2_ENABLED",
    False,
)
STRATEGY_FACTORY_RESEARCH_PROTOCOL_V2_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_RESEARCH_PROTOCOL_V2_ENABLED",
    True,
)
STRATEGY_FACTORY_GATE_MODEL_V2_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_GATE_MODEL_V2_ENABLED",
    True,
)
STRATEGY_FACTORY_TRACE_LEDGER_V2_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_TRACE_LEDGER_V2_ENABLED",
    True,
)
STRATEGY_FACTORY_FEEDBACK_V2_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_FEEDBACK_V2_ENABLED",
    True,
)
# ALPHA-WIRING-V1 (P-A)：泛因子 IC 接入。
# 默认 OFF，保持 _from_factor_ic 仅认经典 5 因子的零变化行为。
# ON 时，_from_factor_ic 额外遍历 factor_ic 映射里所有（含 gp_*/rl_* 挖掘）
# IC 通过阈值的因子，为每个生成带 prediction_contract + evidence_chain 的候选，
# 把已挖出的 alpha 接回生成链，并在源头饿死 fixed_defaults 填充物。
STRATEGY_FACTORY_FACTOR_IC_GENERIC_INTAKE_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_FACTOR_IC_GENERIC_INTAKE_ENABLED",
    False,
)
# 泛因子接入的 |IC| 门槛与单轮配额上限（防止泛因子撑爆候选基数）。
STRATEGY_FACTORY_FACTOR_IC_GENERIC_MIN_ABS_IC: float = _env_float(
    "STRATEGY_FACTORY_FACTOR_IC_GENERIC_MIN_ABS_IC",
    0.03,
    minimum=0.0,
    maximum=1.0,
)
STRATEGY_FACTORY_FACTOR_IC_GENERIC_MAX_FACTORS: int = _env_int(
    "STRATEGY_FACTORY_FACTOR_IC_GENERIC_MAX_FACTORS",
    8,
    minimum=1,
    maximum=64,
)
# ALPHA-WIRING-V1 (P-D)：经典 5 因子分支的 IC 裸阈值改为 env 可配。
# 默认 0.03 / -0.02 与历史硬编码完全一致（零行为变化）；可调以放宽/收紧准入。
STRATEGY_FACTORY_FACTOR_IC_CLASSIC_MIN_IC: float = _env_float(
    "STRATEGY_FACTORY_FACTOR_IC_CLASSIC_MIN_IC",
    0.03,
    minimum=0.0,
    maximum=1.0,
)
STRATEGY_FACTORY_FACTOR_IC_CLASSIC_FALLING_MAX_IC: float = _env_float(
    "STRATEGY_FACTORY_FACTOR_IC_CLASSIC_FALLING_MAX_IC",
    -0.02,
    minimum=-1.0,
    maximum=0.0,
)
# INVERT-DESIGN P3 改动B：晋升门要求"跨主要 regime 都有正 skill_lcb"。
# 默认 OFF，保持 promotion_ready 仅看全局 skill_lcb 的历史行为（零变化）。
# ON 时，晋升额外要求 hit_rate_by_regime 中每个达到最小样本量的 regime 标签 skill_lcb > 0。
STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_ENABLED",
    False,
)
STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_MIN_N: int = _env_int(
    "STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_MIN_N",
    20,
    minimum=1,
    maximum=10000,
)
# ALPHA-WIRING-V1 (P-D a)：factor_pool 因子进 observe 前的 OOS/鲁棒证据门。
# 默认 OFF，保持 _from_factor_pool 历史行为（零变化）。
# ON 时，只放行 validation_summary.evidence_summary 满足 OOS 质量的因子：
#   sample_dates ≥ MIN_SAMPLE_DATES 且 rank_ic_ir ≥ MIN_RANK_IC_IR 且 lookahead_risk != high。
STRATEGY_FACTORY_FACTOR_POOL_OOS_GATE_ENABLED: bool = _env_bool(
    "STRATEGY_FACTORY_FACTOR_POOL_OOS_GATE_ENABLED",
    False,
)
STRATEGY_FACTORY_FACTOR_POOL_OOS_MIN_SAMPLE_DATES: int = _env_int(
    "STRATEGY_FACTORY_FACTOR_POOL_OOS_MIN_SAMPLE_DATES",
    60,
    minimum=1,
    maximum=100000,
)
STRATEGY_FACTORY_FACTOR_POOL_OOS_MIN_RANK_IC_IR: float = _env_float(
    "STRATEGY_FACTORY_FACTOR_POOL_OOS_MIN_RANK_IC_IR",
    0.3,
    minimum=0.0,
    maximum=10.0,
)
STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE: str = str(
    _env_raw("STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE", "revise") or "revise"
).strip().lower() or "revise"
if STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE not in {"warn", "revise", "reject"}:
    STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE = "revise"
FACTORY_BACKLOG_RELAX_WEIGHT_MULTIPLIER: float = _env_float(
    "STRATEGY_FACTORY_BACKLOG_RELAX_WEIGHT_MULTIPLIER",
    0.18,
    minimum=0.01,
    maximum=0.6,
)
FACTORY_BACKLOG_RELAX_PRIORITY_PENALTY: int = _env_int(
    "STRATEGY_FACTORY_BACKLOG_RELAX_PRIORITY_PENALTY",
    10,
    minimum=0,
    maximum=40,
)
FACTORY_PRE_GATE_ENABLED: bool = _env_bool("STRATEGY_FACTORY_PRE_GATE_ENABLED", True)
AUTONOMY_STARTUP_DELAY_SEC = _env_int("STRATEGY_FACTORY_STARTUP_DELAY_SEC", 0, minimum=0, maximum=3600)
FACTORY_SUBMISSION_MIN_BACKTEST_TRADES: int = _env_int(
    "STRATEGY_FACTORY_SUBMISSION_MIN_BACKTEST_TRADES",
    4,
    minimum=1,
    maximum=100,
)
FACTORY_SUBMISSION_MIN_EVENT_TARGET_COVERAGE: float = _env_float(
    "STRATEGY_FACTORY_SUBMISSION_MIN_EVENT_TARGET_COVERAGE",
    0.8,
    minimum=0.0,
    maximum=1.0,
)
FACTORY_SUBMISSION_REQUIRE_TASK_PREFERENCE_MATCH: bool = _env_bool(
    "STRATEGY_FACTORY_SUBMISSION_REQUIRE_TASK_PREFERENCE_MATCH",
    True,
)
FACTORY_INCUBATION_FORMAL_SLOT_COUNT: int = _env_int(
    "STRATEGY_FACTORY_INCUBATION_FORMAL_SLOT_COUNT",
    12,
    minimum=1,
    maximum=128,
)
FACTORY_INCUBATION_OBSERVE_SLOT_COUNT: int = _env_int(
    "STRATEGY_FACTORY_INCUBATION_OBSERVE_SLOT_COUNT",
    24,
    minimum=0,
    maximum=256,
)
FACTORY_INCUBATION_EXPLORATION_RATIO: float = _env_float(
    "STRATEGY_FACTORY_INCUBATION_EXPLORATION_RATIO",
    0.12,
    minimum=0.0,
    maximum=0.5,
)
FACTORY_SUBMISSION_REJECT_GENERIC_AI_NAMES: bool = _env_bool(
    "STRATEGY_FACTORY_SUBMISSION_REJECT_GENERIC_AI_NAMES",
    True,
)
FACTORY_SUBMISSION_REQUIRE_STRICT_PASS_FOR_REFRESH: bool = _env_bool(
    "STRATEGY_FACTORY_SUBMISSION_REQUIRE_STRICT_PASS_FOR_REFRESH",
    True,
)


def resolve_event_runtime_mode(value: Optional[str] = None) -> str:
    mode = str(
        value
        or os.getenv("STRATEGY_FACTORY_EVENT_RUNTIME_MODE", "readonly")
        or "readonly"
    ).strip().lower()
    return mode if mode in {"readonly", "refresh"} else "readonly"


def is_factory_runtime_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_RUNTIME_ENABLED", True)


def is_factory_factor_auto_refresh_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_FACTOR_AUTO_REFRESH", False)


def is_factory_readiness_hard_block_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_READINESS_HARD_BLOCK", True)


def resolve_factory_readiness_min_governed_active_candidates() -> int:
    return _env_int(
        "STRATEGY_FACTORY_READINESS_MIN_GOVERNED_ACTIVE_CANDIDATES",
        1,
        minimum=1,
        maximum=100,
    )


def resolve_factory_readiness_min_governed_active_families() -> int:
    return _env_int(
        "STRATEGY_FACTORY_READINESS_MIN_GOVERNED_ACTIVE_FAMILIES",
        1,
        minimum=1,
        maximum=20,
    )


FACTORY_EVENT_RUNTIME_MODE: str = resolve_event_runtime_mode()
FACTORY_RUNTIME_ENABLED: bool = is_factory_runtime_enabled()
FACTORY_FACTOR_AUTO_REFRESH: bool = is_factory_factor_auto_refresh_enabled()
FACTORY_STARTUP_WARMUP_ENABLED: bool = _env_bool("STRATEGY_FACTORY_STARTUP_WARMUP_ENABLED", True)
FACTORY_STARTUP_WARMUP_FORCE: bool = _env_bool("STRATEGY_FACTORY_STARTUP_WARMUP_FORCE", False)
FACTORY_STARTUP_WARMUP_TASK_TYPE: str = (
    str(
        os.getenv("STRATEGY_FACTORY_STARTUP_WARMUP_TASK_TYPE", "core_market,factor_context")
        or "core_market,factor_context"
    ).strip().lower()
    or "core_market,factor_context"
)
FACTORY_STARTUP_WARMUP_LIMIT: int = _env_int(
    "STRATEGY_FACTORY_STARTUP_WARMUP_LIMIT",
    4,
    minimum=1,
    maximum=20,
)
FACTORY_FACTOR_REFRESH_TIMEOUT_SEC: int = _env_int(
    "STRATEGY_FACTORY_FACTOR_REFRESH_TIMEOUT_SEC",
    30,
    minimum=10,
    maximum=3600,
)
FACTORY_READINESS_HARD_BLOCK: bool = is_factory_readiness_hard_block_enabled()
FACTORY_READINESS_MIN_GOVERNED_ACTIVE_CANDIDATES: int = (
    resolve_factory_readiness_min_governed_active_candidates()
)
FACTORY_READINESS_MIN_GOVERNED_ACTIVE_FAMILIES: int = (
    resolve_factory_readiness_min_governed_active_families()
)
FACTORY_READINESS_MIN_SCORE: float = _env_float(
    "STRATEGY_FACTORY_READINESS_MIN_SCORE",
    0.55,
    minimum=0.0,
    maximum=1.0,
)
FACTORY_READINESS_MIN_COMPLETION_RATIO: float = _env_float(
    "STRATEGY_FACTORY_READINESS_MIN_COMPLETION_RATIO",
    0.5,
    minimum=0.0,
    maximum=1.0,
)
FACTORY_THROUGHPUT_TARGET_CANDIDATES_PER_HOUR: int = _env_int(
    "STRATEGY_FACTORY_TARGET_CANDIDATES_PER_HOUR",
    100,
    minimum=1,
    maximum=10000,
)
FACTORY_THROUGHPUT_TARGET_GATE3_PER_HOUR: int = _env_int(
    "STRATEGY_FACTORY_TARGET_GATE3_PER_HOUR",
    10,
    minimum=1,
    maximum=10000,
)

# --- 有界并发配置 ---
# 研究任务并发度（同时执行的研究任务上限）
RESEARCH_TASK_CONCURRENCY = _env_int("STRATEGY_FACTORY_RESEARCH_TASK_CONCURRENCY", 5, minimum=1, maximum=12)
# 回测并发度（同时执行的候选回测上限）
BACKTEST_CONCURRENCY = _env_int("STRATEGY_FACTORY_BACKTEST_CONCURRENCY", 4, minimum=1, maximum=10)
# 单个候选内部股票回测并发度
BACKTEST_CODE_CONCURRENCY = _env_int("STRATEGY_FACTORY_BACKTEST_CODE_CONCURRENCY", 4, minimum=1, maximum=12)
# 单只股票回测的 wall-clock 上限（秒）。异常股票常因数据脏值在回测引擎内陷入长时间循环；
# 命中 timeout 后该 code 计入 failed，不占用并发槽。
BACKTEST_CODE_TIMEOUT_SEC = _env_float("STRATEGY_FACTORY_BACKTEST_CODE_TIMEOUT_SEC", 30.0, minimum=1.0, maximum=600.0)
# 提交并发度（同时执行的候选提交上限）
SUBMIT_CONCURRENCY = _env_int("STRATEGY_FACTORY_SUBMIT_CONCURRENCY", 3, minimum=1, maximum=8)
# 去重并发度
DEDUP_CONCURRENCY = _env_int("STRATEGY_FACTORY_DEDUP_CONCURRENCY", 3, minimum=1, maximum=6)
# 淘汰检查并发度
ELIMINATION_CONCURRENCY = _env_int("STRATEGY_FACTORY_ELIMINATION_CONCURRENCY", 8, minimum=1, maximum=32)

# --- 分级门禁 (Phase 2) ---
# Gate-1 快速筛选：通过后进入 Gate-2 的比例
GATE1_PASS_RATIO: float = _env_float("STRATEGY_FACTORY_GATE1_PASS_RATIO", 0.45, minimum=0.0, maximum=1.0)
# Gate-1 快速筛选的宽松 Sharpe 下限
GATE1_SHARPE_MIN: float = _env_float("STRATEGY_FACTORY_GATE1_SHARPE_MIN", 0.15, minimum=-5.0, maximum=10.0)
# Gate-1 使用的代表性股票数量
GATE1_REPRESENTATIVE_COUNT: int = _env_int("STRATEGY_FACTORY_GATE1_REPRESENTATIVE_COUNT", 3, minimum=1, maximum=10)
# Gate-2 top-k 下限：默认 1 与历史行为逐位等价；调高可消除小样本 3→1 退化（漏斗瓶颈）
GATE2_TOPK_FLOOR: int = _env_int("STRATEGY_FACTORY_GATE2_TOPK_FLOOR", 1, minimum=1, maximum=50)
# Gate-2 top-k 取整方式：默认 False 用 round（行为不变）；True 用 ceil，避免小样本向下取整吃掉候选
GATE2_TOPK_USE_CEIL: bool = _env_bool("STRATEGY_FACTORY_GATE2_TOPK_USE_CEIL", False)

# 统计验证面板默认选股数（Gate-3 OOS 证据计算用）。
# 旧默认值 6 < IC 引擎每期最小样本数 10 → 每期 IC 被丢弃 → n_folds=0 → Gate-3 全拒
# （策略工厂零产出主因）。提到 20 给足裕量：取数后会因 K 线长度不足 (<90) 再过滤，
# 仍能稳定保留 ≥12 列有效股票。下限锁定在 12（> 引擎 min_samples 10），设回 6 可一键回滚。
FACTORY_VALIDATION_PANEL_SAMPLE_SIZE: int = _env_int(
    "STRATEGY_FACTORY_VALIDATION_PANEL_SAMPLE_SIZE",
    20,
    minimum=6,
    maximum=60,
)

# --- LLM fan-out (Phase 2) ---
LLM_FAN_OUT_COUNT: int = _env_int("STRATEGY_FACTORY_LLM_FAN_OUT_COUNT", 2, minimum=1, maximum=4)

# --- Spawner 扩展 (Phase 2) ---
SPAWNER_TARGET_TOTAL: int = _env_int("STRATEGY_FACTORY_SPAWNER_TARGET_TOTAL", 20, minimum=4, maximum=32)
SPAWNER_FILL_BUDGET_MAX: int = _env_int("STRATEGY_FACTORY_SPAWNER_FILL_BUDGET_MAX", 10, minimum=2, maximum=16)
SPAWNER_EVENT_FILL_BUDGET_MAX: int = _env_int(
    (
        "STRATEGY_FACTORY_EVENT_FILL_BUDGET_MAX",
        "STRATEGY_FACTORY_SPAWNER_EVENT_FILL_BUDGET_MAX",
    ),
    3,
    minimum=0,
    maximum=6,
)
SPAWNER_EVENT_SOURCE_BASE_CAP: int = _env_int("STRATEGY_FACTORY_SPAWNER_EVENT_SOURCE_BASE_CAP", 1, minimum=0, maximum=4)
SPAWNER_EVENT_SOURCE_SUPPLEMENTAL_BONUS: int = _env_int(
    "STRATEGY_FACTORY_SPAWNER_EVENT_SOURCE_SUPPLEMENTAL_BONUS",
    1,
    minimum=0,
    maximum=3,
)

# --- 调度模式配置 ---
# "continuous" = 24/7 循环（盘中15min、盘后60min）；"daily" = 每日定时一次（向后兼容）
FACTORY_SCHEDULE_MODE: str = (os.environ.get("STRATEGY_FACTORY_SCHEDULE_MODE", "continuous").strip().lower() or "continuous")
# A股盘中 (9:30-15:00 工作日) 循环间隔秒数
FACTORY_MARKET_HOURS_INTERVAL_SEC: int = _env_int("STRATEGY_FACTORY_MARKET_HOURS_INTERVAL_SEC", 720, minimum=60, maximum=7200)
# 盘后/周末/节假日循环间隔秒数
FACTORY_OFF_HOURS_INTERVAL_SEC: int = _env_int("STRATEGY_FACTORY_OFF_HOURS_INTERVAL_SEC", 3600, minimum=300, maximum=86400)
# 非交易日（节假日）循环间隔秒数
FACTORY_NON_TRADING_DAY_INTERVAL_SEC: int = _env_int("STRATEGY_FACTORY_NON_TRADING_DAY_INTERVAL_SEC", 7200, minimum=600, maximum=86400)
# 每日最大运行次数安全上限
FACTORY_MAX_DAILY_RUNS: int = _env_int("STRATEGY_FACTORY_MAX_DAILY_RUNS", 48, minimum=1, maximum=200)
# 出错后等待秒数
FACTORY_ERROR_BACKOFF_SEC: int = _env_int("FACTORY_ERROR_BACKOFF_SEC", 120, minimum=10, maximum=3600)
# 错误退避最大上限秒数
FACTORY_ERROR_BACKOFF_MAX_SEC: int = _env_int("STRATEGY_FACTORY_ERROR_BACKOFF_MAX_SEC", 1800, minimum=120, maximum=7200)
# 仅 daily 模式生效：每日运行时间 (HH:MM)
FACTORY_DAILY_RUN_TIME: str = os.environ.get("STRATEGY_FACTORY_DAILY_RUN_TIME", "19:00").strip() or "19:00"
# EMA 反馈下限（防止 family 饥饿）
FACTORY_EMA_FLOOR: float = _env_float("STRATEGY_FACTORY_EMA_FLOOR", 0.15, minimum=0.0, maximum=1.0)
# EMA 探索重置周期（每 N 轮重置低 EMA 的 family）
FACTORY_EMA_EXPLORATION_RESET_INTERVAL: int = _env_int("STRATEGY_FACTORY_EMA_EXPLORATION_RESET_INTERVAL", 20, minimum=5, maximum=100)
# 优雅关闭最大等待秒数（第一阶段）
FACTORY_SHUTDOWN_GRACE_SEC: float = _env_float("STRATEGY_FACTORY_SHUTDOWN_GRACE_SEC", 120.0, minimum=0.25, maximum=300.0)
# 优雅关闭取消后清理等待秒数（第二阶段）
FACTORY_SHUTDOWN_CANCEL_TIMEOUT_SEC: float = _env_float("STRATEGY_FACTORY_SHUTDOWN_CANCEL_TIMEOUT_SEC", 5.0, minimum=1.0, maximum=30.0)

BACKTEST_TYPE_THRESHOLDS: Dict[str, dict] = {
    "momentum": {"sharpe_min": 0.35, "mdd_max": 0.32, "trades_min": 4},
    "ma_cross": {"sharpe_min": 0.25, "mdd_max": 0.35, "trades_min": 3},
    "rsi": {"sharpe_min": 0.20, "mdd_max": 0.38, "trades_min": 4},
    "value_factor": {"sharpe_min": 0.25, "mdd_max": 0.30, "trades_min": 3},
    "quality_factor": {"sharpe_min": 0.28, "mdd_max": 0.30, "trades_min": 3},
    "growth_factor": {"sharpe_min": 0.32, "mdd_max": 0.34, "trades_min": 4},
    "multi_factor": {"sharpe_min": 0.30, "mdd_max": 0.30, "trades_min": 4},
    "macro_timing": {"sharpe_min": 0.20, "mdd_max": 0.28, "trades_min": 2},
    "volatility_breakout": {"sharpe_min": 0.32, "mdd_max": 0.35, "trades_min": 4},
    "event_structure_breakout": {"sharpe_min": 0.34, "mdd_max": 0.30, "trades_min": 4},
    "gap_fill": {"sharpe_min": 0.22, "mdd_max": 0.36, "trades_min": 4},
    "mean_reversion_short": {"sharpe_min": 0.22, "mdd_max": 0.36, "trades_min": 5},
    "sector_rotation": {"sharpe_min": 0.26, "mdd_max": 0.33, "trades_min": 3},
    "north_capital_track": {"sharpe_min": 0.30, "mdd_max": 0.34, "trades_min": 4},
    "margin_divergence": {"sharpe_min": 0.24, "mdd_max": 0.35, "trades_min": 3},
}

# --- 多阶段 Pipeline 配置 ---

PIPELINE_MODE: str = os.environ.get("STRATEGY_LLM_PIPELINE_MODE", "staged").strip().lower() or "staged"

PIPELINE_STAGE_TIMEOUT_SEC: float = float(os.environ.get("STRATEGY_PIPELINE_STAGE_TIMEOUT_SEC", "10") or 10)

def _stage_timeout_env_name(stage_id: str) -> str:
    normalized = str(stage_id or "").strip().upper()
    for old, new in (("-", "_"), (" ", "_")):
        normalized = normalized.replace(old, new)
    return f"STRATEGY_PIPELINE_STAGE_{normalized}_TIMEOUT_SEC"


def _resolve_pipeline_stage_timeout(stage_id: str, legacy_default: float) -> float:
    stage_env_name = _stage_timeout_env_name(stage_id)
    if os.getenv(stage_env_name) is not None:
        return _env_float(stage_env_name, legacy_default, minimum=1.0, maximum=120.0)
    if os.getenv("STRATEGY_PIPELINE_STAGE_TIMEOUT_SEC") is not None:
        return _env_float("STRATEGY_PIPELINE_STAGE_TIMEOUT_SEC", legacy_default, minimum=1.0, maximum=120.0)
    return legacy_default


# 每阶段独立超时（秒），缺失则回退到 PIPELINE_STAGE_TIMEOUT_SEC
PIPELINE_STAGE_TIMEOUTS: Dict[str, float] = {
    "event_recognition": _resolve_pipeline_stage_timeout("event_recognition", 8.0),
    "theme_propagation": _resolve_pipeline_stage_timeout("theme_propagation", 10.0),
    "exposure_mapping": _resolve_pipeline_stage_timeout("exposure_mapping", 10.0),
    "market_confirmation": _resolve_pipeline_stage_timeout("market_confirmation", 10.0),
    "strategy_generation": _resolve_pipeline_stage_timeout("strategy_generation", 12.0),
}

def _resolve_pipeline_stage_max_tokens(stage: str, legacy_default: int) -> int:
    """每阶段 max_tokens,支持 env 覆盖。

    reasoning 类模型(如 deepseek-v4-pro)会先消耗 reasoning token 再产出 content,
    过紧的上限会导致 content 为空 / JSON 截断。可用
    STRATEGY_PIPELINE_STAGE_MAX_TOKENS__<STAGE> 或全局 STRATEGY_PIPELINE_STAGE_MAX_TOKENS 调高。
    """
    specific = os.getenv(f"STRATEGY_PIPELINE_STAGE_MAX_TOKENS__{stage.upper()}")
    if specific is not None:
        try:
            return max(128, int(str(specific).strip()))
        except (TypeError, ValueError) as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/domain/constants.py:937: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
    global_override = os.getenv("STRATEGY_PIPELINE_STAGE_MAX_TOKENS")
    if global_override is not None:
        try:
            return max(128, int(str(global_override).strip()))
        except (TypeError, ValueError) as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/domain/constants.py:943: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
    return legacy_default


PIPELINE_STAGE_MAX_TOKENS: Dict[str, int] = {
    "event_recognition": _resolve_pipeline_stage_max_tokens("event_recognition", 400),
    "theme_propagation": _resolve_pipeline_stage_max_tokens("theme_propagation", 600),
    "exposure_mapping": _resolve_pipeline_stage_max_tokens("exposure_mapping", 800),
    "market_confirmation": _resolve_pipeline_stage_max_tokens("market_confirmation", 600),
    # 2026-05-28 (P0/C): bumped from 1200 → 4000 to accommodate
    # evidence_chain + prediction_contract + confidence_contract auto-backfill
    # which adds ~2500 chars of additional output per candidate when EVIDENCE_CONTRACT_ENABLED=1.
    # JSON truncation at 1200 tokens caused 'external llm response missing candidates'
    # ValueError storms after enabling evidence contract toggle.
    "strategy_generation": _resolve_pipeline_stage_max_tokens("strategy_generation", 4000),
}

PIPELINE_STAGE_TEMPERATURE: Dict[str, float] = {
    "event_recognition": 0.0,
    "theme_propagation": 0.2,
    "exposure_mapping": 0.25,
    "market_confirmation": 0.15,
    "strategy_generation": 0.3,
}


def preferred_strategy_types_for_factor(
    factor_name: str,
    *,
    default: Optional[List[str]] = None,
) -> List[str]:
    lowered = str(factor_name or "").strip().lower()
    for token, mapped in FACTOR_STRATEGY_MAPPING.items():
        if token in lowered:
            return list(mapped)
    return list(default or ["multi_factor"])
