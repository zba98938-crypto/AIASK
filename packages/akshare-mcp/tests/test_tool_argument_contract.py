import asyncio

from akshare_mcp.tools.market import kline as kline_module
from akshare_mcp.tools.market import quote as quote_module
from akshare_mcp.tools.news import notices as notices_module
from akshare_mcp.tools.news import research as research_module


def test_get_stock_research_returns_argument_contract_for_symbol_alias(monkeypatch):
    monkeypatch.setattr(research_module.data_source, "get_tushare_pro", lambda: None)
    monkeypatch.setattr(research_module, "ak", None)
    monkeypatch.setattr(
        research_module,
        "_fetch_eastmoney_research",
        lambda code, limit: [{"title": "研报A", "institution": "机构A", "date": "2026-04-01"}],
    )

    result = research_module.get_stock_research(symbol="600519", limit=5)

    assert result["success"] is True
    contract = result["meta"]["argument_contract"]
    assert contract["canonical_tool"] == "get_stock_research"
    assert contract["canonical_args"] == {"code": "600519", "limit": 5}
    assert contract["alias_hits"] == [{"canonical": "code", "matched": "symbol", "deprecated": True}]


def test_get_stock_notices_returns_argument_contract_for_symbol_alias(monkeypatch):
    monkeypatch.setattr(
        notices_module,
        "_try_tushare_anns",
        lambda start_date, end_date, code_filter, max_items: [
            {
                "code": code_filter,
                "title": "公告A",
                "type": "全部",
                "date": "2026-04-01",
                "url": "https://example.com/a",
            }
        ],
    )

    result = notices_module.get_stock_notices(
        start_date="2026-04-01",
        end_date="2026-04-02",
        symbol="600000",
    )

    assert result["success"] is True
    contract = result["meta"]["argument_contract"]
    assert contract["canonical_tool"] == "get_stock_notices"
    assert contract["canonical_args"]["code"] == "600000"
    assert contract["canonical_args"]["start_date"] == "2026-04-01"
    assert contract["canonical_args"]["end_date"] == "2026-04-02"
    assert contract["alias_hits"] == [{"canonical": "code", "matched": "symbol", "deprecated": True}]


def test_get_realtime_quote_returns_argument_contract_for_symbol_alias(monkeypatch):
    monkeypatch.setattr(
        quote_module,
        "resolve_existing_security_code_sync",
        lambda code=None, **kwargs: ("000001", {"name": "平安银行"}, None),
    )
    monkeypatch.setattr(
        quote_module,
        "get_quote_snapshot_sync",
        lambda code: {"code": code, "name": "平安银行", "price": 10.0, "preClose": 9.8},
    )
    monkeypatch.setattr(
        quote_module,
        "get_quote_snapshot_sync",
        lambda code, **_kwargs: {
            "success": True,
            "data": {"code": code, "name": "bank", "price": 10.0, "preClose": 9.8, "source": "db.stock_quotes"},
            "source_chain": ["db.stock_quotes"],
            "backend_requested": "db.stock_quotes",
            "backend_used": "db.stock_quotes",
            "fallback_used": False,
            "fallback_reason": None,
        },
    )
    monkeypatch.setattr(quote_module, "validate_quote", lambda payload: payload)

    result = quote_module.get_realtime_quote(symbol="000001")

    assert result["success"] is True
    contract = result["meta"]["argument_contract"]
    assert contract["canonical_tool"] == "get_realtime_quote"
    assert contract["canonical_args"] == {"code": "000001"}
    assert contract["alias_hits"] == [{"canonical": "code", "matched": "symbol", "deprecated": True}]


def test_get_kline_data_returns_argument_contract_for_stock_code_alias(monkeypatch):
    async def _fake_get_kline(code: str, period: str, limit: int):
        return {
            "success": True,
            "data": [
                {
                    "date": "2026-04-01",
                    "open": 10.0,
                    "close": 10.2,
                    "high": 10.3,
                    "low": 9.9,
                    "volume": 1000,
                }
            ],
        }

    monkeypatch.setattr(kline_module, "get_kline", _fake_get_kline)

    result = asyncio.run(kline_module.get_kline_data(stock_code="000002", period="daily", limit=3))

    assert result["success"] is True
    contract = result["meta"]["argument_contract"]
    assert contract["canonical_tool"] == "get_kline_data"
    assert contract["canonical_args"]["code"] == "000002"
    assert contract["canonical_args"]["period"] == "daily"
    assert contract["canonical_args"]["limit"] == 3
    assert contract["alias_hits"] == [{"canonical": "code", "matched": "stock_code", "deprecated": True}]
