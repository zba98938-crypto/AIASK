"""因子正交化器 — 确保因子池中的因子提供独立信息。

使用因子值层面的相关性检查（而非仅文本相似度），
解决"表达式不同但产生高相关因子值"的问题。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class OrthogonalResult:
    """正交化结果。"""
    is_orthogonal: bool
    max_correlation: float
    most_correlated_factor: str | None = None
    residual_ic: float | None = None
    independent_ratio: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_orthogonal": self.is_orthogonal,
            "max_correlation": round(self.max_correlation, 6),
            "most_correlated_factor": self.most_correlated_factor,
            "residual_ic": round(self.residual_ic, 6) if self.residual_ic is not None else None,
            "independent_ratio": round(self.independent_ratio, 6),
        }


class FactorOrthogonalizer:
    """因子正交化器。

    方法：
    1. 文本相似度（快速预筛）
    2. 因子值 Spearman 相关性（精确检查）
    3. Gram-Schmidt 残差 IC（增量贡献评估）
    """

    def __init__(self, threshold: float = 0.7):
        self.threshold = threshold

    async def check_orthogonality(
        self,
        new_factor_values: pd.Series,
        pool_factor_values: dict[str, pd.Series],
        *,
        forward_returns: pd.Series | None = None,
    ) -> OrthogonalResult:
        """检查新因子与池中因子的正交性。

        Args:
            new_factor_values: 新因子的值序列
            pool_factor_values: 池中因子的值序列 {factor_id: series}
            forward_returns: 前瞻收益（用于计算残差 IC）
        """
        if not pool_factor_values:
            return OrthogonalResult(is_orthogonal=True, max_correlation=0.0)

        max_corr = 0.0
        most_correlated = None

        for factor_id, existing_values in pool_factor_values.items():
            # 对齐数据
            aligned = pd.concat(
                [new_factor_values.rename("new"), existing_values.rename("existing")],
                axis=1,
            ).dropna()

            if len(aligned) < 30:
                continue

            # Spearman 相关性
            corr = abs(float(aligned["new"].corr(aligned["existing"], method="spearman")))
            if corr > max_corr:
                max_corr = corr
                most_correlated = factor_id

        is_orthogonal = max_corr < self.threshold

        # 计算残差 IC（如果提供了前瞻收益）
        residual_ic = None
        independent_ratio = 1.0
        if forward_returns is not None and pool_factor_values and is_orthogonal:
            residual_ic, independent_ratio = self._compute_residual_ic(
                new_factor_values, pool_factor_values, forward_returns
            )

        return OrthogonalResult(
            is_orthogonal=is_orthogonal,
            max_correlation=max_corr,
            most_correlated_factor=most_correlated,
            residual_ic=residual_ic,
            independent_ratio=independent_ratio,
        )

    def _compute_residual_ic(
        self,
        new_factor: pd.Series,
        pool_factors: dict[str, pd.Series],
        forward_returns: pd.Series,
    ) -> tuple[float, float]:
        """Gram-Schmidt 正交化后计算残差 IC。"""
        try:
            # 构建池因子矩阵
            pool_df = pd.DataFrame(pool_factors)
            aligned = pd.concat([new_factor.rename("new"), pool_df, forward_returns.rename("ret")], axis=1).dropna()

            if len(aligned) < 30:
                return None, 1.0

            # 对池因子做回归，取残差
            y = aligned["new"].values
            X = aligned[pool_df.columns].values

            # 最小二乘回归
            if X.shape[1] > 0 and X.shape[0] > X.shape[1]:
                try:
                    beta = np.linalg.lstsq(X, y, rcond=None)[0]
                    residual = y - X @ beta
                except np.linalg.LinAlgError:
                    residual = y
            else:
                residual = y

            # 残差的 Spearman IC
            residual_series = pd.Series(residual, index=aligned.index)
            residual_ic = float(residual_series.corr(aligned["ret"], method="spearman"))

            # 独立比率
            original_std = float(np.std(y))
            residual_std = float(np.std(residual))
            independent_ratio = residual_std / original_std if original_std > 1e-10 else 0.0

            return residual_ic, independent_ratio

        except Exception as exc:
            logger.debug("Orthogonalizer: residual IC computation failed: %s", exc)
            return None, 1.0
