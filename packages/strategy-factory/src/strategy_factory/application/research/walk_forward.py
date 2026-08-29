"""PR-B2: Walk-Forward 验证框架。

将回测数据分成 N 个滚动窗口，每个窗口内分 train/val/test 三段。
只有 test 段的表现才算数，用于计算 Walk-Forward Efficiency (WFE)。

WFE = 样本外 Sharpe 均值 / 样本内 Sharpe 均值
WFE ≥ 0.5 被行业视为可接受（来源：tradingstrategy.ai）。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_MIN_KLINES_FOR_WF = 252  # 至少 1 年数据
_DEFAULT_N_WINDOWS = 5
_DEFAULT_TRAIN_RATIO = 0.6
_DEFAULT_VAL_RATIO = 0.2


class WalkForwardValidator:
    """5-fold rolling Walk-Forward 验证器。"""

    def __init__(self, backtest_engine: Any = None):
        self._engine = backtest_engine

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        from aiask_quant_core.backtest import backtest_engine

        self._engine = backtest_engine
        return self._engine

    def validate(
        self,
        code: str,
        klines: list[dict[str, Any]],
        strategy_type: str,
        params: dict[str, Any],
        *,
        n_windows: int = _DEFAULT_N_WINDOWS,
        train_ratio: float = _DEFAULT_TRAIN_RATIO,
        val_ratio: float = _DEFAULT_VAL_RATIO,
    ) -> dict[str, Any]:
        """执行 Walk-Forward 验证。

        Returns:
            dict with walk_forward_efficiency, out_of_sample metrics, and passed flag.
        """
        n = len(klines)
        if n < _MIN_KLINES_FOR_WF:
            return {
                "passed": False,
                "reason": f"insufficient_klines ({n} < {_MIN_KLINES_FOR_WF})",
                "walk_forward_efficiency": 0.0,
                "out_of_sample_sharpe": 0.0,
                "out_of_sample_win_rate": 0.0,
                "out_of_sample_profit_factor": 0.0,
                "n_windows": 0,
            }

        engine = self._get_engine()
        window_size = n // n_windows
        if window_size < 60:
            return {
                "passed": False,
                "reason": f"window_too_small ({window_size} bars per window)",
                "walk_forward_efficiency": 0.0,
                "out_of_sample_sharpe": 0.0,
                "out_of_sample_win_rate": 0.0,
                "out_of_sample_profit_factor": 0.0,
                "n_windows": 0,
            }

        in_sample_sharpes: list[float] = []
        out_sample_sharpes: list[float] = []
        out_sample_win_rates: list[float] = []
        out_sample_profit_factors: list[float] = []
        out_sample_returns: list[float] = []
        out_sample_max_drawdowns: list[float] = []
        successful_windows = 0

        for i in range(n_windows):
            start = i * window_size
            end = min(start + window_size, n)
            if end - start < 60:
                continue

            train_end = start + int((end - start) * train_ratio)
            val_end = train_end + int((end - start) * val_ratio)

            train_klines = klines[start:train_end]
            test_klines = klines[val_end:end]

            if len(train_klines) < 30 or len(test_klines) < 20:
                continue

            # 样本内回测
            try:
                r_in = engine.run_backtest(code, train_klines, strategy_type, params)
                if not r_in.get("success"):
                    continue
                in_data = r_in["data"]
                in_sharpe = float(in_data.get("sharpe_ratio") or 0.0)
            except Exception as exc:
                logger.debug("WalkForward window %d in-sample failed: %s", i, exc)
                continue

            # 样本外回测（同参数，不调参）
            try:
                r_out = engine.run_backtest(code, test_klines, strategy_type, params)
                if not r_out.get("success"):
                    continue
                out_data = r_out["data"]
                out_sharpe = float(out_data.get("sharpe_ratio") or 0.0)
                out_wr = float(out_data.get("win_rate") or 0.0)
                out_pf = float(out_data.get("profit_factor") or 0.0)
                out_ret = float(out_data.get("total_return") or 0.0)
                out_mdd = float(out_data.get("max_drawdown") or 0.0)
            except Exception as exc:
                logger.debug("WalkForward window %d out-of-sample failed: %s", i, exc)
                continue

            in_sample_sharpes.append(in_sharpe)
            out_sample_sharpes.append(out_sharpe)
            out_sample_win_rates.append(out_wr)
            out_sample_profit_factors.append(out_pf)
            out_sample_returns.append(out_ret)
            out_sample_max_drawdowns.append(out_mdd)
            successful_windows += 1

        if successful_windows < 3:
            return {
                "passed": False,
                "reason": f"insufficient_successful_windows ({successful_windows} < 3)",
                "walk_forward_efficiency": 0.0,
                "out_of_sample_sharpe": 0.0,
                "out_of_sample_win_rate": 0.0,
                "out_of_sample_profit_factor": 0.0,
                "n_windows": successful_windows,
            }

        avg_in_sharpe = float(np.mean(in_sample_sharpes))
        avg_out_sharpe = float(np.mean(out_sample_sharpes))
        avg_out_wr = float(np.mean(out_sample_win_rates))
        avg_out_pf = float(np.mean(out_sample_profit_factors))
        avg_out_ret = float(np.mean(out_sample_returns))
        avg_out_mdd = float(np.mean(out_sample_max_drawdowns))

        # Walk-Forward Efficiency
        wfe = avg_out_sharpe / max(avg_in_sharpe, 0.01) if avg_in_sharpe > 0 else 0.0

        passed = (
            wfe >= 0.5
            and avg_out_sharpe >= 0.5
            and avg_out_pf >= 1.5
        )

        return {
            "passed": passed,
            "walk_forward_efficiency": round(wfe, 4),
            "out_of_sample_sharpe": round(avg_out_sharpe, 4),
            "out_of_sample_win_rate": round(avg_out_wr, 4),
            "out_of_sample_profit_factor": round(avg_out_pf, 4),
            "out_of_sample_return": round(avg_out_ret, 4),
            "out_of_sample_max_drawdown": round(avg_out_mdd, 4),
            "in_sample_sharpe": round(avg_in_sharpe, 4),
            "n_windows": successful_windows,
            "window_sharpes": [round(s, 4) for s in out_sample_sharpes],
        }


__all__ = ["WalkForwardValidator"]
