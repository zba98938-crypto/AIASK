"""Unified governance monitoring service.

Aggregates 5 monitoring dimensions into a single ``GovernanceReport``:

1. Factor Decay Monitor — detects IC degradation in factor candidates
2. Crowding Monitor — estimates factor crowding risk
3. Model Drift / Calibration Drift — flags model quality degradation
4. Strategy Runtime Health — surfaces strategy posture & alert state
5. Online/Offline Consistency — compares backtest vs execution assumptions

Usage::

    from akshare_mcp.services.governance_monitor import GovernanceMonitor

    monitor = GovernanceMonitor()
    report = monitor.run_full_check(target_type="system")
    report_dict = report.to_dict()

"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    m = _mean(values)
    if m is None:
        return None
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


# ── 1. Factor Decay Monitor ──────────────────────────────────────────────────

def check_factor_decay(
    factor_name: str,
    ic_history: list[float] | None = None,
    *,
    window: int = 8,
    decay_threshold: float = 0.015,
) -> dict[str, Any]:
    """Check whether a factor's IC is decaying over time.

    Parameters
    ----------
    factor_name:
        Human‐readable factor identifier.
    ic_history:
        Chronological list of IC values (e.g. weekly or monthly).
    window:
        Number of recent periods for the "recent" window.
    decay_threshold:
        Absolute drop from all-time mean that triggers "decaying" status.

    Returns
    -------
    dict with decay_status, rolling_ic_trend, half_life_estimate, etc.
    """
    values = [float(v) for v in (ic_history or []) if v is not None]
    if len(values) < 4:
        return {
            "factor_name": factor_name,
            "decay_status": "unknown",
            "reason": "insufficient_history",
            "evaluation_count": len(values),
            "action_recommended": "collect_more_data",
        }

    all_mean = _mean(values)
    all_std = _std(values)
    recent = values[-min(window, len(values)):]
    recent_mean = _mean(recent)

    # Trend detection: compare first half vs second half
    half = len(values) // 2
    early_mean = _mean(values[:max(half, 1)])
    late_mean = _mean(values[max(half, 1):])

    if early_mean is not None and late_mean is not None:
        if late_mean < early_mean - decay_threshold:
            trend = "decaying"
        elif late_mean > early_mean + decay_threshold:
            trend = "improving"
        else:
            trend = "stable"
    else:
        trend = "unknown"

    # Decay status
    degradation = (
        round(float(all_mean) - float(recent_mean), 6)
        if all_mean is not None and recent_mean is not None
        else None
    )
    if degradation is not None and degradation > decay_threshold * 2:
        status = "decayed"
    elif degradation is not None and degradation > decay_threshold:
        status = "decaying"
    else:
        status = "stable"

    # Half-life estimate (rough: number of periods for IC to halve)
    half_life: float | None = None
    if trend == "decaying" and early_mean and late_mean and early_mean > 0:
        decay_rate = (early_mean - late_mean) / max(half, 1)
        if decay_rate > 0:
            half_life = round(early_mean / (2 * decay_rate), 1)

    # F-N43-9 fix (诊断报告 §N43): decay_status 阈值偏宽松。
    # 历史问题: 近期 IC 已转负（recent_mean<0）+ rolling_ic_trend=decaying +
    # half_life 仅 1.4 周期的因子仍被判 stable，decay 维度欠告警。
    # 修复: 近期 IC 转负 → 至少 decaying；趋势 decaying 且 (转负 或 半衰期过短) → decayed。
    escalation_reasons: list[str] = []
    if recent_mean is not None and recent_mean < 0:
        escalation_reasons.append("recent_ic_negative")
        if status == "stable":
            status = "decaying"
    if half_life is not None and half_life <= 2.0:
        escalation_reasons.append(f"short_half_life={half_life}")
    if trend == "decaying" and (
        (recent_mean is not None and recent_mean < 0)
        or (half_life is not None and half_life <= 2.0)
    ):
        status = "decayed"

    action = (
        "retire_or_replace" if status == "decayed"
        else "review_and_monitor" if status == "decaying"
        else "continue_monitoring"
    )

    return {
        "factor_name": factor_name,
        "decay_status": status,
        "evaluation_count": len(values),
        "all_time_ic_mean": round(float(all_mean), 6) if all_mean is not None else None,
        "all_time_ic_std": round(float(all_std), 6) if all_std is not None else None,
        "recent_ic_mean": round(float(recent_mean), 6) if recent_mean is not None else None,
        "degradation_vs_mean": degradation,
        "rolling_ic_trend": trend,
        "half_life_estimate_periods": half_life,
        "escalation_reasons": escalation_reasons,
        "action_recommended": action,
    }


# ── 2. Crowding Monitor ──────────────────────────────────────────────────────

_CROWDED_CATEGORIES = {
    "momentum": 0.85,
    "reversal": 0.70,
    "value": 0.65,
    "size": 0.80,
    "volatility": 0.55,
    "volume": 0.50,
    "quality": 0.40,
    "growth": 0.45,
    "sentiment": 0.35,
    "technical": 0.60,
}

_CROWDED_TOKENS = {
    "momentum", "pct_change", "return", "reversal", "mean_revert",
    "sma", "ema", "rsi", "macd", "bollinger", "volume_ratio",
}


def _tokenize_factor_expr(text: str) -> set[str]:
    """Split a factor expression into comparable tokens.

    Splits on identifiers AND arithmetic/comparison operators so that
    expressions such as ``close/ma_20-1`` decompose into
    ``{close, ma, 20, 1}`` rather than ``{close/ma, 20-1}``. Without this,
    duplicate factors written with operators never match.
    """
    cleaned = str(text or "").lower()
    for ch in "_()/*+-=<>,.[]| ":
        cleaned = cleaned.replace(ch, " ")
    return {tok for tok in cleaned.split() if tok}


def check_crowding(
    factor_name: str,
    expression: str = "",
    category: str | None = None,
    existing_pool: list[str] | None = None,
) -> dict[str, Any]:
    """Estimate crowding risk for a factor.

    F-N43-6 fix (诊断报告 §N43): crowding 不再由类别先验单独决定。
    历史问题: ``_CROWDED_CATEGORIES["momentum"]=0.85`` 使**任何** momentum
    因子在**未提供因子池**时也被判 crowding_score=0.85 / band=high，
    similar_factor_count 恒 0 —— 一个虚假的高拥挤信号会误导 AI 否决正常因子。

    新逻辑:
      - 拥挤度的真实证据是「因子池中已存在多少相似/重复因子」。
      - 未提供 existing_pool 时无法度量真实拥挤度，只能给出**类别先验**，
        且置信度 low、band 永不升到 high（避免虚假告警）。
      - 提供 existing_pool 时，相似度（含精确重复检测、token Jaccard）
        主导评分，类别仅作弱先验。

    Returns
    -------
    dict with crowding_score, crowding_band, similar_factor_count,
    assessment_basis, confidence, exact_duplicate_count, pool_size, warnings.
    """
    expr_lower = str(expression or factor_name or "").lower().strip()
    pool = [str(f).lower().strip() for f in (existing_pool or []) if str(f).strip()]
    pool_provided = bool(pool)

    # Category prior — a WEAK signal from alpha-decay literature, NOT a measurement.
    cat = str(category or "").strip().lower()
    category_prior = _CROWDED_CATEGORIES.get(cat, 0.3)

    # Token-based boost (crowded indicator vocabulary present in the expression)
    token_hits = sum(1 for t in _CROWDED_TOKENS if t in expr_lower)
    token_boost = min(token_hits * 0.05, 0.2)

    # Pool similarity — the dominant, evidence-based signal
    name_tokens = _tokenize_factor_expr(expr_lower)
    similar_count = 0
    exact_dupes = 0
    if pool_provided:
        for existing in pool:
            if existing == expr_lower:
                exact_dupes += 1
                similar_count += 1
                continue
            existing_tokens = _tokenize_factor_expr(existing)
            overlap = len(name_tokens & existing_tokens)
            union = len(name_tokens | existing_tokens) or 1
            jaccard = overlap / union
            if jaccard >= 0.6 or existing in expr_lower or expr_lower in existing:
                similar_count += 1
    similarity_ratio = round(similar_count / len(pool), 4) if pool_provided else 0.0

    if pool_provided:
        # Evidence-based: similarity ratio + exact-duplicate penalty dominate.
        similarity_component = min(similarity_ratio * 1.5 + exact_dupes * 0.3, 0.85)
        score = round(min(0.3 * category_prior + token_boost + similarity_component, 1.0), 3)
        assessment_basis = "pool_similarity"
        confidence = "high" if len(pool) >= 5 else "medium"
        band = "high" if score >= 0.7 else ("medium" if score >= 0.4 else "low")
    else:
        # No pool → cannot measure real crowding. Category prior only, capped below "high".
        score = round(min(0.4 * category_prior + token_boost, 0.6), 3)
        assessment_basis = "category_prior_only"
        confidence = "low"
        # Never escalate to "high" without pool evidence (avoids the F-N43-6 false alarm).
        band = "medium" if score >= 0.45 else "low"

    warnings: list[str] = []
    if not pool_provided:
        warnings.append(
            "未提供 existing_factor_pool，拥挤度仅基于类别先验估计，"
            "置信度低，不能据此判定真实拥挤"
        )
    if band == "high" and pool_provided:
        warnings.append(f"因子 '{factor_name}' 拥挤度高 ({score:.2f})，alpha 衰减风险大")
    if exact_dupes >= 1:
        warnings.append(f"池中有 {exact_dupes} 个与目标表达式完全相同的因子，存在重复")
    if similar_count >= 3:
        warnings.append(f"池中有 {similar_count} 个相似因子，需审视增量价值")

    return {
        "factor_name": factor_name,
        "crowding_score": score,
        "crowding_band": band,
        "category": cat or "unspecified",
        "category_prior": round(category_prior, 3),
        "token_hits": token_hits,
        "similar_factor_count": similar_count,
        "exact_duplicate_count": exact_dupes,
        "pool_size": len(pool),
        "similarity_ratio": similarity_ratio,
        "assessment_basis": assessment_basis,
        "confidence": confidence,
        "warnings": warnings,
    }


# ── 3. Model Drift / Calibration Drift ───────────────────────────────────────

def check_model_drift(
    model_name: str,
    current_metrics: dict[str, Any] | None = None,
    baseline_metrics: dict[str, Any] | None = None,
    *,
    brier_threshold: float = 0.03,
    ece_threshold: float = 0.05,
    ic_threshold: float = 0.02,
    stability_threshold: float = 0.50,
) -> dict[str, Any]:
    """Compare current model metrics against a baseline to detect drift.

    Parameters
    ----------
    current_metrics:
        Dict with optional keys: brier_score, ece, rank_ic_mean,
        stability_ratio, total_score.
    baseline_metrics:
        Same shape as current_metrics, representing historical baseline.

    Returns
    -------
    dict with drift_status, drift_dimensions, severity, action_recommended.
    """
    cur = dict(current_metrics or {})
    base = dict(baseline_metrics or {})

    dimensions: dict[str, dict[str, Any]] = {}
    issues: list[str] = []

    # F-N43-7 fix (诊断报告 §N43): 回显「未识别的指标键」而非静默丢弃。
    # 历史问题: 用户传 auc/ic/sharpe（明显漂移）但全维度 unknown，无任何告警，
    # 模型实际漂移却被判 drift_status=unknown / continue_monitoring。
    _recognized_keys = {"brier_score", "ece", "rank_ic_mean", "stability_ratio", "total_score"}
    _alias_hint = {
        "auc": "rank_ic_mean(或 stability_ratio)",
        "ic": "rank_ic_mean",
        "rank_ic": "rank_ic_mean",
        "sharpe": "total_score",
        "sharpe_ratio": "total_score",
        "brier": "brier_score",
    }
    unrecognized_keys = sorted(
        {str(k) for k in (set(cur) | set(base)) if str(k) not in _recognized_keys}
    )

    def _check(key: str, threshold: float, higher_is_better: bool = True) -> None:
        c = _safe_float(cur.get(key))
        b = _safe_float(base.get(key))
        if c is None or b is None:
            dimensions[key] = {"current": c, "baseline": b, "status": "unknown"}
            return
        delta = float(c) - float(b)
        if abs(delta) <= threshold:
            status = "stable"
        elif (delta > 0) == higher_is_better:
            status = "improved"
        else:
            status = "degraded"
            issues.append(key)
        dimensions[key] = {
            "current": round(c, 6),
            "baseline": round(b, 6),
            "delta": round(delta, 6),
            "status": status,
        }

    _check("brier_score", brier_threshold, higher_is_better=False)
    _check("ece", ece_threshold, higher_is_better=False)
    _check("rank_ic_mean", ic_threshold, higher_is_better=True)
    _check("stability_ratio", stability_threshold, higher_is_better=True)
    _check("total_score", 5.0, higher_is_better=True)

    overall = (
        "degraded" if len(issues) >= 2
        else "warning" if len(issues) == 1
        else "stable" if any(d["status"] != "unknown" for d in dimensions.values())
        else "unknown"
    )
    severity = (
        "high" if len(issues) >= 3
        else "medium" if overall in ("degraded", "warning")
        else "low"
    )
    action = (
        "retrain_or_retire" if overall == "degraded" and severity == "high"
        else "review_model" if overall in ("degraded", "warning")
        else "continue_monitoring"
    )

    warnings: list[str] = []
    if unrecognized_keys:
        hints = [
            f"{k}→{_alias_hint[k]}" for k in unrecognized_keys if k in _alias_hint
        ]
        msg = (
            f"以下指标键未被 model_drift 识别，已忽略: {unrecognized_keys}；"
            f"支持键: {sorted(_recognized_keys)}"
        )
        if hints:
            msg += f"；建议映射: {hints}"
        warnings.append(msg)
        # 全部维度 unknown 且用户确实传了指标 → 升级为 review，避免「漂移被判 unknown」
        if action == "continue_monitoring" and overall == "unknown":
            action = "review_input_keys"

    return {
        "model_name": model_name,
        "drift_status": overall,
        "severity": severity,
        "degraded_dimensions": issues,
        "dimensions": dimensions,
        "unrecognized_keys": unrecognized_keys,
        "warnings": warnings,
        "action_recommended": action,
    }


# ── 4. Strategy Runtime Health ────────────────────────────────────────────────

def check_strategy_health(
    strategy_id: str,
    posture_level: str = "safe",
    control_mode: str = "active",
    open_alert_count: int = 0,
    recovery_eligible: bool = False,
    max_drawdown_pct: float | None = None,
    days_since_last_trade: int | None = None,
) -> dict[str, Any]:
    """Assess strategy runtime health from posture and alert data.

    Returns
    -------
    dict with health_status, issues, action_recommended.
    """
    issues: list[str] = []

    posture = str(posture_level or "safe").strip().lower()
    mode = str(control_mode or "active").strip().lower()

    if posture in ("critical", "guarded"):
        issues.append(f"posture:{posture}")
    if mode in ("halted", "manual_stop"):
        issues.append(f"control_mode:{mode}")
    if open_alert_count > 0:
        issues.append(f"open_alerts:{open_alert_count}")
    if max_drawdown_pct is not None and max_drawdown_pct > 15.0:
        issues.append(f"high_drawdown:{max_drawdown_pct:.1f}%")
    if days_since_last_trade is not None and days_since_last_trade > 10:
        issues.append(f"stale_strategy:{days_since_last_trade}d_no_trade")

    if len(issues) >= 3 or mode in ("halted", "manual_stop"):
        health = "critical"
    elif len(issues) >= 1:
        health = "warning"
    else:
        health = "healthy"

    action = (
        "halt_and_review" if health == "critical"
        else "investigate" if health == "warning"
        else "normal_operation"
    )

    return {
        "strategy_id": strategy_id,
        "health_status": health,
        "posture_level": posture,
        "control_mode": mode,
        "open_alert_count": open_alert_count,
        "recovery_eligible": recovery_eligible,
        "max_drawdown_pct": max_drawdown_pct,
        "days_since_last_trade": days_since_last_trade,
        "issues": issues,
        "action_recommended": action,
    }


# ── 5. Online/Offline Consistency ─────────────────────────────────────────────

def check_online_offline_consistency(
    backtest_assumptions: dict[str, Any] | None = None,
    execution_assumptions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare backtest vs execution cost/fill assumptions.

    P0-3 fix (诊断报告 §S13): 仅当 caller 显式提供两组假设时才执行 consistency 比较。
    历史问题:caller 未提供 backtest_assumptions 时硬编码 0bps 默认值,生成虚假 inconsistent
    现状:未提供时返回 not_applicable,提供后才计算 gaps。

    Returns
    -------
    dict with consistency_status, gaps, warnings.
    consistency_status 取值:
      - "not_applicable": caller 未提供必要假设(non-blocking, 不算 inconsistency)
      - "consistent": 假设一致(差距 < 0.001)
      - "gap_detected": 单维度 gap
      - "inconsistent": 多维度 gap
    """
    # 当两组都未提供时返回 not_applicable,不参与一致性判定
    has_backtest_input = bool(backtest_assumptions)
    has_execution_input = bool(execution_assumptions)
    if not has_backtest_input and not has_execution_input:
        return {
            "consistency_status": "not_applicable",
            "backtest_assumptions": None,
            "execution_assumptions": None,
            "gaps": [],
            "gap_count": 0,
            "warnings": [],
            "reason": "no_assumptions_provided",
        }

    try:
        from .cost_model import resolve_cost_assumptions
        bt = resolve_cost_assumptions(dict(backtest_assumptions or {}), default_mode="backtest")
        ex = resolve_cost_assumptions(dict(execution_assumptions or {}), default_mode="execution")
    except Exception:
        bt = dict(backtest_assumptions or {
            "slippage_bps": 0.0,
            "market_impact_bps": 0.0,
            "commission_rate": 0.0003,
        })
        ex = dict(execution_assumptions or {
            "slippage_bps": 5.0,
            "market_impact_bps": 3.0,
            "commission_rate": 0.0003,
        })

    gaps: list[dict[str, Any]] = []
    warnings: list[str] = []

    # 仅在 caller 显式提供两组假设时,才把零值差距计入 inconsistency
    explicit_both_sides = has_backtest_input and has_execution_input

    for key in ("slippage_bps", "market_impact_bps", "commission_rate"):
        bt_val = _safe_float(bt.get(key), 0.0)
        ex_val = _safe_float(ex.get(key), 0.0)
        if bt_val is not None and ex_val is not None:
            delta = abs(float(ex_val) - float(bt_val))
            if delta > 0.001:
                gaps.append({
                    "parameter": key,
                    "backtest": bt_val,
                    "execution": ex_val,
                    "delta": round(delta, 4),
                })
                if explicit_both_sides:
                    if key == "slippage_bps" and bt_val == 0.0:
                        warnings.append("回测使用零滑点假设，与执行模式差距显著")
                    if key == "market_impact_bps" and bt_val == 0.0:
                        warnings.append("回测使用零市场冲击假设，AI 应注意此差距")

    if not explicit_both_sides and gaps:
        # 单边输入时不算 inconsistent,只是 partial_input
        return {
            "consistency_status": "partial_input",
            "backtest_assumptions": bt,
            "execution_assumptions": ex,
            "gaps": gaps,
            "gap_count": len(gaps),
            "warnings": ["consistency_check_skipped_due_to_partial_input"],
            "reason": "only_one_side_provided",
        }

    status = (
        "inconsistent" if len(gaps) >= 2
        else "gap_detected" if len(gaps) == 1
        else "consistent"
    )

    return {
        "consistency_status": status,
        "backtest_assumptions": bt,
        "execution_assumptions": ex,
        "gaps": gaps,
        "gap_count": len(gaps),
        "warnings": warnings,
    }


