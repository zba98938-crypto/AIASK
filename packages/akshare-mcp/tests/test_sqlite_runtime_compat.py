from __future__ import annotations

import asyncio
import pytest

from akshare_mcp.storage import close_db, get_db
from akshare_mcp.services.factor_candidate_seed import seed_factor_candidate_records
from akshare_mcp.services.factor_candidate_vector_backfill import backfill_factor_candidate_vectors
from akshare_mcp.services.pattern_embedding_pipeline import backfill_kline_pattern_vectors
from akshare_mcp.services.stock_profile_pipeline import build_stock_profile_payload
from akshare_mcp.services.unified_vector_governance import build_vector_collection_snapshot


def test_sqlite_runtime_schema_and_query_compatibility(tmp_path, monkeypatch):
    db_path = str(tmp_path / "akshare_runtime.sqlite3")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", db_path)
    monkeypatch.setenv("AIASK_SQLITE_PATH", db_path)

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()

            async with db.acquire() as conn:
                assert await conn.fetchval(
                    "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = $1",
                    "strategy_signal_event_snapshots_latest",
                )
                for table_name in (
                    "vector_dimension_contracts",
                    "vector_graph_nodes",
                    "vector_graph_edges",
                    "vector_optimization_runs",
                ):
                    assert await conn.fetchval(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = $1",
                        table_name,
                    )
                assert await conn.fetchval(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = $1",
                    "market_doc_chunks_fts",
                )

                await conn.execute("CREATE TABLE IF NOT EXISTS json_probe (payload TEXT)")
                await conn.execute("DELETE FROM json_probe")
                await conn.execute("INSERT INTO json_probe (payload) VALUES ($1)", {"key": "value"})
                assert await conn.fetchval("SELECT payload->>'key' FROM json_probe LIMIT 1") == "value"

                await conn.execute(
                    """
                    INSERT INTO stocks (stock_code, stock_name, market_cap, pe_ratio, pb_ratio)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (stock_code) DO UPDATE SET
                        stock_name = EXCLUDED.stock_name,
                        market_cap = EXCLUDED.market_cap,
                        pe_ratio = EXCLUDED.pe_ratio,
                        pb_ratio = EXCLUDED.pb_ratio
                    """,
                    "000001",
                    "SQLite Test Bank",
                    1000000000,
                    5.0,
                    0.8,
                )
                await conn.execute(
                    """
                    INSERT INTO financials (
                        stock_code, report_date, roe, debt_ratio, revenue_growth, profit_growth
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (stock_code, report_date) DO UPDATE SET
                        roe = EXCLUDED.roe,
                        debt_ratio = EXCLUDED.debt_ratio,
                        revenue_growth = EXCLUDED.revenue_growth,
                        profit_growth = EXCLUDED.profit_growth
                    """,
                    "000001",
                    "2026-03-31",
                    12.5,
                    45.0,
                    8.0,
                    9.0,
                )
                doc = await conn.fetchrow(
                    """
                    INSERT INTO market_documents (doc_uid, stock_code, doc_type, source, title, body, created_at, updated_at)
                    VALUES ($1, $2, 'news', 'test', $3, $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    RETURNING id
                    """,
                    "doc-1",
                    "000001",
                    "Alpha earnings",
                    "Alpha earnings surprise and liquidity expansion",
                )
                await conn.execute(
                    """
                    INSERT INTO market_doc_chunks (doc_id, chunk_no, stock_code, doc_type, source, title, chunk_text, created_at, updated_at)
                    VALUES ($1, 0, $2, 'news', 'test', $3, $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    doc["id"],
                    "000001",
                    "Alpha earnings",
                    "Alpha earnings surprise and liquidity expansion",
                )
                assert await conn.fetchval(
                    "SELECT COUNT(*) FROM market_doc_chunks_fts WHERE market_doc_chunks_fts MATCH $1",
                    '"Alpha"',
                ) >= 1

            financials = await db.get_financials("000001", limit=1)
            assert financials[0]["report_date"] == "2026-03-31"
            payload = await build_stock_profile_payload(db, "000001", profile_type="both", kline_limit=30, version="stock-profile-v1")
            raw_features = payload["metadata"]["raw_features"]
            assert raw_features["roe"] == 12.5
            assert raw_features["debt_ratio"] == 0.45

            with pytest.raises(ValueError, match="dimension contract mismatch"):
                await db.save_vector_profile(
                    {
                        "collection_name": "stock_profile_embeddings",
                        "entity_type": "stock_profile",
                        "entity_id": "bad-dim",
                        "profile_type": "both",
                        "model_id": "stock-profile-v1",
                        "vector_dim": 10,
                        "version": "stock-profile-v1",
                        "embedding": [0.0] * 10,
                    }
                )

            await db.save_klines(
                "000001",
                [
                    {
                        "date": f"2026-01-{day:02d}",
                        "open": 10 + day * 0.1,
                        "high": 10.5 + day * 0.1,
                        "low": 9.5 + day * 0.1,
                        "close": 10.2 + day * 0.1,
                        "volume": 1000 + day,
                        "amount": 10000 + day,
                    }
                    for day in range(1, 31)
                ],
            )
            kline_result = await backfill_kline_pattern_vectors(
                db,
                stock_codes=["000001"],
                window_size=20,
                vector_method="returns",
                version="v1",
            )
            assert kline_result["collection_name"] == "kline_returns_w20_d19"
            assert kline_result["saved_profiles"] == 1
            profile_rows = await db.list_vector_profiles(collection_name="kline_returns_w20_d19", limit=5)
            assert profile_rows[0]["vector_dim"] == 19

            await db.save_strategy(
                {
                    "id": "sqlite-listed",
                    "name": "SQLite Listed",
                    "status": "listed",
                    "strategy_type": "demo",
                    "tags": ["sqlite"],
                }
            )
            await db.save_strategy(
                {
                    "id": "sqlite-draft",
                    "name": "SQLite Draft",
                    "status": "draft",
                    "strategy_type": "demo",
                    "tags": ["sqlite"],
                }
            )
            strategies = await db.list_strategies(status=["listed", "draft"], limit=10)
            assert {"sqlite-listed", "sqlite-draft"}.issubset({item["id"] for item in strategies})

            await db.save_strategy_signal_event_snapshot(
                {
                    "strategy_id": "sqlite-listed",
                    "code": "000001",
                    "as_of_date": "2026-01-01",
                    "latest_bar_signal": 1,
                    "recent_events": [{"event": "old"}],
                }
            )
            await db.save_strategy_signal_event_snapshot(
                {
                    "strategy_id": "sqlite-listed",
                    "code": "000001",
                    "as_of_date": "2026-01-02",
                    "latest_bar_signal": -1,
                    "recent_events": [{"event": "latest"}],
                }
            )
            await db.save_strategy_signal_event_snapshot(
                {
                    "strategy_id": "sqlite-listed",
                    "code": "000002",
                    "as_of_date": "2026-01-01",
                    "latest_bar_signal": 1,
                }
            )
            latest = await db.list_strategy_signal_event_snapshots(
                strategy_id="sqlite-listed",
                latest_only=True,
                limit=10,
            )
            latest_by_code = {item["code"]: item["as_of_date"] for item in latest}
            assert latest_by_code["000001"] == "2026-01-02"
            assert latest_by_code["000002"] == "2026-01-01"

            await db.replace_strategy_vector_index_items(
                "strategy_behavior",
                "v1",
                [
                    {"profile_id": 1, "strategy_id": "sqlite-listed", "bucket_id": "b1", "embedding": [1, 0]},
                    {"profile_id": 2, "strategy_id": "sqlite-draft", "bucket_id": "b2", "embedding": [0, 1]},
                ],
            )
            strategy_items = await db.list_strategy_vector_index_items(
                index_name="strategy_behavior",
                index_version="v1",
                bucket_ids=["b1"],
                limit=10,
            )
            assert [item["bucket_id"] for item in strategy_items] == ["b1"]

            await db.save_vector_collection(
                {
                    "collection_name": "sqlite_test_collection",
                    "entity_family": "strategy",
                    "vector_dim": 2,
                    "active_version": "v1",
                }
            )
            profile_a = await db.save_vector_profile(
                {
                    "collection_name": "sqlite_test_collection",
                    "entity_type": "generic",
                    "entity_id": "a",
                    "vector_dim": 2,
                    "version": "v1",
                    "embedding": [1, 0],
                }
            )
            profile_b = await db.save_vector_profile(
                {
                    "collection_name": "sqlite_test_collection",
                    "entity_type": "generic",
                    "entity_id": "b",
                    "vector_dim": 2,
                    "version": "v1",
                    "embedding": [0, 1],
                }
            )
            await db.replace_vector_index_items(
                "sqlite_test_collection",
                "v1",
                [
                    {"profile_id": profile_a["id"], "entity_id": "a", "bucket_id": "b1", "embedding": [1, 0]},
                    {"profile_id": profile_b["id"], "entity_id": "b", "bucket_id": "b2", "embedding": [0, 1]},
                ],
            )
            unified_items = await db.list_vector_index_items(
                collection_name="sqlite_test_collection",
                index_version="v1",
                bucket_ids=["b1"],
                limit=10,
            )
            assert [item["bucket_id"] for item in unified_items] == ["b1"]

            await db.save_vector_collection(
                {
                    "collection_name": "sqlite_filter_collection",
                    "entity_family": "stock_profile",
                    "vector_dim": 2,
                    "active_version": "v1",
                }
            )
            first_embedding = [1.0, 0.0]
            for idx in range(150):
                embedding = first_embedding if idx == 0 else [0.0, 1.0]
                await db.save_vector_profile(
                    {
                        "collection_name": "sqlite_filter_collection",
                        "entity_type": "stock_profile",
                        "entity_id": f"filter-{idx:03d}",
                        "stock_code": f"F{idx:03d}",
                        "vector_dim": 2,
                        "version": "v1",
                        "embedding": embedding,
                    }
                )
            filtered = await db.search_vector_collection(
                collection_name="sqlite_filter_collection",
                query_embedding=first_embedding,
                version="v1",
                stock_codes=["F000"],
                limit=1,
            )
            assert filtered["items"][0]["stock_code"] == "F000"
            assert filtered["items"][0]["similarity"] == 1.0

            seed_result = await seed_factor_candidate_records(db, limit=6, codes=["000001"])
            assert seed_result["saved_records"] == 6
            factor_result = await backfill_factor_candidate_vectors(db, limit=10, version="v1")
            assert factor_result["processed_records"] >= 6
            assert factor_result["saved_profiles"] >= 6
            factor_profiles = await db.list_vector_profiles(
                collection_name="factor_candidate_embeddings",
                version="v1",
                limit=10,
            )
            assert factor_profiles
            assert {item["vector_dim"] for item in factor_profiles} == {128}
            factor_snapshot = await build_vector_collection_snapshot(
                db,
                collection_name="factor_candidate_embeddings",
                version="v1",
                limit_profiles=100000,
                activate=True,
                source="test_sqlite_runtime_compat",
            )
            assert factor_snapshot["status"] == "active"
            assert factor_snapshot["items_count"] >= 6
            factor_search = await db.search_vector_collection(
                collection_name="factor_candidate_embeddings",
                query_embedding=factor_profiles[0]["embedding"],
                version="v1",
                limit=3,
            )
            assert factor_search["items"]
            assert factor_search["items"][0]["vector_dim"] == 128

            health = await db.get_strategy_vector_health()
            assert health["backend"] == "sqlite_python"
            assert health["sqlite_python_enabled"] is False
            assert isinstance(health["tables"], dict)
        finally:
            await close_db()

    asyncio.run(_run())


def test_sqlite_vector_search_rejects_query_dimension_mismatch(tmp_path, monkeypatch):
    db_path = str(tmp_path / "vector_dim_guard.sqlite3")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", db_path)
    monkeypatch.setenv("AIASK_SQLITE_PATH", db_path)

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            await db.save_vector_collection(
                {
                    "collection_name": "sqlite_dim_guard_collection",
                    "entity_family": "generic",
                    "vector_dim": 3,
                    "active_version": "v1",
                }
            )
            profile_a = await db.save_vector_profile(
                {
                    "collection_name": "sqlite_dim_guard_collection",
                    "entity_type": "generic",
                    "entity_id": "axis-x",
                    "vector_dim": 3,
                    "version": "v1",
                    "embedding": [1, 0, 0],
                }
            )
            profile_b = await db.save_vector_profile(
                {
                    "collection_name": "sqlite_dim_guard_collection",
                    "entity_type": "generic",
                    "entity_id": "axis-y",
                    "vector_dim": 3,
                    "version": "v1",
                    "embedding": [0, 1, 0],
                }
            )
            await db.replace_vector_index_items(
                "sqlite_dim_guard_collection",
                "v1",
                [
                    {"profile_id": profile_a["id"], "entity_id": "axis-x", "bucket_id": "x", "embedding": [1, 0, 0]},
                    {"profile_id": profile_b["id"], "entity_id": "axis-y", "bucket_id": "y", "embedding": [0, 1, 0]},
                ],
            )

            good = await db.search_vector_collection(
                collection_name="sqlite_dim_guard_collection",
                query_embedding=[1, 0, 0],
                version="v1",
                limit=2,
            )
            assert good["items"]
            assert good["items"][0]["entity_id"] == "axis-x"
            assert good["items"][0]["similarity"] == 1.0

            bad = await db.search_vector_collection(
                collection_name="sqlite_dim_guard_collection",
                query_embedding=[1, 0],
                version="v1",
                limit=2,
            )
            assert bad["items"] == []
            assert bad["backend_used"] == "dimension_mismatch"
            assert bad["fallback_reason"] == "query_dimension_mismatch"
            assert bad["query_vector_dim"] == 2
            assert bad["expected_vector_dim"] == 3

            invalid = await db.search_vector_collection(
                collection_name="sqlite_dim_guard_collection",
                query_embedding=[float("nan"), 0, 0],
                version="v1",
                limit=2,
            )
            assert invalid["items"] == []
            assert invalid["backend_used"] == "invalid_query"
            assert invalid["fallback_reason"] == "invalid_query_embedding"

            with pytest.raises(ValueError, match="finite"):
                await db.save_vector_profile(
                    {
                        "collection_name": "sqlite_dim_guard_collection",
                        "entity_type": "generic",
                        "entity_id": "bad-nan",
                        "vector_dim": 3,
                        "version": "v1",
                        "embedding": [float("nan"), 0, 0],
                    }
                )

            with pytest.raises(ValueError, match="finite"):
                await db.replace_vector_index_items(
                    "sqlite_dim_guard_collection",
                    "v2",
                    [
                        {"profile_id": profile_a["id"], "entity_id": "bad-nan", "bucket_id": "nan", "embedding": [1, float("inf"), 0]},
                    ],
                )
        finally:
            await close_db()

    asyncio.run(_run())
