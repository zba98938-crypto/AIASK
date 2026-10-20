"""Strategy Factory release toggles — akshare-mcp side mirror.

与 packages/strategy-factory/.../_runtime_toggles.py 保持同步。
任何修改都需要双侧同时改。

关联:策略工厂到孵化工厂过渡-开发方案-2026-05-26.md (P1)
"""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    return value in {"1", "true", "yes", "on"}


def paper_intake_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", default=True)


def paper_intake_batch_limit() -> int:
    # 默认 500、硬上界 3000。实测每策略孵化处理 ~0.05s(信号/前向/指标多数无新数据快速跳过),
    # 单轮 BATCH_TIMEOUT_SEC=600s 下可安全处理数千策略,旧上界 500 是人为瓶颈——observe
    # 积压(stage=paper)上万时每轮只吃 300-500 需数十轮才清一遍。放宽上界让积压更快收敛。
    raw = os.getenv("INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT", "500")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 500
    return max(1, min(value, 3000))


def recompile_remediation_enabled() -> bool:
    """P0-b/P1: 孵化工厂每轮对 observe 池趋势策略重编译补 compiled_dsl + 测量
    instrument_profile,并把满足 formal readiness 的样本升级到 formal_incubation。
    默认 ON,用于持续修复 observe 池里可转 formal 的样本。"""
    return _env_bool("INCUBATION_FACTORY_RECOMPILE_REMEDIATION_ENABLED", default=True)


def recompile_remediation_batch_limit() -> int:
    raw = os.getenv("INCUBATION_FACTORY_RECOMPILE_REMEDIATION_BATCH_LIMIT", "200")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 200
    return max(1, min(value, 1000))


def recompile_promotion_forward_skill_gate_enabled() -> bool:
    """重编译转正(observe submitted → formal incubating)前是否强制真实前向 skill 门。

    默认 ON。原 recompile 转正只看结构性条件(compiled_dsl/measured profile/提交期回测
    grade),完全不读前向收益,会把"observe 期未经任何前向 skill 验证"的样本仅凭工程补齐
    就升进 incubating 池——伪转正。开启后要求 primary_skill_lcb>0 且 effective_n 达 warmup
    底线才放行。守诚实边界:取不到前向证据(无信号/无 forward_returns)即不升,宁可留在 observe。
    设 INCUBATION_FACTORY_RECOMPILE_PROMOTION_FORWARD_SKILL_GATE_ENABLED=0 可关(不建议)。"""
    return _env_bool(
        "INCUBATION_FACTORY_RECOMPILE_PROMOTION_FORWARD_SKILL_GATE_ENABLED",
        default=True,
    )


def recompile_promotion_min_effective_n() -> int:
    """重编译转正前向 skill 门要求的 primary_effective_n 下限(默认 12,对齐 long-bucket
    warmup_min_n)。低于此样本量视为前向证据不足,不放行转正。"""
    raw = os.getenv("INCUBATION_FACTORY_RECOMPILE_PROMOTION_MIN_EFFECTIVE_N", "12")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 12
    return max(1, min(value, 200))


def diagnostic_intake_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_DIAGNOSTIC_INTAKE_ENABLED", default=False)


def diagnostic_intake_batch_limit() -> int:
    raw = os.getenv("INCUBATION_FACTORY_DIAGNOSTIC_BATCH_LIMIT", "5")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 5
    return max(1, min(value, 50))


def execution_audit_acceptance_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_EXECUTION_AUDIT_ACCEPTANCE_ENABLED", default=True)


def execution_audit_acceptance_backfill_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_EXECUTION_AUDIT_ACCEPTANCE_BACKFILL_ENABLED", default=True)


def execution_audit_acceptance_batch_limit() -> int:
    raw = os.getenv("INCUBATION_FACTORY_EXECUTION_AUDIT_ACCEPTANCE_BATCH_LIMIT", "80")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 80
    return max(1, min(value, 500))


def execution_audit_remediation_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_EXECUTION_AUDIT_REMEDIATION_ENABLED", default=False)


def execution_audit_remediation_batch_limit() -> int:
    raw = os.getenv("INCUBATION_FACTORY_EXECUTION_AUDIT_REMEDIATION_BATCH_LIMIT", "5")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 5
    return max(1, min(value, 50))


def execution_audit_remediation_target_trade_count() -> int:
    raw = os.getenv("INCUBATION_FACTORY_EXECUTION_AUDIT_REMEDIATION_TARGET_TRADE_COUNT", "20")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 20
    return max(1, min(value, 200))


def paper_execution_backlog_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_ENABLED", default=True)


def paper_execution_backlog_batch_limit() -> int:
    raw = os.getenv("INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_BATCH_LIMIT", "200")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 200
    return max(1, min(value, 1000))


def execution_audit_native_evidence_backfill_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_ENABLED", default=True)


def execution_audit_native_evidence_backfill_batch_limit() -> int:
    raw = os.getenv("INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_BATCH_LIMIT", "200")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 200
    return max(1, min(value, 1000))


def stale_paper_position_closure_enabled() -> bool:
    # Closing positions is mutating paper-execution state.  Keep it opt-in so
    # a newly installed runtime cannot start emitting exit orders merely by
    # upgrading to a release that contains this maintenance phase.
    return _env_bool("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED", default=False)


def stale_paper_position_closure_batch_limit() -> int:
    raw = os.getenv("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT", "40")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 40
    return max(1, min(value, 200))


def stale_paper_position_closure_grace_days() -> int:
    raw = os.getenv("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_GRACE_DAYS", "0")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 0
    return max(0, min(value, 30))


def rejected_strategy_position_cleanup_enabled() -> bool:
    # Rejected/archived strategies are excluded from the execution universe, so
    # any paper position they still hold can never receive an exit signal.  The
    # cleanup is therefore lifecycle hygiene, not a trading exit policy.
    return _env_bool("INCUBATION_FACTORY_REJECTED_POSITION_CLEANUP_ENABLED", default=True)


def rejected_strategy_position_cleanup_batch_limit() -> int:
    raw = os.getenv("INCUBATION_FACTORY_REJECTED_POSITION_CLEANUP_BATCH_LIMIT", "100")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 100
    return max(1, min(value, 500))


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
    grade = str(os.getenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_MIN_GRADE", "C") or "").strip().upper()
    return grade if grade in {"D", "C", "B", "A", "S", "SS", "SSS"} else "C"


__all__ = [
    "paper_intake_enabled",
    "paper_intake_batch_limit",
    "recompile_remediation_enabled",
    "recompile_remediation_batch_limit",
    "diagnostic_intake_enabled",
    "diagnostic_intake_batch_limit",
    "execution_audit_acceptance_enabled",
    "execution_audit_acceptance_backfill_enabled",
    "execution_audit_acceptance_batch_limit",
    "execution_audit_remediation_enabled",
    "execution_audit_remediation_batch_limit",
    "execution_audit_remediation_target_trade_count",
    "paper_execution_backlog_enabled",
    "paper_execution_backlog_batch_limit",
    "execution_audit_native_evidence_backfill_enabled",
    "execution_audit_native_evidence_backfill_batch_limit",
    "stale_paper_position_closure_enabled",
    "stale_paper_position_closure_batch_limit",
    "stale_paper_position_closure_grace_days",
    "rejected_strategy_position_cleanup_enabled",
    "rejected_strategy_position_cleanup_batch_limit",
    "gate3_record_only_intake_enabled",
    "gate3_record_only_intake_batch_limit",
    "gate3_record_only_intake_min_grade",
]
