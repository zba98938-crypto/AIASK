from __future__ import annotations

import asyncio
import json
from datetime import date

from aiask_quant_core.storage.sqlite import SQLiteAdapter

from akshare_mcp.services.stock_radar import (
    enhance_radar_event_with_llm,
    extract_radar_event,
    _confirmation_factors,
    fetch_rss_feed_documents,
    push_stock_radar_digest,
    run_stock_radar,
    score_radar_candidate,
)


def test_stock_radar_rule_extraction_detects_contract() -> None:
    extraction = extract_radar_event(
        {
            "doc_uid": "cninfo:1",
            "title": "Company signs AI compute cooperation contract",
            "summary": "The company signed a 5亿元 AI compute service contract.",
            "stock_code": "600000",
            "source_tier": "tier_a",
        }
    )

    assert extraction is not None
    payload = extraction.as_dict()
    assert payload["event_type"] == "ai_compute_cooperation"
    assert payload["direction"] == "positive"
    assert payload["amount_text"]
    assert payload["source_doc_uids"] == ["cninfo:1"]


def test_stock_radar_rule_extraction_handles_chinese_disclosures() -> None:
    samples = [
        ("关于签订AI算力重大合同的公告", "公司与客户签订5亿元AI算力服务重大合同。", "ai_compute_cooperation", "positive"),
        ("关于机器人订单的公告", "公司取得人形机器人订单。", "robotics_order", "positive"),
        ("关于股东减持股份计划的公告", "控股股东拟减持公司股份。", "shareholder_reduction", "negative"),
        ("关于收到监管问询函的公告", "公司收到交易所问询函。", "inquiry_letter", "negative"),
        ("关于业绩预增的公告", "公司预计净利润增长。", "earnings_forecast_up", "positive"),
    ]

    for title, summary, event_type, direction in samples:
        extraction = extract_radar_event(
            {
                "doc_uid": f"cninfo:{event_type}",
                "title": title,
                "summary": summary,
                "stock_code": "600000",
                "source_tier": "tier_a",
            }
        )

        assert extraction is not None, title
        payload = extraction.as_dict()
        assert payload["event_type"] == event_type
        assert payload["direction"] == direction
        assert payload["source_doc_uids"] == [f"cninfo:{event_type}"]


def test_stock_radar_score_applies_risk_penalty() -> None:
    positive = score_radar_candidate(
        extraction={"importance_score": 0.9, "direction": "positive"},
        source_tier="tier_a",
        confirmations={"sector_heat": {"score": 0.5}},
    )
    risky = score_radar_candidate(
        extraction={"importance_score": 0.9, "direction": "negative", "risk_flags": ["investigation"]},
        source_tier="tier_a",
        confirmations={"sector_heat": {"score": 0.5}},
    )

    assert positive["radar_score"] > risky["radar_score"]
    assert risky["component_scores"]["risk_penalty"] >= 18


def test_stock_radar_push_digest_allows_verified_high_confidence_candidate(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "stock_radar_push.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            run = await db.upsert_stock_radar_run(
                {
                    "run_id": "radar_verified_run",
                    "mode": "dry_run",
                    "status": "completed",
                    "summary": {"candidate_count": 1},
                    "metadata": {"no_trade_instructions": True},
                }
            )
            await db.upsert_stock_radar_candidate(
                {
                    "run_id": run["run_id"],
                    "symbol": "600000",
                    "stock_name": "浦发银行",
                    "radar_score": 86.0,
                    "event_id": "radar_evt_verified",
                    "event_type": "ai_compute_cooperation",
                    "direction": "positive",
                    "summary": "签订AI算力重大合同。",
                    "source_doc_uids": ["cninfo:verified"],
                    "source_chain": [{"provider": "cninfo", "source_tier": "tier_a", "url": "https://static.cninfo.com.cn/mock.pdf"}],
                    "extraction": {
                        "event_type": "ai_compute_cooperation",
                        "importance_score": 0.95,
                        "confidence": 0.86,
                        "status": "verified",
                        "llm_status": "ok",
                    },
                    "confirmations": {"fund_flow": {"confirmed": True}},
                    "risk_flags": [],
                }
            )

            result = await push_stock_radar_digest(
                db,
                {"run_id": run["run_id"], "channels": ["wecom", "telegram"], "dry_run": False},
            )

            assert result["success"] is True
            assert result["data"]["gateway_status"] == "queued_for_gateway_adapter"
            assert result["data"]["high_confidence_candidate_count"] == 1
            assert {log["status"] for log in result["data"]["push_logs"]} == {"queued"}
            assert all(log["metadata"]["no_trade_instructions"] is True for log in result["data"]["push_logs"])
        finally:
            await db.close()

    asyncio.run(_run())


