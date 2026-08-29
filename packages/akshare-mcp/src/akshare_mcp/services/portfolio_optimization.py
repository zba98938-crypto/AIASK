"""
组合优化服务 - Black-Litterman模型、有效前沿、风险平价
"""

import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from scipy.optimize import minimize
from scipy.linalg import inv

try:
    from sklearn.covariance import LedoitWolf
except Exception:
    LedoitWolf = None


class PortfolioOptimizer:
    """组合优化器"""

    @staticmethod
    def _prepare_cov_matrix(
        cov_matrix: np.ndarray,
        returns_matrix: Optional[np.ndarray] = None,
        use_ledoit_wolf: bool = False,
    ) -> np.ndarray:
        """协方差预处理：可选 Ledoit-Wolf 收缩 + 数值稳定化。"""
        cov = np.asarray(cov_matrix, dtype=float)

        if use_ledoit_wolf and returns_matrix is not None and LedoitWolf is not None:
            try:
                rm = np.asarray(returns_matrix, dtype=float)
                if rm.ndim == 2 and rm.shape[1] == cov.shape[0] and rm.shape[0] >= max(3, cov.shape[0]):
                    cov = LedoitWolf().fit(rm).covariance_
            except Exception:
                pass

        cov = (cov + cov.T) / 2.0
        cov = cov + np.eye(cov.shape[0]) * 1e-10
        return cov

    # ========== 均值-方差优化 ==========

    @staticmethod
    def mean_variance_optimization(
        expected_returns: np.ndarray,
        cov_matrix: np.ndarray,
        risk_aversion: float = 1.0,
        constraints: Optional[Dict[str, Any]] = None,
        returns_matrix: Optional[np.ndarray] = None,
        use_ledoit_wolf: bool = False,
    ) -> Dict[str, Any]:
        """
        均值-方差优化（马科维茨模型）
        
        Args:
            expected_returns: 预期收益率向量 (n_assets,)
            cov_matrix: 协方差矩阵 (n_assets, n_assets)
            risk_aversion: 风险厌恶系数
            constraints: 约束条件
        
        Returns:
            最优权重和组合指标
        """
        n_assets = len(expected_returns)
        cov_matrix = PortfolioOptimizer._prepare_cov_matrix(
            cov_matrix,
            returns_matrix=returns_matrix,
            use_ledoit_wolf=use_ledoit_wolf,
        )

        # 目标函数：最大化 收益 - 风险厌恶系数 * 风险
        def objective(weights):
            portfolio_return = np.dot(weights, expected_returns)
            portfolio_variance = np.dot(weights, np.dot(cov_matrix, weights))
            return -(portfolio_return - risk_aversion * portfolio_variance)
        
        # 约束条件
        constraints_list = [
            {'type': 'eq', 'fun': lambda w: np.sum(w) - 1}  # 权重和为1
        ]
        
        # 自定义约束
        if constraints:
            if 'max_weight' in constraints:
                max_w = constraints['max_weight']
                constraints_list.append({
                    'type': 'ineq',
                    'fun': lambda w: max_w - w
                })
            
            if 'min_weight' in constraints:
                min_w = constraints['min_weight']
                constraints_list.append({
                    'type': 'ineq',
                    'fun': lambda w: w - min_w
                })
        
        # 边界：权重在[0, 1]之间
        bounds = tuple((0, 1) for _ in range(n_assets))
        
        # 初始权重：等权
        initial_weights = np.array([1.0 / n_assets] * n_assets)
        
        # 优化
        result = minimize(
            objective,
            initial_weights,
            method='SLSQP',
            bounds=bounds,
            constraints=constraints_list
        )
        
        optimal_weights = result.x
        
        # 计算组合指标
        portfolio_return = np.dot(optimal_weights, expected_returns)
        portfolio_variance = np.dot(optimal_weights, np.dot(cov_matrix, optimal_weights))
        portfolio_std = np.sqrt(portfolio_variance)
        sharpe_ratio = portfolio_return / portfolio_std if portfolio_std > 0 else 0
        
        return {
            'weights': optimal_weights.tolist(),
            'expected_return': float(portfolio_return),
            'volatility': float(portfolio_std),
            'sharpe_ratio': float(sharpe_ratio),
            'success': result.success,
        }
    
    # ========== Black-Litterman模型 ==========
    
    @staticmethod
    def black_litterman(
        market_weights: np.ndarray,
        cov_matrix: np.ndarray,
        views: List[Dict[str, Any]],
        risk_aversion: float = 2.5,
        tau: float = 0.05,
        returns_matrix: Optional[np.ndarray] = None,
        use_ledoit_wolf: bool = False,
    ) -> Dict[str, Any]:
        """
        Black-Litterman模型 - 融合市场均衡和主观观点
        
        Args:
            market_weights: 市场权重 (n_assets,)
            cov_matrix: 协方差矩阵 (n_assets, n_assets)
            views: 主观观点列表
                [{'type': 'absolute', 'asset': 0, 'return': 0.10},
                 {'type': 'relative', 'assets': [0, 1], 'return': 0.05}]
            risk_aversion: 风险厌恶系数
            tau: 不确定性参数
        
        Returns:
            后验预期收益和最优权重
        """
        n_assets = len(market_weights)
        cov_matrix = PortfolioOptimizer._prepare_cov_matrix(
            cov_matrix,
            returns_matrix=returns_matrix,
            use_ledoit_wolf=use_ledoit_wolf,
        )

        # 1. 计算市场隐含收益（反向优化）
        pi = risk_aversion * np.dot(cov_matrix, market_weights)
        
        # 2. 构建观点矩阵P和观点向量Q
        n_views = len(views)
        P = np.zeros((n_views, n_assets))
        Q = np.zeros(n_views)
        
        for i, view in enumerate(views):
            if view['type'] == 'absolute':
                # 绝对观点：资产i的收益率为r
                asset_idx = view['asset']
                P[i, asset_idx] = 1
                Q[i] = view['return']
            
            elif view['type'] == 'relative':
                # 相对观点：资产i比资产j的收益高r
                assets = view['assets']
                P[i, assets[0]] = 1
                P[i, assets[1]] = -1
                Q[i] = view['return']
        
        # 3. 观点的不确定性矩阵Omega
        # 简化：假设观点之间独立，Omega为对角矩阵
        Omega = np.diag(np.diag(np.dot(P, np.dot(tau * cov_matrix, P.T))))
        
        # 4. 计算后验预期收益
        # E[R] = [(tau*Sigma)^-1 + P'*Omega^-1*P]^-1 * [(tau*Sigma)^-1*pi + P'*Omega^-1*Q]
        tau_sigma_inv = inv(tau * cov_matrix)
        omega_inv = inv(Omega)
        
        posterior_cov = inv(tau_sigma_inv + np.dot(P.T, np.dot(omega_inv, P)))
        posterior_mean = np.dot(
            posterior_cov,
            np.dot(tau_sigma_inv, pi) + np.dot(P.T, np.dot(omega_inv, Q))
        )
        
        # 5. 计算最优权重
        optimal_weights = np.dot(inv(risk_aversion * cov_matrix), posterior_mean)
        
        # 归一化权重
        optimal_weights = optimal_weights / np.sum(optimal_weights)
        
        # 计算组合指标
        portfolio_return = np.dot(optimal_weights, posterior_mean)
        portfolio_variance = np.dot(optimal_weights, np.dot(cov_matrix, optimal_weights))
        portfolio_std = np.sqrt(portfolio_variance)
        
        return {
            'posterior_returns': posterior_mean.tolist(),
            'optimal_weights': optimal_weights.tolist(),
            'expected_return': float(portfolio_return),
            'volatility': float(portfolio_std),
            'sharpe_ratio': float(portfolio_return / portfolio_std) if portfolio_std > 0 else 0,
        }
    
    # ========== 有效前沿 ==========
    
    @staticmethod
    def efficient_frontier(
        expected_returns: np.ndarray,
        cov_matrix: np.ndarray,
        n_points: int = 50,
        returns_matrix: Optional[np.ndarray] = None,
        use_ledoit_wolf: bool = False,
    ) -> Dict[str, Any]:
        """
        计算有效前沿
        
        Args:
            expected_returns: 预期收益率
            cov_matrix: 协方差矩阵
            n_points: 前沿上的点数
        
        Returns:
            有效前沿数据
        """
        n_assets = len(expected_returns)
        cov_matrix = PortfolioOptimizer._prepare_cov_matrix(
            cov_matrix,
            returns_matrix=returns_matrix,
            use_ledoit_wolf=use_ledoit_wolf,
        )

        # 计算最小方差组合
        def min_variance_objective(weights):
            return np.dot(weights, np.dot(cov_matrix, weights))
        
        constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
        bounds = tuple((0, 1) for _ in range(n_assets))
        initial_weights = np.array([1.0 / n_assets] * n_assets)
        
        min_var_result = minimize(
            min_variance_objective,
            initial_weights,
            method='SLSQP',
            bounds=bounds,
            constraints=constraints
        )
        
        min_var_return = np.dot(min_var_result.x, expected_returns)
        max_return = np.max(expected_returns)
        
        # 在最小方差收益和最大收益之间生成目标收益
        target_returns = np.linspace(min_var_return, max_return, n_points)
        
        frontier_points = []
        
        for target_return in target_returns:
            # 目标：最小化方差，约束：收益率=目标收益率
            constraints_with_return = [
                {'type': 'eq', 'fun': lambda w: np.sum(w) - 1},
                {'type': 'eq', 'fun': lambda w: np.dot(w, expected_returns) - target_return}
            ]
            
            result = minimize(
                min_variance_objective,
                initial_weights,
                method='SLSQP',
                bounds=bounds,
                constraints=constraints_with_return
            )
            
            if result.success:
                weights = result.x
                portfolio_return = np.dot(weights, expected_returns)
                portfolio_std = np.sqrt(np.dot(weights, np.dot(cov_matrix, weights)))
                sharpe = portfolio_return / portfolio_std if portfolio_std > 0 else 0
                
                frontier_points.append({
                    'return': float(portfolio_return),
                    'volatility': float(portfolio_std),
                    'sharpe_ratio': float(sharpe),
                    'weights': weights.tolist(),
                })
        
        # 找到最大夏普比率组合
        max_sharpe_point = max(frontier_points, key=lambda x: x['sharpe_ratio'])
        
        return {
            'frontier': frontier_points,
            'max_sharpe_portfolio': max_sharpe_point,
            'n_points': len(frontier_points),
        }
    
    # ========== 风险平价 ==========
    
    @staticmethod
    def risk_parity(
        cov_matrix: np.ndarray,
        target_risk_contributions: Optional[np.ndarray] = None,
        returns_matrix: Optional[np.ndarray] = None,
        use_ledoit_wolf: bool = False,
    ) -> Dict[str, Any]:
        """
        风险平价策略 - 每个资产贡献相同的风险
        
        Args:
            cov_matrix: 协方差矩阵
            target_risk_contributions: 目标风险贡献（默认等权）
        
        Returns:
            风险平价权重
        """
        n_assets = cov_matrix.shape[0]
        cov_matrix = PortfolioOptimizer._prepare_cov_matrix(
            cov_matrix,
            returns_matrix=returns_matrix,
            use_ledoit_wolf=use_ledoit_wolf,
        )

        if target_risk_contributions is None:
            target_risk_contributions = np.array([1.0 / n_assets] * n_assets)
        
        # 目标函数：最小化风险贡献与目标的差异
        def objective(weights):
            portfolio_variance = np.dot(weights, np.dot(cov_matrix, weights))
            portfolio_std = np.sqrt(portfolio_variance)
            
            # 计算边际风险贡献
            marginal_contrib = np.dot(cov_matrix, weights) / portfolio_std
            
            # 风险贡献
            risk_contrib = weights * marginal_contrib
            risk_contrib = risk_contrib / np.sum(risk_contrib)
            
            # 最小化与目标的差异
            return np.sum((risk_contrib - target_risk_contributions) ** 2)
        
        # 约束和边界
        constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
        bounds = tuple((0.001, 1) for _ in range(n_assets))  # 避免零权重
        initial_weights = np.array([1.0 / n_assets] * n_assets)
        
        # 优化
        result = minimize(
            objective,
            initial_weights,
            method='SLSQP',
            bounds=bounds,
            constraints=constraints
        )
        
        optimal_weights = result.x
        
        # 计算实际风险贡献
        portfolio_variance = np.dot(optimal_weights, np.dot(cov_matrix, optimal_weights))
        portfolio_std = np.sqrt(portfolio_variance)
        marginal_contrib = np.dot(cov_matrix, optimal_weights) / portfolio_std
        risk_contrib = optimal_weights * marginal_contrib
        risk_contrib = risk_contrib / np.sum(risk_contrib)
        
        return {
            'weights': optimal_weights.tolist(),
            'risk_contributions': risk_contrib.tolist(),
            'portfolio_volatility': float(portfolio_std),
            'success': result.success,
        }
    
    # ========== 最大夏普比率 ==========
    
    @staticmethod
    def max_sharpe_ratio(
        expected_returns: np.ndarray,
        cov_matrix: np.ndarray,
        risk_free_rate: float = 0.03,
        max_weight: float = 0.35,
        returns_matrix: Optional[np.ndarray] = None,
        use_ledoit_wolf: bool = False,
    ) -> Dict[str, Any]:
        """
        最大化夏普比率

        Args:
            expected_returns: 预期收益率
            cov_matrix: 协方差矩阵
            risk_free_rate: 无风险利率
            max_weight: 单资产最大权重上限（默认 35%）

        Returns:
            最大夏普比率组合
        """
        n_assets = len(expected_returns)
        cov_matrix = PortfolioOptimizer._prepare_cov_matrix(
            cov_matrix,
            returns_matrix=returns_matrix,
            use_ledoit_wolf=use_ledoit_wolf,
        )

        # 约束可行性保护：当资产数较少时，放宽上限到可行最小值 1/n
        safe_cap = float(max(0.0, min(1.0, max_weight)))
        feasible_min_cap = 1.0 / n_assets if n_assets > 0 else 1.0
        upper_bound = max(safe_cap, feasible_min_cap)

        # 目标函数：最大化夏普比率 = 最小化负夏普比率
        def neg_sharpe(weights):
            portfolio_return = np.dot(weights, expected_returns)
            portfolio_std = np.sqrt(np.dot(weights, np.dot(cov_matrix, weights)))
            if portfolio_std <= 0:
                return 1e9
            sharpe = (portfolio_return - risk_free_rate) / portfolio_std
            return -sharpe

        # 约束和边界
        constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
        bounds = tuple((0, upper_bound) for _ in range(n_assets))
        initial_weights = np.array([1.0 / n_assets] * n_assets)

        # 优化
        result = minimize(
            neg_sharpe,
            initial_weights,
            method='SLSQP',
            bounds=bounds,
            constraints=constraints
        )

        optimal_weights = result.x

        # 计算组合指标
        portfolio_return = np.dot(optimal_weights, expected_returns)
        portfolio_std = np.sqrt(np.dot(optimal_weights, np.dot(cov_matrix, optimal_weights)))
        sharpe_ratio = (portfolio_return - risk_free_rate) / portfolio_std if portfolio_std > 0 else 0.0

        return {
            'weights': optimal_weights.tolist(),
            'expected_return': float(portfolio_return),
            'volatility': float(portfolio_std),
            'sharpe_ratio': float(sharpe_ratio),
            'success': result.success,
            'constraints_applied': {
                'max_weight': float(upper_bound),
            },
        }
    
    # ========== 最小方差 ==========
    
    @staticmethod
    def min_variance(
        cov_matrix: np.ndarray,
        constraints: Optional[Dict[str, Any]] = None,
        returns_matrix: Optional[np.ndarray] = None,
        use_ledoit_wolf: bool = False,
    ) -> Dict[str, Any]:
        """
        最小方差组合

        Args:
            cov_matrix: 协方差矩阵
            constraints: 约束条件

        Returns:
            最小方差组合
        """
        n_assets = cov_matrix.shape[0]
        cov_matrix = PortfolioOptimizer._prepare_cov_matrix(
            cov_matrix,
            returns_matrix=returns_matrix,
            use_ledoit_wolf=use_ledoit_wolf,
        )
        
        # 目标函数：最小化方差
        def objective(weights):
            return np.dot(weights, np.dot(cov_matrix, weights))
        
        # 约束
        constraints_list = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
        
        # 边界
        bounds = tuple((0, 1) for _ in range(n_assets))
        initial_weights = np.array([1.0 / n_assets] * n_assets)
        
        # 优化
        result = minimize(
            objective,
            initial_weights,
            method='SLSQP',
            bounds=bounds,
            constraints=constraints_list
        )
        
        optimal_weights = result.x
        portfolio_std = np.sqrt(np.dot(optimal_weights, np.dot(cov_matrix, optimal_weights)))
        
        return {
            'weights': optimal_weights.tolist(),
            'volatility': float(portfolio_std),
            'success': result.success,
        }


