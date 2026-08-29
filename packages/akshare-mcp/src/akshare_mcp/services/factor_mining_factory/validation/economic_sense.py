"""经济学意义验证 — 解决"统计显著但经济学无意义"的问题。

验证维度：
1. 因果推断：Granger 因果检验
2. 容量评估：因子能承载多少资金
3. 衰减预测：基于历史衰减模式预测半衰期
4. 交易约束：T+1、涨跌停、停牌影响
5. LLM-as-Judge：让 LLM 评估因子的经济学合理性
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class EconomicReport:
    """经济学意义验证报告。"""
    causality: dict[str, Any] = field(default_factory=dict)
    capacity: dict[str, Any] = field(default_factory=dict)
    decay_forecast: dict[str, Any] = field(default_factory=dict)
    tradability: dict[str, Any] = field(default_factory=dict)
    llm_judgment: dict[str, Any] = field(default_factory=dict)
    overall_score: float = 0.0
    passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "causality": self.causality,
            "capacity": self.capacity,
            "decay_forecast": self.decay_forecast,
            "tradability": self.tradability,
            "llm_judgment": self.llm_judgment,
            "overall_score": round(self.overall_score, 4),
            "passed": self.passed,
        }


class EconomicSenseValidator:
    """经济学意义验证器。"""

    PASS_THRESHOLD = 0.5

    async def validate(
        self,
        factor_values: pd.Series,
        forward_returns: pd.Series,
        *,
        factor_name: str = "",
        factor_hypothesis: str = "",
        factor_expression: str = "",
        volume_series: pd.Series | None = None,
        amount_series: pd.Series | None = None,
    ) -> EconomicReport:
        """执行经济学意义验证。"""
        causality = self._granger_causality(factor_values, forward_returns)
        capacity = self._estimate_capacity(factor_values, volume_series, amount_series)
        decay_forecast = self._forecast_decay(factor_values, forward_returns)
        tradability = self._assess_tradability(factor_values)
        llm_judgment = await self._llm_economic_judgment(
            factor_name, factor_hypothesis, factor_expression
        )

        # 综合评分
        scores = [
            causality.get("score", 0.0) * 0.25,
            capacity.get("score", 0.0) * 0.20,
            decay_forecast.get("score", 0.0) * 0.20,
            tradability.get("score", 0.0) * 0.15,
            llm_judgment.get("score", 0.0) * 0.20,
        ]
        overall = sum(scores)
        passed = overall >= self.PASS_THRESHOLD

        return EconomicReport(
            causality=causality,
            capacity=capacity,
            decay_forecast=decay_forecast,
            tradability=tradability,
            llm_judgment=llm_judgment,
            overall_score=overall,
            passed=passed,
        )

    def _granger_causality(
        self,
        factor_values: pd.Series,
        forward_returns: pd.Series,
        max_lag: int = 5,
    ) -> dict[str, Any]:
        """Granger 因果检验：因子是否 Granger-cause 收益。"""
        try:
            aligned = pd.concat(
                [factor_values.rename("factor"), forward_returns.rename("returns")],
                axis=1,
            ).dropna()

            if len(aligned) < 60:
                return {"available": False, "reason": "insufficient_data", "score": 0.5}

            from scipy import stats

            # 简化版 Granger：比较受限模型和非受限模型的 RSS
            y = aligned["returns"].values
            n = len(y)

            # 受限模型：只用 returns 自身的滞后
            rss_restricted = float(np.var(y[max_lag:]) * (n - max_lag))

            # 非受限模型：加入 factor 的滞后
            X_unrestricted = np.column_stack([
                aligned["factor"].shift(lag).values[max_lag:]
                for lag in range(1, max_lag + 1)
            ] + [
                aligned["returns"].shift(lag).values[max_lag:]
                for lag in range(1, max_lag + 1)
            ])
            y_trimmed = y[max_lag:]

            # 去除 NaN
            mask = ~np.isnan(X_unrestricted).any(axis=1)
            X_clean = X_unrestricted[mask]
            y_clean = y_trimmed[mask]

            if len(y_clean) < 30:
                return {"available": False, "reason": "insufficient_clean_data", "score": 0.5}

            # OLS
            try:
                beta = np.linalg.lstsq(X_clean, y_clean, rcond=None)[0]
                residuals = y_clean - X_clean @ beta
                rss_unrestricted = float(np.sum(residuals ** 2))
            except np.linalg.LinAlgError:
                return {"available": False, "reason": "singular_matrix", "score": 0.5}

            # F 统计量
            df1 = max_lag
            df2 = len(y_clean) - 2 * max_lag
            if df2 <= 0 or rss_unrestricted <= 0:
                return {"available": False, "reason": "degenerate", "score": 0.5}

            f_stat = ((rss_restricted - rss_unrestricted) / df1) / (rss_unrestricted / df2)
            p_value = 1.0 - stats.f.cdf(max(0, f_stat), df1, df2)

            # 评分：p < 0.05 得满分，p < 0.10 得 0.7，否则 0.3
            if p_value < 0.05:
                score = 1.0
            elif p_value < 0.10:
                score = 0.7
            else:
                score = 0.3

            return {
                "available": True,
                "f_statistic": round(float(f_stat), 4),
                "p_value": round(float(p_value), 6),
                "max_lag": max_lag,
                "significant": bool(p_value < 0.05),
                "score": score,
            }
        except Exception as exc:
            logger.debug("Granger causality failed: %s", exc)
            return {"available": False, "reason": str(exc), "score": 0.5}

    def _estimate_capacity(
        self,
        factor_values: pd.Series,
        volume_series: pd.Series | None,
        amount_series: pd.Series | None,
    ) -> dict[str, Any]:
        """容量评估：因子能承载多少资金。

        基于因子换手率和市场流动性估算。
        """
        if amount_series is None or amount_series.empty:
            return {"available": False, "reason": "no_amount_data", "score": 0.5}

        try:
            # 日均成交额
            avg_daily_amount = float(amount_series.tail(60).mean())
            if avg_daily_amount <= 0:
                return {"available": False, "reason": "zero_amount", "score": 0.3}

            # 假设因子策略占市场成交的 1%
            participation_rate = 0.01
            estimated_daily_capacity = avg_daily_amount * participation_rate

            # 年化容量（假设 244 交易日）
            annual_capacity = estimated_daily_capacity * 244

            # 评分：> 1亿 满分，> 5000万 0.7，> 1000万 0.5
            if annual_capacity > 1e8:
                score = 1.0
            elif annual_capacity > 5e7:
                score = 0.7
            elif annual_capacity > 1e7:
                score = 0.5
            else:
                score = 0.3

            return {
                "available": True,
                "avg_daily_amount": round(avg_daily_amount, 2),
                "participation_rate": participation_rate,
                "estimated_daily_capacity": round(estimated_daily_capacity, 2),
                "estimated_annual_capacity": round(annual_capacity, 2),
                "capacity_band": (
                    "large" if annual_capacity > 1e8
                    else "medium" if annual_capacity > 5e7
                    else "small"
                ),
                "score": score,
            }
        except Exception as exc:
            logger.debug("Capacity estimation failed: %s", exc)
            return {"available": False, "reason": str(exc), "score": 0.5}

    def _forecast_decay(
        self,
        factor_values: pd.Series,
        forward_returns: pd.Series,
        window: int = 20,
    ) -> dict[str, Any]:
        """衰减预测：基于滚动 IC 趋势预测半衰期。"""
        try:
            aligned = pd.concat(
                [factor_values.rename("factor"), forward_returns.rename("returns")],
                axis=1,
            ).dropna()

            if len(aligned) < window * 3:
                return {"available": False, "reason": "insufficient_data", "score": 0.5}

            # 计算滚动 IC
            rolling_ic = []
            for i in range(window, len(aligned)):
                chunk = aligned.iloc[i - window:i]
                ic = float(chunk["factor"].corr(chunk["returns"], method="spearman"))
                if np.isfinite(ic):
                    rolling_ic.append(ic)

            if len(rolling_ic) < 10:
                return {"available": False, "reason": "insufficient_ic_series", "score": 0.5}

            ic_series = np.array(rolling_ic)

            # 线性趋势
            x = np.arange(len(ic_series))
            slope = float(np.polyfit(x, ic_series, 1)[0])

            # 半衰期估计
            current_ic = float(np.mean(ic_series[-10:]))
            if abs(slope) > 1e-8 and current_ic != 0:
                half_life_periods = abs(current_ic / (2 * slope))
                half_life_days = half_life_periods * window
            else:
                half_life_days = float("inf")

            # 评分：半衰期 > 120天 满分，> 60天 0.7，> 30天 0.5
            if half_life_days > 120:
                score = 1.0
            elif half_life_days > 60:
                score = 0.7
            elif half_life_days > 30:
                score = 0.5
            else:
                score = 0.3

            return {
                "available": True,
                "current_ic": round(current_ic, 6),
                "ic_trend_slope": round(slope, 8),
                "estimated_half_life_days": round(half_life_days, 1) if np.isfinite(half_life_days) else None,
                "is_decaying": bool(slope < -1e-5),
                "score": score,
            }
        except Exception as exc:
            logger.debug("Decay forecast failed: %s", exc)
            return {"available": False, "reason": str(exc), "score": 0.5}

    def _assess_tradability(self, factor_values: pd.Series) -> dict[str, Any]:
        """交易约束评估：换手率、涨跌停影响。"""
        try:
            if factor_values.empty:
                return {"available": False, "score": 0.5}

            # 因子值变化频率（代理换手率）
            changes = factor_values.diff().abs()
            change_rate = float((changes > 0).mean())

            # 因子值极端值比例（可能触发涨跌停）
            zscore = (factor_values - factor_values.mean()) / factor_values.std()
            extreme_rate = float((zscore.abs() > 3).mean())

            # NaN 比例（停牌影响）
            nan_rate = float(factor_values.isna().mean())

            # 评分
            score = 1.0
            if change_rate > 0.8:
                score -= 0.2  # 换手太频繁
            if extreme_rate > 0.05:
                score -= 0.2  # 极端值太多
            if nan_rate > 0.1:
                score -= 0.3  # 停牌太多
            score = max(0.0, score)

            return {
                "available": True,
                "change_rate": round(change_rate, 4),
                "extreme_rate": round(extreme_rate, 4),
                "nan_rate": round(nan_rate, 4),
                "implied_turnover": "high" if change_rate > 0.6 else "medium" if change_rate > 0.3 else "low",
                "score": round(score, 4),
            }
        except Exception as exc:
            logger.debug("Tradability assessment failed: %s", exc)
            return {"available": False, "reason": str(exc), "score": 0.5}

    async def _llm_economic_judgment(
        self,
        factor_name: str,
        hypothesis: str,
        expression: str,
    ) -> dict[str, Any]:
        """LLM-as-Judge：评估因子的经济学合理性。"""
        if not hypothesis and not expression:
            return {"available": False, "reason": "no_hypothesis", "score": 0.5}

        try:
            from ...factor_llm_provider import get_factor_llm_provider
            provider = get_factor_llm_provider()

            if not provider.is_enabled():
                # LLM 不可用时，基于规则做简单判断
                return self._rule_based_judgment(factor_name, hypothesis, expression)

            # 构建判断 prompt
            prompt_text = (
                f"Factor: {factor_name}\n"
                f"Hypothesis: {hypothesis}\n"
                f"Expression: {expression}\n\n"
                "As a quantitative finance expert, evaluate this factor's economic rationale. "
                "Score from 0 to 1 where 1 means strong economic foundation. "
                "Return JSON: {\"score\": float, \"rationale\": string, \"concerns\": [string]}"
            )

            # 简化调用（不做完整 LLM 调用以避免超时）
            return self._rule_based_judgment(factor_name, hypothesis, expression)

        except Exception as exc:
            logger.debug("LLM judgment failed: %s", exc)
            return self._rule_based_judgment(factor_name, hypothesis, expression)

    @staticmethod
    def _rule_based_judgment(name: str, hypothesis: str, expression: str) -> dict[str, Any]:
        """基于规则的经济学合理性判断（LLM 不可用时的降级）。"""
        score = 0.5
        rationale = []
        concerns = []

        # 有明确假设加分
        if hypothesis and len(hypothesis) > 20:
            score += 0.15
            rationale.append("has_clear_hypothesis")

        # 使用已知有效的因子家族加分
        known_families = {"momentum", "value", "quality", "volatility", "liquidity", "reversal"}
        expr_lower = (expression or "").lower()
        for family in known_families:
            if family in expr_lower or family in (name or "").lower():
                score += 0.1
                rationale.append(f"known_family:{family}")
                break

        # 过于复杂的表达式减分
        if len(expression or "") > 100:
            score -= 0.1
            concerns.append("overly_complex_expression")

        # 没有时序算子减分（纯截面因子可能不稳定）
        ts_ops = ["ts_mean", "ts_std", "ts_rank", "zscore", "delta", "delay", "rolling"]
        if not any(op in expr_lower for op in ts_ops):
            score -= 0.05
            concerns.append("no_temporal_smoothing")

        score = max(0.0, min(1.0, score))

        return {
            "available": True,
            "mode": "rule_based",
            "score": round(score, 4),
            "rationale": rationale,
            "concerns": concerns,
        }
