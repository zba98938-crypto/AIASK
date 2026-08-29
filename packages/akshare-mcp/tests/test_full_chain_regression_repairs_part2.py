from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def _register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _register


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_portfolio_manager_same_user_create_get_add_delete(monkeypatch):
    from akshare_mcp.tools.managers import portfolio_manager

    class Conn:
        def __init__(self):
            self.portfolio = None
            self.holdings = []

        async def fetchval(self, query, *args):
            if "INSERT INTO portfolios" in query:
                self.portfolio = {
                    "id": 1,
                    "name": args[0],
                    "description": args[1],
                    "metadata": args[2],
                    "user_id": args[3],
                    "initial_capital": args[4],
                    "current_value": args[4],
                }
                return 1
            return None

        async def fetchrow(self, query, *args):
            if "SELECT * FROM portfolios WHERE id" in query:
                return dict(self.portfolio) if self.portfolio and self.portfolio["id"] == args[0] else None
            return None

        async def fetch(self, query, *args):
            if "SELECT * FROM portfolios WHERE user_id" in query:
                return [dict(self.portfolio)] if self.portfolio and self.portfolio["user_id"] == args[0] else []
            if "SELECT * FROM holdings" in query:
                return [dict(item) for item in self.holdings]
            return []

        async def execute(self, query, *args):
            if "INSERT INTO holdings" in query:
                self.holdings.append({"portfolio_id": args[0], "code": args[1], "shares": args[2], "cost_price": args[3]})
            if "DELETE FROM holdings" in query:
                self.holdings = []
            if "DELETE FROM portfolios" in query:
                self.portfolio = None
            return "OK"

    conn = Conn()

    class Db:
        def acquire(self):
            return FakeAcquire(conn)

    monkeypatch.setattr(portfolio_manager, "get_db", lambda: Db())
    mcp = FakeMcp()
    portfolio_manager.register_portfolio_manager(mcp)

    created = asyncio.run(
        mcp.tools["portfolio_manager"](
            action="create",
            params={"user_id": "u1", "name": "p1", "initial_capital": 1000},
        )
    )
    assert created["success"] is True

    got = asyncio.run(mcp.tools["portfolio_manager"](action="get", params={"user_id": "u1", "portfolio_id": 1}))
    assert got["success"] is True

    added = asyncio.run(
        mcp.tools["portfolio_manager"](
            action="add_holding",
            params={"user_id": "u1", "portfolio_id": 1, "code": "600519", "shares": 100, "cost_price": 10},
        )
    )
    assert added["success"] is True

    deleted = asyncio.run(mcp.tools["portfolio_manager"](action="delete", params={"user_id": "u1", "portfolio_id": 1}))
    assert deleted["success"] is True
    assert conn.portfolio is None


def test_index_quote_returns_degraded_empty_when_all_sources_missing(monkeypatch):
    from akshare_mcp.tools.market import quote

    monkeypatch.setattr(quote, "_fetch_single_index_quote_eastmoney", lambda _code: None)
    monkeypatch.setattr(quote, "_get_index_spot_indexed", lambda: (pd.DataFrame(), False))
    monkeypatch.setattr(quote, "ak", None)
    monkeypatch.setattr(quote.data_source, "get_tushare_pro", lambda: None)

    result = quote.get_index_quote("000001")
    assert result["success"] is True
    assert result["data"]["code"] == "000001"
    assert result["data"]["price"] is None
    assert result["degraded"] is True
    assert result["fallback_used"] is True


def test_limit_up_statistics_normalizes_mixed_items(monkeypatch):
    from akshare_mcp.tools.market import limit_up

    monkeypatch.setattr(
        limit_up,
        "get_limit_up_stocks",
        lambda _date="": {
            "success": True,
            "data": [
                {"code": "600519", "continuousDays": "1", "openTimes": "0", "tradeDate": "2026-05-20"},
                "000858",
                {"code": "002304", "continuousDays": 4, "openTimes": 2},
            ],
            "source": "test",
            "source_chain": ["test"],
        },
    )

    result = limit_up.get_limit_up_statistics.__wrapped__("2026-05-20")
    assert result["success"] is True
    assert result["data"]["totalLimitUp"] == 3
    assert result["data"]["firstBoard"] == 1
    assert result["data"]["higherBoard"] == 1
    assert result["data"]["failedBoard"] == 1