def _patch_degraded_confirmations(monkeypatch) -> None:
    import akshare_mcp.services.stock_radar as radar_mod

    async def fake_confirmations(_db, symbol, extraction):
        return {
            "fund_flow": {"status": "degraded", "confirmed": False, "reason": "test"},
            "north_fund": {"status": "degraded", "confirmed": False, "reason": "test"},
            "dragon_tiger": {"status": "degraded", "confirmed": False, "reason": "test", "alias_policy": "alias_mapping_only"},
            "sector_heat": {"status": "degraded", "score": 0.0, "themes": list(extraction.get("themes") or []), "reason": "test"},
            "late_session_volume": {"status": "disabled", "confirmed": False, "reason": "test"},
            "symbol": symbol,
        }

    monkeypatch.setattr(radar_mod, "_confirmation_factors", fake_confirmations)


def test_stock_radar_run_uses_existing_market_documents_with_degraded_fallbacks(tmp_path, monkeypatch) -> None:
    import akshare_mcp.services.stock_radar as radar_mod

    async def fake_ingest(*args, **kwargs):
        return {"totals": {"saved_docs": 0}, "fetched": {}, "errors": [], "quality_flags": []}

    monkeypatch.setattr(radar_mod, "run_market_text_source_ingest", fake_ingest)
    _patch_degraded_confirmations(monkeypatch)
    db = SQLiteAdapter(path=tmp_path / "stock_radar_run.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            await db.save_market_documents(
                "600000",
                "notice",
                [
                    {
                        "doc_uid": "cninfo:radar:1",
                        "title": "Company signs AI compute cooperation contract",
                        "summary": "Official disclosure says the company signed a major AI compute contract.",
                        "content": "Official disclosure says the company signed a major AI compute contract.",
                        "published_at": date.today().isoformat(),
                        "source": "cninfo",
                        "provider": "cninfo",
                        "source_tier": "tier_a",
                        "url": "https://static.cninfo.com.cn/finalpage/mock.pdf",
                    }
                ],
                embed=False,
            )
            result = await run_stock_radar(
                db,
                mode="dry_run",
                days=3,
                limit=10,
                allow_network=False,
                ingest_market_text=True,
            )

            assert result["success"] is True
            data = result["data"]
            assert data["candidate_count"] == 1
            assert data["candidates"][0]["event_type"] == "ai_compute_cooperation"
            assert "network_disabled" in data["degraded_flags"]
            assert "llm_unavailable_rules_only" in data["degraded_flags"]
            assert data["candidates"][0]["confirmations"]["late_session_volume"]["status"] == "disabled"
            assert data["candidates"][0]["extraction"]["llm_status"] == "unavailable"
            assert data["candidates"][0]["extraction"]["score"]["component_scores"]["risk_penalty"] >= 8
            async with db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT metadata FROM market_documents WHERE doc_uid = $1",
                    "cninfo:radar:1",
                )
            metadata = json.loads(row["metadata"])
            assert metadata["radar_pdf_parse"]["status"] == "degraded"
            assert metadata["radar_pdf_parse"]["reason"] == "network_disabled"

            push = await push_stock_radar_digest(
                db,
                {
                    "run_id": data["run"]["run_id"],
                    "channels": ["wecom"],
                    "dry_run": False,
                },
            )
            assert push["success"] is False
            assert push["error_code"] == "STOCK_RADAR_PUSH_REQUIRES_HIGH_CONFIDENCE"
            assert push["data"]["push_logs"][0]["status"] == "blocked"
        finally:
            await db.close()

    asyncio.run(_run())


