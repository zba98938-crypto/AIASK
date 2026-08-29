"""
模拟交易系统（Paper Trading）

提供完整的模拟交易功能：
- 订单管理（限价/市价/止损）
- 持仓跟踪
- 盈亏计算
- 实时风险监控
- 交易日志

Author: AKShare MCP Server
Version: 2.0
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
from enum import Enum
import uuid


class OrderType(Enum):
    """订单类型"""
    MARKET = "market"  # 市价单
    LIMIT = "limit"    # 限价单
    STOP = "stop"      # 止损单
    STOP_LIMIT = "stop_limit"  # 止损限价单


class OrderSide(Enum):
    """订单方向"""
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"          # 待提交
    SUBMITTED = "submitted"      # 已提交（等待撮合）
    PARTIAL = "partial"          # 部分成交
    FILLED = "filled"            # 完全成交
    CANCELLED = "cancelled"      # 已取消
    REJECTED = "rejected"        # 已拒绝


class Order:
    """订单类"""
    
    def __init__(self, symbol: str, side: OrderSide, order_type: OrderType,
                 quantity: float, price: Optional[float] = None,
                 stop_price: Optional[float] = None):
        self.order_id = str(uuid.uuid4())
        self.symbol = symbol
        self.side = side
        self.order_type = order_type
        self.quantity = quantity
        self.price = price
        self.stop_price = stop_price
        self.filled_quantity = 0.0
        self.avg_fill_price = 0.0
        self.status = OrderStatus.PENDING
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
        self.trades = []
    
    def fill(self, quantity: float, price: float):
        """成交订单"""
        self.filled_quantity += quantity
        # 更新平均成交价
        total_value = self.avg_fill_price * (self.filled_quantity - quantity) + price * quantity
        self.avg_fill_price = total_value / self.filled_quantity
        
        self.trades.append({
            'quantity': quantity,
            'price': price,
            'timestamp': datetime.now()
        })
        
        if self.filled_quantity >= self.quantity:
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.PARTIAL
        
        self.updated_at = datetime.now()
    
    def cancel(self):
        """取消订单"""
        self.status = OrderStatus.CANCELLED
        self.updated_at = datetime.now()
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'order_id': self.order_id,
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'quantity': self.quantity,
            'price': self.price,
            'stop_price': self.stop_price,
            'filled_quantity': self.filled_quantity,
            'avg_fill_price': self.avg_fill_price,
            'status': self.status.value,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'trades': self.trades
        }


class Position:
    """持仓类"""
    
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.quantity = 0.0
        self.avg_cost = 0.0
        self.realized_pnl = 0.0
        self.trades = []
    
    def add(self, quantity: float, price: float):
        """增加持仓"""
        if self.quantity == 0:
            self.avg_cost = price
        else:
            total_cost = self.avg_cost * self.quantity + price * quantity
            new_qty = self.quantity + quantity
            self.avg_cost = total_cost / new_qty if new_qty > 0 else 0

        self.quantity += quantity
        self.trades.append({
            'type': 'buy',
            'quantity': quantity,
            'price': price,
            'timestamp': datetime.now()
        })
    
    def reduce(self, quantity: float, price: float):
        """减少持仓"""
        if quantity > self.quantity:
            raise ValueError(f"Cannot reduce {quantity}, only have {self.quantity}")
        
        # 计算已实现盈亏
        pnl = (price - self.avg_cost) * quantity
        self.realized_pnl += pnl
        
        self.quantity -= quantity
        self.trades.append({
            'type': 'sell',
            'quantity': quantity,
            'price': price,
            'pnl': pnl,
            'timestamp': datetime.now()
        })
        
        if self.quantity == 0:
            self.avg_cost = 0.0
    
    def unrealized_pnl(self, current_price: float) -> float:
        """计算未实现盈亏"""
        if self.quantity == 0:
            return 0.0
        return (current_price - self.avg_cost) * self.quantity
    
    def total_pnl(self, current_price: float) -> float:
        """计算总盈亏"""
        return self.realized_pnl + self.unrealized_pnl(current_price)

    def to_dict(self, current_price: float) -> Dict:
        """转换为字典"""
        return {
            'symbol': self.symbol,
            'quantity': self.quantity,
            'avg_cost': self.avg_cost,
            'unrealized_pnl': self.unrealized_pnl(current_price),
            'realized_pnl': self.realized_pnl,
            'total_pnl': self.total_pnl(current_price),
            'trades': self.trades
        }


class OrderManager:
    """订单管理器"""

    def __init__(self, commission_rate: float = 0.0003):
        """
        初始化订单管理器

        Args:
            commission_rate: 佣金费率（默认0.03%）
        """
        self.orders: Dict[str, Order] = {}
        self.commission_rate = commission_rate

    def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None
    ) -> Order:
        """创建订单"""
        order = Order(symbol, side, order_type, quantity, price, stop_price)
        self.orders[order.order_id] = order
        return order

    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        if order_id not in self.orders:
            return False

        order = self.orders[order_id]
        if order.status in [OrderStatus.FILLED, OrderStatus.CANCELLED]:
            return False

        order.cancel()
        return True

    def get_order(self, order_id: str) -> Optional[Order]:
        """获取订单"""
        return self.orders.get(order_id)

    def get_pending_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """获取待成交订单"""
        orders = [
            order for order in self.orders.values()
            if order.status in [OrderStatus.PENDING, OrderStatus.PARTIAL]
        ]

        if symbol:
            orders = [order for order in orders if order.symbol == symbol]

        return orders

    def calculate_commission(self, quantity: float, price: float) -> float:
        """计算佣金"""
        return quantity * price * self.commission_rate


class PositionTracker:
    """持仓跟踪器"""

    def __init__(self):
        self.positions: Dict[str, Position] = {}

    def update_position(self, symbol: str, quantity: float, price: float):
        """更新持仓"""
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol)

        position = self.positions[symbol]

        if quantity > 0:
            position.add(quantity, price)
        else:
            position.reduce(abs(quantity), price)

        # 如果持仓为0，删除该持仓
        if position.quantity == 0:
            del self.positions[symbol]

    def get_position(self, symbol: str) -> Optional[Position]:
        """获取持仓"""
        return self.positions.get(symbol)

    def get_all_positions(self) -> Dict[str, Position]:
        """获取所有持仓"""
        return self.positions.copy()

    def get_total_value(self, prices: Dict[str, float]) -> float:
        """计算持仓总市值"""
        total = 0.0
        for symbol, position in self.positions.items():
            if symbol in prices:
                total += position.quantity * prices[symbol]
        return total

    def get_total_pnl(self, prices: Dict[str, float]) -> Dict[str, float]:
        """计算总盈亏"""
        total_unrealized = 0.0
        total_realized = 0.0

        for symbol, position in self.positions.items():
            if symbol in prices:
                total_unrealized += position.unrealized_pnl(prices[symbol])
                total_realized += position.realized_pnl

        return {
            'unrealized_pnl': total_unrealized,
            'realized_pnl': total_realized,
            'total_pnl': total_unrealized + total_realized
        }


class PnLCalculator:
    """盈亏计算器"""

    @staticmethod
    def calculate_trade_pnl(
        entry_price: float,
        exit_price: float,
        quantity: float,
        side: OrderSide,
        commission_rate: float = 0.0003
    ) -> Dict[str, float]:
        """
        计算单笔交易盈亏

        Args:
            entry_price: 入场价格
            exit_price: 出场价格
            quantity: 数量
            side: 方向（买入/卖出）
            commission_rate: 佣金费率

        Returns:
            {
                'gross_pnl': 毛盈亏,
                'commission': 佣金,
                'net_pnl': 净盈亏,
                'return_pct': 收益率
            }
        """
        # 计算毛盈亏
        if side == OrderSide.BUY:
            gross_pnl = (exit_price - entry_price) * quantity
        else:
            gross_pnl = (entry_price - exit_price) * quantity

        # 计算佣金
        commission = (entry_price + exit_price) * quantity * commission_rate

        # 计算净盈亏
        net_pnl = gross_pnl - commission

        # 计算收益率
        cost = entry_price * quantity
        return_pct = (net_pnl / cost) * 100 if cost > 0 else 0.0

        return {
            'gross_pnl': float(gross_pnl),
            'commission': float(commission),
            'net_pnl': float(net_pnl),
            'return_pct': float(return_pct)
        }

    @staticmethod
    def calculate_portfolio_metrics(
        positions: Dict[str, Position],
        prices: Dict[str, float],
        initial_capital: float
    ) -> Dict[str, float]:
        """
        计算组合绩效指标

        Args:
            positions: 持仓字典
            prices: 当前价格
            initial_capital: 初始资金

        Returns:
            {
                'total_value': 总市值,
                'total_pnl': 总盈亏,
                'total_return': 总收益率,
                'unrealized_pnl': 未实现盈亏,
                'realized_pnl': 已实现盈亏
            }
        """
        total_value = 0.0
        total_unrealized = 0.0
        total_realized = 0.0

        for symbol, position in positions.items():
            if symbol in prices:
                current_price = prices[symbol]
                total_value += position.quantity * current_price
                total_unrealized += position.unrealized_pnl(current_price)
                total_realized += position.realized_pnl

        total_pnl = total_unrealized + total_realized
        total_return = (total_pnl / initial_capital) * 100 if initial_capital > 0 else 0.0

        return {
            'total_value': float(total_value),
            'total_pnl': float(total_pnl),
            'total_return': float(total_return),
            'unrealized_pnl': float(total_unrealized),
            'realized_pnl': float(total_realized)
        }


class RiskMonitor:
    """风险监控器"""

    def __init__(
        self,
        max_position_size: float = 0.2,  # 单个持仓最大占比
        max_leverage: float = 1.0,       # 最大杠杆
        max_drawdown: float = 0.2        # 最大回撤
    ):
        """
        初始化风险监控器

        Args:
            max_position_size: 单个持仓最大占比（默认20%）
            max_leverage: 最大杠杆（默认1.0，即不使用杠杆）
            max_drawdown: 最大回撤（默认20%）
        """
        self.max_position_size = max_position_size
        self.max_leverage = max_leverage
        self.max_drawdown = max_drawdown
        self.peak_value = 0.0

    def check_position_size(
        self,
        symbol: str,
        quantity: float,
        price: float,
        total_value: float
    ) -> Tuple[bool, str]:
        """
        检查持仓规模是否超限

        Returns:
            (是否通过, 原因)
        """
        position_value = quantity * price
        position_ratio = position_value / total_value if total_value > 0 else 0

        if position_ratio > self.max_position_size:
            return False, f"Position size {position_ratio:.2%} exceeds limit {self.max_position_size:.2%}"

        return True, "OK"

    def check_leverage(
        self,
        total_position_value: float,
        total_capital: float
    ) -> Tuple[bool, str]:
        """
        检查杠杆是否超限

        Returns:
            (是否通过, 原因)
        """
        leverage = total_position_value / total_capital if total_capital > 0 else 0

        if leverage > self.max_leverage:
            return False, f"Leverage {leverage:.2f} exceeds limit {self.max_leverage:.2f}"

        return True, "OK"

    def check_drawdown(self, current_value: float) -> Tuple[bool, str]:
        """
        检查回撤是否超限

        Returns:
            (是否通过, 原因)
        """
        if current_value > self.peak_value:
            self.peak_value = current_value

        if self.peak_value == 0:
            return True, "OK"

        drawdown = (self.peak_value - current_value) / self.peak_value

        if drawdown > self.max_drawdown:
            return False, f"Drawdown {drawdown:.2%} exceeds limit {self.max_drawdown:.2%}"

        return True, "OK"

    def check_all(
        self,
        positions: Dict[str, Position],
        prices: Dict[str, float],
        total_capital: float
    ) -> Dict[str, Any]:
        """
        执行所有风险检查

        Returns:
            {
                'passed': 是否通过所有检查,
                'checks': {
                    'position_size': (bool, str),
                    'leverage': (bool, str),
                    'drawdown': (bool, str)
                }
            }
        """
        # 计算总持仓市值
        total_position_value = sum(
            position.quantity * prices.get(symbol, 0)
            for symbol, position in positions.items()
        )

        # 计算当前总价值
        current_value = total_capital + sum(
            position.total_pnl(prices.get(symbol, 0))
            for symbol, position in positions.items()
        )

        checks = {
            'leverage': self.check_leverage(total_position_value, total_capital),
            'drawdown': self.check_drawdown(current_value)
        }

        # 检查每个持仓的规模
        position_checks = {}
        for symbol, position in positions.items():
            if symbol in prices:
                passed, reason = self.check_position_size(
                    symbol, position.quantity, prices[symbol], current_value
                )
                position_checks[symbol] = (passed, reason)

        checks['position_size'] = position_checks

        # 判断是否通过所有检查
        passed = (
            checks['leverage'][0] and
            checks['drawdown'][0] and
            all(check[0] for check in position_checks.values())
        )

        return {
            'passed': passed,
            'checks': checks
        }


class TradeLogger:
    """交易日志记录器"""

    def __init__(self):
        self.trades: List[Dict] = []

    def log_trade(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float,
        commission: float = 0.0,
        notes: str = ""
    ):
        """记录交易"""
        trade = {
            'timestamp': datetime.now(),
            'symbol': symbol,
            'side': side.value,
            'quantity': quantity,
            'price': price,
            'value': quantity * price,
            'commission': commission,
            'notes': notes
        }
        self.trades.append(trade)

    def get_trades(
        self,
        symbol: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[Dict]:
        """获取交易记录"""
        trades = self.trades.copy()

        if symbol:
            trades = [t for t in trades if t['symbol'] == symbol]

        if start_date:
            trades = [t for t in trades if t['timestamp'] >= start_date]

        if end_date:
            trades = [t for t in trades if t['timestamp'] <= end_date]

        return trades

    def get_trade_summary(self) -> Dict[str, Any]:
        """获取交易汇总"""
        if not self.trades:
            return {
                'total_trades': 0,
                'total_volume': 0.0,
                'total_commission': 0.0,
                'buy_trades': 0,
                'sell_trades': 0
            }

        total_volume = sum(t['value'] for t in self.trades)
        total_commission = sum(t['commission'] for t in self.trades)
        buy_trades = sum(1 for t in self.trades if t['side'] == 'buy')
        sell_trades = sum(1 for t in self.trades if t['side'] == 'sell')

        return {
            'total_trades': len(self.trades),
            'total_volume': float(total_volume),
            'total_commission': float(total_commission),
            'buy_trades': buy_trades,
            'sell_trades': sell_trades
        }


class PaperTradingRepository:
    """模拟交易持久化仓库 — DB-first cache + 进程重启恢复。

    DB 是唯一事实源；内存只做热缓存与恢复。
    """

    def __init__(self):
        self._accounts: Dict[str, Dict] = {}
        self._positions: Dict[str, Dict[str, Position]] = {}  # account_id -> {symbol: Position}
        self._pending_orders: Dict[str, List[Order]] = {}     # account_id -> [Order]
        self._loaded = False

    async def refresh_account_from_db(self, account_id: str):
        """按账户刷新内存缓存，DB 始终优先。"""
        from ..storage import get_db
        db = get_db()

        async with db.acquire() as conn:
            account = await conn.fetchrow("SELECT * FROM paper_accounts WHERE id=$1", account_id)
            if not account:
                self._accounts.pop(account_id, None)
                self._positions.pop(account_id, None)
                self._pending_orders.pop(account_id, None)
                return None

            self._accounts[account_id] = dict(account)
            positions = await conn.fetch(
                "SELECT * FROM paper_positions WHERE account_id=$1",
                account_id,
            )
            self._positions[account_id] = {}
            for row in positions:
                pos = Position(row['stock_code'])
                pos.quantity = float(row.get('quantity') or 0)
                pos.avg_cost = float(row.get('cost_price') or 0)
                self._positions[account_id][row['stock_code']] = pos

            orders = await conn.fetch(
                "SELECT * FROM paper_orders WHERE account_id=$1 AND status IN ('pending','submitted')",
                account_id,
            )
            self._pending_orders[account_id] = []
            for row in orders:
                order = Order(
                    symbol=row.get('code') or row.get('stock_code', ''),
                    side=OrderSide.BUY if row.get('direction') == 'buy' else OrderSide.SELL,
                    order_type=OrderType(row.get('order_type', 'limit')),
                    quantity=float(row.get('shares') or 0),
                    price=float(row.get('price') or 0) if row.get('price') else None,
                    stop_price=float(row.get('stop_price') or 0) if row.get('stop_price') else None,
                )
                order.order_id = str(row['id'])
                order.status = OrderStatus(row.get('status', 'pending'))
                self._pending_orders[account_id].append(order)

        return {
            'account_id': account_id,
            'positions': len(self._positions.get(account_id, {})),
            'pending_orders': len(self._pending_orders.get(account_id, [])),
        }

    async def restore_from_db(self):
        """进程启动时从 DB 恢复所有 active 账户的持仓和 pending 订单到内存"""
        from ..storage import get_db
        db = get_db()

        async with db.acquire() as conn:
            accounts = await conn.fetch("SELECT * FROM paper_accounts")
            account_ids = [str(acct['id']) for acct in accounts]

        for account_id in account_ids:
            await self.refresh_account_from_db(account_id)

        self._loaded = True
        return {
            'accounts': len(self._accounts),
            'positions': sum(len(v) for v in self._positions.values()),
            'pending_orders': sum(len(v) for v in self._pending_orders.values()),
        }

    def get_account(self, account_id: str) -> Optional[Dict]:
        return self._accounts.get(account_id)

    def get_positions(self, account_id: str) -> Dict[str, Position]:
        return self._positions.get(account_id, {})

    def get_pending_orders(self, account_id: str) -> List[Order]:
        return self._pending_orders.get(account_id, [])

    async def save_position_to_db(self, account_id: str, position: Position):
        """持仓落库后再从 DB 回刷缓存，避免内存领先于持久化状态。"""
        from ..storage import get_db
        db = get_db()
        async with db.acquire() as conn:
            if position.quantity > 0:
                await conn.execute(
                    """INSERT INTO paper_positions (account_id, stock_code, stock_name, quantity, cost_price, updated_at)
                       VALUES ($1, $2, $2, $3, $4, CURRENT_TIMESTAMP)
                       ON CONFLICT (account_id, stock_code) DO UPDATE
                       SET quantity=$3, cost_price=$4, updated_at=CURRENT_TIMESTAMP""",
                    account_id, position.symbol, int(position.quantity), position.avg_cost,
                )
            else:
                await conn.execute(
                    "DELETE FROM paper_positions WHERE account_id=$1 AND stock_code=$2",
                    account_id, position.symbol,
                )
        await self.refresh_account_from_db(account_id)

    async def save_order_to_db(self, account_id: str, order: Order):
        """订单状态落库后回刷缓存。"""
        from ..storage import get_db
        db = get_db()
        async with db.acquire() as conn:
            await conn.execute(
                """UPDATE paper_orders SET status=$1, updated_at=CURRENT_TIMESTAMP WHERE id=$2""",
                order.status.value, order.order_id,
            )
        await self.refresh_account_from_db(account_id)

    async def reconcile_from_db(self, account_id: Optional[str] = None):
        """显式从 DB 重新物化缓存，供账本校准后使用。"""
        if account_id:
            result = await self.refresh_account_from_db(account_id)
            self._loaded = True
            return {
                'accounts': 1 if result else 0,
                'positions': len(self._positions.get(account_id, {})),
                'pending_orders': len(self._pending_orders.get(account_id, [])),
            }
        return await self.restore_from_db()

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def status(self) -> Dict:
        return {
            'loaded': self._loaded,
            'accounts': len(self._accounts),
            'positions': sum(len(v) for v in self._positions.values()),
            'pending_orders': sum(len(v) for v in self._pending_orders.values()),
        }


# Singleton
_repository: Optional[PaperTradingRepository] = None


def get_paper_trading_repository() -> PaperTradingRepository:
    global _repository
    if _repository is None:
        _repository = PaperTradingRepository()
    return _repository
