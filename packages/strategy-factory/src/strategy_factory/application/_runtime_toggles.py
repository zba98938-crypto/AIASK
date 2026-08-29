"""Strategy Factory runtime feature toggles.

所有改变 admission/grade 行为的修改都通过环境变量包裹,允许观察期后视效果决定
固化或回滚。命名空间统一以 STRATEGY_FACTORY_ / INCUBATION_FACTORY_ 前缀,集中
在此文件维护。

关联方案: 策略工厂到孵化工厂过渡-开发方案-2026-05-26.md (DEV-V1)
关联架构: 策略工厂到孵化工厂过渡架构方案-2026-05-26.md (V4-network §15.2 / §16.2)
"""

from __future__ import annotations

import os
from typing import FrozenSet


GRADE_RANKS: dict[str, int] = {
    "D": 0,
    "C": 1,
    "B": 2,
    "A": 3,
    "S": 4,
    "SS": 5,
    "SSS": 6,
}


def _env_bool(name: str, default: bool) -> bool:
    """解析 1/true/yes/on (大小写不敏感) 为 True,其它为 False。

    未设置 env 时使用 default。default=False 是安全默认。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _env_set(name: str, default: str = "") -> FrozenSet[str]:
    """解析逗号分隔的字符串为 frozenset,自动 strip 空白与小写化。"""
    raw = os.getenv(name, default)
    value = str(raw or "").strip().lower()
    if not value:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def normalize_validation_grade(value: object) -> str:
    return str(value or "").strip().upper()


def validation_grade_rank(value: object) -> int:
    return GRADE_RANKS.get(normalize_validation_grade(value), -1)


def validation_grade_at_least(value: object, minimum: object) -> bool:
    min_grade = normalize_validation_grade(minimum)
    if not min_grade:
        return True
    min_rank = validation_grade_rank(min_grade)
    if min_rank < 0:
        return True
    return validation_grade_rank(value) >= min_rank


def validation_grade_weight(value: object) -> float:
    grade = normalize_validation_grade(value)
    return {
        "SSS": 1.25,
        "SS": 1.18,
        "S": 1.10,
        "A": 1.0,
        "B": 0.85,
        "C": 0.7,
    }.get(grade, 0.55)


# === DEV-V1 P0: 允许 D 级 + Gate passed 候选走 observe lane ===
# 默认 False 保持现有行为;灰度期间手动 export STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED=1
# 验收期 14 天,验收通过后再修改默认值或固化删除 toggle。
def observe_d_grade_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED", default=False)


def observe_first_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED", default=False)


# === INVERT-DESIGN P1 改动A: Layer 1 宽进准入 ===
# 倒置架构核心:候选只要结构合法(strategy_type 已注册 + runtime 契约字段齐 +
# 无 semantic hard fail),即使 Gate-3 回测盈利未通过(quality_passed=False),
# 也可走 observe_incubation lane,创建 paper 账户进入向前观察。
# 这是"观察先行、向前测命中率"的入口。默认 False 保持现有"证明先于观察"行为;
# 打开后由 ForwardVerifier(改动D 已带 regime 标签)对其持续测量。
# 注意:formal_incubation(真实资本前置)严格性零变化 — 宽进只作用于零资本 observe。
def wide_intake_observe_enabled() -> bool:
    return observe_first_enabled() or _env_bool("STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED", default=False)


# === DEV-V1 P3: 扩展 _TRADE_AWARE_VALIDATION_GRADE_FAMILIES 集合 ===
# 默认空集保持现有行为;灰度期间手动 export STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES=...
# 推荐渐进灰度顺序:
#   Week N+0: ""
#   Week N+1: "volatility_breakout"                    # 占 48% 候选,优先
#   Week N+2: "volatility_breakout,value_factor"
#   Week N+3: "volatility_breakout,value_factor,sector_rotation"
#   Week N+4: 全集 (volatility_breakout,value_factor,sector_rotation,
#                   macro_timing,growth_factor,north_capital_track,event_structure_breakout)
# 短周期反转型 (rsi/mean_reversion_short/gap_fill/margin_divergence) 不建议扩展。
def trade_aware_extra_families() -> FrozenSet[str]:
    return _env_set("STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES", default="")


# === DEV-V1 P1: 孵化工厂消费 paper observation 候选 ===
# 默认 True,让已创建 paper account 的 observe 样本进入孵化工厂消费。
def paper_intake_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", default=True)


# === DEV-V1 P1 配套:单批 paper observation 的 batch limit ===
def paper_intake_batch_limit() -> int:
    # 默认 500、硬上界 3000(与 akshare-mcp/_strategy_factory_toggles.py 同步)。
    # 实测每策略孵化处理 ~0.05s,单轮 BATCH_TIMEOUT_SEC=600s 下可处理数千,旧上界 500
    # 是人为瓶颈——observe(stage=paper)积压上万时收敛太慢。
    raw = os.getenv("INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT", "500")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 500
    return max(1, min(value, 3000))


def recompile_remediation_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_RECOMPILE_REMEDIATION_ENABLED", default=True)


def recompile_remediation_batch_limit() -> int:
    raw = os.getenv("INCUBATION_FACTORY_RECOMPILE_REMEDIATION_BATCH_LIMIT", "200")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 200
    return max(1, min(value, 1000))


def gate3_record_only_intake_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED", default=False)


def gate3_record_only_intake_batch_limit() -> int:
    raw = os.getenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_BATCH_LIMIT", "100")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 100
    return max(1, min(value, 500))


def gate3_record_only_intake_min_grade() -> str:
    grade = normalize_validation_grade(os.getenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_MIN_GRADE", "C"))
    return grade if grade in GRADE_RANKS else "C"


# === DEV-V2: Gate-3 failed but diagnostically useful candidates ===
def diagnostic_observation_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_ENABLED", default=False)


def diagnostic_observation_batch_limit() -> int:
    raw = os.getenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_BATCH_LIMIT", "5")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 5
    return max(1, min(value, 50))


def diagnostic_observation_ttl_days() -> int:
    raw = os.getenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_TTL_DAYS", "7")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 7
    return max(1, min(value, 30))


def diagnostic_observation_final_status() -> str:
    raw = os.getenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_STATUS", "diagnostic")
    value = str(raw or "").strip().lower()
    return value if value in {"diagnostic", "submitted"} else "diagnostic"


def diagnostic_observation_min_win_rate() -> float:
    raw = os.getenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_MIN_WIN_RATE", "0.36")
    try:
        value = float(str(raw).strip())
    except Exception:
        value = 0.36
    return max(0.0, min(value, 0.399))


def diagnostic_observation_min_trade_count() -> int:
    raw = os.getenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_MIN_TRADE_COUNT", "4")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 4
    return max(1, min(value, 100))


def diagnostic_observation_health_guard_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_HEALTH_GUARD_ENABLED", default=True)


def diagnostic_observation_health_max_age_hours() -> int:
    raw = os.getenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_HEALTH_MAX_AGE_HOURS", "24")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 24
    return max(1, min(value, 168))


def diagnostic_observation_dedupe_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_DEDUPE_ENABLED", default=True)


def strategy_trade_prediction_promotion_gate_enabled() -> bool:
    return _env_bool("STRATEGY_TRADE_PREDICTION_PROMOTION_GATE_ENABLED", default=False)


def strategy_trade_prediction_budget_feedback_enabled() -> bool:
    return _env_bool("STRATEGY_TRADE_PREDICTION_BUDGET_FEEDBACK_ENABLED", default=False)


def strategy_trade_prediction_factor_decay_enabled() -> bool:
    return _env_bool("STRATEGY_TRADE_PREDICTION_FACTOR_DECAY_ENABLED", default=False)


def strategy_factory_min_validation_grade() -> str:
    raw = os.getenv("STRATEGY_FACTORY_MIN_VALIDATION_GRADE", "C")
    grade = normalize_validation_grade(raw)
    return grade if grade in GRADE_RANKS else "C"


def strategy_factory_gate3_record_only_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", default=False)


def stock_direction_gate_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_DIRECTION_GATE_ENABLED", default=True)


__all__ = [
    "GRADE_RANKS",
    "normalize_validation_grade",
    "validation_grade_rank",
    "validation_grade_at_least",
    "validation_grade_weight",
    "observe_d_grade_enabled",
    "observe_first_enabled",
    "wide_intake_observe_enabled",
    "trade_aware_extra_families",
    "paper_intake_enabled",
    "paper_intake_batch_limit",
    "recompile_remediation_enabled",
    "recompile_remediation_batch_limit",
    "gate3_record_only_intake_enabled",
    "gate3_record_only_intake_batch_limit",
    "gate3_record_only_intake_min_grade",
    "diagnostic_observation_enabled",
    "diagnostic_observation_batch_limit",
    "diagnostic_observation_ttl_days",
    "diagnostic_observation_final_status",
    "diagnostic_observation_min_win_rate",
    "diagnostic_observation_min_trade_count",
    "diagnostic_observation_health_guard_enabled",
    "diagnostic_observation_health_max_age_hours",
    "diagnostic_observation_dedupe_enabled",
    "strategy_trade_prediction_promotion_gate_enabled",
    "strategy_trade_prediction_budget_feedback_enabled",
    "strategy_trade_prediction_factor_decay_enabled",
    "strategy_factory_min_validation_grade",
    "strategy_factory_gate3_record_only_enabled",
    "stock_direction_gate_enabled",
]
