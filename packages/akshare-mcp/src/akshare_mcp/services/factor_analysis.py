"""
因子分析服务 - IC计算、分组回测、因子评估
"""

import numpy as np
import pandas as pd
from typing import List, Dict, Any, Optional, Tuple
from scipy import stats
from numba import jit

# 中性化截面回归的最小自由度（有效样本数 - 设计矩阵列数）。
# 低于此值跳过中性化（灰度测试发现的小样本翻号风险防护）。可经 env 调整。
import os as _os
try:
    _NEUTRALIZE_MIN_DOF = max(1, int(_os.getenv("FACTOR_NEUTRALIZE_MIN_DOF", "5") or 5))
except (TypeError, ValueError):
    _NEUTRALIZE_MIN_DOF = 5


class FactorAnalyzer:
    """因子分析器"""

    @staticmethod
    def klines_to_ndarrays(
        klines: List[Dict[str, Any]],
        *,
        chronological: bool = False,
        include_volume: bool = False,
        include_returns: bool = False,
    ) -> Dict[str, np.ndarray]:
        """
        Standardize kline rows into ndarray panels for quant pipeline reuse.
        """
        closes: List[float] = []
        volumes: List[float] = []

        for row in klines or []:
            if not isinstance(row, dict):
                continue
            close_raw = row.get("close")
            if close_raw is None:
                continue
            try:
                close_val = float(close_raw)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(close_val):
                continue
            closes.append(close_val)

            if include_volume:
                vol_raw = row.get("volume", 0.0)
                try:
                    vol_val = float(vol_raw or 0.0)
                except (TypeError, ValueError):
                    vol_val = 0.0
                if not np.isfinite(vol_val):
                    vol_val = 0.0
                volumes.append(vol_val)

        close_arr = np.asarray(closes, dtype=np.float64)
        if chronological and close_arr.size > 0:
            close_arr = close_arr[::-1].copy()

        result: Dict[str, np.ndarray] = {"closes": close_arr}

        if include_volume:
            vol_arr = np.asarray(volumes, dtype=np.float64)
            if chronological and vol_arr.size > 0:
                vol_arr = vol_arr[::-1].copy()
            if vol_arr.shape[0] != close_arr.shape[0]:
                vol_arr = np.zeros(close_arr.shape[0], dtype=np.float64)
            result["volumes"] = vol_arr

        if include_returns:
            if close_arr.size < 2:
                result["returns"] = np.empty(0, dtype=np.float64)
            else:
                prev = close_arr[:-1]
                curr = close_arr[1:]
                ret_arr = np.empty(curr.shape[0], dtype=np.float64)
                ret_arr.fill(np.nan)
                valid = prev > 0
                ret_arr[valid] = (curr[valid] - prev[valid]) / prev[valid]
                result["returns"] = ret_arr

        return result

    @staticmethod
    def _neutralize_factor_values(
        factor_values: np.ndarray,
        industry: Optional[List[Any]] = None,
        market_cap: Optional[List[float]] = None,
        beta: Optional[List[float]] = None,
        enable_neutralization: bool = True,
    ) -> Tuple[np.ndarray, Dict[str, Any]]:
        """对因子做行业/市值/Beta 中性化（截面回归残差法）。"""
        x_cols = []
        n = len(factor_values)
        info = {
            'enabled': bool(enable_neutralization),
            'industry': False,
            'market_cap': False,
            'beta': False,
            'styles_used': [],
            'sample_size': int(n),
        }

        if not enable_neutralization or n < 3:
            info['reason'] = 'neutralization_disabled_or_small_sample'
            return factor_values, info

        # 行业哑变量（drop_first 避免完全共线）
        if industry is not None and len(industry) == n:
            industry_s = pd.Series(industry).astype('category')
            dummies = pd.get_dummies(industry_s, drop_first=True)
            if not dummies.empty:
                x_cols.append(dummies.values.astype(float))
                info['industry'] = True
                info['styles_used'].append('industry')

        # 市值（取log）
        if market_cap is not None and len(market_cap) == n:
            mcap = np.asarray(market_cap, dtype=float)
            if np.isfinite(mcap).sum() >= 3:
                mcap = np.where(mcap > 0, np.log(mcap), np.nan)
                x_cols.append(mcap.reshape(-1, 1))
                info['market_cap'] = True
                info['styles_used'].append('market_cap')

        # Beta
        if beta is not None and len(beta) == n:
            b = np.asarray(beta, dtype=float)
            if np.isfinite(b).sum() >= 3:
                x_cols.append(b.reshape(-1, 1))
                info['beta'] = True
                info['styles_used'].append('beta')

        if not x_cols:
            info['reason'] = 'no_style_data'
            return factor_values, info

        X = np.column_stack(x_cols)
        y = np.asarray(factor_values, dtype=float)

        valid_mask = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if valid_mask.sum() < 3:
            info['reason'] = 'insufficient_valid_samples_after_style_mask'
            return factor_values, info

        y_valid = y[valid_mask]
        X_valid = X[valid_mask]

        # 添加截距项
        X_design = np.column_stack([np.ones(len(X_valid)), X_valid])

        # 自由度守卫（灰度测试发现）：当 (有效样本数 - 设计矩阵列数) 过小，
        # 截面回归会过拟合行业均值，残差化反而扭曲因子排序（小样本下 rank IC 可能翻号）。
        # 此时跳过中性化、返回原始值，避免污染 IC。生产大截面（n>>列数）不触发。
        dof = int(X_design.shape[0]) - int(X_design.shape[1])
        if dof < _NEUTRALIZE_MIN_DOF:
            info['reason'] = 'insufficient_degrees_of_freedom'
            info['dof'] = dof
            info['design_cols'] = int(X_design.shape[1])
            return factor_values, info

        coef, _, _, _ = np.linalg.lstsq(X_design, y_valid, rcond=None)
        y_hat = X_design @ coef
        resid = y_valid - y_hat

        neutralized = y.copy()
        neutralized[valid_mask] = resid

        info['residual_mean'] = float(np.nanmean(resid))
        info['residual_std'] = float(np.nanstd(resid))
        info['valid_style_samples'] = int(valid_mask.sum())
        return neutralized, info
    
    @staticmethod
    def calculate_ic(
        factor_values: List[float],
        forward_returns: List[float],
        method: str = 'pearson'
    ) -> Dict[str, float]:
        """
        计算因子IC (Information Coefficient)
        
        Args:
            factor_values: 因子值列表
            forward_returns: 未来收益率列表
            method: 相关系数方法 ('pearson' 或 'spearman')
        
        Returns:
            IC统计信息
        """
        if len(factor_values) != len(forward_returns):
            raise ValueError("Factor values and returns must have same length")
        
        factor_values = np.array(factor_values)
        forward_returns = np.array(forward_returns)
        
        # 移除NaN值
        mask = ~(np.isnan(factor_values) | np.isnan(forward_returns))
        factor_values = factor_values[mask]
        forward_returns = forward_returns[mask]
        
        if len(factor_values) < 2:
            return {'ic': 0.0, 'ic_ir': 0.0, 'p_value': 1.0}
        
        # 计算IC
        if method == 'pearson':
            ic, p_value = stats.pearsonr(factor_values, forward_returns)
        elif method == 'spearman':
            ic, p_value = stats.spearmanr(factor_values, forward_returns)
        else:
            raise ValueError(f"Unknown method: {method}")
        
        return {
            'ic': float(ic),
            'p_value': float(p_value),
            'sample_size': len(factor_values),
        }

    @staticmethod
    def calculate_ic_dual(
        factor_values: List[float],
        forward_returns: List[float],
        industry: Optional[List[Any]] = None,
        market_cap: Optional[List[float]] = None,
        beta: Optional[List[float]] = None,
        enable_neutralization: bool = True,
    ) -> Dict[str, Any]:
        """计算双口径IC（Normal IC + Rank IC），默认启用中性化。"""
        if len(factor_values) != len(forward_returns):
            raise ValueError("Factor values and returns must have same length")

        factor_values = np.array(factor_values, dtype=float)
        forward_returns = np.array(forward_returns, dtype=float)

        # 先按收益/因子有效值筛掉 NaN
        mask = ~(np.isnan(factor_values) | np.isnan(forward_returns))
        factor_values = factor_values[mask]
        forward_returns = forward_returns[mask]

        ind = None
        mcap = None
        b = None
        if industry is not None and len(industry) == len(mask):
            ind = list(np.array(industry, dtype=object)[mask])
        if market_cap is not None and len(market_cap) == len(mask):
            mcap = list(np.array(market_cap, dtype=float)[mask])
        if beta is not None and len(beta) == len(mask):
            b = list(np.array(beta, dtype=float)[mask])

        if len(factor_values) < 3:
            return {
                'normal_ic': 0.0,
                'rank_ic': 0.0,
                'normal_p_value': 1.0,
                'rank_p_value': 1.0,
                'sample_size': int(len(factor_values)),
                'neutralization': {
                    'enabled': bool(enable_neutralization),
                    'reason': 'insufficient_samples',
                },
            }

        neutralized, neutral_meta = FactorAnalyzer._neutralize_factor_values(
            factor_values=factor_values,
            industry=ind,
            market_cap=mcap,
            beta=b,
            enable_neutralization=enable_neutralization,
        )

        n_ic, n_p = stats.pearsonr(neutralized, forward_returns)
        r_ic, r_p = stats.spearmanr(neutralized, forward_returns)

        if np.isnan(n_ic):
            n_ic, n_p = 0.0, 1.0
        if np.isnan(r_ic):
            r_ic, r_p = 0.0, 1.0

        return {
            'normal_ic': float(n_ic),
            'rank_ic': float(r_ic),
            'normal_p_value': float(n_p),
            'rank_p_value': float(r_p),
            'sample_size': int(len(neutralized)),
            'neutralization': neutral_meta,
        }
    
    @staticmethod
    def calculate_ic_series(
        factor_df: pd.DataFrame,
        return_df: pd.DataFrame,
        periods: List[int] = [1, 5, 10, 20]
    ) -> Dict[str, Any]:
        """
        计算IC时间序列
        
        Args:
            factor_df: 因子DataFrame (index=date, columns=codes)
            return_df: 收益率DataFrame (index=date, columns=codes)
            periods: 预测周期列表
        
        Returns:
            IC统计信息
        """
        ic_series = {}
        
        for period in periods:
            ics = []
            dates = []
            
            for date in factor_df.index[:-period]:
                factor_values = factor_df.loc[date].values
                forward_returns = return_df.loc[date:].iloc[period].values
                
                # 计算当期IC
                mask = ~(np.isnan(factor_values) | np.isnan(forward_returns))
                if np.sum(mask) >= 10:  # 至少10个样本
                    ic, _ = stats.spearmanr(
                        factor_values[mask],
                        forward_returns[mask]
                    )
                    ics.append(ic)
                    dates.append(date)
            
            if ics:
                ic_series[f'period_{period}'] = {
                    'mean_ic': float(np.mean(ics)),
                    'std_ic': float(np.std(ics)),
                    'ic_ir': float(np.mean(ics) / np.std(ics)) if np.std(ics) > 0 else 0.0,
                    'positive_ratio': float(np.sum(np.array(ics) > 0) / len(ics)),
                    'ic_series': ics,
                    'dates': dates,
                }
        
        return ic_series
    
    @staticmethod
    def factor_group_backtest(
        factor_values: np.ndarray,
        returns: np.ndarray,
        n_groups: int = 5
    ) -> Dict[str, Any]:
        """
        因子分组回测
        
        Args:
            factor_values: 因子值数组 (n_stocks,)
            returns: 收益率数组 (n_stocks,)
            n_groups: 分组数量
        
        Returns:
            分组回测结果
        """
        # 移除NaN
        mask = ~(np.isnan(factor_values) | np.isnan(returns))
        factor_values = factor_values[mask]
        returns = returns[mask]
        
        if len(factor_values) < n_groups:
            return {'error': 'Insufficient data for grouping'}
        
        # 按因子值分组
        sorted_indices = np.argsort(factor_values)
        group_size = len(sorted_indices) // n_groups
        
        group_returns = []
        for i in range(n_groups):
            start_idx = i * group_size
            end_idx = (i + 1) * group_size if i < n_groups - 1 else len(sorted_indices)
            group_indices = sorted_indices[start_idx:end_idx]
            group_return = np.mean(returns[group_indices])
            group_returns.append(float(group_return))
        
        # 多空组合收益（最高组 - 最低组）
        long_short_return = group_returns[-1] - group_returns[0]
        
        return {
            'group_returns': group_returns,
            'long_short_return': float(long_short_return),
            'monotonicity': FactorAnalyzer._calculate_monotonicity(group_returns),
        }
    
    @staticmethod
    def _calculate_monotonicity(group_returns: List[float]) -> float:
        """计算单调性（组间收益是否单调递增）"""
        if len(group_returns) < 2:
            return 0.0
        
        increasing = sum(1 for i in range(len(group_returns) - 1) if group_returns[i+1] > group_returns[i])
        return increasing / (len(group_returns) - 1)
    
    @staticmethod
    def factor_turnover(
        factor_df: pd.DataFrame,
        top_n: int = 50
    ) -> Dict[str, float]:
        """
        计算因子换手率
        
        Args:
            factor_df: 因子DataFrame (index=date, columns=codes)
            top_n: 选取前N只股票
        
        Returns:
            换手率统计
        """
        turnovers = []
        
        for i in range(1, len(factor_df)):
            prev_top = set(factor_df.iloc[i-1].nlargest(top_n).index)
            curr_top = set(factor_df.iloc[i].nlargest(top_n).index)
            
            # 换手率 = 变化的股票数 / 总数
            turnover = len(prev_top.symmetric_difference(curr_top)) / top_n
            turnovers.append(turnover)
        
        return {
            'mean_turnover': float(np.mean(turnovers)),
            'std_turnover': float(np.std(turnovers)),
            'max_turnover': float(np.max(turnovers)),
        }
    
    @staticmethod
    def factor_decay(
        factor_values: np.ndarray,
        returns_matrix: np.ndarray,
        max_period: int = 20
    ) -> Dict[str, List[float]]:
        """
        因子衰减分析
        
        Args:
            factor_values: 因子值 (n_stocks,)
            returns_matrix: 收益率矩阵 (n_periods, n_stocks)
            max_period: 最大预测周期
        
        Returns:
            各周期的IC值
        """
        ics = []
        
        for period in range(1, min(max_period + 1, len(returns_matrix))):
            forward_returns = returns_matrix[period]
            
            mask = ~(np.isnan(factor_values) | np.isnan(forward_returns))
            if np.sum(mask) >= 10:
                ic, _ = stats.spearmanr(
                    factor_values[mask],
                    forward_returns[mask]
                )
                ics.append(float(ic))
            else:
                ics.append(0.0)
        
        return {
            'periods': list(range(1, len(ics) + 1)),
            'ics': ics,
            'half_life': FactorAnalyzer._calculate_half_life(ics),
        }
    
    @staticmethod
    def _calculate_half_life(ics: List[float]) -> int:
        """计算因子半衰期（IC降至初始值一半的周期）"""
        if not ics or ics[0] == 0:
            return 0
        
        initial_ic = abs(ics[0])
        half_ic = initial_ic / 2
        
        for i, ic in enumerate(ics):
            if abs(ic) <= half_ic:
                return i + 1
        
        return len(ics)
    
    @staticmethod
    def factor_correlation_matrix(
        factors_dict: Dict[str, np.ndarray]
    ) -> pd.DataFrame:
        """
        计算因子相关系数矩阵
        
        Args:
            factors_dict: 因子字典 {factor_name: factor_values}
        
        Returns:
            相关系数矩阵
        """
        factor_names = list(factors_dict.keys())
        n_factors = len(factor_names)
        
        corr_matrix = np.zeros((n_factors, n_factors))
        
        for i, name1 in enumerate(factor_names):
            for j, name2 in enumerate(factor_names):
                if i == j:
                    corr_matrix[i, j] = 1.0
                elif i < j:
                    values1 = factors_dict[name1]
                    values2 = factors_dict[name2]
                    
                    mask = ~(np.isnan(values1) | np.isnan(values2))
                    if np.sum(mask) >= 10:
                        corr, _ = stats.pearsonr(values1[mask], values2[mask])
                        corr_matrix[i, j] = corr
                        corr_matrix[j, i] = corr
        
        return pd.DataFrame(
            corr_matrix,
            index=factor_names,
            columns=factor_names
        )
    
    @staticmethod
    def factor_importance(
        factors_dict: Dict[str, np.ndarray],
        returns: np.ndarray,
        method: str = 'ic'
    ) -> Dict[str, float]:
        """
        计算因子重要性
        
        Args:
            factors_dict: 因子字典
            returns: 收益率数组
            method: 评估方法 ('ic' 或 'regression')
        
        Returns:
            因子重要性得分
        """
        importance = {}
        
        if method == 'ic':
            for name, values in factors_dict.items():
                mask = ~(np.isnan(values) | np.isnan(returns))
                if np.sum(mask) >= 10:
                    ic, _ = stats.spearmanr(values[mask], returns[mask])
                    importance[name] = abs(float(ic))
                else:
                    importance[name] = 0.0
        
        elif method == 'regression':
            # 简化的回归重要性（实际应使用多元回归）
            for name, values in factors_dict.items():
                mask = ~(np.isnan(values) | np.isnan(returns))
                if np.sum(mask) >= 10:
                    slope, _, r_value, _, _ = stats.linregress(values[mask], returns[mask])
                    importance[name] = abs(float(r_value))
                else:
                    importance[name] = 0.0
        
        # 归一化
        total = sum(importance.values())
        if total > 0:
            importance = {k: v / total for k, v in importance.items()}
        
        return importance
    
    @staticmethod
    def factor_performance_summary(
        factor_name: str,
        ic_stats: Dict[str, float],
        group_backtest: Dict[str, Any],
        turnover: Dict[str, float]
    ) -> Dict[str, Any]:
        """
        因子表现综合评估
        
        Returns:
            综合评估结果
        """
        # 计算综合得分
        ic_score = abs(ic_stats.get('ic', 0)) * 100
        ir_score = abs(ic_stats.get('ic_ir', 0)) * 50
        ls_score = abs(group_backtest.get('long_short_return', 0)) * 1000
        mono_score = group_backtest.get('monotonicity', 0) * 50
        turnover_penalty = turnover.get('mean_turnover', 0) * 20
        
        total_score = ic_score + ir_score + ls_score + mono_score - turnover_penalty
        
        # 评级
        if total_score >= 80:
            rating = 'A'
        elif total_score >= 60:
            rating = 'B'
        elif total_score >= 40:
            rating = 'C'
        else:
            rating = 'D'
        
        return {
            'factor_name': factor_name,
            'rating': rating,
            'total_score': float(total_score),
            'ic_stats': ic_stats,
            'group_backtest': group_backtest,
            'turnover': turnover,
            'recommendation': 'Strong' if rating in ['A', 'B'] else 'Weak',
        }


# 全局实例
factor_analyzer = FactorAnalyzer()
