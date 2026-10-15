"""策略工厂上架策略淘汰检查。"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import List

from .utils import _update_strategy_status as _local_update_strategy_status
from ..domain.constants import ELIMINATION_CONCURRENCY

logger = logging.getLogger(__name__)

async def _update_strategy_status(*args, **kwargs):
    return await _local_update_strategy_status(*args, **kwargs)


class EliminationChecker:
    """检查已上架策略是否应该淘汰。"""

    _REGIME_MAP = {
        "momentum": ("greed", "extreme_greed"),
        "growth_factor": ("greed", "extreme_greed", "neutral"),
        "value_factor": ("fear", "extreme_fear", "neutral"),
        "rsi": ("fear", "extreme_fear"),
        "macro_timing": ("fear", "extreme_fear", "neutral"),
        "ma_cross": ("neutral", "greed", "extreme_greed"),
        "quality_factor": ("neutral", "greed", "extreme_greed"),
        "multi_factor": ("neutral", "fear", "greed", "extreme_fear", "extreme_greed"),
    }

    async def check(self, db, current_fg_level: str = "neutral") -> List[dict]:
        eliminated: List[dict] = []
        try:
            listed = await db.list_strategies("listed", limit=500)
        except Exception:
            return eliminated

        sem = asyncio.Semaphore(ELIMINATION_CONCURRENCY)

        async def _check_one(strategy: dict) -> dict | None:
            async with sem:
                return await self._check_strategy(db, strategy, current_fg_level)

        results = await asyncio.gather(*[_check_one(strategy) for strategy in list(listed or [])], return_exceptions=True)
        for item in results:
            if isinstance(item, BaseException):
                logger.debug("EliminationChecker: task failed: %s", item)
                continue
            if item:
                eliminated.append(item)
        return eliminated

    async def _check_strategy(self, db, strategy: dict, current_fg_level: str) -> dict | None:
        try:
            red_flags: List[str] = []
            evidence_warnings: List[str] = []
            metrics_list = await db.get_strategy_metrics(strategy["id"])
            period_priority = {"all": 0, "backtest": 1}
            period_candidates = [row for row in metrics_list if row.get("period") in period_priority]
            period_candidates.sort(key=lambda row: period_priority.get(row.get("period"), 99))
            backtest_metrics = period_candidates[0] if period_candidates else {}
            validation_metrics = next((row for row in metrics_list if row.get("period") == "validation"), {})
            risk_metrics = next((row for row in metrics_list if row.get("period") == "risk"), {})
            quality_report = None
            get_quality_report = getattr(db, "get_strategy_quality_report", None)
            if callable(get_quality_report):
                try:
                    quality_report = await get_quality_report(strategy["id"])
                except TypeError:
                    quality_report = None
            if quality_report:
                validation_metrics = quality_report.get("validation_report") or validation_metrics
                risk_metrics = quality_report.get("risk_report") or risk_metrics

            def _opt_float(payload: dict, key: str):
                if key not in payload or payload.get(key) is None:
                    return None
                try:
                    return float(payload[key])
                except (TypeError, ValueError) as _swallowed:
                    logger.warning("packages/strategy-factory/src/strategy_factory/application/elimination.py:83: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                    return None

            max_drawdown_raw = _opt_float(backtest_metrics, "max_drawdown")
            max_drawdown = abs(max_drawdown_raw) if max_drawdown_raw is not None else None
            sharpe_ratio = _opt_float(backtest_metrics, "sharpe_ratio")
            win_rate = _opt_float(backtest_metrics, "win_rate")
            validation_grade = validation_metrics.get("grade") or validation_metrics.get("rating", {}).get("grade")
            var_percent = _opt_float(risk_metrics, "var_percent")
            cvar_percent = _opt_float(risk_metrics, "cvar_percent")
            stress_loss_percent = _opt_float(risk_metrics, "stress_loss_percent")

            if max_drawdown is None:
                red_flags.append("缺失回撤指标")
            elif max_drawdown > 0.30:
                red_flags.append(f"回撤{max_drawdown:.1%}>30%")
            if sharpe_ratio is None:
                red_flags.append("缺失Sharpe指标")
            elif sharpe_ratio < 0:
                red_flags.append(f"Sharpe {sharpe_ratio:.2f}<0")
            if win_rate is not None and win_rate < 0.30:
                red_flags.append(f"胜率{win_rate:.1%}<30%")
            if validation_grade == "D":
                red_flags.append("验证评级为D")
            if var_percent is None and cvar_percent is None and stress_loss_percent is None:
                # Incomplete risk evidence: warn only (not automatic probation).
                evidence_warnings.append("缺失风险指标(VaR/CVaR/stress)")
            else:
                if var_percent is not None and var_percent > 4.0:
                    red_flags.append(f"VaR {var_percent:.2f}%>4%")
                if cvar_percent is not None and cvar_percent > 6.0:
                    red_flags.append(f"CVaR {cvar_percent:.2f}%>6%")
                if stress_loss_percent is not None and stress_loss_percent <= -25.0:
                    red_flags.append(f"压力测试损失{stress_loss_percent:.1f}%")

            try:
                signal_stats = await db.get_signal_stats(strategy["id"])
                hit_rates = signal_stats.get("hit_rate", {})
                total_signals = signal_stats.get("total_signals", 0)
                if total_signals >= 10:
                    hit_rate_5d = hit_rates.get(5, hit_rates.get("5", None))
                    if hit_rate_5d is not None and float(hit_rate_5d) < 0.30:
                        red_flags.append(f"5日信号命中率{float(hit_rate_5d):.1%}<30%")
            except Exception as exc:
                logger.debug("EliminationChecker: signal stats unavailable for %s: %s", strategy.get("id"), exc)

            strategy_type = strategy.get("strategy_type", "")
            suitable_regimes = self._REGIME_MAP.get(strategy_type)
            # PR-S10: regime 不匹配从 red_flag 降为 warning（周期性环境切换不算根本性退化）
            regime_warnings: List[str] = list(evidence_warnings)
            if suitable_regimes and current_fg_level and current_fg_level not in suitable_regimes:
                regime_warnings.append(
                    f"{strategy_type}策略不适合当前{current_fg_level}环境（warning，不计入淘汰决策）"
                )

            # PR-S10: 年龄 / 累计交易门槛 —— 新策略/样本不足时不强淘汰
            import os
            min_age_days = int(os.getenv("STRATEGY_FACTORY_ELIMINATION_MIN_AGE_DAYS", "14") or 14)
            min_trade_count = int(os.getenv("STRATEGY_FACTORY_ELIMINATION_MIN_TRADE_COUNT", "10") or 10)
            age_days_value: float | None = None
            try:
                created_at = strategy.get("created_at") or strategy.get("listed_at")
                if created_at:
                    from datetime import datetime as _dt
                    if hasattr(created_at, "date"):
                        age_days_value = (date.today() - created_at.date()).days
                    else:
                        parsed = _dt.fromisoformat(str(created_at).replace("Z", "+00:00"))
                        age_days_value = (date.today() - parsed.date()).days
            except Exception:
                age_days_value = None
            trade_count_value = int(backtest_metrics.get("trade_count") or backtest_metrics.get("trades_count") or 0)
            age_protection = (
                (age_days_value is not None and age_days_value < min_age_days)
                or (trade_count_value > 0 and trade_count_value < min_trade_count)
            )

            fatal = max_drawdown > 0.30
            should_eliminate = fatal or (len(red_flags) >= 2)
            # 优化：渐进降级 — 单个非致命红旗进入观察期而非直接淘汰
            should_probation = (not fatal) and (len(red_flags) == 1)
            # 年龄/样本量不足 → 不允许直接淘汰，最多观察期
            if age_protection and not fatal:
                if should_eliminate:
                    should_probation = True
                should_eliminate = False

            if should_eliminate and red_flags:
                reason = "淘汰: " + "; ".join(red_flags)
                await _update_strategy_status(
                    db,
                    strategy["id"],
                    "deprecated",
                    actor_id="elimination_checker",
                    reason="elimination_checker_triggered",
                    metadata={"red_flags": red_flags, "fg_level": current_fg_level},
                )
                try:
                    await db.save_elimination_log(strategy["id"], date.today(), red_flags, reason)
                except Exception as exc:
                    logger.debug("EliminationChecker: save_elimination_log failed for %s: %s", strategy.get("id"), exc)
                return {"id": strategy["id"], "red_flags": red_flags, "reason": reason, "action": "deprecated"}
            elif should_probation and red_flags:
                reason = "观察期: " + "; ".join(red_flags)
                # 进入观察期而非直接淘汰（如果 DB 支持 probation 状态）
                try:
                    await _update_strategy_status(
                        db,
                        strategy["id"],
                        "probation",
                        actor_id="elimination_checker",
                        reason="elimination_checker_probation",
                        metadata={"red_flags": red_flags, "fg_level": current_fg_level},
                    )
                except Exception:
                    # 如果 probation 状态不支持，降级为仅记录警告
                    logger.info("EliminationChecker: probation status not supported for %s, logging warning only", strategy.get("id"))
                return {
                    "id": strategy["id"],
                    "red_flags": red_flags,
                    "regime_warnings": regime_warnings,
                    "age_protection": bool(age_protection),
                    "age_days": age_days_value,
                    "trade_count": trade_count_value,
                    "reason": reason,
                    "action": "probation",
                }
            # 没有 red_flag 但有 regime warning：返回 None（不淘汰、不 probation），但记录 warning 供 summary 统计
            if regime_warnings:
                return {
                    "id": strategy["id"],
                    "red_flags": [],
                    "regime_warnings": regime_warnings,
                    "age_protection": bool(age_protection),
                    "age_days": age_days_value,
                    "trade_count": trade_count_value,
                    "reason": "; ".join(regime_warnings),
                    "action": "warning",
                }
        except Exception as exc:
            logger.debug("EliminationChecker: error checking %s: %s", strategy.get("id"), exc)
        return None
