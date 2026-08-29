"""
压力测试模块

支持多种市场压力场景测试：
- 市场暴跌场景
- 波动率飙升场景
- 流动性危机场景
- 利率冲击场景
- 汇率波动场景

Author: AKShare MCP Server
Version: 2.0
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime


class StressTestScenario:
    """压力测试场景"""
    
    @staticmethod
    def market_crash(
        returns: np.ndarray,
        crash_magnitude: float = -0.10
    ) -> np.ndarray:
        """
        市场暴跌场景
        
        Args:
            returns: 历史收益率序列
            crash_magnitude: 暴跌幅度（默认-10%）
            
        Returns:
            压力测试后的收益率序列
        """
        stressed_returns = returns.copy()
        # 在第一天应用暴跌
        stressed_returns[0] = crash_magnitude
        return stressed_returns
    
    @staticmethod
    def volatility_spike(
        returns: np.ndarray,
        vol_multiplier: float = 2.0
    ) -> np.ndarray:
        """
        波动率飙升场景
        
        Args:
            returns: 历史收益率序列
            vol_multiplier: 波动率倍数（默认2倍）
            
        Returns:
            压力测试后的收益率序列
        """
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        
        # 保持均值，增加波动率
        stressed_returns = mean_return + (returns - mean_return) * vol_multiplier
        return stressed_returns
    
    @staticmethod
    def liquidity_crisis(
        returns: np.ndarray,
        prices: np.ndarray,
        liquidity_shock: float = 0.5
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        流动性危机场景
        
        Args:
            returns: 历史收益率序列
            prices: 历史价格序列
            liquidity_shock: 流动性冲击（默认50%）
            
        Returns:
            (压力测试后的收益率, 压力测试后的价格)
        """
        # 模拟买卖价差扩大
        bid_ask_spread = liquidity_shock * 0.01  # 转换为百分比
        
        # 调整价格（假设卖出时价格下降）
        stressed_prices = prices * (1 - bid_ask_spread)
        
        # 重新计算收益率
        stressed_returns = np.diff(stressed_prices) / stressed_prices[:-1]
        stressed_returns = np.insert(stressed_returns, 0, returns[0])
        
        return stressed_returns, stressed_prices
    
    @staticmethod
    def interest_rate_shock(
        returns: np.ndarray,
        rate_change: float = 0.02
    ) -> np.ndarray:
        """
        利率冲击场景
        
        Args:
            returns: 历史收益率序列
            rate_change: 利率变化（默认+200bp）
            
        Returns:
            压力测试后的收益率序列
        """
        # 简化模型：利率上升导致股票收益下降
        # 假设利率每上升1%，股票收益下降0.5%
        impact_factor = -0.5
        adjustment = rate_change * impact_factor
        
        stressed_returns = returns + adjustment / len(returns)
        return stressed_returns
    
    @staticmethod
    def currency_volatility(
        returns: np.ndarray,
        fx_volatility: float = 0.05
    ) -> np.ndarray:
        """
        汇率波动场景
        
        Args:
            returns: 历史收益率序列
            fx_volatility: 汇率波动幅度（默认±5%）
            
        Returns:
            压力测试后的收益率序列
        """
        # 模拟汇率波动对收益的影响
        fx_shocks = np.random.uniform(-fx_volatility, fx_volatility, len(returns))
        stressed_returns = returns + fx_shocks
        return stressed_returns


