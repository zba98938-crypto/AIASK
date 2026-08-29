"""基本面因子 - 价值/质量/成长/盈利/杠杆/规模/Beta/流动性"""
import numpy as np
from typing import List


class FundamentalFactorsMixin:
    """基本面因子混入类"""

    # ========== 综合因子 ==========

    @staticmethod
    def calculate_value_factor(pe: float, pb: float, ps: float = None) -> float:
        """价值因子（综合PE、PB、PS）"""
        score = 0.0
        count = 0
        if pe > 0:
            score += 1.0 / pe
            count += 1
        if pb > 0:
            score += 1.0 / pb
            count += 1
        if ps and ps > 0:
            score += 1.0 / ps
            count += 1
        return score / count if count > 0 else 0.0

    @staticmethod
    def calculate_quality_factor(roe: float, debt_ratio: float, profit_growth: float = None) -> float:
        """质量因子（ROE、负债率、利润增长）"""
        score = 0.0
        if roe:
            score += min(roe / 20.0, 1.0)
        if debt_ratio:
            score += max(1.0 - debt_ratio, 0.0)
        if profit_growth:
            score += min(profit_growth / 50.0, 1.0)
        return score / 3.0

    @staticmethod
    def calculate_growth_factor(revenue_growth: float, profit_growth: float) -> float:
        """成长因子"""
        score = 0.0
        count = 0
        if revenue_growth is not None:
            score += min(revenue_growth / 30.0, 1.0)
            count += 1
        if profit_growth is not None:
            score += min(profit_growth / 30.0, 1.0)
            count += 1
        return score / count if count > 0 else 0.0

    @staticmethod
    def calculate_profitability_factor(net_profit_margin: float, roe: float, roa: float = None) -> float:
        """盈利能力因子"""
        score = 0.0
        count = 0
        if net_profit_margin:
            score += min(net_profit_margin / 20.0, 1.0)
            count += 1
        if roe:
            score += min(roe / 20.0, 1.0)
            count += 1
        if roa:
            score += min(roa / 10.0, 1.0)
            count += 1
        return score / count if count > 0 else 0.0

    @staticmethod
    def calculate_leverage_factor(debt_ratio: float, current_ratio: float = None) -> float:
        """杠杆因子"""
        score = 0.0
        count = 0
        if debt_ratio is not None:
            if 0.3 <= debt_ratio <= 0.6:
                score += 1.0
            elif debt_ratio < 0.3:
                score += 0.7
            else:
                score += max(0, 1.0 - (debt_ratio - 0.6) / 0.4)
            count += 1
        if current_ratio:
            score += min(current_ratio / 2.0, 1.0)
            count += 1
        return score / count if count > 0 else 0.0

    # ========== 价值因子（新增7个）==========

    @staticmethod
    def calculate_cfp(cfps: float, price: float) -> float:
        """市现率倒数 CFP = CFPS / Price"""
        if not price or price <= 0:
            return 0.0
        if not cfps:
            return 0.0
        return cfps / price

    @staticmethod
    def calculate_dp(dps: float, price: float) -> float:
        """股息率 DP = DPS / Price"""
        if not price or price <= 0:
            return 0.0
        if not dps:
            return 0.0
        return dps / price

    @staticmethod
    def calculate_ev_ebitda(ev: float, ebitda: float) -> float:
        """企业价值倍数 EV/EBITDA"""
        if not ebitda or ebitda <= 0:
            return 0.0
        if not ev:
            return 0.0
        return ev / ebitda

    @staticmethod
    def calculate_ev_sales(ev: float, revenue: float) -> float:
        """EV/销售额 EV/Sales"""
        if not revenue or revenue <= 0:
            return 0.0
        if not ev:
            return 0.0
        return ev / revenue

    @staticmethod
    def calculate_earnings_yield(ebit: float, ev: float) -> float:
        """盈利收益率 EBIT / EV"""
        if not ev or ev <= 0:
            return 0.0
        if not ebit:
            return 0.0
        return ebit / ev

    @staticmethod
    def calculate_fcf_yield(fcf: float, market_cap: float) -> float:
        """自由现金流收益率 FCF / Market Cap"""
        if not market_cap or market_cap <= 0:
            return 0.0
        if not fcf:
            return 0.0
        return fcf / market_cap

    @staticmethod
    def calculate_book_leverage(total_debt: float, equity: float) -> float:
        """账面杠杆 Total Debt / Equity"""
        if not equity or equity <= 0:
            return 0.0
        if not total_debt:
            return 0.0
        return total_debt / equity

    # ========== 质量因子（新增10个）==========

    @staticmethod
    def calculate_roe(net_income: float, equity: float) -> float:
        """净资产收益率 ROE = Net Income / Equity"""
        if not equity or equity <= 0:
            return 0.0
        if not net_income:
            return 0.0
        return net_income / equity

    @staticmethod
    def calculate_roa(net_income: float, total_assets: float) -> float:
        """总资产收益率 ROA = Net Income / Total Assets"""
        if not total_assets or total_assets <= 0:
            return 0.0
        if not net_income:
            return 0.0
        return net_income / total_assets

    @staticmethod
    def calculate_roic(nopat: float, invested_capital: float) -> float:
        """投入资本回报率 ROIC = NOPAT / Invested Capital"""
        if not invested_capital or invested_capital <= 0:
            return 0.0
        if not nopat:
            return 0.0
        return nopat / invested_capital

    @staticmethod
    def calculate_gross_margin(gross_profit: float, revenue: float) -> float:
        """毛利率 Gross Margin = Gross Profit / Revenue"""
        if not revenue or revenue <= 0:
            return 0.0
        if not gross_profit:
            return 0.0
        return gross_profit / revenue

    @staticmethod
    def calculate_operating_margin(operating_income: float, revenue: float) -> float:
        """营业利润率 Operating Margin = Operating Income / Revenue"""
        if not revenue or revenue <= 0:
            return 0.0
        if not operating_income:
            return 0.0
        return operating_income / revenue

    @staticmethod
    def calculate_net_margin(net_income: float, revenue: float) -> float:
        """净利率 Net Margin = Net Income / Revenue"""
        if not revenue or revenue <= 0:
            return 0.0
        if not net_income:
            return 0.0
        return net_income / revenue

    @staticmethod
    def calculate_asset_turnover(revenue: float, total_assets: float) -> float:
        """资产周转率 Asset Turnover = Revenue / Total Assets"""
        if not total_assets or total_assets <= 0:
            return 0.0
        if not revenue:
            return 0.0
        return revenue / total_assets

    @staticmethod
    def calculate_current_ratio(current_assets: float, current_liabilities: float) -> float:
        """流动比率 Current Ratio = Current Assets / Current Liabilities"""
        if not current_liabilities or current_liabilities <= 0:
            return 0.0
        if not current_assets:
            return 0.0
        return current_assets / current_liabilities

    @staticmethod
    def calculate_quick_ratio(current_assets: float, inventory: float, current_liabilities: float) -> float:
        """速动比率 Quick Ratio = (Current Assets - Inventory) / Current Liabilities"""
        if not current_liabilities or current_liabilities <= 0:
            return 0.0
        quick_assets = (current_assets or 0) - (inventory or 0)
        return quick_assets / current_liabilities

    @staticmethod
    def calculate_interest_coverage(ebit: float, interest_expense: float) -> float:
        """利息保障倍数 Interest Coverage = EBIT / Interest Expense"""
        if not interest_expense or interest_expense <= 0:
            return 0.0
        if not ebit:
            return 0.0
        return ebit / interest_expense

    # ========== 成长因子（新增10个）==========

    @staticmethod
    def calculate_revenue_growth_yoy(revenue: float, revenue_ly: float) -> float:
        """营收同比增长率 Revenue Growth YoY"""
        if not revenue_ly or revenue_ly <= 0:
            return 0.0
        if not revenue:
            return 0.0
        return (revenue - revenue_ly) / revenue_ly

    @staticmethod
    def calculate_revenue_growth_qoq(revenue: float, revenue_lq: float) -> float:
        """营收环比增长率 Revenue Growth QoQ"""
        if not revenue_lq or revenue_lq <= 0:
            return 0.0
        if not revenue:
            return 0.0
        return (revenue - revenue_lq) / revenue_lq

    @staticmethod
    def calculate_profit_growth_yoy(profit: float, profit_ly: float) -> float:
        """利润同比增长率 Profit Growth YoY"""
        if not profit_ly or profit_ly <= 0:
            return 0.0
        if not profit:
            return 0.0
        return (profit - profit_ly) / profit_ly

    @staticmethod
    def calculate_profit_growth_qoq(profit: float, profit_lq: float) -> float:
        """利润环比增长率 Profit Growth QoQ"""
        if not profit_lq or profit_lq <= 0:
            return 0.0
        if not profit:
            return 0.0
        return (profit - profit_lq) / profit_lq

    @staticmethod
    def calculate_eps_growth_yoy(eps: float, eps_ly: float) -> float:
        """EPS同比增长率 EPS Growth YoY"""
        if not eps_ly or eps_ly <= 0:
            return 0.0
        if not eps:
            return 0.0
        return (eps - eps_ly) / eps_ly

    @staticmethod
    def calculate_eps_growth_qoq(eps: float, eps_lq: float) -> float:
        """EPS环比增长率 EPS Growth QoQ"""
        if not eps_lq or eps_lq <= 0:
            return 0.0
        if not eps:
            return 0.0
        return (eps - eps_lq) / eps_lq

    @staticmethod
    def calculate_asset_growth(total_assets: float, total_assets_ly: float) -> float:
        """总资产增长率 Asset Growth"""
        if not total_assets_ly or total_assets_ly <= 0:
            return 0.0
        if not total_assets:
            return 0.0
        return (total_assets - total_assets_ly) / total_assets_ly

    @staticmethod
    def calculate_equity_growth(equity: float, equity_ly: float) -> float:
        """净资产增长率 Equity Growth"""
        if not equity_ly or equity_ly <= 0:
            return 0.0
        if not equity:
            return 0.0
        return (equity - equity_ly) / equity_ly

    @staticmethod
    def calculate_operating_cf_growth(ocf: float, ocf_ly: float) -> float:
        """经营现金流增长率 Operating CF Growth"""
        if not ocf_ly or ocf_ly <= 0:
            return 0.0
        if not ocf:
            return 0.0
        return (ocf - ocf_ly) / ocf_ly

    @staticmethod
    def calculate_capex_growth(capex: float, capex_ly: float) -> float:
        """资本支出增长率 CAPEX Growth"""
        if not capex_ly or capex_ly <= 0:
            return 0.0
        if not capex:
            return 0.0
        return (capex - capex_ly) / capex_ly

    # ========== 风格因子 ==========

    @staticmethod
    def calculate_size_factor(market_cap: float) -> float:
        """规模因子（市值）"""
        if not market_cap or market_cap <= 0:
            return 0.0
        return np.log(market_cap)

    @staticmethod
    def calculate_beta_factor(stock_returns: List[float], market_returns: List[float]) -> float:
        """Beta因子"""
        if len(stock_returns) < 20 or len(market_returns) < 20:
            return 1.0
        min_len = min(len(stock_returns), len(market_returns))
        stock_ret = np.array(stock_returns[-min_len:])
        market_ret = np.array(market_returns[-min_len:])
        covariance = np.cov(stock_ret, market_ret)[0, 1]
        market_variance = np.var(market_ret)
        if market_variance == 0:
            return 1.0
        return covariance / market_variance

    @staticmethod
    def calculate_liquidity_factor(volumes: List[float], market_caps: List[float]) -> float:
        """流动性因子（换手率）"""
        if not volumes or not market_caps:
            return 0.0
        turnover_rates = []
        for vol, cap in zip(volumes[-20:], market_caps[-20:]):
            if cap > 0:
                turnover_rates.append(vol / cap)
        return np.mean(turnover_rates) if turnover_rates else 0.0
