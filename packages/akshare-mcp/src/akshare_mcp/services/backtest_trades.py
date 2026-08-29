"""
回测交易记录模块

Phase 2 实现 - MCP 服务开发方案

提供回测交易记录的管理和可视化输出能力：
- 交易记录存储和管理
- 回测结果转换为客户端绘图格式
- 供外部展示层消费的结构化数据
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime
import numpy as np


@dataclass
class Trade:
    """单笔交易记录"""
    time: str                    # 交易时间 (YYYY-MM-DD HH:MM:SS)
    price: float                 # 交易价格
    signal: int                  # 交易信号 (1=买入, -1=卖出)
    shares: int = 0              # 交易股数
    amount: float = 0.0          # 交易金额
    commission: float = 0.0      # 手续费
    profit: float = 0.0          # 盈亏金额 (卖出时计算)
    profit_pct: float = 0.0      # 盈亏比例
    holding_days: int = 0        # 持仓天数
    reason: str = ""             # 交易原因


@dataclass
class BacktestTradeResult:
    """回测交易结果"""
    code: str                    # 股票代码
    strategy: str                # 策略名称
    trades: List[Trade] = field(default_factory=list)
    
    # 统计数据
    total_trades: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    win_rate: float = 0.0
    total_profit: float = 0.0
    max_profit: float = 0.0
    max_loss: float = 0.0
    avg_profit: float = 0.0
    avg_holding_days: float = 0.0
    
    def calculate_stats(self):
        """计算统计数据"""
        if not self.trades:
            return
        
        self.total_trades = len([t for t in self.trades if t.signal == -1])  # 只统计卖出
        profits = [t.profit for t in self.trades if t.signal == -1]
        
        if profits:
            self.win_trades = len([p for p in profits if p > 0])
            self.loss_trades = len([p for p in profits if p < 0])
            self.win_rate = self.win_trades / len(profits) if profits else 0
            self.total_profit = sum(profits)
            self.max_profit = max(profits) if profits else 0
            self.max_loss = min(profits) if profits else 0
            self.avg_profit = np.mean(profits) if profits else 0
        
        holding_days = [t.holding_days for t in self.trades if t.signal == -1 and t.holding_days > 0]
        if holding_days:
            self.avg_holding_days = np.mean(holding_days)


class BacktestTradeManager:
    """回测交易记录管理器"""
    
    def __init__(self):
        self.results: Dict[str, BacktestTradeResult] = {}
    
    def create_result(self, code: str, strategy: str) -> BacktestTradeResult:
        """创建新的回测结果"""
        key = f"{code}_{strategy}"
        result = BacktestTradeResult(code=code, strategy=strategy)
        self.results[key] = result
        return result
    
    def add_trade(
        self,
        code: str,
        strategy: str,
        time: str,
        price: float,
        signal: int,
        shares: int = 0,
        amount: float = 0.0,
        commission: float = 0.0,
        profit: float = 0.0,
        reason: str = ""
    ) -> Trade:
        """添加交易记录"""
        key = f"{code}_{strategy}"
        if key not in self.results:
            self.create_result(code, strategy)
        
        trade = Trade(
            time=time,
            price=price,
            signal=signal,
            shares=shares,
            amount=amount,
            commission=commission,
            profit=profit,
            reason=reason
        )
        
        self.results[key].trades.append(trade)
        return trade
    
    def get_result(self, code: str, strategy: str) -> Optional[BacktestTradeResult]:
        """获取回测结果"""
        key = f"{code}_{strategy}"
        return self.results.get(key)
    
    def finalize_result(self, code: str, strategy: str) -> Optional[BacktestTradeResult]:
        """完成回测并计算统计数据"""
        result = self.get_result(code, strategy)
        if result:
            result.calculate_stats()
        return result
    
    def to_visualization_format(self, code: str, strategy: str) -> Dict[str, Any]:
        """
        转换为通用可视化数据格式
        
        Returns:
            dict: {
                "time_list": ["YYYYMMDDHHMMSS", ...],
                "data_list": [["price", "signal", "shares", "profit"], ...],
                "count": 4
            }
        """
        result = self.get_result(code, strategy)
        if not result or not result.trades:
            return {"time_list": [], "data_list": [], "count": 0}
        
        time_list = []
        data_list = []
        
        for trade in result.trades:
            # 转换时间格式
            time_str = trade.time.replace("-", "").replace(":", "").replace(" ", "")
            if len(time_str) < 14:
                time_str = time_str.ljust(14, "0")
            time_list.append(time_str[:14])
            
            # 构建数据: [价格, 信号, 股数, 盈亏]
            data_list.append([
                str(trade.price),
                str(trade.signal),
                str(trade.shares),
                str(trade.profit)
            ])
        
        return {
            "time_list": time_list,
            "data_list": data_list,
            "count": 4
        }


# 全局管理器实例
trade_manager = BacktestTradeManager()
