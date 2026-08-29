from __future__ import annotations

import asyncio


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return False


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def _register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _register


def test_factor_catalog_includes_latest_ic_from_storage_adapter() -> None:
    from akshare_mcp.services.factor_catalog import build_factor_catalog

    class Conn:
        async def fetchrow(self, *_args):
            return None

    class Db:
        def __init__(self):
            self.calls = []

        async def get_factor_ic_history(self, factor_name: str, period: str, limit: int):
            self.calls.append((factor_name, period, limit))
            if factor_name == "momentum":
                return [
                    {
                        "factor_name": "momentum",
                        "period": "20",
                        "ic_date": "2026-06-04",
                        "ic_value": 0.123,
                        "rank_ic": 0.234,
                        "stock_count": 300,
                    }
                ]
            return []

        def acquire(self):
            return FakeAcquire(Conn())

    db = Db()
    catalog = asyncio.run(build_factor_catalog(include_dsl_metadata=False, db=db))

    assert catalog["momentum"]["ic_history_available"] is True
    assert catalog["momentum"]["latest_ic"] == {
        "factor_name": "momentum",
        "period": "20",
        "ic_date": "2026-06-04",
        "ic_value": 0.123,
        "rank_ic": 0.234,
        "stock_count": 300,
    }
    assert ("momentum", "20", 1) in db.calls


def test_factor_catalog_fallback_query_uses_factor_name_schema() -> None:
    from akshare_mcp.services.factor_catalog import _load_latest_ic_summary

    class Conn:
        def __init__(self):
            self.queries = []

        async def fetchrow(self, query: str, factor_name: str):
            self.queries.append(query)
            assert "factor_name = $1" in query
            assert "WHERE factor =" not in query
            assert factor_name == "momentum"
            return {
                "factor_name": "momentum",
                "period": "10",
                "ic_date": "2026-06-03",
                "ic_value": 0.11,
                "rank_ic": 0.12,
                "stock_count": 120,
            }

    class Db:
        def __init__(self):
            self.conn = Conn()

        def acquire(self):
            return FakeAcquire(self.conn)

    db = Db()
    latest = asyncio.run(_load_latest_ic_summary("momentum", db=db))

    assert latest["factor_name"] == "momentum"
    assert latest["ic_date"] == "2026-06-03"
    assert db.conn.queries


def test_quant_registers_get_factor_catalog_mcp_tool(monkeypatch) -> None:
    from akshare_mcp.services import factor_catalog
    from akshare_mcp.tools import quant

    async def fake_build_factor_catalog(*, include_dsl_metadata: bool = True):
        assert include_dsl_metadata is False
        return {
            "momentum": {
                "name": "momentum",
                "category": "technical",
                "ic_history_available": True,
                "latest_ic": {"ic_date": "2026-06-04", "ic_value": 0.2},
            },
            "pe_ttm": {
                "name": "pe_ttm",
                "category": "fundamental",
                "ic_history_available": False,
                "latest_ic": None,
            },
        }

    monkeypatch.setattr(factor_catalog, "build_factor_catalog", fake_build_factor_catalog)
    monkeypatch.setattr(factor_catalog, "get_dsl_summary", lambda: {"fields": ["close"], "functions": ["zscore"]})

    mcp = FakeMcp()
    quant.register(mcp)

    assert "get_factor_catalog" in mcp.tools
    result = asyncio.run(
        mcp.tools["get_factor_catalog"](
            category="technical",
            include_dsl_metadata="false",
            only_with_ic="true",
        )
    )

    assert result["success"] is True
    data = result["data"]
    assert data["count"] == 1
    assert list(data["catalog"]) == ["momentum"]
    assert data["include_dsl_metadata"] is False
    assert data["dsl"] is None
    assert data["stats"]["ic_history_available_count"] == 1