class StressTestAnalyzer:
    """压力测试分析器"""
    
    def __init__(self, portfolio_returns: np.ndarray, portfolio_value: float):
        """
        初始化压力测试分析器
        
        Args:
            portfolio_returns: 组合历史收益率
            portfolio_value: 组合当前价值
        """
        self.portfolio_returns = portfolio_returns
        self.portfolio_value = portfolio_value
        self.scenarios = {}
    
    def run_scenario(
        self,
        scenario_name: str,
        scenario_func: callable,
        **kwargs
    ) -> Dict[str, Any]:
        """
        运行单个压力测试场景
        
        Args:
            scenario_name: 场景名称
            scenario_func: 场景函数
            **kwargs: 场景参数
            
        Returns:
            {
                'scenario_name': 场景名称,
                'stressed_returns': 压力测试后的收益率,
                'total_return': 总收益率,
                'max_drawdown': 最大回撤,
                'var_95': 95% VaR,
                'cvar_95': 95% CVaR
            }
        """
        # 运行场景
        stressed_returns = scenario_func(self.portfolio_returns, **kwargs)
        
        # 计算指标
        total_return = np.prod(1 + stressed_returns) - 1
        max_drawdown = self._calculate_max_drawdown(stressed_returns)
        var_95 = np.percentile(stressed_returns, 5)
        cvar_95 = np.mean(stressed_returns[stressed_returns <= var_95])

        result = {
            'scenario_name': scenario_name,
            'stressed_returns': stressed_returns,
            'total_return': float(total_return),
            'max_drawdown': float(max_drawdown),
            'var_95': float(var_95),
            'cvar_95': float(cvar_95),
            'final_value': float(self.portfolio_value * (1 + total_return))
        }

        self.scenarios[scenario_name] = result
        return result

    def run_all_scenarios(self) -> Dict[str, Dict[str, Any]]:
        """
        运行所有预定义的压力测试场景

        Returns:
            所有场景的测试结果
        """
        scenarios = {
            'market_crash_10': (StressTestScenario.market_crash, {'crash_magnitude': -0.10}),
            'market_crash_20': (StressTestScenario.market_crash, {'crash_magnitude': -0.20}),
            'market_crash_30': (StressTestScenario.market_crash, {'crash_magnitude': -0.30}),
            'volatility_spike_2x': (StressTestScenario.volatility_spike, {'vol_multiplier': 2.0}),
            'volatility_spike_3x': (StressTestScenario.volatility_spike, {'vol_multiplier': 3.0}),
            'interest_rate_up_100bp': (StressTestScenario.interest_rate_shock, {'rate_change': 0.01}),
            'interest_rate_up_200bp': (StressTestScenario.interest_rate_shock, {'rate_change': 0.02}),
            'currency_volatility_5pct': (StressTestScenario.currency_volatility, {'fx_volatility': 0.05}),
            'currency_volatility_10pct': (StressTestScenario.currency_volatility, {'fx_volatility': 0.10})
        }

        results = {}
        for name, (func, kwargs) in scenarios.items():
            results[name] = self.run_scenario(name, func, **kwargs)

        return results

    def get_worst_case(self) -> Dict[str, Any]:
        """获取最坏情况"""
        if not self.scenarios:
            return {}

        worst_scenario = min(
            self.scenarios.items(),
            key=lambda x: x[1]['total_return']
        )

        return {
            'scenario_name': worst_scenario[0],
            'total_return': worst_scenario[1]['total_return'],
            'max_drawdown': worst_scenario[1]['max_drawdown'],
            'final_value': worst_scenario[1]['final_value']
        }

    def get_summary(self) -> Dict[str, Any]:
        """获取压力测试汇总"""
        if not self.scenarios:
            return {}

        returns = [s['total_return'] for s in self.scenarios.values()]
        drawdowns = [s['max_drawdown'] for s in self.scenarios.values()]

        return {
            'total_scenarios': len(self.scenarios),
            'avg_return': float(np.mean(returns)),
            'min_return': float(np.min(returns)),
            'max_return': float(np.max(returns)),
            'avg_drawdown': float(np.mean(drawdowns)),
            'max_drawdown': float(np.max(drawdowns)),
            'worst_case': self.get_worst_case()
        }

    @staticmethod
    def _calculate_max_drawdown(returns: np.ndarray) -> float:
        """计算最大回撤"""
        cumulative = np.cumprod(1 + returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (cumulative - running_max) / running_max
        return float(np.min(drawdown))


# 创建全局实例
stress_test_analyzer = None  # 需要在使用时初始化

