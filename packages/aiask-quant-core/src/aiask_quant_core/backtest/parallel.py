"""并行回测引擎 - Ray支持"""

from typing import List, Dict, Any, Optional
import os
import json
import numpy as np

from .utils import _compute_slippage_rate
from .strategies import (
    _backtest_ma_cross_jit,
    _backtest_momentum_jit,
    _backtest_rsi_jit,
)
from .engine import backtest_engine

# 可选的Ray支持
RAY_AVAILABLE = False
try:
    import ray
    RAY_AVAILABLE = True
except ImportError:
    pass


def _parallel_backtest_task_optimized_impl(
    code: str,
    data_ref,
    strategy: str,
    params_ref
):
    """优化的并行回测任务 - 使用对象引用减少序列化"""
    try:
        processed_data = ray.get(data_ref)
        params = ray.get(params_ref)

        if code not in processed_data:
            return {'code': code, 'success': False, 'error': 'No data for code'}

        data = processed_data[code]
        closes = data['closes']

        initial_capital = params.get('initial_capital', 100000)
        commission = params.get('commission', 0.0003)
        volumes = data.get('volumes')

        slippage_rate = _compute_slippage_rate(closes, volumes, params, 0.0)
        total_cost_rate = commission + slippage_rate

        if strategy == 'ma_cross':
            short_period = params.get('short_period', 5)
            long_period = params.get('long_period', 20)
            final_capital, total_return, max_dd, sharpe, trades, win_rate, equity = _backtest_ma_cross_jit(
                closes, short_period, long_period, initial_capital, total_cost_rate
            )
        elif strategy == 'momentum':
            period = params.get('period', 20)
            threshold = params.get('threshold', 0.02)
            final_capital, total_return, max_dd, sharpe, trades, win_rate, equity = _backtest_momentum_jit(
                closes, period, threshold, initial_capital, total_cost_rate
            )
        elif strategy == 'rsi':
            rsi_period = params.get('rsi_period', 14)
            oversold = params.get('oversold', 30)
            overbought = params.get('overbought', 70)
            final_capital, total_return, max_dd, sharpe, trades, win_rate, equity = _backtest_rsi_jit(
                closes,
                np.asarray(volumes if volumes is not None else np.zeros_like(closes), dtype=np.float64),
                rsi_period,
                oversold,
                overbought,
                0,
                0,
                6,
                6.0,
                -0.05,
                0.015,
                0,
                6,
                0.008,
                24.0,
                0,
                8,
                0.8,
                0,
                4,
                0.55,
                0.0015,
                0,
                -0.002,
                0,
                0,
                6.0,
                initial_capital,
                total_cost_rate,
            )
        else:
            return {'code': code, 'success': False, 'error': f'Unknown strategy: {strategy}'}

        return {
            'code': code, 'strategy': strategy,
            'initial_capital': float(initial_capital),
            'final_capital': float(final_capital),
            'total_return': float(total_return),
            'total_return_pct': f"{total_return*100:.2f}%",
            'max_drawdown': float(max_dd),
            'max_drawdown_pct': f"{max_dd*100:.2f}%",
            'sharpe_ratio': float(sharpe),
            'trades_count': int(trades),
            'win_rate': float(win_rate),
            'win_rate_pct': f"{win_rate*100:.2f}%",
            'success': True
        }
    except Exception as e:
        return {'code': code, 'success': False, 'error': str(e)}


# Ray并行回测支持
if RAY_AVAILABLE:
    _parallel_backtest_task_optimized = ray.remote(_parallel_backtest_task_optimized_impl)

    @ray.remote
    def _parallel_backtest_task(code, klines, strategy, params):
        """Ray远程任务"""
        return backtest_engine.run_backtest(code, klines, strategy, params)

    class ParallelBacktestEngine:
        """并行回测引擎（使用Ray）- 性能优化版"""

        @staticmethod
        def batch_backtest(
            codes: List[str],
            klines_dict: Dict[str, List[Dict[str, Any]]],
            strategy: str = 'ma_cross',
            params: Optional[Dict[str, Any]] = None
        ) -> Dict[str, Any]:
            """批量并行回测 - 优化版"""
            if not ray.is_initialized():
                ray.init(
                    ignore_reinit_error=True,
                    num_cpus=os.cpu_count(),
                    object_store_memory=2 * 1024 * 1024 * 1024,
                    _system_config={
                        "max_io_workers": 4,
                        "object_spilling_config": json.dumps({
                            "type": "filesystem",
                            "params": {"directory_path": "/tmp/ray_spill"}
                        })
                    }
                )

            params = params or {}

            processed_data = {}
            for code in codes:
                if code in klines_dict and klines_dict[code]:
                    klines = klines_dict[code]
                    processed_data[code] = {
                        'closes': np.array([k['close'] for k in klines]),
                        'volumes': np.array([k['volume'] for k in klines]),
                        'highs': np.array([k['high'] for k in klines]),
                        'lows': np.array([k['low'] for k in klines]),
                    }

            data_ref = ray.put(processed_data)
            params_ref = ray.put(params)

            futures = [
                _parallel_backtest_task_optimized.remote(code, data_ref, strategy, params_ref)
                for code in codes if code in processed_data
            ]

            results = []
            remaining = futures
            while remaining:
                ready, remaining = ray.wait(remaining, num_returns=min(10, len(remaining)), timeout=30)
                if ready:
                    batch_results = ray.get(ready)
                    results.extend(batch_results)

            return {
                'success': True,
                'data': {'results': results, 'count': len(results)}
            }

        @staticmethod
        def batch_backtest_sequential(
            codes: List[str],
            klines_dict: Dict[str, List[Dict[str, Any]]],
            strategy: str = 'ma_cross',
            params: Optional[Dict[str, Any]] = None
        ) -> Dict[str, Any]:
            """批量顺序回测（不使用Ray）- 作为对比"""
            params = params or {}
            results = []
            for code in codes:
                if code in klines_dict:
                    result = backtest_engine.run_backtest(code, klines_dict[code], strategy, params)
                    if result['success']:
                        results.append(result['data'])

            return {
                'success': True,
                'data': {'results': results, 'count': len(results)}
            }
else:
    _parallel_backtest_task_optimized = _parallel_backtest_task_optimized_impl
