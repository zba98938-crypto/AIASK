from __future__ import annotations

import asyncio

from akshare_mcp.services.market_text_source_ingest import run_market_text_source_ingest
from akshare_mcp.storage import close_db, get_db
from akshare_mcp.tools.managers._data_sync_manager_support_core import _build_schedule_params
from akshare_mcp.tools.managers._data_sync_manager_support_sync import (
    _sync_market_text_source_ingest_now,
)


def _write_embedding_env(tmp_path, monkeypatch, db_name: str) -> None:
    env_file = tmp_path / f"{db_name}.env"
    env_file.write_text(
        "\n".join(
            [
                "STRATEGY_EMBEDDING_ENABLED=1",
                "STRATEGY_EMBEDDING_PROVIDER=hash_fallback",
                "STRATEGY_EMBEDDING_HASH_DIMENSIONS=256",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AKSHARE_MCP_ENV", str(env_file))
    db_path = str(tmp_path / f"{db_name}.sqlite3")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", db_path)
    monkeypatch.setenv("AIASK_SQLITE_PATH", db_path)


def test_market_text_source_ingest_vectorizes_public_text(tmp_path, monkeypatch):
    _write_embedding_env(tmp_path, monkeypatch, "market_text")

    import akshare_mcp.services.market_text_source_ingest as ingest_mod

    def fake_news(limit: int):
        return [
            {
                "doc_uid": "mock-news-1",
                "title": "央行释放流动性",
                "summary": "公开新闻摘要，讨论社融和货币政策。",
                "content": "央行释放流动性 公开新闻摘要，讨论社融和货币政策。",
                "date": "2026-05-14",
                "source": "mock_news",
                "url": "https://example.com/news/1",
            }
        ][:limit]

    def fake_notice_head(start_iso: str, end_iso: str, max_items: int = 20):
        return [
            {
                "code": "000001",
                "title": "平安银行:年度股东会决议公告",
                "type": "股东会",
                "date": "2026-05-14",
                "url": "https://data.eastmoney.com/notices/detail/000001/mock.html",
            }
        ][:max_items]

    def fake_stock_notices(**kwargs):
        return {
            "success": True,
            "data": {
                "events": [
                    {
                        "code": kwargs.get("stock_code") or "000001",
                        "title": "平安银行:风险提示公告",
                        "type": "风险提示",
                        "date": "2026-05-14",
                        "url": "https://data.eastmoney.com/notices/detail/000001/mock-risk.html",
                    }
                ]
            },
        }

    def fake_research_reports(**kwargs):
        return {
            "success": True,
            "data": {
                "reports": [
                    {
                        "title": "平安银行研报:资产质量改善",
                        "institution": "Mock Securities",
                        "rating": "买入",
                        "summary": "公开研报摘要，讨论资产质量和利润增长。",
                        "date": "2026-05-14",
                    }
                ]
            },
        }

    monkeypatch.setattr(ingest_mod, "fetch_eastmoney_finance_news", fake_news)
    monkeypatch.setattr(
        ingest_mod,
        "fetch_official_market_event_documents",
        lambda *args, **kwargs: {"items": [], "sources": {"cninfo": {"status": "ok", "fetched": 0}}, "degraded_count": 0},
    )
    monkeypatch.setattr("akshare_mcp.tools.news.notices.fetch_market_notice_head", fake_notice_head)
    monkeypatch.setattr("akshare_mcp.tools.news.notices.get_stock_notices", fake_stock_notices)
    monkeypatch.setattr("akshare_mcp.tools.news.research.get_research_reports", fake_research_reports)

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            from akshare_mcp.services.text_embedding import close_strategy_text_embedding_service

            await close_strategy_text_embedding_service()
            async with db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO stocks (stock_code, stock_name, market, market_cap)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (stock_code) DO UPDATE SET
                        stock_name = EXCLUDED.stock_name,
                        market = EXCLUDED.market,
                        market_cap = EXCLUDED.market_cap
                    """,
                    "000001",
                    "平安银行",
                    "A",
                    100.0,
                )

            result = await run_market_text_source_ingest(
                db,
                stock_codes=["000001"],
                news_limit=1,
                notice_limit=1,
                code_notice_limit=1,
                code_notice_code_limit=1,
                research_code_limit=1,
                research_per_code=1,
                build_snapshot=True,
                activate_snapshot=True,
            )
            assert result["totals"]["errors"] == 0
            assert result["saved"]["news"]["embedded_chunks"] == 1
            assert result["saved"]["notice_head"]["embedded_chunks"] == 1
            assert result["saved"]["code_notices"]["embedded_chunks"] == 1
            assert result["saved"]["research"]["embedded_chunks"] == 1
            assert {item["collection_name"] for item in result["snapshots"]} == {
                "market_doc_chunks__news",
                "market_doc_chunks__notice",
                "market_doc_chunks__research",
            }

            async with db.acquire() as conn:
                docs = await conn.fetchval("SELECT COUNT(*) FROM market_documents")
                fts_rows = await conn.fetchval("SELECT COUNT(*) FROM market_doc_chunks_fts")
                profiles = await conn.fetchval(
                    "SELECT COUNT(*) FROM vector_profiles WHERE collection_name LIKE 'market_doc_chunks__%'"
                )
                dims = await conn.fetch(
                    """
                    SELECT DISTINCT vector_dim
                    FROM vector_profiles
                    WHERE collection_name LIKE 'market_doc_chunks__%'
                    """
                )
                active = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM vector_index_snapshots
                    WHERE collection_name LIKE 'market_doc_chunks__%' AND status = 'active'
                    """
                )
            assert docs == 4
            assert fts_rows == 4
            assert profiles == 4
            assert {int(row["vector_dim"]) for row in dims} == {256}
            assert active == 3
        finally:
            await close_db()

    asyncio.run(_run())


def test_market_text_source_ingest_persists_official_notice_events(tmp_path, monkeypatch):
    _write_embedding_env(tmp_path, monkeypatch, "market_text_official")

    import akshare_mcp.services.market_text_source_ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "fetch_eastmoney_finance_news", lambda limit: [])
    monkeypatch.setattr(
        ingest_mod,
        "fetch_official_market_event_documents",
        lambda *args, **kwargs: {
            "items": [
                {
                    "doc_uid": "cninfo:1225000001",
                    "title": "000001 signs major order contract",
                    "summary": "Official disclosure for a new signed order contract.",
                    "content": "000001 signs major order contract official disclosure.",
                    "published_at": "2026-06-06",
                    "evidence_time": "2026-06-06",
                    "source": "cninfo",
                    "source_tier": "tier_a",
                    "provider": "cninfo",
                    "original_id": "1225000001",
                    "reliability_score": 0.92,
                    "url": "https://static.cninfo.com.cn/finalpage/2026-06-06/1225000001.PDF",
                    "code": "000001",
                    "stock_code": "000001",
                }
            ],
            "sources": {"cninfo": {"tier": "tier_a", "status": "ok", "fetched": 1, "degraded": False}},
            "degraded_count": 0,
        },
    )
    monkeypatch.setattr("akshare_mcp.tools.news.notices.fetch_market_notice_head", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "akshare_mcp.tools.news.notices.get_stock_notices",
        lambda **kwargs: {"success": True, "data": {"events": []}},
    )

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            result = await run_market_text_source_ingest(
                db,
                stock_codes=["000001"],
                doc_types=["notice"],
                news_limit=0,
                notice_limit=0,
                official_notice_limit=1,
                code_notice_limit=0,
                research_code_limit=0,
                build_snapshot=False,
                activate_snapshot=False,
                embed=False,
            )

            assert result["fetched"]["official_notice"] == 1
            assert result["saved"]["official_notice"]["documents"] == 1
            assert result["normalized_events"]["official_notice"]["verified"] == 1
            assert result["strategy_factory_bridge"]["bridged_events"] == 1

            async with db.acquire() as conn:
                doc = await conn.fetchrow(
                    """
                    SELECT source_tier, provider, original_id, reliability_score
                    FROM market_documents
                    WHERE doc_uid = $1
                    """,
                    "cninfo:1225000001",
                )
                event_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM market_events_normalized WHERE source_tier = 'tier_a' AND status = 'verified'"
                )
            assert doc["source_tier"] == "tier_a"
            assert doc["provider"] == "cninfo"
            assert doc["original_id"] == "1225000001"
            assert float(doc["reliability_score"]) == 0.92
            assert event_count == 1
        finally:
            await close_db()

    asyncio.run(_run())