def test_stock_radar_pdf_parse_success_feeds_llm_and_persists_metadata(tmp_path, monkeypatch) -> None:
    import akshare_mcp.services.stock_radar as radar_mod

    async def fake_ingest(*args, **kwargs):
        return {"totals": {}, "fetched": {}, "errors": [], "quality_flags": []}

    monkeypatch.setattr(radar_mod, "run_market_text_source_ingest", fake_ingest)
    monkeypatch.setattr(
        radar_mod,
        "_download_pdf_file",
        lambda url: {"local_pdf_path": str(tmp_path / "mock.pdf"), "checksum": "abc123", "bytes": 1234},
    )
    monkeypatch.setattr(
        radar_mod,
        "_extract_pdf_text_from_file",
        lambda path: {
            "status": "ok",
            "parser": "pymupdf",
            "text": "PDF says the company signed a 5亿元 AI compute cooperation contract with CloudCo.",
            "pages": 1,
            "text_density": 92,
        },
    )
    _patch_degraded_confirmations(monkeypatch)

    class FakeProvider:
        def is_enabled(self):
            return True

        async def call_stage(self, **kwargs):
            assert "PDF says" in kwargs["input_data"]["pdf_text"]
            return {
                "event": {
                    "event_type": "ai_compute_cooperation",
                    "direction": "positive",
                    "importance_score": 0.95,
                    "sentiment_score": 0.8,
                    "themes": ["AI", "算力"],
                    "amount_text": "5亿元",
                    "counterparties": ["CloudCo"],
                    "risk_flags": [],
                    "summary": "AI compute cooperation contract",
                    "confidence": 0.86,
                    "source_doc_uids": ["cninfo:radar:llm"],
                }
            }

    monkeypatch.setattr("akshare_mcp.services.strategy_llm_provider.get_strategy_llm_provider", lambda: FakeProvider())
    db = SQLiteAdapter(path=tmp_path / "stock_radar_pdf.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            await db.save_market_documents(
                "600000",
                "notice",
                [
                    {
                        "doc_uid": "cninfo:radar:llm",
                        "title": "Company signs AI compute cooperation contract",
                        "summary": "Official disclosure has details in PDF.",
                        "content": "Official disclosure has details in PDF.",
                        "published_at": date.today().isoformat(),
                        "source": "cninfo",
                        "provider": "cninfo",
                        "source_tier": "tier_a",
                        "url": "https://static.cninfo.com.cn/finalpage/mock.pdf",
                    }
                ],
                embed=False,
            )
            result = await run_stock_radar(
                db,
                mode="dry_run",
                days=3,
                limit=5,
                allow_network=True,
                allow_llm=True,
                ingest_market_text=True,
            )

            assert result["success"] is True
            candidate = result["data"]["candidates"][0]
            assert candidate["extraction"]["llm_status"] == "ok"
            assert candidate["extraction"]["status"] == "verified"
            assert candidate["extraction"]["confidence"] == 0.86
            assert candidate["extraction"]["score"]["component_scores"]["risk_penalty"] == 0
            async with db.acquire() as conn:
                row = await conn.fetchrow("SELECT metadata FROM market_documents WHERE doc_uid = $1", "cninfo:radar:llm")
            metadata = json.loads(row["metadata"])
            assert metadata["radar_pdf_parse"]["status"] == "ok"
            assert metadata["radar_pdf_parse"]["local_pdf_path"]
            assert "text" not in metadata["radar_pdf_parse"]
        finally:
            await db.close()

    asyncio.run(_run())


