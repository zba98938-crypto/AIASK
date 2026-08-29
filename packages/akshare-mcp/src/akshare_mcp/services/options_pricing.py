"""
期权定价服务 - Black-Scholes模型和Greeks计算
"""

import numpy as np
from scipy.stats import norm
from typing import Dict, Optional
from datetime import datetime


class OptionsPricing:
    """期权定价和Greeks计算"""
    
    @staticmethod
    def _finite_float(name: str, value: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        if not np.isfinite(numeric):
            raise ValueError(f"{name} must be a finite number")
        return numeric

    @staticmethod
    def _positive_float(name: str, value: float) -> float:
        numeric = OptionsPricing._finite_float(name, value)
        if numeric <= 0:
            raise ValueError(f"{name} must be greater than 0")
        return numeric

    @staticmethod
    def _normalize_option_type(option_type: str) -> str:
        normalized = (option_type or 'call').strip().lower()
        if normalized not in {'call', 'put'}:
            raise ValueError("option_type must be 'call' or 'put'")
        return normalized

    @staticmethod
    def black_scholes(
        spot: float,
        strike: float,
        time_to_maturity: float,
        risk_free_rate: float,
        volatility: float,
        option_type: str = 'call',
        dividend_yield: float = 0.0
    ) -> float:
        """
        Black-Scholes期权定价公式
        
        Args:
            spot: 标的资产现价
            strike: 行权价
            time_to_maturity: 到期时间（年）
            risk_free_rate: 无风险利率
            volatility: 波动率
            option_type: 期权类型 ('call' 或 'put')
            dividend_yield: 股息率
        
        Returns:
            期权价格
        """
        option_type = OptionsPricing._normalize_option_type(option_type)
        spot = OptionsPricing._positive_float('spot', spot)
        strike = OptionsPricing._positive_float('strike', strike)
        time_to_maturity = OptionsPricing._finite_float('time_to_maturity', time_to_maturity)
        risk_free_rate = OptionsPricing._finite_float('risk_free_rate', risk_free_rate)
        dividend_yield = OptionsPricing._finite_float('dividend_yield', dividend_yield)

        if time_to_maturity <= 0:
            # 到期时的内在价值
            if option_type == 'call':
                return max(spot - strike, 0)
            else:
                return max(strike - spot, 0)
        
        volatility = OptionsPricing._positive_float('volatility', volatility)
        # 计算d1和d2
        d1 = (np.log(spot / strike) + (risk_free_rate - dividend_yield + 0.5 * volatility ** 2) * time_to_maturity) / (volatility * np.sqrt(time_to_maturity))
        d2 = d1 - volatility * np.sqrt(time_to_maturity)
        
        if option_type == 'call':
            # 看涨期权价格
            price = spot * np.exp(-dividend_yield * time_to_maturity) * norm.cdf(d1) - strike * np.exp(-risk_free_rate * time_to_maturity) * norm.cdf(d2)
        else:
            # 看跌期权价格
            price = strike * np.exp(-risk_free_rate * time_to_maturity) * norm.cdf(-d2) - spot * np.exp(-dividend_yield * time_to_maturity) * norm.cdf(-d1)
        
        return float(price)
    
    @staticmethod
    def calculate_greeks(
        spot: float,
        strike: float,
        time_to_maturity: float,
        risk_free_rate: float,
        volatility: float,
        option_type: str = 'call',
        dividend_yield: float = 0.0
    ) -> Dict[str, float]:
        """
        计算期权Greeks
        
        Args:
            spot: 标的资产现价
            strike: 行权价
            time_to_maturity: 到期时间（年）
            risk_free_rate: 无风险利率
            volatility: 波动率
            option_type: 期权类型 ('call' 或 'put')
            dividend_yield: 股息率
        
        Returns:
            Greeks字典 {delta, gamma, theta, vega, rho}
        """
        option_type = OptionsPricing._normalize_option_type(option_type)
        spot = OptionsPricing._positive_float('spot', spot)
        strike = OptionsPricing._positive_float('strike', strike)
        time_to_maturity = OptionsPricing._finite_float('time_to_maturity', time_to_maturity)

        if time_to_maturity <= 0:
            # 到期时Greeks为0（除了delta）
            if option_type == 'call':
                delta = 1.0 if spot > strike else 0.0
            else:
                delta = -1.0 if spot < strike else 0.0
            
            return {
                'delta': delta,
                'gamma': 0.0,
                'theta': 0.0,
                'vega': 0.0,
                'rho': 0.0,
            }
        
        risk_free_rate = OptionsPricing._finite_float('risk_free_rate', risk_free_rate)
        volatility = OptionsPricing._positive_float('volatility', volatility)
        dividend_yield = OptionsPricing._finite_float('dividend_yield', dividend_yield)
        # 计算d1和d2
        d1 = (np.log(spot / strike) + (risk_free_rate - dividend_yield + 0.5 * volatility ** 2) * time_to_maturity) / (volatility * np.sqrt(time_to_maturity))
        d2 = d1 - volatility * np.sqrt(time_to_maturity)
        
        # Delta
        if option_type == 'call':
            delta = np.exp(-dividend_yield * time_to_maturity) * norm.cdf(d1)
        else:
            delta = -np.exp(-dividend_yield * time_to_maturity) * norm.cdf(-d1)
        
        # Gamma（看涨和看跌相同）
        gamma = np.exp(-dividend_yield * time_to_maturity) * norm.pdf(d1) / (spot * volatility * np.sqrt(time_to_maturity))
        
        # Theta
        term1 = -(spot * norm.pdf(d1) * volatility * np.exp(-dividend_yield * time_to_maturity)) / (2 * np.sqrt(time_to_maturity))
        
        if option_type == 'call':
            term2 = risk_free_rate * strike * np.exp(-risk_free_rate * time_to_maturity) * norm.cdf(d2)
            term3 = -dividend_yield * spot * np.exp(-dividend_yield * time_to_maturity) * norm.cdf(d1)
            theta = (term1 - term2 + term3) / 365  # 转换为每日theta
        else:
            term2 = risk_free_rate * strike * np.exp(-risk_free_rate * time_to_maturity) * norm.cdf(-d2)
            term3 = dividend_yield * spot * np.exp(-dividend_yield * time_to_maturity) * norm.cdf(-d1)
            theta = (term1 + term2 - term3) / 365  # 转换为每日theta
        
        # Vega（看涨和看跌相同）
        vega = spot * np.exp(-dividend_yield * time_to_maturity) * norm.pdf(d1) * np.sqrt(time_to_maturity) / 100  # 除以100转换为1%波动率变化的影响
        
        # Rho
        if option_type == 'call':
            rho = strike * time_to_maturity * np.exp(-risk_free_rate * time_to_maturity) * norm.cdf(d2) / 100  # 除以100转换为1%利率变化的影响
        else:
            rho = -strike * time_to_maturity * np.exp(-risk_free_rate * time_to_maturity) * norm.cdf(-d2) / 100
        
        return {
            'delta': float(delta),
            'gamma': float(gamma),
            'theta': float(theta),
            'vega': float(vega),
            'rho': float(rho),
        }
    
    @staticmethod
    def implied_volatility(
        option_price: float,
        spot: float,
        strike: float,
        time_to_maturity: float,
        risk_free_rate: float,
        option_type: str = 'call',
        dividend_yield: float = 0.0,
        max_iterations: int = 100,
        tolerance: float = 1e-5
    ) -> Optional[float]:
        """
        使用牛顿法计算隐含波动率
        
        Args:
            option_price: 期权市场价格
            spot: 标的资产现价
            strike: 行权价
            time_to_maturity: 到期时间（年）
            risk_free_rate: 无风险利率
            option_type: 期权类型
            dividend_yield: 股息率
            max_iterations: 最大迭代次数
            tolerance: 收敛容差
        
        Returns:
            隐含波动率（如果收敛）
        """
        # 初始猜测
        option_price = OptionsPricing._positive_float('option_price', option_price)
        spot = OptionsPricing._positive_float('spot', spot)
        strike = OptionsPricing._positive_float('strike', strike)
        time_to_maturity = OptionsPricing._positive_float('time_to_maturity', time_to_maturity)
        risk_free_rate = OptionsPricing._finite_float('risk_free_rate', risk_free_rate)
        dividend_yield = OptionsPricing._finite_float('dividend_yield', dividend_yield)
        option_type = OptionsPricing._normalize_option_type(option_type)

        volatility = 0.3
        
        for i in range(max_iterations):
            # 计算期权价格
            price = OptionsPricing.black_scholes(
                spot, strike, time_to_maturity, risk_free_rate, volatility, option_type, dividend_yield
            )
            
            # 计算vega
            greeks = OptionsPricing.calculate_greeks(
                spot, strike, time_to_maturity, risk_free_rate, volatility, option_type, dividend_yield
            )
            vega = greeks['vega'] * 100  # 转换回原始vega
            
            # 价格差异
            diff = price - option_price
            
            # 检查收敛
            if abs(diff) < tolerance:
                return float(volatility)
            
            # 牛顿法更新
            if vega > 0:
                volatility = volatility - diff / vega
            else:
                break
            
            # 确保波动率在合理范围内
            volatility = max(0.01, min(volatility, 5.0))
        
        # 未收敛
        return None
    
    @staticmethod
    def calculate_time_to_maturity(expiry_date: str) -> float:
        """
        计算到期时间（年）
        
        Args:
            expiry_date: 到期日期（YYYY-MM-DD格式）
        
        Returns:
            到期时间（年）
        """
        try:
            expiry = datetime.strptime(expiry_date, '%Y-%m-%d')
            now = datetime.now()
            seconds_to_maturity = (expiry - now).total_seconds()
            return max(seconds_to_maturity / (365.0 * 24 * 3600), 0.0)
        except Exception:
            return 0.0


# 全局实例
options_pricing = OptionsPricing()