def test_data_sync_market_text_source_ingest_task_and_schedule_params(tmp_path, monkeypatch):
    _write_embedding_env(tmp_path, monkeypatch, "market_text_sync")

    import akshare_mcp.services.market_text_source_ingest as ingest_mod
    import strategy_factory.runtime.market_event_ingest as ingest_runtime_module
    import akshare_mcp.runtime.strategy_factory_bootstrap as bootstrap_module

    monkeypatch.setattr(
        ingest_mod,
        "fetch_official_market_event_documents",
        lambda *args, **kwargs: {"items": [], "sources": {"cninfo": {"status": "ok", "fetched": 0}}, "degraded_count": 0},
    )
    monkeypatch.setattr(
        ingest_mod,
        "fetch_eastmoney_finance_news",
        lambda limit: [
            {
                "doc_uid": "mock-sync-news-1",
                "title": "市场日更新闻",
                "summary": "用于 data_sync_manager 调度测试。",
                "content": "市场日更新闻 用于 data_sync_manager 调度测试。",
                "date": "2026-05-14",
                "source": "mock_news",
            }
        ],
    )
    monkeypatch.setattr(bootstrap_module, "configure_local_strategy_factory_runtime", lambda: None)

    class _Runtime:
        async def run_once(self, *, db=None, **kwargs):
            return await ingest_mod.run_market_text_source_ingest(db, **kwargs)

    monkeypatch.setattr(ingest_runtime_module, "get_market_event_ingest_runtime", lambda: _Runtime())

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            from akshare_mcp.services.text_embedding import close_strategy_text_embedding_service

            await close_strategy_text_embedding_service()
            result = await _sync_market_text_source_ingest_now(
                {
                    "doc_types": ["news"],
                    "news_limit": 1,
                    "notice_limit": 0,
                    "research_code_limit": 0,
                    "build_snapshot": True,
                    "activate_snapshot": True,
                }
            )
            assert result["success"] == 1
            assert result["ingest"]["saved"]["news"]["embedded_chunks"] == 1
            assert "market_text_source_ingest" in result["message"]

            params = _build_schedule_params(
                "market_text_source_ingest",
                {
                    "doc_types": ["news", "notice", "research"],
                    "news_limit": 50,
                    "build_snapshot": True,
                },
                [],
            )
            assert params["doc_types"] == ["news", "notice", "research"]
            assert params["news_limit"] == 50
            assert params["build_snapshot"] is True
        finally:
            await close_db()

    asyncio.run(_run())
