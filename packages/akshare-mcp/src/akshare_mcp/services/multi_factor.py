"""
多因子框架模块

提供专业级因子投资功能：
- 因子标准化（Z-Score、排名标准化）
- 因子合成（等权、IC加权、优化加权）
- 因子筛选（IC分析、换手率分析、衰减分析）
- 组合构建（分位数组合、优化组合）

Author: AKShare MCP Server
Version: 2.0
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Union
from scipy import stats
from scipy.optimize import minimize

from .factor_analysis import FactorAnalyzer as CoreFactorAnalyzer
from ..utils import safe_stderr_print


class FactorStandardizer:
    """因子标准化器"""
    
    @staticmethod
    def z_score(factor_values: np.ndarray, clip: float = 3.0) -> np.ndarray:
        """
        Z-Score标准化（截面标准化，适用于同一时点的截面数据）
        
        Args:
            factor_values: 因子值数组
            clip: 截断阈值（默认3倍标准差）
            
        Returns:
            标准化后的因子值
        """
        mean = np.nanmean(factor_values)
        std = np.nanstd(factor_values)
        
        if std == 0:
            return np.zeros_like(factor_values)
        
        z_scores = (factor_values - mean) / std
        
        # 截断极端值
        if clip is not None:
            z_scores = np.clip(z_scores, -clip, clip)
        
        return z_scores

    @staticmethod
    def z_score_rolling(
        factor_values: np.ndarray, window: int = 0, clip: float = 3.0
    ) -> np.ndarray:
        """
        Z-Score标准化（时间序列版，无前视偏差）

        使用 expanding 窗口（window=0）或固定 rolling 窗口标准化，
        每个时点只使用当前及之前的数据。

        Args:
            factor_values: 因子值序列（时间顺序）
            window: rolling 窗口大小，0 表示 expanding（使用全部历史）
            clip: 截断阈值

        Returns:
            标准化后的因子值（前 min_periods-1 个值为 NaN）
        """
        s = pd.Series(factor_values, dtype=np.float64)
        if window > 0:
            roll_mean = s.rolling(window, min_periods=2).mean()
            roll_std = s.rolling(window, min_periods=2).std()
        else:
            roll_mean = s.expanding(min_periods=2).mean()
            roll_std = s.expanding(min_periods=2).std()
        z = (s - roll_mean) / roll_std.replace(0, np.nan)
        if clip is not None:
            z = z.clip(-clip, clip)
        return z.to_numpy()
    
    @staticmethod
    def rank_normalize(factor_values: np.ndarray) -> np.ndarray:
        """
        排名标准化（转换为[0, 1]区间）
        
        Args:
            factor_values: 因子值数组
            
        Returns:
            标准化后的因子值
        """
        # 处理NaN值
        valid_mask = ~np.isnan(factor_values)
        if not np.any(valid_mask):
            return np.zeros_like(factor_values)
        
        # 计算排名
        ranks = np.full_like(factor_values, np.nan)
        ranks[valid_mask] = stats.rankdata(factor_values[valid_mask])
        
        # 归一化到[0, 1]
        n_valid = np.sum(valid_mask)
        if n_valid > 1:
            ranks[valid_mask] = (ranks[valid_mask] - 1) / (n_valid - 1)
        
        return ranks
    
    @staticmethod
    def mad_normalize(factor_values: np.ndarray, clip: float = 3.0) -> np.ndarray:
        """
        MAD（中位数绝对偏差）标准化
        
        Args:
            factor_values: 因子值数组
            clip: 截断阈值
            
        Returns:
            标准化后的因子值
        """
        median = np.nanmedian(factor_values)
        mad = np.nanmedian(np.abs(factor_values - median))
        
        if mad == 0:
            return np.zeros_like(factor_values)
        
        normalized = (factor_values - median) / (1.4826 * mad)
        
        # 截断极端值
        if clip is not None:
            normalized = np.clip(normalized, -clip, clip)
        
        return normalized


class FactorCombiner:
    """因子合成器"""
    
    @staticmethod
    def equal_weight(factors: Dict[str, np.ndarray]) -> np.ndarray:
        """
        等权合成
        
        Args:
            factors: 因子字典 {因子名: 因子值数组}
            
        Returns:
            合成后的因子值
        """
        factor_matrix = np.column_stack(list(factors.values()))
        return np.nanmean(factor_matrix, axis=1)
    
    @staticmethod
    def ic_weight(factors: Dict[str, np.ndarray], 
                  returns: np.ndarray,
                  lookback: int = 20) -> np.ndarray:
        """
        IC加权合成
        
        Args:
            factors: 因子字典
            returns: 收益率数组
            lookback: IC计算回溯期
            
        Returns:
            合成后的因子值
        """
        # 计算每个因子的IC
        ics = {}
        for name, values in factors.items():
            ic = np.corrcoef(values, returns)[0, 1]
            ics[name] = ic if not np.isnan(ic) else 0.0
        
        # IC加权
        total_ic = sum(abs(ic) for ic in ics.values())
        if total_ic == 0:
            return FactorCombiner.equal_weight(factors)
        
        weights = {name: abs(ic) / total_ic for name, ic in ics.items()}
        
        # 合成
        combined = np.zeros(len(returns))
        for name, values in factors.items():
            combined += weights[name] * values

        return combined

    @staticmethod
    def optimize_weight(factors: Dict[str, np.ndarray],
                       returns: np.ndarray,
                       method: str = 'max_ic') -> np.ndarray:
        """
        优化加权合成

        Args:
            factors: 因子字典
            returns: 收益率数组
            method: 优化方法 ('max_ic', 'min_variance', 'max_sharpe')

        Returns:
            合成后的因子值
        """
        n_factors = len(factors)
        factor_matrix = np.column_stack(list(factors.values()))

        # 定义优化目标函数
        def objective(weights):
            combined = factor_matrix @ weights
            if method == 'max_ic':
                ic = np.corrcoef(combined, returns)[0, 1]
                return -ic if not np.isnan(ic) else 0
            elif method == 'min_variance':
                return np.var(combined)
            elif method == 'max_sharpe':
                ic = np.corrcoef(combined, returns)[0, 1]
                vol = np.std(combined)
                return -(ic / vol) if vol > 0 else 0
            return 0

        # 约束条件：权重和为1，权重非负
        constraints = {'type': 'eq', 'fun': lambda w: np.sum(w) - 1}
        bounds = [(0, 1) for _ in range(n_factors)]

        # 初始权重
        x0 = np.ones(n_factors) / n_factors

        # 优化
        result = minimize(objective, x0, method='SLSQP',
                         bounds=bounds, constraints=constraints)

        if result.success:
            return factor_matrix @ result.x
        else:
            return FactorCombiner.equal_weight(factors)


class FactorAnalyzer:
    """因子分析器"""

    @staticmethod
    def calculate_ic(factor_values: np.ndarray,
                    returns: np.ndarray,
                    method: str = 'pearson') -> float:
        """
        计算因子IC（信息系数）

        Args:
            factor_values: 因子值数组
            returns: 收益率数组
            method: 相关系数方法 ('pearson', 'spearman')

        Returns:
            IC值
        """
        if method == 'pearson':
            ic = np.corrcoef(factor_values, returns)[0, 1]
        elif method == 'spearman':
            ic, _ = stats.spearmanr(factor_values, returns)
        else:
            raise ValueError(f"Unknown method: {method}")

        return ic if not np.isnan(ic) else 0.0

    @staticmethod
    def calculate_ic_dual(
        factor_values: np.ndarray,
        returns: np.ndarray,
        industry: Optional[List[Union[str, int]]] = None,
        market_cap: Optional[List[float]] = None,
        beta: Optional[List[float]] = None,
        enable_neutralization: bool = True,
    ) -> Dict[str, Union[float, Dict[str, Union[bool, str, int, float, list]]]]:
        """
        计算双口径 IC（Normal IC + Rank IC），默认启用中性化。

        说明：为保持统一口径，复用 services.factor_analysis.FactorAnalyzer 的实现。
        """
        result = CoreFactorAnalyzer.calculate_ic_dual(
            factor_values=np.asarray(factor_values, dtype=float).tolist(),
            forward_returns=np.asarray(returns, dtype=float).tolist(),
            industry=industry,
            market_cap=market_cap,
            beta=beta,
            enable_neutralization=enable_neutralization,
        )
        return result

    @staticmethod
    def calculate_ic_ir(factor_values_series: List[np.ndarray],
                       returns_series: List[np.ndarray]) -> Tuple[float, float]:
        """
        计算IC均值和IR（信息比率）

        Args:
            factor_values_series: 因子值时间序列
            returns_series: 收益率时间序列

        Returns:
            (IC均值, IR)
        """
        ics = []
        for factor_values, returns in zip(factor_values_series, returns_series):
            ic = FactorAnalyzer.calculate_ic(factor_values, returns)
            ics.append(ic)

        ic_mean = np.mean(ics)
        ic_std = np.std(ics)
        ir = ic_mean / ic_std if ic_std > 0 else 0.0

        return ic_mean, ir

    @staticmethod
    def detect_decay(ic_series: List[float],
                    window: int = 20) -> Dict[str, float]:
        """
        检测因子衰减

        Args:
            ic_series: IC时间序列
            window: 滚动窗口大小

        Returns:
            衰减指标字典
        """
        if len(ic_series) < window * 2:
            return {'decay_rate': 0.0, 'half_life': np.inf}

        # 计算滚动IC均值
        rolling_ic = pd.Series(ic_series).rolling(window).mean().dropna()

        # 计算衰减率（线性回归斜率）
        x = np.arange(len(rolling_ic))
        slope, _ = np.polyfit(x, rolling_ic, 1)

        # 计算半衰期
        if slope < 0:
            half_life = -np.log(2) / slope
        else:
            half_life = np.inf

        return {
            'decay_rate': slope,
            'half_life': half_life,
            'current_ic': rolling_ic.iloc[-1],
            'peak_ic': rolling_ic.max()
        }


class PortfolioBuilder:
    """组合构建器"""

    @staticmethod
    def quantile_portfolio(factor_values: np.ndarray,
                          n_quantiles: int = 5,
                          long_short: bool = True) -> Dict[str, np.ndarray]:
        """
        构建分位数组合

        Args:
            factor_values: 因子值数组
            n_quantiles: 分位数数量
            long_short: 是否构建多空组合

        Returns:
            组合权重字典
        """
        # 计算分位数
        quantiles = pd.qcut(factor_values, n_quantiles, labels=False, duplicates='drop')

        portfolios = {}

        if long_short:
            # 多头组合（最高分位）
            long_mask = quantiles == (n_quantiles - 1)
            long_weights = np.zeros_like(factor_values)
            long_weights[long_mask] = 1.0 / np.sum(long_mask)
            portfolios['long'] = long_weights

            # 空头组合（最低分位）
            short_mask = quantiles == 0
            short_weights = np.zeros_like(factor_values)
            short_weights[short_mask] = -1.0 / np.sum(short_mask)
            portfolios['short'] = short_weights

            # 多空组合
            portfolios['long_short'] = long_weights + short_weights
        else:
            # 只做多
            for q in range(n_quantiles):
                mask = quantiles == q
                weights = np.zeros_like(factor_values)
                weights[mask] = 1.0 / np.sum(mask)
                portfolios[f'Q{q+1}'] = weights

        return portfolios

    @staticmethod
    def optimize_portfolio(factor_values: np.ndarray,
                          expected_returns: np.ndarray,
                          cov_matrix: np.ndarray,
                          method: str = 'max_sharpe',
                          constraints: Optional[Dict] = None) -> np.ndarray:
        """
        优化组合权重

        Args:
            factor_values: 因子值数组
            expected_returns: 预期收益率
            cov_matrix: 协方差矩阵
            method: 优化方法 ('max_sharpe', 'min_variance', 'risk_parity')
            constraints: 约束条件

        Returns:
            优化后的权重
        """
        n_assets = len(factor_values)

        # 定义优化目标函数
        def objective(weights):
            portfolio_return = weights @ expected_returns
            portfolio_vol = np.sqrt(weights @ cov_matrix @ weights)

            if method == 'max_sharpe':
                return -portfolio_return / portfolio_vol if portfolio_vol > 0 else 0
            elif method == 'min_variance':
                return portfolio_vol
            elif method == 'risk_parity':
                # 风险平价：每个资产的风险贡献相等
                marginal_risk = cov_matrix @ weights
                risk_contrib = weights * marginal_risk
                target_risk = portfolio_vol / n_assets
                return np.sum((risk_contrib - target_risk) ** 2)
            return 0

        # 默认约束条件
        default_constraints = [
            {'type': 'eq', 'fun': lambda w: np.sum(w) - 1},  # 权重和为1
        ]

        if constraints:
            default_constraints.extend(constraints.get('additional', []))

        # 权重边界
        bounds = constraints.get('bounds', [(0, 1) for _ in range(n_assets)])

        # 初始权重
        x0 = np.ones(n_assets) / n_assets

        # 优化
        result = minimize(objective, x0, method='SLSQP',
                         bounds=bounds, constraints=default_constraints)

        if result.success:
            return result.x
        else:
            return x0


class FactorBacktester:
    """因子回测器"""

    @staticmethod
    def backtest_quantile(factor_values_series: List[np.ndarray],
                         returns_series: List[np.ndarray],
                         n_quantiles: int = 5) -> Dict[str, pd.DataFrame]:
        """
        分位数回测

        Args:
            factor_values_series: 因子值时间序列
            returns_series: 收益率时间序列
            n_quantiles: 分位数数量

        Returns:
            回测结果字典
        """
        results = {f'Q{i+1}': [] for i in range(n_quantiles)}
        results['long_short'] = []

        for factor_values, returns in zip(factor_values_series, returns_series):
            # 构建分位数组合
            portfolios = PortfolioBuilder.quantile_portfolio(
                factor_values, n_quantiles, long_short=False
            )

            # 计算每个分位数的收益
            for q in range(n_quantiles):
                weights = portfolios[f'Q{q+1}']
                portfolio_return = weights @ returns
                results[f'Q{q+1}'].append(portfolio_return)

            # 计算多空收益
            long_return = portfolios[f'Q{n_quantiles}'] @ returns
            short_return = portfolios['Q1'] @ returns
            results['long_short'].append(long_return - short_return)

        # 转换为DataFrame
        results_df = {}
        for key, values in results.items():
            df = pd.DataFrame({
                'returns': values,
                'cumulative_returns': np.cumprod(1 + np.array(values)) - 1
            })
            results_df[key] = df

        return results_df

    @staticmethod
    def calculate_performance_metrics(returns: np.ndarray) -> Dict[str, float]:
        """
        计算绩效指标

        Args:
            returns: 收益率序列

        Returns:
            绩效指标字典
        """
        # 年化收益率
        annual_return = np.mean(returns) * 252

        # 年化波动率
        annual_vol = np.std(returns) * np.sqrt(252)

        # 夏普比率（扣除无风险利率，默认年化 2.0%）
        risk_free_rate = 0.02
        sharpe = (annual_return - risk_free_rate) / annual_vol if annual_vol > 0 else 0

        # 最大回撤
        cumulative = np.cumprod(1 + returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (cumulative - running_max) / running_max
        max_drawdown = np.min(drawdown)

        # 卡玛比率
        calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0

        # 胜率
        win_rate = np.sum(returns > 0) / len(returns)

        return {
            'annual_return': annual_return,
            'annual_volatility': annual_vol,
            'sharpe_ratio': sharpe,
            'max_drawdown': max_drawdown,
            'calmar_ratio': calmar,
            'win_rate': win_rate,
            'total_return': np.prod(1 + returns) - 1
        }


# 使用示例
if __name__ == '__main__':
    # 生成示例数据
    np.random.seed(42)
    n_stocks = 100
    n_periods = 252

    # 模拟因子值
    factor1 = np.random.randn(n_stocks)
    factor2 = np.random.randn(n_stocks)
    factor3 = np.random.randn(n_stocks)

    # 模拟收益率
    returns = np.random.randn(n_stocks) * 0.01

    safe_stderr_print("=" * 60)
    safe_stderr_print("多因子框架使用示例")
    safe_stderr_print("=" * 60)

    # 1. 因子标准化
    safe_stderr_print("\n1. 因子标准化")
    safe_stderr_print("-" * 60)
    factor1_zscore = FactorStandardizer.z_score(factor1)
    factor1_rank = FactorStandardizer.rank_normalize(factor1)
    safe_stderr_print(f"原始因子均值: {np.mean(factor1):.4f}, 标准差: {np.std(factor1):.4f}")
    safe_stderr_print(f"Z-Score标准化后均值: {np.mean(factor1_zscore):.4f}, 标准差: {np.std(factor1_zscore):.4f}")
    safe_stderr_print(f"排名标准化后范围: [{np.min(factor1_rank):.4f}, {np.max(factor1_rank):.4f}]")

    # 2. 因子合成
    safe_stderr_print("\n2. 因子合成")
    safe_stderr_print("-" * 60)
    factors = {
        'factor1': factor1_zscore,
        'factor2': FactorStandardizer.z_score(factor2),
        'factor3': FactorStandardizer.z_score(factor3)
    }

    combined_equal = FactorCombiner.equal_weight(factors)
    combined_ic = FactorCombiner.ic_weight(factors, returns)
    safe_stderr_print(f"等权合成因子均值: {np.mean(combined_equal):.4f}")
    safe_stderr_print(f"IC加权合成因子均值: {np.mean(combined_ic):.4f}")

    # 3. 因子分析
    safe_stderr_print("\n3. 因子分析")
    safe_stderr_print("-" * 60)
    ic = FactorAnalyzer.calculate_ic(combined_ic, returns)
    safe_stderr_print(f"合成因子IC: {ic:.4f}")

    # 4. 组合构建
    safe_stderr_print("\n4. 组合构建")
    safe_stderr_print("-" * 60)
    portfolios = PortfolioBuilder.quantile_portfolio(combined_ic, n_quantiles=5)
    for name, weights in portfolios.items():
        portfolio_return = weights @ returns
        safe_stderr_print(f"{name:12s} 组合收益: {portfolio_return:.4f}")

    safe_stderr_print("\n" + "=" * 60)
    safe_stderr_print("多因子框架模块加载完成！")
    safe_stderr_print("=" * 60)