def test_stock_radar_rss_ingest_persists_documents_and_normalized_events(tmp_path, monkeypatch) -> None:
    import akshare_mcp.services.stock_radar as radar_mod

    today = date.today().isoformat()
    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>AIASK mock feed</title>
        <item>
          <title>政策支持AI算力基础设施建设</title>
          <description>财联社消息，政策继续支持AI算力和数据中心建设。</description>
          <link>https://example.test/news/ai-compute-policy</link>
          <pubDate>{today}</pubDate>
        </item>
      </channel>
    </rss>
    """.encode("utf-8")

    class FakeResponse:
        content = feed_xml
        text = feed_xml.decode("utf-8")

        def raise_for_status(self):
            return None

    def fake_get(url, timeout=None, headers=None):
        assert url == "https://rsshub.example.test/cls/telegraph"
        return FakeResponse()

    async def fake_ingest(*args, **kwargs):
        return {"totals": {"saved_docs": 0}, "fetched": {}, "errors": [], "quality_flags": []}

    monkeypatch.setenv("AIASK_RADAR_RSS_FEEDS", "https://rsshub.example.test/cls/telegraph")
    monkeypatch.setattr(radar_mod.requests, "get", fake_get)
    monkeypatch.setattr(radar_mod, "run_market_text_source_ingest", fake_ingest)
    _patch_degraded_confirmations(monkeypatch)
    db = SQLiteAdapter(path=tmp_path / "stock_radar_rss.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            result = await run_stock_radar(
                db,
                mode="dry_run",
                days=3,
                limit=10,
                allow_network=True,
                include_rss=True,
                ingest_market_text=True,
            )

            assert result["success"] is True
            assert result["data"]["candidate_count"] == 1
            candidate = result["data"]["candidates"][0]
            assert candidate["symbol"] == "MARKET"
            assert candidate["event_type"] == "policy_news"
            assert candidate["extraction"]["llm_status"] == "unavailable"
            assert "llm_unavailable_rules_only" in result["data"]["degraded_flags"]
            assert candidate["source_chain"][0]["source"] == "rsshub"
            assert "rss_feeds_not_configured" not in result["data"]["degraded_flags"]
            assert result["data"]["run"]["summary"]["allow_network"] is True
            assert result["data"]["run"]["summary"]["allow_llm"] is False
            assert result["data"]["run"]["summary"]["rss"]["documents"] == 1
            async with db.acquire() as conn:
                doc_count = await conn.fetchval("SELECT COUNT(*) FROM market_documents WHERE provider LIKE $1", "%rsshub.example.test%")
                event_count = await conn.fetchval("SELECT COUNT(*) FROM market_events_normalized WHERE provider_chain LIKE $1", "%rsshub%")
            assert doc_count == 1
            assert event_count == 1
        finally:
            await db.close()

    asyncio.run(_run())


def test_fetch_rss_feed_documents_stores_summary_only_metadata(monkeypatch) -> None:
    feed_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><item>
      <title>AI policy news</title>
      <description>Short evidence snippet only.</description>
      <link>https://example.test/rss/item</link>
      <pubDate>2026-06-08</pubDate>
    </item></channel></rss>
    """

    class FakeResponse:
        content = feed_xml

        def raise_for_status(self):
            return None

    monkeypatch.setattr("akshare_mcp.services.stock_radar.requests.get", lambda *args, **kwargs: FakeResponse())

    docs = fetch_rss_feed_documents("https://rsshub.example.test/feed", limit=5)

    assert len(docs) == 1
    assert docs[0]["source"] == "rsshub"
    assert docs[0]["source_tier"] == "tier_c"
    assert docs[0]["metadata"]["copyright_storage"] == "summary_only"
    assert docs[0]["content"] == "AI policy news Short evidence snippet only."


