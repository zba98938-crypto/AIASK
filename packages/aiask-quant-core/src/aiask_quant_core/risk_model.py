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


# Below this many observations a 95% historical percentile is the second-worst
# day of a handful, not a risk estimate. The guard exists because the previous
# code happily returned a number for two data points.
MIN_VAR_SAMPLES = 30


class RiskModel:
    """风险模型 - 完整实现"""
    
    @staticmethod
    def calculate_var(
        returns: List[float],
        confidence: float = 0.95,
        portfolio_value: Optional[float] = None
    ) -> Dict[str, Any]:
        """计算VaR（历史模拟法）。

        Three contracts this function now keeps, each of which it previously broke:

        * **Unmeasurable is ``None`` + a stable ``reason``, never ``0.0``.** It used
          to return ``{'var': 0, 'cvar': 0}`` for an empty series — a dict with no
          ``var_percent`` key at all, which callers read as
          ``var_report.get("var_percent", 0.0)`` and thereby turned into a measured
          0% risk. Eight stored quality reports carry ``var_percent = 0.0`` for
          exactly that reason, and 0.0 passes
          ``RISK_REPORT_THRESHOLDS["var_percent_max"]`` without anything being
          computed.
        * **``var_percent`` is a non-negative loss magnitude, so it is ``0.0`` when
          even the confidence tail is a gain.** The previous ``abs()`` reported such
          a tail as a positive loss percentage — a number that looks like measured
          downside risk and is its opposite.
        * **No invented position size.** ``portfolio_value`` defaults to ``None``,
          and the absolute ``var``/``cvar`` amounts are then ``None`` too, with
          ``amounts_measurable`` False. The percentage does not depend on position
          size, so it is still reported. Three call sites used to silently inherit a
          fabricated 1,000,000 portfolio.
        """
        returns_arr = np.asarray(returns, dtype=float).ravel()
        returns_arr = returns_arr[~np.isnan(returns_arr)]
        sample_size = int(returns_arr.size)
        
        def _unmeasurable(reason: str) -> Dict[str, Any]:
            return {
                'var': None,
                'cvar': None,
                'var_percent': None,
                'cvar_percent': None,
                'confidence': confidence,
                'sample_size': sample_size,
                'measurable': False,
                'reason': reason,
                'amounts_measurable': False,
            }

        if sample_size == 0:
            return _unmeasurable('no_returns')
        if sample_size < MIN_VAR_SAMPLES:
            return _unmeasurable('insufficient_samples')
        
        sorted_returns = np.sort(returns_arr)
        index = int((1 - confidence) * len(sorted_returns))
        var_return = sorted_returns[index] if index < len(sorted_returns) else sorted_returns[0]
        
        # CVaR (尾部平均)
        tail_returns = sorted_returns[:index+1]
        cvar_return = np.mean(tail_returns) if len(tail_returns) > 0 else var_return
        
        # Non-negative loss magnitude; the max() also stops an exactly-zero
        # quantile from surfacing as -0.0.
        var_loss = max(0.0, -float(var_return))
        cvar_loss = max(0.0, -float(cvar_return))
        
        return {
            'var': float(var_loss * portfolio_value) if portfolio_value is not None else None,
            'cvar': float(cvar_loss * portfolio_value) if portfolio_value is not None else None,
            'var_percent': float(var_loss * 100),
            'cvar_percent': float(cvar_loss * 100),
            'confidence': confidence,
            'sample_size': sample_size,
            'measurable': True,
            'reason': None,
            'amounts_measurable': portfolio_value is not None,
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
        custom_shocks: Optional[Dict[str, float]] = None,
        *,
        portfolio_value: Optional[float] = None,
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

        ``portfolio_value`` includes cash when supplied. The uninvested balance
        remains unchanged by the equity shock; loss percentages use the actual
        portfolio net value. Coverage describes the measured invested exposure,
        so cash cannot make unknown stock sensitivities appear measured.
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
        invested_value = float(sum(float(h.get('value', 0) or 0) for h in rows))
        portfolio_value = invested_value if portfolio_value is None else float(portfolio_value)
        cash_value = portfolio_value - invested_value

        base = {
            'scenario': scenario_key,
            'description': RiskModel._SCENARIO_DESCRIPTIONS.get(
                scenario_key, f'自定义压力测试：{scenario_key}'
            ),
            'original_value': portfolio_value,
            'invested_value': invested_value,
            'cash_value': cash_value,
            'coverage_basis': 'invested_value',
            'market_shock': float(market_shock) if market_shock is not None else None,
        }
        if custom_shocks:
            base['custom_shocks'] = dict(custom_shocks)

        if not rows or invested_value <= 0 or portfolio_value <= 0:
            return {
                **base,
                'shocked_value': None,
                'loss': None,
                'loss_percent': None,
                'measurable': False,
                'reason': 'no_positions',
            }

        shocked_value = cash_value
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

        coverage = covered_value / invested_value if invested_value > 0 else 0.0

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
    ) -> Dict[str, Any]:
        """计算Beta系数。

        A beta that could not be estimated is ``None`` with a stable ``reason``,
        never ``1.0``. Three failure paths used to return a confident
        market-neutral beta — a length mismatch, a one-point sample, and a flat
        market series. ``stress_test`` consumes ``beta`` from each holding, so a
        fabricated 1.0 here becomes a fabricated stress loss there; that is the
        exact coupling that made every stored quality report read the same
        ``stress_loss_percent``.

        This method currently has no callers, which is why the defaults were
        cheap to overlook and would have been expensive to inherit.
        """
        stock_arr = np.asarray(stock_returns, dtype=float).ravel()
        market_arr = np.asarray(market_returns, dtype=float).ravel()

        def _unmeasurable(reason: str) -> Dict[str, Any]:
            return {
                'beta': None,
                'alpha': None,
                'r_squared': None,
                'sample_size': int(min(stock_arr.size, market_arr.size)),
                'measurable': False,
                'reason': reason,
            }

        if stock_arr.size != market_arr.size:
            return _unmeasurable('length_mismatch')

        # 移除NaN
        mask = ~(np.isnan(stock_arr) | np.isnan(market_arr))
        stock_arr = stock_arr[mask]
        market_arr = market_arr[mask]

        if stock_arr.size < 2:
            return _unmeasurable('insufficient_samples')

        market_variance = float(np.var(market_arr))
        if market_variance <= 0:
            return _unmeasurable('zero_market_variance')

        beta = float(np.cov(stock_arr, market_arr)[0, 1] / market_variance)
        alpha = float(np.mean(stock_arr) - beta * np.mean(market_arr))

        # corrcoef is nan when either series is constant, which the variance
        # guard above does not exclude for the stock side.
        with np.errstate(invalid="ignore", divide="ignore"):
            correlation = float(np.corrcoef(stock_arr, market_arr)[0, 1])
        r_squared = correlation ** 2 if np.isfinite(correlation) else None

        return {
            'beta': beta,
            'alpha': alpha,
            'r_squared': r_squared,
            'sample_size': int(stock_arr.size),
            'measurable': True,
            'reason': None,
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
