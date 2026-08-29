"""因子组合优化器 — 为策略工厂提供最优因子权重。

方法：
1. 等权重 (equal_weight)
2. IC 加权 (ic_weight)
3. IC-IR 加权 (icir_weight)
4. 最大化组合 Sharpe (max_sharpe)
5. 风险平价 (risk_parity)
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class FactorPortfolioOptimizer:
    """因子组合优化器。"""

    SUPPORTED_METHODS = ("equal_weight", "ic_weight", "icir_weight", "max_sharpe", "risk_parity")

    def optimize(
        self,
        factor_metrics: dict[str, dict[str, float]],
        method: str = "icir_weight",
        *,
        factor_correlation: pd.DataFrame | None = None,
    ) -> dict[str, float]:
        """计算因子组合权重。

        Args:
            factor_metrics: {factor_id: {"ic_mean": ..., "ic_std": ..., "ic_ir": ...}}
            method: 优化方法
            factor_correlation: 因子间相关性矩阵（max_sharpe/risk_parity 需要）

        Returns:
            {factor_id: weight}
        """
        if not factor_metrics:
            return {}

        if method == "equal_weight":
            return self._equal_weight(factor_metrics)
        elif method == "ic_weight":
            return self._ic_weight(factor_metrics)
        elif method == "icir_weight":
            return self._icir_weight(factor_metrics)
        elif method == "max_sharpe":
            return self._max_sharpe(factor_metrics, factor_correlation)
        elif method == "risk_parity":
            return self._risk_parity(factor_metrics, factor_correlation)
        else:
            logger.warning("Unknown method %s, falling back to icir_weight", method)
            return self._icir_weight(factor_metrics)

    def _equal_weight(self, metrics: dict[str, dict[str, float]]) -> dict[str, float]:
        """等权重。"""
        n = len(metrics)
        return {fid: 1.0 / n for fid in metrics}

    def _ic_weight(self, metrics: dict[str, dict[str, float]]) -> dict[str, float]:
        """IC 绝对值加权。"""
        ic_abs = {fid: abs(m.get("ic_mean", 0.0)) for fid, m in metrics.items()}
        total = sum(ic_abs.values())
        if total <= 0:
            return self._equal_weight(metrics)
        return {fid: v / total for fid, v in ic_abs.items()}

    def _icir_weight(self, metrics: dict[str, dict[str, float]]) -> dict[str, float]:
        """IC-IR 加权（IC 均值 / IC 标准差）。"""
        icir = {}
        for fid, m in metrics.items():
            ir = abs(m.get("ic_ir", 0.0))
            if ir <= 0:
                ic_mean = abs(m.get("ic_mean", 0.0))
                ic_std = m.get("ic_std", 1.0)
                ir = ic_mean / ic_std if ic_std > 0 else 0.0
            icir[fid] = max(0.0, ir)

        total = sum(icir.values())
        if total <= 0:
            return self._equal_weight(metrics)
        return {fid: v / total for fid, v in icir.items()}

    def _max_sharpe(
        self,
        metrics: dict[str, dict[str, float]],
        correlation: pd.DataFrame | None,
    ) -> dict[str, float]:
        """最大化组合 Sharpe（均值-方差优化）。"""
        factor_ids = list(metrics.keys())
        n = len(factor_ids)

        if n <= 1 or correlation is None:
            return self._icir_weight(metrics)

        try:
            # 构建期望收益向量（用 IC 均值代理）
            mu = np.array([metrics[fid].get("ic_mean", 0.0) for fid in factor_ids])

            # 构建协方差矩阵
            stds = np.array([metrics[fid].get("ic_std", 0.01) for fid in factor_ids])
            corr_matrix = np.eye(n)
            for i, fi in enumerate(factor_ids):
                for j, fj in enumerate(factor_ids):
                    if i != j and fi in correlation.index and fj in correlation.columns:
                        corr_matrix[i, j] = correlation.loc[fi, fj]

            cov_matrix = np.outer(stds, stds) * corr_matrix

            # 解析解：w* = Σ^{-1} μ / (1^T Σ^{-1} μ)
            cov_inv = np.linalg.pinv(cov_matrix)
            raw_weights = cov_inv @ mu
            total = np.sum(raw_weights)

            if total > 0:
                weights = raw_weights / total
                # 约束非负
                weights = np.maximum(weights, 0.0)
                total = np.sum(weights)
                if total > 0:
                    weights = weights / total
                else:
                    return self._icir_weight(metrics)
            else:
                return self._icir_weight(metrics)

            return {fid: float(w) for fid, w in zip(factor_ids, weights)}

        except Exception as exc:
            logger.debug("max_sharpe optimization failed: %s, falling back", exc)
            return self._icir_weight(metrics)

    def _risk_parity(
        self,
        metrics: dict[str, dict[str, float]],
        correlation: pd.DataFrame | None,
    ) -> dict[str, float]:
        """风险平价：每个因子贡献相等的风险。"""
        factor_ids = list(metrics.keys())
        n = len(factor_ids)

        # 简化版：用 IC 标准差的倒数作为权重
        inv_vol = {}
        for fid, m in metrics.items():
            ic_std = m.get("ic_std", 0.01)
            inv_vol[fid] = 1.0 / max(ic_std, 0.001)

        total = sum(inv_vol.values())
        if total <= 0:
            return self._equal_weight(metrics)
        return {fid: v / total for fid, v in inv_vol.items()}