def test_sentiment_sector_sentiment_uses_params_for_stock_sentiment(monkeypatch):
    from akshare_mcp.tools.managers import sentiment_manager

    class Conn:
        async def fetch(self, query, *_args):
            if "pragma_table_info('stocks')" in query:
                return [{"column_name": "stock_code"}, {"column_name": "industry"}]
            if "FROM stocks" in query:
                return [{"code": "600519"}, {"code": "000858"}]
            if "FROM kline_1d" in query:
                return [
                    {"close": 10, "volume": 100},
                    {"close": 11, "volume": 130},
                    {"close": 12, "volume": 150},
                ]
            return []

    class Db:
        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(sentiment_manager, "get_db", lambda: Db())
    mcp = FakeMcp()
    sentiment_manager.register_sentiment_manager(mcp)

    result = asyncio.run(
        mcp.tools["sentiment_manager"](action="sector_sentiment", params={"sector": "白酒"})
    )
    assert result["success"] is True
    assert result["data"]["stock_count"] == 2


def test_execution_hard_gate_does_not_create_task(monkeypatch):
    from akshare_mcp.tools.managers import execution_manager

    created = []
    monkeypatch.setattr(execution_manager, "_enrich_kwargs_with_realtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execution_manager, "_build_cost_model", lambda *_args, **_kwargs: {"estimated": {"total": 0}})
    monkeypatch.setattr(execution_manager, "_build_soft_gate_warnings", lambda **_kwargs: ([], {"profile": "balanced", "max_order_shares": 1, "max_slice_shares": 1, "min_duration_minutes": 1, "max_cost_ratio": 1}))
    monkeypatch.setattr(execution_manager, "_run_pretrade_gate", lambda **_kwargs: {"compliance_blocked": True, "compliance_violations": ["blocked"], "compliance_passed": False})

    def fake_create(*_args, **_kwargs):
        created.append(True)
        return {"task_id": "exec_should_not_exist"}

    monkeypatch.setattr(execution_manager, "_create_task", fake_create)
    mcp = FakeMcp()
    execution_manager.register_execution_manager(mcp)

    result = asyncio.run(
        mcp.tools["execution_manager"](
            action="twap",
            params={"code": "600519", "total_shares": 100, "duration": 10},
        )
    )
    assert result["success"] is False
    assert result["data"]["compliance_gate"]["compliance_blocked"] is True
    assert created == []


def test_performance_benchmark_comparison_insufficient_returns_degraded(monkeypatch):
    from akshare_mcp.tools.managers import performance_manager

    class Conn:
        async def fetch(self, query, *_args):
            if "FROM portfolios" in query:
                return []
            if "FROM holdings" in query:
                return [{"code": "600519", "shares": 100, "cost_price": 10}]
            return []

        async def fetchrow(self, query, *_args):
            if "FROM portfolios" in query:
                return {
                    "id": 1,
                    "name": "p",
                    "initial_capital": 1000,
                    "current_value": 1100,
                    "created_at": "2026-05-20T00:00:00+00:00",
                }
            return None

    class Db:
        def acquire(self):
            return FakeAcquire(Conn())

    async def empty_portfolio_returns(*_args, **_kwargs):
        return np.array([])

    async def empty_benchmark_returns(*_args, **_kwargs):
        return [], np.array([]), None

    monkeypatch.setattr(performance_manager, "get_db", lambda: Db())
    monkeypatch.setattr(performance_manager, "_build_portfolio_daily_returns", empty_portfolio_returns)
    monkeypatch.setattr(performance_manager, "_fetch_dated_returns_for_code", empty_benchmark_returns)
    mcp = FakeMcp()
    performance_manager.register_performance_manager(mcp)

    result = asyncio.run(
        mcp.tools["performance_manager"](
            action="benchmark_comparison",
            params={"portfolio_id": 1, "benchmark": "000300", "lookback_days": 20},
        )
    )
    assert result["success"] is True
    assert result["data"]["aligned_days"] == 0
    assert result["degraded"] is True


def test_screener_manager_sector_alias_uses_like(monkeypatch):
    from akshare_mcp.tools.managers import screener_manager

    class Conn:
        async def fetch(self, query, *args):
            if "pragma_table_info('stocks')" in query:
                return [
                    {"column_name": "stock_code"},
                    {"column_name": "stock_name"},
                    {"column_name": "market_cap"},
                    {"column_name": "pe_ratio"},
                    {"column_name": "pb_ratio"},
                    {"column_name": "industry"},
                ]
            if "FROM stocks s" in query:
                assert "LIKE" in query
                assert any("酿酒" in str(arg) or "白酒" in str(arg) for arg in args)
                return [
                    {
                        "code": "600519",
                        "stock_name": "贵州茅台",
                        "market_cap": 2.0e12,
                        "pe_ratio": 25,
                        "pb_ratio": 8,
                        "roe": 20,
                        "revenue_growth": 10,
                        "debt_ratio": 20,
                        "industry": "酿酒",
                    }
                ]
            return []

    class Db:
        async def _financials_code_column(self, _conn):
            return "stock_code"

        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(screener_manager, "get_db", lambda: Db())
    mcp = FakeMcp()
    screener_manager.register_screener_manager(mcp)
    result = asyncio.run(
        mcp.tools["screener_manager"](
            action="screen",
            params={"criteria": {"sectors": ["白酒"]}, "limit": 5},
        )
    )
    assert result["success"] is True
    assert result["data"]["stocks"][0]["industry"] == "酿酒"


def test_paper_archive_account_user_mismatch_is_explicit(monkeypatch):
    from akshare_mcp.tools.managers import paper_trading_manager

    class Conn:
        async def fetchrow(self, query, *args):
            if "FROM paper_accounts" in query:
                assert len(args) == 1
                return {"id": args[0], "user_id": "owner", "status": "active"}
            return None

    class Db:
        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(paper_trading_manager, "get_db", lambda: Db())
    mcp = FakeMcp()
    paper_trading_manager.register_paper_trading_manager(mcp)

    result = asyncio.run(
        mcp.tools["paper_trading_manager"](
            action="archive_account",
            params={"user_id": "other", "account_id": "acc1"},
        )
    )
    assert result["success"] is False
    assert result["error_code"] == "USER_SCOPE_MISMATCH"
    assert result["data"]["scope"] == "paper_account"


def test_semantic_stock_search_baijiu_seed_fallback(monkeypatch):
    from akshare_mcp.tools import vector

    class Conn:
        async def fetch(self, *_args):
            return []

    class Db:
        async def _stocks_code_column(self, _conn):
            return "code"

        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(vector, "get_db", lambda: Db())
    result = asyncio.run(vector.semantic_stock_search("白酒", limit=5))
    assert result["success"] is True
    codes = {item["code"] for item in result["data"]["results"]}
    assert {"600519", "000858", "002304"} <= codes


def test_ok_with_meta_propagates_data_degraded():
    from akshare_mcp.tools.manager_protocol import ok_with_meta

    result = ok_with_meta(
        {
            "items": [],
            "degraded": True,
            "fallback_used": True,
            "fallback_reason": "db_empty_result",
            "source_chain": ["db", "python"],
        },
        tool_name="vector_search_manager",
        action="similar_stocks",
        started_at=0,
    )
    assert result["success"] is True
    assert result["degraded"] is True
    assert result["fallback_used"] is True
    assert result["meta"]["degraded"] is True


def test_vector_search_manager_similar_stocks_empty_profile_is_degraded_success(monkeypatch):
    from akshare_mcp.tools.managers import vector_search_manager

    class Tool:
        async def run(self, _args):
            return {
                "success": True,
                "data": {
                    "code": "600519",
                    "similar_stocks": [],
                    "candidate_scope": "unknown",
                    "similarity_type": "both",
                    "backend_requested": "db",
                    "backend_used": "none",
                    "fallback_used": True,
                    "fallback_reason": "target_stock_profile_missing",
                    "degraded": True,
                    "source_chain": ["db.stock_info", "vector_search.similar_stocks"],
                },
                "error": None,
                "degraded": True,
                "fallback_used": True,
                "source_chain": ["db.stock_info", "vector_search.similar_stocks"],
            }

    class ToolManager:
        _tools = {"search_similar_stocks": Tool()}

    mcp = FakeMcp()
    mcp._tool_manager = ToolManager()
    vector_search_manager.register_vector_search_manager(mcp)

    result = asyncio.run(
        mcp.tools["vector_search_manager"](
            action="similar_stocks",
            params={"code": "600519", "similarity_type": "both", "top_n": 3},
        )
    )

    assert result["success"] is True
    assert result["degraded"] is True
    assert result["fallback_used"] is True
    assert result["data"]["similar_stocks"] == []


def test_propagate_data_quality_marks_fallback_as_degraded():
    from akshare_mcp.utils import ok, propagate_data_quality_to_top

    result = propagate_data_quality_to_top(
        ok(
            {
                "items": [{"code": "600519"}],
                "fallback_used": True,
                "fallback_reason": "db_empty_result",
                "source_chain": ["db", "python"],
            }
        )
    )
    assert result["success"] is True
    assert result["fallback_used"] is True
    assert result["degraded"] is True
    assert "fallback" in result["quality_flags"]


def test_semantic_stock_search_respects_exclusion_and_conditions(monkeypatch):
    from akshare_mcp.tools import vector

    class Conn:
        async def fetch(self, *_args):
            return []

    class Db:
        async def _stocks_code_column(self, _conn):
            return "code"

        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(vector, "get_db", lambda: Db())
    result = asyncio.run(vector.semantic_stock_search("白酒但不要茅台 RSI低于30", limit=5))
    assert result["success"] is True
    codes = {item["code"] for item in result["data"]["results"]}
    assert "600519" not in codes
    assert {"000858", "002304"} <= codes
    assert result["data"]["suggestion"] == "parse_selection_query"


def test_semantic_stock_search_respects_real_chinese_exclusion_and_conditions(monkeypatch):
    from akshare_mcp.tools import vector

    class Conn:
        async def fetch(self, *_args):
            return []

    class Db:
        async def _stocks_code_column(self, _conn):
            return "code"

        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(vector, "get_db", lambda: Db())
    result = asyncio.run(vector.semantic_stock_search("白酒但不要茅台 RSI低于30", limit=5))
    assert result["success"] is True
    codes = {item["code"] for item in result["data"]["results"]}
    assert "600519" not in codes
    assert {"000858", "002304"} <= codes
    assert "茅台" in result["data"]["excluded_terms"]
    assert result["data"]["suggestion"] == "parse_selection_query"


def test_build_quant_context_propagates_nested_quality(monkeypatch):
    from akshare_mcp.tools import _decision_context as decision_context

    async def fake_resolve(**_kwargs):
        return "600519", None

    async def fake_build(_code):
        return {
            "code": "600519",
            "data_quality": {
                "degraded": True,
                "fallback_used": True,
                "fallback_reason": ["oos_validation:peer_codes_insufficient"],
                "source_chain": ["decision_quant_builder", "db.get_klines"],
                "quality_flags": ["fallback", "partial", "degraded"],
            },
        }

    monkeypatch.setattr(decision_context, "_resolve_existing_stock_code_or_fail", fake_resolve)
    monkeypatch.setattr(decision_context, "_build_quant_context", fake_build)
    result = asyncio.run(decision_context.build_quant_context(code="600519"))
    assert result["success"] is True
    assert result["degraded"] is True
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == ["oos_validation:peer_codes_insufficient"]


def test_data_quality_workflow_returns_json_safe_checkpoint(monkeypatch):
    from akshare_mcp.tools import ai_workflows

    class FakeMcp:
        def __init__(self):
            self.tools = {}

        def tool(self, *args, **kwargs):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn

            return register

    mcp = FakeMcp()
    ai_workflows.register(mcp)

    result = asyncio.run(
        mcp.tools["data_quality_workflow"](
            dataset_id="dq_json_safe",
            records=[{"code": "600519", "signal": "rsi", "sample_count": 9}],
            required_fields=["code", "signal", "sample_count"],
            minimum_quality_threshold=0.8,
            persist_artifact=True,
            output_artifact_id="dq_json_safe_artifact",
        )
    )
    json.dumps(result, ensure_ascii=False)
    assert result["success"] is True
    assert result["data"]["artifact_id"] == "dq_json_safe_artifact"
    assert len(result["data"]["checkpoint"].get("validations") or []) <= 3


def test_data_quality_workflow_propagates_quality_gate_degraded():
    from akshare_mcp.tools import ai_workflows

    class FakeMcp:
        def __init__(self):
            self.tools = {}

        def tool(self, *args, **kwargs):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn

            return register

    mcp = FakeMcp()
    ai_workflows.register(mcp)

    result = asyncio.run(
        mcp.tools["data_quality_workflow"](
            dataset_id="dq_degraded_top_level",
            records=[{"code": "600519"}, {"code": "000858", "close": 10}],
            required_fields=["code", "close"],
            minimum_quality_threshold=0.9,
        )
    )
    assert result["success"] is True
    assert result["degraded"] is True
    assert "degraded" in result["quality_flags"]
    assert result["source_chain"] == ["workflow.data_quality"]
    assert result["meta"]["degraded"] is True


def test_order_book_quote_fallback_marks_depth_degraded(monkeypatch):
    from akshare_mcp.tools.market import order_book

    monkeypatch.setattr(
        order_book,
        "get_quote_snapshot_sync",
        lambda *_args, **_kwargs: {
            "success": True,
            "data": {"price": 1298.66},
            "backend_requested": "db.stock_quotes",
            "backend_used": "db.stock_quotes",
            "fallback_used": False,
            "fallback_reason": ["db_snapshot_stale"],
            "source_chain": ["db.stock_quotes"],
            "stale": True,
            "db_snapshot_time": "2026-05-22T09:59:39",
            "data_freshness_seconds": 1490.0,
        },
    )

    result = order_book.get_order_book.__wrapped__(code="600519", live=True)
    assert result["success"] is True
    assert result["degraded"] is True
    assert result["data"]["depth_degraded"] is True
    assert "depth_degraded" in result["quality_flags"]


def test_validate_factor_oos_short_panel_is_degraded_success(monkeypatch):
    from akshare_mcp.tools import quant

    class FakeMcp:
        def __init__(self):
            self.tools = {}

        def tool(self, *args, **kwargs):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn

            return register

    async def fake_oos_validation(**_kwargs):
        return {
            "success": False,
            "data": None,
            "error": "Not enough valid codes for panel build, stats={'input_codes': 3, 'processed_codes': 0}",
            "source": "akshare",
            "cached": False,
            "degraded": True,
            "quality_flags": ["failed"],
        }

    monkeypatch.setattr(quant, "run_factor_oos_validation", fake_oos_validation)
    mcp = FakeMcp()
    quant.register(mcp)

    result = asyncio.run(
        mcp.tools["validate_factor_oos"](
            codes=["600519", "000001", "000858"],
            factor="rsi_14",
            start_date="2026-01-01",
            end_date="2026-05-20",
        )
    )

    assert result["success"] is True
    assert result["degraded"] is True
    assert result["data"]["insufficient_sample"] is True
    assert result["fallback_reason"] == "insufficient_panel_sample"