# ── GovernanceReport ──────────────────────────────────────────────────────────

@dataclass
class GovernanceReport:
    """Unified governance check result."""

    checked_at: str = ""
    target_type: str = "system"
    target_id: str | None = None
    factor_decay: dict[str, Any] | None = None
    crowding: dict[str, Any] | None = None
    model_drift: dict[str, Any] | None = None
    strategy_health: dict[str, Any] | None = None
    online_offline_consistency: dict[str, Any] | None = None
    overall_status: str = "unknown"
    issues: list[str] = field(default_factory=list)
    action_recommended: str = "review"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "checked_at": self.checked_at,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "overall_status": self.overall_status,
            "issues": self.issues,
            "action_recommended": self.action_recommended,
        }
        if self.factor_decay is not None:
            d["factor_decay"] = self.factor_decay
        if self.crowding is not None:
            d["crowding"] = self.crowding
        if self.model_drift is not None:
            d["model_drift"] = self.model_drift
        if self.strategy_health is not None:
            d["strategy_health"] = self.strategy_health
        if self.online_offline_consistency is not None:
            d["online_offline_consistency"] = self.online_offline_consistency
        return d


# ── GovernanceMonitor ─────────────────────────────────────────────────────────

class GovernanceMonitor:
    """Orchestrates all 5 governance checks.

    Usage::

        monitor = GovernanceMonitor()
        report = monitor.run_full_check(
            target_type="factor",
            target_id="momentum_20d",
            ic_history=[0.05, 0.04, 0.03, 0.02, 0.015],
        )
    """

    def run_full_check(
        self,
        *,
        target_type: str = "system",
        target_id: str | None = None,
        # Factor decay inputs
        ic_history: list[float] | None = None,
        factor_expression: str = "",
        factor_category: str | None = None,
        existing_factor_pool: list[str] | None = None,
        # Model drift inputs
        current_metrics: dict[str, Any] | None = None,
        baseline_metrics: dict[str, Any] | None = None,
        # Strategy health inputs
        posture_level: str = "safe",
        control_mode: str = "active",
        open_alert_count: int = 0,
        recovery_eligible: bool = False,
        max_drawdown_pct: float | None = None,
        days_since_last_trade: int | None = None,
        # Consistency inputs
        backtest_assumptions: dict[str, Any] | None = None,
        execution_assumptions: dict[str, Any] | None = None,
        # Feature flags
        include_factor_decay: bool = True,
        include_crowding: bool = True,
        include_model_drift: bool = True,
        include_strategy_health: bool = True,
        include_consistency: bool = True,
    ) -> GovernanceReport:
        """Run selected governance checks and return a unified report."""
        resolved_id = str(target_id or "system").strip()
        all_issues: list[str] = []

        # 1. Factor decay
        decay_result: dict[str, Any] | None = None
        if include_factor_decay:
            decay_result = check_factor_decay(
                resolved_id if target_type == "factor" else "system",
                ic_history,
            )
            if decay_result.get("decay_status") in ("decaying", "decayed"):
                all_issues.append(f"factor_decay:{decay_result['decay_status']}")

        # 2. Crowding
        crowding_result: dict[str, Any] | None = None
        if include_crowding:
            crowding_result = check_crowding(
                resolved_id if target_type == "factor" else "system",
                expression=factor_expression,
                category=factor_category,
                existing_pool=existing_factor_pool,
            )
            if crowding_result.get("crowding_band") == "high":
                all_issues.append(f"crowding:{crowding_result['crowding_band']}")

        # 3. Model drift
        drift_result: dict[str, Any] | None = None
        if include_model_drift:
            drift_result = check_model_drift(
                resolved_id if target_type == "model" else "system",
                current_metrics=current_metrics,
                baseline_metrics=baseline_metrics,
            )
            if drift_result.get("drift_status") in ("degraded", "warning"):
                all_issues.append(f"model_drift:{drift_result['drift_status']}")

        # 4. Strategy health
        health_result: dict[str, Any] | None = None
        if include_strategy_health:
            # F-N43-8 fix (诊断报告 §N43): strategy_health.strategy_id 应反映 target_id，
            # 而非在 target_type != strategy 时硬编码 "system"（导致多目标治理归属错标）。
            health_result = check_strategy_health(
                resolved_id,
                posture_level=posture_level,
                control_mode=control_mode,
                open_alert_count=open_alert_count,
                recovery_eligible=recovery_eligible,
                max_drawdown_pct=max_drawdown_pct,
                days_since_last_trade=days_since_last_trade,
            )
            if health_result.get("health_status") in ("critical", "warning"):
                all_issues.append(f"strategy_health:{health_result['health_status']}")

        # 5. Online/offline consistency
        consistency_result: dict[str, Any] | None = None
        if include_consistency:
            consistency_result = check_online_offline_consistency(
                backtest_assumptions=backtest_assumptions,
                execution_assumptions=execution_assumptions,
            )
            if consistency_result.get("consistency_status") == "inconsistent":
                all_issues.append("online_offline:inconsistent")

        # Overall status
        if len(all_issues) >= 3:
            overall = "critical"
            action = "immediate_review_required"
        elif len(all_issues) >= 1:
            overall = "warning"
            action = "investigate_flagged_dimensions"
        else:
            overall = "healthy"
            action = "continue_monitoring"

        return GovernanceReport(
            checked_at=_now_iso(),
            target_type=target_type,
            target_id=target_id,
            factor_decay=decay_result,
            crowding=crowding_result,
            model_drift=drift_result,
            strategy_health=health_result,
            online_offline_consistency=consistency_result,
            overall_status=overall,
            issues=all_issues,
            action_recommended=action,
        )


# Module-level default monitor
default_governance_monitor = GovernanceMonitor()
"""Module-level singleton for convenience imports."""
