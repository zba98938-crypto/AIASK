"""Hard constraints that cannot be overridden by AI judgment.

These are absolute safety boundaries. AI is the "advisor",
hard constraints are the "law".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class HardConstraints:
    """Absolute constraints that no AI judgment can override."""

    # Absolute rejection criteria
    MAX_DRAWDOWN_ABSOLUTE = 0.50
    MIN_BACKTEST_DAYS = 60
    MIN_STOCKS_TESTED = 1

    def check(self, candidate: dict[str, Any], backtest_result: dict[str, Any]) -> dict[str, Any]:
        """Run hard constraint checks before any AI evaluation.

        Returns:
            {"passed": bool, "violations": [...], "checks": {...}}
        """
        violations: list[str] = []
        metrics = dict(backtest_result.get("metrics") or {})

        # 1. Absolute drawdown limit
        max_dd = abs(float(metrics.get("max_drawdown") or 0))
        if max_dd > self.MAX_DRAWDOWN_ABSOLUTE:
            violations.append(f"max_drawdown_{max_dd:.1%}_exceeds_50%")

        # 2. Negative total return
        total_return = float(metrics.get("total_return") or 0)
        if total_return < -0.10:
            violations.append(f"total_return_{total_return:.1%}_severely_negative")

        # 3. Zero trades
        trades = int(metrics.get("trades_count") or metrics.get("trade_count") or 0)
        if trades == 0:
            violations.append("zero_trades")

        # 4. Strategy type must exist
        strategy_type = str(candidate.get("strategy_type") or "").strip()
        if not strategy_type:
            violations.append("missing_strategy_type")

        # 5. Params must be a dict
        params = candidate.get("params")
        if not isinstance(params, dict):
            violations.append("params_not_dict")

        # 6. Must have risk rules
        risk_rules = candidate.get("risk_rules") or (params if isinstance(params, dict) else {}).get("risk_rules")
        if not risk_rules:
            violations.append("missing_risk_rules")

        # 7. Backtest data sufficiency
        curve_points = int(metrics.get("curve_points") or 0)
        if curve_points > 0 and curve_points < self.MIN_BACKTEST_DAYS:
            violations.append(f"insufficient_backtest_data_{curve_points}_days")

        return {
            "passed": len(violations) == 0,
            "violations": violations,
            "checks": {
                "max_drawdown": round(max_dd, 4),
                "total_return": round(total_return, 4),
                "trades_count": trades,
                "strategy_type": strategy_type or None,
                "has_risk_rules": bool(risk_rules),
                "curve_points": curve_points,
            },
        }
