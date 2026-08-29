"""归因分析模块 - 实现Alpha/Beta分解、因子暴露分析、行业归因、时间序列归因"""
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta


class AttributionAnalyzer:
    """归因分析器"""
    
    # ========== Alpha/Beta分解 ==========
    
    @staticmethod
    def calculate_alpha_beta(
        portfolio_returns: List[float],
        benchmark_returns: List[float],
        risk_free_rate: float = 0.03
    ) -> Dict[str, float]:
        """
        计算Alpha和Beta（CAPM模型）
        
        Args:
            portfolio_returns: 组合收益率序列
            benchmark_returns: 基准收益率序列
            risk_free_rate: 无风险利率（年化）
        
        Returns:
            {
                'alpha': Alpha值（年化）,
                'beta': Beta值,
                'r_squared': R²值,
                'tracking_error': 跟踪误差,
                'information_ratio': 信息比率
            }
        """
        if len(portfolio_returns) != len(benchmark_returns):
            raise ValueError("组合收益和基准收益长度不一致")
        
        if len(portfolio_returns) < 10:
            return {
                'alpha': 0.0,
                'beta': 1.0,
                'r_squared': 0.0,
                'tracking_error': 0.0,
                'information_ratio': 0.0
            }
        
        # 转换为numpy数组
        port_ret = np.array(portfolio_returns)
        bench_ret = np.array(benchmark_returns)
        
        # 计算超额收益
        daily_rf = (1 + risk_free_rate) ** (1/252) - 1
        excess_port = port_ret - daily_rf
        excess_bench = bench_ret - daily_rf
        
        # 线性回归计算Beta和Alpha
        covariance = np.cov(excess_port, excess_bench)[0, 1]
        variance = np.var(excess_bench)
        
        if variance == 0:
            beta = 1.0
            alpha = 0.0
        else:
            beta = covariance / variance
            alpha = np.mean(excess_port) - beta * np.mean(excess_bench)
        
        # 年化Alpha
        alpha_annual = alpha * 252
        
        # 计算R²
        predicted = beta * excess_bench + alpha
        ss_res = np.sum((excess_port - predicted) ** 2)
        ss_tot = np.sum((excess_port - np.mean(excess_port)) ** 2)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
        
        # 计算跟踪误差（年化）
        tracking_diff = port_ret - bench_ret
        tracking_error = np.std(tracking_diff) * np.sqrt(252)
        
        # 计算信息比率
        excess_return = np.mean(tracking_diff) * 252
        information_ratio = excess_return / tracking_error if tracking_error > 0 else 0.0
        
        return {
            'alpha': float(alpha_annual),
            'beta': float(beta),
            'r_squared': float(r_squared),
            'tracking_error': float(tracking_error),
            'information_ratio': float(information_ratio)
        }
    
    # ========== 因子暴露分析 ==========
    
    @staticmethod
    def calculate_factor_exposure(
        portfolio_returns: List[float],
        factor_returns: Dict[str, List[float]]
    ) -> Dict[str, Any]:
        """
        计算组合在各因子上的暴露度（多因子回归）
        
        Args:
            portfolio_returns: 组合收益率序列
            factor_returns: 因子收益率字典 {'factor_name': [returns]}
        
        Returns:
            {
                'exposures': {'factor_name': exposure_value},
                'r_squared': R²值,
                'residual_std': 残差标准差
            }
        """
        if not factor_returns:
            return {
                'exposures': {},
                'r_squared': 0.0,
                'residual_std': 0.0
            }
        
        # 检查数据长度一致性
        n = len(portfolio_returns)
        for factor_name, returns in factor_returns.items():
            if len(returns) != n:
                raise ValueError(f"因子{factor_name}的收益率长度不一致")
        
        if n < 10:
            return {
                'exposures': {name: 0.0 for name in factor_returns.keys()},
                'r_squared': 0.0,
                'residual_std': 0.0
            }
        
        # 构建因子矩阵
        y = np.array(portfolio_returns)
        X = np.column_stack([np.array(returns) for returns in factor_returns.values()])
        
        # 添加常数项
        X = np.column_stack([np.ones(n), X])
        
        # 最小二乘回归
        try:
            coefficients = np.linalg.lstsq(X, y, rcond=None)[0]
        except np.linalg.LinAlgError:
            return {
                'exposures': {name: 0.0 for name in factor_returns.keys()},
                'r_squared': 0.0,
                'residual_std': 0.0
            }
        
        # 提取因子暴露度（跳过常数项）
        exposures = {}
        for i, factor_name in enumerate(factor_returns.keys()):
            exposures[factor_name] = float(coefficients[i + 1])
        
        # 计算R²
        predicted = X @ coefficients
        ss_res = np.sum((y - predicted) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

        # 计算残差标准差
        residuals = y - predicted
        residual_std = float(np.std(residuals))

        return {
            'exposures': exposures,
            'r_squared': float(r_squared),
            'residual_std': residual_std
        }

    # ========== 行业归因 ==========

    @staticmethod
    def calculate_industry_attribution(
        portfolio_weights: Dict[str, float],
        portfolio_returns: Dict[str, float],
        benchmark_weights: Dict[str, float],
        benchmark_returns: Dict[str, float],
        stock_industries: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        计算行业归因（Brinson归因模型）

        Args:
            portfolio_weights: 组合权重 {'stock_code': weight}
            portfolio_returns: 组合个股收益 {'stock_code': return}
            benchmark_weights: 基准权重 {'stock_code': weight}
            benchmark_returns: 基准个股收益 {'stock_code': return}
            stock_industries: 股票行业映射 {'stock_code': 'industry'}

        Returns:
            {
                'allocation_effect': 配置效应,
                'selection_effect': 选股效应,
                'interaction_effect': 交互效应,
                'total_active_return': 总超额收益,
                'industry_details': {
                    'industry_name': {
                        'allocation': 配置效应,
                        'selection': 选股效应,
                        'interaction': 交互效应
                    }
                }
            }
        """
        # 计算行业权重和收益
        industries = set(stock_industries.values())

        industry_port_weights = {}
        industry_port_returns = {}
        industry_bench_weights = {}
        industry_bench_returns = {}

        for industry in industries:
            # 组合行业权重和收益
            port_stocks = [s for s, i in stock_industries.items() if i == industry and s in portfolio_weights]
            industry_port_weights[industry] = sum(portfolio_weights.get(s, 0) for s in port_stocks)

            if industry_port_weights[industry] > 0:
                weighted_return = sum(
                    portfolio_weights.get(s, 0) * portfolio_returns.get(s, 0)
                    for s in port_stocks
                )
                industry_port_returns[industry] = weighted_return / industry_port_weights[industry]
            else:
                industry_port_returns[industry] = 0.0

            # 基准行业权重和收益
            bench_stocks = [s for s, i in stock_industries.items() if i == industry and s in benchmark_weights]
            industry_bench_weights[industry] = sum(benchmark_weights.get(s, 0) for s in bench_stocks)

            if industry_bench_weights[industry] > 0:
                weighted_return = sum(
                    benchmark_weights.get(s, 0) * benchmark_returns.get(s, 0)
                    for s in bench_stocks
                )
                industry_bench_returns[industry] = weighted_return / industry_bench_weights[industry]
            else:
                industry_bench_returns[industry] = 0.0

        # 计算基准总收益
        benchmark_total_return = sum(
            benchmark_weights.get(s, 0) * benchmark_returns.get(s, 0)
            for s in benchmark_weights.keys()
        )

        # Brinson归因
        allocation_effect = 0.0
        selection_effect = 0.0
        interaction_effect = 0.0
        industry_details = {}

        for industry in industries:
            wp = industry_port_weights.get(industry, 0)
            wb = industry_bench_weights.get(industry, 0)
            rp = industry_port_returns.get(industry, 0)
            rb = industry_bench_returns.get(industry, 0)

            # 配置效应：(wp - wb) * (rb - benchmark_return)
            alloc = (wp - wb) * (rb - benchmark_total_return)

            # 选股效应：wb * (rp - rb)
            select = wb * (rp - rb)

            # 交互效应：(wp - wb) * (rp - rb)
            interact = (wp - wb) * (rp - rb)

            allocation_effect += alloc
            selection_effect += select
            interaction_effect += interact

            industry_details[industry] = {
                'allocation': float(alloc),
                'selection': float(select),
                'interaction': float(interact)
            }

        # 计算总超额收益
        portfolio_total_return = sum(
            portfolio_weights.get(s, 0) * portfolio_returns.get(s, 0)
            for s in portfolio_weights.keys()
        )
        total_active_return = portfolio_total_return - benchmark_total_return

        return {
            'allocation_effect': float(allocation_effect),
            'selection_effect': float(selection_effect),
            'interaction_effect': float(interaction_effect),
            'total_active_return': float(total_active_return),
            'industry_details': industry_details
        }

    # ========== 时间序列归因 ==========

    @staticmethod
    def calculate_time_series_attribution(
        portfolio_returns: List[float],
        benchmark_returns: List[float],
        dates: List[datetime],
        period: str = 'monthly'
    ) -> Dict[str, Any]:
        """
        按时间周期进行归因分析

        Args:
            portfolio_returns: 组合收益率序列
            benchmark_returns: 基准收益率序列
            dates: 日期序列
            period: 周期类型 ('daily', 'weekly', 'monthly', 'quarterly')

        Returns:
            {
                'period_returns': [
                    {
                        'period': '2024-01',
                        'portfolio_return': 0.05,
                        'benchmark_return': 0.03,
                        'active_return': 0.02,
                        'alpha': 0.015,
                        'beta': 1.2
                    }
                ],
                'cumulative_active_return': 累计超额收益
            }
        """
        if len(portfolio_returns) != len(benchmark_returns) or len(portfolio_returns) != len(dates):
            raise ValueError("收益率和日期序列长度不一致")

        # 按周期分组
        period_data = {}

        for i, date in enumerate(dates):
            if period == 'daily':
                key = date.strftime('%Y-%m-%d')
            elif period == 'weekly':
                key = f"{date.year}-W{date.isocalendar()[1]:02d}"
            elif period == 'monthly':
                key = date.strftime('%Y-%m')
            elif period == 'quarterly':
                quarter = (date.month - 1) // 3 + 1
                key = f"{date.year}-Q{quarter}"
            else:
                key = date.strftime('%Y-%m')

            if key not in period_data:
                period_data[key] = {
                    'portfolio_returns': [],
                    'benchmark_returns': []
                }

            period_data[key]['portfolio_returns'].append(portfolio_returns[i])
            period_data[key]['benchmark_returns'].append(benchmark_returns[i])

        # 计算每个周期的归因
        period_returns = []
        cumulative_active_return = 0.0

        for period_key in sorted(period_data.keys()):
            data = period_data[period_key]
            port_rets = data['portfolio_returns']
            bench_rets = data['benchmark_returns']

            # 计算周期收益
            period_port_return = np.prod([1 + r for r in port_rets]) - 1
            period_bench_return = np.prod([1 + r for r in bench_rets]) - 1
            active_return = period_port_return - period_bench_return

            # 计算Alpha和Beta
            if len(port_rets) >= 5:
                alpha_beta = AttributionAnalyzer.calculate_alpha_beta(
                    port_rets, bench_rets, risk_free_rate=0.03
                )
                alpha = alpha_beta['alpha']
                beta = alpha_beta['beta']
            else:
                alpha = 0.0
                beta = 1.0

            period_returns.append({
                'period': period_key,
                'portfolio_return': float(period_port_return),
                'benchmark_return': float(period_bench_return),
                'active_return': float(active_return),
                'alpha': float(alpha),
                'beta': float(beta)
            })

            cumulative_active_return += active_return

        return {
            'period_returns': period_returns,
            'cumulative_active_return': float(cumulative_active_return)
        }

    # ========== 风格归因分析 ==========

    @staticmethod
    def calculate_style_attribution(
        portfolio_weights: Dict[str, float],
        portfolio_returns: Dict[str, float],
        benchmark_weights: Dict[str, float],
        benchmark_returns: Dict[str, float],
        stock_styles: Dict[str, Dict[str, float]]
    ) -> Dict[str, Any]:
        """
        计算风格归因（基于Fama-French等风格因子）

        Args:
            portfolio_weights: 组合权重 {'stock_code': weight}
            portfolio_returns: 组合个股收益 {'stock_code': return}
            benchmark_weights: 基准权重 {'stock_code': weight}
            benchmark_returns: 基准个股收益 {'stock_code': return}
            stock_styles: 股票风格特征 {'stock_code': {'size': 0.5, 'value': 0.3, 'momentum': 0.2}}

        Returns:
            {
                'style_exposures': {
                    'size': {'portfolio': 0.5, 'benchmark': 0.4, 'active': 0.1},
                    'value': {'portfolio': 0.3, 'benchmark': 0.35, 'active': -0.05},
                    ...
                },
                'style_contributions': {
                    'size': 0.02,  # 该风格对超额收益的贡献
                    'value': -0.01,
                    ...
                },
                'total_style_effect': 0.01,
                'residual_effect': 0.005
            }
        """
        # 获取所有风格因子
        style_factors = set()
        for styles in stock_styles.values():
            style_factors.update(styles.keys())

        # 计算组合和基准的风格暴露
        style_exposures = {}

        for style in style_factors:
            # 组合风格暴露
            port_exposure = sum(
                portfolio_weights.get(stock, 0) * stock_styles.get(stock, {}).get(style, 0)
                for stock in portfolio_weights.keys()
            )

            # 基准风格暴露
            bench_exposure = sum(
                benchmark_weights.get(stock, 0) * stock_styles.get(stock, {}).get(style, 0)
                for stock in benchmark_weights.keys()
            )

            # 主动风格暴露
            active_exposure = port_exposure - bench_exposure

            style_exposures[style] = {
                'portfolio': float(port_exposure),
                'benchmark': float(bench_exposure),
                'active': float(active_exposure)
            }

        # 计算风格因子收益（简化版：使用加权平均收益）
        style_returns = {}
        for style in style_factors:
            # 计算该风格因子的收益（高风格暴露股票的平均收益）
            style_stocks = [
                (stock, stock_styles.get(stock, {}).get(style, 0))
                for stock in portfolio_returns.keys()
                if stock_styles.get(stock, {}).get(style, 0) > 0
            ]

            if style_stocks:
                total_style_value = sum(s[1] for s in style_stocks)
                if total_style_value > 0:
                    weighted_return = sum(
                        portfolio_returns.get(s[0], 0) * s[1]
                        for s in style_stocks
                    ) / total_style_value
                    style_returns[style] = weighted_return
                else:
                    style_returns[style] = 0.0
            else:
                style_returns[style] = 0.0

        # 计算风格贡献
        style_contributions = {}
        total_style_effect = 0.0

        for style in style_factors:
            # 风格贡献 = 主动风格暴露 × 风格因子收益
            contribution = style_exposures[style]['active'] * style_returns.get(style, 0)
            style_contributions[style] = float(contribution)
            total_style_effect += contribution

        # 计算总超额收益
        portfolio_total_return = sum(
            portfolio_weights.get(s, 0) * portfolio_returns.get(s, 0)
            for s in portfolio_weights.keys()
        )
        benchmark_total_return = sum(
            benchmark_weights.get(s, 0) * benchmark_returns.get(s, 0)
            for s in benchmark_weights.keys()
        )
        total_active_return = portfolio_total_return - benchmark_total_return

        # 残差效应 = 总超额收益 - 风格效应
        residual_effect = total_active_return - total_style_effect

        return {
            'style_exposures': style_exposures,
            'style_contributions': style_contributions,
            'total_style_effect': float(total_style_effect),
            'residual_effect': float(residual_effect),
            'total_active_return': float(total_active_return)
        }

    # ========== 完整Brinson归因（多层次） ==========

    @staticmethod
    def calculate_full_brinson_attribution(
        portfolio_weights: Dict[str, float],
        portfolio_returns: Dict[str, float],
        benchmark_weights: Dict[str, float],
        benchmark_returns: Dict[str, float],
        stock_industries: Dict[str, str],
        stock_styles: Optional[Dict[str, Dict[str, float]]] = None
    ) -> Dict[str, Any]:
        """
        完整的Brinson归因分析（行业+风格）

        Args:
            portfolio_weights: 组合权重
            portfolio_returns: 组合个股收益
            benchmark_weights: 基准权重
            benchmark_returns: 基准个股收益
            stock_industries: 股票行业映射
            stock_styles: 股票风格特征（可选）

        Returns:
            {
                'industry_attribution': {...},  # 行业归因结果
                'style_attribution': {...},     # 风格归因结果（如果提供）
                'summary': {
                    'total_active_return': 总超额收益,
                    'industry_effect': 行业效应,
                    'style_effect': 风格效应,
                    'selection_effect': 选股效应,
                    'residual_effect': 残差效应
                }
            }
        """
        # 行业归因
        industry_attr = AttributionAnalyzer.calculate_industry_attribution(
            portfolio_weights, portfolio_returns,
            benchmark_weights, benchmark_returns,
            stock_industries
        )

        result = {
            'industry_attribution': industry_attr
        }

        # 风格归因（如果提供）
        if stock_styles:
            style_attr = AttributionAnalyzer.calculate_style_attribution(
                portfolio_weights, portfolio_returns,
                benchmark_weights, benchmark_returns,
                stock_styles
            )
            result['style_attribution'] = style_attr

        # 汇总
        summary = {
            'total_active_return': industry_attr['total_active_return'],
            'industry_allocation_effect': industry_attr['allocation_effect'],
            'industry_selection_effect': industry_attr['selection_effect'],
            'industry_interaction_effect': industry_attr['interaction_effect']
        }

        if stock_styles:
            summary['style_effect'] = style_attr['total_style_effect']
            summary['residual_effect'] = style_attr['residual_effect']

        result['summary'] = summary

        return result


# 创建全局实例
attribution_analyzer = AttributionAnalyzer()
