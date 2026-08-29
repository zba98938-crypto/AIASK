"""滑点模型模块 - 实现固定滑点、动态滑点、市场冲击模型"""
import numpy as np
from typing import List, Dict, Any, Optional
from enum import Enum


class SlippageModelType(Enum):
    """滑点模型类型"""
    FIXED = "fixed"  # 固定滑点
    VOLUME_BASED = "volume_based"  # 基于成交量的动态滑点
    MARKET_IMPACT = "market_impact"  # 市场冲击模型


class SlippageModel:
    """滑点模型基类"""
    
    def calculate_slippage(
        self,
        price: float,
        volume: float,
        order_size: float,
        is_buy: bool,
        **kwargs
    ) -> float:
        """
        计算滑点
        
        Args:
            price: 当前价格
            volume: 当前成交量
            order_size: 订单大小（股数）
            is_buy: 是否买入
            **kwargs: 其他参数
        
        Returns:
            滑点金额（正数表示成本增加）
        """
        raise NotImplementedError


class FixedSlippageModel(SlippageModel):
    """固定滑点模型"""
    
    def __init__(self, slippage_rate: float = 0.001):
        """
        初始化固定滑点模型
        
        Args:
            slippage_rate: 滑点率（默认0.1%）
        """
        self.slippage_rate = slippage_rate
    
    def calculate_slippage(
        self,
        price: float,
        volume: float,
        order_size: float,
        is_buy: bool,
        **kwargs
    ) -> float:
        """
        计算固定滑点
        
        买入时价格上涨，卖出时价格下跌
        """
        slippage = price * self.slippage_rate
        return slippage if is_buy else -slippage


class VolumeBasedSlippageModel(SlippageModel):
    """基于成交量的动态滑点模型"""
    
    def __init__(
        self,
        base_slippage_rate: float = 0.0005,
        volume_impact_factor: float = 0.1
    ):
        """
        初始化基于成交量的滑点模型
        
        Args:
            base_slippage_rate: 基础滑点率
            volume_impact_factor: 成交量影响因子
        """
        self.base_slippage_rate = base_slippage_rate
        self.volume_impact_factor = volume_impact_factor
    
    def calculate_slippage(
        self,
        price: float,
        volume: float,
        order_size: float,
        is_buy: bool,
        **kwargs
    ) -> float:
        """
        计算基于成交量的动态滑点
        
        参与率越高，滑点越大
        """
        if volume <= 0:
            # 如果没有成交量数据，使用基础滑点
            slippage = price * self.base_slippage_rate
        else:
            # 计算参与率
            participation_rate = order_size / volume
            
            # 滑点 = 基础滑点 + 参与率影响
            slippage_rate = self.base_slippage_rate + \
                           self.volume_impact_factor * participation_rate
            
            slippage = price * slippage_rate
        
        return slippage if is_buy else -slippage


class MarketImpactModel(SlippageModel):
    """市场冲击模型（Almgren-Chriss模型简化版）"""
    
    def __init__(
        self,
        permanent_impact_coef: float = 0.1,
        temporary_impact_coef: float = 0.01,
        volatility: float = 0.02
    ):
        """
        初始化市场冲击模型
        
        Args:
            permanent_impact_coef: 永久冲击系数
            temporary_impact_coef: 临时冲击系数
            volatility: 市场波动率
        """
        self.permanent_impact_coef = permanent_impact_coef
        self.temporary_impact_coef = temporary_impact_coef
        self.volatility = volatility
    
    def calculate_slippage(
        self,
        price: float,
        volume: float,
        order_size: float,
        is_buy: bool,
        **kwargs
    ) -> float:
        """
        计算市场冲击滑点
        
        包括永久冲击和临时冲击
        """
        if volume <= 0:
            volume = 1000000  # 默认成交量
        
        # 计算参与率
        participation_rate = order_size / volume
        
        # 永久冲击（影响后续价格）
        permanent_impact = self.permanent_impact_coef * \
                          self.volatility * \
                          np.sqrt(participation_rate)
        
        # 临时冲击（仅影响当前交易）
        temporary_impact = self.temporary_impact_coef * \
                          self.volatility * \
                          participation_rate
        
        # 总冲击
        total_impact = permanent_impact + temporary_impact
        
        slippage = price * total_impact
        
        return slippage if is_buy else -slippage


class SlippageCalculator:
    """滑点计算器 - 统一接口"""
    
    def __init__(self, model_type: SlippageModelType = SlippageModelType.FIXED):
        """
        初始化滑点计算器
        
        Args:
            model_type: 滑点模型类型
        """
        self.model_type = model_type
        self.model = self._create_model(model_type)
    
    def _create_model(self, model_type: SlippageModelType) -> SlippageModel:
        """创建滑点模型"""
        if model_type == SlippageModelType.FIXED:
            return FixedSlippageModel(slippage_rate=0.001)
        elif model_type == SlippageModelType.VOLUME_BASED:
            return VolumeBasedSlippageModel(
                base_slippage_rate=0.0005,
                volume_impact_factor=0.1
            )
        elif model_type == SlippageModelType.MARKET_IMPACT:
            return MarketImpactModel(
                permanent_impact_coef=0.1,
                temporary_impact_coef=0.01,
                volatility=0.02
            )
        else:
            return FixedSlippageModel()
    
    def calculate(
        self,
        price: float,
        volume: float,
        order_size: float,
        is_buy: bool,
        **kwargs
    ) -> Dict[str, float]:
        """
        计算滑点
        
        Returns:
            {
                'slippage': 滑点金额,
                'slippage_rate': 滑点率,
                'execution_price': 实际成交价格
            }
        """
        slippage = self.model.calculate_slippage(
            price, volume, order_size, is_buy, **kwargs
        )
        
        slippage_rate = slippage / price if price > 0 else 0.0
        execution_price = price + slippage
        
        return {
            'slippage': float(slippage),
            'slippage_rate': float(slippage_rate),
            'execution_price': float(execution_price)
        }
    
    def set_model(self, model_type: SlippageModelType):
        """切换滑点模型"""
        self.model_type = model_type
        self.model = self._create_model(model_type)


# 创建全局实例
slippage_calculator = SlippageCalculator()

