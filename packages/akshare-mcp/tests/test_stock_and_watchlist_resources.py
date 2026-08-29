from __future__ import annotations

import asyncio

from akshare_mcp.resources import stock_and_watchlist


def test_stock_profile_resource_omits_non_finite_numbers(monkeypatch) -> None:
    class Db:
        async def get_stock_info(self, _code):
            return {
                "code": "600519",
                "name": "Demo",
                "industry": "Test",
                "market_cap": float("nan"),
                "pe_ratio": "inf",
                "pb_ratio": "-inf",
            }

    async def fake_quote(_code, **_kwargs):
        return {
            "success": True,
            "data": {
                "price": "nan",
                "pct_chg": "inf",
                "volume": "-inf",
                "amount": 123.45,
                "timestamp": "2026-06-09T10:00:00+08:00",
            },
        }

    async def fake_profile(*_args, **_kwargs):
        return {"metadata": {"summary_text": "ok", "feature_coverage": [], "raw_features": {}}}

    monkeypatch.setattr(stock_and_watchlist, "get_db", lambda: Db())
    monkeypatch.setattr(stock_and_watchlist, "get_quote_snapshot_response", fake_quote)
    monkeypatch.setattr(stock_and_watchlist, "build_stock_profile_payload", fake_profile)

    payload = asyncio.run(stock_and_watchlist.build_stock_profile_resource_payload("600519"))

    assert payload["stock"]["market_cap"] is None
    assert payload["stock"]["pe_ratio"] is None
    assert payload["stock"]["pb_ratio"] is None
    assert payload["realtime_quote"]["price"] is None
    assert payload["realtime_quote"]["change_pct"] is None
    assert payload["realtime_quote"]["volume"] is None
    assert payload["realtime_quote"]["amount"] == 123.45