def test_stock_radar_llm_failure_keeps_rule_fallback(monkeypatch) -> None:
    class FailingProvider:
        def is_enabled(self):
            return True

        async def call_stage(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr("akshare_mcp.services.strategy_llm_provider.get_strategy_llm_provider", lambda: FailingProvider())
    fallback = extract_radar_event(
        {
            "doc_uid": "cninfo:fail",
            "title": "Company signs AI compute cooperation contract",
            "summary": "Official disclosure says AI compute contract.",
            "stock_code": "600000",
        }
    )
    assert fallback is not None

    extraction, meta = asyncio.run(enhance_radar_event_with_llm({"doc_uid": "cninfo:fail"}, fallback))

    assert extraction["event_type"] == "ai_compute_cooperation"
    assert extraction["status"] == "provisional"
    assert extraction["llm_status"] == "failed"
    assert meta["status"] == "failed"


def test_stock_radar_confirmation_factors_use_existing_fund_tools(monkeypatch) -> None:
    import akshare_mcp.tools.fund_flow as fund_flow_tools

    monkeypatch.setattr(
        fund_flow_tools,
        "get_stock_fund_flow",
        lambda **kwargs: {"success": True, "data": {"code": "600000", "mainNetInflow": 12_000_000, "source": "db.stock_fund_flow"}},
    )
    monkeypatch.setattr(
        fund_flow_tools,
        "get_north_fund",
        lambda **kwargs: {"success": True, "data": {"items": [{"total": 1.2}, {"total": 0.8}, {"total": -0.2}], "source": "db"}},
    )
    monkeypatch.setattr(
        fund_flow_tools,
        "get_dragon_tiger",
        lambda **kwargs: {"success": True, "data": [{"code": "600000", "netAmount": 5_000_000}]},
    )
    monkeypatch.setattr(
        fund_flow_tools,
        "get_sector_fund_flow",
        lambda **kwargs: {"success": True, "data": [{"name": "AI", "mainNetInflow": 9_000_000, "changePercent": 0.03}]},
    )
    monkeypatch.setattr(
        fund_flow_tools,
        "get_concept_fund_flow",
        lambda **kwargs: {"success": True, "data": [{"name": "算力", "mainNetInflow": 8_000_000, "changePercent": 0.02}]},
    )

    class NoMinuteDb:
        class _Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def fetchval(self, *args, **kwargs):
                return None

        def acquire(self):
            return self._Conn()

    confirmations = asyncio.run(
        _confirmation_factors(
            NoMinuteDb(),
            "600000",
            {"direction": "positive", "themes": ["AI", "算力"]},
        )
    )

    assert confirmations["fund_flow"]["confirmed"] is True
    assert confirmations["north_fund"]["confirmed"] is True
    assert confirmations["dragon_tiger"]["confirmed"] is True
    assert confirmations["sector_heat"]["score"] > 0
    assert confirmations["late_session_volume"]["status"] == "disabled"


def test_stock_radar_late_session_volume_confirms_when_minute_data_available(tmp_path, monkeypatch) -> None:
    import akshare_mcp.tools.fund_flow as fund_flow_tools

    monkeypatch.setattr(fund_flow_tools, "get_stock_fund_flow", lambda **kwargs: {"success": True, "data": {}})
    monkeypatch.setattr(fund_flow_tools, "get_north_fund", lambda **kwargs: {"success": True, "data": []})
    monkeypatch.setattr(fund_flow_tools, "get_dragon_tiger", lambda **kwargs: {"success": True, "data": []})
    monkeypatch.setattr(fund_flow_tools, "get_sector_fund_flow", lambda **kwargs: {"success": True, "data": []})
    monkeypatch.setattr(fund_flow_tools, "get_concept_fund_flow", lambda **kwargs: {"success": True, "data": []})

    db = SQLiteAdapter(path=tmp_path / "stock_radar_minute.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            rows = [
                ("600000", "2026-06-08 14:00", 100.0, 10.00),
                ("600000", "2026-06-08 14:10", 100.0, 10.02),
                ("600000", "2026-06-08 14:20", 100.0, 10.04),
                ("600000", "2026-06-08 14:25", 100.0, 10.06),
                ("600000", "2026-06-08 14:30", 250.0, 10.08),
                ("600000", "2026-06-08 14:40", 250.0, 10.12),
                ("600000", "2026-06-08 14:50", 250.0, 10.18),
                ("600000", "2026-06-08 15:00", 250.0, 10.22),
            ]
            async with db.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE stock_minute_bars (
                        code TEXT,
                        trade_time TEXT,
                        amount REAL,
                        close REAL
                    )
                    """
                )
                for row in rows:
                    await conn.execute(
                        "INSERT INTO stock_minute_bars (code, trade_time, amount, close) VALUES ($1, $2, $3, $4)",
                        *row,
                    )

            confirmations = await _confirmation_factors(
                db,
                "600000",
                {"direction": "positive", "themes": ["AI", "算力"]},
            )

            late = confirmations["late_session_volume"]
            assert late["status"] == "ok"
            assert late["confirmed"] is True
            assert late["source"] == "stock_minute_bars"
            assert late["amount_ratio"] >= 2.5
            assert late["price_direction"] > 0
        finally:
            await db.close()

    asyncio.run(_run())
