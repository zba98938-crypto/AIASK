"""因子分析 - IC计算、分组回测、单调性检查"""
import numpy as np
from typing import List, Dict, Any


class AnalysisFactorsMixin:
    """因子分析混入类 - IC计算和分组回测"""

    @staticmethod
    def calculate_factor_ic(
        factor_values: List[float],
        future_returns: List[float]
    ) -> Dict[str, float]:
        """计算因子IC（信息系数）"""
        if len(factor_values) != len(future_returns):
            return {'ic': 0.0, 'rank_ic': 0.0}

        factor_arr = np.array(factor_values)
        returns_arr = np.array(future_returns)

        # 去除NaN
        mask = ~(np.isnan(factor_arr) | np.isnan(returns_arr))
        factor_arr = factor_arr[mask]
        returns_arr = returns_arr[mask]

        if len(factor_arr) < 10:
            return {'ic': 0.0, 'rank_ic': 0.0}

        # Pearson相关系数（IC）
        ic = np.corrcoef(factor_arr, returns_arr)[0, 1]

        # Spearman秩相关系数（Rank IC）
        from scipy.stats import spearmanr
        rank_ic, _ = spearmanr(factor_arr, returns_arr)

        return {
            'ic': float(ic) if not np.isnan(ic) else 0.0,
            'rank_ic': float(rank_ic) if not np.isnan(rank_ic) else 0.0,
        }

    @staticmethod
    def backtest_factor(
        codes: List[str],
        factor_values: Dict[str, float],
        returns_dict: Dict[str, List[float]],
        groups: int = 5,
        holding_days: int = 20
    ) -> Dict[str, Any]:
        """因子分组回测"""
        sorted_codes = sorted(codes, key=lambda c: factor_values.get(c, 0))
        group_size = len(sorted_codes) // groups

        group_returns = []

        for i in range(groups):
            start_idx = i * group_size
            end_idx = start_idx + group_size if i < groups - 1 else len(sorted_codes)
            group_codes = sorted_codes[start_idx:end_idx]

            group_ret = []
            for code in group_codes:
                if code in returns_dict:
                    rets = returns_dict[code][:holding_days]
                    if rets:
                        group_ret.append(np.mean(rets))

            avg_return = np.mean(group_ret) if group_ret else 0.0
            group_returns.append(avg_return)

        long_short_return = group_returns[-1] - group_returns[0] if len(group_returns) >= 2 else 0.0

        return {
            'group_returns': group_returns,
            'long_short_return': long_short_return,
            'monotonicity': _check_monotonicity(group_returns),
        }


def _check_monotonicity(values: List[float]) -> float:
    """检查单调性（1表示完全单调递增，-1表示完全单调递减）"""
    if len(values) < 2:
        return 0.0
    increasing = sum(1 for i in range(len(values)-1) if values[i+1] > values[i])
    decreasing = sum(1 for i in range(len(values)-1) if values[i+1] < values[i])
    total = len(values) - 1
    return (increasing - decreasing) / total if total > 0 else 0.0
