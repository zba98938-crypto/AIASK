"""Risk manager tools: VaR, stress test, and exposure analysis."""

from __future__ import annotations

import time
from typing import Any

from ...storage import get_db
from ..manager_protocol import fail_with_meta, normalize_manager_payload, ok_with_meta
from . import _risk_manager_exposure as _risk_manager_exposure_mod
from . import _risk_manager_stress as _risk_manager_stress_mod
from . import _risk_manager_var as _risk_manager_var_mod
from ._risk_manager_support import (
    _get_financials_with_fallback,
    _get_klines_with_fallback,
    _get_stock_info_with_fallback,
)


def _sync_risk_support_overrides() -> None:
    """Keep handler modules aligned with risk_manager monkeypatches."""
    _risk_manager_exposure_mod._get_financials_with_fallback = _get_financials_with_fallback
    _risk_manager_exposure_mod._get_klines_with_fallback = _get_klines_with_fallback
    _risk_manager_exposure_mod._get_stock_info_with_fallback = _get_stock_info_with_fallback
    _risk_manager_stress_mod._get_klines_with_fallback = _get_klines_with_fallback
    _risk_manager_stress_mod._get_stock_info_with_fallback = _get_stock_info_with_fallback
    _risk_manager_var_mod._get_klines_with_fallback = _get_klines_with_fallback


def register_risk_manager(mcp):
    """Register risk manager tool."""

    @mcp.tool()
    async def risk_manager(
        action: str,
        params: dict | None = None,
        kwargs: Any = None,
        portfolio_id: str | int | None = None,
        codes: list[str] | None = None,
        weights: list[float] | None = None,
        scenario: str | None = None,
        scenarios: list[str] | None = None,
        confidence: float | None = None,
        method: str | None = None,
        lookback_days: int | None = None,
        portfolio_value: float | None = None,
    ) -> dict:
        """
        Risk manager with unified action + kwargs protocol.

        Actions:
        - help
        - list
        - calculate_var
        - stress_test
        - risk_exposure
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "portfolio_id": portfolio_id,
                    "codes": codes,
                    "weights": weights,
                    "scenario": scenario,
                    "scenarios": scenarios,
                    "confidence": confidence,
                    "method": method,
                    "lookback_days": lookback_days,
                    "portfolio_value": portfolio_value,
                },
            )

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name="risk_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name="risk_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            if action == "help":
                return _ok(
                    {
                        "supported_actions": {
                            "list": "list available actions and parameter hints",
                            "calculate_var": "calculate VaR/CVaR using portfolio_id or codes+weights",
                            "stress_test": "run scenario stress tests using portfolio_id or codes+weights",
                            "risk_exposure": "exposure and concentration analysis using portfolio_id or codes+weights",
                            "help": "show help information",
                        }
                    },
                    source_chain=["risk_manager"],
                )

            if action == "list":
                return _ok(
                    {
                        "actions": [
                            {
                                "action": "calculate_var",
                                "description": "calculate portfolio VaR/CVaR",
                                "kwargs": "portfolio_id or codes+weights, confidence(0.95), method(historical|parametric|monte_carlo), lookback_days(252)",
                            },
                            {
                                "action": "stress_test",
                                "description": "run scenario stress tests",
                                "kwargs": "portfolio_id or codes+weights, scenario/scenarios, portfolio_value(1000000)",
                            },
                            {
                                "action": "risk_exposure",
                                "description": "portfolio exposure and concentration",
                                "kwargs": "portfolio_id or codes+weights, portfolio_value(1000000)",
                            },
                        ],
                        "count": 3,
                    },
                    source_chain=["risk_manager"],
                )

            if action == "calculate_var":
                _sync_risk_support_overrides()
                return await _risk_manager_var_mod._handle_calculate_var(
                    db=db,
                    kwargs=kwargs,
                    ok=_ok,
                    fail=_fail,
                )

            if action == "stress_test":
                _sync_risk_support_overrides()
                return await _risk_manager_stress_mod._handle_stress_test(
                    db=db,
                    kwargs=kwargs,
                    ok=_ok,
                    fail=_fail,
                )

            if action == "risk_exposure":
                _sync_risk_support_overrides()
                return await _risk_manager_exposure_mod._handle_risk_exposure(
                    db=db,
                    kwargs=kwargs,
                    ok=_ok,
                    fail=_fail,
                )

            return _fail(
                f"Unknown action: {action}. Supported: help, list, calculate_var, stress_test, risk_exposure",
                source_chain=["risk_manager"],
            )
        except Exception as exc:
            message = str(exc).strip() or f"{action} 执行失败"
            return fail_with_meta(
                message,
                tool_name="risk_manager",
                action=action,
                started_at=start_time,
                source_chain=["risk_manager"],
            )