# 全局实例（高级优化器）
portfolio_optimizer = PortfolioOptimizer()


# ---------------------------------------------------------------------------
# 简化接口（原 portfolio_optimizer.py）— 接受 stocks + returns_matrix
# ---------------------------------------------------------------------------

def _shrunk_cov(returns_matrix: np.ndarray) -> np.ndarray:
    """Ledoit-Wolf 收缩协方差估计"""
    if LedoitWolf is None:
        return np.cov(returns_matrix)
    try:
        return LedoitWolf().fit(returns_matrix.T).covariance_
    except Exception:
        return np.cov(returns_matrix)


class SimplePortfolioOptimizer:
    """组合优化器（简化接口，封装 stocks 映射）"""

    @staticmethod
    def optimize_equal_weight(stocks: List[str]) -> Dict[str, float]:
        w = 1.0 / len(stocks)
        return {c: w for c in stocks}

    @staticmethod
    def optimize_risk_parity(stocks: List[str], returns_matrix: np.ndarray) -> Dict[str, float]:
        cov = _shrunk_cov(returns_matrix)
        result = portfolio_optimizer.risk_parity(cov)
        if result['success']:
            return {c: float(w) for c, w in zip(stocks, result['weights'])}
        inv_vol = 1.0 / np.sqrt(np.diag(cov))
        weights = inv_vol / np.sum(inv_vol)
        return {c: float(w) for c, w in zip(stocks, weights)}

    @staticmethod
    def optimize_mean_variance(stocks: List[str], returns_matrix: np.ndarray,
                               expected_returns: np.ndarray, risk_aversion: float = 1.0) -> Dict[str, float]:
        cov = _shrunk_cov(returns_matrix)
        result = portfolio_optimizer.mean_variance_optimization(
            expected_returns=expected_returns, cov_matrix=cov, risk_aversion=risk_aversion)
        if result['success']:
            return {c: float(w) for c, w in zip(stocks, result['weights'])}
        inv_cov = np.linalg.inv(cov)
        ones = np.ones(len(stocks))
        weights = np.dot(inv_cov, ones) / np.dot(ones, np.dot(inv_cov, ones))
        return {c: float(w) for c, w in zip(stocks, weights / np.sum(weights))}

    @staticmethod
    def optimize_black_litterman(stocks: List[str], returns_matrix: np.ndarray,
                                 market_weights: np.ndarray, views: List[Dict[str, Any]],
                                 risk_aversion: float = 2.5, tau: float = 0.05) -> Dict[str, Any]:
        cov = _shrunk_cov(returns_matrix)
        result = portfolio_optimizer.black_litterman(
            market_weights=market_weights, cov_matrix=cov, views=views,
            risk_aversion=risk_aversion, tau=tau)
        return {
            'weights': {c: float(w) for c, w in zip(stocks, result['optimal_weights'])},
            'posterior_returns': result['posterior_returns'],
            'expected_return': result['expected_return'],
            'volatility': result['volatility'],
            'sharpe_ratio': result['sharpe_ratio'],
        }

    @staticmethod
    def optimize_risk_budget(stocks: List[str], returns_matrix: np.ndarray,
                             risk_budgets: List[float] = None) -> Dict[str, Any]:
        cov = _shrunk_cov(returns_matrix)
        if risk_budgets is None:
            risk_budgets = [1.0 / len(stocks)] * len(stocks)
        result = portfolio_optimizer.risk_parity(
            cov_matrix=cov, target_risk_contributions=np.array(risk_budgets))
        if result['success']:
            return {'weights': {c: float(w) for c, w in zip(stocks, result['weights'])},
                    'risk_contributions': result['risk_contributions'],
                    'portfolio_volatility': result['portfolio_volatility']}
        return {'weights': {c: 1.0 / len(stocks) for c in stocks},
                'risk_contributions': risk_budgets, 'portfolio_volatility': 0.0}

    @staticmethod
    def optimize_max_sharpe(stocks: List[str], returns_matrix: np.ndarray,
                            expected_returns: np.ndarray, risk_free_rate: float = 0.03,
                            max_weight: float = 0.35) -> Dict[str, Any]:
        cov = _shrunk_cov(returns_matrix)
        result = portfolio_optimizer.max_sharpe_ratio(
            expected_returns=expected_returns, cov_matrix=cov,
            risk_free_rate=risk_free_rate, max_weight=max_weight)
        if result['success']:
            return {'weights': {c: float(w) for c, w in zip(stocks, result['weights'])},
                    'expected_return': result['expected_return'],
                    'volatility': result['volatility'], 'sharpe_ratio': result['sharpe_ratio']}
        return {'weights': {c: 1.0 / len(stocks) for c in stocks},
                'expected_return': 0.0, 'volatility': 0.0, 'sharpe_ratio': 0.0}


# 简化接口单例（向后兼容 portfolio_optimizer 名称）
simple_portfolio_optimizer = SimplePortfolioOptimizer()
