"""高级回测引擎 - 动态止损、仓位管理、多策略组合"""

from typing import List, Dict, Any, Optional
import numpy as np

from .utils import _compute_slippage_rate
from .engine import _build_strategy_masks, backtest_engine


class AdvancedBacktestEngine:
    """高级回测引擎 - 动态止损、仓位管理、多策略组合"""

    @staticmethod
    def backtest_with_dynamic_stops(
        code: str,
        klines: List[Dict[str, Any]],
        strategy: str = 'ma_cross',
        params: Optional[Dict[str, Any]] = None,
        stop_loss: float = 0.05,
        take_profit: float = 0.10,
        trailing_stop: float = 0.03
    ) -> Dict[str, Any]:
        """带动态止损的回测"""
        if not klines:
            return {'success': False, 'error': 'No kline data'}

        params = params or {}
        initial_capital = params.get('initial_capital', 100000)
        commission = params.get('commission', 0.0003)

        closes = np.array([k['close'] for k in klines])
        volumes = np.array([k.get('volume', 0.0) for k in klines])

        slippage_rate = _compute_slippage_rate(closes, volumes, params, 0.0)
        total_cost_rate = commission + slippage_rate

        base_result = backtest_engine.run_backtest(code, klines, strategy, params)
        if not base_result['success']:
            return base_result

        cash = initial_capital
        shares = 0
        buy_price = 0.0
        highest_price = 0.0
        trades = 0
        wins = 0
        equity = []

        for close in closes:
            if shares > 0:
                if close > highest_price:
                    highest_price = close
                if close <= buy_price * (1 - stop_loss):
                    sell_price = close * (1 - total_cost_rate)
                    cash += shares * sell_price
                    if sell_price > buy_price:
                        wins += 1
                    shares = 0
                    trades += 1
                elif close >= buy_price * (1 + take_profit):
                    sell_price = close * (1 - total_cost_rate)
                    cash += shares * sell_price
                    wins += 1
                    shares = 0
                    trades += 1
                elif close <= highest_price * (1 - trailing_stop):
                    sell_price = close * (1 - total_cost_rate)
                    cash += shares * sell_price
                    if sell_price > buy_price:
                        wins += 1
                    shares = 0
                    trades += 1
            equity.append(cash + shares * close)

        if shares > 0:
            cash += shares * closes[-1] * (1 - total_cost_rate)
            shares = 0

        final_capital = cash
        total_return = (final_capital - initial_capital) / initial_capital

        equity = np.array(equity)
        peak = np.maximum.accumulate(equity)
        drawdown = (peak - equity) / peak
        max_dd = float(np.max(drawdown))

        return {
            'success': True,
            'data': {
                'code': code,
                'strategy': f'{strategy}_dynamic_stops',
                'initial_capital': initial_capital,
                'final_capital': float(final_capital),
                'total_return': float(total_return),
                'max_drawdown': max_dd,
                'trades_count': trades,
                'win_rate': wins / trades if trades > 0 else 0.0,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'trailing_stop': trailing_stop,
            }
        }

    @staticmethod
    def backtest_with_position_sizing(
        code: str,
        klines: List[Dict[str, Any]],
        strategy: str = 'ma_cross',
        params: Optional[Dict[str, Any]] = None,
        sizing_method: str = 'fixed',
        risk_per_trade: float = 0.02
    ) -> Dict[str, Any]:
        """带仓位管理的回测。"""
        if not klines:
            return {'success': False, 'error': 'No kline data'}

        params = params or {}
        initial_capital = float(params.get('initial_capital', 100000))
        commission = float(params.get('commission', 0.0003) or 0.0)
        closes = np.array([float(k['close']) for k in klines], dtype=np.float64)
        volumes = np.array([float(k.get('volume', 0.0) or 0.0) for k in klines], dtype=np.float64)
        masks = _build_strategy_masks(strategy, closes, params, volumes=volumes, klines=klines)
        if masks is None:
            return {'success': False, 'error': 'Insufficient data for strategy signals'}

        def _position_size(idx: int, realized_returns: list[float]) -> float:
            if sizing_method == 'fixed':
                return 1.0
            if sizing_method == 'kelly':
                if not realized_returns:
                    return 0.5
                wins = [ret for ret in realized_returns if ret > 0]
                losses = [abs(ret) for ret in realized_returns if ret <= 0]
                win_rate = len(wins) / len(realized_returns)
                avg_win = float(np.mean(wins)) if wins else 0.05
                avg_loss = float(np.mean(losses)) if losses else 0.03
                edge = win_rate - ((1 - win_rate) / max(avg_win / max(avg_loss, 1e-6), 1e-6))
                return float(np.clip(edge, 0.1, 1.0))
            if sizing_method == 'volatility':
                start = max(1, idx - 20)
                window = np.diff(closes[start:idx + 1]) / np.maximum(closes[start:idx], 1e-12)
                vol = float(np.std(window)) if len(window) > 1 else 0.0
                return float(np.clip(risk_per_trade / max(vol, 1e-4), 0.1, 1.0))
            return 1.0

        entry_mask, exit_mask = masks
        cash = initial_capital
        shares = 0
        buy_cost = 0.0
        equity = np.zeros(len(closes), dtype=np.float64)
        trades = 0
        wins = 0
        realized_returns: list[float] = []
        position_sizes: list[float] = []
        last_size = 0.0

        for i in range(len(closes)):
            price = float(closes[i])
            if entry_mask[i] and shares == 0 and price > 0:
                last_size = _position_size(i, realized_returns)
                capital_to_use = cash * last_size
                trade_price = price * (1 + commission)
                shares = int(capital_to_use / trade_price) if trade_price > 0 else 0
                if shares > 0:
                    cash -= shares * trade_price
                    buy_cost = trade_price
                    position_sizes.append(last_size)
                    trades += 1
            elif exit_mask[i] and shares > 0:
                sell_price = price * (1 - commission)
                cash += shares * sell_price
                realized = (sell_price - buy_cost) / buy_cost if buy_cost > 0 else 0.0
                realized_returns.append(float(realized))
                if realized > 0:
                    wins += 1
                shares = 0
            equity[i] = cash + shares * price

        if shares > 0:
            sell_price = float(closes[-1]) * (1 - commission)
            cash += shares * sell_price
            realized = (sell_price - buy_cost) / buy_cost if buy_cost > 0 else 0.0
            realized_returns.append(float(realized))
            if realized > 0:
                wins += 1
            shares = 0
            equity[-1] = cash

        final_capital = float(cash)
        total_return = (final_capital - initial_capital) / initial_capital if initial_capital > 0 else 0.0
        peak = np.maximum.accumulate(equity if equity.size else np.array([initial_capital], dtype=np.float64))
        drawdown = (peak - equity) / np.maximum(peak, 1e-12) if equity.size else np.array([0.0])
        position_size = float(np.mean(position_sizes)) if position_sizes else 0.0

        return {
            'success': True,
            'data': {
                'code': code,
                'strategy': f'{strategy}_position_sizing',
                'initial_capital': initial_capital,
                'final_capital': final_capital,
                'total_return': float(total_return),
                'max_drawdown': float(np.max(drawdown)) if len(drawdown) else 0.0,
                'trades_count': int(max(len(realized_returns), trades)),
                'win_rate': wins / len(realized_returns) if realized_returns else 0.0,
                'sizing_method': sizing_method,
                'position_size': float(position_size),
                'adjusted_capital': final_capital,
                'adjusted_return': float(total_return),
            }
        }

    @staticmethod
    def multi_strategy_backtest(
        code: str,
        klines: List[Dict[str, Any]],
        strategies: List[Dict[str, Any]],
        allocation: Optional[List[float]] = None
    ) -> Dict[str, Any]:
        """多策略组合回测"""
        if not klines or not strategies:
            return {'success': False, 'error': 'Invalid input'}

        n_strategies = len(strategies)
        if allocation is None:
            allocation = [1.0 / n_strategies] * n_strategies
        if len(allocation) != n_strategies:
            return {'success': False, 'error': 'Allocation length mismatch'}

        results = []
        total_return = 0.0

        for i, strategy_config in enumerate(strategies):
            strategy_name = strategy_config['name']
            strategy_params = strategy_config.get('params', {})
            result = backtest_engine.run_backtest(code, klines, strategy_name, strategy_params)

            if result['success']:
                strategy_return = result['data']['total_return']
                weighted_return = strategy_return * allocation[i]
                total_return += weighted_return
                results.append({
                    'strategy': strategy_name,
                    'allocation': allocation[i],
                    'return': strategy_return,
                    'weighted_return': weighted_return,
                })

        return {
            'success': True,
            'data': {
                'code': code,
                'strategies': results,
                'total_return': float(total_return),
                'n_strategies': n_strategies,
            }
        }


advanced_backtest_engine = AdvancedBacktestEngine()
