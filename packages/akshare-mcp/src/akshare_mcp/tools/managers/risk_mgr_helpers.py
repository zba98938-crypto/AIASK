"""Pure helper / utility functions for the risk manager.

These helpers have **no dependency** on the MCP object or on any async
database calls.  They handle input parsing, value normalisation,
classification, and empty-payload templates.
"""

from __future__ import annotations

import json
from typing import Any

from ...utils import normalize_code


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def _normalize_kwargs(kwargs: dict) -> dict:
    """Merge kwargs payload when passed as kwargs='{"k": "v"}' or kwargs={...}."""
    params = kwargs.get("params")
    if isinstance(params, dict):
        kwargs = {**kwargs, **params}
    raw = kwargs.get("kwargs")
    if isinstance(raw, dict):
        kwargs = {**kwargs, **raw}
    elif isinstance(raw, str):
        try:
            extra = json.loads(raw or "{}")
            if isinstance(extra, dict):
                kwargs = {**kwargs, **extra}
        except Exception:
            pass
    if "code" not in kwargs or not kwargs.get("code"):
        kwargs["code"] = kwargs.get("stock_code") or kwargs.get("symbol") or kwargs.get("ticker")
    if "codes" not in kwargs or not kwargs.get("codes"):
        kwargs["codes"] = (
            kwargs.get("stock_codes")
            or kwargs.get("symbols")
            or kwargs.get("target_symbols")
            or kwargs.get("targets")
        )
    return kwargs


def _safe_portfolio_id(value: Any) -> int | None | Any:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _parse_list_param(value: Any) -> list:
    """Normalize list-like inputs: list / json-string / comma-string."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [item.strip() for item in text.split(",") if item.strip()]
    return [value]


def _parse_dict_param(value: Any) -> dict:
    """Normalize dict-like inputs: dict / json-string."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _parse_codes_weights(kwargs: dict) -> tuple[list[str], list[float], str | None]:
    """Parse codes/weights and normalize weights to sum to 1."""
    raw_codes = _parse_list_param(
        kwargs.get("codes")
        or kwargs.get("stock_codes")
        or kwargs.get("symbols")
        or kwargs.get("target_symbols")
    )
    if not raw_codes:
        single_code = kwargs.get("code") or kwargs.get("stock_code") or kwargs.get("symbol") or kwargs.get("ticker")
        if single_code:
            raw_codes = [single_code]
    codes = [normalize_code(str(c)) for c in raw_codes if str(c).strip()]
    if not codes:
        return [], [], None

    raw_weights = _parse_list_param(kwargs.get("weights"))
    if not raw_weights:
        return codes, [1.0 / len(codes)] * len(codes), None

    try:
        weights = [float(w) for w in raw_weights]
    except Exception:
        return [], [], "weights parse failed; numeric list required"

    if len(weights) != len(codes):
        return [], [], "codes and weights length mismatch"
    if any(w < 0 for w in weights):
        return [], [], "weights cannot be negative"

    total = float(sum(weights))
    if total <= 0:
        return [], [], "weights sum must be > 0"

    weights = [w / total for w in weights]
    return codes, weights, None


# ---------------------------------------------------------------------------
# Value tools
# ---------------------------------------------------------------------------

def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_float(keys: list[str], *sources: dict | None, positive_only: bool = False) -> float | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key not in source:
                continue
            value = _safe_float(source.get(key), None)
            if value is None:
                continue
            if positive_only and value <= 0:
                continue
            return float(value)
    return None


def _format_pct(ratio: float) -> str:
    return f"{ratio * 100:.2f}%"


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def _classify_size_bucket(market_cap: float | None) -> str:
    # CNY market-cap buckets: large >= 200B, mid >= 50B, else small.
    if market_cap is None or market_cap <= 0:
        return "unknown"
    if market_cap >= 2e11:
        return "large"
    if market_cap >= 5e10:
        return "mid"
    return "small"


def _liquidity_level(days_to_exit: float | None) -> str:
    if days_to_exit is None:
        return "unknown"
    if days_to_exit > 5:
        return "high"
    if days_to_exit > 2:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Payload templates (empty portfolios)
# ---------------------------------------------------------------------------

def _empty_var_payload(portfolio_id: Any, input_mode: str, confidence: float, method: str) -> dict:
    """Return a stable, parseable zero-risk payload for empty portfolios."""
    return {
        "portfolio_id": portfolio_id,
        "input_mode": input_mode,
        "method": method,
        "confidence": confidence,
        "total_value": 0.0,
        "constituents": [],
        "empty_portfolio": True,
        "var": {
            "percentage": 0.0,
            "amount": 0.0,
            "description": "empty portfolio",
        },
        "cvar": {
            "percentage": 0.0,
            "amount": 0.0,
            "description": "empty portfolio",
        },
        "volatility": 0.0,
        "max_drawdown": 0.0,
        "message": "empty portfolio, add holdings first",
        "quick_start": {
            "step1": 'portfolio_manager(action="add_holding", portfolio_id="xxx", code="600519", shares=100)',
            "step2": 'risk_manager(action="calculate_var", portfolio_id="xxx")',
        },
    }


def _empty_stress_payload(portfolio_id: Any, input_mode: str, scenario_name: str, description: str) -> dict:
    return {
        "portfolio_id": portfolio_id,
        "input_mode": input_mode,
        "scenario": scenario_name,
        "description": description,
        "current_value": 0.0,
        "stressed_value": 0.0,
        "loss": 0.0,
        "loss_percentage": "0.00%",
        "severity": "low",
        "recommendation": "add holdings first",
        "empty_portfolio": True,
        "message": "empty portfolio, add holdings first",
        "scenario_results": [],
        "summary": {
            "count": 0,
            "worst_scenario": scenario_name,
            "worst_loss": 0.0,
            "worst_loss_pct": "0.00%",
        },
        "quick_start": {
            "step1": 'portfolio_manager(action="add_holding", portfolio_id="xxx", code="600519", shares=100)',
            "step2": 'risk_manager(action="stress_test", portfolio_id="xxx", scenario="market_crash")',
        },
    }
