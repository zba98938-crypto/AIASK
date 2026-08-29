from __future__ import annotations

import asyncio
import math

from akshare_mcp.tools.managers.quant_mgr_classic import handle_calculate_factors


def _ok(data, **kwargs):
    payload = {"success": True, "data": data, "error": None}
    payload.update(kwargs)
    return payload


def _fail(message, **kwargs):
    return {"success": False, "error": message, **kwargs}


def _assert_all_finite(value) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _assert_all_finite(item)
    elif isinstance(value, list):
        for item in value:
            _assert_all_finite(item)
    elif isinstance(value, float):
        assert math.isfinite(value)


def test_calculate_factors_ignores_non_finite_financial_fields() -> None:
    class Db:
        async def get_klines(self, *_args, **_kwargs):
            return [
                {"date": f"2026-01-{(idx % 28) + 1:02d}", "close": 10 + idx * 0.1, "volume": 1000, "amount": 10000}
                for idx in range(140)
            ]

        async def get_financials(self, *_args, **_kwargs):
            return [
                {
                    "ps_ratio": "inf",
                    "roe": "nan",
                    "roa": "inf",
                    "gross_margin": "-inf",
                    "debt_ratio": "nan",
                    "revenue_growth": "nan",
                    "profit_growth": "inf",
                }
            ]

        async def get_stock_info(self, *_args, **_kwargs):
            return {"pe_ratio": "nan", "pb_ratio": "inf", "market_cap": "nan"}

    async def scenario():
        return await handle_calculate_factors(
            kw={"factors": ["value", "quality", "growth"]},
            code="600519",
            db=Db(),
            ok=_ok,
            fail=_fail,
        )

    result = asyncio.run(scenario())

    assert result["success"] is True
    assert result["data"]["composite_score"] == 0.0
    _assert_all_finite(result["data"]["factors"])
