"""风险模型 - VaR/CVaR/Barra/压力测试"""

from typing import List, Dict, Any, Optional
import numpy as np
from sklearn.covariance import LedoitWolf


def _shrunk_cov(returns_matrix: np.ndarray) -> np.ndarray:
    """Ledoit-Wolf 收缩协方差估计，降低小样本噪声"""
    try:
        lw = LedoitWolf().fit(returns_matrix.T)
        return lw.covariance_
    except Exception:
        return np.cov(returns_matrix)


class RiskModel:
    """风险模型 - 完整实现"""
    
    @staticmethod
    def calculate_var(
        returns: List[float],
        confidence: float = 0.95,
        portfolio_value: float = 1000000
    ) -> Dict[str, Any]:
        """计算VaR（历史模拟法）"""
        returns_arr = np.array(returns)
        returns_arr = returns_arr[~np.isnan(returns_arr)]
        
        if len(returns_arr) == 0:
            return {'var': 0, 'cvar': 0}
        
        sorted_returns = np.sort(returns_arr)
        index = int((1 - confidence) * len(sorted_returns))
        var_return = sorted_returns[index] if index < len(sorted_returns) else sorted_returns[0]
        
        # CVaR (尾部平均)
        tail_returns = sorted_returns[:index+1]
        cvar_return = np.mean(tail_returns) if len(tail_returns) > 0 else var_return
        
        var_value = abs(var_return * portfolio_value)
        cvar_value = abs(cvar_return * portfolio_value)
        
        return {
            'var': float(var_value),
            'cvar': float(cvar_value),
            'var_percent': float(abs(var_return) * 100),
            'cvar_percent': float(abs(cvar_return) * 100),
            'confidence': confidence,
        }
    
    @staticmethod
    def calculate_portfolio_risk(
        holdings: List[Dict[str, Any]],
        returns_matrix: np.ndarray
    ) -> Dict[str, Any]:
        """计算组合风险"""
        if returns_matrix is None or getattr(returns_matrix, "ndim", 0) != 2 or returns_matrix.shape[0] == 0:
            return {'volatility': 0.0, 'annual_volatility': 0.0, 'variance': 0.0}

        weights = np.array([float(h.get('weight', 0) or 0) for h in holdings], dtype=float)
        asset_count = int(returns_matrix.shape[0])
        if weights.shape[0] != asset_count:
            weights = weights[:asset_count]
        if weights.shape[0] != asset_count or asset_count == 0:
            weights = np.array([1.0 / asset_count] * asset_count, dtype=float) if asset_count > 0 else np.array([])
        weight_sum = float(np.sum(weights)) if weights.size > 0 else 0.0
        if weight_sum > 0:
            weights = weights / weight_sum
        elif asset_count > 0:
            weights = np.array([1.0 / asset_count] * asset_count, dtype=float)
        
        # 计算协方差矩阵
        cov_matrix = _shrunk_cov(returns_matrix)

        # 组合方差
        portfolio_var = np.dot(weights, np.dot(cov_matrix, weights))
        portfolio_vol = np.sqrt(portfolio_var)
        
        # 年化波动率
        annual_vol = portfolio_vol * np.sqrt(252)
        
        return {
            'volatility': float(portfolio_vol),
            'annual_volatility': float(annual_vol),
            'variance': float(portfolio_var),
        }
    
    @staticmethod
    def calculate_barra_risk(
        holdings: List[Dict[str, Any]],
        factor_exposures: Dict[str, Dict[str, float]],
        factor_covariance: np.ndarray,
        specific_risks: Dict[str, float]
    ) -> Dict[str, Any]:
        """Barra风险模型分解"""
        codes = [h['code'] for h in holdings]
        weights = np.array([h['weight'] for h in holdings])
        
        # 构建因子暴露矩阵
        factor_names = list(next(iter(factor_exposures.values())).keys())
        n_factors = len(factor_names)
        n_stocks = len(codes)
        
        exposure_matrix = np.zeros((n_stocks, n_factors))
        for i, code in enumerate(codes):
            if code in factor_exposures:
                for j, factor in enumerate(factor_names):
                    exposure_matrix[i, j] = factor_exposures[code].get(factor, 0)
        
        # 组合因子暴露
        portfolio_exposure = np.dot(weights, exposure_matrix)
        
        # 因子风险贡献
        factor_risk = np.dot(portfolio_exposure, np.dot(factor_covariance, portfolio_exposure))
        
        # 特质风险贡献
        specific_var = sum(
            (weights[i] ** 2) * (specific_risks.get(codes[i], 0) ** 2)
            for i in range(n_stocks)
        )
        
        # 总风险
        total_risk = np.sqrt(factor_risk + specific_var)
        
        return {
            'total_risk': float(total_risk),
            'factor_risk': float(np.sqrt(factor_risk)),
            'specific_risk': float(np.sqrt(specific_var)),
            'factor_contribution': float(factor_risk / (factor_risk + specific_var)),
            'specific_contribution': float(specific_var / (factor_risk + specific_var)),
            'portfolio_exposure': portfolio_exposure.tolist(),
            'factor_names': factor_names,
        }
    
    # Market shock applied to each named scenario, as a fraction of position value.
    # These are the scenario *definition* — the shock a caller asks to be applied.
    # They are not a result: what the portfolio actually loses depends on how
    # sensitive each holding is to that shock, which is what stress_test computes.
    _SCENARIO_MARKET_SHOCKS = {
        'market_crash': -0.20,
        'black_swan': -0.30,
        'interest_rate_hike': -0.10,
        'sector_rotation': -0.05,
        'liquidity_crisis': -0.15,
    }

    _SCENARIO_DESCRIPTIONS = {
        'market_crash': '市场崩盘：市场下跌20%',
        'black_swan': '黑天鹅事件：市场下跌30%',
        'interest_rate_hike': '利率上升：市场下跌10%',
        'sector_rotation': '板块轮动：市场下跌5%',
        'liquidity_crisis': '流动性危机：市场下跌15%',
    }

    _SCENARIO_ALIASES = {
        'rate_hike': 'interest_rate_hike',
        'crash': 'market_crash',
        'rate': 'interest_rate_hike',
        'swan': 'black_swan',
        'rotation': 'sector_rotation',
    }

    @staticmethod
    def stress_test(
        holdings: List[Dict[str, Any]],
        scenario: str = 'market_crash',
        custom_shocks: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        """压力测试：按每笔持仓对市场冲击的实际敏感度计算组合损失。

        Each holding is shocked by ``market_shock * beta``, where beta comes from
        the holding itself (``beta``, or ``sensitivity``). A holding without a
        beta is *not* silently assumed to move with the market — it is reported
        as unmeasurable, because assuming beta=1.0 is what made this function
        return a constant regardless of what was held.

        ``loss_percent`` is therefore ``None`` when no holding carries a
        sensitivity, and the reason is named in ``reason``. Callers already
        distinguish a missing metric from a measured one (the submission gate
        hard-fails on ``missing_stress_loss_percent`` at incubation/live level),
        so an honest absence is safe; a fabricated number is not.

        ``custom_shocks`` maps a holding code to its own shock and takes
        precedence over the scenario shock for that holding. A holding covered
        by ``custom_shocks`` needs no beta — the shock is already position-level.
        """
        scenario_key = str(scenario or '').strip().lower()
        scenario_key = RiskModel._SCENARIO_ALIASES.get(scenario_key, scenario_key)

        market_shock = RiskModel._SCENARIO_MARKET_SHOCKS.get(scenario_key)
        if market_shock is None and not custom_shocks:
            return {
                'scenario': scenario_key or None,
                'error': f'Unknown scenario: {scenario}',
                'loss_percent': None,
                'measurable': False,
                'reason': 'unknown_scenario',
            }

        if custom_shocks and market_shock is None:
            scenario_key = 'custom'

        rows = [h for h in (holdings or []) if isinstance(h, dict)]
        portfolio_value = float(sum(float(h.get('value', 0) or 0) for h in rows))

        base = {
            'scenario': scenario_key,
            'description': RiskModel._SCENARIO_DESCRIPTIONS.get(
                scenario_key, f'自定义压力测试：{scenario_key}'
            ),
            'original_value': portfolio_value,
            'market_shock': float(market_shock) if market_shock is not None else None,
        }
        if custom_shocks:
            base['custom_shocks'] = dict(custom_shocks)

        if not rows or portfolio_value <= 0:
            return {
                **base,
                'shocked_value': None,
                'loss': None,
                'loss_percent': None,
                'measurable': False,
                'reason': 'no_positions',
            }

        shocked_value = 0.0
        covered_value = 0.0
        missing_sensitivity: List[str] = []

        for holding in rows:
            value = float(holding.get('value', 0) or 0)
            code = str(holding.get('code') or '').strip()

            own_shock = None
            if custom_shocks and code in custom_shocks:
                own_shock = float(custom_shocks[code])
            else:
                beta = holding.get('beta', holding.get('sensitivity'))
                if beta is not None and market_shock is not None:
                    try:
                        own_shock = float(market_shock) * float(beta)
                    except (TypeError, ValueError):
                        own_shock = None

            if own_shock is None:
                missing_sensitivity.append(code or '<unnamed>')
                # Carry the position through unshocked so shocked_value stays a
                # real sum, and report it as uncovered rather than shocking it
                # by an assumed beta.
                shocked_value += value
                continue

            shocked_value += value * (1.0 + own_shock)
            covered_value += value

        coverage = covered_value / portfolio_value if portfolio_value > 0 else 0.0

        if covered_value <= 0:
            return {
                **base,
                'shocked_value': None,
                'loss': None,
                'loss_percent': None,
                'measurable': False,
                'reason': 'no_position_sensitivity',
                'coverage': 0.0,
                'missing_sensitivity': missing_sensitivity,
            }

        loss = portfolio_value - shocked_value
        result = {
            **base,
            'shocked_value': float(shocked_value),
            'loss': float(loss),
            # Negative means the portfolio lost value, matching the gate
            # comparison `stress_loss_percent <= -25.0`.
            'loss_percent': float(-loss / portfolio_value * 100),
            'measurable': True,
            'coverage': float(coverage),
        }
        if missing_sensitivity:
            result['missing_sensitivity'] = missing_sensitivity
            result['reason'] = 'partial_position_sensitivity'
        return result


    @staticmethod
    def calculate_beta(
        stock_returns: np.ndarray,
        market_returns: np.ndarray
    ) -> Dict[str, float]:
        """计算Beta系数"""
        if len(stock_returns) != len(market_returns):
            return {'beta': 1.0, 'alpha': 0.0, 'r_squared': 0.0}
        
        # 移除NaN
        mask = ~(np.isnan(stock_returns) | np.isnan(market_returns))
        stock_returns = stock_returns[mask]
        market_returns = market_returns[mask]
        
        if len(stock_returns) < 2:
            return {'beta': 1.0, 'alpha': 0.0, 'r_squared': 0.0}
        
        # 计算协方差和方差
        covariance = np.cov(stock_returns, market_returns)[0, 1]
        market_variance = np.var(market_returns)
        
        beta = covariance / market_variance if market_variance > 0 else 1.0
        
        # 计算Alpha
        alpha = np.mean(stock_returns) - beta * np.mean(market_returns)
        
        # R-squared
        correlation = np.corrcoef(stock_returns, market_returns)[0, 1]
        r_squared = correlation ** 2
        
        return {
            'beta': float(beta),
            'alpha': float(alpha),
            'r_squared': float(r_squared),
        }
    
    @staticmethod
    def calculate_tracking_error(
        portfolio_returns: np.ndarray,
        benchmark_returns: np.ndarray
    ) -> Dict[str, float]:
        """计算跟踪误差"""
        if len(portfolio_returns) != len(benchmark_returns):
            return {'tracking_error': 0.0, 'information_ratio': 0.0}
        
        # 超额收益
        excess_returns = portfolio_returns - benchmark_returns
        
        # 跟踪误差（超额收益的标准差）
        tracking_error = np.std(excess_returns) * np.sqrt(252)
        
        # 信息比率
        mean_excess = np.mean(excess_returns)
        information_ratio = (mean_excess * 252) / tracking_error if tracking_error > 0 else 0.0
        
        return {
            'tracking_error': float(tracking_error),
            'information_ratio': float(information_ratio),
            'mean_excess_return': float(mean_excess),
        }


risk_model = RiskModel()
